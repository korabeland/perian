"""Tests for the Tier-3 Codex comb + draft pass (U4). The Codex subprocess is ALWAYS stubbed
via an injected runner — never invoke real Codex. v2: Codex combs recurrence CLASSES, every
candidate cites source_class_ids (re-validated against this run's real classes), and recurrence is
TOOL-rendered (never read from Codex prose)."""

import json
from pathlib import Path

from tools import improvement_codex as ic
from tools import improvement_harvest as ih


def _sig(sid, event="gate-held", job_id="A", ts="2026-06-18T09:00:00Z", reason="r"):
    return {"signal_id": sid, "event": event, "job_id": job_id, "ts": ts, "reason": reason}


def _cand(source_class_ids, *, finding="f", tag="universal", confidence="high"):
    return {"finding": finding, "tag": tag, "rationale": "r", "proposed_fix": "p",
            "where_it_lands": "w", "confidence": confidence, "source_class_ids": list(source_class_ids)}


def _events(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.is_file() else []


def _paths(tmp_path):
    return {"digest_dir": tmp_path / "digests", "events_path": tmp_path / "events.jsonl"}


# ============================ validate_candidates (independent re-check) ============================
def test_validate_accepts_good_and_empty():
    good = {"candidates": [_cand(["c1"]), _cand(["c2"], tag="build-specific", confidence="low")]}
    assert ic.validate_candidates(good, {"c1", "c2"})[0] is True
    assert ic.validate_candidates({"candidates": []}, set())[0] is True  # empty is valid
    assert ic.validate_candidates(good)[0] is True  # no valid set -> class-id existence not checked


def test_validate_rejects_missing_field_bad_enum_and_malformed():
    base = _cand(["c1"])
    missing_tag = {k: v for k, v in base.items() if k != "tag"}
    assert ic.validate_candidates({"candidates": [missing_tag]}, {"c1"})[0] is False
    assert ic.validate_candidates({"candidates": [{**base, "tag": "maybe"}]}, {"c1"})[0] is False
    assert ic.validate_candidates({"candidates": [{**base, "confidence": "certain"}]}, {"c1"})[0] is False
    assert ic.validate_candidates({"no": "candidates"})[0] is False
    assert ic.validate_candidates("not a dict")[0] is False


def test_validate_rejects_bad_source_class_ids():
    base = _cand(["c1"])
    no_src = {k: v for k, v in base.items() if k != "source_class_ids"}
    assert ic.validate_candidates({"candidates": [no_src]}, {"c1"})[0] is False        # missing
    assert ic.validate_candidates({"candidates": [_cand([])]}, {"c1"})[0] is False     # empty list
    assert ic.validate_candidates({"candidates": [_cand([7])]}, {"c1"})[0] is False    # non-string
    assert ic.validate_candidates({"candidates": [_cand(["nope"])]}, {"c1"})[0] is False  # unknown id
    assert ic.validate_candidates({"candidates": [_cand(["c1"])]}, {"c1"})[0] is True  # known -> ok


def test_validate_rejects_extra_keys():
    assert ic.validate_candidates({"candidates": [], "evil": 1}, set())[0] is False  # extra top-level
    extra = {**_cand(["c1"]), "evil": "x"}
    assert ic.validate_candidates({"candidates": [extra]}, {"c1"})[0] is False        # extra candidate key


def test_schema_file_loads_and_matches_validator_contract():
    schema = ic._load_schema()  # the deliverable schema file is valid JSON + has the right shape
    item = schema["properties"]["candidates"]["items"]
    assert set(item["required"]) == set(ic._REQUIRED)          # incl. source_class_ids
    assert set(item["properties"]["tag"]["enum"]) == ic._TAGS
    assert set(item["properties"]["confidence"]["enum"]) == ic._CONF
    assert "source_class_ids" in item["properties"]


# ============================ prompt: guardrails + recurrence-as-evidence + citation ============
def test_prompt_carries_guardrails_recurrence_and_citation():
    classes = ih.aggregate_classes([_sig("n1", reason="x")])
    prompt = ic.build_prompt("2026-06-18", classes)
    for needle in ["universal", "build-specific", "monoculture", "ANTI-OVER-FIT", "operating thesis",
                   "STATICALLY", "RECURRENCE IS EVIDENCE", "source_class_ids", "across build TYPES"]:
        assert needle in prompt, f"prompt missing {needle!r}"
    assert "class_id=" in prompt and "signal_id=" not in prompt  # prompt enumerates CLASSES, not occurrences


def test_prompt_marks_already_logged_classes():
    s = _sig("n1", reason="x")
    classes = ih.aggregate_classes([s])
    cid = ih.class_id(s)

    def class_line(prompt):  # the enumeration line for this class (not the guardrail instructions)
        return next(l for l in prompt.splitlines() if f"class_id={cid}" in l)

    assert "[already logged]" not in class_line(ic.build_prompt("2026-06-18", classes))
    assert "[already logged]" in class_line(ic.build_prompt("2026-06-18", classes, already_logged={cid}))


def test_validate_rejects_duplicate_source_class_ids():
    assert ic.validate_candidates({"candidates": [_cand(["c1", "c1"])]}, {"c1"})[0] is False


# ============================ happy draft path ============================
def test_happy_drafts_candidates_with_tags_and_unverified_banner(tmp_path):
    p = _paths(tmp_path)
    survivors = [_sig("n1", reason="x"), _sig("n2", reason="y")]
    cids = [ih.class_id(s) for s in survivors]
    good = {"candidates": [_cand([cids[0]]), _cand([cids[1]], tag="build-specific", confidence="low")]}
    res = ic.run_tier3("2026-06-18", survivors, runner=lambda prompt: good, **p)
    assert res.status == "drafted" and res.exit_code == 0
    assert len(res.candidates) == 2
    text = (p["digest_dir"] / "2026-06-18-candidates.md").read_text()
    assert "UNVERIFIED" in text
    assert "universal" in text and "build-specific" in text
    assert "Recurrence (tool-verified)" in text
    assert any(e["event"] == "improvement-candidates" and e["candidates"] == 2 for e in _events(p["events_path"]))


def test_recurrence_is_tool_rendered_not_codex_prose(tmp_path):
    """The candidate's recurrence is rendered from the deterministic class record (count 3), NOT from
    Codex's prose (which lies '999 times'). doer != certifier — the count is a tool fact."""
    p = _paths(tmp_path)
    survivors = [_sig("a", reason="x"), _sig("b", reason="x"), _sig("c", reason="x")]  # one class, count 3
    cid = ih.class_id(survivors[0])
    bogus = {"candidates": [_cand([cid], finding="this recurred 999 times")]}
    ic.run_tier3("2026-06-18", survivors, runner=lambda prompt: bogus, **p)
    text = (p["digest_dir"] / "2026-06-18-candidates.md").read_text()
    assert "×3 across 1 build(s)/1 day(s)" in text  # TOOL count (3), independent of the prose '999'


# ============================ only fires on survivors ============================
def test_no_survivors_makes_no_codex_call(tmp_path):
    p = _paths(tmp_path)

    def must_not_run(prompt):
        raise AssertionError("Codex must not be called when there are no survivors")

    res = ic.run_tier3("2026-06-18", [], runner=must_not_run, **p)
    assert res.status == "no-survivors" and res.exit_code == 0
    assert not (p["digest_dir"] / "2026-06-18-candidates.md").exists()


# ============================ error / schema / unknown-class -> fail-safe ============================
def test_codex_error_fails_safe_without_fabricating(tmp_path):
    p = _paths(tmp_path)

    def boom(prompt):
        raise RuntimeError("codex exec exit 1: timeout")

    res = ic.run_tier3("2026-06-18", [_sig("n1", reason="x"), _sig("n2", reason="y")], runner=boom, **p)
    assert res.status == "failed-safe" and res.exit_code == 1
    assert not (p["digest_dir"] / "2026-06-18-candidates.md").exists()  # nothing fabricated
    failed = p["digest_dir"] / "2026-06-18-tier3-FAILED.md"
    assert failed.exists()
    assert "n1" in failed.read_text() and "n2" in failed.read_text()  # survivor occurrences preserved
    assert any(e["event"] == "improvement-candidates-failed" for e in _events(p["events_path"]))


def test_malformed_codex_output_rejected_as_failsafe(tmp_path):
    p = _paths(tmp_path)
    base = _cand(["c1"])
    missing_tag = {k: v for k, v in base.items() if k != "tag"}
    res = ic.run_tier3("2026-06-18", [_sig("n1", reason="x")],
                       runner=lambda prompt: {"candidates": [missing_tag]}, **p)
    assert res.status == "failed-safe"  # not silently accepted
    assert not (p["digest_dir"] / "2026-06-18-candidates.md").exists()
    assert (p["digest_dir"] / "2026-06-18-tier3-FAILED.md").exists()


def test_unknown_source_class_id_fails_safe(tmp_path):
    """A candidate citing a class_id not in THIS run's classes is rejected (fabricated citation)."""
    p = _paths(tmp_path)
    res = ic.run_tier3("2026-06-18", [_sig("n1", reason="x")],
                       runner=lambda prompt: {"candidates": [_cand(["deadbeefdead"])]}, **p)
    assert res.status == "failed-safe"
    assert not (p["digest_dir"] / "2026-06-18-candidates.md").exists()
    assert (p["digest_dir"] / "2026-06-18-tier3-FAILED.md").exists()


# ============================ empty candidate draft renders cleanly ============================
def test_empty_candidates_drafts_valid_empty_file(tmp_path):
    p = _paths(tmp_path)
    res = ic.run_tier3("2026-06-18", [_sig("n1", reason="x")], runner=lambda prompt: {"candidates": []}, **p)
    assert res.status == "drafted"
    text = (p["digest_dir"] / "2026-06-18-candidates.md").read_text()
    assert "UNVERIFIED" in text
    assert "no candidate" in text.lower()


# --- the candidates file carries occurrence ids for the certified-known loop; prompt carries classes
def test_source_signal_ids_surfaced_in_file_and_classes_in_prompt(tmp_path):
    p = _paths(tmp_path)
    survivors = [_sig("n1", reason="x"), _sig("n2", reason="y")]
    cids = [ih.class_id(s) for s in survivors]
    ic.run_tier3("2026-06-18", survivors, runner=lambda prompt: {"candidates": [_cand([cids[0]])]}, **p)
    text = (p["digest_dir"] / "2026-06-18-candidates.md").read_text()
    assert "certified-signals: n1 n2" in text  # occurrence ids for EXACT suppression (unchanged)
    prompt = ic.build_prompt("2026-06-18", ih.aggregate_classes(survivors))
    assert f"class_id={cids[0]}" in prompt  # the Codex prompt enumerates classes


# --- the failed event is emitted only AFTER the fail-safe artifact exists ----
def test_fail_safe_emits_event_after_artifact(tmp_path, monkeypatch):
    p = _paths(tmp_path)
    artifact = p["digest_dir"] / "2026-06-18-tier3-FAILED.md"
    captured = {}
    real_emit = ic.emit_event

    def spy(payload, events):
        captured["artifact_existed_at_emit"] = artifact.exists()
        return real_emit(payload, events)

    monkeypatch.setattr(ic, "emit_event", spy)

    def boom(prompt):
        raise RuntimeError("codex error")

    ic.run_tier3("2026-06-18", [_sig("n1", reason="x")], runner=boom, **p)
    assert captured["artifact_existed_at_emit"] is True
