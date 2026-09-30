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
        """Inject runner_running and trading_mode into every template automatically."""
        running, _ = _runner_is_alive()
        try:
            cfg_raw = yaml.safe_load(_load_config_yaml()) or {}
            trading_mode = str(cfg_raw.get("mode", "paper")).lower()
        except Exception:
            trading_mode = "paper"
        return {"runner_running": running, "trading_mode": trading_mode}

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
        return render_template(
            "dashboard.html",
            positions=positions,
            total_pnl=pnl_summary.get("total_pnl", 0.0),
            open_count=sum(1 for p in positions if p.get("state") == "OPEN"),
            win_rate=win_rate,
            shifts_today=shifts_today,
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
        return render_template(
            "positions.html",
            positions=positions,
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
            # Use the narrower wing as the representative spread width
            spread_width = (
                min(put_width, call_width) if (put_width and call_width)
                else max(put_width, call_width)
            )

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
    Count the number of shift events logged today.

    Returns:
        Integer count of SHIFT_TRIGGERED events today.
    """
    events = _load_log_events(event_type="SHIFT_TRIGGERED", limit=200)
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
    api_key = os.environ.get("ZERODHA_API_KEY", "") or _load_credentials().get("ZERODHA_API_KEY", "")
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "") or _load_credentials().get("ZERODHA_ACCESS_TOKEN", "")
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


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=5000, debug=False)
