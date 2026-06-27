"""F3 — known-overreach fixture: bounded clause + over-broad probe → route=gemma.

WHY THIS EXISTS (Rev 5 spec, "Seeded fixtures + canary"):

  > F3 known-overreach: Gemma plans stricter than contract; Codex counter-
  > example; route=gemma.

F3 is the third of the seeded fixtures the spec requires to pass 4/4 before
the QA layer goes live. The shape: a contract clause is BOUNDED (says
something about a specific domain, e.g. non-negative integers only); Gemma
plans a concrete probe whose assertion holds OVER THE WHOLE TYPE (including
negatives, which the contract doesn't promise behavior for); pytest passes
(the coder's implementation happens to handle negatives correctly); Codex
audits the probe as `entailment_verdict=overreach` because a HYPOTHETICAL
compliant implementation could legitimately fail the probe on negatives —
the contract didn't require that behavior. The runner's deterministic
compute_route lands on `gemma` (Gemma must re-plan a probe within the
contract's actual bounds, not a stricter envelope).

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

WHY THE OVERREACH BRANCH ORDERING IS LOAD-BEARING

compute_route checks branches in this order:
  4. `entailment_verdict == "overreach" in per_planned` → gemma.
  5. `triviality_verdict == "trivial" in per_planned`   → gemma.
  6. `audit_verdict == "skip_hollow" in per_skipped`    → gemma.
  7. `pytest_exit == 1 and per_planned all fair`        → coder.
  8. `pytest_exit == 0`                                 → confirm.

The overreach check fires BEFORE the pytest_exit=0 confirm branch. That
ordering is what makes F3 work: pytest passed (because the coder's
implementation is broader than the contract requires), but the probe
overreaches, so we want `gemma`, not `confirm`. If the branch ordering ever
flipped (e.g. an "exit=0 short-circuit to confirm" optimization), F3 fires
because the route would land on `confirm` with an overreach probe in scope.

A second regression-pin test inside this module asserts the overreach
branch fires REGARDLESS of `triviality_verdict`. The Rev 5 schema docs say
Codex should emit `n/a` only on failing tests, but the JSON schema itself
allows `n/a` on any test. A spec-loose Codex that emits `n/a` on a passing
overreach probe must still route to `gemma`. Compute_route reads
entailment first; triviality is a SEPARATE axis.

WHAT THIS TEST DELIBERATELY DOES NOT COVER

- Real Gemma swap (F0a) or real Codex call (shadow phase).
- The pytest run itself — F3 supplies `pytest_exit=0` as a deterministic
  input representing "the synthetic test_qa.py ran in the sandbox and
  passed (the coder's implementation happened to handle the over-broad
  domain)."
- sandbox-exec policy (F0b/F5).
- Deep C1 schema edge cases (qa_check_pre's own tests).

NOTE on strategy-schema coverage: F3 calls `jsonschema.validate` against
QA_STRATEGY_SCHEMA as a REALISM PIN — to catch fixture drift if the
production schema tightens. It deliberately does NOT reproduce qa_check_pre's
deep C1 unit tests.
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


# Two explicit-ID clauses. The contract is INTENTIONALLY bounded: AC-001 only
# promises behavior for non-negative integers. Gemma's probe overreaches the
# bound by asserting the property over ALL ints (including negatives).
KNOWN_OVERREACH_ACCEPTANCE_CHECKS = [
    "[AC-001] sum_nonneg(nums: list[int]) RETURNS sum(nums) when every "
    "element of nums is >= 0. Behavior for negative elements is UNSPECIFIED.",
    "[AC-002] sum_nonneg(nums) RAISES TypeError when nums contains a "
    "non-int element.",
]


def _write_product_plan(tmp_path: Path) -> Path:
    """Write a minimal product-plan.json with two explicit-ID acceptance_checks."""
    plan = {"acceptance_checks": list(KNOWN_OVERREACH_ACCEPTANCE_CHECKS)}
    p = tmp_path / "product-plan.json"
    p.write_text(json.dumps(plan, indent=2))
    return p


def _known_overreach_strategy() -> dict:
    """A Gemma-shaped strategy whose planned probe asserts MORE than AC-001
    requires.

    AC-001 says behavior is defined for non-negative `nums`. Gemma's probe
    drives `sum_nonneg([-3, 1, 2])` and asserts the result is 0 (clamping
    negatives to zero). That assertion isn't grounded in the contract — the
    contract leaves negatives UNSPECIFIED. A compliant implementation could
    reasonably raise ValueError on negatives, or return -3+1+2 == 0
    (coincidentally), or return None, or anything else. The coder's
    implementation happens to clamp-and-sum (returns 0 for [-3, 1, 2]), so
    pytest passes, but the probe is overreach.

    AC-002 (TypeError on non-int) is earned-skipped: the coder already has a
    `pytest.raises(TypeError)` test that covers it.

    n_tests_written == 1 — the single probe lives in
    `test_sum_nonneg_clamps_negatives` (which the fixture doesn't materialize
    on disk; we only need its routing-shape).
    """
    return {
        "reviewed_clauses": ["AC-001", "AC-002"],
        "planned_tests": [
            {
                "clause_id": "AC-001",
                "probe_shape": "concrete",
                "concrete_behavior": {
                    "input": "sum_nonneg([-3, 1, 2])",
                    "expected": "0 (assumes negatives are clamped to zero)",
                },
                "coder_test_evidence": [
                    "tests/test_sum.py::test_sum_nonneg_smoke",
                ],
                "uncovered_behavior": (
                    "the coder test only covers non-negative inputs; Gemma "
                    "extends into the negative-element domain the contract "
                    "leaves unspecified — this probe asserts behavior the "
                    "contract doesn't require."
                ),
                "asserted_in_test": "test_sum_nonneg_clamps_negatives",
            },
        ],
        "skipped_clauses": [
            {
                "clause_id": "AC-002",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_sum.py::test_sum_nonneg_raises_on_non_int",
                ],
                "why_already_covered": (
                    "test_sum_nonneg_raises_on_non_int wraps "
                    "sum_nonneg([1, 'x', 3]) in pytest.raises(TypeError) and "
                    "parametrizes a few non-int values."
                ),
            },
        ],
        "n_tests_written": 1,
    }


def _overreach_with_substantive_codex_verdicts(
    planned_ids: list[str], skipped_ids: list[str]
) -> dict:
    """Codex verdicts: planned test is `overreach` with `triviality=substantive`
    (the test is meaningful, it just oversteps the contract). Skipped clause is
    `skip_earned`. declared_route is `gemma` (telemetry hint).

    Why `triviality=substantive`: the Rev 5 spec says triviality is judged for
    PASSING tests (would an obviously-buggy implementation pass this test?). The
    overreach probe asserts a specific clamping behavior — an obviously-buggy
    implementation that ignored negatives would NOT clamp them and would fail
    the test. So the test is non-trivial in the triviality sense. But it's
    still overreach in the entailment sense — a compliant implementation could
    do something other than clamp. The two axes are orthogonal.
    """
    return {
        "codex_per_planned_test": [
            {
                "clause_id": cid,
                "entailment_verdict": "overreach",
                "triviality_verdict": "substantive",
                "reason": (
                    f"AC-001 leaves negative-element behavior UNSPECIFIED. A "
                    f"compliant implementation could legitimately raise "
                    f"ValueError on a negative input — the probe asserts a "
                    f"specific clamping behavior the contract doesn't require "
                    f"({cid})."
                ),
            }
            for cid in planned_ids
        ],
        "codex_per_skipped_clause": [
            {
                "clause_id": cid,
                "audit_verdict": "skip_earned",
                "reason": f"cited coder test covers {cid} as claimed",
            }
            for cid in skipped_ids
        ],
        "declared_route": "gemma",
        "summary_reason": (
            "probe asserts more than the contract requires; Gemma must "
            "re-plan within the contract's actual bounds"
        ),
        "token_usage": {"prompt": 1600, "completion": 280},
    }


def test_f3_known_overreach_routes_to_gemma_despite_pytest_pass(
    tmp_path: Path,
) -> None:
    """End-to-end of the deterministic pipeline: bounded contract + over-broad
    probe + pytest_exit=0 → route=gemma.

    The probe asserts behavior over a domain the contract leaves unspecified
    (negatives in `sum_nonneg`). The coder's implementation happens to handle
    that domain correctly (pytest passes), but Codex audits the probe as
    `entailment_verdict=overreach` because a compliant implementation could
    legitimately differ on that domain. compute_route's overreach branch
    fires BEFORE the pytest_exit=0 confirm branch, so the route is `gemma`.

    Exercises (in pipeline order):
      1. parse_contract_clauses reads product-plan.json with two [AC-NNN] clauses.
      2. jsonschema.validate(strategy, QA_STRATEGY_SCHEMA) — realism pin.
      3. c18a_clause_completeness — every clause cited exactly once across
         (planned ∪ skipped).
      4. validate_verdict_cardinality — Codex per-item ids multiset-match the
         strategy's ids.
      5. validate_and_compute_route with pytest_exit=0 → the overreach
         branch of compute_route fires before the confirm branch.

    What would BREAK this assertion (regression signals):
      - compute_route's branch ordering changing such that pytest_exit=0
        short-circuits to confirm before checking overreach.
      - validate_and_compute_route gaining a new gate that HOLDs an
        overreach-but-passing-test scenario before reaching compute_route.
      - The CODEX_VERDICT_SCHEMA dropping the `overreach` enum value.
      - parse_contract_clauses changing the `[AC-NNN]` format.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)
    assert [c["id"] for c in clauses] == ["AC-001", "AC-002"], (
        "F3 must parse two explicit AC-IDs; anything else means the contract "
        "format contract changed."
    )

    strategy = _known_overreach_strategy()
    # Realism pin — F1 lesson: fixture must satisfy the production C1 schema.
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass for known-overreach F3: {c18a.reason}"

    codex_verdicts = _overreach_with_substantive_codex_verdicts(
        planned_ids=["AC-001"], skipped_ids=["AC-002"]
    )

    cardinality = validate_verdict_cardinality(strategy, codex_verdicts)
    assert cardinality.ok, (
        f"cardinality should pass for matched ids: {cardinality.reason}"
    )

    # pytest_exit=0: the coder's implementation happens to handle negatives
    # the way Gemma's probe asserts — but the probe is still overreach
    # against the contract.
    qa_route = validate_and_compute_route(
        strategy, codex_verdicts, pytest_exit=0
    )

    assert qa_route["computed_route"] == "gemma", (
        f"F3 known-overreach must route=gemma (overreach branch fires before "
        f"pytest_exit=0 confirm branch); got "
        f"{qa_route['computed_route']!r} (summary_reason="
        f"{qa_route.get('summary_reason')!r})"
    )
    # Telemetry pins.
    assert qa_route["codex_declared_route"] == "gemma"
    assert qa_route["agreement"] is True
    assert len(qa_route["codex_per_planned_test"]) == 1
    assert (
        qa_route["codex_per_planned_test"][0]["entailment_verdict"]
        == "overreach"
    )


def test_f3_overreach_fires_even_when_triviality_verdict_is_na(
    tmp_path: Path,
) -> None:
    """Pin: the overreach branch fires REGARDLESS of `triviality_verdict`.

    The Rev 5 spec text says Codex should emit `triviality=n/a` only on
    FAILING tests, but the JSON schema (CODEX_VERDICT_SCHEMA) allows `n/a` on
    any test (the schema enforces the enum membership but not the
    test-status correlation). A spec-loose Codex emitting `n/a` on a passing
    overreach probe is schema-valid; compute_route must still route to
    `gemma` via the overreach branch (which is checked BEFORE triviality).

    Without this pin, a future regression that hung overreach behind a
    `if triviality != "n/a"` gate (a misguided "only judge entailment when
    triviality is meaningful" optimization) would slip through C1+C18a+
    cardinality and silently route a passing overreach probe to `confirm`.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)

    strategy = _known_overreach_strategy()
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass for the regression-pin strategy: {c18a.reason}"

    codex_verdicts = _overreach_with_substantive_codex_verdicts(
        planned_ids=["AC-001"], skipped_ids=["AC-002"]
    )
    # Mutate triviality to n/a (spec-loose for a passing test, but
    # schema-valid). The route must still land on gemma via overreach.
    codex_verdicts["codex_per_planned_test"][0]["triviality_verdict"] = "n/a"

    qa_route = validate_and_compute_route(
        strategy, codex_verdicts, pytest_exit=0
    )

    assert qa_route["computed_route"] == "gemma", (
        "overreach branch must fire regardless of triviality_verdict's value. "
        "If this fails, the overreach check has been gated behind triviality "
        "in some way — that's a routing regression. Got "
        f"{qa_route['computed_route']!r}."
    )
