"""Cadence evaluation — intervals, time-of-day, weekly, and malformed input."""

from datetime import datetime, timedelta, timezone

import pytest

from scheduler.cadence import is_due, parse_interval


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# --- interval (every:Nm / every:Nh) -------------------------------------------

def test_interval_due_when_never_run():
    assert is_due("every:30m", _utc(2026, 6, 25, 12, 0), None) is True


def test_interval_due_after_elapsed():
    now = _utc(2026, 6, 25, 12, 0)
    assert is_due("every:30m", now, now - timedelta(minutes=31)) is True


def test_interval_not_due_before_elapsed():
    now = _utc(2026, 6, 25, 12, 0)
    assert is_due("every:30m", now, now - timedelta(minutes=10)) is False


def test_interval_hours():
    now = _utc(2026, 6, 25, 12, 0)
    assert is_due("every:6h", now, now - timedelta(hours=6, minutes=1)) is True
    assert is_due("every:6h", now, now - timedelta(hours=5)) is False


def test_parse_interval_units():
    assert parse_interval("30m") == timedelta(minutes=30)
    assert parse_interval("6h") == timedelta(hours=6)


# --- daily@HH:MM --------------------------------------------------------------

def test_daily_due_after_time_not_run_today():
    now = _utc(2026, 6, 25, 3, 5)
    assert is_due("daily@03:00", now, _utc(2026, 6, 24, 3, 0)) is True


def test_daily_not_due_before_time():
    now = _utc(2026, 6, 25, 2, 30)
    assert is_due("daily@03:00", now, _utc(2026, 6, 24, 3, 0)) is False


def test_daily_not_due_if_already_ran_today():
    now = _utc(2026, 6, 25, 5, 0)
    assert is_due("daily@03:00", now, _utc(2026, 6, 25, 3, 1)) is False


# --- weekly:DAY@HH:MM ---------------------------------------------------------

def test_weekly_due_on_matching_weekday():
    now = _utc(2026, 6, 25, 9, 0)
    day = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][now.weekday()]
    assert is_due(f"weekly:{day}@08:00", now, None) is True


def test_weekly_not_due_on_other_weekday():
    now = _utc(2026, 6, 25, 9, 0)
    other = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][(now.weekday() + 1) % 7]
    assert is_due(f"weekly:{other}@08:00", now, None) is False


def test_weekly_not_due_if_already_ran_this_occurrence():
    now = _utc(2026, 6, 25, 9, 0)
    day = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][now.weekday()]
    assert is_due(f"weekly:{day}@08:00", now, _utc(2026, 6, 25, 8, 1)) is False


# --- malformed → ValueError (fail loud, never silent skip) --------------------

@pytest.mark.parametrize("bad", [
    "every:0m", "every:m", "every:30x", "daily@25:00", "daily@noon",
    "weekly:FUNDAY@08:00", "weekly:MON", "nonsense", "",
])
def test_malformed_cadence_raises(bad):
    with pytest.raises(ValueError):
        is_due(bad, datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc), None)
