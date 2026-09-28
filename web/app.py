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
import sqlite3
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

    # ── Register routes ────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        """Render the main dashboard with summary cards and open positions."""
        positions = _load_positions_from_db()
        pnl_summary = _compute_pnl_summary(positions)
        win_rate = _compute_win_rate()
        shifts_today = _count_today_shifts()
        return render_template(
            "dashboard.html",
            positions=positions,
            total_pnl=pnl_summary.get("total_pnl", 0.0),
            open_count=sum(1 for p in positions if p.get("state") == "OPEN"),
            win_rate=win_rate,
            shifts_today=shifts_today,
            kill_switch_active=_KILL_SWITCH_FLAG.exists(),
            now=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
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
        rows = [dict(r) for r in cur.fetchall()]
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


# ── Application instance ───────────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=5000, debug=False)
