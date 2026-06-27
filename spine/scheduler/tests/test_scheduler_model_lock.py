"""Model-slot lock — acquisition, contention, release, and the shared-file guard."""

import fcntl
import os

from scheduler.model_lock import SHARED_MODEL_LOCK, model_slot


def test_acquires_when_free(tmp_path):
    lock = tmp_path / ".model.lock"
    with model_slot(lock) as got:
        assert got is True


def test_contended_when_already_held(tmp_path):
    lock = tmp_path / ".model.lock"
    with model_slot(lock) as outer:
        assert outer is True
        with model_slot(lock) as inner:
            assert inner is False  # second fd cannot take a held advisory flock


def test_releases_on_exit(tmp_path):
    lock = tmp_path / ".model.lock"
    with model_slot(lock) as got:
        assert got is True
    with model_slot(lock) as got_again:
        assert got_again is True  # freed after the first context exited


def test_defers_to_external_holder(tmp_path):
    """If another process-like holder has the slot, model_slot reports False."""
    lock = tmp_path / ".model.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with model_slot(lock) as got:
            assert got is False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_shared_lock_matches_governor_and_aux_watchdog():
    """The scheduler MUST share the build/QA lock file, or PARALLEL=1 is a lie.
    Single source of truth (U4): the governor owns the path; the scheduler and
    the aux watchdog both resolve to it. All three must agree."""
    from memory_governor.residency_policy import MODEL_LOCK as GOV_LOCK
    from tools.aux_watchdog import MODEL_LOCK as AUX_LOCK
    assert SHARED_MODEL_LOCK == GOV_LOCK == AUX_LOCK
