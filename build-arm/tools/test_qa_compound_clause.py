"""Compound-clause fixture: contract sub-case coverage decides confirm vs gemma.

WHY THIS EXISTS (Rev 5 spec, "Atomic clauses — what gets enforced"):

  > v1 simplification: each `acceptance_check` is one atomic clause. If a
  > check compounds sub-cases ("RAISES ValueError when: A, B, C, D, E"),
  > Codex routing audits whether the cited evidence actually covers all
  > sub-cases. v2 may split further.

This fixture pins the v1 behavior: the v1 clause-parsing keeps a compound
clause as one atomic entry (no automatic splitting), and the routing
correctness depends on Codex's audit verdict accurately reflecting
sub-case coverage. The fixture pins BOTH directions:

  Negative: cited evidence covers only some sub-cases (A+B) →
            Codex audit_verdict=skip_hollow → route=gemma.
  Positive: cited evidence covers all sub-cases (parametrized over
            A,B,C,D,E) → Codex audit_verdict=skip_earned → route=confirm.

THE LOAD-BEARING CLAIM

In v1, compute_route doesn't know whether a clause is compound — it reads
the audit verdict that Codex emits. The trust property is that Codex
faithfully audits sub-case coverage and emits skip_hollow whenever
ANY sub-case is uncovered. This fixture proves the v1 routing handles
both verdicts correctly for a realistic compound clause; it does NOT
prove Codex's live audit is sound (that's the shadow phase's job).

What the fixture DOES surface for v2 design: if v2 chooses to split
compound clauses at parse time (so each sub-case becomes its own
contract_clause entry), the strategy/verdict shapes here would have to
change. F1/F2/F3/F4 stay the same — compound-clause is the surface that
codifies the v1 atomic-clause assumption.

WHAT THIS TEST DELIBERATELY DOES NOT COVER

- Codex's live audit judgment (shadow phase).
- Real Gemma swap (F0a) or real Codex call.
- sandbox-exec policy (F0b/F5).
- Deep C1 schema edge cases (qa_check_pre's own tests).

The fixture exercises parse_contract_clauses + c18a_clause_completeness +
validate_verdict_cardinality + validate_and_compute_route end-to-end with
synthetic strategy + synthetic Codex verdicts at the model boundaries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.qa_check_pre import (  # noqa: E402
    QA_STRATEGY_SCHEMA,
    c18a_clause_completeness,
)
from tools.qa_clauses import parse_contract_clauses  # noqa: E402
from tools.qa_route import (  # noqa: E402
    validate_and_compute_route,
    validate_verdict_cardinality,
)


# A single explicit-ID clause that compounds 5 sub-cases. The v1
# parser keeps this as ONE atomic clause; Codex is responsible for
# auditing sub-case coverage of the cited evidence.
COMPOUND_ACCEPTANCE_CHECKS = [
    "[AC-001] parse_color(s: str) RAISES ValueError on each of: "
    "(A) empty string, "
    "(B) non-hex characters in a '#'-prefixed input, "
    "(C) wrong length (not 4 or 7 chars including '#'), "
    "(D) named color outside the X11 baseline set, "
    "(E) leading whitespace.",
]


def _write_product_plan(tmp_path: Path) -> Path:
    """Write a minimal product-plan.json with one compound acceptance_check."""
    plan = {"acceptance_checks": list(COMPOUND_ACCEPTANCE_CHECKS)}
    p = tmp_path / "product-plan.json"
    p.write_text(json.dumps(plan, indent=2))
    return p


def _partial_coverage_strategy() -> dict:
    """Gemma claims AC-001 is covered, citing a coder test that only
    parametrizes sub-cases A and B (empty string + non-hex chars). Sub-cases
    C/D/E are uncovered. The strategy looks plausible on its face — the
    cited test does exist and does raise ValueError for the cases it
    covers — but Codex's audit must catch the partial coverage.
    """
    return {
        "reviewed_clauses": ["AC-001"],
        "planned_tests": [],
        "skipped_clauses": [
            {
                "clause_id": "AC-001",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_parse_color.py::test_parse_color_invalid_basic",
                ],
                "why_already_covered": (
                    "test_parse_color_invalid_basic parametrizes '' and "
                    "'#zzz' — covers the empty-string and non-hex-character "
                    "branches of AC-001."
                ),
            },
        ],
        "n_tests_written": 0,
    }


def _full_coverage_strategy() -> dict:
    """Gemma cites a parametrized coder test that covers all 5 sub-cases of
    AC-001. The fixture's why_already_covered prose explicitly enumerates
    the sub-case coverage so Codex's audit can trace each one to a
    parametrize argument.
    """
    return {
        "reviewed_clauses": ["AC-001"],
        "planned_tests": [],
        "skipped_clauses": [
            {
                "clause_id": "AC-001",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_parse_color.py::test_parse_color_invalid_full",
                ],
                "why_already_covered": (
                    "test_parse_color_invalid_full is parametrized over "
                    "['', '#zzz', '#1234567', 'plaid', '  red']: each "
                    "input exercises one of AC-001's five sub-cases — "
                    "(A) empty, (B) non-hex chars at a length the parser "
                    "would otherwise accept (4 chars = '#RGB' short hex), "
                    "(C) wrong length (8 chars), (D) unknown named color, "
                    "(E) leading whitespace. Sub-case B uses '#zzz' "
                    "rather than '#zz' so the wrong-length and non-hex "
                    "branches stay independent — '#zz' would also trigger "
                    "C, leaving B underprobed."
                ),
            },
        ],
        "n_tests_written": 0,
    }


def _skip_hollow_verdicts() -> dict:
    """Codex audits the partial-coverage strategy: skip_hollow with a
    reason calling out the missing sub-cases. declared_route=gemma.
    """
    return {
        "codex_per_planned_test": [],
        "codex_per_skipped_clause": [
            {
                "clause_id": "AC-001",
                "audit_verdict": "skip_hollow",
                "reason": (
                    "cited test covers sub-cases A (empty) and B (non-hex) "
                    "but does NOT exercise C (wrong length), D (unknown "
                    "named color), or E (leading whitespace). Gemma must "
                    "plan a real probe for the uncovered sub-cases."
                ),
            },
        ],
        "declared_route": "gemma",
        "summary_reason": (
            "compound clause partially covered; Gemma must extend "
            "the probe set"
        ),
        "token_usage": {"prompt": 1800, "completion": 280},
    }


def _skip_earned_verdicts() -> dict:
    """Codex audits the full-coverage strategy: skip_earned with a reason
    confirming each sub-case has a parametrize row. declared_route=confirm.
    """
    return {
        "codex_per_planned_test": [],
        "codex_per_skipped_clause": [
            {
                "clause_id": "AC-001",
                "audit_verdict": "skip_earned",
                "reason": (
                    "parametrize covers all five sub-cases A/B/C/D/E with "
                    "one row each; sub-case coverage is complete."
                ),
            },
        ],
        "declared_route": "confirm",
        "summary_reason": (
            "compound clause fully covered by parametrized coder evidence"
        ),
        "token_usage": {"prompt": 1500, "completion": 210},
    }


def test_compound_clause_partial_coverage_routes_to_gemma(
    tmp_path: Path,
) -> None:
    """Negative variant: cited evidence covers ONLY some sub-cases of a
    compound clause → Codex skip_hollow → route=gemma.

    Pipeline order:
      1. parse_contract_clauses reads product-plan.json (one [AC-NNN] clause).
      2. jsonschema.validate(strategy, QA_STRATEGY_SCHEMA) — realism pin.
      3. c18a_clause_completeness — the single clause appears in skipped.
      4. validate_verdict_cardinality — Codex verdict ids match strategy.
      5. validate_and_compute_route with pytest_exit=0 → skip_hollow branch
         fires → route=gemma.

    What this pins: the v1 atomic-clause assumption forces compound
    sub-case coverage to be audited by Codex, not enforced by structure.
    If Codex's audit miscalls a partial-coverage skip as skip_earned, this
    fixture would still pass (it tests the routing reaction to the
    verdict, not the verdict itself); the shadow phase is where Codex's
    audit judgement is calibrated.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)
    assert [c["id"] for c in clauses] == ["AC-001"], (
        "compound-clause fixture must parse one [AC-001] clause"
    )
    # Codex review round 1 finding #4: pin that the parsed clause text
    # still carries every sub-case marker. Without this, a future parser
    # change that drops clause detail (or rewrites bracketed markers)
    # while keeping the ID would silently pass the fixture even though
    # Codex would no longer see the sub-cases it needs to audit.
    text = clauses[0]["text"]
    for marker in ("(A)", "(B)", "(C)", "(D)", "(E)"):
        assert marker in text, (
            f"compound clause text lost sub-case marker {marker!r}; "
            f"parsed text: {text[:200]!r}"
        )

    strategy = _partial_coverage_strategy()
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass: {c18a.reason}"

    verdicts = _skip_hollow_verdicts()
    cardinality = validate_verdict_cardinality(strategy, verdicts)
    assert cardinality.ok, f"cardinality: {cardinality.reason}"

    # pytest_exit=0: strategic null (no planned tests). The skip_hollow
    # branch fires before the confirm branch.
    qa_route = validate_and_compute_route(strategy, verdicts, pytest_exit=0)

    assert qa_route["computed_route"] == "gemma", (
        f"compound-clause partial-coverage must route=gemma via the "
        f"skip_hollow branch; got {qa_route['computed_route']!r} "
        f"(summary_reason={qa_route.get('summary_reason')!r})"
    )
    # Telemetry pins.
    assert qa_route["codex_declared_route"] == "gemma"
    assert qa_route["agreement"] is True
    skipped = qa_route["codex_per_skipped_clause"]
    assert len(skipped) == 1
    assert skipped[0]["audit_verdict"] == "skip_hollow"


def test_compound_clause_full_coverage_routes_to_confirm(
    tmp_path: Path,
) -> None:
    """Positive variant: cited evidence covers ALL sub-cases of a compound
    clause → Codex skip_earned → route=confirm.

    Same pipeline as the negative variant; the only thing that changes is
    Codex's audit verdict. Together the two tests pin both directions of
    the v1 routing for a compound clause: partial coverage→gemma,
    full coverage→confirm.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)

    strategy = _full_coverage_strategy()
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass: {c18a.reason}"

    verdicts = _skip_earned_verdicts()
    cardinality = validate_verdict_cardinality(strategy, verdicts)
    assert cardinality.ok, f"cardinality: {cardinality.reason}"

    qa_route = validate_and_compute_route(strategy, verdicts, pytest_exit=0)

    assert qa_route["computed_route"] == "confirm", (
        f"compound-clause full-coverage must route=confirm via the "
        f"all-skip_earned branch; got {qa_route['computed_route']!r} "
        f"(summary_reason={qa_route.get('summary_reason')!r})"
    )
    assert qa_route["codex_declared_route"] == "confirm"
    assert qa_route["agreement"] is True
    skipped = qa_route["codex_per_skipped_clause"]
    assert len(skipped) == 1
    assert skipped[0]["audit_verdict"] == "skip_earned"
