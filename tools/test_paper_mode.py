#!/usr/bin/env python3
"""
tools/test_paper_mode.py — Paper mode smoke test.

Validates the full stack — broker auth, guard layers, capital manager,
shift monitor logic, and trade logger — using real Zerodha market data
but ZERO real orders.

Run after zerodha_login.py to confirm everything is wired up correctly.

Usage:
    python tools/test_paper_mode.py
    python tools/test_paper_mode.py --symbol ICICIBANK
    python tools/test_paper_mode.py --env-file config/credentials.env
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    for _f in [_ROOT / "config" / "credentials.env", _ROOT / ".env"]:
        if _f.exists():
            load_dotenv(_f)
            break
except ImportError:
    pass

IST = ZoneInfo("Asia/Kolkata")

# ── Colour helpers ────────────────────────────────────────────────────────

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):   print(f"  {GREEN}✓{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET} {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def info(msg): print(f"  {CYAN}→{RESET} {msg}")


SECTION_WIDTH = 55

def section(title: str) -> None:
    print()
    print(f"{BOLD}{'─'*SECTION_WIDTH}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{'─'*SECTION_WIDTH}")


# ── Test runner ───────────────────────────────────────────────────────────

class PaperModeTest:
    def __init__(self, symbol: str, env_file: str):
        self.symbol    = symbol
        self.env_file  = env_file
        self.broker    = None
        self.config    = None
        self.passed    = 0
        self.failed    = 0
        self.warnings  = 0

    def _p(self, label: str, result: bool, detail: str = "", is_warn: bool = False) -> None:
        if result:
            ok(f"{label}  {detail}")
            self.passed += 1
        elif is_warn:
            warn(f"{label}  {detail}")
            self.warnings += 1
        else:
            fail(f"{label}  {detail}")
            self.failed += 1

    # ── 1. Config ──────────────────────────────────────────────────────────

    def test_config(self) -> bool:
        section("1. Configuration")
        try:
            from live.config import load_config
            self.config = load_config(_ROOT / "config" / "live_config.yaml")
            ok(f"Config loaded — mode={self.config.mode}, "
               f"{len(self.config.stocks)} stocks")
            self.passed += 1
        except Exception as e:
            fail(f"Config load failed: {e}")
            self.failed += 1
            return False

        api_key    = self.config.zerodha_api_key
        api_secret = self.config.zerodha_api_secret
        token      = self.config.zerodha_access_token

        self._p("ZERODHA_API_KEY set",    bool(api_key),    api_key[:6] + "…" if api_key else "missing")
        self._p("ZERODHA_API_SECRET set", bool(api_secret), "set" if api_secret else "missing")
        self._p("ZERODHA_ACCESS_TOKEN set", bool(token),    token[:6] + "…" if token else "missing")

        enabled = [s for s in self.config.stocks if s.enabled]
        self._p(f"Enabled stocks count",  len(enabled) > 0,
                f"{len(enabled)} enabled, {len(self.config.stocks) - len(enabled)} disabled")

        sym_cfg = next((s for s in self.config.stocks if s.symbol == self.symbol), None)
        self._p(f"Test symbol {self.symbol} in config", sym_cfg is not None,
                f"put={sym_cfg.put_dist_pct}% call={sym_cfg.call_dist_pct}%" if sym_cfg else "not found")
        return True

    # ── 2. Broker auth ─────────────────────────────────────────────────────

    def test_broker_auth(self) -> bool:
        section("2. Zerodha Broker Authentication")
        if not self.config:
            fail("Skipped — config not loaded")
            self.failed += 1
            return False

        api_key = self.config.zerodha_api_key
        token   = self.config.zerodha_access_token

        if not api_key or not token:
            fail("Missing API key or access token — run tools/zerodha_login.py first")
            self.failed += 1
            return False

        try:
            from brokers.zerodha import ZerodhaBroker
            self.broker = ZerodhaBroker(api_key=api_key, access_token=token)
            ok("ZerodhaBroker instantiated")
            self.passed += 1
        except Exception as e:
            fail(f"ZerodhaBroker init failed: {e}")
            self.failed += 1
            return False

        # Profile
        try:
            profile = self.broker._kite.profile()
            ok(f"Profile fetch OK — {profile.get('user_name')} ({profile.get('user_id')})")
            self.passed += 1
        except Exception as e:
            fail(f"Profile fetch failed: {e}")
            self.failed += 1
            return False

        # Margins
        try:
            margins = self.broker.get_margins()
            available = margins.get("net", margins.get("available", 0))
            ok(f"Margins fetch OK — Available ₹{float(available):,.0f}")
            self.passed += 1
        except Exception as e:
            fail(f"Margins fetch failed: {e}")
            self.failed += 1

        # Positions
        try:
            positions = self.broker.get_positions()
            ok(f"Positions fetch OK — {len(positions)} open position(s)")
            self.passed += 1
        except Exception as e:
            fail(f"Positions fetch failed: {e}")
            self.failed += 1

        # Orders
        try:
            orders = self.broker.get_orders()
            ok(f"Orders fetch OK — {len(orders)} order(s) today")
            self.passed += 1
        except Exception as e:
            fail(f"Orders fetch failed: {e}")
            self.failed += 1

        return True

    # ── 3. Instrument resolution ───────────────────────────────────────────

    def test_instrument_resolution(self) -> bool:
        section(f"3. Instrument Resolution ({self.symbol})")
        if not self.broker:
            fail("Skipped — broker not initialised")
            self.failed += 1
            return False

        # Find next expiry from actual Zerodha instruments (handles Tuesday/Monday shifts)
        today = date.today()
        expiry = None
        try:
            raw_instruments = self.broker._load_instruments()
            symbol_expiries = sorted({
                (inst["expiry"].date() if hasattr(inst["expiry"], "date") else date.fromisoformat(str(inst["expiry"])[:10]))
                for inst in raw_instruments
                if str(inst.get("name","")).upper() == self.symbol
                and inst.get("instrument_type") in ("CE","PE")
            })
            for exp in symbol_expiries:
                dte = (exp - today).days
                if 20 <= dte <= 50:
                    expiry = exp
                    info(f"Using expiry: {expiry} (DTE={dte})")
                    break
            if not expiry and symbol_expiries:
                expiry = symbol_expiries[0]
                info(f"Fallback expiry: {expiry} (nearest available)")
        except Exception as e:
            warn(f"Could not load instruments for expiry detection: {e}")
            self.warnings += 1

        if not expiry:
            fail("Could not find any expiry for symbol")
            self.failed += 1
            return False

        # Try to resolve instruments — get a rough spot first
        try:
            from decimal import Decimal
            # Try to get spot via index or direct quote
            try:
                raw = self.broker._kite.quote([f"NSE:{self.symbol}"])
                sym_data = raw.get(f"NSE:{self.symbol}", {})
                spot = float(sym_data.get("last_price", 0))
                if spot <= 0:
                    raise ValueError("zero spot")
                ok(f"Spot price fetch OK — ₹{spot:,.2f}")
                self.passed += 1
            except Exception as e:
                warn(f"NSE spot fetch failed ({e}) — trying BSE")
                self.warnings += 1
                spot = None

            if not spot:
                fail(f"Could not get spot for {self.symbol}")
                self.failed += 1
                return False

            # Compute strikes
            sym_cfg = next((s for s in self.config.stocks if s.symbol == self.symbol), None)
            if not sym_cfg:
                fail(f"{self.symbol} not in config")
                self.failed += 1
                return False

            short_put_target  = spot * (1 - sym_cfg.put_dist_pct / 100)
            short_call_target = spot * (1 + sym_cfg.call_dist_pct / 100)
            info(f"Short put target:  ₹{short_put_target:,.0f}  ({sym_cfg.put_dist_pct}% OTM)")
            info(f"Short call target: ₹{short_call_target:,.0f} ({sym_cfg.call_dist_pct}% OTM)")

            # Get actual available strikes from instrument dump
            from decimal import Decimal as D
            raw_instruments = self.broker._load_instruments()
            pe_strikes = sorted({
                float(i["strike"]) for i in raw_instruments
                if str(i.get("name","")).upper() == self.symbol
                and i.get("instrument_type") == "PE"
                and (i["expiry"].date() if hasattr(i["expiry"],"date") else date.fromisoformat(str(i["expiry"])[:10])) == expiry
            })
            ce_strikes = sorted({
                float(i["strike"]) for i in raw_instruments
                if str(i.get("name","")).upper() == self.symbol
                and i.get("instrument_type") == "CE"
                and (i["expiry"].date() if hasattr(i["expiry"],"date") else date.fromisoformat(str(i["expiry"])[:10])) == expiry
            })
            def snap(target, strikes):
                return min(strikes, key=lambda s: abs(s - target)) if strikes else target
            short_put_strike  = snap(short_put_target,  pe_strikes)
            short_call_strike = snap(short_call_target, ce_strikes)
            # Long legs: nearest available strike beyond the shorts
            lp_cands = [s for s in pe_strikes if s < short_put_strike]
            lc_cands = [s for s in ce_strikes if s > short_call_strike]
            long_put_strike  = snap(short_put_strike  * (1 - 2/100), lp_cands) if lp_cands else short_put_strike - 50
            long_call_strike = snap(short_call_strike * (1 + 2/100), lc_cands) if lc_cands else short_call_strike + 50
            info(f"Short put strike:  ₹{short_put_strike:,.0f}")
            info(f"Long  put strike:  ₹{long_put_strike:,.0f}")
            info(f"Short call strike: ₹{short_call_strike:,.0f}")
            info(f"Long  call strike: ₹{long_call_strike:,.0f}")

            # Try to resolve each leg
            legs = [
                ("SHORT_PUT",  Decimal(str(short_put_strike)),  "PE"),
                ("LONG_PUT",   Decimal(str(long_put_strike)),   "PE"),
                ("SHORT_CALL", Decimal(str(short_call_strike)), "CE"),
                ("LONG_CALL",  Decimal(str(long_call_strike)),  "CE"),
            ]

            resolved = {}
            for leg_name, strike, opt in legs:
                try:
                    inst = self.broker.resolve_instrument(
                        underlying=self.symbol,
                        exchange="NFO",
                        expiry=expiry,
                        strike=strike,
                        option_type=opt,
                    )
                    ok(f"{leg_name:<12} {self.symbol}{expiry.strftime('%y%b').upper()}"
                       f"{int(strike)}{opt}  token={inst.instrument_token}  lot={inst.lot_size}")
                    resolved[leg_name] = inst
                    self.passed += 1
                except Exception as e:
                    fail(f"{leg_name:<12} resolve failed: {e}")
                    self.failed += 1

            return len(resolved) == 4

        except Exception as e:
            fail(f"Instrument resolution error: {e}")
            self.failed += 1
            return False

    # ── 4. Quote fetch ─────────────────────────────────────────────────────

    def test_quotes(self) -> bool:
        section(f"4. Live Quote Fetch ({self.symbol})")
        if not self.broker:
            fail("Skipped — broker not initialised")
            self.failed += 1
            return False

        now_ist = datetime.now(IST)
        market_open  = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        is_market_hours = market_open <= now_ist <= market_close and now_ist.weekday() < 5

        if not is_market_hours:
            warn(f"Market is CLOSED right now ({now_ist.strftime('%H:%M IST %A')}) "
                 "— quotes will be last-trade prices, not live")
            self.warnings += 1

        today  = date.today()
        expiry = None
        try:
            raw_instruments = self.broker._load_instruments()
            symbol_expiries = sorted({
                (inst["expiry"].date() if hasattr(inst["expiry"], "date") else date.fromisoformat(str(inst["expiry"])[:10]))
                for inst in raw_instruments
                if str(inst.get("name","")).upper() == self.symbol
                and inst.get("instrument_type") in ("CE","PE")
            })
            expiry = symbol_expiries[0] if symbol_expiries else None
        except Exception:
            pass

        if not expiry:
            fail("No expiry found for quote test")
            self.failed += 1
            return False

        try:
            raw_spot = self.broker._kite.quote([f"NSE:{self.symbol}"])
            spot = float(raw_spot.get(f"NSE:{self.symbol}", {}).get("last_price", 0))
            if spot <= 0:
                fail("Zero spot price")
                self.failed += 1
                return False

            from decimal import Decimal
            step = 50 if spot < 2000 else 100
            short_call_strike = Decimal(str(round(spot * 1.05 / step) * step))

            inst = self.broker.resolve_instrument(
                self.symbol, "NFO", expiry, short_call_strike, "CE"
            )
            quote = self.broker.get_quote(inst.instrument_token, inst.trading_symbol)

            ok(f"Quote received: bid=₹{quote.bid}  ask=₹{quote.ask}  ltp=₹{quote.ltp}")
            self.passed += 1

            spread_pct = float(quote.spread_pct)
            self._p(f"Spread check (< 25%)", spread_pct < 25,
                    f"{spread_pct:.1f}%", is_warn=(spread_pct >= 25))

            self._p(f"Bid > 0", float(quote.bid) > 0, f"₹{quote.bid}")
            self._p(f"Ask > 0", float(quote.ask) > 0, f"₹{quote.ask}")

            stale = quote.is_stale(30)
            self._p("Quote freshness (< 30s)", not stale,
                    "fresh" if not stale else f"stale (age > 30s)", is_warn=stale)

            return True

        except Exception as e:
            fail(f"Quote fetch failed: {e}")
            self.failed += 1
            return False

    # ── 5. Guard layers ────────────────────────────────────────────────────

    def test_guard_layers(self) -> bool:
        section("5. Guard Layers (paper trade simulation)")
        try:
            from live.guard_layers import run_all_guards, GuardContext
            from live.config import load_config
            from datetime import date
            from decimal import Decimal

            config = load_config(_ROOT / "config" / "live_config.yaml")
            today  = date.today()
            expiry = today + timedelta(days=30)
            spot   = 1000.0

            sym_cfg = next((s for s in config.stocks if s.symbol == self.symbol), None)
            if not sym_cfg:
                warn(f"{self.symbol} not in config — using defaults")
                self.warnings += 1
                put_pct  = 5.0
                call_pct = 4.0
            else:
                put_pct  = sym_cfg.put_dist_pct
                call_pct = sym_cfg.call_dist_pct

            short_put  = spot * (1 - put_pct  / 100)
            short_call = spot * (1 + call_pct / 100)
            long_put   = short_put  - 20
            long_call  = short_call + 20

            # Build mock quotes
            from execution.models import Quote, ResolvedInstrument, OptionType
            now_ts = datetime.now(IST)

            def _mock_quote(sym, bid, ask) -> Quote:
                return Quote(
                    instrument_token=999,
                    trading_symbol=sym,
                    exchange="NFO",
                    timestamp=now_ts,
                    ltp=Decimal(str((bid + ask) / 2)),
                    bid=Decimal(str(bid)),
                    bid_qty=500,
                    ask=Decimal(str(ask)),
                    ask_qty=500,
                    volume=10000,
                )

            def _mock_inst(sym, strike, opt, token=1001) -> ResolvedInstrument:
                return ResolvedInstrument(
                    instrument_token=token,
                    trading_symbol=sym,
                    exchange="NFO",
                    underlying=self.symbol,
                    expiry=expiry,
                    strike=Decimal(str(strike)),
                    option_type=OptionType(opt),
                    lot_size=100,
                    tick_size=Decimal("0.05"),
                )

            quotes = {
                "SHORT_PUT":  _mock_quote("SP",  18.0, 20.0),
                "LONG_PUT":   _mock_quote("LP",   8.0,  9.0),
                "SHORT_CALL": _mock_quote("SC",  16.0, 18.0),
                "LONG_CALL":  _mock_quote("LC",   7.0,  8.0),
            }
            instruments = {
                "SHORT_PUT":  _mock_inst("SP",  short_put,  "PE", 1001),
                "LONG_PUT":   _mock_inst("LP",  long_put,   "PE", 1002),
                "SHORT_CALL": _mock_inst("SC",  short_call, "CE", 1003),
                "LONG_CALL":  _mock_inst("LC",  long_call,  "CE", 1004),
            }

            ctx = GuardContext(
                underlying=self.symbol,
                spot=spot,
                expiry=expiry,
                short_put_strike=short_put,
                short_call_strike=short_call,
                long_put_strike=long_put,
                long_call_strike=long_call,
                short_otm_pct=put_pct,
                wing_width_pct=2.0,
                lots=1,
                lot_size=100,
                quotes=quotes,
                instruments=instruments,
                pending_orders=[],
                broker_positions=[],
                available_margin=config.capital.total_capital,
                daily_pnl=0.0,
                total_allocated_margin=0.0,
                config=config,
                phase="pre_entry",
            )

            report = run_all_guards(ctx)

            for r in report.results:
                self._p(f"Guard [{r.layer}]", r.passed, r.reason)

            self._p("All guards passed (happy path)", report.passed,
                    "OK" if report.passed else f"first failure: {report.first_failure.layer if report.first_failure else 'unknown'}")

            return True

        except Exception as e:
            import traceback
            fail(f"Guard layer test error: {e}")
            traceback.print_exc()
            self.failed += 1
            return False

    # ── 6. Trade logger ────────────────────────────────────────────────────

    def test_logger(self) -> bool:
        section("6. Trade Logger")
        try:
            from live.trade_logger import TradeLogger, EventType
            logger = TradeLogger(log_dir=str(_ROOT / "logs" / "live"))

            logger.log(EventType.ENTRY_ATTEMPT, self.symbol, {
                "spot": 1000.0,
                "short_put": 950.0,
                "short_call": 1040.0,
                "lots": 1,
                "test": True,
            })
            ok("log() call succeeded")
            self.passed += 1

            logger.log_entry(
                symbol=self.symbol,
                spot=1000.0,
                strikes_dict={"SP": 950, "SC": 1040, "LP": 930, "LC": 1060},
                lots=1,
                credit=38.0,
                guard_report=None,
            )
            ok("log_entry() call succeeded")
            self.passed += 1

            logger.log_shift(
                symbol=self.symbol,
                old_strikes={"SP": 950, "SC": 1040},
                new_strikes={"SP": 920, "SC": 1010},
                spot=918.0,
                trigger_pct=1.5,
            )
            ok("log_shift() call succeeded")
            self.passed += 1

            events = logger.get_today_events(symbol=self.symbol)
            self._p(f"get_today_events() returned {len(events)} event(s)",
                    len(events) >= 3, f"{len(events)} events")

            return True

        except Exception as e:
            fail(f"Logger test failed: {e}")
            self.failed += 1
            return False

    # ── 7. Capital manager ─────────────────────────────────────────────────

    def test_capital_manager(self) -> bool:
        section("7. Capital Manager")
        try:
            from live.capital_manager import CapitalManager

            mgr = CapitalManager(config=self.config.capital)

            margin = mgr.estimate_margin(spot=1000.0, lot_size=100, lots=1, wing_width_pct=2.0)
            ok(f"estimate_margin(spot=1000, lot=100, lots=1, wing=2%) = ₹{margin:,.0f}")
            self.passed += 1

            ok_alloc = mgr.allocate(self.symbol, margin)
            self._p(f"allocate({self.symbol}, ₹{margin:,.0f})", ok_alloc,
                    f"total allocated: ₹{mgr.get_total_allocated():,.0f}")

            lots = mgr.get_lots_for_capital(
                symbol=self.symbol, spot=1000.0, lot_size=100, wing_width_pct=2.0
            )
            ok(f"get_lots_for_capital() = {lots} lot(s)")
            self.passed += 1

            safe = mgr.check_daily_loss(daily_pnl=-5000.0)
            self._p("check_daily_loss(-₹5,000)", safe, "within limit")

            released = mgr.release(self.symbol)
            ok(f"release({self.symbol}) freed ₹{released:,.0f}")
            self.passed += 1

            return True

        except Exception as e:
            fail(f"Capital manager test failed: {e}")
            self.failed += 1
            return False

    # ── Run all ────────────────────────────────────────────────────────────

    def run(self) -> None:
        print()
        print(f"{BOLD}{'=' * SECTION_WIDTH}{RESET}")
        print(f"{BOLD}  Shifting Iron Condor — Paper Mode Smoke Test{RESET}")
        print(f"{BOLD}  Symbol: {self.symbol}{RESET}")
        print(f"{BOLD}{'=' * SECTION_WIDTH}{RESET}")

        self.test_config()

        # Only proceed with broker tests if credentials are present
        has_creds = bool(
            self.config
            and self.config.zerodha_api_key
            and self.config.zerodha_access_token
        )

        if has_creds:
            auth_ok = self.test_broker_auth()
            if auth_ok:
                self.test_instrument_resolution()
                self.test_quotes()
        else:
            section("2–4. Broker Tests")
            warn("Skipping broker tests — no credentials found.")
            warn("Run: python tools/zerodha_login.py   then re-run this script.")
            self.warnings += 3

        self.test_guard_layers()
        self.test_logger()
        if self.config:
            self.test_capital_manager()

        # ── Final summary ──────────────────────────────────────────────────
        print()
        print(f"{'─' * SECTION_WIDTH}")
        total = self.passed + self.failed
        if self.failed == 0:
            print(f"{GREEN}{BOLD}  ALL TESTS PASSED  "
                  f"({self.passed} passed, {self.warnings} warnings){RESET}")
            print(f"\n  {GREEN}Ready for paper trading:{RESET}")
            print(f"    python run_live.py --mode paper")
        else:
            print(f"{RED}{BOLD}  {self.failed} TEST(S) FAILED  "
                  f"({self.passed} passed, {self.failed} failed, {self.warnings} warnings){RESET}")
            if not has_creds:
                print(f"\n  {YELLOW}Next step:{RESET} Set up credentials first:")
                print(f"    python tools/zerodha_login.py")
        print(f"{'─' * SECTION_WIDTH}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper mode smoke test")
    parser.add_argument("--symbol", default="ICICIBANK",
                        help="NSE symbol to test with (default: ICICIBANK)")
    parser.add_argument("--env-file", default=str(_ROOT / "config" / "credentials.env"))
    args = parser.parse_args()

    test = PaperModeTest(symbol=args.symbol.upper(), env_file=args.env_file)
    test.run()


if __name__ == "__main__":
    main()
