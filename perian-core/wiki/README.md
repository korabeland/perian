# Palantir — Knowledge Base

> The seeing-stones of Perian. A compounding knowledge system that captures what
> works and what doesn't with local LLM orchestration, grounded in observation
> and research.

## What this is

A Karpathy-style LLM wiki — a persistent, LLM-maintained knowledge system where
cross-references, contradictions, and synthesis accumulate over time rather than
being rediscovered each session.

Serves two audiences equally:
- **Human:** narrative for learning, building intuition, potential portfolio piece
- **Frontier model:** structured frontmatter, consistent cross-references, clear
  status fields for programmatic orchestration decisions

## Structure

Entity pages (models), concept pages (techniques), synthesis pages (evaluation,
cross-cutting analysis). See `_schema.md` for page templates and conventions.

Qualitative-first: leads with insight, not spec sheets. Failure modes, open
questions, and cross-references are first-class content.

Internal observations reference raw data by path + commit hash. The observation
in the wiki page is the primary record; the raw data file is the audit trail.

## Methodology

Wiki entries aspire to scientific rigor, applied with increasing depth as the
system matures:

1. **Observation** — unbiased, factual. What happened, measured how, when.
2. **Hypothesis** — why the observation occurred. Stated explicitly, not assumed.
3. **Test plan + results** — how we validated (or plan to validate) the hypothesis.
4. **Grounding in research** — relevant published work, referenced with source links.

This is a north star, not a gate. Most entries start at observation and grow from
there. The wiki has ~25 pages — governance should match the scale.

## What belongs here

Transferable understanding — knowledge that would help architect a different
pipeline tomorrow.

| Palantir | NOT Palantir |
|----------|-------------|
| "MLX activates via model format tags, not env vars" | Which models are in the fleet right now |
| "Off-scale scoring was the real problem, not wrong scoring" | Which items Aragorn challenged tonight |
| "Cross-family models catch different error types" | Current floor penalty config values |
| Full text of the PARSE schema optimization paper | Gandalf finding status (reviewed/dismissed) |

**Rule of thumb:** if it's current state, it belongs in `state/` files.
If it's understanding about *why* something works or doesn't, it belongs here.

## Operations

See `_schema.md` for page templates and `wiki-keeper.md` for ingest commands.
Key operations: `palantir reindex`, `palantir query`, `palantir lint`,
`palantir status`. Gandalf writes technique pages directly to `techniques/`;
Legolas model pages are ingested via `palantir ingest legolas`.
