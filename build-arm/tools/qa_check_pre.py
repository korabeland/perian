#!/usr/bin/env python3
"""Pre-pytest QA checks (C1, C3-C12, C15, C18a) — runs AFTER Gemma writes, BEFORE pytest.

WHY THIS EXISTS (the QA layer, Rev 5 spec, 2026-06-06):
After Gemma produces `tmp_ws/qa-strategy.json` and (optionally) `tmp_ws/ws/test_qa.py`,
the runner must verify the strategy is well-formed and the test file is safe to execute
BEFORE letting pytest near it. This module is the deterministic floor between Gemma's
output and pytest. Every check is a pure function over its inputs; no model anywhere on
this path.

THE CHECKS (per Rev 5 spec section "qa_check_pre.py"):
- C1   qa-strategy.json exists, regular file, schema-valid.
- C2   (deprecated — clause coverage now via C18a).
- C3   Every `coder_test_evidence` nodeid exists in coder pytest collection.
- C4   `n_tests_written` ≤ 3.
- C5   `n_tests_written > 0` iff `tmp_ws/ws/test_qa.py` exists.
- C6   `test_qa.py` (if exists) parses with `ast.parse` cleanly.
- C7   `def test_` count in `test_qa.py` == `n_tests_written`.
- C8   Every test function docstring contains its strategy's `clause_id` (e.g. "[AC-002]").
- C9   AST allowlist: imports + names + attribute access + decorators, plus the inline-
       assertion rule (every test function body contains at least one `ast.Assert` or
       `pytest.raises` use — IMPORTED assertion helpers are not allowed; per goal must-fix 3).
- C10  Every `asserted_in_test` name in the strategy resolves to a `def test_` in the file.
- C11  No symlinks under `tmp_ws/ws/`.
- C12  Post-Gemma diff of `tmp_ws/ws/` against canonical: only `test_qa.py` differs.
- C15  Gemma swap succeeded AND Gemma run did not time out.
- C18a Completeness invariant: every parsed contract clause ID appears EXACTLY once across
       (planned_tests ∪ skipped_clauses). The "mint a confirm by omission" guard.

Each check is a top-level function returning `CheckResult(ok, name, reason)`. The runner
calls them in sequence; HOLD on first failure. Unit-tested per check.

DEFENSE-IN-DEPTH NOTE — the AST checks are the FAST-REJECT layer, not the trust property
(Rev 5 spec, section "The narrow AST allowlist (fast-reject; sandbox is the trust
property)"). Determined SUT mutation through dynamic constructs the AST cannot statically
analyze — container-wrapped module references (`m = (graph,); m[0].f = ...`), function-
return rebinding (`m = make(); m.f = ...`), runtime dict lookup (`d = {"g": graph};
d["g"].f = ...`) — would slip the AST allowlist. The layered guarantees are:
  (a) `sandbox-exec` blocks filesystem writes outside `tmp_ws` (kernel-enforced, in qa.py
      via the sandbox policy at run_pytest_sandboxed); in-memory mutation can still occur.
  (b) Codex's per-planned_test entailment audit (qa_route.py) reads each test's source and
      asks "would any compliant implementation fail this test?" — a test that mutates the
      SUT to make itself pass exhibits an obvious overreach/triviality signature that
      Codex's verdict catches as `entailment: overreach` or `triviality: trivial`.
  (c) The completeness invariant (C18a) plus per-clause Codex verdicts mean Gemma cannot
      mint a confirm by quietly omitting a clause or by writing a hollow probe.
The AST allowlist's job here is to make obvious cheats syntactically impossible and to
fast-reject patterns Gemma should never need; the deep semantic check is downstream.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import NamedTuple

import jsonschema

# =============================================================================
# qa-strategy.json schema (inlined for v1; promote to schemas/ in v2 if reused)
# =============================================================================
# Each planned_test must have EXACTLY one of (concrete_behavior, property_behavior). The
# JSON Schema `oneOf` enforces this. Skipped clauses must include coder evidence when
# concrete; abstract skips justify in prose.

QA_STRATEGY_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "reviewed_clauses",
        "planned_tests",
        "skipped_clauses",
        "n_tests_written",
    ],
    "additionalProperties": False,
    "properties": {
        "reviewed_clauses": {
            "type": "array",
            "items": {"type": "string"},
        },
        "planned_tests": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "clause_id",
                    "probe_shape",
                    "coder_test_evidence",
                    "uncovered_behavior",
                    "asserted_in_test",
                ],
                "additionalProperties": False,
                "properties": {
                    "clause_id": {"type": "string"},
                    "probe_shape": {"enum": ["concrete", "property"]},
                    "concrete_behavior": {
                        "type": "object",
                        "required": ["input", "expected"],
                        "additionalProperties": False,
                        "properties": {
                            "input": {"type": "string"},
                            "expected": {"type": "string"},
                        },
                    },
                    "property_behavior": {
                        "type": "object",
                        "required": [
                            "domain",
                            "examples_or_generator",
                            "invariant",
                            "sample_bound",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "domain": {"type": "string"},
                            "examples_or_generator": {"type": "string"},
                            "invariant": {"type": "string"},
                            "sample_bound": {"type": "integer", "maximum": 20},
                        },
                    },
                    "coder_test_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "uncovered_behavior": {"type": "string"},
                    "asserted_in_test": {"type": "string"},
                },
                # Probe-shape is TIED to which behavior object is required
                # (Codex review 2026-06-06): probe_shape=concrete must have
                # concrete_behavior present AND property_behavior absent, etc.
                "allOf": [
                    {
                        "if": {
                            "properties": {"probe_shape": {"const": "concrete"}},
                            "required": ["probe_shape"],
                        },
                        "then": {
                            "required": ["concrete_behavior"],
                            "not": {"required": ["property_behavior"]},
                        },
                    },
                    {
                        "if": {
                            "properties": {"probe_shape": {"const": "property"}},
                            "required": ["probe_shape"],
                        },
                        "then": {
                            "required": ["property_behavior"],
                            "not": {"required": ["concrete_behavior"]},
                        },
                    },
                ],
            },
        },
        "skipped_clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "clause_id",
                    "abstract_or_concrete",
                    "why_already_covered",
                ],
                "additionalProperties": False,
                "properties": {
                    "clause_id": {"type": "string"},
                    "abstract_or_concrete": {
                        "enum": ["abstract", "concrete"]
                    },
                    "coder_test_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "why_already_covered": {"type": "string"},
                },
                # Concrete skipped clauses MUST carry coder evidence with at least
                # one nodeid (Codex review 2026-06-06): an abstract skip can stand
                # on prose alone, but a concrete skip needs a testable citation.
                "if": {
                    "properties": {
                        "abstract_or_concrete": {"const": "concrete"}
                    },
                    "required": ["abstract_or_concrete"],
                },
                "then": {
                    "required": ["coder_test_evidence"],
                    "properties": {
                        "coder_test_evidence": {"minItems": 1}
                    },
                },
            },
        },
        "n_tests_written": {"type": "integer", "minimum": 0, "maximum": 3},
    },
}

# =============================================================================
# AST allowlist (per Rev 5 spec section "The narrow AST allowlist")
# =============================================================================
# Top-level modules allowed in test_qa.py imports (workspace modules added at runtime).
ALLOWED_STDLIB_IMPORTS: frozenset[str] = frozenset(
    {
        "pytest",  # but only selective attrs (see ALLOWED_PYTEST_ATTRS)
        "typing",
        "pathlib",
        "tempfile",
        "json",
        "re",
        "math",
        "decimal",
        "fractions",
        "string",
        "collections",
        "dataclasses",
        "enum",
        "itertools",
        "functools",
        "operator",
        "numbers",
    }
)

# pytest.X attribute access — X must be one of these.
ALLOWED_PYTEST_ATTRS: frozenset[str] = frozenset(
    {"raises", "approx", "param", "mark", "fixture", "tmp_path"}
)
# pytest.mark.X — only this mark in v1.
ALLOWED_PYTEST_MARK_ATTRS: frozenset[str] = frozenset({"parametrize"})

# Builtin names banned outright (use of `open` is also blocked at the kernel-sandbox level;
# the AST ban is a fast-reject layer, not the trust property).
BANNED_NAMES: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
        "breakpoint",
        "open",
    }
)

# Banned attribute access — block reflection paths into internals.
# Codex review (2026-06-06): widened to catch indirect reflection (.__getattribute__,
# .__getattr__, .__base__) so a test can't reach builtins via `obj.__getattribute__("...")`.
BANNED_ATTRS: frozenset[str] = frozenset(
    {
        "__builtins__",
        "__globals__",
        "__dict__",
        "__class__",
        "__mro__",
        "__subclasses__",
        "__base__",
        "__bases__",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
    }
)

# Banned decorator names — block tests that mint "passed" without running.
BANNED_DECORATOR_NAMES: frozenset[str] = frozenset(
    {"skip", "xfail", "importorskip", "skipif"}
)


# =============================================================================
# Result type
# =============================================================================


class CheckResult(NamedTuple):
    ok: bool
    name: str
    reason: str  # empty string if ok


# =============================================================================
# Helpers
# =============================================================================


def _ok(name: str) -> CheckResult:
    return CheckResult(True, name, "")


def _fail(name: str, reason: str) -> CheckResult:
    return CheckResult(False, name, reason)


def _path_chain_has_symlink(path: Path) -> tuple[bool, Path | None]:
    """Return (True, link_ancestor) if `path` itself OR any ancestor up to the filesystem
    root is a symlink. Codex review round 2 (2026-06-06): a symlinked PARENT redirects
    `is_file()` resolution; harden by walking the whole chain via lstat.
    """
    cur = path
    while True:
        if cur.is_symlink():
            return True, cur
        parent = cur.parent
        if parent == cur:
            return False, None
        cur = parent


def _read_and_parse_test_qa(
    test_qa_path: Path, check_name: str
) -> tuple[CheckResult | None, ast.Module | None]:
    """Centralized fail-closed read + AST parse for test_qa.py.

    Returns (None, tree) on success and (fail_result, None) on any read/parse error.
    Codex review (2026-06-06): callers were doing ad-hoc `exists()` + `read_text()` which
    crashes on a directory-at-path, follows symlinks before C11 fires, and surfaces
    UnicodeDecodeError unwrapped. Round 2 added: parent-chain symlink rejection — any link
    in the path ancestor chain redirects `is_file()` and is itself an attack surface.
    """
    linked, where = _path_chain_has_symlink(test_qa_path)
    if linked:
        return _fail(
            check_name,
            f"symlink in test_qa.py path chain at {where} (rejected)",
        ), None
    if not test_qa_path.is_file():
        return _fail(
            check_name,
            f"test_qa.py is not a regular file: {test_qa_path}",
        ), None
    try:
        raw = test_qa_path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        return _fail(check_name, f"test_qa.py unreadable: {e}"), None
    try:
        tree = ast.parse(raw, filename=str(test_qa_path))
    except SyntaxError as e:
        return _fail(check_name, f"test_qa.py SyntaxError: {e}"), None
    return None, tree


# =============================================================================
# C1 — strategy file exists, regular file, schema-valid
# =============================================================================


def c1_strategy_well_formed(strategy_path: Path) -> CheckResult:
    """C1: tmp_ws/qa-strategy.json exists, regular file, schema-valid."""
    name = "C1"
    if not strategy_path.exists():
        return _fail(name, f"qa-strategy.json missing at {strategy_path}")
    if not strategy_path.is_file() or strategy_path.is_symlink():
        return _fail(name, f"qa-strategy.json not a regular file: {strategy_path}")
    try:
        data = json.loads(strategy_path.read_text())
    except json.JSONDecodeError as e:
        return _fail(name, f"qa-strategy.json not valid JSON: {e}")
    try:
        jsonschema.validate(data, QA_STRATEGY_SCHEMA)
    except jsonschema.ValidationError as e:
        return _fail(name, f"qa-strategy.json schema-invalid: {e.message}")
    return _ok(name)


# =============================================================================
# C3 — coder_test_evidence nodeids exist in coder pytest collection
# =============================================================================


def c3_evidence_nodeids_exist(
    strategy: dict, coder_pytest_nodeids: set[str]
) -> CheckResult:
    """C3: every cited coder_test_evidence nodeid was collected by pytest in the coder ws.

    `coder_pytest_nodeids` is the set returned by `pytest --collect-only -q` in a
    SEPARATE DISPOSABLE COPY of the coder workspace (Rev 5 spec — never the canonical).
    """
    name = "C3"
    cited: set[str] = set()
    for pt in strategy.get("planned_tests") or []:
        cited.update(pt.get("coder_test_evidence") or [])
    for sc in strategy.get("skipped_clauses") or []:
        cited.update(sc.get("coder_test_evidence") or [])
    missing = sorted(cited - coder_pytest_nodeids)
    if missing:
        return _fail(
            name,
            f"cited coder_test_evidence nodeids not in coder pytest collection: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}",
        )
    return _ok(name)


# =============================================================================
# C4 — n_tests_written ≤ 3
# =============================================================================


def c4_n_tests_capped(strategy: dict) -> CheckResult:
    """C4: strategy.n_tests_written must be in [0, 3]."""
    name = "C4"
    n = strategy.get("n_tests_written")
    if not isinstance(n, int):
        return _fail(name, f"n_tests_written not an int: {type(n).__name__}")
    if not (0 <= n <= 3):
        return _fail(name, f"n_tests_written out of range [0, 3]: {n}")
    return _ok(name)


# =============================================================================
# C5 — n_tests_written > 0 ↔ test_qa.py exists
# =============================================================================


def c5_n_tests_matches_test_file_presence(
    strategy: dict, test_qa_path: Path
) -> CheckResult:
    """C5: n_tests_written > 0 IFF test_qa.py exists as a REGULAR (non-symlink) file.

    Codex review (2026-06-06): a directory or symlink at this path used to look like
    "exists" to the old check but would crash later. Fail closed here.
    """
    name = "C5"
    n = strategy.get("n_tests_written", 0)
    exists = test_qa_path.exists()
    if exists:
        if test_qa_path.is_symlink() or not test_qa_path.is_file():
            return _fail(
                name,
                f"test_qa.py at {test_qa_path} is not a regular file "
                f"(symlinks and directories rejected)",
            )
    if n > 0 and not exists:
        return _fail(name, f"n_tests_written={n} but {test_qa_path} missing")
    if n == 0 and exists:
        return _fail(
            name, f"n_tests_written=0 but {test_qa_path} exists"
        )
    return _ok(name)


# =============================================================================
# C6 — test_qa.py parses with ast.parse cleanly
# =============================================================================


def c6_test_qa_parses(test_qa_path: Path) -> CheckResult:
    """C6: test_qa.py is syntactically valid Python (when it exists), is a regular file
    (not a directory, not a symlink), and is UTF-8 readable.
    """
    name = "C6"
    if not test_qa_path.exists():
        return _ok(name)  # no file → no parse → no failure (C5 enforces consistency)
    fail, _ = _read_and_parse_test_qa(test_qa_path, name)
    return fail if fail is not None else _ok(name)


# =============================================================================
# C7 — def test_ count in test_qa.py == n_tests_written
# =============================================================================


def _test_func_names(tree: ast.AST) -> list[str]:
    """Return names of top-level `def test_X` functions in the AST."""
    out: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                out.append(node.name)
    return out


def c7_planned_tests_bijective(strategy: dict, test_qa_path: Path) -> CheckResult:
    """C7: bijection between planned_tests and `def test_` functions in test_qa.py.

    Codex review (2026-06-06) noted that a mere count check (count == n_tests_written)
    let an EXTRA unplanned `def test_` slip through (pytest would still run it). We now
    require multiset equality:
      - len(planned_tests) == n_tests_written
      - Counter(planned.asserted_in_test) == Counter(top_level_test_func_names)
    so every executed test traces to a clause, and every planned clause traces to one
    executed test. No extras, no duplicates, no collapsed names.
    """
    from collections import Counter

    name = "C7"
    n = strategy.get("n_tests_written", 0)
    planned = strategy.get("planned_tests") or []
    if len(planned) != n:
        return _fail(
            name,
            f"len(planned_tests)={len(planned)} but n_tests_written={n}",
        )
    if not test_qa_path.exists():
        if n == 0:
            return _ok(name)
        return _fail(name, f"n_tests_written={n} but test_qa.py missing")
    fail, tree = _read_and_parse_test_qa(test_qa_path, name)
    if fail is not None:
        return fail
    assert tree is not None
    actual_names = _test_func_names(tree)
    expected_names = [pt.get("asserted_in_test", "") for pt in planned]
    if Counter(actual_names) != Counter(expected_names):
        # Helpful diff for the operator.
        actual_set = Counter(actual_names)
        expected_set = Counter(expected_names)
        extras = sorted((actual_set - expected_set).elements())
        missing = sorted((expected_set - actual_set).elements())
        bits: list[str] = []
        if extras:
            bits.append(f"extra unplanned test funcs: {extras[:5]}")
        if missing:
            bits.append(f"planned-but-not-defined: {missing[:5]}")
        return _fail(name, "; ".join(bits) or "test name multiset mismatch")
    return _ok(name)


# Backwards-compatible alias retained so the runner orchestrator can reference either.
c7_def_test_count_matches = c7_planned_tests_bijective


# =============================================================================
# C8 — every test function docstring contains its strategy's clause_id
# =============================================================================


def c8_docstrings_cite_clause_id(
    strategy: dict, test_qa_path: Path
) -> CheckResult:
    """C8: each `def test_X` whose name == an `asserted_in_test` must contain that test's
    clause_id (e.g. `[AC-002]`) somewhere in its docstring.
    """
    name = "C8"
    if not test_qa_path.exists():
        return _ok(name)
    fail, tree = _read_and_parse_test_qa(test_qa_path, name)
    if fail is not None:
        return fail
    assert tree is not None
    # Map test_name → clause_id from strategy.
    expected: dict[str, str] = {}
    for pt in strategy.get("planned_tests") or []:
        tn = pt.get("asserted_in_test")
        cid = pt.get("clause_id")
        if tn and cid:
            expected[tn] = cid
    # For each test function present, if it's an asserted_in_test, check its docstring.
    func_map: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_map[node.name] = node
    missing: list[str] = []
    for test_name, clause_id in expected.items():
        node = func_map.get(test_name)
        if node is None:
            # C10 will catch this; skip here so we don't double-report.
            continue
        doc = ast.get_docstring(node) or ""
        token = f"[{clause_id}]"
        if token not in doc:
            missing.append(f"{test_name} missing {token} in docstring")
    if missing:
        return _fail(name, "; ".join(missing[:3]))
    return _ok(name)


# =============================================================================
# C9 — AST allowlist (imports, names, attribute access, decorators, inline-assert)
# =============================================================================


def _check_imports(
    tree: ast.AST, allowed_modules: set[str]
) -> list[str]:
    """Return list of disallowed-import error strings.

    Codex review round 4 (2026-06-06): wildcard `from <ws_module> import *` is rejected
    outright. The runtime-bound names from a star-import never appear in the AST, so the
    binding tracker can't see them, and a subsequent `build_graph.__code__ = fake.__code__`
    would slip past `_check_no_runtime_patching`. The simplest closure is to ban the form.
    """
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top not in allowed_modules:
                    errors.append(f"disallowed import: {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".", 1)[0]
            if mod not in allowed_modules:
                errors.append(f"disallowed import from: {node.module!r}")
            elif mod == "pytest":
                # Only selective attrs from pytest.
                for alias in node.names:
                    if alias.name == "*":
                        errors.append("disallowed wildcard import from pytest")
                    elif alias.name not in ALLOWED_PYTEST_ATTRS:
                        errors.append(
                            f"disallowed pytest import: {alias.name!r}"
                        )
            else:
                # Reject wildcard from-imports for ALL modules (workspace or stdlib).
                # The unbound runtime names slip past the AST binding tracker.
                for alias in node.names:
                    if alias.name == "*":
                        errors.append(
                            f"disallowed wildcard import from {node.module!r} "
                            f"(use explicit names)"
                        )
    return errors


def _check_names(tree: ast.AST) -> list[str]:
    """Return list of banned-name use error strings."""
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            errors.append(f"banned name use: {node.id!r}")
    return errors


def _check_attrs(tree: ast.AST) -> list[str]:
    """Return list of banned-attribute-access error strings."""
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRS:
                errors.append(f"banned attr access: .{node.attr}")
            # pytest.X check — X must be in ALLOWED_PYTEST_ATTRS.
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "pytest"
                and node.attr not in ALLOWED_PYTEST_ATTRS
            ):
                errors.append(f"disallowed pytest attr: pytest.{node.attr}")
            # pytest.mark.X check — X must be in ALLOWED_PYTEST_MARK_ATTRS.
            if (
                isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "pytest"
                and node.value.attr == "mark"
                and node.attr not in ALLOWED_PYTEST_MARK_ATTRS
            ):
                errors.append(f"disallowed pytest.mark attr: pytest.mark.{node.attr}")
    return errors


def _decorator_name(dec: ast.expr) -> str | None:
    """Return the rightmost name of a decorator expression, or None if not extractable.

    `@skip` → "skip", `@pytest.mark.skip` → "skip", `@pytest.mark.parametrize(...)` →
    "parametrize", `@pytest.mark.skipif(...)` → "skipif".
    """
    # Strip Call() to its func.
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return dec.attr
    return None


def _check_decorators(tree: ast.AST) -> list[str]:
    """Return list of banned-decorator error strings."""
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                dname = _decorator_name(dec)
                if dname is None:
                    continue
                if dname in BANNED_DECORATOR_NAMES:
                    errors.append(
                        f"banned decorator @{dname} on {node.name}"
                    )
                # Also catch direct pytest.MonkeyPatch / pytest.main even if not in
                # BANNED_DECORATOR_NAMES — they're banned at module level.
                if dname in {"MonkeyPatch", "main"}:
                    errors.append(
                        f"banned decorator @{dname} on {node.name}"
                    )
    return errors


def _executable_descendants(node: ast.AST):
    """Yield nodes reachable from `node` while SKIPPING subtrees rooted at nested defs,
    classes, and lambdas.

    Codex review (2026-06-06): `ast.walk(func)` descended into unused nested helpers, so a
    `def test_x():\n    def _helper():\n        assert True` falsely satisfied the inline-
    assertion rule even though the helper was never called. This walker stays inside the
    test function's own executable body.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        yield child
        yield from _executable_descendants(child)


def _is_pytest_raises_func(func: ast.expr) -> bool:
    """True if `func` is `pytest.raises` or a bare `raises` (after `from pytest import raises`)."""
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "raises"
            and isinstance(func.value, ast.Name)
            and func.value.id == "pytest"
        )
    if isinstance(func, ast.Name):
        return func.id == "raises"
    return False


def _has_inline_assertion(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if `func`'s own executable body contains at least one effective inline
    assertion: an `ast.Assert` OR a `with pytest.raises(...):` block.

    Codex review (2026-06-06 round 2): the callable-form `pytest.raises(Exc, func, ...)`
    was previously counted as effective, but Gemma could pass it a non-callable second arg
    (`pytest.raises(TypeError, "x")`) to satisfy the rule without probing the SUT. The
    `with` form is the only unambiguous Pythonic idiom and the only one we now accept.
    Nested function bodies do NOT count toward the parent test's assertion budget.
    """
    for node in _executable_descendants(func):
        if isinstance(node, ast.Assert):
            return True
        # `with pytest.raises(...):` — the With.items[i].context_expr is a Call to
        # pytest.raises (or bare `raises` after `from pytest import raises`).
        if isinstance(node, ast.With):
            for item in node.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _is_pytest_raises_func(ce.func):
                    return True
    return False


def _check_inline_assertions(tree: ast.AST) -> list[str]:
    """Each test_ function body must contain at least one effective inline assertion."""
    errors: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_") and not _has_inline_assertion(node):
                errors.append(
                    f"test function {node.name!r} has no inline assert or effective "
                    f"pytest.raises (imported assertion helpers are not allowed; bare "
                    f"`pytest.raises(Exc)` without `with` or a callable arg asserts nothing)"
                )
    return errors


# Banned forbidden-fixture parameter names: monkeypatch directly (canonical), AND request
# (a route to monkeypatch via request.getfixturevalue("monkeypatch")). Codex review round 2
# (2026-06-06) flagged the indirect path.
BANNED_FIXTURE_PARAMS: frozenset[str] = frozenset({"monkeypatch", "request"})

# Method names whose call indicates runtime patching. Catches calls like
# `mp.setattr(...)`, `mp.delattr(...)`, `mp.setenv(...)` on any object, not just the
# canonical `monkeypatch.` chain — closes the indirect-via-getfixturevalue route.
BANNED_PATCH_METHOD_NAMES: frozenset[str] = frozenset(
    {"setattr", "delattr", "setitem", "delitem", "setenv", "delenv", "syspath_prepend", "chdir"}
)


def _ws_module_bindings(tree: ast.AST, workspace_module_names: set[str]) -> set[str]:
    """Track which local names bind to workspace modules OR to values imported FROM them.

    Codex review round 3 (2026-06-06) flagged three bypass routes left open by a binding
    tracker that only saw direct `import X` aliases:

      (a) `m = graph` then `m.build_graph = fake` — local rebinding.
      (b) `from graph import build_graph` then `build_graph.__code__ = fake.__code__` —
          mutating an imported function's bytecode also mutates `graph.build_graph`.
      (c) `from graph import C` then `C.method = fake` — mutating an imported class also
          mutates `graph.C`.

    The fix: treat `from <ws_module> import X` as binding X, and propagate any simple
    `name = <existing-binding>` assignment to the LHS. Conservative / flow-insensitive:
    once a name appears as a workspace-derived binding ANYWHERE in the source, it stays
    protected. False positives possible; trust-spine wins.

    Import forms covered:
      `import graph`                 → binds 'graph'
      `import graph as g`            → binds 'g'
      `import graph.sub`             → binds 'graph' (top-level binding)
      `import graph.sub as gs`       → binds 'gs'
      `from graph import X`          → binds 'X'  (NEW in Rev 3)
      `from graph import X as Y`     → binds 'Y'  (NEW in Rev 3)

    Propagation:
      `m = graph`                    → binds 'm'   (NEW in Rev 3)
      `n = m` (m already bound)      → binds 'n'   (NEW in Rev 3; fixed-point closure)
      `m: ModuleType = graph`        → binds 'm'   (annotated assignment)
    """
    bindings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in workspace_module_names:
                    bindings.add(alias.asname or top)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".", 1)[0]
            if mod in workspace_module_names:
                for alias in node.names:
                    # `from graph import *` — alias.name == '*' — skip the wildcard token.
                    if alias.name == "*":
                        continue
                    bindings.add(alias.asname or alias.name)

    # Fixed-point: propagate `name = <existing-binding>` and `name: T = <existing>`.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if (
                    len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in bindings
                ):
                    tgt = node.targets[0].id
                    if tgt not in bindings:
                        bindings.add(tgt)
                        changed = True
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in bindings
                ):
                    tgt = node.target.id
                    if tgt not in bindings:
                        bindings.add(tgt)
                        changed = True
    return bindings


def _iter_mutation_target_subs(target: ast.expr):
    """Yield every leaf expression that is a mutation target, walking into Tuple/List
    destructuring. A leaf is anything that's not a Tuple/List/Starred."""
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _iter_mutation_target_subs(elt)
    elif isinstance(target, ast.Starred):
        yield from _iter_mutation_target_subs(target.value)
    else:
        yield target


def _root_name(node: ast.expr) -> str | None:
    """For an attribute/subscript chain like `a.b.c[0]`, return the leftmost Name id ('a').
    Returns None for non-Name roots (e.g. Call().attr = ..., a Constant subscript)."""
    cur: ast.expr = node
    while isinstance(cur, (ast.Attribute, ast.Subscript)):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return None


def _is_ws_mutation_target(target: ast.expr, ws_bindings: set[str]) -> bool:
    """A target is a workspace-module mutation if it is a leaf Attribute or Subscript whose
    root Name resolves to a workspace-module binding."""
    leaves = list(_iter_mutation_target_subs(target))
    for leaf in leaves:
        if isinstance(leaf, (ast.Attribute, ast.Subscript)):
            root = _root_name(leaf)
            if root in ws_bindings:
                return True
    return False


def _check_no_runtime_patching(
    tree: ast.AST, workspace_module_names: set[str]
) -> list[str]:
    """Reject runtime patching of imported workspace modules and the pytest patch APIs.

    Codex review (2026-06-06, rounds 1 + 2). What this now catches:

    Fixture-shaped patches:
      - Any `def test_X(monkeypatch)` or `def test_X(request)` parameter (incl. positional-
        only and kwonly).
      - `<anything>.setattr|delattr|setitem|delitem|setenv|delenv|syspath_prepend|chdir(...)`
        call — these are pytest.MonkeyPatch's interface. Banning the method names directly
        closes the indirect path `mp = request.getfixturevalue("monkeypatch"); mp.setattr`.
      - `.getfixturevalue(...)` call (regardless of receiver).

    Direct mutation of workspace modules:
      - Import bindings tracked: `import graph as g; g.f = ...` is caught even though
        `_root_name` sees 'g', not 'graph'.
      - All target node types: `ast.Assign`, `ast.AnnAssign`, `ast.AugAssign`, `ast.Delete`,
        `ast.For`/`ast.AsyncFor`, `ast.With` (`optional_vars`), `ast.NamedExpr` (walrus).
      - All target shapes: bare Attribute/Subscript, tuple/list destructuring, starred.
    """
    errors: list[str] = []
    ws_bindings = _ws_module_bindings(tree, workspace_module_names)

    for node in ast.walk(tree):
        # Banned fixture parameter names in test_ functions.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                all_args = (
                    node.args.args
                    + node.args.kwonlyargs
                    + node.args.posonlyargs
                )
                for arg in all_args:
                    if arg.arg in BANNED_FIXTURE_PARAMS:
                        errors.append(
                            f"banned fixture {arg.arg!r} in test {node.name!r}"
                        )

        # Banned patch-API calls (setattr/setenv/etc.) on ANY receiver.
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                if f.attr in BANNED_PATCH_METHOD_NAMES:
                    errors.append(
                        f"banned patch-API call: .{f.attr}(...) "
                        f"(monkeypatch and equivalents are blocked)"
                    )
                if f.attr == "getfixturevalue":
                    errors.append(
                        "banned indirect fixture retrieval: .getfixturevalue(...)"
                    )

        # Workspace-module mutation across every assign/del/for/with/walrus target form.
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        elif isinstance(node, ast.AugAssign):
            targets.append(node.target)
        elif isinstance(node, ast.Delete):
            targets.extend(node.targets)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets.append(node.target)
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars is not None:
                    targets.append(item.optional_vars)
        elif isinstance(node, ast.NamedExpr):  # walrus
            targets.append(node.target)

        for t in targets:
            if _is_ws_mutation_target(t, ws_bindings):
                errors.append(
                    f"banned workspace-module mutation: target with root "
                    f"in workspace bindings {sorted(ws_bindings)}"
                )

    return errors


def c9_ast_allowlist(
    test_qa_path: Path, workspace_module_names: set[str]
) -> CheckResult:
    """C9: imports + names + attrs + decorators + inline-assertion checks.

    `workspace_module_names` is the set of top-level module names available under
    `tmp_ws/ws/` (computed by the runner from `*.py` filenames there). Imports of any of
    these are allowed; otherwise only modules in ALLOWED_STDLIB_IMPORTS pass.
    """
    name = "C9"
    if not test_qa_path.exists():
        return _ok(name)
    fail, tree = _read_and_parse_test_qa(test_qa_path, name)
    if fail is not None:
        return fail
    assert tree is not None
    allowed_modules = set(ALLOWED_STDLIB_IMPORTS) | set(workspace_module_names)
    errors: list[str] = []
    errors.extend(_check_imports(tree, allowed_modules))
    errors.extend(_check_names(tree))
    errors.extend(_check_attrs(tree))
    errors.extend(_check_decorators(tree))
    errors.extend(_check_inline_assertions(tree))
    errors.extend(_check_no_runtime_patching(tree, workspace_module_names))
    if errors:
        return _fail(name, "; ".join(errors[:5]))
    return _ok(name)


# =============================================================================
# C10 — every asserted_in_test resolves to a def test_ in the file
# =============================================================================


def c10_asserted_in_test_resolves(
    strategy: dict, test_qa_path: Path
) -> CheckResult:
    """C10: each planned_test.asserted_in_test name must exist as `def test_X` in file."""
    name = "C10"
    expected = [
        pt.get("asserted_in_test", "")
        for pt in (strategy.get("planned_tests") or [])
    ]
    expected = [e for e in expected if e]
    if not expected:
        return _ok(name)
    if not test_qa_path.exists():
        return _fail(
            name,
            f"strategy references asserted_in_test names but {test_qa_path} missing",
        )
    fail, tree = _read_and_parse_test_qa(test_qa_path, name)
    if fail is not None:
        return fail
    assert tree is not None
    present = set(_test_func_names(tree))
    missing = sorted(set(expected) - present)
    if missing:
        return _fail(
            name,
            f"asserted_in_test names not found as def test_: {missing[:5]}",
        )
    return _ok(name)


# =============================================================================
# C11 — no symlinks under tmp_ws/ws/
# =============================================================================


def c11_no_symlinks(ws_path: Path) -> CheckResult:
    """C11: no symlinks anywhere under (or at) the workspace dir.

    Codex review round 2 (2026-06-06): the ws root itself being a symlink redirects every
    downstream `is_file()`/`rglob` check. Reject that explicitly.
    """
    name = "C11"
    if ws_path.is_symlink():
        return _fail(name, f"ws path is itself a symlink: {ws_path}")
    if not ws_path.is_dir():
        return _fail(name, f"ws path is not a directory: {ws_path}")
    symlinks: list[str] = []
    for p in ws_path.rglob("*"):
        if p.is_symlink():
            symlinks.append(str(p.relative_to(ws_path)))
    if symlinks:
        return _fail(name, f"symlinks present under ws: {symlinks[:5]}")
    return _ok(name)


# =============================================================================
# C12 — tmp_ws/ws/ post-Gemma diff against canonical: only test_qa.py differs
# =============================================================================


def c12_only_test_qa_differs(
    tmp_ws_path: Path,
    canonical_ws_path: Path,
    test_qa_relname: str = "test_qa.py",
) -> CheckResult:
    """C12: of the files under tmp_ws/ws that differ from canonical_ws, only test_qa.py
    is permitted.

    Strategy: walk both trees, compute a per-relative-path content set (path → bytes
    sha256), diff the two sets. The only allowed differing path is `test_qa.py`.
    """
    name = "C12"
    if not tmp_ws_path.is_dir() or not canonical_ws_path.is_dir():
        return _fail(
            name,
            f"missing dir: tmp_ws={tmp_ws_path} or canonical={canonical_ws_path}",
        )
    import hashlib

    def _hash_tree(root: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = str(p.relative_to(root))
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    a = _hash_tree(tmp_ws_path)
    b = _hash_tree(canonical_ws_path)
    diffs: list[str] = []
    all_keys = set(a) | set(b)
    for rel in sorted(all_keys):
        if a.get(rel) != b.get(rel):
            diffs.append(rel)
    bad = [d for d in diffs if d != test_qa_relname]
    if bad:
        return _fail(
            name,
            f"unexpected differences vs canonical (only {test_qa_relname} allowed): "
            f"{bad[:5]}",
        )
    return _ok(name)


# =============================================================================
# C15 — Gemma swap succeeded; Gemma run did not time out
# =============================================================================


def c15_gemma_ran_successfully(gemma_result: dict) -> CheckResult:
    """C15: the Gemma swap + invocation completed without error or timeout.

    `gemma_result` shape (Rev 5 runner contract):
        {
          "swap_ok": bool,
          "timed_out": bool,
          "exit_code": int,
          "error": str,
        }
    """
    name = "C15"
    if not gemma_result.get("swap_ok", False):
        return _fail(name, f"Gemma swap failed: {gemma_result.get('error', '?')}")
    if gemma_result.get("timed_out", False):
        return _fail(name, "Gemma run timed out")
    exit_code = gemma_result.get("exit_code")
    if exit_code != 0:
        return _fail(
            name,
            f"Gemma exit_code={exit_code}: {gemma_result.get('error', '?')}",
        )
    return _ok(name)


# =============================================================================
# C18a — completeness invariant (the heart of the QA layer)
# =============================================================================


def c18a_clause_completeness(
    strategy: dict, contract_clauses: list[dict]
) -> CheckResult:
    """C18a: every contract clause ID appears EXACTLY once across the strategy's
    (planned_tests ∪ skipped_clauses).

    A clause appearing twice (double-cited) is also an error: it suggests Gemma planned AND
    skipped the same clause (or two planned tests for the same clause), which means the
    "exactly once" invariant is violated and routing logic downstream is undefined.
    """
    name = "C18a"
    contract_ids: list[str] = [c["id"] for c in contract_clauses]
    contract_set = set(contract_ids)

    planned_ids = [
        pt.get("clause_id", "") for pt in (strategy.get("planned_tests") or [])
    ]
    skipped_ids = [
        sc.get("clause_id", "") for sc in (strategy.get("skipped_clauses") or [])
    ]
    cited = planned_ids + skipped_ids
    cited_set = set(cited)

    from collections import Counter

    unrepresented = sorted(contract_set - cited_set)
    unknown = sorted(cited_set - contract_set)
    counts = Counter(cited)
    duplicates = sorted(cid for cid, n in counts.items() if n > 1)

    bits: list[str] = []
    if unrepresented:
        bits.append(
            f"unrepresented contract clauses (Gemma must plan-or-skip each): "
            f"{unrepresented[:10]}{'...' if len(unrepresented) > 10 else ''}"
        )
    if unknown:
        bits.append(
            f"strategy cites clause IDs not in contract: {unknown[:10]}"
        )
    if duplicates:
        bits.append(f"clause IDs cited more than once: {duplicates[:10]}")
    if bits:
        # Codex review (2026-06-06): accumulate all three violation classes into one
        # reason so the operator sees the FULL picture in one HOLD message.
        return _fail(name, " | ".join(bits))
    return _ok(name)
