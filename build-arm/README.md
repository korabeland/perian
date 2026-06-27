# Auto-think / Auto-build (ATB)

ATB is a self-improving build system: local small models do the work, frontier models harden the
process, and deterministic tools certify trust. It is **two loops with a stack of judges between
them** — a *thinking loop* that vets ideas into briefs, and a *building loop* that builds and
hardens them. No agent ever certifies its own work.

## The macro shape

```
            THINKING LOOP                          BUILDING LOOP
  dreamer → research → main                plan-gate → coder → diff-gate → qa-gate
  (deterministic gates T1–T4)                   (coach loop wraps the diff-gate)
        │                                            ▲
        ▼                                            │
   idea digest ──► Pascal ──► OWNER ──► supervised enqueue (manual today)
                (judge stack: main proposes → Pascal escalates → owner decides)
```

- **Crossing 1 (think → build):** *is the idea worth building?* The thinking loop's terminal
  artifact is an **idea digest** (a human-readable brief). Auto-enqueue is OFF — the owner picks what
  builds.
- **Crossing 2 (build → acceptance):** *did the build meet the original idea?* Judged against the
  brief's end-state by the gates, not by the crew.

## The trust rule

**Doer ≠ certifier.** Independence comes from a different model family or a different *job*:

1. The local crew **builds** (it never certifies its own work).
2. **Codex** (OpenAI, cross-family) certifies the build semantically — plan fidelity + code quality.
3. **Deterministic tools** certify *trust only, never how*: did tests really run, are timestamps
   real, did the diff stay inside the sandbox (`tools/scope_check.py` on `allowed_paths`).
4. **The overseer** (Claude Code + the owner) judges the system itself.

The rule binds the overseers too: contracts/plans/tools authored by Claude Code pass a Codex
review before they ship. Trust-spine code (`tools/*`) is overseer-built, never crew-built.

## The thinking loop

`tools/think_sweep.py` orchestrates one cycle per topic: evidence scan → dreamer proposes an
idea-contract → research fleshes out evidence → main reviews → digest. Between every model stage
sits a **deterministic gate** (T1 evidence anchoring, T2 contract validation, T3 review schema,
T4 digest render with hash re-verification). Key mechanics:

- **Gate-then-promote staging:** artifacts are written to a staging area and promoted into
  `state/tracks/` only after the gate passes; failed artifacts never promote.
- **Bounded retry:** a stage that produces no trusted fresh output is re-spawned a bounded number
  of times; content-gate failures are never retried.
- **Web-grounded research — the sweep fetches, the model never gets external network:** before the
  scan, a deterministic step fetches a **committed-allowlist** of URLs (read from git HEAD so a
  working-tree edit can't take effect unreviewed), SSRF-pinned + GET-only (`tools/web_fetch.py`),
  freezes each page as text and hash-anchors it; a gate certifies integrity and binds every cited
  URL to a fetched, allowlisted, hash-matched page. The research spawn has **no external egress**
  and its file reads are jailed (every credential store in `$HOME` is denied). The
  no-external-egress jail is the real key boundary.
- **Parking, not crashing:** duplicates, open-track caps, risk keywords, and thin evidence park a
  job (recoverable) rather than holding it (trust failure).
- **Cross-family council:** a deterministic risk rule (`tools/council_gate.py`) can summon a
  6-seat council (`tools/council_run.py`) — 2 personas each from Claude, Codex, and Gemini —
  4-of-6 supermajority; fewer than 4 valid votes fails closed. The verdict is one input to main,
  not the decision.
- **Briefs + delivery:** each approved idea gets a durable per-idea brief plus a self-healing day
  roll-up; `python -m tools.brief_inbox` lists the **pending** ones (approved but not yet built).

## The building loop

Tracks are enqueued onto the `auto-build` board. Gate cards are advanced **only** by the
deterministic sweep (`tools/gate_sweep.py`); the dispatcher spawns workers in isolated workspaces.

```
plan-gate (Codex reviews the product-plan)
  → coder (builds in an isolated git workspace)
    → diff-gate: scope_check (deterministic sandbox) → tool-captured verification → Codex review
      → on HOLD: COACH LOOP — Codex-as-teacher writes skill-shaped lessons, the coder card is
        recreated, re-judged; capped at 5 rounds → escalation
    → qa-gate (deterministic: tests truly ran, timestamps real, two runs agree)
      → confirmed
```

The **diff-gate is the load-bearing check** — it has caught real bugs the coder's own tests missed
in nearly every build. A HOLD on a real bug or out-of-sandbox change is the system working.

## The decomposition seam

One step at the think→build seam splits an approved idea into **atomic, dependency-ordered,
complexity-tagged child tracks each sized to one session** — certified by the same three-layer
pattern (local doer → deterministic trust tool → Codex fidelity judge) against a **frozen,
structured list of parent criteria** the split must exactly partition:

1. Main drafts criteria → **Codex expands cross-cutting ones + freezes the set** via a sweep-owned
   freeze-wrapper that emits a `criteria_frozen` event Main cannot forge (it runs outside Main's
   fs-jail).
2. Main proposes a `work-breakdown.json` → **`tools/decompose_check.py` (D1–D8)** proves it's an
   honest, buildable partition — freeze integrity, exact-partition coverage, acyclic + closed
   dependencies, large-child guard, provenance — reading **only trusted records**. Codex then judges
   fidelity.
3. The breakdown **produces** child contract/plan files → a validator freezes their hashes and the
   **mint** copies exactly those bytes; each child then runs the existing chain.

Determinism certifies **trust** (freeze held, partition held, bytes unchanged), never *how big
things really are* — a child over its envelope at build re-splits.

## The cast

| Layer | Who | Model / runtime |
|---|---|---|
| Owner | the operator | final say on both crossings |
| Outer overseer | Claude Code | builds + hardens the system itself |
| Build certifier | Codex | plan/diff reviewer, coach-teacher, contract gate |
| Council seats | Claude, Codex, Gemini | 2 personas each, idea vetting |
| Human interface | Pascal | the only agent the owner talks to; no build work |
| Crew | main, dreamer, research, coder | local headless small models |
| QA judge | qa | cross-family model, out of the trust path |

The crew **does the work — thinking and building — but never certifies it**; quality comes from
the gates, not the local models.

## Layout

```
build-arm/
├── tools/      ← the trust spine (gates, sweeps, council, coach) — overseer-built
├── schemas/    ← JSON Schemas for every contract artifact
├── scripts/    ← entry-point shell wrappers
├── config/     ← path + fetch-allowlist configuration
├── skills/     ← reusable skill definitions
└── docs/       ← methodology docs (charter, meta-audit playbook, system map)
```

Blueprint article: https://x.com/gkisokay/status/2053449921554960545
