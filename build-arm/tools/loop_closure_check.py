#!/usr/bin/env python3
"""Loop-closure ledger check for the overseer meta-layer (Phase 2).

WHY THIS EXISTS (the "no silent drops" half of the loop):
The pipeline DETECTS recurring agent weaknesses already — the coach loop banks coder lessons,
the Improvement Feeder surfaces system recurrences, the diff-gate holds on real bugs. What was
missing is a guarantee that a NOTICED weakness actually resolves to a deliberate, recorded
decision instead of quietly evaporating. This ledger is that guarantee. Phase 1's
decision_record_check binds the OTHER direction (a durable layer change -> a record); this one
binds detection -> decision: every logged recurrence-CLASS must resolve to a recorded decision
(layer-rewrite / no_change / defer), or be visibly still-open. Together they close the loop.

WHAT IT IS NOT (the trust boundary):
This is a deterministic tool, not a judge. It NEVER decides whether a weakness is real, which
layer to rewrite, or whether a decision was good — that stays with Codex + the overseer + Korab
(doer != certifier). It also adds NO new detector: an entry is keyed to existing-detector
artifacts only (schema enum), and the ledger only RECORDS the detection->decision binding.

THE BINDING (to TRUSTED records, never a doer field — the scope-gate law generalized):
A resolved entry cites a decision_record id. The check resolves that id through Phase 1's
load_records — the SAME schema-validated, duplicate-fail-closed loader — so Phase 2 can never
trust a record Phase 1 would reject (no drift between the two gates). The cited record must:
  - exist in the trusted set (a missing OR schema-invalid record => the citation is unbound),
  - be about the SAME target_agent as the class (else it 'closes' an unrelated class), and
  - by its OWN decision field, tell us how the loop closed:
      layer-rewrite / no_change -> genuinely closed (Phase 1 separately binds a layer-rewrite
                                    to its commit; we do not re-check that here),
      defer                     -> closed-for-now UNTIL the record's expiry; an EXPIRED defer is
                                    a HOLD (no silent indefinite deferral).
An open class is always SURFACED (listed) but is never itself a hold — it is legitimate work in
progress. A malformed entry or a duplicate class_id is fail-closed (HOLD), mirroring Phase 1.

OUT OF SCOPE FOR THIS PHASE (named, not silently capped): re-hashing source_refs against the
live detector artifacts, and detecting a detection that never entered the ledger at all, are the
cross-family meta-audit's job (Phase 3) — they require sampling + judgment, not a trust-only
check. An absent/empty ledger is a vacuous clean pass (no classes logged yet).

Usage:
  python -m tools.loop_closure_check [--ledger-dir DIR] [--records-dir DIR] [--today YYYY-MM-DD] [--json]
  python -m tools.loop_closure_check --self-test

Exit: 0 = ledger consistent (every resolved class validly bound; open classes tracked) ·
      2 = >=1 broken binding / expired defer / invalid entry (HOLD) ·
      1 = usage / IO error — FAIL SAFE: never a clean pass on a broken check.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

ATB_ROOT = Path(__file__).resolve().parent.parent
# Bootstrap so `from tools.decision_record_check import ...` resolves regardless of CWD
# (tools/ is an implicit namespace package; reuse keeps record-trust single-sourced).
if str(ATB_ROOT) not in sys.path:
    sys.path.insert(0, str(ATB_ROOT))

from tools.decision_record_check import load_records  # noqa: E402  (after sys.path bootstrap)

DEFAULT_LEDGER_DIR = ATB_ROOT / "overseer" / "ledger"
DEFAULT_RECORDS_DIR = ATB_ROOT / "overseer" / "records"
LEDGER_SCHEMA = ATB_ROOT / "schemas" / "loop-closure-entry.schema.json"

_LEDGER_VALIDATOR: Draft202012Validator | None = None


def _ledger_validator() -> Draft202012Validator:
    """Lazily build the ledger-entry schema validator (repo idiom: Draft202012Validator)."""
    global _LEDGER_VALIDATOR
    if _LEDGER_VALIDATOR is None:
        _LEDGER_VALIDATOR = Draft202012Validator(json.loads(LEDGER_SCHEMA.read_text()))
    return _LEDGER_VALIDATOR


def _parse_date(s) -> date | None:
    """Parse a YYYY-MM-DD date; return None on anything unparseable (caller fails safe).

    The schema declares expiry as format:date, but jsonschema does not enforce `format` by
    default, so a malformed expiry can pass schema validation. Parsing defensively here turns
    an unparseable expiry into a HOLD rather than a silently-ignored deferral.
    """
    if not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def reconcile_ledger(entries: dict, records: dict, today: date) -> dict:
    """Pure core: reconcile loop-closure entries against TRUSTED decision-records.

    Args:
      entries: dict class_id -> entry (already schema-valid via load_ledger).
      records: dict odr-id -> record (the TRUSTED set from load_records — schema-valid).
      today:   datetime.date, injected so the core has no clock (deterministic tests).

    Returns {ok, open, deferred, stale_defers, broken, closed, checked_entries, checked_records}:
      open         : detected, no decision yet (SURFACED, not a hold).
      deferred     : resolved-by-defer, expiry still in the future (tracked, not a hold).
      stale_defers : resolved-by-defer past expiry (HOLD — no silent indefinite deferral).
      broken       : resolved entry whose citation does not bind to a trusted record (HOLD).
      closed       : resolved by layer-rewrite / no_change (loop genuinely closed).
    ok = no broken and no stale_defers. Verdict is unforgeable given trusted inputs.
    """
    open_classes: list[dict] = []
    deferred: list[dict] = []
    stale_defers: list[dict] = []
    broken: list[dict] = []
    closed: list[dict] = []

    for cid, e in entries.items():
        if e.get("status") == "open":
            open_classes.append({"class_id": cid, "summary": e.get("summary", "")})
            continue

        # status == resolved (schema guarantees decision_record is present).
        dr = e.get("decision_record")
        rec = records.get(dr)
        if rec is None:
            broken.append({"class_id": cid, "reason": f"resolution cites '{dr}', not a valid trusted decision-record"})
            continue
        if rec.get("target_agent") != e.get("target_agent"):
            broken.append({"class_id": cid, "reason": f"cited record '{dr}' is about agent '{rec.get('target_agent')}', not this class's '{e.get('target_agent')}'"})
            continue

        decision = rec.get("decision")
        if decision in ("layer-rewrite", "no_change"):
            closed.append({"class_id": cid, "decision_record": dr, "decision": decision})
        elif decision == "defer":
            expiry_raw = rec.get("expiry")
            exp = _parse_date(expiry_raw)
            if exp is None:
                broken.append({"class_id": cid, "reason": f"cited defer record '{dr}' has an unparseable expiry '{expiry_raw}'"})
            elif exp < today:
                stale_defers.append({"class_id": cid, "reason": f"cited defer record '{dr}' expired {expiry_raw} (< {today.isoformat()}) — must be revisited"})
            else:
                deferred.append({"class_id": cid, "decision_record": dr, "expiry": expiry_raw})
        else:
            # Unexpected decision value — fail safe rather than silently treat as closed.
            broken.append({"class_id": cid, "reason": f"cited record '{dr}' has unexpected decision '{decision}'"})

    ok = not broken and not stale_defers
    return {
        "ok": ok,
        "open": open_classes,
        "deferred": deferred,
        "stale_defers": stale_defers,
        "broken": broken,
        "closed": closed,
        "checked_entries": len(entries),
        "checked_records": len(records),
    }


# --------------------------------------------------------------------------- IO layer


def load_ledger(ledger_dir: Path):
    """Load *.json ledger entries. Returns (entries: dict class_id->entry, invalid: list).

    Every entry is validated against schemas/loop-closure-entry.schema.json so the conditional
    invariants (resolved must cite a record; open must not) bite. Failures are surfaced as
    invalid, NEVER silently skipped — a needed binding could hide behind a malformed file, so an
    invalid entry forces a HOLD (run_check folds `invalid` into ok=False).

    A DUPLICATE class_id is fail-closed: both files are marked invalid AND the id is removed from
    the trusted set, so the class is treated as unresolved rather than silently last-write-wins.
    """
    entries: dict[str, dict] = {}
    first_file: dict[str, str] = {}
    invalid: list[dict] = []
    if not ledger_dir.is_dir():
        return entries, invalid
    validator = _ledger_validator()
    for p in sorted(ledger_dir.glob("*.json")):
        try:
            e = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            invalid.append({"file": p.name, "reason": f"unreadable/invalid json: {exc}"})
            continue
        errs = sorted(validator.iter_errors(e), key=lambda er: list(er.path))
        if errs:
            er = errs[0]
            invalid.append({"file": p.name, "reason": f"schema-invalid: {er.message} at {list(er.path)}"})
            continue
        cid = e["class_id"]  # schema guarantees a non-empty, well-formed class_id
        if cid in entries or cid in first_file:
            invalid.append({"file": p.name, "reason": f"duplicate class_id '{cid}' (also in {first_file.get(cid, '?')})"})
            entries.pop(cid, None)  # neither copy trusted -> fail closed (class stays unresolved)
            continue
        entries[cid] = e
        first_file[cid] = p.name
    return entries, invalid


def run_check(ledger_dir: Path, records_dir: Path, today: date) -> dict:
    """Load ledger + TRUSTED records, reconcile. Raises on indeterminate IO (CLI -> fail-safe HOLD)."""
    entries, ledger_invalid = load_ledger(ledger_dir)
    records, record_invalid = load_records(records_dir)
    result = reconcile_ledger(entries, records, today)
    result["invalid_entries"] = ledger_invalid
    # Diagnostic only: a record that is itself invalid is simply absent from the trusted set, so
    # any entry citing it is already caught as `broken`. We surface record_invalid so an operator
    # can SEE why a citation failed, but we do NOT fold it into ok — an invalid record that no
    # entry cites is Phase 1's concern (decision_record_check), not this gate's.
    result["invalid_records"] = record_invalid
    result["ok"] = result["ok"] and not ledger_invalid
    result["today"] = today.isoformat()
    return result


def _self_test() -> int:
    """Pure-core checks: reconcile verdicts against trusted records, no IO needed."""
    failures: list[str] = []
    today = date(2026, 6, 20)

    def expect(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got}, want {want}")

    records = {
        "odr-rw": {"id": "odr-rw", "target_agent": "coder", "decision": "layer-rewrite", "status": "certified", "commit_hash": "abc123def456"},
        "odr-nc": {"id": "odr-nc", "target_agent": "coder", "decision": "no_change", "status": "certified"},
        "odr-def-future": {"id": "odr-def-future", "target_agent": "coder", "decision": "defer", "status": "proposed", "expiry": "2026-12-31"},
        "odr-def-past": {"id": "odr-def-past", "target_agent": "coder", "decision": "defer", "status": "proposed", "expiry": "2026-01-01"},
        "odr-def-bad": {"id": "odr-def-bad", "target_agent": "coder", "decision": "defer", "status": "proposed", "expiry": "not-a-date"},
        "odr-otheragent": {"id": "odr-otheragent", "target_agent": "qa", "decision": "no_change", "status": "certified"},
    }

    def entry(cid, status, dr=None, target="coder"):
        e = {"class_id": cid, "target_agent": target, "status": status, "summary": "x"}
        if dr is not None:
            e["decision_record"] = dr
        return e

    # Open class -> surfaced, not a hold.
    r = reconcile_ledger({"lcc-a": entry("lcc-a", "open")}, records, today)
    expect("open-surfaced", (r["ok"], len(r["open"])), (True, 1))

    # Resolved by layer-rewrite / no_change -> closed, ok.
    r = reconcile_ledger({"lcc-a": entry("lcc-a", "resolved", "odr-rw")}, records, today)
    expect("rewrite-closed", (r["ok"], len(r["closed"])), (True, 1))
    r = reconcile_ledger({"lcc-a": entry("lcc-a", "resolved", "odr-nc")}, records, today)
    expect("no_change-closed", (r["ok"], len(r["closed"])), (True, 1))

    # Resolved citing a missing record -> broken, HOLD.
    r = reconcile_ledger({"lcc-a": entry("lcc-a", "resolved", "odr-ghost")}, records, today)
    expect("missing-record-broken", (r["ok"], len(r["broken"])), (False, 1))

    # Resolved citing a record about a DIFFERENT agent -> broken, HOLD.
    r = reconcile_ledger({"lcc-a": entry("lcc-a", "resolved", "odr-otheragent")}, records, today)
    expect("agent-mismatch-broken", (r["ok"], len(r["broken"])), (False, 1))

    # Defer not yet expired -> deferred, ok. Past expiry -> stale, HOLD. Bad expiry -> broken, HOLD.
    r = reconcile_ledger({"lcc-a": entry("lcc-a", "resolved", "odr-def-future")}, records, today)
    expect("defer-future-ok", (r["ok"], len(r["deferred"])), (True, 1))
    r = reconcile_ledger({"lcc-a": entry("lcc-a", "resolved", "odr-def-past")}, records, today)
    expect("defer-past-stale", (r["ok"], len(r["stale_defers"])), (False, 1))
    r = reconcile_ledger({"lcc-a": entry("lcc-a", "resolved", "odr-def-bad")}, records, today)
    expect("defer-bad-expiry-broken", (r["ok"], len(r["broken"])), (False, 1))

    if failures:
        print("loop_closure_check SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("loop_closure_check self-test: OK (reconcile verdicts vs trusted records)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="loop_closure_check",
        description="Deterministic loop-closure check: every detected recurrence-class resolves to a recorded decision (no silent drops).",
    )
    ap.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR), help="dir of loop-closure ledger json entries")
    ap.add_argument("--records-dir", default=str(DEFAULT_RECORDS_DIR), help="dir of overseer-decision-record json files (trusted set)")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD) for deterministic checks; defaults to UTC today")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    if args.today:
        today = _parse_date(args.today)
        if today is None:
            print(f"error: --today must be YYYY-MM-DD, got '{args.today}'", file=sys.stderr)
            return 1
    else:
        today = datetime.now(timezone.utc).date()

    try:
        result = run_check(Path(args.ledger_dir), Path(args.records_dir), today)
    except (OSError, RuntimeError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: check could not run — failing safe (held): {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(
            f"[loop-closure] OK — {result['checked_entries']} class(es): "
            f"{len(result['closed'])} closed, {len(result['deferred'])} deferred, {len(result['open'])} open (tracked)"
        )
        for o in result["open"]:
            print(f"  open {o['class_id']}: {o['summary']}")
    else:
        print(f"[loop-closure] HELD — {result['checked_entries']} class(es) as of {result['today']}:")
        for b in result["broken"]:
            print(f"  broken {b['class_id']}: {b['reason']}")
        for s in result["stale_defers"]:
            print(f"  stale-defer {s['class_id']}: {s['reason']}")
        for iv in result["invalid_entries"]:
            print(f"  invalid entry {iv['file']}: {iv['reason']}")

    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
