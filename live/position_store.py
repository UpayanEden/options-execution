"""
Persistent position store for the live condor runner.

Writes CondorPosition state to data/live_state.db (SQLite) so positions
survive process restarts.  The web dashboard reads from this same file.

Table schema is intentionally flat — one row per CondorPosition.
Leg details (strikes, instruments) are stored as JSON columns.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

_DB_PATH = Path("data/live_state.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS positions (
    symbol           TEXT    NOT NULL,
    state            TEXT    NOT NULL DEFAULT 'OPEN',
    expiry           TEXT    NOT NULL,
    spot_at_entry    REAL    NOT NULL,
    lots             INTEGER NOT NULL,
    lot_size         INTEGER NOT NULL,
    initial_credit   REAL    NOT NULL DEFAULT 0.0,
    put_dist_pct     REAL    NOT NULL DEFAULT 5.0,
    call_dist_pct    REAL    NOT NULL DEFAULT 5.0,
    entry_time       TEXT    NOT NULL,
    shift_count      INTEGER NOT NULL DEFAULT 0,
    pnl              REAL             DEFAULT 0.0,
    final_pnl        REAL,
    -- leg instruments as JSON {instrument_token, trading_symbol, strike}
    short_put        TEXT,
    long_put         TEXT,
    short_call       TEXT,
    long_call        TEXT,
    -- flattened strikes dict for dashboard display
    strikes          TEXT,
    broker_order_ids TEXT,
    PRIMARY KEY (symbol, entry_time)
);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_SQL)
    # Migrate: add dist_pct columns if they don't exist yet (existing DBs)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    if "put_dist_pct" not in existing:
        conn.execute("ALTER TABLE positions ADD COLUMN put_dist_pct REAL NOT NULL DEFAULT 5.0")
    if "call_dist_pct" not in existing:
        conn.execute("ALTER TABLE positions ADD COLUMN call_dist_pct REAL NOT NULL DEFAULT 5.0")
    conn.commit()
    return conn


def _pos_to_row(pos: "CondorPosition") -> dict:  # noqa: F821
    """Serialise a CondorPosition to a flat dict for the DB."""

    def _inst(inst: "ResolvedInstrument") -> str:  # noqa: F821
        return json.dumps({
            "instrument_token": inst.instrument_token,
            "trading_symbol":   inst.trading_symbol,
            "strike":           float(inst.strike),
            "option_type":      str(inst.option_type.value if hasattr(inst.option_type, "value") else inst.option_type),
        })

    strikes = json.dumps({
        "short_put":  float(pos.short_put.strike),
        "long_put":   float(pos.long_put.strike),
        "short_call": float(pos.short_call.strike),
        "long_call":  float(pos.long_call.strike),
    })

    return {
        "symbol":           pos.symbol,
        "state":            pos.state,
        "expiry":           str(pos.expiry),
        "spot_at_entry":    pos.spot_at_entry,
        "lots":             pos.lots,
        "lot_size":         pos.lot_size,
        "initial_credit":   pos.initial_credit,
        "put_dist_pct":     pos.put_dist_pct,
        "call_dist_pct":    pos.call_dist_pct,
        "entry_time":       pos.entry_time.isoformat(),
        "shift_count":      pos.shift_count,
        "pnl":              0.0,
        "short_put":        _inst(pos.short_put),
        "long_put":         _inst(pos.long_put),
        "short_call":       _inst(pos.short_call),
        "long_call":        _inst(pos.long_call),
        "strikes":          strikes,
        "broker_order_ids": json.dumps(pos.broker_order_ids),
    }


class PositionStore:
    """
    Thread-safe SQLite-backed store for CondorPositions.

    Usage::

        store = PositionStore()
        store.upsert(pos)          # after entry or shift
        store.update_state(pos)    # after state change (SHIFTING → OPEN, CLOSED)
        store.close_position(pos, pnl=1234.0)  # after exit
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        global _DB_PATH
        if db_path:
            _DB_PATH = db_path
        self._conn = _connect()
        logger.info("position_store_ready", extra={"path": str(_DB_PATH)})

    def upsert(self, pos: "CondorPosition") -> None:  # noqa: F821
        """
        Insert or replace a CondorPosition row.
        Called after successful entry and after each shift.
        """
        row = _pos_to_row(pos)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        sql = f"INSERT OR REPLACE INTO positions ({cols}) VALUES ({placeholders})"
        try:
            self._conn.execute(sql, row)
            self._conn.commit()
            logger.debug("position_upserted", extra={"symbol": pos.symbol, "state": pos.state})
        except Exception as exc:
            logger.error(
                "position_store_upsert_failed: %s — %s",
                pos.symbol, exc, exc_info=True,
            )

    def update_state(self, pos: "CondorPosition") -> None:  # noqa: F821
        """Update state and shift_count for an existing position row."""
        try:
            self._conn.execute(
                "UPDATE positions SET state=?, shift_count=?, "
                "short_put=?, long_put=?, short_call=?, long_call=?, strikes=? "
                "WHERE symbol=? AND entry_time=?",
                (
                    pos.state,
                    pos.shift_count,
                    json.dumps({"instrument_token": pos.short_put.instrument_token,
                                "trading_symbol": pos.short_put.trading_symbol,
                                "strike": float(pos.short_put.strike)}),
                    json.dumps({"instrument_token": pos.long_put.instrument_token,
                                "trading_symbol": pos.long_put.trading_symbol,
                                "strike": float(pos.long_put.strike)}),
                    json.dumps({"instrument_token": pos.short_call.instrument_token,
                                "trading_symbol": pos.short_call.trading_symbol,
                                "strike": float(pos.short_call.strike)}),
                    json.dumps({"instrument_token": pos.long_call.instrument_token,
                                "trading_symbol": pos.long_call.trading_symbol,
                                "strike": float(pos.long_call.strike)}),
                    json.dumps({
                        "short_put":  float(pos.short_put.strike),
                        "long_put":   float(pos.long_put.strike),
                        "short_call": float(pos.short_call.strike),
                        "long_call":  float(pos.long_call.strike),
                    }),
                    pos.symbol,
                    pos.entry_time.isoformat(),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("position_store_update_failed", extra={
                "symbol": pos.symbol, "error": str(exc)
            })

    def close_position(self, pos: "CondorPosition", pnl: float = 0.0) -> None:  # noqa: F821
        """Mark a position as CLOSED and record final P&L."""
        try:
            self._conn.execute(
                "UPDATE positions SET state='CLOSED', final_pnl=?, pnl=? "
                "WHERE symbol=? AND entry_time=?",
                (round(pnl, 2), round(pnl, 2), pos.symbol, pos.entry_time.isoformat()),
            )
            self._conn.commit()
            logger.info("position_closed_in_store", extra={
                "symbol": pos.symbol, "pnl": round(pnl, 2)
            })
        except sqlite3.Error as exc:
            logger.error("position_store_close_failed", extra={
                "symbol": pos.symbol, "error": str(exc)
            })

    def load_open_positions(self) -> List[dict]:
        """
        Return all rows with state='OPEN' or 'SHIFTING'.
        Used for session recovery after a restart.
        """
        try:
            cur = self._conn.execute(
                "SELECT * FROM positions WHERE state IN ('OPEN', 'SHIFTING') "
                "ORDER BY entry_time DESC"
            )
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            logger.error("position_store_load_failed", extra={"error": str(exc)})
            return []

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass
