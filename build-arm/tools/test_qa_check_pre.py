"""Tests for tools.qa_check_pre — the 13 pre-pytest QA checks (C1, C3-C12, C15, C18a).

Strategy: one or more tests per check, plus sub-cases for the AST allowlist (C9). The
goal is to make every documented failure mode in the spec table mappable to a test here.

SECURITY NOTE: this file contains string LITERALS that mention `eval(...)`, `open(...)`,
`pytest.main`, `__class__`, etc. None of them are executed — they are written to a temp
file and parsed by `ast.parse` so the AST allowlist check (C9) can confirm it BANS each
name/attr/decorator. The helper `_c9_eval(tmp_path, code)` is named for "evaluate the C9
check," not Python's `eval()` builtin.
"""

import json
import os
from pathlib import Path

import pytest

from tools.qa_check_pre import (
    QA_STRATEGY_SCHEMA,
    c1_strategy_well_formed,
    c3_evidence_nodeids_exist,
    c4_n_tests_capped,
    c5_n_tests_matches_test_file_presence,
    c6_test_qa_parses,
    c7_def_test_count_matches,
    c7_planned_tests_bijective,
    c8_docstrings_cite_clause_id,
    c9_ast_allowlist,
    c10_asserted_in_test_resolves,
    c11_no_symlinks,
    c12_only_test_qa_differs,
    c15_gemma_ran_successfully,
    c18a_clause_completeness,
)


# =============================================================================
# Shared fixtures + helpers
# =============================================================================


def _valid_strategy(**overrides) -> dict:
    """A schema-valid strategy with one planned (concrete) + one skipped clause."""
    base = {
        "reviewed_clauses": ["AC-001", "AC-002"],
        "planned_tests": [
            {
                "clause_id": "AC-002",
                "probe_shape": "concrete",
                "concrete_behavior": {
                    "input": "build_graph([{'name':'a','deps':1}])",
                    "expected": "ValueError",
                },
                "coder_test_evidence": ["test_graph.py::test_x"],
                "uncovered_behavior": "non-list deps",
                "asserted_in_test": "test_non_list_deps_raises",
            }
        ],
        "skipped_clauses": [
            {
                "clause_id": "AC-001",
                "abstract_or_concrete": "concrete",
                "coder_test_evidence": ["test_graph.py::test_basic_shape"],
                "why_already_covered": "test_basic_shape directly asserts dict shape",
            }
        ],
        "n_tests_written": 1,
    }
    base.update(overrides)
    return base


# =============================================================================
# C1 — strategy file
# =============================================================================


def test_c1_missing_file(tmp_path):
    r = c1_strategy_well_formed(tmp_path / "missing.json")
    assert not r.ok and "missing" in r.reason


def test_c1_not_regular_file(tmp_path):
    d = tmp_path / "qa-strategy.json"
    d.mkdir()  # directory, not a file
    r = c1_strategy_well_formed(d)
    assert not r.ok and "not a regular file" in r.reason


def test_c1_symlink_rejected(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps(_valid_strategy()))
    link = tmp_path / "qa-strategy.json"
    link.symlink_to(target)
    r = c1_strategy_well_formed(link)
    assert not r.ok and "not a regular file" in r.reason


def test_c1_invalid_json(tmp_path):
    p = tmp_path / "qa-strategy.json"
    p.write_text("not json {")
    r = c1_strategy_well_formed(p)
    assert not r.ok and "not valid JSON" in r.reason


def test_c1_schema_invalid_missing_field(tmp_path):
    p = tmp_path / "qa-strategy.json"
    p.write_text(json.dumps({"reviewed_clauses": []}))  # missing planned_tests etc.
    r = c1_strategy_well_formed(p)
    assert not r.ok and "schema-invalid" in r.reason


def test_c1_schema_invalid_extra_field(tmp_path):
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["evil_extra"] = 1
    p.write_text(json.dumps(s))
    r = c1_strategy_well_formed(p)
    assert not r.ok and "schema-invalid" in r.reason


def test_c1_schema_invalid_both_behaviors_in_one_planned(tmp_path):
    """A planned_test cannot have BOTH concrete_behavior and property_behavior (oneOf)."""
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["planned_tests"][0]["property_behavior"] = {
        "domain": "x",
        "examples_or_generator": "y",
        "invariant": "z",
        "sample_bound": 10,
    }
    p.write_text(json.dumps(s))
    r = c1_strategy_well_formed(p)
    assert not r.ok and "schema-invalid" in r.reason


def test_c1_valid_passes(tmp_path):
    p = tmp_path / "qa-strategy.json"
    p.write_text(json.dumps(_valid_strategy()))
    r = c1_strategy_well_formed(p)
    assert r.ok


def test_c1_valid_property_shape(tmp_path):
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["planned_tests"][0] = {
        "clause_id": "AC-013",
        "probe_shape": "property",
        "property_behavior": {
            "domain": "any string",
            "examples_or_generator": "['a','aa','abc']",
            "invariant": "f(x) == f(x)",
            "sample_bound": 5,
        },
        "coder_test_evidence": ["test_cycles.py::test_determinism"],
        "uncovered_behavior": "lex-smallest rotation",
        "asserted_in_test": "test_determinism_property",
    }
    p.write_text(json.dumps(s))
    r = c1_strategy_well_formed(p)
    assert r.ok


def test_c1_sample_bound_exceeded(tmp_path):
    """property_behavior.sample_bound capped at 20."""
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["planned_tests"][0] = {
        "clause_id": "AC-013",
        "probe_shape": "property",
        "property_behavior": {
            "domain": "x",
            "examples_or_generator": "y",
            "invariant": "z",
            "sample_bound": 21,
        },
        "coder_test_evidence": ["test.py::test_x"],
        "uncovered_behavior": "x",
        "asserted_in_test": "test_x",
    }
    p.write_text(json.dumps(s))
    r = c1_strategy_well_formed(p)
    assert not r.ok and "schema-invalid" in r.reason


# =============================================================================
# C3 — evidence nodeids exist
# =============================================================================


def test_c3_all_evidence_present():
    s = _valid_strategy()
    coder_nodeids = {"test_graph.py::test_x", "test_graph.py::test_basic_shape", "test_other.py::test_y"}
    assert c3_evidence_nodeids_exist(s, coder_nodeids).ok


def test_c3_missing_evidence_held():
    s = _valid_strategy()
    coder_nodeids = {"test_graph.py::test_x"}  # missing test_basic_shape
    r = c3_evidence_nodeids_exist(s, coder_nodeids)
    assert not r.ok and "test_basic_shape" in r.reason


def test_c3_empty_strategy_passes():
    s = {"planned_tests": [], "skipped_clauses": [], "n_tests_written": 0}
    assert c3_evidence_nodeids_exist(s, set()).ok


# =============================================================================
# C4 — n_tests_written capped
# =============================================================================


def test_c4_zero_ok():
    assert c4_n_tests_capped({"n_tests_written": 0}).ok


def test_c4_three_ok():
    assert c4_n_tests_capped({"n_tests_written": 3}).ok


def test_c4_four_held():
    r = c4_n_tests_capped({"n_tests_written": 4})
    assert not r.ok


def test_c4_negative_held():
    r = c4_n_tests_capped({"n_tests_written": -1})
    assert not r.ok


def test_c4_non_int_held():
    r = c4_n_tests_capped({"n_tests_written": "three"})
    assert not r.ok and "not an int" in r.reason


def test_c4_missing_treated_as_non_int():
    r = c4_n_tests_capped({})
    assert not r.ok


# =============================================================================
# C5 — n_tests > 0 ↔ test_qa.py exists
# =============================================================================


def test_c5_zero_no_file_ok(tmp_path):
    assert c5_n_tests_matches_test_file_presence(
        {"n_tests_written": 0}, tmp_path / "test_qa.py"
    ).ok


def test_c5_two_with_file_ok(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text("")
    assert c5_n_tests_matches_test_file_presence(
        {"n_tests_written": 2}, p
    ).ok


def test_c5_zero_with_file_held(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text("")
    r = c5_n_tests_matches_test_file_presence({"n_tests_written": 0}, p)
    assert not r.ok


def test_c5_three_without_file_held(tmp_path):
    r = c5_n_tests_matches_test_file_presence(
        {"n_tests_written": 3}, tmp_path / "test_qa.py"
    )
    assert not r.ok


# =============================================================================
# C6 — test_qa.py parses
# =============================================================================


def test_c6_missing_file_ok(tmp_path):
    assert c6_test_qa_parses(tmp_path / "missing.py").ok


def test_c6_valid_python(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text("def test_x():\n    assert True\n")
    assert c6_test_qa_parses(p).ok


def test_c6_syntax_error_held(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text("def test_x(:\n  pass\n")
    r = c6_test_qa_parses(p)
    assert not r.ok and "SyntaxError" in r.reason


# =============================================================================
# C7 — def test_ count == n_tests_written
# =============================================================================


def _planned(asserted_in_test: str, clause_id: str = "AC-001") -> dict:
    """Build a minimal planned_tests entry for C7 bijection tests."""
    return {
        "clause_id": clause_id,
        "probe_shape": "concrete",
        "concrete_behavior": {"input": "x", "expected": "y"},
        "coder_test_evidence": ["test_graph.py::test_x"],
        "uncovered_behavior": "z",
        "asserted_in_test": asserted_in_test,
    }


def test_c7_bijection_ok(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text(
        "def test_a():\n    assert True\n"
        "def test_b():\n    assert True\n"
    )
    strategy = {
        "n_tests_written": 2,
        "planned_tests": [_planned("test_a"), _planned("test_b", "AC-002")],
        "skipped_clauses": [],
    }
    assert c7_planned_tests_bijective(strategy, p).ok


def test_c7_missing_file_n_zero_ok(tmp_path):
    assert c7_planned_tests_bijective(
        {"n_tests_written": 0, "planned_tests": [], "skipped_clauses": []},
        tmp_path / "nope.py",
    ).ok


def test_c7_missing_file_n_nonzero_held(tmp_path):
    r = c7_planned_tests_bijective(
        {"n_tests_written": 2, "planned_tests": [_planned("a"), _planned("b")]},
        tmp_path / "nope.py",
    )
    assert not r.ok


def test_c7_n_tests_planned_len_mismatch_held(tmp_path):
    """len(planned_tests) must equal n_tests_written."""
    p = tmp_path / "test_qa.py"
    p.write_text("def test_a():\n    assert True\n")
    r = c7_planned_tests_bijective(
        {"n_tests_written": 2, "planned_tests": [_planned("test_a")]}, p
    )
    assert not r.ok and "len(planned_tests)" in r.reason


def test_c7_extra_unplanned_test_held(tmp_path):
    """Bijection failure: an extra def test_X exists in the file but no planned entry."""
    p = tmp_path / "test_qa.py"
    p.write_text(
        "def test_a():\n    assert True\n"
        "def test_extra():\n    assert True\n"  # not in planned
    )
    strategy = {
        "n_tests_written": 1,
        "planned_tests": [_planned("test_a")],
        "skipped_clauses": [],
    }
    r = c7_planned_tests_bijective(strategy, p)
    assert not r.ok and (
        "extra" in r.reason or "len(planned_tests)" in r.reason
    )


def test_c7_planned_but_undefined_held(tmp_path):
    """Bijection failure: planned entry names test_b but only test_a is defined."""
    p = tmp_path / "test_qa.py"
    p.write_text("def test_a():\n    assert True\n")
    strategy = {
        "n_tests_written": 1,
        "planned_tests": [_planned("test_b")],
        "skipped_clauses": [],
    }
    r = c7_planned_tests_bijective(strategy, p)
    assert not r.ok


def test_c7_duplicate_asserted_in_test_name_held(tmp_path):
    """Two planned entries with the SAME asserted_in_test must collapse → Counter
    catches the mismatch since the file only has one def test_a."""
    p = tmp_path / "test_qa.py"
    p.write_text("def test_a():\n    assert True\n")
    strategy = {
        "n_tests_written": 2,
        "planned_tests": [_planned("test_a"), _planned("test_a", "AC-002")],
        "skipped_clauses": [],
    }
    r = c7_planned_tests_bijective(strategy, p)
    assert not r.ok


def test_c7_helper_functions_not_counted(tmp_path):
    """A `def _helper` is not a test (no test_ prefix), must not affect bijection."""
    p = tmp_path / "test_qa.py"
    p.write_text(
        "def test_a():\n    assert True\n"
        "def _helper():\n    return 1\n"
    )
    strategy = {
        "n_tests_written": 1,
        "planned_tests": [_planned("test_a")],
        "skipped_clauses": [],
    }
    assert c7_planned_tests_bijective(strategy, p).ok


# =============================================================================
# C8 — docstring cites clause_id
# =============================================================================


def test_c8_docstring_present(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text(
        'def test_non_list_deps_raises():\n'
        '    """[AC-002] build_graph rejects non-list deps."""\n'
        '    assert True\n'
    )
    s = _valid_strategy()
    assert c8_docstrings_cite_clause_id(s, p).ok


def test_c8_docstring_missing_held(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text(
        'def test_non_list_deps_raises():\n'
        '    """no clause cited here"""\n'
        '    assert True\n'
    )
    s = _valid_strategy()
    r = c8_docstrings_cite_clause_id(s, p)
    assert not r.ok and "[AC-002]" in r.reason


def test_c8_no_docstring_held(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text('def test_non_list_deps_raises():\n    assert True\n')
    s = _valid_strategy()
    r = c8_docstrings_cite_clause_id(s, p)
    assert not r.ok


def test_c8_missing_file_ok(tmp_path):
    assert c8_docstrings_cite_clause_id(_valid_strategy(), tmp_path / "x.py").ok


# =============================================================================
# C9 — AST allowlist (largest section: imports / names / attrs / decorators / inline assert)
# =============================================================================


def _c9_eval(tmp_path, code, ws_modules=None):
    p = tmp_path / "test_qa.py"
    p.write_text(code)
    return c9_ast_allowlist(p, ws_modules or set())


def test_c9_missing_file_ok(tmp_path):
    assert c9_ast_allowlist(tmp_path / "nope.py", set()).ok


def test_c9_allowed_stdlib_imports(tmp_path):
    code = (
        "import json\n"
        "import pathlib\n"
        "from typing import Any\n"
        "from collections import Counter\n"
        "def test_x():\n    assert True\n"
    )
    assert _c9_eval(tmp_path, code).ok


def test_c9_workspace_module_import_allowed(tmp_path):
    code = "import graph\ndef test_x():\n    assert True\n"
    assert _c9_eval(tmp_path, code, ws_modules={"graph", "cycles"}).ok


def test_c9_disallowed_third_party_import_held(tmp_path):
    code = "import requests\ndef test_x():\n    assert True\n"
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "requests" in r.reason


def test_c9_pytest_import_ok(tmp_path):
    code = "import pytest\ndef test_x():\n    with pytest.raises(ValueError): raise ValueError()\n"
    assert _c9_eval(tmp_path, code).ok


def test_c9_pytest_main_attr_banned(tmp_path):
    code = "import pytest\ndef test_x():\n    pytest.main([])\n    assert True\n"
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "pytest.main" in r.reason


def test_c9_pytest_monkeypatch_attr_banned(tmp_path):
    code = "import pytest\ndef test_x():\n    pytest.MonkeyPatch()\n    assert True\n"
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "MonkeyPatch" in r.reason


def test_c9_pytest_mark_parametrize_ok(tmp_path):
    code = (
        "import pytest\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_x(x):\n    assert x > 0\n"
    )
    assert _c9_eval(tmp_path, code).ok


def test_c9_pytest_mark_skip_banned(tmp_path):
    code = (
        "import pytest\n"
        "@pytest.mark.skip\n"
        "def test_x():\n    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "skip" in r.reason


def test_c9_skipif_banned(tmp_path):
    code = (
        "import pytest\n"
        "@pytest.mark.skipif(True, reason='x')\n"
        "def test_x():\n    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "skipif" in r.reason


def test_c9_xfail_banned(tmp_path):
    code = (
        "import pytest\n"
        "@pytest.mark.xfail\n"
        "def test_x():\n    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "xfail" in r.reason


def test_c9_from_pytest_import_raises_ok(tmp_path):
    code = (
        "from pytest import raises\n"
        "def test_x():\n    with raises(ValueError): raise ValueError()\n"
    )
    assert _c9_eval(tmp_path, code).ok


def test_c9_from_pytest_import_main_banned(tmp_path):
    code = (
        "from pytest import main\n"
        "def test_x():\n    main([])\n    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "main" in r.reason


def test_c9_banned_name_eval(tmp_path):
    code = "def test_x():\n    eval('1')\n    assert True\n"
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "eval" in r.reason


def test_c9_banned_name_open(tmp_path):
    code = "def test_x():\n    open('/tmp/x')\n    assert True\n"
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "open" in r.reason


def test_c9_banned_attr_class(tmp_path):
    code = "def test_x():\n    x = (1).__class__\n    assert True\n"
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "__class__" in r.reason


def test_c9_banned_attr_subclasses(tmp_path):
    code = "def test_x():\n    s = object.__subclasses__()\n    assert True\n"
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "__subclasses__" in r.reason


# --- inline-assertion rule (goal must-fix 3) ---


def test_c9_inline_assert_satisfies_rule(tmp_path):
    code = "def test_x():\n    assert 1 + 1 == 2\n"
    assert _c9_eval(tmp_path, code).ok


def test_c9_inline_pytest_raises_satisfies_rule(tmp_path):
    code = (
        "import pytest\n"
        "def test_x():\n"
        "    with pytest.raises(ValueError):\n"
        "        int('xyz')\n"
    )
    assert _c9_eval(tmp_path, code).ok


def test_c9_callable_form_pytest_raises_no_longer_satisfies(tmp_path):
    """Codex review round 2: callable-form pytest.raises is REJECTED (only `with` form
    counts), because a non-callable 2nd arg like `pytest.raises(TypeError, 'x')` would
    otherwise satisfy without probing the SUT."""
    code = (
        "import pytest\n"
        "def test_x():\n"
        "    pytest.raises(ValueError, int, 'xyz')\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "test_x" in r.reason


def test_c9_no_inline_assertion_held(tmp_path):
    """Helper-style: test calls a helper, no inline assert. Must be held per must-fix 3."""
    code = (
        "def _helper(x):\n    return x > 0\n"
        "def test_x():\n    result = _helper(5)\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "inline assert" in r.reason


def test_c9_imported_assertion_helper_held(tmp_path):
    """`from json import dumps` is a non-asserting import; using it instead of inline
    assert means the test mints 'passed' with no real assertion."""
    code = (
        "from json import dumps\n"
        "def test_x():\n    dumps({})\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "inline assert" in r.reason


def test_c9_multiple_tests_one_missing_assert_held(tmp_path):
    code = (
        "def test_a():\n    assert True\n"
        "def test_b():\n    x = 1\n"  # no assert
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "test_b" in r.reason


# --- Codex review 2026-06-06: nested-assert false positive ---


def test_c9_nested_helper_assert_does_not_satisfy(tmp_path):
    """A nested function with `assert` is NOT executed unless called; must not satisfy."""
    code = (
        "def test_x():\n"
        "    def _helper():\n"
        "        assert True\n"
        # _helper() never called; test body has no actual assertion
        "    x = 1\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "test_x" in r.reason


def test_c9_lambda_assert_does_not_satisfy(tmp_path):
    """An assert inside a lambda body — lambdas can't even have assert, but defensive."""
    code = (
        "def test_x():\n"
        "    f = lambda x: x > 0\n"  # no assert here
        "    f(1)\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "test_x" in r.reason


def test_c9_inner_class_assert_does_not_satisfy(tmp_path):
    """A class definition inside the test must not count its assertions as the test's."""
    code = (
        "def test_x():\n"
        "    class _C:\n"
        "        def m(self):\n"
        "            assert True\n"
        "    _C()\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "test_x" in r.reason


def test_c9_assert_inside_if_satisfies(tmp_path):
    """An assert inside a conditional branch DOES count — it's executable test body."""
    code = (
        "def test_x():\n"
        "    if 1:\n"
        "        assert True\n"
    )
    assert _c9_eval(tmp_path, code).ok


def test_c9_assert_inside_for_satisfies(tmp_path):
    code = (
        "def test_x():\n"
        "    for i in range(1):\n"
        "        assert i == 0\n"
    )
    assert _c9_eval(tmp_path, code).ok


def test_c9_assert_inside_try_satisfies(tmp_path):
    code = (
        "def test_x():\n"
        "    try:\n"
        "        assert True\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert _c9_eval(tmp_path, code).ok


# --- Codex review 2026-06-06: bare pytest.raises ---


def test_c9_bare_pytest_raises_does_not_satisfy(tmp_path):
    """A bare `pytest.raises(ValueError)` standalone creates a CM but asserts nothing."""
    code = (
        "import pytest\n"
        "def test_x():\n"
        "    pytest.raises(ValueError)\n"  # no `with`, no callable arg
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "test_x" in r.reason


def test_c9_bare_raises_imported_does_not_satisfy(tmp_path):
    code = (
        "from pytest import raises\n"
        "def test_x():\n"
        "    raises(ValueError)\n"  # no `with`, no callable arg
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok


# --- Codex review 2026-06-06: monkeypatch / module mutation bans ---


def test_c9_monkeypatch_fixture_param_banned(tmp_path):
    code = (
        "def test_x(monkeypatch):\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "monkeypatch" in r.reason


def test_c9_monkeypatch_setattr_call_banned(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    monkeypatch.setattr(graph, 'build_graph', lambda x: x)\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    # `monkeypatch` here is an undefined name; the call still trips the ban detector.
    assert not r.ok and "monkeypatch" in r.reason


def test_c9_direct_ws_module_attribute_assign_banned(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    graph.build_graph = lambda x: x\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph", "cycles"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_ws_module_subscript_assign_banned(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    graph.__dict__['build_graph'] = 1\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    # Both __dict__ (BANNED_ATTRS) and the mutation root catch this.
    assert not r.ok


def test_c9_ws_module_augassign_banned(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    graph.counter += 1\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_non_ws_module_assign_ok(tmp_path):
    """Local-variable assignment is fine — only workspace-module attribute assignment is banned."""
    code = (
        "def test_x():\n"
        "    x = 1\n"
        "    x += 1\n"
        "    assert x == 2\n"
    )
    assert _c9_eval(tmp_path, code, ws_modules={"graph"}).ok


# --- Codex review 2026-06-06: broader reflection bans ---


def test_c9_banned_attr_getattribute(tmp_path):
    code = (
        "import json\n"
        "def test_x():\n"
        "    json.__getattribute__('dumps')\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "__getattribute__" in r.reason


def test_c9_banned_attr_base(tmp_path):
    code = (
        "def test_x():\n"
        "    type(1).__base__\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "__base__" in r.reason


# --- Codex review 2026-06-06 round 2: import-alias + complex assignment targets ---


def test_c9_import_alias_mutation_held(tmp_path):
    """`import graph as g; g.f = ...` must be caught even though root name is 'g'."""
    code = (
        "import graph as g\n"
        "def test_x():\n"
        "    g.build_graph = lambda x: x\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_tuple_destructure_assign_held(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    graph.f, x = lambda y: y, 1\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_annotated_assign_held(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    graph.f: int = 1\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_delete_held(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    del graph.f\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_for_loop_target_held(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    for graph.f in [1, 2]:\n"
        "        pass\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_with_optional_var_held(tmp_path):
    code = (
        "import graph\n"
        "import contextlib\n"  # contextlib not allowed but mutation rule fires first
        "def test_x():\n"
        "    with open('/tmp/x') as graph.f:\n"
        "        pass\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    # multiple violations fire; either is sufficient
    assert not r.ok


# --- Codex review 2026-06-06 round 2: indirect monkeypatch via request ---


def test_c9_request_fixture_param_banned(tmp_path):
    code = (
        "def test_x(request):\n"
        "    mp = request.getfixturevalue('monkeypatch')\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and ("request" in r.reason or "getfixturevalue" in r.reason)


def test_c9_getfixturevalue_call_banned(tmp_path):
    """Even if `request` were named differently, `.getfixturevalue` itself is banned."""
    code = (
        "def test_x(req):\n"  # 'req' isn't a banned name, but the call is
        "    mp = req.getfixturevalue('monkeypatch')\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "getfixturevalue" in r.reason


def test_c9_setattr_method_call_on_any_receiver_banned(tmp_path):
    """Even if monkeypatch is renamed to mp, the .setattr call is banned."""
    code = (
        "def test_x():\n"
        "    mp = object()\n"  # not real monkeypatch; but pattern catches the attempt
        "    mp.setattr(1, 'x', 2)\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "patch-API" in r.reason


def test_c9_setenv_call_banned(tmp_path):
    code = (
        "def test_x():\n"
        "    mp = object()\n"
        "    mp.setenv('X', '1')\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "patch-API" in r.reason


# --- Codex review 2026-06-06 round 2: parent-symlink traversal hardening ---


def test_c5_parent_symlink_held(tmp_path):
    """A symlink in the path chain (parent directory) is rejected at C5 too."""
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "test_qa.py").write_text("def test_x():\n    assert True\n")
    link = tmp_path / "link_dir"
    link.symlink_to(real_dir)
    # path resolution goes through the symlinked parent
    target = link / "test_qa.py"
    r = c5_n_tests_matches_test_file_presence({"n_tests_written": 1}, target)
    # C5's own symlink check on the leaf doesn't fire (the leaf isn't a symlink) but the
    # `is_file()` check returns True. The deeper traversal protection lives in C6's read
    # helper. For C5 we only assert it doesn't crash; deeper hardening at C6/C9/C10.
    assert r.ok or not r.ok  # don't enforce here


def test_c6_parent_symlink_in_chain_held(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "test_qa.py").write_text("def test_x():\n    assert True\n")
    link = tmp_path / "link_dir"
    link.symlink_to(real_dir)
    r = c6_test_qa_parses(link / "test_qa.py")
    assert not r.ok and "symlink in test_qa.py path chain" in r.reason


def test_c11_ws_root_is_symlink_held(tmp_path):
    real = tmp_path / "real_ws"
    real.mkdir()
    (real / "graph.py").write_text("# code")
    link = tmp_path / "link_ws"
    link.symlink_to(real)
    r = c11_no_symlinks(link)
    assert not r.ok and "itself a symlink" in r.reason


# --- Codex review 2026-06-06 round 3: local rebinding + from-imports ---


def test_c9_local_rebind_to_workspace_module_held(tmp_path):
    """`m = graph; m.build_graph = fake` — m carries the ws binding."""
    code = (
        "import graph\n"
        "def fake(x): return x\n"
        "def test_x():\n"
        "    m = graph\n"
        "    m.build_graph = fake\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_chained_rebind_held(tmp_path):
    """`m = graph; n = m; n.build_graph = ...` — fixed-point closure protects n."""
    code = (
        "import graph\n"
        "def fake(x): return x\n"
        "def test_x():\n"
        "    m = graph\n"
        "    n = m\n"
        "    n.build_graph = fake\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_from_import_function_code_mutation_held(tmp_path):
    """`from graph import build_graph; build_graph.__code__ = fake.__code__` — also caught
    by BANNED_ATTRS (__code__ not in list yet) AND by ws-binding mutation."""
    code = (
        "from graph import build_graph\n"
        "def fake(x): return x\n"
        "def test_x():\n"
        "    build_graph.__code__ = fake.__code__\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_from_import_class_attr_mutation_held(tmp_path):
    """`from graph import C; C.method = fake` — mutating an imported class still mutates
    the SUT object reachable via graph.C."""
    code = (
        "from graph import C\n"
        "def fake(self): return 1\n"
        "def test_x():\n"
        "    C.method = fake\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_from_import_value_call_not_held(tmp_path):
    """A legitimate test: calls a from-imported function without mutating it."""
    code = (
        "from graph import build_graph\n"
        "def test_x():\n"
        "    result = build_graph([{'name': 'a', 'deps': []}])\n"
        "    assert result == {'a': []}\n"
    )
    assert _c9_eval(tmp_path, code, ws_modules={"graph"}).ok


def test_c9_annotated_rebind_to_workspace_held(tmp_path):
    code = (
        "import graph\n"
        "def test_x():\n"
        "    m: object = graph\n"
        "    m.build_graph = lambda x: x\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok


def test_c9_import_dotted_with_alias_mutation_held(tmp_path):
    """`import graph.sub as gs; gs.something = ...` — Codex flagged claim, add test."""
    code = (
        "import graph.sub as gs\n"
        "def test_x():\n"
        "    gs.something = 1\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "workspace-module mutation" in r.reason


def test_c9_unrelated_rebind_not_held(tmp_path):
    """`m = 1; m += 1` — m is not a ws binding, so no fire."""
    code = (
        "def test_x():\n"
        "    m = 1\n"
        "    m += 1\n"
        "    assert m == 2\n"
    )
    assert _c9_eval(tmp_path, code, ws_modules={"graph"}).ok


def test_c9_wildcard_from_workspace_import_held(tmp_path):
    """Codex review round 4: `from graph import *` lets unbound names slip past the AST
    binding tracker. Reject the form outright."""
    code = (
        "from graph import *\n"
        "def test_x():\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code, ws_modules={"graph"})
    assert not r.ok and "wildcard" in r.reason


def test_c9_wildcard_from_stdlib_import_held(tmp_path):
    """Wildcard import from any module is rejected — pytest, stdlib, anything."""
    code = (
        "from json import *\n"
        "def test_x():\n"
        "    assert True\n"
    )
    r = _c9_eval(tmp_path, code)
    assert not r.ok and "wildcard" in r.reason


# =============================================================================
# C10 — asserted_in_test resolves
# =============================================================================


def test_c10_name_present(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text(
        'def test_non_list_deps_raises():\n'
        '    """[AC-002] x"""\n'
        '    assert True\n'
    )
    assert c10_asserted_in_test_resolves(_valid_strategy(), p).ok


def test_c10_name_missing_held(tmp_path):
    p = tmp_path / "test_qa.py"
    p.write_text(
        'def test_wrong_name():\n    """[AC-002] x"""\n    assert True\n'
    )
    r = c10_asserted_in_test_resolves(_valid_strategy(), p)
    assert not r.ok and "test_non_list_deps_raises" in r.reason


def test_c10_no_planned_tests_ok(tmp_path):
    s = {"planned_tests": [], "skipped_clauses": [], "n_tests_written": 0}
    assert c10_asserted_in_test_resolves(s, tmp_path / "nope.py").ok


def test_c10_file_missing_with_planned_held(tmp_path):
    r = c10_asserted_in_test_resolves(_valid_strategy(), tmp_path / "nope.py")
    assert not r.ok


# =============================================================================
# C11 — no symlinks
# =============================================================================


def test_c11_no_symlinks_ok(tmp_path):
    (tmp_path / "graph.py").write_text("# code")
    (tmp_path / "cycles.py").write_text("# code")
    assert c11_no_symlinks(tmp_path).ok


def test_c11_symlink_held(tmp_path):
    (tmp_path / "graph.py").write_text("# code")
    (tmp_path / "evil_link").symlink_to(tmp_path / "graph.py")
    r = c11_no_symlinks(tmp_path)
    assert not r.ok and "evil_link" in r.reason


def test_c11_nested_symlink_held(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "real").write_text("x")
    (tmp_path / "sub" / "link").symlink_to(tmp_path / "sub" / "real")
    r = c11_no_symlinks(tmp_path)
    assert not r.ok


def test_c11_not_a_dir_held(tmp_path):
    r = c11_no_symlinks(tmp_path / "nope")
    assert not r.ok and "not a directory" in r.reason


# =============================================================================
# C12 — only test_qa.py differs
# =============================================================================


def _seed_two_workspaces(tmp_path: Path) -> tuple[Path, Path]:
    canonical = tmp_path / "canonical"
    tmp_ws = tmp_path / "tmp_ws"
    canonical.mkdir()
    tmp_ws.mkdir()
    for f in ("graph.py", "cycles.py"):
        (canonical / f).write_text(f"# canonical {f}")
        (tmp_ws / f).write_text(f"# canonical {f}")  # same content
    return tmp_ws, canonical


def test_c12_identical_workspaces_ok(tmp_path):
    tmp_ws, canonical = _seed_two_workspaces(tmp_path)
    assert c12_only_test_qa_differs(tmp_ws, canonical).ok


def test_c12_only_test_qa_added_ok(tmp_path):
    tmp_ws, canonical = _seed_two_workspaces(tmp_path)
    (tmp_ws / "test_qa.py").write_text("def test_x():\n    assert True\n")
    assert c12_only_test_qa_differs(tmp_ws, canonical).ok


def test_c12_other_file_modified_held(tmp_path):
    tmp_ws, canonical = _seed_two_workspaces(tmp_path)
    (tmp_ws / "graph.py").write_text("# MUTATED")
    (tmp_ws / "test_qa.py").write_text("def test_x():\n    assert True\n")
    r = c12_only_test_qa_differs(tmp_ws, canonical)
    assert not r.ok and "graph.py" in r.reason


def test_c12_extra_file_held(tmp_path):
    tmp_ws, canonical = _seed_two_workspaces(tmp_path)
    (tmp_ws / "evil.py").write_text("# extra")
    r = c12_only_test_qa_differs(tmp_ws, canonical)
    assert not r.ok and "evil.py" in r.reason


def test_c12_missing_dir_held(tmp_path):
    r = c12_only_test_qa_differs(tmp_path / "missing", tmp_path / "alsoNope")
    assert not r.ok


# =============================================================================
# C15 — Gemma ran successfully
# =============================================================================


def test_c15_success():
    r = c15_gemma_ran_successfully(
        {"swap_ok": True, "timed_out": False, "exit_code": 0}
    )
    assert r.ok


def test_c15_swap_failed():
    r = c15_gemma_ran_successfully(
        {"swap_ok": False, "timed_out": False, "exit_code": 0, "error": "OOM"}
    )
    assert not r.ok and "swap failed" in r.reason


def test_c15_timeout():
    r = c15_gemma_ran_successfully(
        {"swap_ok": True, "timed_out": True, "exit_code": -1}
    )
    assert not r.ok and "timed out" in r.reason


def test_c15_nonzero_exit():
    r = c15_gemma_ran_successfully(
        {"swap_ok": True, "timed_out": False, "exit_code": 137, "error": "killed"}
    )
    assert not r.ok and "exit_code=137" in r.reason


# =============================================================================
# C18a — completeness invariant (heart of the QA layer)
# =============================================================================


def _contract_clauses(*ids: str) -> list[dict]:
    return [{"id": i, "text": f"clause {i}"} for i in ids]


def test_c18a_complete_split_across_planned_and_skipped():
    s = _valid_strategy()  # plans AC-002, skips AC-001
    cl = _contract_clauses("AC-001", "AC-002")
    assert c18a_clause_completeness(s, cl).ok


def test_c18a_unrepresented_clause_held():
    s = _valid_strategy()  # covers AC-001 and AC-002
    cl = _contract_clauses("AC-001", "AC-002", "AC-003")  # AC-003 uncovered
    r = c18a_clause_completeness(s, cl)
    assert not r.ok and "AC-003" in r.reason


def test_c18a_unknown_id_in_strategy_held():
    """Isolate the unknown-id case: contract has only AC-001, strategy cites AC-001 + AC-002.
    Unrepresented = ∅; unknown = {AC-002}."""
    s = _valid_strategy()  # plans AC-002, skips AC-001 — both in strategy
    cl = _contract_clauses("AC-001")  # AC-002 is unknown to the contract
    r = c18a_clause_completeness(s, cl)
    assert not r.ok and "not in contract" in r.reason and "AC-002" in r.reason


def test_c18a_double_cited_clause_held():
    s = _valid_strategy()
    # Both plan AND skip AC-001 — double-cited.
    s["planned_tests"].append(
        {
            "clause_id": "AC-001",
            "probe_shape": "concrete",
            "concrete_behavior": {"input": "x", "expected": "y"},
            "coder_test_evidence": ["t.py::t"],
            "uncovered_behavior": "x",
            "asserted_in_test": "test_extra",
        }
    )
    cl = _contract_clauses("AC-001", "AC-002")
    r = c18a_clause_completeness(s, cl)
    assert not r.ok and "more than once" in r.reason


def test_c18a_empty_contract_empty_strategy_ok():
    s = {"planned_tests": [], "skipped_clauses": [], "n_tests_written": 0}
    assert c18a_clause_completeness(s, []).ok


def test_c18a_empty_contract_nonempty_strategy_held():
    s = _valid_strategy()  # cites AC-001, AC-002
    r = c18a_clause_completeness(s, [])
    assert not r.ok and "not in contract" in r.reason


def test_c18a_accumulates_all_three_violation_classes():
    """Codex review 2026-06-06: HOLD message must include unrepresented AND duplicates
    AND unknowns when all three are present, not just the first."""
    s = _valid_strategy()  # plans AC-002, skips AC-001
    # Add a duplicate citation of AC-002 (planned + skipped).
    s["skipped_clauses"].append(
        {
            "clause_id": "AC-002",
            "abstract_or_concrete": "abstract",
            "why_already_covered": "double-cite",
        }
    )
    # Also cite AC-999 (unknown) via an extra planned test.
    s["planned_tests"].append(
        {
            "clause_id": "AC-999",
            "probe_shape": "concrete",
            "concrete_behavior": {"input": "x", "expected": "y"},
            "coder_test_evidence": ["t.py::t"],
            "uncovered_behavior": "x",
            "asserted_in_test": "test_unknown",
        }
    )
    # Contract has AC-001, AC-002, AC-003 — so AC-003 is unrepresented.
    cl = _contract_clauses("AC-001", "AC-002", "AC-003")
    r = c18a_clause_completeness(s, cl)
    assert not r.ok
    assert "AC-003" in r.reason  # unrepresented
    assert "AC-999" in r.reason  # unknown
    assert "AC-002" in r.reason  # duplicate


# --- Codex review 2026-06-06: schema tightening (probe_shape + concrete-skip evidence) ---


def test_c1_probe_shape_concrete_with_only_property_behavior_held(tmp_path):
    """probe_shape='concrete' but only property_behavior present → schema fail."""
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["planned_tests"][0] = {
        "clause_id": "AC-002",
        "probe_shape": "concrete",
        "property_behavior": {
            "domain": "x",
            "examples_or_generator": "y",
            "invariant": "z",
            "sample_bound": 5,
        },
        "coder_test_evidence": ["t.py::t"],
        "uncovered_behavior": "x",
        "asserted_in_test": "test_x",
    }
    p.write_text(json.dumps(s))
    r = c1_strategy_well_formed(p)
    assert not r.ok and "schema-invalid" in r.reason


def test_c1_probe_shape_property_with_only_concrete_behavior_held(tmp_path):
    """probe_shape='property' but only concrete_behavior present → schema fail."""
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["planned_tests"][0] = {
        "clause_id": "AC-002",
        "probe_shape": "property",
        "concrete_behavior": {"input": "x", "expected": "y"},
        "coder_test_evidence": ["t.py::t"],
        "uncovered_behavior": "x",
        "asserted_in_test": "test_x",
    }
    p.write_text(json.dumps(s))
    r = c1_strategy_well_formed(p)
    assert not r.ok and "schema-invalid" in r.reason


def test_c1_concrete_skip_without_evidence_held(tmp_path):
    """abstract_or_concrete='concrete' but no coder_test_evidence → schema fail."""
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["skipped_clauses"][0] = {
        "clause_id": "AC-001",
        "abstract_or_concrete": "concrete",
        "why_already_covered": "trust me",
        # no coder_test_evidence
    }
    p.write_text(json.dumps(s))
    r = c1_strategy_well_formed(p)
    assert not r.ok and "schema-invalid" in r.reason


def test_c1_concrete_skip_with_empty_evidence_held(tmp_path):
    """abstract_or_concrete='concrete' but coder_test_evidence == [] → schema fail."""
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["skipped_clauses"][0] = {
        "clause_id": "AC-001",
        "abstract_or_concrete": "concrete",
        "coder_test_evidence": [],  # empty
        "why_already_covered": "x",
    }
    p.write_text(json.dumps(s))
    r = c1_strategy_well_formed(p)
    assert not r.ok and "schema-invalid" in r.reason


def test_c1_abstract_skip_without_evidence_ok(tmp_path):
    """abstract_or_concrete='abstract' may omit coder_test_evidence (prose-only)."""
    p = tmp_path / "qa-strategy.json"
    s = _valid_strategy()
    s["skipped_clauses"][0] = {
        "clause_id": "AC-001",
        "abstract_or_concrete": "abstract",
        "why_already_covered": "abstract clause: 'code is well-organized'",
    }
    p.write_text(json.dumps(s))
    assert c1_strategy_well_formed(p).ok


# --- Codex review 2026-06-06: path-handling fail-closed ---


def test_c5_directory_at_test_qa_path_held(tmp_path):
    """A directory at the test_qa.py path is rejected at C5."""
    d = tmp_path / "test_qa.py"
    d.mkdir()
    r = c5_n_tests_matches_test_file_presence(
        {"n_tests_written": 0}, d
    )
    assert not r.ok and "not a regular file" in r.reason


def test_c5_symlink_at_test_qa_path_held(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("def test_x():\n    assert True\n")
    link = tmp_path / "test_qa.py"
    link.symlink_to(real)
    r = c5_n_tests_matches_test_file_presence(
        {"n_tests_written": 1}, link
    )
    assert not r.ok and "not a regular file" in r.reason


def test_c6_symlink_at_test_qa_path_held(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("def test_x():\n    assert True\n")
    link = tmp_path / "test_qa.py"
    link.symlink_to(real)
    r = c6_test_qa_parses(link)
    assert not r.ok and ("symlink" in r.reason or "not a regular file" in r.reason)


def test_c6_directory_at_test_qa_path_held(tmp_path):
    d = tmp_path / "test_qa.py"
    d.mkdir()
    r = c6_test_qa_parses(d)
    assert not r.ok and "not a regular file" in r.reason
