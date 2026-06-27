#!/usr/bin/env python3
"""idea_digest — deterministic renderer of idea contracts into a digest document.

Graduated from build #4 (state/builds/20260603-idea-digest/, confirmed, 59 tests) into the
trust-spine tools/ dir, UNCHANGED in behavior. Two source modules — idea_brief.render_brief
and idea_digest.assemble_digest — are merged here verbatim so the digest is a single
self-contained module (no cross-file relative import). The companion build #4 file
check_sanitized.py is 0 bytes (there was never a reusable sanitized-check to graduate); the
sanitized gate lives in tools/think_gates.is_sanitized, where the gates need it.

This is the T4 renderer: given (idea_contract, research_input, main_review) triples that the
sweep has already re-verified against the M2 anchors (every snapshot hash == manifest, every
research-input hash == its flesh-out ledger anchor), it assembles a deterministic markdown
digest. The anchor re-verification itself is think_gates.verify_refs, called by the sweep
BEFORE this renderer runs — the renderer stays a pure, total formatter.

Public API:
    render_brief(idea_contract, research_input=None, main_review=None) -> str
    assemble_digest(triples) -> str

Both are TOTAL: malformed input is skipped-and-noted (briefs) or skipped-and-counted
(digest), never raised.
"""

from __future__ import annotations

import argparse
import json
import sys


# ======================================================================================
# render_brief — one idea contract -> a markdown brief (graduated verbatim from build #4)
# ======================================================================================
def _safe_get(d, key, default=None):
    """Return d[key] if d is a dict and has the key; else default."""
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def _is_dict_with_keys(d, required_keys):
    """Return True if d is a dict and all required_keys are present."""
    return isinstance(d, dict) and all(k in d for k in required_keys)


def _render_definition_of_success(idea_contract):
    """Render the 'Definition of success' section."""
    how_verifiable = _safe_get(idea_contract, "how_verifiable", "")
    what_should_exist = _safe_get(idea_contract, "what_should_exist", "")

    lines = [
        "## Definition of success",
        "",
    ]
    if how_verifiable:
        lines.append(f"**How success is measured:** {how_verifiable}")
    else:
        lines.append("**How success is measured:** *(not specified — skipped)*")

    if what_should_exist:
        lines.append("")
        lines.append(f"**What should exist:** {what_should_exist}")

    return "\n".join(lines) + "\n"


def _render_problem_value(idea_contract):
    """Render the 'Problem value' section."""
    who_benefits = _safe_get(idea_contract, "who_benefits", "")
    why_now = _safe_get(idea_contract, "why_now", "")

    lines = [
        "## Problem value",
        "",
    ]
    if who_benefits:
        lines.append(f"**Who benefits:** {who_benefits}")
    else:
        lines.append("**Who benefits:** *(not specified)*")

    if why_now:
        lines.append("")
        lines.append(f"**Why now:** {why_now}")

    return "\n".join(lines) + "\n"


def _render_evidence_strength(idea_contract, research_input):
    """Render 'Evidence strength', tying the evidence to the claim it supports.

    Leads with the idea's central claim (what_should_exist) and lists each
    evidence item directly beneath it, so evidence renders adjacent to the claim
    it is meant to support rather than as a detached dump. This is what lets a
    reviewer judge whether the evidence actually backs the claim.
    """
    lines = [
        "## Evidence strength",
        "",
    ]

    what_should_exist = _safe_get(idea_contract, "what_should_exist", "")
    if what_should_exist:
        lines.append(f"**Claim under evaluation:** {what_should_exist}")
        lines.append("")

    evidence = _safe_get(research_input, "evidence", [])
    bullets = []
    if isinstance(evidence, list):
        for item in evidence:
            if not isinstance(item, dict):
                continue
            claim = _safe_get(item, "claim", "(no claim)")
            source_ref = _safe_get(item, "source_ref", "")
            confidence = _safe_get(item, "confidence", None)

            conf_label = f" [{confidence}]" if confidence else ""
            source_line = f" (source: {source_ref})" if source_ref else ""

            bullets.append(f"- {claim}{conf_label}{source_line}")

    # Emit the explicit no-evidence line whenever there is nothing renderable —
    # an empty list, a non-list value, OR a list whose items are all malformed —
    # so the section is never a "Supporting evidence:" heading with no bullets.
    if not bullets:
        lines.append("*no evidence*")
        return "\n".join(lines) + "\n"

    lines.append("**Supporting evidence:**")
    lines.extend(bullets)

    return "\n".join(lines) + "\n"


def _render_feasibility_fit_envelope(idea_contract, research_input):
    """Render the 'Feasibility / fit-envelope' section."""
    out_of_scope = _safe_get(idea_contract, "out_of_scope", [])

    lines = [
        "## Feasibility / fit-envelope",
        "",
    ]

    if isinstance(out_of_scope, list) and len(out_of_scope) > 0:
        lines.append("**Out of scope:**")
        for item in out_of_scope:
            if isinstance(item, str):
                lines.append(f"- {item}")
    else:
        lines.append("*no out-of-scope items specified*")

    watch_items = _safe_get(research_input, "watch_items", [])
    if isinstance(watch_items, list) and len(watch_items) > 0:
        lines.append("")
        lines.append("**Watch items:**")
        for wi in watch_items:
            if isinstance(wi, dict):
                obs = _safe_get(wi, "observation", "(no observation)")
                rat = _safe_get(wi, "rationale", "")
                line = f"- {obs}"
                if rat:
                    line += f" — {rat}"
                lines.append(line)
            elif isinstance(wi, str):
                lines.append(f"- {wi}")

    return "\n".join(lines) + "\n"


def _render_differentiation(idea_contract):
    """Render the 'Differentiation' section."""
    where_it_lives = _safe_get(idea_contract, "where_it_lives", "")

    lines = [
        "## Differentiation",
        "",
    ]
    if where_it_lives:
        lines.append(f"**Where it lives:** {where_it_lives}")
    else:
        lines.append("*location not specified*")

    return "\n".join(lines) + "\n"


def _render_effort_vs_payoff(idea_contract):
    """Render the 'Effort vs payoff' section."""
    evidence_refs = _safe_get(idea_contract, "evidence_refs", [])

    lines = [
        "## Effort vs payoff",
        "",
    ]
    if isinstance(evidence_refs, list) and len(evidence_refs) > 0:
        lines.append("**Evidence references:**")
        for ref in evidence_refs:
            if isinstance(ref, str):
                lines.append(f"- {ref}")
    else:
        lines.append("*no evidence references provided*")

    return "\n".join(lines) + "\n"


def _render_end_state_verifiability(idea_contract):
    """Render the 'End-state verifiability' section."""
    how_verifiable = _safe_get(idea_contract, "how_verifiable", "")

    lines = [
        "## End-state verifiability",
        "",
    ]
    if how_verifiable:
        lines.append(how_verifiable)
    else:
        lines.append("*not specified*")

    return "\n".join(lines) + "\n"


def render_brief(idea_contract, research_input=None, main_review=None):
    """Render a deterministic markdown brief from chain artifacts.

    Args:
        idea_contract: dict with keys like what_should_exist, who_benefits,
                       why_now, evidence_refs, out_of_scope, where_it_lives,
                       how_verifiable.
        research_input: dict with keys like evidence, watch_items, summary.
                        May be None or a non-dict (total function).
        main_review: dict with keys like decision, risk_band, risk_score.
                     May be None or a non-dict (total function).

    Returns:
        A markdown string. If inputs are malformed, the brief notes skipped
        sections instead of raising an exception.
    """
    parts = []
    skipped = []

    # --- Header (short title + job id only; the end-state leads below) ---
    job_id = _safe_get(idea_contract, "job_id", "")
    proposed_at = _safe_get(idea_contract, "proposed_at", "")

    parts.append("# Idea Brief")
    parts.append("")
    if job_id:
        parts.append(f"**Job:** {job_id}")
    else:
        skipped.append("idea_contract.job_id")

    if proposed_at:
        parts.append(f"**Proposed at:** {proposed_at}")

    # Skip-and-note any missing required idea_contract fields explicitly, rather
    # than silently rendering a partial brief as if it were complete.
    required_fields = (
        "proposed_at", "what_should_exist", "who_benefits", "why_now",
        "evidence_refs", "out_of_scope", "where_it_lives", "how_verifiable",
    )
    if isinstance(idea_contract, dict):
        missing = [f for f in required_fields if f not in idea_contract]
        if missing:
            skipped.append(
                "idea_contract missing required field(s): " + ", ".join(missing)
            )

    # --- Definition of success LEADS (the brief's spine) ---
    # Renders whenever idea_contract is a dict (the renderer notes missing fields
    # inline) and is skip-noted otherwise, so a malformed contract never silently
    # drops the lead section.
    if isinstance(idea_contract, dict):
        parts.append(_render_definition_of_success(idea_contract))
    else:
        skipped.append("idea_contract is not a dict")

    # --- Main's decision: rendered AFTER the end-state, never before it ---
    if _is_dict_with_keys(main_review, ["decision", "risk_band"]):
        decision = main_review.get("decision", "")
        risk_band = main_review.get("risk_band", "")
        parts.append(f"**Decision:** {decision}  ")
        parts.append(f"**Risk band:** {risk_band}")

        risk_score = _safe_get(main_review, "risk_score")
        if risk_score is not None:
            parts.append(f"**Risk score:** {risk_score}")

        block_reason = _safe_get(main_review, "block_reason")
        if block_reason:
            parts.append(f"**Block reason:** {block_reason}")
        parts.append("")
    else:
        skipped.append("main_review missing or non-dict")

    if isinstance(idea_contract, dict):
        parts.append(_render_problem_value(idea_contract))

    # Evidence strength is a rubric-floor section: it ALWAYS renders (with an
    # explicit no-evidence line) even when research_input is missing, so the brief
    # never silently drops a rubric criterion. The absence is skip-noted, not hidden.
    if research_input is None or not isinstance(research_input, dict):
        skipped.append("research_input missing or non-dict")
    parts.append(_render_evidence_strength(idea_contract, research_input))

    if isinstance(idea_contract, dict):
        parts.append(_render_feasibility_fit_envelope(idea_contract, research_input))

    if isinstance(idea_contract, dict):
        parts.append(_render_differentiation(idea_contract))

    if isinstance(idea_contract, dict):
        parts.append(_render_effort_vs_payoff(idea_contract))

    if isinstance(idea_contract, dict):
        parts.append(_render_end_state_verifiability(idea_contract))

    # --- Skipped sections note ---
    if skipped:
        parts.append("---")
        parts.append("")
        parts.append("**Skipped sections:** " + ", ".join(skipped))

    return "\n".join(parts) + "\n"


# ======================================================================================
# assemble_digest — many briefs -> one sorted digest (graduated verbatim from build #4)
# ======================================================================================
def _decision_order(decision):
    """Return a sort key for main_review.decision.

    approved_for_coder < needs_more < rejected (ascending = most urgent first).
    A non-string (e.g. unhashable) decision sorts last instead of raising, so the
    digest stays total on malformed-but-dict input.
    """
    order = {"approved_for_coder": 0, "needs_more": 1, "rejected": 2}
    if not isinstance(decision, str):
        return 3
    return order.get(decision, 3)


def _risk_key(main_review):
    """Return a sort key tuple for risk: (risk_score, risk_band).

    If risk_score is missing/None, use a high sentinel so it sorts after
    scored items.  risk_band is used as tiebreaker (alphabetical).
    """
    if isinstance(main_review, dict):
        score = main_review.get("risk_score")
        band = main_review.get("risk_band", "")
    else:
        score = None
        band = ""

    # Coerce to homogeneous, sortable types so a malformed-but-dict review (a
    # string score, a non-string band, a mix across triples) can never make the
    # sort raise — the digest must stay total even on schema-loose input.
    band = band if isinstance(band, str) else ""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return (1, 0, band)
    return (0, score, band)


# The idea-contract fields a triple must carry to count as a renderable brief.
# Mirrors idea_brief's required-field set: a triple missing any of these is
# malformed and is skipped-and-counted rather than rendered as a half-brief.
_REQUIRED_CONTRACT_FIELDS = (
    "job_id", "proposed_at", "what_should_exist", "who_benefits", "why_now",
    "evidence_refs", "out_of_scope", "where_it_lives", "how_verifiable",
)


def _is_valid_triple(triple):
    """Check if a triple is renderable (dict with required keys)."""
    if not isinstance(triple, dict):
        return False

    idea_contract = triple.get("idea_contract")
    research_input = triple.get("research_input")
    main_review = triple.get("main_review")

    # A missing required idea-contract field makes the triple malformed
    # (skipped-and-counted), per the spec's skip-and-note-missing-fields rule.
    if not isinstance(idea_contract, dict):
        return False
    if any(f not in idea_contract for f in _REQUIRED_CONTRACT_FIELDS):
        return False
    if idea_contract.get("job_id") is None:
        return False

    # research_input is part of the triple; a missing/non-dict half makes the
    # triple malformed (skipped-and-counted) rather than rendering a half-brief.
    if not isinstance(research_input, dict):
        return False

    # main_review must carry what render_brief renders AND what the sort needs:
    # a string decision AND a string risk_band. A missing/non-string either one is
    # a malformed review -> skipped-and-counted, never rendered as a half-brief.
    if not isinstance(main_review, dict):
        return False
    if not isinstance(main_review.get("decision"), str):
        return False
    if not isinstance(main_review.get("risk_band"), str):
        return False

    return True


def _render_triple(triple):
    """Render a single triple into a markdown brief."""
    idea_contract = triple.get("idea_contract", {})
    research_input = triple.get("research_input")
    main_review = triple.get("main_review")

    return render_brief(idea_contract, research_input, main_review)


def assemble_digest(triples):
    """Assemble a deterministic markdown digest from triples.

    Args:
        triples: list of dicts, each with keys 'idea_contract',
                 'research_input' (optional), and 'main_review'.

    Returns:
        A markdown string containing an escalation header followed by all
        rendered briefs sorted by decision then risk.  Malformed triples are
        skipped and counted in the header.

    The function is total: bad input never raises — malformed triples are
    silently skipped and counted.
    """
    if not isinstance(triples, list):
        return "# Idea Digest\n\n**Error:** input is not a list.\n"

    valid = []
    skipped_count = 0

    for triple in triples:
        if _is_valid_triple(triple):
            valid.append(triple)
        else:
            skipped_count += 1

    # Sort by (decision_order, risk_score_or_sentinel, risk_band)
    def sort_key(triple):
        mr = triple.get("main_review", {})
        decision = _safe_get(mr, "decision", "")
        return (_decision_order(decision), _risk_key(mr))

    valid.sort(key=sort_key)

    # Build the digest
    parts = [
        "# Idea Digest",
        "",
        f"**Escalation header:** {len(valid)} brief(s) rendered, "
        f"{skipped_count} malformed triple(s) skipped.",
        "",
    ]

    for i, triple in enumerate(valid, 1):
        brief = _render_triple(triple)
        parts.append(brief.rstrip("\n"))
        if i < len(valid):
            parts.append("---")
            parts.append("")

    return "\n".join(parts) + "\n"


# ======================================================================================
# Thin CLI — read a JSON array of triples (file path or '-' for stdin), print the digest.
# ======================================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="idea_digest",
        description="Render an idea digest from a JSON array of (idea_contract, "
        "research_input, main_review) triples.",
    )
    ap.add_argument("triples", help="path to a JSON array of triples, or '-' for stdin")
    args = ap.parse_args(argv)

    raw = sys.stdin.read() if args.triples == "-" else open(args.triples, encoding="utf-8").read()
    try:
        triples = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(assemble_digest(triples))
    return 0


if __name__ == "__main__":
    sys.exit(main())
