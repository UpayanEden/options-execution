"""
Structured JSON trade logger.

Writes one JSON line per event to rotating log files under logs/live/.
Each event carries a UTC-ISO timestamp, event type, symbol, and arbitrary
data payload.  A secondary console handler emits colour-coded output for
operator visibility.

Usage::

    from live.trade_logger import TradeLogger, EventType
    logger = TradeLogger(symbol="RELIANCE")
    logger.log_entry("RELIANCE", 2850.0, {...}, lots=1, credit=12.5, guard_report=rpt)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from execution.models import Order

IST = ZoneInfo("Asia/Kolkata")

# ── ANSI colour codes ──────────────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_WHITE = "\033[37m"
_BRIGHT_RED = "\033[91m"

_LEVEL_COLOURS: Dict[str, str] = {
    "DEBUG": _WHITE,
    "INFO": _GREEN,
    "WARNING": _YELLOW,
    "ERROR": _RED,
    "CRITICAL": _BRIGHT_RED,
}


# ── EventType enum ─────────────────────────────────────────────────────────


class EventType(str, Enum):
    """All structured event types emitted by the trading system."""

    ENTRY_ATTEMPT = "ENTRY_ATTEMPT"
    ENTRY_SUCCESS = "ENTRY_SUCCESS"
    ENTRY_FAILED = "ENTRY_FAILED"
    EXIT_ATTEMPT = "EXIT_ATTEMPT"
    EXIT_SUCCESS = "EXIT_SUCCESS"
    EXIT_FAILED = "EXIT_FAILED"
    SHIFT_TRIGGERED = "SHIFT_TRIGGERED"
    SHIFT_COMPLETE = "SHIFT_COMPLETE"
    SHIFT_FAILED = "SHIFT_FAILED"
    TAKE_PROFIT_HIT = "TAKE_PROFIT_HIT"
    GUARD_PASS = "GUARD_PASS"
    GUARD_FAIL = "GUARD_FAIL"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_RECONCILED = "POSITION_RECONCILED"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
    CAPITAL_LIMIT_BREACHED = "CAPITAL_LIMIT_BREACHED"
    DAILY_LOSS_LIMIT_BREACHED = "DAILY_LOSS_LIMIT_BREACHED"
    ERROR = "ERROR"


# ── Custom JSON formatter ──────────────────────────────────────────────────


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        """Serialise the log record to a JSON line."""
        payload = record.__dict__.get("json_payload", {})
        line: Dict[str, Any] = {
            "ts": datetime.now(IST).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        line.update(payload)
        try:
            return json.dumps(line, default=str, ensure_ascii=False)
        except Exception:
            return json.dumps({"error": "serialisation_failed", "raw": str(payload)})


class _ColourConsoleFormatter(logging.Formatter):
    """Console formatter with ANSI colour and event-type highlighting."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        """Format a record for human-readable console output."""
        payload = record.__dict__.get("json_payload", {})
        event = payload.get("event_type", "")
        symbol = payload.get("symbol", "")
        reason = payload.get("reason", payload.get("data", {}).get("reason", ""))
        colour = _LEVEL_COLOURS.get(record.levelname, _WHITE)
        ts = datetime.now(IST).strftime("%H:%M:%S")
        parts = [
            f"{colour}{ts}{_RESET}",
            f"{_BOLD}{record.levelname:<8}{_RESET}",
            f"{_CYAN}{event:<30}{_RESET}",
        ]
        if symbol:
            parts.append(f"{_MAGENTA}{symbol:<15}{_RESET}")
        if reason:
            parts.append(str(reason)[:120])
        return "  ".join(parts)


# ── TradeLogger ────────────────────────────────────────────────────────────


class TradeLogger:
    """
    Structured JSON logger for live trading events.

    Writes one JSON line per event to a rotating file in *log_dir* and
    simultaneously emits colour-coded output to stdout.
    """

    def __init__(
        self,
        log_dir: str = "logs/live",
        symbol: Optional[str] = None,
    ) -> None:
        """
        Initialise the trade logger.

        Args:
            log_dir: Directory where .jsonl log files are written.
            symbol:  Optional symbol name used to scope the logger name.
        """
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._symbol = symbol

        logger_name = f"trade_logger.{symbol}" if symbol else "trade_logger"
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False

        if not self._logger.handlers:
            self._setup_handlers()

    # ── Handler setup ──────────────────────────────────────────────────────

    def _setup_handlers(self) -> None:
        """Attach rotating file handler and stdout colour handler."""
        log_file = self._log_dir / "trades.jsonl"
        fh = logging.handlers.RotatingFileHandler(
            filename=str(log_file),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(_JsonFormatter())
        fh.setLevel(logging.DEBUG)
        self._logger.addHandler(fh)

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(_ColourConsoleFormatter())
        sh.setLevel(logging.INFO)
        self._logger.addHandler(sh)

    # ── Core log method ────────────────────────────────────────────────────

    def log(
        self,
        event_type: EventType,
        symbol: Optional[str],
        data: Dict[str, Any],
        level: str = "INFO",
    ) -> None:
        """
        Emit a structured event log line.

        Args:
            event_type: EventType enum value.
            symbol:     Trading symbol the event relates to (or None).
            data:       Arbitrary key-value payload.
            level:      Logging level string: 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'.
        """
        payload: Dict[str, Any] = {
            "event_type": event_type.value,
            "symbol": symbol or self._symbol or "",
            "ts": datetime.now(IST).isoformat(),
        }
        payload.update({k: v for k, v in data.items() if k not in payload})

        record_level = getattr(logging, level.upper(), logging.INFO)
        log_record = self._logger.makeRecord(
            name=self._logger.name,
            level=record_level,
            fn="",
            lno=0,
            msg=event_type.value,
            args=(),
            exc_info=None,
        )
        log_record.__dict__["json_payload"] = payload
        self._logger.handle(log_record)

    # ── Convenience methods ────────────────────────────────────────────────

    def log_entry(
        self,
        symbol: str,
        spot: float,
        strikes_dict: Dict[str, float],
        lots: int,
        credit: float,
        guard_report: Any,
        put_dist_pct: float = 0.0,
        call_dist_pct: float = 0.0,
        cash_margin: float = 0.0,
        stock_margin: float = 0.0,
        margin_source: str = "",
        required_margin: float = 0.0,
    ) -> None:
        """
        Log a successful condor entry.

        Args:
            symbol:          Underlying symbol.
            spot:            Spot price at entry.
            strikes_dict:    Dict with keys short_put, long_put, short_call, long_call.
            lots:            Number of lots entered.
            credit:          Net premium credit received per share.
            guard_report:    GuardReport instance from run_all_guards().
            put_dist_pct:    Short put distance from spot as % of spot (from StockConfig).
            call_dist_pct:   Short call distance from spot as % of spot (from StockConfig).
            cash_margin:     Available cash balance (live_balance) from broker.
            stock_margin:    Available stock collateral from broker.
            margin_source:   How margin was sourced: cash_only / stock_only / 50_50_split.
            required_margin: Estimated margin required for this trade.
        """
        # Compute actual strike distances from spot for reference
        short_put_strike = strikes_dict.get("short_put", 0)
        short_call_strike = strikes_dict.get("short_call", 0)
        actual_put_dist_pct = round(abs(spot - short_put_strike) / spot * 100, 2) if spot else 0.0
        actual_call_dist_pct = round(abs(short_call_strike - spot) / spot * 100, 2) if spot else 0.0

        self.log(
            EventType.ENTRY_SUCCESS,
            symbol,
            {
                "spot": round(spot, 2),
                "strikes": strikes_dict,
                "lots": lots,
                "credit": round(credit, 4),
                "put_dist_pct": put_dist_pct,
                "call_dist_pct": call_dist_pct,
                "actual_put_dist_pct": actual_put_dist_pct,
                "actual_call_dist_pct": actual_call_dist_pct,
                "required_margin": round(required_margin, 2),
                "cash_margin": round(cash_margin, 2),
                "stock_margin": round(stock_margin, 2),
                "margin_source": margin_source,
                "guard_passed": getattr(guard_report, "passed", True),
            },
        )

    def log_exit(
        self,
        symbol: str,
        reason: str,
        entry_credit: float,
        exit_debit: float,
        pnl: float,
    ) -> None:
        """
        Log a condor exit with P&L summary.

        Args:
            symbol:       Underlying symbol.
            reason:       Human-readable exit reason.
            entry_credit: Credit received at entry.
            exit_debit:   Debit paid to close.
            pnl:          Net P&L (credit - debit) × lots × lot_size.
        """
        level = "INFO" if pnl >= 0 else "WARNING"
        self.log(
            EventType.EXIT_SUCCESS,
            symbol,
            {
                "reason": reason,
                "entry_credit": round(entry_credit, 4),
                "exit_debit": round(exit_debit, 4),
                "pnl": round(pnl, 2),
            },
            level=level,
        )

    def log_shift(
        self,
        symbol: str,
        old_strikes: Dict[str, float],
        new_strikes: Dict[str, float],
        spot: float,
        trigger_pct: float,
    ) -> None:
        """
        Log a condor shift event.

        Args:
            symbol:       Underlying symbol.
            old_strikes:  Previous condor strikes.
            new_strikes:  New condor strikes after shift.
            spot:         Current spot price that triggered the shift.
            trigger_pct:  Shift trigger threshold percentage.
        """
        self.log(
            EventType.SHIFT_TRIGGERED,
            symbol,
            {
                "old_strikes": old_strikes,
                "new_strikes": new_strikes,
                "spot": round(spot, 2),
                "trigger_pct": trigger_pct,
            },
            level="WARNING",
        )

    def log_shift_failed(
        self,
        symbol: str,
        trigger_side: str,
        failed_step: str,
    ) -> None:
        """
        Log a shift execution failure.

        Args:
            symbol:       Underlying symbol.
            trigger_side: "PUT" or "CALL" — which side triggered the shift.
            failed_step:  Short description of the step that failed
                          (e.g. "buy_new_hedge", "buy_back_short", "resolve_instruments").
        """
        self.log(
            EventType.SHIFT_FAILED,
            symbol,
            {"trigger_side": trigger_side, "failed_step": failed_step},
            level="ERROR",
        )

    def log_take_profit(
        self,
        symbol: str,
        credit_received: float,
        current_cost: float,
        profit_pct: float,
        pnl: float,
    ) -> None:
        """
        Log a take-profit exit trigger.

        Args:
            symbol:         Underlying symbol.
            credit_received: Initial premium credit.
            current_cost:   Current cost to close all legs.
            profit_pct:     Profit as percentage of initial credit.
            pnl:            Estimated P&L.
        """
        self.log(
            EventType.TAKE_PROFIT_HIT,
            symbol,
            {
                "credit_received": round(credit_received, 4),
                "current_cost": round(current_cost, 4),
                "profit_pct": round(profit_pct, 2),
                "pnl": round(pnl, 2),
            },
        )

    def log_guard_fail(self, symbol: str, guard_report: Any) -> None:
        """
        Log a guard failure with the full report dict.

        Args:
            symbol:       Underlying symbol.
            guard_report: GuardReport instance.
        """
        report_dict = (
            guard_report.to_dict()
            if hasattr(guard_report, "to_dict")
            else {"raw": str(guard_report)}
        )
        self.log(
            EventType.GUARD_FAIL,
            symbol,
            {"guard_report": report_dict},
            level="WARNING",
        )

    def log_order(self, order: Order, event_type: EventType) -> None:
        """
        Log an order lifecycle event.

        Args:
            order:      The Order model instance.
            event_type: One of ORDER_PLACED, ORDER_FILLED, ORDER_REJECTED.
        """
        self.log(
            event_type,
            order.trading_symbol,
            {
                "order_id": order.order_id,
                "broker_order_id": order.broker_order_id,
                "side": order.side.value,
                "quantity": order.quantity,
                "limit_price": str(order.limit_price),
                "status": order.status.value,
                "filled_qty": order.filled_quantity,
                "avg_fill": str(order.average_fill_price or ""),
            },
        )

    def log_position_check(
        self,
        symbol: str,
        expected: Dict[str, int],
        actual: Dict[str, int],
        matched: bool,
    ) -> None:
        """
        Log a position reconciliation check.

        Args:
            symbol:   Underlying symbol.
            expected: Expected quantities per trading symbol.
            actual:   Actual quantities from broker.
            matched:  True if all quantities matched.
        """
        event = EventType.POSITION_RECONCILED if matched else EventType.POSITION_MISMATCH
        level = "INFO" if matched else "ERROR"
        self.log(
            event,
            symbol,
            {"expected": expected, "actual": actual, "matched": matched},
            level=level,
        )

    def log_error(
        self,
        symbol: Optional[str],
        error: Exception,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log an unhandled exception or error state.

        Args:
            symbol:  Underlying symbol (may be None for system-level errors).
            error:   The exception instance.
            context: Optional additional context dict.
        """
        data: Dict[str, Any] = {
            "error_type": type(error).__name__,
            "error_msg": str(error),
        }
        if context:
            data["context"] = context
        self.log(EventType.ERROR, symbol, data, level="ERROR")

    # ── Query methods ──────────────────────────────────────────────────────

    def get_today_events(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return all events logged today from the .jsonl file.

        Args:
            symbol: If provided, filter events to this symbol only.

        Returns:
            List of event dicts, newest last.
        """
        today = date.today().isoformat()
        events: List[Dict[str, Any]] = []
        log_file = self._log_dir / "trades.jsonl"

        if not log_file.exists():
            return events

        try:
            with open(log_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("ts", "")
                    if not ts.startswith(today):
                        continue
                    if symbol and obj.get("symbol") != symbol:
                        continue
                    events.append(obj)
        except OSError as exc:
            self._logger.error("get_today_events_error: %s", exc)

        return events
