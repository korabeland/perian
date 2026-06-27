#!/bin/bash
# Deterministic contract-chain validator gate — cron entry point (--no-agent --script).
# Runs the no-model schema sweep over every track. Empty stdout + exit 0 = silent
# (cron-friendly); on a real schema FAIL it prints [GATE-FAIL] blocks, appends a
# `validation-failed` event to state/events.jsonl, and exits 2.
# Logic lives in tools/validator_gate.py (tested); this wrapper only fixes cwd so
# the `tools` package imports cleanly.
set -euo pipefail
cd $HOME/.hermes/auto-think-build || exit 0
exec python -m tools.validator_gate --emit-event
