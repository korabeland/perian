---
type: synthesis
domain: evaluation
created: 2026-04-05
updated: 2026-04-12
sources:
  - evaluation/EVAL_METHODOLOGY.md
  - evaluation/RESEARCH_SUMMARY.md
confidence: high
status: current
---

## Summary

Ten findings about Perian's eval scoring — seven systemic issues that distort model comparisons, plus three operational discoveries that improved calibration. Three trace to the rubric measuring the wrong thing (not model failure), two to statistical underpowering, and two to contamination. Understanding these is essential for interpreting scores on any model page.

## Finding 1: Exact String Matching Penalizes Paraphrases

`key_requirements` uses Jaccard on exact strings. "Salesforce Service Cloud" vs "Salesforce" = zero overlap.

**Impact:** L1 extraction systematically depressed for models that paraphrase well.
**Fix:** Semantic matching via nomic-embed-text (0.80 cosine). Implemented in `evaluation/semantic.py`.

## Finding 2: Missing acceptable_alternatives (L3-L6)

L1/L2 define alternatives; L3-L6 have zero. Any valid answer not exactly matching expected is marked wrong.

**Impact:** Inflated failure rates on subjective tasks.
**Fix:** Add alternatives to L3-L6 test cases.

## Finding 3: L6 Synthesis Capped by Rubric

All models cluster at 0.15-0.23 on synthesis (April eval suite). Word-overlap matching penalizes valid rephrasings.

**Impact:** `capabilities.synthesis` is essentially noise — cannot differentiate models.
**Fix:** Embedding similarity per fact (0.70 cosine threshold).

## Finding 4: L4 Binary Scoring Cliff

`conclusion_accuracy` is binary — moderate vs weak (1 step off) scores 0.0, same as strong vs no_match (3 steps off).

**Impact:** Small models appear catastrophically worse than they are. qwen3.5:2b scores 0.64 (April) despite reasonable reasoning.
**Fix:** Distance-based scoring: `max(0, 1.0 - distance / 3.0)`.

## Finding 5: Bonus/Penalty Ground Truth Opacity

L3 bonus/penalty criteria are ambiguous in edge cases. Models penalized for defensible interpretations.

**Impact:** Bonus/penalty metrics unreliable for comparison.
**Fix:** Make bonus/penalty a soft metric (informational only).

## Finding 6: Statistical Underpowering

7-10 cases per level vs ~40 needed for 95% CI on 10% differences. Point estimate comparisons create false confidence.

**Impact:** Cannot distinguish 0.88 from 0.91 with current sample sizes.
**Fix:** Expand to 40 cases per level. Use bootstrap CIs ([[techniques/bootstrap-confidence-intervals]]).

## Finding 7: Triple Contamination

Three stacking contamination vectors:
1. **Ground truth bias** — expected answers validated against qwen3:8b behavior
2. **Prompt optimization bias** — autoresearch tuned prompts for qwen3
3. **Synthetic distribution bias** — L3 was 43% synthetic (edge cases over-represented)

**Impact:** Eval systematically favors Qwen 3 family. Non-Qwen models may score lower due to prompt-fit, not capability.
**Fix:** Multi-model ground truth calibration, dual scoring (production + generic prompts), cap synthetic at 25%.

## Finding 8: Score Anchor Snapping

`snap_dimension_scores()` was clamping dimension scores to the nearest anchor point (0.0, 0.25, 0.5, 0.75, 1.0). Off-scale scoring — where a model's raw output fell between anchors — was being silently corrected to the nearest one. The fix removed the snapping, letting raw scores through.

**Impact:** Scores were artificially clustered at anchor points, masking real variance between models. Dimensions that should have shown gradations (e.g., title_match at 0.65) were being forced to 0.75 or 0.50.
**Fix:** Removed `snap_dimension_scores()`. Commit `a922909`.
**Insight:** The real problem was never "wrong scoring" — it was off-scale scoring that made correct scores look wrong.

## Finding 9: Duplicate Enterprise Penalty

The enterprise `company_size` floor penalty was applied twice: once in the scoring prompt (as an instruction to penalize enterprise companies) and once in `floor_penalties.json` (as a post-hoc penalty). The duplicate caused silent score corruption — items from enterprise companies were penalized more heavily than intended.

**Impact:** 78% floor penalty rate in nightly digests, heavily skewed toward `company_size=enterprise`.
**Fix:** Removed the duplicate penalty from the scoring prompt. Single penalty remains in `floor_penalties.json`. Commit `a922909`.

## Finding 10: Aragorn Precision Validation

After the score anchor snapping fix and enterprise penalty cleanup, Aragorn's adversarial challenges showed 83% strict precision — meaning 83% of the time Aragorn flagged a dimension, the human reviewer agreed. This validates the core system: the scoring was working, but the off-scale snapping and duplicate penalties were masking its accuracy.

**Impact:** Confidence in the pipeline's scoring quality is substantially higher than raw metrics suggested before the fixes.
**Observation date:** 2026-04-09 (based on review of challenge data accumulated since Apr 5).

## Score Interpretation Guide

When reading scores on model pages or from `compare.py`:

| Score range | Reliability |
|-------------|-------------|
| extraction (L1) | Moderate — Jaccard issue affects requirements field |
| classification (L2) | Good — clear metrics, alternatives supported |
| conditional (L3) | Good — score_correlation is well-designed |
| causal (L4) | Low — binary cliff distorts, especially for small models |
| synthesis (L6) | Very low — rubric is the bottleneck, not models |
| json_output | Very high — clear pass/fail, large sample |
| nuanced_judgment | Moderate — composite of good L3 and poor L4 |

## Cross-References

- Model entity pages in `models/` — scores affected by these issues
- [[evaluation/methodology-overview]] — full evaluation context
- [[evaluation/metric-definitions]] — metric formulas
- [[techniques/bootstrap-confidence-intervals]] — statistical fix for Finding 6

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-03-27 | Findings documented in RESEARCH_SUMMARY.md | evaluation research |
| 2026-04-05 | Palantir page created | palantir seed |
| 2026-04-09 | Score anchor snapping fix — `snap_dimension_scores()` removed. Commit `a922909` | pipeline fix |
| 2026-04-09 | Duplicate enterprise penalty removed from scoring prompt. Commit `a922909` | pipeline fix |
| 2026-04-09 | Aragorn precision validated at 83% strict after calibration fixes | review session |
