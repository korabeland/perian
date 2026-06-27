"""Tests for tools/meta_audit_run.py — the verify-the-verifier cross-family meta-audit (Phase 3).

Every advisor CLI is faked via the injectable runner — no subprocess runs. The tests pin the
load-bearing properties: (1) target-family-advisory synthesis (the audited family's seats never
bind; both non-target families must be present or the verdict fails closed to NEEDS_MORE),
(2) FLAGGED -> exit 2 surfacing, (3) verifier-miss routing lifted from binding seats only,
(4) the packet is assembled trust-only and fenced as DATA, (5) a digest is written + an event
emitted, never a mutation of any agent layer.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from pathlib import Path

import pytest

from tools import meta_audit_run as mar

GEM = "Gemini 3.1 Pro (High)"
TODAY = date(2026, 6, 20)
TS = "2026-06-20T12:00:00Z"

_PERSONA_BY_KEY = {
    "grounding": "Grounding-Auditor",
    "independence": "Independence-Auditor",
    "gate": "Gate-Soundness-Auditor",
    "drop": "Drop-Auditor",
    "overfit": "Overfit-Auditor",
    "scope": "Scope-Auditor",
}


# --------------------------------------------------------------------------- fixtures / helpers


def _valid_record(rid, decision="layer-rewrite", status="certified",
                  commit_hash=None, layer="identity", expiry=None, target="coder") -> dict:
    rec = {
        "id": rid, "created_ts": "2026-06-20T12:00:00Z", "target_agent": target,
        "grounding_pack_id": "gp-0123456789abcdef", "memory_refs": ["project_atb_operating_thesis"],
        "log_refs": [], "detector_source_refs": [], "layer_considered": layer,
        "decision": decision, "certifier": "codex+korab", "status": status, "summary": "x",
    }
    if commit_hash is not None:
        rec["commit_hash"] = commit_hash
    if expiry is not None:
        rec["expiry"] = expiry
    return rec


def _setup_root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir()
    rec_dir = tmp_path / "overseer" / "records"
    rec_dir.mkdir(parents=True)
    (tmp_path / "overseer" / "ledger").mkdir(parents=True)
    (rec_dir / "odr-1.json").write_text(json.dumps(_valid_record("odr-1", commit_hash="a" * 12)))
    (rec_dir / "odr-2.json").write_text(json.dumps(_valid_record("odr-2", "no_change", "certified")))
    events = [
        {"ts": "2026-06-19T01:00:00Z", "event": "pre-build-snapshot", "job_id": "j1"},  # filtered
        {"ts": "2026-06-19T02:00:00Z", "event": "gate-held", "job_id": "j1", "gate": "diff", "reasons": ["bug X"]},
        {"ts": "2026-06-19T03:00:00Z", "event": "codex-review", "job_id": "j1", "verdict": "approve"},
        {"ts": "2026-06-19T04:00:00Z", "event": "coach", "job_id": "j1", "round": 1},
    ]
    (tmp_path / "state" / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return tmp_path


def _meta_vote(verdict="SOUND", band="low", reasons=("ok",)) -> str:
    return json.dumps({"verdict": verdict, "risk_band": band, "reasons": list(reasons)})


def _persona_of(prompt: str) -> str:
    m = re.search(r"You are the (.+?) on an independent", prompt)
    assert m, "meta-audit prompt missing its persona header"
    return m.group(1)


class FakeRunner:
    def __init__(self, answers):
        self.answers = answers
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, cmd, *, env, cwd, timeout):
        prompt = cmd[2] if cmd[0] == "agy" else cmd[-1]
        persona = _persona_of(prompt)
        with self._lock:
            self.calls.append({"cmd": cmd, "persona": persona, "prompt": prompt})
        ans = self.answers[persona]
        if isinstance(ans, BaseException):
            raise ans
        return ans


def _runner(**over) -> FakeRunner:
    ok = (0, "analysis...\n" + _meta_vote(), "")
    answers = {name: ok for name in _PERSONA_BY_KEY.values()}
    for key, val in over.items():
        answers[_PERSONA_BY_KEY[key]] = val
    return FakeRunner(answers)


def _run(tmp_path, target="codex", model_lister=None, **over):
    root = _setup_root(tmp_path)
    r = _runner(**over)
    code, doc = mar.run_meta_audit(
        root, target, runner=r, model_lister=model_lister or (lambda: [GEM]),
        today=TODAY, generated_ts=TS,
    )
    return code, doc, r, root


def _digest(root: Path) -> dict:
    return json.loads(next((root / "overseer" / "meta-audits").glob("*/digest.json")).read_text())


# --------------------------------------------------------------------------- pure core


def test_self_test_passes():
    assert mar._self_test() == 0


def test_sound_panel_returns_0_and_writes_digest(tmp_path):
    code, doc, _, root = _run(tmp_path)
    assert code == 0 and doc["verdict"] == "SOUND" and doc["quorum_met"] is True
    assert _digest(root)["verdict"] == "SOUND"  # persisted


def test_flagged_binding_majority_returns_2(tmp_path):
    # target codex -> binding families are claude + gemini; flag all four binding seats.
    code, doc, _, _ = _run(
        tmp_path, target="codex",
        grounding=(0, _meta_vote("FLAGGED", "high", ("no grounding_pack_id",)), ""),
        independence=(0, _meta_vote("FLAGGED", "medium", ("self-certified",)), ""),
        overfit=(0, _meta_vote("FLAGGED", "low"), ""),
        scope=(0, _meta_vote("FLAGGED", "low"), ""),
    )
    assert code == 2 and doc["verdict"] == "FLAGGED" and doc["risk_band"] == "high"


def test_target_family_seats_are_advisory_only(tmp_path):
    # target codex: codex seats (gate, drop) scream FLAGGED/critical but must NOT bind.
    code, doc, _, _ = _run(
        tmp_path, target="codex",
        gate=(0, _meta_vote("FLAGGED", "critical", ("rubber stamp",)), ""),
        drop=(0, _meta_vote("FLAGGED", "critical", ("dropped",)), ""),
    )
    assert code == 0 and doc["verdict"] == "SOUND"  # binding claude+gemini all SOUND
    assert doc["risk_band"] == "low"  # codex's critical is advisory-only, excluded from the band
    advisory = [f for f in doc["findings"] if f["role"] == "advisory"]
    assert {f["family"] for f in advisory} == {"codex"}


def test_quorum_not_met_forces_needs_more(tmp_path):
    # a whole non-target family (gemini) abstains -> only one non-target family present -> fail closed.
    code, doc, _, _ = _run(
        tmp_path, target="codex",
        overfit=(-1, "", ""), scope=(-1, "", ""),
    )
    assert doc["quorum_met"] is False and doc["verdict"] == "NEEDS_MORE" and code == 0


def test_no_binding_majority_is_needs_more(tmp_path):
    # binding pool (claude+gemini): 2 SOUND / 2 FLAGGED -> no strict majority -> NEEDS_MORE.
    code, doc, _, _ = _run(
        tmp_path, target="codex",
        grounding=(0, _meta_vote("FLAGGED", "low"), ""),
        overfit=(0, _meta_vote("FLAGGED", "low"), ""),
    )
    assert doc["verdict"] == "NEEDS_MORE"


def test_routing_lifted_from_binding_seats_only(tmp_path):
    code, doc, _, _ = _run(
        tmp_path, target="codex",
        grounding=(0, _meta_vote("FLAGGED", "low", ("ROUTE: coach | coder mishandles empty input",)), ""),
        gate=(0, _meta_vote("FLAGGED", "low", ("ROUTE: gate_hold | advisory should be ignored",)), ""),  # codex=advisory
    )
    routes = doc["routing"]
    assert len(routes) == 1 and routes[0]["detector"] == "coach"
    assert routes[0]["seat"].startswith("claude/")


def test_routing_rejects_unknown_detector(tmp_path):
    code, doc, _, _ = _run(
        tmp_path, target="codex",
        grounding=(0, _meta_vote("FLAGGED", "low", ("ROUTE: nonsense | not a real detector",)), ""),
    )
    assert doc["routing"] == []


def test_digest_is_schema_valid(tmp_path):
    from jsonschema import Draft202012Validator

    _, _, _, root = _run(tmp_path)
    schema = json.loads(mar.VERDICT_SCHEMA.read_text())
    assert not list(Draft202012Validator(schema).iter_errors(_digest(root)))


def test_event_emitted(tmp_path):
    _, doc, _, root = _run(tmp_path)
    events = [json.loads(ln) for ln in (root / "state" / "events.jsonl").read_text().splitlines()]
    last = events[-1]
    assert last["event"] == "meta-audit" and last["target_family"] == "codex"
    assert last["verdict"] == doc["verdict"] and "path" in last


def test_unknown_target_family_raises(tmp_path):
    root = _setup_root(tmp_path)
    with pytest.raises(ValueError):
        mar.run_meta_audit(root, "gemini", runner=_runner(), model_lister=lambda: [GEM])


# --------------------------------------------------------------------------- packet assembly (trust-only)


def test_sample_decision_records_recent_with_counts(tmp_path):
    root = _setup_root(tmp_path)
    (root / "overseer" / "records" / "bad.json").write_text('{"id": "odr-bad"}')  # schema-invalid
    out = mar.sample_decision_records(root / "overseer" / "records", limit=12)
    assert out["total"] == 2 and out["invalid_count"] == 1  # bad excluded from total, counted
    assert {r["id"] for r in out["sampled"]} == {"odr-1", "odr-2"}


def test_sample_gate_judgments_filters_and_windows(tmp_path):
    root = _setup_root(tmp_path)
    out = mar.sample_gate_judgments(root / "state" / "events.jsonl", limit=25)
    kinds = {e["event"] for e in out}
    assert "pre-build-snapshot" not in kinds  # non-verifier event filtered out
    assert {"gate-held", "codex-review", "coach"} <= kinds


def test_build_packet_is_schema_valid(tmp_path):
    from jsonschema import Draft202012Validator

    root = _setup_root(tmp_path)
    packet = mar.build_packet(
        "codex", records_dir=root / "overseer" / "records",
        events_path=root / "state" / "events.jsonl", ledger_dir=root / "overseer" / "ledger",
        today=TODAY, generated_ts=TS, record_limit=12, gate_limit=25,
    )
    schema = json.loads(mar.PACKET_SCHEMA.read_text())
    assert not list(Draft202012Validator(schema).iter_errors(packet))


# --------------------------------------------------------------------------- prompt + vote parsing


def test_prompt_names_target_scope_overfit_and_fences_packet(tmp_path):
    _, _, r, _ = _run(tmp_path, target="codex")
    prompt = r.calls[0]["prompt"]
    assert "OpenAI / Codex" in prompt  # the audited family is named
    assert "SCOPE DISCIPLINE" in prompt and "ROUTE:" in prompt
    assert "ANTI-OVER-FIT" in prompt  # the reused over-fit guardrail
    assert prompt.count("END UNTRUSTED DATA>>>") == 1  # packet fenced as data


def test_parse_meta_vote_last_and_enum():
    text = 'banner\n{"verdict":"SOUND","risk_band":"low","reasons":["a"]}\nmore\n{"verdict":"FLAGGED","risk_band":"high","reasons":["b","c"]}'
    v = mar._parse_meta_vote(text)
    assert v["verdict"] == "FLAGGED" and v["risk_band"] == "high" and v["reasons"] == ["b", "c"]


def test_parse_meta_vote_rejects_out_of_enum():
    assert mar._parse_meta_vote('{"verdict":"PROCEED","risk_band":"low","reasons":[]}') is None


# --------------------------------------------------------------------------- CLI


def test_main_self_test_returns_0():
    assert mar.main(["--self-test"]) == 0


def test_main_requires_target_family():
    assert mar.main([]) == 1
