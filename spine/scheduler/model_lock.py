"""The single model-slot lock — shared with the build/QA paths.

PARALLEL=1 (one model-bearing job at a time on the 48GB Mac; think-side and
build-side never co-hold) was historically a manual ritual: "pause the think
crons before you run a build." This makes it an invariant for the scheduler's
jobs by reusing the EXACT same advisory flock the rest of the spine already
contends on -- `build-arm/state/.model.lock`, held by `tools/aux_watchdog.py`
and the QA-shadow/Gemma paths. Using a different lock file would re-introduce the
very race U3 is closing, so this resolves to that one file (a drift-guard test
asserts equality with `tools.aux_watchdog.MODEL_LOCK`).

`flock` is advisory and tied to the open file description, so a non-blocking
`LOCK_EX` from a fresh fd fails whenever any other holder (a build, a QA run, a
manual swap) has it -- which is exactly the signal the scheduler wants: defer the
model job to the next tick rather than co-reside. Full residency authority (the
memory governor that decides the keep-set, not just mutual exclusion) is U4; this
is the bounded U3 piece: mutual exclusion through the established lock.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from memory_governor.residency_policy import MODEL_LOCK as _GOVERNOR_LOCK

# The one shared advisory lock — sourced from the governor, the single residency
# authority (U4). Equal to tools.aux_watchdog.MODEL_LOCK; the scheduler and the
# build/QA paths all contend on this ONE file (a drift-guard test asserts it).
SHARED_MODEL_LOCK = _GOVERNOR_LOCK


@contextmanager
def model_slot(lock_path: Path | None = None):
    """Acquire the shared model slot non-blocking; yield whether we got it.

    Yields True when this process now holds the slot (released on exit), or False
    when another holder has it -- the caller defers its model job rather than
    running concurrently. Never blocks. The lock file is created if absent
    (mirroring aux_watchdog), so a clean checkout works on the first tick.
    """
    path = Path(lock_path) if lock_path is not None else SHARED_MODEL_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
