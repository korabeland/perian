---
type: concept
domain: techniques
created: 2026-04-12
updated: 2026-04-12
confidence: low
status: unreviewed
---

## Summary

vllm-mlx brings vLLM's production features (continuous batching, PagedAttention, OpenAI-compatible API) natively to Apple Silicon via MLX. Benchmarks on M4 Max show 21-87% higher throughput than llama.cpp. Key advantage: continuous batching scales to 4.3x aggregate throughput at 16 concurrent requests (Ollama's throughput stays flat). Text prefix caching achieves 5.8x speedup on shared prompt prefixes. Represents significant infrastructure change from Ollama's pull-and-run model.

## How It Works

vllm-mlx is an open-source framework that ports vLLM's key features to Apple Silicon via the MLX framework:

- **Continuous batching**: Dynamically batches requests, scaling throughput with concurrency (4.3x at 16 concurrent requests vs. Ollama's flat throughput)
- **PagedAttention**: Efficient KV cache memory management, reducing waste from fixed-size cache allocations
- **OpenAI-compatible API**: Drop-in replacement for existing API calls
- **Text prefix caching**: 5.8x speedup when requests share prompt prefixes (system prompt reuse)

Benchmarks (M4 Max):
| Model Size | Throughput vs llama.cpp |
|------------|----------------------|
| 0.6B | +21% |
| 8B | +40-60% |
| 30B | +87% |

The throughput advantage grows with model size and concurrency. For single-request, sequential inference (Perian's current pattern), the advantage is smaller.

## Perian Relevance

Two potential improvements over Ollama:

1. **Prefix caching (5.8x speedup)**: Perian reuses the same extraction prompt prefix across hundreds of listings. vllm-mlx's prefix caching could cut first-pass extraction time dramatically by caching the shared system prompt + profile across the batch.

2. **Continuous batching**: Processing multiple listings simultaneously rather than sequentially. The 4.3x throughput at 16 concurrent requests would transform the nightly pipeline from ~8 minutes to ~2 minutes.

**Trade-offs**:
- Significant infrastructure change from Ollama's simple `pull-and-run` model
- Ollama 0.19's MLX backend with improved caching may close part of the gap
- Model management (downloading, updating, format conversion) becomes manual
- No built-in model switching — the fleet router would need adaptation
- Community and documentation much smaller than Ollama

**Assessment**: Worth benchmarking but not adopting near-term. The 5.8x prefix caching is the most compelling feature. Monitor whether Ollama adds comparable prefix caching in future releases before committing to infrastructure migration.

## Source Material

The vllm-mlx paper (arXiv 2601.19139) provides detailed benchmarks comparing against llama.cpp and Ollama on Apple Silicon. The continuous batching implementation is particularly well-characterized, showing that Apple Silicon's unified memory architecture is well-suited to vLLM's memory management patterns. The prefix caching results are measured on shared system prompts — exactly Perian's use case.

- [vllm-mlx: Production MLX Inference (arXiv 2601.19139)](https://arxiv.org/html/2601.19139v2)

## Cross-References

- [[techniques/mlx-backend-speed]] — Ollama's MLX backend is the current approach; vllm-mlx is a potential alternative
- [[techniques/concurrent-model-loading]] — vllm-mlx handles concurrency differently (batching vs. multi-model)

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-12 | Gandalf finding (medium actionability) | finding-2026-04-12-5 |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
