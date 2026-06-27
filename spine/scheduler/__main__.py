"""CLI for the spine periodic scheduler.

    python -m scheduler tick      run all due jobs once (what launchd fires)
    python -m scheduler plan      dry run: show what is due now
    python -m scheduler status    heartbeat freshness + last runs + parked count
    python -m scheduler parked    list unresolved parked entries
    python -m scheduler resolve <id>   clear a parked entry once handled

`tick` takes a single-instance flock so a long job can't be overlapped by the
next launchd fire (the model lock guards the slot; this guards the tick itself).
"""

from __future__ import annotations

import fcntl
import os
import sys
from datetime import datetime, timezone

from . import core, parked_queue

_INSTANCE_LOCK = core.STATE_DIR / "scheduler.tick.lock"


def _now() -> datetime:
    return datetime.now().astimezone()


def cmd_tick() -> int:
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(_INSTANCE_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("tick already running — skipping this fire")
            return 0
        summary = core.tick(core.load_schedule(), _now())
        ran = ", ".join(f"{r['id']}={r['status']}" for r in summary["ran"]) or "—"
        print(f"ran: {ran}")
        if summary["deferred"]:
            print(f"deferred (model slot held): {', '.join(summary['deferred'])}")
        if summary["suspended"]:
            print(f"suspended: {', '.join(summary['suspended'])}")
        if summary["parked"]:
            print(f"parked: {len(summary['parked'])} (see `scheduler parked`)")
        return 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def cmd_plan() -> int:
    now = _now()
    due = core.select_due(core.load_schedule(), now, ledger_path=core.LEDGER_PATH)
    print(f"Due now ({now.strftime('%a %Y-%m-%d %H:%M %Z')}):")
    if not due:
        print("  (nothing)")
        return 0
    for t in due:
        model = t.get("model") or "—"
        print(f"  P{t.get('priority', 1)} {t['id']:<20} {t['cadence']:<18} model={model}")
    return 0


def cmd_status() -> int:
    hb = core.read_heartbeat()
    if not hb:
        print("heartbeat: none yet (scheduler has not ticked)")
    else:
        tick_at = datetime.fromisoformat(hb["tick_at"])
        age_min = (datetime.now(timezone.utc) - tick_at).total_seconds() / 60
        flag = "  ⚠ STALE" if age_min > 15 else ""
        print(f"heartbeat: last tick {age_min:.0f}m ago{flag}")
    schedule = core.load_schedule()
    print("jobs:")
    for t in schedule.get("tasks", []):
        lr = core.last_run(t["id"])
        lr_str = lr.astimezone().strftime("%m-%d %H:%M") if lr else "never"
        state = "on " if t.get("enabled", True) else "off"
        print(f"  [{state}] {t['id']:<20} {t['cadence']:<18} last={lr_str}")
    parked = parked_queue.list_parked()
    print(f"parked (unresolved): {len(parked)}")
    for e in parked:
        print(f"  {e['id']}  {e['status']}  {e['detail'][:60]}")
    return 0


def cmd_parked() -> int:
    parked = parked_queue.list_parked()
    if not parked:
        print("no unresolved parked entries")
        return 0
    for e in parked:
        print(f"{e['id']}  job={e['job_id']}  status={e['status']}")
        print(f"    {e['detail'][:200]}")
    return 0


def cmd_resolve(entry_id: str) -> int:
    ok = parked_queue.resolve(entry_id)
    print(f"resolved {entry_id}" if ok else f"not found: {entry_id}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    command = args[0] if args else "tick"
    if command == "tick":
        return cmd_tick()
    if command == "plan":
        return cmd_plan()
    if command == "status":
        return cmd_status()
    if command == "parked":
        return cmd_parked()
    if command == "resolve":
        if len(args) < 2:
            print("usage: scheduler resolve <id>")
            return 1
        return cmd_resolve(args[1])
    print(f"unknown command: {command}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
