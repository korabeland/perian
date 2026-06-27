---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-06
confidence: medium
status: queued
---

## Summary

Ollama supports concurrent model loading via `OLLAMA_MAX_LOADED_MODELS`. On 48GB unified memory with the MLX backend, practical budget is ~38-40GB for models after OS overhead. A 3-model concurrent setup (8B + 14B + 27B = ~31GB weights) fits with room for KV caches. Combined with KV cache quantization, this eliminates cold-start penalty entirely for the cascade router. **Queued**: not urgent — current keep_alive approach is simpler. Revisit when multi-model latency becomes a bottleneck.

## How It Works

`OLLAMA_MAX_LOADED_MODELS` (available since Ollama v0.1.33) controls how many models stay resident in memory simultaneously. Default is 1 (sequential loading with keep_alive timeouts).

Key configuration:
- `OLLAMA_MAX_LOADED_MODELS=3` — keep three models warm
- `OLLAMA_NUM_PARALLEL` — concurrent requests per model
- Combined with [[techniques/kv-cache-quantization]] (q8_0) to free headroom for KV caches

Memory budget on 48GB M5 Pro:
```
48GB total
 - ~8-10GB OS + apps
= ~38-40GB for models

3-model concurrent (MLX nvfp4):
  qwen3.5:9b-nvfp4    8.9GB  (primary extractor)
  gemma4:26b          17.0GB  (Aragorn challenger)
  qwen3.5:27b-int4   16.0GB  (escalation target)
  Total:             ~41.9GB  (tight, may need to drop one)

2-model concurrent:
  qwen3.5:9b-nvfp4    8.9GB
  gemma4:26b          17.0GB
  Total:             ~25.9GB  (comfortable headroom)
```

MLX's unified memory optimization eliminates CPU-GPU transfer overhead that llama.cpp suffers from when models don't fully fit in GPU VRAM (less relevant on Apple Silicon, but still a win for KV cache management).

## Perian Relevance

Perian's sequential pipeline (9B extract → gemma4:26b challenge → 27B escalate) currently pays model-loading latency between stages. With concurrent loading, all models stay warm and respond immediately.

**Why queued**: The current approach uses `keep_alive` timeouts to manage model loading, which works well enough for the nightly pipeline (sequential, single-threaded). Concurrent loading becomes important when:
- CDM verification ([[techniques/multi-llm-cdm]]) requires running multiple models per item
- Pipeline throughput needs to increase (currently ~23 items/night is sufficient)
- Real-time/interactive scoring is added (not currently planned)

**Revisit conditions**: multi-model latency becomes a measurable bottleneck, or CDM verification is adopted.

## Source Material

The concurrent loading feature was documented through Ollama GitHub discussions and community benchmarks on Apple Silicon. Key insight: the `OLLAMA_MAX_LOADED_MODELS` parameter is well-tested but the memory budgeting requires careful measurement under load — the KV cache grows dynamically and can push total usage beyond the static weights calculation.

- [Apple Silicon LLM Inference Deep Dive](https://www.youngju.dev/blog/culture/2026-03-18-apple-silicon-llm-inference-deep-dive.en)
- [Ollama Multi-Model Loading Discussion](https://github.com/ollama/ollama/issues/9787)

## Cross-References

- [[techniques/kv-cache-quantization]] — reduces KV cache footprint, enabling more concurrent models
- [[techniques/mlx-backend-speed]] — MLX memory efficiency supports concurrent loading
- [[techniques/multi-llm-cdm]] — CDM would make concurrent loading essential

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-05 | Gandalf finding (medium actionability) | finding-2026-04-05-5 |
| 2026-04-06 | Queued — not urgent, revisit when multi-model latency is a bottleneck | human review |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
