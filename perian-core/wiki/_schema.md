# Palantir — Knowledge Base Schema

> The seeing-stones of Perian. What we know about our models, techniques, and evaluation — synthesized from experience, not just scores.

## Purpose

The Palantir synthesizes qualitative understanding from scattered sources — Open Brain thoughts, Gandalf research, Legolas scouting, pipeline experience — into connected, navigable knowledge. It is not a data mirror. Scores live in `compare.py` and `fleet-changelog.jsonl`. The Palantir holds what those tools can't: intuition, failure modes, open questions, and cross-cutting connections.

## Page Types

| Type | Purpose | Example |
|------|---------|---------|
| **entity** | Living dossier per model — character, insights, open questions | `models/qwen3-8b.md` |
| **concept** | Technique or methodology finding from Gandalf research | `techniques/kv-cache-quantization.md` |
| **synthesis** | Cross-cutting analysis distilled from multiple sources | `evaluation/scoring-calibration.md` |
| **overview** | Entry point for a domain — links to detail pages | `evaluation/methodology-overview.md` |

## Model Page Template

```markdown
---
type: entity
domain: models
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources:
  - open-brain
  - state/model-fleet.json
confidence: high|medium|low
status: current|stub
---

## Summary

One paragraph: what we know about this model's character. Lead with the
insight, not the spec sheet.

## What We've Learned

Synthesized insights from OB thoughts, eval observations, pipeline experience.
Each insight is a narrative paragraph with context — what we observed, why it
matters, what it connects to. Not a bullet list with scores.

## Specifications

Compact reference table: tag, params, size, family, context window, think param.

## Open Questions

What we still don't know. Each question drives future experimentation.

## Cross-References

- [[domain/page-name]] — why the connection matters

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| YYYY-MM-DD | What we discovered | What triggered it |
```

## Other Page Templates

**Concept pages** (techniques/): Summary → How It Works → Perian Relevance → Source Material → Cross-References → History.

The `## Source Material` section holds key excerpts from external research, fetched at discovery time so the content survives link rot. Not full papers — the relevant passages that ground the finding's claims, plus the source URLs. Gandalf populates this section during research; it can be expanded later with additional evidence.

**Synthesis pages** (evaluation/): Summary → Findings → Cross-References → History.

**Overview pages**: Summary → links to detail pages.

## Cross-Reference Syntax

`[[domain/page-name]]` links between pages. Example: `[[models/qwen3-14b]]`, `[[techniques/kv-cache-quantization]]`.

## Operations

### `palantir reindex`
Run `qmd update && qmd embed` to rebuild the search index after wiki changes.

### `palantir ingest legolas`
Read `state/model-fleet.json`. Create or update model candidate pages with architectural notes and Legolas's comparison context.

### "update the {model} page"
User triggers synthesis of new observations into a model page. Pull from OB thoughts, recent eval observations, pipeline experience. This is the primary way model pages grow.

### `palantir query "<question>"`
Search page content for an answer. Return excerpts with page links.

### `palantir lint`
Validate: all `[[refs]]` resolve, pages have frontmatter, index matches file count.

### `palantir status`
Page counts, last-updated dates, any lint warnings. Checks for technique pages with `status: unreviewed`.

## Naming Conventions

- Model pages: `{family}{version}-{size}.md` — e.g., `qwen3-8b.md`, `gemma4-26b.md`
- Technique pages: `{kebab-case-title}.md`
- Meta files: prefixed with `_`
