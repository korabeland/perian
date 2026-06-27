"""F0b — sandbox-positive fixture for the QA layer.

Proves the load-bearing claim from the 2026-06-07 handoff:

  > a one-line passing test_qa.py that runs under the real sandbox-exec
  > policy. Proves pytest can RUN inside the jail.

This is the POSITIVE control. The NEGATIVE control (F5: malicious-test
fixtures the sandbox must DENY) is a separate test module.

What we test
------------
Given a clean workspace with one module under test + one passing test,
`tools.qa.run_pytest_sandboxed(tmp_ws)` must:

  - return exit == 0  (pytest interpreter started, collected, ran, all passed)
  - not emit a sandbox-violation hint on stderr

pytest exit-code semantics make exit==0 sufficient: 0 means all tests
passed; 5 means none collected; non-zero ≥2 means pytest itself errored.
So a clean 0 transitively proves the sandbox didn't block any step.

What we DON'T test here
-----------------------
- Gemma is not invoked. The mock-out of run_gemma_under_model_lock is a
  separate fixture concern (F1 family).
- Codex is not invoked. The route computation (compute_route) is not
  exercised.
- Pre-checks (C1-C12, C15, C18a) are not run; this is purely a sandbox-exec
  passes-legit-code test.
- result["failures"] is not asserted on the success path (the field is
  empty by definition when exit==0). Failure-path parsing is exercised
  separately.

What we DO test about result["nodeids"]
---------------------------------------
The follow-up to the original disclaimer landed: `run_pytest_sandboxed`
now invokes pytest with `-v`, which emits per-test nodeid lines on
success. So a confirmed run must surface at least one nodeid. This
catches a future regression where someone re-quietens the runner (back
to `-q` or `--no-header --no-summary`) and silently empties
`qa.json.collected_count` + the Codex prompt's nodeid context.

Each later F-fixture (F1, F2, F3, F4) builds on this isolation.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

# Use the `tools.qa` import path (matches the rest of the test suite) so we
# don't end up with two module identities for `qa` when the full suite runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.qa import run_pytest_sandboxed  # noqa: E402

# Skip the whole module on platforms without sandbox-exec (Linux CI etc.).
if shutil.which("sandbox-exec") is None:
    pytest.skip(
        "sandbox-exec not available — this is a macOS-only fixture",
        allow_module_level=True,
    )


F0_IDENTITY_PY = """\
def f(x):
    return x
"""

# Single one-line passing test. The import resolves because pytest adds the
# test-file's parent dir to sys.path during collection, and `identity.py` is
# the sibling module.
F0_TEST_QA_PY = """\
from identity import f


def test_identity():
    assert f(1) == 1
"""


def test_f0_sandbox_positive(tmp_path: Path) -> None:
    """Real sandbox-exec policy + clean fixture → pytest exits 0, collects 1.

    Failure modes this catches:
      - sandbox policy denies a syscall pytest needs at import-time
      - sandbox blocks `python3 -m pytest` itself (bad allow rule)
      - tmp_ws path substitution into the policy template is broken
      - `_gemma_env` clobbers env in a way that breaks Python interpreter init
    """
    tmp_ws = tmp_path
    # Mirror production's `make_tmp_ws` layout — _gemma_env (called inside
    # run_pytest_sandboxed) points HOME/XDG_CACHE_HOME/TMPDIR at these dirs.
    # If pytest plugins touch TMPDIR mid-run, missing dirs would cause a
    # spurious sandbox deny that isn't a real policy problem.
    for sub in ("ws", "home", "cache", "tmp"):
        (tmp_ws / sub).mkdir()
    ws = tmp_ws / "ws"
    (ws / "identity.py").write_text(F0_IDENTITY_PY)
    (ws / "test_qa.py").write_text(F0_TEST_QA_PY)

    result = run_pytest_sandboxed(tmp_ws)

    assert result["exit"] == 0, (
        f"sandbox blocked legitimate pytest: exit={result['exit']}, "
        f"stderr={result['stderr_excerpt'][-400:]}, "
        f"stdout={result['stdout_excerpt'][-400:]}"
    )
    # Belt-and-suspenders: stderr shouldn't mention sandbox denials. We don't
    # match on the word "sandbox" alone (it can appear in unrelated python
    # warnings), only on the actionable signal "deny".
    stderr_lower = result["stderr_excerpt"].lower()
    assert "deny" not in stderr_lower, (
        f"sandbox-violation hint on stderr despite exit 0: "
        f"{result['stderr_excerpt'][-400:]}"
    )

    # Nodeid surfacing on the success path. See module docstring: pytest must
    # be invoked with `-v` (or anything that prints per-test lines) so the
    # `"::" in line` parser inside run_pytest_sandboxed returns the real
    # nodeids — not the empty list that `-q` produces on passing runs.
    nodeids = result["nodeids"]
    assert len(nodeids) >= 1, (
        f"expected at least one nodeid on a passing sandbox run, got "
        f"{nodeids!r}; stdout={result['stdout_excerpt'][-400:]}"
    )
    # The fixture writes exactly one test (`test_identity` in test_qa.py), so
    # the captured nodeid must be that one. Anchoring the assertion to the
    # actual nodeid string (not just the count) protects against a future
    # parser drift that captures, e.g., a `[100%]` progress token instead of
    # the real `<path>::<name>` shape.
    assert "test_qa.py::test_identity" in nodeids, (
        f"expected the fixture's nodeid in result['nodeids'], got "
        f"{nodeids!r}"
    )
