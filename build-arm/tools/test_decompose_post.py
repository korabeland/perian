"""Phase 4 — post-emit gate: D9 (validate+freeze actual child files), the mint (TOCTOU close),
verify_built_child (plan-gate re-verify), D6 size recount, and the child-detection helpers.
Honest base + one mutation per check; the headline fail-closed cases are vacuous-child→D9 and
mutated-bytes→mint refusal.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import decompose_events as dev
from tools import decompose_fixtures as fx
from tools import decompose_post as dp


def _staged(h):
    return {tid: info["dir"] for tid, info in h["children"].items()}


def _d9(h):
    return dp.check_d9_and_freeze(h["work_breakdown_path"], _staged(h), h["events_path"],
                                  h["run_id"], h["now"], h["work_breakdown_sha256"])


# --------------------------------------------------------------------------------------
# D9
# --------------------------------------------------------------------------------------
def test_d9_honest_freezes_child_hashes(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h")
    res = _d9(h)
    assert res["ok"], res["violations"]
    frozen = dev.select(dev.read_events(h["events_path"]), dev.CHILD_ARTIFACTS_FROZEN, h["run_id"])
    assert len(frozen) == 1
    by_task = {c["task_id"]: c for c in frozen[0]["children"]}
    # the frozen hashes equal the on-disk staged bytes the fixture wrote
    for tid, info in h["children"].items():
        rec = by_task[tid]
        assert rec["idea_contract_sha256"] == info["idea_contract_sha256"]
        assert rec["product_plan_sha256"] == info["product_plan_sha256"]


def test_d9_vacuous_child_contract_fails(tmp_path: Path):
    """A child whose minted contract has empty/absent parent_criteria_refs is the produce-not-
    replace forgery -> D9 fails, NOTHING frozen."""
    cas = fx.honest_child_artifacts()
    cas["taskA"]["idea_contract"]["parent_criteria_refs"] = []
    h = fx.write_tree(tmp_path / "h", child_artifacts=cas)
    before = len(dev.read_events(h["events_path"]))
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9" for v in res["violations"])
    assert len(dev.read_events(h["events_path"])) == before  # no child_artifacts_frozen emitted


def test_d9_refs_not_equal_to_owned_coverage_fails(tmp_path: Path):
    cas = fx.honest_child_artifacts()
    cas["taskA"]["product_plan"]["parent_criteria_refs"] = ["c1"]  # owns c1,c2
    h = fx.write_tree(tmp_path / "h", child_artifacts=cas)
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9" for v in res["violations"])


def test_d9_schema_invalid_child_fails(tmp_path: Path):
    cas = fx.honest_child_artifacts()
    del cas["taskB"]["product_plan"]["verification_commands"]  # required by product-plan schema
    h = fx.write_tree(tmp_path / "h", child_artifacts=cas)
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9" for v in res["violations"])


def test_d9_job_id_mismatch_fails(tmp_path: Path):
    cas = fx.honest_child_artifacts()
    cas["taskA"]["product_plan"]["job_id"] = "different-job"
    h = fx.write_tree(tmp_path / "h", child_artifacts=cas)
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9" for v in res["violations"])


def test_d9_wb_drift_from_prebuild_hash_fails(tmp_path: Path):
    """D9 binds to the work-breakdown the pre-build gate ran on; a wb edited after D1-D8 -> fail."""
    h = fx.write_tree(tmp_path / "h")
    wb = json.loads(h["work_breakdown_path"].read_text())
    wb["drafted_at"] = "2026-06-15T23:59:59Z"  # any change to the bytes
    h["work_breakdown_path"].write_text(json.dumps(wb, indent=2, sort_keys=True) + "\n")
    res = _d9(h)  # _d9 still passes the ORIGINAL pre-build hash
    assert not res["ok"] and any("pre-build" in v["detail"] for v in res["violations"])


def test_d9_duplicate_child_job_id_fails(tmp_path: Path):
    """Two children minting to the same canonical track would collide -> reject, freeze nothing."""
    cas = fx.honest_child_artifacts()
    cas["taskB"]["idea_contract"]["job_id"] = "20260615-demo-a"  # collide with taskA
    cas["taskB"]["product_plan"]["job_id"] = "20260615-demo-a"
    h = fx.write_tree(tmp_path / "h", child_artifacts=cas)
    before = len(dev.read_events(h["events_path"]))
    res = _d9(h)
    assert not res["ok"] and any("collide" in v["detail"] for v in res["violations"])
    assert len(dev.read_events(h["events_path"])) == before


# --------------------------------------------------------------------------------------
# the mint (B3 TOCTOU close)
# --------------------------------------------------------------------------------------
def test_mint_copies_exactly_the_frozen_bytes(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h")
    res = _d9(h)
    assert res["ok"]
    dest_root = tmp_path / "tracks"
    r = dp.mint_children(res["event"], _staged(h), lambda job: dest_root / job)
    assert r["ok"], r
    for ch in res["event"]["children"]:
        dest = dest_root / ch["job_id"]
        assert dp._sha256_bytes((dest / "idea-contract.json").read_bytes()) == ch["idea_contract_sha256"]
        assert dp._sha256_bytes((dest / "product-plan.json").read_bytes()) == ch["product_plan_sha256"]


def test_mint_catches_mutated_bytes_post_d9(tmp_path: Path):
    """Headline B3 fail-closed: a staged child file changed AFTER D9 froze it is caught at mint —
    and the mint is atomic (NOTHING is written)."""
    h = fx.write_tree(tmp_path / "h")
    res = _d9(h)
    assert res["ok"]
    # Mutate taskA's staged idea-contract after the freeze.
    ic = h["children"]["taskA"]["idea_contract_path"]
    ic.write_bytes(ic.read_bytes() + b"\n# tampered\n")
    dest_root = tmp_path / "tracks"
    r = dp.mint_children(res["event"], _staged(h), lambda job: dest_root / job)
    assert not r["ok"] and "mutated post-D9" in r["reason"]
    # Atomic: no child track dir was written (verify-all-first refuses the whole mint).
    assert not dest_root.exists() or not any(dest_root.iterdir())


def test_mint_rolls_back_on_midway_write_failure(tmp_path: Path):
    """Codex P4 BLOCKER 1: if a later child's write fails, earlier minted children are rolled back
    (write-nothing). Force failure by pointing one child's dest under a regular file."""
    h = fx.write_tree(tmp_path / "h")
    res = _d9(h)
    assert res["ok"]
    children = res["event"]["children"]
    good_job = children[0]["job_id"]
    bad_job = children[1]["job_id"]
    dest_root = tmp_path / "tracks"
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir")  # mkdir under this -> NotADirectoryError (OSError)

    def dest_for(job):
        return (blocker / "sub") if job == bad_job else (dest_root / job)

    r = dp.mint_children(res["event"], _staged(h), dest_for)
    assert not r["ok"] and "rolled back" in r["reason"]
    # The earlier (good) child that DID get written must have been rolled back.
    assert not (dest_root / good_job).exists()


def test_mint_refuses_to_clobber_existing_canonical_track(tmp_path: Path):
    """A mint never overwrites pre-existing canonical data (children are NEW tracks)."""
    h = fx.write_tree(tmp_path / "h")
    res = _d9(h)
    dest_root = tmp_path / "tracks"
    childA = res["event"]["children"][0]
    pre = dest_root / childA["job_id"]
    pre.mkdir(parents=True)
    (pre / "idea-contract.json").write_text("PRE-EXISTING — must survive")
    r = dp.mint_children(res["event"], _staged(h), lambda job: dest_root / job)
    assert not r["ok"] and "create-only" in r["reason"]
    assert (pre / "idea-contract.json").read_text() == "PRE-EXISTING — must survive"


# --------------------------------------------------------------------------------------
# verify_built_child (plan-gate re-verify, B3)
# --------------------------------------------------------------------------------------
def test_verify_built_child_matches_then_drifts(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h")
    res = _d9(h)
    dest_root = tmp_path / "tracks"
    dp.mint_children(res["event"], _staged(h), lambda job: dest_root / job)
    childA = next(c for c in res["event"]["children"] if c["task_id"] == "taskA")
    built = dest_root / childA["job_id"]
    assert dp.verify_built_child(built, childA)
    # Drift the built contract after mint -> re-verify must fail.
    pp = built / "product-plan.json"
    pp.write_bytes(pp.read_bytes() + b"\n")
    assert not dp.verify_built_child(built, childA)


# --------------------------------------------------------------------------------------
# child detection (B2/B3 gate_sweep helpers)
# --------------------------------------------------------------------------------------
def test_frozen_child_record_and_is_child(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h")
    _d9(h)  # side effect: emits the child_artifacts_frozen event the helpers read
    events = dev.read_events(h["events_path"])
    assert dp.is_decomposition_child(events, "20260615-demo-a")
    rec = dp.frozen_child_record(events, "20260615-demo-a")
    assert rec["child"]["task_id"] == "taskA"
    assert not dp.is_decomposition_child(events, "some-normal-track")
    assert dp.frozen_child_record(events, "some-normal-track") is None


def test_frozen_child_record_returns_latest_freeze(tmp_path: Path):
    """Codex final-review caveat: on a re-freeze of the same job_id (D9 emitted, mint failed,
    retried with new bytes), the LATEST event wins — matching the stale first would false-HOLD B3."""
    job = "20260615-demo-a"
    stale = dev.make_child_artifacts_frozen(
        parent_idea_id="p", decomposition_run_id="r1", work_breakdown_sha256="c" * 64,
        children=[{"task_id": "taskA", "job_id": job, "idea_contract_sha256": "1" * 64,
                   "product_plan_sha256": "2" * 64}], frozen_at="2026-06-15T12:00:00Z")
    fresh = dev.make_child_artifacts_frozen(
        parent_idea_id="p", decomposition_run_id="r2", work_breakdown_sha256="d" * 64,
        children=[{"task_id": "taskA", "job_id": job, "idea_contract_sha256": "9" * 64,
                   "product_plan_sha256": "8" * 64}], frozen_at="2026-06-15T13:00:00Z")
    events = tmp_path / "e.jsonl"
    events.write_text(json.dumps({"ts": "t1", **stale}) + "\n" + json.dumps({"ts": "t2", **fresh}) + "\n")
    rec = dp.frozen_child_record(dev.read_events(events), job)
    assert rec["child"]["idea_contract_sha256"] == "9" * 64  # the fresh (latest) freeze, not the stale


# --------------------------------------------------------------------------------------
# D6 size ceiling
# --------------------------------------------------------------------------------------
def test_d6_over_cap_pure():
    assert dp.d6_over_cap(dp.FILE_CAP, dp.CHECK_CAP) == []
    assert any(v["check"] == "D6" for v in dp.d6_over_cap(dp.FILE_CAP + 1, dp.CHECK_CAP))
    assert any(v["check"] == "D6" for v in dp.d6_over_cap(dp.FILE_CAP, dp.CHECK_CAP + 1))
    # both over -> two violations
    assert len(dp.d6_over_cap(dp.FILE_CAP + 5, dp.CHECK_CAP + 5)) == 2


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_actual_in_scope_file_count_from_git(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    # product-plan.json with allowed_paths = src/; base commit holds only the plan.
    (repo / "product-plan.json").write_text(json.dumps(
        {"job_id": "x", "allowed_paths": ["src/"], "planned_files": [], "non_goals": [],
         "verification_commands": ["pytest"], "acceptance_checks": ["x"], "risk_assessment": "low"}))
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "base")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    # Two in-scope files + one out-of-scope file, then commit.
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a")
    (repo / "src" / "b.py").write_text("b")
    (repo / "rogue.py").write_text("escaped")
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "work")
    # 3 changed files; 1 escapes allowed_paths -> 2 in scope.
    assert dp.actual_in_scope_file_count(repo, repo, base) == 2


# --------------------------------------------------------------------------------------
# D9 plan-readiness binding (2026-06-18): frozen => plan-cleared, hash-bound to THIS run (R3/R4/R6).
# --------------------------------------------------------------------------------------
def _append_event(events_path, payload, ts="2026-06-15T12:00:00Z"):
    with open(events_path, "a") as fh:
        fh.write(json.dumps({"ts": ts, **payload}) + "\n")


def _clearance_for(h, *, run_id=None, children=None, criteria_sha256=None):
    """A children_plan_cleared matching tree h; override one piece to forge exactly one thing."""
    kids = children if children is not None else [
        {"task_id": tid, "job_id": info["job_id"],
         "idea_contract_sha256": info["idea_contract_sha256"],
         "product_plan_sha256": info["product_plan_sha256"], "verdict_id": "pcv-" + info["job_id"]}
        for tid, info in h["children"].items()]
    return dev.make_children_plan_cleared(
        parent_idea_id=h["parent_idea_id"], parent_idea_sha256="a" * 64,
        criteria_sha256=criteria_sha256 if criteria_sha256 is not None else h["criteria_sha256"],
        decomposition_run_id=run_id if run_id is not None else h["run_id"],
        work_breakdown_sha256=h["work_breakdown_sha256"], children=kids, cleared_at=h["now"])


def test_d9_no_clearance_fails_closed(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h", emit_plan_cleared=False)
    before = len(dev.read_events(h["events_path"]))
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9-PC" for v in res["violations"])
    assert len(dev.read_events(h["events_path"])) == before  # no freeze emitted


def test_d9_mismatched_clearance_bytes_fail(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h", emit_plan_cleared=False)
    bad = _clearance_for(h)
    bad["children"][0]["product_plan_sha256"] = "0" * 64  # cleared bytes != staged/frozen bytes
    _append_event(h["events_path"], bad)
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9-PC" for v in res["violations"])


def test_d9_stale_run_clearance_fails(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h", emit_plan_cleared=False)
    _append_event(h["events_path"], _clearance_for(h, run_id="some-other-run"))
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9-PC" for v in res["violations"])


def test_d9_cardinality_mismatch_fails(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h", emit_plan_cleared=False)
    full = _clearance_for(h)["children"]
    _append_event(h["events_path"], _clearance_for(h, children=full[:-1]))  # covers 2 of 3 children
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9-PC" for v in res["violations"])


def test_d9_wrong_criteria_universe_fails(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h", emit_plan_cleared=False)
    _append_event(h["events_path"], _clearance_for(h, criteria_sha256="9" * 64))
    res = _d9(h)
    assert not res["ok"] and any(v["check"] == "D9-PC" for v in res["violations"])


def test_d9_latest_clearance_wins_on_rerun(tmp_path: Path):
    """A stale (bad) clearance then a fresh (good) one for the same run+wb -> D9 uses the latest and
    freezes (re-run recovery; mirrors frozen_child_record latest-wins)."""
    h = fx.write_tree(tmp_path / "h", emit_plan_cleared=False)
    stale = _clearance_for(h)
    stale["children"][0]["product_plan_sha256"] = "0" * 64
    _append_event(h["events_path"], stale, ts="2026-06-15T11:00:00Z")
    _append_event(h["events_path"], _clearance_for(h), ts="2026-06-15T11:30:00Z")  # fresh + correct
    assert _d9(h)["ok"], _d9(h)["violations"]


def test_plan_clearance_record_binds_run_and_child(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h")          # honest tree emits a matching clearance
    assert _d9(h)["ok"]                        # ...and now a freeze
    events = dev.read_events(h["events_path"])
    rec = dp.frozen_child_record(events, h["children"]["taskA"]["job_id"])
    assert rec is not None
    cl = dp.plan_clearance_record(events, rec["event"], rec["child"])
    assert cl is not None and cl["job_id"] == h["children"]["taskA"]["job_id"]
    # a frozen child re-pointed at a DIFFERENT run has no clearance here (no cross-run replay)
    forged = dict(rec["event"], decomposition_run_id="other-run")
    assert dp.plan_clearance_record(events, forged, rec["child"]) is None
