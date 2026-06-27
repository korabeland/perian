---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-05
sources:
  - open-brain
  - state/model-fleet.json
confidence: high
status: current
---

## Summary

The fleet's triage gatekeeper — not an extraction model, but a binary classifier that decides whether a listing is worth extracting at all. Its value isn't in what it produces but in what it rejects: 54% of listings never reach the expensive extraction step. The surprising finding: giving it *less* context makes it *better*.

## What We've Learned

**Less context, better decisions.** When we evaluated the triage gate (2×2 matrix: 2B vs 4B × title-only vs title+snippet), adding description context actually *reduced* recall for small models. They get distracted by irrelevant details in the description — a salary mention, a benefit list, a tangential skill — and second-guess the title signal. Title-only forces focus on the strongest signal. This was counterintuitive: more information should help, but at 2B parameters the model can't separate signal from noise in longer text.

**KV cache quantization sensitivity.** After enabling q8_0 KV cache, classification dropped from 0.87 to 0.73. At 2.7 GB, the model is already far from memory-constrained — KV cache quantization shouldn't affect it. This may indicate that the smallest models are more sensitive to cache precision, or it could be eval variance (the drop coincided with an eval suite change). Needs investigation.

**The L4 scoring cliff is cruelest here.** Binary conclusion accuracy scored 2b at 0.10 in March — appearing catastrophically bad. But reasoning coverage showed it was considering the right factors, just landing on adjacent conclusions (moderate instead of weak). When L4 scoring switched to distance-based, causal jumped to 0.64. The model didn't get smarter; we got better at measuring it.

**Thinking is off by default.** Like all qwen3.5 models, uses Gated DeltaNet architecture with thinking disabled by default. At 2B, thinking overhead would be proportionally enormous anyway.

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `qwen3.5:2b` |
| Parameters | 2B |
| Size on disk | 2.7 GB |
| Family | Qwen 3.5 |
| Context window | 256K |
| Think parameter | OFF by default |
| Fleet role | Triage gate — binary classification (relevant/irrelevant) |

## Open Questions

- Is the classification regression (0.87→0.73) from KV cache quantization or eval variance? Need to re-run with and without q8_0 on the same eval version.
- Could it serve as Tier 0 in cascade routing — not just triage, but cheap first-pass extraction for obviously simple listings?
- What's the actual false negative rate on the triage gate in production? We measure rejection rate (54%) but not how many good listings it wrongly rejects.

## Cross-References

- [[models/qwen3.5-4b]] — next tier up, significantly better extraction
- [[techniques/cascade-routing-logprobs]] — potential Tier 0 model
- [[evaluation/scoring-calibration]] — L4 binary cliff affected this model most

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-03-26 | Downloaded via Legolas — qwen3.5 family | fleet-changelog |
| 2026-03-26 | First eval: extraction 0.83, causal 0.10 (binary cliff) | fleet-changelog |
| 2026-04-02 | Triage gate deployed: title-only, 54% rejection, ~0.3s/item | OB decision |
| 2026-04-04 | Classification dropped 0.87→0.73 — KV cache sensitivity? | OB observation |
| 2026-04-04 | Causal improved 0.10→0.64 after L4 scoring fix | fleet-changelog |
