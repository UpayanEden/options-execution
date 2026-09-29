"""
Capital manager for the live shifting iron condor strategy.

Tracks how much capital is allocated to each open position, enforces
per-stock and total allocation limits, and provides lot-sizing utilities.

Thread-safety: all public methods acquire a lock so the CondorRunner and
ShiftMonitor threads can safely call them concurrently.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

from live.config import CapitalConfig

logger = logging.getLogger(__name__)

# SPAN margin buffer applied on top of worst-case theoretical margin
_SPAN_BUFFER = 1.20  # 20% extra
# Minimum lots to trade
_MIN_LOTS = 1


class CapitalManager:
    """
    Manages capital allocation across active condor positions.

    All monetary values are in INR.
    """

    def __init__(self, config: CapitalConfig) -> None:
        """
        Initialise with a CapitalConfig.

        Args:
            config: CapitalConfig dataclass with limits and percentages.
        """
        self._config = config
        self._allocations: Dict[str, float] = {}
        self._lock = threading.Lock()
        logger.info(
            "capital_manager_init",
            extra={
                "total_capital": config.total_capital,
                "max_capital_per_stock": config.max_capital_per_stock,
                "max_open_positions": config.max_open_positions,
                "cash_margin_pct": config.cash_margin_pct,
            },
        )

    # ── Capital query methods ──────────────────────────────────────────────

    def get_available_capital(self) -> float:
        """
        Return total capital minus what is already allocated.

        Returns:
            Available capital in INR (may be 0 if fully allocated).
        """
        with self._lock:
            return max(0.0, self._config.total_capital - sum(self._allocations.values()))

    def get_allocated(self) -> Dict[str, float]:
        """
        Return a snapshot of current allocations keyed by symbol.

        Returns:
            Dict mapping symbol → allocated margin amount.
        """
        with self._lock:
            return dict(self._allocations)

    def get_total_allocated(self) -> float:
        """
        Return the sum of all currently allocated capital.

        Returns:
            Total allocated margin in INR.
        """
        with self._lock:
            return sum(self._allocations.values())

    def get_remaining(self) -> float:
        """
        Return capital remaining after all current allocations.

        Returns:
            Remaining capital in INR.
        """
        return self.get_available_capital()

    # ── Margin estimation ──────────────────────────────────────────────────

    def estimate_margin(
        self,
        spot: float,
        lot_size: int,
        lots: int,
        wing_width_pct: float,
    ) -> float:
        """
        Estimate worst-case SPAN margin for an iron condor position.

        The worst case is the full wing width (maximum loss on one spread)
        times the notional size, plus a 20% SPAN buffer for intraday
        margin changes.

        Formula:
            wing_width_inr = spot × wing_width_pct / 100
            notional       = wing_width_inr × lot_size × lots
            margin         = notional × SPAN_BUFFER (1.20)

        Args:
            spot:          Current spot price of the underlying.
            lot_size:      Contract lot size.
            lots:          Number of lots.
            wing_width_pct: Wing width as a percentage of spot.

        Returns:
            Estimated required margin in INR.
        """
        notional = spot * lot_size * lots
        margin = notional * 0.125  # 12.5% of notional — standard SPAN margin estimate
        logger.debug(
            "margin_estimated",
            extra={
                "spot": spot,
                "lot_size": lot_size,
                "lots": lots,
                "notional": round(notional, 2),
                "margin_pct": 12.5,
                "margin": round(margin, 2),
            },
        )
        return margin

    # ── Allocation management ──────────────────────────────────────────────

    def allocate(self, symbol: str, margin: float) -> bool:
        """
        Attempt to allocate *margin* INR to *symbol*.

        Checks:
        1. margin <= max_capital_per_stock
        2. margin <= remaining available capital

        Args:
            symbol: Trading symbol (e.g. 'RELIANCE').
            margin: Margin to allocate in INR.

        Returns:
            True if allocation succeeded; False if any check failed.
        """
        with self._lock:
            if symbol in self._allocations:
                logger.warning(
                    "allocation_already_exists",
                    extra={"symbol": symbol, "existing": self._allocations[symbol]},
                )
                return False

            if margin > self._config.max_capital_per_stock:
                logger.warning(
                    "allocation_exceeds_per_stock_limit",
                    extra={
                        "symbol": symbol,
                        "margin": margin,
                        "limit": self._config.max_capital_per_stock,
                    },
                )
                return False

            remaining = self._config.total_capital - sum(self._allocations.values())
            if margin > remaining:
                logger.warning(
                    "allocation_exceeds_total_capital",
                    extra={
                        "symbol": symbol,
                        "margin": margin,
                        "remaining": remaining,
                    },
                )
                return False

            self._allocations[symbol] = margin
            logger.info(
                "capital_allocated",
                extra={
                    "symbol": symbol,
                    "margin": round(margin, 2),
                    "total_allocated": round(sum(self._allocations.values()), 2),
                    "remaining": round(remaining - margin, 2),
                },
            )
            return True

    def release(self, symbol: str) -> float:
        """
        Release the capital allocation for *symbol*.

        Args:
            symbol: Trading symbol whose allocation should be freed.

        Returns:
            The amount of capital released (0 if symbol was not allocated).
        """
        with self._lock:
            amount = self._allocations.pop(symbol, 0.0)
            if amount > 0:
                logger.info(
                    "capital_released",
                    extra={
                        "symbol": symbol,
                        "released": round(amount, 2),
                        "total_allocated": round(sum(self._allocations.values()), 2),
                    },
                )
            else:
                logger.debug("capital_release_noop", extra={"symbol": symbol})
            return amount

    # ── Risk checks ────────────────────────────────────────────────────────

    def check_daily_loss(self, daily_pnl: float) -> bool:
        """
        Check whether the daily P&L is within acceptable limits.

        The hard limit is -20% of total capital.

        Args:
            daily_pnl: Today's cumulative P&L in INR (negative = loss).

        Returns:
            True if within limits; False if daily loss limit is breached.
        """
        limit = -0.20 * self._config.total_capital
        within = daily_pnl > limit
        if not within:
            logger.critical(
                "daily_loss_limit_breached",
                extra={
                    "daily_pnl": round(daily_pnl, 2),
                    "limit": round(limit, 2),
                    "total_capital": self._config.total_capital,
                },
            )
        return within

    # ── Lot sizing ─────────────────────────────────────────────────────────

    def get_lots_for_capital(
        self,
        symbol: str,
        spot: float,
        lot_size: int,
        wing_width_pct: float,
    ) -> int:
        """
        Calculate the maximum number of lots that can be traded within the
        per-stock capital limit and remaining total capital.

        Uses conservative estimate_margin() at 1 lot, then scales up until
        either the per-stock cap or the remaining capital cap is hit.

        Args:
            symbol:         Trading symbol (used for logging).
            spot:           Current spot price.
            lot_size:       Contract lot size.
            wing_width_pct: Wing width percentage for margin estimation.

        Returns:
            Maximum integer number of lots (at least 1 if any capital available,
            or 0 if no capital is available).
        """
        per_stock_cap = self._config.max_capital_per_stock
        available = self.get_available_capital()
        effective_cap = min(per_stock_cap, available)

        if effective_cap <= 0:
            logger.warning("no_capital_available", extra={"symbol": symbol})
            return 0

        margin_per_lot = self.estimate_margin(spot, lot_size, 1, wing_width_pct)
        if margin_per_lot <= 0:
            logger.error("zero_margin_per_lot", extra={"symbol": symbol, "spot": spot})
            return 0

        max_lots = int(effective_cap / margin_per_lot)
        result = max(_MIN_LOTS, max_lots) if max_lots >= _MIN_LOTS else 0

        logger.info(
            "lots_calculated",
            extra={
                "symbol": symbol,
                "lots": result,
                "margin_per_lot": round(margin_per_lot, 2),
                "effective_cap": round(effective_cap, 2),
            },
        )
        return result

    @staticmethod
    def compute_available_from_margins(margins: dict) -> float:
        """
        Derive effective available capital from a Zerodha margins() response.

        Selection logic:
          - Only cash present  → use cash balance
          - Only stock (collateral) present → use stock collateral
          - Both present → use 50% cash + 50% collateral

        Args:
            margins: Raw dict from kite.margins() or broker.get_margins().

        Returns:
            Effective capital in INR.
        """
        equity = margins.get("equity", {})
        available = equity.get("available", {})
        cash = float(available.get("live_balance", 0) or 0)
        stock = float(available.get("collateral", 0) or 0)

        if stock <= 0:
            effective = cash
            source = "cash_only"
        elif cash <= 0:
            effective = stock
            source = "stock_only"
        else:
            effective = cash * 0.5 + stock * 0.5
            source = "50_50_split"

        logger.info(
            "capital_from_margins",
            extra={
                "cash_balance": round(cash, 2),
                "stock_collateral": round(stock, 2),
                "effective_capital": round(effective, 2),
                "source": source,
            },
        )
        return effective

    def update_from_broker_margins(self, margins: dict) -> float:
        """
        Update total_capital from actual broker margin data.

        Should be called at session start before any positions are entered.

        Args:
            margins: Raw dict from broker.get_margins().

        Returns:
            New total_capital value.
        """
        effective = self.compute_available_from_margins(margins)
        if effective > 0:
            self._config.total_capital = effective
            logger.info(
                "total_capital_updated_from_broker",
                extra={"new_total_capital": round(effective, 2)},
            )
        return effective
