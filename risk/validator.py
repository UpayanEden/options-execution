"""
Pre-trade validation.
All checks must pass before any order is submitted.
Returns a list of ValidationResult objects — never raises directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from execution.models import Quote, RiskConfig, StrategyConfig
from risk.payoff import PayoffResult

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    name: str
    passed: bool
    value: Optional[str] = None
    limit: Optional[str] = None
    message: str = ""


@dataclass
class ValidationReport:
    results: list[ValidationResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[ValidationResult]:
        return [r for r in self.results if not r.passed]

    def add(self, result: ValidationResult) -> None:
        self.results.append(result)

    def print_table(self) -> None:
        print(f"\n{'CHECK':<35} {'RESULT':<8} {'VALUE':<15} {'LIMIT':<15} MESSAGE")
        print("-" * 100)
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            print(
                f"{r.name:<35} {status:<8} {str(r.value or ''):<15} "
                f"{str(r.limit or ''):<15} {r.message}"
            )
        overall = "ALL CHECKS PASSED" if self.passed else f"{len(self.failures)} CHECK(S) FAILED"
        print(f"\n{overall}\n")


class PreTradeValidator:
    """Runs all pre-trade safety checks."""

    def __init__(self, risk: RiskConfig) -> None:
        self.risk = risk

    def validate(
        self,
        strategy: StrategyConfig,
        quotes: dict[str, Quote],           # trading_symbol → Quote
        payoff: PayoffResult,
        estimated_margin: Optional[Decimal],
    ) -> ValidationReport:
        report = ValidationReport()

        report.add(self._check_entry_window())
        report.add(self._check_quote_freshness(quotes))
        report.add(self._check_bid_ask_spreads(quotes))
        report.add(self._check_all_quotes_positive(quotes))
        report.add(self._check_net_credit(payoff))
        report.add(self._check_max_loss(payoff))
        if estimated_margin is not None:
            report.add(self._check_capital(estimated_margin))

        return report

    # ── Individual checks ──────────────────────────────────────────────────

    def _check_entry_window(self) -> ValidationResult:
        now = datetime.now(IST).strftime("%H:%M")
        start = self.risk.entry_start_time
        end = self.risk.entry_end_time
        ok = start <= now <= end
        return ValidationResult(
            name="entry_time_window",
            passed=ok,
            value=now,
            limit=f"{start}–{end}",
            message="" if ok else "Current time is outside the entry window.",
        )

    def _check_quote_freshness(self, quotes: dict[str, Quote]) -> ValidationResult:
        stale = [
            sym for sym, q in quotes.items()
            if q.is_stale(self.risk.quote_staleness_seconds)
        ]
        ok = len(stale) == 0
        return ValidationResult(
            name="quote_freshness",
            passed=ok,
            value=f"{len(stale)} stale" if stale else "all fresh",
            limit=f"{self.risk.quote_staleness_seconds}s",
            message=f"Stale quotes: {stale}" if stale else "",
        )

    def _check_bid_ask_spreads(self, quotes: dict[str, Quote]) -> ValidationResult:
        wide = [
            (sym, float(q.spread_pct))
            for sym, q in quotes.items()
            if q.spread_pct > self.risk.max_bid_ask_spread_pct
        ]
        ok = len(wide) == 0
        return ValidationResult(
            name="bid_ask_spread",
            passed=ok,
            value=f"{len(wide)} wide" if wide else "all ok",
            limit=f"{self.risk.max_bid_ask_spread_pct}%",
            message=f"Wide spreads: {wide}" if wide else "",
        )

    def _check_all_quotes_positive(self, quotes: dict[str, Quote]) -> ValidationResult:
        bad = [sym for sym, q in quotes.items() if q.bid <= 0 or q.ask <= 0]
        ok = len(bad) == 0
        return ValidationResult(
            name="quote_validity",
            passed=ok,
            value="all positive" if ok else f"{len(bad)} zero/negative",
            message=f"Bad quotes: {bad}" if bad else "",
        )

    def _check_net_credit(self, payoff: PayoffResult) -> ValidationResult:
        ok = payoff.net_credit_per_share >= self.risk.min_net_credit
        return ValidationResult(
            name="net_credit",
            passed=ok,
            value=str(payoff.net_credit_per_share),
            limit=str(self.risk.min_net_credit),
            message="" if ok else "Net credit below minimum.",
        )

    def _check_max_loss(self, payoff: PayoffResult) -> ValidationResult:
        ok = payoff.max_loss <= self.risk.max_strategy_loss
        return ValidationResult(
            name="max_loss_within_budget",
            passed=ok,
            value=str(payoff.max_loss),
            limit=str(self.risk.max_strategy_loss),
            message="" if ok else "Max loss exceeds risk budget.",
        )

    def _check_capital(self, estimated_margin: Decimal) -> ValidationResult:
        ok = estimated_margin <= self.risk.capital_limit
        return ValidationResult(
            name="capital_limit",
            passed=ok,
            value=str(estimated_margin),
            limit=str(self.risk.capital_limit),
            message="" if ok else "Estimated margin exceeds capital limit.",
        )
