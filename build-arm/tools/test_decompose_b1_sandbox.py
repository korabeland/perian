"""Phase 5 — B1: prove Main has NO write path to state/events.jsonl.

The whole decomposition trust spine rests on the criteria_frozen / large_override /
child_artifacts_frozen events being UNFORGEABLE by the doer. That holds for exactly one reason:
the model (Main) runs under the same kernel fs-jail think_sweep spawns it in —
`(deny default)(allow file-write* (subpath "<SCRATCH>"))` via sandbox-exec — and events.jsonl
lives OUTSIDE scratch. So a Main-side append to events.jsonl is denied by the kernel, while the
freeze-wrapper / D9 (sweep-owned code, NOT under the jail) append freely.

This test exercises the REAL think_sweep policy (the same _materialize_policy + sandbox-exec
invocation default_spawn uses) and asserts: a write INSIDE scratch succeeds (positive control —
the model can write its own artifact), and an append to events.jsonl OUTSIDE scratch is DENIED
(the forged event never lands). macOS-only (skips where sandbox-exec is absent).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import think_sweep  # noqa: E402

if shutil.which("sandbox-exec") is None:
    pytest.skip("sandbox-exec not available — macOS-only B1 proof", allow_module_level=True)


def test_main_cannot_write_events_jsonl_under_the_fs_jail(tmp_path: Path):
    # Lay out a realistic tree: a per-run scratch (the only writable region) and a sibling
    # state/events.jsonl OUTSIDE it (the sweep-owned trusted log).
    scratch = (tmp_path / "scratch").resolve()
    scratch.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    events = state / "events.jsonl"
    events.write_text('{"event":"criteria_frozen","ts":"real"}\n')
    original = events.read_text()

    # The EXACT policy default_spawn jails the model with (base profile = what Main gets).
    policy = think_sweep._materialize_policy(scratch, profile="")

    marker = scratch / "ws" / "ran.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    forged = '{"event":"criteria_frozen","parent_idea_id":"p","forged":true}\n'
    # Malicious "Main" body: prove it ran by writing INSIDE scratch (allowed), then TRY to append a
    # forged event OUTSIDE scratch. Swallow the deny so the process exits 0 — the on-disk evidence
    # (events unchanged) is the trust signal, not the exit code.
    prog = (
        f"open({str(marker)!r},'w').write('ran')\n"
        "try:\n"
        f"    open({str(events)!r},'a').write({forged!r})\n"
        "except (PermissionError, OSError):\n"
        "    pass\n"
    )
    proc = subprocess.run(
        ["sandbox-exec", "-f", str(policy), sys.executable, "-c", prog],
        capture_output=True, text=True, timeout=60,
    )

    # 1. It actually ran (the in-scratch write is allowed) — otherwise the deny below is vacuous.
    assert marker.exists(), f"sandboxed body never ran; stderr={proc.stderr[-300:]}"
    # 2. THE trust property: the forged event never landed — events.jsonl is byte-unchanged.
    assert events.read_text() == original, "FS-JAIL BREACH: Main wrote to events.jsonl (B1 broken)"
    assert "forged" not in events.read_text()


def test_positive_control_scratch_write_is_allowed(tmp_path: Path):
    """Sanity: the jail must still let the model write its OWN scratch artifact (else the deny
    test above could pass trivially because ALL writes fail)."""
    scratch = (tmp_path / "scratch").resolve()
    (scratch / "ws").mkdir(parents=True)
    policy = think_sweep._materialize_policy(scratch, profile="")
    target = scratch / "ws" / "artifact.json"
    proc = subprocess.run(
        ["sandbox-exec", "-f", str(policy), sys.executable, "-c",
         f"open({str(target)!r},'w').write('ok')"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"in-scratch write was denied: {proc.stderr[-300:]}"
    assert target.read_text() == "ok"
