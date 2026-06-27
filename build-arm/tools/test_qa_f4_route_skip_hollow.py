"""F4 — skip-hollow fixture: Gemma's earned-skip claim doesn't cover the clause → gemma.

WHY THIS EXISTS (Rev 5 spec, "Seeded fixtures + canary"):

  > F4 known-covered subtle: coder covers it non-obviously; Codex audits coder
  > evidence; route=gemma (skip_hollow inverted to overreach probe).

F4 is the fourth of the seeded fixtures the spec requires to pass 4/4 before
the QA layer goes live. The shape: a contract clause is REAL and requires a
specific behavior; Gemma puts it in `skipped_clauses` with a coder-test
citation, claiming the citation already covers the clause; Codex AUDITS the
citation and finds that the cited test DOESN'T actually probe what the clause
requires (it's a hollow skip). compute_route lands on `gemma` because Gemma's
strategy authorship has a hole — Gemma should have planned a real probe
instead of skipping.

WHAT THIS TEST COVERS

End-to-end of the deterministic routing pipeline below the model boundaries:

  product-plan.json  ─▶ parse_contract_clauses          ─▶ 2 clauses
                       (real file I/O)

  (synthetic Gemma)  ─▶ qa-strategy.json (in-memory)
                                                         ├▶ jsonschema.validate           → ok
                                                         ├▶ c18a_clause_completeness      → ok
                                                         └▶ validate_verdict_cardinality  → ok

  (synthetic Codex)  ─▶ codex_verdicts (in-memory)
                                                         └▶ validate_and_compute_route(
                                                                strategy, verdicts,
                                                                pytest_exit=0,
                                                            ) → computed_route == "gemma"

WHY SKIP_HOLLOW BRANCH PRECEDENCE IS LOAD-BEARING

compute_route checks branches in this order:
  4. `entailment_verdict == "overreach" in per_planned`  → gemma.
  5. `triviality_verdict == "trivial" in per_planned`    → gemma.
  6. `audit_verdict == "skip_hollow" in per_skipped`     → gemma.   ← F4
  7. `pytest_exit == 1 and per_planned all fair`         → coder.
  8. `pytest_exit == 0`                                  → confirm.

Skip_hollow fires BEFORE both the coder branch (pytest_exit=1) and the
confirm branch (pytest_exit=0). That means: even if a planned probe
legitimately caught a real coder bug (route would be `coder`), a single
skip_hollow elsewhere in the strategy routes the whole thing to `gemma`. The
interpretation: a strategy with a hollow skip has broader authorship
problems; Gemma must re-plan the entire strategy, not just refine the probe
that caught the bug. The regression-pin test inside this module asserts that
precedence.

WHAT THIS TEST DELIBERATELY DOES NOT COVER

- Real Gemma swap (F0a) or real Codex call (shadow phase).
- The pytest run itself — F4 supplies `pytest_exit=0` (and 1 in the pin) as
  deterministic inputs.
- sandbox-exec policy (F0b/F5).
- Deep C1 schema edge cases (qa_check_pre's own tests).

NOTE on strategy-schema coverage: F4 calls `jsonschema.validate` against
QA_STRATEGY_SCHEMA as a REALISM PIN. It deliberately does NOT reproduce
qa_check_pre's deep C1 unit tests.
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


# Two explicit-ID clauses. AC-001 is a precise requirement (a specific edge
# case). Gemma's strategy will cite a smoke test that doesn't actually probe
# it — the hollow skip. AC-002 is a routine clause covered by an obvious
# coder test (earned skip).
HOLLOW_SKIP_ACCEPTANCE_CHECKS = [
    "[AC-001] normalize_path(p: str) collapses repeated forward slashes "
    "(e.g. '/a//b///c' -> '/a/b/c') BUT preserves a leading '//' as the "
    "POSIX network-prefix convention demands.",
    "[AC-002] normalize_path(p: str) removes a trailing '/' except when p "
    "equals '/' (the root case).",
]


def _write_product_plan(tmp_path: Path) -> Path:
    """Write a minimal product-plan.json with two explicit-ID acceptance_checks."""
    plan = {"acceptance_checks": list(HOLLOW_SKIP_ACCEPTANCE_CHECKS)}
    p = tmp_path / "product-plan.json"
    p.write_text(json.dumps(plan, indent=2))
    return p


def _hollow_skip_strategy() -> dict:
    """A Gemma-shaped strategy that earned-skips AC-002 (real coverage) but
    HOLLOW-skips AC-001 (cites a smoke test that doesn't probe the load-
    bearing leading-`//` edge case).

    AC-001's load-bearing detail is the POSIX network-prefix convention:
    `'//' + rest` must stay `'//' + rest` (NOT collapse to `'/' + rest`).
    Gemma cites `test_normalize_basic` which only checks
    `'/a/b' -> '/a/b'` — a no-op smoke test that doesn't distinguish the
    leading-`//` case at all. The citation looks plausible but is hollow.

    AC-002 is earned-skipped: `test_normalize_trailing_slash` parametrizes
    `'/x/' -> '/x'` and `'/' -> '/'` and asserts the root case is
    preserved. That's real coverage.

    n_tests_written == 0 — Gemma writes no test of its own. Codex catches
    the hollow skip on audit and the runner routes to gemma so Gemma can
    re-plan a real probe.
    """
    return {
        "reviewed_clauses": ["AC-001", "AC-002"],
        "planned_tests": [],
        "skipped_clauses": [
            {
                "clause_id": "AC-001",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_normalize.py::test_normalize_basic",
                ],
                # The why_already_covered prose is plausible-sounding but the
                # cited test only exercises a no-op input — it doesn't drive
                # the leading-'//' POSIX-network-prefix edge case at all.
                "why_already_covered": (
                    "test_normalize_basic covers normalize_path's collapse "
                    "behavior — sufficient for AC-001."
                ),
            },
            {
                "clause_id": "AC-002",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_normalize.py::test_normalize_trailing_slash",
                ],
                "why_already_covered": (
                    "test_normalize_trailing_slash parametrizes "
                    "'/x/' -> '/x' AND '/' -> '/' — both branches of AC-002 "
                    "explicitly probed."
                ),
            },
        ],
        "n_tests_written": 0,
    }


def _hollow_then_earned_codex_verdicts(
    skipped_hollow_ids: list[str], skipped_earned_ids: list[str]
) -> dict:
    """Codex verdicts: hollow-skipped clauses get audit_verdict=skip_hollow
    with a reason calling out the missing coverage; earned-skipped clauses
    get audit_verdict=skip_earned. declared_route is `gemma` (telemetry).

    No per_planned_test entries because the strategy plans nothing
    (strategic null on the planned side).
    """
    per_skipped: list[dict] = []
    for cid in skipped_hollow_ids:
        per_skipped.append(
            {
                "clause_id": cid,
                "audit_verdict": "skip_hollow",
                "reason": (
                    f"cited test for {cid} is a smoke probe over the no-op "
                    f"domain; the contract's leading-'//' edge case is "
                    f"unprobed by the citation. Gemma must plan a real "
                    f"concrete probe instead of claiming this skip."
                ),
            }
        )
    for cid in skipped_earned_ids:
        per_skipped.append(
            {
                "clause_id": cid,
                "audit_verdict": "skip_earned",
                "reason": (
                    f"cited test for {cid} parametrizes both required "
                    f"branches — genuine coverage."
                ),
            }
        )
    return {
        "codex_per_planned_test": [],
        "codex_per_skipped_clause": per_skipped,
        "declared_route": "gemma",
        "summary_reason": (
            "hollow skip on AC-001: cited test doesn't probe the load-bearing "
            "edge case; Gemma must re-plan with a real concrete probe"
        ),
        "token_usage": {"prompt": 1700, "completion": 290},
    }


def test_f4_known_hollow_skip_routes_to_gemma(tmp_path: Path) -> None:
    """End-to-end of the deterministic pipeline: real clause + hollow skip
    citation → route=gemma.

    Exercises (in pipeline order):
      1. parse_contract_clauses reads product-plan.json with two [AC-NNN] clauses.
      2. jsonschema.validate(strategy, QA_STRATEGY_SCHEMA) — realism pin.
      3. c18a_clause_completeness — every clause cited exactly once across
         (planned ∪ skipped).
      4. validate_verdict_cardinality — Codex per-item ids multiset-match the
         strategy's ids.
      5. validate_and_compute_route with pytest_exit=0 → the skip_hollow
         branch of compute_route fires.

    What would BREAK this assertion (regression signals):
      - compute_route dropping the skip_hollow → gemma branch.
      - The CODEX_VERDICT_SCHEMA dropping the `skip_hollow` enum value.
      - parse_contract_clauses changing the `[AC-NNN]` format.
      - validate_and_compute_route gaining a new pre-route gate that
        HOLDs a well-shaped hollow-skip fixture.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)
    assert [c["id"] for c in clauses] == ["AC-001", "AC-002"], (
        "F4 must parse two explicit AC-IDs; anything else means the contract "
        "format contract changed."
    )

    strategy = _hollow_skip_strategy()
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass for hollow-skip F4: {c18a.reason}"

    codex_verdicts = _hollow_then_earned_codex_verdicts(
        skipped_hollow_ids=["AC-001"], skipped_earned_ids=["AC-002"]
    )

    cardinality = validate_verdict_cardinality(strategy, codex_verdicts)
    assert cardinality.ok, (
        f"cardinality should pass for matched ids: {cardinality.reason}"
    )

    # pytest_exit=0: strategic null on the planned side (n_tests_written==0)
    # means the strategic-null guard in qa.py:_resolve_pytest_results
    # synthesizes a clean exit, so 0 is the realistic value here.
    qa_route = validate_and_compute_route(
        strategy, codex_verdicts, pytest_exit=0
    )

    assert qa_route["computed_route"] == "gemma", (
        f"F4 hollow-skip must route=gemma (skip_hollow branch fires before "
        f"pytest_exit=0 confirm branch); got "
        f"{qa_route['computed_route']!r} (summary_reason="
        f"{qa_route.get('summary_reason')!r})"
    )
    # Telemetry pins.
    assert qa_route["codex_declared_route"] == "gemma"
    assert qa_route["agreement"] is True
    assert qa_route["codex_per_planned_test"] == []
    # Two skipped, one hollow one earned.
    skipped = qa_route["codex_per_skipped_clause"]
    assert len(skipped) == 2
    hollow = [s for s in skipped if s["audit_verdict"] == "skip_hollow"]
    earned = [s for s in skipped if s["audit_verdict"] == "skip_earned"]
    assert len(hollow) == 1
    assert hollow[0]["clause_id"] == "AC-001"
    assert len(earned) == 1
    assert earned[0]["clause_id"] == "AC-002"


def test_f4_skip_hollow_takes_precedence_over_coder_route(
    tmp_path: Path,
) -> None:
    """Pin: skip_hollow routes to gemma EVEN WHEN a planned probe legitimately
    caught a real coder bug (pytest_exit=1 + fair probe).

    This is the load-bearing precedence claim in compute_route's branch
    ordering: a strategy with a hollow skip has broader authorship problems,
    so even a legitimate bug-find by another probe doesn't override the need
    for Gemma to re-plan. The interpretation is "the whole strategy is
    suspect; don't act on partial signal."

    Without this pin, a future re-ordering of compute_route's branches that
    moved the pytest_exit=1 coder check ABOVE skip_hollow would silently
    route this scenario to `coder` — letting Gemma's authorship error hide
    behind a real bug-find.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)

    # Strategy: 1 fair planned probe on AC-002 (a real bug-find by the probe)
    # + 1 hollow skip on AC-001. Both clauses cited exactly once.
    strategy = {
        "reviewed_clauses": ["AC-001", "AC-002"],
        "planned_tests": [
            {
                "clause_id": "AC-002",
                "probe_shape": "concrete",
                "concrete_behavior": {
                    "input": "normalize_path('/x/')",
                    "expected": "'/x' (trailing slash removed for non-root)",
                },
                "coder_test_evidence": [
                    "tests/test_normalize.py::test_normalize_basic",
                ],
                "uncovered_behavior": (
                    "the smoke test doesn't probe the trailing-slash branch; "
                    "this probe drives it."
                ),
                "asserted_in_test": "test_normalize_trailing_slash_for_non_root",
            },
        ],
        "skipped_clauses": [
            {
                "clause_id": "AC-001",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_normalize.py::test_normalize_basic",
                ],
                "why_already_covered": (
                    "claimed coverage of the collapse behavior (plausible-"
                    "sounding but hollow against the leading-'//' edge case)"
                ),
            },
        ],
        "n_tests_written": 1,
    }
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass for the regression-pin strategy: {c18a.reason}"

    codex_verdicts = {
        "codex_per_planned_test": [
            {
                "clause_id": "AC-002",
                "entailment_verdict": "fair",
                "triviality_verdict": "n/a",  # test FAILED → n/a
                "reason": "any compliant implementation passes this probe",
            },
        ],
        "codex_per_skipped_clause": [
            {
                "clause_id": "AC-001",
                "audit_verdict": "skip_hollow",
                "reason": (
                    "cited test doesn't probe the leading-'//' edge case; "
                    "the skip is hollow"
                ),
            },
        ],
        # Codex's hint here is gemma (the dominant signal) not coder.
        "declared_route": "gemma",
        "summary_reason": (
            "hollow skip dominates: Gemma's strategy is suspect even though "
            "the one planned probe caught a real bug"
        ),
        # Finding #8 (2026-06-07): token_usage is required + nullable under
        # strict mode. Codex emitting null is legal; the runner accepts it.
        "token_usage": None,
    }

    cardinality = validate_verdict_cardinality(strategy, codex_verdicts)
    assert cardinality.ok, f"cardinality: {cardinality.reason}"

    # pytest_exit=1: the planned probe failed (caught a real bug). In the
    # absence of the skip_hollow, this would route to `coder`. With the
    # skip_hollow, it must route to `gemma` instead.
    qa_route = validate_and_compute_route(
        strategy, codex_verdicts, pytest_exit=1
    )

    assert qa_route["computed_route"] == "gemma", (
        "skip_hollow must take precedence over the pytest_exit=1 + fair "
        "coder branch. If this fails, the branch ordering has been "
        "rearranged in a way that lets Gemma's authorship error hide behind "
        "a real bug-find. Got "
        f"{qa_route['computed_route']!r}."
    )
