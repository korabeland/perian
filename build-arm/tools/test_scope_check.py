"""Tests for tools.scope_check — the deterministic, model-free scope gate.

The load-bearing test is `test_new_file_in_sandbox_is_in_scope`: it reproduces the track #4
(trust-ledger) over-hold — a file that is NOT in planned_files but IS inside the allowed_paths
sandbox — and asserts the gate treats it as in scope (allowed_paths binding, planned_files
advisory). The escape tests assert anything LEAVING the sandbox is still held hard.
"""

import json
import subprocess

import pytest

from tools.scope_check import changed_files, check_scope, files_in_scope


# --- pure core: allowed_paths is binding, planned_files is irrelevant here ---

def test_new_file_in_sandbox_is_in_scope():
    """A new, unplanned file inside the allowed dir is in scope (the #4 over-hold guard)."""
    ok, v = files_in_scope(["events.jsonl", ".gitignore", "trust_ledger.py"], ["/w/x/"], "/w/x")
    assert ok is True
    assert v == []


def test_escape_outside_sandbox_is_held():
    ok, v = files_in_scope(["a.py", "../../tools/run_verification.py"], ["/w/x/"], "/w/x")
    assert ok is False
    assert v == ["../../tools/run_verification.py"]


def test_sibling_prefix_is_not_under():
    """Component-wise containment: /w/xtra/y.py must NOT count as under dir /w/x/."""
    ok, v = files_in_scope(["y.py"], ["/w/x/"], "/w/xtra")
    assert ok is False
    assert v == ["y.py"]


def test_exact_file_entry_matches_only_that_file():
    ok, v = files_in_scope(["a.py", "b.py"], ["/w/x/a.py"], "/w/x")
    assert ok is False
    assert v == ["b.py"]


def test_exact_file_entry_does_not_authorize_children():
    """A bare file entry must not let nested paths under that file name pass (Codex finding)."""
    ok, v = files_in_scope(["a.py/evil.py"], ["/w/x/a.py"], "/w/x")
    assert ok is False
    assert v == ["a.py/evil.py"]


def test_glob_entry_matches_pattern_only():
    ok, v = files_in_scope(["a.py", "notes.txt"], ["/w/x/*.py"], "/w/x")
    assert ok is False
    assert v == ["notes.txt"]


def test_multiple_entries_any_match_is_in_scope():
    ok, v = files_in_scope(["src/a.py", "docs/b.md"], ["/w/x/src/", "/w/x/docs/"], "/w/x")
    assert ok is True
    assert v == []


def test_empty_changes_is_vacuously_in_scope():
    ok, v = files_in_scope([], ["/w/x/"], "/w/x")
    assert ok is True
    assert v == []


# --- git integration + product-plan loading (end to end) --------------------

def _init_repo(root):
    def git(*a):
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (root / "base.txt").write_text("base\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return git


def _write_plan(track, allowed):
    (track / "product-plan.json").write_text(
        json.dumps(
            {
                "job_id": "t",
                "allowed_paths": allowed,
                "planned_files": [],
                "non_goals": [],
                "verification_commands": ["true"],
                "acceptance_checks": [],
                "risk_assessment": "x",
            }
        )
    )


def test_changed_files_lists_committed_diff(tmp_path):
    work = tmp_path / "build"
    work.mkdir()
    git = _init_repo(work)
    (work / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "work")
    assert changed_files(work, "HEAD~1") == ["a.py"]


def test_check_scope_passes_unplanned_file_inside_sandbox(tmp_path):
    work = tmp_path / "build"
    work.mkdir()
    git = _init_repo(work)
    track = tmp_path / "track"
    track.mkdir()
    _write_plan(track, [str(work) + "/"])
    (work / "a.py").write_text("x = 1\n")
    (work / "events.jsonl").write_text("{}\n")  # unplanned, but inside the sandbox
    git("add", "-A")
    git("commit", "-q", "-m", "work")
    res = check_scope(track, work, "HEAD~1")
    assert res["ok"] is True, res
    assert res["violations"] == []
    assert res["changed_count"] == 2


def test_check_scope_canonicalizes_bare_directory_entry(tmp_path):
    """A bare allowed_paths entry that is a REAL dir is treated as a subtree (no slash needed)."""
    work = tmp_path / "build"
    work.mkdir()
    git = _init_repo(work)
    track = tmp_path / "track"
    track.mkdir()
    _write_plan(track, [str(work)])  # bare dir entry, NO trailing slash
    (work / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "work")
    res = check_scope(track, work, "HEAD~1")
    assert res["ok"] is True, res


def test_check_scope_holds_file_outside_sandbox(tmp_path):
    work = tmp_path / "build"
    work.mkdir()
    git = _init_repo(work)
    sub = work / "src"
    sub.mkdir()
    track = tmp_path / "track"
    track.mkdir()
    _write_plan(track, [str(sub) + "/"])  # only src/ is allowed
    (sub / "ok.py").write_text("x = 1\n")
    (work / "escape.py").write_text("y = 2\n")  # outside src/
    git("add", "-A")
    git("commit", "-q", "-m", "work")
    res = check_scope(track, work, "HEAD~1")
    assert res["ok"] is False
    assert "escape.py" in res["violations"]
    assert "src/ok.py" not in res["violations"]


# --- fail-safe contract: indeterminate -> raise (CLI turns it into a hold) ---

def test_check_scope_raises_without_product_plan(tmp_path):
    work = tmp_path / "build"
    work.mkdir()
    _init_repo(work)
    track = tmp_path / "track"
    track.mkdir()  # no product-plan.json
    with pytest.raises((OSError, RuntimeError, json.JSONDecodeError)):
        check_scope(track, work, "HEAD")


def test_check_scope_raises_on_empty_allowed_paths(tmp_path):
    work = tmp_path / "build"
    work.mkdir()
    _init_repo(work)
    track = tmp_path / "track"
    track.mkdir()
    _write_plan(track, [])
    with pytest.raises(RuntimeError):
        check_scope(track, work, "HEAD")
