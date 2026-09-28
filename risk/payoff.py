"""
Iron condor payoff calculations.
Uses Decimal throughout. No floats.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from execution.models import Fill, LegConfig, LegPosition, Side, StrategyConfig


@dataclass
class PayoffResult:
    max_profit: Decimal
    max_loss: Decimal
    upper_breakeven: Decimal
    lower_breakeven: Decimal
    net_credit_per_share: Decimal
    net_credit_total: Decimal
    lot_size: int
    lots: int


@dataclass
class ExecutionCredit:
    buy_cost: Decimal           # sum of fill prices for BUY legs
    sell_credit: Decimal        # sum of fill prices for SELL legs
    net_credit: Decimal         # sell_credit - buy_cost
    quantity: int
    net_credit_total: Decimal   # net_credit × quantity


def calculate_theoretical_payoff(
    strategy: StrategyConfig,
    quotes: dict[int, "Quote"],
    lot_size: int,
    use_midpoint: bool = True,
) -> PayoffResult:
    """
    Compute theoretical payoff structure from strategy legs.
    Works for any strategy type (IC, spread, calendar, BWB).
    Returns zero-credit placeholder — actual credit comes from fills.
    """
    legs = strategy.legs
    quantity = lot_size * strategy.lots

    buy_pe_strikes  = sorted([l.strike for l in legs if l.side == Side.BUY  and l.option_type.value == "PE"])
    sell_pe_strikes = sorted([l.strike for l in legs if l.side == Side.SELL and l.option_type.value == "PE"])
    buy_ce_strikes  = sorted([l.strike for l in legs if l.side == Side.BUY  and l.option_type.value == "CE"])
    sell_ce_strikes = sorted([l.strike for l in legs if l.side == Side.SELL and l.option_type.value == "CE"])

    wing_widths: list[Decimal] = []
    if buy_pe_strikes and sell_pe_strikes:
        wing_widths.append(max(sell_pe_strikes) - min(buy_pe_strikes))
    if buy_ce_strikes and sell_ce_strikes:
        wing_widths.append(max(buy_ce_strikes) - min(sell_ce_strikes))

    all_strikes = [l.strike for l in legs if l.strike and l.strike > 0]
    if wing_widths:
        spread_width = min(wing_widths)
    elif len(all_strikes) >= 2:
        spread_width = max(all_strikes) - min(all_strikes)
    else:
        spread_width = Decimal("100")

    net_credit_per_share = Decimal("0")

    sell_pe = [l for l in legs if l.side == Side.SELL and l.option_type.value == "PE"]
    sell_ce = [l for l in legs if l.side == Side.SELL and l.option_type.value == "CE"]
    lower_be = sell_pe[0].strike - net_credit_per_share if sell_pe else Decimal("0")
    upper_be = sell_ce[0].strike + net_credit_per_share if sell_ce else Decimal("0")

    return PayoffResult(
        max_profit=net_credit_per_share * quantity,
        max_loss=(spread_width - net_credit_per_share) * quantity,
        upper_breakeven=upper_be,
        lower_breakeven=lower_be,
        net_credit_per_share=net_credit_per_share,
        net_credit_total=net_credit_per_share * quantity,
        lot_size=lot_size,
        lots=strategy.lots,
    )


def calculate_payoff_from_credit(
    strategy: StrategyConfig,
    net_credit_per_share: Decimal,
    lot_size: int,
) -> PayoffResult:
    """
    Calculate payoff given a known net credit per share.
    Works for any strategy type — does not assume a specific leg structure.
    """
    legs = strategy.legs
    quantity = lot_size * strategy.lots

    # Compute spread_width as the minimum width of any defined-risk wing pair.
    # For IC: min(put_spread, call_spread). For 2-leg: width of that spread.
    # For other structures: fallback to max-min of all strikes.
    buy_pe_strikes  = sorted([l.strike for l in legs if l.side == Side.BUY  and l.option_type.value == "PE"])
    sell_pe_strikes = sorted([l.strike for l in legs if l.side == Side.SELL and l.option_type.value == "PE"])
    buy_ce_strikes  = sorted([l.strike for l in legs if l.side == Side.BUY  and l.option_type.value == "CE"])
    sell_ce_strikes = sorted([l.strike for l in legs if l.side == Side.SELL and l.option_type.value == "CE"])

    wing_widths: list[Decimal] = []
    if buy_pe_strikes and sell_pe_strikes:
        wing_widths.append(max(sell_pe_strikes) - min(buy_pe_strikes))
    if buy_ce_strikes and sell_ce_strikes:
        wing_widths.append(max(buy_ce_strikes) - min(sell_ce_strikes))

    all_strikes = [l.strike for l in legs if l.strike and l.strike > 0]
    if wing_widths:
        spread_width = min(wing_widths)
    elif len(all_strikes) >= 2:
        spread_width = max(all_strikes) - min(all_strikes)
    else:
        spread_width = Decimal("100")

    sell_pe = [l for l in legs if l.side == Side.SELL and l.option_type.value == "PE"]
    sell_ce = [l for l in legs if l.side == Side.SELL and l.option_type.value == "CE"]
    lower_be = sell_pe[0].strike - net_credit_per_share if sell_pe else Decimal("0")
    upper_be = sell_ce[0].strike + net_credit_per_share if sell_ce else Decimal("0")

    return PayoffResult(
        max_profit=net_credit_per_share * quantity,
        max_loss=(spread_width - net_credit_per_share) * quantity,
        upper_breakeven=upper_be,
        lower_breakeven=lower_be,
        net_credit_per_share=net_credit_per_share,
        net_credit_total=net_credit_per_share * quantity,
        lot_size=lot_size,
        lots=strategy.lots,
    )


def calculate_executable_credit(
    legs: list[LegConfig],
    quotes: dict[str, "Quote"],  # trading_symbol → Quote
    lot_size: int,
    lots: int,
) -> ExecutionCredit:
    """
    Calculate executable net credit using conservative bid/ask prices.
    BUY legs cost: ask price (worst case for buyer).
    SELL legs earn: bid price (worst case for seller).
    """
    buy_cost = Decimal("0")
    sell_credit = Decimal("0")
    quantity = lot_size * lots

    for leg in legs:
        q = quotes.get(leg.option_type.value)  # keyed by trading symbol — caller must pass correctly
        if q is None:
            raise ValueError(f"No quote for leg {leg}")
        if leg.side == Side.BUY:
            buy_cost += q.ask  # we pay ask
        else:
            sell_credit += q.bid  # we receive bid

    net = sell_credit - buy_cost
    return ExecutionCredit(
        buy_cost=buy_cost,
        sell_credit=sell_credit,
        net_credit=net,
        quantity=quantity,
        net_credit_total=net * quantity,
    )


def calculate_cost_to_close(positions: list[LegPosition]) -> Decimal:
    """
    Conservative cost to close all legs.
    Short legs: we pay the ask to buy back.
    Long legs: we receive the bid when selling.
    Returns net debit (positive = we pay, negative = we receive).
    """
    total = Decimal("0")
    for pos in positions:
        close_price = pos.conservative_close_price
        if close_price is None:
            raise ValueError(f"No current quote for position {pos.trading_symbol}")
        if pos.side == Side.SELL:
            # Buying back short: pay ask × quantity
            total += close_price * pos.quantity
        else:
            # Selling long: receive bid × quantity (reduces debit)
            total -= close_price * pos.quantity
    return total


def calculate_mtm_pnl(
    entry_credit_total: Decimal,
    cost_to_close: Decimal,
) -> Decimal:
    """
    Unrealized P&L = entry credit received - current cost to close.
    Positive means profit.
    """
    return entry_credit_total - cost_to_close
