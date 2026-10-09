#!/usr/bin/env python3
"""
run_live.py — CLI entry point for the shifting iron condor live trading system.

Modes:
    paper  — Full session using PaperBroker (no real orders, simulated fills)
    live   — Full session using ZerodhaBroker (real NRML orders on NSE F&O)

Usage::

    python run_live.py --mode paper
    python run_live.py --mode live
    python run_live.py --web-only
    python run_live.py --list-symbols
    python run_live.py --mode paper --web-only

Environment variables (or config/credentials.env):
    ZERODHA_API_KEY
    ZERODHA_API_SECRET
    ZERODHA_ACCESS_TOKEN
    TRADING_MODE               (overrides --mode)
    LIVE_TRADING_CONFIRMATION  (required when mode=live)
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

# ── Ensure project root is on sys.path ────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Optional .env loading ──────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    _env_path = _PROJECT_ROOT / "config" / "credentials.env"
    if _env_path.exists():
        load_dotenv(_env_path)
        print(f"[run_live] Loaded credentials from {_env_path}")
    else:
        _alt = _PROJECT_ROOT / ".env"
        if _alt.exists():
            load_dotenv(_alt)
            print(f"[run_live] Loaded credentials from {_alt}")
except ImportError:
    pass  # python-dotenv not installed; rely on environment variables

# ── Logging setup ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("run_live")

# ── Graceful shutdown ──────────────────────────────────────────────────────

_shutdown_event = threading.Event()


def _handle_sigint(signum, frame):
    """Signal handler for SIGINT/SIGTERM — triggers graceful shutdown."""
    print("\n[run_live] Received shutdown signal. Stopping gracefully...")
    _shutdown_event.set()


signal.signal(signal.SIGINT, _handle_sigint)
signal.signal(signal.SIGTERM, _handle_sigint)


# ── Argument parsing ───────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        Parsed argparse.Namespace.
    """
    parser = argparse.ArgumentParser(
        prog="run_live",
        description="Shifting Iron Condor Live Trading System — Zerodha NSE F&O",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_live.py --mode paper
  python run_live.py --mode live
  python run_live.py --web-only
  python run_live.py --list-symbols
  TRADING_MODE=live python run_live.py
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Trading mode: 'paper' (no real orders) or 'live' (real orders). "
             "Defaults to config value or TRADING_MODE env var.",
    )
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="Start only the Flask web dashboard; do not start the trading runner.",
    )
    parser.add_argument(
        "--runner-only",
        action="store_true",
        help="Start only the trading runner; skip the web dashboard. "
             "Used when the dashboard is already running (e.g. via gunicorn).",
    )
    parser.add_argument(
        "--list-symbols",
        action="store_true",
        help="List all enabled symbols from live_config.yaml and exit.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to live_config.yaml (default: config/live_config.yaml).",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=5000,
        help="Port for the Flask web dashboard (default: 5000).",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO).",
    )
    return parser.parse_args()


# ── Broker factory ─────────────────────────────────────────────────────────


def _build_broker(mode: str, config):
    """
    Instantiate the appropriate broker for the given mode.

    Args:
        mode:   'paper' or 'live'.
        config: LiveConfig instance.

    Returns:
        BrokerAdapter implementation.
    """
    if mode == "live":
        logger.info("Initialising ZerodhaBroker for LIVE trading...")
        if not config.zerodha_api_key:
            print("[ERROR] ZERODHA_API_KEY is not set. Cannot start in live mode.")
            sys.exit(1)
        if not config.zerodha_access_token:
            print("[ERROR] ZERODHA_ACCESS_TOKEN is not set. Cannot start in live mode.")
            sys.exit(1)

        # Safety confirmation must be set for live trading
        confirmation = os.environ.get("LIVE_TRADING_CONFIRMATION", "")
        required = "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED"
        if confirmation != required:
            print(
                "\n[ERROR] Live trading requires LIVE_TRADING_CONFIRMATION environment variable.\n"
                f"  Set: LIVE_TRADING_CONFIRMATION={required}\n"
                "  This confirms you understand real orders will be placed on NSE F&O.\n"
            )
            sys.exit(1)

        from brokers.zerodha import ZerodhaBroker
        return ZerodhaBroker(
            api_key=config.zerodha_api_key,
            access_token=config.zerodha_access_token,
        )
    else:
        # Paper mode — real quotes from Zerodha, simulated fills
        logger.info("Initialising PaperBroker for paper trading...")
        if not config.zerodha_api_key or not config.zerodha_access_token:
            print("[ERROR] Paper mode still needs ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN")
            print("        to fetch live quotes. Refresh your token at /token first.")
            sys.exit(1)
        from brokers.zerodha import ZerodhaBroker
        from brokers.paper import PaperBroker
        live_source = ZerodhaBroker(
            api_key=config.zerodha_api_key,
            access_token=config.zerodha_access_token,
        )
        return PaperBroker(live_quote_source=live_source)


# ── Web server thread ──────────────────────────────────────────────────────


def _start_web_server(port: int) -> threading.Thread:
    """
    Start the Flask web dashboard in a background daemon thread.

    Args:
        port: TCP port to bind to.

    Returns:
        The started thread.
    """
    def _run():
        from web.app import app
        logger.info(f"Starting web dashboard on http://0.0.0.0:{port}")
        # Use waitress or werkzeug; never debug=True in production
        try:
            import waitress
            waitress.serve(app, host="0.0.0.0", port=port, threads=4)
        except ImportError:
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, name="WebServer", daemon=True)
    t.start()
    return t


# ── Runner thread ──────────────────────────────────────────────────────────


def _start_runner(broker, config, log_dir: str, config_path=None) -> threading.Thread:
    """
    Start the CondorRunner session in a background thread.

    Args:
        broker:  BrokerAdapter instance.
        config:  LiveConfig instance.
        log_dir: Directory for TradeLogger output.

    Returns:
        The started thread.
    """
    from live.capital_manager import CapitalManager
    from live.condor_runner import CondorRunner
    from live.trade_logger import TradeLogger
    from risk.kill_switch import KillSwitch

    trade_logger = TradeLogger(log_dir=log_dir)
    capital_manager = CapitalManager(config.capital)
    kill_switch = KillSwitch()

    runner = CondorRunner(
        broker=broker,
        config=config,
        trade_logger=trade_logger,
        capital_manager=capital_manager,
        kill_switch=kill_switch,
        config_path=config_path,
    )

    def _run():
        try:
            runner.run_session()
        except Exception as exc:
            logger.exception(f"CondorRunner session error: {exc}")
            trade_logger.log_error(None, exc, {"phase": "session"})
        finally:
            _shutdown_event.set()

    t = threading.Thread(target=_run, name="CondorRunner", daemon=True)
    t.start()
    return t


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    """
    CLI entry point.

    Parses arguments, loads config, starts the runner and/or web server
    in threads, then blocks until a shutdown signal is received.
    """
    args = _parse_args()

    # Set log level
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # ── Load config ────────────────────────────────────────────────────────
    config_path = Path(args.config) if args.config else None
    try:
        from live.config import load_config
        config = load_config(config_path)
    except FileNotFoundError as exc:
        print(f"[ERROR] Config file not found: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] Failed to load config: {exc}")
        sys.exit(1)

    # Override mode from CLI if specified
    if args.mode:
        config.mode = args.mode

    # ── --list-symbols ─────────────────────────────────────────────────────
    if args.list_symbols:
        print(f"\nEnabled symbols ({len(config.enabled_stocks)}):")
        for s in config.enabled_stocks:
            print(f"  {s.symbol:<20} put={s.put_dist_pct}%  call={s.call_dist_pct}%  premium={s.premium_pct}")
        print(f"\nDisabled symbols ({len(config.stocks) - len(config.enabled_stocks)}):")
        for s in config.stocks:
            if not s.enabled:
                print(f"  {s.symbol}")
        print()
        return

    # ── Print session summary ──────────────────────────────────────────────
    print("=" * 70)
    print(f"  Shifting Iron Condor Live Trader")
    print(f"  Mode:            {config.mode.upper()}")
    print(f"  Enabled stocks:  {len(config.enabled_stocks)} / {len(config.stocks)}")
    print(f"  Total capital:   ₹{config.capital.total_capital:,.0f}")
    print(f"  Max positions:   {config.capital.max_open_positions}")
    print(f"  Entry window:    {config.strategy.entry_start} – {config.strategy.entry_end} IST")
    print(f"  DTE window:      {config.strategy.min_dte} – {config.strategy.max_dte} days")
    print(f"  Web port:        {args.web_port}")
    print("=" * 70)
    print()

    if config.mode == "live":
        print("[WARNING] LIVE MODE — real orders will be placed on NSE F&O!")
        print("          Press Ctrl+C within 5 seconds to abort.")
        for i in range(5, 0, -1):
            print(f"          Starting in {i}...", end="\r")
            time.sleep(1)
        print()

    # ── Log directory ──────────────────────────────────────────────────────
    log_dir = "logs/live"
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    threads: list[threading.Thread] = []

    # ── Start web server (skipped in --runner-only mode) ───────────────────
    if not args.runner_only:
        web_thread = _start_web_server(port=args.web_port)
        threads.append(web_thread)
        print(f"[run_live] Web dashboard started: http://localhost:{args.web_port}")

    if args.web_only:
        print("[run_live] --web-only mode: trading runner not started.")
        print("[run_live] Press Ctrl+C to stop.")
        _shutdown_event.wait()
    else:
        # ── Authenticate broker ────────────────────────────────────────────
        try:
            broker = _build_broker(config.mode, config)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"[ERROR] Failed to initialise broker: {exc}")
            sys.exit(1)

        # ── Start trading runner ───────────────────────────────────────────
        _cfg_path = Path(args.config) if args.config else Path("config/live_config.yaml")
        runner_thread = _start_runner(broker, config, log_dir, config_path=_cfg_path)
        threads.append(runner_thread)
        print(f"[run_live] CondorRunner started in {config.mode} mode.")
        print("[run_live] Press Ctrl+C to stop gracefully.")

        # ── Wait for shutdown ──────────────────────────────────────────────
        _shutdown_event.wait()
        print("\n[run_live] Shutting down...")

        # Give runner thread a moment to complete current operation
        runner_thread.join(timeout=30)
        if runner_thread.is_alive():
            print("[run_live] Runner thread did not stop cleanly within 30s.")

    print("[run_live] Shutdown complete.")


if __name__ == "__main__":
    main()
