#!/usr/bin/env python3
"""Deterministic gate sweep for the ATB hard-gated build graph.

Counterpart to ``tools/atb_enqueue.py``. The kanban dispatcher has no pre-dispatch
hook, so gate cards (assignee ``codex-gate``, parked ``scheduled``) are owned by THIS
sweep -- a plain script, no agent -- keeping a frontier reviewer's verdict on the
critical path instead of a 9B's.

For each ``scheduled`` ``codex-gate`` card whose parents are ALL ``done``/``archived``
(i.e. it is this gate's turn), it runs the matching Codex review:
  - plan gate -> ``scripts/atb_plan_review.sh <track>``
  - diff gate -> ``scripts/atb_diff_review.sh <worktree> <track>``
  - qa   gate -> deterministic, NO Codex + NO model: ``tools.run_verification --verifier
                 qa`` then ``tools.verification_delta``; approves only on delta state
                 ``confirmed``. This takes the local QA model out of the trust path -- on
                 run #3 (2026-06-03) the QA *model* forged a tool-shaped, future-dated
                 verification that passed the validator gate; a tool can't be bribed.

and acts on the exit code (the scripts already FAIL SAFE):
  - 0 approve      -> (plan only) take a pre-build snapshot; swap in the next model;
                      then ``unblock`` + ``complete`` the gate -> its child worker
                      auto-promotes and the live dispatcher spawns it.
  - 2 needs/deny   -> leave the gate ``scheduled`` (the child is held forever) +
                      ``comment`` the verdict + emit event.  HARD STOP.
  - 1 codex error  -> FAIL SAFE: same hold as a fail.
  - swap fails     -> HOLD (never spawn a worker whose model isn't resident).

Diff-gate idempotency (loop guard): a held diff card stays ``scheduled``, so without
this every sweep would re-review the SAME unchanged diff, re-hold, and re-alert forever.
The diff gate records the SHA of the exact diff it held on; a later sweep whose diff is
byte-identical SKIPs silently (no Codex call, no event) until the code actually changes.
A transient Codex ERROR is NOT recorded, so it is always retried.

Usage:
  python -m tools.gate_sweep [--board auto-build] [--dry-run] [--no-swap]
                             [--json] [--emit-event]

Exit: 0 = nothing held (all clear / only waiting) · 2 = >=1 gate held · 1 = sweep error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools import coach
from tools import decompose_events as dev
from tools import decompose_post

from config import paths

ATB_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = paths.hermes_scripts()
EVENTS = ATB_ROOT / "state" / "events.jsonl"
# Per-card record of the diff SHA we last HELD a diff gate on, so an unchanged
# diff is not re-reviewed (and re-alerted) every sweep. See _diff_sha / process_gate.
# TRUST DOMAIN: this lives in the overseer-owned state/ dir, the SAME trust root as
# events.jsonl and the kanban db -- NOT inside the sandboxed build worktrees
# (state/builds/<id>). A forced "unchanged" skip only leaves an already-held card
# held (it can never make a failing build PASS), and writing this file requires
# state/-dir write access that already defeats the whole trust spine -- so it grants
# no capability beyond what that access already would. Keep it out of worker reach.
HELD_DIFF_STATE = ATB_ROOT / "state" / "gate_held_diffs.json"
DONE_STATES = ("done", "archived")


def _kanban(board: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["hermes", "kanban", "--board", board, *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ATB_ROOT),
    )


def _kanban_json(board: str, *args: str):
    cp = _kanban(board, *args)
    if cp.returncode != 0:
        raise RuntimeError(f"`kanban {' '.join(args)}` failed: {cp.stderr.strip()}")
    return json.loads(cp.stdout or "[]")


def _as_list(data) -> list:
    return data if isinstance(data, list) else data.get("tasks", [])


def _ids_with_status(board: str, status: str) -> set[str]:
    items = _as_list(_kanban_json(board, "list", "--status", status, "--json"))
    return {it["id"] for it in items if isinstance(it, dict) and "id" in it}


def _diff_sha(worktree: str, base: str) -> str | None:
    """SHA-256 of the EXACT diff the Codex review will see (``git diff <base>`` in
    the worktree -- the same command atb_diff_review.sh pipes to the reviewer).

    Hashes the RAW bytes (text=False): a binary-file diff is not valid UTF-8, so
    decoding it (and a lossy re-encode) could crash the sweep or collide two
    different diffs to one hash. Returns None on ANY error -- no worktree, bad
    base, git failure, or a subprocess/OS exception -- so the caller falls through
    to a full review. A gate is NEVER skipped on uncertainty.
    """
    if not worktree:
        return None
    try:
        cp = subprocess.run(
            ["git", "-C", worktree, "diff", base or "HEAD"],
            capture_output=True,  # bytes, no text decoding
            check=False,
        )
    except OSError:
        return None
    if cp.returncode != 0:
        return None
    return hashlib.sha256(cp.stdout).hexdigest()


def _load_held_diffs() -> dict:
    try:
        return json.loads(HELD_DIFF_STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _record_held_diff(card_id: str, sha: str | None) -> None:
    """Remember the diff SHA we just HELD this card on (best-effort, never raises).

    Only deterministic holds (scope / failed verification / Codex needs_changes)
    record -- a transient gate ERROR (Codex timeout/auth) does NOT, so it is always
    retried. Recording is what lets the next sweep skip an unchanged held diff.
    """
    if not sha:
        return
    data = _load_held_diffs()
    if data.get(card_id) == sha:
        return
    data[card_id] = sha
    try:
        HELD_DIFF_STATE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _clear_held_diff(card_id: str) -> None:
    """Forget any held-diff record for a card (best-effort), e.g. once it passes."""
    data = _load_held_diffs()
    if card_id in data:
        del data[card_id]
        try:
            HELD_DIFF_STATE.write_text(json.dumps(data, indent=2))
        except OSError:
            pass


def emit_event(payload: dict) -> None:
    if not EVENTS.parent.is_dir():
        return
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **payload}
    )
    with EVENTS.open("a") as fh:
        fh.write(line + "\n")


def _log_gate_metrics(
    role: str, started_at: str, ended_at: str, swap_occurred: bool
) -> None:
    """Append a run-metrics record for a gate turn (best-effort; never raises).

    Wires in tools/runlog.py so gate review latency + whether a model swap ran land
    in events.jsonl as a `run-metrics` event. prompt/completion tokens are 0 (Codex
    does not surface them to the sweep); worker token/inference capture is a later
    tier. Metrics must never break the gate they observe, so all errors are swallowed.
    """
    try:
        from tools.runlog import log_run

        log_run(
            str(EVENTS),
            role=role,
            model="codex",
            prompt_tokens=0,
            completion_tokens=0,
            started_at=started_at,
            ended_at=ended_at,
            swap_occurred=swap_occurred,
        )
    except Exception as exc:  # noqa: BLE001 - metrics are non-critical observability
        print(f"[gate] WARN run-metrics log failed: {exc}", file=sys.stderr)


def run_review(
    gate: str, track: str, worktree: str, base: str = "HEAD"
) -> subprocess.CompletedProcess:
    if gate == "plan":
        cmd = ["bash", str(SCRIPTS / "atb_plan_review.sh"), track]
    elif gate == "diff":
        cmd = ["bash", str(SCRIPTS / "atb_diff_review.sh"), worktree, track, base]
    else:
        raise ValueError(f"unknown gate type {gate!r}")
    return subprocess.run(
        cmd, capture_output=True, text=True, check=False, cwd=str(ATB_ROOT)
    )


def _run_module(module: str, *margs: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", module, *margs],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ATB_ROOT),
    )


def process_qa_layer_pre_gate(
    board: str,
    card_id: str,
    track: str,
    worktree: str,
    *,
    dry_run: bool,
    emit: bool,
) -> str | None:
    """OPT-IN Rev 5 QA-layer step (Gemma adversarial test-writer in a sandbox).

    Runs BEFORE the existing deterministic qa-gate when the `ATB_QA_LAYER` env var
    is set to "1". The new layer invokes the QA agent (Gemma) under a model lock,
    runs its tests inside a `sandbox-exec` filesystem jail, then Codex emits per-item
    verdicts and a pure function computes the route deterministically.

    Returns:
      None       — layer disabled or not applicable; caller continues to the existing
                   deterministic qa-gate.
      "pass"     — new layer routed "confirm"; caller continues to the deterministic
                   qa-gate floor (the layered defense: both must pass).
      "held"     — new layer routed "gemma" or "hold_human"; caller HOLDs the card.
      "coaching" — new layer routed "coder" (real bug found by Gemma); caller emits a
                   coach event for the coder loop to pick up.

    Design note: the new layer is opt-in by env var so a normal gate-sweep run keeps
    its current behavior. Once F1-F4 shadow fixtures pass and dep-planner's first run
    is clean, the flag can default to "1" or be removed.
    """
    if os.environ.get("ATB_QA_LAYER", "0") != "1":
        return None  # opt-in; existing qa-gate runs as today
    if dry_run:
        return None  # dry-run mode: skip the live model invocation

    try:
        from tools.qa import _collect_coder_nodeids, run_qa
    except Exception as exc:
        # Import failure (missing dep, syntax error, etc.) should not silently disable
        # the layer — surface clearly.
        print(
            f"[gate] qa-layer import failed for {card_id}: {exc}",
            file=sys.stderr,
        )
        return "held"

    track_dir = Path(track)
    if not track_dir.is_absolute():
        track_dir = ATB_ROOT / track_dir
    track_dir = track_dir.resolve()
    canonical_ws = Path(worktree).resolve()
    state_dir = ATB_ROOT / "state"

    # Finding #7 (2026-06-07): collect coder pytest nodeids BEFORE run_qa so
    # C3 has a real expected-set to validate Gemma's `coder_test_evidence`
    # against. Without this, every non-empty citation tripped C3. The helper
    # tolerates collection failures (returns empty + a reason) — we emit a
    # diagnostic event when that happens, and let the deterministic checks
    # decide the route (fail-closed by design).
    coder_nodeids, collect_reason = _collect_coder_nodeids(canonical_ws)
    if collect_reason and emit:
        emit_event(
            {
                "job_id": card_id,
                "profile": "gate",
                "event": "qa-layer-collect-warn",
                "reason": collect_reason[:300],
                "nodeid_count": len(coder_nodeids),
            }
        )

    # The runner handles its own try/finally — locks always released, tmp_ws always cleaned.
    try:
        result = run_qa(
            track_dir,
            canonical_ws,
            state_dir,
            coder_test_nodeids=coder_nodeids,
        )
    except Exception as exc:
        # The runner should return a structured QARun on any internal failure, but
        # defend against a programmer error in the orchestrator itself.
        msg = f"qa-layer crashed for {card_id}: {exc}"
        print(f"[gate] {msg}", file=sys.stderr)
        _kanban(board, "comment", card_id, msg)
        if emit:
            emit_event(
                {
                    "job_id": card_id,
                    "profile": "gate",
                    "event": "gate-held",
                    "gate": "qa-layer",
                    "reason": msg[:300],
                }
            )
        return "held"

    summary = (
        f"qa-layer route={result.computed_route} "
        f"({result.elapsed_seconds:.1f}s) — {result.failure_reason or 'ok'}"
    )
    if emit:
        emit_event(
            {
                "job_id": card_id,
                "profile": "gate",
                "event": "qa-layer-result",
                "route": result.computed_route,
                "summary": summary[:300],
            }
        )
    print(f"[gate] qa-layer {card_id}: {summary}", file=sys.stderr)

    if result.computed_route == "confirm":
        return "pass"  # caller continues to existing deterministic qa-gate
    if result.computed_route == "coder":
        # Real bug found by Gemma; emit a coach event (the existing coach loop handles
        # the rest of the coder rework cycle).
        _kanban(board, "comment", card_id, summary)
        return "coaching"
    # gemma / hold_human → HOLD
    _kanban(board, "comment", card_id, summary)
    return "held"


def _job_id_for_track(track_dir: Path) -> str:
    """The canonical job_id for a build track == its dir basename (the mint writes a child to
    state/tracks/<job_id>, and the child_artifacts_frozen record keys on that same job_id)."""
    return Path(track_dir).name


def b2_decomposition_size_gate(track_dir: Path, worktree: str, base: str) -> str | None:
    """B2 (§9): for a DECOMPOSITION CHILD only, recount ACTUAL emitted files (git diff within
    allowed_paths) + ACTUAL checks (the tool-emitted qa-verification.json) against the caps; over
    cap -> a re-split reason string (the caller HOLDs). Returns None for a non-child (no-op — the
    existing corpus is unaffected) or when within cap. Reads the TRUSTED child_artifacts_frozen
    event (Main can't write events.jsonl — B1), never a doer field. Fail-safe: if the actual counts
    can't be computed for a known child, HOLD (never silently pass an over-budget child)."""
    job_id = _job_id_for_track(track_dir)
    # FAIL CLOSED if the trusted event log is UNREADABLE (Codex P5 r2): substituting [] would make
    # a real decomposition child look like a non-child and skip D6. An ABSENT log returns [] inside
    # read_events (no events yet -> legitimately no children), so only a present-but-unreadable log
    # HOLDs — that is a genuine fault that must halt, not silently pass an over-budget child.
    try:
        events = dev.read_events(EVENTS)
    except OSError as exc:
        return f"D6: cannot read the event log ({exc}) — failing closed (cannot rule out a decomposition child)"
    if not decompose_post.is_decomposition_child(events, job_id):
        return None  # not a decomposition child -> D6 does not apply
    try:
        file_count = decompose_post.actual_in_scope_file_count(track_dir, Path(worktree), base)
        qa = json.loads((Path(track_dir) / "qa-verification.json").read_text())
        check_count = len(qa.get("outputs", []))
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        return f"D6 size recount could not run for decomposition child {job_id} — failing closed: {exc}"
    violations = decompose_post.d6_over_cap(file_count, check_count)
    if violations:
        return "; ".join(v["detail"] for v in violations)
    return None


def b3_plan_reverify_detail(track_dir: Path) -> tuple[str, object]:
    """B3 (§9) in ONE events read (Codex R7), returning the frozen record so the caller can also trust
    the plan-readiness clearance without a second read (no latest-wins drift between the two). Returns:
      ("not_child", None)                         — not a decomposition child (no-op; corpus unaffected)
      ("hold", reason)                            — unreadable log / out-of-repo / byte-drift → plan HOLDs
      ("verified", {"events": [...], "rec": {...}}) — in-repo AND bytes byte-match the D9 freeze

    Containment is fail-closed (Codex P5 HIGH): a known decomposition child whose track escaped ATB_ROOT
    HOLDs — never silently skip the reverify and proceed."""
    resolved = Path(track_dir).resolve()
    job_id = _job_id_for_track(resolved)
    # FAIL CLOSED on an UNREADABLE event log (Codex P5 r2): [] would make a child look like a non-child
    # and skip the reverify. (An absent log returns [] inside read_events — no children yet — so only a
    # present-but-unreadable log HOLDs.)
    try:
        events = dev.read_events(EVENTS)
    except OSError as exc:
        return ("hold", f"B3: cannot read the event log ({exc}) — failing closed (cannot rule out a "
                "decomposition child)")
    rec = decompose_post.frozen_child_record(events, job_id)
    if rec is None:
        return ("not_child", None)  # not a decomposition child -> no-op (existing corpus unaffected)
    # It IS a trusted decomposition child: it MUST be in-repo AND byte-match, or fail closed.
    if ATB_ROOT not in resolved.parents:
        return ("hold", f"decomposition child {job_id}: track {resolved} is outside the repo ({ATB_ROOT}) "
                "— refusing to operate (cannot reverify an out-of-repo child)")
    if not decompose_post.verify_built_child(resolved, rec["child"]):
        return ("hold", f"decomposition child {job_id}: built idea-contract/product-plan bytes != the "
                "D9-frozen hashes (contract drifted between mint and build)")
    return ("verified", {"events": events, "rec": rec})


def b3_plan_reverify(track_dir: Path) -> str | None:
    """Back-compat thin wrapper: the HOLD reason (drift / out-of-repo / unreadable log) or None
    (not-a-child OR verified). New callers use b3_plan_reverify_detail to also get the frozen record."""
    status, payload = b3_plan_reverify_detail(track_dir)
    return payload if status == "hold" else None


def process_qa_gate(
    board: str,
    card_id: str,
    track: str,
    worktree: str,
    swap_next,
    *,
    base: str = "HEAD",
    dry_run: bool,
    no_swap: bool,
    emit: bool,
) -> str:
    """Deterministic QA gate -- tool-run the QA verification + compute the delta, NO model.

    Replaces the old QA *worker* card (a local model that could, and on run #3 did, forge
    a tool-shaped, future-dated qa-verification). Here a tool runs the verification
    (real exit codes + real timestamp) and a tool computes the delta; the gate approves
    ONLY when the delta state is ``confirmed``. Any drift/regression/missing_evidence
    (including a fabricated timestamp the freshness guard rejects) HOLDS the build.
    """
    t0 = datetime.now(timezone.utc)
    if not dry_run:
        # 0. Clear any pre-existing QA artifacts so the delta can ONLY read evidence
        #    produced by THIS gate run. A stale or model-forged qa-verification.json left
        #    in the track dir must never be reused: without this, if run_verification fails
        #    to write, verification_delta could confirm from a leftover still inside the
        #    freshness window (Codex review 2026-06-03).
        track_dir = Path(track)
        if not track_dir.is_absolute():
            track_dir = ATB_ROOT / track_dir
        track_dir = track_dir.resolve()
        # Containment: the card body is data; on a malformed/hostile track path, never
        # unlink (or verify) outside the repo. Require the track dir under ATB_ROOT.
        if ATB_ROOT not in track_dir.parents:
            msg = (
                f"qa gate: HELD -- track path {track_dir} is not a directory below the "
                f"repo ({ATB_ROOT}); refusing to operate."
            )
            _kanban(board, "comment", card_id, msg)
            if emit:
                emit_event(
                    {
                        "job_id": card_id,
                        "profile": "gate",
                        "event": "gate-held",
                        "gate": "qa",
                        "reason": f"track outside repo: {track_dir}"[:300],
                    }
                )
            print(
                f"[gate] HELD qa {card_id}: track outside repo -- {track_dir}",
                file=sys.stderr,
            )
            return "held"
        for stale in ("qa-verification.json", "verification-delta.json"):
            try:
                (track_dir / stale).unlink()
            except FileNotFoundError:
                pass  # already absent -- good
            except OSError as exc:
                # Cannot guarantee the leftover is gone -> must NOT risk reusing stale
                # evidence. HARD HOLD before running any verification.
                msg = (
                    f"qa gate: HELD -- could not clear stale {stale} before verifying "
                    f"({exc}); refusing to risk reusing leftover evidence."
                )
                _kanban(board, "comment", card_id, msg)
                if emit:
                    emit_event(
                        {
                            "job_id": card_id,
                            "profile": "gate",
                            "event": "gate-held",
                            "gate": "qa",
                            "reason": f"could not clear {stale}: {exc}"[:300],
                        }
                    )
                print(
                    f"[gate] HELD qa {card_id}: could not clear {stale} -- {exc}",
                    file=sys.stderr,
                )
                return "held"

        # 1. Tool-capture QA's independent run -> fresh qa-verification.json (real ts, rc).
        ver = _run_module(
            "tools.run_verification",
            "--track",
            str(track_dir),  # the resolved, containment-checked path -- not the raw body
            "--worktree",
            worktree,
            "--verifier",
            "qa",
        )
        # rc 0 = passed, 2 = a command failed (still WROTE the file -> delta -> regression).
        # Anything else (1 usage/IO error, etc.) means NO fresh evidence was produced; with
        # the leftovers cleared above there is nothing to fall back on -> HOLD.
        if ver.returncode not in (0, 2):
            tail = (ver.stderr or ver.stdout or "").strip().splitlines()
            why = tail[-1] if tail else f"rc={ver.returncode}"
            _kanban(
                board,
                "comment",
                card_id,
                f"qa gate: HELD -- qa verification tool failed to run ({why}); no fresh evidence.",
            )
            if emit:
                emit_event(
                    {
                        "job_id": card_id,
                        "profile": "gate",
                        "event": "gate-held",
                        "gate": "qa",
                        "reason": f"verification tool failed: {why}"[:300],
                    }
                )
            print(
                f"[gate] HELD qa {card_id}: verification tool failed -- {why}",
                file=sys.stderr,
            )
            return "held"

        # 2. Deterministically compute the delta over THIS run's evidence. The state,
        #    not any agent, decides pass/hold.
        dcp = _run_module("tools.verification_delta", "--track", str(track_dir), "--json")
        delta_rc = dcp.returncode
        try:
            state = (json.loads(dcp.stdout or "{}") or {}).get("state", "missing_evidence")
        except json.JSONDecodeError:
            state = "missing_evidence"
    else:
        # dry-run is intentionally non-mutating: it neither clears stale files, runs the
        # verifier, nor computes a real delta -- so it is a mechanism check, NOT a safety
        # gate. Report a nominal pass shape and mutate nothing.
        state, delta_rc = "confirmed", 0
    t1 = datetime.now(timezone.utc)

    # Approve ONLY on a 'confirmed' delta that the tool also exited 0 for. Any other state,
    # a non-zero delta rc, or missing/truncated stdout -> HOLD.
    if state == "confirmed" and delta_rc == 0:
        if not dry_run:
            # B2 (§9): for a DECOMPOSITION CHILD, the BINDING size gate is this post-build recount
            # of ACTUAL files/checks vs cap — a hard parent gate BEFORE QA `complete`. Over cap ->
            # the child ballooned past a single session -> HOLD + re-split (no-op for every
            # non-decomposition track, so the existing corpus completes exactly as before).
            d6_reason = b2_decomposition_size_gate(track_dir, worktree, base)
            if d6_reason is not None:
                _kanban(board, "comment", card_id,
                        f"qa gate: HELD -- D6 size ceiling: {d6_reason}")
                if emit:
                    emit_event({"job_id": card_id, "profile": "gate", "event": "gate-held",
                                "gate": "qa", "reason": f"D6: {d6_reason}"[:300]})
                print(f"[gate] HELD qa {card_id}: D6 size ceiling -- {d6_reason}", file=sys.stderr)
                return "held"
            if swap_next and not no_swap:
                sw = _run_module("tools.swap_orchestrator", swap_next)
                if sw.returncode != 0:
                    detail = (sw.stderr or "").strip()[:200]
                    _kanban(
                        board,
                        "comment",
                        card_id,
                        f"qa gate: confirmed but model swap to '{swap_next}' FAILED -- holding.",
                    )
                    print(f"[gate] HOLD {card_id}: qa swap failed {detail}", file=sys.stderr)
                    if emit:
                        emit_event(
                            {
                                "job_id": card_id,
                                "profile": "gate",
                                "event": "gate-error",
                                "gate": "qa",
                                "reason": detail,
                            }
                        )
                    return "error"
            _kanban(board, "unblock", card_id)
            done = _kanban(board, "complete", card_id, "--result", "qa gate: confirmed")
            if done.returncode != 0:
                print(
                    f"[gate] ERROR completing {card_id}: {done.stderr.strip()}",
                    file=sys.stderr,
                )
                return "error"
        if emit:
            emit_event(
                {"job_id": card_id, "profile": "gate", "event": "gate-pass", "gate": "qa"}
            )
            if not dry_run:
                _log_gate_metrics(
                    role="gate:qa",
                    started_at=t0.isoformat(),
                    ended_at=t1.isoformat(),
                    swap_occurred=bool(swap_next and not no_swap),
                )
        tail = " (dry-run)" if dry_run else " -> build confirmed"
        print(f"[gate] PASS qa {card_id}{tail}")
        return "pass"

    # Not confirmed, or the delta tool did not exit 0 -> HOLD. HARD STOP.
    if not dry_run:
        _kanban(
            board,
            "comment",
            card_id,
            f"qa gate: HELD (delta state '{state}', rc {delta_rc}; not a confirmed/rc-0 "
            "result). Deterministic QA verification did not confirm the build -- see "
            "verification-delta.json.",
        )
    if emit:
        emit_event(
            {
                "job_id": card_id,
                "profile": "gate",
                "event": "gate-held",
                "gate": "qa",
                "reason": f"delta state '{state}' rc {delta_rc}"[:300],
            }
        )
    print(f"[gate] HELD qa {card_id}: delta state '{state}' (rc {delta_rc})")
    return "held"


def _coach_or_escalate(
    board: str,
    card_id: str,
    track: str,
    worktree: str,
    base: str,
    findings: str,
    diff_sha: str | None,
) -> str:
    """A diff-gate hold -> coach the coder (under the cap) or escalate (coach-exhausted).

    Replaces the old "park it and ping a human every hold" behavior on the DIFF gate. Under
    the cap it runs the TEACHER (tools.coach), writes round-N feedback, and re-dispatches the
    coder to fix it; at the cap it emits ``coach-exhausted`` and leaves the card held for a
    human. Returns ``coaching`` (progress), ``held`` (escalated), or ``error`` (coach failed).

    Held-diff SHA bookkeeping (the convergence guarantee):
      - COACHING does NOT record the sha. Relinking the gate to the new (pending) coder parks
        it until the rework lands; when the coder completes, the gate re-judges the code AS IT
        IS -- even if the coder changed nothing, it gets re-judged and either coaches again or
        escalates. Recording here would let an unchanged rework be skipped as "unchanged" and
        stall silently (Codex review blocker #1).
      - ESCALATION records the sha so the same unchanged held diff is not re-reviewed and
        re-alerted every sweep (the loop-guard's original job).
    Coaching is best-effort: a teacher/re-dispatch failure emits a WATCHED ``gate-error`` (not
    a silent hold) and returns ``error`` so the build is never silently dropped; the sha is NOT
    recorded so a transient failure is retried next sweep. The judge stays the load-bearing gate.

    The coach/coach-exhausted/gate-error events here are emitted UNCONDITIONALLY -- they are NOT
    gated by ``--emit-event``. The cap reads the ``coach`` count back from events.jsonl, so the
    count is load-bearing accounting, not optional observability: gating it on a flag would let a
    bare ``python -m tools.gate_sweep`` (no --emit-event) coach forever and never escalate (Codex
    review). This function only runs on a real sweep (the caller guards ``not dry_run``).
    """
    done = coach.rounds_done(EVENTS, card_id)
    if done >= coach.COACH_CAP:
        msg = (
            f"diff gate: COACH-EXHAUSTED after {done} round(s) -- the coder did not converge "
            "on the contract. Held for human review."
        )
        _kanban(board, "comment", card_id, msg)
        emit_event(
            {
                "job_id": card_id,
                "profile": "gate",
                "event": "coach-exhausted",
                "gate": "diff",
                "rounds": done,
            }
        )
        _record_held_diff(card_id, diff_sha)  # dedup the escalation across sweeps
        print(
            f"[gate] COACH-EXHAUSTED diff {card_id}: {done} rounds -- escalating",
            file=sys.stderr,
        )
        return "held"

    round_n = done + 1
    try:
        result = coach.coach(track, worktree, base, findings, round_n)
        new_coder = coach.redispatch_coder(
            lambda *a: _kanban(board, *a),
            card_id,
            worktree,
            result["feedback_md"],
            round_n,
        )
    except Exception as exc:  # noqa: BLE001 - coaching is best-effort; fail LOUD, never silent
        _kanban(
            board,
            "comment",
            card_id,
            f"diff gate: coaching round {round_n} could not run ({exc}); held for human "
            "(will retry next sweep if transient).",
        )
        emit_event(  # unconditional: a coach failure must always be a watched gate-error
            {
                "job_id": card_id,
                "profile": "gate",
                "event": "gate-error",
                "gate": "diff",
                "reason": f"coach round {round_n} failed: {exc}"[:300],
            }
        )
        print(
            f"[gate] ERROR diff {card_id}: coach round {round_n} failed -- {exc}",
            file=sys.stderr,
        )
        return "error"

    _kanban(
        board,
        "comment",
        card_id,
        f"diff gate: COACHING round {round_n} -- wrote {result['path']} and re-dispatched the "
        f"coder ({new_coder}). The judge will re-certify the changed code against the contract.",
    )
    # Unconditional: this is the event the cap counts (see docstring), not optional observability.
    emit_event(
        {
            "job_id": card_id,
            "profile": "gate",
            "event": "coach",
            "gate": "diff",
            "round": round_n,
            "new_coder": new_coder,
        }
    )
    print(f"[gate] COACHING diff {card_id}: round {round_n} -> coder {new_coder}")
    return "coaching"


def process_gate(
    board: str, card_id: str, show: dict, *, dry_run: bool, no_swap: bool, emit: bool
) -> str:
    """Run one gate (parents already confirmed done). Returns pass|held|error|skip|coaching."""
    task = show.get("task", {})
    try:
        meta = json.loads(task.get("body") or "{}")
    except json.JSONDecodeError:
        meta = {}
    gate = meta.get("atb_gate")
    if gate not in ("plan", "diff", "qa"):
        return "skip"  # scheduled codex-gate card that isn't ours
    track = meta.get("track", "")
    worktree = meta.get("worktree", "")
    base = meta.get("base", "HEAD")
    swap_next = meta.get("swap_next")
    # Set for the diff gate below; stays None for plan/qa so the shared hold paths
    # only record a held-diff SHA on a genuine diff-gate hold.
    diff_sha: str | None = None
    # Set True at the plan gate for a B3-verified, plan-readiness-cleared decomposition child: the
    # build-time review is then SKIPPED (the hash-bound clearance is trusted) and the existing approve
    # path runs. Stays False for every normal track + every non-plan gate.
    decomp_cleared = False

    # B3 + plan-readiness (§9, 2026-06-18): at the PLAN gate of a DECOMPOSITION CHILD, do ONE events
    # read (R7) that BOTH re-verifies the built child's contract bytes against the D9 freeze (closing the
    # mint->coder TOCTOU) AND fetches the frozen record. On a verified child, TRUST the hash-bound
    # plan-readiness clearance instead of re-running a stochastic Codex content review on identical,
    # already-cleared bytes (R2/R8): require the clearance, else HOLD fail-closed. Containment stays
    # fail-closed (an out-of-repo child HOLDs); a non-child falls through to the normal Codex plan review.
    if gate == "plan" and not dry_run:
        track_dir = Path(track)
        if not track_dir.is_absolute():
            track_dir = ATB_ROOT / track_dir
        status, payload = b3_plan_reverify_detail(track_dir)
        if status == "hold":
            _kanban(board, "comment", card_id, f"plan gate: HELD -- {payload}")
            if emit:
                emit_event({"job_id": card_id, "profile": "gate", "event": "gate-held",
                            "gate": "plan", "reason": f"B3: {payload}"[:300]})
            print(f"[gate] HELD plan {card_id}: B3 frozen-bytes re-verify -- {payload}", file=sys.stderr)
            return "held"
        if status == "verified":
            # B3 passed: an in-repo decomposition child whose bytes byte-match the D9 freeze. Its plan was
            # Codex-reviewed AND frozen at decomposition time (the plan-readiness gate). Require a clearance
            # bound to THIS run + these exact bytes; if absent, HOLD fail-closed (never fall through to a
            # review that could rc==0 and pass — Codex design-review R2).
            clearance = decompose_post.plan_clearance_record(
                payload["events"], payload["rec"]["event"], payload["rec"]["child"])
            if clearance is None:
                reason = (f"decomposition child {payload['rec']['child'].get('job_id')}: no matching "
                          "plan-readiness clearance for this run -- run tools.decompose_plan_gate (the "
                          "plan-readiness gate) and re-freeze before building (frozen => plan-cleared)")
                _kanban(board, "comment", card_id, f"plan gate: HELD -- {reason}")
                if emit:
                    emit_event({"job_id": card_id, "profile": "gate", "event": "gate-held",
                                "gate": "plan", "reason": reason[:300]})
                print(f"[gate] HELD plan {card_id}: {reason}", file=sys.stderr)
                return "held"
            _kanban(board, "comment", card_id, "plan gate: plan-readiness clearance trusted "
                    "(B3-verified bytes; stochastic Codex re-review skipped).")
            decomp_cleared = True  # synthesize APPROVE below + reuse the existing approve path (R8)

    if gate == "qa":
        # OPT-IN Rev 5 QA-layer step: when ATB_QA_LAYER=1, run the Gemma adversarial
        # test-writer in a sandbox BEFORE the existing deterministic qa-gate floor.
        # The two together are defense-in-depth — the layer catches semantic forgery,
        # the deterministic gate proves the tests actually ran. Returns None when the
        # layer is disabled so the existing flow is unchanged in default deployment.
        layer_result = process_qa_layer_pre_gate(
            board,
            card_id,
            track,
            worktree,
            dry_run=dry_run,
            emit=emit,
        )
        if layer_result is not None:
            if layer_result in ("held", "coaching"):
                return layer_result
            # layer_result == "pass" → fall through to the existing deterministic qa-gate.
        # Deterministic, model-free verification gate (no Codex review to run).
        return process_qa_gate(
            board,
            card_id,
            track,
            worktree,
            swap_next,
            base=base,
            dry_run=dry_run,
            no_swap=no_swap,
            emit=emit,
        )

    # E4/E2: at the diff gate, capture verification with a TOOL before spending Codex.
    # run_verification re-runs product-plan.verification_commands in the worktree and writes
    # a schema-conformant verification.json whose `pass` is the real exit code (not an agent's
    # claim). A build whose tests don't pass is HELD here -- the Codex review never runs on a
    # broken (or unproven) build. Closes the self-confirm hole both 2026-06-02 councils flagged.
    if gate == "diff" and not dry_run:
        # IDEMPOTENCY (loop guard): if the exact diff the reviewer would see is
        # byte-identical to the one we already HELD this card on, re-running scope +
        # verification + a Codex call just reproduces the same hold and re-pings the
        # operator every sweep. Skip silently (no event, no comment) until the code
        # actually changes. A None SHA (git error) falls through to a full review --
        # never skip a gate on uncertainty.
        diff_sha = _diff_sha(worktree, base)
        if diff_sha and _load_held_diffs().get(card_id) == diff_sha:
            print(
                f"[gate] SKIP diff {card_id}: diff unchanged since last hold "
                f"({diff_sha[:12]}) -- not re-reviewing or re-alerting.",
                file=sys.stderr,
            )
            return "unchanged"

        # SCOPE GATE (deterministic, runs FIRST): every committed changed file must stay
        # inside the product-plan allowed_paths sandbox. Cheapest + most fundamental check,
        # so an escape fails instantly without spending a test run or a Codex call.
        # allowed_paths is BINDING; planned_files is only an advisory forecast. This moves
        # the path question from Codex's fuzzy "does this file look related?" judgment (which
        # held track #4 twice on in-sandbox events.jsonl/.gitignore) to an unforgeable tool.
        scope = _run_module(
            "tools.scope_check",
            "--track",
            track,
            "--worktree",
            worktree,
            "--base",
            base,
        )
        if scope.returncode != 0:
            out = (scope.stdout or "") + "\n" + (scope.stderr or "")
            bad = [ln.strip(" -\t") for ln in out.splitlines() if ln.strip().startswith("-")]
            why = (
                ", ".join(bad)
                if bad
                else ([ln for ln in out.splitlines() if ln.strip()] or [f"scope rc={scope.returncode}"])[-1]
            )
            _kanban(
                board,
                "comment",
                card_id,
                f"diff gate: HELD -- scope: changed file(s) outside allowed_paths ({why}). "
                "allowed_paths is the binding sandbox; planned_files is only a forecast.",
            )
            if emit:
                emit_event(
                    {
                        "job_id": card_id,
                        "profile": "gate",
                        "event": "gate-held",
                        "gate": "diff",
                        "reason": f"scope: {why}"[:300],
                    }
                )
            print(f"[gate] HELD diff {card_id}: scope -- {why}", file=sys.stderr)
            _record_held_diff(card_id, diff_sha)
            return "held"

        ver = _run_module(
            "tools.run_verification",
            "--track",
            track,
            "--worktree",
            worktree,
            "--verifier",
            "coder",
        )
        if ver.returncode != 0:
            vlines = (ver.stdout or ver.stderr or "").strip().splitlines()
            why = vlines[-1] if vlines else f"verification rc={ver.returncode}"
            _kanban(
                board,
                "comment",
                card_id,
                f"diff gate: HELD -- tool-captured verification FAILED ({why}). "
                "product-plan verification_commands must pass before the diff review.",
            )
            if emit:
                emit_event(
                    {
                        "job_id": card_id,
                        "profile": "gate",
                        "event": "verification-failed",
                        "gate": gate,
                        "reason": why[:300],
                    }
                )
            print(
                f"[gate] HELD diff {card_id}: tool-captured verification failed -- {why}",
                file=sys.stderr,
            )
            _record_held_diff(card_id, diff_sha)
            return "held"

    t0 = datetime.now(timezone.utc)
    if decomp_cleared:
        # A B3-verified, plan-readiness-cleared decomposition child: the hash-bound clearance (a Codex
        # plan review at decomposition time, frozen) stands in for a re-review. Synthesize APPROVE and
        # fall into the EXISTING approve path (snapshot/swap/unblock/complete/gate-pass/metrics) — R8.
        cp = None
        rc = 0
        summary = "plan: decomposition child plan-readiness-cleared (re-review skipped — clearance trusted)"
        lines = [summary]
    else:
        cp = run_review(gate, track, worktree, base)
        rc = cp.returncode
        lines = (cp.stdout or cp.stderr or "").strip().splitlines()
        summary = lines[0] if lines else f"codex {gate} rc={rc}"
    t1 = datetime.now(timezone.utc)

    if rc == 0:  # APPROVE
        if not dry_run:
            if gate == "plan":
                snap = _run_module("tools.snapshot", "create")
                if snap.returncode != 0:
                    detail = (snap.stderr or "").strip()[:200]
                    print(
                        f"[gate] WARN snapshot failed before {card_id}: {detail}",
                        file=sys.stderr,
                    )
                    if emit:
                        emit_event(
                            {
                                "job_id": card_id,
                                "profile": "gate",
                                "event": "snapshot-warn",
                                "gate": gate,
                                "detail": detail,
                            }
                        )
            if swap_next and not no_swap:
                sw = _run_module("tools.swap_orchestrator", swap_next)
                if sw.returncode != 0:
                    detail = (sw.stderr or "").strip()[:200]
                    msg = f"codex {gate}: approved but model swap to '{swap_next}' FAILED -- holding."
                    _kanban(board, "comment", card_id, msg)
                    print(f"[gate] HOLD {card_id}: {msg} {detail}", file=sys.stderr)
                    if emit:
                        emit_event(
                            {
                                "job_id": card_id,
                                "profile": "gate",
                                "event": "gate-error",
                                "gate": gate,
                                "reason": detail,
                            }
                        )
                    return "error"
            _kanban(board, "unblock", card_id)
            done = _kanban(
                board, "complete", card_id, "--result", f"codex {gate}: approve"
            )
            if done.returncode != 0:
                print(
                    f"[gate] ERROR completing {card_id}: {done.stderr.strip()}",
                    file=sys.stderr,
                )
                return "error"
        if emit:
            emit_event(
                {
                    "job_id": card_id,
                    "profile": "gate",
                    "event": "gate-pass",
                    "gate": gate,
                }
            )
            if not dry_run:
                _log_gate_metrics(
                    role=f"gate:{gate}",
                    started_at=t0.isoformat(),
                    ended_at=t1.isoformat(),
                    swap_occurred=bool(swap_next and not no_swap),
                )
        if gate == "diff":
            _clear_held_diff(card_id)  # passed -> forget the stale held-diff record
        tail = " (dry-run)" if dry_run else " -> child promoted"
        print(f"[gate] PASS {gate} {card_id}{tail}")
        return "pass"

    if rc == 2:  # needs_changes / deny
        # DIFF gate: turn the hold into COACHING for the coder (within the cap) instead of
        # parking it for a human to patch over the top. The teacher writes contract-anchored
        # feedback and re-dispatches the coder; the judge above re-certifies the changed code,
        # blind to the coaching. Plan-gate holds, and any dry-run, keep the old hard hold.
        if gate == "diff" and not dry_run:
            findings = (cp.stdout or cp.stderr or summary).strip()
            # The helper owns the held-diff sha bookkeeping: it records ONLY on escalation, not
            # on a coaching round (recording on coaching would let an unchanged rework stall
            # silently as "unchanged" -- Codex review blocker #1). Its coach/escalate/error
            # events are emitted unconditionally (the cap counts them) -- not gated by --emit-event.
            return _coach_or_escalate(
                board, card_id, track, worktree, base, findings, diff_sha
            )
        if not dry_run:
            _kanban(
                board,
                "comment",
                card_id,
                f"codex {gate}: HELD (needs_changes/deny). {summary}",
            )
        if emit:
            emit_event(
                {
                    "job_id": card_id,
                    "profile": "gate",
                    "event": "gate-held",
                    "gate": gate,
                    "reason": summary[:300],
                }
            )
        # Diff-gate needs_changes is deterministic given the diff: remember it so an
        # unchanged re-submit is skipped next sweep (no-op for plan -- diff_sha None).
        _record_held_diff(card_id, diff_sha)
        print(f"[gate] HELD {gate} {card_id}: {summary}")
        return "held"

    # rc == 1 (or anything else) -> codex error / timeout -> FAIL SAFE hold
    if not dry_run:
        _kanban(
            board,
            "comment",
            card_id,
            f"codex {gate}: ERROR/timeout -- held for human. {summary}",
        )
    if emit:
        emit_event(
            {
                "job_id": card_id,
                "profile": "gate",
                "event": "gate-error",
                "gate": gate,
                "reason": summary[:300],
            }
        )
    print(
        f"[gate] ERROR {gate} {card_id} (rc={rc}): held -- {summary}", file=sys.stderr
    )
    return "error"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gate_sweep", description="Deterministic ATB gate sweep."
    )
    ap.add_argument("--board", default="auto-build")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="run reviews + report verdicts, but don't mutate the board / swap / snapshot",
    )
    ap.add_argument(
        "--no-swap",
        action="store_true",
        help="skip model swap on pass (for mechanism tests)",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit-event", action="store_true")
    args = ap.parse_args(argv)

    try:
        gates = _as_list(
            _kanban_json(
                args.board,
                "list",
                "--assignee",
                "codex-gate",
                "--status",
                "scheduled",
                "--json",
            )
        )
        done_ids: set[str] = set()
        for st in DONE_STATES:
            done_ids |= _ids_with_status(args.board, st)
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"[gate] sweep error: {exc}", file=sys.stderr)
        return 1

    results: dict[str, list[str]] = {
        k: []
        for k in ("pass", "held", "error", "waiting", "skip", "unchanged", "coaching")
    }
    for g in gates:
        cid = g.get("id") if isinstance(g, dict) else None
        if not cid:
            continue
        try:
            show = _kanban_json(args.board, "show", cid, "--json")
        except (RuntimeError, json.JSONDecodeError):
            results["error"].append(cid)
            continue
        parents = show.get("parents", []) or []
        if not all(p in done_ids for p in parents):
            results["waiting"].append(cid)
            continue
        outcome = process_gate(
            args.board,
            cid,
            show,
            dry_run=args.dry_run,
            no_swap=args.no_swap,
            emit=args.emit_event,
        )
        results[outcome].append(cid)

    tripped = bool(results["held"] or results["error"])
    if args.json:
        print(json.dumps(results, indent=2))
    return 2 if tripped else 0


if __name__ == "__main__":
    sys.exit(main())
