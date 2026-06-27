#!/usr/bin/env python3
"""Deterministic contract-chain validator gate for Auto-think/Auto-build.

Sweeps tracks and runs the schema validator with NO model in the loop. This is
the load-bearing safety check from ACTION_PLAN.md (§3 Phase 2 / §8 step 2): local
models proved unable to validate themselves (known issues #6/#7/#8 — QA self-
confirmed an invalid artifact, Main missed a structural violation, Gemma emitted
corrupted tool-call tokens), so the real gate must be code. It fires only on a
real schema FAIL — no model, no RAM. Sibling to tools/council_gate.py.

Usage:
  python -m tools.validator_gate [target] [--json] [--emit-event]

  target: a tracks-root directory (sweeps every track inside) OR a single track
          directory. Defaults to state/tracks under the ATB root.

Behavior:
  - Silent on stdout + exit 0 when every track is schema-clean (cron-friendly).
  - Prints "[GATE-FAIL] <track>" + the failing artifacts/errors + exit 2 when any
    track has a failing artifact.
  - --json       : always print a JSON result object (exit code unchanged).
  - --emit-event : on a trip, append one `validation-failed` line per failing
                   track to state/events.jsonl.
  - exit 1       : usage / IO error (target missing, no tracks found).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools.validator import schema_loader
from tools.validator import validator as _validator

ATB_ROOT = Path(__file__).resolve().parent.parent


def _is_track_dir(path: Path) -> bool:
    """A track dir directly contains at least one known contract-chain artifact."""
    return any(
        (path / f"{name}.json").is_file() for name in schema_loader.ALL_ARTIFACTS
    )


def validate_track(
    track_dir: Path, require_complete: bool = False, expected=None
) -> dict:
    """Validate every known artifact present in one track dir.

    Returns {track, path, total, failed:[{artifact, errors}]}. Empty `failed` =>
    the track is schema-clean (or has no artifacts yet, which is not a failure).

    With require_complete=True, any artifact in the expected set (default: all 11)
    that is MISSING from the track is added to `failed` — so the gate trips on an
    incomplete chain, not just an invalid one. Opt-in: the continuous cron leaves it
    off (in-progress tracks are legitimately partial); use it at track close.
    """
    results = []
    for name in schema_loader.ALL_ARTIFACTS:
        artifact_path = track_dir / f"{name}.json"
        if not artifact_path.is_file():
            continue
        schema = schema_loader.load_schema(name)
        if schema is None:
            results.append(
                {
                    "artifact": name,
                    "pass_": False,
                    "errors": [f"No schema for '{name}'"],
                }
            )
            continue
        result = _validator.validate_artifact(str(artifact_path), schema)
        result["artifact"] = name
        results.append(result)

    failed = [
        {"artifact": r["artifact"], "errors": r.get("errors", [])}
        for r in results
        if not r.get("pass_")
    ]

    if require_complete:
        expected_set = list(expected) if expected else list(schema_loader.ALL_ARTIFACTS)
        present = {r["artifact"] for r in results}
        for name in expected_set:
            if name not in present:
                failed.append(
                    {
                        "artifact": name,
                        "errors": ["missing: required artifact absent from track"],
                    }
                )

    return {
        "track": track_dir.name,
        "path": str(track_dir),
        "total": len(results),
        "failed": failed,
    }


def resolve_tracks(target: Path) -> list[Path]:
    """Return the list of track dirs implied by *target*.

    A single track dir -> [itself]; otherwise treat *target* as a tracks-root and
    return its immediate subdirectories (sorted), letting validate_track decide
    cleanliness per track.
    """
    if _is_track_dir(target):
        return [target]
    return sorted(p for p in target.iterdir() if p.is_dir())


def emit_event(track: dict, events_path: Path) -> bool:
    """Append one validation-failed event for a failing track. Returns True if written."""
    if not events_path.parent.is_dir():
        return False
    failed_names = [f"{f['artifact']}.json" for f in track["failed"]]
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "job_id": track["track"],
            "profile": "gate",
            "event": "validation-failed",
            "artifact": failed_names[0] if failed_names else None,
            "failed_artifacts": failed_names,
        }
    )
    with events_path.open("a") as fh:
        fh.write(line + "\n")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic contract-chain validator gate."
    )
    ap.add_argument(
        "target",
        nargs="?",
        default=str(ATB_ROOT / "state" / "tracks"),
        help="tracks-root dir or a single track dir (default: state/tracks under the ATB root)",
    )
    ap.add_argument("--json", action="store_true", help="always print a JSON result")
    ap.add_argument(
        "--emit-event",
        action="store_true",
        help="on a trip, append validation-failed to state/events.jsonl",
    )
    ap.add_argument(
        "--require-complete",
        action="store_true",
        help="completeness gate: a MISSING expected artifact trips the gate too (not "
        "just an invalid one). Use at track close, not on the continuous sweep.",
    )
    ap.add_argument(
        "--expected",
        default=None,
        help="comma-separated expected artifact names for --require-complete "
        "(default: all 11 contract-chain artifacts).",
    )
    args = ap.parse_args(argv)

    target = Path(args.target)
    if not target.is_dir():
        print(f"error: target directory not found: {target}", file=sys.stderr)
        return 1

    tracks = resolve_tracks(target)
    if not tracks:
        print(f"error: no track directories found under {target}", file=sys.stderr)
        return 1

    expected = (
        [s.strip() for s in args.expected.split(",") if s.strip()]
        if args.expected
        else None
    )
    reports = [
        validate_track(t, require_complete=args.require_complete, expected=expected)
        for t in tracks
    ]
    failing = [r for r in reports if r["failed"]]

    emitted = 0
    if failing and args.emit_event:
        # state/tracks/<job>/ -> state/events.jsonl ; for a tracks-root target the
        # parent of any track dir is the tracks root, whose parent is state/.
        events_path = tracks[0].parent.parent / "events.jsonl"
        for r in failing:
            if emit_event(r, events_path):
                emitted += 1

    if args.json:
        print(
            json.dumps(
                {
                    "gate_tripped": bool(failing),
                    "tracks_checked": len(reports),
                    "tracks_failed": len(failing),
                    "events_appended": emitted,
                    "failures": failing,
                },
                indent=2,
            )
        )
    elif failing:
        for r in failing:
            print(f"[GATE-FAIL] {r['track']}")
            for f in r["failed"]:
                print(f"  {f['artifact']}.json:")
                for err in f["errors"]:
                    print(f"    - {err}")
        if emitted:
            print(
                f"  -> {emitted} validation-failed event(s) appended to state/events.jsonl"
            )

    return 2 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
