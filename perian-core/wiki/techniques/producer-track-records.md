---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-06
confidence: medium
status: dismissed
---

## Summary

As of April 2026, Qwen is the most commercially deployed open-weight family globally (40M+ HF pulls, Apache 2.0, widest size range). Gemma 4 moved to Apache 2.0 (prior versions had restrictive ToU), benchmarks competitively, and gets fast Ollama support. Mistral ships reliably under Apache 2.0. Llama has the largest ecosystem but uses the more restrictive Llama License. **Dismissed**: validates existing fleet evolution strategy (Qwen primary + Gemma challenger). No new action needed.

## How It Works

This is a landscape assessment, not a technique. It tracks the four major open-weight LLM producers relevant to local inference:

| Producer | License | Key Strength | Ollama Support |
|----------|---------|-------------|----------------|
| **Qwen** (Alibaba) | Apache 2.0 | Widest size range (0.6B-235B), fastest Ollama tags | Same-day |
| **Gemma** (Google) | Apache 2.0 (v4+) | Competitive benchmarks, MoE variants | Within days |
| **Mistral** (Mistral AI) | Apache 2.0 | Strong enterprise adoption, reasoning | Fast |
| **Llama** (Meta) | Llama License | Largest ecosystem (75K+ GitHub stars) | Fast |

All four maintain consistent release quality with no major regressions. Gemma 4's Apache 2.0 license removes the licensing concern that made it second-choice for pipeline modifications.

## Perian Relevance

Validates the fleet evolution policy's multi-family approach:
- **Qwen as primary family**: Apache 2.0, fastest Ollama support, widest size range from any single lab. Safest choice for the core extraction fleet.
- **Gemma as challenger/diversity family**: Competitive benchmarks, permissive license (as of v4), fast Ollama support. Best pick for the cross-family verification role.
- **Mistral viable but narrower**: Good reasoning but fewer size options for fleet composition.
- **Llama less flexible**: Llama License restrictions limit pipeline modification freedom.

No action required — this finding confirmed existing strategy rather than suggesting changes.

## Source Material

The landscape analysis drew from multiple sources tracking open-weight LLM adoption and licensing as of April 2026. Key data point: Gemma 4's license change to Apache 2.0 was the significant development that made it fully viable for Perian's pipeline modifications without legal concern.

- [Gemma 4 and What Makes an Open Model (Interconnects)](https://www.interconnects.ai/p/gemma-4-and-what-makes-an-open-model)
- [Open Source AI Landscape April 2026 (Digital Applied)](https://www.digitalapplied.com/blog/open-source-ai-landscape-april-2026-gemma-qwen-llama)

## Cross-References

- [[models/gemma4-26b]] — Gemma's Apache 2.0 license enabled its adoption as Aragorn challenger
- [[techniques/cross-family-ensembles]] — producer diversity is the mechanism behind cross-family benefits

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-05 | Gandalf finding (medium actionability) | finding-2026-04-05-4 |
| 2026-04-06 | Dismissed — validates existing strategy, no new action | human review |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
