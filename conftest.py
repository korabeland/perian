"""Root pytest shim for the merged monorepo.

The ATB suite under ``build-arm/`` was authored to run with the working directory
at the ATB repo root (now ``build-arm/``). A few tests resolve fixtures relative
to the cwd (e.g. ``Path("state/tracks/..")``). Running the session from
``build-arm/`` lets ``python -m pytest`` from the monorepo root reproduce the
suite's native environment exactly — so **no test or spine file is modified**
and ``build-arm/`` stays byte-identical to ATB.

``pythonpath`` and ``testpaths`` are resolved by pytest (against the rootdir)
before this session-scoped fixture runs, so the chdir does not disturb
collection or import paths.
"""

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _run_from_build_arm():
    build_arm = Path(__file__).parent / "build-arm"
    prev = Path.cwd()
    os.chdir(build_arm)
    try:
        yield
    finally:
        os.chdir(prev)
