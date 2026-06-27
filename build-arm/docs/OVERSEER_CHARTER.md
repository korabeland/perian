# Overseer Charter — verify the verifier / teach the teacher

**Audience:** the two overseers — Claude Code and Codex. **Status:** LIVE (meta-layer Phases 1–3).
**Plan:** `docs/plans/2026-06-20-002-overseer-meta-layer-plan.md`. **Memory:**
[[project_atb_agent_improvement_monitor]].

The ATB pipeline verifies and teaches **downward** (coder → diff-gate → qa-gate → overseer →
Korab; Codex coaches the coder). Nothing verifies the verifiers or teaches the teachers. This
charter is that missing top layer. It is a **standing discipline**, not a detector — detection
already exists (the coach loop banks coder lessons; the Improvement Feeder banks system
recurrences). The charter governs what the overseers DO with what those detectors surface.

## The three reflexes

Every **agent-affecting decision** — any choice to rewrite (or deliberately NOT rewrite) one
of a small agent's rewritable layers — runs through these three reflexes:

1. **Ground in memory + logs, not recall.** Before deciding, pull the relevant memory + logs
   (operating thesis / over-fit guard, prior promotions for this agent, the identity-first
   law, corpus state; events + coach history). Use `tools/grounding_pack.py` — it assembles
   the fixed bundle into a content-addressed snapshot and returns a `grounding_pack_id`.

2. **Refinement-surface reflex — the small agents are rewritable.** Coder/qa/main/dreamer/
   research are not fixed; each has four rewritable layers — **identity** (SOUL.md), **skills**,
   **tools**, **context**. On any recurrence ask *"which layer do we rewrite for durability?"*
   — never one-off-patch the instance. A fix that doesn't change a layer doesn't compound.

3. **doer ≠ certifier, pointed UP the stack.** The rule that binds the crew binds the
   overseers too. An overseer's own artifacts (a contract, plan, rubric, or a proposed agent
   change) get an independent **cross-family** check before they ship; the verifiers' outputs
   get spot-checked. Nobody at the top runs unverified. (Cross-family because Codex can't
   audit Codex — independence comes from a different model *family*, not a different label.)

## What makes it bite — the decision-record + the completeness check

A charter of intentions is decoration unless violations are catchable. So:

- **Every agent-affecting decision emits an `overseer-decision-record`** (schema:
  `schemas/overseer-decision-record.schema.json`, stored under `overseer/records/`). It pins
  the grounding (`grounding_pack_id`, `memory_refs`, `log_refs`, `detector_source_refs`), the
  layer considered, the decision (`layer-rewrite` | `no_change` | `defer`), the independent
  `certifier`, and `status`. A deliberate **no_change** is a first-class, recorded outcome —
  "we considered it and left it" is a decision, not a silent drop.

- **The binding is bidirectional** so neither half can be back-filled alone: a profiles-repo
  commit that changes an agent layer MUST carry a `Decision-Record: <id>` git trailer, and the
  record MUST be a `layer-rewrite` whose `commit_hash` is that commit.

- **`tools/decision_record_check.py` enforces it deterministically.** It treats git commits in
  `~/.hermes/profiles` touching `<agent>/SOUL.md`, `<agent>/config.yaml`, `<agent>/skills/**`,
  `<agent>/tools/**` as the unforgeable source of truth, and HOLDS on any agent-layer change
  with no valid linked record (and any record citing a commit that does not exist). It is
  **trust-only**: it checks only that a record EXISTS and is LINKED — never whether the
  decision was good. Soundness stays with Codex + the overseer + Korab.

The split is the operating thesis applied to the overseers themselves: **determinism certifies
the trust fact (a grounded, certified decision was recorded); the frontier + Korab judge the
*how*.** Schema-valid + linked ≠ sound.

## Closing the loop — the loop-closure ledger (Phase 2)

The decision-record check binds one direction: a durable layer change ⇒ a record. The
**loop-closure ledger** binds the other: a **detected recurrence ⇒ a recorded decision** — so a
weakness the detectors surface can never quietly evaporate. Together they make the loop tight:
no unrecorded change *and* no silently-dropped detection.

- **A ledger entry tracks one recurrence-CLASS** (not one raw lesson) keyed to existing-detector
  artifacts only (schema: `schemas/loop-closure-entry.schema.json`, stored under
  `overseer/ledger/`). It adds **no new detector** — the `detector` field is a closed enum of
  the detectors that already exist (coach / Improvement Feeder / gate-hold).
- **Each class resolves to a recorded decision or is visibly open.** A `resolved` entry MUST
  cite an `overseer-decision-record`; the *kind* of closure (`layer-rewrite` | `no_change` |
  `defer`) is read from that trusted record, not asserted by the entry (single source of truth).
- **`tools/loop_closure_check.py` enforces it deterministically.** It resolves citations through
  the SAME trusted-record loader as Phase 1 (so it can never trust a record Phase 1 would
  reject), and HOLDS on a broken binding (missing / wrong-agent / invalid record), an **expired
  defer** (no silent indefinite deferral), or a malformed / duplicate entry. An **open** class is
  surfaced but not held — it is legitimate work in progress. It is **trust-only**: it records and
  reconciles the binding; it never judges which weakness is real or which layer to pick.

## Verify the verifier — the meta-audit (Phase 3)

The completeness check and the ledger keep the records honest; nothing yet checked whether the
**improvers themselves** — the overseers and the gates — did their job soundly. The **meta-audit**
(`tools/meta_audit_run.py`, playbook in `docs/META_AUDIT_PLAYBOOK.md`) is that top layer. It reuses
the council's own cross-family panel (six seats, two per family — extracted into
`tools/council_core.py` so the council and the audit share one hardened mechanism) over a packet of
sampled improver artifacts: decision-records (grounded? independently certified?), a window of
gate/coach/council/validator judgments (internally sound?), and the loop-closure state (resolved or
dropped? — cross-checks the ledger).

- **Doer ≠ certifier, pointed UP, made structural.** A run targets ONE improver family; that
  family's seats are **advisory only**, and the binding verdict comes from the two non-target
  families, **both required present** (auditing Codex → Claude + Gemini bind). A one-sided panel
  fails closed to `NEEDS_MORE` — a family can never certify its own calls.
- **It adds NO new detector and judges no build.** The only finding is an *improver/verifier*
  failure; a real build bug a seat spots is **routed** to an existing detector (the `routing[]`
  convention), never handled here — this is what keeps it from sliding into a bottom-up bug detector.
- **Supervised — surfaces, never auto-acts.** It writes a digest + emits an event; a `FLAGGED`
  verdict goes to Korab and resolves through the SOUL-promotion governance, tracked back through the
  loop-closure ledger. It reuses the over-fit guardrail from `improvement_codex.py` directly.

## Scope (Phases 1–3) and the standing discipline

Phases 1–3 are the standing discipline + the records + the completeness check + the grounding
assembler + the loop-closure ledger + the cross-family meta-audit — the parts that bite. They add
**no new detector and no new judge**: they reuse the coach loop + Improvement Feeder (detect), the
council core (cross-family audit), and the SOUL-promotion flow (govern). The governance for an
actual `layer-rewrite` is the existing arc, run twice already this corpus: **Codex review → overseer
applies → A/B-prove → Korab certifies**, with the over-fit / provisional guard. Identity NEVER
auto-applies; an agent NEVER edits its own layers; the meta-layer itself is overseer-built +
Codex-reviewed (Phase 3: council-core extraction APPROVE, meta-audit APPROVE).
