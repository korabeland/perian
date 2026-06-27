#!/usr/bin/env python3
"""brief_inbox — enumerate the idea briefs awaiting the operator's eyes.

The think loop writes a durable per-idea brief to ``state/digests/briefs/<job_id>.md``
(see ``think_sweep._digest``). This module is the read-only SEAM over those briefs: it lists
which APPROVED ideas are still PENDING — main approved the idea (``decision ==
"approved_for_coder"``) but no build workspace has been created for it yet
(``state/builds/<job_id>/`` absent) — so a human-facing surface can show them on demand. It is
the unit a future daily-morning-brief cron incorporates; Pascal can also call it on request
("any new idea briefs?"). Delivery is PULL, not push — this module never notifies.

A brief for a NON-approved outcome (a rejected or parked needs_more idea can also reach the
digest stage) is listed under ``--all`` but is never reported as pending: pending means
"awaiting your decision to build", which only applies to an approved-but-not-yet-built idea.

Read-only. Standard library only. TOTAL: a missing/unreadable/oversized/non-UTF-8/malformed or
non-dict track artifact is treated as blank, never raised; all display/sort fields are coerced
to strings so a schema-loose value can never make the sort or formatter raise. Deterministic:
briefs are returned newest-first by proposed_at, ties broken by job_id.

CLI:
    python -m tools.brief_inbox            # human summary of PENDING (approved, unbuilt) briefs
    python -m tools.brief_inbox --all      # every brief, with a status tag
    python -m tools.brief_inbox --json     # machine-readable (pending unless --all)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ATB_ROOT = Path(__file__).resolve().parent.parent
_SNIPPET = 160
_MAX_JSON = 1_000_000  # defensive cap (bytes) on a track artifact we will parse
_APPROVED = "approved_for_coder"


def _s(x: object) -> str:
    """A string for display/sort; a non-str becomes '' so sorting + formatting stay total."""
    return x if isinstance(x, str) else ""


def _load(path: Path) -> dict:
    """Parsed JSON dict, or {} on ANY problem — missing, unreadable, oversized, non-UTF-8,
    invalid JSON, or non-dict. Never raises. (json.JSONDecodeError and UnicodeDecodeError are
    both ValueError subclasses, so (OSError, ValueError) covers read + decode + parse.)"""
    try:
        if path.stat().st_size > _MAX_JSON:
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _short(text: object) -> str:
    """One-line snippet of what_should_exist, truncated for a list view."""
    s = " ".join(_s(text).split())
    return s if len(s) <= _SNIPPET else s[: _SNIPPET - 1] + "…"


def _under(root_resolved: Path, path: Path) -> bool:
    """True iff path (symlinks resolved) stays within root — guards against a track dir that
    is a symlink pointing outside the repo."""
    try:
        rp = path.resolve()
    except OSError:
        return False
    return rp == root_resolved or root_resolved in rp.parents


def list_briefs(root: Path = ATB_ROOT) -> list[dict]:
    """Every durable per-idea brief on disk, annotated from its track + build state.

    ``approved`` mirrors main's decision; ``built`` means a build workspace exists; ``pending``
    (awaiting the operator's build decision) = approved AND not built."""
    root = Path(root)
    root_r = root.resolve()
    briefs_dir = root / "state" / "digests" / "briefs"
    builds_dir = root / "state" / "builds"
    tracks_dir = root / "state" / "tracks"
    out: list[dict] = []
    if not briefs_dir.is_dir():
        return out
    for brief in sorted(briefs_dir.glob("*.md")):
        job_id = brief.stem
        track = tracks_dir / job_id
        contract = _load(track / "idea-contract.json") if _under(root_r, track) else {}
        review = _load(track / "main-review.json") if _under(root_r, track) else {}
        decision = _s(review.get("decision")) or _s(contract.get("decision"))
        built = (builds_dir / job_id).is_dir()
        approved = decision == _APPROVED
        out.append(
            {
                "job_id": job_id,
                "proposed_at": _s(contract.get("proposed_at")),
                "decision": decision,
                "risk_band": _s(review.get("risk_band")),
                "what_should_exist": _short(contract.get("what_should_exist", "")),
                "brief_path": str(brief),
                "built": built,
                "approved": approved,
                "pending": approved and not built,
            }
        )
    out.sort(key=lambda b: (b["proposed_at"], b["job_id"]), reverse=True)
    return out


def pending_briefs(root: Path = ATB_ROOT) -> list[dict]:
    """Approved ideas not yet taken to build — those awaiting the operator's decision."""
    return [b for b in list_briefs(root) if b["pending"]]


def _tag(b: dict) -> str:
    if b.get("built"):
        return "  [built]"
    if not b.get("approved"):
        return f"  [{b.get('decision') or 'no-decision'}]"
    return ""


def format_human(briefs: list[dict]) -> str:
    """A skimmable, plain-text summary. Total — safe on any list of brief dicts."""
    if not briefs:
        return "No idea briefs awaiting review.\n"
    lines = [f"Idea briefs awaiting review ({len(briefs)}):", ""]
    for b in briefs:
        lines.append(f"  - {b.get('job_id', '?')}{_tag(b)}")
        meta = " · ".join(
            x for x in (
                f"proposed {b['proposed_at']}" if b.get("proposed_at") else "",
                b.get("decision", ""),
                f"risk {b['risk_band']}" if b.get("risk_band") else "",
            ) if x
        )
        if meta:
            lines.append(f"      {meta}")
        if b.get("what_should_exist"):
            lines.append(f"      {b['what_should_exist']}")
        lines.append(f"      brief: {b.get('brief_path', '')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="brief_inbox",
        description="List idea briefs awaiting the operator's review (pending unless --all).",
    )
    ap.add_argument("--all", action="store_true", help="include built + non-approved briefs")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON")
    ap.add_argument("--root", default=str(ATB_ROOT), help="ATB root (default: this repo)")
    args = ap.parse_args(argv)

    root = Path(args.root)
    briefs = list_briefs(root) if args.all else pending_briefs(root)
    if args.json:
        sys.stdout.write(json.dumps(briefs, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(format_human(briefs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
