"""
Zerodha Kite Connect broker adapter — READ ONLY.
No order placement methods. Phase 5 adds those behind the safety gate.

Instrument master is cached locally to avoid redundant downloads.
Cache is refreshed if older than CACHE_MAX_AGE_HOURS (default 8).
"""
from __future__ import annotations

import csv
import logging
import os
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

try:
    from kiteconnect import KiteConnect
except ImportError:  # pragma: no cover
    KiteConnect = None  # type: ignore[assignment,misc]

from brokers.base import BrokerAdapter, BrokerError, InstrumentNotFoundError, QuoteError
from execution.models import DepthLevel, OptionType, Quote, ResolvedInstrument, Side

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

CACHE_MAX_AGE_HOURS = 8
QUOTE_BATCH_SIZE = 400        # well under Zerodha's 500 limit
QUOTE_BATCH_DELAY = 0.35      # seconds between batches


class LiveTradingBlockedError(BrokerError):
    """Raised when live order placement is attempted without proper confirmation."""
    pass


class ZerodhaBroker(BrokerAdapter):
    """
    Read-only Zerodha adapter.
    Wraps KiteConnect. Handles instrument resolution, quotes, depth,
    positions, orders, and margin queries.
    """

    def __init__(
        self,
        api_key: str,
        access_token: str,
        instrument_cache_path: str = "logs/nfo_instruments_cache.csv",
    ) -> None:
        # Never log api_key or access_token
        if not api_key or not access_token:
            raise BrokerError("api_key and access_token are required")
        if KiteConnect is None:
            raise BrokerError(
                "kiteconnect package is not installed. "
                "Install it with: pip install kiteconnect"
            )
        self._kite = KiteConnect(api_key=api_key)
        self._kite.set_access_token(access_token)
        self._cache_path = Path(instrument_cache_path)
        self._instruments: Optional[list[dict]] = None  # loaded lazily
        logger.info("zerodha_broker_initialized")

    # ── Instrument master ──────────────────────────────────────────────────

    def _load_instruments(self, force_refresh: bool = False) -> list[dict]:
        """
        Load NFO instrument master. Uses cache if fresh enough.
        Refreshes from API if cache is missing, stale, or force_refresh=True.
        """
        if self._instruments is not None and not force_refresh:
            return self._instruments

        if self._cache_is_fresh() and not force_refresh:
            logger.info("instrument_cache_hit", extra={"path": str(self._cache_path)})
            self._instruments = self._read_cache()
            return self._instruments

        logger.info("instrument_cache_refresh", extra={"path": str(self._cache_path)})
        try:
            raw = self._kite.instruments("NFO")
        except Exception as exc:
            raise BrokerError(f"Failed to fetch NFO instruments: {exc}") from exc

        self._write_cache(raw)
        self._instruments = raw
        logger.info("instrument_master_loaded", extra={"count": len(raw)})
        return self._instruments

    def _cache_is_fresh(self) -> bool:
        if not self._cache_path.exists():
            return False
        age = datetime.now(IST) - datetime.fromtimestamp(
            self._cache_path.stat().st_mtime, tz=IST
        )
        return age < timedelta(hours=CACHE_MAX_AGE_HOURS)

    def _write_cache(self, instruments: list[dict]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not instruments:
            return
        fieldnames = list(instruments[0].keys())
        with open(self._cache_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(instruments)
        logger.debug("instrument_cache_written", extra={"rows": len(instruments)})

    def _read_cache(self) -> list[dict]:
        rows = []
        with open(self._cache_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        logger.debug("instrument_cache_read", extra={"rows": len(rows)})
        return rows

    # ── BrokerAdapter: resolve_instrument ─────────────────────────────────

    def resolve_instrument(
        self,
        underlying: str,
        exchange: str,
        expiry: date,
        strike: Decimal,
        option_type: str,
    ) -> ResolvedInstrument:
        """
        Find the exact instrument matching the given parameters.
        Matches on: underlying name, exchange, expiry date, strike, CE/PE.
        Raises InstrumentNotFoundError if not found.
        """
        instruments = self._load_instruments()
        opt_type_upper = option_type.upper()

        candidates = []
        for inst in instruments:
            # instrument_type is CE or PE for options
            if inst.get("instrument_type", "") not in ("CE", "PE"):
                continue
            if inst.get("instrument_type", "") != opt_type_upper:
                continue

            # underlying name: 'name' field in Kite instruments
            inst_name = str(inst.get("name", "")).upper()
            if inst_name != underlying.upper():
                continue

            # expiry: comes as date or string "YYYY-MM-DD"
            inst_expiry = inst.get("expiry")
            if isinstance(inst_expiry, str):
                try:
                    inst_expiry = date.fromisoformat(inst_expiry)
                except ValueError:
                    continue
            elif hasattr(inst_expiry, "date"):
                inst_expiry = inst_expiry.date()

            if inst_expiry != expiry:
                continue

            # strike: comes as float or string
            try:
                inst_strike = Decimal(str(inst.get("strike", 0)))
            except Exception:
                continue

            if inst_strike != strike:
                continue

            candidates.append(inst)

        if not candidates:
            raise InstrumentNotFoundError(
                f"No instrument found: {underlying} {expiry} {strike} {option_type} on {exchange}"
            )

        if len(candidates) > 1:
            logger.warning(
                "multiple_instruments_found",
                extra={"underlying": underlying, "expiry": str(expiry),
                       "strike": str(strike), "option_type": option_type,
                       "count": len(candidates)},
            )

        inst = candidates[0]
        try:
            lot_size = int(inst.get("lot_size", 0))
            tick_size = Decimal(str(inst.get("tick_size", "0.05")))
            instrument_token = int(inst.get("instrument_token", 0))
        except (ValueError, TypeError) as exc:
            raise BrokerError(f"Malformed instrument data: {exc}") from exc

        if lot_size == 0:
            raise BrokerError(f"Lot size is 0 for {inst.get('tradingsymbol')}")

        resolved = ResolvedInstrument(
            instrument_token=instrument_token,
            trading_symbol=str(inst.get("tradingsymbol", "")),
            exchange=exchange,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=OptionType(opt_type_upper),
            lot_size=lot_size,
            tick_size=tick_size,
        )
        logger.info(
            "instrument_resolved",
            extra={
                "symbol": resolved.trading_symbol,
                "token": resolved.instrument_token,
                "lot_size": lot_size,
            },
        )
        return resolved

    # ── BrokerAdapter: quotes ──────────────────────────────────────────────

    def get_quote(self, instrument_token: int, trading_symbol: str) -> Quote:
        quotes = self.get_quotes([(instrument_token, trading_symbol)])
        if instrument_token not in quotes:
            raise QuoteError(f"No quote returned for {trading_symbol}")
        return quotes[instrument_token]

    def get_quotes(
        self, instruments: list[tuple[int, str]]
    ) -> dict[int, Quote]:
        """
        Batch quote fetch. Handles >QUOTE_BATCH_SIZE instruments by splitting.
        Returns dict keyed by instrument_token.
        Never logs access_token.
        """
        if not instruments:
            return {}

        # Build list of "EXCHANGE:SYMBOL" keys for kite.quote()
        # We use the trading symbol directly; exchange is NFO for options
        symbol_keys = [f"NFO:{sym}" for _, sym in instruments]
        token_map = {sym: tok for tok, sym in instruments}

        result: dict[int, Quote] = {}

        # Split into batches
        for batch_start in range(0, len(symbol_keys), QUOTE_BATCH_SIZE):
            batch = symbol_keys[batch_start : batch_start + QUOTE_BATCH_SIZE]
            if batch_start > 0:
                time.sleep(QUOTE_BATCH_DELAY)
            try:
                raw = self._kite.quote(batch)
            except Exception as exc:
                raise QuoteError(f"Zerodha quote API error: {exc}") from exc

            for key, data in raw.items():
                # key is "NFO:TRADINGSYMBOL"
                sym = key.split(":", 1)[-1]
                token = token_map.get(sym)
                if token is None:
                    continue
                try:
                    quote = self._parse_quote(token, sym, data)
                    result[token] = quote
                except Exception as exc:
                    logger.warning(
                        "quote_parse_error",
                        extra={"symbol": sym, "error": str(exc)},
                    )

        missing = [sym for tok, sym in instruments if tok not in result]
        if missing:
            logger.warning("quotes_missing", extra={"symbols": missing})

        return result

    def _parse_quote(self, token: int, trading_symbol: str, data: dict) -> Quote:
        """Parse a single Zerodha quote dict into a Quote model."""
        now = datetime.now(IST)

        # Zerodha quote timestamp
        ts = data.get("timestamp")
        if ts is not None:
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts).replace(tzinfo=IST)
                except ValueError:
                    ts = now
            elif hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
        else:
            ts = now

        depth_data = data.get("depth", {})
        depth_bids = [
            DepthLevel(
                price=Decimal(str(d.get("price", 0))),
                quantity=int(d.get("quantity", 0)),
                orders=int(d.get("orders", 0)),
            )
            for d in depth_data.get("buy", [])
        ]
        depth_asks = [
            DepthLevel(
                price=Decimal(str(d.get("price", 0))),
                quantity=int(d.get("quantity", 0)),
                orders=int(d.get("orders", 0)),
            )
            for d in depth_data.get("sell", [])
        ]

        # Best bid/ask from depth, fallback to ohlc if depth is empty
        if depth_bids:
            best_bid = depth_bids[0].price
            best_bid_qty = depth_bids[0].quantity
        else:
            best_bid = Decimal(str(data.get("last_price", 0)))
            best_bid_qty = 0

        if depth_asks:
            best_ask = depth_asks[0].price
            best_ask_qty = depth_asks[0].quantity
        else:
            best_ask = Decimal(str(data.get("last_price", 0)))
            best_ask_qty = 0

        return Quote(
            instrument_token=token,
            trading_symbol=trading_symbol,
            exchange="NFO",
            timestamp=ts,
            ltp=Decimal(str(data.get("last_price", 0))),
            bid=best_bid,
            bid_qty=best_bid_qty,
            ask=best_ask,
            ask_qty=best_ask_qty,
            volume=int(data.get("volume", 0)),
            oi=int(data.get("oi", 0)),
            depth_bids=depth_bids,
            depth_asks=depth_asks,
        )

    # ── BrokerAdapter: account data ────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        try:
            pos = self._kite.positions()
            # 'net' gives net positions (day + overnight)
            return pos.get("net", [])
        except Exception as exc:
            raise BrokerError(f"Failed to fetch positions: {exc}") from exc

    def get_orders(self) -> list[dict]:
        try:
            return self._kite.orders() or []
        except Exception as exc:
            raise BrokerError(f"Failed to fetch orders: {exc}") from exc

    def get_margins(self) -> dict:
        try:
            return self._kite.margins() or {}
        except Exception as exc:
            raise BrokerError(f"Failed to fetch margins: {exc}") from exc

    def get_basket_margin(self, orders: list[dict]) -> dict:
        """
        Calculate margin for a basket of orders using Zerodha's margin endpoint.
        orders: list of dicts matching Zerodha's basket_order_margins format.
        Returns margin dict with 'required', 'available', 'charges' etc.
        Does NOT place any orders.
        """
        try:
            result = self._kite.basket_order_margins(orders, consider_positions=False)
            logger.info("basket_margin_calculated", extra={
                "required": result.get("required", "N/A"),
                "charges": result.get("charges", {}).get("total", "N/A"),
            })
            return result
        except Exception as exc:
            raise BrokerError(f"Basket margin calculation failed: {exc}") from exc

    def refresh_instruments(self) -> int:
        """Force refresh the instrument cache from API. Returns count of instruments."""
        instruments = self._load_instruments(force_refresh=True)
        return len(instruments)

    # ── Live order safety gate ─────────────────────────────────────────────

    def _assert_live_mode(self) -> None:
        """
        Check that BOTH required environment variables are set correctly.
        Called at the start of every order-placement method.
        Raises LiveTradingBlockedError if either check fails.
        """
        mode = os.environ.get("TRADING_MODE", "PAPER").upper()
        confirmation = os.environ.get("LIVE_TRADING_CONFIRMATION", "")
        required_confirmation = "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED"

        if mode != "LIVE":
            raise LiveTradingBlockedError(
                f"TRADING_MODE is '{mode}', not 'LIVE'. Live orders blocked."
            )
        if confirmation != required_confirmation:
            raise LiveTradingBlockedError(
                "LIVE_TRADING_CONFIRMATION is not set correctly. Live orders blocked.\n"
                f"Required: LIVE_TRADING_CONFIRMATION={required_confirmation}"
            )
        logger.info("live_mode_confirmed")

    # ── Live order placement methods ───────────────────────────────────────

    def place_order(
        self,
        trading_symbol: str,
        exchange: str,
        transaction_type: str,   # "BUY" or "SELL"
        quantity: int,
        price: Decimal,
        product: str = "NRML",
        tag: str = "",
    ) -> str:
        """
        Place a LIMIT NRML order on Zerodha.
        Requires TRADING_MODE=LIVE and LIVE_TRADING_CONFIRMATION to be set.
        Returns broker_order_id (string).
        NEVER places market orders. Uses NRML product for overnight F&O positions.
        """
        self._assert_live_mode()

        if price <= 0:
            raise BrokerError(f"Invalid limit price: {price}. Only LIMIT orders allowed.")

        params = {
            "exchange": exchange,
            "tradingsymbol": trading_symbol,
            "transaction_type": transaction_type.upper(),
            "quantity": quantity,
            "product": product,
            "order_type": "LIMIT",
            "price": float(price),
            "validity": "DAY",
            "tag": tag[:20] if tag else "",   # Zerodha tag max 20 chars
        }

        logger.info(
            "live_order_placing",
            extra={
                "symbol": trading_symbol,
                "side": transaction_type,
                "quantity": quantity,
                "price": str(price),
                "product": product,
            },
        )

        try:
            order_id = self._kite.place_order(
                variety=self._kite.VARIETY_REGULAR,
                **params,
            )
        except Exception as exc:
            logger.error("live_order_failed", extra={"error": str(exc), "symbol": trading_symbol})
            raise BrokerError(f"Order placement failed for {trading_symbol}: {exc}") from exc

        logger.info(
            "live_order_placed",
            extra={"broker_order_id": str(order_id), "symbol": trading_symbol},
        )
        return str(order_id)

    def place_order_from_model(self, order: "Order") -> str:  # noqa: F821
        """
        Place a NRML limit order from an execution.models.Order instance.
        Returns broker_order_id string. Delegates to place_order() after
        extracting required fields. Never bypasses live-mode gate.
        """
        from execution.models import Side
        transaction_type = "BUY" if order.side == Side.BUY else "SELL"
        tag = f"lc_{order.strategy_run_id[:12]}" if order.strategy_run_id else ""
        return self.place_order(
            trading_symbol=order.trading_symbol,
            exchange="NFO",
            transaction_type=transaction_type,
            quantity=order.quantity,
            price=order.limit_price,
            product="NRML",
            tag=tag,
        )

    def modify_order(
        self,
        broker_order_id: str,
        new_price: Decimal,
        quantity: Optional[int] = None,
    ) -> str:
        """
        Modify an existing open LIMIT order's price.
        Requires live mode confirmation.
        """
        self._assert_live_mode()

        if new_price <= 0:
            raise BrokerError(f"Invalid modification price: {new_price}")

        params: dict = {
            "variety": self._kite.VARIETY_REGULAR,
            "order_id": broker_order_id,
            "price": float(new_price),
            "order_type": "LIMIT",
        }
        if quantity is not None:
            params["quantity"] = quantity

        logger.info(
            "live_order_modifying",
            extra={"broker_order_id": broker_order_id, "new_price": str(new_price)},
        )

        try:
            updated_id = self._kite.modify_order(**params)
        except Exception as exc:
            logger.error("live_order_modify_failed",
                         extra={"broker_order_id": broker_order_id, "error": str(exc)})
            raise BrokerError(f"Order modification failed: {exc}") from exc

        logger.info("live_order_modified", extra={"broker_order_id": str(updated_id)})
        return str(updated_id)

    def cancel_order(self, broker_order_id: str) -> str:
        """
        Cancel an open order.
        Requires live mode confirmation.
        """
        self._assert_live_mode()

        logger.info("live_order_cancelling", extra={"broker_order_id": broker_order_id})

        try:
            cancelled_id = self._kite.cancel_order(
                variety=self._kite.VARIETY_REGULAR,
                order_id=broker_order_id,
            )
        except Exception as exc:
            logger.error("live_order_cancel_failed",
                         extra={"broker_order_id": broker_order_id, "error": str(exc)})
            raise BrokerError(f"Order cancellation failed: {exc}") from exc

        logger.info("live_order_cancelled", extra={"broker_order_id": str(cancelled_id)})
        return str(cancelled_id)

    def get_order_status(self, broker_order_id: str) -> "OrderStatus":  # noqa: F821
        """
        Fetch current OrderStatus enum for a specific order by broker_order_id.
        Read-only — no live-mode confirmation required.
        Maps Zerodha status strings to execution.models.OrderStatus.
        """
        from execution.models import OrderStatus
        _STATUS_MAP: dict[str, "OrderStatus"] = {
            "OPEN": OrderStatus.OPEN,
            "COMPLETE": OrderStatus.COMPLETE,
            "CANCELLED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "TRIGGER PENDING": OrderStatus.OPEN,
            "AMO REQ RECEIVED": OrderStatus.PENDING,
            "PUT ORDER REQ RECEIVED": OrderStatus.PENDING,
            "VALIDATION PENDING": OrderStatus.PENDING,
            "OPEN PENDING": OrderStatus.PENDING,
            "MODIFY VALIDATION PENDING": OrderStatus.OPEN,
            "MODIFY PENDING": OrderStatus.OPEN,
            "CANCEL PENDING": OrderStatus.OPEN,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "UPDATE": OrderStatus.OPEN,
        }
        try:
            orders = self._kite.order_history(broker_order_id)
            if not orders:
                raise BrokerError(f"No order history for {broker_order_id}")
            latest = orders[-1]
            raw_status = str(latest.get("status", "")).upper()
            return _STATUS_MAP.get(raw_status, OrderStatus.PENDING)
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"Order status fetch failed: {exc}") from exc

    def get_order_status_raw(self, broker_order_id: str) -> dict:
        """
        Fetch raw order dict from Zerodha order history.
        Returns the most recent event dict for the given order ID.
        """
        try:
            orders = self._kite.order_history(broker_order_id)
            if not orders:
                raise BrokerError(f"No order history for {broker_order_id}")
            return orders[-1]
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"Order status fetch failed: {exc}") from exc

    def cancel_all_open_orders(self) -> list[str]:
        """
        Cancel all open orders for today.
        Requires live mode confirmation.
        Returns list of cancelled order IDs.
        """
        self._assert_live_mode()
        orders = self.get_orders()
        cancelled = []
        for order in orders:
            status = order.get("status", "")
            if status in ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED"):
                oid = str(order.get("order_id", ""))
                try:
                    self.cancel_order(oid)
                    cancelled.append(oid)
                except BrokerError as exc:
                    logger.error("cancel_all_failed_for", extra={"order_id": oid, "error": str(exc)})
        logger.info("cancel_all_complete", extra={"cancelled_count": len(cancelled)})
        return cancelled

    # ── Historical OHLC data ───────────────────────────────────────────────

    # Stable Zerodha instrument tokens for NSE indices (do not change)
    _INDEX_TOKENS: dict[str, int] = {
        "NIFTY":       256265,
        "BANKNIFTY":   260105,
        "FINNIFTY":    257801,
        "MIDCPNIFTY":  288009,
        "SENSEX":      265,
    }

    def get_index_token(self, underlying: str) -> Optional[int]:
        """Return the Zerodha instrument token for a known NSE index, else None."""
        return self._INDEX_TOKENS.get(underlying.upper())

    def get_historical_data(
        self,
        instrument_token: int,
        from_date: date,
        to_date: date,
        interval: str = "day",
    ) -> list[dict]:
        """
        Fetch OHLC candlestick data from Zerodha historical API.

        interval: 'minute', '3minute', '5minute', '15minute', '30minute',
                  '60minute', 'day', 'week', 'month'

        Returns list of dicts with keys: date, open, high, low, close, volume.
        Returns empty list on any error.
        """
        try:
            data = self._kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
                interval=interval,
                continuous=False,
                oi=False,
            )
            logger.info("historical_data_fetched", extra={
                "token": instrument_token, "interval": interval, "bars": len(data),
            })
            return data
        except Exception as exc:
            logger.warning("historical_data_failed", extra={
                "token": instrument_token, "interval": interval, "error": str(exc),
            })
            return []

    # ── Factory method ─────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, instrument_cache_path: str = "logs/nfo_instruments_cache.csv") -> "ZerodhaBroker":
        """
        Create instance from environment variables.
        Reads ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN.
        Raises BrokerError if either is missing.
        Never logs the values.
        """
        api_key = os.environ.get("ZERODHA_API_KEY", "")
        access_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "")
        if not api_key:
            raise BrokerError("ZERODHA_API_KEY not set in environment")
        if not access_token:
            raise BrokerError("ZERODHA_ACCESS_TOKEN not set in environment")
        return cls(api_key=api_key, access_token=access_token,
                   instrument_cache_path=instrument_cache_path)
