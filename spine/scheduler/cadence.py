"""Cadence vocabulary + due-evaluation for the spine periodic scheduler.

Shadowfax's scheduler answers "does this run TONIGHT?" (daily / weekly:DAY /
biweekly) and then runs it once inside a nightly window. ATB's gateway crons run
*through the day* -- aux-watchdog and stall-alert every 30 minutes, brain-sync
every 6 hours -- which that model cannot express. This module adds the missing
intra-day cadences while keeping the time-of-day ones, in an explicit, testable
vocabulary (full cron parsing is deliberately out of scope -- these six forms
cover every migrated job exactly; see schedule.yaml):

  every:<N>m | every:<N>h   interval since the last attempt (aux-watchdog 30m,
                            stall-alert 30m, brain-sync 6h)
  daily@HH:MM               once a day at local HH:MM (dream 03:00)
  weekly:DAY@HH:MM          once a week on DAY at local HH:MM (brain-digest
                            SAT 08:00, improvement-feeder MON 11:00)

`is_due` is pure: it takes `now` and the job's `last_run` (both timezone-aware)
and returns a bool, so the scheduler tick stays trivial and the whole cadence
surface is unit-testable without a clock. A malformed cadence raises ValueError
-- the scheduler treats that as a loud config error (parked), never a silent skip.
"""

from __future__ import annotations

from datetime import datetime, timedelta

_DAYS = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
}


def _parse_hhmm(text: str) -> tuple[int, int]:
    """Parse 'HH:MM' (24h) into (hour, minute); raise ValueError if invalid."""
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"bad time-of-day {text!r} (want HH:MM)")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time-of-day out of range: {text!r}")
    return hour, minute


def parse_interval(spec: str) -> timedelta:
    """Parse the value part of 'every:<N>m' / 'every:<N>h' into a timedelta."""
    spec = spec.strip()
    if not spec or spec[-1] not in ("m", "h"):
        raise ValueError(f"bad interval {spec!r} (want <N>m or <N>h)")
    value = int(spec[:-1])
    if value <= 0:
        raise ValueError(f"interval must be positive: {spec!r}")
    return timedelta(minutes=value) if spec[-1] == "m" else timedelta(hours=value)


def is_due(cadence: str, now: datetime, last_run: datetime | None) -> bool:
    """Return True if a job with `cadence` is due at `now` given its `last_run`.

    `now` and `last_run` must be timezone-aware. Time-of-day cadences are
    evaluated in `now`'s local timezone (the gateway crons fired in local time);
    `last_run` is converted into that zone for the comparison so the
    "already ran this occurrence" guard is exact across a stored-UTC ledger.
    """
    cadence = cadence.strip()

    # --- interval: every:<N>m | every:<N>h ---
    if cadence.startswith("every:"):
        interval = parse_interval(cadence[len("every:"):])
        if last_run is None:
            return True
        return (now - last_run) >= interval

    # --- daily@HH:MM ---
    if cadence.startswith("daily@"):
        hour, minute = _parse_hhmm(cadence[len("daily@"):])
        occurrence = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < occurrence:
            return False
        if last_run is None:
            return True
        return last_run.astimezone(now.tzinfo) < occurrence

    # --- weekly:DAY@HH:MM ---
    if cadence.startswith("weekly:"):
        body = cadence[len("weekly:"):]
        if "@" not in body:
            raise ValueError(f"weekly cadence needs DAY@HH:MM: {cadence!r}")
        day_name, hhmm = body.split("@", 1)
        day = _DAYS.get(day_name.upper())
        if day is None:
            raise ValueError(f"unknown weekday {day_name!r} in {cadence!r}")
        hour, minute = _parse_hhmm(hhmm)
        if now.weekday() != day:
            return False
        occurrence = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < occurrence:
            return False
        if last_run is None:
            return True
        return last_run.astimezone(now.tzinfo) < occurrence

    raise ValueError(f"unrecognized cadence: {cadence!r}")
