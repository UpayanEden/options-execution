"""
Stock picker and expected-move iron condor builder.
Reads NFO instrument master, computes expected-move IC structure,
fetches live quotes when available.
Does NOT modify any config files — that is the dashboard's job.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_DOWN
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class ICCandidate:
    """Fully specified iron condor candidate for one underlying."""
    underlying: str
    expiry: date
    spot: Optional[Decimal]
    iv: Decimal                    # annualised IV assumption (e.g. 0.35)
    dte: int
    expected_move: Optional[Decimal]  # in points

    buy_put_strike: Decimal
    sell_put_strike: Decimal
    sell_call_strike: Decimal
    buy_call_strike: Decimal

    lot_size: int = 0

    # Filled after quote fetch
    buy_put_ask: Optional[Decimal] = None
    sell_put_bid: Optional[Decimal] = None
    sell_call_bid: Optional[Decimal] = None
    buy_call_ask: Optional[Decimal] = None

    net_credit: Optional[Decimal] = None          # per share
    max_profit: Optional[Decimal] = None          # total INR
    max_loss: Optional[Decimal] = None            # total INR
    put_spread_width: Optional[Decimal] = None
    call_spread_width: Optional[Decimal] = None
    return_on_risk: Optional[Decimal] = None      # max_profit / max_loss %
    estimated_margin: Optional[Decimal] = None

    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0 and self.net_credit is not None and self.net_credit > 0


@dataclass
class SpreadCandidate:
    """Fully specified vertical spread candidate (bull put or bear call)."""
    underlying: str
    expiry: date
    spot: Optional[Decimal]
    iv: Decimal
    dte: int
    expected_move: Optional[Decimal]
    option_type: str          # "PE" = bull put spread, "CE" = bear call spread
    sell_strike: Decimal      # short leg
    buy_strike: Decimal       # long leg (protection)
    lot_size: int = 0

    net_credit: Optional[Decimal] = None
    spread_width: Optional[Decimal] = None
    return_on_risk: Optional[Decimal] = None

    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0 and self.net_credit is not None and self.net_credit > 0


@dataclass
class CalendarCandidate:
    """Vertical calendar spread: SELL near-expiry option, BUY far-expiry option at same strike."""
    underlying: str
    near_expiry: date          # SELL leg (shorter dated)
    far_expiry: date           # BUY leg (longer dated)
    strike: Decimal
    option_type: str           # "CE" or "PE"
    spot: Optional[Decimal]
    iv: Decimal
    dte: int                   # DTE to near expiry
    lot_size: int = 0
    near_bid: Optional[Decimal] = None
    far_ask: Optional[Decimal] = None
    net_credit: Optional[Decimal] = None   # near_bid - far_ask (positive = credit)
    return_on_risk: Optional[Decimal] = None   # near_bid / far_ask * 100
    errors: list[str] = field(default_factory=list)


@dataclass
class BWBCandidate:
    """Broken Wing Butterfly (put): BUY upper + SELL 2× mid + BUY lower (wider lower wing)."""
    underlying: str
    expiry: date
    spot: Optional[Decimal]
    iv: Decimal
    dte: int
    expected_move: Optional[Decimal]
    option_type: str           # "PE" (put BWB)
    upper_strike: Decimal      # BUY (closer to ATM)
    mid_strike: Decimal        # SELL ×2 (OTM)
    lower_strike: Decimal      # BUY (far OTM, wider lower wing)
    lot_size: int = 0
    net_credit: Optional[Decimal] = None
    upper_wing_width: Optional[Decimal] = None   # upper_strike - mid_strike
    lower_wing_width: Optional[Decimal] = None   # mid_strike - lower_strike
    return_on_risk: Optional[Decimal] = None
    errors: list[str] = field(default_factory=list)


def compute_expected_move(spot: Decimal, iv: Decimal, dte: int) -> Decimal:
    """Expected move = IV × spot × sqrt(DTE / 365)"""
    if dte <= 0 or spot <= 0 or iv <= 0:
        return Decimal("0")
    em = float(iv) * float(spot) * math.sqrt(dte / 365.0)
    return Decimal(str(round(em, 2)))


def round_to_strike_interval(price: Decimal, interval: Decimal) -> Decimal:
    """Round price to the nearest valid strike interval."""
    if interval <= 0:
        return price
    return (price / interval).to_integral_value(rounding=ROUND_HALF_EVEN) * interval


def detect_strike_interval(strikes: list[float]) -> Decimal:
    """Detect the common strike interval from a list of strikes."""
    if len(strikes) < 2:
        return Decimal("5")
    sorted_s = sorted(set(strikes))
    diffs = [round(sorted_s[i+1] - sorted_s[i], 2) for i in range(min(10, len(sorted_s)-1))]
    if not diffs:
        return Decimal("5")
    # Most common difference
    from collections import Counter
    common = Counter(diffs).most_common(1)[0][0]
    return Decimal(str(common))


def build_ic_candidate(
    underlying: str,
    expiry: date,
    spot: Decimal,
    iv: Decimal,
    short_mult: float,
    long_mult: float,
    available_strikes: list[Decimal],  # sorted list of available strikes for this expiry
    lot_size: int,
    lots: int = 1,
) -> ICCandidate:
    """
    Build an IC candidate using expected-move strike selection.
    short_mult: short wings at (short_mult × EM) from spot
    long_mult:  long wings at (long_mult × EM) from spot
    """
    dte = max(1, (expiry - date.today()).days)
    em = compute_expected_move(spot, iv, dte)
    em_f = float(em)
    spot_f = float(spot)

    # Ideal strike prices
    ideal_sell_put = spot_f - short_mult * em_f
    ideal_sell_call = spot_f + short_mult * em_f
    ideal_buy_put = spot_f - long_mult * em_f
    ideal_buy_call = spot_f + long_mult * em_f

    def nearest_strike(target: float, strikes: list[Decimal]) -> Optional[Decimal]:
        if not strikes:
            return None
        return min(strikes, key=lambda s: abs(float(s) - target))

    sell_put = nearest_strike(ideal_sell_put, available_strikes)
    sell_call = nearest_strike(ideal_sell_call, available_strikes)
    buy_put = nearest_strike(ideal_buy_put, [s for s in available_strikes if sell_put and s < sell_put])
    buy_call = nearest_strike(ideal_buy_call, [s for s in available_strikes if sell_call and s > sell_call])

    candidate = ICCandidate(
        underlying=underlying,
        expiry=expiry,
        spot=spot,
        iv=iv,
        dte=dte,
        expected_move=em,
        buy_put_strike=buy_put or Decimal("0"),
        sell_put_strike=sell_put or Decimal("0"),
        sell_call_strike=sell_call or Decimal("0"),
        buy_call_strike=buy_call or Decimal("0"),
        lot_size=lot_size,
    )

    if not all([buy_put, sell_put, sell_call, buy_call]):
        candidate.errors.append("Could not find valid strikes for all legs")
        return candidate

    # Spread widths
    candidate.put_spread_width = sell_put - buy_put
    candidate.call_spread_width = buy_call - sell_call

    return candidate


def fill_candidate_quotes(candidate: ICCandidate, quotes: dict[str, "Quote"]) -> ICCandidate:
    """
    Fill quote data into a candidate. quotes keyed by trading_symbol.
    Computes net_credit, max_profit, max_loss, return_on_risk.
    """
    from execution.models import Side

    def find_quote(strike: Decimal, opt_type: str) -> Optional["Quote"]:
        # Try to match any key that contains the strike and option type
        strike_int = int(strike)
        for sym, q in quotes.items():
            if str(strike_int) in sym and sym.endswith(opt_type):
                return q
        return None

    bp_q = find_quote(candidate.buy_put_strike, "PE")
    sp_q = find_quote(candidate.sell_put_strike, "PE")
    sc_q = find_quote(candidate.sell_call_strike, "CE")
    bc_q = find_quote(candidate.buy_call_strike, "CE")

    if not all([bp_q, sp_q, sc_q, bc_q]):
        candidate.errors.append("Could not match quotes to all legs")
        return candidate

    candidate.buy_put_ask = bp_q.ask
    candidate.sell_put_bid = sp_q.bid
    candidate.sell_call_bid = sc_q.bid
    candidate.buy_call_ask = bc_q.ask

    buy_cost = candidate.buy_put_ask + candidate.buy_call_ask
    sell_credit = candidate.sell_put_bid + candidate.sell_call_bid
    net = sell_credit - buy_cost
    candidate.net_credit = net

    quantity = candidate.lot_size * 1  # 1 lot
    spread_width = min(candidate.put_spread_width, candidate.call_spread_width)
    candidate.max_profit = net * quantity
    candidate.max_loss = (spread_width - net) * quantity

    if candidate.max_loss and candidate.max_loss > 0:
        candidate.return_on_risk = (candidate.max_profit / candidate.max_loss) * 100

    return candidate


def get_nfo_underlyings_from_cache(cache_path: str) -> list[str]:
    """
    Read unique underlying names from a cached NFO instruments CSV.
    Returns sorted list of underlying names.
    """
    import csv
    underlyings = set()
    try:
        with open(cache_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("instrument_type") in ("CE", "PE"):
                    name = row.get("name", "").strip()
                    if name:
                        underlyings.add(name)
    except Exception:
        pass
    return sorted(underlyings)


def get_expiries_for_underlying(cache_path: str, underlying: str) -> list[date]:
    """Get all available expiries for an underlying from instrument cache."""
    import csv
    expiries = set()
    try:
        with open(cache_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("name", "").strip().upper() != underlying.upper():
                    continue
                if row.get("instrument_type") not in ("CE", "PE"):
                    continue
                exp_str = row.get("expiry", "")
                try:
                    exp = date.fromisoformat(exp_str[:10])
                    if exp >= date.today():
                        expiries.add(exp)
                except ValueError:
                    pass
    except Exception:
        pass
    return sorted(expiries)


def get_strikes_for_underlying_expiry(
    cache_path: str, underlying: str, expiry: date, opt_type: str
) -> list[Decimal]:
    """Get all available strikes for underlying+expiry+CE/PE from cache."""
    import csv
    strikes = set()
    try:
        with open(cache_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("name", "").strip().upper() != underlying.upper():
                    continue
                if row.get("instrument_type") != opt_type.upper():
                    continue
                exp_str = row.get("expiry", "")
                try:
                    exp = date.fromisoformat(exp_str[:10])
                except ValueError:
                    continue
                if exp != expiry:
                    continue
                try:
                    s = Decimal(str(float(row.get("strike", 0))))
                    if s > 0:
                        strikes.add(s)
                except Exception:
                    pass
    except Exception:
        pass
    return sorted(strikes)


def auto_select_ic_strikes(
    underlying: str,
    expiry: date,
    spot: Decimal,
    iv: Decimal,
    available_strikes: list[Decimal],
    lot_size: int,
    lots: int,
    min_credit: Decimal,
    broker,                           # ZerodhaBroker (or any with resolve_instrument + get_quotes)
    exchange: str = "NFO",
    short_mult_start: float = 1.0,
    short_mult_min: float = 0.3,
    short_mult_step: float = 0.1,
    long_mult_ratio: float = 1.3,
    min_ror_pct: Decimal = Decimal("15.0"),
) -> tuple[Optional["ICCandidate"], list[dict]]:
    """
    Sweep short-wing multipliers from wide to tight until net credit >= min_credit
    AND return-on-risk >= min_ror_pct.

    Algorithm:
      For mult in [short_mult_start, ..., short_mult_min] (step = short_mult_step):
        1. Build IC candidate (selects nearest available strikes)
        2. Resolve real instruments via broker
        3. Fetch live quotes
        4. Compute net credit = sum(SELL bids) - sum(BUY asks)
        5. Compute RoR = credit / (spread_width - credit) * 100
        6. If credit >= min_credit AND RoR >= min_ror_pct AND spreads valid → return candidate
        7. Else tighten mult and retry

    Returns (winning_candidate | None, log_rows)
    Each log row: {mult, sell_put, sell_call, credit, ror, status, errors}
    """
    from execution.models import OptionType
    from execution.quote_engine import QuoteEngine

    qe = QuoteEngine(broker)
    log: list[dict] = []

    mult = short_mult_start
    while mult >= short_mult_min - 1e-9:
        mult_r = round(mult, 2)
        long_mult = round(mult * long_mult_ratio, 2)

        cand = build_ic_candidate(
            underlying=underlying,
            expiry=expiry,
            spot=spot,
            iv=iv,
            short_mult=mult_r,
            long_mult=long_mult,
            available_strikes=available_strikes,
            lot_size=lot_size,
            lots=lots,
        )

        if cand.errors:
            log.append({
                "mult": mult_r,
                "sell_put": None, "sell_call": None,
                "credit": None, "status": "❌ strike error",
                "errors": cand.errors,
            })
            mult -= short_mult_step
            continue

        # Resolve instruments for all 4 legs
        legs = [
            (cand.buy_put_strike,  "PE", "BUY"),
            (cand.sell_put_strike, "PE", "SELL"),
            (cand.sell_call_strike,"CE", "SELL"),
            (cand.buy_call_strike, "CE", "BUY"),
        ]
        instruments = []
        resolved_map: dict[int, object] = {}
        resolve_error = None
        for idx, (strike, opt_type, _side) in enumerate(legs):
            try:
                inst = broker.resolve_instrument(
                    underlying=underlying,
                    exchange=exchange,
                    expiry=expiry,
                    strike=strike,
                    option_type=opt_type,
                )
                resolved_map[idx] = inst
                instruments.append((inst.instrument_token, inst.trading_symbol))
            except Exception as exc:
                resolve_error = str(exc)
                break

        if resolve_error:
            log.append({
                "mult": mult_r,
                "sell_put": float(cand.sell_put_strike),
                "sell_call": float(cand.sell_call_strike),
                "credit": None, "status": f"❌ resolve: {resolve_error}",
                "errors": [resolve_error],
            })
            mult -= short_mult_step
            continue

        # Fetch quotes
        try:
            quotes = qe.fetch_all(instruments)
        except Exception as exc:
            log.append({
                "mult": mult_r,
                "sell_put": float(cand.sell_put_strike),
                "sell_call": float(cand.sell_call_strike),
                "credit": None, "status": f"❌ quote error: {exc}",
                "errors": [str(exc)],
            })
            mult -= short_mult_step
            continue

        # Compute credit and check spreads
        buy_cost    = Decimal("0")
        sell_credit = Decimal("0")
        spread_errors = []
        for idx, (strike, opt_type, side) in enumerate(legs):
            inst = resolved_map[idx]
            q = quotes.get(inst.trading_symbol)
            if q is None:
                spread_errors.append(f"No quote for {inst.trading_symbol}")
                continue
            if side == "SELL":
                sell_credit += q.bid
            else:
                buy_cost += q.ask
            # Max spread check: 15% hard cap per leg (caller enforces risk config separately)
            if q.spread_pct > Decimal("15"):
                spread_errors.append(f"{inst.trading_symbol}: spread {float(q.spread_pct):.1f}% very wide")

        net_credit = sell_credit - buy_cost

        # Return-on-risk: use the tighter of the two spread widths as max loss
        spread_width = min(
            cand.sell_put_strike  - cand.buy_put_strike,
            cand.buy_call_strike  - cand.sell_call_strike,
        )
        max_loss = spread_width - net_credit
        ror = (net_credit / max_loss * 100) if max_loss > 0 else Decimal("0")

        passes_credit = net_credit >= min_credit
        passes_ror    = ror >= min_ror_pct
        passes_spread = not spread_errors

        if passes_credit and passes_ror and passes_spread:
            status = "✅ selected"
        elif not passes_credit:
            status = f"❌ credit {float(net_credit):.2f} < {float(min_credit):.2f}"
        elif not passes_ror:
            status = f"❌ RoR {float(ror):.1f}% < {float(min_ror_pct):.0f}%"
        else:
            status = f"⚠️ spread issues: {'; '.join(spread_errors)}"

        log.append({
            "mult": mult_r,
            "sell_put":  float(cand.sell_put_strike),
            "sell_call": float(cand.sell_call_strike),
            "buy_put":   float(cand.buy_put_strike),
            "buy_call":  float(cand.buy_call_strike),
            "credit":    float(net_credit),
            "ror":       float(ror),
            "status":    status,
            "errors":    spread_errors,
            "candidate": cand,
            "resolved":  resolved_map,
            "quotes":    quotes,
        })

        if passes_credit and passes_ror and passes_spread:
            return cand, log

        mult -= short_mult_step

    return None, log


def build_spread_candidate(
    underlying: str,
    expiry: date,
    spot: Decimal,
    iv: Decimal,
    short_mult: float,
    long_mult: float,
    available_strikes: list[Decimal],
    lot_size: int,
    lots: int = 1,
    option_type: str = "PE",
) -> SpreadCandidate:
    """
    Build a vertical spread candidate using expected-move strike selection.

    option_type="PE" → Bull Put Spread: sell higher put, buy lower put
    option_type="CE" → Bear Call Spread: sell lower call, buy higher call

    short_mult: short leg at (short_mult × EM) OTM from spot
    long_mult:  long leg at (long_mult × EM) OTM from spot (further out)
    """
    dte = max(1, (expiry - date.today()).days)
    em = compute_expected_move(spot, iv, dte)
    em_f = float(em)
    spot_f = float(spot)

    def nearest_strike(target: float, strikes: list[Decimal]) -> Optional[Decimal]:
        if not strikes:
            return None
        return min(strikes, key=lambda s: abs(float(s) - target))

    if option_type == "PE":
        # Bull Put Spread: both legs are puts
        # sell_strike is closer to spot (higher premium), buy_strike is further OTM (protection)
        ideal_sell = spot_f - short_mult * em_f
        ideal_buy  = spot_f - long_mult  * em_f
        sell_strike = nearest_strike(ideal_sell, available_strikes)
        buy_strike  = nearest_strike(ideal_buy, [s for s in available_strikes if sell_strike and s < sell_strike])
    else:
        # Bear Call Spread: both legs are calls
        # sell_strike is closer to spot (higher premium), buy_strike is further OTM (protection)
        ideal_sell = spot_f + short_mult * em_f
        ideal_buy  = spot_f + long_mult  * em_f
        sell_strike = nearest_strike(ideal_sell, available_strikes)
        buy_strike  = nearest_strike(ideal_buy, [s for s in available_strikes if sell_strike and s > sell_strike])

    cand = SpreadCandidate(
        underlying=underlying,
        expiry=expiry,
        spot=spot,
        iv=iv,
        dte=dte,
        expected_move=em,
        option_type=option_type,
        sell_strike=sell_strike or Decimal("0"),
        buy_strike=buy_strike or Decimal("0"),
        lot_size=lot_size,
    )

    if not sell_strike or not buy_strike:
        cand.errors.append("Could not find valid strikes for both legs")
    elif option_type == "PE" and buy_strike >= sell_strike:
        cand.errors.append(f"Invalid PE spread: buy {buy_strike} >= sell {sell_strike}")
    elif option_type == "CE" and buy_strike <= sell_strike:
        cand.errors.append(f"Invalid CE spread: buy {buy_strike} <= sell {sell_strike}")

    return cand


def auto_select_vertical_spread(
    underlying: str,
    expiry: date,
    spot: Decimal,
    iv: Decimal,
    available_strikes: list[Decimal],
    lot_size: int,
    lots: int,
    option_type: str,           # "PE" = bull put spread, "CE" = bear call spread
    min_credit: Decimal,
    broker,
    exchange: str = "NFO",
    short_mult_start: float = 0.7,
    short_mult_min: float = 0.2,
    short_mult_step: float = 0.1,
    long_mult_ratio: float = 1.4,
    min_ror_pct: Decimal = Decimal("15.0"),
) -> tuple[Optional[SpreadCandidate], list[dict]]:
    """
    Sweep short-wing multipliers for a vertical spread (wide → tight) until
    net credit >= min_credit AND return-on-risk >= min_ror_pct.

    Leg index ordering (matches enter_best legs_cfg):
      Bull Put Spread (PE):   index 0 = BUY lower put,  index 1 = SELL higher put
      Bear Call Spread (CE):  index 0 = SELL lower call, index 1 = BUY higher call
    """
    from execution.quote_engine import QuoteEngine

    qe = QuoteEngine(broker)
    log: list[dict] = []
    label = "BPS" if option_type == "PE" else "BCS"

    mult = short_mult_start
    while mult >= short_mult_min - 1e-9:
        mult_r = round(mult, 2)
        long_mult = round(mult * long_mult_ratio, 2)

        cand = build_spread_candidate(
            underlying=underlying,
            expiry=expiry,
            spot=spot,
            iv=iv,
            short_mult=mult_r,
            long_mult=long_mult,
            available_strikes=available_strikes,
            lot_size=lot_size,
            lots=lots,
            option_type=option_type,
        )

        if cand.errors:
            log.append({
                "mult": mult_r, "strategy": label,
                "sell": None, "buy": None, "credit": None,
                "status": "❌ strike error", "errors": cand.errors,
            })
            mult -= short_mult_step
            continue

        # Leg ordering consistent with how enter_best() builds legs_cfg:
        #   PE spread: [BUY lower put (idx 0), SELL higher put (idx 1)]
        #   CE spread: [SELL lower call (idx 0), BUY higher call (idx 1)]
        if option_type == "PE":
            legs = [
                (cand.buy_strike,  "PE", "BUY"),   # index 0
                (cand.sell_strike, "PE", "SELL"),   # index 1
            ]
        else:
            legs = [
                (cand.sell_strike, "CE", "SELL"),  # index 0
                (cand.buy_strike,  "CE", "BUY"),   # index 1
            ]

        # Resolve instruments
        instruments = []
        resolved_map: dict[int, object] = {}
        resolve_error = None
        for idx, (strike, opt_type, _side) in enumerate(legs):
            try:
                inst = broker.resolve_instrument(
                    underlying=underlying,
                    exchange=exchange,
                    expiry=expiry,
                    strike=strike,
                    option_type=opt_type,
                )
                resolved_map[idx] = inst
                instruments.append((inst.instrument_token, inst.trading_symbol))
            except Exception as exc:
                resolve_error = str(exc)
                break

        if resolve_error:
            log.append({
                "mult": mult_r, "strategy": label,
                "sell": float(cand.sell_strike), "buy": float(cand.buy_strike),
                "credit": None, "status": f"❌ resolve: {resolve_error}",
                "errors": [resolve_error],
            })
            mult -= short_mult_step
            continue

        # Fetch quotes
        try:
            quotes = qe.fetch_all(instruments)
        except Exception as exc:
            log.append({
                "mult": mult_r, "strategy": label,
                "sell": float(cand.sell_strike), "buy": float(cand.buy_strike),
                "credit": None, "status": f"❌ quote error: {exc}",
                "errors": [str(exc)],
            })
            mult -= short_mult_step
            continue

        # Compute credit
        buy_cost = Decimal("0")
        sell_credit = Decimal("0")
        spread_errors = []
        for idx, (strike, opt_type, side) in enumerate(legs):
            inst = resolved_map[idx]
            q = quotes.get(inst.trading_symbol)
            if q is None:
                spread_errors.append(f"No quote for {inst.trading_symbol}")
                continue
            if side == "SELL":
                sell_credit += q.bid
            else:
                buy_cost += q.ask
            if q.spread_pct > Decimal("15"):
                spread_errors.append(f"{inst.trading_symbol}: spread {float(q.spread_pct):.1f}% very wide")

        net_credit = sell_credit - buy_cost
        spread_width = abs(cand.sell_strike - cand.buy_strike)
        max_loss = spread_width - net_credit
        ror = (net_credit / max_loss * 100) if max_loss > 0 else Decimal("0")

        passes_credit = net_credit >= min_credit
        passes_ror    = ror >= min_ror_pct
        passes_spread = not spread_errors

        if passes_credit and passes_ror and passes_spread:
            status = "✅ selected"
        elif not passes_credit:
            status = f"❌ credit {float(net_credit):.2f} < {float(min_credit):.2f}"
        elif not passes_ror:
            status = f"❌ RoR {float(ror):.1f}% < {float(min_ror_pct):.0f}%"
        else:
            status = f"⚠️ spread issues: {'; '.join(spread_errors)}"

        cand.net_credit   = net_credit
        cand.spread_width = spread_width
        cand.return_on_risk = ror

        log.append({
            "mult": mult_r, "strategy": label,
            "sell": float(cand.sell_strike), "buy": float(cand.buy_strike),
            "credit": float(net_credit), "ror": float(ror),
            "status": status, "errors": spread_errors,
            "candidate": cand, "resolved": resolved_map, "quotes": quotes,
        })

        if passes_credit and passes_ror and passes_spread:
            return cand, log

        mult -= short_mult_step

    return None, log


def get_all_nfo_underlyings(cache_path: str) -> list[str]:
    """Return all unique underlying names that have options in the NFO cache."""
    import csv
    seen: dict[str, bool] = {}
    try:
        with open(cache_path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("instrument_type") not in ("CE", "PE"):
                    continue
                name = row.get("name", "").strip()
                if name:
                    seen[name] = True
    except Exception:
        pass
    return sorted(seen.keys())


def get_lot_size_for_underlying(cache_path: str, underlying: str) -> int:
    """Get lot size from instrument cache."""
    import csv
    try:
        with open(cache_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("name", "").strip().upper() != underlying.upper():
                    continue
                if row.get("instrument_type") not in ("CE", "PE"):
                    continue
                lot = int(float(row.get("lot_size", 0)))
                if lot > 0:
                    return lot
    except Exception:
        pass
    return 0


def auto_select_calendar_spread(
    underlying: str,
    near_expiry: date,
    far_expiry: date,
    spot: Decimal,
    available_strikes: list[Decimal],
    lot_size: int,
    lots: int,
    option_type: str,           # "CE" or "PE"
    min_credit: Decimal,
    broker,
    exchange: str = "NFO",
    min_ror_pct: Decimal = Decimal("30.0"),
) -> tuple[Optional[CalendarCandidate], list[dict]]:
    """
    Select a calendar spread: SELL near-expiry ATM option, BUY far-expiry same-strike option.
    Profitable when near-term time decay exceeds far-term decay.

    Leg index ordering (matches enter_best legs_cfg):
      index 0 = SELL near-expiry option
      index 1 = BUY  far-expiry option

    Only selects if net_credit >= min_credit AND (near_bid / far_ask * 100) >= min_ror_pct.
    """
    from execution.quote_engine import QuoteEngine
    qe = QuoteEngine(broker)
    log: list[dict] = []

    if not available_strikes:
        return None, [{"status": "❌ no strikes available", "errors": ["empty strikes"]}]

    dte = max(1, (near_expiry - date.today()).days)

    # Try strikes from ATM outward (ATM first, then 1 step up/down)
    atm = min(available_strikes, key=lambda s: abs(float(s) - float(spot)))
    atm_idx = available_strikes.index(atm)
    ordered_strikes = [atm]
    lo, hi = atm_idx - 1, atm_idx + 1
    while lo >= 0 or hi < len(available_strikes):
        if hi < len(available_strikes):
            ordered_strikes.append(available_strikes[hi]); hi += 1
        if lo >= 0:
            ordered_strikes.append(available_strikes[lo]); lo -= 1

    for strike in ordered_strikes[:5]:   # check at most 5 strike candidates
        # Resolve near-expiry instrument (SELL leg, index 0)
        try:
            near_inst = broker.resolve_instrument(
                underlying=underlying, exchange=exchange,
                expiry=near_expiry, strike=strike, option_type=option_type,
            )
        except Exception as exc:
            log.append({"strike": float(strike), "status": f"❌ resolve near: {exc}", "errors": [str(exc)]})
            continue

        # Resolve far-expiry instrument (BUY leg, index 1)
        try:
            far_inst = broker.resolve_instrument(
                underlying=underlying, exchange=exchange,
                expiry=far_expiry, strike=strike, option_type=option_type,
            )
        except Exception as exc:
            log.append({"strike": float(strike), "status": f"❌ resolve far: {exc}", "errors": [str(exc)]})
            continue

        instruments = [
            (near_inst.instrument_token, near_inst.trading_symbol),
            (far_inst.instrument_token,  far_inst.trading_symbol),
        ]
        try:
            quotes = qe.fetch_all(instruments)
        except Exception as exc:
            log.append({"strike": float(strike), "status": f"❌ quote error: {exc}", "errors": [str(exc)]})
            continue

        near_q = quotes.get(near_inst.trading_symbol)
        far_q  = quotes.get(far_inst.trading_symbol)

        if near_q is None or far_q is None:
            log.append({"strike": float(strike), "status": "❌ missing quote", "errors": ["No quote for one or both legs"]})
            continue

        near_bid = near_q.bid
        far_ask  = far_q.ask
        net_credit = near_bid - far_ask
        ror = (near_bid / far_ask * 100) if far_ask > 0 else Decimal("0")

        passes_credit = net_credit >= min_credit
        passes_ror    = ror >= min_ror_pct

        if passes_credit and passes_ror:
            status = "✅ selected"
        elif not passes_credit:
            status = f"❌ credit {float(net_credit):.2f} < {float(min_credit):.2f}"
        else:
            status = f"❌ RoR {float(ror):.1f}% < {float(min_ror_pct):.0f}%"

        cand = CalendarCandidate(
            underlying=underlying, near_expiry=near_expiry, far_expiry=far_expiry,
            strike=strike, option_type=option_type, spot=spot, iv=Decimal("0"),
            dte=dte, lot_size=lot_size,
            near_bid=near_bid, far_ask=far_ask,
            net_credit=net_credit, return_on_risk=ror,
        )

        log.append({
            "strike": float(strike),
            "near_bid": float(near_bid), "far_ask": float(far_ask),
            "credit": float(net_credit), "ror": float(ror),
            "status": status, "errors": [],
            "candidate": cand,
            "resolved": {0: near_inst, 1: far_inst},
            "quotes": quotes,
        })

        if passes_credit and passes_ror:
            return cand, log

    return None, log


def auto_select_bwb(
    underlying: str,
    expiry: date,
    spot: Decimal,
    iv: Decimal,
    available_strikes: list[Decimal],
    lot_size: int,
    lots: int,
    option_type: str = "PE",
    min_credit: Decimal = Decimal("2.0"),
    broker = None,
    exchange: str = "NFO",
    center_mult_start: float = 0.5,
    center_mult_min: float = 0.2,
    center_mult_step: float = 0.1,
    upper_wing_ratio: float = 0.5,   # upper_wing = center_mult × upper_wing_ratio × EM
    lower_wing_ratio: float = 1.6,   # lower_wing = center_mult × lower_wing_ratio × EM
    min_ror_pct: Decimal = Decimal("10.0"),
) -> tuple[Optional[BWBCandidate], list[dict]]:
    """
    Broken Wing Butterfly: BUY upper + SELL 2× mid + BUY lower with wider lower wing.
    Net credit when 2×mid_bid > upper_ask + lower_ask.

    Leg index ordering (matches enter_best legs_cfg):
      index 0 = BUY  upper_strike (closer to ATM)
      index 1 = SELL mid_strike   (first short)
      index 2 = SELL mid_strike   (second short, same instrument)
      index 3 = BUY  lower_strike (far OTM)
    """
    from execution.quote_engine import QuoteEngine
    qe = QuoteEngine(broker)
    log: list[dict] = []

    mult = center_mult_start
    while mult >= center_mult_min - 1e-9:
        mult_r = round(mult, 2)
        dte = max(1, (expiry - date.today()).days)
        em = compute_expected_move(spot, iv, dte)
        em_f = float(em)
        spot_f = float(spot)

        def nearest(target: float) -> Optional[Decimal]:
            if not available_strikes:
                return None
            return min(available_strikes, key=lambda s: abs(float(s) - target))

        if option_type == "PE":
            ideal_mid   = spot_f - mult_r * em_f
            ideal_upper = spot_f - mult_r * upper_wing_ratio * em_f
            ideal_lower = spot_f - mult_r * lower_wing_ratio * em_f
        else:
            ideal_mid   = spot_f + mult_r * em_f
            ideal_upper = spot_f + mult_r * upper_wing_ratio * em_f
            ideal_lower = spot_f + mult_r * lower_wing_ratio * em_f

        mid_strike   = nearest(ideal_mid)
        if option_type == "PE":
            upper_strike = nearest(ideal_upper) if ideal_upper else None
            # upper must be > mid for PE
            upper_candidates = [s for s in available_strikes if s > mid_strike] if mid_strike else []
            upper_strike = min(upper_candidates, key=lambda s: abs(float(s) - ideal_upper)) if upper_candidates else None
            lower_candidates = [s for s in available_strikes if s < mid_strike] if mid_strike else []
            lower_strike = min(lower_candidates, key=lambda s: abs(float(s) - ideal_lower)) if lower_candidates else None
        else:
            lower_candidates = [s for s in available_strikes if s < mid_strike] if mid_strike else []
            upper_strike = min(lower_candidates, key=lambda s: abs(float(s) - ideal_upper)) if lower_candidates else None
            upper_candidates = [s for s in available_strikes if s > mid_strike] if mid_strike else []
            lower_strike = min(upper_candidates, key=lambda s: abs(float(s) - ideal_lower)) if upper_candidates else None

        if not all([upper_strike, mid_strike, lower_strike]):
            log.append({"mult": mult_r, "status": "❌ strike error", "errors": ["Could not find valid strikes"]})
            mult -= center_mult_step
            continue

        # Validate wing asymmetry
        if option_type == "PE":
            upper_wing = upper_strike - mid_strike
            lower_wing = mid_strike - lower_strike
        else:
            upper_wing = mid_strike - upper_strike
            lower_wing = lower_strike - mid_strike

        if lower_wing <= upper_wing:
            log.append({"mult": mult_r, "status": f"❌ symmetric wings ({float(lower_wing):.0f}={float(upper_wing):.0f})", "errors": ["Symmetric wings - not a BWB"]})
            mult -= center_mult_step
            continue

        # Resolve 3 distinct instruments (mid appears twice in legs)
        legs_spec = [
            (upper_strike, option_type, "BUY"),
            (mid_strike,   option_type, "SELL"),
            (mid_strike,   option_type, "SELL"),
            (lower_strike, option_type, "BUY"),
        ]
        instruments = []
        resolved_map: dict[int, object] = {}
        resolve_error = None
        _resolved_cache: dict[Decimal, object] = {}

        for idx, (strike, opt_t, _side) in enumerate(legs_spec):
            if strike in _resolved_cache:
                resolved_map[idx] = _resolved_cache[strike]
                instruments.append((resolved_map[idx].instrument_token, resolved_map[idx].trading_symbol))
                continue
            try:
                inst = broker.resolve_instrument(
                    underlying=underlying, exchange=exchange,
                    expiry=expiry, strike=strike, option_type=opt_t,
                )
                resolved_map[idx] = inst
                _resolved_cache[strike] = inst
                instruments.append((inst.instrument_token, inst.trading_symbol))
            except Exception as exc:
                resolve_error = str(exc)
                break

        if resolve_error:
            log.append({"mult": mult_r, "status": f"❌ resolve: {resolve_error}", "errors": [resolve_error]})
            mult -= center_mult_step
            continue

        try:
            # Deduplicate for API call (mid appears twice)
            unique_instruments = list({tok: (tok, sym) for tok, sym in instruments}.values())
            quotes = qe.fetch_all(unique_instruments)
        except Exception as exc:
            log.append({"mult": mult_r, "status": f"❌ quote error: {exc}", "errors": [str(exc)]})
            mult -= center_mult_step
            continue

        upper_q = quotes.get(resolved_map[0].trading_symbol)
        mid_q   = quotes.get(resolved_map[1].trading_symbol)
        lower_q = quotes.get(resolved_map[3].trading_symbol)

        if not all([upper_q, mid_q, lower_q]):
            log.append({"mult": mult_r, "status": "❌ missing quote", "errors": ["No quote for one or more legs"]})
            mult -= center_mult_step
            continue

        # Net credit = 2 × mid_bid - upper_ask - lower_ask
        net_credit = (2 * mid_q.bid) - upper_q.ask - lower_q.ask
        max_loss = lower_wing - upper_wing - net_credit   # worst case below lower_strike
        ror = (net_credit / max_loss * 100) if max_loss > 0 else Decimal("0")

        passes_credit = net_credit >= min_credit
        passes_ror    = ror >= min_ror_pct

        if passes_credit and passes_ror:
            status = "✅ selected"
        elif not passes_credit:
            status = f"❌ credit {float(net_credit):.2f} < {float(min_credit):.2f}"
        else:
            status = f"❌ RoR {float(ror):.1f}% < {float(min_ror_pct):.0f}%"

        cand = BWBCandidate(
            underlying=underlying, expiry=expiry, spot=spot, iv=iv,
            dte=dte, expected_move=em, option_type=option_type,
            upper_strike=upper_strike, mid_strike=mid_strike, lower_strike=lower_strike,
            lot_size=lot_size, net_credit=net_credit,
            upper_wing_width=upper_wing, lower_wing_width=lower_wing,
            return_on_risk=ror,
        )

        log.append({
            "mult": mult_r,
            "upper": float(upper_strike), "mid": float(mid_strike), "lower": float(lower_strike),
            "credit": float(net_credit), "ror": float(ror),
            "status": status, "errors": [],
            "candidate": cand,
            "resolved": resolved_map,
            "quotes": quotes,
        })

        if passes_credit and passes_ror:
            return cand, log

        mult -= center_mult_step

    return None, log
