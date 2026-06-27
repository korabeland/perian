"""Unit tests for swap_orchestrator.plan_swap (pure eviction planner, U2/Ollama).

plan_swap is the trust-relevant decision: given what's resident, what's on disk,
the keep-set, and the budget, decide what to evict so the target fits. No I/O.
"""
import tools.swap_orchestrator as so
from tools.swap_orchestrator import plan_swap

GIB = 1024**3


def _m(name, size):
    return {"name": name, "size": size}


def test_already_resident_is_noop():
    loaded = [_m("qwen3.6-35b-a3b:latest", 25 * GIB), _m("qwen3.5-9b:latest", 8 * GIB)]
    p = plan_swap("qwen3.6-35b-a3b", loaded, {}, {"qwen3.5-9b"}, 31 * GIB)
    assert p["already_resident"] is True
    assert p["to_evict"] == []
    assert p["fits"] is True


def test_fits_without_eviction():
    loaded = [_m("qwen3.5-9b:latest", 8 * GIB)]
    disk = {"gemma-4-26b": 17 * GIB}
    p = plan_swap("gemma-4-26b", loaded, disk, {"qwen3.5-9b"}, 31 * GIB)
    assert p["already_resident"] is False
    assert p["to_evict"] == []  # 8 + 17 = 25 <= 31
    assert p["fits"] is True


def test_evicts_largest_non_keep_to_fit():
    loaded = [_m("qwen3.6-35b-a3b:latest", 25 * GIB), _m("qwen3.5-9b:latest", 8 * GIB)]
    disk = {"gemma-4-26b": 17 * GIB}
    # budget 31: 25+8+17=50 > 31; evict the 35B (largest non-keep) -> 8+17=25 <= 31
    p = plan_swap("gemma-4-26b", loaded, disk, {"qwen3.5-9b"}, 31 * GIB)
    assert p["to_evict"] == ["qwen3.6-35b-a3b"]
    assert p["fits"] is True


def test_keep_protects_from_eviction():
    loaded = [_m("qwen3.5-9b:latest", 8 * GIB)]
    disk = {"gemma-4-26b": 30 * GIB}
    # 8+30=38 > 31, but the only resident model is kept -> nothing evictable -> won't fit
    p = plan_swap("gemma-4-26b", loaded, disk, {"qwen3.5-9b"}, 31 * GIB)
    assert p["to_evict"] == []
    assert p["fits"] is False


def test_canonical_names_match_with_latest_suffix():
    loaded = [_m("qwen3.6-35b-a3b:latest", 25 * GIB)]
    p = plan_swap("qwen3.6-35b-a3b", loaded, {}, set(), 31 * GIB)
    assert p["already_resident"] is True


def test_evicts_multiple_smallest_kept_when_needed():
    loaded = [
        _m("big:latest", 20 * GIB),
        _m("mid:latest", 10 * GIB),
        _m("qwen3.5-9b:latest", 8 * GIB),
    ]
    disk = {"gemma-4-26b": 17 * GIB}
    # budget 31: total+target = 20+10+8+17 = 55; evict big(20)->35, mid(10)->25 <= 31
    p = plan_swap("gemma-4-26b", loaded, disk, {"qwen3.5-9b"}, 31 * GIB)
    assert p["to_evict"] == ["big", "mid"]
    assert p["fits"] is True


def test_main_already_resident_survives_warm_failure(monkeypatch):
    """Codex HOLD #1: an already-resident target stays a success (exit 0) even if
    the best-effort pin-refresh warm call fails."""
    monkeypatch.setattr(so, "profile_model", lambda p: ("qwen3.6-35b-a3b", 120000))
    monkeypatch.setattr(so, "default_budget_bytes", lambda: 31 * GIB)
    monkeypatch.setattr(so.ollama_client, "list_resident",
                        lambda: [{"name": "qwen3.6-35b-a3b:latest", "size": 25 * GIB}])
    monkeypatch.setattr(so.ollama_client, "list_available", lambda: [])

    def boom(*a, **k):
        raise OSError("ollama hiccup")
    monkeypatch.setattr(so.ollama_client, "warm", boom)
    assert so.main(["main"]) == 0


def test_main_verify_failure_returns_2(monkeypatch):
    """Codex HOLD #2: a failed post-load residency verify returns the fail-safe
    exit 2, not an uncaught traceback / exit 1."""
    monkeypatch.setattr(so, "profile_model", lambda p: ("gemma-4-26b", 120000))
    monkeypatch.setattr(so, "default_budget_bytes", lambda: 31 * GIB)
    calls = {"n": 0}

    def list_resident():
        calls["n"] += 1
        if calls["n"] == 1:
            return []                          # initial snapshot: target not resident
        raise OSError("ollama down at verify")
    monkeypatch.setattr(so.ollama_client, "list_resident", list_resident)
    monkeypatch.setattr(so.ollama_client, "list_available",
                        lambda: [{"name": "gemma-4-26b", "size": 17 * GIB}])
    monkeypatch.setattr(so.ollama_client, "warm", lambda *a, **k: None)
    monkeypatch.setattr(so.ollama_client, "unload", lambda *a, **k: None)
    assert so.main(["qa"]) == 2


# --- U4: governor keep-set is honored on EVERY swap path ----------------------

def test_main_keeps_aux_without_explicit_keep_flag(monkeypatch):
    """The U4 fix: a swap with NO --keep (how gate_sweep/qa invoke it) still
    protects the aux-9B, because the governor's always-resident set is unioned
    into the keep-set. The build-incompatible 35B is evicted; the aux is NOT."""
    state = {"resident": [{"name": "qwen3.6-35b-a3b:latest", "size": 21 * GIB},
                          {"name": "qwen3.5-9b:latest", "size": 8 * GIB}]}
    monkeypatch.setattr(so, "profile_model", lambda p: ("gemma-4-26b", 120000))
    monkeypatch.setattr(so, "default_budget_bytes", lambda: 31 * GIB)
    monkeypatch.setattr(so.ollama_client, "list_resident", lambda: list(state["resident"]))
    monkeypatch.setattr(so.ollama_client, "list_available",
                        lambda: [{"name": "gemma-4-26b", "size": 17 * GIB}])
    unloaded = []

    def fake_unload(ident, **k):
        unloaded.append(ident)
        state["resident"] = [m for m in state["resident"]
                             if so.canonical(m["name"]) != ident]
    monkeypatch.setattr(so.ollama_client, "unload", fake_unload)
    monkeypatch.setattr(so.ollama_client, "warm",
                        lambda model, **k: state["resident"].append(
                            {"name": model + ":latest", "size": 17 * GIB}))
    assert so.main(["qa"]) == 0          # 21+8+17=46>31 -> evict 35B -> 8+17=25<=31
    assert "qwen3.6-35b-a3b" in unloaded  # build-incompatible think model evicted
    assert "qwen3.5-9b" not in unloaded   # aux protected by the governor keep-set


def test_main_fails_closed_if_a_plan_would_evict_aux(monkeypatch):
    """Trust invariant: even a (bugged) plan listing an always-resident model for
    eviction is refused BEFORE any unload — exit 2, aux untouched."""
    monkeypatch.setattr(so, "profile_model", lambda p: ("gemma-4-26b", 120000))
    monkeypatch.setattr(so, "default_budget_bytes", lambda: 31 * GIB)
    monkeypatch.setattr(so.ollama_client, "list_resident",
                        lambda: [{"name": "qwen3.5-9b:latest", "size": 8 * GIB}])
    monkeypatch.setattr(so.ollama_client, "list_available",
                        lambda: [{"name": "gemma-4-26b", "size": 17 * GIB}])
    monkeypatch.setattr(so, "plan_swap",
                        lambda *a, **k: {"already_resident": False,
                                         "to_evict": ["qwen3.5-9b"], "fits": True,
                                         "projected_bytes": 0})
    unloaded = []
    monkeypatch.setattr(so.ollama_client, "unload", lambda ident, **k: unloaded.append(ident))
    monkeypatch.setattr(so.ollama_client, "warm", lambda *a, **k: None)
    assert so.main(["qa"]) == 2
    assert unloaded == []  # fail-closed before any eviction


def test_governor_import_resolves_under_daemon_path():
    """The daemon-env guard (U3 lesson): build-arm tools run as `cd build-arm;
    python -m tools.X` with NO PYTHONPATH, so spine/ is not on the default path.
    The self-bootstrap must make the governor importable anyway — proven by
    running in a clean env with cwd=build-arm, NOT under pytest's pythonpath."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    build_arm = Path(so.__file__).resolve().parents[1]  # .../build-arm
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import tools.swap_orchestrator as s; print(s.governor.AUX_MODEL)"],
        cwd=str(build_arm), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "qwen3.5-9b" in proc.stdout


# --- U4 (Codex HOLD): protect aux when a target can't fit / overflows at runtime --

def test_main_fails_closed_when_target_cannot_fit_with_aux(monkeypatch):
    """A target too large to co-reside with the always-resident keep-set under
    budget is REFUSED before any load (no warm) — loading it risks backend-side
    aux eviction. (Codex HOLD #1: previously it warned and loaded anyway.)"""
    monkeypatch.setattr(so, "profile_model", lambda p: ("gemma-4-26b", 120000))
    monkeypatch.setattr(so, "default_budget_bytes", lambda: 31 * GIB)
    monkeypatch.setattr(so.ollama_client, "list_resident",
                        lambda: [{"name": "qwen3.5-9b:latest", "size": 8 * GIB}])
    monkeypatch.setattr(so.ollama_client, "list_available",
                        lambda: [{"name": "gemma-4-26b", "size": 30 * GIB}])  # 8+30>31, aux kept
    warmed = []
    monkeypatch.setattr(so.ollama_client, "warm", lambda m, **k: warmed.append(m))
    monkeypatch.setattr(so.ollama_client, "unload", lambda *a, **k: None)
    assert so.main(["qa"]) == 2
    assert warmed == []  # never attempted the load


def test_main_repins_and_fails_if_aux_evicted_during_load(monkeypatch):
    """A plan can 'fit' on estimates yet overflow at runtime and let Ollama evict
    the aux. The post-load check must catch the missing aux, re-pin it, and fail
    closed. (Codex HOLD #1: verify previously checked only the target.)"""
    state = {"resident": [{"name": "qwen3.6-35b-a3b:latest", "size": 21 * GIB},
                          {"name": "qwen3.5-9b:latest", "size": 8 * GIB}]}
    monkeypatch.setattr(so, "profile_model", lambda p: ("gemma-4-26b", 120000))
    monkeypatch.setattr(so, "default_budget_bytes", lambda: 31 * GIB)
    monkeypatch.setattr(so.ollama_client, "list_resident", lambda: list(state["resident"]))
    monkeypatch.setattr(so.ollama_client, "list_available",
                        lambda: [{"name": "gemma-4-26b", "size": 17 * GIB}])
    monkeypatch.setattr(so.ollama_client, "unload",
                        lambda ident, **k: state.__setitem__(
                            "resident", [m for m in state["resident"]
                                         if so.canonical(m["name"]) != ident]))
    warm_calls = []

    def fake_warm(model, **k):
        warm_calls.append(model)
        if model == "gemma-4-26b":  # simulate Ollama evicting aux to make room
            state["resident"] = [{"name": "gemma-4-26b:latest", "size": 17 * GIB}]
        else:
            state["resident"].append({"name": model + ":latest", "size": 8 * GIB})
    monkeypatch.setattr(so.ollama_client, "warm", fake_warm)
    assert so.main(["qa"]) == 2
    assert "qwen3.5-9b" in warm_calls  # aux was re-pinned before failing closed
