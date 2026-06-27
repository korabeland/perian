#!/bin/bash
# ATB improvement feeder — WINDOWED orchestrating sweep (--no-agent --script cron entry point).
# Tier-1 deterministic windowed harvest -> Tier-2 local-35B suppress+cluster (recurrence classes) ->
# Tier-3 Codex draft. Writes candidate IMPROVEMENT_LOG entries for OVERSEER certification; NEVER
# auto-writes the log. Exit: 0 = clean / nothing to review · 2 = candidates drafted (review them) ·
# 1 = a tier failed (escalation; survivors preserved in a *-FAILED.md artifact).
# Logic lives in tools/improvement_sweep.py (tested); this wrapper only fixes cwd so the `tools`
# package imports cleanly. Mirrors scripts/atb_validator_gate.sh.
#
# Reads a rolling 14-day window ENDING at the PRIOR COMPLETE UTC day. The sweep's window end defaults
# to "today (UTC)", but this cron fires in local time (UTC+10): a morning run sits mid-UTC-day, so an
# end of "today" would silently miss the day's final UTC hours. Pinning the end to yesterday-UTC
# captures whole days with no boundary gap (schedule the cron after 00:00 UTC = 10:00 local). Cadence
# is WEEKLY (0 11 * * 1) — the 14-day window overlaps run-to-run; re-litigation is avoided by
# occurrence-exact suppression, not by dropping. Pass --date / --window-days in the cron args to override.
set -euo pipefail
cd $HOME/.hermes/auto-think-build || exit 0
exec python -m tools.improvement_sweep --emit-event --window-days 14 --date "$(date -u -v-1d +%F)" "$@"
