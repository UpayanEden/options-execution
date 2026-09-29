#!/usr/bin/env python3
"""
Shifting Iron Condor Backtest — Interactive CLI + Excel-driven runner
=====================================================================
Run:  python shifting_condor_backtest.py
      python shifting_condor_backtest.py --symbols ICICIBANK BHARTIARTL --capital 500000
      python shifting_condor_backtest.py --list-symbols
      python shifting_condor_backtest.py --universe-file "pictures/NSE FO list with targets.xlsx" --capital 1000000

Behaviour:
  • Sells ±N% OTM strangle + buys wider wings for protection.
  • When spot approaches a short strike within the trigger threshold,
    the full position is squared off and re-entered at the new ±N% OTM level.
  • Lots are auto-calculated from your capital allocation unless overridden.
  • Excel-driven mode: reads per-stock OTM distances, triggers, take-profit from xlsx.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ─── Black-Scholes imports ───────────────────────────────────────────────────
try:
    from scipy.stats import norm as _norm_dist
    from scipy.optimize import brentq as _brentq
    _SCIPY = True
except ImportError:
    _SCIPY = False

# ─── Paths ────────────────────────────────────────────────────────────────────

CACHE_DIR  = "/media/upayan/Work/Quant Projects/projects/project1/nse_bhavcopy_cache"
REPORT_DIR = "/media/upayan/Work/options_execution/reports"

# ─── Symbol Registry ──────────────────────────────────────────────────────────
# Add any F&O symbol here. lot_size comes from NSE.

SYMBOL_REGISTRY: dict[str, dict] = {
    "ICICIBANK":   {"lot_size": 700,  "shift_trigger_pct": 1.0},
    "BHARTIARTL":  {"lot_size": 475,  "shift_trigger_pct": 1.5},
    "OBEROIRLTY":  {"lot_size": 350,  "shift_trigger_pct": 1.5},
    "HDFCBANK":    {"lot_size": 550,  "shift_trigger_pct": 1.0},
    "RELIANCE":    {"lot_size": 250,  "shift_trigger_pct": 1.5},
    "INFY":        {"lot_size": 400,  "shift_trigger_pct": 1.5},
    "TCS":         {"lot_size": 175,  "shift_trigger_pct": 1.5},
    "NIFTY":       {"lot_size": 75,   "shift_trigger_pct": 1.0},
    "BANKNIFTY":   {"lot_size": 30,   "shift_trigger_pct": 1.0},
}

# Strategy defaults (overridable at runtime)
DEFAULT_SHORT_OTM_PCT  = 5.0   # short strikes N% from spot
DEFAULT_WING_WIDTH_PCT = 3.0   # longs an additional N% beyond shorts
DEFAULT_MIN_DTE        = 20
DEFAULT_MAX_DTE        = 45
DEFAULT_LOCK_IN_PCT    = 0.25  # close both legs if retained side cost <= 25% of its original credit
MAX_SHIFTS_PER_CYCLE   = 5

# ─── Margin constants ────────────────────────────────────────────────────────
MARGIN_PCT           = 0.125   # 12.5% of one-side notional; configurable via --margin-pct
CASH_MARGIN_PCT      = 0.50    # 50% of margin from free cash balance
STOCK_MARGIN_PCT     = 0.50    # 50% of margin from pledged stock collateral

# ─── Shift trigger cap ────────────────────────────────────────────────────────
# Shift fires when spot enters the trigger zone around a short strike.
# A LOWER trigger_pct means the shift fires later (spot must get closer to strike).
# Cap at 1.5% to prevent excessive shifting that erodes P&L.
MAX_SHIFT_TRIGGER_PCT = 1.5    # hard cap; any per-stock or Excel value above this is clamped

# ─── Excel name -> NSE bhavcopy symbol ────────────────────────────────────────
STOCK_MAP: dict[str, str | None] = {
    "360 One WAM":                              "360ONE",
    "ABB":                                      "ABB",
    "APL Apollo Tubes":                         "APLAPOLLO",
    "AU Small Finance Bank":                    "AUBANK",
    "Adani Energy Solutions":                   "ADANIENSOL",
    "Adani Enterprises":                        "ADANIENT",
    "Adani Green Energy":                       "ADANIGREEN",
    "Adani Ports & SEZ":                        "ADANIPORTS",
    "Adani Power":                              "ADANIPOWER",
    "Aditya Birla Capital":                     "ABCAPITAL",
    "Alkem Laboratories":                       "ALKEM",
    "Amber Enterprises":                        "AMBER",
    "Ambuja Cements":                           "AMBUJACEM",
    "Angel One":                                "ANGELONE",
    "Apollo Hospitals":                         "APOLLOHOSP",
    "Ashok Leyland":                            "ASHOKLEY",
    "Asian Paints":                             "ASIANPAINT",
    "Astral":                                   "ASTRAL",
    "Ather Energy":                             None,
    "Aurobindo Pharma":                         "AUROPHARMA",
    "Avenue Supermarts (DMart)":                "DMART",
    "Axis Bank":                                "AXISBANK",
    "BSE":                                      "BSE",
    "Bajaj Auto":                               "BAJAJ-AUTO",
    "Bajaj Finance":                            "BAJFINANCE",
    "Bajaj Finserv":                            "BAJAJFINSV",
    "Bajaj Holdings & Investments":             "BAJAJHLDNG",
    "Bandhan Bank":                             "BANDHANBNK",
    "Bank of Baroda":                           "BANKBARODA",
    "Bank of India":                            "BANKINDIA",
    "Bank of Maharashtra":                      None,
    "Bharat Dynamics":                          "BDL",
    "Bharat Electronics":                       "BEL",
    "Bharat Forge":                             "BHARATFORG",
    "Bharat Heavy Electricals (BHEL)":          "BHEL",
    "Bharat Petroleum (BPCL)":                  "BPCL",
    "Bharti Airtel":                            "BHARTIARTL",
    "Biocon":                                   "BIOCON",
    "Blue Star":                                "BLUESTARCO",
    "Bosch":                                    "BOSCHLTD",
    "Britannia Industries":                     "BRITANNIA",
    "CAMS":                                     "CAMS",
    "CDSL":                                     "CDSL",
    "CG Power & Industrial Solutions":          "CGPOWER",
    "Canara Bank":                              "CANBK",
    "Cholamandalam Investment":                 "CHOLAFIN",
    "Cipla":                                    "CIPLA",
    "Coal India":                               "COALINDIA",
    "Cochin Shipyard":                          "COCHINSHIP",
    "Coforge":                                  "COFORGE",
    "Colgate-Palmolive":                        "COLPAL",
    "Container Corporation of India (CONCOR)":  "CONCOR",
    "Crompton Greaves Consumer Electricals":    "CROMPTON",
    "Cummins India":                            "CUMMINSIND",
    "DLF":                                      "DLF",
    "Dabur India":                              "DABUR",
    "Delhivery":                                "DELHIVERY",
    "Divi's Laboratories":                      "DIVISLAB",
    "Dixon Technologies":                       "DIXON",
    "Dr. Reddy's Laboratories":                 "DRREDDY",
    "Eicher Motors":                            "EICHERMOT",
    "Eternal":                                  "ETERNAL",
    "Federal Bank":                             "FEDERALBNK",
    "Force Motors":                             "FORCEMOT",
    "Fortis Healthcare":                        "FORTIS",
    "GAIL":                                     "GAIL",
    "GE Vernova T&D India":                     "GVT&D",
    "GMR Airports":                             "GMRAIRPORT",
    "Glenmark Pharmaceuticals":                 "GLENMARK",
    "Godfrey Phillips India":                   "GODFRYPHLP",
    "Godrej Consumer Products":                 "GODREJCP",
    "Godrej Properties":                        "GODREJPROP",
    "Grasim Industries":                        "GRASIM",
    "HCL Technologies":                         "HCLTECH",
    "HDFC AMC":                                 "HDFCAMC",
    "HDFC Bank":                                "HDFCBANK",
    "HDFC Life Insurance":                      "HDFCLIFE",
    "Havells India":                            "HAVELLS",
    "Hero MotoCorp":                            "HEROMOTOCO",
    "Hindalco Industries":                      "HINDALCO",
    "Hindustan Aeronautics (HAL)":              "HAL",
    "Hindustan Petroleum (HPCL)":               "HINDPETRO",
    "Hindustan Unilever":                       "HINDUNILVR",
    "Hindustan Zinc":                           "HINDZINC",
    "Hitachi Energy India":                     None,
    "Hyundai Motor India":                      "HYUNDAI",
    "ICICI Bank":                               "ICICIBANK",
    "ICICI Lombard General Insurance":          "ICICIGI",
    "ICICI Prudential Life Insurance":          "ICICIPRULI",
    "IDFC First Bank":                          "IDFCFIRSTB",
    "IREDA":                                    "IREDA",
    "IRFC":                                     "IRFC",
    "ITC":                                      "ITC",
    "Indian Bank":                              "INDIANB",
    "Indian Energy Exchange (IEX)":             "IEX",
    "Indian Hotels Company":                    "INDHOTEL",
    "Indian Oil Corporation (IOC)":             "IOC",
    "Indus Towers":                             "INDUSTOWER",
    "IndusInd Bank":                            "INDUSINDBK",
    "Info Edge":                                "NAUKRI",
    "Infosys":                                  "INFY",
    "Inox Wind":                                "INOXWIND",
    "InterGlobe Aviation (IndiGo)":             "INDIGO",
    "JSW Energy":                               "JSWENERGY",
    "JSW Steel":                                "JSWSTEEL",
    "Jindal Steel":                             "JINDALSTEL",
    "Jio Financial Services":                   "JIOFIN",
    "Jubilant FoodWorks":                       "JUBLFOOD",
    "KEI Industries":                           "KEI",
    "KFin Technologies":                        "KFINTECH",
    "KPIT Technologies":                        "KPITTECH",
    "Kalyan Jewellers":                         "KALYANKJIL",
    "Kaynes Technology India":                  "KAYNES",
    "Kotak Mahindra Bank":                      "KOTAKBANK",
    "L&T Finance":                              "LTF",
    "LIC Housing Finance":                      "LICHSGFIN",
    "Life Insurance Corporation of India (LIC)":"LICI",
    "LTM":                                      "LTM",
    "Larsen & Toubro":                          "LT",
    "Laurus Labs":                              "LAURUSLABS",
    "Lodha Developers":                         "LODHA",
    "Lupin":                                    "LUPIN",
    "MCX":                                      "MCX",
    "Mahindra & Mahindra":                      "M&M",
    "Manappuram Finance":                       "MANAPPURAM",
    "Mankind Pharma":                           "MANKIND",
    "Marico":                                   "MARICO",
    "Maruti Suzuki India":                      "MARUTI",
    "Max Financial Services":                   "MFSL",
    "Max Healthcare Institute":                 "MAXHEALTH",
    "Mazagon Dock Shipbuilders":                "MAZDOCK",
    "Motilal Oswal Financial Services":         "MOTILALOFS",
    "Mphasis":                                  "MPHASIS",
    "Muthoot Finance":                          "MUTHOOTFIN",
    "NALCO":                                    "NATIONALUM",
    "NBCC India":                               "NBCC",
    "NHPC":                                     "NHPC",
    "NMDC":                                     "NMDC",
    "NTPC":                                     "NTPC",
    "Nestle India":                             "NESTLEIND",
    "NestlA\xa9 India":                            "NESTLEIND",
    "Nippon Life India Asset Management":       "NAM-INDIA",
    "Nykaa":                                    "NYKAA",
    "Oberoi Realty":                            "OBEROIRLTY",
    "ONGC":                                     "ONGC",
    "Oil India":                                "OIL",
    "One 97 Communications (Paytm)":            "PAYTM",
    "Oracle Financial Services Software":       "OFSS",
    "PB Fintech (Policybazaar)":                "POLICYBZR",
    "PG Electroplast":                          "PGEL",
    "PI Industries":                            "PIIND",
    "PNB Housing Finance":                      "PNBHOUSING",
    "Page Industries":                          "PAGEIND",
    "Patanjali Foods":                          "PATANJALI",
    "Persistent Systems":                       "PERSISTENT",
    "Petronet LNG":                             "PETRONET",
    "Phoenix Mills":                            "PHOENIXLTD",
    "Pidilite Industries":                      "PIDILITIND",
    "Polycab India":                            "POLYCAB",
    "Power Finance Corporation (PFC)":          "PFC",
    "Power Grid Corporation of India":          "POWERGRID",
    "Premier Energies":                         "PREMIERENE",
    "Prestige Estates Projects":                "PRESTIGE",
    "Punjab National Bank (PNB)":               "PNB",
    "RBL Bank":                                 "RBLBANK",
    "REC":                                      "RECLTD",
    "Radico Khaitan":                           "RADICO",
    "Rail Vikas Nigam (RVNL)":                  "RVNL",
    "Reliance Industries":                      "RELIANCE",
    "SBI Cards":                                "SBICARD",
    "SBI Life Insurance":                       "SBILIFE",
    "SRF":                                      "SRF",
    "Sagility":                                 None,
    "Samvardhana Motherson International":      "MOTHERSON",
    "Shree Cement":                             "SHREECEM",
    "Shriram Finance":                          "SHRIRAMFIN",
    "Siemens":                                  "SIEMENS",
    "Solar Industries India":                   "SOLARINDS",
    "Sona BLW Precision Forgings":              "SONACOMS",
    "State Bank of India":                      "SBIN",
    "Steel Authority of India (SAIL)":          "SAIL",
    "Sun Pharmaceutical Industries":            "SUNPHARMA",
    "Supreme Industries":                       "SUPREMEIND",
    "Suzlon Energy":                            "SUZLON",
    "Swiggy":                                   "SWIGGY",
    "TVS Motor Company":                        "TVSMOTOR",
    "Tata Consultancy Services (TCS)":          "TCS",
    "Tata Consumer Products":                   "TATACONSUM",
    "Tata Elxsi":                               "TATAELXSI",
    "Tata Motors Passenger Vehicles":           "TMPV",
    "Tata Power":                               "TATAPOWER",
    "Tata Steel":                               "TATASTEEL",
    "Tech Mahindra":                            "TECHM",
    "Titan Company":                            "TITAN",
    "Torrent Pharmaceuticals":                  "TORNTPHARM",
    "Trent":                                    "TRENT",
    "Tube Investments of India":                "TIINDIA",
    "UNO Minda":                                "UNOMINDA",
    "UPL":                                      "UPL",
    "UltraTech Cement":                         "ULTRACEMCO",
    "Union Bank of India":                      "UNIONBANK",
    "United Spirits":                           "UNITDSPR",
    "Varun Beverages":                          "VBL",
    "Vedanta":                                  "VEDL",
    "Vishal Mega Mart":                         "VMM",
    "Vodafone Idea":                            "IDEA",
    "Voltas":                                   "VOLTAS",
    "Waaree Energies":                          "WAAREEENER",
    "Wipro":                                    "WIPRO",
    "Yes Bank":                                 "YESBANK",
    "Zydus Lifesciences":                       "ZYDUSLIFE",
}


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class CondorSegment:
    segment_idx:     int
    entry_date:      date
    exit_date:       date
    expiry:          date
    spot_at_entry:   float
    spot_at_exit:    float
    sell_put:        float
    buy_put:         float
    sell_call:       float
    buy_call:        float
    sell_put_entry:  float
    buy_put_entry:   float
    sell_call_entry: float
    buy_call_entry:  float
    sell_put_exit:   float
    buy_put_exit:    float
    sell_call_exit:  float
    buy_call_exit:   float
    net_credit:      float
    cost_to_close:   float
    segment_pnl:     float
    exit_reason:     str
    shift_trigger:   str = ""
    lot_size:        int = 100
    lots:            int = 1
    # §6/§8: Enhanced fields
    exit_type:              str   = ""
    notional:               float = 0.0
    margin:                 float = 0.0
    cash_margin:            float = 0.0   # 50% of margin (from free cash)
    stock_margin:           float = 0.0   # 50% of margin (from stock collateral)
    put_trigger:            float = 0.0
    call_trigger:           float = 0.0
    short_put_dist_pct:     float = 0.0
    short_call_dist_pct:    float = 0.0
    spot_beyond_short:      bool  = False
    iv_flags:               list  = field(default_factory=list)
    price_from_settlement:  bool  = False


@dataclass
class CondorCycle:
    underlying:       str
    expiry:           date
    cycle_start:      date
    dte_at_entry:     int
    lots:             int
    segments:         list[CondorSegment] = field(default_factory=list)
    total_pnl:        float = 0.0
    num_shifts:       int = 0
    final_exit_reason: str = ""
    margin_required:  float = 0.0
    notional:         float = 0.0


@dataclass
class BacktestSummary:
    underlying:        str
    lot_size:          int
    shift_trigger_pct: float
    short_otm_pct:     float
    wing_width_pct:    float
    capital_allocated: float
    cycles:            list[CondorCycle] = field(default_factory=list)
    total_pnl:         float = 0.0
    total_cycles:      int = 0
    winning_cycles:    int = 0
    losing_cycles:     int = 0
    total_shifts:      int = 0
    win_rate:          float = 0.0
    avg_pnl_per_cycle: float = 0.0
    profit_factor:     float = 0.0
    max_drawdown:      float = 0.0
    best_cycle:        float = 0.0
    worst_cycle:       float = 0.0
    roi_pct:           float = 0.0
    avg_margin:        float = 0.0
    avg_cash_margin:   float = 0.0   # 50% of avg_margin (from free cash)
    avg_stock_margin:  float = 0.0   # 50% of avg_margin (from stock collateral)
    roi_on_margin:     float = 0.0
    errors:            list[str] = field(default_factory=list)
    # §8: Enhanced summary fields
    peak_margin:          float = 0.0
    n_exits_beyond_short: int   = 0
    n_gap_exits:          int   = 0
    n_intraday_triggers:  int   = 0
    n_take_profits:       int   = 0
    n_max_shifts_stop:    int   = 0
    n_insufficient_cap:   int   = 0
    n_iv_neighbour:       int   = 0
    n_price_fallback:     int   = 0
    n_price_from_settle:  int   = 0
    corp_action_events:   list  = field(default_factory=list)
    excel_params:         dict  = field(default_factory=dict)


# ─── Black-Scholes ───────────────────────────────────────────────────────────

def _ncdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


def bs_price(S, K, T, r, sigma, opt):
    if T < 1e-9:
        return max(K - S, 0.0) if opt == 'PE' else max(S - K, 0.0)
    if sigma < 1e-9:
        return max(K - S, 0.0) if opt == 'PE' else max(S - K, 0.0)
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    N = (_norm_dist.cdf if _SCIPY else _ncdf)
    disc = math.exp(-r * T)
    if opt == 'CE':
        return S * N(d1) - K * disc * N(d2)
    return K * disc * N(-d2) - S * N(-d1)


def solve_iv(mkt, S, K, T, r, opt):
    """Returns (iv, flag_str). flag_str='' on success, 'PRICE_FALLBACK' on failure."""
    if T < 1e-9 or mkt <= 0:
        return None, "PRICE_FALLBACK"
    intr = max(K - S, 0.0) if opt == 'PE' else max(S - K, 0.0)
    if mkt <= intr:
        return None, "PRICE_FALLBACK"
    try:
        fn = lambda v: bs_price(S, K, T, r, v, opt) - mkt
        if _SCIPY:
            iv = _brentq(fn, 1e-6, 20.0, xtol=1e-6)
        else:
            lo, hi = 1e-6, 20.0
            for _ in range(64):
                m = (lo + hi) / 2
                if fn(m) < 0:
                    lo = m
                else:
                    hi = m
            iv = (lo + hi) / 2
        return iv, ""
    except Exception:
        return None, "PRICE_FALLBACK"


def estimate_exit_price(S_exit, S_prev, K, T_exit, r, prev_price,
                        opt, day_lo, day_hi, iv_cache, flags_out):
    """
    Fallback chain:
    1. Solve IV from prev_price at S_prev -> BS at S_exit.
    2. Use nearest-strike IV from iv_cache. Flag: IV_FROM_NEIGHBOUR.
    3. prev_price + intrinsic change. Flag: PRICE_FALLBACK.
    Bounded by [intrinsic, day_hi] (and floored at day_lo if set).
    """
    intr = max(K - S_exit, 0.0) if opt == 'PE' else max(S_exit - K, 0.0)
    T_prev = T_exit + 1 / 365.0
    iv, flag = solve_iv(prev_price, S_prev, K, T_prev, r, opt)
    if iv is not None:
        price = bs_price(S_exit, K, T_exit, r, iv, opt)
    else:
        iv2 = None
        if iv_cache:
            nk = min(iv_cache, key=lambda k: abs(k - K))
            iv2 = iv_cache.get(nk)
        if iv2 is not None:
            price = bs_price(S_exit, K, T_exit, r, iv2, opt)
            flag = "IV_FROM_NEIGHBOUR"
        else:
            prev_intr = max(K - S_prev, 0.0) if opt == 'PE' else max(S_prev - K, 0.0)
            price = max(intr, prev_price + (intr - prev_intr))
            flag = "PRICE_FALLBACK"
    if flag:
        flags_out.append(flag)
    price = max(price, intr)
    if day_hi > 0:
        price = min(price, day_hi)
    if day_lo > 0:
        price = max(price, day_lo)
    return max(price, 0.0)


# ─── Data Loading ─────────────────────────────────────────────────────────────

def detect_cache_date_range(cache_dir: str) -> tuple[date, date]:
    files = sorted(Path(cache_dir).glob("fo_*.csv"))
    if not files:
        return date(2025, 1, 1), date.today()
    def parse(f: Path) -> Optional[date]:
        try:
            p = f.stem.split("_")[-1]
            return date(int(p[:4]), int(p[4:6]), int(p[6:8]))
        except Exception:
            return None
    dates = [d for f in files if (d := parse(f))]
    return min(dates), max(dates)


def load_data(
    underlying: str,
    start: date,
    end: date,
    cache_dir: str = CACHE_DIR,
) -> dict[date, dict]:
    """Load bhavcopy data for the underlying within the date range."""
    data: dict[date, dict] = {}
    cache_path = Path(cache_dir)

    for csv_file in sorted(cache_path.glob("fo_*.csv")):
        try:
            p = csv_file.stem.split("_")[-1]
            tdate = date(int(p[:4]), int(p[4:6]), int(p[6:8]))
        except Exception:
            continue
        if tdate < start - timedelta(days=60) or tdate > end:
            continue

        try:
            with open(csv_file, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sym = (row.get("TckrSymb") or row.get("TCKRSYMB") or "").strip().upper()
                    if sym != underlying.upper():
                        continue
                    opt = (row.get("OptnTp") or row.get("OPTNTP") or "").strip().upper()
                    if opt not in ("CE", "PE"):
                        continue
                    exp_raw = (row.get("XpryDt") or row.get("XPRYDT") or "").strip()
                    try:
                        exp_date = date.fromisoformat(exp_raw[:10])
                    except Exception:
                        continue
                    try:
                        strike = float(row.get("StrkPric") or row.get("STRKPRIC") or "0")
                    except Exception:
                        continue
                    try:
                        close = float(row.get("ClsPric") or "0")
                    except Exception:
                        close = 0.0
                    if close == 0:
                        try:
                            close = float(row.get("SttlmPric") or "0")
                        except Exception:
                            close = 0.0
                    try:
                        spot = float(row.get("UndrlygPric") or row.get("UNDRLYGPRIC") or "0")
                    except Exception:
                        spot = 0.0
                    try:
                        oi = int(float(row.get("OpnIntrst") or "0"))
                    except Exception:
                        oi = 0

                    if tdate not in data:
                        data[tdate] = {}
                    data[tdate][(exp_date, round(strike, 2), opt)] = {
                        "close": close, "spot": spot, "oi": oi,
                    }
        except Exception:
            continue

    return data


# ─── Single-pass bulk data loader ────────────────────────────────────────────

def load_all_symbols(
    symbols: set[str],
    start: date,
    end: date,
    cache_dir: str,
) -> tuple[dict, dict, dict]:
    """
    One pass through all bhavcopy files -> per-symbol data dicts.
    Returns (options_data, futures_data, lot_sizes)
      options_data: {symbol: {tdate: {(expiry, strike, opt): {close, spot, oi, volume}}}}
      futures_data: {symbol: {tdate: {expiry_date: {open, high, low, close, spot_close}}}}
      lot_sizes: {symbol: int}
    """
    cache = Path(cache_dir)
    all_data: dict[str, dict] = {sym: {} for sym in symbols}
    futures_data: dict[str, dict] = {sym: {} for sym in symbols}
    lot_sizes: dict[str, int] = {}

    files = sorted(cache.glob("fo_*.csv"))
    print(f"  Loading {len(files)} bhavcopy files for {len(symbols)} symbols...",
          flush=True)

    for fi, csv_file in enumerate(files, 1):
        try:
            p = csv_file.stem.split("_")[-1]
            tdate = date(int(p[:4]), int(p[4:6]), int(p[6:8]))
        except Exception:
            continue
        if tdate < start - timedelta(days=60) or tdate > end:
            continue

        with open(csv_file, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = (row.get("TckrSymb") or "").strip().upper()
                if sym not in symbols:
                    continue

                fin_type = (row.get("FinInstrmTp") or "").strip().upper()

                # lot size (grab once per symbol)
                if sym not in lot_sizes:
                    raw_lot = row.get("NewBrdLotQty", "")
                    try:
                        lot_sizes[sym] = int(float(raw_lot))
                    except Exception:
                        pass

                # §4a: Futures data
                if fin_type == "STF":
                    exp_raw = (row.get("XpryDt") or "").strip()
                    try:
                        exp_date = date.fromisoformat(exp_raw[:10])
                    except Exception:
                        continue
                    try:
                        f_open = float(row.get("OpnPric") or "0")
                        f_high = float(row.get("HghPric") or "0")
                        f_low = float(row.get("LwPric") or "0")
                        f_close = float(row.get("ClsPric") or "0")
                        f_spot = float(row.get("UndrlygPric") or "0")
                    except Exception:
                        continue

                    if sym not in futures_data:
                        futures_data[sym] = {}
                    if tdate not in futures_data[sym]:
                        futures_data[sym][tdate] = {}
                    futures_data[sym][tdate][exp_date] = {
                        "open": f_open, "high": f_high, "low": f_low,
                        "close": f_close, "spot_close": f_spot,
                    }
                    continue

                opt = (row.get("OptnTp") or "").strip().upper()
                if opt not in ("CE", "PE"):
                    continue
                exp_raw = (row.get("XpryDt") or "").strip()
                try:
                    exp_date = date.fromisoformat(exp_raw[:10])
                except Exception:
                    continue
                try:
                    strike = float(row.get("StrkPric") or "0")
                except Exception:
                    continue
                try:
                    close_price = float(row.get("ClsPric") or "0")
                except Exception:
                    close_price = 0.0
                price_from_settlement = False
                if close_price == 0:
                    try:
                        close_price = float(row.get("SttlmPric") or "0")
                        if close_price > 0:
                            price_from_settlement = True
                    except Exception:
                        close_price = 0.0
                try:
                    spot = float(row.get("UndrlygPric") or "0")
                except Exception:
                    spot = 0.0
                try:
                    oi = int(float(row.get("OpnIntrst") or "0"))
                except Exception:
                    oi = 0
                try:
                    volume = int(float(row.get("TtlTradgVol") or "0"))
                except Exception:
                    volume = 0
                try:
                    high_price = float(row.get("HghPric") or "0")
                except Exception:
                    high_price = 0.0
                try:
                    low_price = float(row.get("LwPric") or "0")
                except Exception:
                    low_price = 0.0

                sym_data = all_data[sym]
                if tdate not in sym_data:
                    sym_data[tdate] = {}
                sym_data[tdate][(exp_date, round(strike, 2), opt)] = {
                    "close": close_price, "spot": spot, "oi": oi, "volume": volume,
                    "high": high_price, "low": low_price,
                    "price_from_settlement": price_from_settlement,
                }

        if fi % 20 == 0 or fi == len(files):
            print(f"    {fi}/{len(files)} files scanned...", flush=True)

    return all_data, futures_data, lot_sizes


# ─── Helpers ──────────────────────────────────────────────────────────────────

def nearest(target: float, strikes: list[float]) -> float:
    return min(strikes, key=lambda s: abs(s - target))


def get_strikes(day_data: dict, expiry: date, opt_type: str) -> list[float]:
    return sorted({s for (e, s, o) in day_data if e == expiry and o == opt_type})


def get_price(data: dict, tdate: date, expiry: date, strike: float, opt: str) -> float:
    v = data.get(tdate, {}).get((expiry, round(strike, 2), opt))
    return v["close"] if v else 0.0


def get_spot(data: dict, tdate: date, expiry: date) -> float:
    day = data.get(tdate, {})
    vals = [v["spot"] for (e, _, _), v in day.items() if e == expiry and v["spot"] > 0]
    return sum(vals) / len(vals) if vals else 0.0


def get_latest_spot(data: dict, underlying: str) -> float:
    """Get most recent spot price from loaded data."""
    for tdate in sorted(data.keys(), reverse=True):
        for (_, _, _), v in data[tdate].items():
            if v["spot"] > 0:
                return v["spot"]
    return 0.0


def select_strikes(
    spot: float,
    short_otm_pct: float,
    wing_width_pct: float,
    pe_strikes: list[float],
    ce_strikes: list[float],
) -> Optional[tuple[float, float, float, float]]:
    sp = nearest(spot * (1 - short_otm_pct / 100), pe_strikes)
    sc = nearest(spot * (1 + short_otm_pct / 100), ce_strikes)
    bp_cands = [s for s in pe_strikes if s < sp]
    bc_cands = [s for s in ce_strikes if s > sc]
    if not bp_cands or not bc_cands:
        return None
    bp = nearest(spot * (1 - (short_otm_pct + wing_width_pct) / 100), bp_cands)
    bc = nearest(spot * (1 + (short_otm_pct + wing_width_pct) / 100), bc_cands)
    return sp, bp, sc, bc


# ─── §1: Strict OTM strike selection ─────────────────────────────────────────

def select_short_put(spot: float, put_pct: float, pe_strikes: list) -> float | None:
    """Highest PE strike <= spot*(1 - put_pct/100). Returns None if no valid strike."""
    target = spot * (1.0 - put_pct / 100.0)
    cands = [s for s in pe_strikes if s <= target]
    return max(cands) if cands else None


def select_short_call(spot: float, call_pct: float, ce_strikes: list) -> float | None:
    """Lowest CE strike >= spot*(1 + call_pct/100). Returns None if no valid strike."""
    target = spot * (1.0 + call_pct / 100.0)
    cands = [s for s in ce_strikes if s >= target]
    return min(cands) if cands else None


def select_long_put(short_put: float, spot: float, put_pct: float,
                    wing_pct: float, pe_strikes: list) -> float | None:
    """Highest PE strike < short_put AND <= spot*(1-(put_pct+wing_pct)/100).
    Falls back to highest strike strictly below short_put if no strike meets both."""
    target = spot * (1.0 - (put_pct + wing_pct) / 100.0)
    cands = [s for s in pe_strikes if s < short_put and s <= target]
    if not cands:
        cands = [s for s in pe_strikes if s < short_put]
    return max(cands) if cands else None


def select_long_call(short_call: float, spot: float, call_pct: float,
                     wing_pct: float, ce_strikes: list) -> float | None:
    """Lowest CE strike > short_call AND >= spot*(1+(call_pct+wing_pct)/100).
    Falls back to lowest strike strictly above short_call if no strike meets both."""
    target = spot * (1.0 + (call_pct + wing_pct) / 100.0)
    cands = [s for s in ce_strikes if s > short_call and s >= target]
    if not cands:
        cands = [s for s in ce_strikes if s > short_call]
    return min(cands) if cands else None


# ─── §2: Intraday exit checks ────────────────────────────────────────────────

def check_exit(spot_open, spot_high, spot_low, spot_close,
               put_trigger, call_trigger):
    """Returns (side, exit_spot, exit_type). side: 'PUT_SIDE'|'CALL_SIDE'|''."""
    if spot_open is not None:
        if spot_open <= put_trigger:
            return "PUT_SIDE", spot_open, "GAP_EXIT"
        if spot_open >= call_trigger:
            return "CALL_SIDE", spot_open, "GAP_EXIT"
    lo = spot_low if spot_low is not None else spot_close
    hi = spot_high if spot_high is not None else spot_close
    if lo <= put_trigger:
        return "PUT_SIDE", put_trigger, "INTRADAY_TRIGGER"
    if hi >= call_trigger:
        return "CALL_SIDE", call_trigger, "INTRADAY_TRIGGER"
    return "", 0.0, ""


def check_shift(spot: float, sell_put: float, sell_call: float,
                trigger_pct: float) -> str:
    """Legacy shift check - used as fallback for non-Excel mode."""
    if spot <= sell_put:
        return "PUT_SIDE"
    if spot >= sell_call:
        return "CALL_SIDE"
    if abs(spot - sell_put) / sell_put * 100 <= trigger_pct:
        return "PUT_SIDE"
    if abs(spot - sell_call) / sell_call * 100 <= trigger_pct:
        return "CALL_SIDE"
    return ""


# ─── §4b: Intraday spot OHLC from futures ────────────────────────────────────

def get_intraday_spot(futures_data, symbol, tdate, prev_basis):
    """Returns (open, high, low, close) for spot. prev_basis = fut_close_prev - spot_close_prev."""
    day = futures_data.get(symbol, {}).get(tdate, {})
    if not day:
        return None
    near = min((e for e in day if e > tdate), default=None)
    if near is None:
        return None
    f = day[near]
    b = prev_basis if prev_basis is not None else 0.0
    return f["open"] - b, f["high"] - b, f["low"] - b, f["close"] - b


# ─── §3: Corporate action detection ──────────────────────────────────────────

def detect_corp_actions(sym: str, options_data: dict) -> tuple[set, list]:
    """
    Returns (ex_dates: set[date], events: list[str]).
    Looks for overnight UndrlygPric change > +/-20%.
    """
    spots = {}
    for tdate, day in options_data.items():
        for (exp, strike, opt), rec in day.items():
            if rec["spot"] > 0:
                spots[tdate] = rec["spot"]
                break
    ex_dates, events = set(), []
    prev_d, prev_s = None, None
    for d in sorted(spots):
        s = spots[d]
        if prev_s and prev_s > 0:
            ratio = s / prev_s
            if ratio < 0.80 or ratio > 1.25:
                ex_dates.add(d)
                events.append(f"{sym}: corp action on {d} (spot {prev_s:.1f}->{s:.1f}, {ratio:.2%})")
        prev_d, prev_s = d, s
    return ex_dates, events


# ─── §6: Notional margin + lot sizing ────────────────────────────────────────

def margin_per_lot(spot: float, lot_size: int) -> float:
    return MARGIN_PCT * spot * lot_size


def compute_lots(capital: float, spot: float, lot_size: int) -> int:
    """floor(capital / margin_per_lot). Returns 0 -- never forced to 1."""
    mpl = margin_per_lot(spot, lot_size)
    return int(capital // mpl) if mpl > 0 else 0


def lots_from_capital(
    capital: float,
    spot: float,
    lot_size: int,
    wing_width_pct: float,
) -> int:
    """
    Estimate lots affordable given capital (legacy mode).
    Max loss per lot ~ wing_width * lot_size (conservative: ignores net credit).
    """
    if spot <= 0 or lot_size <= 0:
        return 1
    max_loss_per_lot = (wing_width_pct / 100) * spot * lot_size
    if max_loss_per_lot <= 0:
        return 1
    return max(1, int(capital // max_loss_per_lot))


# ─── Core Engine ──────────────────────────────────────────────────────────────

def run_condor_cycle(
    underlying: str,
    expiry: date,
    entry_date: date,
    data: dict,
    cfg: dict,
    trading_dates: list[date],
    futures_data: dict | None = None,
    corp_action_dates: set | None = None,
) -> Optional[CondorCycle]:
    lot_size      = cfg["lot_size"]
    lots          = cfg["lots"]
    trigger_pct   = cfg["shift_trigger_pct"]
    short_otm_pct = cfg.get("short_otm_pct", DEFAULT_SHORT_OTM_PCT)
    wing_pct      = cfg.get("wing_width_pct", DEFAULT_WING_WIDTH_PCT)
    max_shifts    = cfg.get("max_shifts", MAX_SHIFTS_PER_CYCLE)
    rf_rate       = cfg.get("rf_rate", 0.0525)

    # Excel-driven per-stock params
    short_put_pct  = cfg.get("short_put_pct", short_otm_pct)
    short_call_pct = cfg.get("short_call_pct", short_otm_pct)
    take_profit_pct = cfg.get("take_profit_pct", 0.0)
    excel_mode = cfg.get("excel_mode", False)
    hold_to_expiry = cfg.get("hold_to_expiry", False)

    if corp_action_dates is None:
        corp_action_dates = set()

    cycle = CondorCycle(
        underlying=underlying, expiry=expiry,
        cycle_start=entry_date,
        dte_at_entry=(expiry - entry_date).days,
        lots=lots,
    )

    cur_entry    = entry_date
    last_trigger = ""

    # Retained leg state
    ret_sp = ret_bp = ret_sc = ret_bc = 0.0
    ret_sp_p = ret_bp_p = ret_sc_p = ret_bc_p = 0.0

    # Prev basis for futures-based spot OHLC
    prev_basis: float | None = None
    prev_spot_close: float | None = None

    for seg_idx in range(max_shifts + 1):

        open_put  = (seg_idx == 0 or last_trigger in ("PUT_SIDE", "PUT_SIDE_FULL", "CALL_SIDE_FULL"))
        open_call = (seg_idx == 0 or last_trigger in ("CALL_SIDE", "PUT_SIDE_FULL", "CALL_SIDE_FULL"))

        spot_in = get_spot(data, cur_entry, expiry)
        if not spot_in:
            break

        day_data = data.get(cur_entry, {})
        pe_all = get_strikes(day_data, expiry, "PE")
        ce_all = get_strikes(day_data, expiry, "CE")
        if not pe_all or not ce_all:
            break

        # OI filter for Excel mode
        if excel_mode:
            pe_oi = [s for s in pe_all
                     if day_data.get((expiry, s, "PE"), {}).get("oi", 0) > 0]
            ce_oi = [s for s in ce_all
                     if day_data.get((expiry, s, "CE"), {}).get("oi", 0) > 0]
            if not pe_oi:
                pe_oi = pe_all
            if not ce_oi:
                ce_oi = ce_all
            pe = pe_oi
            ce = ce_oi
        else:
            pe = pe_all
            ce = ce_all

        # ── Select strikes ───────────────────────────────────────────────
        if excel_mode:
            if open_put and open_call:
                sp = select_short_put(spot_in, short_put_pct, pe)
                sc = select_short_call(spot_in, short_call_pct, ce)
                if sp is None or sc is None:
                    break
                bp = select_long_put(sp, spot_in, short_put_pct, wing_pct, pe_all)
                bc = select_long_call(sc, spot_in, short_call_pct, wing_pct, ce_all)
                if bp is None or bc is None:
                    break
            elif open_put:
                sc, bc = ret_sc, ret_bc
                sp = select_short_put(spot_in, short_put_pct, pe)
                if sp is None:
                    break
                bp = select_long_put(sp, spot_in, short_put_pct, wing_pct, pe_all)
                if bp is None:
                    break
            else:
                sp, bp = ret_sp, ret_bp
                sc = select_short_call(spot_in, short_call_pct, ce)
                if sc is None:
                    break
                bc = select_long_call(sc, spot_in, short_call_pct, wing_pct, ce_all)
                if bc is None:
                    break

            # Log distance warnings
            actual_put_dist = (spot_in - sp) / spot_in * 100 if spot_in > 0 else 0
            actual_call_dist = (sc - spot_in) / spot_in * 100 if spot_in > 0 else 0
            if open_put and actual_put_dist < short_put_pct - 0.5:
                print(f"  WARN: {underlying} short put {sp:.0f} only {actual_put_dist:.1f}% OTM "
                      f"(target {short_put_pct}%), no closer strike available")
            if open_call and actual_call_dist < short_call_pct - 0.5:
                print(f"  WARN: {underlying} short call {sc:.0f} only {actual_call_dist:.1f}% OTM "
                      f"(target {short_call_pct}%), no closer strike available")
        else:
            # Legacy mode
            if open_put and open_call:
                strikes = select_strikes(spot_in, short_otm_pct, wing_pct, pe, ce)
                if not strikes:
                    break
                sp, bp, sc, bc = strikes
            elif open_put:
                sc, bc = ret_sc, ret_bc
                sp = nearest(spot_in * (1 - short_otm_pct / 100), pe)
                bp_cands = [s for s in pe if s < sp]
                if not bp_cands:
                    break
                bp = nearest(spot_in * (1 - (short_otm_pct + wing_pct) / 100), bp_cands)
            else:
                sp, bp = ret_sp, ret_bp
                sc = nearest(spot_in * (1 + short_otm_pct / 100), ce)
                bc_cands = [s for s in ce if s > sc]
                if not bc_cands:
                    break
                bc = nearest(spot_in * (1 + (short_otm_pct + wing_pct) / 100), bc_cands)

        # ── Get entry prices ──────────────────────────────────────────────
        if open_put:
            sp_p = get_price(data, cur_entry, expiry, sp, "PE")
            bp_p = get_price(data, cur_entry, expiry, bp, "PE")
            if not sp_p:
                break
        else:
            sp_p, bp_p = ret_sp_p, ret_bp_p

        if open_call:
            sc_p = get_price(data, cur_entry, expiry, sc, "CE")
            bc_p = get_price(data, cur_entry, expiry, bc, "CE")
            if not sc_p:
                break
        else:
            sc_p, bc_p = ret_sc_p, ret_bc_p

        if open_put and open_call:
            net_credit = (sp_p + sc_p) - (bp_p + bc_p)
        elif open_put:
            net_credit = sp_p - bp_p
        else:
            net_credit = sc_p - bc_p

        if net_credit <= 0 and seg_idx == 0:
            break

        # §4c: Trigger levels
        put_trigger_level = sp * (1.0 + trigger_pct / 100.0)
        call_trigger_level = sc * (1.0 - trigger_pct / 100.0)

        # Initial credit for take-profit
        if seg_idx == 0:
            segment_initial_credit = (sp_p + sc_p) - (bp_p + bc_p)
        else:
            segment_initial_credit = net_credit

        # Segment notional and margin
        seg_notional = round(spot_in * lot_size * lots, 2)
        seg_margin = round(margin_per_lot(spot_in, lot_size) * lots, 2)

        if seg_idx == 0:
            cycle.margin_required = round(seg_margin, 2)
            cycle.notional = seg_notional

        # Distance percentages
        sp_dist_pct = round((spot_in - sp) / spot_in * 100, 2) if spot_in > 0 else 0
        sc_dist_pct = round((sc - spot_in) / spot_in * 100, 2) if spot_in > 0 else 0

        # ── Simulate forward ──────────────────────────────────────────────
        exit_date = expiry
        spot_out  = spot_in
        sp_e = bp_e = sc_e = bc_e = 0.0
        reason  = "EXPIRY"
        trigger = ""
        exit_type_str = "EXPIRY"
        seg_iv_flags: list[str] = []
        seg_price_from_settle = False
        seg_beyond_short = False

        # Previous day's data for BS pricing
        prev_day_spot = spot_in
        prev_day_prices = {"sp": sp_p, "bp": bp_p, "sc": sc_p, "bc": bc_p}

        for tdate in trading_dates:
            if tdate <= cur_entry:
                continue
            if tdate > expiry:
                break

            # §3: Corporate action check
            if tdate in corp_action_dates:
                # Close at previous day's prices
                sp_e = prev_day_prices["sp"]
                bp_e = prev_day_prices["bp"]
                sc_e = prev_day_prices["sc"]
                bc_e = prev_day_prices["bc"]
                exit_date = tdate
                spot_out = prev_day_spot
                reason = "CORP_ACTION"
                exit_type_str = "CORP_ACTION"
                break

            spot_now = get_spot(data, tdate, expiry)
            if not spot_now:
                continue

            # Get current prices for all 4 legs
            _sp_now = get_price(data, tdate, expiry, sp, "PE")
            _bp_now = get_price(data, tdate, expiry, bp, "PE")
            _sc_now = get_price(data, tdate, expiry, sc, "CE")
            _bc_now = get_price(data, tdate, expiry, bc, "CE")

            sp_now = _sp_now if _sp_now is not None and _sp_now > 0 else max(0.0, sp - spot_now)
            bp_now = _bp_now if _bp_now is not None and _bp_now > 0 else (max(0.0, bp - spot_now) if spot_now < bp else 0.0)
            sc_now = _sc_now if _sc_now is not None and _sc_now > 0 else max(0.0, spot_now - sc)
            bc_now = _bc_now if _bc_now is not None and _bc_now > 0 else (max(0.0, spot_now - bc) if spot_now > bc else 0.0)

            # §4e: Take-profit check (before trigger)
            if excel_mode and take_profit_pct > 0 and segment_initial_credit > 0 and not hold_to_expiry:
                current_cost = (sp_now - bp_now) + (sc_now - bc_now)
                if current_cost <= (1.0 - take_profit_pct) * segment_initial_credit:
                    sp_e, bp_e = sp_now, bp_now
                    sc_e, bc_e = sc_now, bc_now
                    exit_date = tdate
                    spot_out = spot_now
                    reason = "TAKE_PROFIT"
                    exit_type_str = "TAKE_PROFIT"
                    trigger = ""
                    break

            # §4: Intraday exit check (Excel mode) or legacy check
            if excel_mode and futures_data is not None:
                intra = get_intraday_spot(futures_data, underlying, tdate, prev_basis)
                if intra:
                    s_open, s_high, s_low, s_close = intra
                else:
                    s_open, s_high, s_low, s_close = None, None, None, spot_now

                hit_side, exit_spot, exit_type_tag = check_exit(
                    s_open, s_high, s_low, s_close,
                    put_trigger_level, call_trigger_level
                )

                # Update basis
                day_futures = futures_data.get(underlying, {}).get(tdate, {})
                if day_futures:
                    near_exp = min((e for e in day_futures if e > tdate), default=None)
                    if near_exp:
                        prev_basis = day_futures[near_exp]["close"] - day_futures[near_exp]["spot_close"]

                if hit_side:
                    # Check if spot went beyond short strike
                    if hit_side == "PUT_SIDE" and exit_spot < sp:
                        seg_beyond_short = True
                    elif hit_side == "CALL_SIDE" and exit_spot > sc:
                        seg_beyond_short = True

                    # Use BS pricing for trigger exits
                    T_exit = max((expiry - tdate).days, 0) / 365.0

                    # Build IV cache from previous day
                    iv_cache_pe: dict[float, float] = {}
                    iv_cache_ce: dict[float, float] = {}
                    prev_tdate = None
                    for td in trading_dates:
                        if td >= tdate:
                            break
                        prev_tdate = td
                    if prev_tdate:
                        prev_day_data = data.get(prev_tdate, {})
                        for (e, k, o), rec in prev_day_data.items():
                            if e != expiry:
                                continue
                            if rec["close"] <= 0 or rec["spot"] <= 0:
                                continue
                            T_prev_iv = max((expiry - prev_tdate).days, 0) / 365.0
                            iv_val, _ = solve_iv(rec["close"], rec["spot"], k, T_prev_iv, rf_rate, o)
                            if iv_val is not None:
                                if o == "PE":
                                    iv_cache_pe[k] = iv_val
                                else:
                                    iv_cache_ce[k] = iv_val

                    # Get today's option OHLC for bounds
                    today_data = data.get(tdate, {})

                    def _get_opt_bounds(expiry_d, strike_k, opt_t):
                        rec = today_data.get((expiry_d, round(strike_k, 2), opt_t), {})
                        return rec.get("low", 0.0), rec.get("high", 0.0)

                    sp_lo, sp_hi = _get_opt_bounds(expiry, sp, "PE")
                    bp_lo, bp_hi = _get_opt_bounds(expiry, bp, "PE")
                    sc_lo, sc_hi = _get_opt_bounds(expiry, sc, "CE")
                    bc_lo, bc_hi = _get_opt_bounds(expiry, bc, "CE")

                    sp_e = estimate_exit_price(exit_spot, prev_day_spot, sp, T_exit, rf_rate,
                                               prev_day_prices["sp"], "PE", sp_lo, sp_hi,
                                               iv_cache_pe, seg_iv_flags)
                    bp_e = estimate_exit_price(exit_spot, prev_day_spot, bp, T_exit, rf_rate,
                                               prev_day_prices["bp"], "PE", bp_lo, bp_hi,
                                               iv_cache_pe, seg_iv_flags)
                    sc_e = estimate_exit_price(exit_spot, prev_day_spot, sc, T_exit, rf_rate,
                                               prev_day_prices["sc"], "CE", sc_lo, sc_hi,
                                               iv_cache_ce, seg_iv_flags)
                    bc_e = estimate_exit_price(exit_spot, prev_day_spot, bc, T_exit, rf_rate,
                                               prev_day_prices["bc"], "CE", bc_lo, bc_hi,
                                               iv_cache_ce, seg_iv_flags)

                    exit_date = tdate
                    spot_out = exit_spot
                    reason = "SHIFT"
                    exit_type_str = exit_type_tag

                    if hit_side == "PUT_SIDE":
                        # Only close the put spread; retain the call spread
                        sc_e = bc_e = 0.0
                        trigger = "PUT_SIDE"
                    else:
                        # Only close the call spread; retain the put spread
                        sp_e = bp_e = 0.0
                        trigger = "CALL_SIDE"
                    break
            else:
                # Legacy mode
                hit = check_shift(spot_now, sp, sc, trigger_pct)
                if hit:
                    sp_now_val = sp_now
                    bp_now_val = bp_now
                    sc_now_val = sc_now
                    bc_now_val = bc_now

                    if hit == "PUT_SIDE":
                        # Only close the put spread; retain the call spread
                        sp_e, bp_e = sp_now_val, bp_now_val
                        sc_e = bc_e = 0.0
                        trigger = "PUT_SIDE"
                    else:
                        # Only close the call spread; retain the put spread
                        sc_e, bc_e = sc_now_val, bc_now_val
                        sp_e = bp_e = 0.0
                        trigger = "CALL_SIDE"

                    exit_date = tdate
                    spot_out  = spot_now
                    reason    = "SHIFT"
                    exit_type_str = "SHIFT"
                    break

            if tdate == expiry:
                sp_e = max(0.0, sp - spot_now)
                bp_e = max(0.0, bp - spot_now) if spot_now < sp else 0.0
                sc_e = max(0.0, spot_now - sc)
                bc_e = max(0.0, spot_now - bc) if spot_now > sc else 0.0
                exit_date = tdate
                spot_out  = spot_now
                reason    = "EXPIRY"
                exit_type_str = "EXPIRY"
                break

            # Update prev day tracking
            prev_day_spot = spot_now
            prev_day_prices = {"sp": sp_now, "bp": bp_now, "sc": sc_now, "bc": bc_now}

        # Fallback if expiry date had no data
        if reason == "EXPIRY" and sp_e == 0.0 and sc_e == 0.0:
            last = max((d for d in trading_dates if d <= expiry), default=cur_entry)
            spot_out = get_spot(data, last, expiry) or spot_in
            sp_e = max(0.0, sp - spot_out)
            sc_e = max(0.0, spot_out - sc)
            bp_e = max(0.0, bp - spot_out) if spot_out < sp else 0.0
            bc_e = max(0.0, spot_out - bc) if spot_out > sc else 0.0

        # Cost to close
        if reason == "SHIFT" and trigger == "PUT_SIDE":
            cost_to_close = sp_e - bp_e
        elif reason == "SHIFT" and trigger == "CALL_SIDE":
            cost_to_close = sc_e - bc_e
        else:
            cost_to_close = (sp_e + sc_e) - (bp_e + bc_e)

        seg_pnl = (net_credit - cost_to_close) * lot_size * lots

        seg_cash_margin  = round(seg_margin * CASH_MARGIN_PCT, 2)
        seg_stock_margin = round(seg_margin * STOCK_MARGIN_PCT, 2)

        segment = CondorSegment(
            segment_idx=seg_idx,
            entry_date=cur_entry,   exit_date=exit_date,
            expiry=expiry,
            spot_at_entry=round(spot_in, 2), spot_at_exit=round(spot_out, 2),
            sell_put=sp,  buy_put=bp,  sell_call=sc,  buy_call=bc,
            sell_put_entry=round(sp_p, 2),  buy_put_entry=round(bp_p, 2),
            sell_call_entry=round(sc_p, 2), buy_call_entry=round(bc_p, 2),
            sell_put_exit=round(sp_e, 2),   buy_put_exit=round(bp_e, 2),
            sell_call_exit=round(sc_e, 2),  buy_call_exit=round(bc_e, 2),
            net_credit=round(net_credit, 2),
            cost_to_close=round(cost_to_close, 2),
            segment_pnl=round(seg_pnl, 2),
            exit_reason=reason, shift_trigger=trigger,
            lot_size=lot_size, lots=lots,
            exit_type=exit_type_str,
            notional=seg_notional,
            margin=seg_margin,
            cash_margin=seg_cash_margin,
            stock_margin=seg_stock_margin,
            put_trigger=round(put_trigger_level, 2),
            call_trigger=round(call_trigger_level, 2),
            short_put_dist_pct=sp_dist_pct,
            short_call_dist_pct=sc_dist_pct,
            spot_beyond_short=seg_beyond_short,
            iv_flags=list(seg_iv_flags),
            price_from_settlement=seg_price_from_settle,
        )
        cycle.segments.append(segment)

        if reason != "SHIFT":
            cycle.final_exit_reason = reason
            break

        cycle.num_shifts += 1

        # Check max shifts
        if seg_idx >= max_shifts:
            cycle.final_exit_reason = "MAX_SHIFTS_STOP"
            break

        last_trigger = trigger

        if trigger == "PUT_SIDE":
            ret_sc, ret_bc     = sc, bc
            ret_sc_p, ret_bc_p = sc_p, bc_p
        elif trigger == "CALL_SIDE":
            ret_sp, ret_bp     = sp, bp
            ret_sp_p, ret_bp_p = sp_p, bp_p

        cur_entry = exit_date

    if not cycle.segments:
        return None

    cycle.total_pnl = round(sum(s.segment_pnl for s in cycle.segments), 2)
    if not cycle.final_exit_reason:
        cycle.final_exit_reason = cycle.segments[-1].exit_reason

    return cycle


def run_backtest(
    underlying: str,
    cfg: dict,
    start: date,
    end: date,
) -> BacktestSummary:
    """Run backtest for a single symbol using per-file loading (legacy 9-symbol mode)."""
    summary = BacktestSummary(
        underlying=underlying,
        lot_size=cfg["lot_size"],
        shift_trigger_pct=cfg["shift_trigger_pct"],
        short_otm_pct=cfg.get("short_otm_pct", DEFAULT_SHORT_OTM_PCT),
        wing_width_pct=cfg.get("wing_width_pct", DEFAULT_WING_WIDTH_PCT),
        capital_allocated=cfg.get("capital_allocated", 0),
    )

    data = load_data(underlying, start, end)
    if not data:
        summary.errors.append(f"No bhavcopy data found for {underlying}")
        return summary

    trading_dates = sorted(d for d in data if start <= d <= end)
    if not trading_dates:
        summary.errors.append("No trading dates in selected window")
        return summary

    all_expiries = {
        e for day in data.values()
        for (e, _, _) in day
        if start <= e <= end + timedelta(days=60)
    }

    processed: set[date] = set()
    for expiry in sorted(all_expiries):
        if expiry < start or expiry in processed:
            continue

        entry_date = None
        for tdate in trading_dates:
            if tdate >= expiry:
                break
            dte = (expiry - tdate).days
            if cfg["min_dte"] <= dte <= cfg["max_dte"]:
                if any(e == expiry for (e, _, _) in data.get(tdate, {})):
                    entry_date = tdate
                    break

        if not entry_date:
            continue

        if cfg.get("auto_lots"):
            spot = get_spot(data, entry_date, expiry)
            cfg["lots"] = lots_from_capital(
                cfg["capital_allocated"], spot,
                cfg["lot_size"], cfg["wing_width_pct"]
            )

        cycle = run_condor_cycle(underlying, expiry, entry_date, data, cfg, trading_dates)
        if cycle and cycle.segments:
            summary.cycles.append(cycle)
            processed.add(expiry)

    if not summary.cycles:
        summary.errors.append("No valid trades found -- check symbol/date range")
        return summary

    _compute_summary_stats(summary)
    return summary


def run_backtest_from_data(
    underlying: str,
    data: dict[date, dict],
    cfg: dict,
    start: date,
    end: date,
    futures_data: dict | None = None,
    corp_action_dates: set | None = None,
) -> BacktestSummary:
    """Run backtest using pre-loaded data (Excel/bulk mode)."""
    summary = BacktestSummary(
        underlying=underlying,
        lot_size=cfg["lot_size"],
        shift_trigger_pct=cfg["shift_trigger_pct"],
        short_otm_pct=cfg.get("short_otm_pct", DEFAULT_SHORT_OTM_PCT),
        wing_width_pct=cfg.get("wing_width_pct", DEFAULT_WING_WIDTH_PCT),
        capital_allocated=cfg.get("capital_allocated", 0),
    )

    if not data:
        summary.errors.append(f"No bhavcopy data found for {underlying}")
        return summary

    trading_dates = sorted(d for d in data if start <= d <= end)
    if not trading_dates:
        summary.errors.append("No trading dates in selected window")
        return summary

    all_expiries = {
        e for day in data.values()
        for (e, _, _) in day
        if start <= e <= end + timedelta(days=60)
    }

    # §5: No overlapping cycles
    last_close_date: date | None = None
    skip_until: date | None = None  # for corp action skipping

    for expiry in sorted(all_expiries):
        if expiry < start:
            continue

        # Skip expiries until after corp action ex-date
        if skip_until is not None and expiry <= skip_until:
            continue

        entry_date = None
        for tdate in trading_dates:
            if tdate >= expiry:
                break
            dte = (expiry - tdate).days
            min_dte = cfg.get("min_dte", DEFAULT_MIN_DTE)
            max_dte = cfg.get("max_dte", DEFAULT_MAX_DTE)
            if min_dte <= dte <= max_dte:
                if any(e == expiry for (e, _, _) in data.get(tdate, {})):
                    entry_date = tdate
                    break

        if not entry_date:
            continue

        # §5: No overlapping cycles
        if last_close_date is not None and entry_date <= last_close_date:
            continue

        # §6: Compute lots
        spot = get_spot(data, entry_date, expiry)
        if cfg.get("excel_mode") and cfg.get("auto_lots", False):
            if spot > 0:
                lots = compute_lots(cfg["capital_allocated"], spot, cfg["lot_size"])
                if lots == 0:
                    summary.n_insufficient_cap += 1
                    continue
                cfg["lots"] = lots
            else:
                continue
        elif cfg.get("auto_lots"):
            if spot > 0:
                cfg["lots"] = lots_from_capital(
                    cfg["capital_allocated"], spot,
                    cfg["lot_size"], cfg["wing_width_pct"]
                )

        cycle = run_condor_cycle(
            underlying, expiry, entry_date, data, cfg, trading_dates,
            futures_data=futures_data,
            corp_action_dates=corp_action_dates,
        )
        if cycle and cycle.segments:
            last_close_date = cycle.segments[-1].exit_date
            summary.cycles.append(cycle)

            # After CORP_ACTION, skip expiries until next one after ex-date
            if cycle.final_exit_reason == "CORP_ACTION":
                skip_until = expiry

    if not summary.cycles:
        summary.errors.append("No valid trades found -- check symbol/date range")
        return summary

    _compute_summary_stats(summary)

    # §8: Compute enhanced counts from segments
    for c in summary.cycles:
        for seg in c.segments:
            if seg.exit_type == "GAP_EXIT":
                summary.n_gap_exits += 1
            elif seg.exit_type == "INTRADAY_TRIGGER":
                summary.n_intraday_triggers += 1
            elif seg.exit_type == "TAKE_PROFIT":
                summary.n_take_profits += 1
            if seg.spot_beyond_short:
                summary.n_exits_beyond_short += 1
            for flg in seg.iv_flags:
                if flg == "IV_FROM_NEIGHBOUR":
                    summary.n_iv_neighbour += 1
                elif flg == "PRICE_FALLBACK":
                    summary.n_price_fallback += 1
            if seg.price_from_settlement:
                summary.n_price_from_settle += 1
        if c.final_exit_reason == "MAX_SHIFTS_STOP":
            summary.n_max_shifts_stop += 1

    # Peak margin
    margins_list = [c.margin_required for c in summary.cycles if c.margin_required > 0]
    if margins_list:
        summary.peak_margin = max(margins_list)

    return summary


def _compute_summary_stats(summary: BacktestSummary) -> None:
    """Compute aggregate statistics for a BacktestSummary."""
    pnls = [c.total_pnl for c in summary.cycles]
    summary.total_cycles    = len(pnls)
    summary.total_pnl       = round(sum(pnls), 2)
    summary.winning_cycles  = sum(1 for p in pnls if p > 0)
    summary.losing_cycles   = sum(1 for p in pnls if p <= 0)
    summary.win_rate        = summary.winning_cycles / summary.total_cycles * 100
    summary.avg_pnl_per_cycle = round(summary.total_pnl / summary.total_cycles, 2)
    summary.total_shifts    = sum(c.num_shifts for c in summary.cycles)
    summary.best_cycle      = max(pnls)
    summary.worst_cycle     = min(pnls)

    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p <= 0))
    summary.profit_factor   = round(gp / gl, 2) if gl else float("inf")
    summary.roi_pct         = round(summary.total_pnl / summary.capital_allocated * 100, 2) \
                               if summary.capital_allocated else 0.0

    margins = [c.margin_required for c in summary.cycles if c.margin_required > 0]
    summary.avg_margin       = round(sum(margins) / len(margins), 2) if margins else 0.0
    summary.avg_cash_margin  = round(summary.avg_margin * CASH_MARGIN_PCT, 2)
    summary.avg_stock_margin = round(summary.avg_margin * STOCK_MARGIN_PCT, 2)
    summary.roi_on_margin    = round(summary.total_pnl / summary.avg_margin * 100, 2) \
                               if summary.avg_margin else 0.0

    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = max(max_dd, peak - equity)
    summary.max_drawdown = round(max_dd, 2)


# ─── Excel loader ─────────────────────────────────────────────────────────────

def load_excel_stocks(excel_path: str) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(excel_path)
    ws = wb.active
    # Read header to find optional Wing Distance column
    headers = [cell.value for cell in ws[1]]
    wing_col_idx = None
    for i, h in enumerate(headers):
        if h and "wing" in str(h).lower() and "dist" in str(h).lower():
            wing_col_idx = i

    stocks = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if len(row) < 7:
            continue
        priority, name, put_dist, call_dist, switching, premium_pct, exit_pct = row[:7]
        if not name:
            continue
        wing_dist = None
        if wing_col_idx is not None and len(row) > wing_col_idx:
            wing_dist = row[wing_col_idx]
        stocks.append({
            "name":        name,
            "priority":    priority,
            "put_dist":    put_dist    or 0.03,
            "call_dist":   call_dist   or 0.03,
            "switching":   switching   or 0.02,
            "premium_pct": premium_pct or 0.4,
            "exit_pct":    exit_pct    or 0.01,
            "wing_dist":   wing_dist,
        })
    return stocks


def load_universe(excel_path: str, priority: int, default_wing: float) -> list[dict]:
    """Load Excel stocks filtered by priority, mapped via STOCK_MAP.
    Returns list of dicts with symbol and per-stock params."""
    all_stocks = load_excel_stocks(excel_path)
    filtered = [s for s in all_stocks if s["priority"] == priority]
    print(f"  Priority {priority} filter: {len(filtered)} stocks (from {len(all_stocks)} total)")

    result = []
    skipped = []
    for s in filtered:
        sym = STOCK_MAP.get(s["name"])
        if sym is None:
            skipped.append(s["name"])
            continue
        # Per-stock params from Excel
        short_put_pct = s["put_dist"] * 100
        short_call_pct = s["call_dist"] * 100
        shift_trigger_pct = s["switching"] * 100
        take_profit_pct = s["premium_pct"]  # keep as 0-1 fraction

        # Wing distance
        if s.get("wing_dist") is not None and s["wing_dist"] != "" and s["wing_dist"] is not None:
            try:
                wing = float(s["wing_dist"]) * 100
            except (ValueError, TypeError):
                wing = default_wing
        else:
            wing = default_wing

        result.append({
            "name": s["name"],
            "symbol": sym,
            "short_put_pct": round(short_put_pct, 2),
            "short_call_pct": round(short_call_pct, 2),
            "shift_trigger_pct": round(shift_trigger_pct, 2),
            "take_profit_pct": take_profit_pct,
            "wing_width_pct": round(wing, 2),
        })
    if skipped:
        print(f"  Skipped (no mapping): {', '.join(skipped)}")
    return result


# ─── Interactive CLI ──────────────────────────────────────────────────────────

BOLD  = "\033[1m"
CYAN  = "\033[96m"
GREEN = "\033[92m"
RED   = "\033[91m"
YEL   = "\033[93m"
DIM   = "\033[2m"
RESET = "\033[0m"


def clr(text: str, code: str) -> str:
    return f"{code}{text}{RESET}"


def divider(char: str = "-", width: int = 60) -> str:
    return clr(char * width, DIM)


def prompt(msg: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = input(f"  {CYAN}>{RESET} {msg}{DIM}{hint}{RESET}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def prompt_float(msg: str, default: float, min_val: float = 0.0,
                 max_val: float = 1e12) -> float:
    while True:
        raw = prompt(msg, str(default))
        try:
            v = float(raw)
            if min_val <= v <= max_val:
                return v
            print(f"    {RED}Please enter a value between {min_val} and {max_val}{RESET}")
        except ValueError:
            print(f"    {RED}Invalid number, try again{RESET}")


def prompt_date(msg: str, default: date) -> date:
    while True:
        raw = prompt(msg, default.isoformat())
        try:
            return date.fromisoformat(raw)
        except ValueError:
            print(f"    {RED}Use YYYY-MM-DD format{RESET}")


def select_symbols_interactive(registry: dict[str, dict]) -> list[str]:
    symbols = list(registry.keys())
    print(f"\n{divider()}")
    print(f"  {BOLD}Available Symbols{RESET}")
    print(divider())
    for i, sym in enumerate(symbols, 1):
        cfg = registry[sym]
        print(f"  {DIM}{i:2}.{RESET} {BOLD}{sym:<14}{RESET}"
              f"  lot={cfg['lot_size']:<5}  trigger={cfg['shift_trigger_pct']}%")
    print(f"\n  {DIM}Enter numbers separated by spaces, or press Enter for all{RESET}")
    raw = prompt("Select symbols (e.g. 1 3 5)", "all")

    if raw.lower() == "all":
        return symbols

    selected = []
    for tok in raw.split():
        try:
            idx = int(tok) - 1
            if 0 <= idx < len(symbols):
                selected.append(symbols[idx])
        except ValueError:
            if tok.upper() in registry:
                selected.append(tok.upper())

    if not selected:
        print(f"  {RED}No valid selection, using all symbols{RESET}")
        return symbols
    return selected


def configure_strategy_interactive(
    selected: list[str],
    registry: dict[str, dict],
    total_capital: float,
    short_otm_pct: float,
    wing_width_pct: float,
) -> dict[str, dict]:
    """Build per-symbol config, splitting capital equally across symbols."""
    cfgs: dict[str, dict] = {}
    capital_per_symbol = total_capital / len(selected)

    print(f"\n{divider()}")
    print(f"  {BOLD}Per-Symbol Configuration{RESET}")
    print(divider())
    print(f"  Capital per symbol: {GREEN}Rs.{capital_per_symbol:,.0f}{RESET}")

    for sym in selected:
        base = registry[sym]
        print(f"\n  {BOLD}{sym}{RESET}  (lot={base['lot_size']}, default trigger={base['shift_trigger_pct']}%)")

        trigger = prompt_float(
            f"Shift trigger % for {sym} (max {MAX_SHIFT_TRIGGER_PCT}%)",
            min(base["shift_trigger_pct"], MAX_SHIFT_TRIGGER_PCT), 0.1, MAX_SHIFT_TRIGGER_PCT
        )
        manual_lots_raw = prompt(
            f"Lots for {sym} (Enter for auto from capital)", ""
        )
        if manual_lots_raw:
            try:
                lots = max(1, int(manual_lots_raw))
                auto_lots = False
            except ValueError:
                lots = 1
                auto_lots = False
        else:
            lots = 1
            auto_lots = False

        cfgs[sym] = {
            "lot_size":          base["lot_size"],
            "shift_trigger_pct": trigger,
            "short_otm_pct":     short_otm_pct,
            "wing_width_pct":    wing_width_pct,
            "min_dte":           DEFAULT_MIN_DTE,
            "max_dte":           DEFAULT_MAX_DTE,
            "lots":              lots,
            "auto_lots":         auto_lots,
            "capital_allocated": capital_per_symbol,
            "lock_in_pct":       DEFAULT_LOCK_IN_PCT,
            "hold_to_expiry":    True,
        }

    return cfgs


def print_results(summaries: list[BacktestSummary], start: date, end: date,
                  excel_mode: bool = False) -> None:
    print(f"\n{divider('=')}")
    print(f"  {BOLD}BACKTEST RESULTS  {DIM}{start} -> {end}{RESET}")
    print(divider('='))

    combined_pnl = sum(s.total_pnl for s in summaries)
    total_cap = sum(s.capital_allocated for s in summaries)
    combined_roi = combined_pnl / total_cap * 100 if total_cap else 0

    pnl_col = GREEN if combined_pnl >= 0 else RED
    print(f"\n  {BOLD}Combined P&L : {pnl_col}{'+'if combined_pnl>=0 else ''}Rs.{combined_pnl:,.0f}{RESET}"
          f"  {DIM}({combined_roi:+.2f}% on Rs.{total_cap:,.0f} capital){RESET}")

    for s in summaries:
        print(f"\n{divider()}")
        pf_str = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "inf"
        pnl_c = GREEN if s.total_pnl >= 0 else RED
        print(f"  {BOLD}{s.underlying}{RESET}"
              f"  lot={s.lot_size}  trigger={s.shift_trigger_pct}%  "
              f"capital=Rs.{s.capital_allocated:,.0f}")

        if s.errors:
            for e in s.errors:
                print(f"  {RED}x {e}{RESET}")
            continue

        print(f"  P&L        : {pnl_c}{'+'if s.total_pnl>=0 else ''}Rs.{s.total_pnl:,.0f}{RESET}"
              f"  {DIM}ROI on capital {s.roi_pct:+.2f}%{RESET}")
        print(f"  Margin     : {YEL}Rs.{s.avg_margin:,.0f}{RESET} avg/cycle"
              f"  {DIM}(cash Rs.{s.avg_cash_margin:,.0f} + stock Rs.{s.avg_stock_margin:,.0f})"
              f"  ROI on margin {s.roi_on_margin:+.2f}%{RESET}")
        print(f"  Cycles     : {s.total_cycles}  "
              f"({GREEN}{s.winning_cycles}W{RESET}/{RED}{s.losing_cycles}L{RESET})  "
              f"Win Rate {s.win_rate:.1f}%")
        print(f"  Shifts     : {YEL}{s.total_shifts}{RESET}  "
              f"Avg P&L/cycle Rs.{s.avg_pnl_per_cycle:,.0f}  "
              f"Profit Factor {pf_str}")
        print(f"  Drawdown   : {RED}Rs.{s.max_drawdown:,.0f}{RESET}  "
              f"Best Rs.{s.best_cycle:,.0f}  Worst Rs.{s.worst_cycle:,.0f}")

        if not excel_mode:
            for c in s.cycles:
                pnl_c2 = GREEN if c.total_pnl >= 0 else RED
                shift_tag = f"  {YEL}[{c.num_shifts} shift(s)]{RESET}" if c.num_shifts else ""
                print(f"\n    {DIM}Expiry {c.expiry}{RESET}  entry {c.cycle_start}"
                      f"  DTE={c.dte_at_entry}"
                      f"  lots={c.lots}"
                      f"  P&L {pnl_c2}Rs.{c.total_pnl:,.0f}{RESET}{shift_tag}")
                for seg in c.segments:
                    if seg.exit_reason == "SHIFT":
                        full_tag = f" {RED}[BOTH LEGS]{RESET}" if "FULL" in seg.shift_trigger else ""
                        print(f"      {YEL}~{RESET} Seg {seg.segment_idx}"
                              f"  {seg.entry_date}->{seg.exit_date}"
                              f"  {seg.shift_trigger}{full_tag}"
                              f"  close cost Rs.{seg.cost_to_close:.2f}"
                              f"  P&L {(GREEN if seg.segment_pnl>=0 else RED)}"
                              f"Rs.{seg.segment_pnl:,.0f}{RESET}")
                    else:
                        print(f"      {GREEN}v{RESET} Seg {seg.segment_idx}"
                              f"  {seg.entry_date}->{seg.exit_date}"
                              f"  {seg.exit_reason}"
                              f"  P&L {(GREEN if seg.segment_pnl>=0 else RED)}"
                              f"Rs.{seg.segment_pnl:,.0f}{RESET}")

    # §8: Enhanced summary for Excel mode
    if excel_mode:
        successful = [s for s in summaries if not s.errors]
        total_corp = sum(len(s.corp_action_events) for s in successful)
        total_beyond = sum(s.n_exits_beyond_short for s in successful)
        total_gap = sum(s.n_gap_exits for s in successful)
        total_intra = sum(s.n_intraday_triggers for s in successful)
        total_tp = sum(s.n_take_profits for s in successful)
        total_ms = sum(s.n_max_shifts_stop for s in successful)
        total_iv_n = sum(s.n_iv_neighbour for s in successful)
        total_pf = sum(s.n_price_fallback for s in successful)
        total_ps = sum(s.n_price_from_settle for s in successful)
        total_insuf = sum(s.n_insufficient_cap for s in successful)

        print(f"\n  Corp actions      : {total_corp} events")
        print(f"  Exits beyond short: {total_beyond}  (only GAP_EXIT cases expected after fix)")
        print(f"  Gap exits         : {total_gap}")
        print(f"  Intraday triggers : {total_intra}")
        print(f"  Take profits      : {total_tp}")
        print(f"  Max-shifts stop   : {total_ms}")
        print(f"  IV neighbour flags: {total_iv_n}")
        print(f"  Price fallbacks   : {total_pf}")
        print(f"  Settlement prices : {total_ps}")
        print(f"  Insufficient cap  : {total_insuf}")

    print(f"\n{divider('=')}")


# ─── HTML Report ──────────────────────────────────────────────────────────────

HTML_STYLE = """<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#c9d1d9;padding:24px}
  h1{color:#58a6ff;border-bottom:1px solid #21262d;padding-bottom:12px;margin-bottom:16px;font-size:1.4em}
  h2{color:#3fb950;margin:40px 0 12px;font-size:1.1em}
  h3{color:#79c0ff;margin:20px 0 8px;font-size:.9em;text-transform:uppercase;letter-spacing:.06em}
  .meta{color:#8b949e;font-size:.82em;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin:12px 0 24px}
  .card{background:#161b22;border:1px solid #21262d;border-radius:6px;padding:12px 14px}
  .card .lbl{font-size:.7em;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}
  .card .val{font-size:1.4em;font-weight:700;margin-top:3px}
  .pos{color:#3fb950} .neg{color:#f85149} .neu{color:#58a6ff} .yel{color:#d29922}
  table{width:100%;border-collapse:collapse;font-size:.78em;margin:8px 0 20px}
  th{background:#161b22;color:#58a6ff;padding:7px 10px;text-align:left;
     border-bottom:1px solid #21262d;font-weight:600;white-space:nowrap}
  td{padding:6px 10px;border-bottom:1px solid #161b22;white-space:nowrap}
  tr:hover td{background:#161b22}
  .badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.72em;font-weight:600}
  .b-shift{background:#2d1e00;color:#d29922}
  .b-expiry{background:#0d2818;color:#3fb950}
  .b-other{background:#1a1a2e;color:#8b949e}
  .cycle{background:#0d1117;border:1px solid #21262d;border-radius:6px;
         padding:14px;margin-bottom:14px}
  .cycle-hd{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}
  .sqoff{color:#d29922;font-size:.8em;margin:4px 0 6px;padding-left:16px;border-left:2px solid #2d1e00}
  hr{border:none;border-top:1px solid #21262d;margin:28px 0}
  .footer{color:#484f58;font-size:.72em;margin-top:24px}
</style>"""


def _pnl(v: float) -> str:
    if v > 0:  return f'<span class="pos">+Rs.{v:,.0f}</span>'
    if v < 0:  return f'<span class="neg">-Rs.{abs(v):,.0f}</span>'
    return '<span class="neu">Rs.0</span>'


def _badge(r: str) -> str:
    if r == "SHIFT":   return '<span class="badge b-shift">~ SHIFT</span>'
    if r == "EXPIRY":  return '<span class="badge b-expiry">v EXPIRY</span>'
    if r == "TAKE_PROFIT": return '<span class="badge b-expiry">$ TAKE_PROFIT</span>'
    if r == "CORP_ACTION": return '<span class="badge b-other">! CORP_ACTION</span>'
    if r == "MAX_SHIFTS_STOP": return '<span class="badge b-other">! MAX_SHIFTS</span>'
    if "FULL" in r:    return f'<span class="badge b-shift">~~ {r}</span>'
    return f'<span class="badge b-other">! {r}</span>'


def _seg_table(segs: list[CondorSegment], excel_mode: bool = False) -> str:
    rows = ""
    for s in segs:
        trig = f"<br><small style='color:#d29922'>{s.shift_trigger}</small>" if s.shift_trigger else ""
        extra_cols = ""
        if excel_mode:
            iv_str = ", ".join(s.iv_flags) if s.iv_flags else "-"
            extra_cols = (
                f"<td>{s.exit_type}</td>"
                f"<td>{s.put_trigger:,.0f}</td>"
                f"<td>{s.call_trigger:,.0f}</td>"
                f"<td>{s.short_put_dist_pct:.1f}%</td>"
                f"<td>{s.short_call_dist_pct:.1f}%</td>"
                f"<td>{'Y' if s.spot_beyond_short else ''}</td>"
                f"<td style='font-size:.7em'>{iv_str}</td>"
            )
        rows += (
            f"<tr>"
            f"<td>{s.segment_idx}</td>"
            f"<td>{s.entry_date}</td>"
            f"<td>{s.exit_date}{trig}</td>"
            f"<td>{s.spot_at_entry:,.1f}</td>"
            f"<td>{s.spot_at_exit:,.1f}</td>"
            f"<td>{s.sell_put:,.0f} / {s.buy_put:,.0f}</td>"
            f"<td>{s.sell_call:,.0f} / {s.buy_call:,.0f}</td>"
            f"<td>Rs.{s.net_credit:.2f}</td>"
            f"<td>Rs.{s.cost_to_close:.2f}</td>"
            f"<td>{_pnl(s.segment_pnl)}</td>"
            f"<td>{_badge(s.exit_reason)}</td>"
            f"{extra_cols}"
            f"</tr>"
        )
    extra_headers = ""
    if excel_mode:
        extra_headers = (
            "<th>Exit Type</th><th>Put Trig</th><th>Call Trig</th>"
            "<th>SP Dist%</th><th>SC Dist%</th><th>Beyond?</th><th>IV Flags</th>"
        )
    return (
        "<table><thead><tr>"
        "<th>#</th><th>Entry</th><th>Exit</th>"
        "<th>Spot In</th><th>Spot Out</th>"
        "<th>Short Put / Long Put</th><th>Short Call / Long Call</th>"
        "<th>Net Credit</th><th>Close Cost</th><th>Seg P&L</th><th>Exit</th>"
        f"{extra_headers}"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def generate_html(
    summaries: list[BacktestSummary],
    start: date,
    end: date,
    excel_mode: bool = False,
    skipped_stocks: list[str] | None = None,
) -> str:
    parts: list[str] = []

    combined_pnl = sum(s.total_pnl for s in summaries)
    total_cap    = sum(s.capital_allocated for s in summaries)
    total_shifts = sum(s.total_shifts for s in summaries)
    combined_roi = combined_pnl / total_cap * 100 if total_cap else 0
    total_cyc    = sum(s.total_cycles for s in summaries)

    pnl_cls = "pos" if combined_pnl >= 0 else "neg"

    if excel_mode:
        strategy_desc = "per-stock OTM distances from Excel"
    else:
        strategy_desc = (f"+/-{summaries[0].short_otm_pct if summaries else 5}% OTM shorts, "
                        f"+/-{(summaries[0].short_otm_pct + summaries[0].wing_width_pct) if summaries else 8}% OTM longs")

    # Portfolio summary extra cards for Excel mode
    extra_cards = ""
    if excel_mode:
        successful = [s for s in summaries if not s.errors]
        peak_margin_all = max((s.peak_margin for s in successful), default=0)
        total_gap = sum(s.n_gap_exits for s in successful)
        total_intra = sum(s.n_intraday_triggers for s in successful)
        total_tp = sum(s.n_take_profits for s in successful)
        total_pf = sum(s.n_price_fallback for s in successful)
        extra_cards = f"""
          <div class="card"><div class="lbl">Peak Margin</div>
            <div class="val yel">Rs.{peak_margin_all:,.0f}</div></div>
          <div class="card"><div class="lbl">Gap Exits</div>
            <div class="val yel">{total_gap}</div></div>
          <div class="card"><div class="lbl">Intraday Triggers</div>
            <div class="val yel">{total_intra}</div></div>
          <div class="card"><div class="lbl">Take Profits</div>
            <div class="val pos">{total_tp}</div></div>
          <div class="card"><div class="lbl">Price Fallbacks</div>
            <div class="val neg">{total_pf}</div></div>
        """

    parts.append(f"""
    <h1>Shifting Iron Condor -- Backtest Report</h1>
    <p class="meta">Period: <strong>{start}</strong> -> <strong>{end}</strong>
    &nbsp;-&nbsp; Strategy: {strategy_desc}
    &nbsp;-&nbsp; Capital: Rs.{total_cap:,.0f}</p>

    <div class="grid">
      <div class="card"><div class="lbl">Combined P&L</div>
        <div class="val {pnl_cls}">{'+' if combined_pnl>=0 else ''}Rs.{combined_pnl:,.0f}</div></div>
      <div class="card"><div class="lbl">ROI on Capital</div>
        <div class="val {pnl_cls}">{combined_roi:+.2f}%</div></div>
      <div class="card"><div class="lbl">Total Capital</div>
        <div class="val neu">Rs.{total_cap:,.0f}</div></div>
      <div class="card"><div class="lbl">Symbols</div>
        <div class="val neu">{len(summaries)}</div></div>
      <div class="card"><div class="lbl">Cycles</div>
        <div class="val neu">{total_cyc}</div></div>
      <div class="card"><div class="lbl">Total Shifts</div>
        <div class="val yel">{total_shifts}</div></div>
      {extra_cards}
    </div>
    """)

    # Skipped stocks section
    if skipped_stocks:
        parts.append("<h3>Skipped Stocks</h3><p style='color:#8b949e;font-size:.82em'>")
        parts.append(", ".join(skipped_stocks))
        parts.append("</p>")

    for s in summaries:
        pf_str  = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "inf"
        pc      = "pos" if s.total_pnl >= 0 else "neg"
        wc_cls  = "pos" if s.win_rate >= 50 else "neg"

        parts.append(f"""
        <hr>
        <h2>{s.underlying}
          &nbsp;<small style="color:#8b949e;font-size:.75em">
          lot={s.lot_size} - trigger={s.shift_trigger_pct}% -
          capital=Rs.{s.capital_allocated:,.0f}
          </small>
        </h2>
        """)

        if s.errors:
            for e in s.errors:
                parts.append(f'<p style="color:#f85149">! {e}</p>')
            continue

        # Per-stock Excel params
        if excel_mode and s.excel_params:
            ep = s.excel_params
            parts.append(f"""
            <p style="color:#8b949e;font-size:.78em">
              Put OTM: {ep.get('short_put_pct',0):.1f}% |
              Call OTM: {ep.get('short_call_pct',0):.1f}% |
              Trigger: {ep.get('shift_trigger_pct',0):.1f}% |
              Take-Profit: {ep.get('take_profit_pct',0):.0%} |
              Avg Margin: Rs.{s.avg_margin:,.0f}
              (Cash 50%: Rs.{s.avg_cash_margin:,.0f} | Stock 50%: Rs.{s.avg_stock_margin:,.0f}) |
              ROI on margin: {s.roi_on_margin:+.2f}%
            </p>
            """)

        parts.append(f"""
        <div class="grid">
          <div class="card"><div class="lbl">Total P&L</div>
            <div class="val {pc}">{'+' if s.total_pnl>=0 else ''}Rs.{s.total_pnl:,.0f}</div></div>
          <div class="card"><div class="lbl">ROI on Capital</div>
            <div class="val {pc}">{s.roi_pct:+.2f}%</div></div>
          <div class="card"><div class="lbl">Avg Margin/Cycle</div>
            <div class="val yel">Rs.{s.avg_margin:,.0f}</div></div>
          <div class="card"><div class="lbl">Cash Margin (50%)</div>
            <div class="val yel">Rs.{s.avg_cash_margin:,.0f}</div></div>
          <div class="card"><div class="lbl">Stock Margin (50%)</div>
            <div class="val yel">Rs.{s.avg_stock_margin:,.0f}</div></div>
          <div class="card"><div class="lbl">ROI on Margin</div>
            <div class="val {pc}">{s.roi_on_margin:+.2f}%</div></div>
          <div class="card"><div class="lbl">Win Rate</div>
            <div class="val {wc_cls}">{s.win_rate:.1f}%</div></div>
          <div class="card"><div class="lbl">Cycles (W/L)</div>
            <div class="val neu">{s.total_cycles} ({s.winning_cycles}W/{s.losing_cycles}L)</div></div>
          <div class="card"><div class="lbl">Total Shifts</div>
            <div class="val yel">{s.total_shifts}</div></div>
          <div class="card"><div class="lbl">Avg P&L / Cycle</div>
            <div class="val {pc}">{'+' if s.avg_pnl_per_cycle>=0 else ''}Rs.{s.avg_pnl_per_cycle:,.0f}</div></div>
          <div class="card"><div class="lbl">Profit Factor</div>
            <div class="val neu">{pf_str}</div></div>
          <div class="card"><div class="lbl">Max Drawdown</div>
            <div class="val neg">Rs.{s.max_drawdown:,.0f}</div></div>
          <div class="card"><div class="lbl">Best / Worst</div>
            <div class="val neu">+Rs.{s.best_cycle:,.0f} / -Rs.{abs(s.worst_cycle):,.0f}</div></div>
        </div>
        """)

        # Cycle summary table
        parts.append("<h3>Cycle Summary</h3>")
        rows = ""
        eq = 0.0
        for c in s.cycles:
            eq += c.total_pnl
            st = f"{c.num_shifts}x" if c.num_shifts else "--"
            rows += (
                f"<tr>"
                f"<td>{c.expiry}</td><td>{c.cycle_start}</td>"
                f"<td>{c.dte_at_entry}d</td><td>{c.lots}</td>"
                f"<td>{len(c.segments)}</td><td>{st}</td>"
                f"<td>{_pnl(c.total_pnl)}</td>"
                f"<td>{_pnl(eq)}</td>"
                f"<td>{_badge(c.final_exit_reason)}</td>"
                f"</tr>"
            )
        parts.append(
            "<table><thead><tr>"
            "<th>Expiry</th><th>Entry</th><th>DTE</th><th>Lots</th>"
            "<th>Segs</th><th>Shifts</th><th>Cycle P&L</th>"
            "<th>Running P&L</th><th>Final Exit</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )

        # Detailed cycles
        parts.append("<h3>Detailed Trade Log</h3>")
        for c in s.cycles:
            shift_note = f" &nbsp; <span class='yel'>{c.num_shifts} shift(s)</span>" if c.num_shifts else ""
            parts.append(f"""
            <div class="cycle">
              <div class="cycle-hd">
                <span style="color:#58a6ff;font-weight:700">Expiry {c.expiry}</span>
                <span>{_pnl(c.total_pnl)}</span>
                <span style="color:#8b949e;font-size:.8em">
                  Entry {c.cycle_start} &nbsp;-&nbsp; DTE {c.dte_at_entry}
                  &nbsp;-&nbsp; {c.lots} lot(s){shift_note}
                </span>
              </div>
            """)
            for i, seg in enumerate(c.segments):
                if i > 0:
                    prev = c.segments[i - 1]
                    trig = prev.shift_trigger
                    if trig == "PUT_SIDE":
                        closed_desc   = f"put spread ({prev.sell_put:,.0f}/{prev.buy_put:,.0f})"
                        retained_desc = f"call spread ({prev.sell_call:,.0f}/{prev.buy_call:,.0f}) retained"
                        reenter_desc  = "Re-entering put leg"
                    elif trig == "CALL_SIDE":
                        closed_desc   = f"call spread ({prev.sell_call:,.0f}/{prev.buy_call:,.0f})"
                        retained_desc = f"put spread ({prev.sell_put:,.0f}/{prev.buy_put:,.0f}) retained"
                        reenter_desc  = "Re-entering call leg"
                    elif trig == "PUT_SIDE_FULL":
                        closed_desc   = (f"BOTH legs -- put ({prev.sell_put:,.0f}/{prev.buy_put:,.0f}) "
                                         f"+ call ({prev.sell_call:,.0f}/{prev.buy_call:,.0f})")
                        retained_desc = "call also closed (decayed / also threatened)"
                        reenter_desc  = "Re-entering full condor"
                    else:
                        closed_desc   = (f"BOTH legs -- call ({prev.sell_call:,.0f}/{prev.buy_call:,.0f}) "
                                         f"+ put ({prev.sell_put:,.0f}/{prev.buy_put:,.0f})")
                        retained_desc = "put also closed (decayed / also threatened)"
                        reenter_desc  = "Re-entering full condor"
                    parts.append(f"""
                    <div class="sqoff">
                      ~ <strong>Square-Off #{i}</strong>:
                      Spot {prev.spot_at_exit:,.1f} triggered <strong>{trig}</strong> --
                      closed {closed_desc} at cost Rs.{prev.cost_to_close:.2f}
                      ({retained_desc}).
                      Seg P&L {_pnl(prev.segment_pnl)}.
                      {reenter_desc} at spot {seg.spot_at_entry:,.1f}.
                    </div>
                    """)
            parts.append(_seg_table(c.segments, excel_mode=excel_mode))
            parts.append("</div>")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts.append(f"""
    <hr>
    <p class="footer">
      Generated by shifting_condor_backtest.py - {ts} -
      Data: NSE F&amp;O Bhavcopy settlement prices - For research purposes only.
    </p>""")

    return (f"<!DOCTYPE html><html lang='en'><head>"
            f"<meta charset='UTF-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Shifting Condor Backtest</title>"
            f"{HTML_STYLE}</head><body>"
            f"{''.join(parts)}"
            f"</body></html>")


# ─── Argument Parser ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shifting_condor_backtest.py",
        description="Shifting Iron Condor Backtest -- Interactive or CLI mode",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python shifting_condor_backtest.py\n"
            "  python shifting_condor_backtest.py --symbols ICICIBANK BHARTIARTL --capital 500000\n"
            "  python shifting_condor_backtest.py --list-symbols\n"
            "  python shifting_condor_backtest.py --universe-file 'pictures/NSE FO list with targets.xlsx'"
            " --capital 1000000\n"
        ),
    )
    p.add_argument("--symbols", nargs="+", metavar="SYM",
                   help="Symbols to backtest (space-separated). Use --list-symbols to see all.")
    p.add_argument("--capital", type=float, metavar="INR",
                   help="Total capital in Rs. (split equally across symbols).")
    p.add_argument("--trigger", type=float, nargs="+", metavar="PCT",
                   help="Shift trigger %% per symbol (one value or one per symbol).")
    p.add_argument("--otm", type=float, default=None, metavar="PCT",
                   help=f"Short strike OTM %% (default: per-stock from Excel, or {DEFAULT_SHORT_OTM_PCT} in CLI mode)")
    p.add_argument("--wing", type=float, default=DEFAULT_WING_WIDTH_PCT, metavar="PCT",
                   help=f"Wing width %% beyond short (default: {DEFAULT_WING_WIDTH_PCT})")
    p.add_argument("--lots", type=int, metavar="N",
                   help="Fixed lots for all symbols (overrides capital auto-sizing).")
    p.add_argument("--start", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="Backtest start date")
    p.add_argument("--end", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="Backtest end date")
    p.add_argument("--lock-in", type=float, default=DEFAULT_LOCK_IN_PCT, metavar="PCT",
                   help=(f"Both-legs close threshold (0-1). Default: {DEFAULT_LOCK_IN_PCT}"))
    p.add_argument("--no-report", action="store_true",
                   help="Skip HTML report generation.")
    p.add_argument("--list-symbols", action="store_true",
                   help="Print all available symbols and exit.")
    # Excel-driven mode flags
    p.add_argument("--universe-file", type=str, metavar="XLSX",
                   help="Path to Excel file with per-stock strategy params.")
    p.add_argument("--priority", type=int, default=1, metavar="N",
                   help="Priority level to filter from Excel (default: 1)")
    p.add_argument("--rf-rate", type=float, default=0.0525, metavar="RATE",
                   help="Risk-free rate for BS pricing (default: 0.0525)")
    p.add_argument("--margin-pct", type=float, default=0.125, metavar="PCT",
                   help="Margin percentage of notional (default: 0.125)")
    p.add_argument("--capital-per-symbol", type=float, metavar="INR",
                   help="Capital per symbol (overrides equal split).")
    p.add_argument("--max-shifts", type=int, default=MAX_SHIFTS_PER_CYCLE, metavar="N",
                   help=f"Max shifts per cycle (default: {MAX_SHIFTS_PER_CYCLE})")
    p.add_argument("--exclude", nargs="+", metavar="SYM", default=[],
                   help="Symbols to exclude from the universe (space-separated, e.g. AMBER ANGELONE CAMS)")
    return p


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    global MARGIN_PCT

    parser = build_parser()
    args   = parser.parse_args()

    # Apply global margin pct
    MARGIN_PCT = args.margin_pct

    # ── List symbols mode ─────────────────────────────────────────────────
    if args.list_symbols:
        print(f"\n{BOLD}Available Symbols{RESET}\n")
        for sym, cfg in SYMBOL_REGISTRY.items():
            print(f"  {sym:<14}  lot_size={cfg['lot_size']:<5}  "
                  f"default_trigger={cfg['shift_trigger_pct']}%")
        print()
        sys.exit(0)

    # Detect cache date range
    cache_start, cache_end = detect_cache_date_range(CACHE_DIR)

    # ── Excel-driven mode ─────────────────────────────────────────────────
    if args.universe_file:
        _run_excel_mode(args, cache_start, cache_end)
        return

    # ── CLI mode (all args provided) ──────────────────────────────────────
    if args.symbols and args.capital:
        selected = [s.upper() for s in args.symbols]
        unknown  = [s for s in selected if s not in SYMBOL_REGISTRY]
        if unknown:
            print(f"{RED}Unknown symbol(s): {', '.join(unknown)}{RESET}")
            print(f"Run with --list-symbols to see available options.")
            sys.exit(1)

        total_capital   = args.capital
        short_otm_pct   = args.otm if args.otm is not None else DEFAULT_SHORT_OTM_PCT
        wing_width_pct  = args.wing
        start = args.start or (cache_end - timedelta(days=90))
        end   = args.end   or cache_end

        capital_per = total_capital / len(selected)
        cfgs: dict[str, dict] = {}
        for i, sym in enumerate(selected):
            if args.trigger:
                trig = args.trigger[i] if i < len(args.trigger) else args.trigger[-1]
            else:
                trig = SYMBOL_REGISTRY[sym]["shift_trigger_pct"]
            trig = min(trig, MAX_SHIFT_TRIGGER_PCT)  # cap at 1.5%

            lots_val = args.lots if args.lots else 1
            cfgs[sym] = {
                "lot_size":          SYMBOL_REGISTRY[sym]["lot_size"],
                "shift_trigger_pct": trig,
                "short_otm_pct":     short_otm_pct,
                "wing_width_pct":    wing_width_pct,
                "min_dte":           DEFAULT_MIN_DTE,
                "max_dte":           DEFAULT_MAX_DTE,
                "lots":              lots_val,
                "auto_lots":         False,
                "capital_allocated": capital_per,
                "lock_in_pct":       args.lock_in,
                "max_shifts":        args.max_shifts,
                "hold_to_expiry":    True,
            }

    # ── Interactive mode ──────────────────────────────────────────────────
    else:
        print(f"\n{divider('=')}")
        print(f"  {BOLD}Shifting Iron Condor Backtest{RESET}")
        print(divider('='))
        print(f"  {DIM}Cache data: {cache_start} -> {cache_end}{RESET}")

        selected = select_symbols_interactive(SYMBOL_REGISTRY)
        print(f"\n  {GREEN}Selected: {', '.join(selected)}{RESET}")

        print(f"\n{divider()}")
        print(f"  {BOLD}Capital & Dates{RESET}")
        print(divider())

        total_capital  = prompt_float("Total capital (Rs.)", 500_000, 1_000)
        short_otm_pct  = prompt_float("Short strike OTM %", DEFAULT_SHORT_OTM_PCT, 1.0, 30.0)
        wing_width_pct = prompt_float("Wing width % (beyond short)", DEFAULT_WING_WIDTH_PCT, 0.5, 20.0)

        default_start = cache_end - timedelta(days=90)
        start = prompt_date("Backtest start date", default_start)
        end   = prompt_date("Backtest end date",   cache_end)

        cfgs = configure_strategy_interactive(
            selected, SYMBOL_REGISTRY, total_capital,
            short_otm_pct, wing_width_pct,
        )

    # ── Run backtests ─────────────────────────────────────────────────────
    print(f"\n{divider()}")
    print(f"  {BOLD}Running backtests...{RESET}  {DIM}{start} -> {end}{RESET}")
    print(divider())

    summaries: list[BacktestSummary] = []
    for sym, cfg in cfgs.items():
        print(f"  {DIM}[{sym}]{RESET} loading & simulating...", end="", flush=True)
        s = run_backtest(sym, cfg, start, end)
        summaries.append(s)
        status = f"{GREEN}v{RESET}" if not s.errors else f"{RED}x{RESET}"
        print(f"\r  {status} [{sym}]  {len(s.cycles)} cycle(s), {s.total_shifts} shift(s)")

    # ── Print results ─────────────────────────────────────────────────────
    print_results(summaries, start, end)

    # ── Save HTML report ──────────────────────────────────────────────────
    skip_report = args.no_report if hasattr(args, "no_report") else False
    if not skip_report:
        os.makedirs(REPORT_DIR, exist_ok=True)
        ts_str    = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = os.path.join(REPORT_DIR, f"shifting_condor_{ts_str}.html")
        html      = generate_html(summaries, start, end)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n  {GREEN}Report saved:{RESET} {html_path}")
        try:
            import subprocess
            subprocess.Popen(["xdg-open", html_path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _run_excel_mode(args, cache_start: date, cache_end: date) -> None:
    """Excel-driven backtest mode."""
    print("=" * 65)
    print("  Shifting Iron Condor -- Excel-driven Backtest")
    print("=" * 65)

    start = args.start or cache_start
    end   = args.end   or cache_end
    print(f"  Data range : {start} -> {end}")

    total_capital = args.capital or 1_000_000.0

    # Load universe
    universe = load_universe(args.universe_file, args.priority, args.wing)
    if not universe:
        print("  No stocks found in universe. Exiting.")
        return

    # Apply --exclude filter
    exclude_syms = {s.upper() for s in (args.exclude or [])}
    if exclude_syms:
        before = len(universe)
        universe = [u for u in universe if u["symbol"] not in exclude_syms]
        print(f"  Excluded   : {', '.join(sorted(exclude_syms))}  ({before - len(universe)} removed, {len(universe)} remaining)")

    # Bulk load all symbols
    symbols_needed = {s["symbol"] for s in universe}
    all_data, futures_data, live_lot_sizes = load_all_symbols(
        symbols_needed, start, end, CACHE_DIR
    )
    print(f"  Lot sizes from bhavcopy: {len(live_lot_sizes)} symbols")

    # Check which symbols are actually in bhavcopy
    # Get symbols from latest bhavcopy
    latest_syms = set()
    latest_file = sorted(Path(CACHE_DIR).glob("fo_*.csv"))
    if latest_file:
        lf = latest_file[-1]
        try:
            with open(lf, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    s = (row.get("TckrSymb") or "").strip().upper()
                    if s:
                        latest_syms.add(s)
        except Exception:
            pass

    for u in universe:
        if u["symbol"] not in latest_syms:
            print(f"  WARN: {u['symbol']} (from '{u['name']}') NOT found in latest bhavcopy")

    # Capital split
    capital_per_sym = args.capital_per_symbol or (total_capital / len(universe))
    print(f"  Capital    : Rs.{total_capital:,.0f} total | Rs.{capital_per_sym:,.0f}/symbol")
    print()

    # Run per symbol
    summaries: list[BacktestSummary] = []
    skipped_stocks: list[str] = []

    for i, u in enumerate(universe, 1):
        sym      = u["symbol"]
        lot_size = live_lot_sizes.get(sym, 100)

        # Cap shift trigger at MAX_SHIFT_TRIGGER_PCT (1.5%) to reduce excessive shifting
        # Allow --trigger to override the per-stock Excel value
        excel_trigger = u["shift_trigger_pct"]
        if args.trigger:
            excel_trigger = args.trigger[0] if len(args.trigger) == 1 else args.trigger[min(i - 1, len(args.trigger) - 1)]
        capped_trigger = min(excel_trigger, MAX_SHIFT_TRIGGER_PCT)

        # Allow --otm to override the per-stock Excel OTM values
        put_pct  = args.otm if args.otm is not None else u["short_put_pct"]
        call_pct = args.otm if args.otm is not None else u["short_call_pct"]

        cfg = {
            "lot_size":          lot_size,
            "shift_trigger_pct": capped_trigger,
            "short_otm_pct":     (put_pct + call_pct) / 2,
            "short_put_pct":     put_pct,
            "short_call_pct":    call_pct,
            "wing_width_pct":    u["wing_width_pct"],
            "take_profit_pct":   u["take_profit_pct"],
            "min_dte":           DEFAULT_MIN_DTE,
            "max_dte":           DEFAULT_MAX_DTE,
            "lots":              args.lots if args.lots else 1,
            "auto_lots":         False,
            "excel_mode":        True,
            "hold_to_expiry":    True,
            "capital_allocated": capital_per_sym,
            "lock_in_pct":       DEFAULT_LOCK_IN_PCT,
            "max_shifts":        args.max_shifts,
            "rf_rate":           args.rf_rate,
        }

        print(f"  [{i:3}/{len(universe)}] {sym:<14} "
              f"lot={lot_size:<5} PutOTM={put_pct}% "
              f"CallOTM={call_pct}% "
              f"trigger={capped_trigger}%"
              f"{' (capped)' if capped_trigger < u['shift_trigger_pct'] else ''} ...",
              end="", flush=True)

        sym_data = all_data.get(sym, {})
        sym_futures = futures_data.get(sym, {})

        # Detect corp actions
        corp_dates, corp_events = detect_corp_actions(sym, sym_data)

        summary = run_backtest_from_data(
            sym, sym_data, cfg, start, end,
            futures_data=sym_futures,
            corp_action_dates=corp_dates,
        )
        summary.corp_action_events = corp_events
        summary.excel_params = {
            "short_put_pct":    u["short_put_pct"],
            "short_call_pct":   u["short_call_pct"],
            "shift_trigger_pct": capped_trigger,   # actual value used (capped at 1.5%)
            "take_profit_pct":  u["take_profit_pct"],
            "wing_width_pct":   u["wing_width_pct"],
        }
        summaries.append(summary)

        if summary.errors:
            print(f"  x {summary.errors[0]}")
            skipped_stocks.append(f"{sym}: {summary.errors[0]}")
        else:
            sign = "+" if summary.total_pnl >= 0 else ""
            print(f"  {len(summary.cycles)} cycles | "
                  f"P&L {sign}Rs.{summary.total_pnl:,.0f} | "
                  f"Win {summary.win_rate:.0f}%")

    # ── Summary ────────────────────────────────────────────────────────────
    successful = [s for s in summaries if not s.errors]
    failed     = [s for s in summaries if s.errors]

    total_pnl  = sum(s.total_pnl for s in successful)
    total_cap  = sum(s.capital_allocated for s in summaries)
    total_cyc  = sum(s.total_cycles for s in successful)
    total_shft = sum(s.total_shifts for s in successful)
    overall_wr = (sum(s.winning_cycles for s in successful) /
                  max(1, total_cyc) * 100)
    peak_margin = max((s.peak_margin for s in successful), default=0)

    print()
    print("=" * 65)
    print("  FINAL SUMMARY")
    print("=" * 65)
    print(f"  Stocks run     : {len(summaries)}  "
          f"({len(successful)} succeeded, {len(failed)} no trades)")
    print(f"  Total cycles   : {total_cyc}")
    print(f"  Total shifts   : {total_shft}")
    sign = "+" if total_pnl >= 0 else ""
    print(f"  Combined P&L   : {sign}Rs.{total_pnl:,.0f}")
    print(f"  ROI on capital : {total_pnl / total_cap * 100:+.2f}%" if total_cap else "")
    print(f"  Overall win %  : {overall_wr:.1f}%")
    print(f"  Peak margin    : Rs.{peak_margin:,.0f}")

    # Enhanced exit stats
    total_corp = sum(len(s.corp_action_events) for s in successful)
    total_beyond = sum(s.n_exits_beyond_short for s in successful)
    total_gap = sum(s.n_gap_exits for s in successful)
    total_intra = sum(s.n_intraday_triggers for s in successful)
    total_tp = sum(s.n_take_profits for s in successful)
    total_ms = sum(s.n_max_shifts_stop for s in successful)
    total_iv_n = sum(s.n_iv_neighbour for s in successful)
    total_pf = sum(s.n_price_fallback for s in successful)
    total_ps = sum(s.n_price_from_settle for s in successful)
    total_insuf = sum(s.n_insufficient_cap for s in successful)

    print(f"  Corp actions      : {total_corp} events")
    print(f"  Exits beyond short: {total_beyond}  (only GAP_EXIT cases expected after fix)")
    print(f"  Gap exits         : {total_gap}")
    print(f"  Intraday triggers : {total_intra}")
    print(f"  Take profits      : {total_tp}")
    print(f"  Max-shifts stop   : {total_ms}")
    print(f"  IV neighbour flags: {total_iv_n}")
    print(f"  Price fallbacks   : {total_pf}")
    print(f"  Settlement prices : {total_ps}")
    print(f"  Insufficient cap  : {total_insuf}")
    print()

    # ── HTML Report ────────────────────────────────────────────────────────
    skip_report = args.no_report if hasattr(args, "no_report") else False
    if not skip_report:
        os.makedirs(REPORT_DIR, exist_ok=True)
        ts_str    = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = os.path.join(REPORT_DIR, f"shifting_condor_full_{ts_str}.html")
        html      = generate_html(successful, start, end, excel_mode=True,
                                  skipped_stocks=skipped_stocks)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  Report saved: {html_path}")

        try:
            import subprocess
            subprocess.Popen(["xdg-open", html_path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


if __name__ == "__main__":
    main()
