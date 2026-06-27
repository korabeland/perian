#!/usr/bin/env python3
"""Grounding-pack assembler for overseer agent-affecting decisions (meta-layer Phase 1).

WHY THIS EXISTS (the overseer charter's reflex #1 — "ground in memory + logs"):
Before an overseer (Claude Code or Codex) decides to rewrite an agent's identity / skills /
tools / context — or decides NOT to — that decision must be grounded in the agency's own
memory + logs, not from recall. This tool ASSEMBLES the fixed grounding bundle for one agent
into a single, content-addressed snapshot and returns a ``grounding_pack_id`` the
overseer-decision-record cites.

It is TRUST-ONLY: it gathers evidence; it never judges whether the grounding is adequate or
what to decide (that stays with Codex / the overseer / Korab). Schema-valid != sound; an
assembled pack != a good decision. The id is sha256 over the CONTENT of the sources only
(not the timestamp), so the same memory + identity state reproduces the same id — a cited
pack is re-verifiable.

The bundle (plan 2026-06-20-002 §3.A / 2026-06-20-001 §4):
  - over_fit_guard     : the operating-thesis memory (don't bake the narrow corpus envelope).
  - identity_first_law : the QA-calibration "set identity stance first" memory.
  - corpus_state       : the skill-curation / corpus-state memory.
  - current_identity   : the agent's live SOUL.md + its skills-directory listing.
  - prior_promotions   : the agent's SOUL/skill *.prebak.* trail (don't re-propose done work).
  - evidence           : caller-supplied detector refs (coach lesson / gate-held / class ids).

A missing OPTIONAL source (e.g. a memory file) is recorded in ``missing`` and the pack is
still produced (honest, no silent gap) — judging the gap is the overseer's job. A missing
agent SOUL is a usage error (you cannot ground a nonexistent agent).

Usage:
  python -m tools.grounding_pack --agent coder [--evidence REF ...] [--out-dir DIR] [--json]
  python -m tools.grounding_pack --self-test

Exit: 0 = pack assembled · 1 = usage / IO error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import paths

ATB_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ATB_ROOT / "overseer" / "grounding-packs"

# Where the agency's memory lives, and the role -> memory-slug map for the fixed bundle.
MEMORY_DIR = paths.claude_memory_dir()
PROFILES_DIR = paths.hermes_profiles()
MEMORY_ROLES = {
    "over_fit_guard": "project_atb_operating_thesis.md",
    "identity_first_law": "project_atb_idea_digest_probe.md",
    "corpus_state": "project_atb_skill_curation_plan.md",
}


def _read_text(path: Path) -> tuple[str | None, bool]:
    """Return (content, present). present=False on any read failure — never raises."""
    try:
        return path.read_text(), True
    except OSError:
        return None, False


def collect_sources(
    agent: str,
    memory_dir: Path = MEMORY_DIR,
    profiles_dir: Path = PROFILES_DIR,
    evidence: list[str] | None = None,
) -> dict:
    """Gather the fixed grounding bundle for ``agent`` into a content dict.

    Raises FileNotFoundError if the agent's SOUL.md cannot be found (you cannot ground a
    nonexistent agent). Optional sources that are missing are flagged, not fatal.
    """
    soul_path = profiles_dir / agent / "SOUL.md"
    soul_text, soul_present = _read_text(soul_path)
    if not soul_present:
        raise FileNotFoundError(f"agent SOUL not found: {soul_path}")

    sections: dict[str, dict] = {}
    missing: list[str] = []

    # Memory-backed roles.
    for role, slug in MEMORY_ROLES.items():
        p = memory_dir / slug
        text, present = _read_text(p)
        sections[role] = {"path": str(p), "present": present, "content": text or ""}
        if not present:
            missing.append(role)

    # Current identity: the live SOUL + the skills directory listing.
    skills_dir = profiles_dir / agent / "skills"
    try:
        skills = sorted(
            d.name for d in skills_dir.iterdir() if d.is_dir()
        ) if skills_dir.is_dir() else []
    except OSError:
        skills = []
    sections["current_identity"] = {
        "path": str(soul_path),
        "present": True,
        "content": soul_text or "",
        "skills_enabled_dirs": skills,
    }

    # Prior promotions: the SOUL/skill backup trail (names only — the trail, not the bytes).
    try:
        trail = sorted(
            p.name
            for p in (profiles_dir / agent).glob("SOUL.md.prebak.*")
        )
        trail += sorted(
            p.name for p in (profiles_dir / agent).glob("skills.prebak.*")
        )
    except OSError:
        trail = []
    sections["prior_promotions"] = {"path": str(profiles_dir / agent), "trail": trail}

    return {
        "agent": agent,
        "sections": sections,
        "evidence": list(evidence or []),
        "missing": missing,
    }


def build_pack(sources: dict) -> tuple[str, dict]:
    """Pure: derive a content-addressed pack id + the pack object from collected sources.

    The id hashes the CONTENT (agent + sections + evidence) only — NOT any timestamp — so the
    same grounding state reproduces the same id. ``missing`` is content too (it changes with
    the inputs), so it is hashed.
    """
    content_obj = {
        "agent": sources["agent"],
        "sections": sources["sections"],
        "evidence": sources["evidence"],
        "missing": sources["missing"],
    }
    digest = hashlib.sha256(
        json.dumps(content_obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    pack_id = f"gp-{digest}"
    pack = {"grounding_pack_id": pack_id, **content_obj}
    return pack_id, pack


def write_pack(pack: dict, out_dir: Path) -> Path:
    """Write the pack as <out_dir>/<id>.json (content-addressed; append-only by nature)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{pack['grounding_pack_id']}.json"
    stamped = {"created_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **pack}
    out_path.write_text(json.dumps(stamped, indent=2, ensure_ascii=False) + "\n")
    return out_path


def _self_test() -> int:
    """Pure-core checks: id is content-stable, agent-sensitive, evidence-sensitive."""
    failures: list[str] = []

    base = {
        "agent": "coder",
        "sections": {"over_fit_guard": {"present": True, "content": "thesis text"}},
        "evidence": ["coach:job1:r2"],
        "missing": [],
    }
    id1, _ = build_pack(base)
    id2, _ = build_pack(json.loads(json.dumps(base)))  # deep copy, same content
    if id1 != id2:
        failures.append(f"id not reproducible: {id1} != {id2}")

    diff_agent = {**base, "agent": "qa"}
    if build_pack(diff_agent)[0] == id1:
        failures.append("id should change with agent")

    diff_ev = {**base, "evidence": ["coach:job9:r1"]}
    if build_pack(diff_ev)[0] == id1:
        failures.append("id should change with evidence")

    if not id1.startswith("gp-"):
        failures.append(f"id missing gp- prefix: {id1}")

    if failures:
        print("grounding_pack SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("grounding_pack self-test: OK (reproducible, agent-sensitive, evidence-sensitive)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="grounding_pack",
        description="Assemble a content-addressed grounding pack for an overseer decision.",
    )
    ap.add_argument("--agent", help="crew agent to ground a decision about (coder/qa/main/dreamer/research)")
    ap.add_argument("--evidence", action="append", default=[], help="detector ref(s) that triggered this (repeatable)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="where to write the pack json")
    ap.add_argument("--json", action="store_true", help="print the full pack object")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if not args.agent:
        print("error: --agent is required (or use --self-test)", file=sys.stderr)
        return 1

    try:
        sources = collect_sources(args.agent, evidence=args.evidence)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    pack_id, pack = build_pack(sources)
    try:
        out_path = write_pack(pack, Path(args.out_dir))
    except OSError as exc:
        print(f"error: cannot write pack: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(pack, indent=2, ensure_ascii=False))
    else:
        print(f"[grounding] {pack_id} -> {out_path}")
        if pack["missing"]:
            print(f"  missing source(s): {', '.join(pack['missing'])} (flagged, not fatal)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
