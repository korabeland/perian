---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-12
confidence: medium
status: current
---

## Summary

MoE models like qwen3.5:35b-a3b (3B active from 35B total) offer large-model knowledge at small-model inference speed. Instead of using MoE as a primary extractor, use it as a **cheap verification layer** — run the small model for extraction, then have the MoE model verify the output. **Status: operational** — gemma4:26b serves as Aragorn's cross-family adversarial challenger since Apr 5, running nightly via Shadowfax.

## The Idea

Traditional cascade: small → medium → large (each does full extraction).
MoE verification: small does extraction → MoE verifies extraction (cheaper than re-extracting).

The MoE model's 35B-class knowledge can spot extraction errors at 3B-active-parameter speed. This is faster than running a dense 27B model for verification and potentially more capable than running a small model twice.

## Applicable Models

| Model | Total Params | Active Params | Speed Profile |
|-------|-------------|---------------|---------------|
| [[models/qwen3.5-35b-a3b]] | 35B | 3B | 1822s eval (vs 5653s for 27b dense) |
| [[models/gemma4-26b]] | 25.2B | 3.8B | 1344s eval |

## Perian Relevance

- **Operational since Apr 5**: gemma4:26b (25.2B total, 3.8B active) runs as Aragorn's nightly cross-family adversarial challenger via Shadowfax
- Replaced qwen3:14b (9.3 GB dense, same Qwen family) — the cross-family diversity is the key insight: same-family challengers share blind spots
- The router could use MoE verification as a confidence signal alongside logprobs (future)

## Open Questions

1. Does MoE verification produce consistent results for structured JSON validation? (Early evidence: gemma4:26b's nightly runs have been stable since Apr 5)
2. Is the verification prompt different enough from extraction that MoE inconsistency doesn't matter?
3. Should MoE verification feed back into router confidence, or stay purely as an oversight layer?

## Cross-References

- [[models/qwen3.5-35b-a3b]] — primary MoE candidate
- [[models/gemma4-26b]] — alternative MoE candidate
- [[models/qwen3-14b]] — current Aragorn challenger it could augment
- [[techniques/cascade-routing-logprobs]] — integration with cascade routing

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-03-27 | Gandalf finding (medium actionability) | finding-2026-03-27-4 |
| 2026-04-05 | Palantir page created | palantir seed |
| 2026-04-05 | gemma4:26b adopted as Aragorn challenger — MoE verification now operational. Commit `3850dad` | fleet migration |
| 2026-04-07 | First nightly Aragorn run with gemma4:26b — 9 challenged, 6 disagreements | nightly digest |
