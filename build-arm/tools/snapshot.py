#!/usr/bin/env python3
"""Interim "undo button" for ATB: local APFS snapshots (rewind points).

`tmutil localsnapshot` takes a copy-on-write, point-in-time snapshot of the disk (no sudo,
near-instant, near-zero space until data diverges). These are PURGEABLE (macOS may delete
them under disk pressure) and live on the SAME disk — a rewind, NOT a real backup. Replace
with Time Machine to a dedicated drive when one is available (see OVERSEER_SETUP.md).

Used two ways: hourly via a launchd agent (rolling protection), and as the pre-build hook so
a rewind point always exists right before an autonomous coder run.

Usage:
  python -m tools.snapshot create   # make a snapshot; log a pre-build-snapshot event
  python -m tools.snapshot list     # list the Time Machine local snapshots
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EVENTS = Path(__file__).resolve().parent.parent / "state" / "events.jsonl"


def create() -> int:
    try:
        out = subprocess.run(
            ["tmutil", "localsnapshot"], capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: tmutil localsnapshot failed: {exc}", file=sys.stderr)
        return 1
    m = re.search(r"date:\s*(\S+)", out.stdout)
    if out.returncode != 0 or not m:
        print(
            f"error: no snapshot created: {(out.stderr or out.stdout)[-200:]}",
            file=sys.stderr,
        )
        return 1
    snap = m.group(1)
    if EVENTS.parent.is_dir():
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "profile": "snapshot",
                "event": "pre-build-snapshot",
                "snapshot": snap,
            }
        )
        with EVENTS.open("a") as fh:
            fh.write(line + "\n")
    print(snap)
    return 0


def lst() -> int:
    out = subprocess.run(
        ["tmutil", "listlocalsnapshots", "/"], capture_output=True, text=True
    )
    for ln in out.stdout.splitlines():
        if "TimeMachine" in ln:
            print(ln.strip())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ATB local-snapshot undo button.")
    ap.add_argument("cmd", choices=["create", "list"], nargs="?", default="create")
    args = ap.parse_args(argv)
    return create() if args.cmd == "create" else lst()


if __name__ == "__main__":
    sys.exit(main())
