"""Parked queue — write, list (unresolved by default), resolve, collision-safe."""

from datetime import datetime, timezone

from scheduler import parked_queue


def _now():
    return datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)


def test_park_writes_entry(tmp_path):
    path = parked_queue.park("aux-watchdog", "timeout", "exceeded 5m",
                             parked_dir=tmp_path, now=_now())
    assert path.exists()
    entries = parked_queue.list_parked(parked_dir=tmp_path)
    assert len(entries) == 1
    assert entries[0]["job_id"] == "aux-watchdog"
    assert entries[0]["status"] == "timeout"
    assert entries[0]["resolved"] is False


def test_resolve_hides_from_default_list(tmp_path):
    parked_queue.park("stall-alert", "failed", "rc=1", parked_dir=tmp_path, now=_now())
    entry_id = parked_queue.list_parked(parked_dir=tmp_path)[0]["id"]
    assert parked_queue.resolve(entry_id, parked_dir=tmp_path) is True
    assert parked_queue.list_parked(parked_dir=tmp_path) == []
    assert len(parked_queue.list_parked(parked_dir=tmp_path, include_resolved=True)) == 1


def test_resolve_unknown_returns_false(tmp_path):
    assert parked_queue.resolve("does-not-exist", parked_dir=tmp_path) is False


def test_same_second_collision_does_not_overwrite(tmp_path):
    parked_queue.park("dream", "error", "a", parked_dir=tmp_path, now=_now())
    parked_queue.park("dream", "error", "b", parked_dir=tmp_path, now=_now())
    assert len(parked_queue.list_parked(parked_dir=tmp_path)) == 2
