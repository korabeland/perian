---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-05
confidence: medium
status: current
---

## Summary

For small evaluation sets (100-500 examples), bootstrap resampling with nested confidence intervals is the recommended statistical method. Perian's eval suite has 7-10 cases per level — far below the ~40 needed for 95% CI on a 10% difference. Without CIs, model comparisons (e.g., "0.88 vs 0.91") are statistically meaningless. **Status: implemented** — `evaluation/stats.py` provides bootstrap CIs and paired permutation significance tests.

## The Method

1. **Bootstrap resampling:** Draw N samples with replacement from the score set, compute the metric, repeat 1000 times. The 2.5th and 97.5th percentiles give the 95% CI.

2. **Paired permutation test:** For comparing two models on the same test cases, shuffle the model labels, recompute the difference, and build a null distribution. P-value = fraction of permuted differences ≥ observed difference.

3. **Effect size (Cohen's d):** Even if statistically significant, a tiny effect size doesn't justify switching models.

## Current Gap

| Level | Cases | Min for 95% CI | Gap |
|-------|-------|-----------------|-----|
| L1 | 7 | ~40 | 33 |
| L2 | 7 | ~40 | 33 |
| L3 | 7 | ~40 | 33 |
| L4 | 10 | ~40 | 30 |
| L5 | 3 groups | ~6 groups | 3 |
| L6 | 8 | ~15 | 7 |

With 7 cases, a 2-3 case swing from variance moves field_accuracy by 20-30%.

## Perian Relevance

- `evaluation/stats.py` implements `bootstrap_ci()` and `scores_significantly_different()`
- The autoresearch safety gate should only accept mutations where the improvement's 95% CI excludes zero
- Model upgrade decisions (tracked in `fleet-changelog.jsonl`) should use significance testing

## Cross-References

- [[evaluation/methodology-overview]] — how CIs fit into the evaluation process
- [[evaluation/scoring-calibration]] — scoring issues that compound with small sample sizes
- Model entity pages in `models/` — scores that need CIs for meaningful comparison

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-03-27 | Gandalf finding (medium actionability) | finding-2026-03-27-5 |
| 2026-03-27 | Implemented in evaluation/stats.py | evaluation infrastructure |
| 2026-04-05 | Palantir page created | palantir seed |
