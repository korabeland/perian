"""Tests for tools.qa_clauses — parse acceptance_checks into atomic clauses.

Strategy: the function is small and pure (one file read, one JSON parse, one regex per
entry). Tests cover the prefix-extraction primitive (`_extract_id`), the two ID-resolution
paths (all-prefixed and synthesized fallback), every error condition, plus a smoke test
against the real dep-planner contract (a substantial set of atomic clauses with semantic
IDs like AC-004a and AC-032e).
"""

import json
from pathlib import Path

import pytest

from tools.qa_clauses import _extract_id, parse_contract_clauses


# --- _extract_id primitive ---

def test_extract_id_basic():
    cid, body = _extract_id("[AC-001] graph.py defines build_graph().")
    assert cid == "AC-001"
    assert body == "graph.py defines build_graph()."


def test_extract_id_with_lowercase_suffix():
    cid, body = _extract_id("[AC-032e] main writes EXACTLY one line to stderr.")
    assert cid == "AC-032e"
    assert body == "main writes EXACTLY one line to stderr."


def test_extract_id_short_digits():
    cid, body = _extract_id("[AC-1] tiny.")
    assert cid == "AC-1"
    assert body == "tiny."


def test_extract_id_no_prefix():
    cid, body = _extract_id("just a plain clause")
    assert cid is None
    assert body == "just a plain clause"


def test_extract_id_wrong_kind_prefix_left_alone():
    """Only [AC-...] is recognized — other bracketed prefixes pass through."""
    cid, body = _extract_id("[FOO-001] something")
    assert cid is None
    assert body == "[FOO-001] something"


def test_extract_id_internal_spaces_rejected():
    """The prefix is strict: no whitespace inside brackets."""
    cid, body = _extract_id("[ AC-001 ] something")
    assert cid is None
    assert body == "[ AC-001 ] something"


def test_extract_id_lowercase_ac_rejected():
    cid, body = _extract_id("[ac-001] something")
    assert cid is None
    assert body == "[ac-001] something"


def test_extract_id_strips_one_separator_only():
    """Multiple spaces after ']' are normalized to one separator boundary."""
    cid, body = _extract_id("[AC-001]    multiple spaces")
    assert cid == "AC-001"
    assert body == "multiple spaces"


# --- parse_contract_clauses: all-prefixed path ---

def _write_pp(tmp_path: Path, checks: list) -> Path:
    pp = tmp_path / "product-plan.json"
    pp.write_text(json.dumps({"acceptance_checks": checks}))
    return pp


def test_parse_all_prefixed_round_trips(tmp_path):
    pp = _write_pp(
        tmp_path,
        [
            "[AC-001] first clause",
            "[AC-002] second clause",
            "[AC-003a] third (sub-clause)",
        ],
    )
    clauses = parse_contract_clauses(pp)
    assert clauses == [
        {"id": "AC-001", "text": "first clause"},
        {"id": "AC-002", "text": "second clause"},
        {"id": "AC-003a", "text": "third (sub-clause)"},
    ]


def test_parse_preserves_input_order(tmp_path):
    """Order matters — clause completeness check matches by ID in list order."""
    pp = _write_pp(
        tmp_path,
        ["[AC-099] z", "[AC-001] a", "[AC-050] m"],
    )
    ids = [c["id"] for c in parse_contract_clauses(pp)]
    assert ids == ["AC-099", "AC-001", "AC-050"]


# --- parse_contract_clauses: synthesized fallback path ---

def test_parse_none_prefixed_synthesizes_sequentially(tmp_path):
    pp = _write_pp(tmp_path, ["first", "second", "third"])
    clauses = parse_contract_clauses(pp)
    assert clauses == [
        {"id": "AC-001", "text": "first"},
        {"id": "AC-002", "text": "second"},
        {"id": "AC-003", "text": "third"},
    ]


def test_parse_synthesized_three_digit_padding(tmp_path):
    pp = _write_pp(tmp_path, [f"clause-{i}" for i in range(12)])
    clauses = parse_contract_clauses(pp)
    assert clauses[0]["id"] == "AC-001"
    assert clauses[9]["id"] == "AC-010"
    assert clauses[11]["id"] == "AC-012"


# --- parse_contract_clauses: edge cases ---

def test_parse_empty_list(tmp_path):
    pp = _write_pp(tmp_path, [])
    assert parse_contract_clauses(pp) == []


# --- parse_contract_clauses: error conditions ---

def test_parse_mixed_prefix_and_unprefixed_rejected(tmp_path):
    pp = _write_pp(
        tmp_path,
        ["[AC-001] yes", "no prefix here"],
    )
    with pytest.raises(ValueError, match="mixes prefixed"):
        parse_contract_clauses(pp)


def test_parse_duplicate_explicit_ids_rejected(tmp_path):
    pp = _write_pp(
        tmp_path,
        ["[AC-001] a", "[AC-002] b", "[AC-001] c"],
    )
    with pytest.raises(ValueError, match="duplicate clause ID AC-001"):
        parse_contract_clauses(pp)


def test_parse_missing_file_raises_file_not_found(tmp_path):
    """OS errors stay OS errors (FileNotFoundError, not wrapped to ValueError)."""
    with pytest.raises(FileNotFoundError):
        parse_contract_clauses(tmp_path / "nonexistent.json")


def test_parse_invalid_json_raises_value_error(tmp_path):
    pp = tmp_path / "product-plan.json"
    pp.write_text("not json {")
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_contract_clauses(pp)


def test_parse_missing_acceptance_checks_key_raises(tmp_path):
    pp = tmp_path / "product-plan.json"
    pp.write_text(json.dumps({"other_key": []}))
    with pytest.raises(ValueError, match="missing or non-list"):
        parse_contract_clauses(pp)


def test_parse_acceptance_checks_not_list_raises(tmp_path):
    pp = tmp_path / "product-plan.json"
    pp.write_text(json.dumps({"acceptance_checks": "not a list"}))
    with pytest.raises(ValueError, match="missing or non-list"):
        parse_contract_clauses(pp)


def test_parse_acceptance_checks_entry_not_string_raises(tmp_path):
    pp = _write_pp(tmp_path, ["ok", 42, "also ok"])
    with pytest.raises(ValueError, match=r"acceptance_checks\[1\] is not a string"):
        parse_contract_clauses(pp)


# --- new error paths from Codex review ---

@pytest.mark.parametrize(
    "root",
    [
        "[]",  # list at root
        "42",  # int at root
        '"a string"',  # string at root
        "true",  # bool at root
        "null",  # null at root
    ],
)
def test_parse_non_object_root_raises(tmp_path, root):
    pp = tmp_path / "product-plan.json"
    pp.write_text(root)
    with pytest.raises(ValueError, match="root must be a JSON object"):
        parse_contract_clauses(pp)


@pytest.mark.parametrize(
    "malformed",
    [
        "[AC-001A] uppercase suffix",  # uppercase letter after digits
        " [AC-001] leading whitespace",  # leading space before bracket
        "[ac-001] lowercase ac",  # wrong case
        "[Ac-001] mixed case",  # mixed case
        "[AC- 001] internal space",  # space between AC- and digits
        "[AC-001 something",  # missing closing bracket
        "[ AC-001] space inside bracket",  # space inside open bracket
    ],
)
def test_parse_malformed_ac_prefix_rejected(tmp_path, malformed):
    """The author tried to write an AC prefix and missed — surface the mistake."""
    pp = _write_pp(tmp_path, [malformed])
    with pytest.raises(ValueError, match="malformed AC-prefix attempt"):
        parse_contract_clauses(pp)


def test_parse_unrelated_bracket_prefix_not_flagged_as_malformed(tmp_path):
    """`[FOO-001]` is prose, not a malformed AC attempt — no error, treated as plain text."""
    pp = _write_pp(tmp_path, ["[FOO-001] this is just prose"])
    clauses = parse_contract_clauses(pp)
    assert clauses == [{"id": "AC-001", "text": "[FOO-001] this is just prose"}]


def test_parse_all_malformed_does_not_silently_synthesize(tmp_path):
    """A list of all-malformed AC attempts must raise, NOT fall through to AC-001..N."""
    pp = _write_pp(tmp_path, ["[AC-001A] x", "[ac-002] y", "[AC- 003] z"])
    with pytest.raises(ValueError, match="malformed AC-prefix attempt"):
        parse_contract_clauses(pp)


def test_parse_accepts_str_path(tmp_path):
    """Both Path and str inputs work — runner may have either."""
    pp = _write_pp(tmp_path, ["[AC-001] x"])
    clauses = parse_contract_clauses(str(pp))
    assert clauses == [{"id": "AC-001", "text": "x"}]


# --- Smoke test against the real dep-planner contract ---

def test_parse_real_dep_planner_contract():
    """The real dep-planner contract parses into many atomic clauses with semantic IDs.

    This is a smoke test against a live, hardenable fixture: the contract gains
    atomic clauses as the contract gate splits compound asserts, so we assert a
    lower bound + the presence of specific semantic IDs rather than an exact total
    (which would re-break on every legitimate hardening).
    """
    pp = Path("state/tracks/20260605-dep-planner/product-plan.json")
    if not pp.exists():
        pytest.skip("dep-planner track not present at expected path")
    clauses = parse_contract_clauses(pp)
    assert len(clauses) >= 40
    ids = [c["id"] for c in clauses]
    # Uniqueness invariant
    assert len(set(ids)) == len(ids)
    # Spot-check known IDs survive the split
    assert "AC-001" in ids
    assert "AC-004a" in ids
    assert "AC-004d" in ids
    assert "AC-029a" in ids
    assert "AC-029c" in ids
    assert "AC-032a" in ids
    assert "AC-032e" in ids
    assert "AC-037" in ids
    # Bodies are stripped of their prefixes
    ac001 = next(c for c in clauses if c["id"] == "AC-001")
    assert ac001["text"].startswith("graph.py defines build_graph")
