---
type: synthesis
domain: evaluation
created: 2026-04-05
updated: 2026-04-05
sources:
  - evaluation/EVAL_METHODOLOGY.md
confidence: high
status: current
---

## Summary

Definitions and formulas for every metric in Perian's evaluation suite. Organized by level, with known limitations and planned improvements for each.

## Syntactic Metrics (Gates)

**JSON Parse Rate** — % of runs producing valid JSON.
- Target: >= 0.98. Below 0.95 is disqualifying.
- Parser: `helpers.py:parse_json_response()` strips fences, fixes math expressions, falls back to innermost `{...}`.

**Schema Compliance** — % of parsed outputs with all required fields and valid enums.
- Required: title, company, seniority, remote_policy, domain, key_requirements, compensation.
- Measured at L1 only.

## L1: Extraction Accuracy

**Exact Match** — binary 1/0 for string fields (title, company, seniority, remote_policy, domain).
- Case-sensitive, with `acceptable_alternatives` per test case.

**Jaccard Similarity** — for `key_requirements` (list field).
- `J(A, E) = |A intersect E| / |A union E|` on lowercased sets.
- **Known issue:** Penalizes valid paraphrases. "Salesforce Service Cloud" vs "Salesforce" = zero overlap.
- **Planned:** Replace with semantic matching via nomic-embed-text (0.80 cosine threshold).

**Compensation Sub-field Match** — 4 binary checks (min, max, currency, equity_mentioned). Score = correct/4.

**Overall Field Accuracy** = mean of 7 field scores per case, averaged across cases. Maps to `capabilities.extraction`.

## L2: Classification Accuracy

Per-field accuracy for seniority, domain, remote_policy. Mean of 3 = `overall_classification_accuracy`. Maps to `capabilities.classification`.

## L3: Conditional Reasoning

**Score Correlation:**
```
score_correlation = max(0, 1.0 - 2 x mean_deviation)
```
Where mean_deviation = MAE across 5 dimensions (title_match, domain_match, seniority_match, requirements_fit, compensation_fit), each 0.0-1.0.

Maps to `capabilities.conditional_reasoning`.

**Bonus/Penalty Accuracy** — % matching expected. **Known issue:** Ambiguous criteria create false failures (see [[evaluation/scoring-calibration]]).

## L4: Causal Reasoning

**Conclusion Accuracy** — binary exact match on enum {strong_match, moderate_match, weak_match, no_match}.
- **Known issue:** Creates artificial cliff for small models. See [[evaluation/scoring-calibration]].

**Conclusion Distance** — `|actual - expected|` on numeric scale (strong=3, moderate=2, weak=1, no=0).
- More informative. **Proposed replacement:** `max(0, 1.0 - distance / 3.0)`.

**Reasoning Coverage** — fraction of expected mentions found via substring match.

Maps to `capabilities.causal_reasoning`.

## L5: Batch Consistency

- Extraction consistency: classification fields agree within groups
- Score variance: variance <= 0.05 threshold
- Rank stability: order matches expected
- **Not mapped** to any capability score (diagnostic only).

## L6: Synthesis

**Factual Coverage:**
- Short facts (<=4 words): exact phrase OR >=70% word overlap
- Long facts (>4 words): >=60% keyword match excluding stopwords
- **Known issue:** Crude matching caps all models at 0.15-0.23 range. See [[evaluation/scoring-calibration]].

Maps to `capabilities.synthesis`.

## Composite Scores

**Nuanced Judgment** = 0.6 x conditional_reasoning + 0.4 x causal_reasoning.
- 60/40 weighting: L3 scoring is more stable and pipeline-critical than L4 reasoning.

## Planned Metrics

| Metric | Purpose | Module |
|--------|---------|--------|
| Expected Calibration Error (ECE) | Logprob confidence vs correctness | evaluation/calibration.py |
| Bootstrap CIs | Uncertainty bounds on all scores | evaluation/stats.py (implemented) |
| Semantic Match (BERTScore-style) | Replace exact string matching | evaluation/semantic.py (implemented) |
| Cost-per-Correct-Extraction | Normalize across tiers | evaluation/stats.py |

## Cross-References

- [[evaluation/methodology-overview]] — how metrics fit the evaluation philosophy
- [[evaluation/scoring-calibration]] — known issues with current metrics
- [[techniques/json-schema-constrained-decoding]] — eliminates JSON parse failures
- [[techniques/bootstrap-confidence-intervals]] — statistical context for score comparisons

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-03-27 | Metrics documented in EVAL_METHODOLOGY.md | evaluation research |
| 2026-04-05 | Palantir page created | palantir seed |
