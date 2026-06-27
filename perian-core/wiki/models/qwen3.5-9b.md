---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-12
sources:
  - open-brain
  - state/model-fleet.json
confidence: medium
status: current
---

## Summary

The fleet's daily workhorse since April 2026 (as qwen3.5:9b-nvfp4, MLX backend). Replaced qwen3:8b for first-pass extraction and scoring. Originally puzzling — better on paper (MMMU-Pro 69% vs 57%) but tied in practice on our eval suite. The MLX nvfp4 variant resolved the value question: +31% decode speed and +388% prefill speed over GGUF, quality within noise. The benchmark paradox was real but moot once speed made it the clear choice.

## What We've Learned

**The benchmark paradox.** qwen3.5:9b beats qwen3:8b on every published benchmark. But on Perian's structured extraction tasks, it's a wash — 0.93 vs 0.88 extraction (better), 0.82 vs 0.89 conditional (worse), 0.97 vs 0.97 classification (tied). The conditional reasoning drop is the red flag. Our prompts were autoresearch-optimized for qwen3; the 9b model may parse them differently, losing points on format rather than substance.

**May express answers differently.** The eval leakage insight applies here directly: rigid string matching penalizes models that phrase answers differently. "Customer Experience Manager" vs "CX Manager" — same meaning, zero Jaccard overlap. The 9b model, coming from a different generation of the Qwen family, may legitimately produce better answers that score lower because they're phrased differently from 8b's training-contaminated ground truth.

**MLX nvfp4 changed the equation.** The GGUF variant at 6.6 GB was an awkward middle ground — larger than 8b without clearly justifying the extra memory. The MLX nvfp4 variant (8.9 GB) resolved this by trading size for speed: +31% decode speed and +388% prefill speed over the GGUF equivalent. At that speed, it became the obvious first-pass replacement for qwen3:8b. Quality is within noise of the GGUF variant (extraction 0.91-0.92 across runs).

**Second eval confirmed consistency.** The Apr 10 5-run eval (qwen3.5:9b-nvfp4) showed extraction 0.92, causal 0.85, nuanced 0.84 — virtually identical to the Apr 5 3-run eval. No drift, no variance surprises. This is the stability a daily workhorse needs.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `qwen3.5:9b-nvfp4` |
| Parameters | 9B |
| Size on disk | 8.9 GB (MLX nvfp4) |
| Family | Qwen 3.5 |
| Context window | 256K |
| Think parameter | OFF by default |
| Fleet role | First pass — daily extraction and scoring (MLX nvfp4, 8.9 GB) |

## Open Questions

- Does the conditional reasoning drop disappear with generic prompts? This is the critical test — if yes, it's prompt-fit; if no, 8b was genuinely better at reasoning on our task type.
- How does long-term extraction quality track? Two evals show consistency, but need more data points over weeks of production use.

## Cross-References

- [[models/qwen3-8b]] — the model it should beat but doesn't (on our eval)
- [[models/qwen3.5-4b]] — smaller model that nearly matches it on extraction
- [[evaluation/scoring-calibration]] — eval contamination explains the paradox

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-03-26 | Downloaded via Legolas | fleet-changelog |
| 2026-03-26 | First eval: "surprisingly close to qwen3:8b despite better benchmarks" | OB observation |
| 2026-04-04 | April rebaseline: extraction 0.93, conditional 0.82 | fleet-changelog |
| 2026-04-05 | MLX nvfp4 eval: extraction 0.91, causal 0.85, eval time 929s (vs ~1970s GGUF) | fleet-changelog |
| 2026-04-06 | Fleet migration: adopted as first-pass, replacing qwen3:8b. Commit `3850dad` | model-fleet.json |
| 2026-04-10 | Second eval (5-run): extraction 0.92, causal 0.85, nuanced 0.84 — consistent | fleet-changelog |
