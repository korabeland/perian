"""Targeted tests for the wiring in gate_sweep that finding #7 closed.

The full gate_sweep is tested end-to-end by the ATB build pipeline; this file
only locks down the load-bearing call shape between ``process_qa_layer_pre_gate``
and the QA layer:

  1. ``_collect_coder_nodeids`` runs BEFORE ``run_qa`` (finding #7 closure).
  2. The collected nodeids are passed through as ``coder_test_nodeids=`` so
     C3 has a real expected-set.
  3. A non-None ``collect_reason`` emits a ``qa-layer-collect-warn`` event so
     the operator can grep for collection failures.

Without this wiring, every non-empty ``coder_test_evidence`` array trips C3
and the QA layer holds every real build forever — caught in shadow phase 11.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def qa_layer_env(monkeypatch):
    """Enable the opt-in qa-layer env flag for the duration of one test."""
    monkeypatch.setenv("ATB_QA_LAYER", "1")
    yield


@pytest.fixture
def captured_run_qa(monkeypatch):
    """Replace ``tools.qa.run_qa`` with a recorder + a stub QARun result."""
    captured: dict[str, object] = {}

    from tools.qa import QARun

    def _stub_run_qa(
        track_dir,
        canonical_ws,
        state_dir,
        coder_test_nodeids=None,
        workspace_module_names=None,
        pytest_timeout=300,
    ):
        captured["track_dir"] = track_dir
        captured["canonical_ws"] = canonical_ws
        captured["state_dir"] = state_dir
        captured["coder_test_nodeids"] = list(coder_test_nodeids or [])
        return QARun(
            track_id=Path(track_dir).name,
            computed_route="confirm",
            failure_reason="",
            canonical_hash_pre="",
            canonical_hash_post="",
            qa_json_path=None,
            qa_strategy_path=None,
            qa_route_path=None,
            elapsed_seconds=0.0,
        )

    monkeypatch.setattr("tools.qa.run_qa", _stub_run_qa)
    return captured


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every ``emit_event`` call in gate_sweep."""
    events: list[dict] = []

    def _stub_emit(payload):
        events.append(payload)

    monkeypatch.setattr("tools.gate_sweep.emit_event", _stub_emit)
    return events


@pytest.fixture
def silent_kanban(monkeypatch):
    """Stop the kanban subprocess calls from running."""
    import subprocess

    class _Stub:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "tools.gate_sweep._kanban",
        lambda *args, **kwargs: _Stub(),
    )
    monkeypatch.setattr(
        "tools.gate_sweep._kanban_json",
        lambda *args, **kwargs: [],
    )
    # Suppress incidental writes from any non-mocked subprocess.run reachable
    # via the helper modules.
    yield


def test_process_qa_layer_pre_gate_passes_collected_nodeids_through(
    tmp_path, qa_layer_env, captured_run_qa, captured_events, silent_kanban, monkeypatch
):
    """The wiring finding #7 closes: helper output reaches run_qa intact.

    Without this, ``coder_test_nodeids`` defaults to ``[]`` and C3 trips on
    any non-empty ``coder_test_evidence`` Gemma writes.
    """
    fake_nodeids = [
        "test_alpha.py::test_one",
        "test_alpha.py::test_two[42]",
        "tests/test_nested.py::TestThing::test_bar",
    ]

    def _stub_collect(canonical_ws, timeout_seconds=60):
        return list(fake_nodeids), None

    monkeypatch.setattr("tools.qa._collect_coder_nodeids", _stub_collect)

    # Minimal track + worktree dirs (the stubbed run_qa never touches them).
    track = tmp_path / "tracks" / "fake-job"
    track.mkdir(parents=True)
    worktree = tmp_path / "builds" / "fake-job"
    worktree.mkdir(parents=True)

    from tools.gate_sweep import process_qa_layer_pre_gate

    outcome = process_qa_layer_pre_gate(
        "auto-build",
        "fake-job",
        str(track),
        str(worktree),
        dry_run=False,
        emit=True,
    )

    assert outcome == "pass"  # stubbed run_qa returned route=confirm
    assert captured_run_qa["coder_test_nodeids"] == fake_nodeids
    # No collect-warn event when reason is None.
    warn_events = [e for e in captured_events if e.get("event") == "qa-layer-collect-warn"]
    assert warn_events == []


def test_process_qa_layer_pre_gate_emits_warn_on_collection_failure(
    tmp_path, qa_layer_env, captured_run_qa, captured_events, silent_kanban, monkeypatch
):
    """A collect failure still lets the layer run (fail-closed via C3) AND
    emits a diagnostic event so the operator can find it in events.jsonl."""

    def _stub_collect(canonical_ws, timeout_seconds=60):
        return [], "pytest collect-only failed (exit 2): import error"

    monkeypatch.setattr("tools.qa._collect_coder_nodeids", _stub_collect)

    track = tmp_path / "tracks" / "fake-job"
    track.mkdir(parents=True)
    worktree = tmp_path / "builds" / "fake-job"
    worktree.mkdir(parents=True)

    from tools.gate_sweep import process_qa_layer_pre_gate

    process_qa_layer_pre_gate(
        "auto-build",
        "fake-job",
        str(track),
        str(worktree),
        dry_run=False,
        emit=True,
    )

    # run_qa still called, with the empty list propagated.
    assert captured_run_qa["coder_test_nodeids"] == []

    warn_events = [e for e in captured_events if e.get("event") == "qa-layer-collect-warn"]
    assert len(warn_events) == 1
    assert "import error" in warn_events[0]["reason"]
    assert warn_events[0]["nodeid_count"] == 0


def test_process_qa_layer_pre_gate_disabled_skips_collection(
    tmp_path, captured_run_qa, captured_events, silent_kanban, monkeypatch
):
    """When ATB_QA_LAYER is not "1", the layer must short-circuit BEFORE
    running ``_collect_coder_nodeids`` — collection over a real canonical
    workspace is non-trivial work, so the env-gate must run first.
    """
    monkeypatch.delenv("ATB_QA_LAYER", raising=False)

    collect_calls = {"n": 0}

    def _stub_collect(canonical_ws, timeout_seconds=60):
        collect_calls["n"] += 1
        return [], None

    monkeypatch.setattr("tools.qa._collect_coder_nodeids", _stub_collect)

    track = tmp_path / "tracks" / "fake-job"
    track.mkdir(parents=True)
    worktree = tmp_path / "builds" / "fake-job"
    worktree.mkdir(parents=True)

    from tools.gate_sweep import process_qa_layer_pre_gate

    outcome = process_qa_layer_pre_gate(
        "auto-build",
        "fake-job",
        str(track),
        str(worktree),
        dry_run=False,
        emit=True,
    )

    assert outcome is None
    assert collect_calls["n"] == 0
    assert "coder_test_nodeids" not in captured_run_qa
