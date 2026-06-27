#!/usr/bin/env python3
"""Tests for brief_inbox — the read-only pending-briefs lister (gap #3 delivery seam).

Run from the ATB root: python3 -m pytest tools/test_brief_inbox.py -q
"""

from __future__ import annotations

import json

from tools import brief_inbox as bi


def _mk(root, job_id, *, proposed_at, what="a small tool", decision="approved_for_coder",
        risk="low", build=False, contract=True, review=True):
    (root / "state" / "digests" / "briefs").mkdir(parents=True, exist_ok=True)
    (root / "state" / "digests" / "briefs" / f"{job_id}.md").write_text(
        f"# Idea Brief\n\n**Job:** {job_id}\n"
    )
    track = root / "state" / "tracks" / job_id
    track.mkdir(parents=True, exist_ok=True)
    if contract:
        (track / "idea-contract.json").write_text(
            json.dumps({"job_id": job_id, "proposed_at": proposed_at, "what_should_exist": what})
        )
    if review:
        (track / "main-review.json").write_text(
            json.dumps({"job_id": job_id, "decision": decision, "risk_band": risk})
        )
    if build:
        (root / "state" / "builds" / job_id).mkdir(parents=True, exist_ok=True)


def test_empty_when_no_briefs_dir(tmp_path):
    assert bi.list_briefs(tmp_path) == []
    assert bi.pending_briefs(tmp_path) == []
    assert "No idea briefs" in bi.format_human([])


def test_pending_vs_built(tmp_path):
    _mk(tmp_path, "20260612-0100-alpha", proposed_at="2026-06-12T01:00:00Z", build=False)
    _mk(tmp_path, "20260612-0200-bravo", proposed_at="2026-06-12T02:00:00Z", build=True)
    flags = {b["job_id"]: b["pending"] for b in bi.list_briefs(tmp_path)}
    assert flags == {"20260612-0100-alpha": True, "20260612-0200-bravo": False}
    assert [b["job_id"] for b in bi.pending_briefs(tmp_path)] == ["20260612-0100-alpha"]


def test_newest_first(tmp_path):
    _mk(tmp_path, "20260612-0100-old", proposed_at="2026-06-12T01:00:00Z")
    _mk(tmp_path, "20260612-0900-new", proposed_at="2026-06-12T09:00:00Z")
    assert [b["job_id"] for b in bi.list_briefs(tmp_path)] == [
        "20260612-0900-new", "20260612-0100-old"]


def test_total_on_missing_track(tmp_path):
    (tmp_path / "state" / "digests" / "briefs").mkdir(parents=True)
    (tmp_path / "state" / "digests" / "briefs" / "20260612-0100-ghost.md").write_text("# brief\n")
    out = bi.list_briefs(tmp_path)  # brief with no track artifacts must not raise
    assert len(out) == 1 and out[0]["job_id"] == "20260612-0100-ghost"
    # no track -> no decision -> not approved -> listed but NOT pending
    assert out[0]["proposed_at"] == "" and out[0]["decision"] == "" and out[0]["pending"] is False


def test_total_on_malformed_json(tmp_path):
    _mk(tmp_path, "20260612-0100-bad", proposed_at="2026-06-12T01:00:00Z")
    (tmp_path / "state" / "tracks" / "20260612-0100-bad" / "idea-contract.json").write_text("{not json")
    out = bi.list_briefs(tmp_path)  # corrupt contract must not raise
    assert out[0]["job_id"] == "20260612-0100-bad" and out[0]["proposed_at"] == ""


def test_non_approved_briefs_are_not_pending(tmp_path):
    # _digest can write briefs for rejected / needs_more outcomes too; those are listed under
    # --all but are NEVER pending (pending = approved AND not yet built).
    _mk(tmp_path, "20260612-0100-app", proposed_at="2026-06-12T01:00:00Z", decision="approved_for_coder")
    _mk(tmp_path, "20260612-0200-rej", proposed_at="2026-06-12T02:00:00Z", decision="rejected")
    _mk(tmp_path, "20260612-0300-needs", proposed_at="2026-06-12T03:00:00Z", decision="needs_more")
    assert [b["job_id"] for b in bi.pending_briefs(tmp_path)] == ["20260612-0100-app"]
    assert len(bi.list_briefs(tmp_path)) == 3  # --all still lists every brief
    assert {b["job_id"]: b["approved"] for b in bi.list_briefs(tmp_path)} == {
        "20260612-0100-app": True, "20260612-0200-rej": False, "20260612-0300-needs": False}


def test_non_dict_contract_json_is_blank_not_raised(tmp_path):
    (tmp_path / "state" / "digests" / "briefs").mkdir(parents=True)
    (tmp_path / "state" / "digests" / "briefs" / "20260612-0100-list.md").write_text("# b\n")
    t = tmp_path / "state" / "tracks" / "20260612-0100-list"
    t.mkdir(parents=True)
    (t / "idea-contract.json").write_text(json.dumps(["not", "a", "dict"]))  # JSON list, not dict
    out = bi.list_briefs(tmp_path)
    assert out[0]["proposed_at"] == "" and out[0]["what_should_exist"] == "" and out[0]["pending"] is False


def test_malformed_field_types_do_not_break_sort(tmp_path):
    # A non-string proposed_at across briefs must not make the sort raise (mixed-type compare).
    (tmp_path / "state" / "digests" / "briefs").mkdir(parents=True)
    for jid, pa in [("20260612-0100-a", 12345), ("20260612-0200-b", "2026-06-12T02:00:00Z")]:
        (tmp_path / "state" / "digests" / "briefs" / f"{jid}.md").write_text("# b\n")
        t = tmp_path / "state" / "tracks" / jid
        t.mkdir(parents=True)
        (t / "idea-contract.json").write_text(json.dumps({"proposed_at": pa, "what_should_exist": "x"}))
        (t / "main-review.json").write_text(json.dumps({"decision": "approved_for_coder", "risk_band": "low"}))
    out = bi.list_briefs(tmp_path)  # must not raise
    assert {b["job_id"] for b in out} == {"20260612-0100-a", "20260612-0200-b"}
    assert any(b["proposed_at"] == "" for b in out)  # the int proposed_at was normalized to ""


def test_tie_breaks_on_job_id(tmp_path):
    _mk(tmp_path, "20260612-0100-aaa", proposed_at="2026-06-12T05:00:00Z")
    _mk(tmp_path, "20260612-0100-zzz", proposed_at="2026-06-12T05:00:00Z")
    # equal proposed_at -> stable, deterministic order by job_id (descending under reverse=True)
    assert [b["job_id"] for b in bi.list_briefs(tmp_path)] == [
        "20260612-0100-zzz", "20260612-0100-aaa"]
