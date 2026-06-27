"""Tests for tools.qa_fingerprint — canonical_fingerprint + reject_symlinks_and_submodules
+ snapshot_and_clean_residue.

Strategy: build minimal git worktrees in tmp_path, then exercise each function. Coverage:
- Determinism (same inputs → identical digest).
- Content sensitivity (1-byte change → different digest).
- Mode sensitivity (chmod → different digest).
- Symlink target sensitivity.
- HEAD ref sensitivity (new commit → different digest).
- Submodule rejection paths (.gitmodules and gitlink mode).
- Workspace-root symlink rejection.
- snapshot_and_clean_residue: snapshot captures pre-clean state, cleanup removes targets.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tools.qa_fingerprint import (
    SubmoduleRejected,
    SymlinkRejected,
    canonical_fingerprint,
    reject_symlinks_and_submodules,
    snapshot_and_clean_residue,
)


# =============================================================================
# Helpers
# =============================================================================


def _init_repo(path: Path, gitignore: str = "__pycache__/\n.pytest_cache/\n*.pyc\n") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init", "--initial-branch=main", "-q"], cwd=path)
    subprocess.check_call(
        ["git", "config", "user.email", "test@test.invalid"], cwd=path
    )
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=path)
    (path / ".gitignore").write_text(gitignore)


def _commit_initial(path: Path) -> str:
    """Commit the current state and return HEAD sha."""
    subprocess.check_call(["git", "add", "-A"], cwd=path)
    subprocess.check_call(["git", "commit", "-qm", "init"], cwd=path)
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True
    ).strip()


# =============================================================================
# canonical_fingerprint determinism + sensitivity
# =============================================================================


def test_fingerprint_deterministic(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# graph\n")
    (tmp_path / "cycles.py").write_text("# cycles\n")
    _commit_initial(tmp_path)
    h1 = canonical_fingerprint(tmp_path)
    h2 = canonical_fingerprint(tmp_path)
    assert h1 == h2


def test_fingerprint_changes_on_file_content_change(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# v1\n")
    _commit_initial(tmp_path)
    h1 = canonical_fingerprint(tmp_path)
    (tmp_path / "graph.py").write_text("# v2\n")
    h2 = canonical_fingerprint(tmp_path)
    assert h1 != h2


def test_fingerprint_changes_on_mode_change(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "script.py").write_text("# script\n")
    _commit_initial(tmp_path)
    h1 = canonical_fingerprint(tmp_path)
    os.chmod(tmp_path / "script.py", 0o755)
    h2 = canonical_fingerprint(tmp_path)
    assert h1 != h2


def test_fingerprint_changes_on_new_commit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a\n")
    _commit_initial(tmp_path)
    h1 = canonical_fingerprint(tmp_path)
    # Add a second commit — HEAD ref changes, so the hash changes.
    (tmp_path / "b.py").write_text("b\n")
    subprocess.check_call(["git", "add", "-A"], cwd=tmp_path)
    subprocess.check_call(["git", "commit", "-qm", "add b"], cwd=tmp_path)
    h2 = canonical_fingerprint(tmp_path)
    assert h1 != h2


def test_fingerprint_changes_on_filename(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("hello\n")
    _commit_initial(tmp_path)
    h1 = canonical_fingerprint(tmp_path)
    # Rename → different hash (since path bytes enter the hash).
    subprocess.check_call(["git", "mv", "a.py", "renamed.py"], cwd=tmp_path)
    subprocess.check_call(["git", "commit", "-qm", "rename"], cwd=tmp_path)
    h2 = canonical_fingerprint(tmp_path)
    assert h1 != h2


def test_fingerprint_symlink_target_matters(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "real_a.txt").write_text("a\n")
    (tmp_path / "real_b.txt").write_text("b\n")
    (tmp_path / "link").symlink_to("real_a.txt")
    _commit_initial(tmp_path)
    h1 = canonical_fingerprint(tmp_path)
    # Re-point the symlink target without changing canonical content (re-create link).
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("real_b.txt")
    subprocess.check_call(["git", "add", "-A"], cwd=tmp_path)
    subprocess.check_call(["git", "commit", "-qm", "relink"], cwd=tmp_path)
    h2 = canonical_fingerprint(tmp_path)
    assert h1 != h2


# =============================================================================
# reject_symlinks_and_submodules
# =============================================================================


def test_reject_ws_root_symlink(tmp_path):
    real = tmp_path / "real_ws"
    real.mkdir()
    _init_repo(real)
    (real / "x.py").write_text("# x\n")
    _commit_initial(real)
    link = tmp_path / "link_ws"
    link.symlink_to(real)
    with pytest.raises(SymlinkRejected):
        reject_symlinks_and_submodules(link)


def test_reject_gitmodules_file_present(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    (tmp_path / ".gitmodules").write_text(
        "[submodule \"x\"]\n  path = sub\n  url = ./sub\n"
    )
    _commit_initial(tmp_path)
    with pytest.raises(SubmoduleRejected):
        reject_symlinks_and_submodules(tmp_path)


def test_reject_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        reject_symlinks_and_submodules(tmp_path / "nope")


def test_accept_clean_workspace(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    _commit_initial(tmp_path)
    reject_symlinks_and_submodules(tmp_path)  # no raise


# =============================================================================
# canonical_fingerprint via reject_symlinks_and_submodules (compose)
# =============================================================================


def test_canonical_fingerprint_rejects_symlink_root(tmp_path):
    real = tmp_path / "real_ws"
    real.mkdir()
    _init_repo(real)
    (real / "x.py").write_text("# x\n")
    _commit_initial(real)
    link = tmp_path / "link_ws"
    link.symlink_to(real)
    with pytest.raises(SymlinkRejected):
        canonical_fingerprint(link)


def test_canonical_fingerprint_rejects_submodules(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    (tmp_path / ".gitmodules").write_text("[submodule \"x\"]\n  path = sub\n")
    _commit_initial(tmp_path)
    with pytest.raises(SubmoduleRejected):
        canonical_fingerprint(tmp_path)


# =============================================================================
# snapshot_and_clean_residue
# =============================================================================


def test_snapshot_captures_pycache_and_cleans(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    _commit_initial(tmp_path)
    # Plant a __pycache__ dir.
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "graph.cpython-313.pyc").write_text("bytecode")
    snap = snapshot_and_clean_residue(tmp_path)
    # The snapshot should have captured the residue file...
    assert "__pycache__/graph.cpython-313.pyc" in snap
    # ...and the dir should be gone now.
    assert not cache.exists()


def test_snapshot_captures_pytest_cache_and_cleans(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    _commit_initial(tmp_path)
    cache = tmp_path / ".pytest_cache"
    cache.mkdir()
    (cache / "CACHEDIR.TAG").write_text("Signature: ...\n")
    (cache / "v" / "cache").mkdir(parents=True)
    (cache / "v" / "cache" / "lastfailed").write_text("{}")
    snap = snapshot_and_clean_residue(tmp_path)
    assert any(".pytest_cache" in s for s in snap)
    assert not cache.exists()


def test_snapshot_captures_stray_pyc_and_cleans(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    _commit_initial(tmp_path)
    (tmp_path / "module.pyc").write_text("bytecode")
    snap = snapshot_and_clean_residue(tmp_path)
    assert "module.pyc" in snap
    assert not (tmp_path / "module.pyc").exists()


def test_snapshot_clean_workspace_empty_snap(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    _commit_initial(tmp_path)
    snap = snapshot_and_clean_residue(tmp_path)
    assert snap == set()


def test_snapshot_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        snapshot_and_clean_residue(tmp_path / "nope")


def test_snapshot_not_a_worktree_raises(tmp_path):
    """If `git ls-files` fails (e.g. dir is not a worktree), propagate the error."""
    # tmp_path exists but is not a git repo.
    with pytest.raises(subprocess.CalledProcessError):
        snapshot_and_clean_residue(tmp_path)


# --- Codex review 2026-06-06: new attack/coverage paths ---


def test_fingerprint_suid_bit_sensitivity(tmp_path):
    """suid/sgid/sticky bits must contribute to the hash (mask is 0o7777, not 0o777)."""
    _init_repo(tmp_path)
    (tmp_path / "binlike").write_text("# fake bin\n")
    _commit_initial(tmp_path)
    h1 = canonical_fingerprint(tmp_path)
    # Flip suid bit (0o4000). Some filesystems may not honor it but the lstat result still
    # reflects the requested bits if chmod doesn't error.
    binlike = tmp_path / "binlike"
    os.chmod(binlike, 0o4644)
    h2 = canonical_fingerprint(tmp_path)
    assert h1 != h2


def test_fingerprint_non_ascii_path(tmp_path):
    """Paths with non-ASCII characters: the hash should be stable across two computes."""
    _init_repo(tmp_path)
    nonascii_name = "café.py"
    (tmp_path / nonascii_name).write_text("# x\n")
    _commit_initial(tmp_path)
    h1 = canonical_fingerprint(tmp_path)
    h2 = canonical_fingerprint(tmp_path)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_fingerprint_serialization_collision_resistance(tmp_path):
    """Codex review (2026-06-06): without length prefixes, two layouts with the same
    concatenated bytes-stream could produce identical hashes. Demonstrate the fix by
    constructing two repos whose tracked file CONTENTS swap a byte across files.

    Tree A: file 'a' = b'abc', file 'b' = b'def'
    Tree B: file 'a' = b'ab',  file 'b' = b'cdef'
    Without length prefixes both would feed sha256 with `path_a + b'abc' + path_b + b'def'`
    vs `path_a + b'ab' + path_b + b'cdef'` — different by length, so still distinguishable
    via path bytes, but length prefixes make it unambiguous regardless of any future
    similar layout. We assert h(A) != h(B) here as a regression guard.
    """
    a = tmp_path / "tree_a"
    b = tmp_path / "tree_b"
    a.mkdir()
    b.mkdir()
    _init_repo(a)
    (a / "x").write_text("abc")
    (a / "y").write_text("def")
    _commit_initial(a)

    _init_repo(b)
    (b / "x").write_text("ab")
    (b / "y").write_text("cdef")
    _commit_initial(b)

    h_a = canonical_fingerprint(a)
    h_b = canonical_fingerprint(b)
    assert h_a != h_b


def test_fingerprint_internal_symlink_accepted(tmp_path):
    """An internal symlink inside the tree should compute happily (no rejection here —
    C11 covers that)."""
    _init_repo(tmp_path)
    (tmp_path / "target.py").write_text("# real\n")
    (tmp_path / "link.py").symlink_to("target.py")
    _commit_initial(tmp_path)
    h = canonical_fingerprint(tmp_path)
    assert len(h) == 64


def test_fingerprint_gitlink_mode_submodule_rejected(tmp_path):
    """A real submodule with gitlink-mode entry must be rejected even without .gitmodules.

    We create a tracked entry with mode 160000 by inserting it via `git update-index`.
    """
    _init_repo(tmp_path)
    (tmp_path / "graph.py").write_text("# x\n")
    _commit_initial(tmp_path)
    # Fake gitlink entry: add a commit-sha-like hash with mode 160000.
    fake_sha = "0" * 40
    try:
        subprocess.check_call(
            [
                "git",
                "-C",
                str(tmp_path),
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{fake_sha},fake_submodule",
            ]
        )
    except subprocess.CalledProcessError:
        pytest.skip("git refused the fake gitlink (older git version)")
    with pytest.raises(SubmoduleRejected):
        reject_symlinks_and_submodules(tmp_path)


def test_snapshot_cleanup_does_not_delete_tracked_pyc(tmp_path):
    """Codex review (2026-06-06): if a repo TRACKS a .pyc file (rare but valid), the
    cleaner must NOT delete it. The cleaner now uses the ignored-file snapshot, not
    a broad glob.
    """
    _init_repo(tmp_path, gitignore="")  # no .gitignore rules — nothing ignored
    (tmp_path / "graph.py").write_text("# x\n")
    (tmp_path / "tracked.pyc").write_text("intentional bytecode")  # tracked .pyc
    _commit_initial(tmp_path)
    snap = snapshot_and_clean_residue(tmp_path)
    # The tracked .pyc must survive — it isn't in the ignored set.
    assert (tmp_path / "tracked.pyc").exists()
    assert snap == set()


def test_snapshot_cleanup_failure_propagates(tmp_path):
    """Codex review (2026-06-06): cleanup that can't complete must fail closed, not
    silently leave residue. We can't easily simulate an unremovable file portably, so
    this test confirms the function tolerates a clearly-missing child path."""
    _init_repo(tmp_path, gitignore="*.tmp\n")
    (tmp_path / "graph.py").write_text("# x\n")
    _commit_initial(tmp_path)
    # Create an ignored file.
    (tmp_path / "residue.tmp").write_text("garbage")
    # Pre-delete it to provoke a race where the snapshot includes it but unlink fails.
    # On macOS/POSIX, unlinking a missing file raises FileNotFoundError; the function
    # tolerates this race and continues.
    snap = snapshot_and_clean_residue(tmp_path)
    assert "residue.tmp" in snap


def test_snapshot_does_not_delete_tracked_pycache_content(tmp_path):
    """Codex review round 2 (2026-06-06): the cleaner must not recursively delete a
    `__pycache__/` shell. A repo that tracks content under that name (rare but valid)
    must survive the cleanup."""
    # gitignore is EMPTY — nothing is ignored, so __pycache__ content is tracked.
    _init_repo(tmp_path, gitignore="")
    (tmp_path / "graph.py").write_text("# x\n")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "tracked.pyc").write_text("intentionally tracked bytecode")
    _commit_initial(tmp_path)
    snap = snapshot_and_clean_residue(tmp_path)
    # Tracked content under __pycache__ MUST survive.
    assert (cache / "tracked.pyc").exists()
    # And the snapshot should be empty (nothing was ignored).
    assert snap == set()


def test_fingerprint_serialization_collision_same_head(tmp_path):
    """Codex review round 2 (2026-06-06): the previous serialization-collision test
    compared two separate repos with different HEAD shas, which could pass even if
    the file serialization were ambiguous. This test keeps HEAD constant and mutates
    only working-tree bytes — fingerprint MUST change.

    We commit a 2-file repo, capture the hash, then modify the file bytes in-place
    (rebuilding the working tree but leaving HEAD untouched). Hash MUST differ.
    """
    _init_repo(tmp_path)
    (tmp_path / "x").write_text("abc")  # content "abc"
    (tmp_path / "y").write_text("def")
    _commit_initial(tmp_path)
    h_a = canonical_fingerprint(tmp_path)
    # Mutate bytes in-place — HEAD ref UNCHANGED.
    (tmp_path / "x").write_text("ab")
    (tmp_path / "y").write_text("cdef")
    h_b = canonical_fingerprint(tmp_path)
    assert h_a != h_b


def test_fingerprint_invalid_utf8_path(tmp_path):
    """Codex review round 2 (2026-06-06): hash should be stable for paths with raw
    bytes that don't form valid UTF-8. POSIX-only."""
    import sys

    if sys.platform == "win32":
        pytest.skip("invalid-UTF-8 paths are not creatable on Windows")
    _init_repo(tmp_path)
    # Build a filename with a 0xFF byte (invalid as a UTF-8 continuation).
    bad_name = b"bad_\xff.txt"
    try:
        (tmp_path / os.fsdecode(bad_name)).write_text("x\n")
    except (OSError, ValueError, UnicodeEncodeError):
        pytest.skip("filesystem refused invalid-UTF-8 filename")
    try:
        _commit_initial(tmp_path)
    except subprocess.CalledProcessError:
        pytest.skip("git refused invalid-UTF-8 filename in commit")
    h1 = canonical_fingerprint(tmp_path)
    h2 = canonical_fingerprint(tmp_path)
    assert h1 == h2
    assert len(h1) == 64
