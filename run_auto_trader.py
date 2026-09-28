"""
AutoTrader entry point.

Usage:
    python run_auto_trader.py

What it does:
  1. Loads config/auto_trader.yaml, config/risk.yaml
  2. Connects to Zerodha using ZERODHA_API_KEY + ZERODHA_ACCESS_TOKEN from .env
  3. At 09:30 IST: scans NIFTY / BANKNIFTY / FINNIFTY for the best Iron Condor
     — auto-selects strikes until net credit ≥ minimum
     — enters the best opportunity (highest return-on-risk)
  4. Every 15 minutes: checks open positions
     — exits at 50% profit captured
     — exits if loss > 2× credit
     — exits 2 DTE before expiry
  5. Runs until you press Ctrl+C

Requirements:
  - .env file with ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN
  - logs/nfo_instruments_cache.csv (run resolve-instruments --live first)
  - logs/ directory writable (for trading.db)
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ── Bootstrap ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

# Structured JSON logging
class _JsonFormatter(logging.Formatter):
    _SKIP = {"name", "msg", "args", "created", "relativeCreated", "thread",
             "threadName", "processName", "process", "levelno", "pathname",
             "filename", "module", "funcName", "lineno", "msecs", "stack_info",
             "exc_info", "exc_text", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        base = {
            "time": self.formatTime(record, "%Y-%m-%d %H:%M:%S") + ",%03d" % record.msecs,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in self._SKIP and not k.startswith("_")}
        base.update(extras)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return _json.dumps(base)

_handler_stdout = logging.StreamHandler(sys.stdout)
_handler_file   = logging.FileHandler("logs/auto_trader.log", mode="a")
for _h in (_handler_stdout, _handler_file):
    _h.setFormatter(_JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler_stdout, _handler_file])
logger = logging.getLogger(__name__)


def main() -> None:
    # ── Validate environment ─────────────────────────────────────────────────
    api_key      = os.environ.get("ZERODHA_API_KEY", "")
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "")

    if not api_key or not access_token:
        print("ERROR: ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN must be set in .env")
        print("  Run: python -m brokers.token_helper  to get a fresh access token")
        sys.exit(1)

    cache_path = "logs/nfo_instruments_cache.csv"
    if not Path(cache_path).exists():
        print("ERROR: Instrument cache not found.")
        print("  Run: python -m cli.main resolve-instruments --live --force-refresh")
        sys.exit(1)

    db_path = os.environ.get("DB_PATH", "logs/trading.db")
    Path("logs").mkdir(exist_ok=True)

    # ── Load configs ─────────────────────────────────────────────────────────
    from execution.auto_trader import load_auto_trader_config
    from dashboard_helpers import load_risk_cached

    at_cfg = load_auto_trader_config("config/auto_trader.yaml")
    if not at_cfg.enabled:
        print("AutoTrader is disabled in config/auto_trader.yaml (enabled: false)")
        print("Set 'enabled: true' to start.")
        sys.exit(0)

    risk, exit_rules, paper_cfg, err = load_risk_cached()
    if err:
        print(f"ERROR loading risk config: {err}")
        sys.exit(1)

    if at_cfg.mode.lower() == "live":
        paper_cfg = None   # not used in live mode — suppress paper config entirely

    # ── Connect to Zerodha ───────────────────────────────────────────────────
    from brokers.zerodha import ZerodhaBroker
    broker = ZerodhaBroker.from_env()

    # ── Initialise DB ────────────────────────────────────────────────────────
    from storage.database import create_db_engine
    create_db_engine(db_path)   # also runs metadata.create_all internally

    # ── Prevent duplicate instances ──────────────────────────────────────────
    from execution.process_lock import ProcessLock, ProcessLockError
    lock = ProcessLock()
    try:
        lock.acquire()
    except ProcessLockError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    # ── Start AutoTrader ─────────────────────────────────────────────────────
    from execution.auto_trader import AutoTrader

    trader = AutoTrader(
        cfg=at_cfg,
        risk=risk,
        broker=broker,
        cache_path=cache_path,
        db_path=db_path,
        paper_cfg=paper_cfg,   # None in live mode
    )
    try:
        trader.run()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
