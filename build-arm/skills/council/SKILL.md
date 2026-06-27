---
name: council
description: >-
  Vet a high-stakes decision or an ATB idea-contract with 5 independent advisor
  lenses (Contrarian, First-Principles, Expansionist, Outsider, Executor), an
  anonymized peer-review round, and a chairman synthesis that returns a clear
  PROCEED / DONT_PROCEED / NEEDS_MORE verdict + a risk band + one concrete next
  step. Use when being wrong is expensive and you keep going back and forth.
  Runs on a frontier "Teacher" agent (Claude Code); triggered by `main` at the
  approval gate for borderline ideas. Invoke with: "council this <question/context>".
---

# Council — independent idea vetting

Adapted from Karpathy's **LLM Council** (poll many viewpoints → anonymous peer
review → chairman synthesis) and Ole Lehmann's 5-advisor Claude rebuild. Origin
notes: memory `reference_council_skill_source`. Full ATB context: `../../ACTION_PLAN.md` §6.

**Why this exists:** a single agreeable model tells you what you want to hear — flip the
framing and it flips its answer. The council forces 5 *different* thinking styles onto the
same question so they can't all agree with you, then a peer-review round catches what every
advisor missed. The output is a verdict you can actually trust.

**Who runs it:** depends on the input (2026-06-12 evolution):
- **ATB council-parked jobs → the deterministic cross-family tool**, `python3 -m
  tools.council_run <job-id>`. SIX frontier advisors — TWO per lab family, each family's
  model run under two DIFFERENT personas: claude/First-Principles + claude/Steelman,
  codex/Executor + codex/Safety-Sentinel, gemini/Contrarian + gemini/User-Value (seats run
  in parallel). DETERMINISTIC synthesis: a verdict needs a 4-of-6 supermajority or it
  caution-falls to NEEDS_MORE; `risk_band` is the most-severe vote (fail-closed gate) and
  `risk_band_consensus` the 2nd-most-severe (advisory); the verdict reports per-FAMILY
  participation (six votes aren't six independent signals). <4 valid votes = no verdict, fail
  closed (4-of-6 always spans ≥2 families). No chairman model, so no single family steers the
  ruling. On PROCEED the sweep (not the tool) promotes: `python3 -m tools.think_sweep
  --resume-job <job-id>`; expected terminal is the T3.3 escalate-to-Korab hold. The two
  claude seats need a logged-in headless `claude` CLI; a dead seat abstains, and losing one
  whole family still clears quorum.
- **Free-form questions → a Teacher** (frontier — Claude Code) runs the 5-advisor flow below.
  NOT the local `main` model (it stalls on parallel sub-agents). `main` *requests* a council;
  the Teacher runs it and hands back a verdict.

**Advisory, never the decider.** The council informs `main`'s approval gate — it does **not**
approve anything. `main` (with Korab's sign-off for company canon) still makes the call. This
preserves ATB's hard guardrail: *never delegate approval*.

---

## When to run it
- An ATB **idea-contract** is borderline at `main`'s gate (risk unclear, score on the fence,
  Dreamer and the evidence disagree).
- Any decision where **being wrong is expensive** and you've been circling for more than a
  few days.
- **Skip it** if you already know the answer and just want validation — the council exists to
  tell you things you don't want to hear.

## Inputs
- A free-form `council this <question + context>`, **or**
- An ATB job: `state/tracks/<job-id>/idea-contract.json` (+ `research-input.json` for evidence).

---

## Step 1 — Frame the question (kill the lean)
Restate the decision **neutrally**, stripped of the asker's framing, assumptions, and emotional
lean. State it as a yes/no or fork, plus the context that matters. If running on an ATB job,
read the idea-contract + research-input and frame as: *"Should this idea-contract proceed to a
bounded build? What are we actually trying to solve?"*

Write the framed question to the run folder (below) as `00-question.md`.

## Step 1b — Ground-truth the brief (do this BEFORE spawning anyone)
**The frame is the most dangerous place for a hallucination to hide — every advisor inherits it.**
A real council was nearly wrecked when the brief asserted invented "vendor quotes" and version
facts (2026-06-01, `councils/20260601-0312-atb-individual-gbrains/00-CORRECTION.md`); it survived
only because one advisor ignored the frame and ran the actual binary. Don't rely on that luck.

Before writing `00-question.md`, list the **load-bearing facts** — the claims the verdict will
hinge on (library/vendor behavior, version/capability assertions, file/URL/doc existence, numbers,
anything stated as "already verified") — and **verify each one yourself**:
- Run the command, fetch the real URL, read the file. Prefer **primary sources** (the binary, the
  repo, the live config) over memory, summaries, or what the asker asserts.
- **A tool that returns empty or errors has FAILED — it is not evidence.** Never substitute a
  plausible-sounding value, quote, or version for a fetch that didn't return. Quotes must be
  copied from a source actually pulled *this run*.
- Record each as **VERIFIED** (with the one-line evidence/command), **REFUTED**, or **UNVERIFIED**
  in `00-facts.md`.

In the frame, mark every still-**UNVERIFIED** fact inline as `(unverified)` so the panel discounts
it. If a load-bearing fact can't be verified, say so — never launder an assumption into a stated
fact. If verification *refutes* the asker's premise, reframe around the corrected facts and note
the correction (write a `00-CORRECTION.md` if the brief was already circulated to advisors).

## Step 2 — Spawn the 5 advisors IN PARALLEL
Spawn five sub-agents in a **single message** (five Task/Agent calls at once), each with ONE
lens. Each gets the framed question + context but NOT each other's output. Each returns: its
**position**, the 2-3 points that matter most for its lens, and a 1-line confidence note.

Tell each advisor the brief's facts are **claims, not gospel** — any advisor whose position leans
on a brief "fact" should spot-check it against the source and flag what it can't confirm
(`00-facts.md` from Step 1b lists what's already verified). The lens that catches the fabrication
is worth more than the lens that argues elegantly from it.

| Lens | Mandate |
|---|---|
| **Contrarian** | Assume a fatal flaw exists and find it. What kills this? If it all looks solid, dig deeper. Catches "sounds great, but have you thought about…". |
| **First-Principles** | Ignore the question as asked. What are we *actually* trying to solve? Strip assumptions, rebuild from the ground up. Catches "you're optimizing the wrong variable." |
| **Expansionist** | Hunt the upside being missed. What could be 10x bigger? What adjacent opportunity sits right next to this? Catches "you're thinking too small." |
| **Outsider** | Zero context about the asker, field, or history. React only to what's in front of you. Catches the curse of knowledge — obvious to you, invisible to the customer. |
| **Executor** | Only cares: what do you do Monday morning? If it's brilliant with no first step, say so. Catches plans with no path to action. |

Save each as `01-advisor-<lens>.md`.

## Step 3 — Anonymize, then peer-review
- Collect the 5 takes, **shuffle**, and relabel them randomly **A–E** (record the mapping
  privately in `02-key.json`; reviewers must not see it).
- Spawn **5 reviewer** sub-agents in parallel. Each reads all 5 anonymized takes and answers:
  1. **Which response is strongest, and why?**
  2. **Which has the biggest blind spot?**
  3. **What did all five miss?** ← the most valuable question.
- Save the anonymized set as `03-anon.md` and the reviews as `04-reviews.md`.
  *(Token-constrained? Collapse to 3 reviewers, but never fewer.)*

## Step 4 — Chairman synthesis + verdict
Read everything (advisors + reviews). Produce:
- **Verdict** (one of): `PROCEED` · `DONT_PROCEED` · `NEEDS_MORE`.
- **Risk band**: `low` · `medium` · `high`, with a one-line rationale.
- **One concrete next step** (a Monday-morning action).
- **What all five missed** — surfaced explicitly from the peer-review round.
- **The tension that reinforces** — if two lenses' points combine into something neither saw
  alone (this is usually the real insight).

**Before finalizing:** confirm no part of the verdict rests on an `UNVERIFIED` fact from
`00-facts.md`. If one slipped through, verify it now — or name it as the load-bearing-but-unconfirmed
assumption and lower the verdict's confidence accordingly.

Map the verdict to `main`'s decision vocabulary so it drops straight into the gate:
- `PROCEED`   → `intent-review.recommendation = promote` (carry the council risk band into `main-review`)
- `DONT_PROCEED` → `intent-review.recommendation = reject` (with the killing reason)
- `NEEDS_MORE`   → `intent-review.recommendation = needs_more` (state the exact gap; send back to Dreamer/Research)

`main` reads this and decides. The council never writes `main-review` or the approval ledger itself.

## Step 5 — Output
Write to the run folder:
- **`council-report.html`** — a clean, scannable 60-second report: framed question, the 5
  positions (one card each), the peer-review highlights ("what all five missed" pulled to the
  top), and the chairman verdict banner (verdict + risk + next step).
- **`transcript.md`** — the full reasoning (question → advisors → anonymized set → reviews →
  synthesis) for digging in later.
- **`verdict.json`** — machine-readable for `main`:
  ```json
  {
    "verdict": "PROCEED|DONT_PROCEED|NEEDS_MORE",
    "risk_band": "low|medium|high",
    "rationale": "one line",
    "next_step": "Monday-morning action",
    "what_all_missed": "the peer-review insight",
    "maps_to_intent_review": "promote|reject|needs_more",
    "advisors": {"contrarian": "...", "first_principles": "...", "expansionist": "...", "outsider": "...", "executor": "..."}
  }
  ```

### Run folder
- ATB job → `state/tracks/<job-id>/council/`
- Ad-hoc → `councils/<YYYYMMDD-HHMM>-<slug>/` (under the ATB namespace)

---

## How `main` triggers it (ATB integration)
The trigger is a **deterministic risk rule on the idea-contract**, NOT `main`'s judgment — its
judgment is the weak link the council exists to bypass (established by a council on this exact
policy, 2026-05-30; see `councils/20260530-0547-council-trigger-policy/`). A council fires when the
idea-contract trips any **risk-surface keyword** (auth / secret / payment / delete / migrate /
network / external / irreversible) in `what_should_exist` or `where_it_lives`, declares **unbounded
scope** (empty `out_of_scope`), has **thin evidence** (<2 `evidence_refs`), or is **unmeasurable**
(vague `how_verifiable`). On a trip, `main` appends a `council-requested` event and **pauses** — it
does NOT write a `needs_council` recommendation (not a valid schema value; a pause, not a verdict).
A Teacher runs this skill, writes `verdict.json` into the track, and `main` resumes, folding the
verdict into its own `intent-review` / `main-review` as **one input, not the decision**. The trigger
is implemented as a `--no-agent` script (sibling to the contract-chain validator), zero model
judgment: `python -m tools.council_gate <track> --emit-event` (exit 0 = no council; exit 2 = council
needed + appends the `council-requested` event). See `main`'s SOUL for the exact checklist.

## Guardrails
- **Advisory only.** Never auto-approve; `main` decides, Korab signs off on company canon.
- **No convergence-to-please.** Advisors must not soften to agree with the asker or each other.
- **Reviewers stay blind.** Never leak the A–E → lens mapping into the reviewer prompts.
- **Surface dissent.** If the verdict is "obvious," the chairman must still report the strongest
  opposing case the council raised.
- **Ground-truth before you frame (Step 1b).** Load-bearing facts get verified against primary
  sources *before* the panel runs; results live in `00-facts.md`.
- **No fabricated facts — ever.** An empty or errored tool result is a *failure*, not a fact. Never
  fill the gap with a plausible value, quote, or version number; cite quotes only from sources
  fetched this run. One wrong "fact" in the frame poisons all five lenses at once.
