"""Scheduler core — load the schedule, find what's due, run it under PARALLEL=1.

A tick is one short-lived pass (launchd fires it every few minutes): evaluate
each enabled job's cadence against its last attempt, run the due ones *one at a
time*, route every model-bearing job that doesn't lock itself through the shared
model slot, record outcomes to a timing ledger, and park failures. Nothing
overlaps within a tick, and a single-instance flock keeps two ticks from
overlapping -- so "two model-bearing jobs in one window run sequentially" holds
by construction, not by cron luck.

Reuse, not reinvention: `run_task` mirrors Shadowfax's subprocess+timeout shape;
`order_tasks` mirrors its priority/depends_on ordering; consecutive-failure
suspension mirrors its MAX_CONSECUTIVE_FAILURES backoff. The new parts are the
intra-day cadence (`cadence.is_due`), the shared model lock (`model_lock`), and
the parked queue. External paths resolve through `config.paths` (U13).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from config import paths

from . import parked_queue
from .cadence import is_due
from .model_lock import SHARED_MODEL_LOCK, model_slot

# spine/scheduler/core.py -> spine/scheduler -> spine -> <monorepo root>
_PKG_DIR = Path(__file__).resolve().parent
_MONOREPO_ROOT = _PKG_DIR.parents[1]

SCHEDULE_PATH = _PKG_DIR / "schedule.yaml"
STATE_DIR = _PKG_DIR / "state"
LEDGER_PATH = STATE_DIR / "run-ledger.jsonl"
HEARTBEAT_PATH = STATE_DIR / "heartbeat.json"

MAX_CONSECUTIVE_FAILURES = 3
_FAILURE_STATES = ("failed", "timeout", "error")


# ---------------------------------------------------------------------------
# Schedule loading + command resolution
# ---------------------------------------------------------------------------

def load_schedule(path: Path | None = None) -> dict:
    with open(path or SCHEDULE_PATH) as f:
        return yaml.safe_load(f)


def resolve_command(command: str) -> str:
    """Expand the scheduler's path template vars. All anchors come from U13's
    `config.paths` (env-overridable) or the monorepo layout -- never hardcoded."""
    return (
        command
        .replace("{hermes_scripts}", str(paths.hermes_scripts()))
        .replace("{build_arm}", str(_MONOREPO_ROOT / "build-arm"))
        .replace("{project_root}", str(_MONOREPO_ROOT))
    )


def _subprocess_env() -> dict:
    """Inherit the environment but guarantee the wrapper scripts' bare `python3`
    resolves to the SAME interpreter running the scheduler (which has the repo
    deps). Critical under launchd: its PATH (/opt/homebrew/bin:...) resolves
    `python3` to a deps-less interpreter, so `python3 -m tools.X` would die with
    ModuleNotFoundError. Prepend the scheduler interpreter's own dir (and the
    monorepo venv if one ever exists) so the right python wins."""
    env = dict(os.environ)
    prepend = [str(Path(sys.executable).resolve().parent)]
    venv_bin = _MONOREPO_ROOT / ".venv" / "bin"
    if venv_bin.exists():
        prepend.insert(0, str(venv_bin))
    env["PATH"] = os.pathsep.join([*prepend, env.get("PATH", "")])
    return env


# ---------------------------------------------------------------------------
# Timing ledger (append-only JSONL) -- last-attempt drives cadence
# ---------------------------------------------------------------------------

def append_run(job_id: str, status: str, duration_s: float, now: datetime,
               *, ledger_path: Path = LEDGER_PATH) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": now.astimezone(timezone.utc).isoformat(),
        "job": job_id,
        "status": status,
        "duration_s": round(duration_s, 1),
    }
    with open(ledger_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_ledger(ledger_path: Path) -> list[dict]:
    if not ledger_path.exists():
        return []
    out = []
    for line in open(ledger_path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def last_run(job_id: str, *, ledger_path: Path = LEDGER_PATH) -> datetime | None:
    """Timestamp of the most recent ATTEMPT (any outcome), aware UTC, or None."""
    stamp = None
    for e in _read_ledger(ledger_path):
        if e.get("job") == job_id and e.get("ts"):
            stamp = e["ts"]
    return datetime.fromisoformat(stamp) if stamp else None


def consecutive_failures(job_id: str, *, ledger_path: Path = LEDGER_PATH) -> int:
    """Trailing run of failure outcomes; broken by an ok OR a suspended record."""
    count = 0
    for e in reversed(_read_ledger(ledger_path)):
        if e.get("job") != job_id:
            continue
        if e.get("status") in _FAILURE_STATES:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# Ordering + execution
# ---------------------------------------------------------------------------

def order_tasks(tasks: list[dict]) -> list[dict]:
    """Priority ASC, then depends_on after dependencies (mirrors Shadowfax)."""
    ordered_by_pri = sorted(tasks, key=lambda t: (t.get("priority", 1), t.get("id", "")))
    ordered: list[dict] = []
    placed: set[str] = set()
    remaining = list(ordered_by_pri)
    for _ in range(len(remaining) * 2):
        if not remaining:
            break
        for task in list(remaining):
            if all(d in placed for d in task.get("depends_on", [])):
                ordered.append(task)
                placed.add(task["id"])
                remaining.remove(task)
    ordered.extend(remaining)  # circular deps: append rather than drop
    return ordered


def _kill_group(pgid: int) -> None:
    """SIGKILL a whole process group, tolerating an already-dead group."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_task(task: dict) -> dict:
    """Run one job via subprocess with a hard timeout.

    Uses Popen + start_new_session so a timeout kills the WHOLE process group,
    not just the immediate shell -- otherwise a shell-launched `python -m tools.X`
    (a model-bearing job) could outlive the timeout and keep the VRAM slot after
    the scheduler released the lock, breaking PARALLEL=1. Mirrors the reaping
    pattern in tools/think_sweep.py.
    """
    command = resolve_command(task["command"])
    timeout_s = task.get("timeout_minutes", 10) * 60
    start = time.time()
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=str(_MONOREPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_subprocess_env(), start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001 -- fail-loud: any launch error parks
        return {"id": task["id"], "status": "error", "duration_s": round(time.time() - start, 1),
                "error": str(e)}
    pgid = proc.pid  # start_new_session makes the child its own group leader
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        elapsed = time.time() - start
        if proc.returncode == 0:
            return {"id": task["id"], "status": "ok", "duration_s": round(elapsed, 1),
                    "returncode": 0, "stdout_tail": (stdout or "")[-500:]}
        return {"id": task["id"], "status": "failed", "duration_s": round(elapsed, 1),
                "returncode": proc.returncode, "stderr_tail": (stderr or "")[-500:]}
    except subprocess.TimeoutExpired:
        _kill_group(pgid)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 -- best-effort drain after the kill
            pass
        return {"id": task["id"], "status": "timeout", "duration_s": round(time.time() - start, 1),
                "timeout_minutes": task.get("timeout_minutes", 10)}
    finally:
        _kill_group(pgid)  # defense-in-depth: no group survives the call


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def write_heartbeat(summary: dict, *, heartbeat_path: Path = HEARTBEAT_PATH) -> None:
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))


def read_heartbeat(*, heartbeat_path: Path = HEARTBEAT_PATH) -> dict | None:
    if not heartbeat_path.exists():
        return None
    try:
        return json.loads(heartbeat_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

def evaluate_due(schedule: dict, now: datetime, *,
                 ledger_path: Path = LEDGER_PATH) -> tuple[list[dict], list[dict]]:
    """Return (ordered due tasks, config errors). Never raises on a bad task.

    A malformed or missing cadence on an ENABLED task is a config error -- it is
    surfaced (so `tick` can park it, fail-loud) rather than crashing the whole
    tick or being silently skipped. One bad entry must not silence the others.
    """
    due: list[dict] = []
    errors: list[dict] = []
    for task in schedule.get("tasks", []):
        if not task.get("enabled", True):
            continue
        tid = task.get("id", "<no-id>")
        cadence = task.get("cadence")
        if not cadence:
            errors.append({"id": tid, "detail": "enabled task has no cadence"})
            continue
        try:
            if is_due(cadence, now, last_run(tid, ledger_path=ledger_path)):
                due.append(task)
        except ValueError as e:
            errors.append({"id": tid, "detail": f"bad cadence {cadence!r}: {e}"})
    return order_tasks(due), errors


def select_due(schedule: dict, now: datetime, *, ledger_path: Path = LEDGER_PATH) -> list[dict]:
    """Just the ordered due tasks (config errors surfaced via evaluate_due/tick)."""
    return evaluate_due(schedule, now, ledger_path=ledger_path)[0]


def tick(schedule: dict, now: datetime, *,
         ledger_path: Path = LEDGER_PATH,
         heartbeat_path: Path = HEARTBEAT_PATH,
         parked_dir: Path | None = None,
         lock_path: Path = SHARED_MODEL_LOCK,
         runner=run_task) -> dict:
    """Run all due jobs once, sequentially, honoring PARALLEL=1. Returns a summary.

    Model-bearing jobs that don't lock themselves run inside the shared model
    slot; if another holder (a build, QA, a manual swap) has it, the job is
    deferred to the next tick rather than co-residing. Self-locking jobs (e.g.
    aux-watchdog) run directly so they can take the slot themselves -- the
    scheduler must not double-hold it and deadlock them out.
    """
    summary = {"tick_at": now.astimezone(timezone.utc).isoformat(),
               "ran": [], "deferred": [], "suspended": [], "config_errors": [], "parked": []}

    due, config_errors = evaluate_due(schedule, now, ledger_path=ledger_path)

    # Fail loud on bad config: park it (the tick keeps running the good jobs).
    for err in config_errors:
        p = parked_queue.park(err["id"], "config-error", err["detail"],
                              parked_dir=parked_dir, now=now)
        summary["config_errors"].append(err["id"])
        summary["parked"].append(p.name)

    for task in due:
        job_id = task["id"]

        # Backoff: suspend after a streak; park once on the transition.
        if consecutive_failures(job_id, ledger_path=ledger_path) >= MAX_CONSECUTIVE_FAILURES:
            p = parked_queue.park(job_id, "suspended",
                                  f"{MAX_CONSECUTIVE_FAILURES}+ consecutive failures",
                                  parked_dir=parked_dir, now=now)
            append_run(job_id, "suspended", 0, now, ledger_path=ledger_path)
            summary["suspended"].append(job_id)
            summary["parked"].append(p.name)
            continue

        needs_lock = bool(task.get("model")) and not task.get("self_locks", False)

        if needs_lock:
            with model_slot(lock_path) as got:
                if not got:
                    summary["deferred"].append(job_id)  # retry next tick; no ledger write
                    continue
                result = runner(task)
        else:
            result = runner(task)

        append_run(job_id, result["status"], result.get("duration_s", 0), now,
                   ledger_path=ledger_path)
        summary["ran"].append({"id": job_id, "status": result["status"]})
        if result["status"] in _FAILURE_STATES:
            detail = result.get("stderr_tail") or result.get("error") or \
                f"returncode={result.get('returncode')}"
            p = parked_queue.park(job_id, result["status"], detail,
                                  parked_dir=parked_dir, now=now)
            summary["parked"].append(p.name)

    write_heartbeat(summary, heartbeat_path=heartbeat_path)
    return summary
