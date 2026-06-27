"""F2 — known-buggy fixture: real coder bug surfaced → route=coder.

WHY THIS EXISTS (Rev 5 spec, "Seeded fixtures + canary"):

  > F2 known-buggy: contract names an edge coder missed; Gemma plans it;
  > test fails; route=coder.

F2 is the second of the seeded fixtures the spec requires to pass 4/4 before
the QA layer goes live. The shape: a contract clause names a behavior the
coder's tests don't probe; Gemma plans a single concrete probe; the synthetic
test_qa.py asserts the missing behavior; pytest exits 1 (the coder's
implementation actually has the bug); Codex audits the probe as
`entailment_verdict=fair` (any compliant implementation would pass it). The
runner's deterministic compute_route lands on `coder`.

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
                                                                pytest_exit=1,
                                                            ) → computed_route == "coder"

WHY THE per_planned-NON-EMPTY BRANCH IS LOAD-BEARING

`compute_route` routes pytest_exit=1 + all per_planned `fair` to `coder`. The
spec's goal must-fix #1 closed a vacuous-truth quirk in this branch:
`all(... for p in [])` evaluates to True (Python's `all` over an empty
iterable), so an empty per_planned list with pytest_exit=1 would have routed
to `coder` even with no failing test to point at. The fix added an explicit
`if not per_planned: return "hold_human"` guard inside the pytest_exit=1
branch.

F2 pins the COMPLEMENTARY non-empty path: per_planned has one fair entry,
pytest_exit=1 → `coder`. A second regression-pin test inside this module
asserts the empty-list boundary (per_planned=[] + pytest_exit=1 →
hold_human) — together these two tests box in must-fix #1 from both sides.

WHAT THIS TEST DELIBERATELY DOES NOT COVER

- Real Gemma swap (F0a) or real Codex call (shadow phase).
- The pytest run itself — F2 supplies `pytest_exit=1` as a deterministic input
  representing "the synthetic test_qa.py asserts the missing behavior, ran in
  the sandbox, and failed because the coder's implementation has the bug."
  We don't materialize a coder workspace or a test_qa.py on disk.
- sandbox-exec policy (F0b/F5 cover that).
- Deep C1 schema edge cases (qa_check_pre's own tests).

NOTE on strategy-schema coverage: F2 calls `jsonschema.validate` against
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


# Two explicit-ID clauses. The bug Gemma must surface: the coder's
# implementation returns true division (a / b) but the contract requires
# floor division (a // b). The coder's own smoke test happens to use an
# exact-division input (6 / 2 == 3), so it doesn't distinguish the two.
KNOWN_BUGGY_ACCEPTANCE_CHECKS = [
    "[AC-001] divide(a: int, b: int) RETURNS the floor division a // b for b != 0.",
    "[AC-002] divide(a: int, b: int) RAISES ZeroDivisionError when b == 0.",
]


def _write_product_plan(tmp_path: Path) -> Path:
    """Write a minimal product-plan.json with two explicit-ID acceptance_checks."""
    plan = {"acceptance_checks": list(KNOWN_BUGGY_ACCEPTANCE_CHECKS)}
    p = tmp_path / "product-plan.json"
    p.write_text(json.dumps(plan, indent=2))
    return p


def _known_buggy_strategy() -> dict:
    """A Gemma-shaped strategy that plans one fair concrete probe + skips the
    other clause as earned.

    Probe shape: AC-001 (floor-div) gets a `concrete` probe that drives
    `divide(7, 2)` (odd dividend / even divisor — the case where floor and
    true division diverge: 3 vs 3.5). The coder's only test
    (`test_divide_smoke`) uses an exact-division case that can't distinguish
    the two, which the `uncovered_behavior` field calls out.

    AC-002 (zero divisor) is earned-skipped: the coder already has
    `test_divide_raises_on_zero` wrapping `divide(1, 0)` in
    `pytest.raises(ZeroDivisionError)`.

    n_tests_written == 1 — the single probe lives in
    `test_divide_floor_for_odd_dividend` (which the fixture doesn't
    materialize on disk; we only need its routing-shape).
    """
    return {
        "reviewed_clauses": ["AC-001", "AC-002"],
        "planned_tests": [
            {
                "clause_id": "AC-001",
                "probe_shape": "concrete",
                "concrete_behavior": {
                    "input": "divide(7, 2)",
                    "expected": "3 (floor of 7/2)",
                },
                "coder_test_evidence": [
                    "tests/test_divide.py::test_divide_smoke",
                ],
                "uncovered_behavior": (
                    "test_divide_smoke only checks divide(6, 2) == 3 — exact "
                    "divisions don't distinguish floor from true division, so "
                    "the floor-vs-true-div behavior the contract specifies is "
                    "unprobed."
                ),
                "asserted_in_test": "test_divide_floor_for_odd_dividend",
            },
        ],
        "skipped_clauses": [
            {
                "clause_id": "AC-002",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_divide.py::test_divide_raises_on_zero",
                ],
                "why_already_covered": (
                    "test_divide_raises_on_zero wraps divide(1, 0) in "
                    "pytest.raises(ZeroDivisionError) and parametrizes a few "
                    "nonzero numerators."
                ),
            },
        ],
        "n_tests_written": 1,
    }


def _fair_then_earned_codex_verdicts(
    planned_ids: list[str], skipped_ids: list[str]
) -> dict:
    """Codex verdicts: every planned test is `fair` with `triviality=n/a`
    (the test FAILED so the triviality question doesn't apply), every skipped
    clause is `skip_earned`, declared_route is `coder` (telemetry hint).

    The `triviality_verdict` per the Rev 5 spec is `n/a` when the test was
    not passing — that's the case here (pytest exit 1 means at least one test
    failed). Codex review #4 (round 1, qa_route.py) made triviality required
    to close a hole where a trivial passing test could route to confirm by
    omitting the field; on a failing test `n/a` is the spec-correct value.
    """
    return {
        "codex_per_planned_test": [
            {
                "clause_id": cid,
                "entailment_verdict": "fair",
                "triviality_verdict": "n/a",
                "reason": (
                    f"any compliant floor-div implementation passes the {cid} "
                    f"probe; the coder's true-div implementation fails it."
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
        "declared_route": "coder",
        "summary_reason": "fair probe surfaced a real coder bug",
        "token_usage": {"prompt": 1500, "completion": 250},
    }


def test_f2_known_buggy_fair_probe_routes_to_coder(tmp_path: Path) -> None:
    """End-to-end of the deterministic pipeline: known-buggy contract + 1 fair
    planned probe + pytest_exit=1 + 1 earned skip → route=coder.

    Exercises (in pipeline order):
      1. parse_contract_clauses reads product-plan.json with two [AC-NNN] clauses.
      2. jsonschema.validate(strategy, QA_STRATEGY_SCHEMA) — realism pin.
      3. c18a_clause_completeness — every clause cited exactly once across
         (planned ∪ skipped).
      4. validate_verdict_cardinality — Codex per-item ids multiset-match the
         strategy's ids.
      5. validate_and_compute_route with pytest_exit=1 → the
         "per_planned non-empty + all fair → coder" branch of compute_route.

    What would BREAK this assertion (regression signals):
      - compute_route's pytest_exit=1 branch flipping its condition (e.g.
        checking `any` instead of `all` for fair-ness).
      - Codex schema growing a new required field on per_planned_test items
        that this fixture doesn't supply.
      - parse_contract_clauses changing the `[AC-NNN]` format.
      - validate_and_compute_route gaining a new pre-route gate that
        unexpectedly HOLDs a well-shaped coder-bug fixture.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)
    assert [c["id"] for c in clauses] == ["AC-001", "AC-002"], (
        "F2 must parse two explicit AC-IDs; anything else means the contract "
        "format contract changed."
    )

    strategy = _known_buggy_strategy()
    # Realism pin — F1 lesson: fixture must satisfy the production C1 schema.
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass for known-buggy F2: {c18a.reason}"

    codex_verdicts = _fair_then_earned_codex_verdicts(
        planned_ids=["AC-001"], skipped_ids=["AC-002"]
    )

    cardinality = validate_verdict_cardinality(strategy, codex_verdicts)
    assert cardinality.ok, (
        f"cardinality should pass for matched ids: {cardinality.reason}"
    )

    # pytest_exit=1 represents the synthetic test_qa.py having run in the
    # sandbox and asserted the missing behavior — the coder's true-div
    # implementation fails the floor-div probe.
    qa_route = validate_and_compute_route(
        strategy, codex_verdicts, pytest_exit=1
    )

    assert qa_route["computed_route"] == "coder", (
        f"F2 known-buggy must route=coder; got "
        f"{qa_route['computed_route']!r} (summary_reason="
        f"{qa_route.get('summary_reason')!r})"
    )
    # Telemetry pins.
    assert qa_route["codex_declared_route"] == "coder"
    assert qa_route["agreement"] is True
    assert len(qa_route["codex_per_planned_test"]) == 1
    assert (
        qa_route["codex_per_planned_test"][0]["entailment_verdict"] == "fair"
    )
    assert len(qa_route["codex_per_skipped_clause"]) == 1
    assert (
        qa_route["codex_per_skipped_clause"][0]["audit_verdict"]
        == "skip_earned"
    )


def test_f2_pytest_exit_one_with_empty_planned_routes_to_hold_human(
    tmp_path: Path,
) -> None:
    """Goal must-fix #1 zero-test guard pin: per_planned=[] + pytest_exit=1
    routes to hold_human, NOT coder.

    Why this matters: without the guard, `all(... for p in [])` evaluates to
    True (Python's vacuous-truth quirk on `all` over an empty iterable). The
    `pytest_exit=1 + all fair → coder` branch would slip through with no
    failing test to point at. The guard is `if not per_planned: return
    "hold_human"` inside the pytest_exit=1 branch (qa_route.py).

    This test boxes in the COMPLEMENT of the main F2 assertion: same
    pytest_exit=1, but no planned tests. The main test pins
    per_planned-non-empty → coder; this one pins per_planned-empty →
    hold_human. Together they trace the boundary the guard creates.

    In production, C5 (qa_check_pre) prevents this state from being reached
    (n_tests_written == 0 ↔ test_qa.py absent ↔ pytest can't actually exit 1
    after the strategic-null guard, since the runner doesn't even invoke
    pytest). But compute_route defensively guards the case anyway, and this
    fixture pins that guard.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)
    # Both clauses earned-skipped — same as F1's known-clean shape, but
    # we'll feed pytest_exit=1 to exercise the defensive guard.
    strategy = {
        "reviewed_clauses": ["AC-001", "AC-002"],
        "planned_tests": [],
        "skipped_clauses": [
            {
                "clause_id": "AC-001",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_divide.py::test_divide_smoke",
                ],
                "why_already_covered": (
                    "stub coverage for the regression-pin test"
                ),
            },
            {
                "clause_id": "AC-002",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_divide.py::test_divide_raises_on_zero",
                ],
                "why_already_covered": (
                    "stub coverage for the regression-pin test"
                ),
            },
        ],
        "n_tests_written": 0,
    }
    # Realism pin — the regression-pin strategy must also schema-validate.
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)
    # Codex review (F2 round 1) nit: previously `clauses` was parsed but never
    # used. Asserting C18a on the regression-pin shape closes that gap and
    # documents that this defensive case respects the completeness invariant
    # (both contract clauses appear in skipped_clauses, neither omitted).
    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, (
        f"C18a should pass for the regression-pin strategy too: {c18a.reason}"
    )

    # Finding #8 (2026-06-07): strict-mode-equivalent. Each skipped item
    # needs `reason`; top-level needs declared_route + token_usage. Without
    # these the dict would now schema-fail and the test would return
    # hold_human via the schema-invalid path — masking the actual
    # `not per_planned + pytest_exit==1` guard we're trying to exercise.
    codex_verdicts = {
        "codex_per_planned_test": [],
        "codex_per_skipped_clause": [
            {"clause_id": "AC-001", "audit_verdict": "skip_earned", "reason": None},
            {"clause_id": "AC-002", "audit_verdict": "skip_earned", "reason": None},
        ],
        "declared_route": None,
        "summary_reason": (
            "no probes; pytest exit 1 here is a defensively-guarded "
            "impossible state in production"
        ),
        "token_usage": None,
    }

    qa_route = validate_and_compute_route(
        strategy, codex_verdicts, pytest_exit=1
    )

    assert qa_route["computed_route"] == "hold_human", (
        "must-fix #1 zero-test guard regressed: per_planned=[] + "
        "pytest_exit=1 must land on hold_human (defensive belt against the "
        "`all([]) == True` vacuous-truth quirk in compute_route's pytest_exit=1 "
        "branch). Got "
        f"{qa_route['computed_route']!r}."
    )
