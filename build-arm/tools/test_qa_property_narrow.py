"""Property-narrow-domain fixture: trivially-passing property probe → route=gemma.

WHY THIS EXISTS (Rev 5 spec, "Failure modes" + the property-test shape):

  > Gemma writes trivial tests | passes pytest; Codex triviality: trivial |
  >   route=gemma → HOLD (v1)

The Rev 5 schema adds a third probe shape — `property` — for bounded
property tests (determinism, idempotence, order invariance, round-trip).
The QA_STRATEGY_SCHEMA constrains `sample_bound <= 20` and the spec
caps the examples list at 10. But the schema does NOT enforce a MINIMUM
sample_bound, so `sample_bound: 1` over `examples_or_generator: "[0]"`
is structurally valid: a property probe over a one-element domain.

That probe is TRIVIAL: idempotence over `[0]` only asks
`f(f(0)) == f(0)`. While that DOES distinguish some buggy
implementations (e.g. one where f(x) = x+1 fails for x=0 because
f(f(0)) = 2 but f(0) = 1), it COMPLETELY MISSES the obvious bad-
normalizer class — any implementation that ignores its input and
returns a constant (e.g. `def normalize_int(x): return 0`) passes
trivially. A one-point probe can't probe what a normalizer actually
does. Codex's audit catches this: `triviality_verdict=trivial`.
compute_route's trivial branch then fires before the pytest_exit=0
confirm branch, routing to `gemma`.

WHAT THIS TEST COVERS

End-to-end of the deterministic routing pipeline:

  product-plan.json  ─▶ parse_contract_clauses     ─▶ 1 clause
                       (real file I/O)

  (synthetic Gemma)  ─▶ qa-strategy.json (in-memory)
                                                    ├▶ jsonschema.validate           → ok
                                                    │   (proves the schema ALLOWS sample_bound=1)
                                                    ├▶ c18a_clause_completeness      → ok
                                                    └▶ validate_verdict_cardinality  → ok

  (synthetic Codex)  ─▶ codex_verdicts (in-memory)
                                                    └▶ validate_and_compute_route(
                                                          strategy, verdicts,
                                                          pytest_exit=0,
                                                       ) → computed_route == "gemma"

WHY THE TRIVIALITY BRANCH ORDERING IS LOAD-BEARING

compute_route checks branches in this order:
  5. `triviality_verdict == "trivial" in per_planned` → gemma.
  ...
  8. `pytest_exit == 0`                              → confirm.

The trivial check fires BEFORE the pytest_exit=0 confirm branch. Without
that ordering, a trivial probe that happens to pass would route to
`confirm` — exactly the failure mode F4 + this fixture surface.

A second regression-pin test inside this module asserts the inverse: a
property probe with sample_bound=20 (the schema's MAX) over a meaningful
domain + Codex triviality_verdict=substantive routes to `confirm`. This
shows the trivial-vs-substantive judgment is the deciding signal, not the
schema's structural validity. Both ends of the property-shape envelope
work as designed.

WHAT THIS TEST DELIBERATELY DOES NOT COVER

- Real Gemma swap (F0a) or real Codex call (shadow phase).
- The actual pytest run — F supplies `pytest_exit=0` as a deterministic input.
- sandbox-exec policy (F0b/F5).
- Deep C1 schema edge cases (qa_check_pre's own tests).

NOTE on schema realism: this fixture's jsonschema.validate call serves a
DIFFERENT realism purpose than F1-F4's. Here it actively PROVES that the
sample_bound=1 strategy is schema-valid — that's the load-bearing
precondition for the trivial-content-but-valid-schema scenario. If a
future schema tightening adds `minimum: 2` to sample_bound, this fixture
fails (signaling the constraint should be moved here or the routing
behavior re-evaluated).
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


# Single explicit-ID clause. The property invariant is idempotence — a
# common property-test shape. The contract describes a normalize-int
# function (canonicalize an integer's representation; idempotence is the
# spec's defining property).
PROPERTY_NARROW_ACCEPTANCE_CHECKS = [
    "[AC-001] normalize_int(x: int) is IDEMPOTENT: "
    "normalize_int(normalize_int(x)) == normalize_int(x) for any int x.",
]


def _write_product_plan(tmp_path: Path) -> Path:
    plan = {"acceptance_checks": list(PROPERTY_NARROW_ACCEPTANCE_CHECKS)}
    p = tmp_path / "product-plan.json"
    p.write_text(json.dumps(plan, indent=2))
    return p


def _narrow_property_strategy() -> dict:
    """Gemma plans a property probe with a DEGENERATE one-element domain.

    The schema accepts sample_bound=1 (it only caps `<= 20`, no minimum).
    examples_or_generator='[0]' means only x=0 is probed; the idempotence
    invariant `f(f(0)) == f(0)` does pass some incorrect implementations
    (notably anything constant-valued or ignoring the input, like
    `def normalize_int(x): return 0`), even though it fails others
    (`f(x) = x + 1` doesn't survive). A one-point probe can't tell whether
    the normalizer actually does anything meaningful. That makes it
    TRIVIAL in the spec sense: it lets an obvious bad-normalizer class
    pass without probing what the function should compute.
    """
    return {
        "reviewed_clauses": ["AC-001"],
        "planned_tests": [
            {
                "clause_id": "AC-001",
                "probe_shape": "property",
                "property_behavior": {
                    "domain": "x: int",
                    "examples_or_generator": "[0]",
                    "invariant": "idempotence: f(f(x)) == f(x)",
                    "sample_bound": 1,
                },
                "coder_test_evidence": [
                    "tests/test_normalize.py::test_normalize_int_smoke",
                ],
                "uncovered_behavior": (
                    "the coder's smoke test only checks normalize_int(5); "
                    "the contract's idempotence claim is unprobed."
                ),
                "asserted_in_test": "test_normalize_int_idempotent",
            },
        ],
        "skipped_clauses": [],
        "n_tests_written": 1,
    }


def _broad_property_strategy() -> dict:
    """Comparison strategy: a Codex-judged substantive property probe.

    NOTE on the substantive judgment: idempotence alone (whether over [0]
    or over 10 diverse integers) doesn't authoritatively distinguish a
    constant-returning implementation from a real normalizer — both are
    idempotent everywhere. The "substantive" verdict in this fixture is
    therefore an OPAQUE Codex call: in production, Codex weighs the
    probe's domain, invariant, and surrounding strategy holistically and
    decides. The fixture's load-bearing claim is the ROUTING reaction to
    that verdict (substantive + pytest_exit=0 → confirm), not the
    content-correctness of Codex's judgment itself. The shadow phase is
    where Codex's audit calibration is verified against real builds.

    The probe widens the domain to 10 examples + sample_bound=20 (the
    schema's max) so it represents the upper end of what the property-
    shape envelope can express within v1 constraints.
    """
    return {
        "reviewed_clauses": ["AC-001"],
        "planned_tests": [
            {
                "clause_id": "AC-001",
                "probe_shape": "property",
                "property_behavior": {
                    "domain": "x: int (signed, including zero and large)",
                    "examples_or_generator": (
                        "[-1000000, -1, 0, 1, 7, 42, 256, 1000, 99999, "
                        "1000000]"
                    ),
                    "invariant": "idempotence: f(f(x)) == f(x)",
                    "sample_bound": 20,
                },
                "coder_test_evidence": [
                    "tests/test_normalize.py::test_normalize_int_smoke",
                ],
                "uncovered_behavior": (
                    "the coder's smoke test only checks normalize_int(5); "
                    "this probe checks idempotence across a meaningful "
                    "signed-int domain."
                ),
                "asserted_in_test": "test_normalize_int_idempotent_broad",
            },
        ],
        "skipped_clauses": [],
        "n_tests_written": 1,
    }


def _trivial_codex_verdicts() -> dict:
    """Codex audits the narrow-domain probe as trivial.

    For a passing test (pytest_exit=0), triviality_verdict is REQUIRED to
    be either "substantive" or "trivial" (per the spec section "Per-
    passing-test triviality_verdict"). Here the probe is content-trivial
    even though it's structurally valid → trivial. entailment_verdict is
    "fair" — within the contract's bounds; the issue is content not scope.
    """
    return {
        "codex_per_planned_test": [
            {
                "clause_id": "AC-001",
                "entailment_verdict": "fair",
                "triviality_verdict": "trivial",
                "reason": (
                    "domain is a single example {0}; idempotence over a "
                    "one-element domain doesn't probe what the normalizer "
                    "actually computes — any constant/ignore-input "
                    "implementation (e.g. always returns 0) passes "
                    "trivially. A bounded probe must extend the domain."
                ),
            },
        ],
        "codex_per_skipped_clause": [],
        "declared_route": "gemma",
        "summary_reason": (
            "property probe is content-trivial despite schema-valid "
            "structure; Gemma must extend the domain to make the invariant "
            "meaningful"
        ),
        "token_usage": {"prompt": 1600, "completion": 250},
    }


def _substantive_codex_verdicts() -> dict:
    """Codex's (opaque-here) audit lands on substantive for the broad probe.

    The fixture pins the ROUTING reaction (substantive + pytest_exit=0 →
    confirm), not the content correctness of this judgment. The reason
    field stays brief and acknowledges Codex's audit as the authority.
    """
    return {
        "codex_per_planned_test": [
            {
                "clause_id": "AC-001",
                "entailment_verdict": "fair",
                "triviality_verdict": "substantive",
                "reason": (
                    "10 diverse signed-int examples + sample_bound=20 "
                    "represent the upper end of the v1 property-shape "
                    "envelope; Codex audits this as substantive."
                ),
            },
        ],
        "codex_per_skipped_clause": [],
        "declared_route": "confirm",
        "summary_reason": (
            "property probe substantively judged; pytest passed; confirm"
        ),
        "token_usage": {"prompt": 1500, "completion": 220},
    }


def test_property_narrow_domain_routes_to_gemma_via_trivial_branch(
    tmp_path: Path,
) -> None:
    """Schema-valid sample_bound=1 over [0] + Codex triviality=trivial
    → route=gemma via the trivial branch (fires before pytest_exit=0
    confirm).

    Pipeline order:
      1. parse_contract_clauses reads product-plan.json (one [AC-NNN] clause).
      2. jsonschema.validate(strategy, QA_STRATEGY_SCHEMA) — proves the
         schema ALLOWS sample_bound=1 (this is the LOAD-BEARING realism
         pin for this fixture: the trivial-content + valid-schema scenario
         only exists because the schema doesn't have a sample_bound
         minimum constraint).
      3. c18a_clause_completeness — the single clause appears in planned.
      4. validate_verdict_cardinality — Codex verdict ids match strategy.
      5. validate_and_compute_route with pytest_exit=0 → trivial branch
         fires before confirm.

    What this pins: the routing trust property catches content-trivial
    probes even when they pass structural validation. If a future change
    accidentally moved the trivial check after the pytest_exit=0 confirm
    branch (a misguided "exit=0 → confirm short-circuit" optimization),
    this fixture would route to confirm and fail the assertion.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)
    assert [c["id"] for c in clauses] == ["AC-001"], (
        "property-narrow fixture must parse one [AC-001] clause"
    )

    strategy = _narrow_property_strategy()
    # LOAD-BEARING realism pin: prove the schema accepts sample_bound=1.
    # If a future schema tightening adds `minimum: 2`, jsonschema.validate
    # raises ValidationError — the fixture's "valid schema but trivial
    # content" scenario no longer exists at that surface and the test
    # legitimately fails. That's the right signal: the constraint moved.
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass: {c18a.reason}"

    codex_verdicts = _trivial_codex_verdicts()
    cardinality = validate_verdict_cardinality(strategy, codex_verdicts)
    assert cardinality.ok, f"cardinality: {cardinality.reason}"

    qa_route = validate_and_compute_route(
        strategy, codex_verdicts, pytest_exit=0
    )

    assert qa_route["computed_route"] == "gemma", (
        f"property-narrow-domain must route=gemma via the trivial "
        f"branch (fires before pytest_exit=0 confirm); got "
        f"{qa_route['computed_route']!r} (summary_reason="
        f"{qa_route.get('summary_reason')!r})"
    )
    # Telemetry pins.
    assert qa_route["codex_declared_route"] == "gemma"
    assert qa_route["agreement"] is True
    assert len(qa_route["codex_per_planned_test"]) == 1
    assert (
        qa_route["codex_per_planned_test"][0]["triviality_verdict"]
        == "trivial"
    )


def test_property_broad_domain_routes_to_confirm_via_substantive_pass(
    tmp_path: Path,
) -> None:
    """Inverse: broad domain + Codex triviality=substantive +
    pytest_exit=0 → route=confirm.

    Same property-probe schema shape (sample_bound at the max,
    examples_or_generator enumerates 10 diverse integers); the
    trivial-vs-substantive judgment is the deciding signal.

    This pins that the property-shape ENVELOPE itself isn't the problem
    — well-shaped property probes route to confirm. If a future routing
    regression made `probe_shape=property` automatically suspect (e.g.
    "all property probes route to gemma until reviewer approves"), this
    fixture would fail.

    Note on the substantive judgment: in production Codex weighs the
    probe holistically and decides; the fixture pins the routing
    reaction to that decision, not its content correctness. See
    `_broad_property_strategy` docstring for context.
    """
    plan_path = _write_product_plan(tmp_path)
    clauses = parse_contract_clauses(plan_path)

    strategy = _broad_property_strategy()
    jsonschema.validate(strategy, QA_STRATEGY_SCHEMA)

    c18a = c18a_clause_completeness(strategy, clauses)
    assert c18a.ok, f"C18a should pass: {c18a.reason}"

    codex_verdicts = _substantive_codex_verdicts()
    cardinality = validate_verdict_cardinality(strategy, codex_verdicts)
    assert cardinality.ok, f"cardinality: {cardinality.reason}"

    qa_route = validate_and_compute_route(
        strategy, codex_verdicts, pytest_exit=0
    )

    assert qa_route["computed_route"] == "confirm", (
        f"property-broad-domain must route=confirm (substantive probe + "
        f"pytest_exit=0); got {qa_route['computed_route']!r} "
        f"(summary_reason={qa_route.get('summary_reason')!r})"
    )
    assert qa_route["codex_declared_route"] == "confirm"
    assert qa_route["agreement"] is True
    assert (
        qa_route["codex_per_planned_test"][0]["triviality_verdict"]
        == "substantive"
    )
