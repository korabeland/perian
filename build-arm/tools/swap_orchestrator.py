#!/usr/bin/env python3
"""Model swap-orchestrator for Auto-think/Auto-build (Perian x ATB merge, U2).

Residency is scheduled state on a 48 GB machine: the think-side 35B + aux 9B, OR
a build-side model (QA's Gemma), but not an unbounded set at once. This tool makes
a target profile's model resident, evicting others as needed to fit the memory
budget, then pins it.

Backend = Ollama (U2; was LM Studio `lms`). Residency rides Ollama's native /api
(see tools/ollama_client): warm with keep_alive=-1 to pin, keep_alive=0 to evict,
/api/ps for resident set, /api/tags for disk sizes. Ollama co-resides models
within its own budget (proven: aux-9B + 35B both GPU-resident), so this tool's job
narrows to deterministic eviction policy + pinning on top of that mechanism.

Context length is NOT passed per swap: each model's window is baked into its
Ollama Modelfile (num_ctx) sized >= every profile that uses it, so profile
switches don't force a KV-resize reload. profile_model still returns it for
visibility.

Usage:
  python -m tools.swap_orchestrator <profile> [--keep KEY ...]
                                    [--budget-gb N] [--dry-run] [--json]

Exit codes: 0 = target resident (loaded now or already was) · 2 = load failed or
verify mismatch · 1 = usage / IO error (bad profile, Ollama unreachable, bad JSON).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path

import yaml

from config import paths
from tools import ollama_client
from tools.ollama_client import canonical

# The residency POLICY lives in spine/memory_governor; this MECHANISM consults it.
# build-arm tools run with cwd=build-arm (`cd build-arm; python -m tools.X`) and,
# during builds, under the gateway env — NEITHER puts spine/ on sys.path. pytest
# does (pythonpath=), which would MASK a live ImportError (the U3 daemon-env
# lesson). Self-bootstrap so the governor resolves no matter who spawns this tool.
_SPINE_DIR = Path(__file__).resolve().parents[2] / "spine"
if _SPINE_DIR.is_dir() and str(_SPINE_DIR) not in sys.path:
    sys.path.insert(0, str(_SPINE_DIR))
from memory_governor import residency_policy as governor  # noqa: E402

PROFILES_DIR = paths.hermes_profiles()
GIB = 1024**3


def profile_model(profile: str) -> tuple[str, int | None]:
    """Return (model_key, context_length) from a profile's config.yaml."""
    cfg = PROFILES_DIR / profile / "config.yaml"
    if not cfg.is_file():
        raise FileNotFoundError(f"no profile config at {cfg}")
    model = (yaml.safe_load(cfg.read_text()) or {}).get("model", {})
    key = model.get("default")
    if not key:
        raise ValueError(f"profile '{profile}' has no model.default in {cfg}")
    return key, model.get("context_length")


def default_budget_bytes() -> int:
    """The soft residency budget — from the governor (single source of the ~66%
    factor + the RAM probe)."""
    return governor.budget_bytes()


def plan_swap(
    target_key: str,
    loaded: list[dict],
    disk_sizes: dict[str, int],
    keep: set[str],
    budget_bytes: int,
) -> dict:
    """Pure planner: decide what to evict so the target fits. No side effects.

    `loaded` = /api/ps entries ({'name','size'} where size is the loaded footprint).
    `disk_sizes` = {canonical_name: disk_bytes} from /api/tags (target estimate).
    Projection = everything resident + the target, then evict the largest
    non-`keep` models until it fits the budget.
    """
    loaded_names = {canonical(e.get("name", "")) for e in loaded}
    if target_key in loaded_names:
        return {
            "already_resident": True,
            "to_evict": [],
            "fits": True,
            "projected_bytes": sum(e.get("size", 0) for e in loaded),
        }

    target_size = disk_sizes.get(target_key, 0)
    evictable = sorted(
        (e for e in loaded if canonical(e.get("name", "")) not in keep),
        key=lambda e: e.get("size", 0),
        reverse=True,
    )
    projected = sum(e.get("size", 0) for e in loaded) + target_size
    to_evict: list[str] = []
    for e in evictable:
        if projected <= budget_bytes:
            break
        to_evict.append(canonical(e.get("name", "")))
        projected -= e.get("size", 0)
    return {
        "already_resident": False,
        "to_evict": to_evict,
        "fits": projected <= budget_bytes,
        "projected_bytes": projected,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="swap_orchestrator")
    ap.add_argument("profile", help="target profile (e.g. coder, qa)")
    ap.add_argument("--keep", action="append", default=[],
                    help="model key to leave resident (repeatable)")
    ap.add_argument("--budget-gb", type=float, default=None,
                    help="memory budget for resident models (default ~66%% of RAM)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--json", action="store_true", help="emit a JSON result object")
    args = ap.parse_args(argv)

    try:
        target_key, target_ctx = profile_model(args.profile)
        target_key = canonical(target_key)
        loaded = ollama_client.list_resident()
        disk = {canonical(m.get("name", "")): m.get("size", 0)
                for m in ollama_client.list_available()}
    except (FileNotFoundError, ValueError, OSError, urllib.error.URLError) as exc:
        print(f"[swap] error: {exc}", file=sys.stderr)
        return 1

    budget = int(args.budget_gb * GIB) if args.budget_gb else default_budget_bytes()
    # The keep-set is POLICY, not just the caller's CLI args: the governor's
    # always-resident set (aux-9B) is unioned in, so EVERY swap path (gate_sweep,
    # qa, manual) protects aux by construction — not by arithmetic luck.
    keep = set(governor.always_resident()) | {canonical(k) for k in args.keep}
    plan = plan_swap(target_key, loaded, disk, keep, budget)

    result = {
        "profile": args.profile,
        "target": target_key,
        "context_length": target_ctx,
        "budget_gb": round(budget / GIB, 1),
        **plan,
    }

    if not plan["fits"]:
        print(f"[swap] WARNING: even after evicting all non-kept models, "
              f"{target_key} (+kept) is projected at "
              f"{plan['projected_bytes'] / GIB:.1f} GB > {budget / GIB:.1f} GB budget.",
              file=sys.stderr)

    if args.dry_run:
        result["dry_run"] = True
        if args.json:
            print(json.dumps(result))
        else:
            if plan["already_resident"]:
                verb = "already resident"
            elif not plan["fits"]:
                verb = "would FAIL CLOSED (cannot fit alongside the keep-set)"
            else:
                verb = f"would evict {plan['to_evict'] or '(none)'}, then load"
            print(f"[swap] DRY RUN: {target_key} {verb}")
        return 0

    # Already resident: success is residency itself, not the warm call. Refresh the
    # pin best-effort (so a build-side run can't TTL-evict it) but never let a warm
    # hiccup flip an already-resident target to failure (preserves the no-op contract).
    if plan["already_resident"]:
        try:
            ollama_client.warm(target_key, keep_alive=-1)
        except (urllib.error.URLError, OSError) as exc:
            print(f"[swap] note: {target_key} already resident; pin refresh skipped ({exc})",
                  file=sys.stderr)
        if args.json:
            print(json.dumps(result))
        else:
            print(f"[swap] {target_key} already resident — no-op")
        return 0

    # Fail closed when the target cannot fit alongside the always-resident keep-set
    # under budget: Ollama has no keep-set primitive, so loading an oversized target
    # risks backend-side eviction of the aux (the very thing the keep-set protects).
    # Override with --budget-gb only after verifying real physical room.
    if not plan["fits"]:
        print(f"[swap] FAIL-CLOSED: refusing to load {target_key} — cannot fit with "
              f"the always-resident keep-set under {budget / GIB:.1f} GB budget. "
              f"Pass --budget-gb to override.", file=sys.stderr)
        return 2

    # Trust invariant: never evict an always-resident model. It can't happen (aux
    # is in `keep`, which plan_swap excludes from eviction), but fail CLOSED before
    # touching residency if a future change ever broke that.
    protected = governor.evicts_protected(plan["to_evict"])
    if protected:
        print(f"[swap] FAIL-CLOSED: refusing to evict always-resident "
              f"{', '.join(sorted(protected))}", file=sys.stderr)
        return 2

    for ident in plan["to_evict"]:
        print(f"[swap] unloading {ident}")
        try:
            ollama_client.unload(ident)
        except (urllib.error.URLError, OSError) as exc:
            print(f"[swap] error: failed to unload {ident}: {exc}", file=sys.stderr)
            return 2

    print(f"[swap] loading {target_key} (pinned)")
    try:
        ollama_client.warm(target_key, keep_alive=-1)
    except (urllib.error.URLError, OSError) as exc:
        print(f"[swap] error: failed to load {target_key}: {exc}", file=sys.stderr)
        return 2

    try:
        resident = {canonical(e.get("name", "")) for e in ollama_client.list_resident()}
    except (urllib.error.URLError, OSError) as exc:
        print(f"[swap] error: could not verify residency of {target_key}: {exc}",
              file=sys.stderr)
        return 2
    if target_key not in resident:
        print(f"[swap] error: {target_key} not resident after load (got {resident})",
              file=sys.stderr)
        return 2

    # Aux-survival check: a plan can "fit" on disk-size estimates yet overflow at
    # runtime (real footprint > estimate) and let Ollama evict the always-resident
    # aux. If any always-resident model is gone, re-pin it best-effort and fail
    # closed — the keep-set invariant outranks a "successful" target load.
    missing = set(governor.always_resident()) - resident
    if missing:
        for name in sorted(missing):
            try:
                ollama_client.warm(name, keep_alive=-1)
            except (urllib.error.URLError, OSError):
                pass
        print(f"[swap] error: always-resident {', '.join(sorted(missing))} evicted "
              f"during load of {target_key}; re-pinned and failing closed",
              file=sys.stderr)
        return 2

    result["loaded"] = True
    if args.json:
        print(json.dumps(result))
    else:
        print(f"[swap] OK: {target_key} resident")
    return 0


if __name__ == "__main__":
    sys.exit(main())
