"""Tests for tools.qa_route — compute_route, cardinality, build_route_input, validate_and_compute_route.

`call_codex_route` is NOT exercised here (it requires a live Codex token); the runner's
end-to-end test will integration-cover it.
"""

import json
from pathlib import Path

import pytest

from tools.qa_route import (
    CODEX_VERDICT_SCHEMA,
    _extract_function_sources,
    build_route_input,
    compute_route,
    validate_and_compute_route,
    validate_verdict_cardinality,
)


# =============================================================================
# Helpers
# =============================================================================


def _p(
    clause_id: str,
    entailment: str = "fair",
    triviality: str = "substantive",
    reason: str | None = None,
) -> dict:
    # Finding #8 (2026-06-07): `reason` is required + nullable under OpenAI
    # strict mode. Test fixtures default to None — actual content doesn't
    # matter for routing assertions but the field MUST be present.
    return {
        "clause_id": clause_id,
        "entailment_verdict": entailment,
        "triviality_verdict": triviality,
        "reason": reason,
    }


def _s(clause_id: str, audit: str = "skip_earned", reason: str | None = None) -> dict:
    return {"clause_id": clause_id, "audit_verdict": audit, "reason": reason}


def _v(
    planned: list | None = None,
    skipped: list | None = None,
    declared_route: str | None = None,
    summary_reason: str | None = None,
    token_usage: dict | None = None,
) -> dict:
    """Build a minimum-strict-mode-compliant codex_verdicts dict.

    Finding #8 (2026-06-07): top-level required fields are
    {codex_per_planned_test, codex_per_skipped_clause, declared_route,
    summary_reason, token_usage}. Default to empty/None so tests stay
    focused on the route logic instead of fixture boilerplate.
    """
    return {
        "codex_per_planned_test": planned or [],
        "codex_per_skipped_clause": skipped or [],
        "declared_route": declared_route,
        "summary_reason": summary_reason,
        "token_usage": token_usage,
    }


# =============================================================================
# compute_route — table-driven coverage of every branch
# =============================================================================


def test_compute_route_confirm_happy_path():
    """All fair, all skip_earned, pytest passed → confirm."""
    pp = [_p("AC-001"), _p("AC-002")]
    sp = [_s("AC-003"), _s("AC-004")]
    assert compute_route(pp, sp, pytest_exit=0, unrepresented_clauses=[]) == "confirm"


def test_compute_route_unrepresented_holds_human():
    pp = [_p("AC-001")]
    sp = []
    assert compute_route(pp, sp, pytest_exit=0, unrepresented_clauses=["AC-002"]) == "hold_human"


def test_compute_route_uncertain_entailment_holds_human():
    pp = [_p("AC-001", entailment="uncertain")]
    assert compute_route(pp, [], pytest_exit=0, unrepresented_clauses=[]) == "hold_human"


def test_compute_route_uncertain_audit_holds_human():
    sp = [_s("AC-001", audit="uncertain")]
    assert compute_route([], sp, pytest_exit=0, unrepresented_clauses=[]) == "hold_human"


def test_compute_route_overreach_routes_gemma():
    pp = [_p("AC-001", entailment="overreach")]
    assert compute_route(pp, [], pytest_exit=0, unrepresented_clauses=[]) == "gemma"


def test_compute_route_trivial_routes_gemma():
    pp = [_p("AC-001", entailment="fair", triviality="trivial")]
    assert compute_route(pp, [], pytest_exit=0, unrepresented_clauses=[]) == "gemma"


def test_compute_route_skip_hollow_routes_gemma():
    sp = [_s("AC-001", audit="skip_hollow")]
    assert compute_route([], sp, pytest_exit=0, unrepresented_clauses=[]) == "gemma"


def test_compute_route_pytest_failed_all_fair_routes_coder():
    """A real bug: pytest failed, Gemma's tests fairly probed → coder gets the fix."""
    pp = [_p("AC-001"), _p("AC-002")]
    assert compute_route(pp, [], pytest_exit=1, unrepresented_clauses=[]) == "coder"


def test_compute_route_pytest_failed_no_planned_holds_human_must_fix_1():
    """Goal must-fix 1: zero-test guard. all([]) is True, so without the guard a
    pytest failure with no planned tests would route to 'coder' incorrectly.
    With the guard it routes to 'hold_human'."""
    assert compute_route([], [], pytest_exit=1, unrepresented_clauses=[]) == "hold_human"


def test_compute_route_pytest_failed_mixed_verdicts_holds_human():
    """Some fair, some still uncertain (caught earlier) — but if all fair on a mixed
    set, the loop in the previous test wouldn't fire; verify a mix of fair+overreach
    fired before reaching pytest_exit branch."""
    pp = [_p("AC-001"), _p("AC-002", entailment="overreach")]
    # Overreach fires BEFORE pytest_exit check; routes to gemma.
    assert compute_route(pp, [], pytest_exit=1, unrepresented_clauses=[]) == "gemma"


def test_compute_route_no_tests_no_skips_pytest_passed_confirms():
    """The strategic-null case: Gemma wrote zero tests and skipped nothing (vacuous
    strategy). With pytest exit 0 → confirm. This is also the case where the C18a
    completeness invariant should have already failed pre-pytest if the contract had
    any clauses; the compute_route function itself doesn't enforce that."""
    assert compute_route([], [], pytest_exit=0, unrepresented_clauses=[]) == "confirm"


def test_compute_route_strategic_null_all_skip_earned_confirms():
    """Spec's acceptance criterion: ≥ 1 real build confirms with n_tests_written == 0
    when every clause goes to skipped_clauses with skip_earned."""
    sp = [_s("AC-001"), _s("AC-002"), _s("AC-003")]
    assert compute_route([], sp, pytest_exit=0, unrepresented_clauses=[]) == "confirm"


def test_compute_route_priority_uncertain_over_overreach():
    """If both uncertain and overreach are present, uncertain wins (hold_human is the
    safer outcome for a single mixed run)."""
    pp = [
        _p("AC-001", entailment="uncertain"),
        _p("AC-002", entailment="overreach"),
    ]
    assert compute_route(pp, [], pytest_exit=0, unrepresented_clauses=[]) == "hold_human"


def test_compute_route_unrepresented_dominates_everything():
    """Unrepresented clause fires first regardless of all other verdicts."""
    pp = [_p("AC-001")]
    sp = [_s("AC-002")]
    assert compute_route(pp, sp, pytest_exit=0, unrepresented_clauses=["AC-003"]) == "hold_human"


# =============================================================================
# validate_verdict_cardinality — goal must-fix 2
# =============================================================================


def _strategy_with_ids(planned_ids: list[str], skipped_ids: list[str]) -> dict:
    return {
        "n_tests_written": len(planned_ids),
        "planned_tests": [{"clause_id": cid} for cid in planned_ids],
        "skipped_clauses": [{"clause_id": cid} for cid in skipped_ids],
    }


def _verdicts_with_ids(planned_ids: list[str], skipped_ids: list[str]) -> dict:
    return {
        "codex_per_planned_test": [_p(cid) for cid in planned_ids],
        "codex_per_skipped_clause": [_s(cid) for cid in skipped_ids],
    }


def test_cardinality_exact_match_ok():
    s = _strategy_with_ids(["AC-001"], ["AC-002"])
    v = _verdicts_with_ids(["AC-001"], ["AC-002"])
    assert validate_verdict_cardinality(s, v).ok


def test_cardinality_extra_codex_planned_held():
    s = _strategy_with_ids(["AC-001"], ["AC-002"])
    v = _verdicts_with_ids(["AC-001", "AC-EXTRA"], ["AC-002"])
    r = validate_verdict_cardinality(s, v)
    assert not r.ok and "planned-test verdict ids mismatch" in r.reason


def test_cardinality_missing_codex_planned_held():
    s = _strategy_with_ids(["AC-001", "AC-002"], [])
    v = _verdicts_with_ids(["AC-001"], [])
    r = validate_verdict_cardinality(s, v)
    assert not r.ok


def test_cardinality_codex_skipped_mismatch_held():
    s = _strategy_with_ids([], ["AC-001"])
    v = _verdicts_with_ids([], ["AC-002"])
    r = validate_verdict_cardinality(s, v)
    assert not r.ok and "skipped-clause" in r.reason


def test_cardinality_duplicate_codex_verdicts_held():
    """Two verdicts for the same clause id → Counter rejects."""
    s = _strategy_with_ids(["AC-001"], [])
    v = _verdicts_with_ids(["AC-001", "AC-001"], [])
    r = validate_verdict_cardinality(s, v)
    assert not r.ok


def test_cardinality_empty_strategy_empty_verdicts_ok():
    s = _strategy_with_ids([], [])
    v = _verdicts_with_ids([], [])
    assert validate_verdict_cardinality(s, v).ok


# =============================================================================
# build_route_input + _extract_function_sources
# =============================================================================


def test_extract_function_sources_basic():
    src = (
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "def test_b():\n"
        '    """[AC-002] check x."""\n'
        "    assert 1 + 1 == 2\n"
    )
    out = _extract_function_sources(src, {"test_a", "test_b"})
    assert "test_a" in out
    assert "test_b" in out
    assert "assert True" in out["test_a"]
    assert "[AC-002]" in out["test_b"]


def test_extract_function_sources_empty_source():
    assert _extract_function_sources("", {"test_a"}) == {}


def test_extract_function_sources_no_matching_names():
    src = "def test_a():\n    assert True\n"
    assert _extract_function_sources(src, {"test_other"}) == {}


def test_extract_function_sources_syntax_error_returns_empty():
    src = "def test_a(:\n  assert True\n"  # syntax error
    assert _extract_function_sources(src, {"test_a"}) == {}


def test_build_route_input_full_shape():
    strategy = {
        "n_tests_written": 1,
        "planned_tests": [
            {
                "clause_id": "AC-002",
                "asserted_in_test": "test_x",
                "coder_test_evidence": ["test_graph.py::test_basic"],
            }
        ],
        "skipped_clauses": [],
    }
    src = "def test_x():\n    assert True\n"
    ri = build_route_input(
        contract_clauses=[{"id": "AC-001", "text": "x"}, {"id": "AC-002", "text": "y"}],
        strategy=strategy,
        test_qa_source=src,
        pytest_results={"exit": 0, "nodeids": ["test_qa.py::test_x"]},
        coder_test_sources={"test_graph.py::test_basic": "def test_basic():\n    ..."},
    )
    assert ri["contract_clauses"][0]["id"] == "AC-001"
    assert ri["qa_strategy"] == strategy
    assert ri["test_qa_source"] == src
    assert "test_x" in ri["asserted_in_test_sources"]
    assert "test_graph.py::test_basic" in ri["coder_test_sources"]
    assert ri["pytest_results"]["exit"] == 0


def test_build_route_input_no_asserted_in_test_skipped():
    strategy = {"planned_tests": [], "skipped_clauses": []}
    ri = build_route_input(
        contract_clauses=[],
        strategy=strategy,
        test_qa_source="",
        pytest_results={},
        coder_test_sources={},
    )
    assert ri["asserted_in_test_sources"] == {}


# =============================================================================
# validate_and_compute_route — the top-level orchestrator
# =============================================================================


def test_validate_and_compute_full_confirm():
    s = _strategy_with_ids(["AC-001"], ["AC-002"])
    v = _v(
        planned=[_p("AC-001")],
        skipped=[_s("AC-002")],
        declared_route="confirm",
        summary_reason="looks good",
        token_usage={"prompt": 100, "completion": 50},
    )
    out = validate_and_compute_route(s, v, pytest_exit=0)
    assert out["computed_route"] == "confirm"
    assert out["codex_declared_route"] == "confirm"
    assert out["agreement"] is True
    assert out["summary_reason"] == "looks good"


def test_validate_and_compute_cardinality_fail_holds():
    s = _strategy_with_ids(["AC-001"], [])
    v = _v(planned=[_p("AC-EXTRA")])
    out = validate_and_compute_route(s, v, pytest_exit=0)
    assert out["computed_route"] == "hold_human"
    assert "cardinality" in out["summary_reason"]


def test_validate_and_compute_declared_route_disagrees_no_hold_must_fix_4():
    """Goal must-fix 4: a declared route mismatch with computed_route does NOT trigger
    a HOLD — the declared value is telemetry only. The build proceeds on computed_route.
    """
    s = _strategy_with_ids(["AC-001"], [])
    v = _v(
        planned=[_p("AC-001", entailment="overreach")],
        declared_route="confirm",  # Codex declared confirm but says overreach
    )
    out = validate_and_compute_route(s, v, pytest_exit=0)
    assert out["computed_route"] == "gemma"  # overreach → gemma
    assert out["codex_declared_route"] == "confirm"
    assert out["agreement"] is False
    # The build proceeds on computed_route ('gemma'); the declared route is NOT used.


def test_validate_and_compute_no_declared_route_agreement_none():
    """Finding #8 (2026-06-07): declared_route is required + nullable. Codex
    emitting `null` (or the runner being handed a hand-crafted dict with
    declared_route=None) is the strict-mode equivalent of the old "field
    missing" path — both yield agreement=None and routing proceeds normally.
    """
    s = _strategy_with_ids(["AC-001"], [])
    v = _v(planned=[_p("AC-001")])  # declared_route=None by default
    out = validate_and_compute_route(s, v, pytest_exit=0)
    assert out["computed_route"] == "confirm"
    assert out["codex_declared_route"] is None
    assert out["agreement"] is None


def test_validate_and_compute_invalid_declared_route_agreement_none():
    s = _strategy_with_ids(["AC-001"], [])
    v = _v(
        planned=[_p("AC-001")],
        declared_route="WAT",  # not a valid route value
    )
    out = validate_and_compute_route(s, v, pytest_exit=0)
    assert out["computed_route"] == "confirm"
    assert out["agreement"] is None  # invalid → no comparison


def test_validate_and_compute_pytest_failed_all_fair_routes_coder():
    s = _strategy_with_ids(["AC-001"], [])
    v = _v(planned=[_p("AC-001", entailment="fair")])
    out = validate_and_compute_route(s, v, pytest_exit=1)
    assert out["computed_route"] == "coder"


def test_validate_and_compute_unrepresented_holds_human():
    s = _strategy_with_ids(["AC-001"], [])
    v = _v(planned=[_p("AC-001")])
    out = validate_and_compute_route(
        s, v, pytest_exit=0, unrepresented_clauses=["AC-999"]
    )
    assert out["computed_route"] == "hold_human"


# =============================================================================
# CODEX_VERDICT_SCHEMA structure check
# =============================================================================


def test_codex_verdict_schema_validates_minimal():
    """Finding #8 (2026-06-07): strict-mode requires every property in `properties`
    to be in `required`. The minimal-valid dict therefore carries empty arrays
    for the two list fields AND explicit nulls for the three optional fields.
    """
    import jsonschema

    minimal = _v()  # all five top-level keys present; nulls where conceptually optional
    jsonschema.validate(minimal, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_accepts_any_declared_route_string():
    """The schema relaxes declared_route to any string (telemetry only). The orchestrator
    handles non-canonical values by setting agreement=None — Codex emitting garbage in a
    telemetry field doesn't HOLD the build."""
    import jsonschema

    permissive = _v(declared_route="elsewhere")  # not a canonical route — schema accepts
    jsonschema.validate(permissive, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_rejects_extra_top_level_field():
    import jsonschema

    bad = _v()
    bad["evil_extra"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_requires_clause_id_in_planned():
    import jsonschema

    bad = _v(
        planned=[{"entailment_verdict": "fair", "triviality_verdict": "n/a", "reason": None}]
    )  # no clause_id
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


# --- Codex review (2026-06-06): new attack/coverage paths ---


def test_codex_verdict_schema_requires_triviality_verdict():
    """Codex HIGH 2: triviality_verdict is now REQUIRED in every planned verdict."""
    import jsonschema

    bad = _v(
        planned=[{"clause_id": "AC-001", "entailment_verdict": "fair", "reason": None}]
    )  # no triviality_verdict
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_triviality_na_is_valid():
    import jsonschema

    valid = _v(
        planned=[
            {
                "clause_id": "AC-001",
                "entailment_verdict": "fair",
                "triviality_verdict": "n/a",  # n/a is allowed
                "reason": None,
            }
        ]
    )
    jsonschema.validate(valid, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_rejects_unknown_entailment():
    import jsonschema

    bad = _v(
        planned=[
            {
                "clause_id": "AC-001",
                "entailment_verdict": "made_up",  # not in enum
                "triviality_verdict": "n/a",
                "reason": None,
            }
        ]
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


# --- Finding #8 (2026-06-07): strict-mode-equivalent regression coverage ---


def test_codex_verdict_schema_requires_reason_in_planned_item():
    """OpenAI strict mode REJECTS schemas where `properties` contains a field not in
    `required`. Pre-finding-#8, `reason` was in properties only — every Codex routing
    call 400'd. The fix tightens local validation to match: planned items now require
    reason (nullable). A planned item without `reason` must be rejected.
    """
    import jsonschema

    bad = _v(
        planned=[
            {
                "clause_id": "AC-001",
                "entailment_verdict": "fair",
                "triviality_verdict": "n/a",
                # no `reason`
            }
        ]
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_requires_reason_in_skipped_item():
    """Finding #8: same strict-mode rule applies to skipped-clause items."""
    import jsonschema

    bad = _v(
        skipped=[
            {
                "clause_id": "AC-001",
                "audit_verdict": "skip_earned",
                # no `reason`
            }
        ]
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_accepts_null_reason_in_planned_and_skipped():
    """Finding #8: `reason` is nullable + required. `null` is the strict-mode way
    of saying "Codex chose to not write commentary" — it must validate.
    """
    import jsonschema

    valid = _v(
        planned=[
            {
                "clause_id": "AC-001",
                "entailment_verdict": "fair",
                "triviality_verdict": "substantive",
                "reason": None,
            }
        ],
        skipped=[
            {
                "clause_id": "AC-002",
                "audit_verdict": "skip_earned",
                "reason": None,
            }
        ],
    )
    jsonschema.validate(valid, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_requires_top_level_declared_route():
    """Finding #8: declared_route is required + nullable. A dict missing the
    key entirely must be rejected (would have shipped to Codex strict mode
    pre-fix and 400'd)."""
    import jsonschema

    bad = {
        "codex_per_planned_test": [],
        "codex_per_skipped_clause": [],
        # missing declared_route, summary_reason, token_usage
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_requires_top_level_summary_reason():
    """Finding #8: summary_reason is required + nullable."""
    import jsonschema

    bad = {
        "codex_per_planned_test": [],
        "codex_per_skipped_clause": [],
        "declared_route": None,
        "token_usage": None,
        # missing summary_reason
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_requires_top_level_token_usage():
    """Finding #8: token_usage is required + nullable."""
    import jsonschema

    bad = {
        "codex_per_planned_test": [],
        "codex_per_skipped_clause": [],
        "declared_route": None,
        "summary_reason": None,
        # missing token_usage
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_token_usage_object_requires_both_subfields():
    """Finding #8: when token_usage is an object (not null), both prompt and
    completion must be present. Strict mode requires every property in required.
    """
    import jsonschema

    bad = _v(token_usage={"prompt": 100})  # missing completion
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_token_usage_accepts_null_subfields():
    """Finding #8: subfields are nullable integers — null is valid."""
    import jsonschema

    valid = _v(token_usage={"prompt": None, "completion": None})
    jsonschema.validate(valid, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_token_usage_rejects_extra_subfield():
    """Finding #8: token_usage has additionalProperties:false — extras rejected."""
    import jsonschema

    bad = _v(token_usage={"prompt": 1, "completion": 2, "evil_extra": 99})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, CODEX_VERDICT_SCHEMA)


def test_codex_verdict_schema_passes_strict_mode_self_audit():
    """Finding #8: walk CODEX_VERDICT_SCHEMA recursively and verify EVERY object
    node (a) carries `additionalProperties: false` and (b) lists every key in
    `properties` in its `required` array. This is the property OpenAI strict mode
    enforces — keeping it as a unit test catches future drift before a 400.
    """

    def _audit(node: dict, path: str) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "object" in (node.get("type") or []):
            properties = node.get("properties") or {}
            required = node.get("required") or []
            assert node.get("additionalProperties") is False, (
                f"{path}: object node missing `additionalProperties: false`"
            )
            missing = sorted(set(properties) - set(required))
            assert not missing, (
                f"{path}: properties not in `required`: {missing}"
            )
            for name, sub in properties.items():
                _audit(sub, f"{path}.properties.{name}")
        # Recurse into array items too.
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            _audit(node["items"], f"{path}.items")

    _audit(CODEX_VERDICT_SCHEMA, "$")


def test_compute_route_pytest_exit_2_interrupted_holds_human():
    """Codex MEDIUM 2: pytest exit 2 (interrupted) is NOT a real coder bug."""
    pp = [_p("AC-001", entailment="fair")]
    assert compute_route(pp, [], pytest_exit=2, unrepresented_clauses=[]) == "hold_human"


def test_compute_route_pytest_exit_5_no_tests_collected_holds_human():
    """Codex MEDIUM 2: pytest exit 5 (no tests collected) routes to hold_human."""
    pp = [_p("AC-001", entailment="fair")]
    assert compute_route(pp, [], pytest_exit=5, unrepresented_clauses=[]) == "hold_human"


def test_compute_route_pytest_negative_exit_killed_holds_human():
    """Codex MEDIUM 2: negative exit (killed by signal) routes to hold_human."""
    pp = [_p("AC-001", entailment="fair")]
    assert (
        compute_route(pp, [], pytest_exit=-9, unrepresented_clauses=[]) == "hold_human"
    )


def test_compute_route_pytest_exit_1_real_failure_routes_coder():
    """Goal must-fix 1 + MEDIUM 2: only pytest_exit == 1 routes to coder."""
    pp = [_p("AC-001", entailment="fair")]
    assert compute_route(pp, [], pytest_exit=1, unrepresented_clauses=[]) == "coder"


def test_cardinality_none_clause_id_does_not_crash():
    """Codex HIGH 1: a None clause_id in verdict must not crash sorted() in fail message."""
    s = _strategy_with_ids(["AC-001"], [])
    v = {
        "codex_per_planned_test": [
            {
                "clause_id": None,  # malformed
                "entailment_verdict": "fair",
                "triviality_verdict": "n/a",
            }
        ],
        "codex_per_skipped_clause": [],
    }
    # Must NOT raise TypeError; must return a clean fail CheckResult.
    r = validate_verdict_cardinality(s, v)
    assert not r.ok
    # The malformed None appears in the reason via repr.
    assert "None" in r.reason


def test_validate_and_compute_schema_invalid_holds_human():
    """Codex MEDIUM 1: a hand-crafted verdict with bogus enum routes hold_human via schema."""
    s = _strategy_with_ids(["AC-001"], [])
    v = _v(
        planned=[
            {
                "clause_id": "AC-001",
                "entailment_verdict": "made_up",  # not in enum
                "triviality_verdict": "n/a",
                "reason": None,
            }
        ]
    )
    out = validate_and_compute_route(s, v, pytest_exit=0)
    assert out["computed_route"] == "hold_human"
    assert "schema-invalid" in out["summary_reason"]


def test_validate_and_compute_missing_required_field_holds_human():
    s = _strategy_with_ids(["AC-001"], [])
    v = _v(
        planned=[
            {
                "clause_id": "AC-001",
                "entailment_verdict": "fair",
                "reason": None,
                # missing triviality_verdict
            }
        ]
    )
    out = validate_and_compute_route(s, v, pytest_exit=0)
    assert out["computed_route"] == "hold_human"
    assert "schema-invalid" in out["summary_reason"]


def test_validate_and_compute_non_dict_verdicts_holds_human():
    """Defensive: a list / string / None codex_verdicts → hold_human via schema."""
    s = _strategy_with_ids([], [])
    out = validate_and_compute_route(s, "not a dict", pytest_exit=0)
    assert out["computed_route"] == "hold_human"


def test_extract_function_sources_includes_decorators():
    """Codex LOW: decorators must be included in extracted source so the routing prompt
    sees @pytest.mark.parametrize / @fixture context."""
    src = (
        "import pytest\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_a(x):\n"
        "    assert x > 0\n"
    )
    out = _extract_function_sources(src, {"test_a"})
    assert "test_a" in out
    assert "@pytest.mark.parametrize" in out["test_a"]
    assert "assert x > 0" in out["test_a"]
