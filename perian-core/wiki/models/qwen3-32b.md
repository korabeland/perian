---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-05
sources:
  - open-brain
  - state/model-fleet.json
confidence: high
status: current
---

## Summary

The retired heavyweight. Once the primary model, now deprecated after qwen3.5:27b proved smaller (17 GB vs 20 GB) and better on every metric. Its main legacy is a cautionary tale: bigger isn't always better, and thinking overhead can mask a model's actual performance.

## What We've Learned

**Thinking overhead was the original sin.** The 25-minute pipeline runs that nearly killed Perian's daily automation were qwen3:32b generating 3,800 hidden reasoning tokens for every 100 tokens of output. The Datadog listing timeout that led to discovering `think: false` was this model's doing. With thinking disabled, it became usable — but by then, smaller models had already proven competitive.

**Bigger isn't better for causal reasoning.** The most surprising eval result: qwen3:14b (9.3 GB) consistently outscored qwen3:32b (20 GB) on causal reasoning (0.70 vs 0.60). The larger model had more knowledge but apparently less focused reasoning. This insight shaped the specialist fleet architecture — pick models for their strengths, not their size.

**Completely superseded by qwen3.5:27b.** By March 27, qwen3.5:27b beat or matched 32b on every dimension while being 3 GB smaller. An OB thought captured it perfectly: "Easiest fleet swap decision — no reason to keep 32b unless as an escalation safety net during transition." It was demoted to "heavy_legacy" and kept around only for reference.

**Incomplete eval data.** Never ran L1/L2 (extraction/classification) — by the time the full eval suite was built, the decision to replace it was already made. Only has L3-L6 scores from March 2026.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `qwen3:32b` |
| Parameters | 32B dense |
| Size on disk | 20 GB |
| Family | Qwen 3 |
| Context window | 128K |
| Think parameter | `think: false` (required) |
| Fleet role | Legacy — phased out |

## Open Questions

- None active. This model is retired. Keeping the page as a historical reference and as the origin story for several fleet-wide discoveries (thinking overhead, bigger ≠ better).

## Cross-References

- [[models/qwen3.5-27b]] — the model that replaced it
- [[models/qwen3-14b]] — the smaller model that outperformed it on reasoning
- [[models/qwen3-8b]] — the model that proved 8B is sufficient for extraction

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-03-22 | Thinking overhead discovered via Datadog timeout — 3800 tokens wasted | OB insight |
| 2026-03-22 | Added as primary model, upgraded from qwen2.5-coder:32b | OB decision |
| 2026-03-23 | Eval (L3-L6 only): conditional 0.89, causal 0.60 — beaten by 14b | fleet-changelog |
| 2026-03-27 | qwen3.5:27b beats it on everything — demoted to legacy | OB observation |
