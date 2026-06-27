"""Orchestrating sweep for the ATB improvement feeder — the daily entry-point.

Runs Tier-1 (deterministic harvest) -> if signals, Tier-2 (35B suppress+cluster) -> if survivors
cleanly clustered, Tier-3 (Codex draft), under an outer lock, with scratch lifecycle, writing only
sweep-owned artifacts. Mirrors ``think_sweep.main`` (outer flock + scratch mint + cleanup).

Short-circuits (Korab's "don't pass to Codex every time"): an empty day stops after Tier-1; an
all-known day stops after Tier-2; a Tier-2 fail-closed escalates to the overseer WITHOUT calling
Tier-3. The feeder never auto-writes ``IMPROVEMENT_LOG.md`` — Tier-3 drafts candidates the overseer
+ Korab certify.

Exit codes: 0 = clean / nothing to review (no-activity, all-known, busy-skip); 2 = noteworthy output
for review (candidates drafted, or a dry-run Tier-1 digest); 1 = a tier failed-closed/safe
(escalation; survivors preserved) or an unexpected error.

Design: ``docs/plans/2026-06-18-002-improvement-feeder-PLAN.md`` (U5).
"""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tools.decompose_events import read_events
from tools.improvement_codex import run_tier3
from tools.improvement_dreamer import (
    CERTIFIED_PATH,
    SEEN_PATH,
    SYSTEM_MAP,
    IMPROVEMENT_LOG,
    load_signatures,
    run_tier2,
    save_signatures,
)
from tools.improvement_harvest import emit_event, harvest_window, write_digest
from tools.think_sweep import default_spawn

ATB_ROOT = Path(__file__).resolve().parent.parent
EVENTS = ATB_ROOT / "state" / "events.jsonl"
DIGEST_DIR = ATB_ROOT / "state" / "improvement-digests"
SCRATCH_ROOT = ATB_ROOT / "state" / "think-tmp"
LOCK_PATH = ATB_ROOT / "state" / ".improvement-sweep.lock"


class SweepBusy(Exception):
    """Raised when another improvement sweep already holds the outer lock."""


@contextmanager
def _sweep_lock(lockfile: Path):
    """Non-blocking OUTER lock: REFUSE (don't queue) if another improvement sweep holds it — a
    daily cron shouldn't pile up redundant runs. (The model-lock inside Tier-2's spawn stays the
    blocking think_sweep._flock on state/.model.lock, serializing model work across the stack.)"""
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lockfile), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SweepBusy() from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _mint_scratch(scratch_root: Path, date: str) -> Path:
    scratch_root = Path(scratch_root)
    scratch_root.mkdir(parents=True, exist_ok=True)
    p = scratch_root / f"improvement-sweep-{date}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cleanup_scratch(scratch: Path, scratch_root: Path) -> None:
    """Drop the per-run scratch on a clean cycle; guarded like think_sweep._cleanup_scratch — only
    ever removes an ``improvement-sweep-*`` dir directly under scratch_root, error-suppressed."""
    scratch = Path(scratch).resolve()
    root = Path(scratch_root).resolve()
    if scratch.parent == root and scratch.name.startswith("improvement-sweep-"):
        shutil.rmtree(scratch, ignore_errors=True)


def _append_seen(signals: list[dict], seen_path: Path) -> None:
    """Record this run's harvested signal_ids so an immediate re-run treats them as known (runtime
    same-day dedup). Recall-safe: only ADDS ids; corruption loads empty. Called ONLY on a clean
    cycle — a failed run leaves the set untouched so a fix-and-rerun re-surfaces everything."""
    ids = {s.get("signal_id") for s in signals if s.get("signal_id")}
    if not ids:
        return
    save_signatures(seen_path, load_signatures(seen_path) | ids)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _chain(
    date, *, window_days, scratch_root, digest_dir, events_path, seen_path, certified_path, log_path,
    system_map_path, spawn, codex_runner, emit, dry_run,
) -> int:
    # --- Tier-1: deterministic windowed harvest --------------------------------------------
    result = harvest_window(read_events(events_path), date, window_days)
    # Use the CLAMPED window length from the result (harvest_window floors window_days at 1) so the
    # stem + event never label a degenerate '-window-0d' (Codex U5 LOW).
    stem = f"{date}-window-{result.window_days}d"  # windowed artifact filename base
    if result.status == "no-activity":
        return 0  # full short-circuit: write nothing, exit silent
    write_digest(result, digest_dir, stem=stem)
    if emit:
        emit_event(
            {"event": "improvement-harvest", "date": date, "window_days": result.window_days,
             "start_date": result.start_date, "status": result.status,
             "signals": len(result.signals), "builds": len(result.build_ids)},
            events_path,
        )
    if dry_run:
        return 2  # deterministic preview: Tier-1 only, no model / no Codex

    scratch = _mint_scratch(scratch_root, date)

    # --- Tier-2: 35B suppress + cluster (annotate-only; the floor owns drops) ---------------
    t2 = run_tier2(
        result, scratch=scratch, spawn=spawn, seen_path=seen_path, certified_path=certified_path,
        log_path=log_path, system_map_path=system_map_path, digest_dir=digest_dir,
        events_path=events_path, stem=stem,
    )
    if t2.status == "all-known":
        _append_seen(result.signals, seen_path)
        _cleanup_scratch(scratch, scratch_root)
        return 0  # nothing new survived the floor -> no Codex
    if t2.status == "failed-closed":
        # escalate the raw survivor digest to the overseer; DO NOT append seen (allow re-run);
        # keep scratch for forensics (no cleanup). Tier-3 is NOT called.
        return 1

    # --- Tier-3: Codex comb + draft (only on cleanly clustered survivors). Thread the Tier-2
    #     surviving classes + the advisory 'already logged' set so Codex gets recurrence + overlap
    #     context (the count stays tool-rendered; the flag is advisory only). -----------------
    t3 = run_tier3(
        date, t2.survivors, classes=t2.surviving_classes,
        already_logged=t2.already_logged_class_ids, runner=codex_runner,
        digest_dir=digest_dir, events_path=events_path, stem=stem,
    )
    if t3.status == "failed-safe":
        return 1  # escalate; do not append seen; keep scratch

    # clean terminal: candidates drafted (or none worth drafting). Record + clean up.
    _append_seen(result.signals, seen_path)
    _cleanup_scratch(scratch, scratch_root)
    return 2


def run(
    date: str | None = None,
    *,
    window_days: int = 14,
    scratch_root: Path = SCRATCH_ROOT,
    digest_dir: Path = DIGEST_DIR,
    events_path: Path = EVENTS,
    seen_path: Path = SEEN_PATH,
    certified_path: Path = CERTIFIED_PATH,
    log_path: Path = IMPROVEMENT_LOG,
    system_map_path: Path = SYSTEM_MAP,
    lock_path: Path = LOCK_PATH,
    spawn=default_spawn,
    codex_runner=None,
    emit: bool = False,
    dry_run: bool = False,
) -> int:
    if date is None:
        date = _today()
    try:
        with _sweep_lock(lock_path):
            return _chain(
                date, window_days=window_days, scratch_root=scratch_root, digest_dir=digest_dir,
                events_path=events_path, seen_path=seen_path, certified_path=certified_path,
                log_path=log_path, system_map_path=system_map_path, spawn=spawn,
                codex_runner=codex_runner, emit=emit, dry_run=dry_run,
            )
    except SweepBusy:
        print("another improvement sweep is running; skipping", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 — a sweep fault surfaces as exit 1, never a trace
        print(f"[improvement-sweep] error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ATB improvement feeder — windowed orchestrating sweep.")
    ap.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                    help="window END day (UTC); default today. The wrapper pins the prior complete UTC day.")
    ap.add_argument("--window-days", type=int, default=14,
                    help="rolling window length in complete UTC days (default 14)")
    ap.add_argument("--emit-event", action="store_true", help="append feeder events to state/events.jsonl")
    ap.add_argument(
        "--dry-run", action="store_true",
        help="deterministic preview: Tier-1 harvest only (no model spawn, no Codex)",
    )
    args = ap.parse_args(argv)
    return run(args.date, window_days=args.window_days, emit=args.emit_event, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
