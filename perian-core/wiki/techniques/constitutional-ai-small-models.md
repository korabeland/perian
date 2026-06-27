---
type: concept
domain: techniques
created: 2026-04-12
updated: 2026-04-12
confidence: low
status: unreviewed
---

## Summary

Anthropic's January 2026 Constitutional AI revision revealed that applying strong safety guardrails to small models (tested on Llama 3-8B) causes a 9.8% drop in helpfulness, while larger models show negligible degradation. The revised constitution shifts from rule-based to reason-based alignment. Small models lack the capacity to internalize both safety reasoning and task performance simultaneously. RLAIF is positioned as the scalable alternative to RLHF.

## How It Works

Constitutional AI applies alignment principles through a "constitution" — a set of rules the model follows during generation. The January 2026 revision shifted from prescriptive rules ("never do X") to reason-based guidance ("consider why X might be harmful").

The capacity trade-off in small models:
- Large models (70B+) can internalize safety reasoning without task performance loss
- Small models (8B and below) face a zero-sum trade-off: safety reasoning occupies capacity that would otherwise serve task performance
- The 9.8% helpfulness drop on Llama 3-8B was measured on general assistant tasks
- Structured extraction tasks (narrower than general assistant) may see less degradation, but the principle holds: every constraint added to a small model's behavior budget competes with task capability

RLAIF (Reinforcement Learning from AI Feedback) replaces human annotation with AI-generated preference data, making alignment scalable. But for small models, the capacity limitation remains regardless of the feedback source.

## Perian Relevance

Directly relevant to the Hobbit fleet (2B triage gate, 9B first-pass extraction):

1. **Fine-tuning implication**: If Perian fine-tunes these models (Phase 5), constitutional-style safety constraints would degrade extraction helpfulness by ~10%. Small models should be fine-tuned purely for task performance, not general safety.

2. **Validates current prompt approach**: Perian uses small models for structured extraction with maximally specific prompts and clear task boundaries, not broad constitutional-style guidance. The finding confirms this is the right architecture.

3. **Prompt design principle**: Keep the "reasoning about what to do" in the prompt (explicit instructions), not in the model weights (implicit reasoning). Small models should receive direct, concrete instructions rather than principles they must interpret.

## Source Material

The finding emerged from analysis of Anthropic's Constitutional AI revisions and their impact on different model sizes. The 9.8% helpfulness degradation was specifically measured on small models applying the revised constitution. The shift from rule-based to reason-based alignment is designed for large models that can handle the reasoning overhead.

- [Anthropic AI Constitution and Consciousness (Aionda)](https://aionda.blog/en/posts/anthropic-ai-constitution-and-consciousness)

## Cross-References

- [[models/qwen3.5-2b]] — triage gatekeeper, most capacity-constrained model in fleet
- [[models/qwen3.5-9b]] — first-pass extractor, medium-capacity model
- [[techniques/effort-scaling-prompts]] — effort scaling is an alternative to broad constitutional guidance

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-12 | Gandalf finding (medium actionability) | finding-2026-04-12-3 |
| 2026-04-12 | Migrated to Palantir from autoresearch/methodology/ | gandalf-palantir integration |
