"""Tests for tools.qa_check_post — the 4 post-pytest QA checks (C13, C13b, C16, C18b).

Strategy: pure-function checks tested with crafted fixtures; subprocess paths
(`git status`, `pytest --collect-only`) tested via tmp_path git inits + real pytest runs.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.qa_check_post import (
    c13_canonical_unchanged,
    c13b_no_new_ignored_residue,
    c16_pytest_collected_n_tests,
    c18b_route_input_has_all_source,
)


# =============================================================================
# Helpers
# =============================================================================


def _init_clean_repo(path: Path) -> None:
    """Init a git repo with one tracked file, no uncommitted changes."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        ["git", "init", "--initial-branch=main", "-q"], cwd=path
    )
    subprocess.check_call(
        ["git", "config", "user.email", "test@test.invalid"], cwd=path
    )
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=path)
    (path / "graph.py").write_text("# code\n")
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.pyc\n")
    subprocess.check_call(["git", "add", "-A"], cwd=path)
    subprocess.check_call(
        ["git", "commit", "-qm", "init"], cwd=path
    )


# =============================================================================
# C13 — canonical unchanged
# =============================================================================


def test_c13_hashes_match_and_status_clean_ok(tmp_path):
    _init_clean_repo(tmp_path)
    r = c13_canonical_unchanged(
        canonical_hash_pre="deadbeef",
        canonical_hash_post="deadbeef",
        canonical_ws=tmp_path,
    )
    assert r.ok


def test_c13_hash_mismatch_held(tmp_path):
    _init_clean_repo(tmp_path)
    r = c13_canonical_unchanged(
        canonical_hash_pre="abc",
        canonical_hash_post="xyz",
        canonical_ws=tmp_path,
    )
    assert not r.ok and "hash changed" in r.reason


def test_c13_untracked_file_held(tmp_path):
    _init_clean_repo(tmp_path)
    (tmp_path / "stranger.py").write_text("# uncommitted\n")
    r = c13_canonical_unchanged(
        canonical_hash_pre="x",
        canonical_hash_post="x",
        canonical_ws=tmp_path,
    )
    assert not r.ok and "git status" in r.reason


def test_c13_modified_tracked_file_held(tmp_path):
    _init_clean_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# MUTATED\n")
    r = c13_canonical_unchanged(
        canonical_hash_pre="x",
        canonical_hash_post="x",
        canonical_ws=tmp_path,
    )
    assert not r.ok and "git status" in r.reason


def test_c13_canonical_not_dir_held(tmp_path):
    r = c13_canonical_unchanged(
        canonical_hash_pre="x",
        canonical_hash_post="x",
        canonical_ws=tmp_path / "missing",
    )
    assert not r.ok and "not a directory" in r.reason


# =============================================================================
# C13b — no new ignored residue
# =============================================================================


def test_c13b_no_residue_change_ok(tmp_path):
    _init_clean_repo(tmp_path)
    pre = set()
    r = c13b_no_new_ignored_residue(pre, tmp_path)
    assert r.ok


def test_c13b_new_pycache_appears_held(tmp_path):
    _init_clean_repo(tmp_path)
    pre = set()  # no ignored residue before
    # Simulate pytest leaving __pycache__ behind.
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "graph.cpython-313.pyc").write_text("bytecode")
    r = c13b_no_new_ignored_residue(pre, tmp_path)
    assert not r.ok and "ignored residue" in r.reason


def test_c13b_residue_present_pre_and_post_same_ok(tmp_path):
    """A residue file that was already there before is not 'new' — only deltas hold."""
    _init_clean_repo(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "graph.cpython-313.pyc").write_text("bytecode")
    pre = {"__pycache__/graph.cpython-313.pyc"}
    r = c13b_no_new_ignored_residue(pre, tmp_path)
    assert r.ok


def test_c13b_missing_dir_held(tmp_path):
    r = c13b_no_new_ignored_residue(set(), tmp_path / "missing")
    assert not r.ok and "not a directory" in r.reason


# =============================================================================
# C16 — pytest collected n_tests_written
# =============================================================================


def test_c16_n_zero_no_file_ok(tmp_path):
    """n_tests_written=0 means no test_qa.py to collect; trivially ok."""
    assert c16_pytest_collected_n_tests({"n_tests_written": 0}, tmp_path).ok


def test_c16_collected_matches_n(tmp_path):
    """Integration: write a real test_qa.py with 2 test funcs, n=2 → ok."""
    p = tmp_path / "test_qa.py"
    p.write_text(
        "def test_a():\n    assert True\n"
        "def test_b():\n    assert True\n"
    )
    r = c16_pytest_collected_n_tests({"n_tests_written": 2}, tmp_path)
    assert r.ok


def test_c16_parametrize_expands_held(tmp_path):
    """A parametrized test expands to multiple nodeids — collected > n_tests_written."""
    p = tmp_path / "test_qa.py"
    p.write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('x', [1, 2, 3])\n"
        "def test_a(x):\n    assert x > 0\n"
    )
    r = c16_pytest_collected_n_tests({"n_tests_written": 1}, tmp_path)
    assert not r.ok and "pytest collected 3" in r.reason


def test_c16_no_file_with_nonzero_n_held(tmp_path):
    """n=2 but no file → pytest collects 0 → fail."""
    r = c16_pytest_collected_n_tests({"n_tests_written": 2}, tmp_path)
    assert not r.ok


# =============================================================================
# C18b — route_input source-availability invariant
# =============================================================================


def _strategy_with_evidence() -> dict:
    return {
        "n_tests_written": 1,
        "planned_tests": [
            {
                "clause_id": "AC-002",
                "asserted_in_test": "test_non_list_deps_raises",
                "coder_test_evidence": ["test_graph.py::test_x"],
            }
        ],
        "skipped_clauses": [
            {
                "clause_id": "AC-001",
                "coder_test_evidence": ["test_graph.py::test_basic_shape"],
            }
        ],
    }


def _complete_route_input() -> dict:
    return {
        "asserted_in_test_sources": {
            "test_non_list_deps_raises": "def test_non_list_deps_raises():\n    assert ..."
        },
        "coder_test_sources": {
            "test_graph.py::test_x": "def test_x():\n    ...",
            "test_graph.py::test_basic_shape": "def test_basic_shape():\n    ...",
        },
    }


def test_c18b_complete_route_input_ok():
    assert c18b_route_input_has_all_source(
        _strategy_with_evidence(), _complete_route_input()
    ).ok


def test_c18b_missing_asserted_in_test_source_held():
    ri = _complete_route_input()
    del ri["asserted_in_test_sources"]["test_non_list_deps_raises"]
    r = c18b_route_input_has_all_source(_strategy_with_evidence(), ri)
    assert not r.ok and "asserted_in_test" in r.reason


def test_c18b_empty_asserted_source_held():
    ri = _complete_route_input()
    ri["asserted_in_test_sources"]["test_non_list_deps_raises"] = ""
    r = c18b_route_input_has_all_source(_strategy_with_evidence(), ri)
    assert not r.ok and "asserted_in_test" in r.reason


def test_c18b_missing_coder_test_source_held():
    ri = _complete_route_input()
    del ri["coder_test_sources"]["test_graph.py::test_x"]
    r = c18b_route_input_has_all_source(_strategy_with_evidence(), ri)
    assert not r.ok and "coder_test_evidence" in r.reason


def test_c18b_route_input_not_dict_held():
    r = c18b_route_input_has_all_source(_strategy_with_evidence(), "not a dict")
    assert not r.ok


def test_c18b_missing_required_keys_held():
    r = c18b_route_input_has_all_source(_strategy_with_evidence(), {})
    assert not r.ok and (
        "asserted_in_test_sources" in r.reason
        or "coder_test_sources" in r.reason
    )


def test_c18b_empty_strategy_complete_route_input_ok():
    s = {"planned_tests": [], "skipped_clauses": [], "n_tests_written": 0}
    ri = {"asserted_in_test_sources": {}, "coder_test_sources": {}}
    assert c18b_route_input_has_all_source(s, ri).ok


def test_c18b_planned_test_without_asserted_in_test_ignored():
    """A planned_test missing asserted_in_test (schema-invalid but defensive) doesn't
    inflate the missing list — schema validation in C1 is the right layer for that."""
    s = {
        "n_tests_written": 1,
        "planned_tests": [{"clause_id": "AC-001"}],  # no asserted_in_test
        "skipped_clauses": [],
    }
    ri = {"asserted_in_test_sources": {}, "coder_test_sources": {}}
    assert c18b_route_input_has_all_source(s, ri).ok


def test_c18b_empty_coder_source_held():
    """Codex review (2026-06-06): lock in empty/whitespace coder source as a fail."""
    ri = _complete_route_input()
    ri["coder_test_sources"]["test_graph.py::test_x"] = "   \n  "  # whitespace only
    r = c18b_route_input_has_all_source(_strategy_with_evidence(), ri)
    assert not r.ok and "coder_test_evidence" in r.reason


# --- Codex review (2026-06-06): C13b should fail closed when git is unavailable ---


def test_c13b_subprocess_failure_held(tmp_path):
    """If `git ls-files` fails (e.g. dir isn't a worktree), C13b must HOLD, not pass."""
    # tmp_path is a directory but not a git repo — `git -C` will error.
    r = c13b_no_new_ignored_residue(set(), tmp_path)
    assert not r.ok and "git ignored-file listing failed" in r.reason


# --- Codex review (2026-06-06): C16 should fail closed on pytest non-zero exit ---


def test_c16_pytest_import_error_held(tmp_path):
    """A test_qa.py that fails to import (collection error) → pytest non-zero → -1 → fail."""
    p = tmp_path / "test_qa.py"
    p.write_text("import nonexistent_module_xyz\n\ndef test_x():\n    assert True\n")
    r = c16_pytest_collected_n_tests({"n_tests_written": 1}, tmp_path)
    assert not r.ok and (
        "failed" in r.reason or "timed out" in r.reason
    )
