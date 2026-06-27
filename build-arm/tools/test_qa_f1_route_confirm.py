"""F1 — known-clean fixture: strategic null → route=confirm.

WHY THIS EXISTS (Rev 5 spec, "Seeded fixtures + canary"):

  > F1 known-clean: every clause in skipped_clauses; route=confirm.

F1 is the first of the F1–F5 seeded fixtures the spec requires to pass 4/4
(plus the F5 sandbox-denial suite) before the QA layer goes live. The setup
mimics a contract whose code is already legitimately covered by the coder's
existing tests — Gemma's correct response is to write zero tests and put every
contract clause into `skipped_clauses` with sound coder-evidence citations.
Codex audits each citation as `skip_earned`. The runner's deterministic
`compute_route` then lands on `confirm`.

WHAT THIS TEST COVERS

End-to-end of the deterministic routing pipeline below the model boundaries:

  product-plan.json  ─▶ parse_contract_clauses        ─▶ contract_clauses
                       (real file I/O)

  (synthetic Gemma)  ─▶ qa-strategy.json (in-memory)
                                                       ├▶ c18a_clause_completeness        → ok
                                                       └▶ validate_verdict_cardinality    → ok

  (synthetic Codex)  ─▶ codex_verdicts (in-memory)
                                                       └▶ validate_and_compute_route(
                                                              strategy, verdicts,
                                                              pytest_exit=<from Step 12>,
                                                          ) → computed_route == "confirm"

  Step 12 guard      ─▶ _resolve_pytest_results(strategy, runner)
                       Asserts the strategic-null short-circuit does NOT call
                       the pytest runner thunk and synthesizes `exit: 0`.

WHAT THIS TEST DELIBERATELY DOES NOT COVER

- A real Gemma swap + invocation. That is F0a's job and is stochastic — fixtures
  must be deterministic for the 4/4 acceptance signal.
- A real Codex call. The shadow-mode phase (spec step 11) tests Codex's live
  judgement on historical builds; F1 pins the deterministic wiring only.
- sandbox-exec policy. F0b proves pytest runs cleanly inside the jail; F5
  proves malicious tests are denied. F1 stays out of that surface.

NOTE on strategy-schema coverage: F1 does call jsonschema.validate against
QA_STRATEGY_SCHEMA as a REALISM PIN — to catch fixture drift if the production
schema tightens (e.g. a new required field) — but it deliberately does NOT
reproduce qa_check_pre's deep C1 unit tests for edge cases (oneOf branches,
type confusion, etc.). Those remain owned by tools/test_qa_check_pre.py.

WHY THE STRATEGIC-NULL CASE IS THE LOAD-BEARING CHECK

The Rev 5 spec advertises strategic null (`n_tests_written == 0`; every clause
earned-skipped) as a valid `confirm` outcome. Until commit `9822279` the runner
unconditionally invoked pytest, which exited 4 on the missing `test_qa.py`,
silently burying that outcome. F1 pins the corrected wiring: a known-clean
fixture must land on `confirm`, never on `hold_human`. If a future change
re-introduces the gap (or makes Codex's `declared_route` load-bearing again),
F1 fires.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

# Use the `tools.X` import path (matches the rest of the test suite) so the test
# suite and production code load the same module identities.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.qa import _resolve_pytest_results  # noqa: E402
from tools.qa_check_pre import (  # noqa: E402
    QA_STRATEGY_SCHEMA,
    c18a_clause_completeness,
)
from tools.qa_clauses import parse_contract_clauses  # noqa: E402
from tools.qa_route import (  # noqa: E402
    validate_and_compute_route,
    validate_verdict_cardinality,
)


# Three explicit-ID clauses — small enough to read, large enough to be
# non-trivial. The `[AC-NNN]` prefix is the contract format `parse_contract_clauses`
# resolves into stable IDs.
KNOWN_CLEAN_ACCEPTANCE_CHECKS = [
    "[AC-001] add(a: int, b: int) RETURNS a + b for any two finite integers.",
    "[AC-002] add(a, b) RAISES TypeError when either argument is not an int.",
    "[AC-003] add is commutative: add(a, b) == add(b, a) for any pair of ints.",
]


def _write_product_plan(tmp_path: Path) -> Path:
    """Write a minimal product-plan.json with three explicit-ID acceptance_checks.

    Only the `acceptance_checks` field matters for clause parsing — the rest of
    the product-plan schema is irrelevant to the routing pipeline F1 exercises.
    """
    plan = {"acceptance_checks": list(KNOWN_CLEAN_ACCEPTANCE_CHECKS)}
    p = tmp_path / "product-plan.json"
    p.write_text(json.dumps(plan, indent=2))
    return p


def _known_clean_strategy() -> dict:
    """A Gemma-shaped strategy where every clause is concrete-skipped with a citation.

    This is the well-behaved output for a contract whose code is already covered
    by the coder's tests. `n_tests_written == 0` (strategic null) and every
    skipped clause carries `coder_test_evidence` plus a brief `why_already_covered`
    explanation Codex can audit.
    """
    return {
        "reviewed_clauses": ["AC-001", "AC-002", "AC-003"],
        "planned_tests": [],
        "skipped_clauses": [
            {
                "clause_id": "AC-001",
                "abstract_or_concrete": "concrete",
                # Codex review (round 1) flagged that the prose mentioned a
                # second test without citing it; the strategy MUST cite every
                # test it leans on so build_route_input can stitch the source
                # for Codex. Both are now cited explicitly.
                "coder_test_evidence": [
                    "tests/test_add.py::test_add_positive",
                    "tests/test_add.py::test_add_signed",
                ],
                "why_already_covered": (
                    "test_add_positive asserts add(2, 3) == 5; "
                    "test_add_signed parametrizes negative and zero cases. "
                    "Together they cover the `any two finite integers` domain "
                    "of AC-001."
                ),
            },
            {
                "clause_id": "AC-002",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_add.py::test_add_type_error"
                ],
                "why_already_covered": (
                    "test_add_type_error uses pytest.raises(TypeError) on "
                    "add('x', 1) and add(1, None)."
                ),
            },
            {
                "clause_id": "AC-003",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": [
                    "tests/test_add.py::test_add_commutative"
                ],
                "why_already_covered": (
                    "test_add_commutative loops a small set of (a, b) pairs and "
                    "asserts add(a, b) == add(b, a)."
                ),
            },
        ],
        "n_tests_written": 0,
    }


def _all_skip_earned_codex_verdicts(clause_ids: list[str]) -> dict:
    """A Codex verdict bundle where every skipped clause is audited as skip_earned.

    Each per-item judgment is the well-behaved outcome when Gemma's cited coder
    evidence genuinely covers the clause. The declared_route is `confirm` (a
    telemetry-only field per goal must-fix 4); the runner never uses it for
    routing but stores it for forensics.
    """
    return {
        "codex_per_planned_test": [],
        "codex_per_skipped_clause": [
            {
                "clause_id": cid,
                "audit_verdict": "skip_earned",
                "reason": f"cited coder test covers {cid} as claimed",
            }
            for cid in clause_ids
        ],
        "declared_route": "confirm",
        "summary_reason": "every clause earned-skipped; no probes needed",
        "token_usage": {"prompt": 1234, "completion": 234},
    }


def test_f1_known_clean_strategic_null_routes_to_confirm(tmp_path: Path) -> None:
    """End-to-end of the deterministic pipeline: known-clean → strategic null → confirm.

    Exercises (in pipeline order):
      1. parse_contract_clauses reads product-plan.json from disk and returns
         the three `AC-NNN` clauses.
      2. c18a_clause_completeness verifies every clause appears exactly once
         across (planned ∪ skipped). For F1 that's all three in `skipped_clauses`.
      3. _resolve_pytest_results (the strategic-null guard, commit 9822279)
         short-circuits Step 12: returns `exit: 0` WITHOUT calling the runner
         thunk. A regression that re-introduces the unconditional pytest call
         would either (a) invoke the thunk here (caught by `runner_calls`) or
         (b) get exit 4 from the real runner (would still land on hold_human
         and break the route assertion).
      4. validate_verdict_cardinality verifies Codex's per-item ids multiset-
         match the strategy's ids.
      5. validate_and_compute_route runs the upfront schema check + cardinality
         + the pure compute_route function. The result must be `confirm`.

    What would BREAK this assertion (regression signals):
      - A new pre-condition gate added in front of validate_and_compute_route
        without the strategic-null path being plumbed through it.
      - compute_route gaining a new "hold_human" branch that fires for n=0
        without unrepresented clauses.
      - The Codex schema growing a new required field on skipped-clause verdicts
        that this fixture doesn't provide.
      - parse_contract_clauses changing its clause-ID format and breaking the
        '[AC-NNN]' contract this fixture relies on.
      - The strategic-null guard regressing (Step 12 invokes pytest on a missing
        test_qa.py and exits 4).
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)
    assert [c["id"] for c in clauses] == ["AC-001", "AC-002", "AC-003"], (
        "parse_contract_clauses must yield the three explicit AC-IDs for F1; "
        "anything else means the contract format contract changed."
    )

    strategy = _known_clean_strategy()
    codex_verdicts = _all_skip_earned_codex_verdicts(
        [c["id"] for c in clauses]
    )

    # (a0) realism: the fixture strategy must satisfy the production C1 schema.
    # Codex review (round 1) noted that without this pin the fixture could drift
    # from QA_STRATEGY_SCHEMA and silently become unrepresentative of what the
    # runner's C1 actually accepts. Validate the same way C1 does so any future
    # schema tightening (e.g. a new required field) fails F1 first, not in prod.
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    # (a) completeness invariant — every clause covered exactly once.
    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass for known-clean F1: {c18a.reason}"

    # (b) Step 12 strategic-null guard: synthesize a clean exit, do NOT call the runner.
    runner_calls: list[str] = []

    def _should_not_be_called() -> dict:
        # If the guard regresses, this thunk runs and we'd record the call.
        runner_calls.append("strategic_null_guard_regressed")
        return {"exit": 4, "nodeids": [], "failures": []}

    pytest_results = _resolve_pytest_results(strategy, _should_not_be_called)
    assert runner_calls == [], (
        "strategic-null guard regressed: pytest runner was invoked for n=0"
    )
    assert pytest_results["exit"] == 0, (
        "strategic-null guard must synthesize exit=0, not "
        f"{pytest_results['exit']}"
    )

    # (c) cardinality — Codex per-item ids multiset-match the strategy's ids.
    cardinality = validate_verdict_cardinality(strategy, codex_verdicts)
    assert cardinality.ok, (
        f"cardinality should pass for matched ids: {cardinality.reason}"
    )

    # (d) the deterministic route.
    qa_route = validate_and_compute_route(
        strategy,
        codex_verdicts,
        pytest_exit=pytest_results["exit"],
    )

    assert qa_route["computed_route"] == "confirm", (
        f"F1 known-clean must route=confirm; got "
        f"{qa_route['computed_route']!r} (summary_reason="
        f"{qa_route.get('summary_reason')!r})"
    )
    # Telemetry: Codex declared `confirm` and agreement is recorded as True.
    assert qa_route["codex_declared_route"] == "confirm"
    assert qa_route["agreement"] is True
    assert qa_route["codex_per_planned_test"] == []
    assert len(qa_route["codex_per_skipped_clause"]) == 3


def test_f1_strategic_null_routes_to_confirm_when_codex_omits_declared_route(
    tmp_path: Path,
) -> None:
    """Pin goal must-fix #4: `declared_route` is TELEMETRY ONLY.

    Codex strict mode (finding #8, 2026-06-07) REQUIRES declared_route in the
    response, but allows `null`. `null` is the strict-mode equivalent of the
    old "field absent" — the runner must NOT depend on Codex authoring a real
    route. A future refactor that accidentally made declared_route mandatory
    or load-bearing would fail this test.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)
    strategy = _known_clean_strategy()
    verdicts = _all_skip_earned_codex_verdicts(
        [c["id"] for c in clauses]
    )
    # Set to None (strict-mode "no declaration") rather than popping the key —
    # the schema requires the field to be present.
    verdicts["declared_route"] = None

    qa_route = validate_and_compute_route(
        strategy, verdicts, pytest_exit=0
    )
    assert qa_route["computed_route"] == "confirm", (
        "compute_route must not depend on Codex's declared_route field"
    )
    # declared_route=None → agreement is None (runner can't compare).
    assert qa_route["codex_declared_route"] is None
    assert qa_route["agreement"] is None
