#!/usr/bin/env python3
"""Decomposition POST-EMIT gate (design §5 Phase 2; build-time bindings §9 B2/B3).

Pre-build, decompose_check proves the *plan* (work-breakdown) is an honest partition. This module
covers what can only be checked once the children actually EXIST and BUILD:

  - D9  (post-emit): open the ACTUAL minted child idea-contract.json + product-plan.json, schema-
        validate, require parent_criteria_refs == the child's owned coverage ids, and FREEZE each
        child artifact's bytes by emitting a sweep-owned child_artifacts_frozen event (the hashes a
        forger can't move). Fail-closed: any bad child ⇒ no event ⇒ nothing builds.
  - mint (B3 TOCTOU close): copy the staged child bytes into their canonical track dirs ONLY after
        re-verifying each byte-stream still hashes to the D9-frozen value. A mutation between check
        and build is caught HERE — the mint refuses (atomic: verify all, then write all).
  - verify_built_child (B3): the plan-gate re-verifies a built child's on-disk bytes against the
        frozen hashes, so contracts can't drift between mint and the coder consuming them.
  - D6  (B2 size ceiling): the binding size gate is the POST-build recount of ACTUAL files/checks
        vs caps (forecasts are advisory → Codex). Pure core here; gate_sweep gathers the actual
        counts from trusted artifacts (git diff + the tool-emitted qa-verification) and blocks the
        QA `complete` on over-cap.
  - is_decomposition_child / frozen_child_record: gate_sweep keys off the trusted
        child_artifacts_frozen event to know a track is a decomposition child + fetch its frozen
        hashes (B2 applies D6 only to children; B3 re-verifies their bytes).

Everything reads TRUSTED records (the wrapper/D9 events, the bytes it rehashes itself) — never a
doer self-field. decompose_post runs in the sweep, outside Main's fs-jail, so the event it emits is
provably not doer-forged (B1).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tools import decompose_events as dev
from tools import scope_check

ATB_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ATB_ROOT / "schemas"

# D6 size ceiling — session-fit recalibration knobs (design §6): a miss recalibrates these, it
# does not loosen the gate. The FILE count (git diff within allowed_paths) is the solid signal;
# the CHECK count (verification entries the tool actually ran) is the secondary balloon guard.
FILE_CAP = 6
CHECK_CAP = 80


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _load_schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / name).read_text()))


# --------------------------------------------------------------------------------------
# D9 — validate ACTUAL child files + freeze their bytes
# --------------------------------------------------------------------------------------
def check_d9_and_freeze(work_breakdown_path: Path, staged_dirs: dict, events_path: Path,
                        run_id: str, now_iso: str, expected_wb_sha256: str) -> dict:
    """staged_dirs: {task_id -> Path of the dir holding the minted idea-contract.json +
    product-plan.json}. expected_wb_sha256: the sha256 the pre-build gate (D1-D8) ran on — D9 binds
    to it so a DIFFERENT work-breakdown can't be frozen post-hoc (Codex P4 BLOCKER 2). Returns
    {ok, violations, event?}. ok=False ⇒ NO event emitted (fail-closed)."""
    try:
        wb_bytes = Path(work_breakdown_path).read_bytes()
        wb = json.loads(wb_bytes)
        wb_sha256 = _sha256_bytes(wb_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "violations": [{"check": "D9", "detail": f"cannot read work-breakdown: {exc}"}]}

    # Bind to the EXACT work-breakdown the pre-build gate validated; a drifted/substituted wb -> fail.
    if wb_sha256 != expected_wb_sha256:
        return {"ok": False, "violations": [{"check": "D9", "detail": "work-breakdown bytes != the "
                 "pre-build (D1-D8) hash — wb changed between the partition check and freeze"}]}
    # Schema-validate the wb too (rejects a malformed / empty-children doc that would freeze nothing).
    wb_errs = list(_load_schema("work-breakdown.schema.json").iter_errors(wb))
    if wb_errs:
        return {"ok": False, "violations": [{"check": "D9", "detail": f"work-breakdown schema-invalid: {wb_errs[0].message}"}]}

    owned = {}
    for row in wb.get("coverage", []):
        owned.setdefault(row["owner_task_id"], set()).add(row["criterion_id"])

    ic_schema = _load_schema("idea-contract.schema.json")
    pp_schema = _load_schema("product-plan.schema.json")

    violations: list[dict] = []
    frozen_children: list[dict] = []
    job_ids_seen: dict = {}
    for child in wb.get("children", []):
        tid = child["task_id"]
        sdir = staged_dirs.get(tid)
        if sdir is None:
            violations.append({"check": "D9", "detail": f"child {tid}: no staged dir"})
            continue
        ic_path = Path(sdir) / "idea-contract.json"
        pp_path = Path(sdir) / "product-plan.json"
        try:
            ic_bytes = ic_path.read_bytes()
            pp_bytes = pp_path.read_bytes()
            ic = json.loads(ic_bytes)
            pp = json.loads(pp_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            violations.append({"check": "D9", "detail": f"child {tid}: cannot read minted artifacts: {exc}"})
            continue
        ic_errs = list(ic_schema.iter_errors(ic))
        pp_errs = list(pp_schema.iter_errors(pp))
        if ic_errs or pp_errs:
            msg = (ic_errs or pp_errs)[0].message
            violations.append({"check": "D9", "detail": f"child {tid}: minted artifact schema-invalid: {msg}"})
            continue
        want = owned.get(tid, set())
        # parent_criteria_refs must be present, NON-EMPTY, and EXACTLY the owned coverage ids — on
        # BOTH artifacts (a vacuous/empty/mismatched trace is the 'produce-not-replace' forgery).
        for label, doc in (("idea-contract", ic), ("product-plan", pp)):
            refs = doc.get("parent_criteria_refs")
            if not refs:
                violations.append({"check": "D9", "detail": f"child {tid} {label}: parent_criteria_refs empty/absent"})
            elif len(refs) != len(set(refs)) or set(refs) != want:
                violations.append({"check": "D9", "detail": f"child {tid} {label}: parent_criteria_refs "
                                   f"{sorted(refs)} != owned coverage {sorted(want)}"})
        job_id = ic.get("job_id")
        if job_id != pp.get("job_id"):
            violations.append({"check": "D9", "detail": f"child {tid}: idea-contract/product-plan job_id disagree"})
            continue
        if not job_id:
            violations.append({"check": "D9", "detail": f"child {tid}: empty job_id"})
            continue
        if job_id in job_ids_seen:
            # Two children minting to the same canonical track would overwrite each other (Codex P4 MED).
            violations.append({"check": "D9", "detail": f"child {tid}: job_id {job_id!r} also used by "
                               f"{job_ids_seen[job_id]!r} (would collide at mint)"})
            continue
        job_ids_seen[job_id] = tid
        frozen_children.append({
            "task_id": tid, "job_id": job_id,
            "idea_contract_sha256": _sha256_bytes(ic_bytes),
            "product_plan_sha256": _sha256_bytes(pp_bytes),
        })

    # Plan-readiness binding (2026-06-18): frozen ⇒ plan-cleared. Only meaningful once the frozen set is
    # itself sound (a partial set from earlier violations would mis-compare), so guard on no prior
    # violations; then require THIS run's clearance to cover EXACTLY this set on identical bytes + the
    # same criteria universe (R3/R4/R6). No clearance ⇒ no freeze ⇒ nothing builds (the root-cause fix).
    if not violations:
        violations.extend(_d9_plan_clearance_violations(events_path, run_id, wb, wb_sha256, frozen_children))

    if violations:
        return {"ok": False, "violations": violations}  # emit NOTHING — nothing mints/builds

    payload = dev.make_child_artifacts_frozen(
        parent_idea_id=wb.get("parent_idea_id"), decomposition_run_id=run_id,
        work_breakdown_sha256=wb_sha256, children=frozen_children, frozen_at=now_iso,
    )
    try:
        dev.append_event(events_path, payload, now_iso)
    except OSError as exc:
        return {"ok": False, "violations": [{"check": "D9", "detail": f"cannot emit child_artifacts_frozen: {exc}"}]}
    return {"ok": True, "violations": [], "event": payload}


# --------------------------------------------------------------------------------------
# The mint (B3 TOCTOU close) — copy exactly the frozen bytes, fail-closed on any mutation
# --------------------------------------------------------------------------------------
def _mkdirs_tracked(path: Path, created_out: list) -> None:
    """Create `path` + any missing ancestors, appending each dir to `created_out` AS it is made —
    so even if a later mkdir raises, the caller's rollback list already holds every dir we created
    (no untracked residue — Codex P4 r3 MED). Raises OSError if a creation fails."""
    missing = []
    cur = Path(path)
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    for d in reversed(missing):  # root -> leaf
        d.mkdir()
        created_out.append(d)


def mint_children(frozen_event: dict, staged_dirs: dict, dest_for) -> dict:
    """Copy each staged child's idea-contract.json + product-plan.json into its canonical track
    dir — but ONLY the exact bytes D9 froze. dest_for: job_id -> destination dir Path.

    FAIL-CLOSED + ATOMIC + CREATE-ONLY (Codex P4 r2):
      1. verify EVERY child's staged bytes against the frozen hashes, AND refuse if a canonical
         artifact already exists (a mint never clobbers existing data — children are NEW tracks);
      2. then write all, tracking each file BEFORE the write (so a partial/failed write leaves no
         untracked residue) and using exclusive create ("xb", never overwrites);
      3. on ANY failure, roll back ONLY what we made — unlink the files we wrote, rmdir (empty-only,
         so pre-existing data is never removed) the dirs we created."""
    plan = []  # (tid, job, dest, ic_b, pp_b)
    for ch in frozen_event.get("children", []):
        tid, job = ch.get("task_id"), ch.get("job_id")
        sdir = staged_dirs.get(tid)
        if sdir is None:
            return {"ok": False, "reason": f"child {tid}: no staged dir to mint from"}
        try:
            ic_b = (Path(sdir) / "idea-contract.json").read_bytes()
            pp_b = (Path(sdir) / "product-plan.json").read_bytes()
        except OSError as exc:
            return {"ok": False, "reason": f"child {tid}: cannot read staged bytes: {exc}"}
        if _sha256_bytes(ic_b) != ch.get("idea_contract_sha256"):
            return {"ok": False, "reason": f"child {tid}: idea-contract bytes != frozen hash (mutated post-D9)"}
        if _sha256_bytes(pp_b) != ch.get("product_plan_sha256"):
            return {"ok": False, "reason": f"child {tid}: product-plan bytes != frozen hash (mutated post-D9)"}
        dest = Path(dest_for(job))
        if (dest / "idea-contract.json").exists() or (dest / "product-plan.json").exists():
            return {"ok": False, "reason": f"child {tid}: canonical artifact already exists at {dest} "
                    "— refusing to overwrite (mint is create-only)"}
        plan.append((tid, job, dest, ic_b, pp_b))

    created_dirs: list[Path] = []
    written_files: list[Path] = []
    minted: list[str] = []
    try:
        for tid, job, dest, ic_b, pp_b in plan:
            _mkdirs_tracked(dest, created_dirs)
            for name, b in (("idea-contract.json", ic_b), ("product-plan.json", pp_b)):
                fp = dest / name
                with open(fp, "xb") as f:  # exclusive create — never clobbers a non-owned file
                    written_files.append(fp)  # track ONLY after WE created it (Codex P4 r3)
                    f.write(b)
                if _sha256_bytes(fp.read_bytes()) != _sha256_bytes(b):
                    raise OSError(f"child {tid}: {name} did not land identically")
            minted.append(job)
    except OSError as exc:
        for fp in written_files:
            try:
                fp.unlink()
            except OSError:
                pass
        for d in reversed(created_dirs):  # leaf -> root; rmdir only removes EMPTY dirs (safe)
            try:
                d.rmdir()
            except OSError:
                pass
        return {"ok": False, "reason": f"mint failed, rolled back: {exc}"}
    return {"ok": True, "minted": minted}


def verify_built_child(child_dir: Path, frozen_child: dict) -> bool:
    """Plan-gate re-verify (B3): a built child's on-disk idea-contract.json + product-plan.json must
    still hash to the D9-frozen values, so contracts can't drift between mint and the coder."""
    try:
        ic = _sha256_bytes((Path(child_dir) / "idea-contract.json").read_bytes())
        pp = _sha256_bytes((Path(child_dir) / "product-plan.json").read_bytes())
    except OSError:
        return False
    return ic == frozen_child.get("idea_contract_sha256") and pp == frozen_child.get("product_plan_sha256")


# --------------------------------------------------------------------------------------
# gate_sweep helpers — detect a decomposition child + fetch its frozen record (B2/B3)
# --------------------------------------------------------------------------------------
def frozen_child_record(events: list[dict], job_id: str) -> dict | None:
    """The trusted child_artifacts_frozen entry for this build job_id, or None. Returns
    {event, child} so gate_sweep gets both the run context (wb hash) and the child's frozen hashes.

    Selects the LATEST matching freeze in append order (Codex final-review caveat): if D9 emits,
    the mint fails, and the same job_id is later re-frozen with different bytes, B3 must check the
    FRESH freeze — matching the first (stale) event would false-HOLD. All child_artifacts_frozen
    events are sweep-emitted (Main can't write events.jsonl — B1), so the latest is trusted."""
    found = None
    for ev in dev.select(events, dev.CHILD_ARTIFACTS_FROZEN):
        for ch in ev.get("children", []):
            if isinstance(ch, dict) and ch.get("job_id") == job_id:
                found = {"event": ev, "child": ch}  # keep the latest (append-order) match
    return found


def is_decomposition_child(events: list[dict], job_id: str) -> bool:
    return frozen_child_record(events, job_id) is not None


# --------------------------------------------------------------------------------------
# Plan-readiness clearance readers (2026-06-18) — the trusted children_plan_cleared record D9 binds
# the freeze to, and the build-time gate trusts in place of a stochastic re-review. Both bind by hash
# to the SAME run + work-breakdown, so a clearance from another run / reused job_id / identical bytes
# can never suppress review (Codex design-review R3/R4).
# --------------------------------------------------------------------------------------
def find_run_clearance(events: list[dict], decomposition_run_id: str,
                       work_breakdown_sha256: str) -> dict | None:
    """The LATEST well-formed children_plan_cleared for this run whose work_breakdown_sha256 matches,
    or None. Latest-wins (append order) mirrors frozen_child_record: a re-run's fresh clearance
    supersedes a stale one (all clearances are sweep-emitted — B1 — so the latest is trusted)."""
    found = None
    for ev in dev.select(events, dev.CHILDREN_PLAN_CLEARED, decomposition_run_id):
        if ev.get("work_breakdown_sha256") == work_breakdown_sha256:
            found = ev
    return found


def plan_clearance_record(events: list[dict], frozen_event: dict, frozen_child: dict) -> dict | None:
    """The trusted clearance child-entry for ONE frozen child, bound to the SAME run + work-breakdown as
    its freeze event, or None. Requires a children_plan_cleared (this run + wb) with a child entry
    matching task_id + job_id + BOTH hashes — so a clearance from another run, a reused job_id, or
    identical bytes in a different universe cannot suppress the build-time review (R3)."""
    clearance = find_run_clearance(events, frozen_event.get("decomposition_run_id"),
                                   frozen_event.get("work_breakdown_sha256"))
    if clearance is None:
        return None
    for c in clearance["children"]:
        if (c.get("task_id") == frozen_child.get("task_id")
                and c.get("job_id") == frozen_child.get("job_id")
                and c.get("idea_contract_sha256") == frozen_child.get("idea_contract_sha256")
                and c.get("product_plan_sha256") == frozen_child.get("product_plan_sha256")):
            return c
    return None


def _d9_plan_clearance_violations(events_path: Path, run_id: str, wb: dict, wb_sha256: str,
                                  frozen_children: list[dict]) -> list[dict]:
    """R3/R4/R6: D9 refuses to freeze unless THIS run's children_plan_cleared covers EXACTLY this frozen
    set on identical bytes + the same criteria universe. Returns D9-PC violations (empty = cleared)."""
    clearance = find_run_clearance(dev.read_events(events_path), run_id, wb_sha256)
    if clearance is None:
        return [{"check": "D9-PC", "detail": "no children_plan_cleared for this run/work-breakdown — run "
                 "the plan-readiness gate (tools.decompose_plan_gate) before freezing"}]
    ref_sha = (wb.get("parent_criteria_ref") or {}).get("sha256")
    if clearance.get("criteria_sha256") != ref_sha:
        return [{"check": "D9-PC", "detail": "clearance criteria_sha256 != work-breakdown "
                 "parent_criteria_ref.sha256 (cleared against a different criteria universe)"}]
    cl_children = clearance["children"]  # is_well_formed guarantees a non-empty list of full entries
    cl_jobs = [c.get("job_id") for c in cl_children]
    if len(set(cl_jobs)) != len(cl_jobs):
        return [{"check": "D9-PC", "detail": "clearance has duplicate job_ids"}]
    cl_by_job = {c.get("job_id"): c for c in cl_children}
    fz_by_job = {c["job_id"]: c for c in frozen_children}
    if set(cl_by_job) != set(fz_by_job):
        return [{"check": "D9-PC", "detail": f"clearance child set {sorted(cl_by_job)} != "
                 f"frozen set {sorted(fz_by_job)} (not a 1:1 cover)"}]
    out: list[dict] = []
    for job, fz in fz_by_job.items():
        cl = cl_by_job[job]
        if (cl.get("task_id"), cl.get("idea_contract_sha256"), cl.get("product_plan_sha256")) != \
           (fz["task_id"], fz["idea_contract_sha256"], fz["product_plan_sha256"]):
            out.append({"check": "D9-PC", "detail": f"clearance for job {job} != frozen task/bytes "
                        "(plan-cleared bytes differ from the bytes being frozen)"})
    return out


# --------------------------------------------------------------------------------------
# D6 — size ceiling (pure core; gate_sweep gathers the ACTUAL counts from trusted artifacts)
# --------------------------------------------------------------------------------------
def d6_over_cap(file_count: int, check_count: int,
                file_cap: int = FILE_CAP, check_cap: int = CHECK_CAP) -> list[dict]:
    out = []
    if file_count > file_cap:
        out.append({"check": "D6", "detail": f"actual files {file_count} > cap {file_cap} — child ballooned, re-split"})
    if check_count > check_cap:
        out.append({"check": "D6", "detail": f"actual checks {check_count} > cap {check_cap} — child ballooned, re-split"})
    return out


def actual_in_scope_file_count(track: Path, worktree: Path, base: str) -> int:
    """Count of committed changed files in the worktree that fall WITHIN the product-plan
    allowed_paths — the same trusted basis scope_check uses (git diff + allowed_paths).

    D6 counts IN-SCOPE files (subtracting out-of-scope violations) ONLY because scope_check runs as
    a SEPARATE hard gate in the same diff-gate (gate_sweep), which independently HOLDs any file that
    escapes allowed_paths (Codex P4 LOW). D6's job is the size ceiling, not the sandbox boundary."""
    res = scope_check.check_scope(Path(track), Path(worktree), base)
    return res["changed_count"] - len(res["violations"])
