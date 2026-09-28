from __future__ import annotations
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from execution.models import ExitRulesConfig, ExitTrigger, LegPosition, Quote, Side, StrategyPosition

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class RuntimeLimits:
    """
    Checks runtime exit conditions during position monitoring.
    Returns the first triggered ExitTrigger, or None if no condition is met.
    """

    def __init__(self, exit_rules: ExitRulesConfig, max_profit: Optional[Decimal] = None) -> None:
        self._rules = exit_rules
        self._max_profit = max_profit  # theoretical max profit of the strategy

    def check_exit_conditions(
        self,
        position: StrategyPosition,
        dte: int,
        quotes: dict[str, Quote],
        underlying_spot: Optional[Decimal] = None,
    ) -> Optional[ExitTrigger]:
        """
        Check all configured exit conditions in priority order.
        Returns the first triggered ExitTrigger, or None.
        """
        entry_credit = position.entry_credit
        unrealized_pnl = position.unrealized_pnl

        # 1. Stop loss
        if (
            self._rules.stop_loss_inr is not None
            and unrealized_pnl is not None
            and unrealized_pnl <= -self._rules.stop_loss_inr
        ):
            logger.warning("exit_stop_loss", extra={"pnl": str(unrealized_pnl)})
            return ExitTrigger.STOP_LOSS

        # 2. Stop loss as multiple of credit
        if (
            self._rules.stop_loss_credit_multiple is not None
            and entry_credit is not None
            and unrealized_pnl is not None
        ):
            loss_threshold = -entry_credit * self._rules.stop_loss_credit_multiple
            if unrealized_pnl <= loss_threshold:
                logger.warning("exit_stop_loss_multiple", extra={"pnl": str(unrealized_pnl)})
                return ExitTrigger.STOP_LOSS

        # 3. Profit target (% of max profit)
        if (
            self._rules.profit_target_pct is not None
            and self._max_profit is not None
            and unrealized_pnl is not None
        ):
            target = self._max_profit * (self._rules.profit_target_pct / Decimal("100"))
            if unrealized_pnl >= target:
                logger.info("exit_profit_target_pct", extra={"pnl": str(unrealized_pnl), "target": str(target)})
                return ExitTrigger.PROFIT_TARGET

        # 4. Profit target (INR)
        if (
            self._rules.profit_target_inr is not None
            and unrealized_pnl is not None
            and unrealized_pnl >= self._rules.profit_target_inr
        ):
            logger.info("exit_profit_target_inr", extra={"pnl": str(unrealized_pnl)})
            return ExitTrigger.PROFIT_TARGET

        # 5. DTE exit
        if self._rules.dte_exit is not None and dte <= self._rules.dte_exit:
            logger.info("exit_dte", extra={"dte": dte})
            return ExitTrigger.DTE_EXIT

        # 6. Time-based exit
        if self._rules.time_based_exit is not None:
            now_time = datetime.now(IST).strftime("%H:%M")
            if now_time >= self._rules.time_based_exit:
                logger.info("exit_time", extra={"time": now_time})
                return ExitTrigger.TIME_EXIT

        return None
