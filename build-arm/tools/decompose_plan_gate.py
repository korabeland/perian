#!/usr/bin/env python3
"""Plan-readiness pre-clearance for the decomposition step — sweep-owned, runs OUTSIDE Main's sandbox.

WHY IT EXISTS (design docs/plans/2026-06-18-001; first observed live 2026-06-18):
Decomposition's fidelity review certifies "does each child COVER the idea"; it does NOT certify "is
each child's plan BUILD-READY" (the per-track plan-gate's bar). A child can pass the first and fail the
second — and once D9 byte-freezes it, the plan-gate hold can't be patched in place (B3 re-verifies the
frozen bytes), forcing a full re-decompose loop. This tool shifts the plan-gate LEFT: it runs the
IDENTICAL `codex_review --mode plan` on each STAGED child and, only if EVERY child approves, emits ONE
trusted `children_plan_cleared` event pinning each child's hashes + the structured verdict. D9
(`decompose_post.check_d9_and_freeze`) then REFUSES to freeze unless a matching clearance exists, so
**frozen ⟹ plan-cleared**, enforced by a tool — not by the overseer remembering a step.

Because this runs in the deterministic sweep process (NOT the fs-jailed model spawn), every line it
writes is provably wrapper-written (B1 — Main is fs-jailed out of events.jsonl). FAIL-SAFE: any child
holds / errors / is malformed, or the criteria binding mismatches → emit NOTHING, return ok=False with
per-child findings (the overseer strengthens those plans and re-runs). Mirrors decompose_freeze's
"never auto-pass".

REVIEWED-BYTES == HASHED-BYTES (Codex design-review R1): each staged idea-contract/product-plan is read
ONCE; the SAME buffers are hashed AND (in-process) used to build the review prompt — never re-read by a
codex_review subprocess (whose main() would independently re-open the files, a TOCTOU). The clearance
binds exactly the bytes Codex saw, which are exactly the bytes D9 freezes.

The staged contract/plan content is treated as UNTRUSTED data (codex_review's prompt fences it).

Usage (production, sweep-invoked):
  python -m tools.decompose_plan_gate --work-breakdown <wb.json> --parent-idea <parent idea-contract.json> \
      --criteria <parent-criteria-frozen.json> --staged-root <dir of <task_id>/ child dirs> --run-id <id> \
      [--events <events.jsonl>]
  python -m tools.decompose_plan_gate --self-test

Exit: 0 = clearance emitted (every child approved) · 2 = not cleared (a child held/errored — fail-safe,
no event) · 1 = usage / IO error (fail-safe, no event).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools import codex_review as cr
from tools import decompose_events as dev

ATB_ROOT = Path(__file__).resolve().parent.parent

# The pre-clearance review is the ONLY plan review a decomposition child gets (build-time trusts the
# clearance — design Part 2), so it runs at HIGH reasoning effort: the strongest look before the
# irreversible freeze. Build-time never re-reviews a cleared child, so there is no effort mismatch.
_REVIEW_EFFORT = "high"
_REVIEW_TIMEOUT = 180


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical_json_bytes(obj) -> bytes:
    """Deterministic serialization for the tamper-evident verdict_id (mirrors decompose_freeze)."""
    return (json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_review_fn(prompt: str) -> dict:
    """Run the IDENTICAL codex_review plan reviewer in-process (same prompt builder + schema + runner),
    returning the structured verdict dict. Injectable so tests never hit Codex. Raises on codex error
    (the caller fails safe for that child)."""
    return cr.run_codex(prompt, _REVIEW_EFFORT, _REVIEW_TIMEOUT, None)


def review_one_child(ic_bytes: bytes, pp_bytes: bytes, review_fn=_default_review_fn) -> dict:
    """Plan-review ONE staged child from bytes ALREADY IN HAND (no re-read — reviewed == hashed, R1).
    The product-plan is the reviewed content and the idea-contract supplies the spec context, EXACTLY as
    the build-time plan-gate derives them (codex_review --input product-plan.json --contract
    idea-contract.json). Returns {ic_sha256, pp_sha256, verdict, approved, verdict_id, reason, findings}.
    Raises on a malformed idea-contract or a review error (the caller fails safe)."""
    ic_sha256 = sha256_bytes(ic_bytes)
    pp_sha256 = sha256_bytes(pp_bytes)
    ic = json.loads(ic_bytes)              # parse the SAME buffer we hashed
    pp_text = pp_bytes.decode("utf-8")     # product-plan == build-time --input content
    context = cr.context_from_contract_obj(ic)   # IDENTICAL spec context the build-time gate derives
    prompt = cr.build_prompt("plan", pp_text, context)
    verdict = review_fn(prompt)
    if not isinstance(verdict, dict):
        raise RuntimeError(f"review_fn returned a non-dict verdict ({type(verdict).__name__})")
    v = (verdict.get("verdict") or "").strip().lower()
    return {
        "ic_sha256": ic_sha256,
        "pp_sha256": pp_sha256,
        "verdict": v,
        "approved": v == "approve",
        "verdict_id": sha256_bytes(canonical_json_bytes(verdict)),
        "reason": str(verdict.get("reason", "")),
        "findings": [f for f in (verdict.get("findings") or []) if isinstance(f, str)],
    }


def clear_child_plans(
    *,
    work_breakdown_path: Path,
    parent_idea_path: Path,
    criteria_path: Path,
    staged_dirs: dict,                # {task_id -> staged dir Path holding idea-contract.json + product-plan.json}
    decomposition_run_id: str,
    events_path: Path,
    review_fn=None,
    now_iso: str | None = None,
) -> dict:
    """Plan-review EVERY staged child; on ALL-approve emit ONE children_plan_cleared, else emit NOTHING.

    The clearance pins each child's idea-contract/product-plan sha256 (== the bytes D9 freezes) + a
    content-hashed verdict_id, and binds parent_idea_sha256 + criteria_sha256 to this run's criteria
    universe (replay-proof; D9 + the build-time gate check the match). Returns
    {ok, reason, event?, results:[{task_id, job_id?, verdict?, approved, reason?, findings?}]}.
    ok=False ⇒ NOTHING emitted (fail-closed: no clearance ⇒ D9 refuses to freeze)."""
    # Resolve the default at CALL time (not at def time) so a test/CLI monkeypatch of _default_review_fn
    # is honored even on the no-arg path.
    if review_fn is None:
        review_fn = _default_review_fn
    now_iso = now_iso or _now_iso()
    try:
        wb_bytes = Path(work_breakdown_path).read_bytes()
        wb = json.loads(wb_bytes)
        wb_sha256 = sha256_bytes(wb_bytes)
        parent_idea_sha256 = sha256_bytes(Path(parent_idea_path).read_bytes())
        criteria_sha256 = sha256_bytes(Path(criteria_path).read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": f"cannot read inputs: {exc}", "results": []}

    children = wb.get("children") or []
    if not children:
        return {"ok": False, "reason": "work-breakdown has no children", "results": []}

    # Bind the clearance to the SAME criteria the work-breakdown was built against (R4). A mismatch means
    # we'd clear against a different criteria universe than the freeze — refuse, emit nothing.
    ref_sha = (wb.get("parent_criteria_ref") or {}).get("sha256")
    if ref_sha != criteria_sha256:
        return {"ok": False, "reason": f"criteria file sha {criteria_sha256[:12]} != work-breakdown "
                f"parent_criteria_ref.sha256 {str(ref_sha)[:12]} — refusing to clear", "results": []}

    results: list[dict] = []
    cleared_children: list[dict] = []
    all_ok = True
    for child in children:
        tid = child.get("task_id")
        entry: dict = {"task_id": tid, "approved": False}
        sdir = staged_dirs.get(tid)
        if sdir is None:
            entry["reason"] = "no staged dir"
            results.append(entry); all_ok = False; continue
        try:
            ic_bytes = (Path(sdir) / "idea-contract.json").read_bytes()
            pp_bytes = (Path(sdir) / "product-plan.json").read_bytes()
        except OSError as exc:
            entry["reason"] = f"cannot read staged bytes: {exc}"
            results.append(entry); all_ok = False; continue
        try:
            job_id = json.loads(ic_bytes).get("job_id")
        except json.JSONDecodeError as exc:
            entry["reason"] = f"staged idea-contract not JSON: {exc}"
            results.append(entry); all_ok = False; continue
        try:
            r = review_one_child(ic_bytes, pp_bytes, review_fn)
        except Exception as exc:  # malformed pp / review error -> fail safe for THIS child
            entry["reason"] = f"review error: {exc}"
            results.append(entry); all_ok = False; continue
        entry.update({"job_id": job_id, "verdict": r["verdict"], "approved": r["approved"],
                      "reason": r["reason"], "findings": r["findings"]})
        results.append(entry)
        if r["approved"] and job_id:
            cleared_children.append({
                "task_id": tid, "job_id": job_id,
                "idea_contract_sha256": r["ic_sha256"],
                "product_plan_sha256": r["pp_sha256"],
                "verdict_id": r["verdict_id"],
            })
        else:
            all_ok = False  # approved-but-no-job_id is also un-clearable

    if not all_ok:
        return {"ok": False, "reason": "not all children plan-cleared", "results": results}

    payload = dev.make_children_plan_cleared(
        parent_idea_id=wb.get("parent_idea_id"),
        parent_idea_sha256=parent_idea_sha256,
        criteria_sha256=criteria_sha256,
        decomposition_run_id=decomposition_run_id,
        work_breakdown_sha256=wb_sha256,
        children=cleared_children,
        cleared_at=now_iso,
    )
    try:
        dev.append_event(events_path, payload, now_iso)
    except OSError as exc:
        return {"ok": False, "reason": f"cannot emit children_plan_cleared: {exc}", "results": results}
    return {"ok": True, "reason": "all children plan-cleared", "event": payload, "results": results}


# --------------------------------------------------------------------------------------
# Self-test (no Codex, no real I/O beyond a tmp dir) + CLI
# --------------------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile as _tf

    failures: list[str] = []
    with _tf.TemporaryDirectory() as td:
        root = Path(td)
        events = root / "events.jsonl"
        events.write_text("")  # parent dir must exist for append_event
        (root / "parent-idea.json").write_text(json.dumps({"job_id": "p1", "what_should_exist": "x"}))
        crit_bytes = json.dumps({"parent_idea_id": "p1", "criteria": [{"id": "u1", "text": "a"},
                                                                      {"id": "u2", "text": "b"}]}).encode()
        (root / "criteria.json").write_bytes(crit_bytes)
        crit_sha = sha256_bytes(crit_bytes)
        wb = {"parent_idea_id": "p1", "parent_criteria_ref": {"file": "criteria.json", "sha256": crit_sha},
              "children": [{"task_id": "a"}, {"task_id": "b"}]}
        (root / "wb.json").write_text(json.dumps(wb))
        staged = {}
        for tid, job in (("a", "job-a"), ("b", "job-b")):
            d = root / "staged" / tid
            d.mkdir(parents=True)
            (d / "idea-contract.json").write_text(json.dumps({"job_id": job, "what_should_exist": tid}))
            (d / "product-plan.json").write_text(json.dumps({"job_id": job, "plan": tid}))
            staged[tid] = d

        def approve_fn(prompt):
            return {"verdict": "approve", "reason": "ok", "findings": [], "confidence": "high"}

        def hold_b_fn(prompt):
            # 'b' carries its task id in the product-plan content -> hold only b
            return ({"verdict": "needs_changes", "reason": "gap", "findings": ["fix b"], "confidence": "high"}
                    if '"plan": "b"' in prompt else
                    {"verdict": "approve", "reason": "ok", "findings": [], "confidence": "high"})

        def err_fn(prompt):
            raise RuntimeError("codex down")

        kw = dict(work_breakdown_path=root / "wb.json", parent_idea_path=root / "parent-idea.json",
                  criteria_path=root / "criteria.json", staged_dirs=staged, decomposition_run_id="run1",
                  events_path=events, now_iso="2026-06-18T00:00:00Z")

        # all approve -> event emitted, well-formed, hashes bind the staged bytes
        r = clear_child_plans(**kw, review_fn=approve_fn)
        if not r.get("ok"):
            failures.append(f"all-approve should clear: {r.get('reason')}")
        elif not dev.is_well_formed(r["event"]):
            failures.append("emitted children_plan_cleared is not well-formed")
        else:
            staged_a_pp = sha256_bytes((staged["a"] / "product-plan.json").read_bytes())
            got = next((c for c in r["event"]["children"] if c["task_id"] == "a"), {})
            if got.get("product_plan_sha256") != staged_a_pp:
                failures.append("clearance product_plan_sha256 != staged file hash (reviewed!=hashed)")

        # one child holds -> NO new event line
        before = events.read_text().count("\n")
        r2 = clear_child_plans(**kw, review_fn=hold_b_fn)
        if r2.get("ok") or events.read_text().count("\n") != before:
            failures.append("a single held child must emit nothing")

        # review error -> NO new event line
        r3 = clear_child_plans(**kw, review_fn=err_fn)
        if r3.get("ok") or events.read_text().count("\n") != before:
            failures.append("a review error must emit nothing")

        # criteria-mismatch -> NO new event line (replay/cross-universe guard)
        wb_bad = dict(wb, parent_criteria_ref={"file": "criteria.json", "sha256": "deadbeef"})
        (root / "wb_bad.json").write_text(json.dumps(wb_bad))
        r4 = clear_child_plans(**{**kw, "work_breakdown_path": root / "wb_bad.json"}, review_fn=approve_fn)
        if r4.get("ok") or events.read_text().count("\n") != before:
            failures.append("a criteria-ref mismatch must emit nothing")

    if failures:
        print("decompose_plan_gate SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("decompose_plan_gate self-test: OK (all-approve clears+hash-binds; hold/error/criteria-mismatch fail safe)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="decompose_plan_gate",
                                 description="Plan-readiness pre-clearance for decomposition children (sweep-owned).")
    ap.add_argument("--work-breakdown")
    ap.add_argument("--parent-idea")
    ap.add_argument("--criteria")
    ap.add_argument("--staged-root", help="dir holding one <task_id>/ subdir per child")
    ap.add_argument("--run-id")
    ap.add_argument("--events", default=str(ATB_ROOT / "state" / "events.jsonl"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not (args.work_breakdown and args.parent_idea and args.criteria and args.staged_root and args.run_id):
        print("error: needs --work-breakdown --parent-idea --criteria --staged-root --run-id (or --self-test)",
              file=sys.stderr)
        return 1
    try:
        wb = json.loads(Path(args.work_breakdown).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read work-breakdown: {exc}", file=sys.stderr)
        return 1
    staged_dirs = {c.get("task_id"): Path(args.staged_root) / str(c.get("task_id"))
                   for c in (wb.get("children") or [])}
    r = clear_child_plans(work_breakdown_path=Path(args.work_breakdown), parent_idea_path=Path(args.parent_idea),
                          criteria_path=Path(args.criteria), staged_dirs=staged_dirs,
                          decomposition_run_id=args.run_id, events_path=Path(args.events))
    print(json.dumps({"ok": r["ok"], "reason": r["reason"], "results": r.get("results", [])}, indent=2))
    if not r["ok"]:
        for res in r.get("results", []):
            if not res.get("approved"):
                print(f"[HOLD] {res.get('task_id')}: {res.get('reason', '')}", file=sys.stderr)
                for f in res.get("findings", []) or []:
                    print(f"  - {f}", file=sys.stderr)
    return 0 if r["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
