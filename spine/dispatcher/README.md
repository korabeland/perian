# Build dispatcher — decision record (U14)

**Status:** Decided 2026-06-26 — **gateway-stays** (option a). Korab's call, after a
grounded recommendation. This directory is **intentionally a stub**: the event-driven
build dispatcher is **not** rebuilt as a spine component today. This file records why,
and the trigger to revisit.

## The question

After U3 moved the six *periodic* jobs onto the launchd scheduler, the **last** piece of
the build flow still living inside the Hermes gateway is the **event-driven half**:

- the **kanban dispatcher** (inside the gateway daemon) spawns the **coder worker**
  ~60s after a gate clears, and
- the **coach loop** re-spawns the coder on a diff-gate HOLD, up to `COACH_CAP` (5).

Perian's Shadowfax is a fire-and-wait nightly batch and cannot express this multi-round,
board-reactive flow (plan-gate → coder → diff-gate → coach re-spawn ×5 → qa-gate). So U14
had to decide who owns it.

## How dispatch works today (the ground truth)

The build flow is a **two-part split** — verified in code, not recalled:

1. **The Hermes in-gateway kanban dispatcher** does the actual *spawning*. When a card
   auto-promotes (its parents are `done` and it's unblocked), the dispatcher spawns the
   worker (~60s). It also **reaps** stuck workers: it times a card out past its
   `--max-runtime` and **auto-blocks** it after `--failure-limit` consecutive
   `timed_out`/`crashed`/`spawn_failed` attempts (default 2).
   *(Source: `build-arm/tools/gate_sweep.py` + `build-arm/tools/stall_alert.py` docstrings.)*

2. **The gate sweep** (`build-arm/tools/gate_sweep.py`, via
   `~/.hermes/scripts/atb_gate_sweep.sh` — a deterministic *script*, run by cron **or**
   manually) owns the **gate** cards. It runs each Codex/deterministic gate and, on pass,
   unblocks+completes the gate so the next card auto-promotes → the dispatcher spawns it.
   On a diff-gate HOLD it drives the **coach loop** (Codex-as-teacher → re-spawn coder).

So the spine already triggers the *gates*; the gateway only does the event-driven *spawn*.

## The decision: gateway-stays (option a)

Keep the Hermes in-gateway dispatcher as the build executor. Record the choice; do **not**
build `spine/dispatcher/` now.

### Why (not option b — a new spine watcher)

- **Builds are supervised + hand-kicked today.** The Gate Sweep cron is OFF; builds are
  run via *manual* sweeps with a human in the loop. Option (b)'s only real payoff —
  gateway-independent **unattended** dispatch — buys nothing while a human is present.
- **Option (b) is a substantial new trust-adjacent component** (board-watch + async spawn
  + the coach loop, which is trust-spine behavior) for **zero current benefit**. That is
  exactly the speculative infrastructure the operating thesis says not to build.
- **The liveness gap is mostly already covered.** The dispatcher reaps + auto-blocks stuck
  workers, and **stall-alert** (`build-arm/tools/stall_alert.py`, now owned by the U3
  launchd scheduler, every 30m, **gateway-independent**) is the LOUD layer: it alerts on
  new `blocked` cards and `gate-held`/`gate-error`/`verification-failed` events.
- **Nothing downstream is blocked.** U4 (memory governor) depends on U2 + U3, not U14.
- **The decision is cheap and reversible; the *build* is the expensive, irreversible
  part.** Build the watcher when there is a real unattended-dispatch latency need to size
  it against — not speculatively.

## Consequences

- **R1 ("one scheduler owns all jobs") is explicitly QUALIFIED.** The gateway remains a
  second dispatcher, for build-worker spawn only. This is an accepted outcome named in the
  plan (U14), not a regression. The U3 scheduler owns all *periodic* jobs; the gateway
  owns *event-driven build spawn*.
- **Gateway-down behavior (the accepted residual risk):** if the gateway *process* itself
  is dead mid-build, the dispatcher isn't running to spawn or reap, so a promoted card sits
  un-spawned. Because such a card is *pending* (not `blocked`) and emits no gate event,
  **stall-alert does not catch this specific case by itself.** Accepted today because
  builds are supervised; the scheduler's own heartbeat is gateway-independent. Adding a
  gateway-liveness check is the natural first step *if/when* builds go unattended (see
  revisit trigger). Worker-level failures (the common case) *are* covered: reap →
  auto-block → stall-alert.

## Verification (how U14's acceptance is met)

- A cleared plan-gate spawns the coder at today's latency (~60s) — unchanged.
- A diff-gate HOLD triggers a coach re-spawn up to `COACH_CAP`, identically to today —
  unchanged (the coach loop is untouched).
- The R1 scope qualification and the gateway-down residual are recorded (this file).

No code changed, so there is no logic-invariance surface to Codex-review; the deliverable
is this decision artifact plus the doc-note update in `spine/scheduler/README.md`.

## Revisit trigger → rebuild as option (b)

Promote this stub to a real `spine/dispatcher/` watcher when **either**:

1. **Autonomy is armed** — builds run unattended, so "survives gateway death" stops being
   theoretical, or
2. a **real dispatch-latency need** appears that the gateway's poll cadence can't meet.

At that point, option (b) is: a long-running watcher (board-watch + async worker spawn +
coach re-spawn) replacing the gateway's role for ATB builds, built as trust-adjacent and
Codex-reviewed — the irreversible, high-blast-radius change U14 deliberately deferred.
