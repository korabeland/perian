---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-05
sources:
  - autoresearch/methodology/finding-2026-03-27-1.md
confidence: medium
status: current
---

## Summary

Ollama supports KV cache quantization via `OLLAMA_KV_CACHE_TYPE=q8_0` (50% cache memory reduction) or `q4_0` (75% reduction). Requires `OLLAMA_FLASH_ATTENTION=1`. Frees VRAM for longer contexts or concurrent model loading on the 48 GB M5 Pro. Negligible quality impact at q8_0 for most tasks. **Status: skipped** — not yet tested on Perian's pipeline.

## How It Works

The KV cache stores precomputed attention vectors for all previous tokens during generation. It grows linearly with context length and is often the largest memory consumer after model weights.

```bash
export OLLAMA_FLASH_ATTENTION=1    # Required prerequisite
export OLLAMA_KV_CACHE_TYPE=q8_0   # Or q4_0 for more aggressive compression
```

| Mode | Cache reduction | Quality impact |
|------|----------------|----------------|
| q8_0 | ~50% vs FP16 | Negligible on most tasks |
| q4_0 | ~75% vs FP16 | Slight degradation possible |

**Caveat:** Models with high GQA (Grouped Query Attention) counts — including the Qwen family — may see slightly more quality impact from cache quantization.

## Memory Budget Impact (48 GB M5 Pro)

| Config | Weights | KV Cache (4K ctx) | Available |
|--------|---------|-------------------|-----------|
| qwen3.5:27b @ FP16 cache | 17 GB | ~2 GB | ~21 GB |
| qwen3.5:27b @ q8_0 cache | 17 GB | ~1 GB | ~22 GB |
| + qwen3.5:4b warm | +3.4 GB | +0.3 GB | ~18.3 GB |

The key win: keeping two models warm simultaneously for the cascade router.

## Perian Relevance

- Enables concurrent model loading for [[techniques/cascade-routing-logprobs]]
- Supports longer context windows for complex listings without OOM risk
- Directly impacts Phase 2 router `keep_alive` strategy

## Next Steps (From Brief)

1. Benchmark quality: run holdout eval with and without q8_0
2. Benchmark throughput: measure tok/s with Flash Attention enabled
3. If quality holds: update Ollama launch configuration permanently
4. Multi-model warm test: two models simultaneously with q8_0

## Cross-References

- [[models/qwen3.5-27b]] — primary beneficiary (17 GB weights + reduced cache)
- [[models/qwen3.5-4b]] — candidate for concurrent warm loading
- [[techniques/cascade-routing-logprobs]] — cascade router needs fast model switching
- [[evaluation/methodology-overview]] — eval needed to validate quality impact

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-03-27 | Gandalf finding (high actionability) | finding-2026-03-27-1 |
| 2026-04-05 | Palantir page created | palantir seed |
