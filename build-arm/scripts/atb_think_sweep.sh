#!/bin/bash
# Deterministic think-loop sweep (Phase A2). Spawns the local models and owns every write
# the pure think-gates assume. Logic: ~/.hermes/auto-think-build/tools/think_sweep.py.
#   exit 0 = clean cycle OR a recoverable park (dedup / cap / council / needs_more-exhausted)
#   exit 2 = >=1 trust HOLD (prints [THINK-GATE-FAIL] + emits a think-gate-fail event)
#   exit 1 = usage error (no topics configured)
# No `set -e`: must return think_sweep's exit code verbatim (mirrors atb_gate_sweep.sh).
# Flags pass through, e.g.  atb_think_sweep.sh --topic "local LLM serving" --lane new-thing
set -uo pipefail
cd $HOME/.hermes/auto-think-build || exit 1
exec python3 -m tools.think_sweep "$@"
