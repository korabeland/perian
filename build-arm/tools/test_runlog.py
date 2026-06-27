"""Tests for runlog.log_run — covers every acceptance criterion."""

import json
from pathlib import Path

import pytest

from tools.runlog import log_run


# ── helpers ───────────────────────────────────────────────────────

def _read_lines(path: str) -> list[dict]:
    """Read all JSON lines from *path* and return parsed dicts."""
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ── acceptance: log_run signature exists ───────────────────────────

def test_log_run_exists():
    """log_run is importable and callable with the expected signature."""
    assert callable(log_run)


# ── acceptance: one line per call, never overwrites ────────────────

def test_one_line_per_call(tmp_path: Path):
    """N calls → N lines; existing content is preserved."""
    events = str(tmp_path / "events.jsonl")

    # First call
    r1 = log_run(
        events,
        role="user",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=20,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:01Z",
    )

    # Second call
    r2 = log_run(
        events,
        role="assistant",
        model="gpt-4",
        prompt_tokens=30,
        completion_tokens=40,
        started_at="2026-06-03T01:00:00Z",
        ended_at="2026-06-03T01:00:05Z",
    )

    lines = _read_lines(events)
    assert len(lines) == 2, "Two calls must produce exactly two lines"

    # Verify both records are present (order: first call then second)
    assert lines[0]["role"] == "user"
    assert lines[1]["role"] == "assistant"


# ── acceptance: every required key present ────────────────────────

def test_record_has_required_keys(tmp_path: Path):
    """Each appended line contains exactly the required keys."""
    events = str(tmp_path / "events.jsonl")

    log_run(
        events,
        role="user",
        model="claude-sonnet-4",
        prompt_tokens=100,
        completion_tokens=200,
        started_at="2026-06-03T12:00:00Z",
        ended_at="2026-06-03T12:00:02.500Z",
    )

    lines = _read_lines(events)
    required_keys = {
        "type",
        "role",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "wall_ms",
        "swap_occurred",
        "started_at",
        "ended_at",
    }

    assert set(lines[0].keys()) == required_keys, (
        f"Record keys {set(lines[0].keys())} != expected {required_keys}"
    )


# ── acceptance: type == 'run-metrics' ─────────────────────────────

def test_type_field(tmp_path: Path):
    """The 'type' field equals 'run-metrics'."""
    events = str(tmp_path / "events.jsonl")

    log_run(
        events,
        role="system",
        model="llama-3",
        prompt_tokens=5,
        completion_tokens=15,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:01Z",
    )

    lines = _read_lines(events)
    assert lines[0]["type"] == "run-metrics"


# ── acceptance: wall_ms computed correctly ────────────────────────

def test_wall_ms_integer(tmp_path: Path):
    """wall_ms is an integer count of milliseconds from started/ended."""
    events = str(tmp_path / "events.jsonl")

    # 1.5 seconds → 1500 ms
    log_run(
        events,
        role="user",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=20,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:01.500Z",
    )

    lines = _read_lines(events)
    assert lines[0]["wall_ms"] == 1500

    # Zero-duration run
    events2 = str(tmp_path / "events2.jsonl")
    log_run(
        events2,
        role="user",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=20,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:00Z",
    )

    lines2 = _read_lines(events2)
    assert lines2[0]["wall_ms"] == 0

    # Fractional milliseconds (floor to int)
    events3 = str(tmp_path / "events3.jsonl")
    log_run(
        events3,
        role="user",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=20,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:01.999Z",
    )

    lines3 = _read_lines(events3)
    assert lines3[0]["wall_ms"] == 1999


# ── acceptance: swap_occurred defaults False, round-trips True ────

def test_swap_occurred_default_false(tmp_path: Path):
    """swap_occurred defaults to False when not passed."""
    events = str(tmp_path / "events.jsonl")

    log_run(
        events,
        role="user",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=20,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:01Z",
    )

    lines = _read_lines(events)
    assert lines[0]["swap_occurred"] is False


def test_swap_occurred_true(tmp_path: Path):
    """swap_occurred round-trips True when passed."""
    events = str(tmp_path / "events.jsonl")

    log_run(
        events,
        role="user",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=20,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:01Z",
        swap_occurred=True,
    )

    lines = _read_lines(events)
    assert lines[0]["swap_occurred"] is True


# ── acceptance: returns the same record dict it appends ───────────

def test_returns_record(tmp_path: Path):
    """log_run returns the same dict it appends."""
    events = str(tmp_path / "events.jsonl")

    result = log_run(
        events,
        role="assistant",
        model="gpt-4o",
        prompt_tokens=50,
        completion_tokens=100,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:10Z",
    )

    lines = _read_lines(events)
    assert result == lines[0]


# ── acceptance: raises ValueError when ended_at < started_at ─────

def test_value_error_on_negative_wall(tmp_path: Path):
    """log_run raises ValueError when ended_at is earlier than started_at."""
    events = str(tmp_path / "events.jsonl")

    with pytest.raises(ValueError, match="ended_at.*earlier than started_at"):
        log_run(
            events,
            role="user",
            model="gpt-4",
            prompt_tokens=10,
            completion_tokens=20,
            started_at="2026-06-03T01:00:00Z",
            ended_at="2026-06-03T00:00:00Z",
        )

    # Verify nothing was written (file should not exist or be empty)
    assert not Path(events).exists() or Path(events).read_text().strip() == ""


# ── acceptance: file is valid JSON-lines (one object per line) ───

def test_valid_json_lines(tmp_path: Path):
    """Each appended line is valid JSON containing the expected keys."""
    events = str(tmp_path / "events.jsonl")

    log_run(
        events,
        role="user",
        model="gpt-4",
        prompt_tokens=10,
        completion_tokens=20,
        started_at="2026-06-03T00:00:00Z",
        ended_at="2026-06-03T00:00:01Z",
    )

    # Re-read raw file and verify each line parses as JSON
    with open(events, "r", encoding="utf-8") as fh:
        for line in fh:
            obj = json.loads(line)  # raises if invalid JSON
            assert isinstance(obj, dict)
