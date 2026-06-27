"""Residency-policy authority — keep-set, budget math, and the worst-case proof (U4).

Pure-policy tests: no I/O. They pin the trust invariants the swap mechanism must
honor — aux is never evictable, the budget is the conservative 66%, and the
worst-case co-resident sum fits (the plan's no-OOM proof, on real model sizes).
"""

from memory_governor import residency_policy as gov

GIB = 1024**3

# Live disk sizes captured 2026-06-26 (rounded), used for the no-OOM proof.
AUX = int(8.9 * GIB)        # qwen3.5-9b reserve (7.4 loaded / 8.3 disk, +headroom)
CREW_35B = int(20.8 * GIB)  # qwen3.6-35b-a3b
QA_GEMMA = int(16.8 * GIB)  # gemma-4-26b
RAM_48 = 48 * GIB


# --- identities + keep-set ----------------------------------------------------

def test_aux_is_the_only_always_resident_model():
    assert gov.always_resident() == frozenset({gov.AUX_MODEL})
    assert gov.AUX_MODEL == "qwen3.5-9b"
    assert gov.CREW_MODEL in gov.THINK_MODELS and gov.AUX_MODEL in gov.THINK_MODELS


def test_no_decision_ever_evicts_aux():
    """Scenario 3: aux in any eviction list is flagged (swap fails closed on it)."""
    assert gov.evicts_protected([gov.CREW_MODEL, "gemma-4-26b"]) == set()
    assert gov.evicts_protected([gov.CREW_MODEL, gov.AUX_MODEL]) == {gov.AUX_MODEL}
    assert gov.evicts_protected([]) == set()


def test_evicts_protected_canonicalizes_latest_suffix():
    """A raw ':latest' name must not slip past the guard (Codex U4 finding)."""
    assert gov.evicts_protected(["qwen3.5-9b:latest"]) == {gov.AUX_MODEL}
    assert gov.evicts_protected(["qwen3.6-35b-a3b:latest"]) == set()


# --- budget math --------------------------------------------------------------

def test_budget_is_conservative_66_percent():
    assert gov.budget_bytes(RAM_48) == int(RAM_48 * 0.66)  # 31.7 GB, not 38
    # effective room for the think/build model once aux is reserved.
    assert gov.effective_non_aux_budget_bytes(RAM_48) == gov.budget_bytes(RAM_48) - AUX


def test_total_ram_falls_back_when_sysctl_unavailable(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no sysctl")
    monkeypatch.setattr(gov.subprocess, "run", boom)
    assert gov.total_ram_bytes() == gov.DEFAULT_TOTAL_RAM_BYTES


def test_fits_with_aux_holds_for_real_models_and_fails_for_oversize():
    # Scenario: any single real non-aux model co-resides with aux under budget.
    assert gov.fits_with_aux(CREW_35B, RAM_48) is True   # 20.8 + 8.9 = 29.7 <= 31.7
    assert gov.fits_with_aux(QA_GEMMA, RAM_48) is True    # 16.8 + 8.9 = 25.7 <= 31.7
    # A model too large to co-reside with the (never-dropped) aux is refused.
    assert gov.fits_with_aux(int(25 * GIB), RAM_48) is False  # 25 + 8.9 = 33.9 > 31.7


# --- the no-OOM proof ---------------------------------------------------------

def test_worst_case_fits_soft_budget_and_stays_under_ceiling():
    """The plan's verification: no OOM across the full job mix. Steady state (one
    non-aux model + aux) fits the soft budget; the worst transient (a lagging
    evict overlapping the next load) stays under physical RAM."""
    sizes = {gov.AUX_MODEL: AUX, gov.CREW_MODEL: CREW_35B, "gemma-4-26b": QA_GEMMA}
    r = gov.worst_case_report(sizes, RAM_48)
    assert r["largest_non_aux_bytes"] == CREW_35B
    assert r["fits_soft_budget"] is True                  # 20.8 + 8.9 <= 31.7
    assert r["steady_state_peak_bytes"] <= r["soft_budget_bytes"]
    # transient: 20.8 + 16.8 + 8.9 = 46.5 <= 48 (tight, but ordering keeps it rare)
    assert r["under_hard_ceiling"] is True
    assert r["transient_peak_bytes"] <= r["hard_ceiling_bytes"]


def test_worst_case_flags_a_ceiling_breach():
    """A pathological pair whose transient overlap would exceed RAM is flagged
    (deterministic refusal signal — never silently over-commit 48 GB)."""
    sizes = {gov.AUX_MODEL: AUX, "huge-a": int(30 * GIB), "huge-b": int(20 * GIB)}
    r = gov.worst_case_report(sizes, RAM_48)
    assert r["fits_soft_budget"] is False        # 30 + 8.9 = 38.9 > 31.7
    assert r["under_hard_ceiling"] is False       # 30 + 20 + 8.9 = 58.9 > 48


# --- single lock authority ----------------------------------------------------

def test_model_lock_resolves_to_the_shared_state_file():
    assert gov.MODEL_LOCK.as_posix().endswith("build-arm/state/.model.lock")
