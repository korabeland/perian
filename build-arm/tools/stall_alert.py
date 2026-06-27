#!/usr/bin/env python3
"""Stall alert for the ATB build board (post-mortem F3 — the LOUD layer).

Correction to the post-mortem's framing: Hermes ALREADY reaps stuck workers. The
in-gateway dispatcher times out a card past its ``--max-runtime`` and AUTO-BLOCKS it
after ``--failure-limit`` consecutive timed_out/crashed/spawn_failed attempts (default
2). What is missing is the LOUD part: a blocked worker — or a held/errored gate — then
sits silently with nobody told. For an unattended line, fail-safe must also fail-loud
(post-mortem principle #2). This sweep is that missing alert.

It reports (to stdout, for a ``--deliver telegram`` cron) any NEW:
  - ``blocked`` cards on the board (a worker the dispatcher gave up on), and
  - recent ``gate-held`` / ``gate-error`` / ``verification-failed`` events in
    state/events.jsonl
that it has not already alerted on (file-based dedup, like the other ATB crons). It is
SILENT on stdout + exit 0 when nothing is new, so the cron only ever pings on a real
change. Read-only on the board (lists only; never blocks/reclaims/completes).

Usage:
  python -m tools.stall_alert [--board auto-build] [--state PATH] [--events PATH]
                              [--emit-event] [--json] [--self-test]

Exit: 0 = ran (alerted or nothing new) · 1 = sweep error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ATB_ROOT = Path(__file__).resolve().parent.parent
EVENTS = ATB_ROOT / "state" / "events.jsonl"
STATE = ATB_ROOT / "state" / "stall_alerted.json"
# The per-hold spam the coach loop set out to kill is removed AT THE SOURCE: the diff gate's
# Codex-needs_changes path no longer emits ``gate-held`` -- it COACHES the coder (tools.coach)
# and only emits the rare ``coach-exhausted`` when the cap is hit. So the ``gate-held`` events
# that remain are the ones that SHOULD be loud and are one-shot (deduped by the loop-guard sha):
# a scope safety violation and a plan-gate rejection. We therefore still watch ``gate-held``,
# and ADD ``coach-exhausted`` (the new escalation). A coach/re-dispatch failure surfaces as a
# watched ``gate-error``, never a silent hold (Codex review blocker #2).
STALL_EVENTS = ("gate-held", "coach-exhausted", "gate-error", "verification-failed")
EVENTS_TAIL = 300  # only scan the most recent events


def _kanban_json(board: str, *args: str):
    cp = subprocess.run(
        ["hermes", "kanban", "--board", board, *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ATB_ROOT),
    )
    if cp.returncode != 0:
        raise RuntimeError(f"`kanban {' '.join(args)}` failed: {cp.stderr.strip()}")
    return json.loads(cp.stdout or "[]")


def _as_list(data) -> list:
    return data if isinstance(data, list) else data.get("tasks", [])


def blocked_signatures(board: str) -> list[dict]:
    """Blocked cards = workers the dispatcher gave up on (failure-limit auto-block)."""
    items = _as_list(_kanban_json(board, "list", "--status", "blocked", "--json"))
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("id"):
            out.append(
                {
                    "sig": f"blocked:{it['id']}",
                    "kind": "blocked",
                    "id": it["id"],
                    "title": it.get("title", ""),
                    "assignee": it.get("assignee", ""),
                }
            )
    return out


def event_signatures(events_path: Path, tail: int = EVENTS_TAIL) -> list[dict]:
    """Recent held/error/verification-failed events from the gate sweep."""
    if not events_path.is_file():
        return []
    lines = events_path.read_text(errors="replace").splitlines()[-tail:]
    out = []
    for ln in lines:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("event") in STALL_EVENTS:
            out.append(
                {
                    "sig": f"{e.get('event')}:{e.get('job_id')}:{e.get('ts')}",
                    "kind": e.get("event"),
                    "id": e.get("job_id", ""),
                    "title": e.get("reason", ""),
                    "assignee": e.get("gate", ""),
                }
            )
    return out


def select_new(candidates: list[dict], already) -> list[dict]:
    """Pure: candidates whose `sig` is not already alerted. Deterministic + testable."""
    seen = set(already)
    new = []
    for c in candidates:
        if c["sig"] not in seen:
            new.append(c)
            seen.add(c["sig"])
    return new


def _load_state(path: Path) -> set:
    if path.is_file():
        try:
            return set(json.loads(path.read_text()))
        except (json.JSONDecodeError, ValueError):
            return set()
    return set()


def _save_state(path: Path, sigs) -> None:
    if not path.parent.is_dir():
        return
    path.write_text(json.dumps(sorted(sigs), indent=2) + "\n")


def emit(events_path: Path, payload: dict) -> None:
    if not events_path.parent.is_dir():
        return
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **payload}
    )
    with events_path.open("a") as fh:
        fh.write(line + "\n")


def _fmt(s: dict) -> str:
    if s["kind"] == "blocked":
        who = f" ({s['assignee']})" if s["assignee"] else ""
        return f"[stall] BLOCKED card {s['id']}{who}: {s['title']}".rstrip()
    return f"[stall] {s['kind']} {s['id']}: {s['title']}".rstrip()


def _self_test() -> int:
    fails = []
    cands = [
        {
            "sig": "blocked:1",
            "kind": "blocked",
            "id": "1",
            "title": "x",
            "assignee": "coder",
        },
        {
            "sig": "coach-exhausted:j:ts",
            "kind": "coach-exhausted",
            "id": "j",
            "title": "why",
            "assignee": "diff",
        },
    ]
    if [c["sig"] for c in select_new(cands, {"blocked:1"})] != ["coach-exhausted:j:ts"]:
        fails.append("select_new did not filter an already-alerted signature")
    if [c["sig"] for c in select_new(cands, set())] != ["blocked:1", "coach-exhausted:j:ts"]:
        fails.append("select_new should return all candidates when nothing is alerted")
    if select_new(cands, {"blocked:1", "coach-exhausted:j:ts"}) != []:
        fails.append("select_new should return none when everything is alerted")
    if _fmt(cands[0]) != "[stall] BLOCKED card 1 (coder): x":
        fails.append(f"_fmt blocked wrong: {_fmt(cands[0])!r}")
    if fails:
        print("stall_alert SELF-TEST FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("stall_alert self-test: OK (dedup new/all/none + format)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="stall_alert", description="ATB build-board stall alert (F3)."
    )
    ap.add_argument("--board", default="auto-build")
    ap.add_argument(
        "--state",
        default=str(STATE),
        help="dedup state file (default: state/stall_alerted.json)",
    )
    ap.add_argument("--events", default=str(EVENTS))
    ap.add_argument(
        "--emit-event",
        action="store_true",
        help="append a stall-alert event on a new stall",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    state_path = Path(args.state)
    events_path = Path(args.events)
    try:
        candidates = blocked_signatures(args.board) + event_signatures(events_path)
    except RuntimeError as exc:
        print(f"[stall] sweep error: {exc}", file=sys.stderr)
        return 1

    already = _load_state(state_path)
    new = select_new(candidates, already)

    if new:
        for s in new:
            print(_fmt(s))
        _save_state(state_path, already | {s["sig"] for s in new})
        if args.emit_event:
            emit(
                events_path,
                {
                    "job_id": "stall-alert",
                    "profile": "gate",
                    "event": "stall-alert",
                    "count": len(new),
                    "stalls": [s["sig"] for s in new],
                },
            )

    if args.json:
        print(json.dumps({"new": new, "checked": len(candidates)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
