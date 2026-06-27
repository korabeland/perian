"""Tests for the coder coaching loop (tools.coach + the gate_sweep rework edge).

The load-bearing behaviors:
  - round count is derived from events.jsonl ONLY (no poisonable side-file);
  - the loop COACHES under the cap and ESCALATES (coach-exhausted) at the cap;
  - re-dispatch uses only verbs that exist (create/show/link/unlink) and relinks the
    diff-gate from the old coder to the new one;
  - coaching is best-effort: a teacher/re-dispatch failure falls back to a plain hold,
    never silently drops the build (the judge stays the load-bearing gate).
Codex + the live board are stubbed; no network, no kanban.
"""

import json
import subprocess

import pytest

from tools import coach


def _cp(rc=0, out="{}", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


# ── round counter ──────────────────────────────────────────────────────────


def test_rounds_done_counts_only_coach_events_for_this_job(tmp_path):
    ev = tmp_path / "events.jsonl"
    ev.write_text(
        "\n".join(
            [
                json.dumps({"event": "coach", "job_id": "D"}),
                json.dumps({"event": "coach", "job_id": "D"}),
                json.dumps({"event": "coach", "job_id": "OTHER"}),
                json.dumps({"event": "gate-held", "job_id": "D"}),
                "not valid json",
            ]
        )
        + "\n"
    )
    assert coach.rounds_done(ev, "D") == 2
    assert coach.rounds_done(ev, "OTHER") == 1
    assert coach.rounds_done(ev, "NONE") == 0


def test_rounds_done_missing_file_is_zero(tmp_path):
    assert coach.rounds_done(tmp_path / "nope.jsonl", "D") == 0


# ── feedback rendering ─────────────────────────────────────────────────────


def test_render_feedback_numbers_changes_with_check_and_lesson():
    result = {
        "summary": "Two contract gaps to close.",
        "changes": [
            {"instruction": "Validate the brief is non-empty", "satisfies": "AC1: rejects empty", "lesson": "Validate inputs at the boundary."},
            {"instruction": "Lead with Definition of success", "satisfies": "ordering rule", "lesson": "Honor the spec's stated order."},
        ],
    }
    md = coach.render_feedback(result, 2, __import__("pathlib").Path("/t/idea-contract.json"))
    assert "round 2" in md
    assert "1. Validate the brief is non-empty" in md
    assert "2. Lead with Definition of success" in md
    assert "Satisfies: AC1: rejects empty" in md
    assert "Validate inputs at the boundary." in md
    assert "/t/idea-contract.json" in md


# ── teacher (Codex mocked) ─────────────────────────────────────────────────


def test_coach_writes_round_feedback_file(tmp_path, monkeypatch):
    monkeypatch.setattr(coach, "ATB_ROOT", tmp_path.resolve())  # tmp track is "under repo"
    track = tmp_path / "track"
    track.mkdir()
    (track / "idea-contract.json").write_text(
        json.dumps({"what_should_exist": "a formatter", "out_of_scope": ["scoring"]})
    )
    monkeypatch.setattr(coach, "_read_diff", lambda w, b: "diff --git a b\n+code")
    monkeypatch.setattr(
        coach,
        "run_codex",
        lambda *a, **k: {"summary": "s", "changes": [{"instruction": "i", "satisfies": "AC", "lesson": "L"}]},
    )
    res = coach.coach(str(track), "/w", "HEAD", "judge findings", 1)
    p = __import__("pathlib").Path(res["path"])
    assert p.exists() and p.name == "round-1.md"
    assert "i" in p.read_text()
    assert res["changes"][0]["lesson"] == "L"


def test_coach_raises_on_empty_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(coach, "ATB_ROOT", tmp_path.resolve())
    track = tmp_path / "track"
    track.mkdir()
    (track / "idea-contract.json").write_text(json.dumps({"what_should_exist": "x"}))
    monkeypatch.setattr(coach, "_read_diff", lambda w, b: "   ")
    with pytest.raises(RuntimeError, match="empty diff"):
        coach.coach(str(track), "/w", "HEAD", "f", 1)


def test_coach_rejects_track_outside_repo(tmp_path, monkeypatch):
    """Containment (blocker #3): an absolute track outside the repo root is refused."""
    monkeypatch.setattr(coach, "ATB_ROOT", (tmp_path / "repo").resolve())
    (tmp_path / "repo").mkdir()
    outside = tmp_path / "evil"
    outside.mkdir()
    (outside / "idea-contract.json").write_text(json.dumps({"what_should_exist": "x"}))
    with pytest.raises(RuntimeError, match="not under the repo root"):
        coach.coach(str(outside), "/w", "HEAD", "f", 1)


# ── re-dispatch edge (stubbed board) ───────────────────────────────────────


class FakeKanban:
    """Records calls; returns canned JSON for show/create. board is pre-bound (board-less)."""

    def __init__(self, parents, assignee="coder", title="BUILD", new_id="NEW", fail=()):
        self.parents = parents
        self.assignee = assignee
        self.title = title
        self.new_id = new_id
        self.fail = set(fail)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        verb = args[0]
        if verb in self.fail:
            return _cp(1, "", f"{verb} boom")
        if verb == "show":
            if args[1] == "DIFF":
                return _cp(0, json.dumps({"parents": self.parents, "task": {}}))
            return _cp(0, json.dumps({"task": {"assignee": self.assignee, "title": self.title}}))
        if verb == "create":
            return _cp(0, json.dumps({"id": self.new_id}))
        return _cp(0, "")  # link / unlink


def test_redispatch_creates_links_new_then_unlinks_old():
    """Transactional order: create -> LINK new (gate stays gated) -> UNLINK old (blocker #4)."""
    fk = FakeKanban(parents=["OLDCODER"], assignee="coder", new_id="NEW")
    new = coach.redispatch_coder(fk, "DIFF", "/w/build", "FEEDBACK BODY", 2)
    assert new == "NEW"
    assert [c[0] for c in fk.calls] == ["show", "show", "create", "link", "unlink"]
    create = next(c for c in fk.calls if c[0] == "create")
    assert "coder" in create and "OLDCODER" in create  # assignee + parent
    assert "dir:/w/build" in create  # shared workspace
    assert any("FEEDBACK BODY" in a for a in create)  # feedback inline in the body
    assert fk.calls[3] == ("link", "NEW", "DIFF")  # link new FIRST
    assert fk.calls[4] == ("unlink", "OLDCODER", "DIFF")  # then unlink old


def test_redispatch_rolls_back_new_coder_on_link_failure():
    """Link failure -> roll the orphan back with BOTH archive and block (both checked), and raise."""
    fk = FakeKanban(parents=["OLDCODER"], new_id="NEW", fail={"link"})
    with pytest.raises(RuntimeError):
        coach.redispatch_coder(fk, "DIFF", "/w", "fb", 1)
    assert ("archive", "NEW") in fk.calls  # orphan archived
    assert ("block", "NEW") in fk.calls  # AND blocked, so it cannot run if archive is unsupported
    assert not any(c[0] == "unlink" for c in fk.calls)  # old edge left intact


def test_redispatch_unlink_failure_is_best_effort_and_keeps_the_round():
    """Unlink failure must NOT lose the round: the rework is committed, so return the new id.

    Losing it here would let a real rework run UNCOUNTED against the cap (Codex review blocker #2).
    The graph is safe regardless -- the gate already depends on the pending new coder.
    """
    fk = FakeKanban(parents=["OLDCODER"], new_id="NEW", fail={"unlink"})
    new = coach.redispatch_coder(fk, "DIFF", "/w", "fb", 1)
    assert new == "NEW"  # round kept despite the unlink failure
    assert ("link", "NEW", "DIFF") in fk.calls  # new coder IS linked (gate waits on it)


def test_redispatch_raises_without_a_coder_parent():
    fk = FakeKanban(parents=[])
    with pytest.raises(RuntimeError):
        coach.redispatch_coder(fk, "DIFF", "/w", "fb", 1)


def test_redispatch_raises_on_create_failure():
    fk = FakeKanban(parents=["OLDCODER"], fail={"create"})
    with pytest.raises(RuntimeError):
        coach.redispatch_coder(fk, "DIFF", "/w", "fb", 1)


# ── the gate_sweep decision: coach under cap, escalate at cap, fail-safe ────


def _patch_board(monkeypatch, gate_sweep, events_path, tmp_path):
    monkeypatch.setattr(gate_sweep, "EVENTS", events_path)
    monkeypatch.setattr(gate_sweep, "HELD_DIFF_STATE", tmp_path / "held.json")
    calls, emitted = [], []
    monkeypatch.setattr(gate_sweep, "_kanban", lambda board, *a: calls.append(a) or _cp(0, "{}"))
    monkeypatch.setattr(gate_sweep, "emit_event", lambda p: emitted.append(p))
    return calls, emitted


def _coach_n_rounds(events_path, n):
    events_path.write_text("\n".join(json.dumps({"event": "coach", "job_id": "D"}) for _ in range(n)) + ("\n" if n else ""))


def test_gate_sweep_escalates_at_cap_and_records_sha(tmp_path, monkeypatch):
    from tools import gate_sweep

    ev = tmp_path / "events.jsonl"
    _coach_n_rounds(ev, coach.COACH_CAP)  # cap already reached
    _, emitted = _patch_board(monkeypatch, gate_sweep, ev, tmp_path)
    out = gate_sweep._coach_or_escalate("b", "D", "/t", "/w", "HEAD", "findings", "SHA1")
    assert out == "held"
    assert any(e["event"] == "coach-exhausted" and e["rounds"] == coach.COACH_CAP for e in emitted)
    # escalation is deduped across sweeps: the held sha IS recorded
    assert json.loads((tmp_path / "held.json").read_text()).get("D") == "SHA1"


def test_gate_sweep_coaches_under_cap_without_recording_sha(tmp_path, monkeypatch):
    from tools import gate_sweep

    ev = tmp_path / "events.jsonl"
    ev.write_text("")  # zero rounds so far -> round 1
    _, emitted = _patch_board(monkeypatch, gate_sweep, ev, tmp_path)
    monkeypatch.setattr(
        coach, "coach",
        lambda *a, **k: {"feedback_md": "FB", "path": "/t/feedback/round-1.md", "changes": [], "summary": ""},
    )
    monkeypatch.setattr(coach, "redispatch_coder", lambda *a, **k: "NEWCODER")
    out = gate_sweep._coach_or_escalate("b", "D", "/t", "/w", "HEAD", "findings", "SHA1")
    assert out == "coaching"
    coach_ev = [e for e in emitted if e["event"] == "coach"]
    assert coach_ev and coach_ev[0]["round"] == 1 and coach_ev[0]["new_coder"] == "NEWCODER"
    # CRUCIAL (blocker #1): coaching must NOT record the sha, or an unchanged rework stalls silently
    assert not (tmp_path / "held.json").exists()


def test_gate_sweep_last_round_still_coaches(tmp_path, monkeypatch):
    """Boundary: CAP-1 rounds done -> the CAP-th round still coaches (escalation is the next)."""
    from tools import gate_sweep

    ev = tmp_path / "events.jsonl"
    _coach_n_rounds(ev, coach.COACH_CAP - 1)  # one short of the cap
    _, emitted = _patch_board(monkeypatch, gate_sweep, ev, tmp_path)
    monkeypatch.setattr(coach, "coach", lambda *a, **k: {"feedback_md": "FB", "path": "p", "changes": [], "summary": ""})
    monkeypatch.setattr(coach, "redispatch_coder", lambda *a, **k: "NEWCODER")
    out = gate_sweep._coach_or_escalate("b", "D", "/t", "/w", "HEAD", "findings", "SHA1")
    assert out == "coaching"
    assert [e for e in emitted if e["event"] == "coach"][0]["round"] == coach.COACH_CAP


def test_gate_sweep_errors_loud_on_teacher_failure(tmp_path, monkeypatch):
    from tools import gate_sweep

    ev = tmp_path / "events.jsonl"
    ev.write_text("")
    _, emitted = _patch_board(monkeypatch, gate_sweep, ev, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("codex down")

    monkeypatch.setattr(coach, "coach", boom)
    out = gate_sweep._coach_or_escalate("b", "D", "/t", "/w", "HEAD", "findings", "SHA1")
    # blocker #2: a coach failure is a WATCHED gate-error, returns "error" (a trip), not silent
    assert out == "error"
    err = [e for e in emitted if e["event"] == "gate-error"]
    assert err and "coach round 1 failed" in err[0]["reason"]
    assert not any(e["event"] == "coach" for e in emitted)  # no coach event on failure
    assert not (tmp_path / "held.json").exists()  # transient failure -> not recorded, retried


def test_gate_sweep_errors_loud_on_redispatch_failure(tmp_path, monkeypatch):
    from tools import gate_sweep

    ev = tmp_path / "events.jsonl"
    ev.write_text("")
    _, emitted = _patch_board(monkeypatch, gate_sweep, ev, tmp_path)
    monkeypatch.setattr(coach, "coach", lambda *a, **k: {"feedback_md": "FB", "path": "p", "changes": [], "summary": ""})

    def boom(*a, **k):
        raise RuntimeError("kanban link failed")

    monkeypatch.setattr(coach, "redispatch_coder", boom)
    out = gate_sweep._coach_or_escalate("b", "D", "/t", "/w", "HEAD", "findings", "SHA1")
    assert out == "error"
    assert [e for e in emitted if e["event"] == "gate-error"]


def test_process_gate_diff_hold_writes_coach_event_even_with_emit_false(tmp_path, monkeypatch):
    """End-to-end: a diff needs_changes routes to coaching AND records the cap-counting `coach`
    event EVEN WHEN emit=False -- the cap must not depend on --emit-event (Codex final review)."""
    from tools import gate_sweep

    ev = tmp_path / "events.jsonl"
    ev.write_text("")
    _, emitted = _patch_board(monkeypatch, gate_sweep, ev, tmp_path)
    # stub the heavy pre-checks so we reach the (stubbed) Codex verdict
    monkeypatch.setattr(gate_sweep, "_diff_sha", lambda w, b: "SHA1")
    monkeypatch.setattr(gate_sweep, "_run_module", lambda *a, **k: _cp(0, "", ""))  # scope + verify pass
    monkeypatch.setattr(
        gate_sweep, "run_review",
        lambda *a, **k: _cp(2, "[CODEX-REVIEW diff] NEEDS_CHANGES: x\n  - a real finding", ""),
    )
    monkeypatch.setattr(coach, "coach", lambda *a, **k: {"feedback_md": "FB", "path": "p", "changes": [], "summary": ""})
    monkeypatch.setattr(coach, "redispatch_coder", lambda *a, **k: "NEWCODER")
    show = {
        "task": {"body": json.dumps({"atb_gate": "diff", "track": str(tmp_path), "worktree": "/w", "base": "HEAD"})},
        "parents": [],
    }
    out = gate_sweep.process_gate("b", "D", show, dry_run=False, no_swap=True, emit=False)
    assert out == "coaching"
    assert any(e["event"] == "coach" for e in emitted)  # written despite emit=False
