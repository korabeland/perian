#!/usr/bin/env python3
"""Deterministic verification-delta for ATB builds (post-mortem #9 / L1 hardening).

Compares the Coder's tool-captured ``verification.json`` against QA's tool-captured
``qa-verification.json`` and writes ``verification-delta.json`` -- with NO model in the
loop. Run by the deterministic qa gate (see tools/gate_sweep.py), so the delta state
('confirmed' etc.) is derived from real captured exit codes, not an agent's prose.

Why this exists: on run #3 (2026-06-03) the QA *model* hand-wrote a schema-valid,
tool-shaped ``qa-verification.json`` + ``verification-delta.json`` stamped ~12 minutes
in the FUTURE -- a forgery that sailed past the validator gate. A model left in its own
trust path will forge the certificate when forging is cheaper than the work. The fix is
to take the model OUT of the trust path: a tool runs the verification and a tool
computes the delta. This module is the latter.

Freshness guard: any input report whose ``ran_at`` is in the future (beyond a small
clock-jitter tolerance) or implausibly stale is treated as fabricated/untrustworthy ->
state ``missing_evidence``. This is the mechanical form of the "compare the artifact's
timestamp to the wall clock" check that caught the run #3 forgery.

Usage:
  python -m tools.verification_delta --track <dir>
      [--max-future-seconds N] [--max-age-hours N] [--json]

Exit: 0 = state 'confirmed' · 2 = any non-confirmed state (held) · 1 = usage/IO error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ATB_ROOT = Path(__file__).resolve().parent.parent

# Freshness thresholds. A worker subprocess and the gate run on the SAME machine, so a
# legitimate clock gap is seconds; 120s is generous jitter tolerance while still flagging
# the ~12-minute future stamp of the run #3 forgery. Stale guard is loose (builds can sit
# in the cron grid for a while) but still rejects a wildly old/placeholder timestamp.
DEFAULT_MAX_FUTURE_SECONDS = 120
DEFAULT_MAX_AGE_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value):
    """Parse an ISO-8601 UTC timestamp to an aware datetime, or None if unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # Tolerate both the strftime 'Z' form and isoformat '+00:00'.
    candidate = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_freshness(
    report,
    now: datetime,
    *,
    max_future_seconds: int = DEFAULT_MAX_FUTURE_SECONDS,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
):
    """Return (ok, reason) for a report's ``ran_at`` timestamp against the wall clock.

    A future timestamp (beyond ``max_future_seconds``) is the run #3 fabrication
    fingerprint; an implausibly old one is a stale/placeholder fingerprint. Either ->
    not fresh, so the report cannot be trusted as a real tool run.
    """
    raw = report.get("ran_at") if isinstance(report, dict) else None
    ts = _parse_ts(raw)
    if ts is None:
        return False, f"unparseable ran_at: {raw!r}"
    skew = (ts - now).total_seconds()
    if skew > max_future_seconds:
        return False, f"ran_at {_fmt(ts)} is {int(skew)}s in the future (now {_fmt(now)}) -- fabricated"
    if -skew > max_age_hours * 3600:
        return False, f"ran_at {_fmt(ts)} is {int(-skew / 3600)}h stale (now {_fmt(now)})"
    return True, "fresh"


def _commands(report) -> list:
    if not isinstance(report, dict):
        return []
    return report.get("commands_run") or []


def _outputs(report) -> list:
    """Return a report's ``outputs`` list (each item {command, exit_code, ...}), or []."""
    if isinstance(report, dict) and isinstance(report.get("outputs"), list):
        return report["outputs"]
    return []


def _at(lst, i) -> dict:
    """``outputs[i]`` as a dict (or {} if out of range / not a dict)."""
    v = lst[i] if i < len(lst) else None
    return v if isinstance(v, dict) else {}


def _diff(field: str, coder_claim, qa_finding) -> dict:
    """Build a schema-conformant differences[] entry (all three fields are strings)."""
    return {
        "field": str(field),
        "coder_claim": str(coder_claim),
        "qa_finding": str(qa_finding),
    }


def compute_delta(
    coder,
    qa,
    *,
    job_id: str,
    now: datetime,
    max_future_seconds: int = DEFAULT_MAX_FUTURE_SECONDS,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> dict:
    """Compute a verification-delta dict from two report dicts (pure; no I/O).

    State machine (deterministic):
      missing_evidence -- a report is absent/malformed, has no commands, or its
                          timestamp fails the freshness guard (fabricated/stale).
      confirmed        -- both pass AND ran the same commands with the same exit codes.
      regression       -- QA's independent run does not pass (cp & not qp, or neither).
      drift            -- both pass but the evidence differs, or results are inconsistent.
    """
    differences: list = []

    # --- missing evidence: structural ---
    if not isinstance(coder, dict) or not isinstance(qa, dict):
        which = "verification.json" if not isinstance(coder, dict) else "qa-verification.json"
        differences.append(_diff(which, "present+parseable", "missing or not a JSON object"))
        return _report(job_id, now, "missing_evidence", differences, coder, qa)

    c_cmds, q_cmds = _commands(coder), _commands(qa)
    if not c_cmds or not q_cmds:
        differences.append(
            _diff("commands_run", f"{len(c_cmds)} command(s)", f"{len(q_cmds)} command(s)")
        )
        return _report(job_id, now, "missing_evidence", differences, coder, qa)

    # --- missing evidence: freshness (fabricated/stale timestamp) ---
    for label, rep in (("verification.json", coder), ("qa-verification.json", qa)):
        ok, reason = check_freshness(
            rep, now, max_future_seconds=max_future_seconds, max_age_hours=max_age_hours
        )
        if not ok:
            differences.append(_diff(f"{label} ran_at", "real tool timestamp", reason))
    if differences:
        return _report(job_id, now, "missing_evidence", differences, coder, qa)

    cp, qp = bool(coder.get("pass")), bool(qa.get("pass"))

    # --- evidence comparison (ORDER- and duplicate-sensitive) ---
    # Compare the ordered command lists, then the captured outputs position-by-position.
    # set() would collapse duplicates and ignore order, letting a reordered/repeated
    # command sequence falsely 'confirm' (Codex review 2026-06-03).
    if c_cmds != q_cmds:
        differences.append(_diff("commands_run", c_cmds, q_cmds))
    c_out, q_out = _outputs(coder), _outputs(qa)
    for i in range(max(len(c_out), len(q_out))):
        co, qo = _at(c_out, i), _at(q_out, i)
        if co.get("command") != qo.get("command") or co.get("exit_code") != qo.get("exit_code"):
            differences.append(
                _diff(
                    f"outputs[{i}]",
                    f"{co.get('command')!r} rc={co.get('exit_code')}",
                    f"{qo.get('command')!r} rc={qo.get('exit_code')}",
                )
            )
    if cp != qp:
        differences.append(_diff("pass", cp, qp))

    # --- state ---
    if cp and qp and not differences:
        state = "confirmed"
    elif not qp:
        # QA's independent run did not pass -> the build is not actually verified.
        state = "regression"
    else:
        # Both pass (or coder-fail/qa-pass) but the evidence disagrees.
        state = "drift"

    return _report(job_id, now, state, differences, coder, qa)


def _report(job_id, now, state, differences, coder, qa) -> dict:
    notes = {
        "confirmed": "Tool-captured QA run agrees with the coder verification: same commands, same exit codes, both pass.",
        "regression": "QA's independent tool run does NOT pass -- the build is not verified.",
        "drift": "Both runs pass but the captured evidence differs (commands or exit codes).",
        "missing_evidence": "Verification evidence is missing, malformed, or has a fabricated/stale timestamp -- cannot trust the delta.",
    }[state]
    return {
        "job_id": job_id,
        "compared_at": _fmt(now),
        "state": state,
        "differences": differences,
        "notes": notes,
        "coder_pass": bool(coder.get("pass")) if isinstance(coder, dict) else None,
        "qa_pass": bool(qa.get("pass")) if isinstance(qa, dict) else None,
    }


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def build_delta(
    track: Path,
    *,
    now: datetime,
    max_future_seconds: int = DEFAULT_MAX_FUTURE_SECONDS,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> dict:
    """Load both verification files from *track* and compute the delta."""
    coder = _load(track / "verification.json")
    qa = _load(track / "qa-verification.json")
    job_id = None
    for rep in (coder, qa):
        if isinstance(rep, dict) and rep.get("job_id"):
            job_id = rep["job_id"]
            break
    job_id = job_id or track.name
    return compute_delta(
        coder,
        qa,
        job_id=job_id,
        now=now,
        max_future_seconds=max_future_seconds,
        max_age_hours=max_age_hours,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="verification_delta",
        description="Deterministic ATB verification-delta (no model in the trust path).",
    )
    ap.add_argument("--track", required=True, help="track dir holding the two verification files")
    ap.add_argument("--max-future-seconds", type=int, default=DEFAULT_MAX_FUTURE_SECONDS)
    ap.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    track = Path(args.track)
    if not track.is_absolute():
        track = ATB_ROOT / track
    track = track.resolve()  # resolve symlinks/aliases BEFORE the containment check
    # Containment: refuse to read or operate on a track outside the repo before consuming
    # ANY evidence -- this tool is part of the trust spine (hostile/absolute --track).
    if ATB_ROOT not in track.parents:
        print(
            f"error: track is not a directory below the repo ({ATB_ROOT}): {track}",
            file=sys.stderr,
        )
        return 1
    if not track.is_dir():
        print(f"error: track dir not found: {track}", file=sys.stderr)
        return 1

    delta = build_delta(
        track,
        now=_now(),
        max_future_seconds=args.max_future_seconds,
        max_age_hours=args.max_age_hours,
    )
    # The delta is always written track-local. The track is already resolved and contained
    # below ATB_ROOT, so the write is deterministic with a minimal blast radius (no --out
    # to anchor against cwd or point outside the track).
    out_path = track / "verification-delta.json"
    try:
        out_path.write_text(json.dumps(delta, indent=2) + "\n")
    except OSError as exc:
        print(f"error: cannot write {out_path}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "state": delta["state"],
                    "out": str(out_path),
                    "differences": len(delta["differences"]),
                    "job_id": delta["job_id"],
                },
                indent=2,
            )
        )
    else:
        print(f"[delta] {delta['state']} — {len(delta['differences'])} difference(s) -> {out_path}")
        for d in delta["differences"]:
            print(f"  {d['field']}: coder={d['coder_claim']} qa={d['qa_finding']}")

    return 0 if delta["state"] == "confirmed" else 2


if __name__ == "__main__":
    sys.exit(main())
