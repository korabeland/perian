#!/usr/bin/env python3
"""Deterministic DECOMPOSE gate (D1-D8 pre-build) — is an idea's split into child tracks honest?

WHY (design §3, §5; the scope-gate law generalized): the local doer (Main) proposes a
work-breakdown (children + coverage + produces/requires + depends_on). This tool is the
unforgeable trust layer between that doer and the Codex fidelity judge: it does pure structural
math and reads ONLY trusted records —
  - the wrapper-emitted ``criteria_frozen`` / ``large_override`` events (Main is fs-jailed out of
    events.jsonl; the freeze-wrapper, outside Main's sandbox, is the only writer), and
  - the on-disk bytes it rehashes itself (parent-criteria.json),
never a field the doer authored to describe its own honesty. It renders zero judgment about
whether the split is a *good* idea (that's Codex C1-C5); it only proves the split is a faithful,
buildable PARTITION of a Codex-frozen criteria set.

Checks (all pre-build; D9/D6 are post-emit, in decompose_check_post / gate_sweep):
  D0  work-breakdown + parent-criteria are schema-valid (can't reason about a malformed doc).
  D1  Freeze integrity — sha256(parent-criteria.json) == criteria_sha256 in THIS run's
      wrapper-emitted criteria_frozen event (no event / mismatch / stale run-id -> FAIL). The
      doer's advisory parent_criteria_ref.sha256 must also match the frozen bytes.
  D2  Coverage = exact partition — the multiset of coverage criterion_ids equals the frozen id
      set (each owned once; no drop/extra/dup) AND each child's parent_criteria_refs equals the
      coverage rows it owns.
  D3  Owner validity — every coverage owner_task_id is a declared child.
  D4  Acyclic + buildable — depends_on topo-sorts (no cycle, no dangling id).
  D5  Dependency closure — produces are namespaced to their producer; every requires resolves to
      a producer IN that child's transitive depends_on closure, OR a verified external_input.
  D7  Large-child guard — a tool-derived 'L' child FAILS unless a matching non-stale
      wrapper-emitted large_override (same run-id + work_breakdown + criteria hashes + task) exists.
  D8  Provenance — authored_by present; the certifier (frozen_by / granted_by, from wrapper
      events) differs from authored_by.

Usage:
  python -m tools.decompose_check --work-breakdown <wb.json> --parent-criteria <pc.json> \
      --events <events.jsonl> --run-id <id> [--json]
  python -m tools.decompose_check --self-test

Exit: 0 = honest partition · 2 = >=1 violation (HOLD) · 1 = indeterminate (IO/parse) — FAIL SAFE,
never a pass on a broken check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from tools import decompose_events as dev

ATB_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ATB_ROOT / "schemas"


class Indeterminate(Exception):
    """Raised when the verdict can't be computed (IO/parse) -> CLI fail-safe (exit 1)."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


# --------------------------------------------------------------------------------------
# Individual checks — each returns a list of violation dicts {"check","detail"}.
# --------------------------------------------------------------------------------------
def d0_schema(wb: dict, pc: dict) -> list[dict]:
    out = []
    for label, doc, schema_name in [
        ("work-breakdown", wb, "work-breakdown.schema.json"),
        ("parent-criteria", pc, "parent-criteria.schema.json"),
    ]:
        v = Draft202012Validator(_load_schema(schema_name))
        errs = sorted(v.iter_errors(doc), key=lambda e: e.path)
        if errs:
            first = errs[0]
            out.append({"check": "D0", "detail": f"{label} schema-invalid: {first.message} at {list(first.path)}"})
    return out


def d1_freeze_integrity(wb: dict, pc: dict, pc_path: Path, events: list[dict],
                        run_id: str) -> tuple[list[dict], str | None]:
    """Returns (violations, verified_criteria_sha256_or_None). The freeze must be a wrapper-emitted
    criteria_frozen for THIS run whose criteria_sha256 == the on-disk parent-criteria.json bytes,
    and the parent_idea_id must agree across the work-breakdown, the criteria file, and the event
    (Codex P3 HIGH — so one parent's split can't ride another's frozen criteria)."""
    frozen = dev.select(events, dev.CRITERIA_FROZEN, decomposition_run_id=run_id)
    if not frozen:
        return [{"check": "D1", "detail": f"no wrapper-emitted criteria_frozen for run {run_id!r}"}], None
    if len(frozen) > 1:
        return [{"check": "D1", "detail": f"multiple criteria_frozen for run {run_id!r} (ambiguous freeze)"}], None
    ev = frozen[0]
    file_sha = _sha256_file(pc_path)
    if file_sha != ev.get("criteria_sha256"):
        return [{"check": "D1", "detail": "parent-criteria.json bytes != frozen criteria_sha256 "
                 "(moved/edited after freeze, or stale run)"}], None
    # The doer's advisory pointer must agree with the frozen bytes too.
    ptr = (wb.get("parent_criteria_ref") or {}).get("sha256")
    if ptr is not None and ptr != file_sha:
        return [{"check": "D1", "detail": "work-breakdown.parent_criteria_ref.sha256 != frozen bytes"}], None
    # parent_idea_id must be the SAME across all three records.
    ids = {wb.get("parent_idea_id"), pc.get("parent_idea_id"), ev.get("parent_idea_id")}
    if len(ids) != 1 or None in ids:
        return [{"check": "D1", "detail": f"parent_idea_id disagrees across work-breakdown / criteria / "
                 f"freeze event: {sorted(str(i) for i in ids)}"}], None
    return [], file_sha


def d2_coverage_partition(wb: dict, pc: dict) -> list[dict]:
    out = []
    frozen_ids = [c["id"] for c in pc.get("criteria", [])]
    if len(frozen_ids) != len(set(frozen_ids)):
        out.append({"check": "D2", "detail": "frozen criteria set has duplicate ids (unpartitionable)"})
        return out
    frozen_set = set(frozen_ids)
    cov = wb.get("coverage", [])
    cov_ids = [row["criterion_id"] for row in cov]
    # exact partition: each frozen id owned exactly once, nothing extra, no dup.
    seen = {}
    for cid in cov_ids:
        seen[cid] = seen.get(cid, 0) + 1
    dups = sorted(k for k, n in seen.items() if n > 1)
    missing = sorted(frozen_set - set(cov_ids))
    extra = sorted(set(cov_ids) - frozen_set)
    if dups:
        out.append({"check": "D2", "detail": f"criteria owned more than once: {dups}"})
    if missing:
        out.append({"check": "D2", "detail": f"frozen criteria with no owner: {missing}"})
    if extra:
        out.append({"check": "D2", "detail": f"coverage references unknown criteria: {extra}"})
    # per-child parent_criteria_refs must equal the coverage rows that child owns.
    owned_by = {}
    for row in cov:
        owned_by.setdefault(row["owner_task_id"], set()).add(row["criterion_id"])
    for child in wb.get("children", []):
        tid = child["task_id"]
        ref_list = child.get("parent_criteria_refs", [])
        if len(ref_list) != len(set(ref_list)):
            out.append({"check": "D2", "detail": f"child {tid} parent_criteria_refs has duplicate ids "
                        f"{sorted(ref_list)} (ownership must be exact, not just a covering set)"})
        refs = set(ref_list)
        if refs != owned_by.get(tid, set()):
            out.append({"check": "D2", "detail": f"child {tid} parent_criteria_refs {sorted(refs)} != "
                        f"its coverage rows {sorted(owned_by.get(tid, set()))}"})
    return out


def d3_owner_validity(wb: dict) -> list[dict]:
    declared = {c["task_id"] for c in wb.get("children", [])}
    bad = sorted({row["owner_task_id"] for row in wb.get("coverage", [])} - declared)
    if bad:
        return [{"check": "D3", "detail": f"coverage owner_task_id not a declared child: {bad}"}]
    return []


def d4_acyclic(wb: dict) -> tuple[list[dict], dict]:
    """Kahn topo-sort over depends_on. Returns (violations, closure_map). closure_map[task] =
    set of all transitive depends_on ancestors (computed only if acyclic + no dangling)."""
    children = wb.get("children", [])
    nodes = {c["task_id"] for c in children}
    deps = {c["task_id"]: list(c.get("depends_on", [])) for c in children}
    dangling = sorted({d for ds in deps.values() for d in ds} - nodes)
    if dangling:
        return [{"check": "D4", "detail": f"depends_on references unknown task(s): {dangling}"}], {}
    # Kahn: in-degree = number of deps each node has.
    indeg = {n: len(deps[n]) for n in nodes}
    queue = [n for n in nodes if indeg[n] == 0]
    seen = 0
    # adjacency: dep -> dependents
    dependents = {n: [] for n in nodes}
    for n in nodes:
        for d in deps[n]:
            dependents[d].append(n)
    q = list(queue)
    while q:
        n = q.pop()
        seen += 1
        for m in dependents[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    if seen != len(nodes):
        return [{"check": "D4", "detail": "dependency cycle in depends_on (not topo-sortable)"}], {}
    # transitive closure of ancestors
    closure = {}
    for n in nodes:
        acc, stack = set(), list(deps[n])
        while stack:
            d = stack.pop()
            if d in acc:
                continue
            acc.add(d)
            stack.extend(deps[d])
        closure[n] = acc
    return [], closure


def _git_blob_sha256(repo: Path, commit: str, path: str) -> str | None:
    """sha256 of the file CONTENT at <commit>:<path> in `repo`, or None if it can't be read."""
    if not (commit and path):
        return None
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", f"{commit}:{path}"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return hashlib.sha256(cp.stdout).hexdigest()


def _external_source_verified(source_ref: dict, events: list[dict], repo: Path) -> bool:
    """Bind a doer-declared external source to a TRUSTED record (Codex P3 BLOCKER 1) — the `kind`
    LABEL alone proves nothing, and mere event PRESENCE is not provenance (Codex P3 r2: a
    criteria_frozen verdict_id is not an attestation of external content).

    Only ``git`` is soundly verifiable today: the blob content at commit:path is hashed and must
    equal the declared sha256 in the overseer-controlled repo — an unforgeable content binding.
    ``sweep-fetch`` and ``frozen-verdict`` stay RESERVED in the schema (the design's enumerated
    types) but FAIL CLOSED here: the §4b sweep-fetch shape {anchor,event_id} does not even align
    with the real sweep-owned fetch event {url,sha256,path}, and no content-attesting verdict event
    exists yet — so there is no trusted record to bind them to. They become acceptable only when
    their binding is wired to the actual event shape (a scoped follow-up the first time a real
    external input of that kind appears). Until then: unverifiable -> HOLD."""
    if not isinstance(source_ref, dict):
        return False
    if source_ref.get("kind") == "git":
        want = source_ref.get("sha256")
        got = _git_blob_sha256(repo, source_ref.get("commit"), source_ref.get("path"))
        return bool(want) and got == want
    return False


def d5_dependency_closure(wb: dict, closure: dict, events: list[dict], repo: Path) -> list[dict]:
    out = []
    children = wb.get("children", [])
    # produces namespacing + producer map (reparse canonical ids — Codex P1 carry-forward).
    producer_of = {}
    for c in children:
        tid = c["task_id"]
        for p in c.get("produces", []):
            prefix = p.split(".", 1)[0]
            if prefix != tid:
                out.append({"check": "D5", "detail": f"child {tid} produces {p!r} not namespaced to itself"})
                continue
            if p in producer_of:
                out.append({"check": "D5", "detail": f"output id {p!r} produced by two children "
                            f"({producer_of[p]}, {tid})"})
                continue
            producer_of[p] = tid
    external_by_id = {e["id"]: (e.get("source_ref") or {}) for e in wb.get("external_inputs", [])}
    for c in children:
        tid = c["task_id"]
        for r in c.get("requires", []):
            # PRODUCER-FIRST (Codex P3 BLOCKER 2): an id produced by ANY child is an INTERNAL
            # product and must resolve through the ordered closure — it can never be laundered as
            # external (even if the doer also lists it in external_inputs).
            prod = producer_of.get(r)
            if prod is not None:
                if prod not in closure.get(tid, set()):
                    out.append({"check": "D5", "detail": f"child {tid} requires {r!r} produced by {prod}, "
                                f"but {prod} is not in its depends_on closure (unordered dependency)"})
                continue
            if r in external_by_id:
                if not _external_source_verified(external_by_id[r], events, repo):
                    out.append({"check": "D5", "detail": f"child {tid} requires external {r!r} whose "
                                "source_ref does not bind to a trusted record (forged/unverifiable)"})
                continue
            out.append({"check": "D5", "detail": f"child {tid} requires {r!r} — no producer and not a "
                        "declared external input (laundered/forged dependency)"})
    return out


def d7_large_guard(wb: dict, events: list[dict], run_id: str, wb_sha256: str,
                   criteria_sha256: str, parent_idea_id: str) -> list[dict]:
    out = []
    overrides = dev.select(events, dev.LARGE_OVERRIDE, decomposition_run_id=run_id)
    for c in wb.get("children", []):
        tid = c["task_id"]
        new_loc = (c.get("size_estimate") or {}).get("new_loc")
        bucket = dev.derive_complexity(new_loc)
        if bucket != "L":
            continue
        # The override must bind to THIS run, parent, work-breakdown bytes, criteria bytes, task,
        # and bucket — so a stale (earlier-wb) or cross-parent override can't cover this child.
        match = [o for o in overrides
                 if o.get("task_id") == tid
                 and o.get("derived_bucket") == "L"
                 and o.get("parent_idea_id") == parent_idea_id
                 and o.get("work_breakdown_sha256") == wb_sha256
                 and o.get("criteria_sha256") == criteria_sha256]
        if not match:
            out.append({"check": "D7", "detail": f"child {tid} is tool-derived 'L' (new_loc={new_loc}) "
                        "without a matching non-stale large_override for this run+work-breakdown"})
    return out


def d8_provenance(wb: dict, events: list[dict], run_id: str) -> list[dict]:
    out = []
    author = wb.get("authored_by")
    if not author:
        out.append({"check": "D8", "detail": "work-breakdown has no authored_by"})
        return out
    certifiers = set()
    for ev in dev.select(events, dev.CRITERIA_FROZEN, run_id):
        certifiers.add(ev.get("frozen_by"))
    for ev in dev.select(events, dev.LARGE_OVERRIDE, run_id):
        certifiers.add(ev.get("granted_by"))
    if author in certifiers:
        out.append({"check": "D8", "detail": f"authored_by {author!r} == certifier (doer == certifier)"})
    return out


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
def _dup_task_ids(wb: dict) -> list[str]:
    ids = [c.get("task_id") for c in wb.get("children", [])]
    seen, dups = set(), set()
    for i in ids:
        (dups if i in seen else seen).add(i)
    return sorted(dups)


def check_decomposition(work_breakdown_path: Path, parent_criteria_path: Path,
                        events_path: Path, run_id: str, repo: Path | None = None) -> dict:
    """Run D0..D8. Returns {ok, violations}. Raises Indeterminate on IO/parse (CLI -> exit 1).
    `repo` is the git repo against which kind:git external sources are verified (default ATB_ROOT)."""
    repo = repo or ATB_ROOT
    try:
        wb = json.loads(Path(work_breakdown_path).read_text(encoding="utf-8"))
        pc = json.loads(Path(parent_criteria_path).read_text(encoding="utf-8"))
        events = dev.read_events(events_path)
        wb_sha256 = _sha256_file(work_breakdown_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise Indeterminate(str(exc)) from exc

    # D0 first — everything downstream assumes a structurally-valid doc.
    v0 = d0_schema(wb, pc)
    if v0:
        return {"ok": False, "violations": v0}

    # Duplicate child task_ids collapse ownership / closure / override binding into ambiguity —
    # reject before any id-keyed check runs (Codex P3 BLOCKER 3). Short-circuit like D0/D1.
    dups = _dup_task_ids(wb)
    if dups:
        return {"ok": False, "violations": [{"check": "D3", "detail": f"duplicate child task_id(s): {dups} "
                 "(child identities must be unique)"}]}

    # D1 next — the criteria set isn't trustworthy until the freeze is verified.
    v1, criteria_sha256 = d1_freeze_integrity(wb, pc, parent_criteria_path, events, run_id)
    if v1:
        return {"ok": False, "violations": v1}
    parent_idea_id = wb.get("parent_idea_id")

    violations: list[dict] = []
    violations += d2_coverage_partition(wb, pc)
    violations += d3_owner_validity(wb)
    v4, closure = d4_acyclic(wb)
    violations += v4
    # D5 needs the closure; only meaningful if D4 produced one (acyclic + no dangling).
    if not v4:
        violations += d5_dependency_closure(wb, closure, events, repo)
    violations += d7_large_guard(wb, events, run_id, wb_sha256, criteria_sha256, parent_idea_id)
    violations += d8_provenance(wb, events, run_id)
    return {"ok": not violations, "violations": violations}


# --------------------------------------------------------------------------------------
# Self-test (honest tree passes; one mutation per check fails) + CLI
# --------------------------------------------------------------------------------------
def _self_test() -> int:
    import tempfile

    from tools import decompose_fixtures as fx

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        h = fx.write_tree(Path(td) / "honest")
        res = check_decomposition(h["work_breakdown_path"], h["parent_criteria_path"], h["events_path"], h["run_id"])
        if not res["ok"]:
            failures.append(f"honest tree should pass: {res['violations']}")

        # No freeze event -> D1.
        h2 = fx.write_tree(Path(td) / "nofreeze", emit_criteria_frozen=False)
        r2 = check_decomposition(h2["work_breakdown_path"], h2["parent_criteria_path"], h2["events_path"], h2["run_id"])
        if r2["ok"] or r2["violations"][0]["check"] != "D1":
            failures.append(f"missing freeze should fail D1: {r2}")

    if failures:
        print("decompose_check SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("decompose_check self-test: OK (honest passes; missing freeze -> D1)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="decompose_check",
                                 description="Deterministic decompose gate (D1-D8, trusted records only).")
    ap.add_argument("--work-breakdown")
    ap.add_argument("--parent-criteria")
    ap.add_argument("--events", default=str(ATB_ROOT / "state" / "events.jsonl"))
    ap.add_argument("--run-id")
    ap.add_argument("--repo", default=str(ATB_ROOT),
                    help="git repo for verifying kind:git external sources (default ATB root)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not (args.work_breakdown and args.parent_criteria and args.run_id):
        print("error: --work-breakdown, --parent-criteria, --run-id required (or --self-test)", file=sys.stderr)
        return 1

    try:
        result = check_decomposition(Path(args.work_breakdown), Path(args.parent_criteria),
                                     Path(args.events), args.run_id, repo=Path(args.repo))
    except Indeterminate as exc:
        print(f"error: decompose check could not run — failing safe (held): {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("[decompose] OK — honest partition (D1-D8)")
    else:
        print(f"[decompose] HELD — {len(result['violations'])} violation(s):")
        for v in result["violations"]:
            print(f"  - {v['check']}: {v['detail']}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
