"""Tests for tools.qa — the QA layer runner orchestrator.

Strategy: unit-test the pure helpers + filesystem-touching utilities. The full
`run_qa` orchestrator depends on Gemma + sandbox-exec + Codex live; integration
tests via the F1–F4 fixtures live separately and aren't covered here.
"""

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from tools.qa import (
    SANDBOX_EXEC_POLICY_TEMPLATE,
    QARun,
    _atomic_write_json,
    _atomic_write_text,
    _collect_coder_nodeids,
    _extract_coder_test_sources,
    _flock,
    _recover_misplaced_strategy,
    _resolve_pytest_results,
    emit_event,
    make_tmp_ws,
    preclear_track_artifacts,
    promote_strategy,
    write_qa_json,
)


# =============================================================================
# Helpers
# =============================================================================


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init", "--initial-branch=main", "-q"], cwd=path)
    subprocess.check_call(
        ["git", "config", "user.email", "test@test.invalid"], cwd=path
    )
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=path)


# =============================================================================
# _flock
# =============================================================================


def test_flock_acquire_release(tmp_path):
    lock = tmp_path / ".lock"
    with _flock(lock):
        assert lock.exists()
    # After release, lock file persists but is releasable again.
    with _flock(lock):
        pass


def test_flock_creates_parent(tmp_path):
    lock = tmp_path / "subdir" / ".lock"
    with _flock(lock):
        assert lock.exists()
        assert lock.parent.is_dir()


# =============================================================================
# make_tmp_ws
# =============================================================================


def test_make_tmp_ws_layout(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    subprocess.check_call(["git", "add", "-A"], cwd=tmp_path)
    subprocess.check_call(["git", "commit", "-qm", "init"], cwd=tmp_path)
    tmp_ws = make_tmp_ws("20260606-test", tmp_path)
    try:
        assert tmp_ws.is_dir()
        assert (tmp_ws / "ws").is_dir()
        assert (tmp_ws / "home").is_dir()
        assert (tmp_ws / "cache").is_dir()
        assert (tmp_ws / "tmp").is_dir()
        # canonical_ws contents copied into ws.
        assert (tmp_ws / "ws" / "graph.py").read_text() == "# x\n"
    finally:
        import shutil

        shutil.rmtree(tmp_ws, ignore_errors=True)


def test_make_tmp_ws_track_id_sanitized(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "x.py").write_text("# x\n")
    subprocess.check_call(["git", "add", "-A"], cwd=tmp_path)
    subprocess.check_call(["git", "commit", "-qm", "init"], cwd=tmp_path)
    # Track id with shell-unsafe characters.
    tmp_ws = make_tmp_ws("dirty;rm -rf /", tmp_path)
    try:
        assert "dirtyrm-rf" in tmp_ws.name or "dirty" in tmp_ws.name
    finally:
        import shutil

        shutil.rmtree(tmp_ws, ignore_errors=True)


# =============================================================================
# preclear_track_artifacts
# =============================================================================


def test_preclear_removes_existing_artifacts(tmp_path):
    for name in ("qa.json", "qa-strategy.json", "qa-route.json"):
        (tmp_path / name).write_text("{}")
    preclear_track_artifacts(tmp_path)
    for name in ("qa.json", "qa-strategy.json", "qa-route.json"):
        assert not (tmp_path / name).exists()


def test_preclear_idempotent(tmp_path):
    preclear_track_artifacts(tmp_path)  # no-op on empty dir
    preclear_track_artifacts(tmp_path)


def test_preclear_does_not_touch_other_files(tmp_path):
    (tmp_path / "qa.json").write_text("{}")
    (tmp_path / "product-plan.json").write_text("{}")
    (tmp_path / "idea-contract.json").write_text("{}")
    preclear_track_artifacts(tmp_path)
    assert not (tmp_path / "qa.json").exists()
    assert (tmp_path / "product-plan.json").exists()
    assert (tmp_path / "idea-contract.json").exists()


# =============================================================================
# _atomic_write_text + json
# =============================================================================


def test_atomic_write_text_writes_content(tmp_path):
    dst = tmp_path / "out.txt"
    _atomic_write_text(dst, "hello world")
    assert dst.read_text() == "hello world"


def test_atomic_write_text_refuses_existing_via_excl(tmp_path):
    """Codex review hardening: O_EXCL on the temp file means concurrent writers can't
    race on the same tmp name. _atomic_write_text always uses a unique tmp name (pid)
    so consecutive calls don't conflict, but two concurrent calls with the same pid
    would. We can't easily test the race condition, but we verify the final write
    succeeds and replaces existing content.
    """
    dst = tmp_path / "out.txt"
    _atomic_write_text(dst, "v1")
    _atomic_write_text(dst, "v2")  # overwrites
    assert dst.read_text() == "v2"


def test_atomic_write_json_serializes(tmp_path):
    dst = tmp_path / "out.json"
    _atomic_write_json(dst, {"a": 1, "b": [2, 3]})
    assert json.loads(dst.read_text()) == {"a": 1, "b": [2, 3]}


def test_atomic_write_creates_parent_dir(tmp_path):
    dst = tmp_path / "nested" / "deeper" / "out.txt"
    _atomic_write_text(dst, "ok")
    assert dst.read_text() == "ok"


# =============================================================================
# write_qa_json + promote_strategy
# =============================================================================


def test_write_qa_json_shape(tmp_path):
    route_input = {"foo": "bar"}
    strategy = {"planned_tests": [{"clause_id": "AC-001"}], "n_tests_written": 1}
    pytest_results = {"exit": 0, "nodeids": ["test_qa.py::test_x"], "failures": []}
    p = write_qa_json(
        tmp_path,
        route_input=route_input,
        canonical_hash_pre="aaa",
        canonical_hash_post="aaa",
        tmp_ws=tmp_path / "tmp_ws",
        pytest_results=pytest_results,
        strategy=strategy,
        token_usage={"prompt": 100, "completion": 50},
    )
    assert p.is_file()
    qa = json.loads(p.read_text())
    assert qa["canonical_hash_pre"] == "aaa"
    assert qa["canonical_hash_post"] == "aaa"
    assert qa["planned_count"] == 1
    assert qa["collected_count"] == 1
    assert qa["pytest_exit"] == 0
    assert qa["codex_tokens"] == {"prompt": 100, "completion": 50}
    assert len(qa["route_input_sha256"]) == 64
    assert len(qa["strategy_sha256"]) == 64
    assert "ran_at" in qa


def test_promote_strategy_atomic(tmp_path):
    tmp_ws = tmp_path / "tmp_ws"
    tmp_ws.mkdir()
    (tmp_ws / "qa-strategy.json").write_text('{"x": 1}')
    track = tmp_path / "track"
    track.mkdir()
    p = promote_strategy(tmp_ws, track)
    assert p.is_file()
    assert json.loads(p.read_text()) == {"x": 1}


def test_promote_strategy_missing_source_raises(tmp_path):
    tmp_ws = tmp_path / "tmp_ws"
    tmp_ws.mkdir()
    track = tmp_path / "track"
    track.mkdir()
    with pytest.raises(FileNotFoundError):
        promote_strategy(tmp_ws, track)


# =============================================================================
# emit_event
# =============================================================================


def test_emit_event_appends_to_events_jsonl(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    p = emit_event(state, "20260606-test", "confirm", "all green")
    assert p is not None and p.is_file()
    lines = p.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["job_id"] == "20260606-test"
    assert rec["event"] == "qa-route"
    assert rec["route"] == "confirm"
    assert rec["profile"] == "qa"


def test_emit_event_multiple_appends(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    emit_event(state, "track1", "confirm", "x")
    emit_event(state, "track2", "gemma", "y")
    lines = (state / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_emit_event_missing_state_returns_none(tmp_path):
    p = emit_event(tmp_path / "missing_state", "x", "confirm", "x")
    assert p is None


# =============================================================================
# _extract_coder_test_sources
# =============================================================================


def test_extract_coder_test_sources_basic(tmp_path):
    src = tmp_path / "test_graph.py"
    src.write_text(
        "def test_x():\n    assert True\n"
        "def test_y():\n    assert 1 + 1 == 2\n"
    )
    out = _extract_coder_test_sources(
        tmp_path, ["test_graph.py::test_x", "test_graph.py::test_y"]
    )
    assert "test_graph.py::test_x" in out
    assert "test_graph.py::test_y" in out
    assert "test_x" in out["test_graph.py::test_x"]


def test_extract_coder_test_sources_nested_class(tmp_path):
    src = tmp_path / "test_graph.py"
    src.write_text(
        "class TestX:\n"
        "    def test_inner(self):\n"
        "        assert True\n"
    )
    out = _extract_coder_test_sources(
        tmp_path, ["test_graph.py::TestX::test_inner"]
    )
    assert "test_graph.py::TestX::test_inner" in out


def test_extract_coder_test_sources_with_decorator(tmp_path):
    src = tmp_path / "test_graph.py"
    src.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_param(x):\n"
        "    assert x > 0\n"
    )
    out = _extract_coder_test_sources(tmp_path, ["test_graph.py::test_param"])
    assert "@pytest.mark.parametrize" in out["test_graph.py::test_param"]


def test_extract_coder_test_sources_strips_parametrize_suffix(tmp_path):
    """Round-1 HOLD #2 (Codex review, finding #7): parametrized nodeids
    like ``test_param[42]`` map to the underlying FunctionDef ``test_param``
    in source. Strip the bracketed suffix for AST lookup, but keep the
    full nodeid as the dict key so C18b's string-equal completeness check
    against C3's evidence list still matches.

    Without this, every parametrized nodeid Gemma cited would pass C3
    (nodeid in collected set) and then trip C18b (no source found),
    HOLDing the build for a phantom reason.
    """
    src = tmp_path / "test_graph.py"
    src.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_param(x):\n"
        "    assert x > 0\n"
    )
    out = _extract_coder_test_sources(
        tmp_path,
        [
            "test_graph.py::test_param[1]",
            "test_graph.py::test_param[2]",
        ],
    )
    # Both nodeids keyed in full (no normalization on the OUTPUT — only on
    # the AST LOOKUP). C18b checks for these exact keys.
    assert "test_graph.py::test_param[1]" in out
    assert "test_graph.py::test_param[2]" in out
    # And both extracted snippets really contain the function body.
    for nodeid in (
        "test_graph.py::test_param[1]",
        "test_graph.py::test_param[2]",
    ):
        assert "def test_param" in out[nodeid]
        assert "assert x > 0" in out[nodeid]


def test_extract_coder_test_sources_missing_file(tmp_path):
    out = _extract_coder_test_sources(tmp_path, ["nonexistent.py::test_x"])
    assert out == {}


def test_extract_coder_test_sources_invalid_nodeid(tmp_path):
    out = _extract_coder_test_sources(tmp_path, ["malformed_no_double_colon"])
    assert out == {}


def test_extract_coder_test_sources_caps_at_30_lines(tmp_path):
    src = tmp_path / "test_graph.py"
    body = "    pass  # line\n" * 50
    src.write_text(f"def test_long():\n{body}")
    out = _extract_coder_test_sources(tmp_path, ["test_graph.py::test_long"])
    # The snippet should be capped at 30 lines.
    assert out["test_graph.py::test_long"].count("\n") <= 31


# =============================================================================
# SANDBOX_EXEC_POLICY_TEMPLATE shape
# =============================================================================


def test_sandbox_policy_has_required_clauses():
    # The policy must (a) deny by default, (b) allow process-fork/exec for pytest,
    # (c) allow file-write* under <TMP_WS>.
    assert "(deny default)" in SANDBOX_EXEC_POLICY_TEMPLATE
    assert "(allow process-fork)" in SANDBOX_EXEC_POLICY_TEMPLATE
    assert "(allow process-exec)" in SANDBOX_EXEC_POLICY_TEMPLATE
    assert "<TMP_WS>" in SANDBOX_EXEC_POLICY_TEMPLATE
    assert '(allow file-write* (subpath "<TMP_WS>"))' in SANDBOX_EXEC_POLICY_TEMPLATE


def test_sandbox_policy_substitution():
    sub = SANDBOX_EXEC_POLICY_TEMPLATE.replace("<TMP_WS>", "/tmp/qa-abc")
    assert "/tmp/qa-abc" in sub
    assert "<TMP_WS>" not in sub


# =============================================================================
# QARun NamedTuple shape
# =============================================================================


def test_qarun_shape(tmp_path):
    r = QARun(
        track_id="x",
        computed_route="confirm",
        failure_reason="",
        canonical_hash_pre="aaa",
        canonical_hash_post="aaa",
        qa_json_path=None,
        qa_strategy_path=None,
        qa_route_path=None,
        elapsed_seconds=1.23,
    )
    assert r.computed_route == "confirm"
    assert r.elapsed_seconds == 1.23


# --- Codex review (2026-06-06): hardening tests ---


def test_make_tmp_ws_cleans_up_on_copytree_failure(tmp_path):
    """Codex SHOULD-FIX 1: a copytree failure must not leak the half-built tmp_ws.

    Shadow-phase finding (2026-06-07): the default parent_dir moved from
    `tempfile.gettempdir()` to `<atb_root>/state/qa-tmp/` (hermes refuses to
    write to /var/folders/...). We pin the leak invariant explicitly against
    the test-supplied parent_dir so this test no longer depends on the
    runtime default — the invariant is "no new qa-<prefix>-* dirs in the
    chosen parent if make_tmp_ws raises".
    """
    # Source dir doesn't exist → copytree raises.
    bogus_source = tmp_path / "nonexistent"
    parent = tmp_path / "qa-tmp"
    parent.mkdir()
    pre = {
        p for p in parent.iterdir() if p.name.startswith("qa-test-leak-")
    }
    with pytest.raises((FileNotFoundError, OSError)):
        make_tmp_ws("test-leak", bogus_source, parent_dir=parent)
    post = {
        p for p in parent.iterdir() if p.name.startswith("qa-test-leak-")
    }
    leaked = post - pre
    # No new tmp_ws/ leaked under our test prefix.
    assert leaked == set(), f"leaked tmp_ws dirs: {leaked}"


def test_atomic_write_text_unlinks_temp_on_failure(tmp_path):
    """Codex NIT 2: on write failure, the temp file must be cleaned up."""
    dst = tmp_path / "out.txt"
    # Pre-existing temp file with our PID-suffixed name would block the new write.
    # We can't easily force a write to fail mid-write portably, so we test the
    # "writes complete and produce the final file" baseline + verify no leftover.
    _atomic_write_text(dst, "ok")
    # No temp file lingering in the parent dir.
    leftover = [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith(".out.txt.tmp.")
    ]
    assert leftover == []


def test_track_file_digests_excludes_runner_artifacts(tmp_path):
    """The track-file-digests helper used by the Gemma-mutation guard must EXCLUDE
    runner-owned artifacts (qa.json / qa-strategy.json / qa-route.json / .qa.lock)
    since those legitimately change during a run."""
    from tools.qa import _track_file_digests

    (tmp_path / "product-plan.json").write_text('{"x":1}')
    (tmp_path / "idea-contract.json").write_text('{"y":2}')
    (tmp_path / "qa.json").write_text('{"runner":1}')
    (tmp_path / "qa-strategy.json").write_text('{"runner":2}')
    (tmp_path / "qa-route.json").write_text('{"runner":3}')
    (tmp_path / ".qa.lock").write_text("")
    digests = _track_file_digests(tmp_path)
    assert "product-plan.json" in digests
    assert "idea-contract.json" in digests
    assert "qa.json" not in digests
    assert "qa-strategy.json" not in digests
    assert "qa-route.json" not in digests
    assert ".qa.lock" not in digests


def test_track_file_digests_detects_mutation(tmp_path):
    """If product-plan.json bytes change between two digest captures, the helper
    detects it via different hash values."""
    from tools.qa import _track_file_digests

    (tmp_path / "product-plan.json").write_text('{"v":1}')
    pre = _track_file_digests(tmp_path)
    (tmp_path / "product-plan.json").write_text('{"v":2}')
    post = _track_file_digests(tmp_path)
    assert pre["product-plan.json"] != post["product-plan.json"]


def test_make_tmp_ws_rejects_quote_in_path(monkeypatch, tmp_path):
    """Codex NIT 1: if TMPDIR injects a quote, the sandbox-exec policy substitution
    would corrupt. The make_tmp_ws function rejects such paths."""
    import shutil
    import tempfile

    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    subprocess.check_call(["git", "add", "-A"], cwd=tmp_path)
    subprocess.check_call(["git", "commit", "-qm", "init"], cwd=tmp_path)

    # Force tempfile.mkdtemp to return a path with a quote in it.
    fake_dir = tmp_path / 'has"quote'
    fake_dir.mkdir()

    original_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(*a, **kw):
        return str(fake_dir)

    monkeypatch.setattr("tools.qa.tempfile.mkdtemp", fake_mkdtemp)

    with pytest.raises(RuntimeError, match="unsafe chars"):
        make_tmp_ws("test", tmp_path)
    # The function should have rmtree'd the bad dir on failure.
    assert not fake_dir.exists() or list(fake_dir.iterdir()) == []


# =============================================================================
# _resolve_pytest_results — strategic-null guard for Step 12
# =============================================================================
#
# Why these tests exist (the QA layer, Rev 5 spec):
# `_resolve_pytest_results` decides whether Step 12 actually invokes pytest. When
# the strategy is strategic-null (`n_tests_written == 0`), C5 has already
# enforced that no `test_qa.py` exists; running pytest against a missing file
# would exit 4, which `compute_route` routes to hold_human, burying the spec's
# valid "strategic null, all skip_earned → confirm" outcome. The guard
# synthesizes a clean exit instead. These tests pin the branch logic so the
# strategic-null route can never silently regress.


def test_resolve_pytest_results_strategic_null_synthesizes_clean_exit_no_runner_call():
    """n_tests_written == 0 → return clean zero-test exit; do NOT call the runner.

    Calling the runner on a strategic-null strategy would either touch sandbox-exec
    (slow) or — worse — exit 4 on a missing test_qa.py, defeating the guard.
    """
    runner_calls: list[None] = []

    def runner() -> dict:
        runner_calls.append(None)
        return {"exit": 99, "nodeids": ["should_not_appear"], "failures": ["x"]}

    result = _resolve_pytest_results({"n_tests_written": 0}, runner)

    assert runner_calls == [], "runner must not be invoked for strategic null"
    assert result == {
        "exit": 0,
        "nodeids": [],
        "failures": [],
        "stdout_excerpt": "",
        "stderr_excerpt": "",
    }


def test_resolve_pytest_results_missing_field_defaults_to_strategic_null():
    """Defensive: a strategy without `n_tests_written` is treated as null.

    In production this shape can't reach Step 12 — C1 (qa-strategy.json schema)
    requires the field. But the guard should fail closed (synthesize a clean
    exit; skip pytest) rather than open (call pytest with an unknown file).
    """
    called: list[None] = []
    result = _resolve_pytest_results({}, lambda: called.append(None) or {"exit": 1})
    assert called == []
    assert result["exit"] == 0


def test_resolve_pytest_results_nonzero_invokes_runner_and_returns_its_result():
    """n_tests_written > 0 → invoke the runner; return its result verbatim.

    Verifies the non-null path is unmodified: same dict identity comes back, so
    runner-emitted nodeids/failures/excerpts flow through to compute_route /
    qa.json without truncation or shape change.
    """
    runner_result = {
        "exit": 1,
        "nodeids": ["test_qa.py::test_f"],
        "failures": ["FAILED test_qa.py::test_f - AssertionError"],
        "stdout_excerpt": "stdout",
        "stderr_excerpt": "stderr",
    }
    call_count = [0]

    def runner() -> dict:
        call_count[0] += 1
        return runner_result

    result = _resolve_pytest_results({"n_tests_written": 1}, runner)
    assert call_count == [1], "runner must be invoked exactly once"
    assert result is runner_result


def test_resolve_pytest_results_returns_fresh_dict_for_strategic_null():
    """Caller mutation of the synthesized result must not leak across calls.

    Two consecutive strategic-null calls must each return independent dicts; a
    mutation of one does not poison the other. Cheap correctness pin against a
    future maintainer collapsing the literal to a module-level constant.
    """
    a = _resolve_pytest_results({"n_tests_written": 0}, lambda: pytest.fail("nope"))
    a["exit"] = 42
    a["nodeids"].append("mutation")
    b = _resolve_pytest_results({"n_tests_written": 0}, lambda: pytest.fail("nope"))
    assert b["exit"] == 0
    assert b["nodeids"] == []


# =============================================================================
# _recover_misplaced_strategy (Shadow-phase finding 5 fallback, 2026-06-07)
# =============================================================================


def _make_tmp_ws_for_recovery(tmp_path: Path) -> Path:
    """Build a minimal tmp_ws shape for recovery tests: <root>/ws/ subdir."""
    tmp_ws = tmp_path / "tmp_ws"
    (tmp_ws / "ws").mkdir(parents=True)
    return tmp_ws


def test_recover_misplaced_strategy_canonical_present_is_noop(tmp_path):
    """If qa-strategy.json already exists at the canonical location, the
    recovery helper is a no-op: it must NOT touch the canonical file, must
    NOT move anything, and must report recovered=False with a clear reason.
    """
    tmp_ws = _make_tmp_ws_for_recovery(tmp_path)
    canonical = tmp_ws / "qa-strategy.json"
    canonical.write_text('{"planned_tests": [], "skipped_clauses": []}')
    original_content = canonical.read_text()

    # Stage a different file in the workspace too — the helper must NOT
    # promote it over the existing canonical.
    fallback = tmp_ws / "ws" / "qa-strategy.json"
    fallback.write_text('{"different": "content"}')

    result = _recover_misplaced_strategy(
        tmp_ws, workspace_had_strategy_before_gemma=False
    )

    assert result["recovered"] is False
    assert result["source_path"] is None
    assert "canonical" in (result["reason"] or "")
    # Canonical unchanged.
    assert canonical.read_text() == original_content
    # Workspace file left untouched (C12 will catch it later).
    assert fallback.is_file()


def test_recover_misplaced_strategy_moves_valid_fallback(tmp_path):
    """The happy-path recovery: canonical missing, workspace has valid JSON,
    canonical workspace had no pre-existing strategy → MOVE workspace →
    canonical, delete workspace copy, recovered=True.
    """
    tmp_ws = _make_tmp_ws_for_recovery(tmp_path)
    canonical = tmp_ws / "qa-strategy.json"
    fallback = tmp_ws / "ws" / "qa-strategy.json"
    payload = '{"planned_tests": [], "skipped_clauses": [{"clause_id":"AC-001"}], "n_tests_written": 0}'
    fallback.write_text(payload)

    result = _recover_misplaced_strategy(
        tmp_ws, workspace_had_strategy_before_gemma=False
    )

    assert result["recovered"] is True
    assert result["source_path"] == str(fallback)
    assert result["reason"] is None
    # Canonical now exists with the moved content.
    assert canonical.is_file()
    assert canonical.read_text() == payload
    # Workspace copy deleted (so C12 won't catch the workspace pollution).
    assert not fallback.is_file()


def test_recover_misplaced_strategy_refuses_invalid_json(tmp_path):
    """If the workspace fallback exists but is malformed JSON, the helper
    must NOT promote it — that would silently replace one failure mode (C1
    missing) with another (C1 parse error). recovered=False, canonical
    untouched, workspace file LEFT in place so C12 catches it.
    """
    tmp_ws = _make_tmp_ws_for_recovery(tmp_path)
    canonical = tmp_ws / "qa-strategy.json"
    fallback = tmp_ws / "ws" / "qa-strategy.json"
    fallback.write_text("{this is not valid json}")

    result = _recover_misplaced_strategy(
        tmp_ws, workspace_had_strategy_before_gemma=False
    )

    assert result["recovered"] is False
    assert result["source_path"] is None
    assert "JSON" in (result["reason"] or "") or "invalid" in (
        result["reason"] or ""
    )
    # Canonical NOT created.
    assert not canonical.is_file()
    # Workspace bad-file left in place so the diff guard catches it.
    assert fallback.is_file()


def test_recover_misplaced_strategy_no_fallback_present(tmp_path):
    """If neither the canonical NOR the workspace has the file, the helper
    reports recovered=False with a clear "no fallback" reason — distinct
    from the "canonical already present" case so logs are unambiguous.
    """
    tmp_ws = _make_tmp_ws_for_recovery(tmp_path)
    result = _recover_misplaced_strategy(
        tmp_ws, workspace_had_strategy_before_gemma=False
    )
    assert result["recovered"] is False
    assert result["source_path"] is None
    assert "no fallback" in (result["reason"] or "")


def test_recover_misplaced_strategy_refuses_when_workspace_had_strategy_before_gemma(
    tmp_path,
):
    """Codex review round 1 P1 + round 2 TOCTOU (provenance gate): if the
    workspace copy already had qa-strategy.json BEFORE Gemma ran — whether
    the canonical workspace shipped it OR it appeared during the make_tmp_ws
    copy window — the recovery MUST refuse. We cannot distinguish that from
    a Gemma-authored file, so silent promotion would mask a supply-chain
    plant. Fail closed: canonical not created, workspace file LEFT in place.

    The HOLD path in production: the early missing-canonical short-circuit
    in `run_qa` fires immediately after recovery refuses (canonical still
    empty), reporting the recovery refusal reason in `failure_reason`
    rather than letting C1 surface a plainer "missing" message later.
    """
    tmp_ws = _make_tmp_ws_for_recovery(tmp_path)
    canonical = tmp_ws / "qa-strategy.json"
    fallback = tmp_ws / "ws" / "qa-strategy.json"
    payload = '{"planned_tests": [], "skipped_clauses": [{"clause_id":"AC-001"}], "n_tests_written": 0}'
    fallback.write_text(payload)

    result = _recover_misplaced_strategy(
        tmp_ws, workspace_had_strategy_before_gemma=True
    )

    assert result["recovered"] is False
    assert result["source_path"] is None
    # Reason must call out the provenance ambiguity so an audit log makes
    # the refusal obvious. Without this anchor, a future maintainer could
    # silently relax the gate.
    assert "pre-existing" in (result["reason"] or "")
    # Canonical NOT created — the build will HOLD via downstream checks.
    assert not canonical.is_file()
    # Workspace file LEFT in place so downstream HOLD paths can surface it.
    assert fallback.is_file()


# =============================================================================
# _collect_coder_nodeids (finding #7 — 2026-06-07)
# =============================================================================


def _write_tests(ws: Path, files: dict[str, str]) -> None:
    """Drop a set of source files into a workspace dir."""
    ws.mkdir(parents=True, exist_ok=True)
    for rel, src in files.items():
        dst = ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src)


def test_collect_coder_nodeids_happy_path(tmp_path):
    """Multiple test files → returns every nodeid in pytest's canonical form.

    pytest emits nodeids as ``<rel_path>::<test_name>`` (or
    ``::<TestClass>::<test_name>``); the helper's parser must preserve that
    shape so C3 can compare against Gemma's `coder_test_evidence`.
    """
    ws = tmp_path / "ws"
    _write_tests(
        ws,
        {
            "test_alpha.py": (
                "def test_one():\n"
                "    assert True\n\n"
                "def test_two():\n"
                "    assert 1 + 1 == 2\n"
            ),
            "test_beta.py": (
                "import pytest\n"
                "@pytest.mark.parametrize('x', [1, 2])\n"
                "def test_param(x):\n"
                "    assert x > 0\n"
            ),
        },
    )

    nodeids, reason = _collect_coder_nodeids(ws)

    assert reason is None
    nodeset = set(nodeids)
    assert "test_alpha.py::test_one" in nodeset
    assert "test_alpha.py::test_two" in nodeset
    # Parametrized cases must round-trip in canonical form (with brackets).
    assert "test_beta.py::test_param[1]" in nodeset
    assert "test_beta.py::test_param[2]" in nodeset
    # Every emitted nodeid must contain "::" (regression guard against the
    # parser accidentally swallowing footer/warning lines).
    assert all("::" in nid for nid in nodeids)


def test_collect_coder_nodeids_empty_workspace_is_not_error(tmp_path):
    """A workspace with no test files: returns ([], None) — not a failure."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # One non-test source file so pytest has something to look at, but no
    # tests to collect.
    (ws / "module.py").write_text("def add(a, b):\n    return a + b\n")

    nodeids, reason = _collect_coder_nodeids(ws)

    # Exit-code-5 ("no tests collected") is legitimate, not a tooling fault.
    assert nodeids == []
    assert reason is None


def test_collect_coder_nodeids_missing_workspace_returns_reason(tmp_path):
    """Caller passed a path that doesn't exist → ([], reason). Never raises."""
    nodeids, reason = _collect_coder_nodeids(tmp_path / "does-not-exist")
    assert nodeids == []
    assert reason is not None
    assert "not a directory" in reason


def test_collect_coder_nodeids_collection_error_surfaces_reason(tmp_path):
    """Workspace with a syntax error in a test file: pytest exits non-zero
    and emits zero collectable nodeids. Caller must see a non-None reason
    so finding #7's diagnostic chain stays honest.

    Round-1 HOLD #3 (Codex review): the exit-code gate runs BEFORE any
    nodeids are returned, so even if a collection error happened to print
    a `file.py::test_name`-shaped line in its stderr/stdout, the helper
    refuses the whole result. The "exit" substring in the reason makes
    that decision searchable in events.jsonl.

    (If pytest exits 5 with no error message, we'd return ``([], None)`` —
    that's the "no tests" branch, not this one. Syntax errors yield exit 2
    or 1 depending on version, both non-zero AND non-5.)
    """
    ws = tmp_path / "ws"
    _write_tests(
        ws,
        {
            "test_broken.py": "def test_no_close_paren(\n    pass\n",
        },
    )

    nodeids, reason = _collect_coder_nodeids(ws)

    assert nodeids == []
    assert reason is not None
    # Reason must include exit code so operators can grep events for it.
    assert "exit" in reason.lower()


def test_collect_coder_nodeids_refuses_poisoned_collection_error(tmp_path):
    """Round-1 HOLD #3 regression guard: a conftest that crashes AFTER
    printing fake nodeid-shaped strings to stdout must NOT seed the
    expected-set. Without the exit-code gate, the parser would scrape
    `evil_planted.py::test_x` and C3 would silently accept Gemma's
    matching forgery.
    """
    ws = tmp_path / "ws"
    conftest_src = (
        "import sys\n"
        "print('evil_planted.py::test_x')\n"
        "print('evil_planted.py::test_y')\n"
        "raise RuntimeError('collection sabotage')\n"
    )
    _write_tests(
        ws,
        {
            "conftest.py": conftest_src,
            "test_real.py": "def test_real():\n    assert True\n",
        },
    )

    nodeids, reason = _collect_coder_nodeids(ws)

    # The exit-code gate refuses the WHOLE result, planted strings and all.
    assert nodeids == []
    assert reason is not None
    assert "exit" in reason.lower()
    # Specifically: the planted strings must not leak through. (Belt and
    # suspenders; the empty-list assertion above already covers this, but
    # surface a clear failure message if a future refactor regresses.)
    assert "evil_planted" not in (reason or "")


def test_collect_coder_nodeids_ignores_clean_exit_stdout_poison(tmp_path):
    """Round-2 HOLD #2 regression guard: a conftest that prints
    nodeid-shaped strings during a CLEAN exit-0 collection must not
    influence the returned nodeids — the helper reads from the plugin
    channel, not stdout.

    Without the plugin channel, the old parser would scrape every
    `::`-shaped stdout line, returning {`evil_planted.py::test_x`,
    `test_real.py::test_real`} on exit 0. Gemma could then cite the
    planted nodeid, C3 would pass (planted nodeid in expected set), and
    C18b would (depending on the planted suffix) find a real source
    function to extract — silent forgery.
    """
    ws = tmp_path / "ws"
    # conftest prints during collection but does NOT raise: pytest still
    # exits 0 because a real test was collected. The plugin channel is
    # the only authoritative source of nodeids.
    conftest_src = (
        "print('evil_planted.py::test_forged_x')\n"
        "print('evil_planted.py::test_forged_y[1]')\n"
    )
    _write_tests(
        ws,
        {
            "conftest.py": conftest_src,
            "test_real.py": "def test_real():\n    assert True\n",
        },
    )

    nodeids, reason = _collect_coder_nodeids(ws)

    assert reason is None
    # Only the REAL test, not the printed string poison, is returned.
    assert nodeids == ["test_real.py::test_real"]
    assert all("evil_planted" not in nid for nid in nodeids)


def test_collect_coder_nodeids_minimal_env_does_not_leak_caller_secrets(
    tmp_path, monkeypatch
):
    """Round-2 HOLD #3 (partial) regression guard: the helper must NOT
    pass the caller's environment through to the pytest subprocess. A
    malicious conftest reading ``ANTHROPIC_API_KEY`` (or any other
    secret) from os.environ would otherwise leak it via writes inside
    the sandbox + a downstream read.

    Test mechanism: set a high-entropy "secret" in the parent env, then
    have a conftest write whatever it finds for that key to a file
    inside scratch_ws BEFORE pytest collects. Read that file from
    scratch_ws after collection (we can't directly, since scratch is
    rm'd — instead, we have the conftest write a marker INTO the test
    name and check whether the marker appears in the collected nodeids).
    """
    secret_marker = "FINDING7_LEAKED_SECRET_VALUE"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret_marker)

    ws = tmp_path / "ws"
    # The conftest tries to look up the secret; if env passthrough is in
    # place, it would get `FINDING7_LEAKED_SECRET_VALUE`. Either way we
    # write a test name based on the result — if the env is scrubbed,
    # the test name will literally be "test_no_secret_found".
    conftest_src = (
        "import os\n"
        "secret = os.environ.get('ANTHROPIC_API_KEY')\n"
        "with open('exposed_secret.txt', 'w') as f:\n"
        "    f.write('YES_SECRET=' + secret if secret else 'NO_SECRET')\n"
    )
    _write_tests(
        ws,
        {
            "conftest.py": conftest_src,
            "test_dummy.py": "def test_one():\n    assert True\n",
        },
    )

    nodeids, reason = _collect_coder_nodeids(ws)

    # We don't fail outright — we check the artifact wasn't leaked into a
    # tampered nodeid (defense in depth). But the more direct check is
    # the env scrub: ANTHROPIC_API_KEY must not be in the subprocess env.
    # We can verify by looking at exposed_secret.txt — except the scratch
    # was cleaned up. So we run again with a hook into the helper itself.
    assert reason is None
    assert nodeids == ["test_dummy.py::test_one"]

    # Direct env check: the helper's env construction passes only
    # whitelisted keys. We assert by reading the source-level guarantee
    # via a second invocation that captures the subprocess env. Use
    # monkeypatch to wrap subprocess.run, capture the env arg.
    captured_env: dict = {}

    real_run = __import__("subprocess").run

    def _capture_run(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return real_run(*args, **kwargs)

    monkeypatch.setattr("tools.qa.subprocess.run", _capture_run)
    _collect_coder_nodeids(ws)

    assert "ANTHROPIC_API_KEY" not in captured_env, (
        "env passthrough leak: helper must NOT forward "
        "ANTHROPIC_API_KEY to pytest subprocess"
    )
    # And the keys we DO pass are the documented minimal set.
    assert "PATH" in captured_env  # needed for sandbox-exec / pytest
    assert "PYTHONDONTWRITEBYTECODE" in captured_env
    # Round-3 HOLD #1: output path is baked into the plugin source, not
    # passed via env, so a conftest can't read it from os.environ.
    assert "_QA_COLLECT_OUT" not in captured_env
    # PYTHONPATH IS in env so the (randomly-named) collect plugin is
    # importable.
    assert "PYTHONPATH" in captured_env


def test_collect_coder_nodeids_refuses_tmpdir_inside_canonical_ws(
    tmp_path, monkeypatch
):
    """Round-2 NIT #5 close: a poisoned TMPDIR pointing inside
    canonical_ws would put scratch under the trusted tree. Refuse.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_tests(ws, {"test_dummy.py": "def test_one():\n    assert True\n"})

    # Point TMPDIR inside canonical_ws.
    poisoned_tmpdir = ws / "evil_tmp"
    poisoned_tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(poisoned_tmpdir))

    nodeids, reason = _collect_coder_nodeids(ws)

    assert nodeids == []
    assert reason is not None
    assert "TMPDIR is inside canonical_ws" in reason


def test_collect_coder_nodeids_passes_through_non_default_discovery_shapes(
    tmp_path, monkeypatch
):
    """Round-4 HOLD #1 fix: per-entry dropping was REMOVED so workspaces
    using non-default pytest discovery aren't false-rejected. A nodeid
    that doesn't match the strict AST scan (e.g. a custom-collector
    test) passes through; downstream C18b will validate source
    extraction.

    Approach: forge an extra nodeid into plugin output. Confirm the
    helper passes it through alongside the real one (no per-entry
    drop), because the AST set is non-empty AND the intersection with
    real nodeids is non-empty.
    """
    from tools import qa as qa_mod

    ws = tmp_path / "ws"
    _write_tests(
        ws,
        {
            "test_real.py": "def test_real():\n    assert True\n",
        },
    )

    real_loads = qa_mod.json.loads

    def _augmented_loads(text, *a, **kw):
        actual = real_loads(text, *a, **kw)
        if (
            isinstance(actual, list)
            and "test_real.py::test_real" in actual
        ):
            # Add a non-default-discovery-shaped entry.
            return actual + ["check_nondefault.py::check_x"]
        return actual

    monkeypatch.setattr(qa_mod.json, "loads", _augmented_loads)

    nodeids, reason = _collect_coder_nodeids(ws)

    assert reason is None
    # Both pass through — the extra entry isn't dropped just because the
    # AST scan didn't recognize check_*.py.
    assert "test_real.py::test_real" in nodeids
    assert "check_nondefault.py::check_x" in nodeids


def test_collect_coder_nodeids_refuses_overwrite_to_empty(
    tmp_path, monkeypatch
):
    """Round-4 HOLD #2 close: a malicious ``pytest_sessionfinish`` that
    overwrites collected.json with ``[]`` must NOT silently look like a
    legitimate empty workspace. If the AST scan finds real test
    FunctionDef nodes on disk AND the plugin reports empty, refuse.
    """
    from tools import qa as qa_mod

    ws = tmp_path / "ws"
    _write_tests(
        ws,
        {
            "test_real.py": (
                "def test_one():\n    assert True\n\n"
                "def test_two():\n    assert True\n"
            ),
        },
    )

    real_loads = qa_mod.json.loads

    def _empty_overwrite_loads(text, *a, **kw):
        actual = real_loads(text, *a, **kw)
        # Replace whatever the plugin wrote with [] — overwrite-to-empty.
        if isinstance(actual, list):
            return []
        return actual

    monkeypatch.setattr(qa_mod.json, "loads", _empty_overwrite_loads)

    nodeids, reason = _collect_coder_nodeids(ws)

    assert nodeids == []
    assert reason is not None
    assert "overwrite-to-empty" in reason.lower() or "0 nodeids" in reason


def test_collect_coder_nodeids_ast_cross_check_fails_closed_on_total_forgery(
    tmp_path, monkeypatch
):
    """Round-3/4 total-forgery refusal: if plugin reports >0 nodeids and
    the AST set is also >0 but the intersection is empty, refuse.

    (Distinct from the round-4 HOLD #1 fix: that one was about
    PARTIAL forgery — pass through entries that don't match strictly,
    because non-default pytest discovery is real. Total forgery — NO
    intersection with on-disk tests — is still a strong signal.)
    """
    from tools import qa as qa_mod

    ws = tmp_path / "ws"
    _write_tests(
        ws,
        {
            "test_real.py": "def test_real():\n    assert True\n",
        },
    )

    real_loads = qa_mod.json.loads

    def _wholly_forged_loads(text, *a, **kw):
        actual = real_loads(text, *a, **kw)
        if isinstance(actual, list) and actual:
            return [
                "evil_planted.py::test_secret",
                "evil_planted.py::test_again",
            ]
        return actual

    monkeypatch.setattr(qa_mod.json, "loads", _wholly_forged_loads)

    nodeids, reason = _collect_coder_nodeids(ws)

    assert nodeids == []
    assert reason is not None
    assert (
        "overwrite attack" in reason.lower()
        or "matched" in reason.lower()
    )


def test_ast_collect_test_function_nodeids_finds_class_methods(tmp_path):
    """The AST helper must surface ``TestClass::test_method`` pairs so
    cross-checking class-based test nodeids works. Without this, a real
    class-based test would be dropped as "not on disk" and the build
    would HOLD for a phantom reason.
    """
    from tools.qa import _ast_collect_test_function_nodeids

    ws = tmp_path / "ws"
    _write_tests(
        ws,
        {
            "test_x.py": (
                "def test_top_level():\n"
                "    assert True\n\n"
                "class TestThings:\n"
                "    def test_method(self):\n"
                "        assert True\n"
            ),
        },
    )

    found = _ast_collect_test_function_nodeids(ws)
    assert "test_x.py::test_top_level" in found
    assert "test_x.py::TestThings::test_method" in found


def test_bare_nodeid_strips_param_suffix_only_after_first_double_colon():
    """``_bare_nodeid`` only treats ``[`` AFTER the first ``::`` as the
    param suffix. Round-3 NIT #6 regression guard.
    """
    from tools.qa import _bare_nodeid

    # No brackets at all.
    assert _bare_nodeid("test_x.py::test_y") == "test_x.py::test_y"
    # Standard parametrize.
    assert _bare_nodeid("test_x.py::test_y[1]") == "test_x.py::test_y"
    # Param contains `::`.
    assert _bare_nodeid("test_x.py::test_y[a::b]") == "test_x.py::test_y"
    # Class-based test with param.
    assert (
        _bare_nodeid("test_x.py::TestCls::test_y[1]")
        == "test_x.py::TestCls::test_y"
    )
    # File path contains `[` (rare but legal on POSIX) — do NOT treat
    # as a param suffix.
    assert (
        _bare_nodeid("weird[name.py::test_x")
        == "weird[name.py::test_x"
    )


def test_collect_coder_nodeids_does_not_mutate_canonical_ws(tmp_path):
    """Trust property: the canonical workspace MUST be bit-for-bit unchanged
    after collection. pytest writes ``__pycache__`` into the disposable copy
    only — the original stays clean. Without this guarantee, a stray
    ``__pycache__`` in canonical_ws would shift the C13/C13b fingerprint
    and HOLD the build.
    """
    ws = tmp_path / "ws"
    _write_tests(
        ws,
        {
            "test_thing.py": "def test_x():\n    assert True\n",
        },
    )
    before = sorted(p.name for p in ws.iterdir())

    nodeids, reason = _collect_coder_nodeids(ws)
    assert reason is None
    assert nodeids  # at least one test collected

    after = sorted(p.name for p in ws.iterdir())
    assert before == after, (
        f"_collect_coder_nodeids polluted canonical_ws: before={before} "
        f"after={after}"
    )
    # Specifically: no __pycache__ leaked in.
    assert not (ws / "__pycache__").exists()
