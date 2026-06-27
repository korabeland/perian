"""Phase 3 — decompose_check D0-D8 (pre-build). Adversarial fixtures: honest base, one mutation
per check. Each test pins exactly which gate catches which forgery, and proves the gate reads a
TRUSTED record (the wrapper event / the rehashed bytes), never the doer's self-description.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools import decompose_check as dc
from tools import decompose_fixtures as fx


def _run(h):
    return dc.check_decomposition(h["work_breakdown_path"], h["parent_criteria_path"],
                                  h["events_path"], h["run_id"])


def _checks(res):
    return {v["check"] for v in res["violations"]}


# --------------------------------------------------------------------------------------
# Honest baseline
# --------------------------------------------------------------------------------------
def test_honest_tree_passes(tmp_path: Path):
    res = _run(fx.write_tree(tmp_path / "h"))
    assert res["ok"], res["violations"]


def test_self_test_passes():
    assert dc._self_test() == 0


# --------------------------------------------------------------------------------------
# D1 — freeze integrity (the trusted record is the wrapper event + the rehashed bytes)
# --------------------------------------------------------------------------------------
def test_d1_missing_freeze_event(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h", emit_criteria_frozen=False)
    res = _run(h)
    assert not res["ok"] and _checks(res) == {"D1"}


def test_d1_moved_or_edited_criteria_after_freeze(tmp_path: Path):
    """Editing parent-criteria.json after the freeze makes its bytes != the frozen hash -> D1."""
    h = fx.write_tree(tmp_path / "h")
    pc = json.loads(h["parent_criteria_path"].read_text())
    pc["criteria"][0]["text"] = "secretly reworded after freeze"
    h["parent_criteria_path"].write_text(json.dumps(pc, indent=2, sort_keys=True) + "\n")
    res = _run(h)
    assert not res["ok"] and _checks(res) == {"D1"}


def test_d1_stale_run_id(tmp_path: Path):
    """A freeze exists for run1 but the gate is asked about a different run -> no trusted record."""
    h = fx.write_tree(tmp_path / "h", run_id="run1")
    res = dc.check_decomposition(h["work_breakdown_path"], h["parent_criteria_path"],
                                 h["events_path"], "run-DIFFERENT")
    assert not res["ok"] and _checks(res) == {"D1"}


def test_d1_doer_pointer_disagrees_with_frozen_bytes(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    wb["parent_criteria_ref"]["sha256"] = "9" * 64  # doer points at bytes that aren't the frozen ones
    h = fx.write_tree(tmp_path / "h", work_breakdown=wb)
    res = _run(h)
    assert not res["ok"] and _checks(res) == {"D1"}


# --------------------------------------------------------------------------------------
# D2 — coverage = exact partition
# --------------------------------------------------------------------------------------
def test_d2_frozen_criterion_with_no_owner(tmp_path: Path):
    """A frozen criterion (c5) that no child owns -> coverage isn't an exact partition. (A child
    owning ZERO is instead a schema/minItems reject — D0 — so the partition miss is exercised by
    an unowned criterion, not an empty owner.)"""
    pc = fx.honest_parent_criteria()
    pc["criteria"].append({"id": "c5", "text": "an obligation nobody picked up"})
    res = _run(fx.write_tree(tmp_path / "h", parent_criteria=pc))  # coverage still only c1-c4
    assert not res["ok"] and "D2" in _checks(res)


def test_d2_double_owned_criterion(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    wb["coverage"].append({"criterion_id": "c1", "owner_task_id": "taskB"})  # c1 owned twice
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D2" in _checks(res)


def test_d2_child_refs_disagree_with_coverage(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    for c in wb["children"]:
        if c["task_id"] == "taskA":
            c["parent_criteria_refs"] = ["c1"]  # coverage says taskA owns c1+c2
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D2" in _checks(res)


# --------------------------------------------------------------------------------------
# D3 — owner validity
# --------------------------------------------------------------------------------------
def test_d3_owner_not_a_declared_child(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    wb["coverage"][0]["owner_task_id"] = "ghostTask"
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D3" in _checks(res)


# --------------------------------------------------------------------------------------
# D4 — acyclic + buildable
# --------------------------------------------------------------------------------------
def test_d4_cycle(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    by = {c["task_id"]: c for c in wb["children"]}
    by["taskA"]["depends_on"] = ["taskC"]  # A<-C, B<-A, C<-B  => cycle
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D4" in _checks(res)


def test_d4_dangling_dependency(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    by = {c["task_id"]: c for c in wb["children"]}
    by["taskB"]["depends_on"] = ["taskA", "ghost"]
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D4" in _checks(res)


# --------------------------------------------------------------------------------------
# D5 — dependency closure (wrong-producer + laundered-external)
# --------------------------------------------------------------------------------------
def test_d5_wrong_producer_unordered(tmp_path: Path):
    """taskB requires taskC's output but doesn't depend on taskC -> producer outside closure."""
    wb = fx.honest_work_breakdown()
    for c in wb["children"]:
        if c["task_id"] == "taskB":
            c["requires"] = ["taskA.parsed", "taskC.attest"]
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D5" in _checks(res)


def test_d5_laundered_external_input(tmp_path: Path):
    """taskA requires an id that is neither produced nor a declared external input."""
    wb = fx.honest_work_breakdown()
    for c in wb["children"]:
        if c["task_id"] == "taskA":
            c["requires"] = ["ext.bogus"]
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D5" in _checks(res)


def test_d5_produce_under_wrong_namespace(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    for c in wb["children"]:
        if c["task_id"] == "taskA":
            c["produces"] = ["taskB.parsed"]  # taskA claims to produce under taskB's namespace
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D5" in _checks(res)


def test_d5_reserved_external_kinds_hold(tmp_path: Path):
    """sweep-fetch / frozen-verdict are reserved in the schema but NOT soundly bindable yet, so
    D5 fails closed on them — mere event presence is not provenance (Codex P3 r2)."""
    for sr in (
        {"kind": "frozen-verdict", "verdict_id": "v-deadbeef", "sha256": "a" * 64},
        {"kind": "sweep-fetch", "anchor": "fetch://x", "event_id": "evt-1"},
    ):
        wb = fx.honest_work_breakdown()
        wb["external_inputs"] = [{"id": "ext.dep", "source_ref": sr}]
        for c in wb["children"]:
            if c["task_id"] == "taskA":
                c["requires"] = ["ext.dep"]
        # Even if a verdict with that id is present, it must NOT pass (presence != attestation).
        res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb,
                                 extra_events=[{"event": "codex-review", "verdict_id": "v-deadbeef",
                                                "ts": "2026-06-15T12:00:00Z"}]))
        assert not res["ok"] and "D5" in _checks(res), sr


def test_d5_git_external_verified_against_repo(tmp_path: Path):
    """git external source: the blob content at commit:path must hash to the declared sha256 in
    the overseer-controlled repo. Real sha passes; a forged sha HOLDs."""
    import shutil
    if shutil.which("git") is None:
        import pytest
        pytest.skip("git not available")
    real = dc._git_blob_sha256(dc.ATB_ROOT, "HEAD", "README.md")
    if real is None:
        import pytest
        pytest.skip("README.md not resolvable at HEAD in this repo")

    def build(sha):
        wb = fx.honest_work_breakdown()
        wb["external_inputs"] = [{"id": "ext.readme",
                                  "source_ref": {"kind": "git", "commit": "HEAD",
                                                 "path": "README.md", "sha256": sha}}]
        for c in wb["children"]:
            if c["task_id"] == "taskA":
                c["requires"] = ["ext.readme"]
        return fx.write_tree(tmp_path / sha[:6], work_breakdown=wb)

    assert _run(build(real))["ok"]                       # real content hash -> verified
    assert not _run(build("0" * 64))["ok"]               # forged hash -> HOLD


def test_d5_internal_product_cannot_be_laundered_external(tmp_path: Path):
    """Codex P3 BLOCKER 2: taskB requires taskA.parsed (a real internal product) WITHOUT depending
    on taskA, and tries to launder it as an external input. Producer-first still HOLDs (unordered)."""
    wb = fx.honest_work_breakdown()
    wb["external_inputs"] = [{"id": "taskA.parsed",
                              "source_ref": {"kind": "frozen-verdict", "verdict_id": "ext-vid-1",
                                             "sha256": "a" * 64}}]
    for c in wb["children"]:
        if c["task_id"] == "taskB":
            c["depends_on"] = []           # drop the real ordering
            c["requires"] = ["taskA.parsed"]
    h = fx.write_tree(tmp_path / "h", work_breakdown=wb,
                      extra_events=[{"event": "codex-review", "verdict_id": "ext-vid-1", "ts": "2026-06-15T12:00:00Z"}])
    res = _run(h)
    assert not res["ok"] and "D5" in _checks(res)


# --------------------------------------------------------------------------------------
# D7 — large-child guard (the override must be a wrapper event bound to THIS run+wb)
# --------------------------------------------------------------------------------------
def test_d7_large_child_without_override_fails(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    for c in wb["children"]:
        if c["task_id"] == "taskA":
            c["size_estimate"]["new_loc"] = 300  # -> derived L
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and "D7" in _checks(res)


def test_d7_large_child_with_matching_override_passes(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    for c in wb["children"]:
        if c["task_id"] == "taskA":
            c["size_estimate"]["new_loc"] = 300
    h = fx.write_tree(tmp_path / "h", work_breakdown=wb, large_override_tasks=["taskA"])
    res = _run(h)
    assert res["ok"], res["violations"]


def test_d7_stale_override_after_wb_change_fails(tmp_path: Path):
    """An override granted for an EARLIER work-breakdown is stale once the wb bytes change."""
    wb = fx.honest_work_breakdown()
    for c in wb["children"]:
        if c["task_id"] == "taskA":
            c["size_estimate"]["new_loc"] = 300
    h = fx.write_tree(tmp_path / "h", work_breakdown=wb, large_override_tasks=["taskA"])
    # Mutate the work-breakdown AFTER the override was emitted -> override.work_breakdown_sha256 stale.
    wb2 = json.loads(h["work_breakdown_path"].read_text())
    for c in wb2["children"]:
        if c["task_id"] == "taskA":
            c["size_estimate"]["new_loc"] = 305  # still L, but different bytes
    h["work_breakdown_path"].write_text(json.dumps(wb2, indent=2, sort_keys=True) + "\n")
    res = _run(h)
    assert not res["ok"] and "D7" in _checks(res)


# --------------------------------------------------------------------------------------
# D8 — provenance (doer != certifier; certifier read from the wrapper event)
# --------------------------------------------------------------------------------------
def test_d8_author_equals_certifier_fails(tmp_path: Path):
    h = fx.write_tree(tmp_path / "h", author_as_certifier=True)  # frozen_by := authored_by
    res = _run(h)
    assert not res["ok"] and "D8" in _checks(res)


# --------------------------------------------------------------------------------------
# Structural integrity (Codex P3 BLOCKER 3 / HIGH / MED)
# --------------------------------------------------------------------------------------
def test_duplicate_task_ids_rejected(tmp_path: Path):
    """Two children sharing a task_id collapse ownership/closure/override binding -> reject."""
    wb = fx.honest_work_breakdown()
    wb["children"][1]["task_id"] = "taskA"  # taskB renamed to a duplicate taskA
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and _checks(res) == {"D3"}


def test_parent_idea_id_mismatch_fails_d1(tmp_path: Path):
    """The work-breakdown's parent_idea_id must match the criteria file + the freeze event."""
    wb = fx.honest_work_breakdown()
    wb["parent_idea_id"] = "20260615-DIFFERENT"
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and _checks(res) == {"D1"}


def test_d2_duplicate_refs_within_child(tmp_path: Path):
    pc = fx.honest_parent_criteria()  # c1..c4
    wb = fx.honest_work_breakdown()
    for c in wb["children"]:
        if c["task_id"] == "taskA":
            c["parent_criteria_refs"] = ["c1", "c1", "c2"]  # duplicate c1 (still 'covers' c1,c2)
    res = _run(fx.write_tree(tmp_path / "h", parent_criteria=pc, work_breakdown=wb))
    assert not res["ok"] and "D2" in _checks(res)


# --------------------------------------------------------------------------------------
# D0 — schema gate + fail-safe
# --------------------------------------------------------------------------------------
def test_d0_schema_invalid_work_breakdown(tmp_path: Path):
    wb = fx.honest_work_breakdown()
    del wb["children"][0]["allowed_paths"]  # schema-invalid child
    res = _run(fx.write_tree(tmp_path / "h", work_breakdown=wb))
    assert not res["ok"] and _checks(res) == {"D0"}


def test_indeterminate_on_unreadable_inputs(tmp_path: Path):
    import pytest
    with pytest.raises(dc.Indeterminate):
        dc.check_decomposition(tmp_path / "nope.json", tmp_path / "nope2.json",
                               tmp_path / "events.jsonl", "run1")
