"""
Intraday entry timing score engine for NSE Iron Condors.

Research basis for NSE options timing:
  09:15–09:45  Opening volatility — wide spreads, erratic fills → AVOID
  09:45–10:30  Primary window    — spreads tighten, direction visible → GOOD
  10:30–11:30  Best window       — peak liquidity, tight spreads → BEST
  11:30–13:00  Midday lull       — volume drops, theta still decaying → OK
  13:00–14:00  Afternoon revival — liquidity returns pre-close → GOOD
  14:00–14:30  Last resort       — entries cut off at 14:30 per risk config → POOR

Score is 0–100.  AutoTrader multiplies entry score into composite opportunity
score. Dashboard renders a colour-coded timeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


# ── Timing windows ────────────────────────────────────────────────────────────

@dataclass
class TimingWindow:
    start: time
    end: time
    label: str
    base_score: float      # peak score achievable in this window
    color: str             # for dashboard: "green" | "yellow" | "orange" | "red"


TIMING_WINDOWS: list[TimingWindow] = [
    TimingWindow(time(9, 15),  time(9, 45),  "Opening (Avoid)",      10.0,  "red"),
    TimingWindow(time(9, 45),  time(10, 30), "Primary (Good)",       75.0,  "green"),
    TimingWindow(time(10, 30), time(11, 30), "Optimal (Best)",       100.0, "green"),
    TimingWindow(time(11, 30), time(13, 0),  "Midday (OK)",          55.0,  "yellow"),
    TimingWindow(time(13, 0),  time(14, 0),  "Afternoon (Good)",     70.0,  "green"),
    TimingWindow(time(14, 0),  time(14, 30), "Last Resort (Poor)",   30.0,  "orange"),
    TimingWindow(time(14, 30), time(15, 30), "Post-Entry Cutoff",    0.0,   "red"),
]


@dataclass
class TimingScore:
    current_time_ist: str          # "HH:MM"
    window_label: str
    score: float                   # 0–100
    color: str
    next_window: Optional[str]     # label of next better window
    minutes_to_optimal: Optional[int]  # None if already in best window
    recommendation: str            # "ENTER_NOW" | "WAIT" | "SKIP_TODAY"
    windows: list[dict]            # all windows for timeline rendering


def _current_window(now: time) -> Optional[TimingWindow]:
    for w in TIMING_WINDOWS:
        if w.start <= now < w.end:
            return w
    return None


def score_entry_time(now_ist: Optional[datetime] = None) -> TimingScore:
    """
    Compute the timing quality score for the current IST time.
    Call without arguments for live use; pass now_ist for testing.
    """
    if now_ist is None:
        now_ist = datetime.now(IST)

    current_t = now_ist.time().replace(tzinfo=None)
    hhmm      = now_ist.strftime("%H:%M")

    window = _current_window(current_t)

    if window is None or current_t < time(9, 15):
        # Pre-market
        return TimingScore(
            current_time_ist=hhmm, window_label="Pre-Market",
            score=0.0, color="red",
            next_window="Opening (Avoid)",
            minutes_to_optimal=int((datetime.combine(now_ist.date(), time(10, 30)) - now_ist.replace(tzinfo=None)).total_seconds() / 60),
            recommendation="WAIT",
            windows=_window_list(current_t),
        )

    if window.base_score == 0.0:
        return TimingScore(
            current_time_ist=hhmm, window_label=window.label,
            score=0.0, color=window.color,
            next_window=None,
            minutes_to_optimal=None,
            recommendation="SKIP_TODAY",
            windows=_window_list(current_t),
        )

    # Smooth score within the window: ramp up in first quarter, peak, ramp down in last quarter
    w_start = window.start
    w_end   = window.end
    total_m = (w_end.hour * 60 + w_end.minute) - (w_start.hour * 60 + w_start.minute)
    elapsed = (current_t.hour * 60 + current_t.minute) - (w_start.hour * 60 + w_start.minute)
    frac    = elapsed / max(total_m, 1)   # 0→1 through window

    # Bell curve: ramp up in first 25%, flat in middle 50%, ramp down in last 25%
    if frac < 0.25:
        smoothed = frac / 0.25            # 0→1
    elif frac < 0.75:
        smoothed = 1.0
    else:
        smoothed = 1.0 - (frac - 0.75) / 0.25   # 1→0

    score = window.base_score * (0.6 + 0.4 * smoothed)   # at least 60% of base within window

    # Next optimal window
    next_window = None
    mins_to_opt = None
    best_upcoming = [
        w for w in TIMING_WINDOWS
        if w.start > current_t and w.base_score >= 70.0
    ]
    if best_upcoming:
        bw = best_upcoming[0]
        next_window  = bw.label
        bw_m = bw.start.hour * 60 + bw.start.minute
        now_m = current_t.hour * 60 + current_t.minute
        mins_to_opt = bw_m - now_m

    if score >= 70.0:
        rec = "ENTER_NOW"
    elif score >= 40.0 and mins_to_opt and mins_to_opt <= 20:
        rec = "WAIT"
    elif score >= 40.0:
        rec = "ENTER_NOW"   # acceptable window, proceed
    else:
        rec = "WAIT" if mins_to_opt else "SKIP_TODAY"

    return TimingScore(
        current_time_ist=hhmm,
        window_label=window.label,
        score=round(score, 1),
        color=window.color,
        next_window=next_window,
        minutes_to_optimal=mins_to_opt,
        recommendation=rec,
        windows=_window_list(current_t),
    )


def _window_list(current_t: time) -> list[dict]:
    """Serialise windows for dashboard timeline rendering."""
    result = []
    for w in TIMING_WINDOWS:
        is_active = w.start <= current_t < w.end
        result.append({
            "label":   w.label,
            "start":   w.start.strftime("%H:%M"),
            "end":     w.end.strftime("%H:%M"),
            "score":   w.base_score,
            "color":   w.color,
            "active":  is_active,
        })
    return result


# ── Composite opportunity score ───────────────────────────────────────────────

@dataclass
class OpportunityScore:
    composite: float           # 0–100 — final score used to rank entries
    timing_score: float        # 0–100
    iv_rank_score: float       # 0–100
    market_score: float        # 0–100
    credit_score: float        # 0–100 (net_credit vs target)
    grade: str                 # "A" | "B" | "C" | "D" | "F"
    action: str                # "STRONG_BUY" | "BUY" | "HOLD" | "SKIP"
    reason: str                # brief human explanation


def compute_opportunity_score(
    timing_score: float,
    iv_rank: float,            # 0–100
    market_score: float,       # 0–100 from MarketConditionScore.composite
    net_credit: float,         # actual credit in pts
    target_credit: float,      # from risk config: target_net_credit
    tradeable: bool,           # from MarketConditionScore
    min_iv_rank: float = 30.0,
) -> OpportunityScore:
    """
    Combine timing, IV rank, market conditions, and credit quality into one score.

    Weights (research-based for IC strategies):
      Timing      20%  — when you enter affects fill quality & early theta capture
      IV Rank     35%  — single biggest predictor of IC success
      Market      25%  — VIX level & event risk
      Credit      20%  — actual premium vs target
    """
    # IV rank score
    if iv_rank >= 80:
        iv_s = 100.0
    elif iv_rank >= 50:
        iv_s = 60.0 + (iv_rank - 50) / 30 * 40
    elif iv_rank >= 30:
        iv_s = 30.0 + (iv_rank - 30) / 20 * 30
    else:
        iv_s = iv_rank / 30 * 30   # linear 0→30 before threshold

    # Credit score
    if target_credit > 0:
        ratio   = net_credit / target_credit
        credit_s = min(100.0, ratio * 80)   # 80 at target, 100 at 125% of target
    else:
        credit_s = 50.0

    composite = (
        0.20 * timing_score +
        0.35 * iv_s         +
        0.25 * market_score +
        0.20 * credit_s
    )

    # Hard blocks
    if not tradeable:
        composite = min(composite, 20.0)

    composite = round(composite, 1)

    if composite >= 75:
        grade  = "A"
        action = "STRONG_BUY"
    elif composite >= 60:
        grade  = "B"
        action = "BUY"
    elif composite >= 45:
        grade  = "C"
        action = "HOLD"
    elif composite >= 25:
        grade  = "D"
        action = "SKIP"
    else:
        grade  = "F"
        action = "SKIP"

    # Reason string
    parts = []
    if iv_rank < min_iv_rank:
        parts.append(f"IV rank {iv_rank:.0f} below threshold {min_iv_rank:.0f}")
    if not tradeable:
        parts.append("market conditions block entry")
    if net_credit < target_credit * 0.5:
        parts.append(f"credit {net_credit:.1f} too low")
    if timing_score < 40:
        parts.append("poor entry timing window")
    if not parts:
        parts.append(f"all conditions satisfied (IV rank {iv_rank:.0f}, score {composite:.0f})")

    return OpportunityScore(
        composite=composite,
        timing_score=round(timing_score, 1),
        iv_rank_score=round(iv_s, 1),
        market_score=round(market_score, 1),
        credit_score=round(credit_s, 1),
        grade=grade,
        action=action,
        reason="; ".join(parts),
    )
