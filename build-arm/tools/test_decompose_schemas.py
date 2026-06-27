"""Phase 1 — decomposition schema validity + shape enforcement.

These tests pin the STRUCTURAL contract of the decomposition artifacts (work-breakdown
§4b, parent-criteria §4a) and the child-schema trace field (§9 LOW). They assert ONLY
shape — the honesty checks (exact-partition coverage, dependency closure, acyclicity,
provenance, large-child guard, byte-freeze) are decompose_check D1-D9's job and are tested
separately. The point here: a malformed split can't even be parsed, and the design's
"Main doesn't self-author complexity" / "a bare-string source_ref is rejected" rules are
enforced by the schema itself, not by downstream prose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"

_HEX64 = "a" * 64


def _load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def _validator(name: str) -> Draft202012Validator:
    schema = _load(name)
    # The schema document itself must be a well-formed Draft 2020-12 schema.
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


# --------------------------------------------------------------------------------------
# Canonical valid fixtures (a 3-child honest split)
# --------------------------------------------------------------------------------------
def valid_parent_criteria() -> dict:
    return {
        "parent_idea_id": "20260615-demo",
        "criteria": [
            {"id": "c1", "text": "parse the input file into records"},
            {"id": "c2", "text": "reject malformed records, fail closed"},
            {"id": "c3", "text": "emit a validation report"},
            {"id": "c4", "text": "end-to-end integration verifier"},
        ],
    }


def valid_work_breakdown() -> dict:
    return {
        "parent_idea_id": "20260615-demo",
        "drafted_at": "2026-06-15T12:00:00Z",
        "authored_by": "main",
        "parent_criteria_ref": {"file": "parent-criteria.json", "sha256": _HEX64},
        "coverage": [
            {"criterion_id": "c1", "owner_task_id": "taskA"},
            {"criterion_id": "c2", "owner_task_id": "taskA"},
            {"criterion_id": "c3", "owner_task_id": "taskB"},
            {"criterion_id": "c4", "owner_task_id": "taskC"},
        ],
        "external_inputs": [
            {
                "id": "ext.spec",
                "source_ref": {
                    "kind": "git",
                    "commit": "abc123",
                    "path": "docs/spec.md",
                    "sha256": _HEX64,
                },
            }
        ],
        "children": [
            {
                "task_id": "taskA",
                "summary": "core parser",
                "parent_criteria_refs": ["c1", "c2"],
                "acceptance_checks": ["parses a well-formed record"],
                "depends_on": [],
                "produces": ["taskA.parsed"],
                "requires": ["ext.spec"],
                "planned_files": ["parser.py", "test_parser.py"],
                "allowed_paths": ["state/builds/20260615-demo-a/"],
                "out_of_scope": ["networking"],
                "size_estimate": {"new_loc": 40},
            },
            {
                "task_id": "taskB",
                "summary": "validator over parsed records",
                "parent_criteria_refs": ["c3"],
                "acceptance_checks": ["flags a malformed record"],
                "depends_on": ["taskA"],
                "produces": ["taskB.report"],
                "requires": ["taskA.parsed"],
                "planned_files": ["validator.py", "test_validator.py"],
                "allowed_paths": ["state/builds/20260615-demo-b/"],
                "out_of_scope": [],
                "size_estimate": {"new_loc": 50},
            },
            {
                "task_id": "taskC",
                "summary": "integration verifier",
                "parent_criteria_refs": ["c4"],
                "acceptance_checks": ["full chain produces a report"],
                "depends_on": ["taskB"],
                "produces": ["taskC.attest"],
                "requires": ["taskB.report"],
                "planned_files": ["verify.py", "test_verify.py"],
                "allowed_paths": ["state/builds/20260615-demo-c/"],
                "out_of_scope": [],
                "size_estimate": {"new_loc": 30},
            },
        ],
    }


# --------------------------------------------------------------------------------------
# Schema documents are themselves well-formed
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "parent-criteria.schema.json",
        "work-breakdown.schema.json",
        "idea-contract.schema.json",
        "product-plan.schema.json",
    ],
)
def test_schema_document_is_valid_draft202012(name):
    Draft202012Validator.check_schema(_load(name))


# --------------------------------------------------------------------------------------
# parent-criteria.schema.json
# --------------------------------------------------------------------------------------
def test_parent_criteria_valid_passes():
    _validator("parent-criteria.schema.json").validate(valid_parent_criteria())


def test_parent_criteria_missing_criteria_fails():
    v = _validator("parent-criteria.schema.json")
    bad = valid_parent_criteria()
    del bad["criteria"]
    assert not v.is_valid(bad)


def test_parent_criteria_criterion_missing_text_fails():
    v = _validator("parent-criteria.schema.json")
    bad = valid_parent_criteria()
    del bad["criteria"][0]["text"]
    assert not v.is_valid(bad)


def test_parent_criteria_empty_criteria_fails():
    v = _validator("parent-criteria.schema.json")
    bad = valid_parent_criteria()
    bad["criteria"] = []
    assert not v.is_valid(bad)


def test_parent_criteria_extra_toplevel_prop_fails():
    v = _validator("parent-criteria.schema.json")
    bad = valid_parent_criteria()
    bad["criteria_sha256"] = _HEX64  # the hash belongs in the event, never the file
    assert not v.is_valid(bad)


# --------------------------------------------------------------------------------------
# work-breakdown.schema.json
# --------------------------------------------------------------------------------------
def test_work_breakdown_valid_passes():
    _validator("work-breakdown.schema.json").validate(valid_work_breakdown())


def test_work_breakdown_bare_string_source_ref_rejected():
    """A 'laundered' external input — a bare string instead of a typed provenance object."""
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    bad["external_inputs"][0]["source_ref"] = "trust me, it's from git"
    assert not v.is_valid(bad)


def test_work_breakdown_self_authored_complexity_rejected():
    """§8.1: Main must NOT self-author complexity_derived (tool-derived only)."""
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    bad["children"][0]["complexity_derived"] = "S"
    assert not v.is_valid(bad)


def test_work_breakdown_missing_required_child_key_fails():
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    del bad["children"][0]["allowed_paths"]
    assert not v.is_valid(bad)


def test_work_breakdown_empty_allowed_paths_fails():
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    bad["children"][0]["allowed_paths"] = []
    assert not v.is_valid(bad)


def test_work_breakdown_noncanonical_produces_id_fails():
    """produces/requires must be canonical <task_id>.<output_id> (contain a dot)."""
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    bad["children"][0]["produces"] = ["taskAparsed"]  # no separating dot
    assert not v.is_valid(bad)


def test_work_breakdown_coverage_row_missing_owner_fails():
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    del bad["coverage"][0]["owner_task_id"]
    assert not v.is_valid(bad)


def test_work_breakdown_empty_children_fails():
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    bad["children"] = []
    assert not v.is_valid(bad)


@pytest.mark.parametrize(
    "source_ref",
    [
        {"kind": "git", "commit": "abc123", "path": "docs/spec.md", "sha256": _HEX64},
        {"kind": "sweep-fetch", "anchor": "fetch://x", "event_id": "evt-1"},
        {"kind": "frozen-verdict", "verdict_id": "v-1", "sha256": _HEX64},
    ],
)
def test_work_breakdown_all_three_source_ref_types_pass(source_ref):
    v = _validator("work-breakdown.schema.json")
    wb = valid_work_breakdown()
    wb["external_inputs"][0]["source_ref"] = source_ref
    v.validate(wb)


def test_work_breakdown_missing_kind_discriminator_rejected():
    """No `kind` -> matches no source_ref variant (Codex P1 finding 2)."""
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    bad["external_inputs"][0]["source_ref"] = {
        "commit": "abc123",
        "path": "docs/spec.md",
        "sha256": _HEX64,
    }
    assert not v.is_valid(bad)


def test_work_breakdown_hybrid_source_ref_rejected():
    """A source_ref tagged one kind but smuggling another kind's key is rejected
    (additionalProperties:false on each variant)."""
    v = _validator("work-breakdown.schema.json")
    bad = valid_work_breakdown()
    bad["external_inputs"][0]["source_ref"] = {
        "kind": "git",
        "commit": "abc123",
        "path": "docs/spec.md",
        "sha256": _HEX64,
        "event_id": "evt-1",
    }
    assert not v.is_valid(bad)


def test_work_breakdown_allow_large_optional_boolean():
    v = _validator("work-breakdown.schema.json")
    wb = valid_work_breakdown()
    wb["children"][0]["allow_large"] = True
    v.validate(wb)
    wb["children"][0]["allow_large"] = "yes"  # not a boolean
    assert not v.is_valid(wb)


# --------------------------------------------------------------------------------------
# child schemas — parent_criteria_refs is OPTIONAL (§9 LOW)
# --------------------------------------------------------------------------------------
def _valid_idea_contract() -> dict:
    return {
        "job_id": "20260615-demo-a",
        "proposed_at": "2026-06-15T12:00:00Z",
        "what_should_exist": "a record parser",
        "who_benefits": "the build pipeline",
        "why_now": "a multi-track idea needs splitting",
        "evidence_refs": ["state/research/x.json"],
        "out_of_scope": ["networking"],
        "where_it_lives": "state/builds/20260615-demo-a/",
        "how_verifiable": "pytest green",
    }


def _valid_product_plan() -> dict:
    return {
        "job_id": "20260615-demo-a",
        "allowed_paths": ["state/builds/20260615-demo-a/"],
        "planned_files": ["parser.py"],
        "non_goals": ["networking"],
        "verification_commands": ["python -m pytest"],
        "acceptance_checks": ["parses a record"],
        "risk_assessment": "low",
    }


def test_idea_contract_with_and_without_parent_criteria_refs():
    v = _validator("idea-contract.schema.json")
    base = _valid_idea_contract()
    v.validate(base)  # absent -> valid (normal idea)
    base["parent_criteria_refs"] = ["c1", "c2"]
    v.validate(base)  # present -> valid (decomposition child)
    base["parent_criteria_refs"] = "c1"  # wrong type
    assert not v.is_valid(base)


def test_product_plan_with_and_without_parent_criteria_refs():
    v = _validator("product-plan.schema.json")
    base = _valid_product_plan()
    v.validate(base)  # absent -> valid (normal plan)
    base["parent_criteria_refs"] = ["c1", "c2"]
    v.validate(base)  # present -> valid (decomposition child)
    base["parent_criteria_refs"] = [123]  # wrong item type
    assert not v.is_valid(base)
