#!/usr/bin/env python3
"""QA routing — pure compute_route, route_input construction, Codex per-item verdicts.

WHY THIS EXISTS (the QA layer, Rev 5 spec, 2026-06-06):
After the pre-pytest checks pass and pytest runs in the sandbox, the runner must decide
where the build goes next: `confirm`, `coder` (real bug surfaced), `gemma` (Gemma's
output is bad), or `hold_human` (uncertain). The Rev 5 spec moves this decision OUT of
Codex's authorship — Codex emits PER-ITEM verdicts about each planned test and each
skipped clause; the runner computes the route deterministically from those verdicts via
a pure function. Codex's emitted `declared_route` is telemetry only.

PUBLIC API:
- `compute_route(per_planned, per_skipped, pytest_exit, unrepresented_clauses) -> str`
  PURE; the load-bearing decision function. Unit-tested with table-driven cases.
- `validate_verdict_cardinality(strategy, codex_verdicts) -> CheckResult` — multiset
  equality between strategy clause IDs and Codex per-item clause IDs (goal must-fix 2).
- `build_route_input(...)` — assemble the dict captured BEFORE tmp_ws cleanup.
- `call_codex_route(route_input, ...)` — invoke Codex with the route_input prompt,
  return parsed per-item verdicts (strict schema enforcement).
- `validate_and_compute_route(strategy, codex_verdicts, pytest_exit, ...)` — top-level
  helper that runs cardinality check → compute route → assembles the qa-route.json dict.

GOAL MUST-FIX ITEMS APPLIED HERE:
- (1) `compute_route` zero-test guard: `pytest_exit != 0 and not per_planned → hold_human`.
      Without this, `all(... for p in per_planned)` over an empty list evaluates to True,
      and the function would route to `coder` even though no test failed because no test
      existed.
- (2) Cardinality check: assert exactly one Codex verdict per planned test and one per
      skipped clause, matched by clause_id, BEFORE compute_route runs. Codex emitting
      verdicts for ids the strategy doesn't claim (or omitting some) → HOLD.
- (4) Drop Codex's declared route: the spec previously had the runner compare
      declared_route to computed_route and HOLD on mismatch. The simpler rule is "never
      let Codex author the trust-spine control bit at all" — `declared_route` is
      preserved in the qa-route.json output for forensics/telemetry but never
      compared and never triggers a HOLD.
"""

from __future__ import annotations

import ast
import json
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import NamedTuple


# =============================================================================
# Result type
# =============================================================================


class CheckResult(NamedTuple):
    ok: bool
    name: str
    reason: str


def _ok(name: str) -> CheckResult:
    return CheckResult(True, name, "")


def _fail(name: str, reason: str) -> CheckResult:
    return CheckResult(False, name, reason)


# =============================================================================
# compute_route — the pure decision function
# =============================================================================


_VALID_ROUTES = frozenset({"coder", "gemma", "confirm", "hold_human"})


def compute_route(
    per_planned: list[dict],
    per_skipped: list[dict],
    pytest_exit: int,
    unrepresented_clauses: list[str],
) -> str:
    """Compute the QA route deterministically from per-item verdicts.

    Decision order (Rev 5 spec section "The deterministic route function"):
      1. Any unrepresented contract clause → hold_human (defensive belt; C18a already
         caught this in pre-pytest, but the runner re-checks here for safety).
      2. Any `entailment_verdict: uncertain` in planned → hold_human (Codex couldn't
         decide whether the test is fair or overreach).
      3. Any `audit_verdict: uncertain` in skipped → hold_human.
      4. Any `entailment_verdict: overreach` in planned → gemma (Gemma's test asserts
         more than the contract requires; rework strategy).
      5. Any `triviality_verdict: trivial` in planned → gemma (Gemma planned a useless
         test that would pass on any obviously-buggy implementation).
      6. Any `audit_verdict: skip_hollow` in skipped → gemma (Gemma claimed a skip but
         evidence doesn't support it).
      7. pytest_exit != 0:
         a. If no planned tests (n_tests_written == 0 and pytest failed for some other
            reason — unlikely but possible) → hold_human (goal must-fix 1: closes the
            `all([]) == True` corner that would otherwise route to coder).
         b. If all planned have `entailment_verdict: fair` → coder (Gemma's tests fairly
            probed a real bug in the coder's code).
         c. Else → hold_human (mixed verdicts; needs human eyes).
      8. pytest_exit == 0 → confirm.

    Inputs are list[dict] (not NamedTuple) so they can be hydrated directly from JSON.
    Each dict provides `entailment_verdict` / `triviality_verdict` for planned items and
    `audit_verdict` for skipped items.
    """
    if unrepresented_clauses:
        return "hold_human"
    if any(p.get("entailment_verdict") == "uncertain" for p in per_planned):
        return "hold_human"
    if any(s.get("audit_verdict") == "uncertain" for s in per_skipped):
        return "hold_human"
    if any(p.get("entailment_verdict") == "overreach" for p in per_planned):
        return "gemma"
    if any(p.get("triviality_verdict") == "trivial" for p in per_planned):
        return "gemma"
    if any(s.get("audit_verdict") == "skip_hollow" for s in per_skipped):
        return "gemma"
    # Codex review (2026-06-06) MEDIUM 2: only pytest's ACTUAL TEST FAILURE code
    # (`pytest_exit == 1`) means real-bug-in-coder. Other nonzero codes are
    # operational failures that need human triage:
    #   0  ok                  (handled below as `confirm`)
    #   1  tests failed        -> coder (if all fair)
    #   2  interrupted         -> hold_human
    #   3  internal error      -> hold_human
    #   4  pytest usage error  -> hold_human
    #   5  no tests collected  -> hold_human
    #   <0 killed by signal    -> hold_human
    if pytest_exit == 1:
        # Goal must-fix 1: zero-test guard. all([]) is True — without this branch a
        # pytest failure with no planned tests would incorrectly route to "coder".
        if not per_planned:
            return "hold_human"
        if all(
            p.get("entailment_verdict") == "fair" for p in per_planned
        ):
            return "coder"
        return "hold_human"
    if pytest_exit != 0:
        # Non-1 nonzero: operational failure, not a real coder bug.
        return "hold_human"
    return "confirm"


# =============================================================================
# Verdict cardinality check — goal must-fix 2
# =============================================================================


def validate_verdict_cardinality(
    strategy: dict, codex_verdicts: dict
) -> CheckResult:
    """Goal must-fix 2: the strategy's planned/skipped clause IDs and Codex's per-item
    verdict clause IDs must be MULTISET-EQUAL. Codex emitting verdicts for ids the
    strategy doesn't claim, OR omitting some, → HOLD before compute_route runs.

    A multiset (Counter) equality catches both extras and duplicates (a single clause id
    appearing twice in Codex verdicts is rejected). Subset-checking would miss those.
    """
    name = "verdict_cardinality"

    planned_clause_ids = [
        pt.get("clause_id") for pt in strategy.get("planned_tests") or []
    ]
    skipped_clause_ids = [
        sc.get("clause_id") for sc in strategy.get("skipped_clauses") or []
    ]

    pp = codex_verdicts.get("codex_per_planned_test") or []
    sp = codex_verdicts.get("codex_per_skipped_clause") or []
    pp_ids = [v.get("clause_id") for v in pp]
    sp_ids = [v.get("clause_id") for v in sp]

    # Codex review (2026-06-06): `sorted([None, "AC-001"])` raises TypeError on Py3.
    # Use `repr` as a safe sort key so a malformed None clause_id surfaces cleanly
    # in the failure message instead of crashing the runner.
    if Counter(pp_ids) != Counter(planned_clause_ids):
        return _fail(
            name,
            f"planned-test verdict ids mismatch: "
            f"codex={sorted(pp_ids, key=repr)}, "
            f"strategy={sorted(planned_clause_ids, key=repr)}",
        )
    if Counter(sp_ids) != Counter(skipped_clause_ids):
        return _fail(
            name,
            f"skipped-clause verdict ids mismatch: "
            f"codex={sorted(sp_ids, key=repr)}, "
            f"strategy={sorted(skipped_clause_ids, key=repr)}",
        )
    return _ok(name)


# =============================================================================
# build_route_input — capture-before-cleanup (Rev 5 spec C18b prerequisite)
# =============================================================================


def _extract_function_sources(
    source: str, asserted_names: set[str]
) -> dict[str, str]:
    """Extract the source text of each top-level test function whose name is in
    `asserted_names`.

    Uses ast to find FunctionDef nodes; uses `node.lineno`/`node.end_lineno` to slice
    the source string. Empty source → empty dict (no functions to extract).
    """
    if not source.strip() or not asserted_names:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    src_lines = source.splitlines(keepends=True)
    out: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in asserted_names:
                # Codex review (2026-06-06): start at the first DECORATOR line if any,
                # not the `def` line. Otherwise parametrize/mark/fixture context is lost
                # from the snippet sent to Codex.
                if node.decorator_list:
                    start = min(d.lineno for d in node.decorator_list) - 1
                else:
                    start = node.lineno - 1  # 0-indexed
                end = node.end_lineno  # 1-indexed last line, used as exclusive slice
                if end is None:
                    end = start + 1
                out[node.name] = "".join(src_lines[start:end])
    return out


def build_route_input(
    contract_clauses: list[dict],
    strategy: dict,
    test_qa_source: str,
    pytest_results: dict,
    coder_test_sources: dict[str, str],
) -> dict:
    """Assemble the route_input dict consumed by `call_codex_route` and C18b.

    This MUST run BEFORE `rmtree(tmp_ws)` in the runner — once tmp_ws is gone, the test
    source is unrecoverable. The returned dict is also written to disk as forensics in
    case Codex's response or subsequent processing fails.

    Shape:
        {
          "contract_clauses": [{"id": "AC-001", "text": "..."}, ...],
          "qa_strategy": {...},
          "test_qa_source": "<full file contents>",
          "asserted_in_test_sources": {"test_x": "def test_x():\n    ...", ...},
          "coder_test_sources": {"test_graph.py::test_x": "def test_x():\n    ...", ...},
          "pytest_results": {"exit": int, "nodeids": [...], "failures": [...]},
        }
    """
    asserted_names = {
        pt.get("asserted_in_test", "")
        for pt in (strategy.get("planned_tests") or [])
        if pt.get("asserted_in_test")
    }
    asserted_sources = _extract_function_sources(test_qa_source, asserted_names)
    return {
        "contract_clauses": list(contract_clauses),
        "qa_strategy": strategy,
        "test_qa_source": test_qa_source,
        "asserted_in_test_sources": asserted_sources,
        "coder_test_sources": dict(coder_test_sources),
        "pytest_results": pytest_results,
    }


# =============================================================================
# call_codex_route — invoke Codex with strict output schema
# =============================================================================


# OpenAI strict-mode JSON-schema rules (Codex `--output-schema` uses strict mode):
#   1. Every property in `properties` MUST appear in `required`.
#   2. `additionalProperties: false` MUST be on every object.
#   3. Conceptually-optional fields are expressed as nullable unions
#      (`["string", "null"]`, `["object", "null"]`, etc.) and STILL listed in
#      `required` — Codex emits `null` when it has no value to fill.
#
# Finding #8 (2026-06-07): the prior schema marked `reason`, `declared_route`,
# `summary_reason`, and `token_usage` as `properties`-without-`required`, so every
# routing call 400'd against OpenAI strict mode. Local jsonschema validation
# (used by tests + `validate_and_compute_route`) is permissive by default; both
# layers now share ONE schema by making the optional fields nullable + required.
# The runtime helpers (`tools/qa_route.py`) handle a null value the same as a
# missing key — `.get(name) or default`.
CODEX_VERDICT_SCHEMA: dict = {
    "type": "object",
    "required": [
        "codex_per_planned_test",
        "codex_per_skipped_clause",
        "declared_route",
        "summary_reason",
        "token_usage",
    ],
    "additionalProperties": False,
    "properties": {
        "codex_per_planned_test": {
            "type": "array",
            "items": {
                "type": "object",
                # Codex review (2026-06-06) HIGH 2: triviality_verdict required.
                # When the test failed, Codex must emit "n/a"; when the test passed,
                # Codex must emit "substantive" or "trivial". Making it optional let
                # a trivial passing test route to `confirm` by simply omitting the field.
                #
                # Finding #8 (2026-06-07): `reason` was missing from `required` —
                # OpenAI strict-mode HARD-rejects that. Now required + nullable.
                "required": [
                    "clause_id",
                    "entailment_verdict",
                    "triviality_verdict",
                    "reason",
                ],
                "additionalProperties": False,
                "properties": {
                    "clause_id": {"type": "string"},
                    # Codex review (finding #8 close-out, 2026-06-07): pair `enum`
                    # with an explicit `type: string`. OpenAI's strict-mode docs
                    # and the sibling `codex_review.py` schema do this — bare-enum
                    # is accepted but not idiomatic; the type adds belt+suspenders
                    # against a regex-style validator interpreting the enum
                    # constraint loosely.
                    "entailment_verdict": {
                        "type": "string",
                        "enum": ["fair", "overreach", "uncertain"],
                    },
                    "triviality_verdict": {
                        "type": "string",
                        "enum": ["substantive", "trivial", "n/a"],
                    },
                    "reason": {"type": ["string", "null"]},
                },
            },
        },
        "codex_per_skipped_clause": {
            "type": "array",
            "items": {
                "type": "object",
                # Finding #8 (2026-06-07): same `reason` strict-mode fix.
                "required": ["clause_id", "audit_verdict", "reason"],
                "additionalProperties": False,
                "properties": {
                    "clause_id": {"type": "string"},
                    "audit_verdict": {
                        "type": "string",
                        "enum": ["skip_earned", "skip_hollow", "uncertain"],
                    },
                    "reason": {"type": ["string", "null"]},
                },
            },
        },
        # Codex may emit a declared_route as a hint, but the runner ignores it for
        # routing (goal must-fix 4 — telemetry only). The schema accepts any string
        # OR null; the orchestrator checks against _VALID_ROUTES in code and treats
        # anything else (incl. null) as "no comparable declared route"
        # (agreement = None) rather than HOLDing the build. Codex emitting garbage
        # in a telemetry field shouldn't break the pipeline.
        "declared_route": {"type": ["string", "null"]},
        "summary_reason": {"type": ["string", "null"]},
        # Telemetry hook for Codex's own prompt/completion token counts. Strict
        # mode requires the whole object body to be present whenever it is non-null,
        # so we declare both sub-fields nullable + required. `validate_and_compute_route`
        # falls back to `{}` via `... or {}` if Codex emits null.
        "token_usage": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["prompt", "completion"],
            "properties": {
                "prompt": {"type": ["integer", "null"]},
                "completion": {"type": ["integer", "null"]},
            },
        },
    },
}


def _build_routing_prompt(route_input: dict) -> str:
    """Construct the prompt sent to Codex for per-item verdict authorship.

    The prompt embeds the route_input as JSON. Codex is instructed to emit per-item
    verdicts ONLY; the runner derives the final route deterministically. Any declared
    route Codex includes is captured for telemetry but never used to gate the build.
    """
    return (
        "You are the independent frontier reviewer (Codex) judging a Gemma-authored QA "
        "test strategy against an ATB contract. Your output must conform to the provided "
        "STRICT JSON schema: per-planned-test verdicts (clause_id, entailment_verdict, "
        "triviality_verdict, reason) and per-skipped-clause verdicts (clause_id, "
        "audit_verdict, reason). Do NOT decide the build's route — the runner computes "
        "that deterministically from your per-item verdicts. If you include a "
        "`declared_route` field it is telemetry only.\n\n"
        "Per-test entailment_verdict:\n"
        "  - `fair`: any compliant implementation would pass this test.\n"
        "  - `overreach`: a compliant implementation could fail this test.\n"
        "  - `uncertain`: you can't decide from the evidence.\n"
        "Per-passing-test triviality_verdict:\n"
        "  - `substantive`: an obviously-buggy implementation would FAIL this test.\n"
        "  - `trivial`: an obviously-buggy implementation would still pass.\n"
        "  - `n/a`: the test was not passing or triviality doesn't apply.\n"
        "Per-skipped-clause audit_verdict:\n"
        "  - `skip_earned`: cited evidence genuinely covers the clause.\n"
        "  - `skip_hollow`: cited evidence does NOT cover the clause.\n"
        "  - `uncertain`: you can't decide.\n\n"
        "Schema compliance notes (strict mode — every field MUST appear in output):\n"
        "  - `reason` (per-item AND top-level `summary_reason`): a 1-2 sentence "
        "explanation, OR `null` when you have no commentary to add.\n"
        "  - `declared_route`: a route hint string (`confirm`/`coder`/`gemma`/"
        "`hold_human`) OR `null` if you don't want to declare one — the runner "
        "ignores it for routing either way.\n"
        "  - `token_usage`: emit `null` if you don't track your own counts.\n\n"
        "Input (contract clauses, strategy, test_qa.py source, coder evidence "
        "snippets, pytest results) follows as JSON:\n\n"
        f"{json.dumps(route_input, indent=2)}"
    )


def call_codex_route(
    route_input: dict,
    effort: str = "high",
    timeout: int = 600,
    model: str | None = None,
) -> dict:
    """Invoke `codex exec` with the routing prompt + strict output schema; return parsed
    Codex verdicts dict. Raises RuntimeError on subprocess failure or missing output.

    Mirrors `tools/codex_review.run_codex` for consistency.
    """
    prompt = _build_routing_prompt(route_input)
    with tempfile.TemporaryDirectory(prefix="qa_route_") as td:
        schema_path = Path(td) / "schema.json"
        out_path = Path(td) / "verdict.json"
        schema_path.write_text(json.dumps(CODEX_VERDICT_SCHEMA))
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            "read-only",
            "--output-schema",
            str(schema_path),
            "-o",
            str(out_path),
            "-c",
            f'model_reasoning_effort="{effort}"',
        ]
        if model:
            cmd += ["-c", f'model="{model}"']
        cmd.append(prompt)
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"codex exit {proc.returncode}: "
                f"{(proc.stderr or proc.stdout)[-500:]}"
            )
        if not out_path.exists():
            raise RuntimeError("codex produced no verdict file")
        return json.loads(out_path.read_text())


# =============================================================================
# validate_and_compute_route — top-level orchestrator
# =============================================================================


def validate_and_compute_route(
    strategy: dict,
    codex_verdicts: dict,
    pytest_exit: int,
    unrepresented_clauses: list[str] | None = None,
) -> dict:
    """Top-level helper: schema-check codex_verdicts → cardinality check → compute_route
    → assemble qa-route.json dict.

    Codex review (2026-06-06): runs CODEX_VERDICT_SCHEMA validation upfront so a caller
    that hands `validate_and_compute_route` a hand-crafted dict (i.e. not through
    `call_codex_route` which already schema-validates via codex exec) still gets the same
    fail-closed guarantees. An invalid-enum verdict that escaped both layers would
    otherwise reach compute_route, which treats unknown verdict strings as not-uncertain,
    not-overreach, not-trivial — silently routing to `confirm`.

    Returns the qa-route.json dict regardless of outcome (a HOLD becomes a route with
    `computed_route: "hold_human"` and the failure reason in `summary_reason`).
    """
    import jsonschema

    unrepresented_clauses = unrepresented_clauses or []

    declared = codex_verdicts.get("declared_route") if isinstance(codex_verdicts, dict) else None
    per_planned = (
        codex_verdicts.get("codex_per_planned_test")
        if isinstance(codex_verdicts, dict)
        else None
    ) or []
    per_skipped = (
        codex_verdicts.get("codex_per_skipped_clause")
        if isinstance(codex_verdicts, dict)
        else None
    ) or []
    # Finding #8 (2026-06-07): summary_reason and token_usage are nullable +
    # required in the strict-mode schema, so the value can be `None` from a
    # legal Codex response. Coalesce to "" / {} for downstream consumers.
    summary = (
        codex_verdicts.get("summary_reason") or ""
        if isinstance(codex_verdicts, dict)
        else ""
    )
    tokens = (
        codex_verdicts.get("token_usage") or {}
        if isinstance(codex_verdicts, dict)
        else {}
    )

    try:
        jsonschema.validate(codex_verdicts, CODEX_VERDICT_SCHEMA)
    except jsonschema.ValidationError as e:
        return {
            "computed_route": "hold_human",
            "codex_declared_route": declared,
            "agreement": None,
            "codex_per_planned_test": per_planned,
            "codex_per_skipped_clause": per_skipped,
            "summary_reason": f"codex verdicts schema-invalid: {e.message}",
            "token_usage": tokens,
        }

    card = validate_verdict_cardinality(strategy, codex_verdicts)
    if not card.ok:
        return {
            "computed_route": "hold_human",
            "codex_declared_route": declared,  # telemetry only (goal must-fix 4)
            "agreement": None,
            "codex_per_planned_test": per_planned,
            "codex_per_skipped_clause": per_skipped,
            "summary_reason": f"verdict cardinality failed: {card.reason}",
            "token_usage": tokens,
        }

    computed = compute_route(
        per_planned, per_skipped, pytest_exit, unrepresented_clauses
    )

    # Telemetry only — never gates. (goal must-fix 4)
    agreement: bool | None = None
    if declared in _VALID_ROUTES:
        agreement = declared == computed

    return {
        "computed_route": computed,
        "codex_declared_route": declared,
        "agreement": agreement,
        "codex_per_planned_test": per_planned,
        "codex_per_skipped_clause": per_skipped,
        "summary_reason": summary,
        "token_usage": tokens,
    }
