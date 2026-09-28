"""Helpers for the Streamlit dashboard. Loaded by dashboard.py."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

IST = ZoneInfo("Asia/Kolkata")


def load_strategy_cached():
    """Load strategy config. Returns (StrategyConfig, error_str)."""
    try:
        import yaml
        from execution.models import LegConfig, StrategyConfig
        path = os.environ.get("STRATEGY_CONFIG", "config/strategy.yaml")
        with open(path) as f:
            raw = yaml.safe_load(f)
        s = raw["strategy"]
        legs = [LegConfig(**l) for l in s.pop("legs")]
        return StrategyConfig(**s, legs=legs), None
    except Exception as e:
        return None, str(e)


def load_risk_cached():
    """Load risk config. Returns (RiskConfig, ExitRulesConfig, PaperConfig, error_str)."""
    try:
        import yaml
        from execution.models import ExitRulesConfig, PaperConfig, RiskConfig
        path = os.environ.get("RISK_CONFIG", "config/risk.yaml")
        with open(path) as f:
            raw = yaml.safe_load(f)
        risk = RiskConfig(**raw["risk"])
        exit_rules = ExitRulesConfig(**raw.get("exit_rules", {}))
        paper = PaperConfig(**raw.get("paper", {}))
        return risk, exit_rules, paper, None
    except Exception as e:
        return None, None, None, str(e)


def get_trading_mode() -> str:
    return os.environ.get("TRADING_MODE", "PAPER").upper()


def has_zerodha_credentials() -> bool:
    return bool(os.environ.get("ZERODHA_API_KEY")) and bool(
        os.environ.get("ZERODHA_ACCESS_TOKEN")
    )


def get_db_path() -> str:
    return os.environ.get("DB_PATH", "logs/trading.db")


def decimal_to_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return None


def now_ist() -> datetime:
    return datetime.now(IST)


def fmt_inr(val) -> str:
    try:
        return f"\u20b9{float(val):,.2f}"
    except Exception:
        return "N/A"


def fmt_pts(val) -> str:
    try:
        return f"{float(val):.2f} pts"
    except Exception:
        return "N/A"
