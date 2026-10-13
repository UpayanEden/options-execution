"""
Flask web application for the shifting iron condor live trading dashboard.

Reads all state from:
- data/live_state.db   (SQLite database written by CondorRunner)
- logs/live/*.jsonl    (Structured JSON event logs written by TradeLogger)

No shared memory with the runner process — fully decoupled.

Routes:
    GET  /                          — Dashboard (summary cards + positions table)
    GET  /positions                 — Detailed 4-leg breakdown
    GET  /logs                      — Log viewer with filters
    GET  /config                    — View live_config.yaml
    POST /config                    — Update config (saves to config/live_config.yaml)
    GET  /api/positions             — JSON positions
    GET  /api/pnl                   — JSON daily P&L per symbol
    GET  /api/logs                  — JSON last N events
    GET  /api/health                — JSON {status, kill_switch_active, last_heartbeat}
    POST /api/kill_switch/activate  — Activate kill switch
    POST /api/kill_switch/deactivate — Deactivate kill switch
"""
from __future__ import annotations

import glob
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml
from flask import Flask, jsonify, redirect, render_template, request, url_for

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────

_DB_PATH = Path("data/live_state.db")
_LOG_DIR = Path("logs/live")
_CONFIG_PATH = Path("config/live_config.yaml")
_KILL_SWITCH_FLAG = Path("logs/KILL_SWITCH_ACTIVE")
_HEARTBEAT_FILE = Path("logs/live/heartbeat.txt")
_RUNNER_PID_FILE = Path("logs/runner.pid")
_RUNNER_MODE_FILE = Path("logs/runner.mode")

# ── Flask app factory ──────────────────────────────────────────────────────


def create_app() -> Flask:
    """
    Create and configure the Flask application.

    Returns:
        Configured Flask app instance.
    """
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-prod")

    @app.context_processor
    def inject_runner_status():
        """Inject runner_running, trading_mode, and alert_count into every template automatically."""
        running, _ = _runner_is_alive()
        try:
            cfg_raw = yaml.safe_load(_load_config_yaml()) or {}
            trading_mode = str(cfg_raw.get("mode", "paper")).lower()
        except Exception:
            trading_mode = "paper"
        alert_count = _count_active_alerts()
        return {"runner_running": running, "trading_mode": trading_mode, "alert_count": alert_count}

    # ── Register routes ────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        """Render the main dashboard with summary cards and open positions."""
        positions = _load_positions_from_db()
        pnl_summary = _compute_pnl_summary(positions)
        win_rate = _compute_win_rate()
        shifts_today = _count_today_shifts()
        cfg_raw = yaml.safe_load(_load_config_yaml()) or {}
        capital_info = cfg_raw.get("capital", {})
        strategy_info = cfg_raw.get("strategy", {})
        pos_metrics = _compute_position_metrics(positions, cfg_raw)
        shift_stats = _get_shift_stats_by_symbol()
        open_positions = [p for p in positions if p.get("state") in ("OPEN", "SHIFTING")]
        total_cash_margin = round(sum(
            pos_metrics.get(p.get("symbol", ""), {}).get("cash_margin", 0)
            for p in open_positions
        ), 2)
        total_net_premium = round(sum(
            pos_metrics.get(p.get("symbol", ""), {}).get("net_premium_total", 0)
            + shift_stats.get(p.get("symbol", ""), {}).get("shift_pnl", 0.0)
            for p in open_positions
        ), 2)
        return render_template(
            "dashboard.html",
            positions=positions,
            pos_metrics=pos_metrics,
            shift_stats=shift_stats,
            total_pnl=pnl_summary.get("total_pnl", 0.0),
            open_count=sum(1 for p in positions if p.get("state") == "OPEN"),
            win_rate=win_rate,
            shifts_today=shifts_today,
            total_cash_margin=total_cash_margin,
            total_net_premium=total_net_premium,
            kill_switch_active=_KILL_SWITCH_FLAG.exists(),
            now=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            capital_info=capital_info,
            strategy_info=strategy_info,
            runner_running=_runner_is_alive()[0],
        )

    @app.route("/positions")
    def positions_view():
        """Render the detailed positions page with 4-leg breakdown."""
        positions = _load_positions_from_db()
        cfg_raw = yaml.safe_load(_load_config_yaml()) or {}
        pos_metrics = _compute_position_metrics(positions, cfg_raw)
        return render_template(
            "positions.html",
            positions=positions,
            pos_metrics=pos_metrics,
            kill_switch_active=_KILL_SWITCH_FLAG.exists(),
            now=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    @app.route("/logs")
    def logs_view():
        """Render the log viewer with filter controls."""
        symbol_filter = request.args.get("symbol", "")
        event_filter = request.args.get("event_type", "")
        level_filter = request.args.get("level", "")
        limit = int(request.args.get("limit", 200))

        events = _load_log_events(
            symbol=symbol_filter or None,
            event_type=event_filter or None,
            level=level_filter or None,
            limit=limit,
        )
        all_symbols = _get_log_symbols()
        all_event_types = _get_log_event_types()

        return render_template(
            "logs.html",
            events=events,
            symbol_filter=symbol_filter,
            event_filter=event_filter,
            level_filter=level_filter,
            limit=limit,
            all_symbols=all_symbols,
            all_event_types=all_event_types,
            kill_switch_active=_KILL_SWITCH_FLAG.exists(),
        )

    @app.route("/alerts")
    def alerts_view():
        """Render the alerts page with shift failures and critical events."""
        alerts_data = _load_alerts()
        return render_template(
            "alerts.html",
            alerts=alerts_data["alerts"],
            counts=alerts_data["counts"],
            kill_switch_active=_KILL_SWITCH_FLAG.exists(),
            now=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        )

    @app.route("/token")
    def token_view():
        """Render the daily token refresh page."""
        creds = _load_credentials()
        login_url = _build_kite_login_url(creds.get("ZERODHA_API_KEY", ""))
        return render_template(
            "token.html",
            login_url=login_url,
            api_key=creds.get("ZERODHA_API_KEY", ""),
            token_set=bool(creds.get("ZERODHA_ACCESS_TOKEN", "")),
            kill_switch_active=_KILL_SWITCH_FLAG.exists(),
        )

    @app.route("/api/token/refresh", methods=["POST"])
    def api_token_refresh():
        """
        Exchange a request_token (from Kite redirect URL) for an access_token.
        Saves the new token to credentials.env and updates os.environ live.
        """
        data = request.get_json(force=True, silent=True) or {}
        raw = data.get("redirect_url", "").strip()
        if not raw:
            return jsonify({"error": "redirect_url is required"}), 400

        # Extract request_token from full URL or bare token
        import re as _re
        from urllib.parse import parse_qs, urlparse
        request_token = None
        if raw.startswith("http"):
            params = parse_qs(urlparse(raw).query)
            toks = params.get("request_token") or params.get("request-token")
            if toks:
                request_token = toks[0]
        elif _re.fullmatch(r"[A-Za-z0-9]{10,}", raw):
            request_token = raw

        if not request_token:
            return jsonify({"error": "Could not extract request_token from input"}), 400

        creds = _load_credentials()
        api_key = creds.get("ZERODHA_API_KEY", "")
        api_secret = creds.get("ZERODHA_API_SECRET", "")
        if not api_key or not api_secret:
            return jsonify({"error": "ZERODHA_API_KEY or ZERODHA_API_SECRET not set in credentials.env"}), 500

        try:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=api_key)
            session = kite.generate_session(request_token, api_secret=api_secret)
            access_token = session["access_token"]
        except Exception as exc:
            logger.error("token_exchange_failed", extra={"error": str(exc)})
            return jsonify({"error": f"Token exchange failed: {exc}"}), 500

        # Save to credentials.env and update live environment
        if _save_credential("ZERODHA_ACCESS_TOKEN", access_token):
            os.environ["ZERODHA_ACCESS_TOKEN"] = access_token
            logger.info("access_token_refreshed_via_web")
            return jsonify({"refreshed": True, "token_preview": access_token[:6] + "****"})
        return jsonify({"error": "Failed to write credentials.env"}), 500

    @app.route("/config", methods=["GET"])
    def config_view():
        """Render the config editor page."""
        cfg_data = _load_config_yaml()
        return render_template(
            "config.html",
            config=cfg_data,
            kill_switch_active=_KILL_SWITCH_FLAG.exists(),
        )

    @app.route("/config", methods=["POST"])
    def config_update():
        """Save updated config to live_config.yaml from form POST."""
        try:
            raw_yaml = request.form.get("config_yaml", "")
            # Validate that it parses
            parsed = yaml.safe_load(raw_yaml)
            if not isinstance(parsed, dict):
                raise ValueError("Config must be a YAML mapping")
            _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_CONFIG_PATH, "w") as fh:
                fh.write(raw_yaml)
            logger.info("config_updated_via_web")
            return redirect(url_for("config_view"))
        except Exception as exc:
            cfg_data = _load_config_yaml()
            return render_template(
                "config.html",
                config=cfg_data,
                error=str(exc),
                kill_switch_active=_KILL_SWITCH_FLAG.exists(),
            )

    # ── JSON API routes ────────────────────────────────────────────────────

    @app.route("/api/positions")
    def api_positions():
        """Return current positions as JSON."""
        positions = _load_positions_from_db()
        return jsonify({"positions": positions, "count": len(positions)})

    @app.route("/api/dashboard/summary")
    def api_dashboard_summary():
        """Return all data needed to refresh the dashboard without a full page reload."""
        positions = _load_positions_from_db()
        cfg_raw = yaml.safe_load(_load_config_yaml()) or {}
        pos_metrics = _compute_position_metrics(positions, cfg_raw)
        shift_stats = _get_shift_stats_by_symbol()
        positions_with_metrics = []
        for pos in positions:
            sym = pos.get("symbol", "")
            entry = dict(pos)
            entry["metrics"] = pos_metrics.get(sym, {})
            ss = shift_stats.get(sym, {})
            entry["total_shifts"] = ss.get("total_shifts", pos.get("shift_count", 0))
            entry["net_premium_with_shifts"] = round(
                pos_metrics.get(sym, {}).get("net_premium_total", 0)
                + ss.get("shift_pnl", 0.0), 2
            )
            positions_with_metrics.append(entry)
        return jsonify({
            "open_count": sum(1 for p in positions if p.get("state") == "OPEN"),
            "win_rate": _compute_win_rate(),
            "shifts_today": _count_today_shifts(),
            "now": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            "positions": positions_with_metrics,
        })

    @app.route("/api/pnl")
    def api_pnl():
        """Return today's P&L per symbol as JSON."""
        pnl = _load_daily_pnl()
        return jsonify({"pnl": pnl, "date": date.today().isoformat()})

    @app.route("/api/live_pnl")
    def api_live_pnl():
        """
        Fetch live option quotes for all open positions and return unrealised P&L.

        Unrealised P&L per position:
          close_cost = sum(ask of short legs) - sum(bid of long legs)
          unrealised_pnl = (entry_credit - close_cost) * lots * lot_size

        Returns JSON with per-position P&L and total.
        """
        positions = _load_positions_from_db()
        open_pos = [p for p in positions if p.get("state") in ("OPEN", "SHIFTING")]
        if not open_pos:
            return jsonify({"positions": [], "total_unrealised_pnl": 0.0})

        try:
            broker = _get_read_broker()
        except Exception as exc:
            return jsonify({"error": str(exc), "positions": [], "total_unrealised_pnl": 0.0}), 200

        # Build flat list of (token, symbol) for batch quote fetch
        instruments: list[tuple[int, str]] = []
        pos_legs: dict[str, dict] = {}
        for pos in open_pos:
            sym = pos["symbol"]
            legs = {}
            for leg_key in ("short_put", "long_put", "short_call", "long_call"):
                leg = pos.get(leg_key)
                if isinstance(leg, dict):
                    token = leg.get("instrument_token")
                    tsym = leg.get("trading_symbol", "")
                    if token:
                        token = int(token)
                        instruments.append((token, tsym))
                        legs[leg_key] = token
            pos_legs[sym] = legs

        try:
            quotes = broker.get_quotes(instruments)
        except Exception as exc:
            return jsonify({"error": f"Quote fetch failed: {exc}", "positions": [], "total_unrealised_pnl": 0.0}), 200

        results = []
        total_pnl = 0.0
        for pos in open_pos:
            sym = pos["symbol"]
            entry_credit = float(pos.get("initial_credit", 0) or 0)
            lots = int(pos.get("lots", 1) or 1)
            lot_size = int(pos.get("lot_size", 1) or 1)
            qty = lots * lot_size
            legs = pos_legs.get(sym, {})

            close_cost = 0.0
            complete = True
            for leg_key, token in legs.items():
                q = quotes.get(token)
                if not q:
                    complete = False
                    break
                if "short" in leg_key:
                    close_cost += float(q.ask)   # buy back short at ask
                else:
                    close_cost -= float(q.bid)   # sell long at bid

            if complete:
                unrealised = round((entry_credit - close_cost) * qty, 2)
                total_pnl += unrealised
            else:
                unrealised = None

            results.append({
                "symbol": sym,
                "unrealised_pnl": unrealised,
                "entry_credit": round(entry_credit, 4),
                "close_cost": round(close_cost, 4) if complete else None,
            })

        return jsonify({
            "positions": results,
            "total_unrealised_pnl": round(total_pnl, 2),
        })

    @app.route("/api/position_details")
    def api_position_details():
        """
        Enhanced position data with live quotes, margins, and derived metrics.

        Returns per-position:
          - Current spot price
          - Per-leg bid/ask/LTP and order book depth (top 5 levels)
          - Distance from current spot to each short strike (₹ and %)
          - Net premium as % of spread width
          - Estimated cash margin (max-loss proxy) and contract value
          - ROI on cash margin and on contract value
          - Current cost to exit / shift (close_cost × qty)
          - Unrealised P&L
        """
        positions = _load_positions_from_db()
        open_pos = [p for p in positions if p.get("state") in ("OPEN", "SHIFTING")]
        if not open_pos:
            return jsonify({"positions": []})

        try:
            broker = _get_read_broker()
        except Exception as exc:
            return jsonify({"error": str(exc), "positions": []}), 200

        # Collect all option instruments to quote
        instruments: list[tuple[int, str]] = []
        pos_leg_tokens: dict[str, dict[str, int]] = {}

        for pos in open_pos:
            sym = pos["symbol"]
            leg_tokens: dict[str, int] = {}
            for leg_key in ("short_put", "long_put", "short_call", "long_call"):
                leg = pos.get(leg_key)
                if isinstance(leg, dict):
                    token = leg.get("instrument_token")
                    tsym = leg.get("trading_symbol", "")
                    if token:
                        token = int(token)
                        instruments.append((token, tsym))
                        leg_tokens[leg_key] = token
            pos_leg_tokens[sym] = leg_tokens

        try:
            quotes = broker.get_quotes(instruments)
        except Exception as exc:
            return jsonify({"error": f"Quote fetch failed: {exc}", "positions": []}), 200

        results = []
        for pos in open_pos:
            sym = pos["symbol"]
            entry_credit = float(pos.get("initial_credit", 0) or 0)
            lots = int(pos.get("lots", 1) or 1)
            lot_size = int(pos.get("lot_size", 1) or 1)
            qty = lots * lot_size
            leg_tokens = pos_leg_tokens.get(sym, {})
            strikes = pos.get("strikes", {}) or {}

            # Build per-leg quote data including depth
            leg_quotes: dict[str, dict] = {}
            for leg_key, token in leg_tokens.items():
                q = quotes.get(token)
                if q:
                    leg_quotes[leg_key] = {
                        "ltp": float(q.ltp),
                        "bid": float(q.bid),
                        "ask": float(q.ask),
                        "bid_qty": int(q.bid_qty),
                        "ask_qty": int(q.ask_qty),
                        "oi": int(q.oi),
                        "depth_bids": [
                            {"price": float(d.price), "qty": d.quantity, "orders": d.orders}
                            for d in (q.depth_bids or [])[:5]
                        ],
                        "depth_asks": [
                            {"price": float(d.price), "qty": d.quantity, "orders": d.orders}
                            for d in (q.depth_asks or [])[:5]
                        ],
                    }

            # Close cost: cost to exit all legs at current market prices
            close_cost = 0.0
            close_complete = True
            for leg_key in ("short_put", "short_call", "long_put", "long_call"):
                lq = leg_quotes.get(leg_key)
                if not lq:
                    close_complete = False
                    break
                if "short" in leg_key:
                    close_cost += lq["ask"]   # buy back short at ask
                else:
                    close_cost -= lq["bid"]   # sell long at bid (receive)

            unrealised_pnl = round((entry_credit - close_cost) * qty, 2) if close_complete else None

            # Current spot price
            current_spot: Optional[float] = None
            try:
                if hasattr(broker, "get_equity_spot"):
                    current_spot = broker.get_equity_spot(sym)
            except Exception:
                pass

            # Strike arithmetic
            sp = float(strikes.get("short_put") or 0)
            sc = float(strikes.get("short_call") or 0)
            lp = float(strikes.get("long_put") or 0)
            lc = float(strikes.get("long_call") or 0)

            put_width = round(sp - lp, 2) if (sp and lp) else 0.0
            call_width = round(lc - sc, 2) if (lc and sc) else 0.0
            # Use the wider wing as the representative spread width
            spread_width = max(put_width, call_width)

            # Net premium as % of spread width
            net_premium_pct = round(entry_credit / spread_width * 100, 2) if spread_width > 0 else None

            # Distance from current spot to each short strike
            dist_sp: Optional[float] = None
            dist_sc: Optional[float] = None
            dist_sp_pct: Optional[float] = None
            dist_sc_pct: Optional[float] = None
            if current_spot:
                if sp:
                    dist_sp = round(abs(current_spot - sp), 2)
                    dist_sp_pct = round(dist_sp / current_spot * 100, 2)
                if sc:
                    dist_sc = round(abs(current_spot - sc), 2)
                    dist_sc_pct = round(dist_sc / current_spot * 100, 2)

            # Margin estimates
            # Cash margin ≈ max loss = (spread_width − net_credit_per_share) × qty
            max_loss_per_share = max(spread_width - entry_credit, 0.0)
            cash_margin = round(max_loss_per_share * qty, 2)
            # Stock/contract value = spot × qty (notional value of underlying)
            stock_margin = round(float(current_spot or 0) * qty, 2)

            net_credit_total = round(entry_credit * qty, 2)
            roi_cash = round(net_credit_total / cash_margin * 100, 2) if cash_margin > 0 else None
            roi_stock = round(net_credit_total / stock_margin * 100, 2) if stock_margin > 0 else None

            results.append({
                "symbol": sym,
                "current_spot": current_spot,
                "unrealised_pnl": unrealised_pnl,
                "close_cost_per_share": round(close_cost, 4) if close_complete else None,
                "exit_cost_total": round(close_cost * qty, 2) if close_complete else None,
                "leg_quotes": leg_quotes,
                "dist_from_short_put": dist_sp,
                "dist_from_short_call": dist_sc,
                "dist_from_short_put_pct": dist_sp_pct,
                "dist_from_short_call_pct": dist_sc_pct,
                "spread_width": spread_width,
                "net_premium_pct_of_spread": net_premium_pct,
                "cash_margin": cash_margin,
                "stock_margin": stock_margin,
                "total_margin": round(cash_margin + stock_margin, 2),
                "roi_cash_pct": roi_cash,
                "roi_stock_pct": roi_stock,
                "net_credit_total": net_credit_total,
                "qty": qty,
                "entry_credit": entry_credit,
            })

        return jsonify({"positions": results})

    @app.route("/api/shift_cost")
    def api_shift_cost():
        """
        Estimate the cost of shifting each open position, replicating the runner's
        exact shift_condor() logic against current live quotes.

        For each open position and each side (PUT / CALL):
          close_debit     = short_ask − long_bid           (pay to close threatened spread)
          new_credit      = mid(new_short) − mid(new_long) (receive from re-entry)
          net_shift_cost  = (close_debit − new_credit) × qty  [positive = net outflow]

        New target strikes mirror condor_runner exactly:
          PUT  shift: new_short = spot × (1 − put_dist_pct/100)
                      new_long  = new_short − spot × wing_width_pct/100
          CALL shift: new_short = spot × (1 + call_dist_pct/100)
                      new_long  = new_short + spot × wing_width_pct/100
        Strikes are snapped to the nearest available on the exchange.
        """
        from datetime import date as _date
        from decimal import Decimal as _Dec

        positions = _load_positions_from_db()
        open_pos = [p for p in positions if p.get("state") in ("OPEN", "SHIFTING")]
        if not open_pos:
            return jsonify({"positions": []})

        try:
            broker = _get_read_broker()
        except Exception as exc:
            return jsonify({"error": str(exc), "positions": []}), 200

        # Strategy config
        try:
            cfg_raw = yaml.safe_load(_load_config_yaml()) or {}
        except Exception:
            cfg_raw = {}
        strat = cfg_raw.get("strategy", {})
        wing_width_pct   = float(strat.get("wing_width_pct",   2.0))
        trigger_pct      = float(strat.get("shift_trigger_pct", 1.0))
        stocks_map       = {s["symbol"]: s for s in cfg_raw.get("stocks", []) if isinstance(s, dict)}

        # Load instrument master once (disk-cached — no extra API call)
        inst_master: list[dict] = []
        try:
            if hasattr(broker, "_load_instruments"):
                inst_master = broker._load_instruments()
        except Exception:
            pass

        # ── helpers ──────────────────────────────────────────────────────────

        def _get_strikes(symbol: str, expiry: _date):
            puts: set[float] = set()
            calls: set[float] = set()
            for inst in inst_master:
                if str(inst.get("name", "")).upper() != symbol.upper():
                    continue
                raw_exp = inst.get("expiry")
                if hasattr(raw_exp, "date"):
                    raw_exp = raw_exp.date()
                elif isinstance(raw_exp, str):
                    try:
                        raw_exp = _date.fromisoformat(raw_exp)
                    except ValueError:
                        continue
                if raw_exp != expiry:
                    continue
                try:
                    strike = float(inst.get("strike", 0))
                except (TypeError, ValueError):
                    continue
                itype = inst.get("instrument_type", "")
                if itype == "PE":
                    puts.add(strike)
                elif itype == "CE":
                    calls.add(strike)
            return sorted(puts), sorted(calls)

        def _snap(target: float, available: list[float]) -> float:
            return min(available, key=lambda s: abs(s - target)) if available else target

        def _mid(q) -> float:
            from decimal import Decimal as D
            mid = (q.bid + q.ask) / D("2")
            return float((mid / D("0.05")).quantize(D("1")) * D("0.05"))

        # ── per-position ──────────────────────────────────────────────────────

        results = []
        for pos in open_pos:
            sym          = pos["symbol"]
            qty          = int(pos.get("lots", 1) or 1) * int(pos.get("lot_size", 1) or 1)
            expiry_str   = pos.get("expiry", "")
            strikes      = pos.get("strikes", {}) or {}
            stock_cfg    = stocks_map.get(sym, {})
            put_dist     = float(stock_cfg.get("put_dist_pct",  5.0))
            call_dist    = float(stock_cfg.get("call_dist_pct", 5.0))

            # Current short strikes
            sp = float(strikes.get("short_put")  or 0)
            sc = float(strikes.get("short_call") or 0)

            # Spot
            spot: Optional[float] = None
            try:
                if hasattr(broker, "get_equity_spot"):
                    spot = broker.get_equity_spot(sym)
            except Exception:
                pass
            if not spot:
                results.append({"symbol": sym, "error": "no_spot"})
                continue

            try:
                exp = _date.fromisoformat(expiry_str)
            except (ValueError, TypeError):
                results.append({"symbol": sym, "error": "bad_expiry"})
                continue

            avail_puts, avail_calls = _get_strikes(sym, exp)
            wing_width = spot * wing_width_pct / 100.0

            # New target strikes
            new_sp_tgt = spot * (1.0 - put_dist  / 100.0);  new_sp = _snap(new_sp_tgt, avail_puts)
            new_lp_tgt = new_sp_tgt - wing_width;            new_lp = _snap(new_lp_tgt, avail_puts)
            new_sc_tgt = spot * (1.0 + call_dist / 100.0);  new_sc = _snap(new_sc_tgt, avail_calls)
            new_lc_tgt = new_sc_tgt + wing_width;            new_lc = _snap(new_lc_tgt, avail_calls)

            # Resolve new instruments
            instruments_to_quote: list[tuple[int, str]] = []
            current_tokens: dict[str, int] = {}
            for leg_key in ("short_put", "long_put", "short_call", "long_call"):
                leg = pos.get(leg_key)
                if isinstance(leg, dict) and leg.get("instrument_token"):
                    tok = int(leg["instrument_token"])
                    current_tokens[leg_key] = tok
                    instruments_to_quote.append((tok, leg.get("trading_symbol", "")))

            new_insts: dict[str, Any] = {}
            for new_key, strike_val, opt_type in [
                ("new_sp", new_sp, "PE"),
                ("new_lp", new_lp, "PE"),
                ("new_sc", new_sc, "CE"),
                ("new_lc", new_lc, "CE"),
            ]:
                try:
                    inst = broker.resolve_instrument(sym, "NFO", exp, _Dec(str(strike_val)), opt_type)
                    new_insts[new_key] = inst
                    instruments_to_quote.append((inst.instrument_token, inst.trading_symbol))
                except Exception:
                    pass

            try:
                all_q = broker.get_quotes(instruments_to_quote)
            except Exception as exc:
                results.append({"symbol": sym, "error": f"quote_failed: {exc}"})
                continue

            # Quotes for current legs
            def cq(key):
                return all_q.get(current_tokens.get(key))

            q_sp = cq("short_put");  q_lp = cq("long_put")
            q_sc = cq("short_call"); q_lc = cq("long_call")

            # Quotes for new legs
            def nq(key):
                inst = new_insts.get(key)
                return all_q.get(inst.instrument_token) if inst else None

            q_nsp = nq("new_sp"); q_nlp = nq("new_lp")
            q_nsc = nq("new_sc"); q_nlc = nq("new_lc")

            # Spread close debits (what you pay to close the spread)
            put_close  = round(float(q_sp.ask) - float(q_lp.bid), 4) if q_sp and q_lp else None
            call_close = round(float(q_sc.ask) - float(q_lc.bid), 4) if q_sc and q_lc else None

            # New spread credits (what you receive from the re-entry at mid)
            put_new_cr  = round(_mid(q_nsp) - _mid(q_nlp), 4) if q_nsp and q_nlp else None
            call_new_cr = round(_mid(q_nsc) - _mid(q_nlc), 4) if q_nsc and q_nlc else None

            # Net shift cost per share and total
            put_net  = round(put_close  - put_new_cr,  4) if (put_close  is not None and put_new_cr  is not None) else None
            call_net = round(call_close - call_new_cr, 4) if (call_close is not None and call_new_cr is not None) else None
            put_net_total  = round(put_net  * qty, 2) if put_net  is not None else None
            call_net_total = round(call_net * qty, 2) if call_net is not None else None

            # Trigger levels: fire when spot comes within trigger_pct% of the short strike
            put_trigger  = round(sp * (1.0 + trigger_pct / 100.0), 2) if sp else None
            call_trigger = round(sc * (1.0 - trigger_pct / 100.0), 2) if sc else None
            put_triggered  = bool(spot < put_trigger)  if (put_trigger  and sp) else False
            call_triggered = bool(spot > call_trigger) if (call_trigger and sc) else False

            dist_to_sp = round(spot - sp, 2) if sp else None
            dist_to_sc = round(sc - spot, 2) if sc else None
            if dist_to_sp is not None and dist_to_sc is not None:
                threatened = "PUT" if dist_to_sp < dist_to_sc else "CALL"
            else:
                threatened = "PUT" if dist_to_sp is not None else "CALL"

            results.append({
                "symbol":         sym,
                "current_spot":   spot,
                "threatened_side": threatened,
                "put_triggered":  put_triggered,
                "call_triggered": call_triggered,
                "put_trigger_level":  put_trigger,
                "call_trigger_level": call_trigger,
                "dist_to_short_put":  dist_to_sp,
                "dist_to_short_call": dist_to_sc,
                "put_side": {
                    "current_short_strike": sp,
                    "current_long_strike":  float(strikes.get("long_put") or 0),
                    "current_short_ask":    float(q_sp.ask) if q_sp else None,
                    "current_long_bid":     float(q_lp.bid) if q_lp else None,
                    "close_debit":          put_close,
                    "new_short_strike":     new_sp,
                    "new_long_strike":      new_lp,
                    "new_short_mid":        _mid(q_nsp) if q_nsp else None,
                    "new_long_mid":         _mid(q_nlp) if q_nlp else None,
                    "new_credit":           put_new_cr,
                    "net_per_share":        put_net,
                    "net_total":            put_net_total,
                },
                "call_side": {
                    "current_short_strike": sc,
                    "current_long_strike":  float(strikes.get("long_call") or 0),
                    "current_short_ask":    float(q_sc.ask) if q_sc else None,
                    "current_long_bid":     float(q_lc.bid) if q_lc else None,
                    "close_debit":          call_close,
                    "new_short_strike":     new_sc,
                    "new_long_strike":      new_lc,
                    "new_short_mid":        _mid(q_nsc) if q_nsc else None,
                    "new_long_mid":         _mid(q_nlc) if q_nlc else None,
                    "new_credit":           call_new_cr,
                    "net_per_share":        call_net,
                    "net_total":            call_net_total,
                },
            })

        return jsonify({"positions": results})

    @app.route("/api/margin_summary")
    def api_margin_summary():
        """
        Return actual Zerodha margin data: pledged stock collateral and how much
        of it is consumed by current F&O positions.

        stock_margin_used = min(collateral_pledged, span + exposure)
        """
        try:
            broker = _get_read_broker()
            funds = broker._kite.margins()
            eq = funds.get("equity", {})
            used = eq.get("used", {})
            available = eq.get("available", {})
            span = float(used.get("span", 0) or 0)
            exposure = float(used.get("exposure", 0) or 0)
            collateral = float(available.get("collateral", 0) or 0)
            total_required = span + exposure
            stock_margin_used = min(collateral, total_required)
            cash_margin_used = max(0.0, total_required - collateral)
            return jsonify({
                "span": round(span, 2),
                "exposure": round(exposure, 2),
                "total_required": round(total_required, 2),
                "collateral_pledged": round(collateral, 2),
                "stock_margin_used": round(stock_margin_used, 2),
                "cash_margin_used": round(cash_margin_used, 2),
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 200

    @app.route("/api/alerts")
    def api_alerts():
        """Return alerts JSON. Query param: count_only=true returns just the badge count."""
        if request.args.get("count_only") == "true":
            return jsonify({"count": _count_active_alerts()})
        return jsonify(_load_alerts())

    @app.route("/api/logs")
    def api_logs():
        """Return last N log events as JSON. Query params: n (default 100), symbol, event_type."""
        n = int(request.args.get("n", 100))
        symbol = request.args.get("symbol")
        event_type = request.args.get("event_type")
        events = _load_log_events(symbol=symbol, event_type=event_type, limit=n)
        return jsonify({"events": events, "count": len(events)})

    @app.route("/api/health")
    def api_health():
        """Return system health JSON with kill switch and heartbeat status."""
        last_heartbeat = _read_heartbeat()
        return jsonify({
            "status": "ok",
            "kill_switch_active": _KILL_SWITCH_FLAG.exists(),
            "last_heartbeat": last_heartbeat,
            "timestamp": datetime.now(IST).isoformat(),
        })

    @app.route("/api/kill_switch/activate", methods=["POST"])
    def api_kill_switch_activate():
        """Activate the kill switch flag file."""
        reason = request.json.get("reason", "Activated via web UI") if request.is_json else "Activated via web UI"
        _KILL_SWITCH_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _KILL_SWITCH_FLAG.write_text(f"KILL SWITCH ACTIVATED\n{reason}\n")
        logger.critical("kill_switch_activated_web", extra={"reason": reason})
        return jsonify({"activated": True, "reason": reason})

    @app.route("/api/kill_switch/deactivate", methods=["POST"])
    def api_kill_switch_deactivate():
        """Deactivate the kill switch by removing the flag file."""
        if _KILL_SWITCH_FLAG.exists():
            _KILL_SWITCH_FLAG.unlink()
            logger.info("kill_switch_deactivated_web")
            return jsonify({"deactivated": True})
        return jsonify({"deactivated": False, "message": "Kill switch was not active"})

    @app.route("/api/runner/status")
    def api_runner_status():
        """Return whether the trading runner process is currently alive."""
        running, pid = _runner_is_alive()
        mode = _RUNNER_MODE_FILE.read_text().strip() if _RUNNER_MODE_FILE.exists() else "unknown"
        return jsonify({"running": running, "pid": pid, "mode": mode})

    @app.route("/api/runner/start", methods=["POST"])
    def api_runner_start():
        """Launch run_live.py as a background subprocess using mode from config file."""
        running, pid = _runner_is_alive()
        if running:
            return jsonify({"started": False, "reason": "already_running", "pid": pid})

        # Read mode from live_config.yaml — the dashboard config is the source of truth.
        # Do NOT accept a mode override from the request body.
        try:
            cfg_raw = yaml.safe_load(_load_config_yaml()) or {}
            mode = str(cfg_raw.get("mode", "paper")).lower()
            if mode not in ("paper", "live"):
                mode = "paper"
        except Exception:
            mode = "paper"

        try:
            project_root = Path(__file__).resolve().parent.parent
            log_dir = project_root / "logs" / "live"
            log_dir.mkdir(parents=True, exist_ok=True)
            runner_log = open(log_dir / "runner_stdout.log", "a")
            # No --mode flag: run_live.py reads mode directly from live_config.yaml
            proc = subprocess.Popen(
                [sys.executable, str(project_root / "run_live.py"), "--runner-only"],
                cwd=str(project_root),
                stdout=runner_log,
                stderr=runner_log,
                start_new_session=True,
            )
            _RUNNER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            _RUNNER_PID_FILE.write_text(str(proc.pid))
            _RUNNER_MODE_FILE.write_text(mode)
            logger.info("runner_started_web", extra={"pid": proc.pid, "mode": mode})
            return jsonify({"started": True, "pid": proc.pid, "mode": mode})
        except Exception as exc:
            logger.error("runner_start_failed", extra={"error": str(exc)})
            return jsonify({"started": False, "reason": str(exc)}), 500

    @app.route("/api/runner/stop", methods=["POST"])
    def api_runner_stop():
        """Send SIGTERM to the runner process (does not exit positions)."""
        running, pid = _runner_is_alive()
        if not running:
            return jsonify({"stopped": False, "reason": "not_running"})
        try:
            os.kill(pid, signal.SIGTERM)
            _RUNNER_PID_FILE.unlink(missing_ok=True)
            logger.info("runner_stopped_web", extra={"pid": pid})
            return jsonify({"stopped": True, "pid": pid})
        except Exception as exc:
            logger.error("runner_stop_failed", extra={"error": str(exc)})
            return jsonify({"stopped": False, "reason": str(exc)}), 500

    @app.route("/api/config/capital", methods=["POST"])
    def api_config_capital():
        """Update total_capital in live_config.yaml via JSON POST."""
        data = request.get_json(force=True, silent=True) or {}
        capital = data.get("total_capital")
        if capital is None:
            return jsonify({"error": "total_capital field required"}), 400
        try:
            capital = float(capital)
            if capital <= 0:
                return jsonify({"error": "total_capital must be positive"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "total_capital must be a number"}), 400

        success = _update_config_capital(capital)
        if success:
            return jsonify({"updated": True, "total_capital": capital})
        return jsonify({"error": "Failed to update config"}), 500

    @app.route("/api/config/lots", methods=["POST"])
    def api_config_lots():
        """Update default_lots in live_config.yaml via JSON POST."""
        data = request.get_json(force=True, silent=True) or {}
        lots = data.get("default_lots")
        if lots is None:
            return jsonify({"error": "default_lots field required"}), 400
        try:
            lots = int(lots)
            if lots < 0:
                return jsonify({"error": "default_lots must be 0 or positive"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "default_lots must be an integer"}), 400

        success = _update_config_lots(lots)
        if success:
            return jsonify({"updated": True, "default_lots": lots})
        return jsonify({"error": "Failed to update config"}), 500

    @app.route("/reports")
    def reports_view():
        """Render the reports page with shift history and premium summary."""
        data = _load_reports_data()
        return render_template(
            "reports.html",
            kill_switch_active=_KILL_SWITCH_FLAG.exists(),
            now=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            positions=data["positions"],
            shift_events=data["shift_events"][:50],
            premium_summary=data["premium_summary"],
            total_shifts=data["total_shifts"],
            total_positions=data["total_positions"],
            total_net_premium=data["total_net_premium"],
        )

    @app.route("/api/reports/download")
    def api_reports_download():
        """Generate and return an Excel report with 4 sheets."""
        import io
        try:
            import openpyxl
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            return jsonify({"error": "openpyxl not installed — run: pip install openpyxl"}), 500

        from flask import send_file

        data = _load_reports_data()
        wb = openpyxl.Workbook()

        header_fill = PatternFill(start_color="1a3a5c", end_color="1a3a5c", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF")

        def _style_sheet(ws):
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")
            for col_idx, col in enumerate(ws.columns, 1):
                max_len = max((len(str(cell.value or "")) for cell in col), default=0)
                ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 2, 12), 40)

        # ── Sheet 1: Trade Summary ─────────────────────────────────────────
        ws1 = wb.active
        ws1.title = "Trade Summary"
        ws1.append([
            "Symbol", "Entry Date", "Expiry", "Lots", "Lot Size",
            "Credit/Share", "Total Credit (INR)", "Short Put", "Short Call",
            "State", "Final P&L (INR)", "Shift Count",
        ])
        shift_counts_map: Dict[str, int] = {}
        for ev in data["shift_events"]:
            sym = ev.get("symbol", "")
            shift_counts_map[sym] = shift_counts_map.get(sym, 0) + 1

        for pos in data["positions"]:
            strikes = pos.get("strikes", {}) or {}
            sym = pos.get("symbol", "")
            entry_t = pos.get("entry_time", "") or ""
            ws1.append([
                sym,
                entry_t[:10] if entry_t else "",
                str(pos.get("expiry", "")),
                int(pos.get("lots", 0) or 0),
                int(pos.get("lot_size", 0) or 0),
                round(float(pos.get("initial_credit", 0) or 0), 4),
                round(
                    float(pos.get("initial_credit", 0) or 0)
                    * int(pos.get("lots", 1) or 1)
                    * int(pos.get("lot_size", 1) or 1),
                    2,
                ),
                float(strikes.get("short_put", 0) or 0),
                float(strikes.get("short_call", 0) or 0),
                pos.get("state", ""),
                round(float(pos.get("final_pnl", pos.get("pnl", 0)) or 0), 2),
                shift_counts_map.get(sym, 0),
            ])
        _style_sheet(ws1)

        # ── Sheet 2: Shift Events ──────────────────────────────────────────
        ws2 = wb.create_sheet("Shift Events")
        ws2.append([
            "Timestamp", "Symbol", "Direction", "Spot",
            "Short Strike", "Breach Level", "% Away from Strike",
        ])
        for ev in data["shift_events"]:
            direction = ev.get("direction", "")
            short_strike = (
                ev.get("short_put_strike") if direction == "PUT"
                else ev.get("short_call_strike", "")
            )
            ws2.append([
                ev.get("ts", ""),
                ev.get("symbol", ""),
                direction,
                ev.get("spot", ""),
                short_strike,
                ev.get("breach_level", ""),
                ev.get("pct_away_from_strike", ""),
            ])
        _style_sheet(ws2)

        # ── Sheet 3: Orders Detail ─────────────────────────────────────────
        ws3 = wb.create_sheet("Orders Detail")
        ws3.append([
            "Order ID", "Strategy ID", "Trading Symbol", "Side",
            "Qty", "Limit Price", "Avg Fill Price", "Status",
            "Submitted At", "Filled Qty",
        ])
        for order in _load_orders_from_db():
            ws3.append([
                order.get("order_id", ""),
                order.get("strategy_id", ""),
                order.get("trading_symbol", ""),
                order.get("side", ""),
                int(order.get("quantity", 0) or 0),
                round(float(order.get("limit_price", 0) or 0), 4),
                round(float(order.get("average_fill_price", 0) or 0), 4),
                order.get("status", ""),
                str(order.get("submitted_at", "")),
                int(order.get("filled_quantity", 0) or 0),
            ])
        _style_sheet(ws3)

        # ── Sheet 4: Net Premium Summary ───────────────────────────────────
        ws4 = wb.create_sheet("Net Premium Summary")
        ws4.append([
            "Symbol", "Total Trades", "Gross Credit (INR)",
            "Shift Count", "Net Premium (INR)",
        ])
        for row in data["premium_summary"]:
            ws4.append([
                row["symbol"],
                row["trade_count"],
                row["total_credit"],
                row["shift_count"],
                row["net_premium"],
            ])
        _style_sheet(ws4)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        filename = f"condor_report_{date.today().isoformat()}.xlsx"
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )

    return app


# ── Data access helpers ────────────────────────────────────────────────────


def _db_connect() -> Optional[sqlite3.Connection]:
    """
    Open a read-only connection to the live state SQLite database.

    Returns:
        sqlite3.Connection or None if file does not exist.
    """
    if not _DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


_JSON_COLUMNS = {"strikes", "short_put", "long_put", "short_call", "long_call", "broker_order_ids"}


def _get_shift_stats_by_symbol() -> Dict[str, Dict]:
    """All-time per-symbol shift count and cumulative shift P&L from JSONL logs."""
    events = _load_log_events(event_type="SHIFT_COMPLETE", limit=2000)
    stats: Dict[str, Dict] = {}
    for ev in events:
        sym = ev.get("symbol", "UNKNOWN")
        if sym not in stats:
            stats[sym] = {"total_shifts": 0, "shift_pnl": 0.0}
        stats[sym]["total_shifts"] += 1
        stats[sym]["shift_pnl"] = round(
            stats[sym]["shift_pnl"] + float(ev.get("shift_pnl_total", 0) or 0), 2
        )
    return stats


def _compute_position_metrics(
    positions: List[Dict[str, Any]],
    cfg_raw: Optional[Dict] = None,
) -> Dict[str, Dict]:
    """
    Compute static (non-live) per-position metrics for the dashboard table.

    Returns a dict keyed by symbol with keys:
      put_width, call_width, spread_width, net_premium_total, max_profit,
      max_loss, cash_margin, net_premium_pct, rr_strategy, rr_wait,
      profit_if_wait, premium_pct_target, qty
    """
    if cfg_raw is None:
        try:
            cfg_raw = yaml.safe_load(_load_config_yaml()) or {}
        except Exception:
            cfg_raw = {}
    stocks_map = {
        s["symbol"]: s
        for s in cfg_raw.get("stocks", [])
        if isinstance(s, dict) and "symbol" in s
    }
    metrics: Dict[str, Dict] = {}
    for pos in positions:
        sym = pos.get("symbol", "")
        strikes = pos.get("strikes", {}) or {}
        lots = int(pos.get("lots", 1) or 1)
        lot_size = int(pos.get("lot_size", 1) or 1)
        qty = lots * lot_size
        initial_credit = float(pos.get("initial_credit", 0) or 0)

        sp = float(strikes.get("short_put") or 0)
        lp = float(strikes.get("long_put") or 0)
        sc = float(strikes.get("short_call") or 0)
        lc = float(strikes.get("long_call") or 0)

        put_width = round(sp - lp, 2) if (sp and lp) else 0.0
        call_width = round(lc - sc, 2) if (lc and sc) else 0.0
        spread_width = max(put_width, call_width)  # use wider wing

        stock_cfg = stocks_map.get(sym, {})
        premium_pct_target = float(stock_cfg.get("premium_pct", 0.4))

        net_premium_total = round(initial_credit * qty, 2)
        max_profit = net_premium_total
        max_loss_ps = max(spread_width - initial_credit, 0.0)
        max_loss = round(max_loss_ps * qty, 2)
        cash_margin = max_loss

        net_premium_pct = (
            round(initial_credit / spread_width * 100, 2) if spread_width > 0 else None
        )
        # R/R: how many ₹ at risk for every ₹ of max profit
        rr_strategy = (
            round(max_loss_ps / initial_credit, 2) if initial_credit > 0 else None
        )
        profit_if_wait = round(
            initial_credit * (1.0 - premium_pct_target) * qty, 2
        )
        rr_wait = (
            round(max_loss / profit_if_wait, 2) if profit_if_wait > 0 else None
        )

        metrics[sym] = {
            "put_width": put_width,
            "call_width": call_width,
            "spread_width": spread_width,
            "net_premium_total": net_premium_total,
            "max_profit": max_profit,
            "max_loss": max_loss,
            "cash_margin": cash_margin,
            "net_premium_pct": net_premium_pct,
            "rr_strategy": rr_strategy,
            "rr_wait": rr_wait,
            "profit_if_wait": profit_if_wait,
            "premium_pct_target": premium_pct_target,
            "qty": qty,
        }
    return metrics


def _load_positions_from_db() -> List[Dict[str, Any]]:
    """
    Load all positions from the SQLite database.

    Expects a table named 'positions' with columns matching CondorPosition fields.
    Returns an empty list if the database or table does not exist.

    Returns:
        List of position dicts.
    """
    conn = _db_connect()
    if conn is None:
        return _load_positions_from_logs()
    try:
        cur = conn.execute(
            "SELECT * FROM positions ORDER BY entry_time DESC LIMIT 100"
        )
        rows = []
        for r in cur.fetchall():
            row = dict(r)
            # Parse JSON columns so templates receive dicts/lists, not strings
            for col in _JSON_COLUMNS:
                val = row.get(col)
                if isinstance(val, str) and val:
                    try:
                        row[col] = json.loads(val)
                    except (json.JSONDecodeError, ValueError):
                        pass
            rows.append(row)
        conn.close()
        return rows
    except sqlite3.OperationalError:
        conn.close()
        return _load_positions_from_logs()


def _load_positions_from_logs() -> List[Dict[str, Any]]:
    """
    Derive open positions from today's ENTRY_SUCCESS log events.

    Used as a fallback when the SQLite database is unavailable.

    Returns:
        List of position dicts synthesised from log events.
    """
    events = _load_log_events(event_type="ENTRY_SUCCESS", limit=500)
    exits = {
        e["symbol"]
        for e in _load_log_events(event_type="EXIT_SUCCESS", limit=500)
    }
    positions = []
    seen: set[str] = set()
    for ev in events:
        sym = ev.get("symbol", "")
        if sym in exits or sym in seen:
            continue
        seen.add(sym)
        positions.append({
            "symbol": sym,
            "state": "OPEN",
            "spot_at_entry": ev.get("spot", 0),
            "lots": ev.get("lots", 0),
            "initial_credit": ev.get("credit", 0),
            "entry_time": ev.get("ts", ""),
            "strikes": ev.get("strikes", {}),
            "pnl": 0.0,
        })
    return positions


def _compute_pnl_summary(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute aggregate P&L across all positions.

    Args:
        positions: List of position dicts.

    Returns:
        Dict with keys: total_pnl, open_count, closed_count.
    """
    closed_pnl = sum(
        float(p.get("final_pnl", p.get("pnl", 0)) or 0)
        for p in positions
        if p.get("state") in ("CLOSED", "EXIT_COMPLETE")
    )
    open_unrealised = sum(
        float(p.get("unrealised_pnl", p.get("pnl", 0)) or 0)
        for p in positions
        if p.get("state") == "OPEN"
    )
    return {
        "total_pnl": round(closed_pnl + open_unrealised, 2),
        "open_count": sum(1 for p in positions if p.get("state") == "OPEN"),
        "closed_count": sum(1 for p in positions if p.get("state") not in ("OPEN", "SHIFTING")),
    }


def _compute_win_rate() -> float:
    """
    Compute win rate from today's closed exits.

    Returns:
        Win rate as a percentage (0–100), or 0 if no closed trades.
    """
    events = _load_log_events(event_type="EXIT_SUCCESS", limit=500)
    if not events:
        return 0.0
    wins = sum(1 for e in events if float(e.get("pnl", 0)) >= 0)
    return round(wins / len(events) * 100, 1)


def _count_today_shifts() -> int:
    """
    Count the number of successfully completed shifts today.

    Returns:
        Integer count of SHIFT_COMPLETE events today.
    """
    events = _load_log_events(event_type="SHIFT_COMPLETE", limit=200)
    today = date.today().isoformat()
    return sum(1 for e in events if e.get("ts", "").startswith(today))


def _load_daily_pnl() -> Dict[str, float]:
    """
    Load per-symbol P&L from today's exit events.

    Returns:
        Dict mapping symbol → net P&L.
    """
    events = _load_log_events(event_type="EXIT_SUCCESS", limit=500)
    today = date.today().isoformat()
    pnl: Dict[str, float] = {}
    for ev in events:
        if not ev.get("ts", "").startswith(today):
            continue
        sym = ev.get("symbol", "UNKNOWN")
        pnl[sym] = pnl.get(sym, 0.0) + float(ev.get("pnl", 0))
    return pnl


def _load_log_events(
    symbol: Optional[str] = None,
    event_type: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """
    Load structured log events from all .jsonl files in the log directory.

    Events are returned newest-first.

    Args:
        symbol:     Filter by this symbol (or None for all).
        event_type: Filter by this EventType value (or None for all).
        level:      Filter by log level (or None for all).
        limit:      Maximum number of events to return.

    Returns:
        List of event dicts, newest first.
    """
    events: List[Dict[str, Any]] = []
    log_files = sorted(
        glob.glob(str(_LOG_DIR / "*.jsonl")),
        reverse=True,
    )

    for log_file in log_files:
        try:
            with open(log_file, encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if symbol and obj.get("symbol", "") != symbol:
                continue
            if event_type and obj.get("event_type", "") != event_type:
                continue
            if level and obj.get("level", "").upper() != level.upper():
                continue

            events.append(obj)
            if len(events) >= limit:
                return events

    return events


_ALERT_EVENT_TYPES = {
    "SHIFT_FAILED",
    "ORDER_REJECTED",
    "POSITION_MISMATCH",
    "KILL_SWITCH_ACTIVATED",
    "CAPITAL_LIMIT_BREACHED",
    "DAILY_LOSS_LIMIT_BREACHED",
    "ENTRY_FAILED",
    "EXIT_FAILED",
    "ERROR",
}

_ALERT_DESCRIPTIONS = {
    "SHIFT_FAILED":              "Shift execution failed mid-way",
    "ORDER_REJECTED":            "Broker rejected an order",
    "POSITION_MISMATCH":         "Live position doesn't match expected state",
    "KILL_SWITCH_ACTIVATED":     "Kill switch was activated",
    "CAPITAL_LIMIT_BREACHED":    "Capital limit breached",
    "DAILY_LOSS_LIMIT_BREACHED": "Daily loss limit breached",
    "ENTRY_FAILED":              "Strategy entry failed",
    "EXIT_FAILED":               "Exit order failed",
    "ERROR":                     "Unhandled system error",
}


def _load_alerts(limit: int = 200) -> Dict[str, Any]:
    """
    Load actionable alert events from log files.

    Returns a dict with:
      - alerts: list of event dicts (newest first, up to limit)
      - counts: dict mapping category → count
    """
    raw = _load_log_events(limit=2000)
    alerts: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {k: 0 for k in _ALERT_EVENT_TYPES}
    counts["OTHER_ERROR"] = 0

    for ev in raw:
        etype = ev.get("event_type", "")
        level = ev.get("level", "")
        is_alert_type = etype in _ALERT_EVENT_TYPES
        is_error_level = level in ("ERROR", "CRITICAL") and not is_alert_type

        if is_alert_type:
            ev = dict(ev)
            ev.setdefault("alert_description", _ALERT_DESCRIPTIONS.get(etype, etype))
            alerts.append(ev)
            counts[etype] = counts.get(etype, 0) + 1
            if len(alerts) >= limit:
                break
        elif is_error_level:
            ev = dict(ev)
            ev.setdefault("alert_description", "Unexpected error in system")
            alerts.append(ev)
            counts["OTHER_ERROR"] += 1
            if len(alerts) >= limit:
                break

    counts["total"] = len(alerts)
    return {"alerts": alerts, "counts": counts}


def _count_active_alerts() -> int:
    """Return the count of alert-worthy events for the nav badge."""
    try:
        data = _load_alerts(limit=500)
        return data["counts"].get("total", 0)
    except Exception:
        return 0


def _get_log_symbols() -> List[str]:
    """
    Return the set of unique symbols seen in today's log files.

    Returns:
        Sorted list of symbol strings.
    """
    events = _load_log_events(limit=2000)
    return sorted({e.get("symbol", "") for e in events if e.get("symbol")})


def _get_log_event_types() -> List[str]:
    """
    Return the set of unique event types seen in today's log files.

    Returns:
        Sorted list of event type strings.
    """
    events = _load_log_events(limit=2000)
    return sorted({e.get("event_type", "") for e in events if e.get("event_type")})


def _load_config_yaml() -> str:
    """
    Load the live_config.yaml file as a raw string for the config editor.

    Returns:
        YAML content string, or a placeholder if file not found.
    """
    if _CONFIG_PATH.exists():
        try:
            return _CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            pass
    return "# config/live_config.yaml not found\n"


def _read_heartbeat() -> Optional[str]:
    """
    Read the last heartbeat timestamp from the heartbeat file.

    Returns:
        ISO timestamp string or None if file not found.
    """
    if _HEARTBEAT_FILE.exists():
        try:
            return _HEARTBEAT_FILE.read_text().strip()
        except OSError:
            pass
    return None


_CREDENTIALS_PATH = Path("config/credentials.env")


def _load_credentials() -> dict:
    """Load credentials.env as a key-value dict."""
    if not _CREDENTIALS_PATH.exists():
        return {}
    result = {}
    for line in _CREDENTIALS_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _save_credential(key: str, value: str) -> bool:
    """Update or insert a single key in credentials.env."""
    try:
        existing = _CREDENTIALS_PATH.read_text() if _CREDENTIALS_PATH.exists() else ""
        import re as _re
        pattern = rf"^{_re.escape(key)}\s*=.*$"
        new_line = f"{key}={value}"
        if _re.search(pattern, existing, flags=_re.MULTILINE):
            updated = _re.sub(pattern, new_line, existing, flags=_re.MULTILINE)
        else:
            updated = existing.rstrip("\n") + f"\n{new_line}\n"
        _CREDENTIALS_PATH.write_text(updated)
        return True
    except Exception as exc:
        logger.error("save_credential_failed", extra={"key": key, "error": str(exc)})
        return False


def _build_kite_login_url(api_key: str) -> str:
    """Return the Kite Connect OAuth login URL for the given API key."""
    if not api_key:
        return ""
    return f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"


def _runner_is_alive() -> tuple[bool, int]:
    """
    Check whether the trading runner process is alive using the PID file.

    Returns:
        (is_alive: bool, pid: int)  — pid is 0 if not running.
    """
    if not _RUNNER_PID_FILE.exists():
        return False, 0
    try:
        pid = int(_RUNNER_PID_FILE.read_text().strip())
        os.kill(pid, 0)   # signal 0 = existence check, no actual signal sent
        return True, pid
    except (ValueError, ProcessLookupError, PermissionError):
        _RUNNER_PID_FILE.unlink(missing_ok=True)
        return False, 0


# ── Application instance ───────────────────────────────────────────────────


def _get_read_broker():
    """
    Return a read-only ZerodhaBroker using credentials from env / credentials.env.
    Only used for quote fetching — never places orders.
    Raises ValueError if credentials are missing.
    """
    # Always read credentials.env first so a token refresh is picked up by all
    # gunicorn workers immediately (os.environ is per-process and may be stale).
    creds = _load_credentials()
    api_key = creds.get("ZERODHA_API_KEY", "") or os.environ.get("ZERODHA_API_KEY", "")
    access_token = creds.get("ZERODHA_ACCESS_TOKEN", "") or os.environ.get("ZERODHA_ACCESS_TOKEN", "")
    if not api_key or not access_token:
        raise ValueError("ZERODHA_API_KEY or ZERODHA_ACCESS_TOKEN not set")
    from brokers.zerodha import ZerodhaBroker
    return ZerodhaBroker(api_key=api_key, access_token=access_token)


def _update_config_lots(new_lots: int) -> bool:
    """
    Update default_lots in the strategy section of live_config.yaml.

    If the key doesn't exist yet it is appended after entry_end.

    Args:
        new_lots: New default_lots value (0 = auto-size from capital).

    Returns:
        True on success, False on error.
    """
    try:
        if not _CONFIG_PATH.exists():
            return False
        import re
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
        if re.search(r"default_lots\s*:", raw):
            updated = re.sub(
                r"(default_lots\s*:\s*)\d+",
                lambda m: m.group(1) + str(new_lots),
                raw,
            )
        else:
            # Append after entry_end line
            updated = re.sub(
                r'(entry_end\s*:.*)',
                lambda m: m.group(1) + f"\n  default_lots: {new_lots}           # Lots per trade for all stocks (0 = auto-size from capital)",
                raw,
            )
        _CONFIG_PATH.write_text(updated, encoding="utf-8")
        logger.info("config_lots_updated", extra={"default_lots": new_lots})
        return True
    except Exception as exc:
        logger.error("config_lots_update_failed", extra={"error": str(exc)})
        return False


def _update_config_capital(new_capital: float) -> bool:
    """
    Update total_capital in live_config.yaml.

    Args:
        new_capital: New total capital value in INR.

    Returns:
        True on success, False on error.
    """
    try:
        if _CONFIG_PATH.exists():
            raw = _CONFIG_PATH.read_text(encoding="utf-8")
        else:
            return False
        import re
        # Replace the total_capital line
        updated = re.sub(
            r"(total_capital\s*:\s*)\d+",
            lambda m: m.group(1) + str(int(new_capital)),
            raw,
        )
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(updated, encoding="utf-8")
        logger.info("config_capital_updated", extra={"new_capital": new_capital})
        return True
    except Exception as exc:
        logger.error("config_capital_update_failed", extra={"error": str(exc)})
        return False


def _load_orders_from_db() -> List[Dict[str, Any]]:
    """
    Load orders from the live state database.

    Returns:
        List of order dicts, or empty list if the table does not exist.
    """
    conn = _db_connect()
    if conn is None:
        return []
    try:
        cur = conn.execute(
            "SELECT * FROM orders ORDER BY submitted_at DESC LIMIT 500"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except sqlite3.OperationalError:
        conn.close()
        return []


def _load_reports_data() -> Dict[str, Any]:
    """
    Aggregate all data needed for the reports page and Excel download.

    Returns:
        Dict with keys: positions, shift_events, premium_summary,
        total_shifts, total_positions, total_net_premium.
    """
    positions = _load_positions_from_db()
    shift_events = _load_log_events(event_type="SHIFT_COMPLETE", limit=2000)

    # Per-symbol: shift count and cumulative shift P&L from pricing data
    shift_counts: Dict[str, int] = {}
    shift_pnl_by_sym: Dict[str, float] = {}
    for ev in shift_events:
        sym = ev.get("symbol", "UNKNOWN")
        shift_counts[sym] = shift_counts.get(sym, 0) + 1
        shift_pnl_by_sym[sym] = round(
            shift_pnl_by_sym.get(sym, 0.0) + float(ev.get("shift_pnl_total", 0) or 0),
            2,
        )

    # Net premium summary per symbol
    seen: Dict[str, Dict[str, Any]] = {}
    for pos in positions:
        sym = pos.get("symbol", "UNKNOWN")
        credit_per_share = float(pos.get("initial_credit", 0) or 0)
        lots = int(pos.get("lots", 1) or 1)
        lot_size = int(pos.get("lot_size", 1) or 1)
        total_credit = round(credit_per_share * lots * lot_size, 2)
        if sym not in seen:
            seen[sym] = {"trade_count": 0, "total_credit": 0.0}
        seen[sym]["trade_count"] += 1
        seen[sym]["total_credit"] = round(seen[sym]["total_credit"] + total_credit, 2)

    premium_summary = []
    for sym, agg in sorted(seen.items()):
        shifts = shift_counts.get(sym, 0)
        shift_pnl = shift_pnl_by_sym.get(sym, 0.0)
        gross_credit = agg["total_credit"]
        net_premium = round(gross_credit + shift_pnl, 2)
        premium_summary.append({
            "symbol": sym,
            "trade_count": agg["trade_count"],
            "total_credit": gross_credit,
            "shift_count": shifts,
            "shift_pnl": shift_pnl,
            "net_premium": net_premium,
        })

    total_net_premium = round(sum(r["net_premium"] for r in premium_summary), 2)

    return {
        "positions": positions,
        "shift_events": shift_events,
        "premium_summary": premium_summary,
        "total_shifts": len(shift_events),
        "total_positions": len(positions),
        "total_net_premium": total_net_premium,
    }


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=5000, debug=False)
