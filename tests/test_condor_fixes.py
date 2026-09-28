"""Pytest suite for the corrected shifting condor engine."""
import math, pytest
from datetime import date
import sys; sys.path.insert(0, '/media/upayan/Work/options_execution')

from shifting_condor_backtest import (
    select_short_put, select_short_call, select_long_put, select_long_call,
    bs_price, solve_iv, compute_lots, margin_per_lot, check_exit, MARGIN_PCT,
)

# -- Strike selection ----------------------------------------------------------

def test_short_put_never_atm():
    sp = select_short_put(1000, 3.0, [950, 970, 990, 1000, 1010])
    assert sp is not None and sp <= 1000*(1-3/100)

def test_short_call_never_atm():
    sc = select_short_call(1000, 3.0, [990, 1000, 1010, 1030, 1050])
    assert sc is not None and sc >= 1000*(1+3/100)

def test_short_put_none_when_no_otm_strike():
    assert select_short_put(1000, 5.0, [960, 970, 980]) is None

def test_long_put_strictly_below_short():
    sp = select_short_put(1000, 3.0, [900,930,950,960,970])
    bp = select_long_put(sp, 1000, 3.0, 3.0, [900,930,950,960,970])
    assert bp is not None and bp < sp

def test_long_call_strictly_above_short():
    sc = select_short_call(1000, 3.0, [1030,1050,1070,1090,1100])
    bc = select_long_call(sc, 1000, 3.0, 3.0, [1030,1050,1070,1090,1100])
    assert bc is not None and bc > sc

# -- Trigger logic -------------------------------------------------------------

def test_intraday_put_trigger():
    pt, ct = 950*(1+2/100), 1050*(1-2/100)
    side, spot, typ = check_exit(1000, 1005, 968, 990, pt, ct)
    assert side == "PUT_SIDE" and abs(spot-pt)<1e-6 and typ == "INTRADAY_TRIGGER"

def test_gap_put_exit():
    pt, ct = 950*(1+2/100), 1050*(1-2/100)
    side, spot, typ = check_exit(940, 960, 938, 955, pt, ct)
    assert side == "PUT_SIDE" and abs(spot-940)<1e-6 and typ == "GAP_EXIT"

def test_intraday_call_trigger():
    pt, ct = 950*(1+2/100), 1050*(1-2/100)
    side, spot, typ = check_exit(1000, 1035, 995, 1020, pt, ct)
    assert side == "CALL_SIDE" and abs(spot-ct)<1e-6 and typ == "INTRADAY_TRIGGER"

def test_no_trigger():
    side, _, _ = check_exit(1000,1010,990,1000, 940.0, 1060.0)
    assert side == ""

# -- Margin + lots -------------------------------------------------------------

def test_margin_formula():
    assert abs(margin_per_lot(2000,500) - MARGIN_PCT*2000*500) < 1e-6

def test_lots_zero_when_insufficient():
    # 0.125*2000*500=125000 > 100000 -> 0 lots
    assert compute_lots(100_000, 2000, 500) == 0

def test_lots_nonzero():
    assert compute_lots(200_000, 2000, 500) == 1

def test_lots_no_force_one():
    assert compute_lots(1_000, 5000, 1000) == 0

# -- Black-Scholes -------------------------------------------------------------

def test_put_call_parity():
    S,K,T,r,sig = 1000,1000,30/365,0.0525,0.20
    assert abs((bs_price(S,K,T,r,sig,'CE') - bs_price(S,K,T,r,sig,'PE')) - (S - K*math.exp(-r*T))) < 0.01

def test_iv_roundtrip():
    S,K,T,r,iv0 = 1000,1000,30/365,0.0525,0.22
    p = bs_price(S,K,T,r,iv0,'CE')
    iv, flag = solve_iv(p, S, K, T, r, 'CE')
    assert iv is not None and abs(iv-iv0) < 0.001
