---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-05
sources:
  - open-brain
  - state/model-fleet.json
confidence: medium
status: current
---

## Summary

The model that taught us to match architecture to role. As a primary extractor, its MoE sparse routing introduces consistency problems that matter for structured JSON. As a verification layer, its 35B knowledge at 3B-active speed could be exactly right. The lesson: MoE models shine in oversight roles, not structured output generation.

## What We've Learned

**MoE is not a free lunch for structured output.** Extraction consistency dropped to 0.80 vs 1.00 for the dense 27b equivalent. When every token in a JSON schema matters, sparse expert routing introduces minor inconsistencies. The model is fast (3.8x faster than 27b dense) and knowledgeable (35B total parameters), but the randomness in which experts activate per token creates variance that structured output tasks expose.

**The verification insight.** An OB thought crystallized the right mental model: "Wide knowledge + light footprint = oversight, not production." The MoE profile — 35B-class knowledge at 3B-active-parameter speed — is ideal for *checking* output rather than *generating* it. Aragorn's adversarial challenge role, currently held by qwen3:14b, is exactly this kind of oversight task. The question is whether MoE verification produces consistent enough judgments.

**Uniquely strong at synthesis.** In the April rebaseline, 35b-a3b scored 0.63 on synthesis vs 0.14-0.24 for all other models. This was flagged as unusual — the broad expert knowledge may genuinely help with cross-document summarization, even if it hurts single-document extraction consistency.

**Causal reasoning improved dramatically between eval versions.** March: 0.57. April: 0.91. The jump is entirely due to eval suite changes (distance-based L4 scoring), not model improvement. This is a cautionary tale about comparing scores across eval versions without provenance tracking.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `qwen3.5:35b-a3b` |
| Parameters | 35B total (3B active) |
| Size on disk | 24 GB |
| Family | Qwen 3.5 |
| Context window | 256K |
| Architecture | MoE — 256 experts, 3B active per token |
| Think parameter | `think: false` (recommended) |
| Fleet role | Candidate — verification layer or synthesis specialist |

## Open Questions

- Is MoE verification consistent enough for Aragorn's challenger role? Need to test verification-style prompts (check this output) vs generation-style prompts (produce this output).
- Can it serve as a cheap synthesis step in the pipeline — summarizing multiple listings — where consistency matters less than breadth?
- How does it compare to gemma4:26b (different MoE architecture, 3.8B active, 18 GB) on the same tasks?

## Cross-References

- [[models/qwen3.5-27b]] — dense equivalent, more consistent but 3.8x slower
- [[models/gemma4-26b]] — competing MoE architecture
- [[models/qwen3-14b]] — current Aragorn challenger it could augment
- [[techniques/moe-verification-layer]] — the architectural pattern

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-03-26 | Downloaded via Legolas | fleet-changelog |
| 2026-03-27 | First eval: extraction consistency 0.80 — MoE inconsistency on structured output | OB observation |
| 2026-03-27 | "MoE for oversight, not production" insight captured | OB insight |
| 2026-04-05 | April rebaseline: causal 0.91 (was 0.57 — eval version change) | fleet-changelog |
