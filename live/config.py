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
    """Take-profit threshold: close when current cost <= (1 - premium_pct) × initial_credit."""

    lots: int = 0
    """Fixed number of lots to trade. 0 = auto-size from available capital."""

    enabled: bool = True
    """Whether this stock is actively traded."""


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

    @property
    def enabled_stocks(self) -> list[StockConfig]:
        """Return only stocks that have enabled=True."""
        return [s for s in self.stocks if s.enabled]


# ── Loader ─────────────────────────────────────────────────────────────────


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
        stocks.append(
            StockConfig(
                symbol=str(s["symbol"]),
                put_dist_pct=float(s.get("put_dist_pct", 5.0)),
                call_dist_pct=float(s.get("call_dist_pct", 5.0)),
                premium_pct=float(s.get("premium_pct", 0.4)),
                lots=int(s.get("lots", 0)),
                enabled=bool(s.get("enabled", True)),
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
    )

    # ── Zerodha credentials (yaml first, env overrides) ────────────────────
    z_raw = raw.get("zerodha", {})
    api_key = os.environ.get("ZERODHA_API_KEY") or str(z_raw.get("api_key", ""))
    api_secret = os.environ.get("ZERODHA_API_SECRET") or str(z_raw.get("api_secret", ""))
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN") or str(z_raw.get("access_token", ""))

    mode = os.environ.get("TRADING_MODE") or str(raw.get("mode", "paper"))

    cfg = LiveConfig(
        capital=capital,
        stocks=stocks,
        strategy=strategy,
        zerodha_api_key=api_key,
        zerodha_api_secret=api_secret,
        zerodha_access_token=access_token,
        mode=mode.lower(),
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
