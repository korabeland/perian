---
type: overview
domain: evaluation
created: 2026-04-05
updated: 2026-04-05
sources:
  - evaluation/EVAL_METHODOLOGY.md
  - evaluation/RESEARCH_SUMMARY.md
confidence: high
status: current
---

## Summary

Perian's eval system answers: **which model produces the best structured output for this pipeline, on this hardware, at this cost?** It measures three dimensions (CMU "Beyond Capable" framework): accuracy, calibration, and robustness. Six test levels cover extraction through synthesis, producing a 7-score capability profile that feeds the fleet router.

## The Three Dimensions

| Dimension | What it means | Status |
|-----------|---------------|--------|
| **Accuracy** | Does the model get the right answer? | Measured (L1-L6) |
| **Calibration** | Does confidence match correctness? | Planned (ECE from logprobs) |
| **Robustness** | Stable across runs and rephrasings? | Partial (n_runs=3, no rephrasing tests) |

## The 6-Level Pipeline

| Level | Tests | Primary Metric | Maps to Capability |
|-------|-------|----------------|-------------------|
| L1 Extraction | Field accuracy from raw listing text | `field_accuracy` | `extraction` |
| L2 Classification | Categorical field accuracy | `overall_classification_accuracy` | `classification` |
| L3 Conditional | Multi-dimensional scoring vs ground truth | `score_correlation` | `conditional_reasoning` |
| L4 Causal | Multi-step reasoning on match quality | `conclusion_accuracy` | `causal_reasoning` |
| L5 Batch | Consistency across similar items | `extraction_consistency` | (diagnostic only) |
| L6 Synthesis | Multi-document summarization | `factual_coverage` | `synthesis` |

Two composite scores:
- `json_output` = mean(L1-L4 parse rates)
- `nuanced_judgment` = 0.6 x conditional_reasoning + 0.4 x causal_reasoning

## What We Don't Measure

- General knowledge (trust published benchmarks)
- Code generation (not in pipeline)
- Creative writing / style
- Multilingual capability

## Evaluation Cadence

| Trigger | What runs |
|---------|-----------|
| Legolas candidate | Full L1-L6 eval |
| Autoresearch mutation | L1-L3 safety gate + holdout |
| Monthly maintenance | Full L1-L6 on current fleet |
| Post-fine-tune (Phase 5) | Full L1-L6 + general regression |

## Key Insight: Capability vs Prompt-Fit

A model's eval score = model capability x prompt fit. Perian's prompts are optimized for Qwen. Non-Qwen models may score lower due to prompt-fit, not capability. The upgrade decision framework uses dual scoring (production prompts + generic prompts) to separate these factors.

See: [[evaluation/scoring-calibration]] for known scoring issues that distort comparisons.

## Cross-References

- [[evaluation/metric-definitions]] — detailed metric formulas and computation
- [[evaluation/scoring-calibration]] — known issues and planned fixes
- Model entity pages in `models/` — how eval results shape understanding of each model
- [[techniques/bootstrap-confidence-intervals]] — statistical rigor for small test suites

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-03-27 | Methodology documented (EVAL_METHODOLOGY.md) | evaluation research |
| 2026-04-05 | Palantir page created | palantir seed |
