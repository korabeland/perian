"""Held / parked reviewable queue — the scheduler's fail-loud sink.

A gateway cron that failed told someone via Telegram and then evaporated; there
was no durable, reviewable record of "this periodic job is unhealthy." U3's
contract is fail-loud *into a place the alert can land* (the plan's "minimal
operator path"): every timed-out / failed / errored job -- and a missed
scheduler heartbeat -- writes a small JSON entry here. `scheduler status` and
`scheduler parked` read it; `scheduler resolve <id>` clears one once handled.

This is deliberately a flat directory of append-only JSON files, not a database:
trivially inspectable, crash-safe (one file per event), and easy for a later
notifier (Telegram forwarder, U4 governor) to tail. Full operator tooling stays
deferred per the plan.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# spine/scheduler/parked_queue.py -> spine/scheduler
_PKG_DIR = Path(__file__).resolve().parent
DEFAULT_PARKED_DIR = _PKG_DIR / "state" / "parked"


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in text)[:60]


def park(
    job_id: str,
    status: str,
    detail: str = "",
    *,
    parked_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Write one parked entry and return its path.

    `status` is the failure kind (failed / timeout / error / missed-heartbeat /
    config-error). `now` is injectable for deterministic tests.
    """
    directory = Path(parked_dir) if parked_dir is not None else DEFAULT_PARKED_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entry_id = f"{stamp.strftime('%Y%m%dT%H%M%S')}-{_slug(job_id)}"
    entry = {
        "id": entry_id,
        "job_id": job_id,
        "status": status,
        "detail": detail[:2000],
        "parked_at": stamp.isoformat(),
        "resolved": False,
    }
    path = directory / f"{entry_id}.json"
    # Disambiguate the rare same-second collision rather than overwrite.
    suffix = 1
    while path.exists():
        path = directory / f"{entry_id}-{suffix}.json"
        entry["id"] = f"{entry_id}-{suffix}"
        suffix += 1
    path.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
    return path


def list_parked(*, parked_dir: Path | None = None, include_resolved: bool = False) -> list[dict]:
    """Return parked entries (unresolved by default), oldest first."""
    directory = Path(parked_dir) if parked_dir is not None else DEFAULT_PARKED_DIR
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.glob("*.json")):
        try:
            entry = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if entry.get("resolved") and not include_resolved:
            continue
        entries.append(entry)
    return entries


def resolve(entry_id: str, *, parked_dir: Path | None = None) -> bool:
    """Mark a parked entry resolved. Return True if found and updated."""
    directory = Path(parked_dir) if parked_dir is not None else DEFAULT_PARKED_DIR
    path = directory / f"{entry_id}.json"
    if not path.exists():
        return False
    entry = json.loads(path.read_text())
    entry["resolved"] = True
    entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
    return True
