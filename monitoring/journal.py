from __future__ import annotations
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from execution.models import AlertLevel, StrategyState
from storage.repositories import SystemEventRepository

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class Journal:
    """
    Writes structured audit entries for every significant event.
    Backed by the system_events table.
    """

    def __init__(self, repo: Optional[SystemEventRepository] = None) -> None:
        self._repo = repo

    def _write(self, event_type: str, strategy_id: Optional[str] = None, details: Optional[dict] = None) -> None:
        logger.info("journal", extra={"event_type": event_type, "strategy_id": strategy_id, **(details or {})})
        if self._repo:
            try:
                self._repo.log(event_type, strategy_id, details)
            except Exception as exc:
                logger.error(f"journal_persist_error: {exc}")

    def strategy_created(self, strategy_id: str, name: str, underlying: str) -> None:
        self._write("STRATEGY_CREATED", strategy_id, {"name": name, "underlying": underlying})

    def state_change(self, strategy_id: str, from_state: StrategyState, to_state: StrategyState, reason: str = "") -> None:
        self._write("STATE_CHANGE", strategy_id, {
            "from": from_state.value, "to": to_state.value, "reason": reason
        })

    def order_submitted(self, strategy_id: str, order_id: str, symbol: str, side: str,
                        quantity: int, limit: Decimal) -> None:
        self._write("ORDER_SUBMITTED", strategy_id, {
            "order_id": order_id, "symbol": symbol,
            "side": side, "quantity": quantity, "limit": str(limit)
        })

    def fill_received(self, strategy_id: str, order_id: str, symbol: str,
                      quantity: int, price: Decimal) -> None:
        self._write("FILL_RECEIVED", strategy_id, {
            "order_id": order_id, "symbol": symbol,
            "quantity": quantity, "price": str(price)
        })

    def entry_complete(self, strategy_id: str, net_credit: Decimal) -> None:
        self._write("ENTRY_COMPLETE", strategy_id, {"net_credit": str(net_credit)})

    def exit_triggered(self, strategy_id: str, trigger: str) -> None:
        self._write("EXIT_TRIGGERED", strategy_id, {"trigger": trigger})

    def exit_complete(self, strategy_id: str, exit_debit: Decimal, final_pnl: Decimal) -> None:
        self._write("EXIT_COMPLETE", strategy_id, {
            "exit_debit": str(exit_debit), "final_pnl": str(final_pnl)
        })

    def kill_switch(self, reason: str, strategy_id: Optional[str] = None) -> None:
        self._write("KILL_SWITCH", strategy_id, {"reason": reason})

    def error(self, strategy_id: Optional[str], message: str) -> None:
        self._write("ERROR", strategy_id, {"message": message})
