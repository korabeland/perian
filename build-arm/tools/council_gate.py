#!/usr/bin/env python3
"""Deterministic council-trigger gate for Auto-think/Auto-build.

Reads an idea-contract.json and decides — with NO model in the loop — whether
Main must request an independent council before approving. This is the robust
form of the checklist in profiles/main/SOUL.md: the council-firing decision is a
pure function of the idea-contract's declared fields, never a model's judgment.

Usage:
  python -m tools.council_gate <track-dir | idea-contract.json> [--json] [--emit-event]

Behavior:
  - Silent on stdout + exit 0 when NO council is needed (cron-friendly).
  - Prints "[COUNCIL-NEEDED] <job_id>" + reasons + exit 2 when a council should fire.
  - --json   : always print a JSON result object (exit code unchanged).
  - --emit-event : on a trip, append a `council-requested` line to state/events.jsonl.
  - exit 1   : usage / IO / parse error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Curated risk-surface markers, prefix-matched at a word boundary (see _KW_RE below).
# Deliberately NOT bare "schema" (too broad — benign tooling mentions schemas);
# real schema risk is captured by "schema change" / "migration" / "alter table".
# Bare "auth" is excluded — it prefix-collides with "author"; "authentication" /
# "oauth" / "login" / "credential" cover the auth surface instead.
# Bare "token" is excluded (Korab call, 2026-06-12) — it false-positive-parks any idea
# whose domain vocabulary includes tokens/sec or token counts (local LLM serving, eval
# harnesses); the credential-token surface is covered by the explicit multi-word
# phrases below (session/api/access/bearer/auth/refresh/id token, jwt) instead.
RISK_KEYWORDS = [
    "authentication",
    "login",
    "oauth",
    "session token",
    "api token",
    "access token",
    "bearer token",
    "auth token",
    "refresh token",
    "id token",
    "jwt",
    "secret",
    "credential",
    "password",
    "api key",
    "private key",
    "payment",
    "billing",
    "charge",
    "invoice",
    "refund",
    "stripe",
    "paypal",
    "delete",
    "drop table",
    "truncate",
    "destroy",
    "purge",
    "wipe",
    "rm -rf",
    "migrate",
    "migration",
    "alter table",
    "schema change",
    "backfill",
    "external api",
    "third-party",
    "scrape",
    "crawl",
    "webhook",
    "send email",
    "send message",
    "send sms",
    "notify users",
    "production",
    "deploy",
    "irreversible",
    "destructive",
    "pii",
    "personal data",
    "user data",
    "customer data",
]
# Prefix match at a word boundary so inflections trip (delete/deletes/deleting,
# deploy/deployment, api token/API tokens) without matching unrelated words.
# Multi-word keywords tolerate hyphen/underscore separators (api-token, refresh_token).
_KW_RE = {
    kw: re.compile(
        r"\b" + r"[\s_\-]+".join(re.escape(w) for w in kw.split()) + r"\w*",
        re.IGNORECASE,
    )
    for kw in RISK_KEYWORDS
}


def evaluate(contract: dict) -> list[str]:
    """Return the list of tripped-rule reasons. Empty list => no council needed."""
    reasons: list[str] = []

    for field in ("what_should_exist", "where_it_lives"):
        text = contract.get(field) or ""
        for kw, rx in _KW_RE.items():
            if rx.search(text):
                reasons.append(f"risk keyword '{kw}' in {field}")

    scope = contract.get("out_of_scope") or []
    if not [s for s in scope if isinstance(s, str) and len(s.strip()) >= 5]:
        reasons.append("unbounded scope (out_of_scope empty or trivial)")

    refs = contract.get("evidence_refs") or []
    if len([r for r in refs if isinstance(r, str) and r.strip()]) < 2:
        reasons.append(f"thin evidence ({len(refs)} evidence_refs, need >=2)")

    verifiable = (contract.get("how_verifiable") or "").strip()
    if len(verifiable) < 20 or verifiable.lower() in {"tbd", "n/a", "none", "unknown"}:
        reasons.append("unmeasurable (how_verifiable missing or vague)")

    return reasons


def resolve_contract_path(arg: str) -> Path:
    p = Path(arg)
    return p / "idea-contract.json" if p.is_dir() else p


def emit_event(contract_path: Path, job_id: str) -> Path | None:
    """Append a council-requested event to state/events.jsonl, if reachable."""
    # contract_path = .../state/tracks/<job>/idea-contract.json -> state/events.jsonl
    events = contract_path.parent.parent.parent / "events.jsonl"
    if not events.parent.is_dir():
        return None
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "job_id": job_id,
            "profile": "main",
            "event": "council-requested",
            "artifact": "idea-contract.json",
        }
    )
    with events.open("a") as fh:
        fh.write(line + "\n")
    return events


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic council-trigger gate.")
    ap.add_argument("target", help="track directory or path to idea-contract.json")
    ap.add_argument("--json", action="store_true", help="always print a JSON result")
    ap.add_argument(
        "--emit-event",
        action="store_true",
        help="on a trip, append council-requested to state/events.jsonl",
    )
    args = ap.parse_args(argv)

    path = resolve_contract_path(args.target)
    try:
        contract = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"error: idea-contract not found at {path}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, OSError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 1

    job_id = contract.get("job_id", "<unknown>")
    reasons = evaluate(contract)
    needed = bool(reasons)

    emitted = None
    if needed and args.emit_event:
        emitted = emit_event(path, job_id)

    if args.json:
        print(
            json.dumps(
                {
                    "council_needed": needed,
                    "job_id": job_id,
                    "reasons": reasons,
                    "event_appended_to": str(emitted) if emitted else None,
                },
                indent=2,
            )
        )
    elif needed:
        print(f"[COUNCIL-NEEDED] {job_id}")
        for r in reasons:
            print(f"  - {r}")
        if emitted:
            print(f"  -> council-requested appended to {emitted}")

    return 2 if needed else 0


if __name__ == "__main__":
    sys.exit(main())
