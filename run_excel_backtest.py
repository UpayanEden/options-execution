#!/usr/bin/env python3
"""
Shifting Iron Condor Backtest — Excel-driven runner (optimised)
================================================================
Single-pass cache loader: reads all 159 bhavcopy files ONCE and builds
a per-symbol data dict, then runs the condor engine for each symbol
without touching disk again.

Reads strategy params from:
  pictures/NSE FO list with targets.xlsx

Column mapping (Excel → backtest engine):
  Put/Call Distance  → short_otm_pct  (avg of both, ×100)
  Switching Margin   → shift_trigger_pct (×100)
  Premium %          → lock_in_pct    (0–1 as-is)
  Week 3 Exit %      → wing_width_pct (×100, min 1%)
"""

from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl

# ── Paths ──────────────────────────────────────────────────────────────────────
CACHE_DIR  = "/media/upayan/Work/Quant Projects/projects/project1/nse_bhavcopy_cache"
EXCEL_PATH = "/media/upayan/Work/options_execution/pictures/NSE FO list with targets(1).xlsx"
REPORT_DIR = "/media/upayan/Work/options_execution/reports"

# ── Excel name → NSE bhavcopy symbol ──────────────────────────────────────────
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
    "Nestlé India":                             "NESTLEIND",
    "NestlÃ© India":                            "NESTLEIND",
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

# ── Import backtest engine ─────────────────────────────────────────────────────
sys.path.insert(0, "/media/upayan/Work/options_execution")
from shifting_condor_backtest import (
    run_condor_cycle, BacktestSummary, CondorCycle,
    detect_cache_date_range, generate_html,
    get_spot, get_strikes, lots_from_capital,
    CASH_MARGIN_PCT, STOCK_MARGIN_PCT,
)


# ── Single-pass bulk data loader ───────────────────────────────────────────────

def load_all_symbols(
    symbols: set[str],
    start: date,
    end: date,
    cache_dir: str,
) -> dict[str, dict[date, dict]]:
    """
    One pass through all bhavcopy files → per-symbol data dicts.
    Returns {symbol: {tdate: {(expiry, strike, opt): {close, spot}}}}
    """
    cache = Path(cache_dir)
    # alloc per-symbol dicts up front
    all_data: dict[str, dict] = {sym: {} for sym in symbols}
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
                    close = float(row.get("SttlmPric") or row.get("ClsPric") or "0")
                except Exception:
                    close = 0.0
                try:
                    spot = float(row.get("UndrlygPric") or "0")
                except Exception:
                    spot = 0.0
                # lot size (grab once per symbol)
                if sym not in lot_sizes:
                    raw_lot = row.get("NewBrdLotQty", "")
                    try:
                        lot_sizes[sym] = int(float(raw_lot))
                    except Exception:
                        pass

                sym_data = all_data[sym]
                if tdate not in sym_data:
                    sym_data[tdate] = {}
                sym_data[tdate][(exp_date, round(strike, 2), opt)] = {
                    "close": close, "spot": spot,
                }

        if fi % 20 == 0 or fi == len(files):
            print(f"    {fi}/{len(files)} files scanned...", flush=True)

    return all_data, lot_sizes


# ── Backtest runner using pre-loaded data ──────────────────────────────────────

def run_backtest_from_data(
    underlying: str,
    data: dict[date, dict],
    cfg: dict,
    start: date,
    end: date,
) -> BacktestSummary:
    from shifting_condor_backtest import DEFAULT_MIN_DTE, DEFAULT_MAX_DTE

    summary = BacktestSummary(
        underlying=underlying,
        lot_size=cfg["lot_size"],
        shift_trigger_pct=cfg["shift_trigger_pct"],
        short_otm_pct=cfg["short_otm_pct"],
        wing_width_pct=cfg["wing_width_pct"],
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

    processed: set[date] = set()
    for expiry in sorted(all_expiries):
        if expiry < start or expiry in processed:
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
        summary.errors.append("No valid trades found — check symbol/date range")
        return summary

    pnls = [c.total_pnl for c in summary.cycles]
    summary.total_cycles     = len(pnls)
    summary.total_pnl        = round(sum(pnls), 2)
    summary.winning_cycles   = sum(1 for p in pnls if p > 0)
    summary.losing_cycles    = sum(1 for p in pnls if p <= 0)
    summary.win_rate         = summary.winning_cycles / summary.total_cycles * 100
    summary.avg_pnl_per_cycle = round(summary.total_pnl / summary.total_cycles, 2)
    summary.total_shifts     = sum(c.num_shifts for c in summary.cycles)
    summary.best_cycle       = max(pnls)
    summary.worst_cycle      = min(pnls)

    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p <= 0))
    summary.profit_factor    = round(gp / gl, 2) if gl else float("inf")
    summary.roi_pct          = round(summary.total_pnl / summary.capital_allocated * 100, 2) \
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

    return summary


# ── Excel loader ───────────────────────────────────────────────────────────────

def load_excel_stocks(excel_path: str) -> list[dict]:
    wb = openpyxl.load_workbook(excel_path)
    ws = wb.active
    stocks = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if len(row) < 6:
            continue
        priority, name, put_dist, call_dist, switching, premium_pct = row[:6]
        removal = row[6] if len(row) > 6 else None
        if not name:
            continue
        # Skip stocks not configured (no distances set) or marked for removal
        if put_dist is None or call_dist is None:
            continue
        if removal and str(removal).strip().lower() == "remove":
            continue
        stocks.append({
            "name":        name,
            "priority":    priority,
            "put_dist":    put_dist,
            "call_dist":   call_dist,
            "switching":   switching   or 0.01,
            "premium_pct": premium_pct or 0.4,
        })
    return stocks


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 65)
    print("  Shifting Iron Condor — Full Stock Backtest (optimised)")
    print("=" * 65)

    cache_start, cache_end = detect_cache_date_range(CACHE_DIR)
    start, end = cache_start, cache_end
    print(f"  Data range : {start} → {end}")

    excel_stocks = load_excel_stocks(EXCEL_PATH)

    # Filter to priority 1 only
    excel_stocks = [s for s in excel_stocks if s.get("priority") == 1]
    print(f"  Priority 1 : {len(excel_stocks)} stocks")

    # Build runnable list
    skipped: list[str] = []
    runnable: list[dict] = []
    for s in excel_stocks:
        sym = STOCK_MAP.get(s["name"])
        if sym is None:
            skipped.append(s["name"])
            continue
        runnable.append({**s, "symbol": sym})

    print(f"  Runnable   : {len(runnable)} stocks")
    if skipped:
        print(f"  Skipped    : {', '.join(skipped)}")

    # ── One-pass bulk load ─────────────────────────────────────────────────
    symbols_needed = {s["symbol"] for s in runnable}
    all_data, live_lot_sizes = load_all_symbols(symbols_needed, start, end, CACHE_DIR)
    print(f"  Lot sizes from bhavcopy: {len(live_lot_sizes)} symbols")

    # Capital split
    total_capital   = 1_000_000.0
    capital_per_sym = total_capital / len(runnable)
    print(f"  Capital    : ₹{total_capital:,.0f} total | ₹{capital_per_sym:,.0f}/symbol")
    print()

    # ── Run per symbol ─────────────────────────────────────────────────────
    summaries: list[BacktestSummary] = []
    for i, s in enumerate(runnable, 1):
        sym      = s["symbol"]
        lot_size = live_lot_sizes.get(sym, 100)

        short_otm_pct = round((s["put_dist"] + s["call_dist"]) / 2 * 100, 2)
        trigger_pct   = 1.0  # 1% shift trigger
        lock_in       = s["premium_pct"]
        wing_pct      = 2.0  # fixed wing width (longs 2% beyond shorts)

        cfg = {
            "lot_size":          lot_size,
            "shift_trigger_pct": trigger_pct,
            "short_otm_pct":     short_otm_pct,
            "wing_width_pct":    wing_pct,
            "min_dte":           20,
            "max_dte":           45,
            "lots":              1,
            "auto_lots":         False,
            "capital_allocated": capital_per_sym,
            "lock_in_pct":       lock_in,
            "hold_to_expiry":    True,
        }

        print(f"  [{i:3}/{len(runnable)}] {sym:<14} "
              f"lot={lot_size:<5} OTM={short_otm_pct}% "
              f"trigger={trigger_pct}% ...",
              end="", flush=True)

        sym_data = all_data.get(sym, {})
        summary  = run_backtest_from_data(sym, sym_data, cfg, start, end)
        summaries.append(summary)

        if summary.errors:
            print(f"  ✗ {summary.errors[0]}")
        else:
            sign = "+" if summary.total_pnl >= 0 else ""
            print(f"  {len(summary.cycles)} cycles | "
                  f"P&L {sign}₹{summary.total_pnl:,.0f} | "
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

    print()
    print("=" * 65)
    print("  FINAL SUMMARY")
    print("=" * 65)
    print(f"  Stocks run     : {len(summaries)}  "
          f"({len(successful)} succeeded, {len(failed)} no trades)")
    print(f"  Total cycles   : {total_cyc}")
    print(f"  Total shifts   : {total_shft}")
    sign = "+" if total_pnl >= 0 else ""
    print(f"  Combined P&L   : {sign}₹{total_pnl:,.0f}")
    print(f"  ROI on capital : {total_pnl / total_cap * 100:+.2f}%")
    print(f"  Overall win %  : {overall_wr:.1f}%")
    print()

    # ── HTML Report ────────────────────────────────────────────────────────
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts_str    = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_path = os.path.join(REPORT_DIR, f"shifting_condor_full_{ts_str}.html")
    html      = generate_html(successful, start, end)
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
