"""Spine periodic scheduler — the single owner of ATB's cron-shaped jobs.

Perian x ATB merge, U3 (Phase A). The build arm's periodic jobs (aux-watchdog,
stall-alert, brain-sync, dream, brain-digest, improvement-feeder) ran as Hermes
*gateway* crons. That coupled their liveness to the gateway, gave no shared
authority over the single 48GB model slot (the manual "pause think-side crons
before a build" ritual), and raced manual sweeps. This package moves them under
one launchd-driven scheduler that:

  - evaluates an explicit cadence vocabulary (intra-day intervals + time-of-day),
    which the Perian nightly batch (`perian-core/scripts/shadowfax.py`) could not
    express -- it only knows daily/weekly within one nightly window;
  - serializes model-bearing jobs through the SAME `state/.model.lock` the
    build/QA paths already hold, so PARALLEL=1 becomes an invariant instead of a
    ritual (full residency authority lands in U4's memory governor);
  - fails loud: a timed-out / failed / errored job lands in a reviewable parked
    queue rather than vanishing.

Design reuse: the cadence/order/timeout machinery mirrors Shadowfax's proven
`materialize_tonight` / `group_by_model` / `run_task` shapes; the model-lock flock
mirrors `tools/aux_watchdog.py`. External-world paths resolve through
`config.paths` (U13), never hardcoded.
"""
