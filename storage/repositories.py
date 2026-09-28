"""
Repository layer — CRUD operations for all tables.
All methods take an SQLAlchemy connection or session.
Interface is designed to be portable to PostgreSQL with minimal changes.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, insert, select, update

from execution.models import Alert, Fill, Order, StrategyState
from storage.database import (
    alerts_table,
    execution_decisions,
    fills,
    order_events,
    orders,
    pnl_snapshots,
    positions,
    quote_snapshots,
    risk_snapshots,
    strategies,
    strategy_legs,
    system_events,
)

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(IST)


class StrategyRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def create(self, strategy_id: str, name: str, underlying: str,
               exchange: str, expiry: str, lots: int,
               trading_mode: str) -> None:
        now = _now()
        self._conn.execute(insert(strategies).values(
            id=strategy_id, name=name, underlying=underlying,
            exchange=exchange, expiry=expiry, lots=lots,
            state=StrategyState.CREATED.value,
            trading_mode=trading_mode,
            created_at=now, updated_at=now,
        ))
        self._conn.commit()

    def update_state(self, strategy_id: str, state: StrategyState,
                     notes: Optional[str] = None) -> None:
        vals: dict[str, Any] = {"state": state.value, "updated_at": _now()}
        if notes:
            vals["notes"] = notes
        self._conn.execute(
            update(strategies).where(strategies.c.id == strategy_id).values(**vals)
        )
        self._conn.commit()

    def update_pnl(self, strategy_id: str,
                   entry_credit: Optional[Decimal] = None,
                   exit_debit: Optional[Decimal] = None,
                   final_pnl: Optional[Decimal] = None) -> None:
        vals: dict[str, Any] = {"updated_at": _now()}
        if entry_credit is not None:
            vals["entry_credit"] = entry_credit
        if exit_debit is not None:
            vals["exit_debit"] = exit_debit
        if final_pnl is not None:
            vals["final_pnl"] = final_pnl
        self._conn.execute(
            update(strategies).where(strategies.c.id == strategy_id).values(**vals)
        )
        self._conn.commit()


class OrderRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def save(self, order: Order, strategy_id: str) -> None:
        vals = {
            "order_id": order.order_id,
            "idempotency_key": order.idempotency_key,
            "broker_order_id": order.broker_order_id,
            "strategy_id": strategy_id,
            "leg_index": order.leg_index,
            "trading_symbol": order.trading_symbol,
            "instrument_token": order.instrument_token,
            "side": order.side.value,
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "status": order.status.value,
            "filled_quantity": order.filled_quantity,
            "average_fill_price": order.average_fill_price,
            "submitted_at": order.submitted_at,
            "last_updated_at": order.last_updated_at,
            "reprice_count": order.reprice_count,
            "initial_bid": order.initial_bid,
            "initial_ask": order.initial_ask,
            "notes": order.notes,
        }
        self._conn.execute(insert(orders).values(**vals))
        self._conn.commit()

    def update(self, order: Order) -> None:
        self._conn.execute(
            update(orders).where(orders.c.order_id == order.order_id).values(
                broker_order_id=order.broker_order_id,
                status=order.status.value,
                filled_quantity=order.filled_quantity,
                average_fill_price=order.average_fill_price,
                last_updated_at=order.last_updated_at,
                reprice_count=order.reprice_count,
                notes=order.notes,
            )
        )
        self._conn.commit()

    def log_event(self, order_id: str, event_type: str,
                  old_status: Optional[str] = None, new_status: Optional[str] = None,
                  price: Optional[Decimal] = None, quantity: Optional[int] = None,
                  message: str = "") -> None:
        self._conn.execute(insert(order_events).values(
            order_id=order_id,
            event_time=_now(),
            event_type=event_type,
            old_status=old_status,
            new_status=new_status,
            price=price,
            quantity=quantity,
            message=message,
        ))
        self._conn.commit()


class FillRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def save(self, fill: Fill) -> None:
        self._conn.execute(insert(fills).values(
            fill_id=fill.fill_id,
            order_id=fill.order_id,
            broker_fill_id=fill.broker_fill_id,
            quantity=fill.quantity,
            price=fill.price,
            timestamp=fill.timestamp,
        ))
        self._conn.commit()


class AlertRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def save(self, alert: Alert) -> None:
        self._conn.execute(insert(alerts_table).values(
            alert_id=alert.alert_id,
            strategy_id=alert.strategy_run_id,
            level=alert.level.value,
            event=alert.event,
            message=alert.message,
            timestamp=alert.timestamp,
            acknowledged=alert.acknowledged,
        ))
        self._conn.commit()


class SystemEventRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def log(self, event_type: str, strategy_id: Optional[str] = None,
            details: Optional[dict] = None) -> None:
        self._conn.execute(insert(system_events).values(
            event_time=_now(),
            event_type=event_type,
            strategy_id=strategy_id,
            details=json.dumps(details) if details else None,
        ))
        self._conn.commit()
