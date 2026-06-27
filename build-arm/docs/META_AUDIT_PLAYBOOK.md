# Meta-Audit Playbook — verify the verifier (meta-layer Phase 3)

**Tool:** `tools/meta_audit_run.py` · **Schemas:** `schemas/meta-audit-packet.schema.json`,
`schemas/meta-audit-verdict.schema.json` · **Digests:** `overseer/meta-audits/<ts>-<target>/digest.json`
· **Memory:** [[project_atb_agent_improvement_monitor]]. **Status:** LIVE, SUPERVISED (surfaces, never auto-acts).

## What it does

The pipeline verifies *downward* (coder → gates → overseer → Korab). The meta-audit verifies *up*:
it convenes the same cross-family panel the council uses (six seats, two per lab family) on a packet
of sampled **improver** artifacts — overseer-decision-records, a window of gate/coach/council/validator
judgments, and the loop-closure state — and asks one question: **did the IMPROVERS (the overseers +
the gates) do their own job soundly?** It surfaces a digest; it never changes an agent layer.

## Cross-family independence (why it can be trusted)

A run **targets one improver family** (`claude` = the overseer who authors decisions, or `codex` =
the diff-gate + coach teacher). The target family's own seats are **advisory only**; the **binding**
verdict comes from the two *non-target* families, **both required present**. Auditing Codex → Claude
+ Gemini bind; auditing Claude → Codex + Gemini bind. A one-sided panel fails closed to `NEEDS_MORE`.
This is doer ≠ certifier pointed up the stack: a family cannot certify its own calls.

## When to run (cadence)

**Overseer-triggered, not auto-armed** (the panel spawns six frontier CLIs — real cost — and autonomy
is not armed). Run it:
- **After a batch of agent-affecting decisions** (e.g. a SOUL/skill/tool promotion lands), and
- **around a corpus-review milestone** (it pairs with the Q1/Q2 spine reviews), or roughly **every ~5
  confirmed builds**.
- Run **both targets** each time, so each improver is audited by the other two families:
  ```
  python3 -m tools.meta_audit_run --target-family codex
  python3 -m tools.meta_audit_run --target-family claude
  ```
- **Preflight:** all three seat CLIs authenticated (`claude`, `codex`, `agy` with the pinned Gemini
  model) — a dead auth on a whole family fails that family's seats closed and can break binding
  quorum (→ `NEEDS_MORE`, not a false pass).

## Reading a digest

| Field | Meaning |
|---|---|
| `verdict` | `SOUND` (improvers held up) · `FLAGGED` (≥1 improver/verifier failure, binding majority) · `NEEDS_MORE` (no majority **or** cross-family quorum not met — inconclusive) |
| `quorum_met` | `false` ⇒ both non-target families were not present; verdict is forced `NEEDS_MORE`. Check the seat abstentions before re-running. |
| `risk_band` / `_consensus` | severity from binding votes only (fail-closed max / 2nd-worst) |
| `findings[]` | every seat, tagged `role: binding\|advisory` — advisory + minority concerns are shown, never hidden |
| `routing[]` | verifier-miss routing (see below) |

Exit codes: `0` = SOUND/NEEDS_MORE · `2` = FLAGGED (surface to Korab) · `1` = run error (no digest).

## The verifier-miss routing protocol

The meta-audit audits *verifiers*, not *builds*. If a seat spots a real **build** bug in a sampled
gate, it does **not** make it a finding — it emits a `ROUTE: <detector> | <note>` reason, and the
tool lifts that into `routing[]`. For each routing entry, the **overseer routes it to the named
existing detector** — never the meta-audit itself:
- `coach` → the coach loop (a coder-skill weakness) · `gate_hold` → re-examine that diff-gate call ·
  `improvement_feeder` → a system/plumbing recurrence · `loop_closure` → open a ledger class.

## Acting on a FLAGGED verdict

A flagged improver/verifier failure is **surfaced to Korab**, then resolved through the existing
SOUL-promotion governance (**Codex review → overseer applies → A/B-prove → Korab certifies**, with
the over-fit guard) and **tracked back through the loop-closure ledger** (Phase 2): open a
`loop-closure-entry` for the class and close it with an `overseer-decision-record` (Phase 1). The
meta-audit closes the top of the loop; the ledger records that the loop actually closed.
