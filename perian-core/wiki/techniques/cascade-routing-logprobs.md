---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-05
sources:
  - autoresearch/methodology/finding-2026-03-27-3.md
confidence: medium
status: current
---

## Summary

Cascade routing starts with the cheapest model and escalates to a larger model if output confidence is low. Confidence signal: mean log probability of content tokens in the generated JSON. Research shows this achieves comparable quality to always using the large model at 40-60% of the cost. **Status: wired but dormant** — Perian collects logprobs but Ollama 0.20.0 still doesn't return them; escalation threshold (`mean_logprob < -1.5`) is in logging-only mode.

## The Cascade Pattern

```
Input → Small Model (2-4B) → Confidence Check
                                 ├── High confidence → Return result
                                 └── Low confidence → Medium Model (9-14B) → Confidence Check
                                                                                 ├── High → Return
                                                                                 └── Low → Heavy (27B) → Return
```

## Confidence Signal

1. Generate full JSON response from the small model
2. Collect per-token logprobs (Perian already does this)
3. **Filter out structural tokens** — especially important with [[techniques/json-schema-constrained-decoding]] since brackets/colons are grammar-forced
4. Compute mean logprob of remaining content tokens
5. Compare against calibrated threshold

**Threshold calibration:** Run the small model on holdout data with ground-truth labels. Find the logprob threshold giving (e.g.) 90% precision on "correctly extracted" items.

## Proposed Perian Implementation

| Tier | Model | Role |
|------|-------|------|
| Tier 1 (fast) | [[models/qwen3.5-4b]] or [[models/qwen3.5-2b]] | Handle all items first |
| Tier 2 (medium) | [[models/qwen3.5-9b]] | Re-process low-confidence items |
| Tier 3 (heavy) | [[models/qwen3.5-27b]] | Handle remaining hard cases |

**Expected impact:** If 60-70% of listings are straightforward, the small model handles them. Only ambiguous listings escalate. Average latency drops significantly.

## Infrastructure Status

**Already in place:**
- Logprob collection on every call (`"options": {"logprobs": true}`)
- Three-tier fleet (soon qwen3.5 variants)
- Per-phase `keep_alive` management
- Router config in `config/router.yaml` with threshold setting

**Needed:**
- Content-token logprob extraction (filter structural tokens)
- Threshold calibration on holdout data
- Cascade controller managing escalation flow
- Per-item logging of which tier handled it

**Blocker:** Ollama 0.20.0 does not actually return logprobs despite accepting the parameter. Escalation is wired but dormant.

## Cross-References

- [[techniques/json-schema-constrained-decoding]] — grammar-forced tokens must be filtered from logprob calculation
- [[techniques/kv-cache-quantization]] — enables keeping multiple models warm for fast switching
- [[techniques/moe-verification-layer]] — alternative MoE-based verification pattern
- Model entity pages in `models/` — tier model selection based on capability profiles
- [[evaluation/scoring-calibration]] — calibration methodology for threshold setting

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-03-27 | Gandalf finding (high actionability) | finding-2026-03-27-3 |
| 2026-04-05 | Palantir page created | palantir seed |
