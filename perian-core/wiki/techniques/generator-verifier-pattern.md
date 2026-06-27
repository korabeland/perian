---
type: concept
domain: techniques
created: 2026-04-12
updated: 2026-04-12
confidence: high
status: unreviewed
---

## Summary

Anthropic's April 2026 multi-agent coordination guide formalized the generator-verifier pattern: a generator produces output, a verifier evaluates against explicit criteria, and rejected output loops back with specific feedback. Key insight: **the verifier is only as good as its criteria** — a verifier told to check if output is "good" will rubber-stamp everything. Verification must have concrete, measurable criteria. The pattern also mandates iteration limits with fallback strategies.

## How It Works

1. **Generator** receives a task and produces initial output
2. **Verifier** checks output against **explicit, concrete criteria** — not vague quality assessments
3. If rejected, verifier returns **specific feedback naming the exact problem** (e.g., "feature misattributed to wrong pricing tier")
4. Generator revises using feedback
5. Loop continues until acceptance or **maximum iteration limit**
6. If max iterations hit: escalate to human, or return best attempt with caveats

### Critical Design Principles (from Anthropic)

- **Criteria specificity**: "Check if output is good" → rubber-stamping. "Check if seniority_match <0.3 when JD requires VP-level and role is entry-level" → actionable verification.
- **Separable skills**: Generation and verification must be separable. If evaluating output is as hard as generating it, the verifier won't reliably catch problems.
- **Iteration limits are essential**: Without them, the system oscillates without converging, burning tokens indefinitely.
- **Fallback strategy required**: Escalate to human review or return best attempt with explicit caveats.

## Perian Relevance

Perian's Hobbit-Aragorn architecture is already a generator-verifier pattern:
- **Generator**: Hobbit fleet (9B) extracts and scores job listings
- **Verifier**: Aragorn (gemma4:26b) challenges specific dimensions with reasoning

**What Anthropic validates**:
1. Dimension-specific criteria are correct — Aragorn challenges individual dimensions (title_match, seniority_match, etc.) with specific reasoning
2. Single-pass challenge is correct — one challenge round + human review for disagreements is a sensible iteration-limit implementation
3. The feedback workflow is the right fallback — disagreements go to human review

**What could be improved**:
1. **Sharpen Aragorn's verification criteria** — define what constitutes a "wrong" score per dimension with concrete thresholds and examples
2. **Add convergence detection** — if Aragorn's challenge delta is consistently <0.05 on all dimensions for a listing type, skip the challenge (it's noise)
3. **"Best attempt with caveats" output** — when the review backlog grows, flag items with a confidence marker rather than blocking on review

## Source Material

Anthropic published this alongside their production multi-agent research system architecture (June 2025 blog). The generator-verifier is one of five coordination patterns, positioned as the simplest and most widely deployed. The key contribution is the criteria-specificity principle — most production failures of this pattern trace back to vague verification criteria, not model capability.

The guide also introduced effort-scaling rules (see [[techniques/effort-scaling-prompts]]) and context-centric decomposition, both from the same production system.

- [Anthropic: Multi-Agent Coordination Patterns](https://claude.com/blog/multi-agent-coordination-patterns)
- [Anthropic: When to Use Multi-Agent Systems](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them)
- [Anthropic: How We Built Our Multi-Agent Research System](https://www.anthropic.com/engineering/multi-agent-research-system)

## Cross-References

- [[techniques/effort-scaling-prompts]] — from the same Anthropic research, effort-aware pipeline design
- [[techniques/cross-family-ensembles]] — cross-family diversity makes the verifier genuinely independent
- [[models/gemma4-26b]] — current Aragorn verifier
- [[evaluation/scoring-calibration]] — Aragorn precision findings (criteria sharpness directly impacts this)

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-12 | Gandalf finding (high actionability) | finding-2026-04-12-1 |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
