#!/usr/bin/env python3
"""Enqueue an approved ATB track as a HARD-GATED build graph on a Kanban board.

The kanban dispatcher has NO native pre-dispatch hook (verified live 2026-05-31:
source map + CLI surface + state-machine probe). The only code-enforced gate is the
task **dependency graph**: a child can't reach `ready` until every parent is `done`,
and a parked parent holds it indefinitely. So a build becomes:

    [plan-gate] --> [coder (shared workspace)] --> [diff-gate] --> [qa]

- Gate cards (plan/diff) get assignee ``codex-gate`` (UNREGISTERED -> the live
  dispatcher *skips* them, never spawns them) and are parked ``scheduled`` (sticky).
  They are owned by the deterministic sweep (``tools/gate_sweep.py``), NOT by any
  agent -- so a frontier reviewer's verdict, not a 9B's, sits on the critical path.
- Worker cards (coder/qa) get real assignees and SHARE one git workspace. They stay
  ``todo`` behind their gate parent until the sweep passes that gate.

Nothing runs until the sweep passes plan-gate; a held gate stops the whole build.

Usage:
  python -m tools.atb_enqueue --track state/tracks/<id> --goal "<one line>" \\
      --worktree <abs git dir> [--board auto-build] [--coder coder] [--qa qa] \\
      [--max-runtime 30m] [--json]

Exit: 0 = graph created · 1 = usage / IO / kanban error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ATB_ROOT = Path(__file__).resolve().parent.parent
GATE_ASSIGNEE = "codex-gate"  # intentionally unregistered: the dispatcher skips it

# Python build/test artifacts the coder must never commit. Seeded into the build
# workspace's .gitignore at enqueue and committed INTO THE BASE, so they stay out of
# the diff the diff-gate reviews. WHY this is necessary, not just tidy: a dispatched
# worker runs `cron_mode: deny`, which blocks the coder's own `rm -rf __pycache__`
# cleanup -- so without this, `git add -A` commits the caches and the diff gate holds
# every pytest-generating build (proven in the 2026-05-31 supervised run).
_GITIGNORE_LINES = ("__pycache__/", "*.py[cod]", ".pytest_cache/")


def _kanban(board: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["hermes", "kanban", "--board", board, *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ATB_ROOT),
    )


def _create(
    board: str,
    title: str,
    *,
    assignee: str,
    parent: str | None = None,
    body: str | None = None,
    workspace: str | None = None,
    max_runtime: str | None = None,
) -> str:
    """Create one card and return its id."""
    args = [
        "create",
        title,
        "--json",
        "--assignee",
        assignee,
        "--created-by",
        "atb-enqueue",
    ]
    if parent:
        args += ["--parent", parent]
    if body:
        args += ["--body", body]
    if workspace:
        args += ["--workspace", workspace]
    if max_runtime:
        args += ["--max-runtime", max_runtime]
    cp = _kanban(board, *args)
    if cp.returncode != 0:
        raise RuntimeError(
            f"create '{title}' failed: {cp.stderr.strip() or cp.stdout.strip()}"
        )
    try:
        return json.loads(cp.stdout)["id"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(
            f"create '{title}': bad JSON id ({exc}); out={cp.stdout!r}"
        ) from exc


def _schedule(board: str, task_id: str, reason: str) -> None:
    cp = _kanban(board, "schedule", task_id, reason)
    if cp.returncode != 0:
        raise RuntimeError(f"schedule {task_id} failed: {cp.stderr.strip()}")


def _git_head(worktree: Path) -> str:
    """Worktree HEAD sha = the build base the diff gate reviews against ('HEAD' if unknown)."""
    cp = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else "HEAD"


def _ensure_gitignore(worktree: Path) -> bool:
    """Seed + commit a .gitignore covering Python build/test artifacts into the base.

    Idempotent: appends only the missing lines, and commits only when it changed
    something. Uses a fixed git identity via ``-c`` so it works even when the caller's
    ``user.name``/``user.email`` are unset (e.g. a fresh CI workspace). Committing it
    into the base keeps .gitignore OUT of the diff the diff-gate reviews. Returns True
    if a commit was made.
    """
    gi = worktree / ".gitignore"
    existing = gi.read_text().splitlines() if gi.is_file() else []
    missing = [ln for ln in _GITIGNORE_LINES if ln not in existing]
    if not missing:
        return False
    with gi.open("a") as fh:
        if existing and existing[-1].strip():
            fh.write("\n")
        fh.write("\n".join(missing) + "\n")
    subprocess.run(
        ["git", "-C", str(worktree), "add", ".gitignore"],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=ATB",
            "-c",
            "user.email=atb@local",
            "commit",
            "-m",
            "chore: gitignore python build/test artifacts (ATB enqueue)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="atb_enqueue", description="Enqueue a hard-gated ATB build graph."
    )
    ap.add_argument(
        "--track",
        required=True,
        help="track dir (must hold product-plan.json + idea-contract.json)",
    )
    ap.add_argument(
        "--goal", required=True, help="one-line build goal (used in card titles)"
    )
    ap.add_argument(
        "--worktree",
        required=True,
        help="git build dir the coder+qa share (diff gate reviews changes since this dir's HEAD)",
    )
    ap.add_argument("--board", default="auto-build")
    ap.add_argument("--coder", default="coder", help="coder profile (default: coder)")
    ap.add_argument("--qa", default="qa", help="qa profile (default: qa)")
    ap.add_argument(
        "--max-runtime", default="30m", help="per-worker runtime cap (default: 30m)"
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    track = Path(args.track)
    if not track.is_absolute():
        track = (ATB_ROOT / track).resolve()
    if not track.is_dir():
        print(f"error: track dir not found: {track}", file=sys.stderr)
        return 1
    for need in ("product-plan.json", "idea-contract.json"):
        if not (track / need).is_file():
            print(f"error: track missing {need}: {track}", file=sys.stderr)
            return 1

    worktree = Path(args.worktree).resolve()
    if not worktree.is_dir():
        print(f"error: worktree dir not found: {worktree}", file=sys.stderr)
        return 1
    if not (worktree / ".git").exists():
        print(
            f"warning: {worktree} is not a git repo -- the diff gate needs `git diff HEAD`",
            file=sys.stderr,
        )
    elif _ensure_gitignore(worktree):
        print(
            f"seeded .gitignore (python build/test artifacts) into {worktree}",
            file=sys.stderr,
        )

    base_sha = _git_head(worktree)
    plan_body = json.dumps(
        {"atb_gate": "plan", "track": str(track), "swap_next": args.coder}
    )
    diff_body = json.dumps(
        {
            "atb_gate": "diff",
            "track": str(track),
            "worktree": str(worktree),
            "base": base_sha,
        }
    )
    # The qa gate is deterministic -- tools.run_verification + tools.verification_delta,
    # NO model -- so the diff gate swaps no model in for it and the qa gate never swaps.
    # The local QA model is intentionally OUT of the trust path (it forged a tool-shaped,
    # future-dated verification on run #3); a tool runs the check and a tool writes the delta.
    qa_body = json.dumps(
        {
            "atb_gate": "qa",
            "track": str(track),
            "worktree": str(worktree),
        }
    )
    ws = f"dir:{worktree}"

    try:
        plan_gate = _create(
            args.board,
            f"GATE plan — {args.goal}",
            assignee=GATE_ASSIGNEE,
            body=plan_body,
        )
        _schedule(args.board, plan_gate, "held: awaiting Codex plan review (atb gate)")

        coder = _create(
            args.board,
            f"BUILD — {args.goal}",
            assignee=args.coder,
            parent=plan_gate,
            workspace=ws,
            max_runtime=args.max_runtime,
            body=(
                f"Build exactly per the approved plan in {track}/product-plan.json. "
                "Work ONLY in this workspace. The build is NOT done until "
                "product-plan.json's verification_commands actually pass here: the diff "
                "gate re-runs them with a tool and HOLDS the build if they fail, so a "
                "'passing' claim without a passing run will not get through. Once they pass, "
                "stage AND commit ALL your work (`git add -A && git commit -m ...`) so the "
                "diff gate can review it, then mark the task complete."
            ),
        )

        diff_gate = _create(
            args.board,
            f"GATE diff — {args.goal}",
            assignee=GATE_ASSIGNEE,
            parent=coder,
            body=diff_body,
        )
        _schedule(args.board, diff_gate, "held: awaiting Codex diff review (atb gate)")

        qa_gate = _create(
            args.board,
            f"GATE qa — {args.goal}",
            assignee=GATE_ASSIGNEE,
            parent=diff_gate,
            body=qa_body,
        )
        _schedule(
            args.board,
            qa_gate,
            "held: awaiting deterministic QA verification (atb gate)",
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    graph = {
        "board": args.board,
        "plan_gate": plan_gate,
        "coder": coder,
        "diff_gate": diff_gate,
        "qa": qa_gate,
        "track": str(track),
        "worktree": str(worktree),
        "base": base_sha,
    }
    if args.json:
        print(json.dumps(graph, indent=2))
    else:
        print(f"Enqueued hard-gated build graph on board '{args.board}':")
        print(f"  plan-gate  {plan_gate}  [scheduled, {GATE_ASSIGNEE}]")
        print(f"   └─ coder  {coder}  [todo, {args.coder}]  ws=dir:{worktree}")
        print(f"       └─ diff-gate  {diff_gate}  [scheduled, {GATE_ASSIGNEE}]")
        print(f"           └─ qa-gate  {qa_gate}  [scheduled, {GATE_ASSIGNEE}]")
        print(
            "\nNothing runs until the sweep passes plan-gate.  Sweep: scripts/atb_gate_sweep.sh"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
