#!/usr/bin/env python3
"""Aux-model residency watchdog (born of the 2026-06-13 news-digest browser-loop post-mortem).

The Hermes auxiliary stack (context compression, title generation, skills hub,
approval, MCP summaries -- `auxiliary:` in ~/.hermes/config.yaml) runs on a small
model (the aux 9B). If it gets evicted, EVERY long session silently degrades: aux
calls slow/stall, compression falls back to lossy truncation, and an amnesiac
agent can degenerate into an identical-tool-call loop (the 2026-06-13 Saturday
cron). So: keep the aux model RESIDENT, checked deterministically from outside.

Backend = Ollama (U2; was LM Studio `lms`). The aux is pinned with keep_alive=-1
(tools/ollama_client); Ollama co-resides it with the crew 35B within its own
budget. Residency is read from /api/ps; pinning/eviction via keep_alive.

Cron mode (default, every 30m, --no-agent, telegram delivery):
  1. Single-instance guard (flock, non-blocking) -- overlapping runs exit silently.
  2. /api/ps -> canonical resident model names.
  3. Aux resident -> healthy, silent (exit 0, empty stdout).
  4. Any non-think-side model resident, OR the production model lock
     (state/.model.lock, held by QA-shadow/Gemma paths) is taken -> a swap is
     in progress on purpose -> stand down, silent. Never fight a swap.
  5. Otherwise warm + pin the aux (keep_alive=-1), re-verify.
     Heal -> silent (watchdog log only; no Telegram noise).
     Fail -> LOUD stdout (cron delivers it), rate-limited to one alert per 6h
     failure streak so a stuck state can't spam (lesson of known-issue #10);
     state writes are atomic (tmp + os.replace). Streak clears on a healthy run.

Restore mode (--restore, used by scripts/atb_restore_think.sh after build work):
  "Think-side only" semantics, ALL under the model lock (fail LOUD if it -- or
  the single-instance lock -- is held; a manual ritual must never silently
  no-op): evict every non-think-side resident model, THEN run swap_orchestrator
  main (35B), then ensure the aux is pinned. Prints a confirmation.

Exit codes: 0 = healthy / healed / stood down · 1 = infra error or lock held in
--restore · 2 = reload/eviction failed.
State: ~/.hermes/logs/aux_watchdog_state.json · log: ~/.hermes/logs/aux_watchdog.log
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from config import paths
from tools import ollama_client
from tools.ollama_client import canonical

# Residency POLICY (the keep-set, model identities, the shared lock path) is owned
# by spine/memory_governor; the watchdog ENFORCES it but no longer DEFINES it.
# Self-bootstrap spine onto sys.path: this tool runs with cwd=build-arm and is also
# invoked manually (atb_restore_think.sh) — neither guarantees spine/ is importable,
# and pytest's pythonpath would otherwise mask a live ImportError (the U3 lesson).
_SPINE_DIR = Path(__file__).resolve().parents[2] / "spine"
if _SPINE_DIR.is_dir() and str(_SPINE_DIR) not in sys.path:
    sys.path.insert(0, str(_SPINE_DIR))
from memory_governor import residency_policy as governor  # noqa: E402

AUX_MODEL = governor.AUX_MODEL
CREW_MODEL = governor.CREW_MODEL
THINK_MODELS = set(governor.THINK_MODELS)
LOAD_TIMEOUT_S = 180
UNLOAD_TIMEOUT_S = 60
SWAP_TIMEOUT_S = 600  # swap-to-main may load the 20GB 35B
ALERT_COOLDOWN_S = 6 * 3600

ATB_ROOT = Path(__file__).resolve().parent.parent
MODEL_LOCK = governor.MODEL_LOCK  # the one shared lock, owned by the governor
LOGS_DIR = paths.hermes_logs()
INSTANCE_LOCK = LOGS_DIR / "aux_watchdog.lock"
STATE_FILE = LOGS_DIR / "aux_watchdog_state.json"
LOG_FILE = LOGS_DIR / "aux_watchdog.log"

# Ollama (or any inference backend) being unreachable / refusing is infra error.
INFRA_ERRORS = (urllib.error.URLError, OSError, json.JSONDecodeError)


def _try_flock(lockfile: Path) -> int | None:
    """Non-blocking exclusive flock. Returns an open fd (caller keeps it until
    process exit; close releases) or None if another process holds the lock."""
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lockfile), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _log(line: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG_FILE.open("a") as fh:
        fh.write(f"{stamp} {line}\n")


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_FILE)


def _resident_names() -> set[str]:
    """Canonical names of currently-resident models (/api/ps)."""
    return {canonical(e.get("name", "")) for e in ollama_client.list_resident()}


def _load_aux() -> None:
    ollama_client.warm(AUX_MODEL, keep_alive=-1, timeout=LOAD_TIMEOUT_S)
    if AUX_MODEL not in _resident_names():
        raise RuntimeError("warm reported success but aux not resident on re-check")


def _alert(state: dict, message: str) -> int:
    """Emit a delivered (stdout) alert unless one already fired this failure streak."""
    now = time.time()
    last = state.get("last_alert_ts", 0)
    state["fail_streak"] = state.get("fail_streak", 0) + 1
    _log(f"FAIL streak={state['fail_streak']}: {message}")
    if now - last >= ALERT_COOLDOWN_S:
        state["last_alert_ts"] = now
        print(f"[aux-watchdog] {message}")
    _write_state(state)
    return 2


def _restore(resident: set[str]) -> int:
    """Think-side-only residency, all under the model lock: evict foreign models,
    swap-to-main (35B), ensure aux pinned. Fail LOUD."""
    lock_fd = _try_flock(MODEL_LOCK)  # noqa: F841 -- held until process exit
    if lock_fd is None:
        print(
            "[aux-watchdog] model lock held (QA-shadow/Gemma run active?) -- "
            "not touching residency; retry when it finishes."
        )
        return 1
    foreign = sorted(resident - THINK_MODELS)
    for name in foreign:
        try:
            ollama_client.unload(name, timeout=UNLOAD_TIMEOUT_S)
        except INFRA_ERRORS as exc:
            print(f"[aux-watchdog] restore FAILED: could not unload {name}: {exc}")
            return 2
        _log(f"restore: evicted {name}")
    swap = subprocess.run(
        [sys.executable, "-m", "tools.swap_orchestrator", "main", "--keep", AUX_MODEL],
        cwd=ATB_ROOT,
        capture_output=True,
        text=True,
        timeout=SWAP_TIMEOUT_S,
        check=False,
    )
    if swap.stdout.strip():
        print(swap.stdout.strip())
    if swap.returncode != 0:
        print(
            f"[aux-watchdog] restore FAILED: swap-to-main rc={swap.returncode}: "
            f"{swap.stderr.strip()[:300]}"
        )
        return 2
    # decide from FRESH post-swap state -- the swap itself may have evicted models
    try:
        post_swap = _resident_names()
    except INFRA_ERRORS as exc:
        print(f"[aux-watchdog] restore FAILED: residency check after swap failed: {exc}")
        return 2
    if AUX_MODEL not in post_swap:
        try:
            _load_aux()
        except Exception as exc:
            print(f"[aux-watchdog] restore FAILED: aux reload -- {exc}")
            return 2
        _log(f"restore: pinned {AUX_MODEL}")
    try:
        final = _resident_names()
    except INFRA_ERRORS as exc:
        print(f"[aux-watchdog] restore FAILED: final residency check failed: {exc}")
        return 2
    if not THINK_MODELS <= final:
        print(
            f"[aux-watchdog] restore FAILED: expected {', '.join(sorted(THINK_MODELS))} "
            f"resident, got {', '.join(sorted(final)) or 'none'}"
        )
        return 2
    _write_state({})
    print(
        f"[aux-watchdog] think-side restore done: resident = {', '.join(sorted(final))}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--restore",
        action="store_true",
        help="think-side-only residency: evict build-side models, then ensure aux",
    )
    args = parser.parse_args(argv)

    instance_fd = _try_flock(INSTANCE_LOCK)  # noqa: F841 -- held until process exit
    if instance_fd is None:
        if args.restore:  # manual ritual must not silently no-op
            print(
                "[aux-watchdog] another watchdog run is active -- "
                "restore NOT performed; retry in a minute."
            )
            return 1
        return 0  # another watchdog run is active; it owns this cycle

    state = _read_state()
    try:
        resident = _resident_names()
    except INFRA_ERRORS as exc:  # Ollama unreachable / bad JSON -- infra, alert path
        _alert(state, f"residency check failed: {exc}")
        return 1

    if args.restore:
        return _restore(resident)

    if AUX_MODEL in resident:
        if state.get("fail_streak"):
            _log(f"OK after {state['fail_streak']} failed run(s); streak cleared")
        _write_state({})
        return 0

    foreign = sorted(resident - THINK_MODELS)
    if foreign:
        _log(f"stand-down: aux absent but build-side resident ({', '.join(foreign)})")
        return 0
    model_lock_fd = _try_flock(MODEL_LOCK)  # noqa: F841 -- held until process exit
    if model_lock_fd is None:
        _log("stand-down: model lock held (swap/QA run in progress)")
        return 0

    try:
        _load_aux()
    except subprocess.TimeoutExpired:
        return _alert(
            state,
            f"{AUX_MODEL} evicted and reload timed out after {LOAD_TIMEOUT_S}s -- "
            "aux stack (compression/title-gen) is DOWN; long sessions will go amnesiac.",
        )
    except Exception as exc:
        return _alert(
            state,
            f"{AUX_MODEL} evicted and reload FAILED ({exc}) -- "
            "aux stack (compression/title-gen) is DOWN; long sessions will go amnesiac.",
        )

    _log(
        f"healed: pinned {AUX_MODEL} "
        f"(was evicted; resident={', '.join(sorted(resident)) or 'none'})"
    )
    _write_state({})
    return 0


if __name__ == "__main__":
    sys.exit(main())
