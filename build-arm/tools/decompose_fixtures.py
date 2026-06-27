"""Shared honest-decomposition fixture (NOT a test module — no test_ prefix, not collected).

One canonical, fully-HONEST decomposition tree on disk: parent-criteria.json + work-breakdown.json
+ a wrapper-emitted events.jsonl (criteria_frozen, optional large_override) + staged child
idea-contract/product-plan files. Every adversarial test (D1..D9, the bindings) takes this honest
base and mutates exactly ONE thing, so a failure pins exactly which gate bit.

The honest tree is a 3-child split of a demo idea:
  c1,c2 -> taskA (core parser)   c3 -> taskB (validator)   c4 -> taskC (integration verifier)
taskA requires an external git input; taskB requires taskA's output; taskC requires taskB's.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools import decompose_events as dev

_HEX_PLACEHOLDER = "0" * 64  # overwritten with the real hash at write time


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _write_json(path: Path, obj) -> bytes:
    """Write deterministically and return the exact bytes written (so the caller hashes what
    a reader will rehash off disk)."""
    b = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b)
    return b


def honest_parent_criteria() -> dict:
    return {
        "parent_idea_id": "20260615-demo",
        "criteria": [
            {"id": "c1", "text": "parse the input file into records"},
            {"id": "c2", "text": "reject malformed records, fail closed"},
            {"id": "c3", "text": "emit a validation report"},
            {"id": "c4", "text": "end-to-end integration verifier"},
        ],
    }


def honest_work_breakdown() -> dict:
    return {
        "parent_idea_id": "20260615-demo",
        "drafted_at": "2026-06-15T11:00:00Z",
        "authored_by": "main",
        "parent_criteria_ref": {"file": "parent-criteria.json", "sha256": _HEX_PLACEHOLDER},
        "coverage": [
            {"criterion_id": "c1", "owner_task_id": "taskA"},
            {"criterion_id": "c2", "owner_task_id": "taskA"},
            {"criterion_id": "c3", "owner_task_id": "taskB"},
            {"criterion_id": "c4", "owner_task_id": "taskC"},
        ],
        # No external inputs in the honest baseline: every requires resolves through the internal
        # producer closure, so the baseline doesn't depend on external-source verification. The
        # external path (verified vs forged source) is exercised by dedicated D5 tests.
        "external_inputs": [],
        "children": [
            {
                "task_id": "taskA", "summary": "core parser",
                "parent_criteria_refs": ["c1", "c2"],
                "acceptance_checks": ["parses a well-formed record"],
                "depends_on": [], "produces": ["taskA.parsed"], "requires": [],
                "planned_files": ["parser.py", "test_parser.py"],
                "allowed_paths": ["state/builds/20260615-demo-a/"],
                "out_of_scope": ["networking"], "size_estimate": {"new_loc": 40},
            },
            {
                "task_id": "taskB", "summary": "validator over parsed records",
                "parent_criteria_refs": ["c3"],
                "acceptance_checks": ["flags a malformed record"],
                "depends_on": ["taskA"], "produces": ["taskB.report"], "requires": ["taskA.parsed"],
                "planned_files": ["validator.py", "test_validator.py"],
                "allowed_paths": ["state/builds/20260615-demo-b/"],
                "out_of_scope": [], "size_estimate": {"new_loc": 50},
            },
            {
                "task_id": "taskC", "summary": "integration verifier",
                "parent_criteria_refs": ["c4"],
                "acceptance_checks": ["full chain produces a report"],
                "depends_on": ["taskB"], "produces": ["taskC.attest"], "requires": ["taskB.report"],
                "planned_files": ["verify.py", "test_verify.py"],
                "allowed_paths": ["state/builds/20260615-demo-c/"],
                "out_of_scope": [], "size_estimate": {"new_loc": 30},
            },
        ],
    }


def honest_child_artifacts() -> dict:
    """task_id -> {idea_contract, product_plan} with parent_criteria_refs == owned coverage ids.
    Used by D9 (Phase 4); harmless for D1-D8."""
    def ic(job, owns):
        return {"job_id": job, "proposed_at": "2026-06-15T11:30:00Z",
                "what_should_exist": f"{job} deliverable", "who_benefits": "the pipeline",
                "why_now": "parent split", "evidence_refs": ["state/research/x.json"],
                "out_of_scope": [], "where_it_lives": f"state/builds/{job}/",
                "how_verifiable": "pytest", "parent_criteria_refs": owns}

    def pp(job, owns, path):
        return {"job_id": job, "allowed_paths": [path], "planned_files": [path + "m.py"],
                "non_goals": [], "verification_commands": ["python -m pytest"],
                "acceptance_checks": ["works"], "risk_assessment": "low",
                "parent_criteria_refs": owns}

    return {
        "taskA": {"idea_contract": ic("20260615-demo-a", ["c1", "c2"]),
                  "product_plan": pp("20260615-demo-a", ["c1", "c2"], "state/builds/20260615-demo-a/")},
        "taskB": {"idea_contract": ic("20260615-demo-b", ["c3"]),
                  "product_plan": pp("20260615-demo-b", ["c3"], "state/builds/20260615-demo-b/")},
        "taskC": {"idea_contract": ic("20260615-demo-c", ["c4"]),
                  "product_plan": pp("20260615-demo-c", ["c4"], "state/builds/20260615-demo-c/")},
    }


def write_tree(
    root: Path,
    *,
    parent_criteria: dict | None = None,
    work_breakdown: dict | None = None,
    child_artifacts: dict | None = None,
    run_id: str = "run1",
    now: str = "2026-06-15T12:00:00Z",
    large_override_tasks=(),
    emit_criteria_frozen: bool = True,
    emit_plan_cleared: bool = True,
    author_as_certifier: bool = False,
    extra_events=(),
) -> dict:
    """Materialize a decomposition tree. Returns handles. Hashes are computed from the EXACT
    bytes written, and the criteria_frozen / large_override events are built with the shared
    dev builders — so a reader (decompose_check) sees exactly what an honest run would.

    Knobs let a test forge ONE thing: skip the freeze event, point the freeze at moved bytes,
    set author == certifier, request a large_override, etc.
    """
    root = Path(root)
    pc = parent_criteria if parent_criteria is not None else honest_parent_criteria()
    wb = work_breakdown if work_breakdown is not None else honest_work_breakdown()
    cas = child_artifacts if child_artifacts is not None else honest_child_artifacts()

    parent_idea_path = root / "parent-idea.json"
    idea_bytes = _write_json(parent_idea_path, {"job_id": pc["parent_idea_id"], "what_should_exist": "demo"})
    parent_idea_sha256 = _sha256_bytes(idea_bytes)

    pc_path = root / "parent-criteria.json"
    pc_bytes = _write_json(pc_path, pc)
    criteria_sha256 = _sha256_bytes(pc_bytes)

    # The doer's advisory pointer should match the frozen file (unless a test overrides it).
    if "parent_criteria_ref" in wb and wb["parent_criteria_ref"].get("sha256") == _HEX_PLACEHOLDER:
        wb["parent_criteria_ref"]["sha256"] = criteria_sha256

    wb_path = root / "work-breakdown.json"
    wb_bytes = _write_json(wb_path, wb)
    work_breakdown_sha256 = _sha256_bytes(wb_bytes)

    # Staged child artifacts (for D9 / mint).
    children = {}
    for task_id, arts in cas.items():
        cdir = root / "staged-children" / task_id
        ic_path = cdir / "idea-contract.json"
        pp_path = cdir / "product-plan.json"
        ic_bytes = _write_json(ic_path, arts["idea_contract"])
        pp_bytes = _write_json(pp_path, arts["product_plan"])
        children[task_id] = {
            "dir": cdir, "idea_contract_path": ic_path, "product_plan_path": pp_path,
            "idea_contract_sha256": _sha256_bytes(ic_bytes), "product_plan_sha256": _sha256_bytes(pp_bytes),
            "job_id": arts["idea_contract"].get("job_id"),
        }

    events_path = root / "events.jsonl"
    lines = []
    if emit_criteria_frozen:
        frozen_by = wb.get("authored_by", "main") if author_as_certifier else "codex"
        cf = dev.make_criteria_frozen(
            parent_idea_id=pc["parent_idea_id"], parent_idea_sha256=parent_idea_sha256,
            criteria_sha256=criteria_sha256, decomposition_run_id=run_id,
            verdict_id="v-" + criteria_sha256[:8], frozen_at=now, frozen_by=frozen_by,
        )
        lines.append({"ts": now, **cf})
    for task_id in large_override_tasks:
        lo = dev.make_large_override(
            parent_idea_id=pc["parent_idea_id"], parent_idea_sha256=parent_idea_sha256,
            criteria_sha256=criteria_sha256, decomposition_run_id=run_id,
            work_breakdown_sha256=work_breakdown_sha256, task_id=task_id, derived_bucket="L",
            reason="irreducible", verdict_id="ov-" + task_id, granted_at=now,
        )
        lines.append({"ts": now, **lo})
    if emit_plan_cleared:
        # An honest run plan-readiness-clears the children BEFORE D9 freezes them; the clearance pins
        # the SAME staged bytes D9 will freeze + this run's criteria universe (so the honest D9 passes
        # the plan-readiness binding). A test that wants the no-clearance path passes emit_plan_cleared=False.
        cpc = dev.make_children_plan_cleared(
            parent_idea_id=pc["parent_idea_id"], parent_idea_sha256=parent_idea_sha256,
            criteria_sha256=criteria_sha256, decomposition_run_id=run_id,
            work_breakdown_sha256=work_breakdown_sha256,
            children=[{"task_id": tid, "job_id": info["job_id"],
                       "idea_contract_sha256": info["idea_contract_sha256"],
                       "product_plan_sha256": info["product_plan_sha256"],
                       "verdict_id": "pcv-" + str(info["job_id"])}
                      for tid, info in children.items()],
            cleared_at=now,
        )
        lines.append({"ts": now, **cpc})
    lines.extend(extra_events)  # e.g. a trusted verdict/fetch event an external source binds to
    events_path.write_text("".join(json.dumps(x) + "\n" for x in lines))

    return {
        "root": root, "run_id": run_id, "now": now,
        "parent_idea_id": pc["parent_idea_id"], "parent_idea_path": parent_idea_path,
        "parent_criteria_path": pc_path, "criteria_sha256": criteria_sha256,
        "work_breakdown_path": wb_path, "work_breakdown_sha256": work_breakdown_sha256,
        "events_path": events_path, "children": children,
    }
