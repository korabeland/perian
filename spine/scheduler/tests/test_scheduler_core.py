"""Scheduler core — ordering, due-selection, and the tick's run/defer/park/suspend.

Integration-level: every tick path is exercised with an injected runner (no real
subprocess) and tmp ledger/heartbeat/parked/lock paths, so the trust behaviors
(PARALLEL=1, no-deadlock for self-locking jobs, fail-loud parking, backoff) are
asserted deterministically.
"""

import fcntl
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scheduler import core, parked_queue


def test_subprocess_env_prepends_interpreter_dir():
    """Wrapper scripts call bare `python3`; under launchd that resolves to a
    deps-less interpreter. The scheduler must prepend its own interpreter dir so
    `python3 -m tools.X` uses the python that has the repo deps."""
    interp_dir = str(Path(sys.executable).resolve().parent)
    paths = core._subprocess_env()["PATH"].split(os.pathsep)
    assert interp_dir in paths[:2]  # first (or second, behind a venv) on PATH
    if "/usr/bin" in paths:
        assert paths.index(interp_dir) < paths.index("/usr/bin")


def _utc(h, mi, *, d=25):
    return datetime(2026, 6, d, h, mi, tzinfo=timezone.utc)


def _paths(tmp_path):
    return dict(
        ledger_path=tmp_path / "ledger.jsonl",
        heartbeat_path=tmp_path / "heartbeat.json",
        parked_dir=tmp_path / "parked",
        lock_path=tmp_path / ".model.lock",
    )


def _recording_runner(calls, status="ok"):
    def run(task):
        calls.append(task["id"])
        result = {"id": task["id"], "status": status, "duration_s": 0.1}
        if status in core._FAILURE_STATES:
            result["stderr_tail"] = "boom"
        return result
    return run


# --- ordering -----------------------------------------------------------------

def test_order_tasks_priority_then_deps():
    tasks = [
        {"id": "b", "priority": 1},
        {"id": "a", "priority": 0},
        {"id": "c", "priority": 0, "depends_on": ["a"]},
    ]
    order = [t["id"] for t in core.order_tasks(tasks)]
    assert order.index("a") < order.index("c")  # dependency before dependent
    assert order.index("a") < order.index("b")  # priority 0 before priority 1


# --- due selection ------------------------------------------------------------

def test_select_due_interval_respects_last_run(tmp_path):
    p = _paths(tmp_path)
    sched = {"tasks": [{"id": "j", "cadence": "every:30m", "enabled": True}]}
    # never run -> due
    assert [t["id"] for t in core.select_due(sched, _utc(12, 0), ledger_path=p["ledger_path"])] == ["j"]
    # ran 10m ago -> not due
    core.append_run("j", "ok", 1, _utc(11, 50), ledger_path=p["ledger_path"])
    assert core.select_due(sched, _utc(12, 0), ledger_path=p["ledger_path"]) == []


def test_select_due_skips_disabled(tmp_path):
    p = _paths(tmp_path)
    sched = {"tasks": [{"id": "j", "cadence": "every:30m", "enabled": False}]}
    assert core.select_due(sched, _utc(12, 0), ledger_path=p["ledger_path"]) == []


# --- the tick -----------------------------------------------------------------

def test_tick_happy_records_and_writes_heartbeat(tmp_path):
    p = _paths(tmp_path)
    calls = []
    sched = {"tasks": [{"id": "stall-alert", "cadence": "every:30m", "model": None, "enabled": True}]}
    summary = core.tick(sched, _utc(12, 0), runner=_recording_runner(calls), **p)
    assert calls == ["stall-alert"]
    assert summary["ran"] == [{"id": "stall-alert", "status": "ok"}]
    assert p["heartbeat_path"].exists()
    assert core.last_run("stall-alert", ledger_path=p["ledger_path"]) is not None


def test_tick_failure_parks(tmp_path):
    p = _paths(tmp_path)
    calls = []
    sched = {"tasks": [{"id": "brain-sync", "cadence": "every:30m", "model": None, "enabled": True}]}
    summary = core.tick(sched, _utc(12, 0), runner=_recording_runner(calls, "failed"), **p)
    assert summary["parked"]  # one entry
    parked = parked_queue.list_parked(parked_dir=p["parked_dir"])
    assert len(parked) == 1 and parked[0]["job_id"] == "brain-sync"


def test_tick_defers_non_self_locking_model_job_when_slot_held(tmp_path):
    """A model job that does NOT self-lock is routed through the slot; if a holder
    has it, the job defers to the next tick rather than co-residing."""
    p = _paths(tmp_path)
    calls = []
    sched = {"tasks": [{"id": "generic-model-job", "cadence": "every:30m",
                        "model": "some-model", "self_locks": False, "enabled": True}]}
    # External holder takes the shared slot.
    fd = os.open(p["lock_path"], os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        summary = core.tick(sched, _utc(12, 0), runner=_recording_runner(calls), **p)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert calls == []  # never ran
    assert summary["deferred"] == ["generic-model-job"]
    # deferred -> NOT recorded, so it stays due for the next tick
    assert core.last_run("generic-model-job", ledger_path=p["ledger_path"]) is None


def test_tick_parks_config_error_without_crashing_good_jobs(tmp_path):
    """A bad/missing cadence on one enabled job parks a config-error and the
    tick still runs the healthy jobs (no whole-tick crash)."""
    p = _paths(tmp_path)
    calls = []
    sched = {"tasks": [
        {"id": "broken", "cadence": "every:zzz", "model": None, "enabled": True},
        {"id": "healthy", "cadence": "every:30m", "model": None, "enabled": True},
    ]}
    summary = core.tick(sched, _utc(12, 0), runner=_recording_runner(calls), **p)
    assert calls == ["healthy"]
    assert summary["config_errors"] == ["broken"]
    parked = parked_queue.list_parked(parked_dir=p["parked_dir"])
    assert any(e["job_id"] == "broken" and e["status"] == "config-error" for e in parked)


def test_real_schedule_model_jobs_are_self_locking():
    """Guard: every model-bearing job in the shipped schedule.yaml self-locks, or
    the scheduler would pre-hold state/.model.lock and deadlock it (Codex U3)."""
    schedule = core.load_schedule()  # the real spine/scheduler/schedule.yaml
    model_jobs = [t for t in schedule["tasks"] if t.get("model")]
    assert {t["id"] for t in model_jobs} == {"aux-watchdog", "improvement-feeder"}
    for t in model_jobs:
        assert t.get("self_locks") is True, f"{t['id']} must self_locks"


def test_tick_runs_self_locking_job_even_when_slot_held(tmp_path):
    """aux-watchdog takes MODEL_LOCK itself; the scheduler must NOT pre-hold it,
    or the job deadlocks out. So a self_locks job runs regardless of the slot."""
    p = _paths(tmp_path)
    calls = []
    sched = {"tasks": [{"id": "aux-watchdog", "cadence": "every:30m",
                        "model": "qwen3.5-9b", "self_locks": True, "enabled": True}]}
    fd = os.open(p["lock_path"], os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        core.tick(sched, _utc(12, 0), runner=_recording_runner(calls), **p)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert calls == ["aux-watchdog"]  # ran despite the slot being held


def test_tick_runs_model_jobs_sequentially(tmp_path):
    p = _paths(tmp_path)
    calls = []
    sched = {"tasks": [
        {"id": "later", "cadence": "every:30m", "model": "m", "priority": 1, "enabled": True},
        {"id": "first", "cadence": "every:30m", "model": "m", "priority": 0, "enabled": True},
    ]}
    core.tick(sched, _utc(12, 0), runner=_recording_runner(calls), **p)
    assert calls == ["first", "later"]  # priority order, one at a time


def test_tick_suspends_after_consecutive_failures(tmp_path):
    p = _paths(tmp_path)
    for mins_ago in (40, 35, 31):
        core.append_run("brain-sync", "failed", 1, _utc(12, 0) - timedelta(minutes=mins_ago),
                        ledger_path=p["ledger_path"])
    calls = []
    sched = {"tasks": [{"id": "brain-sync", "cadence": "every:30m", "model": None, "enabled": True}]}
    summary = core.tick(sched, _utc(12, 0), runner=_recording_runner(calls), **p)
    assert calls == []  # suspended, not run
    assert summary["suspended"] == ["brain-sync"]
    assert summary["parked"]  # alerted into the queue
