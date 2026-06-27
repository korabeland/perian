#!/usr/bin/env python3
"""Decomposition event contract — the SINGLE source of truth for the three trusted
records the decomposition step turns on.

WHY THIS MODULE EXISTS (the drift trap):
The whole decomposition design rests on the deterministic gate reading a record the doer
CANNOT forge. There are two halves to that: (1) the record is *written by sweep-owned code
outside Main's sandbox* (the freeze-wrapper / decompose_check / D9 emit it; Main is fs-jailed
out of events.jsonl — proven in Phase 5 / B1); and (2) the *reader* (D1, D7, gate_sweep)
checks exactly the keys the *emitter* wrote. If the emitter and reader disagree on a key
name, a gate silently reads `None` and fails open. So both sides import the builders +
required-key sets from HERE — a mismatch becomes impossible by construction.

Three events (all append-only lines in state/events.jsonl):
  - ``criteria_frozen``      §4a — the freeze-wrapper captures Codex's freeze verdict.
  - ``large_override``       §4c — the freeze-wrapper captures Codex's large-child grant.
  - ``child_artifacts_frozen`` §5/§9-B3 — decompose_check D9 freezes the ACTUAL child files'
    bytes (tool-computed hashes, not a Codex verdict).

Each event also carries a generic top-level ``ts`` added by the emitter (mirrors
think_sweep.emit_event / codex_review.emit_event); the domain timestamps (``frozen_at`` /
``granted_at``) are part of the payload so a reader can apply a freshness/identity check
without depending on the generic ts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CRITERIA_FROZEN = "criteria_frozen"
LARGE_OVERRIDE = "large_override"
CHILD_ARTIFACTS_FROZEN = "child_artifacts_frozen"
CHILDREN_PLAN_CLEARED = "children_plan_cleared"


# --------------------------------------------------------------------------------------
# Shared complexity derivation — the freeze-wrapper (which records derived_bucket in a
# large_override) and decompose_check D7 (which checks derived_bucket == "L") MUST bucket
# new_loc identically, or an L child could be granted under one rule and gated under another.
# One function, imported by both. Thresholds are session-fit heuristics + recalibration knobs
# (design §6): a miss recalibrates these, it doesn't loosen a gate. complexity_derived is
# DISPLAY-ONLY and TOOL-DERIVED — never authored by Main (§8.1).
# --------------------------------------------------------------------------------------
S_MAX_LOC = 60   # new_loc <= S_MAX_LOC          -> "S"
M_MAX_LOC = 150  # S_MAX_LOC < new_loc <= M_MAX_LOC -> "M"; new_loc > M_MAX_LOC -> "L"


def derive_complexity(new_loc: int) -> str:
    """new_loc -> 'S' | 'M' | 'L'. Total: a non-int / negative value fails to the most
    conservative bucket ('L') so a malformed size can only ever trip MORE oversight, never less."""
    if not isinstance(new_loc, int) or isinstance(new_loc, bool) or new_loc < 0:
        return "L"
    if new_loc <= S_MAX_LOC:
        return "S"
    if new_loc <= M_MAX_LOC:
        return "M"
    return "L"

# Required keys per event (the reader's contract). Emitter builders below produce exactly
# these (plus the generic ``ts`` the emitter prepends). A reader MUST reject an event line
# missing any required key — a partial event is not a trusted record.
REQUIRED_KEYS = {
    CRITERIA_FROZEN: {
        "event",
        "parent_idea_id",
        "parent_idea_sha256",
        "criteria_sha256",
        "decomposition_run_id",
        "frozen_by",
        "frozen_at",
        "verdict_id",
    },
    LARGE_OVERRIDE: {
        "event",
        "parent_idea_id",
        "parent_idea_sha256",
        "criteria_sha256",
        "decomposition_run_id",
        "work_breakdown_sha256",
        "task_id",
        "derived_bucket",
        "reason",
        "granted_by",
        "granted_at",
        "verdict_id",
    },
    CHILD_ARTIFACTS_FROZEN: {
        "event",
        "parent_idea_id",
        "decomposition_run_id",
        "work_breakdown_sha256",
        "children",
        "frozen_by",
        "frozen_at",
    },
    # §plan-readiness-gate (2026-06-18): the per-child Codex plan-review clearance that D9 binds the
    # freeze to. Binds the criteria universe (parent_idea_sha256 + criteria_sha256, like large_override)
    # so a clearance cannot be replayed across two runs that share job_ids (Codex design-review R3/R4).
    CHILDREN_PLAN_CLEARED: {
        "event",
        "parent_idea_id",
        "parent_idea_sha256",
        "criteria_sha256",
        "decomposition_run_id",
        "work_breakdown_sha256",
        "children",
        "cleared_by",
        "cleared_at",
    },
}


# --------------------------------------------------------------------------------------
# Builders (the EMITTER side — freeze-wrapper + D9). Pure: no I/O, no clock.
# --------------------------------------------------------------------------------------
def make_criteria_frozen(
    *,
    parent_idea_id: str,
    parent_idea_sha256: str,
    criteria_sha256: str,
    decomposition_run_id: str,
    verdict_id: str,
    frozen_at: str,
    frozen_by: str = "codex",
) -> dict:
    return {
        "event": CRITERIA_FROZEN,
        "parent_idea_id": parent_idea_id,
        "parent_idea_sha256": parent_idea_sha256,
        "criteria_sha256": criteria_sha256,
        "decomposition_run_id": decomposition_run_id,
        "frozen_by": frozen_by,
        "frozen_at": frozen_at,
        "verdict_id": verdict_id,
    }


def make_large_override(
    *,
    parent_idea_id: str,
    parent_idea_sha256: str,
    criteria_sha256: str,
    decomposition_run_id: str,
    work_breakdown_sha256: str,
    task_id: str,
    derived_bucket: str,
    reason: str,
    verdict_id: str,
    granted_at: str,
    granted_by: str = "codex",
) -> dict:
    return {
        "event": LARGE_OVERRIDE,
        "parent_idea_id": parent_idea_id,
        "parent_idea_sha256": parent_idea_sha256,
        "criteria_sha256": criteria_sha256,
        "decomposition_run_id": decomposition_run_id,
        "work_breakdown_sha256": work_breakdown_sha256,
        "task_id": task_id,
        "derived_bucket": derived_bucket,
        "reason": reason,
        "granted_by": granted_by,
        "granted_at": granted_at,
        "verdict_id": verdict_id,
    }


def make_child_artifacts_frozen(
    *,
    parent_idea_id: str,
    decomposition_run_id: str,
    work_breakdown_sha256: str,
    children: list[dict],
    frozen_at: str,
    frozen_by: str = "decompose_check",
) -> dict:
    """children: [{task_id, idea_contract_sha256, product_plan_sha256}] — D9's frozen bytes."""
    return {
        "event": CHILD_ARTIFACTS_FROZEN,
        "parent_idea_id": parent_idea_id,
        "decomposition_run_id": decomposition_run_id,
        "work_breakdown_sha256": work_breakdown_sha256,
        "children": children,
        "frozen_by": frozen_by,
        "frozen_at": frozen_at,
    }


def make_children_plan_cleared(
    *,
    parent_idea_id: str,
    parent_idea_sha256: str,
    criteria_sha256: str,
    decomposition_run_id: str,
    work_breakdown_sha256: str,
    children: list[dict],
    cleared_at: str,
    cleared_by: str = "codex",
) -> dict:
    """children: [{task_id, job_id, idea_contract_sha256, product_plan_sha256, verdict_id}] — the staged
    bytes the plan-readiness gate Codex-reviewed AND approved (exactly the bytes D9 will freeze). The
    per-child verdict_id content-hashes the structured Codex verdict (tamper-evident, like the freeze)."""
    return {
        "event": CHILDREN_PLAN_CLEARED,
        "parent_idea_id": parent_idea_id,
        "parent_idea_sha256": parent_idea_sha256,
        "criteria_sha256": criteria_sha256,
        "decomposition_run_id": decomposition_run_id,
        "work_breakdown_sha256": work_breakdown_sha256,
        "children": children,
        "cleared_by": cleared_by,
        "cleared_at": cleared_at,
    }


_CHILD_ENTRY_KEYS = {"task_id", "job_id", "idea_contract_sha256", "product_plan_sha256"}
# A clearance entry additionally carries the per-child verdict_id (the hashed Codex plan verdict).
_CLEARED_ENTRY_KEYS = _CHILD_ENTRY_KEYS | {"verdict_id"}
# Events whose `children` list is load-bearing: each maps to the required keys of ITS entries, so a
# reader rejects a partial/forged entry before trusting any field (a missing key is never a pass).
_CHILD_LIST_EVENTS = {
    CHILD_ARTIFACTS_FROZEN: _CHILD_ENTRY_KEYS,
    CHILDREN_PLAN_CLEARED: _CLEARED_ENTRY_KEYS,
}


def is_well_formed(payload: dict) -> bool:
    """True iff payload has its event type's required keys (and, for a child-list event, a non-empty
    children list of well-shaped entries — Codex P4 HIGH). A reader uses this to reject a partial/forged
    line BEFORE trusting any field (a missing key is never read as a pass)."""
    if not isinstance(payload, dict):
        return False
    name = payload.get("event")
    req = REQUIRED_KEYS.get(name)
    if req is None or not req.issubset(payload.keys()):
        return False
    entry_keys = _CHILD_LIST_EVENTS.get(name)
    if entry_keys is not None:
        children = payload.get("children")
        if not isinstance(children, list) or not children:
            return False  # an empty/non-list children is not a trusted record of anything
        for ch in children:
            if not isinstance(ch, dict) or not entry_keys.issubset(ch.keys()):
                return False
    return True


# --------------------------------------------------------------------------------------
# Reader side (D1 / D7 / gate_sweep). events.jsonl is sweep-owned and append-only.
# --------------------------------------------------------------------------------------
def append_event(events_path: Path, payload: dict, now_iso: str) -> None:
    """SWEEP-OWNED append with a landed-proof read-back. The emitter (freeze-wrapper, D9) runs
    OUTSIDE Main's fs-jail, so every line written here is provably not doer-forged (B1). Raises if
    the events dir is missing OR the just-written line isn't the last well-formed line on re-read —
    we must never report success on an event that didn't durably land (a gate would then fail
    closed while the caller believed it passed). events.jsonl is append-only + single-writer per
    run (the gate-sweep cron is paused for deterministic runs), so the appended line IS the last."""
    events_path = Path(events_path)
    if not events_path.parent.is_dir():
        raise OSError(f"events dir does not exist: {events_path.parent}")
    full = {"ts": now_iso, **payload}
    with events_path.open("a") as fh:
        fh.write(json.dumps(full) + "\n")
        fh.flush()
        os.fsync(fh.fileno())  # durability, not just visibility (Codex P4 MED)
    nonblank = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    try:
        last = json.loads(nonblank[-1]) if nonblank else None
    except json.JSONDecodeError:
        last = None
    if last != full or not is_well_formed(last):
        raise OSError("event did not land verifiably (last-line read-back mismatch)")


def read_events(events_path: Path) -> list[dict]:
    """All well-shaped JSON object lines from events.jsonl. Malformed lines are skipped
    (never crash the gate), but a skipped line is simply absent — it can never read as a
    trusted record. Missing file -> []."""
    p = Path(events_path)
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def select(events: list[dict], event_name: str, decomposition_run_id: str | None = None) -> list[dict]:
    """Well-formed events of one type, optionally pinned to a decomposition_run_id. Only
    well-formed events are returned — a partial line is never a candidate trusted record."""
    out = []
    for e in events:
        if e.get("event") != event_name or not is_well_formed(e):
            continue
        if decomposition_run_id is not None and e.get("decomposition_run_id") != decomposition_run_id:
            continue
        out.append(e)
    return out
