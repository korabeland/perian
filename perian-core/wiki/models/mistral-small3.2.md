---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-05
sources:
  - open-brain
  - state/model-fleet.json
confidence: low
status: current
---

## Summary

The fleet's reasoning champion — highest scores on conditional (0.90), causal (0.92), and nuanced judgment (0.91). The only non-Qwen, non-Gemma model in the evaluation pool, which makes it architecturally valuable for catching errors that family-shared blind spots might miss. But it has a critical quirk: the `think` parameter is not just unnecessary — sending it breaks the model.

## What We've Learned

**The think parameter is a landmine.** Every other model in the fleet accepts `think: false` (Qwen) or works with it (Gemma). Mistral does not support the `think` parameter at all — sending it causes errors or degraded output. This is the only model where the pipeline code needs special-case handling: omit `think` entirely rather than setting it to false. A small thing, but it broke the first eval run completely.

**First eval catastrophic, second eval fleet-best.** The first eval (2026-04-04 07:55) produced: extraction 0.95, classification 1.00, but json_output 0.5, conditional 0, causal 0, synthesis 0. Something went wrong — likely the think parameter or a prompt format issue. The re-run 50 minutes later scored fleet-best on reasoning across the board. The gap between these two runs is a reminder that a single eval tells you very little.

**Architectural diversity has value.** All Perian models are Qwen family, which means they share training data, architectural biases, and failure modes. When qwen3:14b and qwen3:8b agree on an extraction, it might mean they're both right — or both wrong in the same way. Mistral comes from a different lineage. As an Aragorn challenger, it could catch errors that Qwen models systematically miss.

**Strong reasoning scores need context.** Conditional 0.90 and causal 0.92 are fleet-best — but from a single successful eval, on a prompt suite optimized for Qwen. If anything, non-Qwen models should score *lower* on Perian's contaminated eval, not higher. Mistral scoring fleet-best despite the prompt-fit disadvantage is either genuinely impressive or a statistical fluke from n=1.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `mistral-small3.2:latest` |
| Parameters | 24B dense |
| Size on disk | 15 GB |
| Family | Mistral |
| Context window | 128K |
| Architecture | Dense, multimodal (text+image) |
| Think parameter | **NOT SUPPORTED — omit entirely** |
| License | Proprietary (Mistral) |

## Open Questions

- Are the fleet-best reasoning scores reproducible across 3+ runs?
- What caused the first eval failure? If it's the think parameter, the fix is simple. If it's a deeper prompt format issue, integration is harder.
- Could it replace qwen3:14b as Aragorn challenger? Higher reasoning scores + architectural diversity is a compelling case, but pipeline integration needs testing.
- At 15 GB (2 GB lighter than qwen3.5:27b), could it serve as both escalation target and Aragorn challenger?

## Cross-References

- [[models/qwen3-14b]] — current Aragorn challenger (0.91 causal vs Mistral's 0.92)
- [[models/qwen3.5-27b]] — current escalation target (better extraction, worse reasoning)
- [[evaluation/scoring-calibration]] — non-Qwen models face prompt-fit disadvantage

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-04-03 | Found by Legolas, downloaded | model-fleet.json |
| 2026-04-04 | Failed first eval: json_output 0.5, reasoning all zeros | fleet-changelog |
| 2026-04-04 | Successful re-eval: conditional 0.90, causal 0.92 — fleet best | fleet-changelog |
| 2026-04-05 | "Highest on conditional reasoning" flagged in rebaseline summary | OB observation |
