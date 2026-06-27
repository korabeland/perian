#!/usr/bin/env python3
"""Post-pytest QA checks (C13, C13b, C16, C18b) — runs AFTER pytest finishes.

WHY THIS EXISTS (the QA layer, Rev 5 spec, 2026-06-06):
After pytest completes inside the sandboxed disposable workspace, the runner verifies that
canonical workspace bytes are unchanged (no side-effects escaped the sandbox), that no
ignored-residue files materialized in canonical, that pytest actually collected the tests
Gemma claimed to write, and that the route_input dict the runner built BEFORE cleanup
contains complete source for every cited evidence + every planned test. Pure functions
over their inputs; no model on the path.

THE CHECKS (per Rev 5 spec section "qa_check_post.py"):
- C13   canonical_hash_post == canonical_hash_pre AND `git status --porcelain
        --untracked-files=all` empty. The trust-property load-bearing check — proves
        Gemma's run mutated nothing in canonical.
- C13b  No NEW ignored-residue files appeared in canonical between pre- and post-snapshot
        (env var setup correct; no leaked __pycache__/.pytest_cache).
- C16   `pytest --collect-only -q` on `tmp_ws/ws/test_qa.py` collected exactly
        `n_tests_written` items — pytest sees the tests Gemma wrote (no skip injection,
        no parametrization sleight-of-hand expanding 1 test to 10 collected items).
- C18b  Source-availability invariant: `route_input` (built by qa_route.build_route_input)
        contains source for every planned `asserted_in_test` + every cited
        `coder_test_evidence` nodeid. Closes the "Codex routes without complete source"
        path. Codex's per-item verdicts MUST be made against the captured source, not the
        deleted tmp_ws.

Each check returns `CheckResult(ok, name, reason)`. Caller HOLDs on first failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple


class CheckResult(NamedTuple):
    ok: bool
    name: str
    reason: str


def _ok(name: str) -> CheckResult:
    return CheckResult(True, name, "")


def _fail(name: str, reason: str) -> CheckResult:
    return CheckResult(False, name, reason)


# =============================================================================
# C13 — canonical workspace bytes unchanged (the trust-property check)
# =============================================================================


def c13_canonical_unchanged(
    canonical_hash_pre: str,
    canonical_hash_post: str,
    canonical_ws: Path,
) -> CheckResult:
    """C13: hashes match AND `git status --porcelain --untracked-files=all` is empty.

    Both clauses are required: hashes ALONE could be fooled by a perfectly compensating
    set of mutations (extraordinarily unlikely with sha256, but the git-status check is
    cheap belt-and-braces). The git-status check ALONE could be fooled by an attacker who
    mutated a tracked file's bytes but reverted them right before the snapshot (timing
    not actually possible here, but defense-in-depth).
    """
    name = "C13"
    if canonical_hash_pre != canonical_hash_post:
        return _fail(
            name,
            f"canonical hash changed: pre={canonical_hash_pre[:12]}..., "
            f"post={canonical_hash_post[:12]}...",
        )
    if not canonical_ws.is_dir():
        return _fail(name, f"canonical_ws is not a directory: {canonical_ws}")
    try:
        out = subprocess.check_output(
            [
                "git",
                "-C",
                str(canonical_ws),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        return _fail(name, f"git status failed: {e}")
    if out.strip():
        # Show at most 10 lines for readability.
        lines = out.decode("utf-8", errors="replace").splitlines()
        return _fail(
            name,
            f"git status --porcelain not empty: {lines[:10]}",
        )
    return _ok(name)


# =============================================================================
# C13b — no new ignored-residue files in canonical
# =============================================================================


def _list_ignored_files(canonical_ws: Path) -> set[str]:
    """Return the set of paths (relative to ws) that git considers ignored and which
    actually exist on disk. Empty on a clean workspace because step 2 (snapshot/clean
    residue) ran before this check.

    Codex review (2026-06-06): raises on subprocess failure rather than returning an empty
    set. Returning empty here would let C13b pass-open when git is unavailable or the dir
    isn't a worktree — for a post-pytest trust check, that's the wrong disposition.
    """
    out = subprocess.check_output(
        [
            "git",
            "-C",
            str(canonical_ws),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
        ],
        stderr=subprocess.STDOUT,
    )
    return set(out.decode("utf-8", errors="replace").splitlines())


def c13b_no_new_ignored_residue(
    ignored_pre: set[str], canonical_ws: Path
) -> CheckResult:
    """C13b: list of ignored files in canonical did NOT grow during the Gemma+pytest run.

    `ignored_pre` is the set captured by `snapshot_and_clean_residue` BEFORE Gemma loaded;
    we list again now and HOLD if anything new appeared. Codex review 2026-06-06: any
    subprocess failure here is itself a HOLD reason — we don't trust the workspace if we
    can't enumerate its ignored files.
    """
    name = "C13b"
    if not canonical_ws.is_dir():
        return _fail(name, f"canonical_ws is not a directory: {canonical_ws}")
    try:
        ignored_post = _list_ignored_files(canonical_ws)
    except (OSError, subprocess.CalledProcessError) as e:
        return _fail(name, f"git ignored-file listing failed: {e}")
    new_residue = sorted(ignored_post - ignored_pre)
    if new_residue:
        return _fail(
            name,
            f"new ignored residue in canonical (env-var prevention failed): "
            f"{new_residue[:10]}",
        )
    return _ok(name)


# =============================================================================
# C16 — pytest collected exactly n_tests_written tests in test_qa.py
# =============================================================================


def _pytest_collect_count(ws_path: Path, test_qa_relname: str = "test_qa.py") -> int:
    """Run pytest --collect-only -q on ws_path/test_qa.py and parse the test count.

    Spec env: PYTHONDONTWRITEBYTECODE=1, -p no:cacheprovider, isolated cwd. We're not
    INSIDE the sandbox here (this is the runner counting), so no sandbox-exec wrap.
    """
    test_path = ws_path / test_qa_relname
    if not test_path.is_file():
        return 0
    # `pytest --collect-only -q` emits one line per nodeid + a summary "X tests collected".
    # We count nodeids (lines that look like "<path>::<test_name>") rather than parsing
    # the summary, which is robust against minor formatting drift.
    import os

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [
                "python3",
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                test_qa_relname,
            ],
            cwd=str(ws_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1
    # Codex review (2026-06-06): a non-zero exit (collection error, import error, etc.)
    # might still produce SOME nodeid-looking lines in stdout — counting them in that case
    # would silently understate the real failure. Treat non-zero as a hard collection
    # failure and let the caller emit a richer message.
    if proc.returncode != 0:
        return -1
    lines = proc.stdout.splitlines()
    count = sum(1 for ln in lines if "::" in ln and ln.strip())
    return count


def c16_pytest_collected_n_tests(
    strategy: dict, ws_path: Path, test_qa_relname: str = "test_qa.py"
) -> CheckResult:
    """C16: pytest --collect-only on test_qa.py finds exactly n_tests_written items."""
    name = "C16"
    n = strategy.get("n_tests_written", 0)
    if n == 0:
        # Nothing to collect; success.
        return _ok(name)
    collected = _pytest_collect_count(ws_path, test_qa_relname)
    if collected < 0:
        return _fail(name, "pytest --collect-only failed or timed out")
    if collected != n:
        return _fail(
            name,
            f"n_tests_written={n} but pytest collected {collected} items "
            f"(parametrization or skip injection?)",
        )
    return _ok(name)


# =============================================================================
# C18b — source availability for Codex routing
# =============================================================================


def c18b_route_input_has_all_source(
    strategy: dict, route_input: dict
) -> CheckResult:
    """C18b: `route_input` (built BEFORE tmp_ws cleanup) carries source for every
    `asserted_in_test` plus every cited `coder_test_evidence` nodeid.

    Shape of route_input (Rev 5 spec, qa_route.build_route_input):
        {
          "test_qa_source": "<full test_qa.py file contents or empty>",
          "asserted_in_test_sources": {"test_x": "<def + body>", ...},
          "coder_test_sources": {"test_graph.py::test_x": "<def + body>", ...},
          "contract_clauses": [...],
          "qa_strategy": {...},
          "pytest_results": {...}
        }

    We check (a) every planned test's `asserted_in_test` is a non-empty key in
    `asserted_in_test_sources` AND (b) every cited `coder_test_evidence` nodeid (across
    planned_tests and skipped_clauses) is a non-empty key in `coder_test_sources`.
    """
    name = "C18b"
    if not isinstance(route_input, dict):
        return _fail(name, "route_input is not a dict")

    asserted_sources = route_input.get("asserted_in_test_sources")
    if not isinstance(asserted_sources, dict):
        return _fail(
            name, "route_input.asserted_in_test_sources missing or not a dict"
        )
    coder_sources = route_input.get("coder_test_sources")
    if not isinstance(coder_sources, dict):
        return _fail(
            name, "route_input.coder_test_sources missing or not a dict"
        )

    # (a) planned asserted_in_test names → source present + non-empty.
    missing_asserted: list[str] = []
    for pt in strategy.get("planned_tests") or []:
        name_ = pt.get("asserted_in_test")
        if not name_:
            continue
        src = asserted_sources.get(name_)
        if not isinstance(src, str) or not src.strip():
            missing_asserted.append(name_)
    if missing_asserted:
        return _fail(
            name,
            f"route_input missing source for asserted_in_test: "
            f"{missing_asserted[:5]}",
        )

    # (b) cited coder_test_evidence → source present + non-empty.
    cited_coder: set[str] = set()
    for pt in strategy.get("planned_tests") or []:
        cited_coder.update(pt.get("coder_test_evidence") or [])
    for sc in strategy.get("skipped_clauses") or []:
        cited_coder.update(sc.get("coder_test_evidence") or [])
    missing_coder: list[str] = []
    for nodeid in sorted(cited_coder):
        src = coder_sources.get(nodeid)
        if not isinstance(src, str) or not src.strip():
            missing_coder.append(nodeid)
    if missing_coder:
        return _fail(
            name,
            f"route_input missing source for coder_test_evidence: "
            f"{missing_coder[:5]}",
        )
    return _ok(name)
