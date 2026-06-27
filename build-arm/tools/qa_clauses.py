#!/usr/bin/env python3
"""Parse acceptance_checks from product-plan.json into atomic clauses with stable IDs.

WHY THIS EXISTS (the QA layer, 2026-06-06):
The QA layer's completeness invariant requires that Gemma's qa-strategy.json reference EVERY
contract clause exactly once (either as a planned_test or a skipped_clause). To compute the
set difference `all_clauses − (planned_ids ∪ skipped_ids)`, the runner needs a deterministic
way to derive the set of clause IDs from the contract — without trusting any model output.

This module is the source of truth for that set. It reads `product-plan.json`'s
`acceptance_checks` list and returns one atomic clause record per entry. Pure function over
the JSON file's bytes; no I/O beyond the read.

CLAUSE IDs — two sources, in order:

1. EXPLICIT PREFIX (preferred): if an acceptance_check string begins with `[AC-<token>]`,
   that token is the clause ID. `<token>` is digits with an optional lowercase suffix
   (e.g. `AC-001`, `AC-004a`, `AC-032e`). Using explicit IDs lets contract authors pin
   semantic identifiers that survive sub-clause splits without renumbering the whole list.

2. SYNTHESIZED FALLBACK: if NO entries are prefixed, IDs are synthesized in list order as
   `AC-001`, `AC-002`, ..., `AC-N` (the v1 spec literal form). This stays backwards-
   compatible with older contracts that did not adopt the `[AC-XXX]` convention.

Mixing prefixed and unprefixed entries in the same list is rejected — pick one form per
contract. Duplicate IDs (when prefixed) are also rejected.

Returns: `list[{id: str, text: str}]` where `text` is the clause body (the prefix is
stripped on extraction so downstream consumers see a clean clause string).

Errors raised as ValueError (FileNotFoundError if the file is missing — that's an OS error,
not a contract error):
  - product-plan.json present but not valid JSON
  - product-plan.json root is not a JSON object (list/scalar at top level)
  - `acceptance_checks` key missing or not a list
  - acceptance_check entry is not a string
  - MALFORMED AC-prefix attempts (e.g. `[AC-001A]` uppercase suffix, ` [AC-001]` leading
    whitespace, `[ac-001]` wrong case, `[AC- 001]` internal whitespace, `[AC-001` no
    closing bracket) — close misses are surfaced rather than silently downgraded to "no
    prefix" so the ID set always reflects the author's intent.
  - mixed prefix / no-prefix in the same list
  - duplicate clause IDs (when prefixed)

Bytes that cannot be decoded as UTF-8 surface as `UnicodeDecodeError`; filesystem errors
beyond "not found" surface as the underlying `OSError`. Both fail closed (no parse, no
silent empty list) so the caller's completeness invariant cannot be tricked by an
unreadable contract.

The QA runner's C18a check (completeness invariant) compares the IDs from this function to
the IDs cited in Gemma's strategy; any clause not represented is HOLD.
"""

import json
import re
from pathlib import Path

# Strict form: `[AC-<digits>(<lowercase-letters>)?]` at column 0, then optional inline
# horizontal whitespace (space/tab — not newlines) before the clause body.
# Examples that match: [AC-001], [AC-004a], [AC-032e], [AC-1] (one or more digits, any count).
PREFIX_RE = re.compile(r"^\[(AC-[0-9]+[a-z]*)\][ \t]*")

# "Looks like the author tried to write an AC prefix, but malformed it." Catches the close
# misses Codex listed (`[AC-001A]` uppercase suffix, ` [AC-001]` leading space, `[ac-001]`
# wrong case, `[AC- 001]` internal space, `[AC-001` no closing bracket). If something matches
# this loose form but NOT the strict PREFIX_RE, raise — silently falling through to the
# synthesized fallback would produce a different ID set than the author intended.
# This deliberately does NOT match `[FOO-001]` or unrelated bracketed prose.
LOOKS_LIKE_AC_RE = re.compile(r"^\s*\[\s*ac[\s\-]*[0-9]", re.IGNORECASE)


def _extract_id(text: str) -> tuple[str | None, str]:
    """Return (clause_id, body_without_prefix) for one acceptance_check string.

    Pure primitive: matches the STRICT PREFIX_RE only. If `text` does not start with the
    strict form, returns (None, text) unchanged — including for malformed-but-AC-shaped
    inputs. Caller (`parse_contract_clauses`) is responsible for distinguishing genuine
    "no prefix" from "malformed prefix attempt" and rejecting the latter.
    """
    m = PREFIX_RE.match(text)
    if m:
        return m.group(1), text[m.end() :]
    return None, text


def parse_contract_clauses(product_plan_path: Path | str) -> list[dict[str, str]]:
    """Parse `product-plan.json:acceptance_checks` into `[{id, text}, ...]`.

    See module docstring for ID resolution rules and error conditions.
    """
    p = Path(product_plan_path)
    raw = p.read_text()  # FileNotFoundError propagates — that's an OS error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"product-plan.json is not valid JSON: {e}") from e

    # Top-level must be a JSON object (the schema's contract). Lists/scalars at the root
    # would crash with AttributeError on data.get(...) — normalize to ValueError.
    if not isinstance(data, dict):
        raise ValueError(
            f"product-plan.json root must be a JSON object, got "
            f"{type(data).__name__}"
        )

    checks = data.get("acceptance_checks")
    if not isinstance(checks, list):
        raise ValueError(
            "product-plan.json missing or non-list 'acceptance_checks'"
        )

    # Each entry must be a string.
    for i, entry in enumerate(checks):
        if not isinstance(entry, str):
            raise ValueError(
                f"acceptance_checks[{i}] is not a string: {type(entry).__name__}"
            )

    # Reject malformed AC-like prefixes: the strict PREFIX_RE missed them, but the looser
    # LOOKS_LIKE_AC_RE catches them — indicating the author intended an AC prefix and
    # mis-typed it. Falling through to the synthesized fallback in that case would produce
    # a SILENTLY-DIFFERENT ID set than the author intended, which is a trust-spine hole.
    for i, entry in enumerate(checks):
        if PREFIX_RE.match(entry):
            continue
        if LOOKS_LIKE_AC_RE.match(entry):
            raise ValueError(
                f"acceptance_checks[{i}] has a malformed AC-prefix attempt "
                f"({entry[:60]!r}); required form is '[AC-NNN]' or '[AC-NNNx]' "
                f"at column 0 (no leading whitespace, no internal whitespace, "
                f"uppercase 'AC', lowercase suffix only)"
            )

    extracted = [_extract_id(c) for c in checks]
    n_prefixed = sum(1 for cid, _ in extracted if cid is not None)

    if 0 < n_prefixed < len(checks):
        raise ValueError(
            f"acceptance_checks mixes prefixed and unprefixed entries "
            f"({n_prefixed}/{len(checks)} prefixed); pick one form per contract"
        )

    if n_prefixed == 0:
        # Synthesized fallback: AC-001..AC-N from list index.
        return [
            {"id": f"AC-{i + 1:03d}", "text": c} for i, c in enumerate(checks)
        ]

    # All prefixed: use explicit IDs; enforce uniqueness.
    clauses: list[dict[str, str]] = []
    seen: set[str] = set()
    for cid, body in extracted:
        assert cid is not None  # already checked n_prefixed == len(checks)
        if cid in seen:
            raise ValueError(f"duplicate clause ID {cid}")
        seen.add(cid)
        clauses.append({"id": cid, "text": body})
    return clauses
