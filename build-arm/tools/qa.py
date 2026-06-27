#!/usr/bin/env python3
"""QA layer runner — orchestrates the named units of the Rev 5 spec end-to-end.

WHY THIS EXISTS (the QA layer, Rev 5 spec, 2026-06-06):
This module sequences the 21 steps of the QA pipeline:

  plan-gate → coder → diff-gate → [QA RUNNER STARTS HERE]
    1.  reject_symlinks_and_submodules(canonical_ws)
    2.  snapshot_and_clean_residue(canonical_ws) → ignored_pre
    3.  canonical_hash_pre = canonical_fingerprint(canonical_ws)
    4.  acquire <track>/.qa.lock                                 (track lock FIRST)
    5.  make_tmp_ws(track_id) → tmp_ws/{ws, home, cache, tmp}
    6.  preclear_track_artifacts(track)
    7.  acquire state/.model.lock                                (model lock SECOND)
    8.  run_gemma_under_model_lock(contract, tmp_ws, nodeids)
        → tmp_ws/qa-strategy.json + (optional) tmp_ws/ws/test_qa.py
    9.  release state/.model.lock
    10. clauses = parse_contract_clauses(<track>/product-plan.json)
    11. validate_pre(strategy, test_qa, clauses, ...)            → C1–C12, C15, C18a
    12. run_pytest_sandboxed(tmp_ws/ws)                          → sandbox-exec
    13. canonical_hash_post = canonical_fingerprint(canonical_ws)
    14. validate_post(...)                                       → C13, C13b, C16
    15. build_route_input(...)                                   (CAPTURES SOURCE BEFORE CLEANUP)
    16. write_qa_json(track, route_input)
    17. promote_strategy(tmp_ws → track)                         (O_EXCL + fsync + os.replace)
    18. rmtree(tmp_ws)
    19. codex_verdicts = call_codex_route(route_input)
    20. validate_and_compute_route(strategy, codex_verdicts, pytest_exit, unrep)
        → qa-route.json (computed deterministically)
    21. emit_event(coach OR confirm)
    22. release <track>/.qa.lock
  → qa-gate (existing deterministic floor) → confirmed

Each step is a top-level function (independently unit-testable). The main
orchestrator `run_qa(track_dir, canonical_ws, ...)` sequences them and returns
the structured outcome. Failures short-circuit cleanly with a HOLD route plus
informative reason; the runner ALWAYS releases its locks via try/finally.

SANDBOX-EXEC POLICY (Rev 5 spec section "The sandbox-exec policy"):
The `SANDBOX_EXEC_POLICY` constant below is the macOS sandbox profile that
wraps pytest. Writes are denied except under the disposable tmp_ws root.

CALLED BY:
- `gate_sweep.py` (the deterministic gate sweep) at the QA gate.
- Manual invocation: `python -m tools.qa <track_dir>`.

THIS MODULE IS PART OF THE TRUST SPINE. Codex-reviewed before commit.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, NamedTuple

from config import paths
from tools.qa_check_post import (
    c13_canonical_unchanged,
    c13b_no_new_ignored_residue,
    c16_pytest_collected_n_tests,
    c18b_route_input_has_all_source,
)
from tools.qa_check_pre import (
    c1_strategy_well_formed,
    c3_evidence_nodeids_exist,
    c4_n_tests_capped,
    c5_n_tests_matches_test_file_presence,
    c6_test_qa_parses,
    c7_planned_tests_bijective,
    c8_docstrings_cite_clause_id,
    c9_ast_allowlist,
    c10_asserted_in_test_resolves,
    c11_no_symlinks,
    c12_only_test_qa_differs,
    c15_gemma_ran_successfully,
    c18a_clause_completeness,
)
from tools.qa_clauses import parse_contract_clauses
from tools.qa_fingerprint import (
    canonical_fingerprint,
    reject_symlinks_and_submodules,
    snapshot_and_clean_residue,
)
from tools.qa_route import (
    build_route_input,
    call_codex_route,
    validate_and_compute_route,
)


# =============================================================================
# Constants
# =============================================================================


# Rev 5 spec sandbox-exec policy. Substitute `<TMP_WS>` with the disposable
# tmp_ws path at run time before passing to sandbox-exec.
SANDBOX_EXEC_POLICY_TEMPLATE = """(version 1)
(deny default)
(allow process-fork)
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow file-read*)
(allow file-write* (subpath "<TMP_WS>"))
(allow file-write-data (literal "/dev/null"))
(allow file-write-data (literal "/dev/dtracehelper"))
"""


QA_PROFILE = "qa"  # used as both the swap_orchestrator profile arg and hermes -p target
# Recorded into qa.json as `gemma_model`. MUST match `model.default` in
# ~/.hermes/profiles/qa/config.yaml — they're not validated at runtime.
GEMMA_MODEL = "gemma-4-26b-a4b-it"
GEMMA_TIMEOUT_SECONDS = 600  # 10 minutes


# =============================================================================
# Result type
# =============================================================================


class QARun(NamedTuple):
    """Structured outcome of a single QA pipeline run.

    `computed_route` is the deterministic decision; `failure_reason` is set when a
    pre-flight or check failed before Codex was ever consulted.
    """

    track_id: str
    computed_route: str  # coder | gemma | confirm | hold_human
    failure_reason: str  # empty if route is computed normally
    canonical_hash_pre: str
    canonical_hash_post: str
    qa_json_path: Path | None
    qa_strategy_path: Path | None
    qa_route_path: Path | None
    elapsed_seconds: float


# =============================================================================
# File locks (track + model)
# =============================================================================


@contextmanager
def _flock(lockfile: Path) -> Iterator[None]:
    """Acquire an exclusive flock on `lockfile`, yield, release. Creates the file
    if absent. Caller's body runs only when the lock is held.
    """
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lockfile), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# =============================================================================
# Step 5: make_tmp_ws — disposable workspace copy
# =============================================================================


def make_tmp_ws(
    track_id: str, canonical_ws: Path, parent_dir: Path | None = None
) -> Path:
    """Create a disposable tmp_ws/{ws, home, cache, tmp} layout for a single QA run.

    Returns the tmp_ws root. The caller is responsible for `rmtree` after step 18.

    Codex review (2026-06-06): if `shutil.copytree` fails mid-copy, the partially-built
    tmp_ws would leak (the outer `finally` block can't see it because we haven't returned
    yet). Wrap the construction; on ANY exception, best-effort rmtree what we've built so
    far, then re-raise so the orchestrator returns a structured HOLD.

    Codex review (2026-06-06) NIT 1: also reject tmp_ws paths containing a `"` character,
    which would corrupt the sandbox-exec Scheme policy at substitution time. mkdtemp won't
    return such a path in practice, but TMPDIR pollution could theoretically reach us.

    Shadow-phase finding (2026-06-07): hermes's `write_file` tool refuses to
    write into `/var/folders/...` paths (it treats them as "sensitive system
    paths"). That meant Gemma could not author qa-strategy.json under the
    default mkdtemp location. Default the parent to a project-managed dir
    (`<atb_root>/state/qa-tmp/`) where hermes will permit writes. Callers can
    still pass an explicit `parent_dir` if they need a different location.
    """
    safe_id = "".join(c for c in track_id if c.isalnum() or c in "-_")
    if parent_dir is None:
        # Default: project-managed location under state/. This stays out of
        # system tmp dirs that hermes's write_file tool refuses.
        atb_root = Path(__file__).resolve().parent.parent
        parent_dir = atb_root / "state" / "qa-tmp"
    parent_dir.mkdir(parents=True, exist_ok=True)

    # Codex review (2026-06-07 finding 1): opportunistically scavenge stale
    # qa-* dirs older than 24h. Without this, SIGKILL/process-crash leaks
    # accumulate in state/qa-tmp/ since the caller's try/finally cleanup
    # can't run on hard kill. Match only `qa-*` prefixes so this never
    # touches anything else the operator may stash under the same dir.
    try:
        cutoff = time.time() - 86_400  # 24h
        for child in parent_dir.iterdir():
            if not child.name.startswith("qa-") or not child.is_dir():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                # Race with another process or transient FS error — ignore.
                pass
    except OSError:
        # If we can't even iterdir the parent, skip the scavenger.
        pass

    tmp_ws = Path(
        tempfile.mkdtemp(prefix=f"qa-{safe_id}-", dir=str(parent_dir))
    )
    try:
        if '"' in str(tmp_ws) or "\\" in str(tmp_ws):
            raise RuntimeError(
                f"tmp_ws path contains unsafe chars (quote/backslash): {tmp_ws}"
            )
        (tmp_ws / "ws").mkdir()
        (tmp_ws / "home").mkdir()
        (tmp_ws / "cache").mkdir()
        (tmp_ws / "tmp").mkdir()
        # Deep-copy canonical_ws into tmp_ws/ws (preserves modes; rejects symlinks
        # if present — but reject_symlinks_and_submodules already verified the root).
        # `dirs_exist_ok=True` is needed because we pre-created `ws/` above.
        shutil.copytree(
            canonical_ws,
            tmp_ws / "ws",
            symlinks=True,  # preserve internal symlinks as-is
            dirs_exist_ok=True,
        )
    except Exception:
        shutil.rmtree(tmp_ws, ignore_errors=True)
        raise
    return tmp_ws


# =============================================================================
# Step 6: preclear_track_artifacts
# =============================================================================


def preclear_track_artifacts(track_dir: Path) -> None:
    """Remove any stale qa.json / qa-strategy.json / qa-route.json from a previous run.

    Forces a fresh write each time so an old artifact can't be mistaken for current.
    """
    for name in ("qa.json", "qa-strategy.json", "qa-route.json"):
        p = track_dir / name
        if p.is_file():
            p.unlink()


# =============================================================================
# Step 6b: collect coder pytest nodeids — disposable-copy + collect-only
# =============================================================================

# Codex review round 2 HOLD #2 → round 3/4 HOLD #1/#2 (2026-06-07, finding #7):
# a controlled pytest plugin writes the nodeid list to a JSON file. The
# plugin is materialized into scratch_parent with a RANDOM module name so
# a workspace-shipped `_collect_plugin.py` cannot shadow it on import.
# The output file path is hardcoded INTO the plugin source (not via env)
# so coder-controlled code has to find it via filesystem enumeration
# rather than reading `os.environ`. After pytest exits, the helper runs
# an AST cross-check against scratch_ws as a SHAPE DIAGNOSTIC (NOT a
# per-entry filter — that would false-drop non-default-discovery tests,
# documented as F7d). Plugin nodeids pass through as-is; the AST scan
# triggers refusal only on two whole-shape forgeries:
#   - overwrite-to-empty: plugin reports [] yet AST scan found real
#     test FunctionDef nodes on disk → refuse.
#   - total forgery: plugin and AST scan each found >0, but intersection
#     is empty → refuse.
# Residuals (NOT closed in this scope): an individual forged nodeid that
# matches a real bare on-disk function name → F7b. A non-default-
# discovery workspace where AST scan finds 0 nodes can slip an
# overwrite-to-empty past the diagnostic → F7d.
_QA_COLLECT_PLUGIN_TEMPLATE = '''"""QA collection-channel plugin (finding #7).

Writes ``[item.nodeid for item in items]`` to a hardcoded path right when
collection finishes. Reading from this controlled channel + the helper's
AST cross-check is strictly more robust than scraping pytest stdout.
"""
import json

_OUT_PATH = __OUT_PATH_PLACEHOLDER__


def pytest_collection_modifyitems(config, items):
    try:
        with open(_OUT_PATH, "w") as fh:
            json.dump([it.nodeid for it in items], fh)
    except OSError:
        # The caller will fail the run via empty-result + reason if the
        # output file is missing; never raise out of the hook.
        pass
'''


def _ast_collect_test_function_nodeids(ws: Path) -> set[str]:
    """Walk every ``test_*.py`` (and ``*_test.py``) file under ``ws`` and
    return the set of ``relpath::test_name`` and
    ``relpath::ClassName::test_name`` nodeids that exist as real
    FunctionDef nodes in the source.

    Used to cross-check the plugin's JSON output (Codex review round 3
    HOLD #1): an attacker who overwrites the JSON cannot make the
    helper accept a nodeid for a function that doesn't exist on disk.

    Parametrize expansion is NOT done here — the AST sees a single
    ``def test_param`` while the plugin reports ``test_param[1]``,
    ``test_param[2]``. The caller filters plugin nodeids by their
    BARE form (param suffix stripped) and accepts everything whose bare
    form is in this set.
    """
    import ast as _ast

    found: set[str] = set()
    for path in ws.rglob("*.py"):
        if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        rel = path.relative_to(ws).as_posix()
        try:
            tree = _ast.parse(path.read_text())
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if (
                isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            ):
                found.add(f"{rel}::{node.name}")
            elif isinstance(node, _ast.ClassDef) and node.name.startswith("Test"):
                for child in node.body:
                    if (
                        isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                        and child.name.startswith("test_")
                    ):
                        found.add(f"{rel}::{node.name}::{child.name}")
    return found


def _bare_nodeid(nodeid: str) -> str:
    """Strip the parametrize ``[...]`` suffix that appears AFTER the
    first ``::`` separator. Symmetric with the strip logic in
    ``_extract_coder_test_sources``.
    """
    first_sep = nodeid.find("::")
    if first_sep == -1:
        return nodeid
    bracket_idx = nodeid.find("[", first_sep)
    return nodeid if bracket_idx == -1 else nodeid[:bracket_idx]


def _collect_coder_nodeids(
    canonical_ws: Path, timeout_seconds: int = 60
) -> tuple[list[str], str | None]:
    """Run ``pytest --collect-only`` against a disposable copy of canonical_ws
    and return its pytest-canonical nodeids via a controlled plugin
    channel.

    Returns ``(nodeids, error_reason)``:
      - On success: ``(list of nodeids, None)``. The list is empty when the
        workspace has no test files (legitimate empty state, NOT an error).
      - On any failure: ``([], short reason string)``. NEVER raises — callers
        treat empty list as "no nodeids known" and downstream checks (C3)
        receive an empty expected-set.

    Trust properties (5 layers, each closing a distinct Codex-reviewed hole):

      1. **Disposable copy.** pytest is invoked with ``cwd=scratch_ws``;
         its own writes are confined to the copy.
      2. **PYTHONDONTWRITEBYTECODE=1** disables ``__pycache__`` writes
         entirely as a belt-and-suspenders measure.
      3. **sandbox-exec wraps pytest** with a deny-default policy that
         allows writes only inside ``scratch_parent``. Closes round-1
         HOLD #1: coder-controlled import / conftest code can no longer
         write hardcoded paths back into ``canonical_ws``.
      4. **Plugin channel for nodeids.** A scratch-loaded pytest plugin
         writes ``items`` (the real on-disk Item objects pytest
         collected) to a JSON file inside the sandbox. The helper reads
         that file — NEVER scrapes stdout. Closes round-2 HOLD #2: a
         conftest can no longer plant fake ``file.py::test_name``
         strings during a clean exit-0 collection.
      5. **Minimal env passthrough.** The subprocess gets only the env
         keys it actually needs (PATH, LANG, plus per-collection
         overrides). The caller's ``os.environ`` is NOT passed through.
         Closes part of round-2 HOLD #3: malicious collection code can
         no longer harvest ``ANTHROPIC_API_KEY`` / SSH / OAuth tokens
         from the parent env. (The read side of ``file-read*`` in the
         sandbox-exec policy is still permissive — that's a separate
         policy redesign, documented as a FOLLOW-UP.)
      6. **Exit-code gate.** Only exit ``0`` or ``5`` ("no tests
         collected") are accepted; everything else returns
         ``([], reason)``.
      7. **TMPDIR safety.** Refuses to run if the host TMPDIR is inside
         ``canonical_ws`` — closes round-2 NIT #5 (a poisoned TMPDIR
         could put scratch creation inside the trusted tree).

    Nodeid format: pytest's canonical form,
    ``<relative_path>::<test_name>`` (or
    ``<relative_path>::<TestClass>::<test_name>``,
    ``<relative_path>::<test_name>[<param>]``), relative to the
    workspace root — which is exactly what
    ``qa_check_pre.c3_evidence_nodeids_exist`` expects.

    Finding #7 (2026-06-07): before this helper, gate_sweep called run_qa
    with ``coder_test_nodeids=[]`` by default, so C3 always tripped on any
    non-empty ``coder_test_evidence`` array Gemma wrote. Callers now run
    this helper first and pass its output through.
    """
    if not canonical_ws.is_dir():
        return [], f"canonical_ws is not a directory: {canonical_ws}"

    # Round-2 NIT #5: refuse if the host's effective TMPDIR is inside the
    # trusted tree. Without this, mkdtemp would silently create scratch
    # under canonical_ws and pytest's collection would briefly run from
    # inside the trusted dir.
    try:
        canonical_resolved = canonical_ws.resolve()
        host_tmpdir = Path(
            os.environ.get("TMPDIR") or tempfile.gettempdir()
        ).resolve()
    except OSError as e:
        return [], f"path resolve failed: {e}"
    if (
        host_tmpdir == canonical_resolved
        or canonical_resolved in host_tmpdir.parents
    ):
        return [], (
            f"TMPDIR is inside canonical_ws — refusing to create scratch "
            f"under trusted tree: TMPDIR={host_tmpdir} canonical_ws="
            f"{canonical_resolved}"
        )

    # Round-1 NIT 4: include mkdtemp in the caught-failure paths so the
    # helper's no-raise contract holds even on rare tempdir creation errors.
    try:
        # `.resolve()` is REQUIRED: on macOS, tempfile.mkdtemp returns a
        # path under /var/folders/..., which is a symlink to
        # /private/var/folders/... — sandbox-exec policy rules match the
        # KERNEL-resolved canonical path, so an unresolved /var/... path
        # in the policy wouldn't match a subprocess's resolved
        # /private/var/... write.
        scratch_parent = Path(
            tempfile.mkdtemp(prefix="qa-collect-")
        ).resolve()
    except OSError as e:
        return [], f"mkdtemp failed: {e}"

    scratch_ws = scratch_parent / "ws"
    scratch_tmp = scratch_parent / "tmp"
    # Round-3 HOLD #2: randomize the plugin module name so a workspace-
    # shipped `_collect_plugin.py` cannot win import resolution. The
    # random suffix is generated from the scratch_parent path itself
    # (which is itself random via mkdtemp + already unique per call).
    plugin_module = (
        "_qa_collect_" + hashlib.sha256(scratch_str_bytes(scratch_parent)).hexdigest()[:16]
    )
    plugin_path = scratch_parent / f"{plugin_module}.py"
    nodeids_out = scratch_parent / "collected.json"
    try:
        try:
            shutil.copytree(
                canonical_ws,
                scratch_ws,
                symlinks=True,
                dirs_exist_ok=False,
            )
        except (OSError, shutil.Error) as e:
            return [], f"copytree failed: {e}"

        scratch_str = str(scratch_parent)
        if '"' in scratch_str or "\\" in scratch_str:
            return [], (
                f"scratch_parent contains unsafe chars (quote/backslash): "
                f"{scratch_str}"
            )
        policy_file = scratch_parent / "collect-sandbox.sb"
        try:
            policy_file.write_text(
                SANDBOX_EXEC_POLICY_TEMPLATE.replace("<TMP_WS>", scratch_str)
            )
            # Inject the output path INTO the plugin source as a Python
            # literal (round-3 HOLD #1: don't expose the path via env, so
            # the conftest must enumerate the filesystem to find it,
            # raising the bar over a one-liner `os.environ.get`).
            plugin_src = _QA_COLLECT_PLUGIN_TEMPLATE.replace(
                "__OUT_PATH_PLACEHOLDER__", repr(str(nodeids_out))
            )
            plugin_path.write_text(plugin_src)
            scratch_tmp.mkdir(exist_ok=True)
        except OSError as e:
            return [], f"scratch setup failed: {e}"

        # Round-2 HOLD #3 (partial): minimal env passthrough. NOTE:
        # `file-read*` in the sandbox policy itself is still permissive —
        # fully closing the read side is FOLLOW-UP F7a in
        # `docs/plans/2026-06-07-005-finding-7-followups.md`.
        env: dict[str, str] = {}
        if "PATH" in os.environ:
            env["PATH"] = os.environ["PATH"]
        for key in ("LANG", "LC_ALL", "LC_CTYPE"):
            if key in os.environ:
                env[key] = os.environ[key]
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["TMPDIR"] = str(scratch_tmp)
        env["HOME"] = str(scratch_parent)
        env["PYTHONPATH"] = str(scratch_parent)

        try:
            proc = subprocess.run(
                [
                    "sandbox-exec",
                    "-f",
                    str(policy_file),
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-p",
                    plugin_module,
                ],
                cwd=str(scratch_ws),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return [], (
                f"pytest --collect-only timed out after {timeout_seconds}s"
            )
        except OSError as e:
            return [], f"pytest invocation failed: {e}"

        if proc.returncode not in (0, 5):
            err = (proc.stderr or proc.stdout)[-300:].strip()
            return [], (
                f"pytest collect-only failed (exit {proc.returncode}): {err}"
            )

        if not nodeids_out.is_file():
            return [], "plugin did not write collected.json"
        try:
            data = json.loads(nodeids_out.read_text())
        except (OSError, json.JSONDecodeError) as e:
            return [], f"collected.json unreadable: {e}"
        if not isinstance(data, list) or not all(
            isinstance(x, str) for x in data
        ):
            return [], "collected.json shape: expected list[str]"

        # Round-3/4 HOLD #1+#2 mitigation: AST cross-check as a SHAPE
        # diagnostic, NOT a per-entry filter. The plugin's reported list
        # is passed through as-is (so non-default pytest discovery
        # patterns — `python_files` overrides, unittest.TestCase
        # subclasses, nose-style — are not false-dropped). But we use
        # the AST scan to detect TWO clear forgery shapes:
        #
        #   - **Overwrite-to-empty.** A malicious sessionfinish that
        #     overwrites collected.json with `[]` would look identical
        #     to a true empty workspace. We catch this: if plugin
        #     reports empty AND scratch_ws contains any real
        #     `def test_*` FunctionDef on disk, refuse.
        #
        #   - **Total forgery.** If plugin reports >0 nodeids but NONE
        #     match the on-disk AST set, AND the AST set is non-empty,
        #     that's a strong signal the JSON was overwritten with
        #     entries unrelated to anything on disk. Refuse.
        #
        # Per-entry dropping was removed (round-4 HOLD #1): workspaces
        # using non-default pytest discovery would have their real
        # nodeids false-rejected. Individual forged entries that
        # masquerade as a real on-disk function are documented as F7b
        # in 2026-06-07-005-finding-7-followups.md.
        ast_nodeids = _ast_collect_test_function_nodeids(scratch_ws)
        if not data and ast_nodeids:
            return [], (
                f"plugin reported 0 nodeids but AST scan found "
                f"{len(ast_nodeids)} on-disk test FunctionDef nodes "
                f"(possible sessionfinish overwrite-to-empty attack)"
            )
        if data and ast_nodeids:
            bare_set = {_bare_nodeid(n) for n in data}
            if not (bare_set & ast_nodeids):
                return [], (
                    f"plugin reported {len(data)} nodeids but NONE "
                    f"matched any of the {len(ast_nodeids)} on-disk "
                    f"test FunctionDef nodes (possible total-forgery "
                    f"overwrite attack)"
                )
        return list(data), None
    finally:
        shutil.rmtree(scratch_parent, ignore_errors=True)


def scratch_str_bytes(p: Path) -> bytes:
    """Stable-bytes from a Path, used to seed the per-collection plugin
    module-name hash. Defined as a tiny helper rather than inline so
    the hash input is testable in isolation.
    """
    return str(p).encode("utf-8")


# =============================================================================
# Step 8: run_gemma_under_model_lock — invoke Gemma to author qa-strategy.json
# =============================================================================


def run_gemma_under_model_lock(
    track_dir: Path,
    tmp_ws: Path,
    coder_test_nodeids: list[str],
    contract_clauses: list[dict],
    state_dir: Path,
) -> dict:
    """Invoke the Gemma local model under the global state/.model.lock to author
    `tmp_ws/qa-strategy.json` and optionally `tmp_ws/ws/test_qa.py`.

    Returns a structured result dict with keys: swap_ok, timed_out, exit_code, error.

    NOTE: this is a thin wrapper around the existing `swap_orchestrator` for the model
    swap + the Gemma prompt. The actual invocation harness for Gemma is provided by the
    crew driver (Hermes); we shell out to it. The full prompt + harness wiring is the
    `qa` Hermes profile, which is authored separately as `~/.hermes/profiles/qa/SOUL.md`.

    Failure modes returned in the dict:
      - swap failure (lms swap returned non-zero / model not found): swap_ok=False
      - Gemma timed out: timed_out=True
      - Gemma exited non-zero: exit_code != 0
    """
    model_lock = state_dir / ".model.lock"

    result: dict = {
        "swap_ok": False,
        "timed_out": False,
        "exit_code": -1,
        "error": "",
    }

    # Shadow-phase finding 2026-06-07: `_gemma_env` overrides HOME to sandbox
    # Gemma into tmp_ws, but `hermes -p qa chat` reads its profile from
    # `$HOME/.hermes/profiles/qa/`. The sandboxed HOME has no profile, so
    # hermes errors with "Profile 'qa' does not exist" and Gemma never runs.
    # Fix: stage the read-only QA profile config into the sandboxed HOME
    # BEFORE invoking hermes. We copy only the two files hermes actually
    # reads (config.yaml + SOUL.md); the rest of `~/.hermes/` stays out so
    # Gemma cannot reach user auth, history, or other agents' configs.
    try:
        _stage_qa_profile_into_tmp_home(tmp_ws)
    except OSError as e:
        result["error"] = f"profile staging failed: {e}"
        return result

    with _flock(model_lock):
        # 1. Swap to gemma via existing swap_orchestrator.
        #    swap_orchestrator takes a positional `profile` arg and reads
        #    model.default from ~/.hermes/profiles/<profile>/config.yaml.
        try:
            swap = subprocess.run(
                [
                    "python3",
                    "-m",
                    "tools.swap_orchestrator",
                    QA_PROFILE,
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            result["error"] = f"swap orchestrator failed: {e}"
            return result
        if swap.returncode != 0:
            result["error"] = f"swap exit {swap.returncode}: {swap.stderr[-200:]}"
            return result
        result["swap_ok"] = True

        # 2. Invoke Gemma via the QA profile.
        #    The profile is `~/.hermes/profiles/qa/SOUL.md` (authored separately).
        #    `hermes -p qa chat -q <prompt> -Q --max-turns N` runs the QA agent
        #    in non-interactive single-query mode against the profile config.
        #    The CLI --max-turns is belt-and-suspenders on top of the
        #    `agent.max_turns` value in the profile config (kept in sync — see
        #    F9 2026-06-07 bump notes below).
        try:
            proc = subprocess.run(
                [
                    "hermes",
                    "-p",
                    QA_PROFILE,
                    "chat",
                    "-q",
                    _build_gemma_prompt(
                        track_dir, tmp_ws, coder_test_nodeids, contract_clauses
                    ),
                    "-Q",
                    "--max-turns",
                    # Shadow-phase finding (2026-06-07 Option 1 follow-up):
                    # at 8 Gemma hit the iteration limit while reading the
                    # contract + code + tests, never reaching the write step.
                    # F9 (2026-06-07): bumped 12 → 16 after shadow #3
                    # (12 clauses, 59 nodeids, ~1086 LOC) reproduced the same
                    # "Reached maximum iterations" exit ~50% of runs at 12.
                    # The SOUL "WRITE FIRST, REFINE LATER" directive is the
                    # primary fix; this is belt+suspenders headroom for the
                    # cases where Gemma still does some reconnaissance.
                    "16",
                ],
                capture_output=True,
                text=True,
                timeout=GEMMA_TIMEOUT_SECONDS,
                env=_gemma_env(tmp_ws),
            )
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            result["error"] = "gemma timed out"
            return result
        except OSError as e:
            result["error"] = f"gemma invocation failed: {e}"
            return result
        result["exit_code"] = proc.returncode
        if proc.returncode != 0:
            result["error"] = f"gemma exit {proc.returncode}: {proc.stderr[-200:]}"
    return result


def _build_gemma_prompt(
    track_dir: Path,
    tmp_ws: Path,
    coder_test_nodeids: list[str],
    contract_clauses: list[dict],
) -> str:
    """Build the routing prompt for Gemma. Embeds the parsed clause IDs so Gemma's
    qa-strategy.json must account for every one (C18a enforces this).

    Shadow-phase finding (2026-06-07): the previous wording "Author
    qa-strategy.json in {tmp_ws}" was ambiguous — Gemma interpreted "in
    {tmp_ws}" as "in the workspace dir under tmp_ws", and wrote the file to
    `tmp_ws/ws/qa-strategy.json`. C1 then failed because the runner looks at
    `tmp_ws/qa-strategy.json`. The fix is to give the absolute path
    explicitly, and to call out the schema split (planned_tests vs
    skipped_clauses + abstract_or_concrete) so Gemma doesn't conflate them.
    """
    strategy_path = tmp_ws / "qa-strategy.json"
    test_qa_path = tmp_ws / "ws" / "test_qa.py"
    clause_ids = [c["id"] for c in contract_clauses]
    return (
        f"Read the contract at {track_dir}/product-plan.json "
        f"(clause IDs: {clause_ids}) and the coder's tests, whose pytest "
        f"nodeids are: {coder_test_nodeids}.\n\n"
        f"WRITE TWO FILES AT THESE EXACT ABSOLUTE PATHS:\n"
        f"  1. {strategy_path}\n"
        f"  2. {test_qa_path}  (OPTIONAL — only if n_tests_written > 0)\n\n"
        f"Your qa-strategy.json must follow the schema in your SOUL.md and "
        f"account for EVERY clause_id in {clause_ids} exactly once across "
        f"reviewed_clauses (array of all ids considered), planned_tests "
        f"(ids you authored a probe for), and skipped_clauses (ids you "
        f"affirmatively skipped). When you cite a coder test in "
        f"`coder_test_evidence`, use the EXACT nodeid strings above "
        f"(canonical pytest form `file.py::test_name`); the deterministic "
        f"check C3 compares string-equal. Schema invariants and forbidden "
        f"patterns are in your SOUL.md."
    )


def _gemma_env(tmp_ws: Path) -> dict:
    """Environment for the Gemma invocation: HOME/XDG/TMPDIR all under tmp_ws.

    Codex review finding (2026-06-07 P1): `HERMES_HOME`, if inherited from the
    caller's env, overrides HOME-based profile resolution and re-exposes the
    real `~/.hermes/`. Pop it explicitly so hermes falls back to the sandboxed
    HOME.
    """
    env = dict(os.environ)
    env["HOME"] = str(tmp_ws / "home")
    env["XDG_CACHE_HOME"] = str(tmp_ws / "cache")
    env["TMPDIR"] = str(tmp_ws / "tmp")
    env.pop("HERMES_HOME", None)
    return env


def _recover_misplaced_strategy(
    tmp_ws: Path, workspace_had_strategy_before_gemma: bool
) -> dict:
    """Shadow-phase finding 5 fallback (2026-06-07): if Gemma wrote
    `qa-strategy.json` into `tmp_ws/ws/` (the workspace dir) instead of
    `tmp_ws/` (the canonical location), MOVE it to the canonical location
    and record the recovery in qa.json.

    Returns `{recovered, source_path, reason}`:
      - `recovered=True` only when the move succeeded with VALID JSON content
        AND `tmp_ws/ws/qa-strategy.json` did NOT exist before Gemma ran (the
        provenance gate; see below).
      - `source_path` is the workspace location it was moved from, or None.
      - `reason` is a non-None diagnostic string when the function decides
        not to recover.

    Trust property (Codex review round 1 P1 + round 2 TOCTOU): the workspace
    dir `tmp_ws/ws/` is writable by anything that runs in the QA pipeline.
    During Gemma's authoring turn only Gemma's write_file tool can plant a
    file there (the `qa` profile's toolset is `file`-only, no terminal), but
    the canonical workspace could ALREADY ship a `qa-strategy.json` — a
    coder mistake, or a deliberate plant. `make_tmp_ws` would copy that
    into `tmp_ws/ws/`, and a naive recovery would then promote it as if
    Gemma authored it.

    Provenance gate: the caller samples
    `(tmp_ws / "ws" / "qa-strategy.json").is_file()` IMMEDIATELY after
    `make_tmp_ws` and BEFORE invoking Gemma, then passes the bit here as
    `workspace_had_strategy_before_gemma`. (Sampling the canonical workspace
    BEFORE `make_tmp_ws` — as the round-1 fix did — leaves a TOCTOU window
    where a strategy that appears during the copy slips past the gate; the
    round-2 fix closes that by sampling the actual copy post-copy.) When
    True, recovery REFUSES even with valid JSON — we cannot distinguish a
    Gemma-authored file from a workspace-shipped one, so we fail closed.
    The caller's early missing-canonical short-circuit in `run_qa` then
    HOLDs the build immediately with the refusal reason in `failure_reason`,
    before C1/C12 even fire.

    Safety on invalid JSON: a misplaced strategy with INVALID JSON is NEVER
    promoted — it would defeat the audit trail and replace one failure mode
    (C1 file missing) with another (C1 parse error) silently. We leave the
    bad file in place so both downstream checks surface it.

    Atomicity: `Path.rename` within the same filesystem is atomic on POSIX.
    Both source and destination live under tmp_ws (under state/qa-tmp/),
    same filesystem guaranteed by `make_tmp_ws`.
    """
    canonical = tmp_ws / "qa-strategy.json"
    fallback = tmp_ws / "ws" / "qa-strategy.json"

    if canonical.is_file():
        return {
            "recovered": False,
            "source_path": None,
            "reason": "canonical path already present; no recovery needed",
        }
    if not fallback.is_file():
        return {
            "recovered": False,
            "source_path": None,
            "reason": "no fallback at tmp_ws/ws/qa-strategy.json",
        }

    # Provenance gate (Codex review round 1 P1 + round 2 TOCTOU): refuse to
    # recover if the workspace copy already had qa-strategy.json BEFORE
    # Gemma ran. Sampling tmp_ws/ws/ post-make_tmp_ws is the canonical
    # state Gemma will see, with no TOCTOU window vs the canonical_ws.
    if workspace_had_strategy_before_gemma:
        return {
            "recovered": False,
            "source_path": None,
            "reason": (
                "workspace copy had a pre-existing qa-strategy.json before "
                "Gemma ran; refusing to recover (provenance ambiguous — "
                "would mask a supply-chain plant)"
            ),
        }

    # Validate the fallback before promoting — never recover invalid JSON.
    try:
        content = fallback.read_text()
        json.loads(content)
    except (OSError, json.JSONDecodeError) as e:
        return {
            "recovered": False,
            "source_path": None,
            "reason": f"fallback present but unreadable/invalid JSON: {e}",
        }

    # Atomic move. If the rename fails (e.g. cross-device), fall back to
    # copy+unlink so we still recover but record the slower path.
    try:
        fallback.rename(canonical)
    except OSError:
        try:
            shutil.copy2(fallback, canonical)
            fallback.unlink()
        except OSError as e:
            return {
                "recovered": False,
                "source_path": None,
                "reason": f"could not move fallback to canonical: {e}",
            }
    return {
        "recovered": True,
        "source_path": str(fallback),
        "reason": None,
    }


def _stage_qa_profile_into_tmp_home(tmp_ws: Path) -> None:
    """Stage the read-only QA profile (config + SOUL) into `tmp_ws/home`.

    `_gemma_env` overrides HOME so Gemma's subprocess cannot reach the user's
    real `~/.hermes/` (auth, history, other agents' configs). But hermes itself
    needs `$HOME/.hermes/profiles/<QA_PROFILE>/{config.yaml,SOUL.md}` to know
    what model to use and what system prompt to load. We copy ONLY those two
    files — nothing else from `~/.hermes/` — so the sandbox stays tight while
    hermes still finds the profile.

    Idempotent: safe to call repeatedly within a tmp_ws lifecycle.

    Raises OSError (incl. FileNotFoundError) if the user's QA profile is
    missing or its required files cannot be read; the caller short-circuits
    the Gemma invocation and surfaces the error.
    """
    real_profile = paths.hermes_profiles() / QA_PROFILE
    staged_profile = tmp_ws / "home" / ".hermes" / "profiles" / QA_PROFILE
    staged_profile.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "SOUL.md"):
        src = real_profile / name
        if not src.is_file():
            raise FileNotFoundError(
                f"required QA profile file missing: {src}"
            )
        shutil.copy2(src, staged_profile / name)


# =============================================================================
# Step 11: validate_pre — run C1–C12, C15, C18a in sequence
# =============================================================================


def validate_pre(
    strategy_path: Path,
    test_qa_path: Path,
    contract_clauses: list[dict],
    workspace_module_names: set[str],
    canonical_ws: Path,
    tmp_ws: Path,
    gemma_result: dict,
    coder_test_nodeids: set[str],
) -> list:
    """Run the pre-pytest check sequence and return the first failure (or [] if all ok).

    Returns the LIST of CheckResults; the caller HOLDs as soon as one fails.
    """
    results = []

    r1 = c1_strategy_well_formed(strategy_path)
    results.append(r1)
    if not r1.ok:
        return results

    strategy = json.loads(strategy_path.read_text())

    for check in (
        ("C18a", lambda: c18a_clause_completeness(strategy, contract_clauses)),
        ("C15", lambda: c15_gemma_ran_successfully(gemma_result)),
        ("C3", lambda: c3_evidence_nodeids_exist(strategy, coder_test_nodeids)),
        ("C4", lambda: c4_n_tests_capped(strategy)),
        ("C5", lambda: c5_n_tests_matches_test_file_presence(strategy, test_qa_path)),
        ("C6", lambda: c6_test_qa_parses(test_qa_path)),
        ("C7", lambda: c7_planned_tests_bijective(strategy, test_qa_path)),
        ("C8", lambda: c8_docstrings_cite_clause_id(strategy, test_qa_path)),
        ("C9", lambda: c9_ast_allowlist(test_qa_path, workspace_module_names)),
        ("C10", lambda: c10_asserted_in_test_resolves(strategy, test_qa_path)),
        ("C11", lambda: c11_no_symlinks(tmp_ws / "ws")),
        ("C12", lambda: c12_only_test_qa_differs(tmp_ws / "ws", canonical_ws)),
    ):
        r = check[1]()
        results.append(r)
        if not r.ok:
            return results
    return results


# =============================================================================
# Step 12: run_pytest_sandboxed
# =============================================================================


def run_pytest_sandboxed(tmp_ws: Path, timeout_seconds: int = 300) -> dict:
    """Run pytest inside the sandbox-exec jail. Returns dict with exit code, nodeids,
    failures, stdout/stderr excerpts.

    The sandbox-exec policy is materialized to a temp file and substituted with the
    actual tmp_ws path so that writes outside the jail are denied at the kernel level.
    """
    ws = tmp_ws / "ws"
    policy_file = tmp_ws / "sandbox.sb"
    policy_file.write_text(SANDBOX_EXEC_POLICY_TEMPLATE.replace("<TMP_WS>", str(tmp_ws)))

    env = _gemma_env(tmp_ws)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        proc = subprocess.run(
            [
                "sandbox-exec",
                "-f",
                str(policy_file),
                "python3",
                "-m",
                "pytest",
                # `-v` (not `-q`): we need per-test nodeid lines on the
                # SUCCESS path. `-q` only prints `.`/`F` progress dots on
                # pass, so the `"::" in line` parser below returned an
                # empty list, making `qa.json.collected_count` always 0
                # on confirmed runs and starving the Codex routing prompt
                # of test-name context. `-v` emits
                # `test_qa.py::test_x PASSED` per test, which the parser
                # captures correctly. Output is noisier but the runner
                # already truncates stdout/stderr to the last 2000 chars,
                # so blast is bounded; failure-path parsing is unchanged
                # (the summary `FAILED <nodeid>` lines that drive the
                # `failures` list still appear).
                "-v",
                "-p",
                "no:cacheprovider",
                "--tb=short",
                "test_qa.py",
            ],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {
            "exit": -1,
            "nodeids": [],
            "failures": ["pytest timed out"],
            "stdout_excerpt": "",
            "stderr_excerpt": "TIMEOUT",
        }
    except OSError as e:
        # Codex review (2026-06-06): sandbox-exec missing or unexecutable.
        # Return a structured failure so the orchestrator can HOLD cleanly.
        return {
            "exit": -1,
            "nodeids": [],
            "failures": [f"sandbox-exec invocation failed: {e}"],
            "stdout_excerpt": "",
            "stderr_excerpt": str(e),
        }

    # Parse pytest output for nodeids + failures.
    nodeids: list[str] = []
    failures: list[str] = []
    for line in proc.stdout.splitlines():
        if "::" in line and line.strip():
            nodeids.append(line.split()[0])
        if line.startswith("FAILED"):
            failures.append(line)

    return {
        "exit": proc.returncode,
        "nodeids": nodeids,
        "failures": failures,
        "stdout_excerpt": proc.stdout[-2000:],
        "stderr_excerpt": proc.stderr[-2000:],
    }


# =============================================================================
# Step 12 guard: strategic-null short-circuit
# =============================================================================


def _resolve_pytest_results(
    strategy: dict, runner: Callable[[], dict]
) -> dict:
    """Step 12 wrapper: invoke pytest in the sandbox UNLESS the strategy is
    strategic-null (`n_tests_written == 0`).

    Why this guard (Rev 5 spec, "Failure modes" / SOUL):

    > Writing zero tests is a VALID and POSITIVE outcome: every clause goes in
    > skipped_clauses with sound coder-evidence citations.

    The orchestrator's Step 11 (C5) has already enforced
    `n_tests_written == 0 ↔ test_qa.py absent`. So when n=0 there's literally
    nothing for pytest to run. But `pytest -q test_qa.py` against a missing file
    exits 4 (file-not-found), and `compute_route` routes any non-1 nonzero exit
    to `hold_human` — which would silently bury the spec's intended
    "strategic null, all skip_earned → confirm" outcome.

    Synthesizing a zero-test clean exit here is the semantically-correct
    equivalent of running pytest against an empty test file. `compute_route`'s
    confirm branch then sees `pytest_exit == 0` and the Codex `skip_earned`
    verdicts and lands on `confirm`. Without this guard the post-live acceptance
    signal "≥ 1 real build CONFIRMS with n_tests_written == 0" is unreachable.

    `runner` is a thunk wrapping `run_pytest_sandboxed(tmp_ws, ...)`, decoupled
    from the call site so this branch can be unit-tested without scaffolding the
    full QA pipeline.
    """
    if strategy.get("n_tests_written", 0) == 0:
        # NB: a fresh dict each call so callers may mutate without state-sharing.
        return {
            "exit": 0,
            "nodeids": [],
            "failures": [],
            "stdout_excerpt": "",
            "stderr_excerpt": "",
        }
    return runner()


# =============================================================================
# Step 14, 16: write_qa_json, promote_strategy (atomic)
# =============================================================================


def write_qa_json(
    track_dir: Path,
    route_input: dict,
    canonical_hash_pre: str,
    canonical_hash_post: str,
    tmp_ws: Path,
    pytest_results: dict,
    strategy: dict,
    token_usage: dict | None = None,
    strategy_recovered_from_workspace: bool = False,
    strategy_recovery_source: str | None = None,
) -> Path:
    """Write `qa.json` to track_dir atomically. Returns the path.

    qa.json shape (Rev 5 spec):
      {
        ran_at: ISO8601,
        canonical_hash_pre, canonical_hash_post,
        tmp_ws_path,
        gemma_model,
        planned_count,
        collected_count,
        pytest_exit,
        failures,
        route_input_sha256,
        strategy_sha256,
        codex_tokens,
        strategy_recovered_from_workspace,  // shadow-phase finding 5 fallback
        strategy_recovery_source,            // path Gemma misplaced to, or null
      }
    """
    ri_sha = hashlib.sha256(
        json.dumps(route_input, sort_keys=True).encode("utf-8")
    ).hexdigest()
    s_sha = hashlib.sha256(
        json.dumps(strategy, sort_keys=True).encode("utf-8")
    ).hexdigest()

    qa_json = {
        "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "canonical_hash_pre": canonical_hash_pre,
        "canonical_hash_post": canonical_hash_post,
        "tmp_ws_path": str(tmp_ws),
        "gemma_model": GEMMA_MODEL,
        "planned_count": len(strategy.get("planned_tests") or []),
        "collected_count": len(pytest_results.get("nodeids") or []),
        "pytest_exit": pytest_results.get("exit"),
        "failures": pytest_results.get("failures") or [],
        "route_input_sha256": ri_sha,
        "strategy_sha256": s_sha,
        "codex_tokens": token_usage or {},
        # Shadow-phase finding 5 fallback (2026-06-07): when True, Gemma
        # wrote qa-strategy.json to the workspace dir (tmp_ws/ws/) and the
        # runner moved it to the canonical location. Surfaces in audit so
        # the underlying SOUL path-discipline issue is visible.
        "strategy_recovered_from_workspace": strategy_recovered_from_workspace,
        "strategy_recovery_source": strategy_recovery_source,
    }
    return _atomic_write_json(track_dir / "qa.json", qa_json)


def promote_strategy(tmp_ws: Path, track_dir: Path) -> Path:
    """Move tmp_ws/qa-strategy.json → track/qa-strategy.json atomically (O_EXCL + fsync)."""
    src = tmp_ws / "qa-strategy.json"
    if not src.is_file():
        raise FileNotFoundError(f"qa-strategy.json missing in tmp_ws: {src}")
    data = src.read_text()
    return _atomic_write_text(track_dir / "qa-strategy.json", data)


def _atomic_write_json(dst: Path, data: dict) -> Path:
    return _atomic_write_text(dst, json.dumps(data, indent=2, sort_keys=True))


def _atomic_write_text(dst: Path, text: str) -> Path:
    """O_EXCL+fsync+os.replace pattern. Refuses to leave a half-written artifact.

    Codex review (2026-06-06) NIT 2: write in a loop (handles short writes), unlink the
    temp file on exception, and fsync the parent dir after `os.replace` so the rename
    survives a power loss.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{dst.name}.tmp.{os.getpid()}"
    tmp_path = dst.parent / tmp_name
    payload = text.encode("utf-8")
    fd = os.open(
        str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
    )
    try:
        # Handle short writes by looping until everything is on disk.
        offset = 0
        while offset < len(payload):
            n = os.write(fd, payload[offset:])
            if n <= 0:
                raise OSError(f"os.write returned {n}; cannot make progress")
            offset += n
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(tmp_path, dst)
    # Fsync the parent dir so the rename is durable across power loss.
    try:
        dir_fd = os.open(str(dst.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # On some filesystems (e.g. some FUSE mounts) parent fsync is a no-op or errors;
        # the os.replace itself was atomic so this is best-effort.
        pass
    return dst


def _track_file_digests(track_dir: Path) -> dict[str, str]:
    """Compute sha256 of every regular file in `track_dir` (non-recursive). Used by the
    Gemma-mutation guard (Codex review BLOCKER 2): we snapshot track files before Gemma
    runs, then compare after — any change to product-plan.json / idea-contract.json /
    etc. HOLDs the build."""
    out: dict[str, str] = {}
    if not track_dir.is_dir():
        return out
    for entry in track_dir.iterdir():
        if entry.is_file() and not entry.is_symlink():
            # Skip the lockfiles and any runner-owned artifacts (we DO write these).
            if entry.name in {".qa.lock", "qa.json", "qa-strategy.json", "qa-route.json"}:
                continue
            out[entry.name] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return out


# =============================================================================
# Step 21: emit_event
# =============================================================================


def emit_event(
    state_dir: Path, track_id: str, route: str, summary_reason: str
) -> Path | None:
    """Append a `qa-route` event to state/events.jsonl. Returns the path (or None if
    the events dir isn't reachable)."""
    events = state_dir / "events.jsonl"
    if not events.parent.is_dir():
        return None
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "job_id": track_id,
            "profile": "qa",
            "event": "qa-route",
            "route": route,
            "summary_reason": summary_reason,
        }
    )
    with open(events, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    return events


# =============================================================================
# Top-level orchestrator
# =============================================================================


def run_qa(
    track_dir: Path,
    canonical_ws: Path,
    state_dir: Path,
    coder_test_nodeids: list[str] | None = None,
    workspace_module_names: set[str] | None = None,
    pytest_timeout: int = 300,
) -> QARun:
    """Run the full QA pipeline for one track. Returns a QARun structured outcome.

    The function ALWAYS releases its locks via try/finally; tmp_ws is cleaned up after
    route input capture (step 15) regardless of pass/fail.
    """
    started = time.monotonic()
    track_id = track_dir.name
    coder_test_nodeids = coder_test_nodeids or []
    workspace_module_names = workspace_module_names or set()

    # Steps 1–3 happen BEFORE the lock (we want fingerprint_pre captured early).
    try:
        reject_symlinks_and_submodules(canonical_ws)
        ignored_pre = snapshot_and_clean_residue(canonical_ws)
        canonical_hash_pre = canonical_fingerprint(canonical_ws)
    except Exception as e:
        return QARun(
            track_id=track_id,
            computed_route="hold_human",
            failure_reason=f"pre-flight failed: {e}",
            canonical_hash_pre="",
            canonical_hash_post="",
            qa_json_path=None,
            qa_strategy_path=None,
            qa_route_path=None,
            elapsed_seconds=time.monotonic() - started,
        )

    qa_lock = track_dir / ".qa.lock"
    tmp_ws: Path | None = None
    canonical_hash_post = ""
    try:
        with _flock(qa_lock):
            # Codex review BLOCKER 1 (2026-06-06): parse contract clauses BEFORE Gemma
            # is invoked, so the prompt embeds the real clause IDs Gemma must account for.
            # Previously we parsed AFTER the Gemma run with [], causing C18a to fail for
            # any non-empty contract.
            try:
                contract_clauses = parse_contract_clauses(
                    track_dir / "product-plan.json"
                )
            except (FileNotFoundError, ValueError) as e:
                return _short_circuit(
                    track_id,
                    f"contract parse failed: {e}",
                    canonical_hash_pre,
                    "",
                    state_dir,
                    started,
                )

            # Codex review BLOCKER 2 (2026-06-06): snapshot track-dir file digests
            # BEFORE Gemma runs. Any mutation of product-plan.json or idea-contract.json
            # between snapshot and post-Gemma check is a trust-spine attack and HOLDs.
            track_pre_digests = _track_file_digests(track_dir)

            # Steps 5–6: build tmp_ws + preclear track artifacts (after snapshot so the
            # snapshot reflects the pre-runner-write state).
            try:
                tmp_ws = make_tmp_ws(track_id, canonical_ws)
            except Exception as e:
                return _short_circuit(
                    track_id,
                    f"make_tmp_ws failed: {e}",
                    canonical_hash_pre,
                    "",
                    state_dir,
                    started,
                )
            preclear_track_artifacts(track_dir)

            # Codex review round 2 P1 (2026-06-07 Option 2 TOCTOU fix):
            # snapshot whether the COPIED workspace already has qa-strategy.json
            # right BEFORE Gemma runs. The original P1 check sampled
            # `canonical_ws / "qa-strategy.json"` BEFORE make_tmp_ws — that
            # left a TOCTOU window where a planted strategy appearing during
            # the copy would bypass the gate. Sampling tmp_ws/ws/ post-copy
            # captures the actual workspace state Gemma will see.
            workspace_had_strategy_before_gemma = (
                tmp_ws / "ws" / "qa-strategy.json"
            ).is_file()

            # Step 8: Gemma authors qa-strategy.json (+ test_qa.py) under model lock.
            gemma_result = run_gemma_under_model_lock(
                track_dir,
                tmp_ws,
                coder_test_nodeids,
                contract_clauses,  # real IDs now (Codex review BLOCKER 1 fix)
                state_dir,
            )

            # Codex review finding (2026-06-07 P2): if Gemma never ran (profile
            # staging failure, swap failure, timeout, non-zero exit), the
            # downstream C1 check would report "qa-strategy.json missing" and
            # mask the real root cause. Short-circuit here so the operator sees
            # the actual Gemma-side error in the failure_reason field.
            if (
                not gemma_result.get("swap_ok", False)
                or gemma_result.get("timed_out", False)
                or gemma_result.get("exit_code", -1) != 0
            ):
                err = gemma_result.get("error") or "unknown gemma failure"
                return _short_circuit(
                    track_id,
                    f"Gemma did not run cleanly: {err}",
                    canonical_hash_pre,
                    "",
                    state_dir,
                    started,
                )

            # Shadow-phase finding 5 fallback (2026-06-07 Option 2): Gemma is
            # non-deterministic at hitting the canonical strategy path. If it
            # wrote to tmp_ws/ws/qa-strategy.json instead, MOVE the file (only
            # when valid JSON AND the canonical workspace did NOT ship one
            # pre-existing — the provenance gate from Codex review round 1
            # P1) so C1 finds it at the canonical location. The recovery is
            # recorded in qa.json so it shows up in audit + ops dashboards —
            # silent recovery would mask the underlying SOUL path-discipline
            # issue.
            recovery = _recover_misplaced_strategy(
                tmp_ws, workspace_had_strategy_before_gemma
            )
            strategy_recovered_from_workspace = recovery["recovered"]
            strategy_recovery_source = recovery["source_path"]

            # Belt-and-suspenders: short-circuit early if we still have no
            # canonical strategy after recovery attempt. This is the same
            # state C1 will catch in a few lines, but surfacing it here makes
            # the failure_reason mention the recovery attempt explicitly.
            if not (tmp_ws / "qa-strategy.json").is_file():
                fallback_note = recovery["reason"] or "no recovery attempted"
                return _short_circuit(
                    track_id,
                    f"qa-strategy.json missing at canonical path; "
                    f"recovery: {fallback_note}",
                    canonical_hash_pre,
                    "",
                    state_dir,
                    started,
                )

            # Codex review BLOCKER 2 (2026-06-06): verify Gemma didn't mutate track files
            # (product-plan.json / idea-contract.json / etc.) during its run. Tmp_ws is
            # disposable; track files are not.
            track_post_digests = _track_file_digests(track_dir)
            tampered = sorted(
                fn
                for fn in track_pre_digests
                if track_pre_digests[fn] != track_post_digests.get(fn)
            )
            if tampered:
                return _short_circuit(
                    track_id,
                    f"track files mutated during Gemma run: {tampered[:5]}",
                    canonical_hash_pre,
                    "",
                    state_dir,
                    started,
                )

            # Step 11: pre-pytest validation. (Step 10 already done above — clauses
            # parsed before Gemma per BLOCKER 1 fix.)
            strategy_path = tmp_ws / "qa-strategy.json"
            test_qa_path = tmp_ws / "ws" / "test_qa.py"
            results = validate_pre(
                strategy_path,
                test_qa_path,
                contract_clauses,
                workspace_module_names,
                canonical_ws,
                tmp_ws,
                gemma_result,
                set(coder_test_nodeids),
            )
            last = results[-1]
            if not last.ok:
                return _short_circuit(
                    track_id,
                    f"pre-check {last.name} failed: {last.reason}",
                    canonical_hash_pre,
                    "",
                    state_dir,
                    started,
                )

            strategy = json.loads(strategy_path.read_text())

            # Step 12: pytest in sandbox — short-circuited for strategic-null
            # (n_tests_written == 0; C5 already enforced no test_qa.py). See
            # `_resolve_pytest_results` for why running pytest against a missing
            # file would route this case incorrectly to hold_human.
            pytest_results = _resolve_pytest_results(
                strategy,
                lambda: run_pytest_sandboxed(
                    tmp_ws, timeout_seconds=pytest_timeout
                ),
            )

            # Step 13: post-fingerprint.
            canonical_hash_post = canonical_fingerprint(canonical_ws)

            # Step 14: post-pytest checks.
            post = [
                c13_canonical_unchanged(
                    canonical_hash_pre, canonical_hash_post, canonical_ws
                ),
                c13b_no_new_ignored_residue(ignored_pre, canonical_ws),
                c16_pytest_collected_n_tests(strategy, tmp_ws / "ws"),
            ]
            for r in post:
                if not r.ok:
                    return _short_circuit(
                        track_id,
                        f"post-check {r.name} failed: {r.reason}",
                        canonical_hash_pre,
                        canonical_hash_post,
                        state_dir,
                        started,
                    )

            # Step 15: build route_input BEFORE cleanup.
            test_qa_source = (
                test_qa_path.read_text() if test_qa_path.is_file() else ""
            )
            coder_test_sources = _extract_coder_test_sources(
                canonical_ws, coder_test_nodeids
            )
            route_input = build_route_input(
                contract_clauses=contract_clauses,
                strategy=strategy,
                test_qa_source=test_qa_source,
                pytest_results=pytest_results,
                coder_test_sources=coder_test_sources,
            )

            # Step 14b: C18b (post-cleanup completeness — we have everything we need).
            c18b = c18b_route_input_has_all_source(strategy, route_input)
            if not c18b.ok:
                return _short_circuit(
                    track_id,
                    f"post-check C18b failed: {c18b.reason}",
                    canonical_hash_pre,
                    canonical_hash_post,
                    state_dir,
                    started,
                )

            # Step 16: write qa.json. Step 17: promote strategy. Step 18: rmtree.
            qa_json_path = write_qa_json(
                track_dir,
                route_input,
                canonical_hash_pre,
                canonical_hash_post,
                tmp_ws,
                pytest_results,
                strategy,
                strategy_recovered_from_workspace=(
                    strategy_recovered_from_workspace
                ),
                strategy_recovery_source=strategy_recovery_source,
            )
            qa_strategy_path = promote_strategy(tmp_ws, track_dir)

            # Codex review SHOULD-FIX 2 (2026-06-06): explicit step-18 rmtree happens
            # HERE (after route_input captured + qa.json/qa-strategy.json promoted) so
            # the Codex call below sees a clean state. The outer try/finally rmtree is
            # the backup safety net for unexpected failures.
            shutil.rmtree(tmp_ws, ignore_errors=True)
            tmp_ws = None

            # Step 19–20: Codex routing + deterministic compute.
            try:
                codex_verdicts = call_codex_route(route_input)
            except Exception as e:
                return _short_circuit(
                    track_id,
                    f"codex routing call failed: {e}",
                    canonical_hash_pre,
                    canonical_hash_post,
                    state_dir,
                    started,
                )
            qa_route = validate_and_compute_route(
                strategy,
                codex_verdicts,
                pytest_exit=pytest_results.get("exit", -1),
            )
            qa_route_path = _atomic_write_json(
                track_dir / "qa-route.json", qa_route
            )

            # Step 21: emit event.
            emit_event(
                state_dir,
                track_id,
                qa_route["computed_route"],
                qa_route.get("summary_reason", ""),
            )

            return QARun(
                track_id=track_id,
                computed_route=qa_route["computed_route"],
                failure_reason="",
                canonical_hash_pre=canonical_hash_pre,
                canonical_hash_post=canonical_hash_post,
                qa_json_path=qa_json_path,
                qa_strategy_path=qa_strategy_path,
                qa_route_path=qa_route_path,
                elapsed_seconds=time.monotonic() - started,
            )
    finally:
        # Step 18: cleanup tmp_ws. Idempotent.
        if tmp_ws is not None and tmp_ws.is_dir():
            shutil.rmtree(tmp_ws, ignore_errors=True)


def _short_circuit(
    track_id: str,
    reason: str,
    canonical_hash_pre: str,
    canonical_hash_post: str,
    state_dir: Path,
    started: float,
) -> QARun:
    """Helper: emit a hold_human QARun on any check failure."""
    emit_event(state_dir, track_id, "hold_human", reason)
    return QARun(
        track_id=track_id,
        computed_route="hold_human",
        failure_reason=reason,
        canonical_hash_pre=canonical_hash_pre,
        canonical_hash_post=canonical_hash_post,
        qa_json_path=None,
        qa_strategy_path=None,
        qa_route_path=None,
        elapsed_seconds=time.monotonic() - started,
    )


def _extract_coder_test_sources(
    canonical_ws: Path, coder_test_nodeids: list[str]
) -> dict[str, str]:
    """Extract source snippets for each cited coder test nodeid.

    Reads the canonical_ws files and extracts up to 30 lines of each test function.

    Codex review round 1 HOLD #2 (2026-06-07, finding #7): parametrized
    nodeids like ``test_alpha.py::test_param[42]`` map to the FunctionDef
    ``test_param`` in source (the ``[42]`` is the runtime parametrization
    id, not part of the def name). Strip the bracketed suffix before AST
    lookup; keep the full original nodeid as the dict key so C18b can
    string-equal-match what C3 saw.
    """
    import ast as _ast

    out: dict[str, str] = {}
    for nodeid in coder_test_nodeids:
        # nodeid shape: "test_graph.py::test_x" or "tests/test_graph.py::TestX::test_y"
        # or "test_graph.py::test_x[param]" (parametrized).
        #
        # Codex review round 2 HOLD #1 + round 3 NIT #6 (2026-06-07,
        # finding #7): the parametrize id can itself contain `::` (e.g.
        # `test_x[a::b]`). Strip the bracketed suffix BEFORE splitting on
        # `::`; otherwise `nodeid.split("::")[-1]` returns `"b]"` and AST
        # lookup fails for a real parametrized test → C18b HOLD. AND:
        # only look for `[` AFTER the first `::`, so a file path that
        # happens to contain `[` (rare but legal on POSIX) doesn't get
        # treated as a param suffix.
        first_sep = nodeid.find("::")
        if first_sep == -1:
            continue
        bracket_idx = nodeid.find("[", first_sep)
        bare_nodeid = nodeid if bracket_idx == -1 else nodeid[:bracket_idx]
        parts = bare_nodeid.split("::")
        path_part = parts[0]
        if len(parts) < 2:
            continue
        func_name = parts[-1]
        src_file = canonical_ws / path_part
        if not src_file.is_file():
            continue
        try:
            src = src_file.read_text()
            tree = _ast.parse(src)
        except (OSError, SyntaxError):
            continue
        src_lines = src.splitlines(keepends=True)
        for node in _ast.walk(tree):
            if (
                isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and node.name == func_name
            ):
                start = (
                    min(d.lineno for d in node.decorator_list) - 1
                    if node.decorator_list
                    else node.lineno - 1
                )
                end = node.end_lineno or start + 1
                # Cap at 30 lines.
                snippet = "".join(src_lines[start : min(end, start + 30)])
                out[nodeid] = snippet
                break
    return out


# =============================================================================
# CLI entry
# =============================================================================


def _main() -> int:
    """`python -m tools.qa <track_dir> <canonical_ws> [--state state/]`"""
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("track_dir", type=Path)
    p.add_argument("canonical_ws", type=Path)
    p.add_argument("--state", type=Path, default=Path("state"))
    args = p.parse_args()
    result = run_qa(args.track_dir, args.canonical_ws, args.state)
    print(
        json.dumps(
            {
                "track_id": result.track_id,
                "computed_route": result.computed_route,
                "failure_reason": result.failure_reason,
                "elapsed_seconds": round(result.elapsed_seconds, 2),
            },
            indent=2,
        )
    )
    return 0 if result.computed_route == "confirm" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
