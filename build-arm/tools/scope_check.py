#!/usr/bin/env python3
"""Deterministic SCOPE gate for ATB builds — changed files MUST stay in allowed_paths.

WHY THIS EXISTS (the SCOPE leak, 2026-06-03):
Until now "did the build stay in its sandbox?" was judged two soft ways: the coder's own
prose discipline, and the Codex diff reviewer's *semantic* sense of whether a file "looks
related". That over-reaches. On track #4 (trust-ledger) the coder committed a stray
events.jsonl and a .gitignore — BOTH inside the declared workspace dir (allowed_paths) but
not in planned_files — and Codex held the build twice for them, while the one real bug was
a third, separate hold. 2 of 3 holds were rigid file-bookkeeping beating sensible behavior.

This tool moves the path question from fuzzy judgment to a deterministic check — the same
move run_verification made for the pass-claim and verification_delta made for the delta:
  - allowed_paths is the BINDING sandbox (a whitelist of dirs / files / globs).
  - planned_files stays ADVISORY (a forecast; NOT enforced here).
A changed file inside ANY allowed_paths entry is in scope (so the coder may freely create
the files it needs); a changed file OUTSIDE every entry escaped the sandbox -> HARD HOLD.
Every file is checked, and a tool's verdict can't be bribed. Codex is then freed to judge
correctness, behavior, and safety instead of policing the file list.

Scope is computed over the SAME basis the diff review uses: ``git diff --name-only <base>``
in the worktree (committed changes vs the build base) — so it sees exactly the file set
that would be accepted. Untracked files never enter a diff and are not part of the accepted
build, so they are out of view here too (matching scripts/atb_diff_review.sh).

Matching per allowed_paths entry (against each changed file's absolute, normalized path):
  - glob entry (contains * ? [)   -> fnmatch (note: * also spans '/', fine for a whitelist).
  - directory entry (trailing '/' OR a real dir on disk) -> the changed file is nested under
    it, component-wise (so '/a/foo' is NOT treated as under '/a/foobar').
  - bare file entry (no trailing '/', not a dir) -> EXACT match ONLY; it does NOT authorize
    nested children (an allowed '/x/a.py' must not also permit '/x/a.py/evil.py').
An entry may be absolute or worktree-relative (relative entries resolve against the worktree).

Usage:
  python -m tools.scope_check --track <dir> --worktree <dir> [--base HEAD] [--json]
  python -m tools.scope_check --self-test

Exit: 0 = every changed file is in scope · 2 = >=1 file escaped allowed_paths (HOLD) ·
      1 = usage / IO / git error — FAIL SAFE: never an in-scope pass on a broken check.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

ATB_ROOT = Path(__file__).resolve().parent.parent
_GLOB_CHARS = set("*?[")


def _norm_abs(path: str, base: Path) -> str:
    """Absolute, normalized path string. No symlink resolution -> pure + total."""
    p = path if os.path.isabs(path) else os.path.join(str(base), path)
    return os.path.normpath(p)


def _is_under(child: str, parent: str) -> bool:
    """True if `child` equals or is nested under directory `parent`, COMPONENT-wise.

    Component comparison (not raw string prefix) so '/a/foo' is NOT under '/a/foobar'.
    """
    cp = Path(child).parts
    pp = Path(parent).parts
    return len(pp) <= len(cp) and cp[: len(pp)] == pp


def _is_dir_entry(raw: str) -> bool:
    """An allowed_paths entry denotes a DIRECTORY subtree iff it ends with a separator.

    Bare entries (no trailing slash) denote an EXACT FILE — they must NOT authorize nested
    children (Codex 2026-06-03: an allowed '/x/a.py' must not also permit '/x/a.py/evil.py').
    check_scope canonicalizes a bare entry that is a real directory on disk by appending a
    separator, so authors are not punished for omitting it on an actual dir.
    """
    return raw.endswith("/") or raw.endswith(os.sep)


def file_in_scope(changed_abs: str, allowed_raw: list[str], worktree: Path) -> bool:
    """Is one changed file (absolute, normalized) inside any allowed_paths entry?

    Each entry resolves to ONE absolute path (against the worktree if relative). glob entry
    -> fnmatch. directory entry (trailing sep) -> exact match OR nested under it
    (component-wise). bare file entry -> EXACT match ONLY (it authorizes no children).
    """
    for raw in allowed_raw:
        cand = _norm_abs(raw, worktree)
        if _GLOB_CHARS & set(raw):
            if fnmatch.fnmatch(changed_abs, cand):
                return True
            continue
        if changed_abs == cand:
            return True
        if _is_dir_entry(raw) and _is_under(changed_abs, cand):
            return True
    return False


def files_in_scope(changed: list[str], allowed_paths: list[str], worktree):
    """Pure core: split changed files (relative to worktree) into in/out of scope.

    Returns (ok: bool, violations: list[str]) — violations are the original (relative)
    changed paths that escape every allowed_paths entry. Entries are absolute or
    worktree-relative; each resolves to exactly one path.
    """
    wt = Path(worktree)
    allowed = [a for a in allowed_paths if isinstance(a, str) and a.strip()]
    violations = [
        rel for rel in changed if not file_in_scope(_norm_abs(rel, wt), allowed, wt)
    ]
    return (not violations), violations


def changed_files(worktree: Path, base: str) -> list[str]:
    """Committed changed files vs the build base — the basis atb_diff_review.sh reviews."""
    cp = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", base],
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            f"git diff --name-only {base} failed in {worktree}: {(cp.stderr or '').strip()}"
        )
    return [ln for ln in (cp.stdout or "").splitlines() if ln.strip()]


def _canonicalize_allowed(allowed: list[str], worktree) -> list[str]:
    """Promote a bare entry that is a real directory on disk to a trailing-sep dir entry.

    A bare entry stays an EXACT FILE match (so an allowed file cannot authorize nested
    children); but punishing an author for omitting the slash on an ACTUAL directory would
    reintroduce over-holding, so a bare entry resolving (against the worktree) to an existing
    directory becomes a subtree entry. Globs, already dir-suffixed entries, real files, and
    nonexistent paths are left as-is (nonexistent -> exact, i.e. fail closed).
    """
    wt = Path(worktree)
    out = []
    for raw in allowed:
        if (_GLOB_CHARS & set(raw)) or _is_dir_entry(raw):
            out.append(raw)
        elif os.path.isdir(_norm_abs(raw, wt)):
            out.append(raw + "/")
        else:
            out.append(raw)
    return out


def check_scope(track: Path, worktree: Path, base: str) -> dict:
    """Load allowed_paths from product-plan.json, diff the worktree, compute violations.

    Returns {ok, violations, changed_count, allowed_paths}. Raises (RuntimeError / OSError /
    JSONDecodeError) on any condition that makes the verdict indeterminate — the CLI turns
    that into a FAIL-SAFE hold (rc 1), never an in-scope pass.
    """
    pp_path = track / "product-plan.json"
    pp = json.loads(pp_path.read_text())
    allowed = [a for a in (pp.get("allowed_paths") or []) if isinstance(a, str) and a.strip()]
    if not allowed:
        raise RuntimeError(f"product-plan.json has no usable allowed_paths in {track}")
    allowed = _canonicalize_allowed(allowed, worktree)
    changed = changed_files(worktree, base)
    ok, violations = files_in_scope(changed, allowed, worktree)
    return {
        "ok": ok,
        "violations": violations,
        "changed_count": len(changed),
        "allowed_paths": allowed,
    }


def _self_test() -> int:
    """Pure-core checks: in-sandbox new file passes; an escape is caught; boundaries hold."""
    failures: list[str] = []

    def expect(label, ok, viol, want_ok, want_viol):
        if ok != want_ok or viol != want_viol:
            failures.append(
                f"{label}: got ok={ok} viol={viol}, want ok={want_ok} viol={want_viol}"
            )

    wt = Path("/w/build/x")

    # 1. THE regression guard: a new file NOT in planned_files but inside the allowed dir
    #    is in scope (the trust-ledger events.jsonl / .gitignore over-hold must not recur).
    ok, v = files_in_scope(["events.jsonl", "trust_ledger.py"], ["/w/build/x/"], wt)
    expect("new-file-in-sandbox", ok, v, True, [])

    # 2. A file OUTSIDE the sandbox escaped -> held.
    ok, v = files_in_scope(["trust_ledger.py", "../../tools/run_verification.py"], ["/w/build/x/"], wt)
    expect("escape-out-of-sandbox", ok, v, False, ["../../tools/run_verification.py"])

    # 3. Sibling-prefix dir is NOT inside (component-wise containment, not string prefix).
    ok, v = files_in_scope(["y.py"], ["/w/build/x/"], Path("/w/build/xtra"))
    expect("sibling-prefix-not-under", ok, v, False, ["y.py"])

    # 4. Exact-file entry: the file matches, a sibling does not.
    ok, v = files_in_scope(["a.py", "b.py"], ["/w/build/x/a.py"], wt)
    expect("exact-file-only", ok, v, False, ["b.py"])

    # 4b. Codex 2026-06-03: a bare exact-FILE entry must NOT authorize nested children.
    ok, v = files_in_scope(["a.py/evil.py"], ["/w/build/x/a.py"], wt)
    expect("file-entry-no-children", ok, v, False, ["a.py/evil.py"])

    # 5. Glob entry: *.py matches, *.txt escapes.
    ok, v = files_in_scope(["a.py", "notes.txt"], ["/w/build/x/*.py"], wt)
    expect("glob-py-only", ok, v, False, ["notes.txt"])

    # 6. Multiple entries: a file matching ANY is in scope.
    ok, v = files_in_scope(["src/a.py", "docs/b.md"], ["/w/build/x/src/", "/w/build/x/docs/"], wt)
    expect("multi-allowed", ok, v, True, [])

    if failures:
        print("scope_check SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        "scope_check self-test: OK (in-sandbox new file, escape, sibling-prefix, "
        "exact, file-no-children, glob, multi)"
    )
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="scope_check",
        description="Deterministic ATB scope gate: changed files must stay in allowed_paths.",
    )
    ap.add_argument("--track", help="track dir holding product-plan.json")
    ap.add_argument("--worktree", help="build dir whose committed diff is checked")
    ap.add_argument("--base", default="HEAD", help="git base ref to diff against (default HEAD)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.track or not args.worktree:
        print(
            "error: --track and --worktree are required (or use --self-test)",
            file=sys.stderr,
        )
        return 1

    track = Path(args.track)
    if not track.is_absolute():
        track = (ATB_ROOT / track).resolve()
    worktree = Path(args.worktree).resolve()
    if not track.is_dir():
        print(f"error: track dir not found: {track}", file=sys.stderr)
        return 1
    if not worktree.is_dir():
        print(f"error: worktree dir not found: {worktree}", file=sys.stderr)
        return 1

    try:
        result = check_scope(track, worktree, args.base)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(
            f"error: scope check could not run — failing safe (held): {exc}",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(
            f"[scope] OK — {result['changed_count']} changed file(s), all within allowed_paths"
        )
    else:
        print(f"[scope] HELD — {len(result['violations'])} file(s) outside allowed_paths:")
        for v in result["violations"]:
            print(f"  - {v}")

    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
