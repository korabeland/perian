---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-08
confidence: medium
status: dismissed
---

## Summary

Formal frameworks for combining outputs from multiple heterogeneous LLMs through structured voting — majority, weighted, or quorum-based. CDM (Collective Decision-Making) suppresses hallucination and off-ontology labeling in classification tasks. LLM-TOPLA maximizes ensemble diversity, showing that 2-3 diverse small models can outperform a single larger model on structured tasks. **Dismissed**: premature — validate single-challenger Aragorn precision first (gemma4:26b). Revisit if single challenger doesn't hit >50% precision.

## How It Works

### Collective Decision-Making (CDM) — "Majority Rules" (arXiv 2511.15714)

Treats each LLM as an independent voter in a collective decision:
- Each model processes the same input and produces structured output independently
- Outputs aggregate through explicit decision rules:
  - **Majority vote**: Simple consensus (works for classification)
  - **Weighted consensus**: Models weighted by reliability on calibration data
  - **Quorum-based**: Requires minimum agreement threshold before accepting
- CDM suppresses hallucination and off-ontology labeling. When models disagree, structured voting catches outputs that don't belong to the valid label space.

### Diversity-Maximized Ensembles — LLM-TOPLA (EMNLP 2024)

Selects models that maximize disagreement (not accuracy alone). A diverse ensemble of weaker models can outperform a single stronger model on structured tasks where error types vary by architecture.

### Practical Implementation Pattern

```
Input → [Model A: Qwen 8B] → JSON scores
      → [Model B: Gemma e4b] → JSON scores
      → Per-dimension comparison:
         Agreement → Accept (high confidence)
         Disagreement → Flag for review OR escalate
         Off-ontology → Reject, use agreeing model's output
```

## Perian Relevance

CDM would upgrade Aragorn from single-challenger to multi-voter verification: run 2-3 heterogeneous models on disputed dimensions, merge via weighted voting. The framework's suppression of hallucination and off-ontology labeling maps directly to Perian's structured JSON scoring.

**Why dismissed**: Current priority is validating the simpler single-challenger approach (gemma4:26b) and measuring its precision. CDM adds complexity (multi-model loading, voting logic, calibration weights) that isn't justified until we know the baseline precision. If gemma4:26b single-challenger exceeds 50% precision, CDM's marginal benefit may not justify the infrastructure cost on 48GB hardware.

**Revisit conditions**: Aragorn single-challenger precision <50% after prompt tightening, or growing false positive rate that single-model challenge can't address.

## Source Material

The CDM framework (arXiv 2511.15714) formalizes what was previously ad-hoc "ask two models and compare." Key contribution: explicit aggregation rules optimized for structured label spaces like JSON classification. The quorum-based approach is particularly relevant — it requires minimum agreement before accepting, which maps to Perian's per-dimension challenge architecture.

LLM-TOPLA (EMNLP 2024) provides the model selection strategy: for a fixed compute budget, choose models that maximize disagreement, not just accuracy.

- [Majority Rules: CDM Among Heterogeneous LLMs](https://arxiv.org/html/2511.15714v1)
- [LLM-TOPLA: Efficient Ensemble by Maximising Diversity (EMNLP 2024)](https://aclanthology.org/2024.findings-emnlp.698.pdf)
- [Enhancing Answer Reliability Through Inter-Model Consensus](https://arxiv.org/html/2411.16797v2)

## Cross-References

- [[techniques/cross-family-ensembles]] — foundational finding that motivates cross-family CDM
- [[techniques/moe-verification-layer]] — MoE as cheap voter in CDM ensemble
- [[models/gemma4-26b]] — current single-challenger whose precision determines CDM need

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-05 | Gandalf finding (high actionability) | finding-2026-04-05-3 |
| 2026-04-08 | Dismissed — premature, validate single-challenger precision first | human review |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
