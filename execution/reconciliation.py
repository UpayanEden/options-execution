from __future__ import annotations
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from execution.fill_tracker import FillTracker
from execution.models import LegPosition, Side

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    matched: bool
    mismatches: list[str]
    broker_positions: list[dict]
    local_positions: list[LegPosition]


class Reconciliation:
    """
    Compares local position state with broker-reported positions.
    Flags any quantity or side mismatches.
    """

    def reconcile(
        self,
        local_positions: list[LegPosition],
        broker_positions: list[dict],  # raw dicts from broker.get_positions()
    ) -> ReconciliationResult:
        mismatches = []

        # Build broker map: trading_symbol -> net_qty
        broker_map: dict[str, int] = {}
        for pos in broker_positions:
            sym = pos.get("tradingsymbol") or pos.get("trading_symbol", "")
            qty = int(pos.get("net_quantity", pos.get("quantity", 0)))
            broker_map[sym] = broker_map.get(sym, 0) + qty

        for lp in local_positions:
            expected_qty = lp.quantity if lp.side == Side.BUY else -lp.quantity
            broker_qty = broker_map.get(lp.trading_symbol, None)

            if broker_qty is None:
                mismatches.append(
                    f"{lp.trading_symbol}: local has {expected_qty} but broker shows no position"
                )
            elif broker_qty != expected_qty:
                mismatches.append(
                    f"{lp.trading_symbol}: local={expected_qty} broker={broker_qty}"
                )

        matched = len(mismatches) == 0
        if matched:
            logger.info("reconciliation_ok", extra={"legs": len(local_positions)})
        else:
            logger.warning("reconciliation_mismatch", extra={"mismatches": mismatches})

        return ReconciliationResult(
            matched=matched,
            mismatches=mismatches,
            broker_positions=broker_positions,
            local_positions=local_positions,
        )
