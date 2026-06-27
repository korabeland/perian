#!/usr/bin/env python3
"""Completeness-reconciliation check for overseer decision-records (meta-layer Phase 1).

WHY THIS EXISTS (what makes the overseer charter BITE):
A charter of good intentions is decoration unless violations are catchable. The charter says
every durable change to an agent's rewritable layers (identity/skills/tools/config) must be a
deliberate, grounded, independently-certified decision — recorded in an overseer-decision-
record. Auditing the records that EXIST cannot catch a change made with NO record. So this
tool pins "agent-layer change" to an OBSERVABLE, unforgeable source of truth — git commits in
the profiles repo — and flags any agent-layer change with no linked record.

THE BINDING (bidirectional, so neither half can be back-filled alone):
  - every profiles-repo commit that touches an agent-layer path MUST carry a
    `Decision-Record: <id>` git trailer, and
  - the record `<id>` MUST be a decision=layer-rewrite record whose `commit_hash` is that
    same commit AND whose status is applied/certified (a real durable change must be
    certified, not merely proposed/reverted).
A commit with no trailer, a trailer to a missing record, a record that doesn't point back, a
record citing a commit that doesn't exist OR isn't itself an agent-layer change, a record that
fails schema validation, or a duplicate record id — each is a HOLD. Merge commits are diffed
against every parent (-m) so a conflict-resolution edit to a layer file can't slip through;
records are validated against schemas/overseer-decision-record.schema.json so the conditional
invariants (applied layer-rewrite must cite a commit; no_change/defer must not; defer needs an
expiry) bite.

TRUST BOUNDARY (this is a deterministic tool, not a judge): it checks only that a record
EXISTS and is correctly LINKED. It NEVER judges whether the decision was good, whether the
grounding was adequate, or which layer to pick — that stays with Codex + the overseer + Korab
(doer != certifier). Schema-valid + linked != sound. (This mirrors scope_check: the path
question moved from fuzzy judgment to a deterministic, unbribable check.)

An agent-layer path (component-wise, generic over the agent dir, backups EXCLUDED):
  <agent>/SOUL.md            (exact — NOT SOUL.md.prebak.*)
  <agent>/config.yaml        (exact — NOT config.yaml.bak.*)
  <agent>/skills/**          (subtree)
  <agent>/tools/**           (subtree)
The first path component is treated as the agent dir generically, so adding a new crew agent
does NOT silently exempt its identity from the check (fail-safe).

Usage:
  python -m tools.decision_record_check --since <ref> [--profiles-repo DIR] [--records-dir DIR] [--json]
  python -m tools.decision_record_check --self-test

`--since` is the charter-baseline ref; the check audits <ref>..HEAD. It is REQUIRED (no
silent whole-history default that would either flag every pre-charter commit or pass nothing).

Exit: 0 = every audited agent-layer commit has a valid linked record (clean) ·
      2 = >=1 orphan / dangling / invalid (HOLD) ·
      1 = usage / git / IO error — FAIL SAFE: never a clean pass on a broken check.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from config import paths

ATB_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILES_REPO = paths.hermes_profiles()
DEFAULT_RECORDS_DIR = ATB_ROOT / "overseer" / "records"
RECORD_SCHEMA = ATB_ROOT / "schemas" / "overseer-decision-record.schema.json"

_LAYER_SUBTREES = ("skills", "tools")
_LAYER_FILES = ("SOUL.md", "config.yaml")
_VALIDATOR: Draft202012Validator | None = None


def _record_validator() -> Draft202012Validator:
    """Lazily build the decision-record schema validator (repo idiom: Draft202012Validator)."""
    global _VALIDATOR
    if _VALIDATOR is None:
        _VALIDATOR = Draft202012Validator(json.loads(RECORD_SCHEMA.read_text()))
    return _VALIDATOR


def is_agent_layer_path(path: str) -> bool:
    """Pure: does a repo-relative path denote a durable agent-LAYER change?

    Generic over the first component (the agent dir). Exact match for SOUL.md / config.yaml
    (so SOUL.md.prebak.* / config.yaml.bak.* — backups — do NOT count); subtree match for
    skills/ and tools/.
    """
    parts = Path(path).parts
    if len(parts) < 2:
        return False
    rest = parts[1:]
    if len(rest) == 1 and rest[0] in _LAYER_FILES:
        return True
    if len(rest) >= 2 and rest[0] in _LAYER_SUBTREES:
        return True
    return False


def commit_is_agent_layer(changed_files: list[str]) -> bool:
    """Pure: a commit is an agent-layer change iff >=1 of its files is an agent-layer path."""
    return any(is_agent_layer_path(f) for f in changed_files)


def reconcile(layer_commits, records, commit_status):
    """Pure core: reconcile agent-layer commits against decision-records.

    Args:
      layer_commits: list of {"sha": str, "trailer_id": str | None} — the audited agent-layer
                     commits and their parsed `Decision-Record:` trailer (None if absent).
      records:       dict id -> record dict (already shape-loaded + schema-valid).
      commit_status: callable(sha) -> 'missing' | 'layer' | 'nonlayer' (injected so this is
                     testable without git). 'layer' = the commit exists and itself touches an
                     agent-layer path; 'nonlayer' = exists but touches none; 'missing' = absent.

    Returns {ok, orphans, dangling, checked_commits, checked_records}. orphans/dangling are
    lists of {sha|record_id, reason}. The verdict is unforgeable given trusted inputs.
    """
    orphans = []
    for c in layer_commits:
        sha, tid = c["sha"], c["trailer_id"]
        if not tid:
            orphans.append({"sha": sha, "reason": "agent-layer commit has no Decision-Record trailer"})
            continue
        rec = records.get(tid)
        if rec is None:
            orphans.append({"sha": sha, "reason": f"trailer '{tid}' has no matching decision-record"})
            continue
        if rec.get("decision") != "layer-rewrite":
            orphans.append({"sha": sha, "reason": f"linked record '{tid}' decision is '{rec.get('decision')}', cannot back a layer change"})
            continue
        rec_hash = (rec.get("commit_hash") or "").strip()
        if not rec_hash:
            orphans.append({"sha": sha, "reason": f"linked record '{tid}' does not cite this commit (commit_hash empty)"})
        elif rec_hash != sha:
            orphans.append({"sha": sha, "reason": f"linked record '{tid}' cites commit '{rec_hash}', not this commit '{sha}'"})
        elif rec.get("status") not in ("applied", "certified"):
            orphans.append({"sha": sha, "reason": f"linked record '{tid}' status is '{rec.get('status')}', not applied/certified (a real change must be certified, not merely proposed)"})

    dangling = []
    for rid, rec in records.items():
        decision = rec.get("decision")
        rec_hash = (rec.get("commit_hash") or "").strip()
        if decision in ("no_change", "defer") and rec_hash:
            dangling.append({"record_id": rid, "reason": f"{decision} record must not cite a commit (commit_hash '{rec_hash}')"})
            continue
        if decision == "layer-rewrite" and rec_hash:
            st = commit_status(rec_hash)
            if st == "missing":
                dangling.append({"record_id": rid, "reason": f"record cites commit '{rec_hash}' which does not exist in the repo"})
            elif st == "nonlayer":
                dangling.append({"record_id": rid, "reason": f"record cites commit '{rec_hash}' which is not an agent-layer change"})

    ok = not orphans and not dangling
    return {
        "ok": ok,
        "orphans": orphans,
        "dangling": dangling,
        "checked_commits": len(layer_commits),
        "checked_records": len(records),
    }


# --------------------------------------------------------------------------- git/IO layer


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if cp.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {(cp.stderr or '').strip()}")
    return cp.stdout


def commits_in_range(repo: Path, since: str) -> list[str]:
    out = _git(repo, "log", "--format=%H", f"{since}..HEAD")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def changed_files_of(repo: Path, sha: str) -> list[str]:
    """Files changed by one commit, deduplicated.

    Flags chosen to FAIL SAFE — never miss a layer change:
      -m     diff a MERGE commit against EACH parent (a conflict-resolution edit to a layer
             file would be invisible without it — the fail-open hole Codex flagged). Erring
             toward over-inclusion on merges (a merge that touched a layer file requires a
             record) is the correct bias for a trust gate.
      --root the ROOT commit shows its full tree as additions (instead of nothing).
      -r     recurse into subtrees.
    No rename detection (-M) is requested, so a move is reported as delete(old)+add(new); the
    classifier then catches whichever side is an agent-layer path (both sides are visible).
    """
    out = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--root", sha)
    seen: list[str] = []
    s: set[str] = set()
    for ln in out.splitlines():
        f = ln.strip()
        if f and f not in s:
            s.add(f)
            seen.append(f)
    return seen


def decision_record_trailer(repo: Path, sha: str) -> str | None:
    out = _git(repo, "show", "--no-patch", "--format=%(trailers:key=Decision-Record,valueonly)", sha)
    for ln in out.splitlines():
        if ln.strip():
            return ln.strip()
    return None


def make_commit_status(repo: Path):
    """Return a callable(sha) -> 'missing' | 'layer' | 'nonlayer' for reconcile().

    A cited commit must not only EXIST but be an actual agent-layer change — else a
    layer-rewrite record could 'back' an unrelated commit (Codex finding #5).
    """
    def _status(sha: str) -> str:
        cp = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, text=True, check=False,
        )
        if cp.returncode != 0:
            return "missing"
        return "layer" if commit_is_agent_layer(changed_files_of(repo, sha)) else "nonlayer"
    return _status


def load_records(records_dir: Path):
    """Load *.json records. Returns (records: dict id->rec, invalid: list[{file,reason}]).

    Every record is validated against schemas/overseer-decision-record.schema.json (so the
    conditional invariants — applied layer-rewrite must cite a commit, no_change/defer must
    not, defer needs an expiry — actually bite). Failures are surfaced as invalid, NEVER
    silently skipped: a needed binding could be hiding behind a malformed file, so an invalid
    record forces a HOLD (run_check folds `invalid` into ok=False).

    A DUPLICATE id is fail-closed: both files are marked invalid AND the id is removed from
    the trusted set, so any commit linking to it becomes an orphan (Codex finding #4 — a
    last-write-wins overwrite could otherwise mask a record by sort order).
    """
    records: dict[str, dict] = {}
    first_file: dict[str, str] = {}
    invalid: list[dict] = []
    if not records_dir.is_dir():
        return records, invalid
    validator = _record_validator()
    for p in sorted(records_dir.glob("*.json")):
        try:
            rec = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            invalid.append({"file": p.name, "reason": f"unreadable/invalid json: {exc}"})
            continue
        errs = sorted(validator.iter_errors(rec), key=lambda e: list(e.path))
        if errs:
            e = errs[0]
            invalid.append({"file": p.name, "reason": f"schema-invalid: {e.message} at {list(e.path)}"})
            continue
        rid = rec["id"]  # schema guarantees a non-empty, well-formed id
        if rid in records or rid in first_file:
            invalid.append({"file": p.name, "reason": f"duplicate id '{rid}' (also in {first_file.get(rid, '?')})"})
            records.pop(rid, None)  # neither copy is trusted as a binding -> fail closed
            continue
        records[rid] = rec
        first_file[rid] = p.name
    return records, invalid


def run_check(profiles_repo: Path, since: str, records_dir: Path) -> dict:
    """Gather agent-layer commits + records, reconcile. Raises on indeterminate git/IO (the
    CLI turns that into a fail-safe HOLD, never a clean pass)."""
    shas = commits_in_range(profiles_repo, since)
    layer_commits = []
    for sha in shas:
        files = changed_files_of(profiles_repo, sha)
        if commit_is_agent_layer(files):
            layer_commits.append({"sha": sha, "trailer_id": decision_record_trailer(profiles_repo, sha)})
    records, invalid = load_records(records_dir)
    result = reconcile(layer_commits, records, make_commit_status(profiles_repo))
    result["invalid_records"] = invalid
    result["ok"] = result["ok"] and not invalid
    result["since"] = since
    return result


def _self_test() -> int:
    """Pure-core checks: path classification + reconcile verdicts, no git needed."""
    failures: list[str] = []

    def expect(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got}, want {want}")

    # Path classification.
    expect("soul-exact", is_agent_layer_path("coder/SOUL.md"), True)
    expect("config-exact", is_agent_layer_path("qa/config.yaml"), True)
    expect("skills-subtree", is_agent_layer_path("main/skills/foo/SKILL.md"), True)
    expect("tools-subtree", is_agent_layer_path("dreamer/tools/x.py"), True)
    expect("soul-prebak-excluded", is_agent_layer_path("coder/SOUL.md.prebak.20260620T0Z"), False)
    expect("config-bak-excluded", is_agent_layer_path("coder/config.yaml.bak.x"), False)
    expect("logs-not-layer", is_agent_layer_path("coder/logs/run.log"), False)
    expect("toplevel-not-layer", is_agent_layer_path("SOUL.md"), False)
    expect("session-not-layer", is_agent_layer_path("coder/sessions/s.db"), False)

    # commit_status stubs: layer = exists & is a layer change; nonlayer = exists, not layer.
    as_layer = lambda _sha: "layer"
    as_missing = lambda _sha: "missing"
    as_nonlayer = lambda _sha: "nonlayer"

    # Reconcile: a clean, fully-bound, certified layer-rewrite passes.
    rec_ok = {"r1": {"id": "r1", "decision": "layer-rewrite", "commit_hash": "abc", "status": "certified"}}
    r = reconcile([{"sha": "abc", "trailer_id": "r1"}], rec_ok, as_layer)
    expect("clean-pass", r["ok"], True)

    # Orphan: linked record is only PROPOSED (uncertified) — must not back a real change.
    rec_proposed = {"r1": {"id": "r1", "decision": "layer-rewrite", "commit_hash": "abc", "status": "proposed"}}
    r = reconcile([{"sha": "abc", "trailer_id": "r1"}], rec_proposed, as_layer)
    expect("proposed-status-orphan", (r["ok"], len(r["orphans"])), (False, 1))

    # Orphan: commit with no trailer.
    r = reconcile([{"sha": "abc", "trailer_id": None}], {}, as_layer)
    expect("no-trailer-orphan", (r["ok"], len(r["orphans"])), (False, 1))

    # Orphan: trailer to a missing record.
    r = reconcile([{"sha": "abc", "trailer_id": "ghost"}], {}, as_layer)
    expect("missing-record-orphan", (r["ok"], len(r["orphans"])), (False, 1))

    # Orphan: linked record is a no_change (can't back a real layer change).
    rec_nc = {"r1": {"id": "r1", "decision": "no_change"}}
    r = reconcile([{"sha": "abc", "trailer_id": "r1"}], rec_nc, as_layer)
    expect("no_change-cant-back", (r["ok"], len(r["orphans"])), (False, 1))

    # Orphan: record doesn't point back (commit_hash empty / mismatched).
    rec_empty = {"r1": {"id": "r1", "decision": "layer-rewrite", "commit_hash": ""}}
    r = reconcile([{"sha": "abc", "trailer_id": "r1"}], rec_empty, as_layer)
    expect("no-backlink-orphan", (r["ok"], len(r["orphans"])), (False, 1))
    rec_mismatch = {"r1": {"id": "r1", "decision": "layer-rewrite", "commit_hash": "zzz"}}
    r = reconcile([{"sha": "abc", "trailer_id": "r1"}], rec_mismatch, as_layer)
    expect("backlink-mismatch-orphan", (r["ok"], len(r["orphans"])), (False, 1))

    # Dangling: layer-rewrite record cites a commit that doesn't exist.
    r = reconcile([], rec_ok, as_missing)
    expect("dangling-missing-commit", (r["ok"], len(r["dangling"])), (False, 1))

    # Dangling: layer-rewrite record cites a commit that is NOT an agent-layer change.
    r = reconcile([], rec_ok, as_nonlayer)
    expect("dangling-nonlayer-commit", (r["ok"], len(r["dangling"])), (False, 1))

    # Dangling: a no_change record must not cite a commit.
    rec_nc_hash = {"r1": {"id": "r1", "decision": "no_change", "commit_hash": "abc"}}
    r = reconcile([], rec_nc_hash, as_layer)
    expect("no_change-with-commit-dangling", (r["ok"], len(r["dangling"])), (False, 1))

    # A no_change record with no commit is fine (not flagged).
    r = reconcile([], {"r1": {"id": "r1", "decision": "no_change"}}, as_layer)
    expect("clean-no_change", r["ok"], True)

    if failures:
        print("decision_record_check SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("decision_record_check self-test: OK (path classification + reconcile verdicts)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="decision_record_check",
        description="Deterministic completeness check: every agent-layer change has a linked decision-record.",
    )
    ap.add_argument("--since", help="charter-baseline git ref; audits <ref>..HEAD (required)")
    ap.add_argument("--profiles-repo", default=str(DEFAULT_PROFILES_REPO), help="profiles git repo (source of truth)")
    ap.add_argument("--records-dir", default=str(DEFAULT_RECORDS_DIR), help="dir of overseer-decision-record json files")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.since:
        print("error: --since <baseline-ref> is required (or use --self-test)", file=sys.stderr)
        return 1

    profiles_repo = Path(args.profiles_repo)
    if not (profiles_repo / ".git").exists():
        print(f"error: not a git repo: {profiles_repo}", file=sys.stderr)
        return 1

    try:
        result = run_check(profiles_repo, args.since, Path(args.records_dir))
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: check could not run — failing safe (held): {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(
            f"[decision-record] OK — {result['checked_commits']} agent-layer commit(s) since "
            f"{result['since']}, all with a valid linked record"
        )
    else:
        print(f"[decision-record] HELD — {result['checked_commits']} agent-layer commit(s) since {result['since']}:")
        for o in result["orphans"]:
            print(f"  orphan {o['sha'][:10]}: {o['reason']}")
        for d in result["dangling"]:
            print(f"  dangling {d['record_id']}: {d['reason']}")
        for iv in result["invalid_records"]:
            print(f"  invalid record {iv['file']}: {iv['reason']}")

    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
