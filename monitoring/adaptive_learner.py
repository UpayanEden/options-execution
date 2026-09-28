"""
Adaptive threshold learner.

After each closed trade the learner adjusts the scanner quality thresholds:
  - Loss  → tighten (raise IV rank floor, timing floor, RoR floor)
  - Win   → relax back toward baseline

Thresholds are clamped within [baseline, max_tightening] so the system
never becomes so restrictive that it stops trading entirely.

State is persisted to logs/adaptive_state.json so it survives restarts.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Threshold spec ────────────────────────────────────────────────────────────

@dataclass
class ThresholdSpec:
    """One adaptive threshold: current value + bounds + step sizes."""
    baseline:   float
    current:    float
    max_val:    float
    loss_step:  float   # how much to tighten on each loss
    win_step:   float   # how much to relax on each win

    def tighten(self) -> None:
        self.current = min(self.max_val, self.current + self.loss_step)

    def relax(self) -> None:
        self.current = max(self.baseline, self.current - self.win_step)


# ── Learner ───────────────────────────────────────────────────────────────────

class AdaptiveLearner:
    """
    Adjusts scanner thresholds based on trade outcomes.

    Thresholds managed:
      iv_rank_min      — minimum IV rank required to enter
      min_timing_score — minimum timing quality score
      min_ror_pct      — minimum return-on-risk %
    """

    STATE_FILE = Path("logs/adaptive_state.json")

    def __init__(
        self,
        iv_rank_baseline:    float = 30.0,
        timing_baseline:     float = 40.0,
        ror_baseline:        float = 15.0,
    ) -> None:
        self._thresholds: dict[str, ThresholdSpec] = {
            "iv_rank_min": ThresholdSpec(
                baseline=iv_rank_baseline, current=iv_rank_baseline,
                max_val=70.0, loss_step=5.0, win_step=2.0,
            ),
            "min_timing_score": ThresholdSpec(
                baseline=timing_baseline, current=timing_baseline,
                max_val=80.0, loss_step=5.0, win_step=2.0,
            ),
            "min_ror_pct": ThresholdSpec(
                baseline=ror_baseline, current=ror_baseline,
                max_val=50.0, loss_step=3.0, win_step=1.0,
            ),
        }
        self._trade_count: int = 0
        self._win_count:   int = 0
        self._loss_count:  int = 0
        self._load()

    # ── Public API ───────────────────────────────────────────────────────────

    def record_outcome(
        self,
        pnl: float,
        underlying: str,
        strategy: str,
        iv_rank: Optional[float] = None,
        timing_score: Optional[float] = None,
        ror: Optional[float] = None,
    ) -> None:
        """Call after every completed trade."""
        self._trade_count += 1
        won = pnl > 0

        if won:
            self._win_count += 1
            for spec in self._thresholds.values():
                spec.relax()
            action = "relaxed"
        else:
            self._loss_count += 1
            for spec in self._thresholds.values():
                spec.tighten()
            action = "tightened"

        logger.info(
            "adaptive_threshold_update",
            extra={
                "pnl": round(pnl, 2),
                "outcome": "WIN" if won else "LOSS",
                "underlying": underlying,
                "strategy": strategy,
                "action": action,
                "iv_rank_min": self.iv_rank_min,
                "min_timing_score": self.min_timing_score,
                "min_ror_pct": self.min_ror_pct,
                "trades": self._trade_count,
                "wins": self._win_count,
                "losses": self._loss_count,
            },
        )
        print(
            f"\n[AdaptiveLearner] Trade {'WON' if won else 'LOST'} ₹{pnl:,.0f} "
            f"→ thresholds {action}:\n"
            f"  IV rank ≥ {self.iv_rank_min:.0f}  |  "
            f"Timing ≥ {self.min_timing_score:.0f}  |  "
            f"RoR ≥ {self.min_ror_pct:.0f}%  "
            f"  (W:{self._win_count} L:{self._loss_count})\n"
        )
        self._save()

    @property
    def iv_rank_min(self) -> float:
        return self._thresholds["iv_rank_min"].current

    @property
    def min_timing_score(self) -> float:
        return self._thresholds["min_timing_score"].current

    @property
    def min_ror_pct(self) -> float:
        return self._thresholds["min_ror_pct"].current

    def apply_to_config(self, cfg, risk) -> None:
        """Push current thresholds into the live config objects."""
        cfg.iv_rank_min        = self.iv_rank_min
        cfg.min_timing_score   = self.min_timing_score
        from decimal import Decimal
        risk.min_ror_pct       = Decimal(str(self.min_ror_pct))

    def summary(self) -> str:
        win_rate = (self._win_count / self._trade_count * 100) if self._trade_count else 0
        return (
            f"Trades={self._trade_count}  W={self._win_count}  L={self._loss_count}  "
            f"WinRate={win_rate:.0f}%  |  "
            f"IV≥{self.iv_rank_min:.0f}  Timing≥{self.min_timing_score:.0f}  "
            f"RoR≥{self.min_ror_pct:.0f}%"
        )

    # ── Persistence ──────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            state = {
                "trade_count": self._trade_count,
                "win_count":   self._win_count,
                "loss_count":  self._loss_count,
                "thresholds":  {k: asdict(v) for k, v in self._thresholds.items()},
            }
            self.STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as exc:
            logger.warning("adaptive_save_failed", extra={"error": str(exc)})

    def _load(self) -> None:
        try:
            if not self.STATE_FILE.exists():
                return
            state = json.loads(self.STATE_FILE.read_text())
            self._trade_count = state.get("trade_count", 0)
            self._win_count   = state.get("win_count",   0)
            self._loss_count  = state.get("loss_count",  0)
            for key, spec_data in state.get("thresholds", {}).items():
                if key in self._thresholds:
                    s = self._thresholds[key]
                    s.current = spec_data.get("current", s.baseline)
            logger.info(
                "adaptive_state_loaded",
                extra={"trades": self._trade_count, "summary": self.summary()},
            )
        except Exception as exc:
            logger.warning("adaptive_load_failed", extra={"error": str(exc)})
