"""
CLI entry point.
Phase 2 implements: validate-config, resolve-instruments.
Phase 3 adds: preview-entry, paper-enter, monitor, etc.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import click
import yaml
from dotenv import load_dotenv

# Ensure project root is in sys.path when running as module
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

from execution.models import (
    ExitRulesConfig,
    LegConfig,
    PaperConfig,
    RiskConfig,
    StrategyConfig,
    TradingMode,
)
from risk.payoff import calculate_payoff_from_credit
from risk.validator import PreTradeValidator

logger = logging.getLogger(__name__)


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _load_strategy(path: str) -> StrategyConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    s = raw["strategy"]
    legs = [LegConfig(**l) for l in s.pop("legs")]
    return StrategyConfig(**s, legs=legs)


def _load_risk(path: str) -> tuple[RiskConfig, ExitRulesConfig, PaperConfig]:
    with open(path) as f:
        raw = yaml.safe_load(f)
    risk = RiskConfig(**raw["risk"])
    exit_rules = ExitRulesConfig(**raw.get("exit_rules", {}))
    paper = PaperConfig(**raw.get("paper", {}))
    return risk, exit_rules, paper


def _check_trading_mode() -> TradingMode:
    mode_str = os.environ.get("TRADING_MODE", "PAPER").upper()
    confirmation = os.environ.get("LIVE_TRADING_CONFIRMATION", "")
    if mode_str == "LIVE":
        required = "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED"
        if confirmation != required:
            click.echo(
                "\nLIVE TRADING BLOCKED.\n"
                f"TRADING_MODE=LIVE requires LIVE_TRADING_CONFIRMATION={required}\n"
                "Set both environment variables to enable live trading.\n",
                err=True,
            )
            sys.exit(1)
        return TradingMode.LIVE
    return TradingMode.PAPER


@click.group()
@click.option("--strategy", default="config/strategy.yaml", show_default=True,
              help="Path to strategy YAML config")
@click.option("--risk", default="config/risk.yaml", show_default=True,
              help="Path to risk YAML config")
@click.option("--log-level", default="INFO", show_default=True)
@click.pass_context
def cli(ctx: click.Context, strategy: str, risk: str, log_level: str) -> None:
    """Options execution and monitoring system. Defaults to PAPER mode."""
    _setup_logging(log_level)
    ctx.ensure_object(dict)
    ctx.obj["strategy_path"] = strategy
    ctx.obj["risk_path"] = risk


@cli.command("validate-config")
@click.pass_context
def validate_config(ctx: click.Context) -> None:
    """Validate strategy and risk configuration files."""
    errors: list[str] = []

    # Strategy
    try:
        strategy = _load_strategy(ctx.obj["strategy_path"])
        click.echo(f"\nStrategy config:   OK  ({ctx.obj['strategy_path']})")
        click.echo(f"  Name:       {strategy.name}")
        click.echo(f"  Underlying: {strategy.underlying}")
        click.echo(f"  Exchange:   {strategy.exchange}")
        click.echo(f"  Expiry:     {strategy.expiry}")
        click.echo(f"  Lots:       {strategy.lots}")
        click.echo(f"  Legs:")
        for i, leg in enumerate(strategy.legs):
            click.echo(f"    [{i}] {leg.side.value:4s} {leg.strike} {leg.option_type.value}")
    except Exception as e:
        click.echo(f"\nStrategy config:   FAIL  ({ctx.obj['strategy_path']})\n  {e}", err=True)
        errors.append(str(e))

    # Risk
    try:
        risk, exit_rules, paper = _load_risk(ctx.obj["risk_path"])
        click.echo(f"\nRisk config:       OK  ({ctx.obj['risk_path']})")
        click.echo(f"  Capital limit:   \u20b9{risk.capital_limit}")
        click.echo(f"  Max loss:        \u20b9{risk.max_strategy_loss}")
        click.echo(f"  Min net credit:  {risk.min_net_credit} pts")
        click.echo(f"  Entry window:    {risk.entry_start_time}\u2013{risk.entry_end_time} IST")
        click.echo(f"  Paper fill mode: {paper.fill_mode.value}")
    except Exception as e:
        click.echo(f"\nRisk config:       FAIL  ({ctx.obj['risk_path']})\n  {e}", err=True)
        errors.append(str(e))

    # Trading mode
    mode_str = os.environ.get("TRADING_MODE", "PAPER")
    click.echo(f"\nTrading mode:      {mode_str}")
    if mode_str == "LIVE":
        confirmation = os.environ.get("LIVE_TRADING_CONFIRMATION", "")
        required = "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED"
        if confirmation == required:
            click.echo("  Live confirmation: SET (live orders will be placed!)")
        else:
            click.echo("  Live confirmation: NOT SET (live orders blocked)")

    if errors:
        click.echo(f"\n{len(errors)} error(s) found. Fix before proceeding.", err=True)
        sys.exit(1)
    else:
        click.echo("\nAll checks passed.\n")


@cli.command("resolve-instruments")
@click.option("--live", is_flag=True, default=False,
              help="Fetch from Zerodha API (requires ZERODHA_ACCESS_TOKEN in .env)")
@click.option("--force-refresh", is_flag=True, default=False,
              help="Force re-download of instrument master even if cache is fresh")
@click.pass_context
def resolve_instruments(ctx: click.Context, live: bool, force_refresh: bool) -> None:
    """
    Resolve instrument tokens and trading symbols.
    Without --live: shows estimated symbols from config.
    With --live: connects to Zerodha and resolves actual tokens and lot sizes.
    """
    strategy = _load_strategy(ctx.obj["strategy_path"])
    click.echo(f"\nStrategy: {strategy.name}")
    click.echo(f"Underlying: {strategy.underlying}  Expiry: {strategy.expiry}\n")

    if not live:
        click.echo("Estimated symbols (no live connection):")
        for i, leg in enumerate(strategy.legs):
            symbol_hint = (
                f"{strategy.underlying}"
                f"{strategy.expiry.strftime('%y%b').upper()}"
                f"{int(leg.strike)}"
                f"{leg.option_type.value}"
            )
            click.echo(f"  [{i}] {leg.side.value:4s} {leg.strike:>8} {leg.option_type.value}  \u2192  {symbol_hint}")
        click.echo("\nRun with --live to resolve actual instrument tokens from Zerodha.\n")
        return

    # Live resolution
    try:
        from brokers.zerodha import ZerodhaBroker
        broker = ZerodhaBroker.from_env()
    except Exception as e:
        click.echo(f"\nCannot connect to Zerodha: {e}", err=True)
        click.echo("Ensure ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are set in your .env\n", err=True)
        sys.exit(1)

    if force_refresh:
        click.echo("Force-refreshing instrument master from Zerodha API...")
        count = broker.refresh_instruments()
        click.echo(f"Loaded {count} NFO instruments.\n")

    click.echo(f"{'#':<4} {'SIDE':<5} {'STRIKE':>8} {'TYPE':<4}  {'TRADING SYMBOL':<35} {'TOKEN':>12} {'LOT SIZE':>10} {'TICK':>6}")
    click.echo("-" * 90)

    all_resolved = {}
    errors = []
    for i, leg in enumerate(strategy.legs):
        try:
            resolved = broker.resolve_instrument(
                underlying=strategy.underlying,
                exchange=strategy.exchange,
                expiry=strategy.expiry,
                strike=leg.strike,
                option_type=leg.option_type.value,
            )
            all_resolved[i] = resolved
            click.echo(
                f"[{i}] {leg.side.value:<5} {leg.strike:>8} {leg.option_type.value:<4}  "
                f"{resolved.trading_symbol:<35} {resolved.instrument_token:>12} "
                f"{resolved.lot_size:>10} {resolved.tick_size:>6}"
            )
        except Exception as e:
            errors.append(f"[{i}] {leg.side.value} {leg.strike} {leg.option_type.value}: {e}")
            click.echo(f"[{i}] {leg.side.value:<5} {leg.strike:>8} {leg.option_type.value:<4}  ERROR: {e}", err=True)

    if errors:
        click.echo(f"\n{len(errors)} instrument(s) could not be resolved. Cannot proceed to live trading.\n", err=True)
        sys.exit(1)
    else:
        click.echo(f"\nAll {len(strategy.legs)} instruments resolved successfully.\n")


@cli.command("preview-entry")
@click.pass_context
def preview_entry(ctx: click.Context) -> None:
    """
    Show a full entry preview table without placing any orders.
    Requires resolved instruments (Phase 4 for live data).
    In Phase 3, shows config-based preview with placeholder instrument tokens.
    """
    strategy = _load_strategy(ctx.obj["strategy_path"])
    risk, exit_rules, paper = _load_risk(ctx.obj["risk_path"])
    mode = os.environ.get("TRADING_MODE", "PAPER")

    click.echo(f"\n{'='*70}")
    click.echo(f"  ENTRY PREVIEW  [{mode} MODE]  — NO ORDERS WILL BE PLACED")
    click.echo(f"{'='*70}")
    click.echo(f"  Strategy:   {strategy.name}")
    click.echo(f"  Underlying: {strategy.underlying}")
    click.echo(f"  Exchange:   {strategy.exchange}")
    click.echo(f"  Expiry:     {strategy.expiry}")
    click.echo(f"  Lots:       {strategy.lots}")
    click.echo()

    click.echo(f"  {'LEG':<3} {'SIDE':<5} {'STRIKE':>8} {'TYPE':<4}  SYMBOL (estimated)")
    click.echo(f"  {'-'*55}")
    for i, leg in enumerate(strategy.legs):
        symbol_hint = (
            f"{strategy.underlying}"
            f"{strategy.expiry.strftime('%y%b').upper()}"
            f"{int(leg.strike)}"
            f"{leg.option_type.value}"
        )
        click.echo(f"  [{i}] {leg.side.value:<5} {leg.strike:>8} {leg.option_type.value:<4}  {symbol_hint}")

    click.echo()
    click.echo(f"  Risk parameters:")
    click.echo(f"    Capital limit:        \u20b9{risk.capital_limit}")
    click.echo(f"    Max strategy loss:    \u20b9{risk.max_strategy_loss}")
    click.echo(f"    Min acceptable credit: {risk.min_net_credit} pts")
    click.echo(f"    Target credit:         {risk.target_net_credit} pts")
    click.echo(f"    Entry window:         {risk.entry_start_time}\u2013{risk.entry_end_time} IST")
    click.echo(f"    Max reprice attempts: {risk.max_repricing_attempts}")
    click.echo()
    click.echo(f"  Paper fill mode: {paper.fill_mode.value}")
    click.echo()
    click.echo("  [Live instrument tokens and real quotes available after Phase 4 integration]")
    click.echo(f"{'='*70}\n")


@cli.command("paper-enter")
@click.option("--poll-interval", default=0, type=int, show_default=True,
              help="Seconds between reprice attempts (0 = use risk.yaml value)")
@click.pass_context
def paper_enter(ctx: click.Context, poll_interval: int) -> None:
    """
    Run a full paper-mode iron condor entry with live Zerodha quotes.
    Simulates fills against real market data — no real orders are placed.
    """
    from brokers.paper import PaperBroker
    from brokers.zerodha import ZerodhaBroker
    from execution.entry_engine import EntryEngine
    from execution.models import ResolvedInstrument, Side
    from execution.state_machine import StateMachine
    from monitoring.alerts import AlertManager
    from risk.kill_switch import KillSwitch
    from storage.database import create_db_engine
    from storage.repositories import StrategyRepository

    strategy = _load_strategy(ctx.obj["strategy_path"])
    risk, exit_rules, paper_cfg = _load_risk(ctx.obj["risk_path"])
    db_path = os.environ.get("DB_PATH", "logs/trading.db")

    click.echo(f"\n[PAPER MODE] Starting paper entry for {strategy.name}")
    click.echo(f"  Underlying: {strategy.underlying}  Expiry: {strategy.expiry}")
    click.echo(f"  This is a SIMULATION. No real orders will be placed.\n")

    # Connect to Zerodha for live quotes
    try:
        live_broker = ZerodhaBroker.from_env()
        click.echo("  Zerodha connected — using live quotes.\n")
    except Exception as e:
        click.echo(f"  ERROR: Cannot connect to Zerodha: {e}\n", err=True)
        click.echo("  Make sure ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are set in .env", err=True)
        sys.exit(1)

    # Resolve real instruments from the NSE instrument cache
    resolved = {}
    click.echo(f"  {'LEG':<4} {'SIDE':<5} {'STRIKE':>8} {'TYPE':<4}  SYMBOL                             TOKEN  LOT_SIZE")
    click.echo(f"  {'-'*80}")
    errors = []
    for i, leg in enumerate(strategy.legs):
        try:
            inst = live_broker.resolve_instrument(
                underlying=strategy.underlying,
                exchange=strategy.exchange,
                expiry=strategy.expiry,
                strike=leg.strike,
                option_type=leg.option_type.value,
            )
            resolved[i] = inst
            click.echo(
                f"  [{i}] {leg.side.value:<5} {leg.strike:>8} {leg.option_type.value:<4}  "
                f"{inst.trading_symbol:<35} {inst.instrument_token:>8}  {inst.lot_size:>6}"
            )
        except Exception as e:
            errors.append(str(e))
            click.echo(f"  [{i}] {leg.side.value:<5} {leg.strike:>8} {leg.option_type.value:<4}  ERROR: {e}", err=True)

    if errors:
        click.echo(f"\n  {len(errors)} instrument(s) not resolved. Cannot proceed.\n", err=True)
        sys.exit(1)

    # Fetch live quotes for a pre-trade sanity display
    instruments = [(r.instrument_token, r.trading_symbol) for r in resolved.values()]
    try:
        raw_quotes = live_broker.get_quotes(instruments)
        click.echo(f"\n  {'LEG':<4} {'SIDE':<5} {'STRIKE':>8} {'TYPE':<4}  {'BID':>8} {'ASK':>8} {'SPREAD%':>8}")
        click.echo(f"  {'-'*58}")
        net_preview = Decimal("0")
        lot_size = resolved[0].lot_size
        for i, leg in enumerate(strategy.legs):
            q = raw_quotes.get(resolved[i].instrument_token)
            if q:
                spread_pct = float(q.spread_pct)
                if leg.side == Side.SELL:
                    net_preview += q.bid
                else:
                    net_preview -= q.ask
                click.echo(
                    f"  [{i}] {leg.side.value:<5} {leg.strike:>8} {leg.option_type.value:<4}  "
                    f"{float(q.bid):>8.2f} {float(q.ask):>8.2f} {spread_pct:>7.1f}%"
                )
            else:
                click.echo(f"  [{i}] {leg.side.value:<5} {leg.strike:>8} {leg.option_type.value:<4}  NO QUOTE")
        click.echo(f"\n  Net credit (preview):  {float(net_preview):.2f} pts/share")
        click.echo(f"  Net credit (1 lot):    Rs {float(net_preview) * lot_size:,.2f}")
    except Exception as e:
        click.echo(f"  [WARN] Could not fetch preview quotes: {e}")

    click.echo()

    # Build PaperBroker using live Zerodha as the quote source
    broker = PaperBroker(
        live_quote_source=live_broker,
        fill_mode=paper_cfg.fill_mode,
        fill_delay_seconds=0,
        slippage_per_order=paper_cfg.slippage_per_order,
    )

    ks = KillSwitch()
    sm = StateMachine()
    alerts = AlertManager()

    run_id = str(uuid.uuid4())
    engine_kwargs = dict(
        strategy=strategy,
        resolved=resolved,
        broker=broker,
        risk=risk,
        kill_switch=ks,
        alert_manager=alerts,
        state_machine=sm,
        strategy_run_id=run_id,
    )

    # Wire up database
    try:
        engine = create_db_engine(db_path)
        with engine.connect() as conn:
            sr = StrategyRepository(conn)
            sr.create(run_id, strategy.name, strategy.underlying,
                      strategy.exchange, str(strategy.expiry),
                      strategy.lots, "PAPER")
    except Exception as e:
        click.echo(f"  [WARN] DB not available: {e}")

    entry = EntryEngine(**engine_kwargs)
    tracker = entry.run()

    lot_size = resolved[0].lot_size
    click.echo(f"\n{'='*60}")
    click.echo(f"  PAPER ENTRY RESULT")
    click.echo(f"{'='*60}")
    click.echo(f"  Run ID:       {run_id}")
    click.echo(f"  Final state:  {sm.state.value}")
    summary = tracker.summary()
    click.echo(f"  Filled legs:  {summary['filled_legs']} / {summary['total_legs']}")
    if tracker.all_filled:
        sides = {i: leg.side for i, leg in enumerate(strategy.legs)}
        credit = tracker.actual_net_credit(sides)
        total = float(credit) * lot_size * strategy.lots if credit else None
        click.echo(f"  Net credit:   {credit} pts/share")
        click.echo(f"  Total credit: \u20b9{total:,.2f}" if total else "  Total credit: N/A")
        click.echo(f"\n  Entry SUCCESSFUL in paper mode.")
        click.echo(f"  Run 'python -m cli.main status' to monitor positions.")
    else:
        click.echo(f"  Unfilled:     {summary['unfilled_legs']}")
        click.echo(f"\n  Entry did NOT complete. Check alerts above.")
    click.echo(f"{'='*60}\n")


@cli.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show system status: config, trading mode, DB, kill switch."""
    from storage.database import create_db_engine
    from risk.kill_switch import KillSwitch

    click.echo(f"\n{'='*50}")
    click.echo(f"  SYSTEM STATUS")
    click.echo(f"{'='*50}")

    mode = os.environ.get("TRADING_MODE", "PAPER")
    click.echo(f"  Trading mode:   {mode}")
    if mode == "LIVE":
        conf = os.environ.get("LIVE_TRADING_CONFIRMATION", "NOT SET")
        click.echo(f"  Confirmation:   {conf}")

    ks = KillSwitch()
    click.echo(f"  Kill switch:    {'ACTIVE' if ks.is_active() else 'inactive'}")

    db_path = os.environ.get("DB_PATH", "logs/trading.db")
    db_exists = Path(db_path).exists()
    click.echo(f"  Database:       {db_path} ({'exists' if db_exists else 'not found'})")

    strategy_path = ctx.obj.get("strategy_path", "config/strategy.yaml")
    risk_path = ctx.obj.get("risk_path", "config/risk.yaml")
    click.echo(f"  Strategy cfg:   {strategy_path} ({'ok' if Path(strategy_path).exists() else 'missing'})")
    click.echo(f"  Risk cfg:       {risk_path} ({'ok' if Path(risk_path).exists() else 'missing'})")
    click.echo(f"{'='*50}\n")


@cli.command("emergency-exit")
@click.option("--confirm", is_flag=True, default=False)
@click.pass_context
def emergency_exit(ctx: click.Context, confirm: bool) -> None:
    """Activate the kill switch to halt all operations immediately."""
    from risk.kill_switch import KillSwitch
    if not confirm:
        click.echo("\nThis will activate the kill switch and halt all operations.")
        click.echo("Re-run with --confirm to proceed.\n")
        return
    ks = KillSwitch()
    ks.activate("Manual emergency exit via CLI")
    click.echo("\n[EMERGENCY] Kill switch activated. All operations halted.\n")


@cli.command("fetch-quotes")
@click.pass_context
def fetch_quotes(ctx: click.Context) -> None:
    """
    Fetch live quotes for all strategy legs from Zerodha.
    Displays bid, ask, LTP, spread, OI for each leg.
    Requires ZERODHA_ACCESS_TOKEN to be set.
    Does NOT place any orders.
    """
    strategy = _load_strategy(ctx.obj["strategy_path"])
    risk, _, _ = _load_risk(ctx.obj["risk_path"])

    try:
        from brokers.zerodha import ZerodhaBroker
        from execution.quote_engine import QuoteEngine

        broker = ZerodhaBroker.from_env()
    except Exception as e:
        click.echo(f"\nCannot connect to Zerodha: {e}", err=True)
        sys.exit(1)

    # Resolve instruments
    resolved = {}
    for i, leg in enumerate(strategy.legs):
        try:
            resolved[i] = broker.resolve_instrument(
                underlying=strategy.underlying,
                exchange=strategy.exchange,
                expiry=strategy.expiry,
                strike=leg.strike,
                option_type=leg.option_type.value,
            )
        except Exception as e:
            click.echo(f"Cannot resolve leg {i}: {e}", err=True)
            sys.exit(1)

    instruments = [(r.instrument_token, r.trading_symbol) for r in resolved.values()]
    qe = QuoteEngine(broker)

    try:
        quotes = qe.fetch_all(instruments)
    except Exception as e:
        click.echo(f"\nQuote fetch failed: {e}", err=True)
        sys.exit(1)

    click.echo(f"\n{'='*85}")
    click.echo(f"  LIVE QUOTES — {strategy.underlying}  Expiry: {strategy.expiry}")
    click.echo(f"{'='*85}")
    click.echo(f"  {'SYMBOL':<35} {'SIDE':<5} {'BID':>8} {'ASK':>8} {'LTP':>8} {'SPREAD%':>8} {'OI':>10}")
    click.echo(f"  {'-'*82}")

    legs = strategy.legs
    for i, (leg_idx, inst) in enumerate(resolved.items()):
        leg = legs[leg_idx]
        q = quotes.get(inst.trading_symbol)
        if q is None:
            click.echo(f"  {inst.trading_symbol:<35} {leg.side.value:<5} {'N/A':>8} {'N/A':>8} {'N/A':>8} {'N/A':>8} {'N/A':>10}")
            continue
        click.echo(
            f"  {inst.trading_symbol:<35} {leg.side.value:<5} "
            f"{q.bid:>8.2f} {q.ask:>8.2f} {q.ltp:>8.2f} "
            f"{float(q.spread_pct):>7.1f}% {q.oi:>10,}"
        )

    # Basket credit calculation
    qe2 = QuoteEngine(broker)
    quotes_by_symbol = {inst.trading_symbol: quotes.get(inst.trading_symbol)
                        for inst in resolved.values()
                        if quotes.get(inst.trading_symbol) is not None}

    if len(quotes_by_symbol) == len(resolved):
        resolved_symbols = {i: r.trading_symbol for i, r in resolved.items()}
        lot_size = next(iter(resolved.values())).lot_size
        try:
            credit = qe2.calculate_executable_basket_credit(
                legs=strategy.legs,
                quotes=quotes_by_symbol,
                lot_size=lot_size,
                lots=strategy.lots,
                resolved_symbols=resolved_symbols,
            )
            click.echo(f"\n  Lot size:              {lot_size}")
            click.echo(f"  Buy cost (ask):        {credit.buy_cost:.2f} pts")
            click.echo(f"  Sell credit (bid):     {credit.sell_credit:.2f} pts")
            click.echo(f"  Net credit per share:  {credit.net_credit:.2f} pts")
            click.echo(f"  Net credit total:      \u20b9{credit.net_credit_total:.2f}")
            click.echo(f"  Min required credit:   {risk.min_net_credit} pts")
            status_str = "ABOVE MINIMUM \u2713" if credit.net_credit >= risk.min_net_credit else "BELOW MINIMUM \u2717"
            click.echo(f"  Status:                {status_str}")
        except Exception as e:
            click.echo(f"\n  Could not calculate basket credit: {e}")

    # Validation
    errors = qe.validate_basket(quotes_by_symbol, risk)
    if errors:
        click.echo(f"\n  Validation errors:")
        for e in errors:
            click.echo(f"    \u2717 {e}")
    else:
        click.echo(f"\n  All quotes valid.")

    click.echo(f"{'='*85}\n")


@cli.command("check-margin")
@click.pass_context
def check_margin(ctx: click.Context) -> None:
    """
    Calculate estimated margin for the strategy using Zerodha's basket margin API.
    Read-only. Does NOT place any orders.
    Requires ZERODHA_ACCESS_TOKEN.
    """
    strategy = _load_strategy(ctx.obj["strategy_path"])
    risk, _, _ = _load_risk(ctx.obj["risk_path"])

    try:
        from brokers.zerodha import ZerodhaBroker
        broker = ZerodhaBroker.from_env()
    except Exception as e:
        click.echo(f"\nCannot connect to Zerodha: {e}", err=True)
        sys.exit(1)

    resolved = {}
    for i, leg in enumerate(strategy.legs):
        try:
            resolved[i] = broker.resolve_instrument(
                underlying=strategy.underlying,
                exchange=strategy.exchange,
                expiry=strategy.expiry,
                strike=leg.strike,
                option_type=leg.option_type.value,
            )
        except Exception as e:
            click.echo(f"Cannot resolve leg {i}: {e}", err=True)
            sys.exit(1)

    # Build Zerodha basket_order_margins format
    from execution.models import Side
    kite_orders = []
    for i, leg in enumerate(strategy.legs):
        inst = resolved[i]
        lot_size = inst.lot_size
        quantity = lot_size * strategy.lots
        kite_orders.append({
            "exchange": strategy.exchange,
            "tradingsymbol": inst.trading_symbol,
            "transaction_type": "BUY" if leg.side == Side.BUY else "SELL",
            "variety": "regular",
            "product": "MIS",
            "order_type": "LIMIT",
            "quantity": quantity,
            "price": 0,
        })

    try:
        margin_data = broker.get_basket_margin(kite_orders)
    except Exception as e:
        click.echo(f"\nMargin calculation failed: {e}", err=True)
        sys.exit(1)

    required = Decimal(str(margin_data.get("required", 0) or 0))
    available = Decimal(str(margin_data.get("available", {}).get("cash", 0) or 0))
    charges = margin_data.get("charges", {})
    total_charges = Decimal(str(charges.get("total", 0) or 0))

    click.echo(f"\n{'='*50}")
    click.echo(f"  MARGIN ESTIMATE")
    click.echo(f"{'='*50}")
    click.echo(f"  Required margin:   \u20b9{required:,.2f}")
    click.echo(f"  Available cash:    \u20b9{available:,.2f}")
    click.echo(f"  Estimated charges: \u20b9{total_charges:,.2f}")
    click.echo(f"  Capital limit:     \u20b9{risk.capital_limit:,.2f}")

    if required > risk.capital_limit:
        click.echo(f"\n  \u2717 Required margin EXCEEDS capital limit. Cannot enter.")
    elif required > available:
        click.echo(f"\n  \u2717 Insufficient available funds.")
    else:
        click.echo(f"\n  \u2713 Margin within limits.")
    click.echo(f"{'='*50}\n")


@cli.command("account-status")
@click.pass_context
def account_status(ctx: click.Context) -> None:
    """
    Show Zerodha account: open positions, today's orders, available margins.
    Read-only. Does NOT place any orders.
    """
    try:
        from brokers.zerodha import ZerodhaBroker
        broker = ZerodhaBroker.from_env()
    except Exception as e:
        click.echo(f"\nCannot connect to Zerodha: {e}", err=True)
        sys.exit(1)

    # Margins
    try:
        margins = broker.get_margins()
        equity = margins.get("equity", {})
        available = equity.get("available", {}).get("cash", "N/A")
        used = equity.get("utilised", {}).get("debits", "N/A")
        click.echo(f"\n{'='*50}")
        click.echo(f"  ACCOUNT MARGINS")
        click.echo(f"{'='*50}")
        click.echo(f"  Available cash:  \u20b9{available}")
        click.echo(f"  Margin used:     \u20b9{used}")
    except Exception as e:
        click.echo(f"  Margin fetch error: {e}")

    # Positions
    try:
        positions = broker.get_positions()
        click.echo(f"\n  Open positions: {len(positions)}")
        if positions:
            click.echo(f"  {'SYMBOL':<30} {'QTY':>6} {'AVG PRICE':>12} {'P&L':>10}")
            click.echo(f"  {'-'*62}")
            for p in positions:
                click.echo(
                    f"  {p.get('tradingsymbol', ''):<30} "
                    f"{p.get('net_quantity', 0):>6} "
                    f"{p.get('average_price', 0):>12.2f} "
                    f"{p.get('pnl', 0):>10.2f}"
                )
    except Exception as e:
        click.echo(f"  Position fetch error: {e}")

    # Orders
    try:
        orders = broker.get_orders()
        open_orders = [o for o in orders if o.get("status") in ("OPEN", "TRIGGER PENDING")]
        click.echo(f"\n  Today's orders: {len(orders)}  (open: {len(open_orders)})")
        if open_orders:
            click.echo(f"  {'ORDER ID':<20} {'SYMBOL':<30} {'TYPE':<5} {'QTY':>6} {'PRICE':>10} {'STATUS':<15}")
            click.echo(f"  {'-'*90}")
            for o in open_orders:
                click.echo(
                    f"  {str(o.get('order_id','')):<20} "
                    f"{o.get('tradingsymbol',''):<30} "
                    f"{o.get('transaction_type',''):<5} "
                    f"{o.get('quantity',0):>6} "
                    f"{o.get('price',0):>10.2f} "
                    f"{o.get('status',''):<15}"
                )
    except Exception as e:
        click.echo(f"  Order fetch error: {e}")

    click.echo(f"{'='*50}\n")


def _print_preflight_table(
    strategy,
    resolved: dict,
    quotes: dict,
    payoff,
    estimated_margin,
    risk,
) -> None:
    """Print the pre-flight confirmation table before any live order."""
    from decimal import Decimal
    print(f"\n{'='*80}")
    print(f"  *** LIVE ORDER PRE-FLIGHT CONFIRMATION ***")
    print(f"  {'Strategy:':<25} {strategy.name}")
    print(f"  {'Underlying:':<25} {strategy.underlying}")
    print(f"  {'Expiry:':<25} {strategy.expiry}")
    print(f"  {'Lots:':<25} {strategy.lots}")
    print(f"{'='*80}")
    print(f"\n  {'#':<3} {'SIDE':<5} {'SYMBOL':<35} {'TOKEN':>10} {'BID':>8} {'ASK':>8} {'LIMIT':>8}")
    print(f"  {'-'*80}")
    for i, leg in enumerate(strategy.legs):
        inst = resolved.get(i)
        if inst is None:
            print(f"  [{i}] UNRESOLVED")
            continue
        q = quotes.get(inst.trading_symbol)
        bid = f"{float(q.bid):.2f}" if q else "N/A"
        ask = f"{float(q.ask):.2f}" if q else "N/A"
        from execution.pricing import initial_limit_price
        if q:
            from execution.models import Side
            limit = initial_limit_price(q, leg.side)
            limit_str = f"{float(limit):.2f}"
        else:
            limit_str = "N/A"
        print(
            f"  [{i}] {leg.side.value:<5} {inst.trading_symbol:<35} "
            f"{inst.instrument_token:>10} {bid:>8} {ask:>8} {limit_str:>8}"
        )
    print(f"\n  {'Expected net credit:':<30} {payoff.net_credit_per_share:.2f} pts/share")
    print(f"  {'Expected total credit:':<30} ₹{float(payoff.net_credit_total):.2f}")
    print(f"  {'Theoretical max profit:':<30} ₹{float(payoff.max_profit):.2f}")
    print(f"  {'Theoretical max loss:':<30} ₹{float(payoff.max_loss):.2f}")
    print(f"  {'Lower breakeven:':<30} {payoff.lower_breakeven:.2f}")
    print(f"  {'Upper breakeven:':<30} {payoff.upper_breakeven:.2f}")
    if estimated_margin:
        print(f"  {'Estimated margin:':<30} ₹{float(estimated_margin):.2f}")
    print(f"  {'Capital limit:':<30} ₹{float(risk.capital_limit):.2f}")
    print(f"  {'Max strategy loss:':<30} ₹{float(risk.max_strategy_loss):.2f}")
    print(f"\n  *** ALL ORDERS ARE LIMIT ORDERS ON LIVE EXCHANGE ***")
    print(f"{'='*80}\n")


@cli.command("live-enter")
@click.option("--confirm", is_flag=True, default=False,
              help="Required flag. Without it, no orders are placed.")
@click.option("--product", default="MIS", show_default=True,
              help="Zerodha product type: MIS or NRML")
@click.pass_context
def live_enter(ctx: click.Context, confirm: bool, product: str) -> None:
    """
    Execute a live iron condor entry on Zerodha.
    Requires --confirm flag AND both env vars:
      TRADING_MODE=LIVE
      LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED
    """
    import sys
    from decimal import Decimal
    from zoneinfo import ZoneInfo
    from brokers.zerodha import ZerodhaBroker, LiveTradingBlockedError
    from brokers.base import BrokerError
    from execution.entry_engine import EntryEngine
    from execution.live_order_manager import LiveOrderManager
    from execution.models import (
        OptionType, ResolvedInstrument, Side, StrategyState,
    )
    from execution.process_lock import ProcessLock, ProcessLockError
    from execution.quote_engine import QuoteEngine
    from execution.state_machine import StateMachine
    from monitoring.alerts import AlertManager
    from risk.kill_switch import KillSwitch
    from risk.payoff import calculate_payoff_from_credit
    from risk.validator import PreTradeValidator
    from storage.database import create_db_engine
    from storage.repositories import (
        AlertRepository, FillRepository, OrderRepository,
        StrategyRepository, SystemEventRepository,
    )

    IST = ZoneInfo("Asia/Kolkata")

    # ── Safety gate 1: --confirm flag ─────────────────────────────────────
    if not confirm:
        click.echo(
            "\nThis command places REAL orders on the live exchange.\n"
            "You must also set in your environment:\n"
            "  TRADING_MODE=LIVE\n"
            "  LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED\n"
            "\nRe-run with --confirm to proceed.\n",
            err=True,
        )
        sys.exit(1)

    # ── Safety gate 2: env vars ────────────────────────────────────────────
    mode = os.environ.get("TRADING_MODE", "PAPER")
    confirmation = os.environ.get("LIVE_TRADING_CONFIRMATION", "")
    if mode != "LIVE" or confirmation != "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED":
        click.echo(
            "\nLIVE TRADING BLOCKED.\n"
            "Both of the following must be set:\n"
            "  TRADING_MODE=LIVE\n"
            "  LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED\n",
            err=True,
        )
        sys.exit(1)

    strategy = _load_strategy(ctx.obj["strategy_path"])
    risk, exit_rules, _ = _load_risk(ctx.obj["risk_path"])
    db_path = os.environ.get("DB_PATH", "logs/trading.db")

    # ── Process lock ───────────────────────────────────────────────────────
    lock = ProcessLock()
    try:
        lock.acquire()
    except ProcessLockError as e:
        click.echo(f"\nCannot start: {e}\n", err=True)
        sys.exit(1)

    # ── Kill switch ────────────────────────────────────────────────────────
    ks = KillSwitch()
    if ks.is_active():
        lock.release()
        click.echo("\nKill switch is ACTIVE. Deactivate it before entering.\n", err=True)
        sys.exit(1)

    try:
        # ── Connect to Zerodha ─────────────────────────────────────────────
        try:
            broker = ZerodhaBroker.from_env()
        except BrokerError as e:
            click.echo(f"\nBroker connection failed: {e}\n", err=True)
            sys.exit(1)

        # ── Resolve instruments ────────────────────────────────────────────
        click.echo(f"\nResolving instruments...")
        resolved = {}
        for i, leg in enumerate(strategy.legs):
            try:
                resolved[i] = broker.resolve_instrument(
                    underlying=strategy.underlying,
                    exchange=strategy.exchange,
                    expiry=strategy.expiry,
                    strike=leg.strike,
                    option_type=leg.option_type.value,
                )
            except Exception as e:
                click.echo(f"  Failed to resolve leg {i}: {e}", err=True)
                sys.exit(1)
        click.echo(f"  All {len(resolved)} instruments resolved.")

        lot_size = next(iter(resolved.values())).lot_size

        # ── Fetch live quotes ──────────────────────────────────────────────
        click.echo(f"\nFetching live quotes...")
        qe = QuoteEngine(broker)
        instruments = [(r.instrument_token, r.trading_symbol) for r in resolved.values()]
        try:
            quotes = qe.fetch_all(instruments)
        except Exception as e:
            click.echo(f"  Quote fetch failed: {e}", err=True)
            sys.exit(1)

        resolved_symbols = {i: r.trading_symbol for i, r in resolved.items()}
        credit = qe.calculate_executable_basket_credit(
            strategy.legs, quotes, lot_size, strategy.lots, resolved_symbols
        )

        # ── Payoff calculation ─────────────────────────────────────────────
        payoff = calculate_payoff_from_credit(strategy, credit.net_credit, lot_size)

        # ── Margin check ───────────────────────────────────────────────────
        estimated_margin = None
        try:
            kite_orders = []
            for i, leg in enumerate(strategy.legs):
                inst = resolved[i]
                kite_orders.append({
                    "exchange": strategy.exchange,
                    "tradingsymbol": inst.trading_symbol,
                    "transaction_type": "BUY" if leg.side == Side.BUY else "SELL",
                    "variety": "regular",
                    "product": product,
                    "order_type": "LIMIT",
                    "quantity": inst.lot_size * strategy.lots,
                    "price": 0,
                })
            margin_data = broker.get_basket_margin(kite_orders)
            estimated_margin = Decimal(str(margin_data.get("required", 0) or 0))
        except Exception as e:
            click.echo(f"  [WARN] Margin check failed: {e}")

        # ── Pre-trade validation ───────────────────────────────────────────
        validator = PreTradeValidator(risk)
        quotes_by_symbol = {r.trading_symbol: quotes.get(r.trading_symbol)
                            for r in resolved.values()
                            if quotes.get(r.trading_symbol) is not None}
        report = validator.validate(strategy, quotes_by_symbol, payoff, estimated_margin)
        report.print_table()

        if not report.passed:
            click.echo(f"\nPre-trade validation FAILED. Cannot proceed.\n", err=True)
            sys.exit(1)

        # ── Pre-flight table ───────────────────────────────────────────────
        _print_preflight_table(strategy, resolved, quotes_by_symbol, payoff, estimated_margin, risk)

        # ── Final manual confirmation ──────────────────────────────────────
        click.echo("Type CONFIRM to place live orders, or anything else to abort:")
        user_input = input("> ").strip()
        if user_input != "CONFIRM":
            click.echo("\nAborted by user. No orders placed.\n")
            sys.exit(0)

        # ── Build DB session ───────────────────────────────────────────────
        sm = StateMachine()
        alerts = AlertManager()
        run_id = str(uuid.uuid4())

        try:
            engine = create_db_engine(db_path)
            conn = engine.connect()
            sr = StrategyRepository(conn)
            or_ = OrderRepository(conn)
            fr = FillRepository(conn)
            sr.create(run_id, strategy.name, strategy.underlying,
                      strategy.exchange, str(strategy.expiry), strategy.lots, "LIVE")
        except Exception as e:
            click.echo(f"  [WARN] DB error: {e}")
            conn = None

        # ── Live entry ─────────────────────────────────────────────────────
        live_om = LiveOrderManager(
            broker=broker,
            risk=risk,
            kill_switch=ks,
            order_repo=or_ if conn else None,
            fill_repo=fr if conn else None,
        )

        entry = EntryEngine(
            strategy=strategy,
            resolved=resolved,
            broker=broker,       # EntryEngine will use live_om instead
            risk=risk,
            kill_switch=ks,
            alert_manager=alerts,
            state_machine=sm,
            order_manager=live_om,
            strategy_run_id=run_id,
        )

        click.echo(f"\n[LIVE] Starting entry — Run ID: {run_id}")
        tracker = entry.run()

        click.echo(f"\n{'='*60}")
        click.echo(f"  LIVE ENTRY RESULT")
        click.echo(f"{'='*60}")
        click.echo(f"  Run ID:      {run_id}")
        click.echo(f"  State:       {sm.state.value}")
        summary = tracker.summary()
        click.echo(f"  Filled:      {summary['filled_legs']} / {summary['total_legs']}")
        if tracker.all_filled:
            sides = {i: leg.side for i, leg in enumerate(strategy.legs)}
            actual_credit = tracker.actual_net_credit(sides)
            click.echo(f"  Net credit:  {actual_credit} pts/share")
            click.echo(f"\n  *** ENTRY COMPLETE — Position is LIVE ***")
        else:
            click.echo(f"  Unfilled:    {summary['unfilled_legs']}")
            click.echo(f"\n  *** ENTRY INCOMPLETE — Check positions immediately ***")
        click.echo(f"{'='*60}\n")

    finally:
        lock.release()


@cli.command("live-exit")
@click.option("--confirm", is_flag=True, default=False)
@click.option("--run-id", required=True, help="Strategy run ID from live-enter")
@click.option("--product", default="MIS", show_default=True)
@click.pass_context
def live_exit(ctx: click.Context, confirm: bool, run_id: str, product: str) -> None:
    """
    Execute a live exit for an open iron condor position.
    Requires --confirm AND TRADING_MODE=LIVE env vars.
    """
    import sys
    from brokers.zerodha import ZerodhaBroker, LiveTradingBlockedError
    from brokers.base import BrokerError
    from execution.models import ExitTrigger, LegPosition, OptionType, Side, StrategyState
    from execution.exit_engine import ExitEngine
    from execution.live_order_manager import LiveOrderManager
    from execution.process_lock import ProcessLock, ProcessLockError
    from execution.state_machine import StateMachine
    from monitoring.alerts import AlertManager
    from risk.kill_switch import KillSwitch
    from decimal import Decimal

    if not confirm:
        click.echo("\nRe-run with --confirm to place live exit orders.\n", err=True)
        sys.exit(1)

    mode = os.environ.get("TRADING_MODE", "PAPER")
    confirmation = os.environ.get("LIVE_TRADING_CONFIRMATION", "")
    if mode != "LIVE" or confirmation != "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED":
        click.echo("\nLIVE TRADING BLOCKED. Set TRADING_MODE=LIVE and LIVE_TRADING_CONFIRMATION.\n", err=True)
        sys.exit(1)

    ks = KillSwitch()
    lock = ProcessLock()
    try:
        lock.acquire()
    except ProcessLockError as e:
        click.echo(f"\n{e}\n", err=True)
        sys.exit(1)

    try:
        try:
            broker = ZerodhaBroker.from_env()
        except BrokerError as e:
            click.echo(f"\nBroker error: {e}\n", err=True)
            sys.exit(1)

        strategy = _load_strategy(ctx.obj["strategy_path"])
        risk, exit_rules, _ = _load_risk(ctx.obj["risk_path"])

        # Fetch current positions from broker and build LegPosition list
        broker_positions = broker.get_positions()
        click.echo(f"\nOpen positions from broker: {len(broker_positions)}")

        # Resolve instruments to match positions
        resolved = {}
        for i, leg in enumerate(strategy.legs):
            try:
                resolved[i] = broker.resolve_instrument(
                    underlying=strategy.underlying,
                    exchange=strategy.exchange,
                    expiry=strategy.expiry,
                    strike=leg.strike,
                    option_type=leg.option_type.value,
                )
            except Exception as e:
                click.echo(f"  Failed to resolve leg {i}: {e}", err=True)
                sys.exit(1)

        # Build LegPosition list from resolved instruments and broker positions
        broker_pos_map = {p.get("tradingsymbol", ""): p for p in broker_positions}
        leg_positions = []
        for i, leg in enumerate(strategy.legs):
            inst = resolved[i]
            bp = broker_pos_map.get(inst.trading_symbol, {})
            net_qty = abs(int(bp.get("net_quantity", inst.lot_size * strategy.lots)))
            avg_price = Decimal(str(bp.get("average_price", 0) or 0))
            leg_positions.append(LegPosition(
                leg_index=i,
                trading_symbol=inst.trading_symbol,
                instrument_token=inst.instrument_token,
                side=leg.side,
                option_type=leg.option_type,
                strike=leg.strike,
                lot_size=inst.lot_size,
                lots=strategy.lots,
                quantity=net_qty,
                average_entry_price=avg_price if avg_price > 0 else Decimal("1"),
            ))

        click.echo(f"\nPositions to close:")
        for lp in leg_positions:
            close_side = "BUY" if lp.side == Side.SELL else "SELL"
            click.echo(f"  {close_side} {lp.quantity} {lp.trading_symbol}")

        click.echo("\nType CONFIRM to place live exit orders, or anything else to abort:")
        user_input = input("> ").strip()
        if user_input != "CONFIRM":
            click.echo("\nAborted by user.\n")
            sys.exit(0)

        sm = StateMachine(initial=StrategyState.MONITORING)
        alerts = AlertManager()
        live_om = LiveOrderManager(broker=broker, risk=risk, kill_switch=ks)

        exit_engine = ExitEngine(
            strategy=strategy,
            positions=leg_positions,
            broker=broker,
            risk=risk,
            kill_switch=ks,
            alert_manager=alerts,
            state_machine=sm,
            exit_trigger=ExitTrigger.MANUAL,
            order_manager=live_om,
            strategy_run_id=run_id,
        )

        click.echo(f"\n[LIVE] Starting exit — Run ID: {run_id}")
        tracker = exit_engine.run()

        summary = tracker.summary()
        click.echo(f"\n{'='*50}")
        click.echo(f"  LIVE EXIT RESULT")
        click.echo(f"{'='*50}")
        click.echo(f"  State:    {sm.state.value}")
        click.echo(f"  Filled:   {summary['filled_legs']} / {summary['total_legs']}")
        if summary['unfilled_legs']:
            click.echo(f"  Unfilled: {summary['unfilled_legs']}")
            click.echo(f"\n  *** EXIT INCOMPLETE — Check broker positions immediately ***")
        else:
            click.echo(f"\n  *** EXIT COMPLETE ***")
        click.echo(f"{'='*50}\n")

    finally:
        lock.release()


@cli.command("cancel-open-orders")
@click.option("--confirm", is_flag=True, default=False)
@click.pass_context
def cancel_open_orders(ctx: click.Context, confirm: bool) -> None:
    """
    Cancel ALL open orders on Zerodha.
    Requires --confirm AND TRADING_MODE=LIVE env vars.
    """
    import sys
    from brokers.zerodha import ZerodhaBroker
    from brokers.base import BrokerError

    if not confirm:
        click.echo("\nRe-run with --confirm to cancel all open orders.\n")
        return

    mode = os.environ.get("TRADING_MODE", "PAPER")
    confirmation = os.environ.get("LIVE_TRADING_CONFIRMATION", "")
    if mode != "LIVE" or confirmation != "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED":
        click.echo("\nLIVE mode not confirmed. Set TRADING_MODE=LIVE and LIVE_TRADING_CONFIRMATION.\n", err=True)
        sys.exit(1)

    try:
        broker = ZerodhaBroker.from_env()
    except BrokerError as e:
        click.echo(f"\nBroker error: {e}\n", err=True)
        sys.exit(1)

    try:
        cancelled = broker.cancel_all_open_orders()
        click.echo(f"\nCancelled {len(cancelled)} open order(s): {cancelled}\n")
    except BrokerError as e:
        click.echo(f"\nError cancelling orders: {e}\n", err=True)
        sys.exit(1)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
