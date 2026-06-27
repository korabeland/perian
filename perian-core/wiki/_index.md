# Palantir — Content Index

> Synthesized knowledge about Perian's models, techniques, and evaluation methodology.

## Models (12 pages)

| Page | Summary |
|------|---------|
| [qwen3-8b](models/qwen3-8b.md) | Retired workhorse — replaced by qwen3.5:9b-nvfp4, legacy eval contamination source |
| [qwen3-14b](models/qwen3-14b.md) | Causal reasoning specialist — retired from active fleet, replaced as Aragorn by gemma4:26b |
| [qwen3-32b](models/qwen3-32b.md) | Retired heavyweight — thinking overhead origin story, superseded by 27b |
| [qwen3.5-2b](models/qwen3.5-2b.md) | Triage gatekeeper — less context is better, KV cache sensitivity question |
| [qwen3.5-4b](models/qwen3.5-4b.md) | Reserve fast tier — 0.94 extraction (MLX nvfp4), strong but not in active fleet |
| [qwen3.5-9b](models/qwen3.5-9b.md) | Fleet workhorse — daily extraction via MLX nvfp4, replaced qwen3:8b |
| [qwen3.5-27b](models/qwen3.5-27b.md) | Escalation target — extraction champion, MLX int4 backend, cross-model check anchor |
| [qwen3.5-35b-a3b](models/qwen3.5-35b-a3b.md) | MoE for oversight — fast verification, not primary extraction |
| [gemma4-e4b](models/gemma4-e4b.md) | Edge model — thinking discovery, biggest KV cache speedup |
| [gemma4-26b](models/gemma4-26b.md) | Aragorn adversarial challenger — cross-family MoE, operational since Apr 5 |
| [gemma4-31b](models/gemma4-31b.md) | Extraction leader (stub) — 0.97, but slowest and thin data |
| [mistral-small3.2](models/mistral-small3.2.md) | Reasoning champion — fleet-best on judgment, think parameter landmine |

## Techniques (18 pages)

| Page | Summary |
|------|---------|
| [kv-cache-quantization](techniques/kv-cache-quantization.md) | Ollama KV cache q8_0/q4_0 — frees memory for concurrent models |
| [json-schema-constrained-decoding](techniques/json-schema-constrained-decoding.md) | Ollama format parameter — guaranteed valid JSON output |
| [cascade-routing-logprobs](techniques/cascade-routing-logprobs.md) | Logprob-based confidence escalation across model tiers |
| [moe-verification-layer](techniques/moe-verification-layer.md) | MoE as cheap verification instead of primary extraction |
| [bootstrap-confidence-intervals](techniques/bootstrap-confidence-intervals.md) | Statistical rigor for small eval suites |
| [cross-family-ensembles](techniques/cross-family-ensembles.md) | Cross-family models break correlated failures — applied in Aragorn |
| [mlx-backend-speed](techniques/mlx-backend-speed.md) | MLX backend via model format tags, +31% decode on nvfp4 — testing |
| [multi-llm-cdm](techniques/multi-llm-cdm.md) | Formal multi-LLM voting for structured output — dismissed (premature) |
| [producer-track-records](techniques/producer-track-records.md) | Qwen/Gemma/Mistral/Llama landscape — validates fleet strategy |
| [concurrent-model-loading](techniques/concurrent-model-loading.md) | Ollama OLLAMA_MAX_LOADED_MODELS for warm multi-model — queued |
| [generator-verifier-pattern](techniques/generator-verifier-pattern.md) | Anthropic's generator-verifier with criteria specificity — unreviewed |
| [parse-schema-optimization](techniques/parse-schema-optimization.md) | PARSE: LLM-optimized JSON schemas, +64.7% extraction accuracy — unreviewed |
| [constitutional-ai-small-models](techniques/constitutional-ai-small-models.md) | Safety guardrails degrade small model helpfulness by 9.8% — unreviewed |
| [effort-scaling-prompts](techniques/effort-scaling-prompts.md) | Anthropic's effort scaling + self-improving schemas + context-centric design — unreviewed |
| [vllm-mlx-inference](techniques/vllm-mlx-inference.md) | vllm-mlx: production MLX inference with continuous batching — unreviewed |
| [decoupled-structured-generation](techniques/decoupled-structured-generation.md) | Format Tax Mitigation: decouple reasoning from formatting — unreviewed |
| [gemma4-mtp-speculative-decoding](techniques/gemma4-mtp-speculative-decoding.md) | Gemma 4 MTP speculative decoding, 2-3x speedups on Mac — unreviewed |
| [two-level-nested-pairwise-bootstrap](techniques/two-level-nested-pairwise-bootstrap.md) | Two-level nested pairwise bootstrap for stochastic eval CIs — unreviewed |

## Evaluation (3 pages)

| Page | Summary |
|------|---------|
| [methodology-overview](evaluation/methodology-overview.md) | 6-level pipeline, 3 dimensions, 7 capability scores |
| [metric-definitions](evaluation/metric-definitions.md) | Every metric formula, known issues, planned improvements |
| [scoring-calibration](evaluation/scoring-calibration.md) | 10 scoring findings — 7 systemic issues + 3 calibration fixes (anchor snapping, enterprise penalty, Aragorn precision) |

## Meta

| Page | Purpose |
|------|---------|
| [README](README.md) | Methodology and what belongs here |
| [_schema](_schema.md) | Page templates, operations, conventions |
| [_index](_index.md) | This file |
| [_log](_log.md) | Change log |

---

**Total: 37 pages** (12 entity, 18 concept, 2 synthesis, 1 overview, 4 meta)
