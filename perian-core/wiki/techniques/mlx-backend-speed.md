---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-06
confidence: high
status: testing
---

## Summary

Ollama v0.19+ added Apple's MLX framework as an inference backend. **Critical correction**: MLX is NOT activated by environment variables — it activates automatically when running MLX-native model format tags (nvfp4, int4, mlx-bf16). GGUF models always use ggml/llama.cpp Metal. Benchmarked on M5 Pro: qwen3.5:9b-nvfp4 vs Q4_K_M shows +31% decode, +388% prefill, -38% wall time, with quality identical within noise. **Status: testing** — fleet-wide MLX format migration under evaluation.

## How It Works

Ollama selects the inference runner based on **model format**, not configuration:

| Format | Example Tag | Size (9B) | Backend |
|--------|-------------|-----------|---------|
| GGUF (Q4_K_M, Q8_0, etc.) | `qwen3.5:9b` | 6.6GB | ggml/llama.cpp Metal |
| `nvfp4` | `qwen3.5:9b-nvfp4` | 8.9GB | MLX |
| `int4` | `qwen3.5:9b-int4` | ~6GB | MLX |
| `int8` | `qwen3.5:9b-int8` | ~10GB | MLX |
| `mlx-bf16` | `qwen3.5:9b-mlx-bf16` | ~19GB | MLX |

Env vars that do NOT switch to MLX: `OLLAMA_RUNNERS=mlx`, `OLLAMA_NEW_ENGINE=true`, `OLLAMA_LLM_LIBRARY=mlx`.

**Headline benchmark caveat**: The 57% prefill / 93% decode numbers were from qwen3.5:35b-a3b (MoE) on M5 **Max**, not Pro. MoE models see disproportionate MLX gains due to optimized expert routing. Dense 9B on M5 Pro expects 1.3-1.8x decode improvement.

## Perian Relevance

MLX speed gains require pulling different model tags (nvfp4 at 8.9GB vs Q4_K_M at 6.6GB), changing the fleet memory budget calculus. Benchmarked results on M5 Pro:

| Metric | Q4_K_M (ggml) | nvfp4 (MLX) | Change |
|--------|---------------|-------------|--------|
| Decode | 33 tok/s | ~43 tok/s | +31% |
| Prefill | 268 tok/s | ~1308 tok/s | +388% |
| Wall time | baseline | -38% | significant |
| Quality | baseline | identical (within noise) | no regression |

Fleet memory impact (nvfp4 vs Q4_K_M):

| Model | GGUF Q4_K_M | nvfp4 | Delta |
|-------|-------------|-------|-------|
| qwen3.5:9b | 6.6GB | 8.9GB | +2.3GB |
| qwen3.5:27b | 17GB | ~23GB | +6GB |
| qwen3.5:2b | 2.7GB | ~3.5GB | +0.8GB |

**Next step**: Survey MLX-native tags across ALL fleet models before committing to fleet-wide migration. MLX tags currently available for Qwen3.5 only; other families use `mlx-lm` with HuggingFace models.

## Source Material

Original Ollama blog post details the MLX runner architecture and benchmarks on various Apple Silicon chips. The runner selection is based on model manifest metadata — the `format` field in the model's Modelfile determines which backend loads.

The smoke-test that uncovered the env var misconception: setting `OLLAMA_RUNNERS=mlx` and running qwen3.5:9b (GGUF) showed no speed change because GGUF models are structurally incompatible with the MLX runner.

- [Ollama Blog: MLX on Apple Silicon](https://ollama.com/blog/mlx)
- [DeepWiki: MLX Runner Architecture](https://deepwiki.com/ollama/ollama/5.7-mlx-runner-(apple-silicon))
- [Ollama MLX Runner PR #14185](https://github.com/ollama/ollama/pull/14185)

## Cross-References

- [[models/qwen3.5-9b]] — primary fleet model, benchmarked nvfp4 vs Q4_K_M
- [[models/qwen3.5-27b]] — escalation target, int4 MLX adopted
- [[models/qwen3.5-4b]] — reserve tier, nvfp4 quality preserved
- [[techniques/concurrent-model-loading]] — MLX memory efficiency enables concurrent loading

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-05 | Gandalf finding (high actionability) | finding-2026-04-05-2 |
| 2026-04-06 | Reviewed — corrected env var misconception, benchmarked nvfp4 vs Q4_K_M | smoke test + human review |
| 2026-04-07 | Fleet-wide MLX eval: 2b-nvfp4 too low quality, 4b/9b/27b/35b-a3b all adopted | eval runs |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
