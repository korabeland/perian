---
type: concept
domain: techniques
created: 2026-04-12
updated: 2026-04-12
confidence: high
status: unreviewed
---

## Summary

PARSE (Parameter Automated Refinement and Schema Extraction), from Amazon at EMNLP 2025 Industry Track, demonstrates that JSON schemas designed for humans are suboptimal for LLMs. ARCHITECT automatically refines schemas by adding detailed entity descriptions, validation rules, and restructuring for clearer field relationships. Results: up to 64.7% improvement in extraction accuracy, 92% error reduction within first retry, consistent across model sizes. The key insight: **schemas are natural language contracts that can be systematically optimized for LLM consumption**.

## How It Works

### ARCHITECT (Build Phase — one-time)

Iteratively refines JSON schemas for LLM consumption:
1. Analyze extraction failures on a sample of inputs
2. Add detailed entity descriptions to each field (not just type annotations)
3. Add validation rules as natural language constraints
4. Restructure field relationships for clearer representation
5. Maintain backward compatibility via RELAY (automated transformation code)

Example transformation:
```
BEFORE (human-oriented):
  "seniority_match": {"type": "number"}

AFTER (LLM-optimized):
  "seniority_match": {
    "type": "number",
    "minimum": 0, "maximum": 1,
    "description": "Match between candidate's experience level and JD requirements.
     1.0 = exact match (e.g., JD asks for 5-7 years, candidate has 6).
     0.5 = partial match (JD asks for senior, candidate is mid-level but with
     relevant experience). 0.0 = clear mismatch (JD asks for VP-level, role is
     entry-level). Set to null if seniority cannot be determined from JD."
  }
```

### SCOPE (Extract Phase — per-request)

Reflection-based extraction with multi-stage guardrails:
1. Extract using optimized schema
2. Static validation (type checking, range constraints)
3. LLM-based validation (semantic consistency checks)
4. If validation fails: structured reflection → re-extract
5. 92% of errors corrected within first retry

### Key Results

- SWDE (web extraction): 64.7% accuracy improvement
- SGD (dialogue extraction): ~10% combined improvement
- Model-agnostic: consistent across Claude 3.5/3.7, Haiku, Llama 4-Maverick, DeepSeek-R1-671B
- Schema optimization is one-time cost; extraction adds one retry at most

## Perian Relevance

Perian's extraction prompt includes JSON schema descriptions written for human readability, not systematically optimized for LLM interpretation. PARSE suggests three zero-cost improvements:

1. **Optimize field descriptions**: Add explicit scoring rubrics per dimension — what makes a 0.3 vs. 0.7? Add null-handling rules and field dependency rules.

2. **Restructure field ordering for reasoning flow**: PARSE found that ordering fields to match natural reasoning sequence improves accuracy. For Perian: title → seniority → domain → company_size → remote → compensation (each builds on prior context).

3. **Add validation constraints as schema annotations**: Cross-field consistency rules (e.g., "if title_match < 0.3 and seniority_match < 0.3, overall_fit should not exceed 0.5").

4. **Integrate into autoresearch**: Schema refinement could be a new mutation type — use the 27B model to analyze extraction failures and suggest schema improvements, evaluated on holdout data.

**Why high-impact**: Zero infrastructure cost (no model change, no new dependencies), directly improves core pipeline output, compounds with existing constrained decoding, and naturally fits the autoresearch mutation framework.

## Source Material

The PARSE paper demonstrates that the gap between human-readable and LLM-readable schemas is systematic, not random. The ARCHITECT component was validated by having the LLM itself analyze extraction failures and suggest schema improvements — a meta-learning approach where the model improves its own input format. The 64.7% improvement on SWDE (web extraction) is particularly relevant because web data shares characteristics with job listing text: semi-structured, variable formatting, implicit relationships between fields.

The reflection-based retry (SCOPE) achieves 92% error correction within one retry by generating structured feedback about what went wrong, not just "try again." This maps to Perian's parse_json_response fallback but with a principled mechanism.

- [PARSE: EMNLP 2025 Industry Track](https://arxiv.org/abs/2510.08623)
- [PARSE Full Paper](https://aclanthology.org/2025.emnlp-industry.184.pdf)
- [Beyond JSON Mode: Production Structured Outputs](https://tianpan.co/blog/2025-10-29-structured-outputs-llm-production)

## Cross-References

- [[techniques/json-schema-constrained-decoding]] — structural validity via Ollama format parameter; PARSE adds semantic accuracy
- [[techniques/generator-verifier-pattern]] — SCOPE's retry loop is a mini generator-verifier
- [[evaluation/scoring-calibration]] — schema optimization could address systematic scoring biases

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-12 | Gandalf finding (high actionability) | finding-2026-04-12-2 |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
