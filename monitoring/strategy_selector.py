"""
Strategy type selection based on current market conditions.

Rules:
  Iron Condor    — both wings; needs elevated IV rank (≥35) and VIX in a
                   comfortable range (11–24). Earns the most credit when viable.
  Bull Put Spread — single put wing; works at lower IV rank (≥30) and tolerates
                   higher VIX. Preferred when market bias is neutral-to-bullish.
  Bear Call Spread — single call wing; symmetric with BPS. Preferred when
                   market bias is neutral-to-bearish.

The caller should try all returned strategy types and let the composite
opportunity score decide the winner across all underlyings × strategies.
"""
from __future__ import annotations

from execution.models import StrategyType


def rank_strategy_types(
    iv_rank: float,
    vix: float | None,
    enabled: list[str] | None = None,
) -> list[StrategyType]:
    """
    Return viable strategy types ordered by general preference.
    IC and BWB first when conditions are ideal; spreads and calendar as alternatives.
    Empty list means no strategy is viable.
    """
    vix_val = vix if vix is not None else 17.0

    if iv_rank < 20:
        return []  # premium too thin for any strategy

    viable: list[StrategyType] = []

    # Iron Condor: elevated IV + moderate VIX → collect from both wings
    if iv_rank >= 35 and 11.0 <= vix_val <= 24.0:
        viable.append(StrategyType.IRON_CONDOR)

    # Broken Wing Butterfly: moderate-high IV, not extreme VIX
    if iv_rank >= 35 and vix_val <= 22.0:
        viable.append(StrategyType.BROKEN_WING_BUTTERFLY)

    # Vertical spreads: lower IV threshold, wider VIX tolerance
    if iv_rank >= 30 and vix_val <= 28.0:
        viable.append(StrategyType.BULL_PUT_SPREAD)
        viable.append(StrategyType.BEAR_CALL_SPREAD)

    # Calendar spread: benefits from time-decay differential and moderate IV
    # Works well when near-term IV is elevated relative to far-term
    if 20.0 <= iv_rank <= 65.0 and 11.0 <= vix_val <= 22.0:
        viable.append(StrategyType.CALENDAR_SPREAD)

    if enabled is not None:
        enabled_set = set(enabled)
        viable = [s for s in viable if s.value in enabled_set]

    return viable
