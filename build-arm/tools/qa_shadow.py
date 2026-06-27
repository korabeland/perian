"""Read-only QA-layer replay harness for Rev 5 phase 11 — shadow calibration.

WHY THIS EXISTS

Rev 5 spec phase 11 ("Shadow on 3 historical confirmed builds. Route, do NOT
block. Tune."): before the QA layer gates a live build, we replay it against
known-good historical builds to calibrate Codex's audit verdict against ground
truth (each chosen build did confirm). If Codex routes any of them to anything
other than `confirm`, the result is a calibration signal — investigate.

WHAT IT DOES NOT DO

- Does NOT mutate the historical track dir (state/tracks/<id>/) or build
  worktree (state/builds/<id>/). Both are deep-copied to a tmp scratch dir
  before run_qa() touches them. Symlinks inside the source are preserved
  (not dereferenced) so the copy is a faithful snapshot.
- Does NOT append to production state/events.jsonl. The qa_route event lands
  in a synthetic shadow state dir under `state/shadow/<id>/state-events.jsonl`.

WHAT IT DOES TOUCH ON PRODUCTION STATE

- Holds an exclusive flock on the PRODUCTION `state/.model.lock` for the
  duration of one shadow run (Codex review finding #1: synthetic-only locking
  would let a live Hermes invocation model-swap underfoot). If the gate-sweep
  cron is paused per the documented pre-condition, contention will be brief
  or absent; the shadow waits if a live swap is in flight.

WHAT IT PROVES

The same `run_qa()` function gate_sweep calls in production runs end-to-end
against an unmodified copy of the historical inputs. The captured artifacts
(qa.json, qa-strategy.json, qa-route.json) are the same shape gate_sweep would
have seen, minus any side effects.

PRE-CONDITIONS (caller responsibility)

- Gate-sweep cron `09a24f272468` paused so no live build contends.
- Codex CLI authenticated (`codex --version`).
- LM Studio reachable; Gemma swap should work via swap_orchestrator.

LINKED-WORKTREE LIMITATION

We require the build dir to be a standalone git repo (`.git` is a directory).
Linked worktrees (`.git` as a file pointing back to a shared gitdir) would not
copy faithfully via shutil.copytree and are rejected up front rather than
copied and silently misinterpreted (Codex review finding #2). All historical
ATB builds use standalone repos so this is not a regression in practice.

USAGE

  python3 tools/qa_shadow.py <job_id> [--output state/shadow] [--timeout 300]
                            [--keep-scratch-on-error]

  Returns 0 if the harness completed (regardless of route), nonzero only on a
  harness-internal failure (copy error, missing inputs, etc.). The computed
  route lives in <output>/<job_id>/shadow-summary.json.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import NamedTuple

# Make sibling modules importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.qa import _collect_coder_nodeids, _flock, run_qa  # noqa: E402

ATB_ROOT = Path(__file__).resolve().parent.parent

# Artifact filenames that run_qa may write under the (tmp) track_dir.
_PROMOTABLE_TRACK_ARTIFACTS = ("qa.json", "qa-strategy.json", "qa-route.json")

# Files we promote out of the synthetic state_dir into the output dir.
_PROMOTABLE_STATE_FILES = (("events.jsonl", "state-events.jsonl"),)


class ShadowResult(NamedTuple):
    """Captured outcome of one shadow replay."""

    job_id: str
    computed_route: str
    failure_reason: str
    elapsed_seconds: float
    canonical_hash_pre: str
    canonical_hash_post: str
    output_dir: Path  # where qa.json / qa-strategy.json / qa-route.json landed
    shadow_state_dir: Path  # where the synthetic events.jsonl landed
    promoted: dict[str, bool]  # filename -> whether it was actually written


def _reject_linked_worktree(build_src: Path) -> None:
    """If `.git` is a FILE (linked-worktree pointer), refuse to shadow this
    build — plain copytree would clone the pointer without the shared gitdir,
    and `git -C <copy> ls-files ...` would either fail or read the original
    metadata. Fail closed with a clear error instead.
    """
    git_path = build_src / ".git"
    if git_path.exists() and not git_path.is_dir():
        raise RuntimeError(
            f"build dir {build_src} uses a linked git worktree (.git is "
            f"not a directory); shadow replay does not support linked "
            f"worktrees. Materialize into a standalone repo first."
        )


def _copy_tree_readonly_source(src: Path, dst: Path) -> None:
    """Deep-copy `src` to `dst` so subsequent mutation of `dst` cannot reach
    back into `src`. Caller guarantees `dst` does not yet exist.

    `symlinks=True` preserves symlinks as symlinks rather than chasing them,
    so the copy is a faithful mirror (Codex review finding #2: default
    `symlinks=False` would silently dereference internal symlinks contrary
    to the doc).
    """
    shutil.copytree(src, dst, symlinks=True)


def _preclear_output_dir(output_dir: Path) -> None:
    """Remove any stale promoted artifacts from a previous shadow run.

    Codex review finding #3: `run_qa` can short-circuit (pre-flight failure,
    contract-parse failure) without writing qa-*.json. A subsequent run that
    short-circuits earlier would otherwise leave the previous run's stale
    qa-route.json in place, mis-leading the operator.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in _PROMOTABLE_TRACK_ARTIFACTS:
        p = output_dir / name
        if p.is_file():
            p.unlink()
    for _src, dst_name in _PROMOTABLE_STATE_FILES:
        p = output_dir / dst_name
        if p.is_file():
            p.unlink()
    # Also remove the summary; we'll always rewrite it.
    summary = output_dir / "shadow-summary.json"
    if summary.is_file():
        summary.unlink()


def _promote_artifacts(
    tmp_track: Path,
    shadow_state_dir: Path,
    output_dir: Path,
) -> dict[str, bool]:
    """Copy any qa-*.json that run_qa produced (in the tmp track) plus the
    synthetic events.jsonl into the persistent output dir.

    Returns a presence map so the summary records explicitly which artifacts
    were produced — silently missing files would otherwise read as "run was
    clean" when in fact the pipeline short-circuited before writing them.
    """
    promoted: dict[str, bool] = {}
    for name in _PROMOTABLE_TRACK_ARTIFACTS:
        src = tmp_track / name
        if src.is_file():
            shutil.copy2(src, output_dir / name)
            promoted[name] = True
        else:
            promoted[name] = False
    for src_name, dst_name in _PROMOTABLE_STATE_FILES:
        src = shadow_state_dir / src_name
        if src.is_file():
            shutil.copy2(src, output_dir / dst_name)
            promoted[dst_name] = True
        else:
            promoted[dst_name] = False
    return promoted


def shadow_replay(
    job_id: str,
    *,
    atb_root: Path = ATB_ROOT,
    output_root: Path | None = None,
    pytest_timeout: int = 300,
    keep_scratch_on_error: bool = False,
) -> ShadowResult:
    """Run the QA pipeline against a historical confirmed build, read-only.

    The function snapshots the track + build dirs to a scratch location, holds
    the production model lock for the duration of the run, calls run_qa() with
    a synthetic state_dir, then promotes the produced artifacts into
    <output_root>/<job_id>/. The scratch dir is removed on success and (by
    default) on failure too; pass `keep_scratch_on_error=True` to retain it
    for post-mortem.
    """
    track_src = atb_root / "state" / "tracks" / job_id
    build_src = atb_root / "state" / "builds" / job_id

    if not track_src.is_dir():
        raise FileNotFoundError(f"track dir missing: {track_src}")
    if not build_src.is_dir():
        raise FileNotFoundError(f"build dir missing: {build_src}")
    _reject_linked_worktree(build_src)

    output_root = output_root or (atb_root / "state" / "shadow")
    output_dir = output_root / job_id

    # Per-job output lock — protects preclear → run → promotion → summary
    # against a concurrent same-job_id replay (Codex review round 2 #2). The
    # lockfile lives under output_root/_locks/ so it survives between runs
    # but doesn't pollute the per-job output dir. Held for the full critical
    # section so two operators racing the same job serialize cleanly.
    lock_dir = output_root / "_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    job_output_lock = lock_dir / f"{job_id}.lock"

    # Production model lock — outer guard against a stale live Hermes
    # invocation model-swapping under us (Codex review round 1 #1).
    prod_model_lock = atb_root / "state" / ".model.lock"

    # Capture pre-scratch state so we can clean up on any failure path.
    started = time.monotonic()
    scratch_root: Path | None = None
    tmp_track: Path | None = None
    shadow_state_dir: Path | None = None

    # Codex review round 2 #1: the try block must cover scratch creation so
    # a KeyboardInterrupt or mkdir failure during setup still hits the crash
    # branch.
    # Codex review round 3: the crash-summary write MUST happen while still
    # holding the per-job output lock — otherwise a later same-job replay
    # could grab the lock and write its newer summary first, only to be
    # overwritten by our older status=crashed. The inner try/except thus
    # nests inside `with _flock(job_output_lock):`.
    qa_run = None
    promoted: dict[str, bool] = {}
    try:
        with _flock(job_output_lock):
            try:
                _preclear_output_dir(output_dir)

                # Scratch: per-run, sibling of the final output so a failed
                # run leaves a trail (when requested). `time.monotonic_ns()`
                # keeps the name unique across rapid re-invocations.
                scratch_root = (
                    output_root
                    / "_scratch"
                    / f"{job_id}-{time.monotonic_ns()}"
                )
                scratch_root.mkdir(parents=True, exist_ok=False)
                tmp_track = scratch_root / "track"
                tmp_build = scratch_root / "build"
                shadow_state_dir = scratch_root / "state"
                shadow_state_dir.mkdir()

                _copy_tree_readonly_source(track_src, tmp_track)
                _copy_tree_readonly_source(build_src, tmp_build)

                # Finding #7 (2026-06-07): mirror gate_sweep's behavior —
                # collect coder pytest nodeids BEFORE run_qa so the shadow
                # exercises the same C3 evidence-check path the live layer
                # will. Run against tmp_build (the scratch copy), not
                # build_src, so the helper's disposable-copy guarantee
                # composes cleanly with shadow's own read-only-source
                # invariant. Reason (if any) lands in the summary so the
                # operator can grep collect failures across replays.
                coder_nodeids, collect_reason = _collect_coder_nodeids(
                    tmp_build
                )

                with _flock(prod_model_lock):
                    qa_run = run_qa(
                        tmp_track,
                        tmp_build,
                        shadow_state_dir,
                        coder_test_nodeids=coder_nodeids,
                        pytest_timeout=pytest_timeout,
                    )

                # Promote + write summary while still holding the per-job
                # output lock (Codex review round 2 #2).
                promoted = _promote_artifacts(
                    tmp_track, shadow_state_dir, output_dir
                )

                summary = {
                    "status": "completed",
                    "job_id": job_id,
                    "computed_route": qa_run.computed_route,
                    "failure_reason": qa_run.failure_reason,
                    "elapsed_seconds": qa_run.elapsed_seconds,
                    "canonical_hash_pre": qa_run.canonical_hash_pre,
                    "canonical_hash_post": qa_run.canonical_hash_post,
                    "track_src": str(track_src),
                    "build_src": str(build_src),
                    "shadow_state_dir": str(shadow_state_dir),
                    "wall_seconds_total": time.monotonic() - started,
                    "promoted_artifacts": promoted,
                    "coder_nodeid_count": len(coder_nodeids),
                    "coder_collect_reason": collect_reason,
                }
                (output_dir / "shadow-summary.json").write_text(
                    json.dumps(summary, indent=2) + "\n"
                )
            except BaseException as exc:
                # Codex review round 1 #5 + round 3: write the crash summary
                # WHILE the job output lock is still held, so a concurrent
                # same-job replay cannot race and overwrite us. Tolerate the
                # case where the crash happened BEFORE scratch was assigned
                # (Codex review round 2 #1).
                try:
                    partial_promoted: dict[str, bool] = {}
                    if tmp_track is not None:
                        for name in _PROMOTABLE_TRACK_ARTIFACTS:
                            partial_promoted[name] = (
                                tmp_track / name
                            ).is_file()
                    else:
                        for name in _PROMOTABLE_TRACK_ARTIFACTS:
                            partial_promoted[name] = False
                    if shadow_state_dir is not None:
                        for src_name, dst_name in _PROMOTABLE_STATE_FILES:
                            partial_promoted[dst_name] = (
                                shadow_state_dir / src_name
                            ).is_file()
                    else:
                        for _src_name, dst_name in _PROMOTABLE_STATE_FILES:
                            partial_promoted[dst_name] = False
                    crash_summary = {
                        "status": "crashed",
                        "job_id": job_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:1000],
                        "traceback": traceback.format_exc()[-4000:],
                        "scratch_root": (
                            str(scratch_root) if scratch_root else None
                        ),
                        "scratch_kept": keep_scratch_on_error,
                        "partial_artifacts_in_scratch": partial_promoted,
                        "wall_seconds_to_crash": time.monotonic() - started,
                    }
                    output_dir.mkdir(parents=True, exist_ok=True)
                    (output_dir / "shadow-summary.json").write_text(
                        json.dumps(crash_summary, indent=2) + "\n"
                    )
                except Exception:
                    # Don't let the crash-summary writer eat the original
                    # exception.
                    pass
                raise
    except BaseException:
        # Lock-free cleanup: scratch is per-run unique, no race concern.
        # Codex review round 1 #4: default cleanup on failure to avoid disk
        # leak; opt-in retention via keep_scratch_on_error.
        if not keep_scratch_on_error and scratch_root is not None:
            shutil.rmtree(scratch_root, ignore_errors=True)
            scratch_parent = scratch_root.parent
            try:
                scratch_parent.rmdir()
            except OSError:
                pass
        raise

    # Success path (lock released, no exception): clean scratch + return.
    assert qa_run is not None and scratch_root is not None
    shutil.rmtree(scratch_root, ignore_errors=True)
    scratch_parent = scratch_root.parent
    try:
        scratch_parent.rmdir()
    except OSError:
        pass

    return ShadowResult(
        job_id=job_id,
        computed_route=qa_run.computed_route,
        failure_reason=qa_run.failure_reason,
        elapsed_seconds=qa_run.elapsed_seconds,
        canonical_hash_pre=qa_run.canonical_hash_pre,
        canonical_hash_post=qa_run.canonical_hash_post,
        output_dir=output_dir,
        shadow_state_dir=shadow_state_dir,
        promoted=promoted,
    )


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only shadow replay of the QA layer against a historical "
            "confirmed build (Rev 5 spec phase 11)."
        )
    )
    parser.add_argument(
        "job_id",
        help="The historical track/build ID (e.g. 20260603-run-metrics-logger).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Root for shadow output (default: state/shadow under repo root).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="pytest sandbox timeout in seconds (default 300).",
    )
    parser.add_argument(
        "--keep-scratch-on-error",
        action="store_true",
        help=(
            "Retain the scratch dir if the run crashes, for post-mortem "
            "(default: clean up on both success and failure)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        result = shadow_replay(
            args.job_id,
            output_root=args.output,
            pytest_timeout=args.timeout,
            keep_scratch_on_error=args.keep_scratch_on_error,
        )
    except FileNotFoundError as e:
        print(f"shadow: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"shadow: harness crashed: {e}", file=sys.stderr)
        return 3

    print(
        f"shadow {result.job_id}: route={result.computed_route} "
        f"reason={result.failure_reason or 'ok'} "
        f"elapsed={result.elapsed_seconds:.1f}s "
        f"out={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
