---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-12
sources:
  - open-brain
  - state/model-fleet.json
confidence: high
status: current
---

## Summary

The surprise performer — nearly matches qwen3:8b on extraction at 35% less memory. A strong candidate for the fast tier in the proposed fleet restructure. The question is whether its edge is real or whether 8b's prompt-fit advantage masks a true capability gap.

## What We've Learned

**Remarkably close to qwen3:8b.** First eval (March 27): extraction 0.89, classification 0.97 — nearly identical to 8b (0.90, 0.97). By April, 4b's extraction improved to 0.93 while 8b's dropped to 0.88. On paper, the 4B model is now *better* than the 8B model at extraction. At 3.4 GB vs 5.0 GB, that's a significant efficiency win.

**Drops hard on causal reasoning.** March eval showed causal at 0.47, far below 8b's 0.60. By April (with the distance-based L4 fix), both improved — 4b to 0.77, 8b to 0.77. They converged. This suggests the original gap was partly an artifact of binary scoring. Still, causal reasoning isn't this model's strength.

**Viable as extraction-only tier.** The insight from OB: "viable as ultra-fast extraction-only tier where multi-step reasoning isn't needed." In the cascade routing pattern, 4b handles the 60-70% of straightforward listings, escalating complex ones. At 3.4 GB, two of these could fit in memory alongside qwen3.5:27b (17 GB) with headroom.

**Consistent improvement across evals.** Extraction steadily improved: 0.89 → 0.92 → 0.93 across three eval runs. This is unusual — most models show variance, not a trend. May indicate the eval suite changes (semantic matching, distance-based scoring) disproportionately benefit models that paraphrase well rather than copying verbatim.

**MLX nvfp4 variant evaluated.** The Apr 6 eval of qwen3.5:4b-nvfp4 showed extraction 0.94 and eval time 559s (vs 1313s GGUF — 57% faster). Quality within noise of GGUF. Kept as reserve — not adopted into the active fleet because qwen3.5:9b-nvfp4 fills the first-pass role, and there's no active use case for a second fast-tier model.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `qwen3.5:4b` |
| Parameters | 4B |
| Size on disk | 3.4 GB |
| Family | Qwen 3.5 |
| Context window | 256K |
| Think parameter | OFF by default |
| Fleet role | Reserve — MLX nvfp4 variant evaluated, not in active fleet |

## Open Questions

- Is the extraction lead over 8b real capability or an artifact of eval suite changes that favor paraphrasing models?
- How does it perform with Perian's qwen3-optimized prompts vs generic prompts? Prompt-fit gap is the key unknown.
- Can it reliably handle the full pipeline (extract + score) or only extraction?

## Cross-References

- [[models/qwen3-8b]] — the model it would replace at the fast tier
- [[models/gemma4-e4b]] — alternative fast-tier candidate (larger but multimodal)
- [[techniques/cascade-routing-logprobs]] — fast-tier model in the cascade
- [[techniques/kv-cache-quantization]] — enables concurrent loading with heavy model

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-03-26 | Downloaded via Legolas | fleet-changelog |
| 2026-03-26 | First eval: extraction 0.89, causal 0.47 — "surprise performer" | OB observation |
| 2026-04-04 | Extraction improved to 0.93 — now surpasses qwen3:8b (0.88) | fleet-changelog |
| 2026-04-06 | MLX nvfp4 eval: extraction 0.94, eval time 559s (vs 1313s GGUF — 57% faster). Commit `3850dad` | fleet-changelog |
