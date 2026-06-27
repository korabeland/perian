"""Tests for the orchestrating sweep (U5). Fake spawn + stubbed Codex — never a real model/Codex.
Covers the short-circuits, the fail-closed escalation, the concurrency guard, scratch cleanup, and
the seen-set bookkeeping."""

import fcntl
import json
import os
import time
from pathlib import Path

from tools import improvement_dreamer as idr
from tools import improvement_harvest as ih
from tools import improvement_sweep as sw


def _ev(event, ts, **kw):
    d = {"event": event, "ts": ts}
    d.update(kw)
    return d


def _events_file(tmp_path, events):
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def _paths(tmp_path):
    sm = tmp_path / "system-map.md"
    sm.write_text("# system map\n")
    log = tmp_path / "log.md"
    log.write_text("# improvement log (no certified-signals markers)\n")
    return {
        "scratch_root": tmp_path / "scratch",
        "digest_dir": tmp_path / "digests",
        "seen_path": tmp_path / "seen.json",
        "certified_path": tmp_path / "certified.json",
        "log_path": log,
        "system_map_path": sm,
        "lock_path": tmp_path / ".lock",
    }


def cluster_spawn(cluster_obj, *, ok=True, write=True):
    def spawn(profile, prompt, scratch, *, max_turns, timeout):
        scratch = Path(scratch)
        scratch.mkdir(parents=True, exist_ok=True)
        if write and cluster_obj is not None:
            out = scratch / idr.ARTIFACT_NAME
            out.write_text(json.dumps(cluster_obj))
            t = time.time() + 5
            os.utime(out, (t, t))
        return idr.SpawnResult(ok=ok)

    return spawn


_GOOD_CANDIDATES = {
    "candidates": [
        {"finding": "f", "tag": "universal", "rationale": "r", "proposed_fix": "p",
         "where_it_lands": "w", "confidence": "high"}
    ]
}

DAY = "2026-06-18"
STEM = f"{DAY}-window-14d"  # default 14-day window -> windowed artifact filename base


# --- scenario 1: empty build-day -> full short-circuit -------------------------------------
def test_empty_day_short_circuits(tmp_path):
    p = _paths(tmp_path)
    events = _events_file(tmp_path, [_ev("not-stress", f"{DAY}T09:00:00Z", job_id="A")])
    calls = []

    def spawn(*a, **k):
        calls.append("spawn")
        return idr.SpawnResult(ok=True)

    def codex(prompt):
        calls.append("codex")
        return _GOOD_CANDIDATES

    code = sw.run(DAY, events_path=events, spawn=spawn, codex_runner=codex, **p)
    assert code == 0
    assert calls == []  # neither model nor Codex called
    assert not (p["digest_dir"] / f"{STEM}.md").exists()


# --- scenario 2: all-known -> stop after Tier-2, no Codex ----------------------------------
def test_all_known_stops_before_codex(tmp_path):
    p = _paths(tmp_path)
    events = [_ev("gate-held", f"{DAY}T09:00:00Z", job_id="A"),
              _ev("coach-exhausted", f"{DAY}T10:00:00Z", job_id="B")]
    ids = [ih.signal_id(e) for e in events]
    idr.save_signatures(p["seen_path"], set(ids))  # everything already known
    ef = _events_file(tmp_path, events)
    calls = []

    def spawn(*a, **k):
        calls.append("spawn")  # must NOT run — all-known short-circuits before the spawn
        return idr.SpawnResult(ok=True)

    def codex(prompt):
        calls.append("codex")
        return _GOOD_CANDIDATES

    code = sw.run(DAY, events_path=ef, spawn=spawn, codex_runner=codex, **p)
    assert code == 0
    assert "spawn" not in calls and "codex" not in calls
    assert (p["digest_dir"] / f"{STEM}.md").exists()  # Tier-1 digest still written
    assert not (p["digest_dir"] / f"{STEM}-candidates.md").exists()


# --- scenario 3: novel signal -> full chain -> candidates ----------------------------------
def test_novel_signal_runs_full_chain(tmp_path):
    p = _paths(tmp_path)
    events = [_ev("gate-held", f"{DAY}T09:00:00Z", job_id="A", reason="real bug"),
              _ev("coach-exhausted", f"{DAY}T10:00:00Z", job_id="B", reason="cap")]
    ids = [ih.signal_id(e) for e in events]            # occurrence ids (for the seen-set assertion)
    cids = [ih.class_id(e) for e in events]            # class ids (what the v2 model clusters)
    ef = _events_file(tmp_path, events)
    cluster = {"clusters": [{"label": "novel", "tag": "escalate", "class_ids": cids}]}
    codex_called = []

    def codex(prompt):
        codex_called.append(prompt)
        # v2: a candidate must cite a real source_class_id from this run's classes
        return {"candidates": [{"finding": "f", "tag": "universal", "rationale": "r",
                                "proposed_fix": "p", "where_it_lands": "w", "confidence": "high",
                                "source_class_ids": [cids[0]]}]}

    code = sw.run(DAY, events_path=ef, spawn=cluster_spawn(cluster), codex_runner=codex, **p)
    assert code == 2
    assert (p["digest_dir"] / f"{STEM}.md").exists()                  # Tier-1 (windowed)
    assert (p["digest_dir"] / f"{STEM}-tier2-clusters.md").exists()   # Tier-2
    assert (p["digest_dir"] / f"{STEM}-candidates.md").exists()       # Tier-3
    assert len(codex_called) == 1
    # seen-set updated so an immediate re-run is all-known
    assert set(ids) <= idr.load_signatures(p["seen_path"])
    # ...and seen records ONLY occurrence ids, never class ids (recall stays occurrence-scoped)
    assert not (set(cids) & idr.load_signatures(p["seen_path"]))
    # the hidden ingested sidecar also carries the windowed stem (no un-suffixed collision)
    assert (p["digest_dir"] / f".{STEM}-tier2.ingested.json").exists()
    # scratch cleaned on the clean cycle
    assert not (p["scratch_root"] / f"improvement-sweep-{DAY}").exists()


# --- scenario 4: Tier-2 fail-closed -> escalate, no Tier-3 ---------------------------------
def test_tier2_fail_closed_skips_tier3(tmp_path):
    p = _paths(tmp_path)
    events = [_ev("gate-held", f"{DAY}T09:00:00Z", job_id="A")]
    ef = _events_file(tmp_path, events)
    codex_called = []

    def codex(prompt):
        codex_called.append(prompt)
        return _GOOD_CANDIDATES

    # spawn says not-ok -> Tier-2 fails closed
    code = sw.run(DAY, events_path=ef, spawn=cluster_spawn(None, ok=False, write=False),
                  codex_runner=codex, **p)
    assert code == 1
    assert (p["digest_dir"] / f"{STEM}-tier2-FAILED.md").exists()
    assert not (p["digest_dir"] / f"{STEM}-candidates.md").exists()
    assert codex_called == []                                  # Tier-3 not called
    assert idr.load_signatures(p["seen_path"]) == set()        # seen NOT appended (allow re-run)
    assert (p["scratch_root"] / f"improvement-sweep-{DAY}").exists()  # scratch kept for forensics


# --- scenario 5: concurrency -> outer lock held -> refuse, no double-write ------------------
def test_outer_lock_busy_refuses(tmp_path):
    p = _paths(tmp_path)
    events = [_ev("gate-held", f"{DAY}T09:00:00Z", job_id="A")]
    ef = _events_file(tmp_path, events)
    # hold the outer lock ourselves
    p["lock_path"].parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p["lock_path"]), os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code = sw.run(DAY, events_path=ef, spawn=cluster_spawn(None), codex_runner=lambda x: _GOOD_CANDIDATES, **p)
        assert code == 0                                      # refused (busy), clean exit
        assert not (p["digest_dir"] / f"{STEM}.md").exists()  # no double-write
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- dry-run: Tier-1 only, no model / no Codex ---------------------------------------------
def test_dry_run_tier1_only(tmp_path):
    p = _paths(tmp_path)
    events = [_ev("gate-held", f"{DAY}T09:00:00Z", job_id="A")]
    ef = _events_file(tmp_path, events)
    calls = []
    code = sw.run(DAY, events_path=ef, spawn=lambda *a, **k: calls.append("s"),
                  codex_runner=lambda x: calls.append("c"), dry_run=True, **p)
    assert code == 2
    assert calls == []                                       # no spawn, no Codex
    assert (p["digest_dir"] / f"{STEM}.md").exists()         # but the Tier-1 digest is written


# --- U5: the window spans multiple days ----------------------------------------------------
def test_window_spans_multiple_days(tmp_path):
    p = _paths(tmp_path)
    # one signal 5 days before the window end, one on the end day — both inside the 14-day window
    events = [_ev("gate-held", "2026-06-13T09:00:00Z", job_id="OLD", reason="r1"),
              _ev("coach-exhausted", f"{DAY}T10:00:00Z", job_id="NEW", reason="r2")]
    cids = [ih.class_id(e) for e in events]
    ef = _events_file(tmp_path, events)
    cluster = {"clusters": [{"label": "x", "tag": "escalate", "class_ids": cids}]}

    def codex(prompt):
        return {"candidates": [{"finding": "f", "tag": "universal", "rationale": "r",
                                "proposed_fix": "p", "where_it_lands": "w", "confidence": "high",
                                "source_class_ids": [cids[0]]}]}

    code = sw.run(DAY, events_path=ef, spawn=cluster_spawn(cluster), codex_runner=codex, **p)
    assert code == 2
    text = (p["digest_dir"] / f"{STEM}-tier2-clusters.md").read_text()
    assert "OLD" in text and "NEW" in text  # both days' builds reached the windowed digest


# --- U5 (Codex U4 #1): the sweep threads the advisory already-logged set into Tier-3 -------
def test_sweep_threads_already_logged_to_tier3(tmp_path):
    p = _paths(tmp_path)
    # a certified occurrence + a NEW occurrence of the SAME class (same event+reason, later ts)
    cert = _ev("gate-held", f"{DAY}T09:00:00Z", job_id="A", reason="same failure")
    new = _ev("gate-held", f"{DAY}T10:00:00Z", job_id="B", reason="same failure")
    p["log_path"].write_text(f"<!-- certified-signals: {ih.signal_id(cert)} -->\n")  # certified via LOG
    ef = _events_file(tmp_path, [cert, new])
    new_class = ih.class_id(new)
    captured = {}

    def codex(prompt):
        captured["prompt"] = prompt
        return {"candidates": [{"finding": "f", "tag": "universal", "rationale": "r",
                                "proposed_fix": "p", "where_it_lands": "w", "confidence": "high",
                                "source_class_ids": [new_class]}]}

    cluster = {"clusters": [{"label": "x", "tag": "escalate", "class_ids": [new_class]}]}
    code = sw.run(DAY, events_path=ef, spawn=cluster_spawn(cluster), codex_runner=codex, **p)
    assert code == 2
    # the Tier-3 prompt enumerates the surviving class flagged [already logged] — advisory threaded end-to-end
    line = next(l for l in captured["prompt"].splitlines() if f"class_id={new_class}" in l)
    assert "[already logged]" in line


def _one_candidate(class_id):  # a schema-valid candidate citing one real class
    return {"candidates": [{"finding": "f", "tag": "universal", "rationale": "r", "proposed_fix": "p",
                            "where_it_lands": "w", "confidence": "high", "source_class_ids": [class_id]}]}


def test_nondefault_window_days_in_stem(tmp_path):
    p = _paths(tmp_path)
    events = [_ev("gate-held", f"{DAY}T09:00:00Z", job_id="A", reason="r")]
    cids = [ih.class_id(e) for e in events]
    ef = _events_file(tmp_path, events)
    cluster = {"clusters": [{"label": "x", "tag": "escalate", "class_ids": cids}]}
    code = sw.run(DAY, window_days=7, events_path=ef, spawn=cluster_spawn(cluster),
                  codex_runner=lambda pr: _one_candidate(cids[0]), **p)
    assert code == 2
    assert (p["digest_dir"] / f"{DAY}-window-7d.md").exists()  # stem reflects --window-days 7
    assert not (p["digest_dir"] / f"{STEM}.md").exists()       # not the default 14d stem


def test_tier3_fail_safe_uses_windowed_stem(tmp_path):
    p = _paths(tmp_path)
    events = [_ev("gate-held", f"{DAY}T09:00:00Z", job_id="A", reason="r")]
    cids = [ih.class_id(e) for e in events]
    ef = _events_file(tmp_path, events)
    cluster = {"clusters": [{"label": "x", "tag": "escalate", "class_ids": cids}]}

    def boom(prompt):
        raise RuntimeError("codex down")

    code = sw.run(DAY, events_path=ef, spawn=cluster_spawn(cluster), codex_runner=boom, **p)
    assert code == 1
    assert (p["digest_dir"] / f"{STEM}-tier3-FAILED.md").exists()       # windowed stem on the fail-safe
    assert not (p["digest_dir"] / f"{STEM}-candidates.md").exists()
