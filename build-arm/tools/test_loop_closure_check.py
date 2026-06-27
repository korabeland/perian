"""Tests for tools/loop_closure_check.py — the loop-closure ledger ("no silent drops") gate.

The pure reconcile core is covered by --self-test, re-run here. The integration tests stand up
a REAL temporary ledger dir + decision-records dir and prove the binding both ways: a resolved
class with a valid, agent-matching, trusted record passes; a citation to a missing / invalid /
wrong-agent record is HELD; an expired defer is HELD; an open class is surfaced but never a hold;
malformed/duplicate entries fail closed. Records are loaded through Phase 1's load_records, so a
schema-invalid record is untrusted to BOTH gates (the no-drift property).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from tools import loop_closure_check as lcc

TODAY = date(2026, 6, 20)


# --------------------------------------------------------------------------- helpers


def _ledger_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ledger"
    d.mkdir()
    return d


def _records_dir(tmp_path: Path) -> Path:
    d = tmp_path / "records"
    d.mkdir()
    return d


def _valid_record(rid: str, decision="layer-rewrite", status="certified",
                  commit_hash=None, layer="identity", expiry=None, target="coder") -> dict:
    """A fully schema-valid overseer-decision-record (load_records validates against the schema)."""
    rec = {
        "id": rid,
        "created_ts": "2026-06-20T12:00:00Z",
        "target_agent": target,
        "grounding_pack_id": "gp-0123456789abcdef",
        "memory_refs": ["project_atb_operating_thesis"],
        "log_refs": [],
        "detector_source_refs": [],
        "layer_considered": layer,
        "decision": decision,
        "certifier": "codex+korab",
        "status": status,
        "summary": "test record",
    }
    if commit_hash is not None:
        rec["commit_hash"] = commit_hash
    if expiry is not None:
        rec["expiry"] = expiry
    return rec


def _write_record(records_dir: Path, rec: dict) -> None:
    (records_dir / f"{rec['id']}.json").write_text(json.dumps(rec))


def _entry(cid: str, status="open", decision_record=None, detector="coach",
           target="coder", summary="recurring weakness X") -> dict:
    e = {
        "class_id": cid,
        "created_ts": "2026-06-20T12:00:00Z",
        "target_agent": target,
        "detector": detector,
        "summary": summary,
        "source_refs": [{"source_id": "coach:job1:r2", "hash": "abc123"}],
        "status": status,
    }
    if decision_record is not None:
        e["decision_record"] = decision_record
    return e


def _write_entry(ledger_dir: Path, e: dict) -> None:
    (ledger_dir / f"{e['class_id']}.json").write_text(json.dumps(e))


# --------------------------------------------------------------------------- pure core


def test_self_test_passes():
    assert lcc._self_test() == 0


def test_empty_ledger_is_clean(tmp_path):
    res = lcc.run_check(_ledger_dir(tmp_path), _records_dir(tmp_path), TODAY)
    assert res["ok"] is True
    assert res["checked_entries"] == 0


def test_absent_ledger_dir_is_clean(tmp_path):
    res = lcc.run_check(tmp_path / "nope", _records_dir(tmp_path), TODAY)
    assert res["ok"] is True
    assert res["checked_entries"] == 0


# --------------------------------------------------------------------------- the binding (pass)


def test_open_class_surfaced_not_held(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_entry(ledger, _entry("lcc-open-thing", status="open"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is True
    assert [o["class_id"] for o in res["open"]] == ["lcc-open-thing"]


def test_resolved_by_layer_rewrite_passes(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_record(records, _valid_record("odr-rw", "layer-rewrite", "certified", commit_hash="a" * 12))
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-rw"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is True
    assert [c["class_id"] for c in res["closed"]] == ["lcc-x"]


def test_resolved_by_no_change_passes(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_record(records, _valid_record("odr-nc", "no_change", "certified"))
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-nc"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is True
    assert [c["class_id"] for c in res["closed"]] == ["lcc-x"]


def test_defer_not_yet_expired_is_tracked_not_held(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_record(records, _valid_record("odr-def", "defer", "proposed", layer="none", expiry="2026-12-31"))
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-def"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is True
    assert [d["class_id"] for d in res["deferred"]] == ["lcc-x"]


# --------------------------------------------------------------------------- the binding (HOLD)


def test_resolved_citing_missing_record_held(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-ghost"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert res["broken"] and res["broken"][0]["class_id"] == "lcc-x"


def test_resolved_citing_wrong_agent_record_held(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_record(records, _valid_record("odr-qa", "no_change", "certified", target="qa"))
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-qa", target="coder"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert "not this class's" in res["broken"][0]["reason"]


def test_expired_defer_held(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_record(records, _valid_record("odr-def", "defer", "proposed", layer="none", expiry="2026-01-01"))
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-def"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert res["stale_defers"] and res["stale_defers"][0]["class_id"] == "lcc-x"


def test_defer_with_malformed_expiry_held(tmp_path):
    """jsonschema does not enforce format:date, so a bad expiry loads — reconcile must fail safe."""
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_record(records, _valid_record("odr-def", "defer", "proposed", layer="none", expiry="someday"))
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-def"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert "unparseable expiry" in res["broken"][0]["reason"]


def test_citation_to_schema_invalid_record_is_untrusted_and_held(tmp_path):
    """No-drift property: a record Phase 1 would reject is absent from the trusted set, so a
    Phase 2 citation to it is unbound -> HELD (Phase 2 cannot trust what Phase 1 rejects)."""
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    bad = _valid_record("odr-bad", "layer-rewrite", "certified", commit_hash="a" * 12)
    del bad["grounding_pack_id"]  # violates the decision-record schema (required)
    _write_record(records, bad)
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-bad"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert res["broken"] and res["broken"][0]["class_id"] == "lcc-x"


# --------------------------------------------------------------------------- load_ledger fail-closed


def test_schema_invalid_entry_held(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    bad = _entry("lcc-x", status="resolved")  # resolved but no decision_record -> schema-invalid
    _write_entry(ledger, bad)
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert res["invalid_entries"]


def test_open_entry_with_decision_record_held(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    bad = _entry("lcc-x", status="open", decision_record="odr-rw")  # open must not cite
    _write_entry(ledger, bad)
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert res["invalid_entries"]


def test_duplicate_class_id_fails_closed(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_record(records, _valid_record("odr-rw", "layer-rewrite", "certified", commit_hash="a" * 12))
    # Two files, same class_id, one would otherwise pass — both must become untrusted.
    (ledger / "a.json").write_text(json.dumps(_entry("lcc-dup", status="resolved", decision_record="odr-rw")))
    (ledger / "b.json").write_text(json.dumps(_entry("lcc-dup", status="open")))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert any("duplicate class_id" in iv["reason"] for iv in res["invalid_entries"])
    assert res["checked_entries"] == 0  # neither copy trusted


def test_unreadable_json_entry_held(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    (ledger / "broken.json").write_text("{not json")
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is False
    assert res["invalid_entries"]


def test_open_and_resolved_mix(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_record(records, _valid_record("odr-rw", "layer-rewrite", "certified", commit_hash="a" * 12))
    _write_entry(ledger, _entry("lcc-open", status="open"))
    _write_entry(ledger, _entry("lcc-done", status="resolved", decision_record="odr-rw"))
    res = lcc.run_check(ledger, records, TODAY)
    assert res["ok"] is True
    assert len(res["open"]) == 1 and len(res["closed"]) == 1


# --------------------------------------------------------------------------- CLI exit codes


def test_main_clean_returns_0(tmp_path, capsys):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_entry(ledger, _entry("lcc-open", status="open"))
    rc = lcc.main(["--ledger-dir", str(ledger), "--records-dir", str(records), "--today", "2026-06-20"])
    assert rc == 0


def test_main_hold_returns_2(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_entry(ledger, _entry("lcc-x", status="resolved", decision_record="odr-ghost"))
    rc = lcc.main(["--ledger-dir", str(ledger), "--records-dir", str(records), "--today", "2026-06-20"])
    assert rc == 2


def test_main_bad_today_returns_1(tmp_path):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    rc = lcc.main(["--ledger-dir", str(ledger), "--records-dir", str(records), "--today", "nope"])
    assert rc == 1


def test_main_self_test_returns_0():
    assert lcc.main(["--self-test"]) == 0


def test_main_json_output(tmp_path, capsys):
    ledger, records = _ledger_dir(tmp_path), _records_dir(tmp_path)
    _write_entry(ledger, _entry("lcc-open", status="open"))
    lcc.main(["--ledger-dir", str(ledger), "--records-dir", str(records), "--today", "2026-06-20", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["today"] == "2026-06-20"


# --------------------------------------------------------------------------- schema conditionals


class TestSchemaConditionals:
    def _validator(self):
        return lcc._ledger_validator()

    def _valid(self, rec):
        return not list(self._validator().iter_errors(rec))

    def test_good_open_validates(self):
        assert self._valid(_entry("lcc-x", status="open"))

    def test_good_resolved_validates(self):
        assert self._valid(_entry("lcc-x", status="resolved", decision_record="odr-rw"))

    def test_resolved_without_record_rejected(self):
        assert not self._valid(_entry("lcc-x", status="resolved"))

    def test_open_with_record_rejected(self):
        assert not self._valid(_entry("lcc-x", status="open", decision_record="odr-rw"))

    def test_bad_class_id_rejected(self):
        assert not self._valid(_entry("contract-shape", status="open"))  # missing lcc- prefix

    def test_empty_source_refs_rejected(self):
        e = _entry("lcc-x", status="open")
        e["source_refs"] = []
        assert not self._valid(e)

    def test_source_ref_missing_hash_rejected(self):
        e = _entry("lcc-x", status="open")
        e["source_refs"] = [{"source_id": "coach:job1:r2"}]
        assert not self._valid(e)

    def test_bad_detector_rejected(self):
        assert not self._valid(_entry("lcc-x", status="open", detector="overseer"))

    def test_bad_decision_record_pattern_rejected(self):
        assert not self._valid(_entry("lcc-x", status="resolved", decision_record="rw-1"))
