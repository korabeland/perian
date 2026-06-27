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

The fleet's Aragorn adversarial challenger — a cross-family MoE model that breaks correlated Qwen failure modes. Adopted into the active fleet in April 2026 to replace qwen3:14b in the challenger role. The cross-family diversity (Gemma vs Qwen) is the strategic value: same-family challengers share blind spots, making disagreements less meaningful. Also a speed king — matches qwen3.5:27b on extraction while running 5x faster, with 0.96 extraction and 0.90 causal at 22 minutes total eval time.

## What We've Learned

**Speed without obvious quality sacrifice.** gemma4:26b completed a full L1-L6 eval in 1344 seconds (22 minutes) — comparable to qwen3.5:4b despite having 26B total parameters. With 3.8B active parameters per token, it's faster than models a fraction of its size. Extraction (0.96) beats qwen3.5:27b (0.94), causal reasoning (0.90) beats it too (0.89), and it's 5x faster.

**think:false works cleanly.** Smoke-tested 2026-04-08: with `think: false`, no thinking output, 0.9s warm response. With `think: true`, 587 chars of hidden reasoning, 5.4s. No silent overhead — the parameter is respected. Aragorn's nightly run (2026-04-07) with gemma4:26b completed successfully with think:false in production.

**Only one eval run.** Everything above comes from a single evaluation. The MoE consistency concern from qwen3.5:35b-a3b (extraction consistency 0.80) applies here too — different MoE architecture (128 experts + 1 shared, vs 256 experts) but the same fundamental question: does sparse routing introduce variance on structured output tasks?

**Potential escalation target replacement.** If the scores hold, the upgrade case is strong: 18 GB (vs 17 GB for 27b), similar quality, 5x faster. The 1 GB size penalty is negligible. But "if the scores hold" is doing heavy lifting — need at least 3 eval runs.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `gemma4:26b` |
| Parameters | 25.2B total (3.8B active) |
| Size on disk | 18 GB |
| Family | Gemma 4 |
| Context window | 256K |
| Architecture | MoE — 128 experts + 1 shared, 3.8B active |
| Think parameter | `think: false` (confirmed working) |
| License | Apache 2.0 |
| External benchmarks | AIME 88.3%, MMLU-Pro 82.6% |

## Open Questions

- Do scores hold across 3+ eval runs? This is the single blocking question.
- How does extraction consistency compare to qwen3.5:27b dense?
- Non-Qwen family — what's the prompt-fit gap with Perian's qwen3-optimized prompts?
- How does it interact with KV cache quantization at 18 GB?

## Cross-References

- [[models/qwen3.5-27b]] — current escalation target it could replace
- [[models/qwen3.5-35b-a3b]] — competing MoE architecture (different expert count/strategy)
- [[models/gemma4-31b]] — same family, dense variant
- [[techniques/moe-verification-layer]] — MoE verification pattern

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-04-03 | Found by Legolas, downloaded | model-fleet.json |
| 2026-04-04 | Single eval: extraction 0.96, causal 0.90, eval time 1344s | fleet-changelog |
| 2026-04-05 | "Fleet speed king" — 22min eval at 27b-class quality | OB observation |
| 2026-04-07 | First Aragorn production run with gemma4:26b — 9 challenged, 6 disagreements | nightly digest |
| 2026-04-08 | think:false smoke test passed — no silent overhead, 0.9s warm | manual test |
| 2026-04-12 | Operational as Aragorn challenger for 7+ days. Nightly runs stable via Shadowfax | production |
