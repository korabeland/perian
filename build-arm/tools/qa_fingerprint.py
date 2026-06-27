#!/usr/bin/env python3
"""Canonical workspace fingerprint + symlink/submodule rejection + ignored-residue snapshot.

WHY THIS EXISTS (the QA layer, Rev 5 spec, 2026-06-06):
The trust property "Gemma can withhold a confirm but cannot mint one" hinges on the
runner being able to PROVE the canonical workspace is byte-identical before and after the
Gemma+pytest run. A cheap-to-fool hash (e.g. git index hashing, mtime stat) would let
Gemma mutate canonical and forge a passing build. We use a deep, working-tree-resolved
hash that includes (a) per-file actual bytes via lstat, (b) symlink targets where present,
(c) the HEAD ref. Combined with a separate `git status --porcelain --untracked-files=all`
assertion in `c13_canonical_unchanged`, this catches every mutation path including index-
bypass, untracked-file injection, and runtime-visible ignored-file injection (Rev 5 spec
section "Trust property — seven structural fixes").

PUBLIC API:
- `canonical_fingerprint(ws: Path) -> str` — the load-bearing hash.
- `reject_symlinks_and_submodules(ws: Path) -> None` — raises on rejection.
- `snapshot_and_clean_residue(ws: Path) -> set[str]` — returns the set of ignored files
  present BEFORE we clean them; the runner uses this as `ignored_pre` for C13b.

DESIGN CHOICES:
- All git invocations use `git -C <ws>` (works for linked worktrees; `rev-parse HEAD`
  resolves to the worktree's HEAD, not the parent repo's).
- Submodules (`mode 160000` in `git ls-files -s` AND/OR `.gitmodules` presence) raise
  RuntimeError; the fingerprint logic would skip their contents.
- The hash construction is sensitive to filenames, file type (regular/symlink), the
  FULL mode mask `0o7777` (permission bits PLUS suid/sgid/sticky), and content. This
  is sufficient for a strict equality check, but is not a content-addressed identifier
  that maps to filesystem state alone.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path


# =============================================================================
# Public exception types
# =============================================================================


class SubmoduleRejected(RuntimeError):
    """Raised when the workspace contains git submodules (unsupported in v1)."""


class SymlinkRejected(RuntimeError):
    """Raised when the workspace root itself is a symlink. (Internal tracked symlinks are
    NOT rejected here; they fold into the fingerprint via lstat and are covered by C11.)"""


# =============================================================================
# reject_symlinks_and_submodules — step 1 of the QA pipeline
# =============================================================================


def reject_symlinks_and_submodules(ws: Path) -> None:
    """Raise if `ws` itself is a symlink OR contains git submodules.

    Codex review (2026-06-06): the previous docstring overpromised "rejects tracked
    symlinks whose target leaves the workspace" — that check actually lives in C11
    (qa_check_pre.c11_no_symlinks), not here. This function's scope is the two cheap,
    fail-fast guards needed BEFORE fingerprinting can be trusted.

    Submodule detection: `git ls-files -s` output mode `160000` is the gitlink mode; OR
    a top-level `.gitmodules` file exists (even if stale/untracked, treat as a fail-
    closed signal that this workspace might want submodule machinery we don't support).
    Either triggers SubmoduleRejected.

    Workspace-root symlink: redirects every subsequent `git -C <ws>` call to a different
    location, breaking the trust property; SymlinkRejected fires before any subprocess.

    Internal symlinks WITHIN the tree are intentionally NOT rejected here. They contribute
    their (target bytes) to the fingerprint via lstat, and C11 takes a separate pass at
    pre-pytest time.
    """
    if ws.is_symlink():
        raise SymlinkRejected(f"workspace root is a symlink: {ws}")
    if not ws.is_dir():
        raise FileNotFoundError(f"workspace is not a directory: {ws}")
    # .gitmodules check.
    if (ws / ".gitmodules").exists():
        raise SubmoduleRejected(
            f"submodules not supported in v1 (.gitmodules present in {ws})"
        )
    # ls-files -s check (gitlink mode = 160000).
    out = subprocess.check_output(
        ["git", "-C", str(ws), "ls-files", "-s"],
        stderr=subprocess.STDOUT,
    )
    # Each line: "<mode> <sha> <stage>\t<path>\n"; we look for the gitlink mode.
    for line in out.splitlines():
        if line.startswith(b"160000 "):
            raise SubmoduleRejected(
                f"submodules not supported in v1 (gitlink found in {ws})"
            )


# =============================================================================
# canonical_fingerprint — the load-bearing hash
# =============================================================================


def _u32(n: int) -> bytes:
    """Big-endian 4-byte length prefix used to make all variable-length fields
    prefix-free in the hash stream. Codex review (2026-06-06): without length prefixes,
    `[path_a, content_a, path_b, content_b]` could be re-bracketed in a colliding way."""
    return n.to_bytes(4, "big")


def _u64(n: int) -> bytes:
    """Big-endian 8-byte length prefix for file contents that may exceed 2^32 bytes."""
    return n.to_bytes(8, "big")


def canonical_fingerprint(ws: Path) -> str:
    """Return a sha256 hex digest deterministically derived from the canonical workspace's
    tracked content. RAW BYTES throughout — no UTF-8 decode of filesystem paths.

    Construction (Codex-reviewed 2026-06-06):
      h.update(b'CFP1')                    # version tag — bumping if format changes
      h.update(_u32(N))                    # number of tracked paths
      For each tracked path (sorted by raw bytes):
        h.update(_u32(len(path_bytes))    + path_bytes)         # length-prefixed
        h.update(stat.S_IFMT(st_mode).to_bytes(4, 'big'))       # file type bits
        h.update((st_mode & 0o7777).to_bytes(2, 'big'))         # mode bits incl. suid/sgid/sticky
        if symlink:
            h.update(b'L' + _u32(len(target_bytes)) + target_bytes)
        else:
            h.update(b'F' + _u64(len(content_bytes)) + content_bytes)
      h.update(b'H' + _u32(len(head_bytes)) + head_bytes)

    Codex review fixes:
      - `git ls-files -z` for null-delimited raw path bytes (Codex HIGH 2). No text
        decode; no `splitlines()` (would split on a path containing a newline). Sorts
        path bytes lexicographically.
      - Every variable field length-prefixed (Codex HIGH 1). Prevents two different
        tree layouts hashing identically by redistributing bytes across records.
      - Mode mask widened from `0o777` to `0o7777` (Codex HIGH 3). suid/sgid/sticky
        bits now contribute to the hash; flipping any of them changes the digest.
      - `os.readlink(full, encoding=None)` returns bytes when the path is bytes —
        we pass a bytes path so symlink targets are raw bytes too.

    The function does NOT internally assert clean status — the runner pairs this hash
    with a separate `git status --porcelain --untracked-files=all` check in C13.
    """
    reject_symlinks_and_submodules(ws)

    h = hashlib.sha256()
    h.update(b"CFP1")  # format version tag

    # -z: null-delimited raw bytes; no quoting, no line-break confusion.
    tracked_out = subprocess.check_output(
        ["git", "-C", str(ws), "ls-files", "-z"],
        stderr=subprocess.STDOUT,
    )
    raw_paths = [p for p in tracked_out.split(b"\x00") if p]
    raw_paths.sort()
    h.update(_u32(len(raw_paths)))

    ws_bytes = os.fsencode(str(ws))
    for path_bytes in raw_paths:
        # Build full path as bytes (avoids any text encoding round-trip).
        full_bytes = ws_bytes + b"/" + path_bytes
        st = os.lstat(full_bytes)
        h.update(_u32(len(path_bytes)))
        h.update(path_bytes)
        h.update(stat.S_IFMT(st.st_mode).to_bytes(4, "big"))
        # Widen mask: 0o7777 catches suid/sgid/sticky in addition to the low 9 bits.
        h.update((st.st_mode & 0o7777).to_bytes(2, "big"))
        if stat.S_ISLNK(st.st_mode):
            # os.readlink on bytes path returns bytes target — raw, no UTF-8 dance.
            target_bytes = os.readlink(full_bytes)
            h.update(b"L")
            h.update(_u32(len(target_bytes)))
            h.update(target_bytes)
        else:
            with open(full_bytes, "rb") as f:
                content = f.read()
            h.update(b"F")
            h.update(_u64(len(content)))
            h.update(content)

    head = subprocess.check_output(
        ["git", "-C", str(ws), "rev-parse", "HEAD"],
        stderr=subprocess.STDOUT,
    ).strip()
    h.update(b"H")
    h.update(_u32(len(head)))
    h.update(head)

    return h.hexdigest()


# =============================================================================
# snapshot_and_clean_residue — step 2 of the QA pipeline
# =============================================================================


def snapshot_and_clean_residue(ws: Path) -> set[str]:
    """Snapshot the set of ignored files currently present in `ws`, then remove ONLY the
    paths that the snapshot confirms are ignored.

    Codex review (2026-06-06): the previous cleaner used broad globs (`rglob("__pycache__")`,
    `rglob("*.pyc")`) which would delete TRACKED `.pyc` content if a repo intentionally
    tracks bytecode. The new cleaner removes only paths reported by
    `git ls-files --others --ignored --exclude-standard`, so anything the .gitignore
    rules don't classify as ignored is untouched. Cleanup failures FAIL CLOSED — we
    raise OSError back to the runner rather than silently swallowing.

    Return value = the ignored-file set captured BEFORE cleanup (used as `ignored_pre`
    for C13b later).
    """
    if not ws.is_dir():
        raise FileNotFoundError(f"workspace is not a directory: {ws}")

    # Snapshot ignored files BEFORE cleanup. Propagate errors — the runner should HOLD
    # up-front rather than silently treat the workspace as clean.
    out = subprocess.check_output(
        [
            "git",
            "-C",
            str(ws),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ],
        stderr=subprocess.STDOUT,
    )
    raw_snapshot_entries = [p for p in out.split(b"\x00") if p]
    snapshot = {os.fsdecode(p) for p in raw_snapshot_entries}

    # Clean ONLY the snapshotted ignored files (Codex review 2026-06-06): if it's not
    # in the ignored set, don't touch it. This avoids deleting tracked content under
    # `.pyc` / `__pycache__` names that the repo intentionally keeps.
    # `git ls-files --others --ignored` lists FILES only (not directories); after we
    # remove the files, we attempt to rmdir any newly-empty ancestor cache shells.
    # NEVER recursively delete a cache shell — Codex round 2 (2026-06-06) flagged that
    # broad rmtree could nuke tracked content stored inside a `__pycache__` dir.
    removed_files: list[bytes] = []
    for raw_rel in raw_snapshot_entries:
        full = os.path.join(os.fsencode(str(ws)), raw_rel)
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            # Race: ignored entry vanished before we got to it. Treat as already-clean.
            continue
        # git ls-files --others --ignored emits regular files / symlinks, not directories.
        # Defensive: if it IS a directory somehow, only rmdir if empty; never recurse.
        if stat.S_ISDIR(st.st_mode):
            try:
                os.rmdir(full)  # raises ENOTEMPTY if non-empty — leave it alone
            except OSError:
                continue
        else:
            os.unlink(full)
        removed_files.append(raw_rel)

    # Empty-only cleanup of well-known cache shells. Cache dirs like `.pytest_cache/v/
    # cache/` have nested subdirs that become empty after we removed the file content
    # in the loop above. We bottom-up `rmdir` those empty NESTED dirs, then attempt the
    # shell itself. We NEVER delete files and NEVER traverse symlinks — if tracked
    # content remains anywhere under the shell, the shell will fail to rmdir (ENOTEMPTY)
    # and we leave it alone.
    for shell_name in ("__pycache__", ".pytest_cache"):
        for d in list(ws.rglob(shell_name)):
            if d.is_dir() and not d.is_symlink():
                _rmdir_empty_tree(d)

    return snapshot


def _rmdir_empty_tree(d: Path) -> None:
    """Bottom-up rmdir of empty directories inside `d`, then `d` itself if empty.

    Codex review (2026-06-06): this is the precise complement to the ignored-file
    cleanup loop above. It NEVER deletes files. It NEVER traverses symlinks. After the
    walk, any dir that's empty is rmdir'd; anything containing a file (tracked or not)
    survives because rmdir raises ENOTEMPTY.
    """
    if not d.is_dir() or d.is_symlink():
        return
    for child in sorted(d.iterdir(), reverse=True):
        if child.is_dir() and not child.is_symlink():
            _rmdir_empty_tree(child)
    try:
        os.rmdir(d)
    except OSError:
        pass
