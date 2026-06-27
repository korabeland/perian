---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-05
sources:
  - state/model-fleet.json
  - state/fleet-changelog.jsonl
confidence: low
status: stub
---

## Summary

The fleet's extraction leader on paper — 0.97, the highest of any model tested. But also the slowest model at 115 minutes per eval, and only one eval run. A dense 31B model from the Gemma 4 family with strong external benchmarks (#3 on Arena AI text leaderboard) but insufficient Perian-specific data to draw conclusions.

## What We've Learned

**Highest extraction, lowest speed.** 0.97 extraction is a marginal lead over gemma4:26b (0.96) but at 5x the eval time (6903s vs 1344s). The dense architecture avoids MoE consistency concerns, but the speed penalty makes it impractical for daily pipeline use.

**Conditional reasoning anomaly.** Conditional reasoning scored 0.81 — below even qwen3.5:2b. Given its MMLU-Pro 85.2% and Arena rankings, this is almost certainly a prompt-fit issue. Perian's prompts are Qwen-optimized; Gemma may parse them differently.

**Thin data.** One eval run, no OB insights, no pipeline experience. This page is a stub waiting for more evidence.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `gemma4:31b` |
| Parameters | 30.7B dense |
| Size on disk | 20 GB |
| Family | Gemma 4 |
| Context window | 256K |
| Think parameter | `think: false` (confirmed working) |
| License | Apache 2.0 |
| External benchmarks | AIME 89.2%, MMLU-Pro 85.2%, #3 Arena AI |

## Open Questions

- Is the conditional reasoning score (0.81) prompt-fit or genuine weakness? Generic prompt eval would answer this.
- Is the extraction lead (0.97 vs 0.94-0.96) real or within noise? Need multiple runs + bootstrap CIs.
- At 20 GB and 115-minute eval, is there any pipeline role where speed doesn't matter enough to justify it?

## Cross-References

- [[models/gemma4-26b]] — same family MoE variant (faster, nearly as good)
- [[models/qwen3.5-27b]] — current escalation target (smaller, faster)
- [[evaluation/scoring-calibration]] — prompt-fit gap likely affects non-Qwen models

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-04-03 | Found by Legolas, downloaded | model-fleet.json |
| 2026-04-04 | Single eval: extraction 0.97, conditional 0.81, eval time 6903s | fleet-changelog |
