from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from live.condor_runner import CondorRunner


def select(expiries, min_dte=20, max_dte=45):
    instruments = [
        {"name": "WAAREEENER", "instrument_type": "CE", "expiry": expiry}
        for expiry in expiries
    ]
    runner = object.__new__(CondorRunner)
    runner._broker = SimpleNamespace(_load_instruments=lambda: instruments)
    with patch("live.condor_runner.datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 29)
        return runner._find_expiry("WAAREEENER", min_dte, max_dte)


def test_holiday_shifted_monthly_expiry():
    assert select(["2026-11-23", "2026-12-29"]) == date(2026, 11, 23)


def test_weekly_is_not_selected_when_monthly_outside_dte():
    assert select(["2026-11-17", "2026-11-24"], 10, 20) is None


def test_nearest_monthly_uses_all_listed_expiries():
    assert select(["2026-11-17", "2026-11-23", "2026-12-29"]) == date(2026, 11, 23)


def test_dte_limits_still_apply():
    assert select(["2026-12-29"]) is None
