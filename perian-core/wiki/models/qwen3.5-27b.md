---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-12
sources:
  - open-brain
  - state/model-fleet.json
  - state/fleet-changelog.jsonl
confidence: high
status: current
---

## Summary

The fleet's escalation target and extraction champion. Replaced qwen3:32b by being 3 GB smaller and better on every metric — the first easy upgrade decision in Perian's history. Dense architecture gives it consistency that MoE models can't match on structured output, but its causal reasoning trails the much smaller qwen3:14b.

## What We've Learned

**The upgrade that validated the framework.** qwen3.5:27b was the first real test case for the upgrade decision framework in EVAL_METHODOLOGY.md §5. It passed every criterion: extraction 0.94 (fleet best), classification 1.00 (perfect), 3 GB smaller than qwen3:32b. The decision was captured as "easiest fleet swap — no reason to keep 32b." This set the precedent for how Perian evaluates model upgrades.

**The specialist split emerged from its profile.** The proposed fleet restructure came from studying 27b's strengths and weaknesses: dominant on extraction/classification but weaker on causal reasoning than 14b. Rather than forcing one model to do everything, the insight was to let each model handle what it's best at. The router's per-step model selection makes this possible — 27b handles extraction, 14b handles judgment.

**Synthesis scores tell a confusing story.** In March, 27b hit 0.67 on synthesis — well above the 0.46-0.55 cluster. This seemed to break the "rubric is the bottleneck" hypothesis. But after the eval suite v5 changes, synthesis dropped to 0.22 — back in line with other models. The earlier score likely reflected rubric-matching ability (using expected keywords) rather than genuine synthesis quality. Lesson: when one model dramatically outperforms all others on a metric, check the rubric before celebrating.

**Thinking is off by default.** Unlike qwen3 (which required explicit `think: false`), qwen3.5 models have thinking off by default (Gated DeltaNet architecture). This eliminates the hidden token overhead that plagued the qwen3 generation. Still need `think: false` in the API call for safety, but the model cooperates without it.

**MLX int4 delivers a major speed boost.** The migration from GGUF to MLX int4 (Apr 2026) cut eval time from 5653s to 2132s — a 62% reduction. The quality trade-off is within noise: extraction stayed at 0.92 (vs 0.94 GGUF), classification 1.00, causal 0.87 (vs 0.89). For an escalation target that runs on a subset of items, the speed gain is valuable but less critical than for the first-pass model.

**The cross-model check anchor.** Runs nightly via Shadowfax to verify extraction consistency against the first-pass model. The most divergent field is `key_requirements` (Jaccard 0.07), suggesting that requirements extraction is the most model-sensitive part of the pipeline.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `qwen3.5:27b-int4` |
| Parameters | 27B dense |
| Size on disk | 16 GB (MLX int4) |
| Family | Qwen 3.5 |
| Context window | 256K |
| Think parameter | `think: false` (recommended) |
| Fleet role | Escalation target — extraction/classification leader |

## Open Questions

- Should gemma4:26b replace it as escalation target? The MoE model matches it on extraction (0.96 vs 0.94) at 5x the speed — but only has one eval run, so consistency is unknown.
- Does its causal reasoning improve with generic prompts? The prompt-fit gap may explain why it trails 14b on reasoning despite being a larger model.
- How does 17 GB interact with KV cache quantization for concurrent model loading? At q8_0, could it run alongside qwen3.5:4b simultaneously?

## Cross-References

- [[models/qwen3-32b]] — the model it replaced
- [[models/qwen3-14b]] — better at causal reasoning despite being smaller
- [[models/gemma4-26b]] — MoE challenger at the heavy tier
- [[techniques/kv-cache-quantization]] — enables concurrent loading
- [[techniques/cascade-routing-logprobs]] — Tier 3 (heavy) in the cascade

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-03-26 | Downloaded via Legolas — all qwen3.5 family, thinking off by default | OB insight |
| 2026-03-26 | First eval: extraction 0.94, classification 1.00, synthesis 0.67 | fleet-changelog |
| 2026-03-27 | Promoted to escalation target, replacing qwen3:32b | model-fleet.json |
| 2026-03-29 | Cross-model check: key_requirements most divergent field (Jaccard 0.07) | OB observation |
| 2026-04-04 | April rebaseline: synthesis dropped 0.67→0.22 (eval suite change, not regression) | fleet-changelog |
| 2026-04-06 | MLX int4 eval: extraction 0.92, causal 0.87, eval time 2132s (vs 5653s GGUF — 62% faster). Commit `3850dad` | fleet-changelog |
