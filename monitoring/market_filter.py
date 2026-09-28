"""
Market condition filters for Iron Condor entry quality.

Components:
  - India VIX filter  (ideal range 12–22; too low = no premium, too high = dangerous)
  - Event calendar    (RBI policy, budget, FOMC, index rebalances, earnings)
  - Composite MarketScore (0–100)

The AutoTrader consults these before every entry scan and attaches the score
to each candidate so the Intelligence tab can display it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── VIX Filter ────────────────────────────────────────────────────────────────

@dataclass
class VIXReading:
    value: float
    score: float           # 0–100 contribution to composite score
    label: str             # "LOW" | "IDEAL" | "ELEVATED" | "HIGH" | "EXTREME"
    tradeable: bool        # False = skip entry regardless of other signals


def score_vix(
    vix: float,
    min_vix: float = 11.0,
    max_vix: float = 25.0,
    ideal_lo: float = 14.0,
    ideal_hi: float = 20.0,
) -> VIXReading:
    """
    Score India VIX for Iron Condor entry.

    Scoring logic:
      < min_vix  : LOW — premium too thin (score 20, tradeable=False)
      ideal zone : IDEAL — peak score 100
      max_vix–30 : ELEVATED — score decays linearly to 30
      > max_vix  : HIGH / EXTREME — score 0, tradeable=False
    """
    if vix < min_vix:
        return VIXReading(value=vix, score=20.0, label="LOW", tradeable=False)
    if vix > 30.0:
        return VIXReading(value=vix, score=0.0, label="EXTREME", tradeable=False)
    if vix > max_vix:
        # Linear decay from 30 (at max_vix) to 0 (at 30)
        decay = 1.0 - (vix - max_vix) / (30.0 - max_vix)
        return VIXReading(value=vix, score=max(0.0, decay * 30), label="HIGH", tradeable=False)
    if ideal_lo <= vix <= ideal_hi:
        # Inside ideal zone — peak at midpoint
        mid = (ideal_lo + ideal_hi) / 2.0
        dist = abs(vix - mid) / ((ideal_hi - ideal_lo) / 2.0)
        score = 100.0 - dist * 20.0     # 80–100 in ideal zone
        return VIXReading(value=vix, score=min(100.0, score), label="IDEAL", tradeable=True)
    # Outside ideal but within tradeable range — linear ramp
    if vix < ideal_lo:
        score = (vix - min_vix) / (ideal_lo - min_vix) * 60 + 20
        return VIXReading(value=vix, score=score, label="LOW", tradeable=True)
    # ideal_hi < vix <= max_vix
    score = 80.0 - (vix - ideal_hi) / (max_vix - ideal_hi) * 50
    return VIXReading(value=vix, score=max(30.0, score), label="ELEVATED", tradeable=True)


def fetch_india_vix(broker) -> Optional[float]:
    """Fetch India VIX from Zerodha. Returns float or None on failure."""
    try:
        raw = broker._kite.quote(["NSE:INDIA VIX"])
        v   = raw.get("NSE:INDIA VIX", {}).get("last_price")
        if v:
            return float(v)
        ltp_raw = broker._kite.ltp(["NSE:INDIA VIX"])
        ltp_v   = ltp_raw.get("NSE:INDIA VIX", {}).get("last_price")
        return float(ltp_v) if ltp_v else None
    except Exception as exc:
        logger.warning("vix_fetch_failed", extra={"error": str(exc)})
        return None


# ── Event Calendar ────────────────────────────────────────────────────────────

@dataclass
class MarketEvent:
    event_date: date
    name: str
    event_type: str        # "RBI" | "FOMC" | "BUDGET" | "EARNINGS" | "CUSTOM"
    affected: list[str]    # underlyings affected; empty = all indices


# Hardcoded known events for FY 2026–27 (update as needed)
KNOWN_EVENTS: list[MarketEvent] = [
    # RBI Monetary Policy Committee meetings
    MarketEvent(date(2026, 8, 6),   "RBI MPC Decision",       "RBI",    []),
    MarketEvent(date(2026, 10, 8),  "RBI MPC Decision",       "RBI",    []),
    MarketEvent(date(2026, 12, 5),  "RBI MPC Decision",       "RBI",    []),
    MarketEvent(date(2027, 2, 6),   "RBI MPC Decision",       "RBI",    []),
    MarketEvent(date(2027, 4, 9),   "RBI MPC Decision",       "RBI",    []),
    MarketEvent(date(2027, 6, 4),   "RBI MPC Decision",       "RBI",    []),
    # FOMC (global risk-off events)
    MarketEvent(date(2026, 9, 17),  "US FOMC Meeting",        "FOMC",   []),
    MarketEvent(date(2026, 11, 5),  "US FOMC Meeting",        "FOMC",   []),
    MarketEvent(date(2026, 12, 17), "US FOMC Meeting",        "FOMC",   []),
    MarketEvent(date(2027, 1, 29),  "US FOMC Meeting",        "FOMC",   []),
    MarketEvent(date(2027, 3, 19),  "US FOMC Meeting",        "FOMC",   []),
    # Union Budget
    MarketEvent(date(2027, 2, 1),   "Union Budget",           "BUDGET", []),
    # Nifty 50 rebalance (typically semi-annual)
    MarketEvent(date(2026, 9, 26),  "NIFTY Rebalance",        "CUSTOM", ["NIFTY"]),
    MarketEvent(date(2027, 3, 27),  "NIFTY Rebalance",        "CUSTOM", ["NIFTY"]),
]


@dataclass
class EventCheckResult:
    has_event: bool
    events: list[MarketEvent]
    days_to_nearest: Optional[int]
    score_penalty: float       # 0 = no impact; 100 = full block
    skip: bool                 # True = do not enter


def check_event_calendar(
    underlying: str,
    expiry: date,
    today: Optional[date] = None,
    blackout_days: int = 2,
    custom_events: Optional[list[MarketEvent]] = None,
) -> EventCheckResult:
    """
    Check whether any market event falls within the entry blackout window
    or before the expiry.

    blackout_days: Don't enter if an event is within this many calendar days.
    """
    if today is None:
        today = date.today()

    all_events = list(KNOWN_EVENTS)
    if custom_events:
        all_events.extend(custom_events)

    # Events that affect this underlying and fall within today→expiry
    relevant = [
        e for e in all_events
        if today <= e.event_date <= expiry
        and (not e.affected or underlying in e.affected or underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY"))
    ]

    if not relevant:
        return EventCheckResult(has_event=False, events=[], days_to_nearest=None,
                                score_penalty=0.0, skip=False)

    nearest = min(relevant, key=lambda e: e.event_date)
    days_to = (nearest.event_date - today).days

    if days_to <= blackout_days:
        penalty = 100.0
        skip    = True
    elif days_to <= 5:
        penalty = 50.0
        skip    = False
    elif days_to <= 10:
        penalty = 20.0
        skip    = False
    else:
        penalty = 5.0
        skip    = False

    return EventCheckResult(
        has_event=True,
        events=relevant,
        days_to_nearest=days_to,
        score_penalty=penalty,
        skip=skip,
    )


# ── Composite Market Score ────────────────────────────────────────────────────

@dataclass
class MarketConditionScore:
    composite: float          # 0–100 (higher = better IC entry conditions)
    vix_reading: Optional[VIXReading]
    event_check: Optional[EventCheckResult]
    tradeable: bool
    summary: str              # human-readable one-liner


def compute_market_score(
    underlying: str,
    expiry: date,
    broker=None,
    vix_cfg: Optional[dict] = None,
    event_cfg: Optional[dict] = None,
    custom_events: Optional[list[MarketEvent]] = None,
) -> MarketConditionScore:
    """
    Compute composite market condition score.

    vix_cfg keys: enabled, min, max, ideal_min, ideal_max
    event_cfg keys: enabled, days_before
    """
    vix_cfg    = vix_cfg    or {}
    event_cfg  = event_cfg  or {}

    vix_reading   : Optional[VIXReading]       = None
    event_result  : Optional[EventCheckResult] = None
    vix_score     = 70.0     # neutral default when VIX unavailable
    event_penalty = 0.0

    # ── VIX ──────────────────────────────────────────────────────────────────
    if vix_cfg.get("enabled", True) and broker is not None:
        vix_val = fetch_india_vix(broker)
        if vix_val is not None:
            vix_reading = score_vix(
                vix_val,
                min_vix   = float(vix_cfg.get("min",      11.0)),
                max_vix   = float(vix_cfg.get("max",      25.0)),
                ideal_lo  = float(vix_cfg.get("ideal_min",14.0)),
                ideal_hi  = float(vix_cfg.get("ideal_max",20.0)),
            )
            vix_score = vix_reading.score

    # ── Events ───────────────────────────────────────────────────────────────
    if event_cfg.get("enabled", True):
        blackout = int(event_cfg.get("days_before", 2))
        event_result = check_event_calendar(
            underlying=underlying, expiry=expiry,
            blackout_days=blackout, custom_events=custom_events,
        )
        event_penalty = event_result.score_penalty if event_result else 0.0

    # ── Composite ────────────────────────────────────────────────────────────
    raw = max(0.0, vix_score - event_penalty)

    tradeable = True
    if vix_reading is not None and not vix_reading.tradeable:
        tradeable = False
    if event_result is not None and event_result.skip:
        tradeable = False

    # Build summary
    parts = []
    if vix_reading:
        parts.append(f"VIX {vix_reading.value:.1f} ({vix_reading.label})")
    if event_result and event_result.has_event:
        nearest = event_result.events[0]
        parts.append(f"Event in {event_result.days_to_nearest}d: {nearest.name}")

    summary = " | ".join(parts) if parts else "No market data"

    return MarketConditionScore(
        composite=round(raw, 1),
        vix_reading=vix_reading,
        event_check=event_result,
        tradeable=tradeable,
        summary=summary,
    )
