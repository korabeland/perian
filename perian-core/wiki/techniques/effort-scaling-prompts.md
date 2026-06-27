---
type: concept
domain: techniques
created: 2026-04-12
updated: 2026-04-12
confidence: high
status: unreviewed
---

## Summary

Anthropic's production multi-agent research system (June 2025, updated April 2026) revealed key prompting principles for multi-model pipelines. Three directly actionable: (1) **Effort scaling** — agents overinvest in simple tasks without explicit guidelines; embed effort-scaling rules in prompts. (2) **Self-improving schemas** — having an LLM rewrite tool/schema descriptions based on failure analysis reduced task completion time by 40%. (3) **Context-centric decomposition** — splitting by development role creates more coordination overhead than actual work; split by context isolation instead.

## How It Works

### Effort Scaling Rules in Prompts

Without explicit effort guidelines, models default to maximum thoroughness regardless of task complexity. Anthropic's early versions spawned 50 subagents for simple queries.

Solution: embed explicit effort-scaling rules directly in the prompt:
- Simple fact-finding: minimal processing
- Direct comparisons: moderate depth
- Complex research: full pipeline

The model reads these rules and adjusts behavior. The overhead of unnecessary thoroughness on simple tasks is pure waste.

### Self-Improving Tool/Schema Descriptions

Tool descriptions written by developers don't match how LLMs interpret them. Solution: deploy an LLM to test tools dozens of times, diagnose failures, and rewrite descriptions. Result: **40% decrease in task completion time** for subsequent agents using improved descriptions.

Key insight: LLMs are excellent at prompt/schema engineering for other LLMs. They can analyze their own failure modes and suggest improvements — if given the framework to do so.

### Context-Centric Decomposition

Splitting by role (one agent writes, another tests, a third reviews) creates massive coordination overhead. In one experiment, subagents spent more tokens on coordination than on actual work.

Solution: split by context isolation. An agent handling a feature should also handle its tests because it already has the context. Only split when context can be truly isolated — independent research paths, components with clean API contracts, or blackbox verification.

### Extended Thinking as Controllable Scratchpad

Testing showed extended thinking (planning/scratchpad before action) improved instruction-following, reasoning, and efficiency across the board. The lead agent plans its approach; subagents evaluate quality after tool results.

### Parallel Operations

Sequential → parallel tool calling cut research time by up to 90%. Two levels: lead agent spawns multiple subagents in parallel, each subagent uses multiple tools in parallel.

## Perian Relevance

### Effort Scaling (High Priority)

Perian currently applies identical extraction depth to every listing. The 2B triage gate rejects obvious mismatches, but everything that passes gets full 9B extraction.

**Proposed change**: Make the triage gate classify effort level, not just accept/reject:
- **Light**: Obviously good/bad fit — fast extraction with fewer dimensions
- **Standard**: Needs full extraction and scoring
- **Deep**: Ambiguous listing requiring 27B escalation target

If >20% of listings classify as "light," the time savings justify the change.

### Self-Improving Schemas (High Priority)

Use the 27B model to analyze extraction failures from Aragorn reviews:
1. Collect cases where Aragorn found meaningful errors
2. Feed extraction prompt + failure cases to 27B model
3. Diagnose why the 9B model made those errors
4. Generate schema/prompt modifications to prevent recurrence
5. Test modifications on holdout data via autoresearch

This is a direct application of "let agents improve themselves" — using a stronger model to improve prompts that guide weaker models.

### Context-Centric Architecture (Validates Current Approach)

Anthropic's finding directly validates Perian's monolithic pipeline design. Each stage (collect → triage → extract → score → challenge) has full context from previous stages. Splitting into microservices would add handoff cost without benefit. **Do not prematurely abstract.**

## Source Material

These principles emerged from Anthropic iterating a real production system processing hundreds of research queries with 3-10 parallel agents per query. They are not theoretical — they represent hard-won engineering lessons.

The effort-scaling principle is particularly well-validated: without it, their system was "like hiring a team of PhD researchers to answer 'what time is it?'" The 40% improvement from self-improving descriptions was measured across hundreds of tool calls.

The context-centric decomposition finding contradicts conventional "separation of concerns" wisdom from software engineering. In LLM systems, the cost of context handoff is high enough that co-location usually wins.

- [Anthropic: How We Built Our Multi-Agent Research System](https://www.anthropic.com/engineering/multi-agent-research-system)
- [Anthropic: Multi-Agent Coordination Patterns](https://claude.com/blog/multi-agent-coordination-patterns)
- [Anthropic: 2026 Agentic Coding Trends Report](https://resources.anthropic.com/2026-agentic-coding-trends-report)

## Cross-References

- [[techniques/generator-verifier-pattern]] — from the same Anthropic research, the generator-verifier coordination pattern
- [[techniques/parse-schema-optimization]] — PARSE's schema refinement is a concrete implementation of self-improving schemas
- [[models/qwen3.5-2b]] — triage gate that could implement effort classification

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-12 | Gandalf finding (high actionability) | finding-2026-04-12-4 |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
