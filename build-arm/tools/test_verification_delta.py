"""Tests for tools.verification_delta — the deterministic, model-free delta.

The load-bearing test is `test_future_timestamp_is_rejected_as_fabricated`: it reproduces
the run #3 forgery (a tool-shaped qa-verification stamped in the future) and asserts the
delta refuses to `confirmed` it.
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tools.verification_delta import (
    build_delta,
    check_freshness,
    compute_delta,
)

UTC = timezone.utc
NOW = datetime(2026, 6, 3, 4, 0, 0, tzinfo=UTC)
FRESH = "2026-06-03T03:59:30Z"   # 30s before NOW
FUTURE = "2026-06-03T04:12:00Z"  # 12 min in the future (the run #3 forgery)
STALE = "2026-06-01T00:00:00Z"   # >24h before NOW


def _vrep(ran_at, commands=("pytest -q",), exits=(0,), passed=True, verifier="coder", job_id="job"):
    outputs = [
        {"command": c, "exit_code": e, "stdout_excerpt": "", "stderr_excerpt": ""}
        for c, e in zip(commands, exits)
    ]
    return {
        "job_id": job_id,
        "ran_at": ran_at,
        "commands_run": list(commands),
        "outputs": outputs,
        "pass": passed,
        "evidence_refs": [],
        "verifier": verifier,
        "notes": "",
    }


def _delta(coder, qa, now=NOW):
    return compute_delta(coder, qa, job_id="job", now=now)


# --- confirmed --------------------------------------------------------------

def test_confirmed_when_both_pass_and_agree():
    d = _delta(_vrep(FRESH), _vrep(FRESH, verifier="qa"))
    assert d["state"] == "confirmed"
    assert d["differences"] == []
    assert d["coder_pass"] is True and d["qa_pass"] is True


# --- regression -------------------------------------------------------------

def test_regression_when_qa_fails():
    d = _delta(_vrep(FRESH, exits=(0,), passed=True), _vrep(FRESH, exits=(1,), passed=False, verifier="qa"))
    assert d["state"] == "regression"
    assert any(x["field"] == "pass" for x in d["differences"])


def test_regression_when_neither_passes():
    d = _delta(_vrep(FRESH, exits=(1,), passed=False), _vrep(FRESH, exits=(1,), passed=False, verifier="qa"))
    assert d["state"] == "regression"


# --- drift ------------------------------------------------------------------

def test_drift_when_both_pass_but_commands_differ():
    coder = _vrep(FRESH, commands=("pytest -q",), passed=True)
    qa = _vrep(FRESH, commands=("pytest -q tests/",), passed=True, verifier="qa")
    d = _delta(coder, qa)
    assert d["state"] == "drift"
    assert any(x["field"] == "commands_run" for x in d["differences"])


def test_drift_when_exit_codes_differ_for_same_command():
    coder = _vrep(FRESH, commands=("a", "b"), exits=(0, 0), passed=True)
    qa = _vrep(FRESH, commands=("a", "b"), exits=(0, 3), passed=True, verifier="qa")
    d = _delta(coder, qa)
    assert d["state"] == "drift"
    assert any(x["field"].startswith("outputs[") for x in d["differences"])


def test_drift_when_same_commands_different_order():
    """set() comparison would wrongly confirm this; ordered comparison must flag drift."""
    coder = _vrep(FRESH, commands=("a", "b"), exits=(0, 0), passed=True)
    qa = _vrep(FRESH, commands=("b", "a"), exits=(0, 0), passed=True, verifier="qa")
    d = _delta(coder, qa)
    assert d["state"] == "drift"
    assert any(x["field"] == "commands_run" for x in d["differences"])


# --- missing_evidence -------------------------------------------------------

def test_missing_evidence_when_qa_absent():
    assert _delta(_vrep(FRESH), None)["state"] == "missing_evidence"


def test_missing_evidence_when_coder_absent():
    assert _delta(None, _vrep(FRESH, verifier="qa"))["state"] == "missing_evidence"


def test_missing_evidence_when_empty_commands():
    d = _delta(
        _vrep(FRESH, commands=(), exits=(), passed=False),
        _vrep(FRESH, commands=(), exits=(), passed=False, verifier="qa"),
    )
    assert d["state"] == "missing_evidence"


def test_unparseable_timestamp_is_missing_evidence():
    d = _delta(_vrep("not-a-timestamp", passed=True), _vrep(FRESH, verifier="qa"))
    assert d["state"] == "missing_evidence"


# --- freshness guard (the run #3 fingerprint) -------------------------------

def test_future_timestamp_is_rejected_as_fabricated():
    """run #3 forgery: a qa-verification stamped in the future must NOT confirm."""
    d = _delta(_vrep(FRESH), _vrep(FUTURE, verifier="qa"))
    assert d["state"] == "missing_evidence"
    assert any("future" in x["qa_finding"] for x in d["differences"])


def test_stale_timestamp_is_rejected():
    d = _delta(_vrep(STALE), _vrep(FRESH, verifier="qa"))
    assert d["state"] == "missing_evidence"


def test_check_freshness_accepts_recent_and_rejects_future():
    ok, _ = check_freshness(_vrep(FRESH), NOW)
    assert ok
    bad, reason = check_freshness(_vrep(FUTURE), NOW)
    assert not bad and "future" in reason


# --- differences are schema-shaped ------------------------------------------

def test_differences_entries_have_required_fields():
    d = _delta(_vrep(FRESH, passed=True), _vrep(FRESH, exits=(1,), passed=False, verifier="qa"))
    for entry in d["differences"]:
        assert set(entry) >= {"field", "coder_claim", "qa_finding"}
        assert all(isinstance(entry[k], str) for k in ("field", "coder_claim", "qa_finding"))


# --- produced artifact validates against the real schema --------------------

def test_delta_validates_against_schema():
    from tools.validator import schema_loader, validator as _vd

    d = _delta(_vrep(FRESH), _vrep(FRESH, verifier="qa"))
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "verification-delta.json"
        p.write_text(json.dumps(d))
        schema = schema_loader.load_schema("verification-delta")
        res = _vd.validate_artifact(str(p), schema)
    assert res.get("pass_"), res.get("errors")


# --- build_delta reads files off disk ---------------------------------------

def test_build_delta_reads_files(tmp_path):
    (tmp_path / "verification.json").write_text(json.dumps(_vrep(FRESH)))
    (tmp_path / "qa-verification.json").write_text(json.dumps(_vrep(FRESH, verifier="qa")))
    d = build_delta(tmp_path, now=NOW)
    assert d["state"] == "confirmed"
    assert d["job_id"] == "job"
