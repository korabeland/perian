"""Phase 1 — the plan-readiness pre-clearance gate (decompose_plan_gate).

Honest base (decompose_fixtures.write_tree) + one mutation per check. The headline fail-closed cases:
ONE held child -> no clearance; review error -> no clearance; malformed staged ic -> no clearance;
criteria-ref mismatch -> no clearance. The headline positive: all-approve emits a clearance whose
per-child hashes EQUAL the staged bytes (reviewed == hashed) and that binds the run's criteria universe.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools import decompose_events as dev
from tools import decompose_fixtures as fx
from tools import decompose_plan_gate as pg


def _tree(path, **kw):
    """The plan-gate tests exercise clear_child_plans EMITTING the clearance, so they start from a tree
    with no pre-emitted one (the honest fixture default emits one — that's for the D9 tests)."""
    kw.setdefault("emit_plan_cleared", False)
    return fx.write_tree(path, **kw)


def _staged(h):
    return {tid: info["dir"] for tid, info in h["children"].items()}


def _approve_all(prompt):
    return {"verdict": "approve", "reason": "ok", "findings": [], "confidence": "high"}


def _kw(h):
    return dict(work_breakdown_path=h["work_breakdown_path"], parent_idea_path=h["parent_idea_path"],
               criteria_path=h["parent_criteria_path"], staged_dirs=_staged(h),
               decomposition_run_id=h["run_id"], events_path=h["events_path"], now_iso=h["now"])


def _count(h):
    return len(dev.read_events(h["events_path"]))


# --------------------------------------------------------------------------------------
# Positive: all-approve emits a well-formed, hash-bound, run-bound clearance
# --------------------------------------------------------------------------------------
def test_all_approve_emits_clearance_binding_staged_hashes(tmp_path: Path):
    h = _tree(tmp_path / "h")
    res = pg.clear_child_plans(**_kw(h), review_fn=_approve_all)
    assert res["ok"], res
    ev = res["event"]
    assert dev.is_well_formed(ev)
    assert ev["event"] == dev.CHILDREN_PLAN_CLEARED
    # binds the run's criteria universe
    assert ev["criteria_sha256"] == h["criteria_sha256"]
    assert ev["decomposition_run_id"] == h["run_id"]
    assert ev["work_breakdown_sha256"] == h["work_breakdown_sha256"]
    # per-child hashes EQUAL the staged bytes the fixture wrote (reviewed == hashed == will-be-frozen)
    by_task = {c["task_id"]: c for c in ev["children"]}
    assert set(by_task) == set(h["children"])
    for tid, info in h["children"].items():
        assert by_task[tid]["idea_contract_sha256"] == info["idea_contract_sha256"]
        assert by_task[tid]["product_plan_sha256"] == info["product_plan_sha256"]
        assert by_task[tid]["job_id"]
        assert len(by_task[tid]["verdict_id"]) == 64  # a real sha256 of the structured verdict
    # the clearance is selectable by run id
    sel = dev.select(dev.read_events(h["events_path"]), dev.CHILDREN_PLAN_CLEARED, h["run_id"])
    assert len(sel) == 1


def test_reviewed_content_is_the_staged_product_plan(tmp_path: Path):
    """R1: the review sees the exact staged product-plan bytes we hash — prove the staged content
    reaches the prompt and the clearance hash equals the on-disk file hash."""
    h = _tree(tmp_path / "h")
    seen = []

    def capture(prompt):
        seen.append(prompt)
        return _approve_all(prompt)

    res = pg.clear_child_plans(**_kw(h), review_fn=capture)
    assert res["ok"]
    joined = "\n".join(seen)
    # the fixture stamps each job_id into its product-plan; it must appear in the reviewed prompt
    assert "20260615-demo-a" in joined and "20260615-demo-b" in joined
    a = next(c for c in res["event"]["children"] if c["task_id"] == "taskA")
    assert a["product_plan_sha256"] == h["children"]["taskA"]["product_plan_sha256"]


def test_verdict_id_reflects_the_verdict(tmp_path: Path):
    h = _tree(tmp_path / "h")
    r1 = pg.review_one_child(b'{"job_id":"j","what_should_exist":"x"}', b'{"plan":1}',
                             lambda p: {"verdict": "approve", "reason": "a", "findings": [], "confidence": "high"})
    r2 = pg.review_one_child(b'{"job_id":"j","what_should_exist":"x"}', b'{"plan":1}',
                             lambda p: {"verdict": "approve", "reason": "DIFFERENT", "findings": [], "confidence": "high"})
    assert r1["verdict_id"] != r2["verdict_id"]  # content-addressed, not a constant


# --------------------------------------------------------------------------------------
# Fail-closed: every non-all-approve path emits NOTHING
# --------------------------------------------------------------------------------------
def test_one_held_child_emits_nothing(tmp_path: Path):
    h = _tree(tmp_path / "h")
    before = _count(h)

    def hold_b(prompt):
        if "20260615-demo-b" in prompt:
            return {"verdict": "needs_changes", "reason": "gap", "findings": ["fix b"], "confidence": "high"}
        return _approve_all(prompt)

    res = pg.clear_child_plans(**_kw(h), review_fn=hold_b)
    assert not res["ok"]
    assert _count(h) == before  # no clearance line
    held = [r for r in res["results"] if not r["approved"]]
    assert len(held) == 1 and held[0]["task_id"] == "taskB" and held[0]["findings"] == ["fix b"]


def test_review_error_emits_nothing(tmp_path: Path):
    h = _tree(tmp_path / "h")
    before = _count(h)

    def boom(prompt):
        raise RuntimeError("codex down")

    res = pg.clear_child_plans(**_kw(h), review_fn=boom)
    assert not res["ok"]
    assert _count(h) == before


def test_malformed_staged_idea_contract_emits_nothing(tmp_path: Path):
    h = _tree(tmp_path / "h")
    before = _count(h)
    (h["children"]["taskA"]["dir"] / "idea-contract.json").write_text("{not json")
    res = pg.clear_child_plans(**_kw(h), review_fn=_approve_all)
    assert not res["ok"]
    assert _count(h) == before


def test_criteria_ref_mismatch_emits_nothing(tmp_path: Path):
    """The work-breakdown points its parent_criteria_ref at a DIFFERENT criteria sha than the criteria
    file -> clearing would bind the wrong criteria universe -> refuse, emit nothing."""
    bad_wb = fx.honest_work_breakdown()
    bad_wb["parent_criteria_ref"]["sha256"] = "f" * 64  # not the placeholder -> fixture won't fix it up
    h = _tree(tmp_path / "h", work_breakdown=bad_wb)
    before = _count(h)
    res = pg.clear_child_plans(**_kw(h), review_fn=_approve_all)
    assert not res["ok"] and "criteria" in res["reason"]
    assert _count(h) == before


def test_missing_staged_dir_emits_nothing(tmp_path: Path):
    h = _tree(tmp_path / "h")
    before = _count(h)
    staged = _staged(h)
    staged.pop("taskC")  # a child with no staged dir
    kw = {**_kw(h), "staged_dirs": staged}
    res = pg.clear_child_plans(**kw, review_fn=_approve_all)
    assert not res["ok"]
    assert _count(h) == before


def test_cli_exit_codes(tmp_path: Path, monkeypatch, capsys):
    """The CLI exits 0 when cleared and 2 when a child holds (fail-safe)."""
    h = _tree(tmp_path / "h")
    staged_root = h["children"]["taskA"]["dir"].parent  # .../staged-children/

    # force the in-process reviewer to a deterministic approve, no Codex
    monkeypatch.setattr(pg, "_default_review_fn", _approve_all)
    argv = ["--work-breakdown", str(h["work_breakdown_path"]), "--parent-idea", str(h["parent_idea_path"]),
            "--criteria", str(h["parent_criteria_path"]), "--staged-root", str(staged_root),
            "--run-id", h["run_id"], "--events", str(h["events_path"])]
    assert pg.main(argv) == 0

    # a held child -> exit 2, still nothing emitted beyond the first clearance
    monkeypatch.setattr(pg, "_default_review_fn",
                        lambda p: {"verdict": "deny", "reason": "no", "findings": [], "confidence": "high"})
    h2 = _tree(tmp_path / "h2")
    staged_root2 = h2["children"]["taskA"]["dir"].parent
    before = _count(h2)
    argv2 = ["--work-breakdown", str(h2["work_breakdown_path"]), "--parent-idea", str(h2["parent_idea_path"]),
             "--criteria", str(h2["parent_criteria_path"]), "--staged-root", str(staged_root2),
             "--run-id", h2["run_id"], "--events", str(h2["events_path"])]
    assert pg.main(argv2) == 2
    assert _count(h2) == before
