"""Phase 2 — the shared decomposition event contract (drift guard).

These tests pin the property the whole step rests on: the EMITTER builders and the READER
helpers agree on exactly the same keys, so a gate can never silently read a missing key as a
pass. They also pin that a partial / malformed event line is never returned as a trusted record.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools import decompose_events as dev


def _criteria_frozen(run_id="run1"):
    return dev.make_criteria_frozen(
        parent_idea_id="p1", parent_idea_sha256="a" * 64, criteria_sha256="b" * 64,
        decomposition_run_id=run_id, verdict_id="v1", frozen_at="2026-06-15T00:00:00Z",
    )


def test_builders_emit_well_formed_events():
    assert dev.is_well_formed(_criteria_frozen())
    assert dev.is_well_formed(dev.make_large_override(
        parent_idea_id="p1", parent_idea_sha256="a" * 64, criteria_sha256="b" * 64,
        decomposition_run_id="run1", work_breakdown_sha256="c" * 64, task_id="taskA",
        derived_bucket="L", reason="irreducible", verdict_id="v1", granted_at="2026-06-15T00:00:00Z",
    ))
    assert dev.is_well_formed(dev.make_child_artifacts_frozen(
        parent_idea_id="p1", decomposition_run_id="run1", work_breakdown_sha256="c" * 64,
        children=[{"task_id": "taskA", "job_id": "p1-a", "idea_contract_sha256": "d" * 64,
                   "product_plan_sha256": "e" * 64}],
        frozen_at="2026-06-15T00:00:00Z",
    ))


def test_child_artifacts_frozen_rejects_empty_or_malformed_children():
    base = dict(parent_idea_id="p1", decomposition_run_id="run1", work_breakdown_sha256="c" * 64,
                frozen_at="2026-06-15T00:00:00Z")
    assert not dev.is_well_formed(dev.make_child_artifacts_frozen(children=[], **base))
    assert not dev.is_well_formed(dev.make_child_artifacts_frozen(
        children=[{"task_id": "taskA"}], **base))  # child entry missing the hash/job keys


def test_required_keys_match_builder_output_exactly():
    """The reader's REQUIRED_KEYS must be a subset of what the builder emits — otherwise a
    gate reads a key the emitter never wrote. (Catches emitter/reader drift at the source.)"""
    ev = _criteria_frozen()
    assert dev.REQUIRED_KEYS[dev.CRITERIA_FROZEN] <= set(ev.keys())
    lo = dev.make_large_override(
        parent_idea_id="p1", parent_idea_sha256="a" * 64, criteria_sha256="b" * 64,
        decomposition_run_id="run1", work_breakdown_sha256="c" * 64, task_id="taskA",
        derived_bucket="L", reason="r", verdict_id="v1", granted_at="2026-06-15T00:00:00Z",
    )
    assert dev.REQUIRED_KEYS[dev.LARGE_OVERRIDE] <= set(lo.keys())


def test_partial_event_is_not_well_formed():
    ev = _criteria_frozen()
    del ev["criteria_sha256"]
    assert not dev.is_well_formed(ev)
    assert not dev.is_well_formed({"event": "criteria_frozen"})
    assert not dev.is_well_formed({"event": "unknown_event", "x": 1})
    assert not dev.is_well_formed("not a dict")


def test_read_events_skips_malformed_lines(tmp_path: Path):
    p = tmp_path / "events.jsonl"
    p.write_text(
        json.dumps(_criteria_frozen()) + "\n"
        + "not json at all\n"
        + "\n"
        + json.dumps(["a", "list", "not", "an", "object"]) + "\n"
        + json.dumps({"event": "other", "x": 1}) + "\n"
    )
    events = dev.read_events(p)
    # 2 object lines survive: the criteria_frozen + the 'other' object. The bad-json line, the
    # blank line, and the JSON *list* (not an object) are all dropped.
    assert len(events) == 2
    assert dev.read_events(tmp_path / "missing.jsonl") == []


def test_derive_complexity_boundaries_and_conservative_fail():
    assert dev.derive_complexity(0) == "S"
    assert dev.derive_complexity(dev.S_MAX_LOC) == "S"
    assert dev.derive_complexity(dev.S_MAX_LOC + 1) == "M"
    assert dev.derive_complexity(dev.M_MAX_LOC) == "M"
    assert dev.derive_complexity(dev.M_MAX_LOC + 1) == "L"
    # Conservative: any malformed/negative size buckets to L (more oversight, never less).
    for bad in (-1, None, "120", 3.5, True):
        assert dev.derive_complexity(bad) == "L"


def _children_plan_cleared(run_id="run1"):
    return dev.make_children_plan_cleared(
        parent_idea_id="p1", parent_idea_sha256="a" * 64, criteria_sha256="b" * 64,
        decomposition_run_id=run_id, work_breakdown_sha256="c" * 64,
        children=[{"task_id": "taskA", "job_id": "p1-a", "idea_contract_sha256": "d" * 64,
                   "product_plan_sha256": "e" * 64, "verdict_id": "f" * 64}],
        cleared_at="2026-06-18T00:00:00Z",
    )


def test_children_plan_cleared_well_formed_and_rejects_partial_entries():
    ev = _children_plan_cleared()
    assert dev.is_well_formed(ev)
    assert dev.REQUIRED_KEYS[dev.CHILDREN_PLAN_CLEARED] <= set(ev.keys())
    # empty children list -> not a clearance of anything
    empty = dev.make_children_plan_cleared(
        parent_idea_id="p1", parent_idea_sha256="a" * 64, criteria_sha256="b" * 64,
        decomposition_run_id="run1", work_breakdown_sha256="c" * 64, children=[],
        cleared_at="2026-06-18T00:00:00Z")
    assert not dev.is_well_formed(empty)
    # a clearance entry MUST carry verdict_id (the freeze entry does not) — a frozen-shaped entry is rejected
    frozen_shaped = dev.make_children_plan_cleared(
        parent_idea_id="p1", parent_idea_sha256="a" * 64, criteria_sha256="b" * 64,
        decomposition_run_id="run1", work_breakdown_sha256="c" * 64,
        children=[{"task_id": "taskA", "job_id": "p1-a", "idea_contract_sha256": "d" * 64,
                   "product_plan_sha256": "e" * 64}],  # missing verdict_id
        cleared_at="2026-06-18T00:00:00Z")
    assert not dev.is_well_formed(frozen_shaped)
    # a missing top-level binding key (criteria_sha256) is rejected
    no_crit = _children_plan_cleared()
    del no_crit["criteria_sha256"]
    assert not dev.is_well_formed(no_crit)


def test_select_filters_by_type_run_and_wellformedness(tmp_path: Path):
    p = tmp_path / "events.jsonl"
    good = _criteria_frozen(run_id="runA")
    other_run = _criteria_frozen(run_id="runB")
    partial = _criteria_frozen(run_id="runA")
    del partial["verdict_id"]  # partial -> must be excluded even though run matches
    p.write_text("\n".join(json.dumps(x) for x in [good, other_run, partial]) + "\n")
    events = dev.read_events(p)
    sel = dev.select(events, dev.CRITERIA_FROZEN, decomposition_run_id="runA")
    assert len(sel) == 1 and sel[0]["verdict_id"] == "v1"
    # without a run filter, both well-formed events match; the partial is still excluded.
    assert len(dev.select(events, dev.CRITERIA_FROZEN)) == 2
