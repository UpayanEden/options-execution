"""
SQLite database setup using SQLAlchemy Core.
Schema is defined here. Clean interface for future PostgreSQL migration.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, MetaData, Numeric, String, Table,
    Text, create_engine, text,
)
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)
metadata = MetaData()

# ── Table definitions ──────────────────────────────────────────────────────

strategies = Table(
    "strategies", metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("underlying", String, nullable=False),
    Column("exchange", String, nullable=False),
    Column("expiry", String, nullable=False),
    Column("lots", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("trading_mode", String, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("entry_credit", Numeric(12, 4)),
    Column("exit_debit", Numeric(12, 4)),
    Column("final_pnl", Numeric(12, 4)),
    Column("notes", Text),
)

strategy_legs = Table(
    "strategy_legs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy_id", String, nullable=False),
    Column("leg_index", Integer, nullable=False),
    Column("side", String, nullable=False),
    Column("option_type", String, nullable=False),
    Column("strike", Numeric(10, 2), nullable=False),
    Column("trading_symbol", String),
    Column("instrument_token", Integer),
    Column("lot_size", Integer),
    Column("quantity", Integer),
)

quote_snapshots = Table(
    "quote_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy_id", String),
    Column("instrument_token", Integer, nullable=False),
    Column("trading_symbol", String, nullable=False),
    Column("snapshot_time", DateTime, nullable=False),
    Column("ltp", Numeric(10, 4)),
    Column("bid", Numeric(10, 4)),
    Column("bid_qty", Integer),
    Column("ask", Numeric(10, 4)),
    Column("ask_qty", Integer),
    Column("volume", Integer),
    Column("oi", Integer),
    Column("spread_pct", Numeric(8, 4)),
)

execution_decisions = Table(
    "execution_decisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy_id", String, nullable=False),
    Column("decision_time", DateTime, nullable=False),
    Column("decision", String, nullable=False),
    Column("net_credit_available", Numeric(10, 4)),
    Column("min_credit_required", Numeric(10, 4)),
    Column("reason", Text),
)

orders = Table(
    "orders", metadata,
    Column("order_id", String, primary_key=True),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("broker_order_id", String),
    Column("strategy_id", String, nullable=False),
    Column("leg_index", Integer, nullable=False),
    Column("trading_symbol", String, nullable=False),
    Column("instrument_token", Integer, nullable=False),
    Column("side", String, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("limit_price", Numeric(10, 4), nullable=False),
    Column("status", String, nullable=False),
    Column("filled_quantity", Integer, default=0),
    Column("average_fill_price", Numeric(10, 4)),
    Column("submitted_at", DateTime),
    Column("last_updated_at", DateTime),
    Column("reprice_count", Integer, default=0),
    Column("initial_bid", Numeric(10, 4)),
    Column("initial_ask", Numeric(10, 4)),
    Column("notes", Text),
)

order_events = Table(
    "order_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", String, nullable=False),
    Column("event_time", DateTime, nullable=False),
    Column("event_type", String, nullable=False),
    Column("old_status", String),
    Column("new_status", String),
    Column("price", Numeric(10, 4)),
    Column("quantity", Integer),
    Column("message", Text),
)

fills = Table(
    "fills", metadata,
    Column("fill_id", String, primary_key=True),
    Column("order_id", String, nullable=False),
    Column("broker_fill_id", String),
    Column("quantity", Integer, nullable=False),
    Column("price", Numeric(10, 4), nullable=False),
    Column("timestamp", DateTime, nullable=False),
)

positions = Table(
    "positions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy_id", String, nullable=False),
    Column("leg_index", Integer, nullable=False),
    Column("trading_symbol", String, nullable=False),
    Column("instrument_token", Integer, nullable=False),
    Column("side", String, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("average_entry_price", Numeric(10, 4), nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("leg_expiry", String),   # YYYY-MM-DD; nullable — only set for calendar spreads
)

risk_snapshots = Table(
    "risk_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy_id", String, nullable=False),
    Column("snapshot_time", DateTime, nullable=False),
    Column("entry_credit_total", Numeric(12, 4)),
    Column("cost_to_close", Numeric(12, 4)),
    Column("unrealized_pnl", Numeric(12, 4)),
    Column("max_profit", Numeric(12, 4)),
    Column("max_loss", Numeric(12, 4)),
    Column("underlying_spot", Numeric(10, 4)),
    Column("distance_to_short_put", Numeric(10, 4)),
    Column("distance_to_short_call", Numeric(10, 4)),
    Column("dte", Integer),
)

pnl_snapshots = Table(
    "pnl_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy_id", String, nullable=False),
    Column("snapshot_time", DateTime, nullable=False),
    Column("realized_pnl", Numeric(12, 4)),
    Column("unrealized_pnl", Numeric(12, 4)),
    Column("entry_credit", Numeric(12, 4)),
    Column("cost_to_close", Numeric(12, 4)),
    Column("pct_of_max_profit", Numeric(8, 4)),
)

alerts_table = Table(
    "alerts", metadata,
    Column("alert_id", String, primary_key=True),
    Column("strategy_id", String),
    Column("level", String, nullable=False),
    Column("event", String, nullable=False),
    Column("message", Text, nullable=False),
    Column("timestamp", DateTime, nullable=False),
    Column("acknowledged", Boolean, default=False),
)

system_events = Table(
    "system_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_time", DateTime, nullable=False),
    Column("event_type", String, nullable=False),
    Column("strategy_id", String),
    Column("details", Text),
)

iv_history = Table(
    "iv_history", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("date", String, nullable=False),          # YYYY-MM-DD
    Column("underlying", String, nullable=False),
    Column("iv_value", Numeric(8, 4), nullable=False),  # decimal fraction e.g. 0.23
    Column("spot", Numeric(10, 2)),
    Column("source", String),                        # "computed" | "configured"
)

entry_signals = Table(
    "entry_signals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("signal_time", DateTime, nullable=False),
    Column("underlying", String, nullable=False),
    Column("composite_score", Numeric(6, 2)),
    Column("iv_rank", Numeric(6, 2)),
    Column("vix_level", Numeric(6, 2)),
    Column("timing_score", Numeric(6, 2)),
    Column("credit", Numeric(10, 4)),
    Column("ror", Numeric(8, 4)),
    Column("grade", String),
    Column("recommendation", String),
    Column("acted_on", Boolean, default=False),
    Column("notes", Text),
)


def create_db_engine(db_path: str = "logs/trading.db") -> Engine:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    metadata.create_all(engine)
    # Incremental migration: add leg_expiry to existing databases that predate this column.
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE positions ADD COLUMN leg_expiry TEXT"))
            conn.commit()
            logger.info("migration_applied", extra={"column": "positions.leg_expiry"})
        except Exception:
            pass  # column already exists — safe to ignore
    logger.info("database_ready", extra={"path": db_path})
    return engine
