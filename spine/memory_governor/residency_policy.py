#!/usr/bin/env python3
"""Residency policy — the single authority for what stays loaded (U4).

On a 48 GB Mac the resident model set is scheduled state: the think-side crew 35B
+ aux 9B, OR a build-side model (QA's Gemma) + aux 9B, but never an unbounded mix.
This module is POLICY over U2's MECHANISM: `tools/swap_orchestrator` (via
`tools/ollama_client`) loads/pins/evicts; this module decides the *keep-set* and
the *budget* every such decision must honor. It is pure — no I/O, no mutation.

THE BUG THIS CLOSES
-------------------
Before U4 the aux-9B keep-set was a per-CALLER argument: `aux_watchdog` passed
`--keep qwen3.5-9b`, but `gate_sweep` and `qa` swapped with NO `--keep`, leaving
the always-on aux model in the *evictable* set during a build/QA swap. It survived
only because evicting the larger 35B happened to free enough memory first —
arithmetic luck, the exact failure behind the 2026-06-13 aux-eviction amnesia
incident (compression dies -> agent goes amnesiac -> identical-tool-call loop),
caught today only by a reactive 30-min watchdog. Making the keep-set POLICY —
sourced HERE and honored by every swap — protects aux by construction, not luck.

SCOPE (U4 = "policy brain", Korab 2026-06-26)
---------------------------------------------
This module is the single source of truth for the residency facts; the existing
mechanism and the watchdog consult it. Making a long-running governor PROCESS the
sole OS-level mutator (ripping the `.model.lock` out of the build/QA paths) is
deliberately deferred to when Perian's lock-free Shadowfax discovery actually
lands (Phase C). Until then PARALLEL=1 via the one shared lock (U3) already
serializes every mutator, so there is no race to close yet.

Budget math (live numbers, 2026-06-26): 48 GB total, soft budget 31.7 GB (66%);
aux 7.4 GB loaded / 8.3 GB disk (reserve 8.9 GB for KV growth); crew 35B 20.8 GB;
QA Gemma 16.8 GB. Worst steady state = one non-aux model + aux = 20.8 + 8.9 =
29.7 <= 31.7; PARALLEL=1 means only one non-aux model is ever resident, so no OOM.
"""
from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

# residency_policy.py -> memory_governor -> spine -> <monorepo root>
_MONOREPO_ROOT = Path(__file__).resolve().parents[2]

GIB = 1024**3

# --- Model identities (single source of truth; consumers import these) --------
AUX_MODEL = "qwen3.5-9b"        # always-resident aux stack (compression/title-gen/...)
CREW_MODEL = "qwen3.6-35b-a3b"  # think-side crew model
# Think-side steady state: crew + aux co-resident.
THINK_MODELS = frozenset({CREW_MODEL, AUX_MODEL})

# --- The one shared model-slot lock (PARALLEL=1) ------------------------------
# Every residency mutator (build, QA, aux-watchdog) and the U3 scheduler contend
# on THIS file; the governor owns its canonical path so there is one lock, not a
# divergent set. Must equal what tools.aux_watchdog and scheduler.model_lock
# resolve (a drift-guard test asserts all three agree).
MODEL_LOCK = _MONOREPO_ROOT / "build-arm" / "state" / ".model.lock"

# --- Budget -------------------------------------------------------------------
# Fraction of unified memory safe for model residency: ATB's conservative ~66%
# (= 31.7 GB of 48), NOT Perian's 38 GB (38 + 8.9 aux overflows 48).
BUDGET_FACTOR = 0.66
# Conservative reserve for the always-resident aux (live 7.4 GB loaded / 8.3 GB
# disk; 8.9 GB leaves headroom for its 80k-ctx KV). Used for the STATIC worst-case
# proof + the effective-budget framing; the LIVE planner uses real /api/ps sizes.
AUX_RESERVE_BYTES = int(8.9 * GIB)
DEFAULT_TOTAL_RAM_BYTES = 48 * GIB  # fallback if sysctl is unavailable


# --- Keep-set policy ----------------------------------------------------------

def always_resident() -> frozenset[str]:
    """Models the governor will NEVER evict — the aux-9B keep-set Ollama has no
    native primitive for. Sourcing this HERE (not from each caller's CLI args) is
    what protects aux across every swap path."""
    return frozenset({AUX_MODEL})


def _canonical(name: str) -> str:
    """Strip Ollama's implicit ':latest' so 'qwen3.5-9b' and 'qwen3.5-9b:latest'
    compare equal. Mirrors tools.ollama_client.canonical, inlined to keep this
    policy module free of any build-arm import (the dependency direction is
    build-arm -> spine, never the reverse)."""
    return name[: -len(":latest")] if name.endswith(":latest") else name


def evicts_protected(to_evict: Iterable[str]) -> set[str]:
    """Any always-resident models a proposed eviction list would remove. MUST be
    empty: swap_orchestrator fails closed if it isn't, so 'no decision ever evicts
    aux-9B' is a runtime invariant, not just a convention. Inputs are canonicalized
    so a raw ':latest' name can't slip past the guard (Codex U4)."""
    return set(always_resident()) & {_canonical(n) for n in to_evict}


# --- Budget math --------------------------------------------------------------

def total_ram_bytes() -> int:
    """Physical RAM via sysctl; falls back to the target machine's 48 GB."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return int(out)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return DEFAULT_TOTAL_RAM_BYTES


def budget_bytes(total_ram: int | None = None) -> int:
    """Soft budget for ALL resident models together (aux included). This is the
    number the planner compares the projected resident sum against."""
    total = total_ram if total_ram is not None else total_ram_bytes()
    return int(total * BUDGET_FACTOR)


def effective_non_aux_budget_bytes(total_ram: int | None = None) -> int:
    """Room left for the think/build model once the always-resident aux is
    reserved: soft budget - aux reserve. The framing for the worst-case proof."""
    return budget_bytes(total_ram) - AUX_RESERVE_BYTES


def fits_with_aux(model_bytes: int, total_ram: int | None = None) -> bool:
    """True if a single non-aux model of `model_bytes` co-resides with the aux
    reserve within the soft budget."""
    return model_bytes + AUX_RESERVE_BYTES <= budget_bytes(total_ram)


def worst_case_report(model_sizes: dict[str, int], total_ram: int | None = None) -> dict:
    """The budget proof. With PARALLEL=1 at most ONE non-aux model is resident
    beside the always-resident aux, so the steady-state peak is (largest non-aux +
    aux): it must fit the soft budget. The worst *transient* (an evict not yet
    freed as the next model loads = largest + 2nd-largest + aux) must stay under
    the hard RAM ceiling. Returns both bounds + the inputs, so the proof is a
    checkable artifact rather than a comment."""
    ceiling = total_ram if total_ram is not None else total_ram_bytes()
    non_aux = sorted(
        (sz for name, sz in model_sizes.items() if name != AUX_MODEL), reverse=True
    )
    largest = non_aux[0] if non_aux else 0
    second = non_aux[1] if len(non_aux) > 1 else 0
    steady_peak = largest + AUX_RESERVE_BYTES
    transient_peak = largest + second + AUX_RESERVE_BYTES
    return {
        "soft_budget_bytes": budget_bytes(total_ram),
        "hard_ceiling_bytes": ceiling,
        "aux_reserve_bytes": AUX_RESERVE_BYTES,
        "largest_non_aux_bytes": largest,
        "steady_state_peak_bytes": steady_peak,
        "transient_peak_bytes": transient_peak,
        "fits_soft_budget": steady_peak <= budget_bytes(total_ram),
        "under_hard_ceiling": transient_peak <= ceiling,
    }
