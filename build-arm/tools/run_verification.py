#!/usr/bin/env python3
"""Tool-captured verification for ATB builds (post-mortem E4).

Runs ``product-plan.verification_commands`` in the build worktree, captures the REAL
exit codes, and writes a schema-conformant ``verification.json`` (verifier ``coder``)
or ``qa-verification.json`` (verifier ``qa``). The pass/fail comes from the captured
exit codes, NOT an agent's free-text claim.

Why this exists: both 2026-06-02 councils flagged that a completeness gate alone is
theater — the one real run's QA wrote "exit code 0, 2 tests passed" as a prose JSON
field while the coder produced no verification.json at all. A gate that only checks an
artifact EXISTS and is SHAPED right still passes a well-formed lie. So the truth must
be produced by a tool actually running the tests. When invoked by the deterministic
sweep (see tools/gate_sweep.py diff gate), the agent never touches the result -> the
pass is forgery-proof.

Usage:
  python -m tools.run_verification --track <dir> --worktree <dir>
      [--verifier coder|qa] [--timeout SECONDS] [--out PATH] [--json] [--self-test]

Exit: 0 = all verification_commands passed · 2 = a command failed / none defined ·
      1 = usage / IO error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ATB_ROOT = Path(__file__).resolve().parent.parent
EXCERPT = (
    2000  # max chars of stdout/stderr captured per command (keep artifact bounded)
)
ARTIFACT_BY_VERIFIER = {"coder": "verification.json", "qa": "qa-verification.json"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_commands(commands, worktree: Path, timeout: int):
    """Run each shell command in the worktree, capturing real exit codes.

    Returns (outputs, all_ok). all_ok is False if any command exits non-zero or times out.
    """
    outputs = []
    all_ok = True
    for cmd in commands:
        try:
            cp = subprocess.run(
                cmd,
                shell=True,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            rc = cp.returncode
            out, err = cp.stdout or "", cp.stderr or ""
        except subprocess.TimeoutExpired:
            rc = 124
            out, err = "", f"TIMEOUT after {timeout}s"
        outputs.append(
            {
                "command": cmd,
                "exit_code": rc,
                "stdout_excerpt": out[-EXCERPT:],
                "stderr_excerpt": err[-EXCERPT:],
            }
        )
        if rc != 0:
            all_ok = False
    return outputs, all_ok


def build_report(track: Path, worktree: Path, verifier: str, timeout: int):
    """Produce a verification report dict (or (None, error_str))."""
    pp_path = track / "product-plan.json"
    if not pp_path.is_file():
        return None, f"no product-plan.json in {track}"
    try:
        pp = json.loads(pp_path.read_text())
    except json.JSONDecodeError as exc:
        return None, f"product-plan.json is not valid JSON: {exc}"

    commands = pp.get("verification_commands") or []
    job_id = pp.get("job_id") or track.name
    outputs, all_ok = (
        run_commands(commands, worktree, timeout) if commands else ([], False)
    )
    passed = bool(commands) and all_ok
    report = {
        "job_id": job_id,
        "ran_at": _now(),
        "commands_run": list(commands),
        "outputs": outputs,
        "pass": passed,
        "evidence_refs": [],
        "verifier": verifier,
        "notes": (
            ""
            if commands
            else "no verification_commands in product-plan.json — nothing to verify (pass=false)"
        ),
    }
    return report, None


def _self_test() -> int:
    """Synthetic pass-case, fail-case, schema-conformance, and empty-commands checks."""
    from tools.validator import schema_loader, validator as _vd

    failures = []
    tmp = Path(tempfile.mkdtemp(prefix="atb-runverif-"))

    def _write_plan(commands):
        (tmp / "product-plan.json").write_text(
            json.dumps(
                {
                    "job_id": "selftest",
                    "allowed_paths": ["x"],
                    "planned_files": [],
                    "non_goals": [],
                    "verification_commands": commands,
                    "acceptance_checks": [],
                    "risk_assessment": "n/a",
                }
            )
        )

    try:
        # pass case: exit 0
        _write_plan(['python3 -c "import sys; sys.exit(0)"'])
        rep, err = build_report(tmp, tmp, "coder", 60)
        assert err is None, err
        if rep["pass"] is not True:
            failures.append("expected pass=True for an exit-0 command")
        # produced artifact must validate against verification.schema.json
        (tmp / "verification.json").write_text(json.dumps(rep))
        schema = schema_loader.load_schema("verification")
        res = _vd.validate_artifact(str(tmp / "verification.json"), schema)
        if not res.get("pass_"):
            failures.append(
                f"produced verification.json failed schema: {res.get('errors')}"
            )

        # fail case: exit 1, verifier threaded
        _write_plan(['python3 -c "import sys; sys.exit(1)"'])
        rep2, _ = build_report(tmp, tmp, "qa", 60)
        if rep2["pass"] is not False:
            failures.append("expected pass=False for an exit-1 command")
        if rep2["verifier"] != "qa":
            failures.append("verifier not threaded into the report")

        # empty-commands case: cannot verify -> pass=False (not a vacuous pass)
        _write_plan([])
        rep3, _ = build_report(tmp, tmp, "coder", 60)
        if rep3["pass"] is not False:
            failures.append(
                "expected pass=False when there are no verification_commands"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("run_verification SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(
        "run_verification self-test: OK (pass-case, fail-case, schema-conformance, empty-commands)"
    )
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_verification",
        description="Tool-captured ATB build verification (E4).",
    )
    ap.add_argument("--track", help="track dir holding product-plan.json")
    ap.add_argument("--worktree", help="build dir to run verification_commands in")
    ap.add_argument("--verifier", choices=["coder", "qa"], default="coder")
    ap.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="per-command timeout seconds (default 600)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="output path (default: <track>/<verification|qa-verification>.json)",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.track or not args.worktree:
        print(
            "error: --track and --worktree are required (or use --self-test)",
            file=sys.stderr,
        )
        return 1

    track = Path(args.track)
    if not track.is_absolute():
        track = (ATB_ROOT / track).resolve()
    worktree = Path(args.worktree).resolve()
    if not track.is_dir():
        print(f"error: track dir not found: {track}", file=sys.stderr)
        return 1
    if not worktree.is_dir():
        print(f"error: worktree dir not found: {worktree}", file=sys.stderr)
        return 1

    report, err = build_report(track, worktree, args.verifier, args.timeout)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    out_path = (
        Path(args.out) if args.out else track / ARTIFACT_BY_VERIFIER[args.verifier]
    )
    out_path.write_text(json.dumps(report, indent=2) + "\n")

    if args.json:
        print(
            json.dumps(
                {
                    "pass": report["pass"],
                    "out": str(out_path),
                    "commands": len(report["commands_run"]),
                    "failed": [
                        o["command"] for o in report["outputs"] if o["exit_code"] != 0
                    ],
                },
                indent=2,
            )
        )
    else:
        status = "PASS" if report["pass"] else "FAIL"
        print(
            f"[verify:{args.verifier}] {status} — {len(report['commands_run'])} command(s) -> {out_path}"
        )
        for o in report["outputs"]:
            if o["exit_code"] != 0:
                print(f"  FAIL (rc={o['exit_code']}): {o['command']}")

    return 0 if report["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
