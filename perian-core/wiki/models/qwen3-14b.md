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

Retired from the active fleet in April 2026. Was the fleet's causal reasoning specialist — consistently outreasons models twice its size. Served as Aragorn's adversarial challenger until gemma4:26b took over that role, bringing cross-family diversity that a same-family Qwen model couldn't provide. The question we kept exploring: was its reasoning edge genuine architecture, or prompt-fit from autoresearch optimizing for qwen3?

## What We've Learned

**Reasoning punches above its weight.** Across every eval run since March, 14b has led the fleet on causal reasoning — beating qwen3:32b, qwen3.5:27b, and even qwen3.5:35b-a3b. The only model to challenge it is mistral-small3.2 (0.92 vs 0.91 causal), which comes from an entirely different architecture. This pattern survived the eval suite v5 rebaseline, suggesting it's not an artifact of the test cases.

**Thinking mode is redundant for it.** We discovered that `think: true` helps small models far more than large ones — qwen3:8b gained +0.17 on causal reasoning with thinking enabled, while qwen3:32b gained +0.00. At 14B parameters, the model has enough internal capacity to reason during normal generation. The explicit chain-of-thought scratchpad is wasted overhead. Always run with `think: false`.

**The prompt-fit question is unresolved.** Perian's prompts were optimized for the qwen3 family via autoresearch (Phase 4). The eval suite's ground truth was validated against qwen3:8b behavior. This creates a triple contamination that systematically advantages qwen3 models. Until we run stripped-prompt capability evals (`--prompt-variant stripped` exists but hasn't been run on 14b yet), we can't separate prompt-fit from raw capability. This is the single biggest open question for the fleet composition decision.

**It's the right model for Aragorn.** The adversarial challenger role needs a model that can judge whether the pipeline's scoring is defensible — that's a causal reasoning task, exactly where 14b excels. It runs weekly on Wednesdays via Shadowfax, challenging items scored above 0.7. After the April recalibration (challenge prompt rewritten, delta raised to 0.20), precision improved from 5% to 25%.

**One catastrophic failure on record.** On 2026-04-03 at 21:13, an eval produced all zeros across every capability. Re-run 30 minutes later scored normally. Likely Ollama state issue (model not loaded, or lingering state from a previous run), not a model defect. Worth remembering as a reminder that eval infrastructure can silently fail — always check for all-zero results.

**The specialist split hypothesis.** The proposed fleet restructure (qwen3.5:4b fast + qwen3.5:27b heavy + qwen3:14b specialist) would give each model its strongest role. The router already supports per-step model selection via capability thresholds, so 14b would only handle causal/nuanced judgment tasks. This preserves its strength while letting newer models handle extraction.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `qwen3:14b` |
| Parameters | 14B dense |
| Size on disk | 9.3 GB |
| Family | Qwen 3 |
| Context window | 128K |
| Think parameter | `think: false` (required) |
| Fleet role | Retired — replaced as Aragorn by gemma4:26b (Apr 2026) |

## Open Questions

- Does 14b maintain its reasoning edge with generic (non-Perian) prompts? The stripped-prompt eval will answer this.
- Would mistral-small3.2 be a better Aragorn challenger? It scored higher on causal (0.92 vs 0.91) but has the `think` parameter incompatibility and only one successful eval run.
- How does reasoning quality degrade under KV cache quantization (q8_0)? At 9.3 GB it's not memory-constrained, so probably no impact — but worth verifying.
- Is there a causal reasoning ceiling for 14B-class models, or could fine-tuning (Phase 5) push it further?

## Cross-References

- [[models/mistral-small3.2]] — only model that challenges 14b on causal reasoning
- [[models/qwen3.5-27b]] — escalation target, better extraction but lower causal
- [[models/qwen3-8b]] — first-pass model whose prompts were optimized for the same family
- [[techniques/cascade-routing-logprobs]] — 14b as the medium tier in cascade routing
- [[evaluation/scoring-calibration]] — prompt-fit contamination affects all qwen3 models

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-03-22 | Fleet switched from qwen2.5 to qwen3 family — 14b added as medium tier | OB decision |
| 2026-03-23 | Phase 3 eval: conditional 0.91, causal 0.70 — causal champion identified | fleet-changelog |
| 2026-03-26 | No qwen3.5 model matched 14b on causal (2b=0.10, 4b=0.47, 9b=0.57, 27b=0.57) | OB observation |
| 2026-04-03 | All-zero eval failure at 21:13, normal re-run at 21:44 | fleet-changelog |
| 2026-04-03 | Aragorn recalibrated — precision 5%→25%, weekly schedule, delta 0.15→0.20 | TODO.md |
| 2026-04-05 | April rebaseline: causal 0.91, nuanced 0.88 — still fleet leader | fleet-changelog |
| 2026-04-05 | Stripped-prompt infrastructure ready but not yet run on 14b | OB task |
