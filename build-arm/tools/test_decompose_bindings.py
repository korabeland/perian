"""Phase 5 — B2 (D6 blocks QA `complete`) + B3 (plan-gate re-verifies frozen child bytes), wired
into gate_sweep. Each binding gets (a) a unit test of the real gate logic and (b) an integration
test proving it actually fails closed in process_qa_gate / process_gate. B2/B3 are NO-OPS for any
non-decomposition track (the existing corpus is unaffected) — also asserted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from tools import decompose_events as dev
from tools import decompose_post as dp
from tools import gate_sweep


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _child_frozen_event(job_id, ic_sha="a" * 64, pp_sha="b" * 64, run_id="run1"):
    return {"ts": "2026-06-15T12:00:00Z", **dev.make_child_artifacts_frozen(
        parent_idea_id="20260615-demo", decomposition_run_id=run_id, work_breakdown_sha256="c" * 64,
        children=[{"task_id": "taskA", "job_id": job_id, "idea_contract_sha256": ic_sha,
                   "product_plan_sha256": pp_sha}], frozen_at="2026-06-15T12:00:00Z")}


def _make_child_build(tmp_path: Path, *, n_files: int, n_checks: int):
    """A git build worktree for a decomposition child: n_files in-scope src files + a
    qa-verification.json with n_checks outputs. Returns (track_dir, base, job_id)."""
    if shutil.which("git") is None:
        pytest.skip("git not available")
    job_id = "20260615-demo-a"
    repo = tmp_path / "state" / "tracks" / job_id
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "product-plan.json").write_text(json.dumps(
        {"job_id": job_id, "allowed_paths": ["src/"], "planned_files": [], "non_goals": [],
         "verification_commands": ["pytest"], "acceptance_checks": ["x"], "risk_assessment": "low"}))
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    (repo / "src").mkdir()
    for i in range(n_files):
        (repo / "src" / f"f{i}.py").write_text(f"# file {i}\n")
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "work")
    (repo / "qa-verification.json").write_text(json.dumps(
        {"job_id": job_id, "ran_at": "2026-06-15T12:00:00Z", "commands_run": ["pytest"],
         "outputs": [{"command": "pytest", "exit_code": 0, "stdout_excerpt": str(i)} for i in range(n_checks)],
         "pass": True, "verifier": "qa"}))
    return repo, base, job_id


# --------------------------------------------------------------------------------------
# B2 — D6 size ceiling
# --------------------------------------------------------------------------------------
def test_b2_real_overcap_detected(tmp_path: Path, monkeypatch):
    track, base, job_id = _make_child_build(tmp_path, n_files=dp.FILE_CAP + 1, n_checks=dp.CHECK_CAP + 1)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(_child_frozen_event(job_id)) + "\n")
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    reason = gate_sweep.b2_decomposition_size_gate(track, str(track), base)
    assert reason is not None and "D6" not in reason  # the detail names files/checks over cap
    assert "ballooned" in reason


def test_b2_within_cap_passes(tmp_path: Path, monkeypatch):
    track, base, job_id = _make_child_build(tmp_path, n_files=2, n_checks=3)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(_child_frozen_event(job_id)) + "\n")
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    assert gate_sweep.b2_decomposition_size_gate(track, str(track), base) is None


def test_b2_unreadable_event_log_fails_closed(tmp_path: Path, monkeypatch):
    """An UNREADABLE event log can't prove 'not a child' -> HOLD, even within cap (Codex P5 r2)."""
    track, base, job_id = _make_child_build(tmp_path, n_files=2, n_checks=3)  # within cap
    def boom(_p):
        raise OSError("permission denied")
    monkeypatch.setattr(gate_sweep.dev, "read_events", boom)
    reason = gate_sweep.b2_decomposition_size_gate(track, str(track), base)
    assert reason is not None and "failing closed" in reason


def test_b2_non_decomposition_track_is_noop(tmp_path: Path, monkeypatch):
    """A normal (non-child) track has no child_artifacts_frozen -> D6 never applies, even over cap."""
    track, base, job_id = _make_child_build(tmp_path, n_files=dp.FILE_CAP + 5, n_checks=dp.CHECK_CAP + 5)
    events = tmp_path / "events.jsonl"
    events.write_text("")  # NO child_artifacts_frozen for this job
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    assert gate_sweep.b2_decomposition_size_gate(track, str(track), base) is None


def test_b2_blocks_qa_complete(tmp_path: Path, monkeypatch):
    """Headline B2 fail-closed: an over-cap decomposition child does NOT complete at the QA gate —
    even with a confirmed delta. process_qa_gate returns 'held' and never calls kanban `complete`."""
    track, base, job_id = _make_child_build(tmp_path, n_files=dp.FILE_CAP + 1, n_checks=dp.CHECK_CAP + 1)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(_child_frozen_event(job_id)) + "\n")
    monkeypatch.setattr(gate_sweep, "ATB_ROOT", tmp_path)   # so the track passes containment
    monkeypatch.setattr(gate_sweep, "EVENTS", events)

    calls = []
    monkeypatch.setattr(gate_sweep, "_kanban",
                        lambda *a, **k: calls.append(a) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))

    def fake_run_module(module, *margs):
        # run_verification -> rc 0 (passed); verification_delta -> confirmed.
        if module == "tools.verification_delta":
            return types.SimpleNamespace(returncode=0, stdout='{"state":"confirmed"}', stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gate_sweep, "_run_module", fake_run_module)

    result = gate_sweep.process_qa_gate("auto-build", "card1", str(track), str(track), None,
                                        base=base, dry_run=False, no_swap=True, emit=False)
    assert result == "held"
    assert not any(a[1] == "complete" for a in calls), "over-cap child must NOT be completed"


# --------------------------------------------------------------------------------------
# B3 — plan-gate re-verify of the D9-frozen child bytes
# --------------------------------------------------------------------------------------
def _built_child(tmp_path: Path):
    job_id = "20260615-demo-a"
    track = tmp_path / "state" / "tracks" / job_id
    track.mkdir(parents=True)
    ic = track / "idea-contract.json"
    pp = track / "product-plan.json"
    ic.write_text(json.dumps({"job_id": job_id, "parent_criteria_refs": ["c1"]}))
    pp.write_text(json.dumps({"job_id": job_id, "parent_criteria_refs": ["c1"]}))
    return track, job_id, dp._sha256_bytes(ic.read_bytes()), dp._sha256_bytes(pp.read_bytes())


def test_b3_reverify_matches_then_drifts(tmp_path: Path, monkeypatch):
    track, job_id, ic_sha, pp_sha = _built_child(tmp_path)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(_child_frozen_event(job_id, ic_sha, pp_sha)) + "\n")
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    monkeypatch.setattr(gate_sweep, "ATB_ROOT", tmp_path)  # the tmp track counts as in-repo
    assert gate_sweep.b3_plan_reverify(track) is None  # bytes match the frozen hashes
    # Drift the built product-plan after mint -> re-verify must HOLD.
    (track / "product-plan.json").write_text(json.dumps({"job_id": job_id, "tampered": True}))
    reason = gate_sweep.b3_plan_reverify(track)
    assert reason is not None and "drifted" in reason


def test_b3_outside_repo_child_holds(tmp_path: Path, monkeypatch):
    """Codex P5 HIGH: a known decomposition child whose track resolves OUTSIDE the repo must
    fail closed (HOLD) — never silently skip the reverify and proceed."""
    track, job_id, ic_sha, pp_sha = _built_child(tmp_path)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(_child_frozen_event(job_id, ic_sha, pp_sha)) + "\n")
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    # ATB_ROOT is some OTHER dir, so `track` (under tmp_path) is outside the repo.
    monkeypatch.setattr(gate_sweep, "ATB_ROOT", tmp_path / "elsewhere")
    reason = gate_sweep.b3_plan_reverify(track)
    assert reason is not None and "outside the repo" in reason


def test_b3_unreadable_event_log_fails_closed(tmp_path: Path, monkeypatch):
    """An unreadable event log -> B3 can't rule out a decomposition child -> HOLD (Codex P5 r2)."""
    track, job_id, ic_sha, pp_sha = _built_child(tmp_path)
    def boom(_p):
        raise OSError("permission denied")
    monkeypatch.setattr(gate_sweep.dev, "read_events", boom)
    monkeypatch.setattr(gate_sweep, "ATB_ROOT", tmp_path)
    reason = gate_sweep.b3_plan_reverify(track)
    assert reason is not None and "failing closed" in reason


def test_b3_non_child_is_noop(tmp_path: Path, monkeypatch):
    track, job_id, ic_sha, pp_sha = _built_child(tmp_path)
    events = tmp_path / "events.jsonl"
    events.write_text("")  # no freeze for this job
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    # A non-child is a no-op regardless of location (rec is None short-circuits before containment).
    assert gate_sweep.b3_plan_reverify(track) is None


def test_b3_blocks_plan_gate(tmp_path: Path, monkeypatch):
    """Headline B3 fail-closed: a child whose built contract bytes drifted from the D9-freeze is
    HELD at the plan gate before any review/build."""
    track, job_id, ic_sha, pp_sha = _built_child(tmp_path)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(_child_frozen_event(job_id, ic_sha, pp_sha)) + "\n")
    # Drift the contract so the frozen hashes no longer match.
    (track / "idea-contract.json").write_text(json.dumps({"job_id": job_id, "tampered": True}))
    monkeypatch.setattr(gate_sweep, "ATB_ROOT", tmp_path)
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    calls = []
    monkeypatch.setattr(gate_sweep, "_kanban",
                        lambda *a, **k: calls.append(a) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(gate_sweep, "run_review",
                        lambda *a, **k: pytest.fail("plan review must not run — B3 should HOLD first"))

    show = {"task": {"body": json.dumps(
        {"atb_gate": "plan", "track": str(track), "worktree": str(track), "base": "HEAD"})}}
    result = gate_sweep.process_gate("auto-build", "card1", show, dry_run=False, no_swap=True, emit=False)
    assert result == "held"
    assert not any(a[1] == "complete" for a in calls)


# --------------------------------------------------------------------------------------
# Plan-readiness build-time trust (2026-06-18): a B3-verified child trusts the hash-bound clearance
# instead of re-reviewing; a missing clearance HOLDs fail-closed (Codex design-review R2/R8).
# --------------------------------------------------------------------------------------
def _clearance_event(job_id, ic_sha, pp_sha, *, run_id="run1", wb_sha="c" * 64, task_id="taskA"):
    return {"ts": "2026-06-15T12:00:00Z", **dev.make_children_plan_cleared(
        parent_idea_id="20260615-demo", parent_idea_sha256="a" * 64, criteria_sha256="d" * 64,
        decomposition_run_id=run_id, work_breakdown_sha256=wb_sha,
        children=[{"task_id": task_id, "job_id": job_id, "idea_contract_sha256": ic_sha,
                   "product_plan_sha256": pp_sha, "verdict_id": "v" * 64}], cleared_at="2026-06-15T12:00:00Z")}


def _plan_card(track):
    return {"task": {"body": json.dumps({"atb_gate": "plan", "track": str(track), "worktree": str(track),
                                         "base": "HEAD", "swap_next": "coder"})}}


def test_plan_gate_verified_child_without_clearance_holds(tmp_path: Path, monkeypatch):
    """R2 fail-closed: a B3-verified decomposition child with NO plan-readiness clearance must HOLD —
    never fall through to a Codex review that could rc==0 and pass."""
    track, job_id, ic_sha, pp_sha = _built_child(tmp_path)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(_child_frozen_event(job_id, ic_sha, pp_sha)) + "\n")  # frozen, NO clearance
    monkeypatch.setattr(gate_sweep, "ATB_ROOT", tmp_path)
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    calls = []
    monkeypatch.setattr(gate_sweep, "_kanban",
                        lambda *a, **k: calls.append(a) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(gate_sweep, "run_review",
                        lambda *a, **k: pytest.fail("must not review — a missing clearance must HOLD"))
    result = gate_sweep.process_gate("auto-build", "card1", _plan_card(track), dry_run=False, no_swap=True, emit=False)
    assert result == "held"
    assert not any(a[1] == "complete" for a in calls)


def test_plan_gate_verified_child_with_clearance_passes_without_review(tmp_path: Path, monkeypatch):
    """R8: a B3-verified child WITH a matching clearance passes via the existing approve path WITHOUT a
    build-time Codex review (the hash-bound clearance is trusted)."""
    track, job_id, ic_sha, pp_sha = _built_child(tmp_path)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(_child_frozen_event(job_id, ic_sha, pp_sha)) + "\n"
                      + json.dumps(_clearance_event(job_id, ic_sha, pp_sha)) + "\n")
    monkeypatch.setattr(gate_sweep, "ATB_ROOT", tmp_path)
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    calls = []
    monkeypatch.setattr(gate_sweep, "_kanban",
                        lambda *a, **k: calls.append(a) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(gate_sweep, "run_review",
                        lambda *a, **k: pytest.fail("must NOT run a build-time review — clearance is trusted"))
    monkeypatch.setattr(gate_sweep, "_run_module",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))  # snapshot
    monkeypatch.setattr(gate_sweep, "_log_gate_metrics", lambda *a, **k: None)
    result = gate_sweep.process_gate("auto-build", "card1", _plan_card(track), dry_run=False, no_swap=True, emit=False)
    assert result == "pass"
    assert any(a[1] == "complete" for a in calls)  # child promoted to the coder


def test_b3_plan_reverify_detail_states(tmp_path: Path, monkeypatch):
    track, job_id, ic_sha, pp_sha = _built_child(tmp_path)
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(gate_sweep, "ATB_ROOT", tmp_path)
    monkeypatch.setattr(gate_sweep, "EVENTS", events)
    events.write_text("")  # no freeze -> not_child
    assert gate_sweep.b3_plan_reverify_detail(track)[0] == "not_child"
    events.write_text(json.dumps(_child_frozen_event(job_id, ic_sha, pp_sha)) + "\n")  # match -> verified + record
    status, payload = gate_sweep.b3_plan_reverify_detail(track)
    assert status == "verified" and payload["rec"]["child"]["job_id"] == job_id and "events" in payload
    (track / "product-plan.json").write_text(json.dumps({"job_id": job_id, "tampered": True}))  # drift -> hold
    assert gate_sweep.b3_plan_reverify_detail(track)[0] == "hold"
