# Spine periodic scheduler (U3)

One launchd-driven owner for ATB's cron-shaped jobs — replacing the Hermes
gateway crons. It exists to kill three hazards the gateway crons carried:

- **cron-vs-manual race** — a manual sweep and the every-30m cron could fire the
  same work at once.
- **PARALLEL=1 starvation** — nothing enforced "one model-bearing job at a time";
  it was a manual ritual (pause think-side crons before a build).
- **gateway-liveness coupling** — if the gateway died, every periodic job went
  silent (the 2026-06-13 incident).

It is **not** the build dispatcher. The event-driven coder spawn + coach loop stays
in the Hermes gateway — **U14 decided gateway-stays** (see
`spine/dispatcher/README.md`). This scheduler only owns *periodic* fire-and-wait jobs.

## How it works

`launchd` fires `python -m scheduler tick` every 5 minutes. Each tick:

1. reads `schedule.yaml`, finds jobs whose **cadence** is due (vs. their last
   attempt in `state/run-ledger.jsonl`),
2. runs them **one at a time** (a single-instance flock stops overlapping ticks),
3. routes each model-bearing job that doesn't lock itself through the **shared
   model slot** (`build-arm/state/.model.lock` — the same lock the build/QA paths
   hold); if another holder has it, the job is **deferred** to the next tick,
4. records outcomes and **parks** any failure/timeout/error into a reviewable
   queue (`state/parked/`),
5. writes a **heartbeat** (`state/heartbeat.json`).

### Cadence vocabulary
`every:<N>m` · `every:<N>h` · `daily@HH:MM` · `weekly:DAY@HH:MM` (local time).

### The `self_locks` contract
Both current model jobs — `aux-watchdog` and `improvement-feeder` — take
`state/.model.lock` themselves (improvement-feeder's Tier-2 spawn goes through
`think_sweep`'s blocking flock). The scheduler must **not** pre-hold the slot for
a self-locking job, or it would deadlock the job out — so their entries set
`self_locks: true` and the scheduler runs them directly (sequencing still gives
PARALLEL=1, and their own flock serializes them against builds/QA). Only a
*non*-self-locking model job (none today; the general case) is wrapped by the
scheduler's `model_slot`, deferring if another holder has it.

> Full residency authority — a governor that decides the keep-set, not just mutual
> exclusion — is **U4**. This is the bounded U3 piece: one scheduler, one shared
> lock, PARALLEL=1 as an invariant.

## Operating

```
python -m scheduler plan      # what's due right now (dry run)
python -m scheduler status    # heartbeat freshness + last runs + parked count
python -m scheduler parked    # unresolved parked entries
python -m scheduler resolve <id>   # clear one once handled
python -m scheduler tick      # run due jobs now (launchd fires this)
```

## Live migration runbook (the supervised checkpoint)

`schedule.yaml` ships with **every job `enabled: false`**, so loading the
scheduler is inert. Migrate **one job at a time** so the two schedulers never
double-run the same work and the system is never headless:

1. Pre-flight: `python -m scheduler plan` (should show nothing while all jobs are
   off), board idle.
2. `launchctl load -w launchd/com.perian.scheduler.plist` — the tick now runs,
   does nothing yet.
3. Per job, in this order (liveness-critical first): `aux-watchdog`,
   `stall-alert`, `brain-sync`, `dream`, `brain-digest`, `improvement-feeder`.
   Do it as an **immediate supervised handoff** — no "wait and see" gap where the
   job is unowned:
   - **Pause** the matching Hermes gateway cron (so it can't double-fire).
   - Flip the job to `enabled: true` in `schedule.yaml`.
   - **Immediately** run `python -m scheduler tick`, then `python -m scheduler
     status` to confirm it ran clean.
   - **If it failed or the edit was wrong: un-pause the gateway cron right away**
     (restore the old owner), then debug. Do not leave a liveness-critical job
     (especially `aux-watchdog`) without an owner.
   - Only once it's confirmed clean, move to the next job.
4. After all six: leave the gateway crons **paused** (retired), not deleted, until
   a full cycle has run clean — then delete.

| scheduler job | retires gateway cron | live cadence |
|---|---|---|
| aux-watchdog | `Aux model watchdog` (a38910591a23) | every 30m |
| stall-alert | `ATB: Stall alert` (8965043b5155) | every 30m |
| brain-sync | `ATB: Brain Sync` (079d662d5ef9) | every 6h |
| dream | `ATB: Dream` (972fdb791d46) | daily 03:00 |
| brain-digest | `ATB: Brain Digest` (8ba63ae0dd48) | Sat 08:00 |
| improvement-feeder | `ATB: Improvement Feeder` (462ca1029683) | Mon 11:00 |

> `ATB: Validator Gate` and `ATB: Gate Sweep` are **build/gate** crons, not
> periodic jobs — they stay out of this scheduler. The event-driven build dispatch
> stays in the gateway (U14 → gateway-stays; see `spine/dispatcher/README.md`).

### Rollback
`launchctl unload -w launchd/com.perian.scheduler.plist`, then un-pause the
gateway crons. Nothing destructive happens until the gateway crons are deleted in
step 4, which is the last and most reversible-until-then act.
