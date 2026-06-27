#!/usr/bin/env python3
"""Tests for the think-loop kanban projector (mirror, not motor).

Every kanban call is mocked — no hermes subprocess ever runs. The tests pin the THREE
load-bearing properties: (1) the verb whitelist makes the no-motor invariant code-enforced,
(2) derivation is a pure fold of disk truth, (3) projection is idempotent and never leaves
a card in a claimable state.

Run from the ATB root: python3 -m pytest tools/test_think_board.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import think_board as tb


# --------------------------------------------------------------------------------------
# Fixtures — disk truth + a recording fake for _kanban
# --------------------------------------------------------------------------------------
def _emit(root: Path, **e) -> None:
    p = root / "state" / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps({"profile": "think", **e}) + "\n")


def _track(root: Path, job: str, what: str = "a useful tool", lane: str = "new-thing") -> None:
    d = root / "state" / "tracks" / job
    d.mkdir(parents=True, exist_ok=True)
    (d / "idea-contract.json").write_text(json.dumps({"what_should_exist": what}))
    (d / "lane").write_text(lane)


class FakeKanban:
    """Records every (verb, argv) and answers like the real CLI: list returns the board,
    create returns a fresh task JSON (status mirrors --initial-status, default ready)."""

    def __init__(self, existing: list[dict] | None = None):
        self.board: list[dict] = list(existing or [])
        self.calls: list[tuple[str, ...]] = []
        self._n = 0

    _TRANSITION = {"schedule": "scheduled", "block": "blocked",
                   "complete": "done", "archive": "archived"}

    def __call__(self, *args: str) -> str:
        if args[0] not in tb.ALLOWED_VERBS:  # mirror the real guard for realism
            raise tb.KanbanError(f"verb not allowed: {args[0]}")
        self.calls.append(args)
        if args[0] == "list":
            return json.dumps(self.board)
        if args[0] == "create":
            self._n += 1
            status = "ready"
            if "--initial-status" in args:
                status = args[args.index("--initial-status") + 1]
            task = {"id": f"t_{self._n}", "title": args[1], "status": status}
            self.board.append(task)
            return json.dumps(task)
        if args[0] in self._TRANSITION:  # the real CLI moves the card; model that
            for t in self.board:
                if t["id"] == args[1]:
                    t["status"] = self._TRANSITION[args[0]]
        return ""

    def verbs(self, verb: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == verb]


@pytest.fixture
def fake(monkeypatch):
    fk = FakeKanban()
    monkeypatch.setattr(tb, "_kanban", fk)
    return fk


# --------------------------------------------------------------------------------------
# (1) No-motor invariant: the verb whitelist is code, not convention
# --------------------------------------------------------------------------------------
class TestVerbWhitelist:
    @pytest.mark.parametrize(
        "verb", ["dispatch", "daemon", "claim", "promote", "unblock", "assign", "swarm", ""]
    )
    def test_motor_verbs_are_refused(self, verb):
        with pytest.raises(tb.KanbanError):
            tb._kanban(verb, "t_1")

    def test_whitelist_is_exactly_the_mirror_verbs(self):
        assert tb.ALLOWED_VERBS == {
            "create", "list", "comment", "complete", "block", "schedule", "archive"
        }


# --------------------------------------------------------------------------------------
# (2) Derivation — a pure fold of events.jsonl + tracks/ + builds/
# --------------------------------------------------------------------------------------
class TestDerive:
    def test_stage_fold_last_event_wins(self, tmp_path):
        _emit(tmp_path, job_id="j1", event="t1-pass")
        _emit(tmp_path, job_id="j1", event="t2-pass")
        _track(tmp_path, "j1")
        jobs = tb.derive_jobs(tmp_path)
        assert jobs["j1"]["stage"] == "proposed"
        assert jobs["j1"]["lane"] == "new-thing"
        assert "a useful tool" in jobs["j1"]["title"]

    def test_hold_park_digest_and_build_override(self, tmp_path):
        _emit(tmp_path, job_id="held", event="t1-pass")
        _emit(tmp_path, job_id="held", event="think-gate-fail", gate="T2", reasons=["bad"])
        _emit(tmp_path, job_id="parked", event="t2-pass")
        _emit(tmp_path, job_id="parked", event="think-parked", gate="T2", reasons=["dup"])
        _emit(tmp_path, job_id="digested", event="t2-pass")
        _emit(tmp_path, job_id="digested", event="digest")
        _emit(tmp_path, job_id="crossed", event="digest")
        (tmp_path / "state" / "builds" / "crossed").mkdir(parents=True)
        jobs = tb.derive_jobs(tmp_path)
        assert jobs["held"]["stage"] == "held" and "T2" in jobs["held"]["note"]
        assert jobs["parked"]["stage"] == "parked"
        assert jobs["digested"]["stage"] == "digest"
        assert jobs["crossed"]["stage"] == "building"  # builds/ overrides the event fold

    def test_non_think_and_seed_events_ignored(self, tmp_path):
        p = tmp_path / "state" / "events.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text(
            json.dumps({"profile": "coder", "job_id": "b", "event": "t1-pass"}) + "\n"
            + json.dumps({"profile": "think", "job_id": "seed", "event": "t1-pass"}) + "\n"
            + "not json\n"
        )
        assert tb.derive_jobs(tmp_path) == {}

    def test_topics_skip_comments_and_blanks(self, tmp_path):
        f = tmp_path / "state" / "think" / "topics.txt"
        f.parent.mkdir(parents=True)
        f.write_text("# c\n\nalpha\n beta \n")
        assert tb.derive_topics(tmp_path) == ["alpha", "beta"]


# --------------------------------------------------------------------------------------
# (3) Projection — idempotent, never claimable, correct transitions
# --------------------------------------------------------------------------------------
class TestProject:
    def test_idea_cards_never_created_claimable(self, tmp_path, fake):
        _emit(tmp_path, job_id="j1", event="t1-pass")
        _track(tmp_path, "j1")
        tb.project(tmp_path)
        creates = fake.verbs("create")
        idea = [c for c in creates if c[1].startswith("j1")]
        assert idea and "--initial-status" in idea[0]
        assert idea[0][idea[0].index("--initial-status") + 1] == "running"
        assert "think:job:j1" in idea[0]  # idempotency key

    def test_topic_seed_created_then_scheduled(self, tmp_path, fake):
        f = tmp_path / "state" / "think" / "topics.txt"
        f.parent.mkdir(parents=True)
        f.write_text("alpha\n")
        tb.project(tmp_path)
        assert any(c[1] == "Topic: alpha" for c in fake.verbs("create"))
        assert fake.verbs("schedule")  # parked in the Queued column

    def test_transitions_block_complete_archive(self, tmp_path, fake):
        _emit(tmp_path, job_id="held", event="think-gate-fail", gate="T1", reasons=["no write"])
        _emit(tmp_path, job_id="digested", event="digest")
        _emit(tmp_path, job_id="crossed", event="t2-pass")
        (tmp_path / "state" / "builds" / "crossed").mkdir(parents=True)
        tb.project(tmp_path)
        assert fake.verbs("block"), "held idea must move to blocked"
        completes = fake.verbs("complete")
        assert len(completes) == 2  # digest -> done; building -> complete+archive
        assert fake.verbs("archive"), "crossed idea must be archived"

    def test_second_run_is_idempotent(self, tmp_path, fake):
        _emit(tmp_path, job_id="j1", event="t1-pass")
        _track(tmp_path, "j1")
        f = tmp_path / "state" / "think" / "topics.txt"
        f.parent.mkdir(parents=True)
        f.write_text("alpha\n")
        tb.project(tmp_path)
        n_calls = len(fake.calls)
        tb.project(tmp_path)  # same disk state -> no new create/move/comment
        new = fake.calls[n_calls:]
        assert all(c[0] == "list" for c in new), f"second run mutated the board: {new}"

    def test_stage_comment_appears_once_per_stage(self, tmp_path, fake):
        _emit(tmp_path, job_id="j1", event="t1-pass")
        _track(tmp_path, "j1")
        tb.project(tmp_path)
        tb.project(tmp_path)
        comments = [c for c in fake.verbs("comment")]
        assert len(comments) == 1
        _emit(tmp_path, job_id="j1", event="t2-pass")
        tb.project(tmp_path)
        assert len(fake.verbs("comment")) == 2  # new stage -> exactly one new comment

    def test_done_cards_never_move_back(self, tmp_path, fake):
        fake.board.append({"id": "t_done", "title": "j1", "status": "done"})
        _emit(tmp_path, job_id="j1", event="t1-pass")  # disk says researched (stale board ok)
        _track(tmp_path, "j1")
        tb.project(tmp_path)
        assert not fake.verbs("block") and not fake.verbs("complete")


class TestTopicRepair:
    def test_ready_topic_card_is_rescheduled(self, tmp_path, fake):
        """Codex board HOLD #2: a topic card stranded in `ready` (create succeeded, schedule
        failed on a past run) must be REPAIRED on the next pass, never skipped — no card may
        be LEFT claimable."""
        fake.board.append({"id": "t_stranded", "title": "Topic: alpha", "status": "ready"})
        f = tmp_path / "state" / "think" / "topics.txt"
        f.parent.mkdir(parents=True)
        f.write_text("alpha\n")
        tb.project(tmp_path)
        assert not fake.verbs("create")  # not duplicated
        scheds = fake.verbs("schedule")
        assert scheds and scheds[0][1] == "t_stranded"

    def test_scheduled_topic_card_untouched(self, tmp_path, fake):
        fake.board.append({"id": "t_ok", "title": "Topic: alpha", "status": "scheduled"})
        f = tmp_path / "state" / "think" / "topics.txt"
        f.parent.mkdir(parents=True)
        f.write_text("alpha\n")
        tb.project(tmp_path)
        assert not fake.verbs("create") and not fake.verbs("schedule")


class TestBestEffort:
    def test_never_raises(self, tmp_path, monkeypatch):
        def boom(*a):
            raise tb.KanbanError("kanban down")

        monkeypatch.setattr(tb, "_kanban", boom)
        tb.project_best_effort(tmp_path)  # must not raise

    def test_sweep_exit_code_unaffected(self, tmp_path, monkeypatch, capsys):
        # the hook contract: even a TypeError inside projection is swallowed
        monkeypatch.setattr(tb, "project", lambda root: (_ for _ in ()).throw(TypeError("bug")))
        tb.project_best_effort(tmp_path)
        assert "projection skipped" in capsys.readouterr().err
