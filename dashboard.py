"""
Options Execution Dashboard
Run: streamlit run dashboard.py
from /media/upayan/Work/options_execution/
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import streamlit as st

# Project root on path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from dashboard_helpers import (
    load_strategy_cached,
    load_risk_cached,
    get_trading_mode,
    has_zerodha_credentials,
    get_db_path,
    decimal_to_float,
    fmt_inr,
    fmt_pts,
    now_ist,
)

IST = ZoneInfo("Asia/Kolkata")

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Iron Condor Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state defaults ─────────────────────────────────────────────────────
if "last_quotes" not in st.session_state:
    st.session_state.last_quotes = {}
if "last_entry_result" not in st.session_state:
    st.session_state.last_entry_result = None
if "last_run_id" not in st.session_state:
    st.session_state.last_run_id = None
if "alert_log" not in st.session_state:
    st.session_state.alert_log = []
if "resolved_symbols" not in st.session_state:
    st.session_state.resolved_symbols = {}
if "lot_size" not in st.session_state:
    st.session_state.lot_size = 100

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("\u2699\ufe0f Controls")

    mode = get_trading_mode()
    mode_color = "\U0001f7e2" if mode == "PAPER" else "\U0001f534"
    st.metric("Trading Mode", f"{mode_color} {mode}")

    try:
        from risk.kill_switch import KillSwitch
        ks = KillSwitch()
        ks_active = ks.is_active()
    except Exception as _ks_err:
        ks_active = False
        ks = None

    ks_label = "\U0001f534 KILL SWITCH ACTIVE" if ks_active else "\U0001f7e2 Kill Switch Inactive"
    st.write(ks_label)

    if ks is not None:
        if ks_active:
            if st.button("\u2705 Deactivate Kill Switch", type="primary"):
                try:
                    ks.deactivate()
                    st.success("Kill switch deactivated.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not deactivate: {e}")
        else:
            if st.button("\U0001f6d1 Activate Kill Switch", type="secondary"):
                try:
                    ks.activate("Manual activation from dashboard")
                    st.warning("Kill switch ACTIVATED. All operations halted.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not activate: {e}")

    st.divider()

    creds = has_zerodha_credentials()
    st.write("\U0001f50c Zerodha: " + ("Connected" if creds else "Not configured"))
    db_path_sidebar = get_db_path()
    st.write(f"\U0001f5c4\ufe0f DB: {'exists' if Path(db_path_sidebar).exists() else 'not found'}")

    try:
        from execution.process_lock import ProcessLock
        lock_file = Path("logs/strategy.lock")
        st.write(f"\U0001f512 Lock: {'HELD' if lock_file.exists() else 'free'}")
    except Exception:
        st.write("\U0001f512 Lock: unknown")

    st.divider()

    # ── Quick capital limit setter ─────────────────────────────────────────
    st.write("**Capital Limit**")
    try:
        import yaml as _yaml
        with open(os.environ.get("RISK_CONFIG", "config/risk.yaml")) as _f:
            _cur_capital = float(_yaml.safe_load(_f)["risk"]["capital_limit"])
    except Exception:
        _cur_capital = 200000.0
    _new_capital = st.number_input(
        "₹ Available Capital",
        min_value=10000,
        max_value=10000000,
        value=int(_cur_capital),
        step=10000,
        key="sidebar_capital",
        help="Sets the capital limit used for margin checks and risk validation.",
    )
    if _new_capital != int(_cur_capital):
        if st.button("💾 Save Capital Limit", key="save_capital_sidebar"):
            try:
                import yaml
                _rc_path = os.environ.get("RISK_CONFIG", "config/risk.yaml")
                with open(_rc_path) as _f:
                    _raw_rc = yaml.safe_load(_f)
                _raw_rc["risk"]["capital_limit"] = int(_new_capital)
                with open(_rc_path, "w") as _f:
                    yaml.dump(_raw_rc, _f, default_flow_style=False, sort_keys=False)
                st.success(f"Saved ₹{_new_capital:,}")
                st.rerun()
            except Exception as _ce:
                st.error(f"Save failed: {_ce}")

    st.divider()
    st.caption(f"Last refresh: {now_ist().strftime('%H:%M:%S IST')}")
    if st.button("\U0001f504 Refresh All"):
        st.rerun()

# ── Main title ─────────────────────────────────────────────────────────────────
st.title("\U0001f4ca Iron Condor Execution Dashboard")

if not creds:
    st.info("\U0001f4dd PAPER MODE — No broker connection. Zerodha credentials not set. Using simulated quotes.")

strategy, strategy_err = load_strategy_cached()
risk, exit_rules, paper_cfg, risk_err = load_risk_cached()

if strategy_err:
    st.error(f"Strategy config error: {strategy_err}")
if risk_err:
    st.error(f"Risk config error: {risk_err}")

# ── Tabs ───────────────────────────────────────────────────────────────────────
tab_status, tab_quotes, tab_picker, tab_tester, tab_config, tab_entry, tab_monitor, tab_exit, tab_history, tab_alerts, tab_auto, tab_intel, tab_reports = st.tabs([
    "📋 Status", "📈 Quotes", "🔍 Stock Picker", "🧪 Strategy Tester", "⚙️ Config Editor",
    "🚀 Entry", "📡 Monitor", "🏁 Exit", "📜 History", "🔔 Alerts", "🤖 Auto Trader",
    "🧠 Intelligence", "📊 Reports"
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB: STOCK PICKER
# ══════════════════════════════════════════════════════════════════════════════
with tab_picker:
    st.subheader("🔍 Stock Picker — Expected Move Iron Condor Builder")

    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from stock_picker import (
        build_ic_candidate, fill_candidate_quotes, compute_expected_move,
        get_nfo_underlyings_from_cache, get_expiries_for_underlying,
        get_strikes_for_underlying_expiry, get_lot_size_for_underlying,
    )

    CACHE_PATH = "logs/nfo_instruments_cache.csv"
    cache_exists = Path(CACHE_PATH).exists()

    if not cache_exists:
        st.warning("No instrument cache found. Run `resolve-instruments --live` first to download it.")
        st.code("python3 -m cli.main resolve-instruments --live --force-refresh")
    else:
        # Load underlyings
        underlyings = get_nfo_underlyings_from_cache(CACHE_PATH)
        if not underlyings:
            st.warning("Instrument cache exists but no underlyings found. Try refreshing.")
        else:
            col_p1, col_p2 = st.columns([2, 3])
            with col_p1:
                selected_underlying = st.selectbox("Select Underlying", underlyings,
                    index=underlyings.index("OBEROIRLTY") if "OBEROIRLTY" in underlyings else 0)
                expiries = get_expiries_for_underlying(CACHE_PATH, selected_underlying)
                if expiries:
                    selected_expiry = st.selectbox("Expiry", expiries,
                        format_func=lambda d: d.strftime("%d %b %Y"))
                else:
                    st.warning("No upcoming expiries found for this underlying.")
                    selected_expiry = None

            with col_p2:
                iv_pct = st.slider("IV Assumption (%)", min_value=10, max_value=80, value=35, step=1)
                iv = Decimal(str(iv_pct / 100))
                short_mult = st.slider("Short Wing Multiplier", min_value=0.5, max_value=2.0, value=1.0, step=0.05)
                long_mult = st.slider("Long Wing Multiplier", min_value=0.6, max_value=2.5, value=1.3, step=0.05)
                lots = st.number_input("Lots", min_value=1, max_value=10, value=1)

            if selected_expiry and st.button("📐 Build IC Structure", type="primary"):
                with st.spinner("Building iron condor structure..."):
                    try:
                        pe_strikes = get_strikes_for_underlying_expiry(CACHE_PATH, selected_underlying, selected_expiry, "PE")
                        ce_strikes = get_strikes_for_underlying_expiry(CACHE_PATH, selected_underlying, selected_expiry, "CE")
                        all_strikes = sorted(set(pe_strikes + ce_strikes))
                        lot_size_val = get_lot_size_for_underlying(CACHE_PATH, selected_underlying)
                        if lot_size_val == 0:
                            lot_size_val = 100

                        # Get spot — use Zerodha if available, otherwise ask user
                        spot_val = None
                        if creds:
                            try:
                                from brokers.zerodha import ZerodhaBroker
                                zb = ZerodhaBroker.from_env()
                                # Try fetching NSE equity quote for spot
                                raw = zb._kite.quote([f"NSE:{selected_underlying}"])
                                spot_key = f"NSE:{selected_underlying}"
                                if spot_key in raw:
                                    spot_val = Decimal(str(raw[spot_key]["last_price"]))
                            except Exception:
                                pass

                        if spot_val is None:
                            spot_val = Decimal(str(st.session_state.get(f"spot_{selected_underlying}", 0)))

                        if not spot_val or spot_val == 0:
                            st.info("Could not fetch live spot price. Enter it manually:")
                            spot_input = st.number_input("Spot Price", min_value=1.0, value=1800.0, step=0.5)
                            spot_val = Decimal(str(spot_input))

                        candidate = build_ic_candidate(
                            underlying=selected_underlying,
                            expiry=selected_expiry,
                            spot=spot_val,
                            iv=iv,
                            short_mult=float(short_mult),
                            long_mult=float(long_mult),
                            available_strikes=all_strikes,
                            lot_size=lot_size_val,
                            lots=int(lots),
                        )
                        st.session_state[f"candidate_{selected_underlying}"] = candidate

                    except Exception as e:
                        st.error(f"Error building structure: {e}")

            # Show candidate if available
            key = f"candidate_{selected_underlying}"
            if key in st.session_state:
                cand = st.session_state[key]

                st.divider()
                st.subheader(f"Iron Condor Structure — {cand.underlying}")

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Spot", f"{float(cand.spot):.2f}" if cand.spot else "N/A")
                c2.metric("Expected Move", f"±{float(cand.expected_move):.2f}" if cand.expected_move else "N/A")
                c3.metric("DTE", cand.dte)
                c4.metric("Lot Size", cand.lot_size)

                import pandas as pd
                legs_df = pd.DataFrame([
                    {"Leg": "BUY PUT",  "Strike": float(cand.buy_put_strike),  "Type": "PE", "Side": "BUY",  "Est. Ask": float(cand.buy_put_ask) if cand.buy_put_ask else "—"},
                    {"Leg": "SELL PUT", "Strike": float(cand.sell_put_strike), "Type": "PE", "Side": "SELL", "Est. Bid": float(cand.sell_put_bid) if cand.sell_put_bid else "—"},
                    {"Leg": "SELL CALL","Strike": float(cand.sell_call_strike),"Type": "CE", "Side": "SELL", "Est. Bid": float(cand.sell_call_bid) if cand.sell_call_bid else "—"},
                    {"Leg": "BUY CALL", "Strike": float(cand.buy_call_strike), "Type": "CE", "Side": "BUY",  "Est. Ask": float(cand.buy_call_ask) if cand.buy_call_ask else "—"},
                ])
                st.dataframe(legs_df, use_container_width=True, hide_index=True)

                if cand.net_credit:
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Net Credit", f"{float(cand.net_credit):.2f} pts")
                    m2.metric("Max Profit", fmt_inr(cand.max_profit))
                    m3.metric("Max Loss", fmt_inr(cand.max_loss))
                    m4.metric("Return/Risk", f"{float(cand.return_on_risk):.1f}%" if cand.return_on_risk else "N/A")

                if cand.errors:
                    for err in cand.errors:
                        st.warning(f"⚠️ {err}")

                if not cand.errors:
                    st.divider()
                    if st.button("✅ Use This Strategy", type="primary"):
                        # Write to strategy.yaml
                        import yaml
                        new_strategy = {
                            "strategy": {
                                "name": "expected_move_iron_condor",
                                "underlying": cand.underlying,
                                "exchange": "NFO",
                                "expiry": str(cand.expiry),
                                "lots": int(lots),
                                "legs": [
                                    {"side": "BUY",  "option_type": "PE", "strike": float(cand.buy_put_strike)},
                                    {"side": "SELL", "option_type": "PE", "strike": float(cand.sell_put_strike)},
                                    {"side": "SELL", "option_type": "CE", "strike": float(cand.sell_call_strike)},
                                    {"side": "BUY",  "option_type": "CE", "strike": float(cand.buy_call_strike)},
                                ],
                            }
                        }
                        with open("config/strategy.yaml", "w") as f:
                            yaml.dump(new_strategy, f, default_flow_style=False, sort_keys=False)
                        st.success(f"✅ strategy.yaml updated with {cand.underlying} {cand.expiry} IC structure!")
                        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB: STRATEGY TESTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_tester:
    st.subheader("🧪 Strategy Tester — Historical Backtest")

    BHAVCOPY_DIR = "/media/upayan/Work/Quant Projects/projects/project1/nse_bhavcopy_cache"
    bhavcopy_exists = Path(BHAVCOPY_DIR).exists()

    if not bhavcopy_exists:
        st.warning(f"Bhavcopy cache not found at:\n`{BHAVCOPY_DIR}`")
    else:
        # Count files
        n_files = len(list(Path(BHAVCOPY_DIR).glob("fo_*.csv")))
        st.caption(f"Bhavcopy cache: {n_files} files in `{BHAVCOPY_DIR}`")

        col_t1, col_t2 = st.columns(2)
        with col_t1:
            # Suggest the currently configured underlying as default
            default_sym = strategy.underlying if strategy else "OBEROIRLTY"
            test_underlying = st.text_input("Underlying", value=default_sym)
            test_iv = st.slider("IV (%)", 10, 80, 35, key="test_iv")
            test_short_mult = st.slider("Short Mult", 0.5, 2.0, 1.0, 0.05, key="test_sm")
            test_long_mult = st.slider("Long Mult", 0.6, 2.5, 1.3, 0.05, key="test_lm")
        with col_t2:
            test_min_dte = st.slider("Min DTE", 5, 30, 15, key="test_min_dte")
            test_max_dte = st.slider("Max DTE", 20, 60, 45, key="test_max_dte")
            test_sl_mult = st.slider("Stop Loss (× credit)", 1.0, 5.0, 2.0, 0.5, key="test_sl")
            test_pt_pct = st.slider("Profit Target (%)", 50, 100, 80, key="test_pt")
            test_lot_size = st.number_input("Lot Size", min_value=1, value=100, key="test_ls")

        if st.button("▶️ Run Backtest", type="primary"):
            with st.spinner(f"Running backtest for {test_underlying}..."):
                try:
                    from strategy_tester import run_backtest
                    bt_result = run_backtest(
                        underlying=test_underlying.upper(),
                        iv=test_iv / 100,
                        short_mult=test_short_mult,
                        long_mult=test_long_mult,
                        min_dte=test_min_dte,
                        max_dte=test_max_dte,
                        stop_loss_mult=test_sl_mult,
                        profit_target_pct=test_pt_pct,
                        lot_size=test_lot_size,
                        cache_dir=BHAVCOPY_DIR,
                    )
                    st.session_state["bt_result"] = bt_result
                except Exception as e:
                    st.error(f"Backtest error: {e}")

        if "bt_result" in st.session_state:
            bt = st.session_state["bt_result"]

            if bt.errors:
                for err in bt.errors:
                    st.error(err)
            elif bt.total_trades == 0:
                st.warning("No trades found. Try different parameters or check bhavcopy data.")
            else:
                st.subheader(f"Results — {bt.underlying}  ({bt.total_trades} trades)")

                m1, m2, m3, m4, m5, m6 = st.columns(6)
                m1.metric("Win Rate", f"{bt.win_rate:.1f}%")
                m2.metric("Total P&L", fmt_inr(bt.total_pnl))
                m3.metric("Avg P&L", fmt_inr(bt.avg_pnl))
                m4.metric("Profit Factor", f"{bt.profit_factor:.2f}" if bt.profit_factor != float("inf") else "∞")
                m5.metric("Max Drawdown", fmt_inr(bt.max_drawdown))
                m6.metric("Avg Return/Risk", f"{bt.avg_return_on_risk:.1f}%")

                m7, m8, m9 = st.columns(3)
                m7.metric("Best Trade", fmt_inr(bt.best_trade))
                m8.metric("Worst Trade", fmt_inr(bt.worst_trade))
                m9.metric("Avg Credit", fmt_pts(bt.avg_credit))

                # Exit trigger breakdown
                import pandas as pd
                exit_counts = {"EXPIRY": 0, "STOP_LOSS": 0, "PROFIT_TARGET": 0}
                for t in bt.trades:
                    exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1
                ec_df = pd.DataFrame([{"Exit Trigger": k, "Count": v, "% of Trades": f"{v/bt.total_trades*100:.1f}%"}
                                       for k, v in exit_counts.items()])
                col_ec, col_eq = st.columns(2)
                col_ec.write("**Exit Triggers**")
                col_ec.dataframe(ec_df, use_container_width=True, hide_index=True)

                # Equity curve
                cumulative = []
                running = 0.0
                for t in bt.trades:
                    running += t.exit_pnl
                    cumulative.append({"Trade": bt.trades.index(t) + 1, "Cumulative P&L": running})
                if len(cumulative) > 1:
                    eq_df = pd.DataFrame(cumulative).set_index("Trade")
                    col_eq.write("**Equity Curve**")
                    col_eq.line_chart(eq_df)

                # Trade table
                st.subheader("Trade Log")
                trades_df = pd.DataFrame([{
                    "Entry": t.entry_date,
                    "Expiry": t.expiry,
                    "DTE": t.dte_at_entry,
                    "Spot": round(t.spot_at_entry, 2),
                    "Sell Put": t.sell_put_strike,
                    "Sell Call": t.sell_call_strike,
                    "Credit": round(t.net_credit, 2),
                    "Max Profit": round(t.max_profit, 0),
                    "Max Loss": round(t.max_loss, 0),
                    "Exit": t.exit_reason,
                    "P&L": round(t.exit_pnl, 0),
                    "R/R%": round(t.return_on_risk, 1),
                } for t in bt.trades])
                st.dataframe(trades_df, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB: CONFIG EDITOR
# ══════════════════════════════════════════════════════════════════════════════
with tab_config:
    st.subheader("⚙️ Config Editor")
    st.info("Edit strategy and risk parameters here. Changes are saved to YAML and take effect immediately.")

    import yaml

    cfg_tab1, cfg_tab2, cfg_tab3, cfg_tab4 = st.tabs(["📄 Strategy Config", "🎛️ Risk Config", "🏗️ Live Config", "🔑 Credentials"])

    # ── Strategy config editor ─────────────────────────────────────────────
    with cfg_tab1:
        st.write("**`config/strategy.yaml`**")
        try:
            with open("config/strategy.yaml") as f:
                raw_strategy = yaml.safe_load(f)
            s = raw_strategy.get("strategy", {})

            with st.form("strategy_form"):
                f_name = st.text_input("Strategy Name", value=s.get("name", "expected_move_iron_condor"))
                f_underlying = st.text_input("Underlying", value=s.get("underlying", "OBEROIRLTY"))
                f_exchange = st.text_input("Exchange", value=s.get("exchange", "NFO"))
                f_expiry = st.text_input("Expiry (YYYY-MM-DD)", value=str(s.get("expiry", "")))
                f_lots = st.number_input("Lots", min_value=1, max_value=50, value=int(s.get("lots", 1)))

                st.write("**Legs** (must be exactly 4)")
                legs = s.get("legs", [{}, {}, {}, {}])
                while len(legs) < 4:
                    legs.append({})

                new_legs = []
                for i in range(4):
                    leg = legs[i]
                    c1, c2, c3 = st.columns(3)
                    side = c1.selectbox(f"Leg {i} Side", ["BUY", "SELL"],
                                        index=0 if leg.get("side", "BUY") == "BUY" else 1,
                                        key=f"side_{i}")
                    opt = c2.selectbox(f"Leg {i} Type", ["PE", "CE"],
                                       index=0 if leg.get("option_type", "PE") == "PE" else 1,
                                       key=f"opt_{i}")
                    strike = c3.number_input(f"Leg {i} Strike",
                                             min_value=1.0, value=float(leg.get("strike", 100.0)),
                                             step=5.0, key=f"strike_{i}")
                    new_legs.append({"side": side, "option_type": opt, "strike": strike})

                save_strategy = st.form_submit_button("💾 Save Strategy Config", type="primary")

            if save_strategy:
                try:
                    # Validate expiry format
                    date.fromisoformat(f_expiry)
                    new_config = {
                        "strategy": {
                            "name": f_name,
                            "underlying": f_underlying.upper(),
                            "exchange": f_exchange.upper(),
                            "expiry": f_expiry,
                            "lots": int(f_lots),
                            "legs": new_legs,
                        }
                    }
                    with open("config/strategy.yaml", "w") as f:
                        yaml.dump(new_config, f, default_flow_style=False, sort_keys=False)
                    st.success("✅ strategy.yaml saved!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e}")

        except Exception as e:
            st.error(f"Could not load strategy config: {e}")

    # ── Risk config editor ─────────────────────────────────────────────────
    with cfg_tab2:
        st.write("**`config/risk.yaml`**")
        try:
            with open("config/risk.yaml") as f:
                raw_risk = yaml.safe_load(f)
            r = raw_risk.get("risk", {})
            er = raw_risk.get("exit_rules", {})
            pc = raw_risk.get("paper", {})

            with st.form("risk_form"):
                st.write("**Capital & Loss Limits**")
                c1, c2, c3 = st.columns(3)
                f_capital = c1.number_input("Capital Limit (₹)", min_value=10000, value=int(r.get("capital_limit", 200000)), step=10000)
                f_max_loss = c2.number_input("Max Strategy Loss (₹)", min_value=500, value=int(r.get("max_strategy_loss", 5000)), step=500)
                f_daily_loss = c3.number_input("Max Daily Loss (₹)", min_value=500, value=int(r.get("max_daily_loss", 8000)), step=500)

                st.write("**Credit Requirements**")
                c4, c5 = st.columns(2)
                f_min_credit = c4.number_input("Min Net Credit (pts)", min_value=0.5, value=float(r.get("min_net_credit", 15.0)), step=0.5)
                f_target_credit = c5.number_input("Target Net Credit (pts)", min_value=0.5, value=float(r.get("target_net_credit", 20.0)), step=0.5)

                st.write("**Quote Validation**")
                c6, c7 = st.columns(2)
                f_spread_pct = c6.number_input("Max Bid-Ask Spread (%)", min_value=1.0, value=float(r.get("max_bid_ask_spread_pct", 8.0)), step=0.5)
                f_staleness = c7.number_input("Quote Staleness (seconds)", min_value=5, value=int(r.get("quote_staleness_seconds", 30)), step=5)

                st.write("**Entry/Exit Timing (IST)**")
                c8, c9, c10, c11 = st.columns(4)
                f_entry_start = c8.text_input("Entry Start", value=r.get("entry_start_time", "09:20"))
                f_entry_end = c9.text_input("Entry End", value=r.get("entry_end_time", "14:30"))
                f_exit_start = c10.text_input("Exit Start", value=r.get("exit_start_time", "09:20"))
                f_exit_end = c11.text_input("Exit End", value=r.get("exit_end_time", "15:15"))

                st.write("**Order Parameters**")
                c12, c13, c14 = st.columns(3)
                f_limit_step = c12.number_input("Limit Step (pts)", min_value=0.05, value=float(r.get("limit_step", 0.05)), step=0.05, format="%.2f")
                f_wait_sec = c13.number_input("Order Wait (seconds)", min_value=1, value=int(r.get("order_wait_seconds", 15)), step=1)
                f_max_reprice = c14.number_input("Max Reprice Attempts", min_value=1, value=int(r.get("max_repricing_attempts", 5)), step=1)

                st.write("**Exit Rules**")
                c15, c16, c17 = st.columns(3)
                f_profit_pct = c15.number_input("Profit Target (% of max)", min_value=10.0, max_value=100.0,
                                                  value=float(er.get("profit_target_pct") or 80.0), step=5.0)
                f_sl_inr = c16.number_input("Stop Loss (₹)", min_value=0,
                                             value=int(er.get("stop_loss_inr") or 5000), step=500)
                f_dte_exit = c17.number_input("DTE Exit (days)", min_value=0,
                                               value=int(er.get("dte_exit") or 1), step=1)

                st.write("**Paper Trading**")
                c18, c19 = st.columns(2)
                f_fill_mode = c18.selectbox("Paper Fill Mode", ["CONSERVATIVE", "MIDPOINT", "TOUCH"],
                                             index=["CONSERVATIVE","MIDPOINT","TOUCH"].index(
                                                 pc.get("fill_mode", "CONSERVATIVE")))
                f_slippage = c19.number_input("Paper Slippage (pts)", min_value=0.0,
                                               value=float(pc.get("slippage_per_order", 0.05)),
                                               step=0.05, format="%.2f")

                save_risk = st.form_submit_button("💾 Save Risk Config", type="primary")

            if save_risk:
                try:
                    new_risk = {
                        "risk": {
                            "capital_limit": int(f_capital),
                            "max_strategy_loss": int(f_max_loss),
                            "max_daily_loss": int(f_daily_loss),
                            "min_net_credit": float(f_min_credit),
                            "target_net_credit": float(f_target_credit),
                            "max_bid_ask_spread_pct": float(f_spread_pct),
                            "max_total_slippage": float(r.get("max_total_slippage", 5.0)),
                            "quote_staleness_seconds": int(f_staleness),
                            "entry_start_time": f_entry_start,
                            "entry_end_time": f_entry_end,
                            "exit_start_time": f_exit_start,
                            "exit_end_time": f_exit_end,
                            "limit_step": float(f_limit_step),
                            "order_wait_seconds": int(f_wait_sec),
                            "max_repricing_attempts": int(f_max_reprice),
                        },
                        "exit_rules": {
                            "hold_to_expiry": er.get("hold_to_expiry", True),
                            "profit_target_pct": float(f_profit_pct),
                            "profit_target_inr": er.get("profit_target_inr"),
                            "stop_loss_inr": int(f_sl_inr),
                            "stop_loss_credit_multiple": er.get("stop_loss_credit_multiple"),
                            "dte_exit": int(f_dte_exit),
                            "time_based_exit": er.get("time_based_exit", "15:00"),
                            "emergency_exit_enabled": er.get("emergency_exit_enabled", False),
                            "manual_approval_required": er.get("manual_approval_required", True),
                        },
                        "paper": {
                            "fill_mode": f_fill_mode,
                            "fill_delay_seconds": int(pc.get("fill_delay_seconds", 2)),
                            "slippage_per_order": float(f_slippage),
                        },
                    }
                    with open("config/risk.yaml", "w") as f:
                        yaml.dump(new_risk, f, default_flow_style=False, sort_keys=False)
                    st.success("✅ risk.yaml saved!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Save failed: {e}")

        except Exception as e:
            st.error(f"Could not load risk config: {e}")

    # ── Live Config editor ─────────────────────────────────────────────────
    with cfg_tab3:
        st.write("**`config/live_config.yaml`**")
        st.caption("Changes here affect the live auto-trader on next run. Max open positions and global strategy params apply system-wide.")
        try:
            import yaml as _lcy
            _lc_path = os.environ.get("CONFIG_PATH", "config/live_config.yaml")
            with open(_lc_path) as _lf:
                _lc_raw = _lcy.safe_load(_lf)
            _lc_cap = _lc_raw.get("capital", {})
            _lc_strat = _lc_raw.get("strategy", {})

            with st.form("live_config_form"):
                st.write("**Mode & Capital**")
                _lc_c1, _lc_c2, _lc_c3 = st.columns(3)
                _lc_mode = _lc_c1.selectbox("Mode", ["paper", "live"],
                                             index=0 if _lc_raw.get("mode", "paper") == "paper" else 1)
                _lc_capital = _lc_c2.number_input("Total Capital (₹)", min_value=100000, max_value=50000000,
                                                   value=int(_lc_cap.get("total_capital", 2000000)), step=50000)
                _lc_max_pos = _lc_c3.number_input("Max Open Positions", min_value=1, max_value=50,
                                                   value=int(_lc_cap.get("max_open_positions", 10)), step=1)

                st.write("**Strategy Parameters**")
                _lc_s1, _lc_s2, _lc_s3 = st.columns(3)
                _lc_shift_trig = _lc_s1.number_input("Shift Trigger (%)", min_value=0.1, max_value=10.0,
                                                       value=float(_lc_strat.get("shift_trigger_pct", 1.0)), step=0.1, format="%.1f")
                _lc_wing = _lc_s2.number_input("Wing Width (%)", min_value=0.5, max_value=10.0,
                                                value=float(_lc_strat.get("wing_width_pct", 2.0)), step=0.5, format="%.1f")
                _lc_def_lots = _lc_s3.number_input("Default Lots (0=auto)", min_value=0, max_value=50,
                                                    value=int(_lc_strat.get("default_lots", 0)), step=1)

                _lc_s4, _lc_s5, _lc_s6, _lc_s7, _lc_s8 = st.columns(5)
                _lc_min_dte = _lc_s4.number_input("Min DTE", min_value=1, max_value=60,
                                                   value=int(_lc_strat.get("min_dte", 20)), step=1)
                _lc_max_dte = _lc_s5.number_input("Max DTE", min_value=5, max_value=120,
                                                   value=int(_lc_strat.get("max_dte", 45)), step=1)
                _lc_dte_exit = _lc_s8.number_input("Close DTE (≤ N days)", min_value=0, max_value=10,
                                                    value=int(_lc_strat.get("dte_exit", 2)), step=1,
                                                    help="Close all positions when DTE ≤ this (2 = 2 days before expiry)")
                _lc_entry_start = _lc_s6.text_input("Entry Start (IST)", value=_lc_strat.get("entry_start", "09:30"))
                _lc_entry_end   = _lc_s7.text_input("Entry End (IST)", value=_lc_strat.get("entry_end", "14:00"))

                _lc_save = st.form_submit_button("💾 Save Live Config", type="primary")

            if _lc_save:
                try:
                    _lc_raw["mode"] = _lc_mode
                    _lc_raw.setdefault("capital", {})["total_capital"] = int(_lc_capital)
                    _lc_raw["capital"]["max_open_positions"] = int(_lc_max_pos)
                    _lc_raw.setdefault("strategy", {})["shift_trigger_pct"] = float(_lc_shift_trig)
                    _lc_raw["strategy"]["wing_width_pct"] = float(_lc_wing)
                    _lc_raw["strategy"]["default_lots"] = int(_lc_def_lots)
                    _lc_raw["strategy"]["min_dte"] = int(_lc_min_dte)
                    _lc_raw["strategy"]["max_dte"] = int(_lc_max_dte)
                    _lc_raw["strategy"]["entry_start"] = _lc_entry_start
                    _lc_raw["strategy"]["entry_end"] = _lc_entry_end
                    _lc_raw["strategy"]["dte_exit"] = int(_lc_dte_exit)
                    with open(_lc_path, "w") as _lf2:
                        _lcy.dump(_lc_raw, _lf2, default_flow_style=False, sort_keys=False)
                    st.success("✅ live_config.yaml saved! New positions will use these settings.")
                    st.rerun()
                except Exception as _lce:
                    st.error(f"Save failed: {_lce}")

        except Exception as _lc_err:
            st.error(f"Could not load live config: {_lc_err}")

    # ── Credentials editor ─────────────────────────────────────────────────
    with cfg_tab4:
        st.write("**Zerodha API Credentials**")
        st.caption("Access token refreshes daily. Save here to apply immediately — no restart needed.")

        _cred_path = Path("config/credentials.env")

        # Read current values (masked)
        _cur_key   = os.environ.get("ZERODHA_API_KEY", "")
        _cur_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "")
        _cur_secret = os.environ.get("ZERODHA_API_SECRET", "")

        def _mask(v: str) -> str:
            return v[:6] + "****" if len(v) > 6 else ("(not set)" if not v else v[:2] + "**")

        st.info(
            f"Current API Key: `{_mask(_cur_key)}`  \n"
            f"Current Token: `{_mask(_cur_token)}`"
        )

        with st.form("credentials_form"):
            _new_token = st.text_input(
                "Access Token (refreshed daily)",
                value="",
                type="password",
                help="Paste today's Zerodha access token here. Leave blank to keep the current one.",
            )
            st.caption("To update API Key / Secret (rarely needed):")
            _new_key = st.text_input(
                "API Key", value="", type="password",
                help="Leave blank to keep current."
            )
            _new_secret = st.text_input(
                "API Secret", value="", type="password",
                help="Leave blank to keep current."
            )
            _cred_save = st.form_submit_button("💾 Save & Apply Credentials", type="primary")

        if _cred_save:
            try:
                from dotenv import set_key as _set_key
                _saved = []
                if _new_token.strip():
                    _set_key(str(_cred_path), "ZERODHA_ACCESS_TOKEN", _new_token.strip())
                    os.environ["ZERODHA_ACCESS_TOKEN"] = _new_token.strip()
                    _saved.append("ACCESS_TOKEN")
                if _new_key.strip():
                    _set_key(str(_cred_path), "ZERODHA_API_KEY", _new_key.strip())
                    os.environ["ZERODHA_API_KEY"] = _new_key.strip()
                    _saved.append("API_KEY")
                if _new_secret.strip():
                    _set_key(str(_cred_path), "ZERODHA_API_SECRET", _new_secret.strip())
                    os.environ["ZERODHA_API_SECRET"] = _new_secret.strip()
                    _saved.append("API_SECRET")
                if _saved:
                    st.success(f"✅ Saved and applied: {', '.join(_saved)}. In effect immediately.")
                    st.rerun()
                else:
                    st.warning("Nothing to save — all fields were blank.")
            except Exception as _ce2:
                st.error(f"Save failed: {_ce2}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: STATUS
# ══════════════════════════════════════════════════════════════════════════════
with tab_status:
    st.subheader("System Status")

    # ── Accepted trade banner ──────────────────────────────────────────────
    accepted = st.session_state.get("accepted_trade")
    if accepted:
        st.success(
            f"✅ **ACCEPTED TRADE** — {accepted.get('underlying')} "
            f"expiry {accepted.get('expiry')} | "
            f"Net credit: **{accepted.get('credit', 0):.2f} pts/share** | "
            f"Locked at: {accepted.get('accepted_at', '—')}"
        )
        if st.button("🗑️ Clear Accepted Trade", key="clear_accepted"):
            del st.session_state["accepted_trade"]
            st.rerun()
        st.divider()

    # ── Workflow guide ─────────────────────────────────────────────────────
    _steps = ["🔍 Pick Stock", "🧪 Backtest", "📈 Fetch Quotes", "🚀 Paper Enter", "✅ Accept"]
    _current = 0
    if strategy:
        _current = 1
    if st.session_state.get("bt_result"):
        _current = 2
    if st.session_state.get("last_quotes"):
        _current = 3
    if (st.session_state.get("last_entry_result") or {}).get("all_filled"):
        _current = 4
    if accepted:
        _current = 5
    _step_cols = st.columns(len(_steps))
    for _si, (_sc, _sl) in enumerate(zip(_step_cols, _steps)):
        if _si < _current:
            _sc.markdown(f"~~{_sl}~~ ✓")
        elif _si == _current:
            _sc.markdown(f"**→ {_sl}**")
        else:
            _sc.markdown(f"<span style='color:grey'>{_sl}</span>", unsafe_allow_html=True)
    st.divider()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Mode", mode)
    col2.metric("Kill Switch", "ACTIVE" if ks_active else "Inactive")
    col3.metric("Zerodha", "Connected" if creds else "Not set")
    col4.metric("Database", "Found" if Path(get_db_path()).exists() else "Missing")

    if strategy:
        st.subheader("Strategy Configuration")
        col_a, col_b = st.columns(2)
        with col_a:
            st.write(f"**Name:** {strategy.name}")
            st.write(f"**Underlying:** {strategy.underlying}")
            st.write(f"**Exchange:** {strategy.exchange}")
            st.write(f"**Expiry:** {strategy.expiry}")
            st.write(f"**Lots:** {strategy.lots}")

            try:
                dte = max(0, (strategy.expiry - date.today()).days)
                st.metric("Days to Expiry", dte)
            except Exception as e:
                st.write(f"DTE error: {e}")

        with col_b:
            try:
                import pandas as pd
                legs_data = []
                for i, leg in enumerate(strategy.legs):
                    symbol_hint = (
                        f"{strategy.underlying}"
                        f"{strategy.expiry.strftime('%y%b').upper()}"
                        f"{int(leg.strike)}"
                        f"{leg.option_type.value}"
                    )
                    legs_data.append(
                        {
                            "#": i,
                            "Side": leg.side.value,
                            "Strike": float(leg.strike),
                            "Type": leg.option_type.value,
                            "Est. Symbol": symbol_hint,
                        }
                    )
                st.dataframe(
                    pd.DataFrame(legs_data), use_container_width=True, hide_index=True
                )
            except Exception as e:
                st.error(f"Legs table error: {e}")

    if risk:
        st.subheader("Risk Configuration")
        col_r1, col_r2, col_r3 = st.columns(3)
        col_r1.metric("Capital Limit", fmt_inr(risk.capital_limit))
        col_r2.metric("Max Strategy Loss", fmt_inr(risk.max_strategy_loss))
        col_r3.metric("Min Net Credit", fmt_pts(risk.min_net_credit))

        col_r4, col_r5, col_r6 = st.columns(3)
        col_r4.metric("Max Spread %", f"{float(risk.max_bid_ask_spread_pct):.1f}%")
        col_r5.metric(
            "Entry Window", f"{risk.entry_start_time}\u2013{risk.entry_end_time}"
        )
        col_r6.metric("Max Reprice Attempts", risk.max_repricing_attempts)

        if exit_rules:
            st.subheader("Exit Rules")
            col_e1, col_e2, col_e3 = st.columns(3)
            col_e1.metric(
                "Hold to Expiry", "Yes" if exit_rules.hold_to_expiry else "No"
            )
            col_e2.metric(
                "Profit Target %",
                f"{float(exit_rules.profit_target_pct):.0f}%"
                if exit_rules.profit_target_pct
                else "N/A",
            )
            col_e3.metric(
                "Stop Loss",
                fmt_inr(exit_rules.stop_loss_inr)
                if exit_rules.stop_loss_inr
                else "N/A",
            )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: QUOTES
# ══════════════════════════════════════════════════════════════════════════════
with tab_quotes:
    @st.fragment(run_every=30)
    def _quotes_fragment():
        st.subheader("Live Quotes")

        # ── Controls row ───────────────────────────────────────────────────────
        col_fetch, col_manual, col_status = st.columns([2, 2, 3])
        fetch_clicked  = col_fetch.button("\U0001f4e1 Fetch Quotes", type="primary", key="q_fetch")
        manual_refresh = col_manual.button("\U0001f504 Refresh Now", key="q_manual")
        with col_status:
            _last_ts = st.session_state.get("auto_refresh_ts", 0)
            if _last_ts:
                _last_str = datetime.fromtimestamp(_last_ts).strftime("%H:%M:%S")
                _remaining = max(0, int(300 - (time.time() - _last_ts)))
                _rm, _rs = divmod(_remaining, 60)
                st.caption(
                    f"Last fetch: **{_last_str}** IST · "
                    f"Auto-refresh in {_rm}m {_rs:02d}s"
                )
            else:
                st.caption("Auto-refresh every **5 min** · not fetched yet")

        # ── Fetch trigger: manual button, explicit refresh, or 5-min auto-tick ─
        _elapsed_since_fetch = time.time() - st.session_state.get("auto_refresh_ts", 0)
        _auto_tick = "auto_refresh_ts" in st.session_state and _elapsed_since_fetch >= 298

        if fetch_clicked or manual_refresh or _auto_tick:
            if not strategy or not risk:
                st.error("Strategy or risk config not loaded.")
            else:
                with st.spinner("Fetching quotes..."):
                    try:
                        # ── 1. Resolve instruments & fetch quotes ──────────────
                        if creds:
                            from brokers.zerodha import ZerodhaBroker
                            broker = ZerodhaBroker.from_env()
                            from execution.quote_engine import QuoteEngine
                            qe = QuoteEngine(broker)
                            instruments = []
                            resolved_map = {}
                            lot_size_val = 100
                            for i, leg in enumerate(strategy.legs):
                                inst = broker.resolve_instrument(
                                    underlying=strategy.underlying,
                                    exchange=strategy.exchange,
                                    expiry=strategy.expiry,
                                    strike=leg.strike,
                                    option_type=leg.option_type.value,
                                )
                                instruments.append(
                                    (inst.instrument_token, inst.trading_symbol)
                                )
                                resolved_map[i] = inst.trading_symbol
                                lot_size_val = inst.lot_size
                            quotes = qe.fetch_all(instruments)
                        else:
                            from unittest.mock import MagicMock
                            from brokers.paper import PaperBroker
                            from execution.models import Quote
                            from execution.quote_engine import QuoteEngine
                            sim_prices = {
                                0: ("7.50", "8.00"),
                                1: ("12.00", "12.50"),
                                2: ("13.00", "13.50"),
                                3: ("10.00", "10.50"),
                            }
                            resolved_map = {}
                            instruments = []
                            for i, leg in enumerate(strategy.legs):
                                sym = (
                                    f"{strategy.underlying}"
                                    f"{strategy.expiry.strftime('%y%b').upper()}"
                                    f"{int(leg.strike)}{leg.option_type.value}"
                                )
                                resolved_map[i] = sym
                                instruments.append((1000 + i, sym))
                            def make_sim_quotes(instrument_list):
                                result = {}
                                for token, sym in instrument_list:
                                    idx = token - 1000
                                    bid, ask = sim_prices.get(idx, ("5.00", "6.00"))
                                    q = Quote(
                                        instrument_token=token,
                                        trading_symbol=sym,
                                        exchange=strategy.exchange,
                                        timestamp=datetime.now(IST),
                                        ltp=Decimal(bid),
                                        bid=Decimal(bid),
                                        bid_qty=200,
                                        ask=Decimal(ask),
                                        ask_qty=200,
                                        volume=10000,
                                        oi=100000,
                                    )
                                    result[token] = q
                                return result
                            mock_source = MagicMock()
                            mock_source.get_quotes.side_effect = make_sim_quotes
                            broker = PaperBroker(
                                live_quote_source=mock_source,
                                fill_delay_seconds=0,
                            )
                            qe = QuoteEngine(broker)
                            quotes = qe.fetch_all(instruments)
                            lot_size_val = 100

                        st.session_state.last_quotes = quotes
                        st.session_state.resolved_symbols = resolved_map
                        st.session_state.lot_size = lot_size_val
                        st.session_state.quote_engine = qe

                        quotes_by_sym = {
                            sym: quotes[sym]
                            for sym in resolved_map.values()
                            if sym in quotes
                        }

                        # ── 2. Basket credit ───────────────────────────────────
                        try:
                            credit = qe.calculate_executable_basket_credit(
                                strategy.legs,
                                quotes_by_sym,
                                lot_size_val,
                                strategy.lots,
                                resolved_map,
                            )
                            st.session_state.quotes_credit = credit
                            st.session_state.quotes_credit_err = None
                        except Exception as _ce:
                            st.session_state.quotes_credit = None
                            st.session_state.quotes_credit_err = str(_ce)

                        # ── 3. Pre-trade validation ────────────────────────────
                        try:
                            from risk.validator import PreTradeValidator
                            from risk.payoff import calculate_payoff_from_credit
                            _credit_v = st.session_state.quotes_credit
                            if _credit_v:
                                validator = PreTradeValidator(risk)
                                payoff = calculate_payoff_from_credit(
                                    strategy, _credit_v.net_credit, lot_size_val
                                )
                                report = validator.validate(strategy, quotes_by_sym, payoff, None)
                                st.session_state.quotes_validation = report
                            else:
                                st.session_state.quotes_validation = None
                        except Exception:
                            st.session_state.quotes_validation = None

                        # ── 4. Margin (live only) ──────────────────────────────
                        if creds:
                            try:
                                _basket_orders = []
                                for _i, _leg in enumerate(strategy.legs):
                                    _sym_mg = resolved_map.get(_i, "")
                                    _q_mg = quotes.get(_sym_mg)
                                    if not _q_mg:
                                        continue
                                    _price_mg = float(
                                        _q_mg.bid if _leg.side.value == "SELL" else _q_mg.ask
                                    )
                                    _basket_orders.append({
                                        "exchange": strategy.exchange,
                                        "tradingsymbol": _sym_mg,
                                        "transaction_type": _leg.side.value,
                                        "variety": "regular",
                                        "product": "NRML",
                                        "order_type": "LIMIT",
                                        "quantity": lot_size_val * strategy.lots,
                                        "price": _price_mg,
                                    })
                                _margin_result = broker.get_basket_margin(_basket_orders)
                                _funds = broker.get_margins()
                                st.session_state.quotes_margin = {
                                    "result": _margin_result,
                                    "funds": _funds,
                                    "error": None,
                                }
                            except Exception as _me:
                                st.session_state.quotes_margin = {"error": str(_me)}
                        else:
                            st.session_state.quotes_margin = None

                        st.session_state.auto_refresh_ts = time.time()
                        st.success(
                            f"Quotes fetched at {now_ist().strftime('%H:%M:%S IST')}"
                        )

                    except Exception as e:
                        st.error(f"Quote fetch error: {e}")

        # ── Display (reads session_state only — zero API calls) ────────────────
        if st.session_state.last_quotes and risk and strategy:
            try:
                import pandas as pd

                quotes = st.session_state.last_quotes
                resolved_symbols = st.session_state.resolved_symbols
                lot_size_val = st.session_state.lot_size

                rows = []
                for i, leg in enumerate(strategy.legs):
                    sym = resolved_symbols.get(i, "")
                    q = quotes.get(sym)
                    if q:
                        spread_pct = float(q.spread_pct)
                        stale = q.is_stale(risk.quote_staleness_seconds)
                        rows.append(
                            {
                                "Side": leg.side.value,
                                "Symbol": sym,
                                "Bid": float(q.bid),
                                "Ask": float(q.ask),
                                "LTP": float(q.ltp),
                                "Spread %": round(spread_pct, 2),
                                "OI": q.oi,
                                "Stale": "\u26a0\ufe0f" if stale else "\u2713",
                                "Wide": (
                                    "\u26a0\ufe0f"
                                    if spread_pct > float(risk.max_bid_ask_spread_pct)
                                    else "\u2713"
                                ),
                            }
                        )
                    else:
                        rows.append(
                            {
                                "Side": leg.side.value,
                                "Symbol": sym,
                                "Bid": None,
                                "Ask": None,
                                "LTP": None,
                                "Spread %": None,
                                "OI": None,
                                "Stale": "?",
                                "Wide": "?",
                            }
                        )

                df = pd.DataFrame(rows)
                st.dataframe(df, use_container_width=True, hide_index=True)

                # ── Basket Credit ──────────────────────────────────────────────
                st.subheader("Basket Credit")
                credit = st.session_state.get("quotes_credit")
                credit_err = st.session_state.get("quotes_credit_err")
                if credit:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Buy Cost", fmt_pts(credit.buy_cost))
                    c2.metric("Sell Credit", fmt_pts(credit.sell_credit))
                    net = float(credit.net_credit)
                    min_credit = float(risk.min_net_credit)
                    c3.metric(
                        "Net Credit",
                        fmt_pts(credit.net_credit),
                        delta=f"{net - min_credit:+.2f} vs min",
                        delta_color="normal" if net >= min_credit else "inverse",
                    )
                    c4.metric("Total Credit (INR)", fmt_inr(credit.net_credit_total))
                    if net >= min_credit:
                        st.success(
                            f"\u2705 Net credit {net:.2f} pts is ABOVE minimum {min_credit:.2f} pts"
                        )
                    else:
                        st.warning(
                            f"\u26a0\ufe0f Net credit {net:.2f} pts is BELOW minimum {min_credit:.2f} pts"
                        )
                elif credit_err:
                    st.error(f"Credit calculation error: {credit_err}")

                # ── Pre-Trade Validation ───────────────────────────────────────
                st.subheader("Pre-Trade Validation")
                report = st.session_state.get("quotes_validation")
                if report:
                    for r in report.results:
                        icon = "\u2705" if r.passed else "\u274c"
                        val_str = str(r.value or "")
                        lim_str = f" (limit: {r.limit})" if r.limit else ""
                        msg = f" \u2014 {r.message}" if r.message else ""
                        st.write(f"{icon} **{r.name}**: {val_str}{lim_str}{msg}")

                # ── Margin Required ────────────────────────────────────────────
                st.subheader("Margin Required")
                _capital = float(risk.capital_limit)
                margin_data = st.session_state.get("quotes_margin")
                if creds and margin_data:
                    if margin_data.get("error"):
                        st.error(f"Margin fetch error: {margin_data['error']}")
                    else:
                        _margin_result = margin_data["result"]
                        _funds_data    = margin_data["funds"]
                        _initial  = _margin_result.get("initial", {})
                        _final    = _margin_result.get("final",   {})
                        _charges  = _margin_result.get("charges",  {})
                        _span     = float(_final.get("span",     _initial.get("span",     0)) or 0)
                        _exposure = float(_final.get("exposure", _initial.get("exposure", 0)) or 0)
                        _required = float(_final.get("total",    _initial.get("total",    0)) or 0)
                        if _required == 0:
                            _required = _span + _exposure
                        _charges_total = float(_charges.get("total", 0) or 0)
                        _avail = None
                        try:
                            _eq   = _funds_data.get("equity", {})
                            _live = _eq.get("available", {}).get("live_balance")
                            _net  = _eq.get("net")
                            _avail = float(_live if _live is not None else _net or 0)
                            st.caption(f"Zerodha account balance: ₹{_avail:,.2f} (live)")
                        except Exception as _mf_err:
                            st.caption(f"Could not read account balance: {_mf_err}")
                        if _avail is None:
                            _avail = _capital
                            st.caption(f"Using capital limit ₹{_capital:,.0f} as proxy for available funds.")
                        _utilization = (_required / _avail * 100) if _avail > 0 else 0
                        mc1, mc2, mc3, mc4, mc5 = st.columns(5)
                        mc1.metric("SPAN Margin",      fmt_inr(_span))
                        mc2.metric("Exposure Margin",  fmt_inr(_exposure))
                        mc3.metric("Total Required",   fmt_inr(_required))
                        mc4.metric("Est. Charges",     fmt_inr(_charges_total))
                        mc5.metric("Available Funds",  fmt_inr(_avail))
                        st.progress(
                            min(_utilization / 100, 1.0),
                            text=f"Margin utilization: {_utilization:.1f}%  ({fmt_inr(_required)} of {fmt_inr(_avail)})"
                        )
                        if _required > _avail:
                            st.error(f"❌ Insufficient funds — need {fmt_inr(_required)}, have {fmt_inr(_avail)}")
                        elif _utilization > 80:
                            st.warning(f"⚠️ High margin utilization ({_utilization:.1f}%). Consider reducing lots.")
                        else:
                            st.success(f"✅ Sufficient funds. {fmt_inr(_avail - _required - _charges_total)} will remain free.")
                else:
                    # Paper mode — estimate from max loss
                    from risk.payoff import calculate_payoff_from_credit
                    _credit_mg = st.session_state.get("quotes_credit")
                    _lot_mg    = st.session_state.get("lot_size", 100)
                    if _credit_mg:
                        try:
                            _payoff_mg = calculate_payoff_from_credit(
                                strategy, _credit_mg.net_credit, _lot_mg
                            )
                            _max_loss_est  = float(_payoff_mg.max_loss)
                            _span_est      = _max_loss_est
                            _exposure_est  = _max_loss_est * 0.10
                            _required_est  = _span_est + _exposure_est
                            me1, me2, me3 = st.columns(3)
                            me1.metric("Est. SPAN (max loss)", fmt_inr(_span_est))
                            me2.metric("Est. Exposure (~10%)",  fmt_inr(_exposure_est))
                            me3.metric("Est. Total Margin",     fmt_inr(_required_est))
                            _util_est = (_required_est / _capital * 100) if _capital > 0 else 0
                            st.progress(
                                min(_util_est / 100, 1.0),
                                text=f"Est. margin utilization: {_util_est:.1f}%  of ₹{_capital:,.0f} capital limit"
                            )
                            if _required_est > _capital:
                                st.error(f"❌ Capital limit ₹{_capital:,.0f} may be insufficient. Estimated need: {fmt_inr(_required_est)}")
                            else:
                                st.success(f"✅ Within capital limit. Est. free after trade: {fmt_inr(_capital - _required_est)}")
                            st.caption("⚠️ Estimates only — connect Zerodha for exact SPAN/Exposure margin from NSE.")
                        except Exception:
                            pass
                    else:
                        st.info("Fetch quotes first to calculate margin estimate.")

            except Exception as e:
                st.error(f"Quotes display error: {e}")

    _quotes_fragment()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3: ENTRY
# ══════════════════════════════════════════════════════════════════════════════
with tab_entry:
    st.subheader("Paper Entry")

    if mode == "LIVE":
        st.warning(
            "\u26a0\ufe0f TRADING_MODE=LIVE detected. Paper entry uses simulated fills "
            "regardless \u2014 use `live-enter --confirm` from CLI for real orders."
        )

    if ks_active:
        st.error("\U0001f6d1 Kill switch is active. Deactivate it before entering.")
    else:
        st.info(
            "Paper entry simulates the full state machine and fill logic using current "
            "quotes. No real orders are placed."
        )

        col_btn1, col_btn2 = st.columns(2)
        preview_clicked = col_btn1.button("\U0001f50d Preview Entry")
        paper_enter_clicked = col_btn2.button(
            "\U0001f680 Paper Enter", type="primary", disabled=ks_active
        )

        if preview_clicked and strategy and risk:
            st.subheader("Entry Preview")
            try:
                import pandas as pd

                # Use live quotes if available in session, else fetch from Zerodha
                _prev_q = st.session_state.get("last_quotes", {})
                _prev_sym = st.session_state.get("resolved_symbols", {})
                if not _prev_q and creds:
                    with st.spinner("Fetching live quotes for preview..."):
                        try:
                            from brokers.zerodha import ZerodhaBroker
                            from execution.quote_engine import QuoteEngine
                            _zb_prev = ZerodhaBroker.from_env()
                            _instr_prev = []
                            for _ii, _ll in enumerate(strategy.legs):
                                _inst_pv = _zb_prev.resolve_instrument(
                                    strategy.underlying, strategy.exchange,
                                    strategy.expiry, _ll.strike, _ll.option_type.value,
                                )
                                _instr_prev.append((_inst_pv.instrument_token, _inst_pv.trading_symbol))
                                _prev_sym[_ii] = _inst_pv.trading_symbol
                            _prev_q = QuoteEngine(_zb_prev).fetch_all(_instr_prev)
                        except Exception as _pv_err:
                            st.caption(f"Could not fetch live preview: {_pv_err}")

                rows = []
                net_credit_preview = Decimal("0")
                for i, leg in enumerate(strategy.legs):
                    sym = _prev_sym.get(i) or (
                        f"{strategy.underlying}"
                        f"{strategy.expiry.strftime('%y%b').upper()}"
                        f"{int(leg.strike)}{leg.option_type.value}"
                    )
                    q_pv = _prev_q.get(sym)
                    row = {
                        "#": i,
                        "Side": leg.side.value,
                        "Strike": float(leg.strike),
                        "Type": leg.option_type.value,
                        "Symbol": sym,
                        "Bid": float(q_pv.bid) if q_pv else "—",
                        "Ask": float(q_pv.ask) if q_pv else "—",
                        "Spread %": (
                            f"{float(q_pv.spread_pct):.1f}%" if q_pv else "—"
                        ),
                    }
                    rows.append(row)
                    if q_pv:
                        if leg.side.value == "SELL":
                            net_credit_preview += q_pv.bid
                        else:
                            net_credit_preview -= q_pv.ask

                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Capital Limit", fmt_inr(risk.capital_limit))
                c2.metric("Min Net Credit", fmt_pts(risk.min_net_credit))
                if _prev_q:
                    net_f = float(net_credit_preview)
                    min_f = float(risk.min_net_credit)
                    c3.metric(
                        "Est. Net Credit",
                        f"{net_f:.2f} pts",
                        delta=f"{net_f - min_f:+.2f} vs min",
                        delta_color="normal" if net_f >= min_f else "inverse",
                    )
                    if net_f >= min_f:
                        c4.markdown("✅ **Meets credit minimum**")
                    else:
                        c4.markdown("⚠️ **Below credit minimum**")
            except Exception as e:
                st.error(f"Preview error: {e}")

        # ── Auto Entry ────────────────────────────────────────────────────
        st.divider()
        st.subheader("🤖 Auto Entry — Algorithm Selects Strikes")
        st.caption(
            "Automatically sweeps short-wing multipliers (wide → tight) until "
            "net credit ≥ minimum. No manual strike tuning needed."
        )

        _cache_path_ae = "logs/nfo_instruments_cache.csv"
        _cache_exists_ae = Path(_cache_path_ae).exists()

        if not _cache_exists_ae:
            st.warning("Instrument cache missing. Run `resolve-instruments --live` first.")
        elif not creds:
            st.warning("Zerodha credentials required for Auto Entry (needs live quotes to check credit).")
        elif not strategy or not risk or not paper_cfg:
            st.warning("Strategy/risk config not loaded.")
        else:
            _ae_col1, _ae_col2 = st.columns(2)
            with _ae_col1:
                _ae_iv = st.slider(
                    "IV Assumption (%)", 10, 80,
                    st.session_state.get("ae_iv", 35), 1, key="ae_iv_slider",
                    help="Implied volatility used to compute expected move and select strikes"
                )
                _ae_lots = st.number_input(
                    "Lots", 1, 20,
                    strategy.lots if strategy else 1, key="ae_lots"
                )
            with _ae_col2:
                _ae_mult_start = st.slider(
                    "Start: short wing mult", 0.5, 2.0, 1.0, 0.1, key="ae_mult_start",
                    help="Start wide (1.0×EM from spot) and tighten until credit is met"
                )
                _ae_mult_min = st.slider(
                    "Stop: min short wing mult", 0.2, 1.0, 0.3, 0.1, key="ae_mult_min",
                    help="Give up if strikes are this close to spot and credit still not met"
                )

            auto_enter_clicked = st.button(
                "🤖 Auto-Select Strikes & Paper Enter", type="primary", disabled=ks_active
            )

            if auto_enter_clicked:
                with st.spinner("Finding optimal strikes and entering..."):
                    try:
                        from brokers.zerodha import ZerodhaBroker
                        from brokers.paper import PaperBroker
                        from execution.entry_engine import EntryEngine
                        from execution.models import (
                            LegConfig, OptionType, ResolvedInstrument,
                            Side, StrategyConfig, StrategyState,
                        )
                        from execution.state_machine import StateMachine
                        from monitoring.alerts import AlertManager
                        from risk.kill_switch import KillSwitch
                        from stock_picker import (
                            auto_select_ic_strikes,
                            get_strikes_for_underlying_expiry,
                            get_lot_size_for_underlying,
                        )

                        _ae_broker = ZerodhaBroker.from_env()

                        # Fetch live spot
                        try:
                            _ae_raw = _ae_broker._kite.quote([f"NSE:{strategy.underlying}"])
                            _ae_spot = Decimal(str(_ae_raw[f"NSE:{strategy.underlying}"]["last_price"]))
                        except Exception:
                            st.error("Could not fetch live spot price from Zerodha. Is the market open?")
                            _ae_spot = None

                        if _ae_spot:
                            st.info(f"Live spot: **{float(_ae_spot):.2f}**")

                            _pe_strikes = get_strikes_for_underlying_expiry(
                                _cache_path_ae, strategy.underlying, strategy.expiry, "PE"
                            )
                            _ce_strikes = get_strikes_for_underlying_expiry(
                                _cache_path_ae, strategy.underlying, strategy.expiry, "CE"
                            )
                            _all_strikes_ae = sorted(set(_pe_strikes + _ce_strikes))
                            _lot_size_ae = get_lot_size_for_underlying(_cache_path_ae, strategy.underlying) or 100

                            _winner, _ae_log = auto_select_ic_strikes(
                                underlying=strategy.underlying,
                                expiry=strategy.expiry,
                                spot=_ae_spot,
                                iv=Decimal(str(_ae_iv / 100)),
                                available_strikes=_all_strikes_ae,
                                lot_size=_lot_size_ae,
                                lots=int(_ae_lots),
                                min_credit=risk.min_net_credit,
                                broker=_ae_broker,
                                exchange=strategy.exchange,
                                short_mult_start=_ae_mult_start,
                                short_mult_min=_ae_mult_min,
                            )

                            # Show strike sweep log
                            st.write("**Strike sweep log**")
                            import pandas as pd
                            _log_display = [
                                {
                                    "Mult": r["mult"],
                                    "Sell PE": r.get("sell_put"),
                                    "Sell CE": r.get("sell_call"),
                                    "Credit (pts)": f"{r['credit']:.2f}" if r.get("credit") is not None else "—",
                                    "Result": r["status"],
                                }
                                for r in _ae_log
                            ]
                            st.dataframe(pd.DataFrame(_log_display), use_container_width=True, hide_index=True)

                            if _winner is None:
                                st.error(
                                    f"❌ Could not find strikes meeting minimum credit "
                                    f"({float(risk.min_net_credit):.2f} pts) even at tightest wings. "
                                    f"Try lowering `min_net_credit` in Config Editor, or increasing IV assumption."
                                )
                            else:
                                # Pull resolved instruments + quotes from winning log entry
                                _win_row = next(r for r in _ae_log if r["status"].startswith("✅"))
                                _ae_resolved = _win_row["resolved"]
                                _ae_quotes   = _win_row["quotes"]

                                st.success(
                                    f"✅ Found: SELL PE {float(_winner.sell_put_strike):.0f}  |  "
                                    f"SELL CE {float(_winner.sell_call_strike):.0f}  |  "
                                    f"Credit: {float(_win_row['credit']):.2f} pts"
                                )

                                # Update strategy.yaml with auto-selected strikes
                                import yaml as _yaml_ae
                                _new_strat_ae = {
                                    "strategy": {
                                        "name": strategy.name,
                                        "underlying": strategy.underlying,
                                        "exchange": strategy.exchange,
                                        "expiry": str(strategy.expiry),
                                        "lots": int(_ae_lots),
                                        "legs": [
                                            {"side": "BUY",  "option_type": "PE", "strike": float(_winner.buy_put_strike)},
                                            {"side": "SELL", "option_type": "PE", "strike": float(_winner.sell_put_strike)},
                                            {"side": "SELL", "option_type": "CE", "strike": float(_winner.sell_call_strike)},
                                            {"side": "BUY",  "option_type": "CE", "strike": float(_winner.buy_call_strike)},
                                        ],
                                    }
                                }
                                with open("config/strategy.yaml", "w") as _f_ae:
                                    _yaml_ae.dump(_new_strat_ae, _f_ae, default_flow_style=False, sort_keys=False)

                                # Build StrategyConfig from auto-selected legs
                                _ae_legs = [
                                    LegConfig(side=Side.BUY,  option_type=OptionType.PE, strike=_winner.buy_put_strike),
                                    LegConfig(side=Side.SELL, option_type=OptionType.PE, strike=_winner.sell_put_strike),
                                    LegConfig(side=Side.SELL, option_type=OptionType.CE, strike=_winner.sell_call_strike),
                                    LegConfig(side=Side.BUY,  option_type=OptionType.CE, strike=_winner.buy_call_strike),
                                ]
                                _ae_strategy = StrategyConfig(
                                    name=strategy.name,
                                    underlying=strategy.underlying,
                                    exchange=strategy.exchange,
                                    expiry=strategy.expiry,
                                    lots=int(_ae_lots),
                                    legs=_ae_legs,
                                )

                                # Run paper entry with auto-resolved instruments + live broker
                                _ae_paper = PaperBroker(
                                    live_quote_source=_ae_broker,
                                    fill_mode=paper_cfg.fill_mode,
                                    fill_delay_seconds=0,
                                    slippage_per_order=paper_cfg.slippage_per_order,
                                )
                                _ae_ks = KillSwitch()
                                _ae_sm = StateMachine()
                                _ae_alerts = AlertManager()
                                _ae_run_id = str(uuid.uuid4())

                                from execution.order_manager import OrderManager
                                _ae_om = OrderManager(_ae_paper, risk, _ae_ks)

                                from execution.entry_engine import EntryEngine
                                _ae_entry = EntryEngine(
                                    strategy=_ae_strategy,
                                    resolved=_ae_resolved,
                                    broker=_ae_paper,
                                    risk=risk,
                                    kill_switch=_ae_ks,
                                    alert_manager=_ae_alerts,
                                    state_machine=_ae_sm,
                                    order_manager=_ae_om,
                                    strategy_run_id=_ae_run_id,
                                )
                                _ae_tracker = _ae_entry.run()

                                _ae_sides = {i: l.side for i, l in enumerate(_ae_legs)}
                                _ae_credit = _ae_tracker.actual_net_credit(_ae_sides) if _ae_tracker.all_filled else None

                                st.session_state.last_run_id = _ae_run_id
                                st.session_state.last_entry_result = {
                                    "run_id": _ae_run_id,
                                    "state": _ae_sm.state.value,
                                    "summary": _ae_tracker.summary(),
                                    "all_filled": _ae_tracker.all_filled,
                                    "credit": decimal_to_float(_ae_credit),
                                    "lot_size": _lot_size_ae,
                                    "resolved_syms": {
                                        i: inst.trading_symbol
                                        for i, inst in _ae_resolved.items()
                                    },
                                }

                                # DB persist
                                try:
                                    from storage.database import create_db_engine
                                    from storage.repositories import StrategyRepository
                                    _ae_eng = create_db_engine(get_db_path())
                                    with _ae_eng.connect() as _ae_conn:
                                        _ae_sr = StrategyRepository(_ae_conn)
                                        _ae_sr.create(
                                            _ae_run_id, _ae_strategy.name,
                                            _ae_strategy.underlying, _ae_strategy.exchange,
                                            str(_ae_strategy.expiry), _ae_strategy.lots, "PAPER",
                                        )
                                        if _ae_tracker.all_filled and _ae_credit:
                                            _ae_sr.update_pnl(
                                                _ae_run_id,
                                                entry_credit=_ae_credit * _lot_size_ae * int(_ae_lots),
                                            )
                                            _ae_sr.update_state(_ae_run_id, _ae_sm.state)
                                except Exception:
                                    pass

                                st.rerun()

                    except Exception as _ae_err:
                        st.error(f"Auto entry error: {_ae_err}")

        st.divider()
        if paper_enter_clicked and strategy and risk and paper_cfg:
            with st.spinner("Running paper entry simulation..."):
                try:
                    from brokers.paper import PaperBroker
                    from execution.entry_engine import EntryEngine
                    from execution.models import (
                        Quote,
                        ResolvedInstrument,
                        Side,
                        StrategyState,
                    )
                    from execution.state_machine import StateMachine
                    from monitoring.alerts import AlertManager
                    from risk.kill_switch import KillSwitch

                    lot_size = 100
                    resolved = {}

                    if creds:
                        # ── Live path: resolve real instruments via Zerodha ──
                        from brokers.zerodha import ZerodhaBroker
                        live_broker = ZerodhaBroker.from_env()

                        for i, leg in enumerate(strategy.legs):
                            inst = live_broker.resolve_instrument(
                                underlying=strategy.underlying,
                                exchange=strategy.exchange,
                                expiry=strategy.expiry,
                                strike=leg.strike,
                                option_type=leg.option_type.value,
                            )
                            resolved[i] = inst
                            lot_size = inst.lot_size

                        broker = PaperBroker(
                            live_quote_source=live_broker,
                            fill_mode=paper_cfg.fill_mode,
                            fill_delay_seconds=0,
                            slippage_per_order=paper_cfg.slippage_per_order,
                        )
                    else:
                        # ── Offline path: use last fetched quotes or hardcoded fallback ──
                        from unittest.mock import MagicMock

                        last_q = st.session_state.get("last_quotes", {})
                        last_sym = st.session_state.get("resolved_symbols", {})
                        _fallback = {
                            0: ("7.50", "8.00"),
                            1: ("12.00", "12.50"),
                            2: ("13.00", "13.50"),
                            3: ("10.00", "10.50"),
                        }

                        for i, leg in enumerate(strategy.legs):
                            sym = last_sym.get(i) or (
                                f"{strategy.underlying}"
                                f"{strategy.expiry.strftime('%y%b').upper()}"
                                f"{int(leg.strike)}{leg.option_type.value}"
                            )
                            resolved[i] = ResolvedInstrument(
                                instrument_token=1000 + i,
                                trading_symbol=sym,
                                exchange=strategy.exchange,
                                underlying=strategy.underlying,
                                expiry=strategy.expiry,
                                strike=leg.strike,
                                option_type=leg.option_type,
                                lot_size=lot_size,
                                tick_size=Decimal("0.05"),
                            )

                        def _make_sim_quotes(instrument_list):
                            result = {}
                            for token, sym in instrument_list:
                                cached = last_q.get(sym)
                                if cached:
                                    result[token] = cached.model_copy(
                                        update={"instrument_token": token}
                                    )
                                else:
                                    idx = token - 1000
                                    bid, ask = _fallback.get(idx, ("5.00", "6.00"))
                                    result[token] = Quote(
                                        instrument_token=token,
                                        trading_symbol=sym,
                                        exchange=strategy.exchange,
                                        timestamp=datetime.now(IST),
                                        ltp=Decimal(bid),
                                        bid=Decimal(bid),
                                        bid_qty=200,
                                        ask=Decimal(ask),
                                        ask_qty=200,
                                        volume=10000,
                                        oi=100000,
                                    )
                            return result

                        mock_source = MagicMock()
                        mock_source.get_quotes.side_effect = _make_sim_quotes

                        broker = PaperBroker(
                            live_quote_source=mock_source,
                            fill_mode=paper_cfg.fill_mode,
                            fill_delay_seconds=0,
                            slippage_per_order=paper_cfg.slippage_per_order,
                        )

                    ks2 = KillSwitch()
                    sm = StateMachine()
                    alerts = AlertManager()
                    run_id = str(uuid.uuid4())

                    entry = EntryEngine(
                        strategy=strategy,
                        resolved=resolved,
                        broker=broker,
                        risk=risk,
                        kill_switch=ks2,
                        alert_manager=alerts,
                        state_machine=sm,
                        strategy_run_id=run_id,
                    )
                    tracker = entry.run()

                    # Build resolved symbol map for display
                    resolved_syms = {
                        i: (inst.trading_symbol if hasattr(inst, "trading_symbol") else "")
                        for i, inst in resolved.items()
                    }

                    st.session_state.last_run_id = run_id
                    st.session_state.last_entry_result = {
                        "run_id": run_id,
                        "state": sm.state.value,
                        "summary": tracker.summary(),
                        "all_filled": tracker.all_filled,
                        "credit": None,
                        "lot_size": lot_size,
                        "resolved_syms": resolved_syms,
                    }
                    if tracker.all_filled:
                        sides = {i: leg.side for i, leg in enumerate(strategy.legs)}
                        credit = tracker.actual_net_credit(sides)
                        st.session_state.last_entry_result["credit"] = decimal_to_float(credit)

                    # Persist to DB if available
                    try:
                        from storage.database import create_db_engine
                        from storage.repositories import StrategyRepository

                        db_path = get_db_path()
                        engine = create_db_engine(db_path)
                        with engine.connect() as conn:
                            sr = StrategyRepository(conn)
                            sr.create(
                                run_id,
                                strategy.name,
                                strategy.underlying,
                                strategy.exchange,
                                str(strategy.expiry),
                                strategy.lots,
                                "PAPER",
                            )
                            if tracker.all_filled:
                                sides2 = {
                                    i: leg.side
                                    for i, leg in enumerate(strategy.legs)
                                }
                                actual_credit = tracker.actual_net_credit(sides2)
                                sr.update_pnl(
                                    run_id,
                                    entry_credit=actual_credit * lot_size * strategy.lots
                                    if actual_credit
                                    else None,
                                )
                                sr.update_state(run_id, sm.state)
                    except Exception as _db_err:
                        pass

                except Exception as e:
                    st.error(f"Paper entry error: {e}")

        result = st.session_state.last_entry_result
        if result:
            st.subheader("Last Entry Result")
            state = result["state"]
            if result["all_filled"]:
                st.success(f"✅ **ENTRY COMPLETE** — State: {state}")
                c1, c2, c3 = st.columns(3)
                c1.metric("Run ID", result["run_id"][:16] + "...")
                if result["credit"] is not None:
                    lot_sz = result.get("lot_size", 100)
                    lots_n = strategy.lots if strategy else 1
                    total_credit_inr = result["credit"] * lot_sz * lots_n
                    c2.metric("Net Credit", f"{result['credit']:.2f} pts/share")
                    c3.metric("Total Credit", fmt_inr(total_credit_inr))

                # Show fill prices from DB
                try:
                    import pandas as pd
                    from sqlalchemy import text
                    from storage.database import create_db_engine
                    _eng = create_db_engine(get_db_path())
                    with _eng.connect() as _conn:
                        _fills = pd.read_sql(
                            text(
                                "SELECT o.trading_symbol, o.side, o.quantity, "
                                "f.price as fill_price, f.timestamp "
                                "FROM fills f JOIN orders o ON f.order_id=o.order_id "
                                "WHERE o.strategy_id=:sid ORDER BY f.timestamp"
                            ),
                            _conn,
                            params={"sid": result["run_id"]},
                        )
                    if not _fills.empty:
                        _fills["fill_price"] = _fills["fill_price"].apply(
                            lambda x: float(x) if x is not None else None
                        )
                        st.write("**Fill Prices**")
                        st.dataframe(_fills, use_container_width=True, hide_index=True)
                except Exception:
                    pass

                # ── Accept this trade ──────────────────────────────────────
                st.divider()
                already_accepted = (
                    st.session_state.get("accepted_trade", {}).get("run_id") == result["run_id"]
                )
                if already_accepted:
                    st.success(
                        "✅ **Trade accepted.** Credit locked in: "
                        f"{st.session_state.accepted_trade['credit']:.2f} pts/share  |  "
                        f"Run ID: {result['run_id'][:16]}..."
                    )
                else:
                    if st.button("✅ Accept This Trade", type="primary", key="accept_trade_btn"):
                        st.session_state.accepted_trade = {
                            "run_id": result["run_id"],
                            "credit": result["credit"],
                            "lot_size": result.get("lot_size", 100),
                            "underlying": strategy.underlying if strategy else "—",
                            "expiry": str(strategy.expiry) if strategy else "—",
                            "accepted_at": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
                        }
                        st.success("Trade accepted! See Status tab for confirmation.")
                        st.rerun()
            else:
                st.warning(f"⚠️ Entry did NOT complete — State: **{state}**")
                s = result["summary"]
                st.write(
                    f"Filled: {s['filled_legs']} / {s['total_legs']}  |  "
                    f"Unfilled: {s['unfilled_legs']}"
                )

                # ── Abort diagnosis ────────────────────────────────────────
                st.divider()
                st.write("**Why it aborted**")

                # Pull alerts from DB for this run — they contain the exact reason
                _abort_explained = False
                try:
                    import pandas as pd
                    from sqlalchemy import text
                    from storage.database import create_db_engine
                    _eng_d = create_db_engine(get_db_path())
                    with _eng_d.connect() as _conn_d:
                        _why_df = pd.read_sql(
                            text(
                                "SELECT level, event, message FROM alerts "
                                "WHERE strategy_id=:sid ORDER BY timestamp"
                            ),
                            _conn_d,
                            params={"sid": result["run_id"]},
                        )
                    if not _why_df.empty:
                        _abort_explained = True
                        _icons = {"WARNING": "⚠️", "CRITICAL": "🔴", "EMERGENCY": "🚨", "INFO": "ℹ️"}
                        for _, _row in _why_df.iterrows():
                            _ic = _icons.get(str(_row["level"]), "•")
                            st.write(f"{_ic} `{_row['event']}` — {_row['message']}")
                except Exception:
                    pass

                # If no DB, guess from quote data in session
                if not _abort_explained:
                    _lq = st.session_state.get("last_quotes", {})
                    _ls = st.session_state.get("resolved_symbols", {})
                    if _lq and strategy and risk:
                        import pandas as pd
                        _diag_rows = []
                        _net = Decimal("0")
                        for _di, _dl in enumerate(strategy.legs):
                            _ds = _ls.get(_di, "")
                            _dq = _lq.get(_ds)
                            if _dq:
                                _spread_ok = _dq.spread_pct <= risk.max_bid_ask_spread_pct
                                _diag_rows.append({
                                    "Leg": _ds,
                                    "Side": _dl.side.value,
                                    "Bid": float(_dq.bid),
                                    "Ask": float(_dq.ask),
                                    "Spread %": f"{float(_dq.spread_pct):.1f}%",
                                    "Spread OK?": "✅" if _spread_ok else f"❌ > {float(risk.max_bid_ask_spread_pct):.0f}%",
                                })
                                if _dl.side.value == "SELL":
                                    _net += _dq.bid
                                else:
                                    _net -= _dq.ask
                        if _diag_rows:
                            st.dataframe(pd.DataFrame(_diag_rows), use_container_width=True, hide_index=True)
                            _net_f = float(_net)
                            _min_f = float(risk.min_net_credit)
                            if _net_f < _min_f:
                                st.error(
                                    f"❌ **Net credit {_net_f:.2f} pts < minimum {_min_f:.2f} pts.** "
                                    f"The strategy doesn't collect enough premium at current prices.  \n"
                                    f"**Fix:** lower `min_net_credit` in Config Editor, widen the spread "
                                    f"(move short strikes closer to spot), or wait for higher IV."
                                )
                            else:
                                st.info("Credit looks OK — check Quotes tab for spread/staleness issues.")
                    else:
                        st.info("Go to the **Quotes tab** and click 'Fetch Quotes' to diagnose the issue before retrying.")

                # Quick-fix suggestions
                st.divider()
                st.write("**Common fixes**")
                st.markdown(
                    "- **Credit too low** → lower `min_net_credit` in Config Editor or widen the spreads in Stock Picker  \n"
                    "- **Spread too wide** → raise `max_bid_ask_spread_pct` (illiquid option — try a more liquid underlying)  \n"
                    "- **Order not filled** → lower `max_repricing_attempts` or check market hours (entry window 09:20–14:30 IST)  \n"
                    "- **Outside entry window** → check `entry_start_time`/`entry_end_time` in Risk Config"
                )

        # Entry log from SQLite
        db_path = get_db_path()
        if Path(db_path).exists():
            st.subheader("Recent Paper Entries (from DB)")
            try:
                import pandas as pd
                from sqlalchemy import text
                from storage.database import create_db_engine

                engine = create_db_engine(db_path)
                with engine.connect() as conn:
                    entry_df = pd.read_sql(
                        text(
                            "SELECT id, name, underlying, expiry, lots, state, trading_mode, "
                            "entry_credit, final_pnl, created_at "
                            "FROM strategies WHERE trading_mode='PAPER' "
                            "ORDER BY created_at DESC LIMIT 10"
                        ),
                        conn,
                    )
                if not entry_df.empty:
                    # Convert Decimal columns to float for Streamlit
                    for col in ["entry_credit", "final_pnl"]:
                        if col in entry_df.columns:
                            entry_df[col] = entry_df[col].apply(
                                lambda x: float(x) if x is not None else None
                            )
                    st.dataframe(
                        entry_df, use_container_width=True, hide_index=True
                    )
                else:
                    st.info("No paper entries in database yet.")
            except Exception as e:
                st.error(f"Entry log error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4: MONITOR
# ══════════════════════════════════════════════════════════════════════════════
with tab_monitor:
    st.subheader("Position Monitor")

    col_mon1, col_mon2 = st.columns([3, 1])
    with col_mon2:
        if st.button("\U0001f504 Refresh Monitor"):
            st.rerun()

    db_path = get_db_path()
    if not Path(db_path).exists():
        st.info("No database found yet. Run a paper entry first.")
    else:
        try:
            import pandas as pd
            from sqlalchemy import text
            from storage.database import create_db_engine

            engine = create_db_engine(db_path)

            with engine.connect() as conn:
                # Latest strategy
                strat_df = pd.read_sql(
                    text(
                        "SELECT id, name, underlying, expiry, state, trading_mode, "
                        "entry_credit, final_pnl, created_at "
                        "FROM strategies ORDER BY created_at DESC LIMIT 1"
                    ),
                    conn,
                )
                if strat_df.empty:
                    st.info("No strategies recorded yet.")
                else:
                    s = strat_df.iloc[0]
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("State", str(s["state"]))
                    col2.metric("Mode", str(s["trading_mode"]))
                    entry_credit_val = s["entry_credit"]
                    final_pnl_val = s["final_pnl"]
                    col3.metric(
                        "Entry Credit",
                        fmt_inr(entry_credit_val)
                        if entry_credit_val is not None
                        else "N/A",
                    )
                    col4.metric(
                        "Final P&L",
                        fmt_inr(final_pnl_val)
                        if final_pnl_val is not None
                        else "N/A",
                    )
                    st.caption(
                        f"Run ID: {s['id']}  |  Created: {s['created_at']}"
                    )

                    # MTM P&L section (computed from current sim prices if positions exist)
                    st.subheader("MTM P&L")
                    try:
                        from execution.models import LegPosition, Side, OptionType
                        from risk.payoff import (
                            calculate_cost_to_close,
                            calculate_mtm_pnl,
                        )

                        pos_df_check = pd.read_sql(
                            text(
                                "SELECT leg_index, trading_symbol, instrument_token, "
                                "side, quantity, average_entry_price "
                                "FROM positions WHERE strategy_id=:sid"
                            ),
                            conn,
                            params={"sid": str(s["id"])},
                        )

                        if not pos_df_check.empty and strategy and entry_credit_val is not None:
                            # Use live quotes from Quotes tab if available, else Zerodha, else fallback
                            _live_q = st.session_state.get("last_quotes", {})
                            _live_sym = st.session_state.get("resolved_symbols", {})
                            if not _live_q and creds:
                                try:
                                    from brokers.zerodha import ZerodhaBroker
                                    from execution.quote_engine import QuoteEngine
                                    _zb_mon = ZerodhaBroker.from_env()
                                    _instr_mon = []
                                    _sym_map_mon = {}
                                    for _ii, _ll in enumerate(strategy.legs):
                                        _inst_mon = _zb_mon.resolve_instrument(
                                            strategy.underlying, strategy.exchange,
                                            strategy.expiry, _ll.strike, _ll.option_type.value,
                                        )
                                        _instr_mon.append((_inst_mon.instrument_token, _inst_mon.trading_symbol))
                                        _sym_map_mon[_ii] = _inst_mon.trading_symbol
                                    _qe_mon = QuoteEngine(_zb_mon)
                                    _live_q = _qe_mon.fetch_all(_instr_mon)
                                    _live_sym = _sym_map_mon
                                except Exception:
                                    pass

                            _fallback_mon = {
                                0: ("7.50", "8.00"),
                                1: ("12.00", "12.50"),
                                2: ("13.00", "13.50"),
                                3: ("10.00", "10.50"),
                            }
                            leg_positions_mon = []
                            for _, row in pos_df_check.iterrows():
                                idx = int(row["leg_index"])
                                leg = strategy.legs[idx] if idx < len(strategy.legs) else None
                                if leg is None:
                                    continue
                                sym = _live_sym.get(idx, str(row["trading_symbol"]))
                                q_mon = _live_q.get(sym)
                                if q_mon:
                                    bid_dec = q_mon.bid
                                    ask_dec = q_mon.ask
                                else:
                                    fb_b, fb_a = _fallback_mon.get(idx, ("5.00", "6.00"))
                                    bid_dec = Decimal(fb_b)
                                    ask_dec = Decimal(fb_a)
                                leg_positions_mon.append(
                                    LegPosition(
                                        leg_index=idx,
                                        trading_symbol=str(row["trading_symbol"]),
                                        instrument_token=int(row["instrument_token"]),
                                        side=Side(str(row["side"])),
                                        option_type=leg.option_type,
                                        strike=leg.strike,
                                        lot_size=100,
                                        lots=strategy.lots,
                                        quantity=int(row["quantity"]),
                                        average_entry_price=Decimal(str(row["average_entry_price"])),
                                        current_bid=bid_dec,
                                        current_ask=ask_dec,
                                        current_ltp=bid_dec,
                                    )
                                )

                            if leg_positions_mon:
                                cost_to_close = calculate_cost_to_close(
                                    leg_positions_mon
                                )
                                entry_credit_total = Decimal(
                                    str(entry_credit_val)
                                )
                                mtm = calculate_mtm_pnl(
                                    entry_credit_total, cost_to_close
                                )
                                mtm_float = float(mtm)
                                ctc_float = float(cost_to_close)

                                mc1, mc2, mc3, mc4 = st.columns(4)
                                mc1.metric(
                                    "Exit P&L (if closed now)",
                                    fmt_inr(mtm),
                                    delta_color="normal"
                                    if mtm_float >= 0
                                    else "inverse",
                                )
                                mc2.metric("Cost to Close", fmt_inr(cost_to_close))
                                # Premium retention pct
                                try:
                                    _ret_pct = (1.0 - ctc_float / float(entry_credit_val)) * 100
                                    mc4.metric("Premium Retained", f"{_ret_pct:.1f}%",
                                               help="% of initial credit still retained. Close at 80%.")
                                except Exception:
                                    pass

                                if entry_credit_val and strategy:
                                    try:
                                        from risk.payoff import (
                                            calculate_payoff_from_credit,
                                        )
                                        payoff_mon = calculate_payoff_from_credit(
                                            strategy,
                                            Decimal(str(entry_credit_val))
                                            / (100 * strategy.lots),
                                            100,
                                        )
                                        max_profit = float(payoff_mon.max_profit)
                                        if max_profit > 0:
                                            pct_captured = min(
                                                100.0,
                                                max(
                                                    0.0,
                                                    mtm_float / max_profit * 100,
                                                ),
                                            )
                                            mc3.metric(
                                                "% Max Profit Captured",
                                                f"{pct_captured:.1f}%",
                                            )
                                            st.progress(
                                                pct_captured / 100,
                                                text=f"{pct_captured:.1f}% of max profit captured",
                                            )
                                    except Exception:
                                        pass
                        else:
                            st.info(
                                "No position data available for MTM calculation."
                            )
                    except Exception as e:
                        st.info(f"MTM calculation not available: {e}")

                    # DTE countdown
                    if strategy:
                        try:
                            dte_mon = max(0, (strategy.expiry - date.today()).days)
                            st.metric("Days to Expiry (DTE)", dte_mon)
                        except Exception:
                            pass

                    # ── Candlestick Chart ──────────────────────────────────
                    st.divider()
                    st.subheader("📈 Price Chart")

                    _underlying_mon = str(s["underlying"])
                    _entry_date_mon = s["created_at"]
                    if hasattr(_entry_date_mon, "date"):
                        _entry_date_mon = _entry_date_mon.date()
                    elif isinstance(_entry_date_mon, str):
                        try:
                            _entry_date_mon = date.fromisoformat(str(_entry_date_mon)[:10])
                        except Exception:
                            _entry_date_mon = date.today()

                    # Collect short strikes by parsing trading symbols directly
                    # (avoids mismatch between strategy.yaml and DB entry)
                    import re as _re
                    def _strike_from_sym(sym: str) -> Optional[float]:
                        m = _re.search(r'(\d+(?:\.\d+)?)(CE|PE)$', str(sym))
                        return float(m.group(1)) if m else None

                    _sell_put_k: Optional[float] = None
                    _sell_call_k: Optional[float] = None
                    _buy_put_k: Optional[float] = None
                    _buy_call_k: Optional[float] = None
                    if not pos_df_check.empty:
                        for _, _prow in pos_df_check.iterrows():
                            _ps  = str(_prow["side"])
                            _sym = str(_prow["trading_symbol"])
                            _pk  = _strike_from_sym(_sym)
                            if _pk is None:
                                continue
                            if _ps == "SELL" and _sym.endswith("PE"):
                                _sell_put_k = _pk
                            elif _ps == "SELL" and _sym.endswith("CE"):
                                _sell_call_k = _pk
                            elif _ps == "BUY" and _sym.endswith("PE"):
                                _buy_put_k = _pk
                            elif _ps == "BUY" and _sym.endswith("CE"):
                                _buy_call_k = _pk

                    if creds:
                        try:
                            import plotly.graph_objects as go
                            from datetime import timedelta
                            from brokers.zerodha import ZerodhaBroker as _ZB

                            _zb_chart = _ZB.from_env()
                            _idx_token = _zb_chart.get_index_token(_underlying_mon)

                            if _idx_token:
                                _hist_from = date.today() - timedelta(days=60)
                                _hist_to   = date.today()
                                _ohlc = _zb_chart.get_historical_data(
                                    _idx_token, _hist_from, _hist_to, interval="day"
                                )

                                if _ohlc:
                                    import pandas as pd
                                    _df_ohlc = pd.DataFrame(_ohlc)
                                    _df_ohlc["date"] = pd.to_datetime(_df_ohlc["date"])

                                    _fig = go.Figure()

                                    # Candlestick body
                                    _fig.add_trace(go.Candlestick(
                                        x=_df_ohlc["date"],
                                        open=_df_ohlc["open"],
                                        high=_df_ohlc["high"],
                                        low=_df_ohlc["low"],
                                        close=_df_ohlc["close"],
                                        name=_underlying_mon,
                                        increasing_line_color="#26a69a",
                                        decreasing_line_color="#ef5350",
                                    ))

                                    # Profit zone shading (between sell strikes)
                                    if _sell_put_k and _sell_call_k:
                                        _fig.add_hrect(
                                            y0=_sell_put_k, y1=_sell_call_k,
                                            fillcolor="rgba(38,166,154,0.10)",
                                            line_width=0,
                                            annotation_text="Profit Zone",
                                            annotation_position="top left",
                                            annotation_font_color="#26a69a",
                                        )

                                    # Strike lines
                                    _strike_lines = [
                                        (_sell_put_k,  "#ef5350", "Sell Put"),
                                        (_sell_call_k, "#ef5350", "Sell Call"),
                                        (_buy_put_k,   "#b0bec5", "Buy Put"),
                                        (_buy_call_k,  "#b0bec5", "Buy Call"),
                                    ]
                                    for _sk, _sc, _sl in _strike_lines:
                                        if _sk:
                                            _fig.add_hline(
                                                y=_sk,
                                                line_color=_sc,
                                                line_dash="dash",
                                                line_width=1.5,
                                                annotation_text=f"{_sl}: {_sk:.0f}",
                                                annotation_position="right",
                                                annotation_font_size=11,
                                            )

                                    # Entry date marker
                                    _fig.add_vline(
                                        x=str(_entry_date_mon),
                                        line_color="#ffa726",
                                        line_dash="dot",
                                        line_width=1.5,
                                        annotation_text="Entry",
                                        annotation_position="top right",
                                        annotation_font_color="#ffa726",
                                    )

                                    _fig.update_layout(
                                        title=f"{_underlying_mon} — Daily OHLC (last 60 days)",
                                        xaxis_rangeslider_visible=False,
                                        height=480,
                                        margin=dict(l=10, r=80, t=50, b=10),
                                        plot_bgcolor="#0e1117",
                                        paper_bgcolor="#0e1117",
                                        font_color="#fafafa",
                                        xaxis=dict(gridcolor="#1e2130"),
                                        yaxis=dict(gridcolor="#1e2130"),
                                    )
                                    st.plotly_chart(_fig, use_container_width=True)
                                else:
                                    st.info("No historical data returned from Zerodha.")
                            else:
                                st.info(
                                    f"Candlestick not available for **{_underlying_mon}** "
                                    "(index token unknown — only NIFTY, BANKNIFTY, FINNIFTY supported)."
                                )
                        except Exception as _chart_err:
                            st.warning(f"Chart unavailable: {_chart_err}")
                    else:
                        st.info("Connect Zerodha to view live candlestick charts.")

                    # ── Prediction Panel ───────────────────────────────────
                    st.divider()
                    st.subheader("🔮 Prediction — Current Month")

                    try:
                        from monitoring.prediction import (
                            calculate_historical_volatility,
                            probability_of_profit,
                            expected_pnl_at_expiry,
                            monthly_forecast,
                        )
                        from decimal import Decimal as _Dec

                        # Collect inputs — DTE from strategy expiry in DB
                        _pred_dte = 0
                        try:
                            _db_expiry_str = str(s["expiry"] or "")[:10]
                            if _db_expiry_str:
                                _db_expiry = date.fromisoformat(_db_expiry_str)
                                _pred_dte = max(0, (_db_expiry - date.today()).days)
                        except Exception:
                            if strategy:
                                try:
                                    _pred_dte = max(0, (strategy.expiry - date.today()).days)
                                except Exception:
                                    pass

                        # IV from auto_trader config (source of truth for live runs)
                        _pred_iv = 0.30
                        try:
                            import yaml as _yaml_pred
                            _pred_iv = float(
                                _yaml_pred.safe_load(
                                    open("config/auto_trader.yaml").read()
                                ).get("auto_trader", {}).get("entry", {}).get("iv_assumption", 0.30)
                            )
                        except Exception:
                            pass

                        # Spot price for the position's underlying (from DB row)
                        _pred_spot = None
                        _INDEX_QUOTE_MAP = {
                            "NIFTY":      "NSE:NIFTY 50",
                            "BANKNIFTY":  "NSE:NIFTY BANK",
                            "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
                            "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
                        }
                        if creds:
                            try:
                                from brokers.zerodha import ZerodhaBroker as _ZBP
                                _zb_pred = _ZBP.from_env()
                                _pred_sym = _INDEX_QUOTE_MAP.get(
                                    _underlying_mon.upper(), f"NSE:{_underlying_mon}"
                                )
                                _spot_raw = _zb_pred._kite.ltp([_pred_sym])
                                _pred_spot = float(_spot_raw[_pred_sym]["last_price"])
                            except Exception:
                                pass

                        # HV from historical data (reuse _ohlc if fetched above)
                        _pred_hv = 0.0
                        _ohlc_for_hv = locals().get("_ohlc", [])
                        if _ohlc_for_hv:
                            _pred_hv = calculate_historical_volatility(_ohlc_for_hv, window=20)

                        _pred_col1, _pred_col2, _pred_col3 = st.columns(3)
                        _pred_col1.metric("IV Assumption", f"{_pred_iv*100:.0f}%")
                        _pred_col2.metric("HV (20-day)", f"{_pred_hv*100:.1f}%" if _pred_hv else "N/A")
                        _pred_col3.metric(
                            "IV vs HV",
                            f"{'Elevated ↑' if _pred_hv and _pred_iv > _pred_hv else 'Compressed ↓' if _pred_hv else '—'}",
                            help="Elevated IV means better premium collection environment.",
                        )

                        if _pred_spot and _sell_put_k and _sell_call_k and _pred_dte > 0:
                            _prob = probability_of_profit(
                                spot=_pred_spot,
                                sell_put_strike=_sell_put_k,
                                sell_call_strike=_sell_call_k,
                                iv=_pred_iv,
                                dte=_pred_dte,
                                hv=_pred_hv,
                            )
                            _pop  = _prob["prob_profit"]
                            _pput = _prob["prob_below_put"]
                            _pcal = _prob["prob_above_call"]
                            _sig  = _prob["sigma_used"]

                            st.markdown(f"**Spot:** {_pred_spot:,.0f}  |  "
                                        f"Sell Put: {_sell_put_k:.0f}  |  "
                                        f"Sell Call: {_sell_call_k:.0f}  |  "
                                        f"σ used: {_sig*100:.1f}%")

                            _pc1, _pc2, _pc3 = st.columns(3)
                            _pc1.metric(
                                "Probability of Profit",
                                f"{_pop*100:.1f}%",
                                help="P(spot expires between sell strikes). "
                                     "Uses blended IV/HV via Black-Scholes.",
                            )
                            _pc2.metric(
                                "P(Below Sell Put)",
                                f"{_pput*100:.1f}%",
                                delta=f"{-_pput*100:.1f}% risk",
                                delta_color="inverse",
                            )
                            _pc3.metric(
                                "P(Above Sell Call)",
                                f"{_pcal*100:.1f}%",
                                delta=f"{-_pcal*100:.1f}% risk",
                                delta_color="inverse",
                            )

                            # Probability bar
                            st.progress(
                                _pop,
                                text=f"Probability of full profit at expiry: {_pop*100:.1f}%",
                            )

                            # Expected P&L at expiry
                            if entry_credit_val:
                                try:
                                    from risk.payoff import calculate_payoff_from_credit
                                    _payoff_pred = calculate_payoff_from_credit(
                                        strategy,
                                        _Dec(str(entry_credit_val)) / (100 * strategy.lots),
                                        100,
                                    )
                                    _ev = expected_pnl_at_expiry(
                                        prob_profit=_pop,
                                        max_profit=float(_payoff_pred.max_profit),
                                        max_loss=float(_payoff_pred.max_loss),
                                        partial_exit_pct=0.5,
                                    )
                                    st.metric(
                                        "Expected Value at Expiry",
                                        fmt_inr(_ev),
                                        help="Probability-weighted expected P&L. "
                                             "Winning trades assume 50% credit captured; "
                                             "losing trades assume max loss.",
                                        delta_color="normal" if _ev >= 0 else "inverse",
                                    )
                                except Exception:
                                    pass
                        else:
                            st.info(
                                "Enter position with Zerodha connected to see probability metrics."
                            )

                        # Monthly forecast
                        st.markdown("**Month-to-Date Forecast**")
                        try:
                            from sqlalchemy import text as _text2
                            _month_start = date.today().replace(day=1).isoformat()
                            _mdf = pd.read_sql(
                                _text2(
                                    "SELECT final_pnl, entry_credit FROM strategies "
                                    "WHERE trading_mode='PAPER' "
                                    "AND state IN ('EXIT_COMPLETE','ABORTED') "
                                    "AND created_at >= :start"
                                ),
                                conn,
                                params={"start": _month_start},
                            )
                            _m_trades = len(_mdf)
                            _m_total  = float(_mdf["final_pnl"].sum()) if _m_trades else 0.0
                            _m_wins   = int((_mdf["final_pnl"] > 0).sum()) if _m_trades else 0
                            _m_avg_credit = (
                                float(_mdf["entry_credit"].mean()) if _m_trades else 0.0
                            )
                            _m_summary = {
                                "trades": _m_trades, "wins": _m_wins, "total_pnl": _m_total,
                            }
                            _capital = float(risk.capital_limit) if risk else 200000.0
                            _m_target_pct = 10.0
                            try:
                                import yaml as _yaml
                                with open("config/auto_trader.yaml") as _f:
                                    _m_target_pct = float(
                                        _yaml.safe_load(_f)
                                        .get("auto_trader", {})
                                        .get("monthly_target_pct", 10.0)
                                    )
                            except Exception:
                                pass

                            _fc = monthly_forecast(
                                _m_summary,
                                capital=_capital,
                                monthly_target_pct=_m_target_pct,
                                avg_credit_per_trade=_m_avg_credit,
                            )

                            _fc1, _fc2, _fc3, _fc4 = st.columns(4)
                            _fc1.metric("Completed Trades", _fc["trades_completed"])
                            _fc2.metric(
                                "Win Rate",
                                f"{_fc['obs_win_rate']*100:.0f}%",
                                help="Observed win rate this month (prior: 70% if < 3 trades).",
                            )
                            _fc3.metric("Month P&L", fmt_inr(_fc["current_pnl"]))
                            _fc4.metric(
                                "Target",
                                fmt_inr(_fc["target"]),
                                delta=f"{_fc['pct_of_target']:.1f}% achieved",
                                delta_color="normal" if _fc["pct_of_target"] >= 50 else "inverse",
                            )

                            _prog_val = min(1.0, max(0.0, _fc["pct_of_target"] / 100))
                            st.progress(
                                _prog_val,
                                text=(
                                    f"{_fc['pct_of_target']:.1f}% of monthly target — "
                                    f"{_fc['trading_days_remaining']} trading days remain — "
                                    f"Projected: {fmt_inr(_fc['projected_total'])}"
                                ),
                            )
                            if _fc["on_track"]:
                                st.success("On track to hit monthly target.")
                            else:
                                st.warning(
                                    f"Behind target. Shortfall: {fmt_inr(_fc['shortfall'])}. "
                                    f"~{_fc['trades_remaining']} trade(s) remaining."
                                )
                        except Exception as _fc_err:
                            st.caption(f"Monthly forecast unavailable: {_fc_err}")

                    except Exception as _pred_err:
                        st.warning(f"Prediction unavailable: {_pred_err}")

                # Positions table
                pos_df = pd.read_sql(
                    text(
                        "SELECT trading_symbol, side, quantity, average_entry_price, "
                        "updated_at FROM positions ORDER BY leg_index"
                    ),
                    conn,
                )
                if not pos_df.empty:
                    st.subheader("Open Positions")
                    for col in ["average_entry_price"]:
                        if col in pos_df.columns:
                            pos_df[col] = pos_df[col].apply(
                                lambda x: float(x) if x is not None else None
                            )
                    st.dataframe(pos_df, use_container_width=True, hide_index=True)

                # Orders table
                orders_df = pd.read_sql(
                    text(
                        "SELECT trading_symbol, side, quantity, limit_price, status, "
                        "filled_quantity, average_fill_price, reprice_count, submitted_at "
                        "FROM orders ORDER BY submitted_at DESC LIMIT 20"
                    ),
                    conn,
                )
                if not orders_df.empty:
                    st.subheader("Recent Orders")
                    for col in ["limit_price", "average_fill_price"]:
                        if col in orders_df.columns:
                            orders_df[col] = orders_df[col].apply(
                                lambda x: float(x) if x is not None else None
                            )
                    st.dataframe(
                        orders_df, use_container_width=True, hide_index=True
                    )

                # Fills table
                try:
                    fills_df = pd.read_sql(
                        text(
                            "SELECT f.fill_id, o.trading_symbol, f.quantity, f.price, "
                            "f.timestamp FROM fills f "
                            "JOIN orders o ON f.order_id = o.order_id "
                            "ORDER BY f.timestamp DESC LIMIT 20"
                        ),
                        conn,
                    )
                    if not fills_df.empty:
                        st.subheader("Recent Fills")
                        if "price" in fills_df.columns:
                            fills_df["price"] = fills_df["price"].apply(
                                lambda x: float(x) if x is not None else None
                            )
                        st.dataframe(
                            fills_df, use_container_width=True, hide_index=True
                        )
                except Exception as e:
                    st.caption(f"Fills not available: {e}")

        except Exception as e:
            st.error(f"Database error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5: EXIT
# ══════════════════════════════════════════════════════════════════════════════
with tab_exit:
    st.subheader("Paper Exit")
    st.info(
        "Paper exit simulates closing all legs. No real orders are placed."
    )

    if mode == "LIVE":
        st.warning(
            "For real exit orders, use CLI: "
            "`python3 -m cli.main live-exit --confirm --run-id <id>`"
        )

    trigger_choice = st.selectbox(
        "Exit trigger",
        ["MANUAL", "PROFIT_TARGET", "STOP_LOSS", "DTE_EXIT", "EMERGENCY"],
    )
    paper_exit_clicked = st.button(
        "\U0001f3c1 Paper Exit", type="primary", disabled=ks_active
    )

    if ks_active:
        st.error("\U0001f6d1 Kill switch is active. Cannot exit.")

    if paper_exit_clicked and strategy and risk and not ks_active:
        with st.spinner("Running paper exit simulation..."):
            try:
                from brokers.paper import PaperBroker
                from execution.exit_engine import ExitEngine
                from execution.models import (
                    ExitTrigger,
                    LegPosition,
                    Quote,
                    ResolvedInstrument,
                    Side,
                    StrategyState,
                )
                from execution.state_machine import StateMachine
                from monitoring.alerts import AlertManager
                from risk.kill_switch import KillSwitch
                from risk.payoff import calculate_cost_to_close

                lot_size = 100

                # ── Resolve quotes: live Zerodha > session cache > fallback ──
                _exit_q = st.session_state.get("last_quotes", {})
                _exit_sym = st.session_state.get("resolved_symbols", {})
                _exit_resolved = {}

                if creds:
                    from brokers.zerodha import ZerodhaBroker
                    from execution.quote_engine import QuoteEngine
                    live_broker_exit = ZerodhaBroker.from_env()
                    _instr_exit = []
                    for _ii, _ll in enumerate(strategy.legs):
                        _inst_ex = live_broker_exit.resolve_instrument(
                            strategy.underlying, strategy.exchange,
                            strategy.expiry, _ll.strike, _ll.option_type.value,
                        )
                        _exit_resolved[_ii] = _inst_ex
                        _instr_exit.append((_inst_ex.instrument_token, _inst_ex.trading_symbol))
                        _exit_sym[_ii] = _inst_ex.trading_symbol
                        lot_size = _inst_ex.lot_size
                    _qe_exit = QuoteEngine(live_broker_exit)
                    _exit_q = _qe_exit.fetch_all(_instr_exit)
                    broker_exit = PaperBroker(
                        live_quote_source=live_broker_exit,
                        fill_delay_seconds=0,
                    )
                else:
                    from unittest.mock import MagicMock
                    _fallback_exit = {
                        0: ("7.50", "8.00"),
                        1: ("12.00", "12.50"),
                        2: ("13.00", "13.50"),
                        3: ("10.00", "10.50"),
                    }
                    for _ii, _ll in enumerate(strategy.legs):
                        _sym_ex = _exit_sym.get(_ii) or (
                            f"{strategy.underlying}"
                            f"{strategy.expiry.strftime('%y%b').upper()}"
                            f"{int(_ll.strike)}{_ll.option_type.value}"
                        )
                        _exit_resolved[_ii] = ResolvedInstrument(
                            instrument_token=1000 + _ii,
                            trading_symbol=_sym_ex,
                            exchange=strategy.exchange,
                            underlying=strategy.underlying,
                            expiry=strategy.expiry,
                            strike=_ll.strike,
                            option_type=_ll.option_type,
                            lot_size=lot_size,
                            tick_size=Decimal("0.05"),
                        )

                    def _make_exit_sim(instrument_list):
                        result = {}
                        for token, sym in instrument_list:
                            cached = _exit_q.get(sym)
                            if cached:
                                result[token] = cached.model_copy(update={"instrument_token": token})
                            else:
                                idx = token - 1000
                                bid, ask = _fallback_exit.get(idx, ("5.00", "6.00"))
                                result[token] = Quote(
                                    instrument_token=token, trading_symbol=sym,
                                    exchange=strategy.exchange, timestamp=datetime.now(IST),
                                    ltp=Decimal(bid), bid=Decimal(bid), bid_qty=200,
                                    ask=Decimal(ask), ask_qty=200, volume=10000, oi=100000,
                                )
                        return result

                    _mock_exit = MagicMock()
                    _mock_exit.get_quotes.side_effect = _make_exit_sim
                    broker_exit = PaperBroker(live_quote_source=_mock_exit, fill_delay_seconds=0)

                # Build leg positions using live/cached quotes
                leg_positions = []
                for i, leg in enumerate(strategy.legs):
                    inst_ex = _exit_resolved[i]
                    sym_ex = inst_ex.trading_symbol if hasattr(inst_ex, "trading_symbol") else ""
                    q_ex = _exit_q.get(sym_ex)
                    if q_ex:
                        bid_ex = q_ex.bid
                        ask_ex = q_ex.ask
                    else:
                        fb_b, fb_a = {0: ("7.50","8.00"),1:("12.00","12.50"),2:("13.00","13.50"),3:("10.00","10.50")}.get(i, ("5.00","6.00"))
                        bid_ex, ask_ex = Decimal(fb_b), Decimal(fb_a)
                    leg_positions.append(
                        LegPosition(
                            leg_index=i,
                            trading_symbol=sym_ex,
                            instrument_token=inst_ex.instrument_token if hasattr(inst_ex, "instrument_token") else 1000 + i,
                            side=leg.side,
                            option_type=leg.option_type,
                            strike=leg.strike,
                            lot_size=lot_size,
                            lots=strategy.lots,
                            quantity=lot_size * strategy.lots,
                            average_entry_price=bid_ex,
                            current_bid=bid_ex,
                            current_ask=ask_ex,
                        )
                    )

                # Show cost to close before exit
                try:
                    ctc = calculate_cost_to_close(leg_positions)
                    st.metric("Current Cost to Close", fmt_inr(ctc))
                except Exception as e:
                    st.caption(f"Cost to close: {e}")

                ks2 = KillSwitch()
                sm = StateMachine(initial=StrategyState.MONITORING)
                alerts = AlertManager()
                run_id = st.session_state.last_run_id or str(uuid.uuid4())

                exit_engine = ExitEngine(
                    strategy=strategy,
                    positions=leg_positions,
                    broker=broker_exit,
                    risk=risk,
                    kill_switch=ks2,
                    alert_manager=alerts,
                    state_machine=sm,
                    exit_trigger=ExitTrigger[trigger_choice],
                    strategy_run_id=run_id,
                )
                tracker = exit_engine.run()
                summary = tracker.summary()

                if tracker.all_filled:
                    st.success(
                        f"\u2705 Exit complete. All {summary['filled_legs']} legs closed."
                    )
                else:
                    st.warning(
                        f"\u26a0\ufe0f Exit incomplete. "
                        f"Filled: {summary['filled_legs']}/{summary['total_legs']}. "
                        f"Unfilled: {summary['unfilled_legs']}"
                    )
                st.write(f"Final state: **{sm.state.value}**")

            except Exception as e:
                st.error(f"Paper exit error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 6: HISTORY
# ══════════════════════════════════════════════════════════════════════════════
with tab_history:
    st.subheader("Trade History")
    db_path = get_db_path()
    if not Path(db_path).exists():
        st.info("No database found yet.")
    else:
        try:
            import pandas as pd
            from sqlalchemy import text
            from storage.database import create_db_engine

            engine = create_db_engine(db_path)

            with engine.connect() as conn:
                all_strats = pd.read_sql(
                    text(
                        "SELECT id, name, underlying, expiry, lots, state, trading_mode, "
                        "entry_credit, final_pnl, created_at, updated_at "
                        "FROM strategies ORDER BY created_at DESC"
                    ),
                    conn,
                )
                if all_strats.empty:
                    st.info("No trade history yet.")
                else:
                    # Convert Decimal columns to float
                    for col in ["entry_credit", "final_pnl"]:
                        if col in all_strats.columns:
                            all_strats[col] = all_strats[col].apply(
                                lambda x: float(x) if x is not None else None
                            )
                    st.dataframe(
                        all_strats, use_container_width=True, hide_index=True
                    )

                    selected_id = st.selectbox(
                        "Select strategy to inspect",
                        all_strats["id"].tolist(),
                        format_func=lambda x: str(x)[:20] + "...",
                    )
                    if selected_id:
                        col_h1, col_h2 = st.columns(2)
                        with col_h1:
                            st.write("**Orders**")
                            try:
                                ord_df = pd.read_sql(
                                    text(
                                        "SELECT trading_symbol, side, quantity, limit_price, "
                                        "status, filled_quantity, average_fill_price "
                                        "FROM orders WHERE strategy_id=:sid "
                                        "ORDER BY submitted_at"
                                    ),
                                    conn,
                                    params={"sid": selected_id},
                                )
                                for col in ["limit_price", "average_fill_price"]:
                                    if col in ord_df.columns:
                                        ord_df[col] = ord_df[col].apply(
                                            lambda x: float(x)
                                            if x is not None
                                            else None
                                        )
                                st.dataframe(
                                    ord_df, use_container_width=True, hide_index=True
                                )
                            except Exception as e:
                                st.error(f"Orders load error: {e}")

                        with col_h2:
                            st.write("**System Events**")
                            try:
                                evt_df = pd.read_sql(
                                    text(
                                        "SELECT event_time, event_type, details "
                                        "FROM system_events WHERE strategy_id=:sid "
                                        "ORDER BY event_time"
                                    ),
                                    conn,
                                    params={"sid": selected_id},
                                )
                                st.dataframe(
                                    evt_df, use_container_width=True, hide_index=True
                                )
                            except Exception as e:
                                st.error(f"Events load error: {e}")

                    # P&L chart if enough data
                    pnl_data = all_strats[["created_at", "final_pnl"]].dropna(
                        subset=["final_pnl"]
                    )
                    if len(pnl_data) >= 2:
                        st.subheader("P&L History")
                        try:
                            pnl_chart = pnl_data.set_index("created_at")[
                                ["final_pnl"]
                            ]
                            st.line_chart(pnl_chart)
                        except Exception as e:
                            st.caption(f"P&L chart error: {e}")

        except Exception as e:
            st.error(f"History error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 7: ALERTS
# ══════════════════════════════════════════════════════════════════════════════
with tab_alerts:
    st.subheader("Alert Log")

    level_colors = {
        "INFO": "\U0001f535",
        "WARNING": "\U0001f7e1",
        "CRITICAL": "\U0001f7e0",
        "EMERGENCY": "\U0001f534",
    }

    col_al1, col_al2 = st.columns([3, 1])
    with col_al2:
        if st.button("\U0001f504 Refresh Alerts"):
            st.rerun()

    db_path = get_db_path()
    if Path(db_path).exists():
        try:
            import pandas as pd
            from sqlalchemy import text
            from storage.database import create_db_engine

            engine = create_db_engine(db_path)
            with engine.connect() as conn:
                alerts_df = pd.read_sql(
                    text(
                        "SELECT timestamp, level, event, message, strategy_id "
                        "FROM alerts ORDER BY timestamp DESC LIMIT 100"
                    ),
                    conn,
                )
                if alerts_df.empty:
                    st.info("No alerts recorded yet.")
                else:
                    # Level filter
                    available_levels = sorted(alerts_df["level"].unique().tolist())
                    selected_levels = st.multiselect(
                        "Filter by level",
                        available_levels,
                        default=available_levels,
                    )
                    filtered_df = alerts_df[
                        alerts_df["level"].isin(selected_levels)
                    ]

                    for _, row in filtered_df.iterrows():
                        icon = level_colors.get(str(row["level"]), "\u26aa")
                        ts = str(row["timestamp"])[:19]
                        st.write(
                            f"{icon} `{ts}` **{row['event']}** \u2014 {row['message']}"
                        )

        except Exception as e:
            st.error(f"Alert history error: {e}")
    else:
        st.info("No database found yet. Run a paper entry first to generate alerts.")

    # In-session alerts from session_state
    if st.session_state.alert_log:
        st.subheader("Session Alerts")
        for alert in reversed(st.session_state.alert_log):
            icon = level_colors.get(alert.get("level", "INFO"), "\u26aa")
            st.write(f"{icon} {alert.get('message', '')}")

    # Kill switch panel (also in sidebar, duplicated here for convenience)
    st.divider()
    st.subheader("Kill Switch Control")
    if ks_active:
        st.error(
            "\U0001f6d1 **Kill switch is ACTIVE.** All operations are halted."
        )
        if ks is not None:
            if st.button(
                "\u2705 Deactivate Kill Switch (Alerts Tab)", key="deactivate_alerts"
            ):
                try:
                    ks.deactivate()
                    st.success("Kill switch deactivated.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not deactivate: {e}")
    else:
        st.success("\U0001f7e2 Kill switch is inactive. System operating normally.")
        if ks is not None:
            if st.button(
                "\U0001f6d1 Activate Kill Switch (Alerts Tab)",
                type="secondary",
                key="activate_alerts",
            ):
                try:
                    ks.activate("Manual activation from dashboard alerts tab")
                    st.warning("Kill switch ACTIVATED.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not activate: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB: AUTO TRADER
# ══════════════════════════════════════════════════════════════════════════════
with tab_auto:
    st.subheader("🤖 Auto Trader")
    st.caption(
        "The Auto Trader runs fully unattended. It scans NIFTY / BANKNIFTY / FINNIFTY, "
        "auto-selects Iron Condor strikes, enters, monitors, and exits automatically."
    )

    # ── Config display ─────────────────────────────────────────────────────
    try:
        import yaml as _at_yaml
        with open("config/auto_trader.yaml") as _at_f:
            _at_raw = _at_yaml.safe_load(_at_f)
        _at = _at_raw.get("auto_trader", {})
        _at_enabled = _at.get("enabled", False)
        _at_entry   = _at.get("entry", {})
        _at_exit    = _at.get("exit", {})

        _st_col1, _st_col2, _st_col3, _st_col4 = st.columns(4)
        _st_col1.metric("Status",        "🟢 Enabled" if _at_enabled else "🔴 Disabled")
        _st_col2.metric("Entry Time",    _at_entry.get("time", "09:30") + " IST")
        _st_col3.metric("Profit Exit",   f"{_at_exit.get('profit_target_pct', 50)}% of credit")
        _st_col4.metric("Stop Loss",     f"{_at_exit.get('stop_loss_mult', 2)}× credit")

        _at_col1, _at_col2, _at_col3 = st.columns(3)
        _at_col1.metric("Underlyings",   ", ".join(_at.get("underlyings", [])))
        _at_col2.metric("DTE Range",     f"{_at.get('expiry',{}).get('min_dte',15)}–{_at.get('expiry',{}).get('max_dte',45)} days")
        _at_col3.metric("Monthly Target",f"{_at.get('monthly_target_pct', 10)}%")

        st.divider()

        # Quick toggles
        _tog_col1, _tog_col2 = st.columns(2)
        if _tog_col1.button(
            "✅ Enable Auto Trader" if not _at_enabled else "⏸️ Disable Auto Trader",
            type="primary" if not _at_enabled else "secondary",
        ):
            _at_raw["auto_trader"]["enabled"] = not _at_enabled
            with open("config/auto_trader.yaml", "w") as _at_wf:
                _at_yaml.dump(_at_raw, _at_wf, default_flow_style=False, sort_keys=False)
            st.rerun()

        if _tog_col2.button("⚙️ Edit Auto Trader Config"):
            st.session_state["show_at_config"] = not st.session_state.get("show_at_config", False)

        if st.session_state.get("show_at_config", False):
            with st.form("at_config_form"):
                _f_entry_time   = st.text_input("Entry Time (IST HH:MM)", value=_at_entry.get("time", "09:30"))
                _f_underlyings  = st.multiselect(
                    "Underlyings to scan",
                    ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"],
                    default=_at.get("underlyings", ["NIFTY", "BANKNIFTY", "FINNIFTY"]),
                )
                _f_iv           = st.slider("IV Assumption (%)", 10, 80, int(_at_entry.get("iv_assumption", 0.30)*100))
                _f_profit_tgt   = st.slider("Profit Exit (% of credit)", 20, 100, int(_at_exit.get("profit_target_pct", 50)))
                _f_sl_mult      = st.slider("Stop Loss (× credit)", 1.0, 5.0, float(_at_exit.get("stop_loss_mult", 2.0)), 0.5)
                _f_dte_exit     = st.number_input("Force Exit DTE", 0, 10, int(_at_exit.get("dte_exit", 2)))
                _f_interval     = st.number_input("Monitor Interval (seconds)", 60, 3600, int(_at_exit.get("monitor_interval_seconds", 900)), 60)
                _f_monthly_tgt  = st.slider("Monthly Return Target (%)", 2, 30, int(_at.get("monthly_target_pct", 10)))
                _f_min_dte      = st.number_input("Min DTE at Entry", 5, 30, int(_at.get("expiry",{}).get("min_dte",15)))
                _f_max_dte      = st.number_input("Max DTE at Entry", 20, 90, int(_at.get("expiry",{}).get("max_dte",45)))

                if st.form_submit_button("💾 Save Config", type="primary"):
                    _at_raw["auto_trader"]["underlyings"]  = _f_underlyings
                    _at_raw["auto_trader"]["monthly_target_pct"] = _f_monthly_tgt
                    _at_raw["auto_trader"]["entry"]["time"]           = _f_entry_time
                    _at_raw["auto_trader"]["entry"]["iv_assumption"]  = _f_iv / 100
                    _at_raw["auto_trader"]["exit"]["profit_target_pct"] = _f_profit_tgt
                    _at_raw["auto_trader"]["exit"]["stop_loss_mult"]    = _f_sl_mult
                    _at_raw["auto_trader"]["exit"]["dte_exit"]          = int(_f_dte_exit)
                    _at_raw["auto_trader"]["exit"]["monitor_interval_seconds"] = int(_f_interval)
                    _at_raw["auto_trader"]["expiry"]["min_dte"] = int(_f_min_dte)
                    _at_raw["auto_trader"]["expiry"]["max_dte"] = int(_f_max_dte)
                    with open("config/auto_trader.yaml", "w") as _at_wf2:
                        _at_yaml.dump(_at_raw, _at_wf2, default_flow_style=False, sort_keys=False)
                    st.success("Saved!")
                    st.rerun()

    except Exception as _at_cfg_err:
        st.error(f"Auto trader config error: {_at_cfg_err}")

    st.divider()

    # ── Process status + Start/Stop ────────────────────────────────────────
    import subprocess as _sp
    def _at_pid() -> int | None:
        """Return PID of running run_auto_trader.py, or None."""
        try:
            r = _sp.run(["pgrep", "-f", "run_auto_trader.py"], capture_output=True, text=True)
            pids = [int(p) for p in r.stdout.strip().split() if p.isdigit()]
            return pids[0] if pids else None
        except Exception:
            return None

    _pid = _at_pid()
    _is_running = _pid is not None

    _proc_col1, _proc_col2 = st.columns([3, 1])
    if _is_running:
        _proc_col1.success(f"🟢 Auto Trader is RUNNING (PID {_pid})")
    else:
        _proc_col1.warning("🔴 Auto Trader is NOT running")

    if _is_running:
        if _proc_col2.button("⏹ Stop", type="secondary", key="at_stop_btn"):
            try:
                _sp.run(["kill", str(_pid)], check=True)
                st.success("Auto Trader stopped.")
                st.rerun()
            except Exception as _ke:
                st.error(f"Could not stop: {_ke}")
    else:
        if _proc_col2.button("▶ Start", type="primary", key="at_start_btn"):
            if not _at_enabled:
                st.error("Enable the Auto Trader first (click ✅ Enable above).")
            else:
                try:
                    log_path = str(Path("logs/auto_trader_live.log").resolve())
                    _sp.Popen(
                        ["venv/bin/python3", "run_auto_trader.py"],
                        stdout=open(log_path, "a"),
                        stderr=_sp.STDOUT,
                        start_new_session=True,
                    )
                    import time as _t; _t.sleep(1)
                    st.success("Auto Trader started! Check logs/auto_trader_live.log for output.")
                    st.rerun()
                except Exception as _se:
                    st.error(f"Failed to start: {_se}")

    st.caption(
        "You can also start it manually from the terminal:  \n"
        "`source venv/bin/activate && python run_auto_trader.py`"
    )

    # ── Live log tail ──────────────────────────────────────────────────────
    _log_path = Path("logs/auto_trader_live.log")
    if _log_path.exists():
        with st.expander("📋 Recent log (last 20 lines)", expanded=_is_running):
            try:
                _log_lines = _log_path.read_text().splitlines()[-20:]
                _log_clean = []
                for _ll in _log_lines:
                    _ll = _ll.strip()
                    if not _ll:
                        continue
                    if _ll.startswith("{"):
                        try:
                            import json as _json
                            _d = _json.loads(_ll)
                            _log_clean.append(
                                f"[{_d.get('time','')[:19]}] {_d.get('level',''):8s} {_d.get('msg','')}"
                            )
                        except Exception:
                            _log_clean.append(_ll)
                    else:
                        _log_clean.append(_ll)
                st.code("\n".join(_log_clean), language=None)
            except Exception as _le:
                st.error(f"Log read error: {_le}")

    # ── Monthly P&L vs target ───────────────────────────────────────────────
    st.divider()
    st.subheader("📊 Monthly Performance")

    _db_path_at = get_db_path()
    if not Path(_db_path_at).exists():
        st.info("No trades yet. Run the auto trader and check back.")
    else:
        try:
            import pandas as pd
            from sqlalchemy import text
            from storage.database import create_db_engine
            from datetime import date as _date_at

            _at_engine = create_db_engine(_db_path_at)
            _first_of_month = _date_at.today().replace(day=1).isoformat()

            with _at_engine.connect() as _at_conn:
                _at_df = pd.read_sql(
                    text(
                        "SELECT underlying, expiry, entry_credit, final_pnl, "
                        "state, created_at FROM strategies "
                        "WHERE trading_mode='PAPER' AND created_at >= :start "
                        "ORDER BY created_at DESC"
                    ),
                    _at_conn, params={"start": _first_of_month},
                )

            if _at_df.empty:
                st.info("No trades this month yet.")
            else:
                for _col in ["entry_credit", "final_pnl"]:
                    if _col in _at_df.columns:
                        _at_df[_col] = _at_df[_col].apply(lambda x: float(x) if x is not None else None)

                _completed = _at_df[_at_df["final_pnl"].notna()]
                _total_pnl = _completed["final_pnl"].sum() if not _completed.empty else 0.0
                _trades    = len(_completed)
                _wins      = int((_completed["final_pnl"] > 0).sum()) if _trades else 0
                _capital   = float(risk.capital_limit) if risk else 200000.0
                _ret_pct   = _total_pnl / _capital * 100 if _capital else 0

                try:
                    _monthly_target_pct = float(_at.get("monthly_target_pct", 10))
                except Exception:
                    _monthly_target_pct = 10.0
                _target_inr = _capital * _monthly_target_pct / 100

                _pm1, _pm2, _pm3, _pm4, _pm5 = st.columns(5)
                _pm1.metric("Trades (this month)", _trades)
                _pm2.metric("Win Rate", f"{_wins/_trades*100:.0f}%" if _trades else "—")
                _pm3.metric(
                    "Total P&L",
                    fmt_inr(_total_pnl),
                    delta=f"{_ret_pct:+.1f}% return",
                    delta_color="normal" if _total_pnl >= 0 else "inverse",
                )
                _pm4.metric("Target", fmt_inr(_target_inr))
                _pm5.metric(
                    "Progress",
                    f"{min(_ret_pct / _monthly_target_pct * 100, 100):.0f}%" if _monthly_target_pct else "—",
                )

                st.progress(
                    min(max(_ret_pct / _monthly_target_pct, 0.0), 1.0) if _monthly_target_pct else 0.0,
                    text=f"Monthly target: {fmt_inr(_total_pnl)} / {fmt_inr(_target_inr)} ({_ret_pct:.1f}% / {_monthly_target_pct:.0f}%)",
                )

                st.write("**This Month's Trades**")
                st.dataframe(_at_df, use_container_width=True, hide_index=True)

        except Exception as _at_db_err:
            st.error(f"Monthly P&L error: {_at_db_err}")

    # ── Open positions live MTM ─────────────────────────────────────────────
    st.divider()
    st.subheader("📡 Open Positions — Live MTM")
    if st.button("🔄 Refresh MTM"):
        st.rerun()

    if Path(_db_path_at).exists():
        try:
            import pandas as pd
            from sqlalchemy import text
            from storage.database import create_db_engine
            from execution.models import LegPosition, OptionType, Side
            from risk.payoff import calculate_cost_to_close, calculate_mtm_pnl

            _mtm_engine = create_db_engine(_db_path_at)
            with _mtm_engine.connect() as _mc:
                _open_strats = pd.read_sql(
                    text(
                        "SELECT s.id, s.underlying, s.expiry, s.entry_credit, "
                        "p.leg_index, p.trading_symbol, p.instrument_token, "
                        "p.side, p.quantity, p.average_entry_price "
                        "FROM strategies s JOIN positions p ON p.strategy_id=s.id "
                        "WHERE s.state IN ('ENTRY_COMPLETE','MONITORING') "
                        "AND s.trading_mode='PAPER' ORDER BY s.id, p.leg_index"
                    ),
                    _mc,
                )
            if _open_strats.empty:
                st.info("No open positions.")
            else:
                for _sid, _grp in _open_strats.groupby("id"):
                    st.write(f"**{_grp.iloc[0]['underlying']}** — expiry {_grp.iloc[0]['expiry']}")
                    _instr_mtm = [
                        (int(r["instrument_token"]), str(r["trading_symbol"]))
                        for _, r in _grp.iterrows()
                    ]
                    _mtm_pnl_disp = None
                    if creds:
                        try:
                            from brokers.zerodha import ZerodhaBroker
                            from execution.quote_engine import QuoteEngine
                            _zb_mtm = ZerodhaBroker.from_env()
                            _q_mtm  = QuoteEngine(_zb_mtm).fetch_all(_instr_mtm)
                            _lps_mtm = []
                            for _, _row in _grp.iterrows():
                                _sym_m = str(_row["trading_symbol"])
                                _qm    = _q_mtm.get(_sym_m)
                                if _qm:
                                    _opt_m = OptionType.CE if _sym_m.endswith("CE") else OptionType.PE
                                    _lps_mtm.append(LegPosition(
                                        leg_index=int(_row["leg_index"]),
                                        trading_symbol=_sym_m,
                                        instrument_token=int(_row["instrument_token"]),
                                        side=Side(str(_row["side"])),
                                        option_type=_opt_m,
                                        strike=Decimal("0"),
                                        lot_size=1, lots=1,
                                        quantity=int(_row["quantity"]),
                                        average_entry_price=Decimal(str(_row["average_entry_price"])),
                                        current_bid=_qm.bid,
                                        current_ask=_qm.ask,
                                        current_ltp=_qm.ltp,
                                    ))
                            if _lps_mtm:
                                _ctc   = calculate_cost_to_close(_lps_mtm)
                                _ec    = Decimal(str(float(_grp.iloc[0]["entry_credit"] or 0)))
                                _mtm_pnl_disp = calculate_mtm_pnl(_ec, _ctc)
                        except Exception:
                            pass

                    _mc1, _mc2, _mc3 = st.columns(3)
                    _ec_disp = float(_grp.iloc[0]["entry_credit"] or 0)
                    _mc1.metric("Entry Credit", fmt_inr(_ec_disp))
                    if _mtm_pnl_disp is not None:
                        _pnl_f = float(_mtm_pnl_disp)
                        _mc2.metric(
                            "MTM P&L", fmt_inr(_pnl_f),
                            delta_color="normal" if _pnl_f >= 0 else "inverse",
                        )
                        _pct = _pnl_f / _ec_disp * 100 if _ec_disp else 0
                        _mc3.metric("% Credit Captured", f"{_pct:.1f}%")
                        _at_profit_tgt = float(_at_exit.get("profit_target_pct", 50))
                        _at_sl_mult    = float(_at_exit.get("stop_loss_mult", 2.0))
                        st.progress(
                            min(max(_pct / _at_profit_tgt, 0.0), 1.0),
                            text=f"{_pct:.1f}% captured → exit at {_at_profit_tgt:.0f}%",
                        )
                        if _pnl_f <= -_ec_disp * _at_sl_mult:
                            st.error("🚨 Stop loss threshold reached — auto trader will exit next cycle")
                    else:
                        _mc2.metric("MTM P&L", "Connect Zerodha")

        except Exception as _open_err:
            st.error(f"Open positions error: {_open_err}")

# ══════════════════════════════════════════════════════════════════════════════
# TAB: INTELLIGENCE — IV Rank, VIX, Timing, Opportunity Scanner, Signals
# ══════════════════════════════════════════════════════════════════════════════
with tab_intel:
    import json as _ij
    import math as _imath
    import pandas as _ipd
    from datetime import datetime as _idt, date as _idate
    from zoneinfo import ZoneInfo as _iZI

    _IST = _iZI("Asia/Kolkata")

    st.subheader("🧠 Market Intelligence Center")
    st.caption(
        "Real-time IV rank, VIX conditions, entry timing quality, and composite opportunity scores. "
        "Refreshes every minute when Auto Trader is running."
    )

    if st.button("🔄 Refresh Intelligence", key="intel_refresh"):
        st.rerun()

    # ── Section 1: Entry Timing Timeline ──────────────────────────────────────
    st.markdown("### ⏱️ Intraday Entry Timing Window")

    try:
        from monitoring.entry_timing import score_entry_time, TIMING_WINDOWS
        _timing = score_entry_time()

        _t_cols = st.columns([2, 1, 1, 1])
        _t_cols[0].metric("Current Window", _timing.window_label)
        _score_color = (
            "🟢" if _timing.score >= 70 else
            "🟡" if _timing.score >= 40 else "🔴"
        )
        _t_cols[1].metric("Timing Score", f"{_score_color} {_timing.score:.0f}/100")
        _t_cols[2].metric("Recommendation", _timing.recommendation.replace("_", " "))
        if _timing.minutes_to_optimal is not None and _timing.minutes_to_optimal > 0:
            _t_cols[3].metric("Next Good Window", f"in {_timing.minutes_to_optimal} min → {_timing.next_window}")
        else:
            _t_cols[3].metric("Next Good Window", "—")

        # Timeline bar chart
        _tl_df = _ipd.DataFrame(_timing.windows)
        _tl_df["width_min"] = _tl_df.apply(
            lambda r: (
                int(r["end"][:2]) * 60 + int(r["end"][3:]) -
                int(r["start"][:2]) * 60 - int(r["start"][3:])
            ), axis=1
        )
        _color_map = {"green": "#2ecc71", "yellow": "#f1c40f", "orange": "#e67e22", "red": "#e74c3c"}

        import altair as _alt
        _tl_chart_df = _ipd.DataFrame([
            {
                "window": r["label"],
                "start":  r["start"],
                "end":    r["end"],
                "score":  r["score"],
                "color":  _color_map.get(r["color"], "#aaaaaa"),
                "active": "▶ NOW" if r["active"] else "",
            }
            for r in _timing.windows
        ])
        _tl_bar = (
            _alt.Chart(_tl_chart_df)
            .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
            .encode(
                x=_alt.X("start:O", title="Time (IST)", sort=list(_tl_chart_df["start"])),
                y=_alt.Y("score:Q", title="Quality Score", scale=_alt.Scale(domain=[0, 110])),
                color=_alt.Color("color:N", scale=None, legend=None),
                tooltip=["window", "start", "end", "score", "active"],
            )
            .properties(height=200, title="Entry Window Quality — Green=Best, Yellow=OK, Orange=Poor, Red=Avoid")
        )
        _tl_text = (
            _alt.Chart(_tl_chart_df[_tl_chart_df["active"] == "▶ NOW"])
            .mark_text(dy=-10, fontSize=14, color="white")
            .encode(
                x=_alt.X("start:O", sort=list(_tl_chart_df["start"])),
                y=_alt.Y("score:Q"),
                text=_alt.Text("active:N"),
            )
        )
        st.altair_chart((_tl_bar + _tl_text).interactive(), use_container_width=True)

    except Exception as _te:
        st.warning(f"Timing engine unavailable: {_te}")

    st.divider()

    # ── Section 2: VIX Monitor ────────────────────────────────────────────────
    st.markdown("### 📊 India VIX Monitor")

    _vix_col1, _vix_col2 = st.columns([1, 2])
    _vix_val = None

    with _vix_col1:
        if creds:
            try:
                from brokers.zerodha import ZerodhaBroker as _ZVIX
                from monitoring.market_filter import fetch_india_vix, score_vix
                _zb_vix = _ZVIX.from_env()
                _vix_val = fetch_india_vix(_zb_vix)
                if _vix_val:
                    _vix_scored = score_vix(_vix_val)
                    _vix_icon = {
                        "IDEAL": "🟢", "LOW": "🔵", "ELEVATED": "🟡",
                        "HIGH": "🔴", "EXTREME": "🚨"
                    }.get(_vix_scored.label, "⚪")
                    st.metric("India VIX", f"{_vix_val:.2f}",
                              delta=f"{_vix_icon} {_vix_scored.label}")
                    st.metric("VIX Score", f"{_vix_scored.score:.0f}/100")
                    if not _vix_scored.tradeable:
                        st.error(f"⚠️ VIX {_vix_scored.label} — Iron Condor entry BLOCKED")
                    else:
                        st.success("✅ VIX in acceptable range for IC entry")
                else:
                    st.info("VIX fetch failed — check Zerodha connection")
            except Exception as _vix_err:
                st.warning(f"VIX unavailable: {_vix_err}")
        else:
            st.info("Connect Zerodha to fetch live VIX")

        # VIX range guide
        st.markdown("""
        **VIX Guide for Iron Condors:**
        | Range | Label | Action |
        |-------|-------|--------|
        | < 11 | Too Low | Skip — thin premium |
        | 11–14 | Low | Caution |
        | 14–20 | **Ideal** | **Enter** |
        | 20–25 | Elevated | Caution |
        | > 25 | High | Skip — tail risk |
        """)

    with _vix_col2:
        # VIX zones as reference chart
        _vix_zones = _ipd.DataFrame([
            {"zone": "Skip (too low)",  "low": 0,  "high": 11, "color": "#95a5a6"},
            {"zone": "Caution",          "low": 11, "high": 14, "color": "#f39c12"},
            {"zone": "Ideal ✓",          "low": 14, "high": 20, "color": "#2ecc71"},
            {"zone": "Elevated",         "low": 20, "high": 25, "color": "#e67e22"},
            {"zone": "Skip (too high)",  "low": 25, "high": 40, "color": "#e74c3c"},
        ])
        _vbar = (
            _alt.Chart(_vix_zones)
            .mark_bar(opacity=0.7)
            .encode(
                x=_alt.X("low:Q", title="VIX Level"),
                x2="high:Q",
                y=_alt.Y("zone:N", title=""),
                color=_alt.Color("color:N", scale=None, legend=None),
                tooltip=["zone", "low", "high"],
            )
            .properties(height=180, title="VIX Entry Quality Zones")
        )
        if _vix_val:
            _vline = (
                _alt.Chart(_ipd.DataFrame([{"vix": _vix_val}]))
                .mark_rule(color="white", strokeWidth=2, strokeDash=[4, 2])
                .encode(x="vix:Q", tooltip=_alt.Tooltip("vix:Q", title="Current VIX"))
            )
            st.altair_chart((_vbar + _vline).interactive(), use_container_width=True)
        else:
            st.altair_chart(_vbar.interactive(), use_container_width=True)

    st.divider()

    # ── Section 3: IV Rank Scanner ────────────────────────────────────────────
    st.markdown("### 📈 IV Rank Scanner")
    st.caption(
        "IV Rank 0–100: how rich current IV is vs 52-week range. "
        "Iron Condors work best when IV rank > 50 (selling expensive premium)."
    )

    _ivr_col1, _ivr_col2 = st.columns([3, 2])

    with _ivr_col1:
        _db_path_ivr = get_db_path()
        try:
            import yaml as _ivy
            with open("config/auto_trader.yaml") as _ivf:
                _ivcfg = _ivy.safe_load(_ivf).get("auto_trader", {})
            _iv_underlyings = _ivcfg.get("underlyings", ["NIFTY", "BANKNIFTY", "FINNIFTY"])
            _iv_assumption  = float(_ivcfg.get("entry", {}).get("iv_assumption", 0.30))
            _ivr_min        = float(_ivcfg.get("filters", {}).get("iv_rank", {}).get("min_rank", 30))

            from monitoring.iv_rank import get_iv_history, compute_iv_rank

            _ivr_rows = []
            for _ul in _iv_underlyings:
                _hist = get_iv_history(_db_path_ivr, _ul, 252)
                _curr_iv = _hist[-1] if _hist else _iv_assumption
                _ivr = compute_iv_rank(_ul, _curr_iv, _db_path_ivr, _ivr_min)
                _ivr_rows.append({
                    "Underlying":  _ul,
                    "Current IV %": f"{_ivr.current_iv*100:.1f}%",
                    "IV Rank":      _ivr.iv_rank,
                    "IV %ile":      _ivr.iv_percentile,
                    "52w High %":   f"{_ivr.iv_52w_high*100:.1f}%",
                    "52w Low %":    f"{_ivr.iv_52w_low*100:.1f}%",
                    "Data Days":    _ivr.history_days,
                    "Confidence":   _ivr.confidence,
                    "Signal":       _ivr.recommendation,
                })

            _ivr_df = _ipd.DataFrame(_ivr_rows)

            def _iv_row_color(row):
                sig = row["Signal"]
                if sig == "ENTER":
                    return ["background-color: #1a3a1a"] * len(row)
                elif sig == "CAUTION":
                    return ["background-color: #3a2e00"] * len(row)
                else:
                    return ["background-color: #3a0d0d"] * len(row)

            st.dataframe(
                _ivr_df.style.apply(_iv_row_color, axis=1),
                use_container_width=True, hide_index=True,
            )

            # IV Rank bar chart
            _ivr_chart_df = _ipd.DataFrame([
                {"Underlying": r["Underlying"], "IV Rank": r["IV Rank"],
                 "Signal": r["Signal"]} for r in _ivr_rows
            ])
            _ivr_color = _alt.condition(
                _alt.datum["IV Rank"] >= _ivr_min,
                _alt.value("#2ecc71"),
                _alt.value("#e74c3c"),
            )
            _ivr_bar = (
                _alt.Chart(_ivr_chart_df)
                .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                .encode(
                    x=_alt.X("Underlying:N"),
                    y=_alt.Y("IV Rank:Q", scale=_alt.Scale(domain=[0, 100])),
                    color=_ivr_color,
                    tooltip=["Underlying", "IV Rank", "Signal"],
                )
                .properties(height=220, title=f"IV Rank by Underlying (threshold: {_ivr_min:.0f})")
            )
            _threshold_line = (
                _alt.Chart(_ipd.DataFrame([{"y": _ivr_min}]))
                .mark_rule(color="white", strokeDash=[6, 3], strokeWidth=1.5)
                .encode(y="y:Q")
            )
            st.altair_chart((_ivr_bar + _threshold_line).interactive(), use_container_width=True)

        except Exception as _ivr_err:
            st.warning(f"IV Rank scanner error: {_ivr_err}")

    with _ivr_col2:
        st.markdown("**IV Rank Interpretation**")
        st.markdown("""
        | IV Rank | Signal | What it means |
        |---------|--------|---------------|
        | 0–30 | ❌ Skip | IV too low; selling premium is cheap |
        | 30–50 | ⚠️ Caution | Acceptable; expect lower credit |
        | 50–75 | ✅ Enter | Good — above-average IV |
        | 75–100 | 🔥 Strong | Excellent — IV near yearly highs |
        """)
        st.info(
            "IV Rank builds up over time. In the first 30 days, confidence "
            "is LOW and the rank is indicative only. After 252 trading days "
            "it becomes fully reliable."
        )

    st.divider()

    # ── Section 4: Opportunity Scanner ───────────────────────────────────────
    st.markdown("### 🎯 Live Opportunity Scanner")
    st.caption("Composite score = 35% IV Rank + 25% Market + 20% Timing + 20% Credit")

    try:
        from monitoring.notifier import read_recent_signals
        _recent_sigs = read_recent_signals(100)

        # Filter to ENTRY_OPPORTUNITY signals from today
        _today_str = _idate.today().isoformat()
        _today_sigs = [
            s for s in _recent_sigs
            if s.get("signal_type") == "ENTRY_OPPORTUNITY"
            and s.get("signal_time", "")[:10] == _today_str
        ]

        if not _today_sigs:
            st.info("No signals yet today. Auto Trader generates signals at each entry scan.")
        else:
            # Latest signal per underlying
            _seen_ul: dict = {}
            for s in _today_sigs:
                ul = s.get("underlying", "")
                if ul not in _seen_ul:
                    _seen_ul[ul] = s

            _opp_rows = []
            for ul, s in sorted(_seen_ul.items(), key=lambda x: x[1].get("composite_score", 0), reverse=True):
                _opp_rows.append({
                    "Grade":        s.get("grade", "?"),
                    "Underlying":   ul,
                    "Score":        s.get("composite_score", 0),
                    "Action":       s.get("action", "?"),
                    "IV Rank":      s.get("iv_rank", 0),
                    "VIX":          f"{s.get('vix', 0):.1f}" if s.get("vix") else "—",
                    "Timing":       f"{s.get('timing_score', 0):.0f}",
                    "Window":       s.get("timing_window", "?"),
                    "Credit":       f"{s.get('credit', 0):.1f} pts" if s.get("credit") else "—",
                    "RoR%":         f"{s.get('ror', 0):.1f}%" if s.get("ror") else "—",
                    "Reason":       s.get("reason", ""),
                    "Time":         s.get("signal_time", "")[:16],
                })

            _opp_df = _ipd.DataFrame(_opp_rows)

            def _opp_row_color(row):
                g = row["Grade"]
                colors = {"A": "#0d2e0d", "B": "#1a2e00", "C": "#2e2200", "D": "#2e0d0d", "F": "#1a0000"}
                bg = colors.get(g, "#1a1a1a")
                return [f"background-color: {bg}"] * len(row)

            st.dataframe(
                _opp_df.style.apply(_opp_row_color, axis=1),
                use_container_width=True, hide_index=True,
            )

            # Composite score bar chart
            _opp_chart_df = _ipd.DataFrame([
                {
                    "Underlying": r["Underlying"],
                    "Score": r["Score"],
                    "Grade": r["Grade"],
                }
                for r in _opp_rows
            ])
            _grade_colors = {"A": "#2ecc71", "B": "#27ae60", "C": "#f39c12", "D": "#e67e22", "F": "#e74c3c"}
            _opp_chart_df["color"] = _opp_chart_df["Grade"].map(_grade_colors).fillna("#aaaaaa")

            _opp_bar = (
                _alt.Chart(_opp_chart_df)
                .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
                .encode(
                    x=_alt.X("Underlying:N", sort="-y"),
                    y=_alt.Y("Score:Q", scale=_alt.Scale(domain=[0, 100])),
                    color=_alt.Color("color:N", scale=None, legend=None),
                    tooltip=["Underlying", "Score", "Grade"],
                )
                .properties(height=230, title="Today's Opportunity Scores by Underlying")
            )
            st.altair_chart(_opp_bar.interactive(), use_container_width=True)

            # One-click manual entry for best opportunity
            _best_sig = _opp_rows[0] if _opp_rows else None
            if _best_sig and _best_sig["Grade"] in ("A", "B"):
                st.success(
                    f"🚀 Best opportunity: **{_best_sig['Underlying']}** "
                    f"(Grade {_best_sig['Grade']}, Score {_best_sig['Score']:.0f}) — "
                    f"Credit {_best_sig['Credit']} | RoR {_best_sig['RoR%']}"
                )
                st.info(
                    "To enter this trade manually, go to the **🚀 Entry** tab "
                    "and set the underlying to **" + _best_sig["Underlying"] + "**. "
                    "The Auto Trader will enter automatically at its next scan if enabled."
                )

    except Exception as _opp_err:
        st.warning(f"Opportunity scanner error: {_opp_err}")

    st.divider()

    # ── Section 5: Event Calendar ─────────────────────────────────────────────
    st.markdown("### 📅 Event Calendar (Entry Risk)")

    try:
        from monitoring.market_filter import KNOWN_EVENTS, check_event_calendar
        import yaml as _evy

        with open("config/auto_trader.yaml") as _evf:
            _evcfg = _evy.safe_load(_evf).get("auto_trader", {})
        _ev_underlyings = _evcfg.get("underlyings", ["NIFTY"])
        _ev_blackout    = int(_evcfg.get("filters", {}).get("event_blackout", {}).get("days_before", 2))

        _today_ev = _idate.today()
        _ev_rows  = []
        for ev in sorted(KNOWN_EVENTS, key=lambda e: e.event_date):
            days_away = (ev.event_date - _today_ev).days
            if days_away < -3:
                continue
            affected_str = ", ".join(ev.affected) if ev.affected else "All Indices"
            _ev_rows.append({
                "Date":       ev.event_date.strftime("%d %b %Y"),
                "Event":      ev.name,
                "Type":       ev.event_type,
                "Affects":    affected_str,
                "Days Away":  days_away,
                "Risk":       "🔴 BLACKOUT" if 0 <= days_away <= _ev_blackout else
                              "🟡 CAUTION" if 0 <= days_away <= 5 else
                              "✅ OK" if days_away > 5 else "✔ Past",
            })

        if _ev_rows:
            _ev_df = _ipd.DataFrame(_ev_rows)
            def _ev_color(row):
                r = row["Risk"]
                if "BLACKOUT" in r:
                    return ["background-color: #3a0d0d"] * len(row)
                elif "CAUTION" in r:
                    return ["background-color: #2e2200"] * len(row)
                else:
                    return [""] * len(row)
            st.dataframe(
                _ev_df.style.apply(_ev_color, axis=1),
                use_container_width=True, hide_index=True,
            )
            st.caption(f"🔴 Blackout = within {_ev_blackout} days — Auto Trader will skip entry automatically")
        else:
            st.success("No upcoming events in calendar.")

    except Exception as _ev_err:
        st.warning(f"Event calendar error: {_ev_err}")

    st.divider()

    # ── Section 6: Signal History Log ────────────────────────────────────────
    st.markdown("### 📋 Signal History")
    st.caption("All signals generated by the Auto Trader intelligence engine today")

    try:
        from monitoring.notifier import read_recent_signals
        _all_sigs = read_recent_signals(50)

        if not _all_sigs:
            st.info("No signals yet. Start the Auto Trader to begin generating signals.")
        else:
            _sig_rows = []
            for s in _all_sigs[:30]:
                _sig_rows.append({
                    "Time":       s.get("signal_time", "")[:16],
                    "Type":       s.get("signal_type", ""),
                    "Underlying": s.get("underlying", ""),
                    "Grade":      s.get("grade", ""),
                    "Score":      s.get("composite_score", 0),
                    "Action":     s.get("action", ""),
                    "IV Rank":    f"{s.get('iv_rank', 0):.0f}",
                    "Acted On":   "✅" if s.get("acted_on") else "—",
                    "Reason":     s.get("reason", "")[:60],
                })
            st.dataframe(_ipd.DataFrame(_sig_rows), use_container_width=True, hide_index=True)

    except Exception as _sl_err:
        st.warning(f"Signal log error: {_sl_err}")

    st.divider()

    # ── Section 7: IV History Chart ───────────────────────────────────────────
    st.markdown("### 📉 IV History (builds over time)")
    st.caption("Stored daily IV readings used to compute IV rank. More history = more accurate rank.")

    try:
        from monitoring.iv_rank import get_iv_history
        import yaml as _ivy2
        with open("config/auto_trader.yaml") as _ivhf:
            _ivh_uls = _ivy2.safe_load(_ivhf).get("auto_trader", {}).get(
                "underlyings", ["NIFTY", "BANKNIFTY", "FINNIFTY"]
            )

        _ivh_sel = st.multiselect(
            "Select underlyings to chart",
            options=_ivh_uls,
            default=_ivh_uls[:3],
            key="ivh_select",
        )

        _ivh_rows = []
        for _ul in _ivh_sel:
            _hist = get_iv_history(get_db_path(), _ul, 252)
            for i, iv in enumerate(_hist):
                _ivh_rows.append({"Underlying": _ul, "Day": i + 1, "IV %": iv * 100})

        if _ivh_rows:
            _ivh_df = _ipd.DataFrame(_ivh_rows)
            _ivh_chart = (
                _alt.Chart(_ivh_df)
                .mark_line(point=False)
                .encode(
                    x=_alt.X("Day:Q", title="Trading Days of History"),
                    y=_alt.Y("IV %:Q", title="Implied Volatility %"),
                    color=_alt.Color("Underlying:N"),
                    tooltip=["Underlying", "Day", "IV %"],
                )
                .properties(height=250, title="IV History by Underlying (used for IV Rank computation)")
            )
            st.altair_chart(_ivh_chart.interactive(), use_container_width=True)
        else:
            st.info(
                "No IV history yet — the Auto Trader stores one reading per underlying per day. "
                "Run it for at least 30 days to get a meaningful IV rank."
            )

    except Exception as _ivh_err:
        st.warning(f"IV history chart error: {_ivh_err}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB: REPORTS
# ══════════════════════════════════════════════════════════════════════════════
with tab_reports:
    import io as _io
    import pandas as _rpd

    st.subheader("📊 Shift & Trade Reports")
    st.caption("Download Excel reports covering all shifts, premiums collected, shift costs, and net P&L.")

    _rp_db = get_db_path()
    if not Path(_rp_db).exists():
        st.info("No database found yet. Run paper entries first to generate trade data.")
    else:
        try:
            from sqlalchemy import text as _rp_text
            from storage.database import create_db_engine as _rp_engine_fn

            _rp_engine = _rp_engine_fn(_rp_db)

            with _rp_engine.connect() as _rp_conn:
                # ── Strategies (trade summary) ─────────────────────────────────
                _rp_strat = _rpd.read_sql(
                    _rp_text(
                        "SELECT id, underlying, expiry, lots, state, trading_mode, "
                        "entry_credit, exit_debit, final_pnl, created_at, updated_at "
                        "FROM strategies ORDER BY created_at DESC"
                    ),
                    _rp_conn,
                )

                # ── Shift events from alerts table ─────────────────────────────
                _rp_shifts = _rpd.read_sql(
                    _rp_text(
                        "SELECT timestamp, strategy_id, event, message "
                        "FROM alerts "
                        "WHERE event IN ('SHIFT_TRIGGERED','SHIFT_COMPLETE','SHIFT_FAILED') "
                        "ORDER BY timestamp DESC"
                    ),
                    _rp_conn,
                )

                # ── Orders for cost-of-shift detail ────────────────────────────
                _rp_orders = _rpd.read_sql(
                    _rp_text(
                        "SELECT strategy_id, trading_symbol, side, quantity, "
                        "limit_price, average_fill_price, status, submitted_at "
                        "FROM orders ORDER BY submitted_at DESC"
                    ),
                    _rp_conn,
                )

            # ── Summary metrics ────────────────────────────────────────────────
            if not _rp_strat.empty:
                _rp_completed = _rp_strat[_rp_strat["final_pnl"].notna()]
                _rp_total_pnl = float(_rp_completed["final_pnl"].sum()) if not _rp_completed.empty else 0.0
                _rp_total_credit = float(_rp_strat["entry_credit"].fillna(0).sum())
                _rp_shift_count = len(_rp_shifts)

                _rp_m1, _rp_m2, _rp_m3, _rp_m4 = st.columns(4)
                _rp_m1.metric("Total Trades", len(_rp_strat))
                _rp_m2.metric("Total Shifts", _rp_shift_count)
                _rp_m3.metric("Total Premium Collected", fmt_inr(_rp_total_credit))
                _rp_m4.metric("Net Realised P&L", fmt_inr(_rp_total_pnl))

                st.divider()

            # ── Date range filter ──────────────────────────────────────────────
            _rp_fc1, _rp_fc2, _rp_fc3 = st.columns(3)
            _rp_date_from = _rp_fc1.date_input("From", value=None, key="rp_date_from")
            _rp_date_to   = _rp_fc2.date_input("To",   value=None, key="rp_date_to")
            _rp_mode_filter = _rp_fc3.selectbox("Mode", ["All", "paper", "live"], key="rp_mode")

            # Apply filters
            _rp_strat_f = _rp_strat.copy()
            if "created_at" in _rp_strat_f.columns:
                _rp_strat_f["created_at"] = _rpd.to_datetime(_rp_strat_f["created_at"])
                if _rp_date_from:
                    _rp_strat_f = _rp_strat_f[_rp_strat_f["created_at"].dt.date >= _rp_date_from]
                if _rp_date_to:
                    _rp_strat_f = _rp_strat_f[_rp_strat_f["created_at"].dt.date <= _rp_date_to]
            if _rp_mode_filter != "All" and "trading_mode" in _rp_strat_f.columns:
                _rp_strat_f = _rp_strat_f[_rp_strat_f["trading_mode"] == _rp_mode_filter]

            # ── Trade summary table ────────────────────────────────────────────
            st.subheader("Trade Summary")
            if _rp_strat_f.empty:
                st.info("No trades in selected range.")
            else:
                # Compute shift count per strategy
                if not _rp_shifts.empty and "strategy_id" in _rp_shifts.columns:
                    _rp_shift_per = _rp_shifts.groupby("strategy_id").size().reset_index(name="shifts")
                    _rp_strat_f = _rp_strat_f.merge(_rp_shift_per, left_on="id", right_on="strategy_id", how="left")
                    _rp_strat_f["shifts"] = _rp_strat_f["shifts"].fillna(0).astype(int)
                else:
                    _rp_strat_f["shifts"] = 0

                # Numeric columns
                for _col in ["entry_credit", "exit_debit", "final_pnl"]:
                    if _col in _rp_strat_f.columns:
                        _rp_strat_f[_col] = _rp_strat_f[_col].apply(lambda x: float(x) if x is not None else None)

                _rp_display = _rp_strat_f[["underlying", "expiry", "lots", "state", "trading_mode",
                                            "entry_credit", "exit_debit", "final_pnl", "shifts",
                                            "created_at"]].copy()
                _rp_display.columns = ["Symbol", "Expiry", "Lots", "State", "Mode",
                                        "Entry Credit (pts)", "Exit Debit (pts)", "Net P&L (₹)",
                                        "Shifts", "Entered At"]
                st.dataframe(_rp_display, use_container_width=True, hide_index=True)

            # ── Shift events table ─────────────────────────────────────────────
            st.subheader("Shift Events")
            if _rp_shifts.empty:
                st.info("No shift events recorded yet.")
            else:
                _rp_shifts_d = _rp_shifts[["timestamp", "strategy_id", "event", "message"]].copy()
                _rp_shifts_d.columns = ["Time", "Strategy ID", "Event", "Details"]
                st.dataframe(_rp_shifts_d, use_container_width=True, hide_index=True)

            # ── Excel download ─────────────────────────────────────────────────
            st.divider()
            st.subheader("Download Excel Report")

            if st.button("📥 Generate Excel Report", type="primary", key="rp_generate"):
                try:
                    _rp_buf = _io.BytesIO()
                    with _rpd.ExcelWriter(_rp_buf, engine="openpyxl") as _rp_writer:

                        # Sheet 1: Trade Summary
                        _rp_sheet1 = _rp_strat.copy()
                        for _c in ["entry_credit", "exit_debit", "final_pnl"]:
                            if _c in _rp_sheet1.columns:
                                _rp_sheet1[_c] = _rp_sheet1[_c].apply(lambda x: float(x) if x is not None else None)
                        _rp_sheet1.to_excel(_rp_writer, sheet_name="Trade Summary", index=False)

                        # Sheet 2: Shift Events
                        if not _rp_shifts.empty:
                            _rp_shifts.to_excel(_rp_writer, sheet_name="Shift Events", index=False)

                        # Sheet 3: Orders (shift costs & premiums)
                        if not _rp_orders.empty:
                            _rp_orders_s = _rp_orders.copy()
                            for _c in ["limit_price", "average_fill_price"]:
                                if _c in _rp_orders_s.columns:
                                    _rp_orders_s[_c] = _rp_orders_s[_c].apply(
                                        lambda x: float(x) if x is not None else None
                                    )
                            # Cost/premium per order = fill_price × quantity
                            if "average_fill_price" in _rp_orders_s.columns and "quantity" in _rp_orders_s.columns:
                                _rp_orders_s["order_value"] = (
                                    _rp_orders_s["average_fill_price"].fillna(0) *
                                    _rp_orders_s["quantity"].fillna(0)
                                )
                                # Net premium: SELL orders add, BUY orders subtract
                                _rp_orders_s["net_premium_contribution"] = _rp_orders_s.apply(
                                    lambda r: r["order_value"] if str(r.get("side", "")).upper() == "SELL"
                                    else -r["order_value"],
                                    axis=1,
                                )
                            _rp_orders_s.to_excel(_rp_writer, sheet_name="Orders Detail", index=False)

                        # Sheet 4: Net Premium Summary per strategy
                        if not _rp_orders.empty and "strategy_id" in _rp_orders.columns:
                            _rp_net = _rp_orders.copy()
                            for _c in ["limit_price", "average_fill_price"]:
                                if _c in _rp_net.columns:
                                    _rp_net[_c] = _rp_net[_c].apply(lambda x: float(x) if x is not None else None)
                            if "average_fill_price" in _rp_net.columns and "quantity" in _rp_net.columns:
                                _rp_net["order_value"] = (
                                    _rp_net["average_fill_price"].fillna(0) * _rp_net["quantity"].fillna(0)
                                )
                                _rp_net["net_contribution"] = _rp_net.apply(
                                    lambda r: r["order_value"] if str(r.get("side", "")).upper() == "SELL"
                                    else -r["order_value"],
                                    axis=1,
                                )
                                _rp_summary = _rp_net.groupby("strategy_id").agg(
                                    total_premium_received=("net_contribution", lambda x: x[x > 0].sum()),
                                    total_cost_paid=("net_contribution", lambda x: (-x[x < 0]).sum()),
                                    net_premium=("net_contribution", "sum"),
                                    total_orders=("strategy_id", "count"),
                                ).reset_index()
                                _rp_summary.to_excel(_rp_writer, sheet_name="Net Premium Summary", index=False)

                    _rp_buf.seek(0)
                    st.download_button(
                        label="⬇️ Download Excel",
                        data=_rp_buf,
                        file_name=f"shift_report_{now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="rp_download",
                    )
                    st.success("Report ready — click Download Excel above.")
                except Exception as _rp_xe:
                    st.error(f"Excel generation failed: {_rp_xe}")

        except Exception as _rp_err:
            st.error(f"Reports error: {_rp_err}")
