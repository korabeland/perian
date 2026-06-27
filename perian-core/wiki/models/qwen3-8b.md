---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-05
sources:
  - open-brain
  - state/model-fleet.json
  - state/fleet-changelog.jsonl
confidence: high
status: retired
---

## Summary

Retired from the active fleet in April 2026, replaced by qwen3.5:9b-nvfp4 as first-pass extractor. Still important to understand because it's the source of a systemic bias: every prompt, every ground truth answer, and every autoresearch optimization in Perian was built around this model. Understanding qwen3:8b means understanding both its real capabilities and the invisible advantage it had on our eval suite.

## What We've Learned

**Thinking mode is devastating at this size.** The discovery that changed everything: with `think: true`, qwen3:32b generates thousands of hidden reasoning tokens (invisible in API output) before producing the actual response — 3,800 thinking tokens for 100 output tokens. Turning thinking off reduced the pipeline from 25 minutes to 5 minutes for 6 items. The trigger was a Datadog listing consistently timing out while identical-length listings completed in 30 seconds. At 8B, `think: false` is non-negotiable.

**But thinking *helps* small models on hard tasks.** The flip side: 8b gained +0.17 on causal reasoning with thinking enabled, while 32b gained +0.00. Small models genuinely benefit from the explicit scratchpad for multi-step reasoning. The trade-off is a 3-7x slowdown. For daily extraction (simple structured output), thinking is pure waste. For occasional hard cases, it has value — but we never use it because the pipeline optimizes for throughput.

**Published benchmarks don't predict Perian performance.** qwen3.5:9b beats qwen3:8b on MMMU-Pro (69% vs 57%) but ties or slightly loses on our eval suite. The lesson: always run project-specific evals before upgrading. General capability doesn't transfer automatically to structured JSON extraction from job listings.

**The contamination problem runs deep.** Ground truth was validated against 8b's behavior. Prompts were tuned for 8b via autoresearch. The eval rubric uses exact string matching that rewards models phrasing answers like 8b does. This triple contamination means every non-qwen3 model starts at a disadvantage on our eval. We know this intellectually but haven't yet fixed it (stripped-prompt evals are the path forward).

**The router picks it because it's cheapest, not best.** Capability-based routing selects the cheapest model meeting the threshold. With extraction at 0.88 and the threshold at 0.85, 8b barely qualifies — the router is correct but the margin is thin. Any extraction regression could drop it below threshold and trigger escalation to the 27b model.

**The 2B triage gate works.** qwen3.5:2b with title-only prompt runs as a binary gate before 8b extraction. 54% rejection rate at ~0.3s/item. Interestingly, adding description context *reduced* recall for small models — they get distracted by irrelevant details. Title-only forces focus on the strongest signal.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `qwen3:8b` |
| Parameters | 8B dense |
| Size on disk | 5.0 GB |
| Family | Qwen 3 |
| Context window | 128K |
| Think parameter | `think: false` (required) |
| Fleet role | Retired — replaced by qwen3.5:9b-nvfp4 (Apr 2026) |

## Open Questions

- Would qwen3.5:4b replace it? Nearly identical extraction (0.93 vs 0.88) at 35% less memory. But the prompts are optimized for qwen3, not qwen3.5 — prompt-fit may explain the gap.
- How much of its eval advantage is real capability vs prompt-fit contamination? The stripped-prompt eval will answer this.
- Should the router threshold be raised from 0.85 to 0.90 to force more escalation? Current thin margin means marginal items get 8b-quality extraction.

## Cross-References

- [[models/qwen3.5-4b]] — potential replacement at the fast tier
- [[models/qwen3.5-9b]] — potential replacement that should be better but isn't (on our eval)
- [[models/qwen3-14b]] — same family, causal specialist
- [[evaluation/scoring-calibration]] — 8b contamination affects the entire eval suite
- [[techniques/cascade-routing-logprobs]] — Tier 1 in the cascade pattern

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-03-22 | Thinking overhead discovered — pipeline 25min→5min with think:false | OB insight |
| 2026-03-22 | Fleet switched from qwen2.5 to qwen3 family — 8b as small tier | OB decision |
| 2026-03-26 | First full eval: extraction 0.90, causal 0.60 | fleet-changelog |
| 2026-03-26 | Eval leakage identified — ground truth calibrated to 8b behavior | OB insight |
| 2026-04-02 | 2B triage gate deployed — 54% rejection rate, title-only prompt | OB decision |
| 2026-04-04 | April rebaseline: extraction 0.88, conditional 0.89, causal 0.77 | fleet-changelog |
