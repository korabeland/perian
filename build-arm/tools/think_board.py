#!/usr/bin/env python3
"""Think-loop kanban board projector — a MIRROR, never a MOTOR (plan doc 2026-06-12-004 item 7).

WHAT THIS IS:
A small deterministic projector that maps the think-loop's on-disk state (events.jsonl,
state/tracks/, state/builds/, state/think/topics.txt) onto a SEPARATE hermes kanban board
(`think-loop`) so Korab gets the same UI as the auto-build board for idea visibility. It runs
at the end of a think sweep (best-effort) and as a standalone backfill CLI.

THE HARD RULE — mirror, not motor:
  - DISK IS THE SINGLE SOURCE OF TRUTH. The board is a read-only projection of it; board edits
    drive nothing, and the projector never reads board state back into the pipeline.
  - NO DISPATCHER EVER POINTS AT THIS BOARD. Cards are never left in a claimable state:
    idea cards are created with --initial-status running (never `ready`), and the only
    transitions used are block / complete / archive / schedule. The subcommand whitelist
    below makes the no-motor invariant code-enforced: _kanban() refuses dispatch/daemon/
    claim/promote/unblock/assign and anything else that could hand work to an agent.
  - BEST-EFFORT BY DESIGN: a board failure must never affect a sweep's exit code or any gate.
    project_best_effort() swallows every exception (stderr note only).

COLUMN MAPPING (kanban statuses are fixed; verified transition matrix 2026-06-12 —
`scheduled` is a dead end except archive, `triage` is a dead end entirely, so):
  scheduled  <- Queued: one card per topics.txt seed (permanent; never moves)
  running    <- in the loop: Researched (T1) / Proposed (T2, lane tag) / Under review (T3) —
                stage detail arrives as comments (kanban titles are immutable)
  blocked    <- Parked (dedup/cap/council/needs_more-exhausted) and Held (trust failure)
  done       <- Digest rendered — Korab's call
  archived   <- crossed into the build loop (state/builds/<job>/ exists)

Card identity: the job_id leads the title (titles are unique + stable); create also passes
--idempotency-key think:<id> as belt-and-suspenders (list --json does not expose the key, so
reconciliation matches on the title prefix). A tiny sidecar cache
(state/think/board-projection.json) remembers the last projected stage per job purely to
de-duplicate comments — it is a display cache, NEVER pipeline truth.

Model text (what_should_exist) appears in card titles for display only: newline-stripped,
truncated, and consumed by NO agent (no dispatcher on this board).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ATB_ROOT = Path(__file__).resolve().parent.parent
BOARD = "think-loop"

# The ONLY kanban verbs a mirror may use. Anything that could hand work to an agent
# (dispatch, daemon, claim, promote, unblock -> ready, assign, swarm...) is refused.
ALLOWED_VERBS = frozenset({"create", "list", "comment", "complete", "block", "schedule", "archive"})

# Think-loop stages, in pipeline order (later wins when events disagree).
STAGE_ORDER = ["researched", "proposed", "under-review", "parked", "held", "digest", "building"]
STAGE_STATUS = {
    "researched": "running",
    "proposed": "running",
    "under-review": "running",
    "parked": "blocked",
    "held": "blocked",
    "digest": "done",
    "building": "archived",
}


class KanbanError(RuntimeError):
    pass


def _kanban(*args: str) -> str:
    """Run one whitelisted kanban verb against the think-loop board. Raises KanbanError on
    any failure (callers inside the sweep path wrap everything in project_best_effort)."""
    if not args or args[0] not in ALLOWED_VERBS:
        raise KanbanError(f"verb not allowed for a mirror board: {args[:1]}")
    proc = subprocess.run(
        ["hermes", "kanban", "--board", BOARD, *args],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise KanbanError(f"kanban {args[0]} failed: {(proc.stderr or proc.stdout)[-200:]}")
    return proc.stdout


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _one_line(text, limit: int = 70) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


# --------------------------------------------------------------------------------------
# Derivation — disk -> desired cards (pure; no kanban calls)
# --------------------------------------------------------------------------------------
def derive_jobs(root: Path) -> dict[str, dict]:
    """Fold the sweep-owned event log into one {job_id: {stage, lane, title, note}} view.
    Events are appended chronologically by the sweep, so a plain forward fold is the
    timeline; state/builds/<job>/ existing overrides everything (the idea crossed over)."""
    state = root / "state"
    jobs: dict[str, dict] = {}
    for e in _read_jsonl(state / "events.jsonl"):
        if e.get("profile") != "think":
            continue
        job = e.get("job_id")
        ev = e.get("event", "")
        if not job or job == "seed":
            continue
        j = jobs.setdefault(job, {"stage": None, "note": ""})
        if ev == "t1-pass":
            j["stage"] = "researched"
        elif ev == "t2-pass":
            j["stage"] = "proposed"
        elif ev.startswith("t3-"):
            j["stage"] = "under-review"
            j["note"] = f"T3 {ev[3:]}: decision={e.get('decision')}"
        elif ev == "think-parked":
            j["stage"] = "parked"
            j["note"] = _one_line("; ".join(e.get("reasons", [])), 160)
        elif ev == "think-gate-fail":
            j["stage"] = "held"
            j["note"] = _one_line(
                f"{e.get('gate')}: " + "; ".join(e.get("reasons", [])), 160
            )
        elif ev == "digest":
            j["stage"] = "digest"
    for job, j in jobs.items():
        if (state / "builds" / job).is_dir():
            j["stage"] = "building"
        contract = state / "tracks" / job / "idea-contract.json"
        what = ""
        try:
            what = json.loads(contract.read_text()).get("what_should_exist", "")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        lane_file = state / "tracks" / job / "lane"
        j["lane"] = lane_file.read_text().strip() if lane_file.is_file() else ""
        j["title"] = f"{job} — {_one_line(what)}" if what else job
    return {k: v for k, v in jobs.items() if v["stage"] in STAGE_STATUS}


def derive_topics(root: Path) -> list[str]:
    topics_file = root / "state" / "think" / "topics.txt"
    if not topics_file.exists():
        return []
    return [
        ln.strip()
        for ln in topics_file.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


# --------------------------------------------------------------------------------------
# Projection — desired cards -> kanban calls (idempotent per run)
# --------------------------------------------------------------------------------------
def _existing_by_title_prefix() -> dict[str, dict]:
    """{first-token-of-title: task} for every card on the board (archived included), so a
    job card is found by its job_id and a topic card by its 'Topic:' marker + name."""
    tasks = json.loads(_kanban("list", "--json", "--archived") or "[]")
    out = {}
    for t in tasks:
        title = t.get("title", "")
        key = title.split(" — ")[0].strip()
        out[key] = t
    return out


def _cache_path(root: Path) -> Path:
    return root / "state" / "think" / "board-projection.json"


def _load_cache(root: Path) -> dict:
    try:
        return json.loads(_cache_path(root).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(root: Path, cache: dict) -> None:
    p = _cache_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2, sort_keys=True))


def project(root: Path = ATB_ROOT) -> dict:
    """One idempotent projection pass. Returns a small summary dict (for the CLI)."""
    summary = {"created": 0, "moved": 0, "commented": 0}
    existing = _existing_by_title_prefix()
    cache = _load_cache(root)

    # Topic seeds -> scheduled (permanent; created once, then never touched). A seed card
    # briefly passes through `ready` (create has no scheduled initial-status); if an earlier
    # run's schedule call failed, REPAIR it here instead of skipping — a topic card must
    # never be LEFT claimable (Codex board HOLD #2).
    for topic in derive_topics(root):
        key = f"Topic: {topic}"
        task = existing.get(key)
        if task is not None:
            if task.get("status") == "ready":
                _kanban("schedule", task["id"], "recurring research seed (repair)")
                summary["moved"] += 1
            continue
        out = _kanban(
            "create", key, "--json",
            "--idempotency-key", f"think:topic:{topic}",
            "--body", "Think-loop research seed (state/think/topics.txt). Mirror card — "
                      "edits here drive nothing.",
        )
        task_id = json.loads(out)["id"]
        _kanban("schedule", task_id, "recurring research seed")
        summary["created"] += 1

    # Idea cards.
    for job, j in derive_jobs(root).items():
        target_status = STAGE_STATUS[j["stage"]]
        lane_tag = f" [{j['lane']}]" if j.get("lane") else ""
        task = existing.get(job)
        if task is None:
            out = _kanban(
                "create", j["title"] + lane_tag, "--json",
                "--initial-status", "running",  # NEVER `ready` — no claimable instant
                "--idempotency-key", f"think:job:{job}",
                "--body", "Mirror of the think-loop track state/tracks/" + job +
                          " — disk is the source of truth; edits here drive nothing.",
            )
            task = json.loads(out)
            summary["created"] += 1
        task_id, status = task["id"], task.get("status", "running")
        if status != target_status and status not in ("done", "archived"):
            note = j.get("note") or j["stage"]
            if target_status == "blocked":
                _kanban("block", task_id, note)
            elif target_status == "done":
                _kanban("complete", task_id, "--result", f"digest rendered — {note}"[:160])
            elif target_status == "archived":
                _kanban("complete", task_id, "--result", "promoted to the build loop")
                _kanban("archive", task_id)
            summary["moved"] += 1
        elif status != "archived" and target_status == "archived":
            _kanban("archive", task_id)
            summary["moved"] += 1
        # Stage-transition comment, de-duplicated via the display cache.
        if cache.get(job) != j["stage"]:
            _kanban(
                "comment", task_id,
                f"stage: {j['stage']}" + (f" — {j['note']}" if j.get("note") else ""),
            )
            cache[job] = j["stage"]
            summary["commented"] += 1

    _save_cache(root, cache)
    return summary


def project_best_effort(root: Path = ATB_ROOT) -> None:
    """The sweep-side entry point: NEVER raises. The board is a mirror; a kanban outage,
    a missing board, or any bug here must not touch a cycle's exit code or any gate."""
    try:
        project(root)
    except Exception as exc:  # noqa: BLE001 — by design: mirror failures are non-fatal
        print(f"[think-board] projection skipped: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Project think-loop disk state onto the kanban mirror.")
    ap.add_argument("--root", default=str(ATB_ROOT), help="ATB root (default: repo root)")
    args = ap.parse_args(argv)
    s = project(Path(args.root))
    print(f"think-board: {s['created']} created, {s['moved']} moved, {s['commented']} commented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
