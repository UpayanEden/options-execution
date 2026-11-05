"""
Live trading configuration.

Loads from config/live_config.yaml and merges environment variable overrides
for Zerodha credentials so secrets never need to live in yaml files.

Usage::

    from live.config import load_config
    cfg = load_config()
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("config/live_config.yaml")


# ── Dataclasses ────────────────────────────────────────────────────────────


@dataclass
class CapitalConfig:
    """Capital allocation parameters."""

    total_capital: float
    """Total capital allocated to the strategy in INR."""

    cash_margin_pct: float
    """Fraction of required margin that must be available in cash (0–1)."""

    max_capital_per_stock: float
    """Maximum capital that can be allocated to a single stock position."""

    max_open_positions: int
    """Maximum number of simultaneously open condor positions."""


@dataclass
class StockConfig:
    """Per-stock strategy configuration."""

    symbol: str
    """NSE F&O trading symbol (e.g. 'RELIANCE')."""

    put_dist_pct: float
    """Distance of short put from spot as a percentage of spot price."""

    call_dist_pct: float
    """Distance of short call from spot as a percentage of spot price."""

    premium_pct: float
    """Entry premium gate (0–1 fraction of spread width).
    Position is only entered when net_credit / spread_width >= premium_pct.
    If the threshold is not met, the stock stays in the retry queue and is
    re-tried with fresh quotes every 30 s until the entry cutoff.
    This is NOT the take-profit threshold — exit is controlled by
    strategy.profit_target_pct (default 85% of original credit)."""

    lots: int = 0
    """Fixed number of lots to trade. 0 = auto-size from available capital."""

    enabled: bool = True
    """Whether this stock is actively traded."""

    expiry_type: str = "monthly"
    """Expiry cycle to target: 'monthly' (last Thursday of month) or 'weekly' (any Thursday)."""

    expiry_date: Optional[date] = None
    """Pin this stock to a specific expiry date (YYYY-MM-DD). Overrides expiry_type and DTE window."""


@dataclass
class StrategyParams:
    """Strategy-level parameters shared across all stocks."""

    shift_trigger_pct: float
    """Spot movement percentage that triggers a condor shift."""

    wing_width_pct: float
    """Width of each wing as a percentage of spot (long - short distance)."""

    min_dte: int
    """Minimum days-to-expiry at entry."""

    max_dte: int
    """Maximum days-to-expiry at entry."""

    entry_start: str
    """Earliest time to enter new positions in IST 'HH:MM' format."""

    entry_end: str
    """Latest time to enter new positions in IST 'HH:MM' format."""

    default_lots: int = 0
    """Default lots for all stocks. 0 = auto-size from capital. Overridden per-stock by StockConfig.lots."""

    dte_exit: int = 2
    """Close positions when days-to-expiry reaches this threshold (default: 2 days before expiry)."""

    trend_adx_min: float = 20.0
    """Minimum ADX(14) value for trend gate to be active. Below this → NEUTRAL."""

    trend_dist_boost: float = 0.10
    """Fractional boost to shift distance when trend aligns with trigger side (e.g. 0.10 = +10%)."""

    fill_poll_interval_s: float = 0.5
    """Seconds between each order fill status poll for shift legs."""

    shift_order_timeout_s: float = 2.0
    """Seconds to wait for a shift leg fill before cancelling and re-placing at fresh market price."""

    spot_missing_alert_cycles: int = 4
    """Number of consecutive spot-missing cycles before sending an alert."""

    allow_dte_exit_during_ban: bool = True
    """If True, allow DTE exit even if symbol is on F&O ban list."""

    profit_target_pct: float = 85.0
    """Exit when profit captured >= this % of original_credit."""

    stop_loss_credit_multiple: Optional[float] = None
    """Exit when loss >= this multiple of original_credit (e.g. 2.0 = 2× credit). None = disabled."""

    stop_loss_inr: Optional[float] = None
    """Exit when loss >= this absolute INR amount (lots × lot_size × per-unit-loss). None = disabled."""

    stop_loss_confirm_checks: int = 2
    """Number of consecutive monitoring cycles that must breach the stop-loss before exiting.
    Reduces false exits from momentary price spikes (default: 2)."""

    max_daily_loss_inr: Optional[float] = None
    """Intraday circuit breaker: activate kill switch when session realized loss
    (sum of closed-position PnL) exceeds this INR amount. None = disabled."""

    entry_completion_buffer_min: int = 20
    """Minutes required to complete an entry after it starts. No new entries will be
    initiated if now + buffer > effective_cutoff (min of entry_end and 15:15 IST)."""

    allow_reentry: bool = False
    """Allow re-entering the same symbol+expiry after an exit within the same session.
    When False (default) the first exit on a given expiry blocks all further entries for it."""

    max_fo_ban_suspension_cycles: int = 2
    """Exit the position after this many completed 1-hour F&O ban suspensions.
    After N suspensions the stock is clearly stuck in ban and further waiting only
    deepens the loss — exit instead of waiting indefinitely."""


@dataclass
class LiveConfig:
    """Root configuration object for the live trading session."""

    capital: CapitalConfig
    """Capital allocation settings."""

    stocks: list[StockConfig]
    """List of per-stock configurations."""

    strategy: StrategyParams
    """Shared strategy parameters."""

    zerodha_api_key: str
    """Zerodha Kite Connect API key."""

    zerodha_api_secret: str
    """Zerodha Kite Connect API secret (used for token refresh)."""

    zerodha_access_token: str
    """Current session access token obtained via Kite login flow."""

    mode: str = "paper"
    """Trading mode: 'paper' or 'live'."""

    telegram_bot_token: str = ""
    """Telegram bot token for alert notifications. Leave blank to disable."""

    telegram_chat_id: str = ""
    """Telegram chat/channel ID to send alerts to. Leave blank to disable."""

    @property
    def enabled_stocks(self) -> list[StockConfig]:
        """Return only stocks that have enabled=True."""
        return [s for s in self.stocks if s.enabled]


# ── Loader ─────────────────────────────────────────────────────────────────


def _opt_float(value) -> Optional[float]:
    """Return float(value) or None if value is None/empty."""
    if value is None or value == "" or value == "null":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_config(config_path: Optional[Path] = None) -> LiveConfig:
    """
    Load LiveConfig from a YAML file, then override Zerodha credentials from
    environment variables if present.

    Environment variables consulted:
        ZERODHA_API_KEY        — overrides yaml zerodha.api_key
        ZERODHA_API_SECRET     — overrides yaml zerodha.api_secret
        ZERODHA_ACCESS_TOKEN   — overrides yaml zerodha.access_token
        TRADING_MODE           — overrides yaml mode (paper/live)

    Raises:
        FileNotFoundError: if the config file does not exist.
        ValueError: if required keys are missing from the config.
    """
    path = config_path or _CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path.resolve()}")

    logger.info("loading_config", extra={"path": str(path)})

    with open(path) as fh:
        raw: dict = yaml.safe_load(fh) or {}

    # ── Capital ────────────────────────────────────────────────────────────
    cap_raw = raw.get("capital", {})
    capital = CapitalConfig(
        total_capital=float(cap_raw.get("total_capital", 0)),
        cash_margin_pct=float(cap_raw.get("cash_margin_pct", 0.5)),
        max_capital_per_stock=float(cap_raw.get("max_capital_per_stock", 0)),
        max_open_positions=int(cap_raw.get("max_open_positions", 5)),
    )

    # ── Stocks ─────────────────────────────────────────────────────────────
    stocks_raw: list[dict] = raw.get("stocks", [])
    stocks: list[StockConfig] = []
    for s in stocks_raw:
        expiry_type = str(s.get("expiry_type", "monthly")).lower()
        if expiry_type not in ("monthly", "weekly"):
            logger.warning(
                "invalid_expiry_type",
                extra={"symbol": s.get("symbol"), "expiry_type": expiry_type},
            )
            expiry_type = "monthly"
        expiry_date: Optional[date] = None
        raw_expiry_date = s.get("expiry_date")
        if raw_expiry_date is not None:
            try:
                expiry_date = date.fromisoformat(str(raw_expiry_date))
            except ValueError:
                logger.warning(
                    "invalid_expiry_date",
                    extra={"symbol": s.get("symbol"), "expiry_date": raw_expiry_date},
                )

        stocks.append(
            StockConfig(
                symbol=str(s["symbol"]),
                put_dist_pct=float(s.get("put_dist_pct", 5.0)),
                call_dist_pct=float(s.get("call_dist_pct", 5.0)),
                premium_pct=float(s.get("premium_pct", 0.4)),
                lots=int(s.get("lots", 0)),
                enabled=bool(s.get("enabled", True)),
                expiry_type=expiry_type,
                expiry_date=expiry_date,
            )
        )

    # ── Strategy params ────────────────────────────────────────────────────
    strat_raw = raw.get("strategy", {})
    strategy = StrategyParams(
        shift_trigger_pct=float(strat_raw.get("shift_trigger_pct", 1.0)),
        wing_width_pct=float(strat_raw.get("wing_width_pct", 2.0)),
        min_dte=int(strat_raw.get("min_dte", 20)),
        max_dte=int(strat_raw.get("max_dte", 45)),
        entry_start=str(strat_raw.get("entry_start", "09:30")),
        entry_end=str(strat_raw.get("entry_end", "14:00")),
        default_lots=int(strat_raw.get("default_lots", 0)),
        dte_exit=int(strat_raw.get("dte_exit", 2)),
        trend_adx_min=float(strat_raw.get("trend_adx_min", 20.0)),
        trend_dist_boost=float(strat_raw.get("trend_dist_boost", 0.10)),
        fill_poll_interval_s=float(strat_raw.get("fill_poll_interval_s", 0.5)),
        shift_order_timeout_s=float(strat_raw.get("shift_order_timeout_s", 2.0)),
        spot_missing_alert_cycles=int(strat_raw.get("spot_missing_alert_cycles", 4)),
        allow_dte_exit_during_ban=bool(strat_raw.get("allow_dte_exit_during_ban", True)),
        profit_target_pct=float(strat_raw.get("profit_target_pct", 85.0)),
        stop_loss_credit_multiple=_opt_float(strat_raw.get("stop_loss_credit_multiple")),
        stop_loss_inr=_opt_float(strat_raw.get("stop_loss_inr")),
        stop_loss_confirm_checks=int(strat_raw.get("stop_loss_confirm_checks", 2)),
        max_daily_loss_inr=_opt_float(strat_raw.get("max_daily_loss_inr")),
        entry_completion_buffer_min=int(strat_raw.get("entry_completion_buffer_min", 20)),
        allow_reentry=bool(strat_raw.get("allow_reentry", False)),
        max_fo_ban_suspension_cycles=int(strat_raw.get("max_fo_ban_suspension_cycles", 2)),
    )

    # ── Zerodha credentials (yaml first, env overrides) ────────────────────
    z_raw = raw.get("zerodha", {})
    api_key = os.environ.get("ZERODHA_API_KEY") or str(z_raw.get("api_key", ""))
    api_secret = os.environ.get("ZERODHA_API_SECRET") or str(z_raw.get("api_secret", ""))
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN") or str(z_raw.get("access_token", ""))

    mode = os.environ.get("TRADING_MODE") or str(raw.get("mode", "paper"))

    # ── Telegram alerts ────────────────────────────────────────────────────
    alerts_raw = raw.get("alerts", {})
    telegram_bot_token = str(alerts_raw.get("telegram_bot_token", ""))
    telegram_chat_id = str(alerts_raw.get("telegram_chat_id", ""))

    cfg = LiveConfig(
        capital=capital,
        stocks=stocks,
        strategy=strategy,
        zerodha_api_key=api_key,
        zerodha_api_secret=api_secret,
        zerodha_access_token=access_token,
        mode=mode.lower(),
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
    )

    logger.info(
        "config_loaded",
        extra={
            "mode": cfg.mode,
            "stocks_total": len(cfg.stocks),
            "stocks_enabled": len(cfg.enabled_stocks),
        },
    )
    return cfg
