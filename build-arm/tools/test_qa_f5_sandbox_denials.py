"""F5 — sandbox-denial fixture: real sandbox-exec must DENY 5 escape attempts.

WHY THIS EXISTS (Rev 5 spec, "Seeded fixtures + canary", "The sandbox-exec
policy"):

  > Test writes `open('/tmp/escape', 'w')` → denied.
  > Test calls `subprocess.run(['rm', '-rf', '/tmp/escape'])` → denied.
  > Test calls `subprocess.run(['osascript', ...])` → denied.
  > Test creates named FIFO outside tmp_ws → denied.
  > Test attaches shared memory segment → denied.
  > All five denials are required for F5 to pass.

F5 is qualitatively different from F1-F4. F1-F4 are unit-style routing
fixtures (synthetic Gemma + synthetic Codex; assertions on compute_route).
F5 exercises REAL sandbox-exec via the production `run_pytest_sandboxed`
function — each parametrized case materializes a malicious `test_qa.py` and
asserts the sandbox policy successfully blocked the escape.

THE TRUST PROPERTY F5 PINS

The Rev 5 spec is explicit: sandbox-exec is the load-bearing kernel-enforced
denial layer. The narrow AST allowlist (qa_check_pre.py:C9) is a fast-reject
of obvious cheats; the sandbox is what makes determined SUT mutation
HARMLESS by blocking the filesystem side-effects that would otherwise leak
out of tmp_ws. F5 is the regression alarm: if sandbox-exec's policy
silently drifts (e.g. a future commit broadens `(allow file-write*)` to
make pytest happy on a new test), F5 turns red.

THE ASSERTION CONTRACT

For each parametrized case, F5:
  1. Builds a minimal tmp_ws layout (ws / home / cache / tmp).
  2. Writes a malicious test_qa.py that ATTEMPTS the escape AND writes an
     IN-SANDBOX marker before the attempt.
  3. Runs `setup_canary(case)` — defaults to "ensure absent"; for the
     destructive-rm case it pre-creates a sentinel file the malicious test
     will try to delete.
  4. Calls `run_pytest_sandboxed(tmp_ws)` — the production code path.
  5. Asserts the in-sandbox marker EXISTS after the run — proves the
     malicious test body actually executed (without this, a syntactically
     broken malicious test_qa.py would silently pass F5 by never running
     the escape attempt at all).
  6. Asserts `probe_escape(case)` returns False — the canonical denial
     signal: for creation cases the canary is absent; for the destructive-
     rm case the sentinel is still present.
  7. Cleans up any residue (escape evidence or pre-seeded sentinels).

The Rev 5 spec says: "the CRITICAL assertion is side-effect absence on
disk, NOT just pytest exit." A swallowed exception inside the malicious
test could mask the side-effect from pytest exit code. The canary check is
the unforgeable signal; the marker check is the "we actually ran the
malicious code" complement.

IF A CASE FAILS

Per the Rev 5 spec acceptance criteria:
  > Add measured lookups only if F5 fixtures show pytest legitimately
  > requires them.

But the inverse is also true: if a CURRENT malicious case escapes, that's
NOT a fixture relaxation — it's a policy-tightening commit. The fixture
captures intent; the sandbox policy must implement that intent.

WHAT THIS TEST DELIBERATELY DOES NOT COVER

- The narrow AST allowlist (qa_check_pre.py:C9). F5 is downstream of the
  AST checks; in production the AST would reject most of these patterns
  before pytest runs. F5 bypasses the AST check by going straight to
  `run_pytest_sandboxed`, exercising ONLY the kernel-enforced denial.
- Real Gemma swap (F0a), real Codex call (shadow phase), compute_route
  (F1-F4).
- Side channels other than filesystem (network, environment manipulation).
  F5 v1 covers the spec's enumerated five; expansion is a v2 surface
  (symlink/hardlink write-through, rename/chmod outside tmp_ws, network,
  secret reads).

SKIP CONDITION

This module skips on platforms without `sandbox-exec` (Linux CI, etc.).
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.qa import run_pytest_sandboxed  # noqa: E402

if shutil.which("sandbox-exec") is None:
    pytest.skip(
        "sandbox-exec not available — this is a macOS-only fixture",
        allow_module_level=True,
    )


# Unique session prefix so concurrent test runs / repeated runs don't clobber
# each other's canaries. PID + monotonic-ns is sufficient (no wall-clock
# fabrication — the project rule forbids fake wall-clock timestamps, but
# monotonic_ns is purely a uniqueness counter).
_SESSION = f"f5_{os.getpid()}_{time.monotonic_ns()}"
_TMP = Path("/tmp")


class _Case(NamedTuple):
    """One sandbox-denial test case.

    Attributes:
      label: short kebab name, also used as the canary suffix and pytest id.
      canary: filesystem path the case's escape signal lives at.
      test_source_template: the malicious test_qa.py body. May embed
        `{canary}` (path string) and `{marker}` (marker path string) which
        get format-substituted at runtime.
      setup_canary: pre-test setup, given the case. Default: ensure canary
        is ABSENT. The destructive-rm case overrides this to pre-create a
        sentinel file the malicious test will try to delete.
      probe_escape: returns True if the escape SUCCEEDED. Default:
        `canary.exists()`. The destructive-rm case overrides this to
        `not canary.exists()`. The shared-memory case overrides this to
        attempt an attach-by-name from the outer un-sandboxed test.
    """

    label: str
    canary: Path
    test_source_template: str
    setup_canary: Callable[["_Case"], None]
    probe_escape: Callable[["_Case"], bool]


def _ensure_canary_absent(c: "_Case") -> None:
    """Default setup: make sure the canary doesn't exist before the run."""
    if c.canary.exists() or c.canary.is_symlink():
        # is_file() returns False for FIFOs and other special files, so use
        # unlink() unconditionally for anything that isn't a directory.
        # Codex review #5 round 1 fix.
        if c.canary.is_dir():
            shutil.rmtree(c.canary, ignore_errors=True)
        else:
            try:
                c.canary.unlink()
            except OSError:
                pass


def _ensure_canary_present(c: "_Case") -> None:
    """Destructive-rm setup: pre-seed the canary with a sentinel byte so the
    malicious test has something to try to delete. The escape signal for
    this case is the canary DISAPPEARING.
    """
    _ensure_canary_absent(c)
    c.canary.write_text("sentinel — must survive sandboxed rm")


def _canary_exists(c: "_Case") -> bool:
    return c.canary.exists()


def _canary_missing(c: "_Case") -> bool:
    """For the destructive-rm case: 'escape' means the sentinel got deleted."""
    return not c.canary.exists()


def _direct_write_case() -> _Case:
    """Case (a): direct `open(escape_path, 'w')` in the test body."""
    canary = _TMP / f"{_SESSION}_direct_write.txt"
    src = (
        "import os\n"
        "\n"
        "def test_direct_write_escape():\n"
        "    \"\"\"Sandbox-deny test: attempt direct write outside tmp_ws.\"\"\"\n"
        "    # Marker proves the test body executed (Codex review #4 round 1).\n"
        "    with open({marker!r}, 'w') as f:\n"
        "        f.write('ran')\n"
        "    # PermissionError on a sandbox-denied write is the expected\n"
        "    # behavior. We swallow it so pytest exits 0 here — the OUTER\n"
        "    # test's canary check is the trust-property assertion.\n"
        "    try:\n"
        "        with open({canary!r}, 'w') as f:\n"
        "            f.write('escape')\n"
        "    except (PermissionError, OSError):\n"
        "        pass\n"
    )
    return _Case(
        label="direct-write",
        canary=canary,
        test_source_template=src,
        setup_canary=_ensure_canary_absent,
        probe_escape=_canary_exists,
    )


def _subprocess_rm_case() -> _Case:
    """Case (b): subprocess `rm -rf` an outside-jail sentinel.

    Rev 5 spec wording verbatim: `subprocess.run(['rm', '-rf', '/tmp/escape'])`.
    The setup pre-creates the sentinel; the malicious test attempts to
    delete it via a subprocess. The kernel-level sandbox-exec policy denies
    file-write* (which includes unlink) outside tmp_ws. The child inherits
    the deny-default rule (process-exec is allowed but file-write isn't).
    The denial signal is the sentinel still existing after the run.
    """
    canary = _TMP / f"{_SESSION}_subprocess_rm.sentinel"
    src = (
        "import subprocess\n"
        "\n"
        "def test_subprocess_rm_escape():\n"
        "    \"\"\"Sandbox-deny test: subprocess rm of an outside-jail sentinel.\"\"\"\n"
        "    with open({marker!r}, 'w') as f:\n"
        "        f.write('ran')\n"
        "    try:\n"
        "        subprocess.run(\n"
        "            ['rm', '-rf', {canary!r}],\n"
        "            check=False,\n"
        "            timeout=5,\n"
        "        )\n"
        "    except (PermissionError, OSError, subprocess.SubprocessError):\n"
        "        pass\n"
    )
    return _Case(
        label="subprocess-rm",
        canary=canary,
        test_source_template=src,
        setup_canary=_ensure_canary_present,
        probe_escape=_canary_missing,
    )


def _osascript_case() -> _Case:
    """Case (c): osascript delegation to a shell script that writes outside.

    AppleEvent delegation to osascript can be a sandbox-bypass vector if
    the policy doesn't restrict process-exec narrowly enough. The Rev 5
    policy currently allows unrestricted process-exec; the sandbox still
    holds because the CHILD inherits the kernel-enforced deny on file-write.
    """
    canary = _TMP / f"{_SESSION}_osascript.txt"
    src = (
        "import subprocess\n"
        "\n"
        "def test_osascript_escape():\n"
        "    \"\"\"Sandbox-deny test: osascript delegation to shell write.\"\"\"\n"
        "    with open({marker!r}, 'w') as f:\n"
        "        f.write('ran')\n"
        "    try:\n"
        "        subprocess.run(\n"
        "            [\n"
        "                'osascript', '-e',\n"
        "                'do shell script \"echo escape > ' + {canary!r} + '\"',\n"
        "            ],\n"
        "            check=False,\n"
        "            timeout=5,\n"
        "        )\n"
        "    except (PermissionError, OSError, subprocess.SubprocessError):\n"
        "        pass\n"
    )
    return _Case(
        label="osascript",
        canary=canary,
        test_source_template=src,
        setup_canary=_ensure_canary_absent,
        probe_escape=_canary_exists,
    )


def _mkfifo_case() -> _Case:
    """Case (d): named FIFO creation outside tmp_ws.

    `os.mkfifo` makes a special file via mknod(2)/mkfifo(2). It's still a
    file-write at the policy level; sandbox-exec's deny-default on
    file-write* outside tmp_ws blocks it.
    """
    canary = _TMP / f"{_SESSION}_fifo.fifo"
    src = (
        "import os\n"
        "\n"
        "def test_mkfifo_escape():\n"
        "    \"\"\"Sandbox-deny test: named FIFO outside tmp_ws.\"\"\"\n"
        "    with open({marker!r}, 'w') as f:\n"
        "        f.write('ran')\n"
        "    try:\n"
        "        os.mkfifo({canary!r})\n"
        "    except (PermissionError, OSError):\n"
        "        pass\n"
    )
    return _Case(
        label="mkfifo",
        canary=canary,
        test_source_template=src,
        setup_canary=_ensure_canary_absent,
        probe_escape=_canary_exists,
    )


def _shared_memory_case() -> _Case:
    """Case (e): named POSIX shared-memory segment.

    Shared memory uses shm_open(2) which performs a file-write* against
    POSIX shm's anchor. Sandbox-exec's deny-default on file-write blocks
    it.

    DETECTION TWIST 1: shm segments don't show up as ordinary files in
    /tmp. The probe_escape predicate ATTEMPTS to attach to the segment by
    name from the OUTER (un-sandboxed) test. Codex review #3 round 1:
    narrow the "denied" signal to `FileNotFoundError` ONLY — broader
    OSError can mask probe bugs that incorrectly READ as denial.

    DETECTION TWIST 2 (Codex review #2 round 1): CPython 3.13's
    `multiprocessing.shared_memory.SharedMemory` registers segments with
    the resource_tracker by default (`track=True`). If the malicious
    create escaped, the tracker may unlink the segment when the inside
    pytest process exits — BEFORE the outer probe attempts attach — and
    we'd see FileNotFoundError on attach despite the escape. The fix:
    pass `track=False` so the tracker doesn't auto-clean. If attach
    succeeds, the create escaped; if FileNotFoundError, the create was
    sandbox-blocked.

    macOS POSIX shm names: must start with '/', limited to 31 chars total.
    """
    # Short shm name (max 31 chars incl leading '/'). PID + monotonic_ns
    # base16 keeps it unique without bumping the length over the limit.
    shm_name = f"/f5_{os.getpid() % 10000}_{time.monotonic_ns() & 0xFFFFFF:06x}"
    assert len(shm_name) <= 31, f"shm name too long: {shm_name!r}"

    # Placeholder Path — probe_escape doesn't use it.
    placeholder = _TMP / f"{_SESSION}_shm_placeholder"

    src = (
        "from multiprocessing import shared_memory\n"
        "\n"
        "def test_shared_memory_escape():\n"
        "    \"\"\"Sandbox-deny test: named POSIX shm segment outside tmp_ws.\"\"\"\n"
        "    with open({marker!r}, 'w') as f:\n"
        "        f.write('ran')\n"
        "    try:\n"
        "        shm = shared_memory.SharedMemory(\n"
        "            create=True, size=64, name=" + repr(shm_name) + ",\n"
        "            track=False,  # don't let the resource_tracker clean\n"
        "                          # up before our outer probe runs.\n"
        "        )\n"
        "        shm.close()\n"
        "    except (PermissionError, OSError, FileExistsError):\n"
        "        pass\n"
    )

    def setup(_case: "_Case") -> None:
        """Best-effort pre-clean: try to attach + unlink any stale segment
        from a previous run. FileNotFoundError = clean state already.
        """
        try:
            from multiprocessing import shared_memory

            existing = shared_memory.SharedMemory(name=shm_name)
        except FileNotFoundError:
            return
        except OSError:
            return
        try:
            existing.close()
            existing.unlink()
        except OSError:
            pass

    def probe(_case: "_Case") -> bool:
        """True if the malicious test successfully created the shm segment.

        We attempt to attach (create=False) from the OUTER un-sandboxed
        test. SUCCESS → the inside create worked → sandbox failed.
        FileNotFoundError → sandbox blocked the create.

        Codex review #3 round 1: ONLY FileNotFoundError counts as denial.
        Any other exception (PermissionError, etc.) means we couldn't
        confirm — re-raise so the test fails with a clear diagnostic
        instead of silently passing.
        """
        from multiprocessing import shared_memory

        try:
            existing = shared_memory.SharedMemory(name=shm_name)
        except FileNotFoundError:
            return False
        try:
            existing.close()
            existing.unlink()
        except OSError:
            pass
        return True

    return _Case(
        label="shared-memory",
        canary=placeholder,
        test_source_template=src,
        setup_canary=setup,
        probe_escape=probe,
    )


def _all_cases() -> list[_Case]:
    return [
        _direct_write_case(),
        _subprocess_rm_case(),
        _osascript_case(),
        _mkfifo_case(),
        _shared_memory_case(),
    ]


@pytest.mark.parametrize(
    "case", _all_cases(), ids=lambda c: c.label
)
def test_f5_sandbox_denies_escape(case: _Case, tmp_path: Path) -> None:
    """One sandbox-denial assertion per malicious case.

    Three layers, in order of trust:
      1. pytest_exit == 0 — confirms pytest collected + ran the malicious
         test AND that the test's try/except cleanly swallowed the deny
         exception. A non-zero exit means either the test didn't run, the
         deny raised an unanticipated exception type, or the sandbox setup
         broke. (Codex review #1 round 2 tightened this from `in (0, 1)`.)
      2. marker EXISTS — proves the malicious test body actually executed
         up to (at least) the marker write that precedes the escape
         attempt. Without this, a syntactically-broken malicious test_qa.py
         would silently pass F5 (canary absent because no code ran).
         (Codex review #4 round 1.)
      3. probe_escape returns False — the LOAD-BEARING trust-property
         signal: the side-effect didn't leak out of the jail. For
         creation cases (a/c/d) the canary is absent; for the destructive-
         rm case (b) the pre-seeded sentinel is still present; for the
         shared-memory case (e) the attach-from-outside attempt gets
         FileNotFoundError.

    Layer 3 alone could be defeated by a no-op test (canary never created
    because no code ran). Layers 1+2 close that hole; together the three
    signals are the F5 acceptance contract.
    """
    tmp_ws = tmp_path / "ws_root"
    for sub in ("ws", "home", "cache", "tmp"):
        (tmp_ws / sub).mkdir(parents=True)

    # The marker lives INSIDE the sandbox (in tmp_ws/tmp which is allowed by
    # the policy). Existence after the run proves the test body executed.
    marker = tmp_ws / "tmp" / f"marker_{case.label}.txt"
    marker_in_sandbox = str(marker)
    test_source = case.test_source_template.format(
        canary=str(case.canary), marker=marker_in_sandbox
    )
    (tmp_ws / "ws" / "test_qa.py").write_text(test_source)

    # Per-case setup (default: ensure canary absent; rm case: pre-create sentinel).
    case.setup_canary(case)

    # Pre-condition: probe signals no-escape (i.e. clean starting state).
    assert not case.probe_escape(case), (
        f"F5 setup error: escape signal present BEFORE the run "
        f"({case.label}); something leaked from a prior F5 run."
    )

    try:
        # Exercise the production trust property.
        result = run_pytest_sandboxed(tmp_ws)

        # Defensive: confirm pytest actually collected + ran the malicious
        # test AND swallowed the deny cleanly. pytest exit semantics:
        #   0  = collected, ran, all tests passed (try/except swallowed deny)
        #   1  = collected, ran, at least one test failed (deny raised
        #        through OR the malicious test raised something we didn't
        #        try/except for — e.g. a sandbox-blocked import, or a
        #        deny-exception-class we didn't anticipate)
        #   2  = interrupted
        #   3  = internal error
        #   4  = usage error (e.g. file not found — sandbox setup broken)
        #   5  = no tests collected (malicious test_qa.py was syntax-broken)
        #   -1 = subprocess timeout / sandbox-exec invocation failure
        # Codex review #1 round 2: tighten to == 0. The malicious tests
        # are designed to catch (PermissionError, OSError, ...) — so a
        # clean denial signal should produce exit 0. Accepting exit 1
        # would mask cases where the test raised an unexpected exception
        # type (e.g. a sandbox change made the deny exception something
        # outside our try/except clauses), masking a regression in the
        # signal channel.
        assert result.get("exit") == 0, (
            f"F5 setup error for case {case.label!r}: pytest exit was "
            f"{result.get('exit')}, expected 0 (clean denial swallowed by "
            f"the malicious test's try/except). A non-zero exit means the "
            f"malicious test either didn't run, didn't reach the escape "
            f"line, or raised an unanticipated exception type — possibly "
            f"because the sandbox's denial signal changed. "
            f"stderr_tail={result.get('stderr_excerpt', '')[-300:]}"
        )

        # Codex review #4 round 1: marker existence after the run proves
        # the malicious test body actually executed. Without this, a
        # syntactically-broken test would silently pass F5 (the canary
        # would never exist because no code ran).
        assert marker.exists(), (
            f"F5 setup error for case {case.label!r}: the in-sandbox marker "
            f"{marker} was not written. The malicious test body never ran "
            f"(or the marker write itself was sandbox-denied — a policy "
            f"regression of the allowed-write rule for tmp_ws). exit="
            f"{result.get('exit')}, stderr_tail="
            f"{result.get('stderr_excerpt', '')[-300:]}"
        )

        # Post-condition: the sandbox DENIED the escape attempt.
        escaped = case.probe_escape(case)
    finally:
        # Always clean up the canary (whether escape succeeded or not) so the
        # NEXT run starts clean.
        _ensure_canary_absent(case)

    assert not escaped, (
        f"F5 sandbox FAILED for case {case.label!r}: the malicious "
        f"test_qa.py successfully {'deleted' if case.label == 'subprocess-rm' else 'created'} "
        f"the canary ({case.canary!r}). This is a policy regression — the "
        f"sandbox-exec rules in tools/qa.py:SANDBOX_EXEC_POLICY_TEMPLATE "
        f"no longer block this escape vector. Per the Rev 5 spec: this is "
        f"a policy-tightening commit, NOT a fixture relaxation."
    )
