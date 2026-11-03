"""
QuoteService — single source of truth for all live price lookups.

Every quote access in the strategy code (condor_runner, shift_monitor, guard_layers)
must go through this service.  It provides:

    1. Freshness guarantee — quotes older than max_age_seconds are never returned;
       the caller gets QuoteServiceError instead of a stale price.

    2. received_at tracking — all quotes carry a local monotonic timestamp so that
       QuoteGuard age checks are based on when WE got the data, not when the exchange
       last touched the packet (exchange timestamps for illiquid OTM options can be
       5–15 min old even with a live order book).

    3. KiteTicker primary feed — if api_key + access_token are supplied, starts a
       WebSocket in MODE_FULL.  Ticks update the in-memory cache with received_at.
       The watchdog thread reconnects if no tick arrives in watchdog_timeout_s.

    4. REST fallback with token-bucket rate limiting — when the ticker is absent or
       the cache is cold, falls back to kite.quote() REST calls.  A TokenBucket limits
       calls to rest_rps (default 2/s) to avoid Zerodha 429s.

    5. Pre-trade refresh — pre_trade_refresh() forces a REST round-trip for the listed
       instruments, refreshing received_at, then validates freshness.

    6. Fail-closed — get() / get_many() raise QuoteServiceError if a fresh quote
       cannot be obtained; they never return a stale one silently.

    7. Market-hours gate — requests outside 09:00–15:30 IST raise MarketClosedError
       unless allow_outside_hours=True is passed.

    8. Metrics — simple counters accessible via .metrics property.

Usage::

    from live.quote_service import QuoteService, QuoteServiceError

    svc = QuoteService(
        broker,
        api_key="xxx",       # optional — enables KiteTicker
        access_token="yyy",  # optional
        max_age_seconds=8.0,
        pretrade_age_seconds=3.0,
    )
    svc.start()                          # starts ticker + watchdog threads
    svc.ensure_subscribed(instruments)   # subscribe tokens to live feed

    quotes = svc.get_many(instruments)   # raises on stale/missing
    svc.pre_trade_refresh(instruments)   # force REST before placing orders
    svc.stop()                           # clean shutdown
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from brokers.base import BrokerAdapter, QuoteError
from execution.models import Quote

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Market session boundaries (IST)
_MARKET_OPEN_H, _MARKET_OPEN_M = 9, 0
_MARKET_CLOSE_H, _MARKET_CLOSE_M = 15, 30

# Watchdog: reconnect if no tick in this many seconds
_DEFAULT_WATCHDOG_TIMEOUT_S = 30.0
# REST batch size matches zerodha.py
_REST_BATCH_SIZE = 400


class QuoteServiceError(Exception):
    """Raised when a fresh quote cannot be obtained."""


class MarketClosedError(QuoteServiceError):
    """Raised when a quote is requested outside market hours."""


# ── Token bucket for REST rate limiting ───────────────────────────────────────

class _TokenBucket:
    """
    Leaky-bucket rate limiter.  consume() blocks until a token is available.
    """

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate          # tokens per second
        self._capacity = capacity  # max burst
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self._rate
            time.sleep(wait)


# ── QuoteService ──────────────────────────────────────────────────────────────

class QuoteService:
    """Central quote hub.  See module docstring for full description."""

    def __init__(
        self,
        broker: BrokerAdapter,
        *,
        api_key: Optional[str] = None,
        access_token: Optional[str] = None,
        max_age_seconds: float = 8.0,
        pretrade_age_seconds: float = 3.0,
        rest_rps: float = 2.0,
        watchdog_timeout_s: float = _DEFAULT_WATCHDOG_TIMEOUT_S,
    ) -> None:
        self._broker = broker
        self._api_key = api_key
        self._access_token = access_token
        self._max_age = max_age_seconds
        self._pretrade_age = pretrade_age_seconds
        self._watchdog_timeout = watchdog_timeout_s

        # In-memory quote cache: token → Quote (with received_at set)
        self._cache: Dict[int, Quote] = {}
        self._cache_lock = threading.Lock()

        # Subscribed tokens (for ticker and re-subscription on reconnect)
        self._subscribed: List[Tuple[int, str]] = []
        self._subscribed_lock = threading.Lock()

        # Token bucket for REST calls
        self._bucket = _TokenBucket(rate=rest_rps, capacity=rest_rps * 2)

        # KiteTicker state
        self._ticker = None
        self._ticker_connected = False
        self._last_tick_at: float = 0.0  # monotonic

        # Threads
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Metrics counters
        self._m_cache_hits = 0
        self._m_rest_fetches = 0
        self._m_stale_evictions = 0
        self._m_ticker_ticks = 0
        self._m_pretrade_refreshes = 0
        self._m_errors = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the ticker (if credentials available) and watchdog thread."""
        self._stop_event.clear()
        self._maybe_start_ticker()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="quote-service-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        logger.info(
            "quote_service_started",
            extra={
                "ticker_enabled": self._ticker is not None,
                "max_age_s": self._max_age,
                "pretrade_age_s": self._pretrade_age,
            },
        )

    def stop(self) -> None:
        """Stop watchdog and disconnect ticker."""
        self._stop_event.set()
        if self._ticker is not None:
            try:
                self._ticker.stop()
            except Exception:
                pass
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=5.0)
        logger.info("quote_service_stopped")

    # ── Subscription ─────────────────────────────────────────────────────────

    def ensure_subscribed(self, instruments: List[Tuple[int, str]]) -> None:
        """
        Register instruments for the live feed.  New tokens are added to the
        ticker subscription; already-subscribed tokens are left unchanged.
        Returns immediately in REST-only mode (no ticker).
        """
        with self._subscribed_lock:
            existing_tokens = {tok for tok, _ in self._subscribed}
            new_pairs = [(tok, sym) for tok, sym in instruments if tok not in existing_tokens]
            if not new_pairs:
                return
            self._subscribed.extend(new_pairs)

        new_tokens = [tok for tok, _ in new_pairs]
        logger.info("quote_service_subscribing", extra={"new_tokens": len(new_tokens)})

        if self._ticker is not None and self._ticker_connected:
            try:
                self._ticker.subscribe(new_tokens)
                self._ticker.set_mode(self._ticker.MODE_FULL, new_tokens)
            except Exception as exc:
                logger.warning(
                    "ticker_subscribe_failed",
                    extra={"error": str(exc), "tokens": new_tokens[:10]},
                )

    # ── Quote access ──────────────────────────────────────────────────────────

    def get(
        self,
        instrument_token: int,
        trading_symbol: str,
        *,
        allow_outside_hours: bool = False,
    ) -> Quote:
        """
        Return a fresh Quote for a single instrument.
        Raises QuoteServiceError if no fresh quote can be obtained.
        Raises MarketClosedError if called outside market hours.
        """
        if not allow_outside_hours:
            self._check_market_hours()

        # Try cache first
        quote = self._get_cached(instrument_token)
        if quote is not None:
            self._m_cache_hits += 1
            return quote

        # Cache miss — REST fetch
        quotes = self._rest_fetch([(instrument_token, trading_symbol)])
        q = quotes.get(instrument_token)
        if q is None:
            self._m_errors += 1
            raise QuoteServiceError(
                f"Quote unavailable for {trading_symbol} (token {instrument_token})"
            )
        return q

    def get_many(
        self,
        instruments: List[Tuple[int, str]],
        *,
        allow_outside_hours: bool = False,
    ) -> Dict[int, Quote]:
        """
        Return fresh Quotes for all instruments.
        Any instrument without a fresh quote raises QuoteServiceError.
        """
        if not allow_outside_hours:
            self._check_market_hours()

        result: Dict[int, Quote] = {}
        missing: List[Tuple[int, str]] = []

        for tok, sym in instruments:
            q = self._get_cached(tok)
            if q is not None:
                self._m_cache_hits += 1
                result[tok] = q
            else:
                missing.append((tok, sym))

        if missing:
            fetched = self._rest_fetch(missing)
            for tok, sym in missing:
                q = fetched.get(tok)
                if q is None:
                    self._m_errors += 1
                    raise QuoteServiceError(
                        f"Quote unavailable for {sym} (token {tok})"
                    )
                result[tok] = q

        return result

    def pre_trade_refresh(
        self, instruments: List[Tuple[int, str]]
    ) -> Dict[int, Quote]:
        """
        Force a REST round-trip for all instruments, update the cache, and
        validate that every quote is fresh enough for order placement.
        Raises QuoteServiceError if any quote is older than pretrade_age_seconds.
        """
        self._m_pretrade_refreshes += 1
        fetched = self._rest_fetch(instruments, force=True)

        stale = []
        for tok, sym in instruments:
            q = fetched.get(tok)
            if q is None:
                raise QuoteServiceError(f"Pre-trade refresh: no quote for {sym}")
            if q.received_at is None or (time.monotonic() - q.received_at) > self._pretrade_age:
                stale.append(sym)

        if stale:
            raise QuoteServiceError(
                f"Pre-trade refresh: quotes still stale after REST fetch: {stale}"
            )
        return fetched

    # ── Market hours ─────────────────────────────────────────────────────────

    def is_market_hours(self) -> bool:
        """True if current IST time is within 09:00–15:30."""
        now = datetime.now(IST)
        open_mins = _MARKET_OPEN_H * 60 + _MARKET_OPEN_M
        close_mins = _MARKET_CLOSE_H * 60 + _MARKET_CLOSE_M
        current_mins = now.hour * 60 + now.minute
        return open_mins <= current_mins <= close_mins

    # ── Metrics ───────────────────────────────────────────────────────────────

    @property
    def metrics(self) -> Dict[str, int]:
        return {
            "cache_hits": self._m_cache_hits,
            "rest_fetches": self._m_rest_fetches,
            "stale_evictions": self._m_stale_evictions,
            "ticker_ticks": self._m_ticker_ticks,
            "pretrade_refreshes": self._m_pretrade_refreshes,
            "errors": self._m_errors,
        }

    # ── Internal: cache ───────────────────────────────────────────────────────

    def _get_cached(self, token: int) -> Optional[Quote]:
        """
        Return cached Quote if it exists and is fresh; None otherwise.
        Evicts stale entries.
        """
        with self._cache_lock:
            q = self._cache.get(token)
        if q is None:
            return None
        age = (
            time.monotonic() - q.received_at
            if q.received_at is not None
            else float("inf")
        )
        if age > self._max_age:
            with self._cache_lock:
                self._cache.pop(token, None)
            self._m_stale_evictions += 1
            return None
        return q

    def _put_cache(self, quotes: Dict[int, Quote]) -> None:
        with self._cache_lock:
            self._cache.update(quotes)

    # ── Internal: REST fetch ──────────────────────────────────────────────────

    def _rest_fetch(
        self,
        instruments: List[Tuple[int, str]],
        *,
        force: bool = False,
    ) -> Dict[int, Quote]:
        """
        Fetch quotes via REST.  Rate-limited by token bucket.
        Updates cache.  Returns token → Quote.
        """
        if not instruments:
            return {}

        self._bucket.consume()
        self._m_rest_fetches += 1

        try:
            raw = self._broker.get_quotes(instruments)
        except QuoteError as exc:
            self._m_errors += 1
            logger.warning("quote_service_rest_error", extra={"error": str(exc)})
            raise QuoteServiceError(f"REST quote fetch failed: {exc}") from exc

        # Stamp received_at on any quote that doesn't already have it (e.g. PaperBroker)
        now_mono = time.monotonic()
        result: Dict[int, Quote] = {}
        for tok, q in raw.items():
            if q.received_at is None:
                q = q.model_copy(update={"received_at": now_mono})
            result[tok] = q

        self._put_cache(result)
        return result

    # ── Internal: market hours ────────────────────────────────────────────────

    def _check_market_hours(self) -> None:
        if not self.is_market_hours():
            now = datetime.now(IST)
            raise MarketClosedError(
                f"Market closed at {now.strftime('%H:%M')} IST "
                f"(allowed 09:00–15:30)"
            )

    # ── Internal: KiteTicker ──────────────────────────────────────────────────

    def _maybe_start_ticker(self) -> None:
        """Start KiteTicker in FULL mode if credentials were supplied."""
        if not self._api_key or not self._access_token:
            logger.info("quote_service_ticker_disabled_no_credentials")
            return

        try:
            from kiteconnect import KiteTicker  # type: ignore[import]
        except ImportError:
            logger.info("quote_service_ticker_disabled_no_kiteticker")
            return

        ticker = KiteTicker(self._api_key, self._access_token)
        ticker.on_ticks = self._on_ticks
        ticker.on_connect = self._on_ticker_connect
        ticker.on_close = self._on_ticker_close
        ticker.on_error = self._on_ticker_error
        ticker.on_reconnect = self._on_ticker_reconnect

        self._ticker = ticker
        # connect() is non-blocking; it starts an internal thread
        ticker.connect(threaded=True)
        logger.info("quote_service_ticker_connecting")

    def _on_ticks(self, _ws: object, ticks: list) -> None:
        now_mono = time.monotonic()
        now_ist = datetime.now(IST)
        self._last_tick_at = now_mono

        updates: Dict[int, Quote] = {}
        for tick in ticks:
            token = tick.get("instrument_token")
            if token is None:
                continue
            depth = tick.get("depth", {})
            bids = depth.get("buy", [])
            asks = depth.get("sell", [])
            bid = Decimal(str(bids[0]["price"])) if bids else Decimal(str(tick.get("last_price", 0)))
            bid_qty = int(bids[0]["quantity"]) if bids else 0
            ask = Decimal(str(asks[0]["price"])) if asks else Decimal(str(tick.get("last_price", 0)))
            ask_qty = int(asks[0]["quantity"]) if asks else 0
            ltp = Decimal(str(tick.get("last_price", 0)))

            q = Quote(
                instrument_token=token,
                trading_symbol=tick.get("tradingsymbol", ""),
                exchange=tick.get("exchange", "NFO"),
                timestamp=now_ist,
                ltp=ltp,
                bid=bid,
                bid_qty=bid_qty,
                ask=ask,
                ask_qty=ask_qty,
                volume=int(tick.get("volume_traded", 0)),
                oi=int(tick.get("oi", 0)),
                received_at=now_mono,
            )
            updates[token] = q
            self._m_ticker_ticks += 1

        if updates:
            self._put_cache(updates)

    def _on_ticker_connect(self, _ws: object, _response: object) -> None:
        self._ticker_connected = True
        self._last_tick_at = time.monotonic()
        logger.info("quote_service_ticker_connected")
        # Re-subscribe all known tokens after (re)connect
        with self._subscribed_lock:
            tokens = [tok for tok, _ in self._subscribed]
        if tokens and self._ticker is not None:
            try:
                self._ticker.subscribe(tokens)
                self._ticker.set_mode(self._ticker.MODE_FULL, tokens)
                logger.info(
                    "quote_service_ticker_subscribed",
                    extra={"token_count": len(tokens)},
                )
            except Exception as exc:
                logger.warning("ticker_resubscribe_failed", extra={"error": str(exc)})

    def _on_ticker_close(self, _ws: object, code: int, reason: str) -> None:
        self._ticker_connected = False
        logger.warning(
            "quote_service_ticker_closed",
            extra={"code": code, "reason": reason},
        )

    def _on_ticker_error(self, _ws: object, code: int, reason: str) -> None:
        logger.error(
            "quote_service_ticker_error",
            extra={"code": code, "reason": reason},
        )

    def _on_ticker_reconnect(self, _ws: object, attempts_count: int) -> None:
        logger.info(
            "quote_service_ticker_reconnecting",
            extra={"attempt": attempts_count},
        )

    # ── Internal: watchdog ────────────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        """
        Runs in a daemon thread.  If the ticker is connected but no tick has
        arrived within watchdog_timeout_s, forcibly reconnects.
        In REST-only mode, logs a heartbeat every 60 s.
        """
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=10.0)
            if self._stop_event.is_set():
                break

            if self._ticker is not None and self._ticker_connected:
                silence = time.monotonic() - self._last_tick_at
                if silence > self._watchdog_timeout:
                    logger.warning(
                        "quote_service_watchdog_reconnect",
                        extra={"silence_s": round(silence, 1)},
                    )
                    try:
                        self._ticker.reconnect()
                    except Exception as exc:
                        logger.error(
                            "quote_service_watchdog_reconnect_failed",
                            extra={"error": str(exc)},
                        )
