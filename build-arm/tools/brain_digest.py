#!/usr/bin/env python3
"""Weekly batch-review digest for the ATB brain (gbrain contradiction probe).

Wraps ``gbrain eval suspected-contradictions`` (read-only; an LLM judge samples
retrieval results for a set of queries and flags pairs that contradict). The probe
NEVER auto-applies anything -- "the output is data, the operator decides"
(gbrain docs/contradictions.md). This tool just FORMATS the ProbeReport JSON into a
compact, paste-ready digest for a weekly Telegram cron. Korab reviews and runs any
``gbrain takes supersede`` / ``gbrain takes mark-debate`` commands he agrees with.

The probe's judge prompt is Claude-tuned: routing it to ``openrouter:openai/gpt-5.2``
parse-fails, so the judge is pinned to ``openrouter:anthropic/claude-haiku-4.5`` --
the Claude judge it expects (reached via OpenRouter).
The OpenRouter key is supplied by the wrapper script (scripts/atb_brain_digest.sh),
which exports it from the brain's gitignored config; gbrain reads OPENROUTER_API_KEY
from the environment.

Sibling to tools/gate_sweep.py (reuses its emit_event + stdout contract).
  exit 0 = digest emitted (clean or with findings), or benignly skipped (brain busy)
  exit 1 = probe / parse error (FAIL LOUD -- including schema_version drift)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ATB_ROOT = Path(__file__).resolve().parent.parent
EVENTS = ATB_ROOT / "state" / "events.jsonl"
GBRAIN = os.environ.get("GBRAIN_BIN", str(Path.home() / ".bun" / "bin" / "gbrain"))
QUERIES_FILE = ATB_ROOT / "tools" / "brain_probe_queries.txt"
# Claude-tuned judge, reached via OpenRouter.
JUDGE_MODEL = "openrouter:anthropic/claude-haiku-4.5"
EXPECTED_SCHEMA = 1
LOCK_MSG = "Timed out waiting for PGLite lock"
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}


def emit_event(payload: dict) -> None:
    if not EVENTS.parent.is_dir():
        return
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **payload}
    )
    with EVENTS.open("a") as fh:
        fh.write(line + "\n")


def run_probe(queries_file: Path, judge: str, budget_usd: float):
    cmd = [
        GBRAIN,
        "eval",
        "suspected-contradictions",
        "--queries-file",
        str(queries_file),
        "--judge",
        judge,
        "--budget-usd",
        str(budget_usd),
        "--yes",
        "--json",
    ]
    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GBRAIN_HOME": str(ATB_ROOT)},
    )
    return cp.returncode, cp.stdout, cp.stderr


def _findings(report: dict) -> list[dict]:
    """Flatten actionable findings from per_query[].contradictions[], severity-sorted."""
    out: list[dict] = []
    for pq in report.get("per_query", []) or []:
        for f in pq.get("contradictions", []) or []:
            if f.get("verdict") in (None, "no_contradiction"):
                continue
            out.append({**f, "query": pq.get("query", "")})
    out.sort(key=lambda f: SEVERITY_RANK.get(f.get("severity", "info"), 9))
    return out


def format_digest(report: dict) -> str:
    """Pure formatter: ProbeReport JSON -> compact paste-ready digest. Testable."""
    sv = report.get("schema_version")
    if sv != EXPECTED_SCHEMA:
        raise ValueError(
            f"probe schema_version={sv!r}, expected {EXPECTED_SCHEMA} "
            f"(gbrain bumped the envelope -- update brain_digest.py before trusting it)"
        )

    q = report.get("queries_evaluated", 0)
    n_contra = report.get("total_contradictions_flagged", 0)
    q_with = report.get("queries_with_contradiction", 0)
    cal = report.get("calibration", {}) or {}
    ci = cal.get("wilson_ci_95", {}) or {}
    errs = report.get("judge_errors", {}) or {}
    cost = report.get("cost_usd", {})
    cost_total = cost.get("total", 0) if isinstance(cost, dict) else (cost or 0)
    hot = report.get("hot_pages", []) or []
    findings = _findings(report)

    lines = ["ATB brain - weekly contradiction digest"]
    plural = "y" if q_with == 1 else "ies"
    lines.append(
        f"queries={q}  contradictions={n_contra} (in {q_with} quer{plural})  "
        f"cost=${cost_total:.4f}"
    )
    pt, lo, hi = ci.get("point"), ci.get("lower"), ci.get("upper")
    if pt is not None and lo is not None and hi is not None:
        lines.append(f"rate={pt:.0%}  CI95=[{lo:.0%}, {hi:.0%}]")
    if cal.get("small_sample_note"):
        lines.append(f"NOTE: {cal['small_sample_note']}")
    if errs.get("total"):
        lines.append(
            f"WARN judge errors: {errs.get('total')} "
            f"(parse_fail={errs.get('parse_fail', 0)} timeout={errs.get('timeout', 0)} "
            f"http_5xx={errs.get('http_5xx', 0)} refusal={errs.get('refusal', 0)})"
        )

    if not findings:
        lines += ["", "OK nothing to review - no contradictions surfaced."]
        return "\n".join(lines)

    lines += [
        "",
        f"Findings ({len(findings)}), severity-ranked - run the fixes you agree with:",
    ]
    for i, f in enumerate(findings, 1):
        a = (f.get("a") or {}).get("slug", "?")
        b = (f.get("b") or {}).get("slug", "?")
        sev = f.get("severity", "?")
        verdict = f.get("verdict", "?")
        axis = f.get("axis", "")
        lines.append(
            f"{i}. [{sev}] {verdict}: {a}  <->  {b}"
            + (f"  (axis: {axis})" if axis else "")
        )
        cmd = f.get("resolution_command")
        if cmd:
            lines.append(f"     fix: {cmd}")

    if hot:
        top = sorted(
            hot, key=lambda h: SEVERITY_RANK.get(h.get("max_severity", "info"), 9)
        )[:5]
        lines += ["", "Hot pages (most contradiction-prone):"]
        for h in top:
            lines.append(
                f"  - {h.get('slug', '?')} "
                f"(max={h.get('max_severity', '?')}, x{h.get('appearances', 0)})"
            )

    lines += [
        "",
        "(The probe never auto-applies -- you decide. Supersede: "
        "gbrain takes supersede <id> --by <id>; flag a debate: gbrain takes mark-debate <id>.)",
    ]
    return "\n".join(lines)


def _self_test() -> int:
    sample = {
        "schema_version": 1,
        "queries_evaluated": 30,
        "queries_with_contradiction": 1,
        "total_contradictions_flagged": 1,
        "cost_usd": {"judge": 0.07, "embedding": 0.002, "total": 0.072},
        "calibration": {
            "wilson_ci_95": {"point": 0.033, "lower": 0.006, "upper": 0.168},
            "small_sample_note": "n=30 is borderline.",
        },
        "judge_errors": {
            "total": 0,
            "parse_fail": 0,
            "timeout": 0,
            "http_5xx": 0,
            "refusal": 0,
        },
        "hot_pages": [{"slug": "canon/mrr", "appearances": 2, "max_severity": "high"}],
        "per_query": [
            {
                "query": "what is MRR",
                "contradictions": [
                    {
                        "verdict": "contradiction",
                        "severity": "high",
                        "axis": "revenue",
                        "resolution_kind": "supersede",
                        "resolution_command": "gbrain takes supersede 42 --by 99",
                        "a": {"slug": "canon/mrr"},
                        "b": {"slug": "chat/2024-call"},
                    },
                    {
                        "verdict": "no_contradiction",
                        "severity": "info",
                        "a": {},
                        "b": {},
                    },
                ],
            }
        ],
    }
    out = format_digest(sample)
    assert "Findings (1)" in out, out
    assert "canon/mrr  <->  chat/2024-call" in out, out
    assert "gbrain takes supersede 42 --by 99" in out, out
    assert "[high] contradiction" in out, out
    assert "Hot pages" in out, out

    clean = dict(
        sample,
        per_query=[],
        hot_pages=[],
        total_contradictions_flagged=0,
        queries_with_contradiction=0,
    )
    assert "nothing to review" in format_digest(clean)

    try:
        format_digest({"schema_version": 2})
    except ValueError:
        pass
    else:
        print("[self-test] FAIL: schema drift not caught", file=sys.stderr)
        return 1

    print(
        "[brain-digest self-test] OK - findings render, clean renders, schema drift fails loud."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="brain_digest", description="Weekly ATB brain contradiction digest."
    )
    ap.add_argument("--queries-file", default=str(QUERIES_FILE))
    ap.add_argument("--judge", default=JUDGE_MODEL)
    ap.add_argument("--budget-usd", type=float, default=1.0)
    ap.add_argument("--emit-event", action="store_true")
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="render synthetic envelopes and exit (no probe call)",
    )
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    qf = Path(args.queries_file)
    if not qf.is_file():
        print(f"[brain-digest] ERROR: queries file not found: {qf}", file=sys.stderr)
        return 1

    rc, out, err = run_probe(qf, args.judge, args.budget_usd)
    if LOCK_MSG in (out + err):
        # The probe writes a judge cache (a WRITE); if a worker holds the lock, skip benignly.
        print(
            "OK ATB brain digest skipped - brain busy (PGLite lock); retry next week."
        )
        if args.emit_event:
            emit_event({"event": "brain-digest-skip", "reason": "pglite-lock"})
        return 0

    try:
        report = json.loads(out)
    except json.JSONDecodeError:
        print(
            f"[brain-digest] ERROR: probe returned no JSON (rc={rc}).", file=sys.stderr
        )
        print((err or out or "")[-800:], file=sys.stderr)
        if args.emit_event:
            emit_event({"event": "brain-digest-error", "reason": "non-json", "rc": rc})
        return 1

    try:
        digest = format_digest(report)
    except ValueError as exc:
        print(f"[brain-digest] ERROR: {exc}", file=sys.stderr)
        if args.emit_event:
            emit_event({"event": "brain-digest-error", "reason": str(exc)})
        return 1

    print(digest)
    if args.emit_event:
        emit_event(
            {
                "event": "brain-digest",
                "queries": report.get("queries_evaluated", 0),
                "contradictions": report.get("total_contradictions_flagged", 0),
                "judge_errors": (report.get("judge_errors") or {}).get("total", 0),
            }
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
