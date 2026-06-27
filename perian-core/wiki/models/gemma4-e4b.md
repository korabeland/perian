---
type: entity
domain: models
created: 2026-04-05
updated: 2026-04-05
sources:
  - open-brain
  - state/model-fleet.json
confidence: medium
status: current
---

## Summary

Google's edge-optimized model — the first non-Qwen model to achieve perfect classification on Perian's eval suite. Its most interesting property isn't raw scores but what we learned about thinking behavior: contrary to earlier research, `think: false` *does* work on Gemma 4 models in Ollama. The initial eval with thinking enabled was measuring a quality ceiling, not real-world performance.

## What We've Learned

**Thinking works differently than expected.** Early research said Gemma 4 disables thinking by omitting the `<|think|>` token, not via a `think` parameter. But in practice on Ollama 0.20.0, `think: false` works to disable it. With thinking enabled: 223 tokens generated, 33-minute eval. Without thinking: 54 tokens, eval time should drop 3-4x. The initial eval scores represent a quality ceiling with thinking overhead — a re-run without thinking is needed for fair comparison.

**KV cache quantization gave the biggest speedup here.** gemma4:e4b (9.6 GB MoE) got a 40% speedup from q8_0 KV cache — from 33 minutes to 20 minutes. This was the largest improvement of any model in the fleet rebaseline. At 9.6 GB it's on the edge of being memory-constrained, which is exactly where cache quantization helps most.

**Surprisingly capable for its size.** Perfect classification (1.00), strong extraction (0.93), and good conditional reasoning (0.87) — matching models 2-3x its size. Native function calling and multimodal support (text+image+audio) suggest a richer architecture than the parameter count implies.

**Large disk footprint for a "fast tier" model.** At 9.6 GB, it's 3x larger than qwen3.5:4b (3.4 GB) for similar extraction scores. The multimodal architecture adds weight that Perian doesn't currently use. As a fast-tier candidate, the size penalty is hard to justify unless we eventually need image processing (screenshot-based job listings?).

## Specifications

| Field | Value |
|-------|-------|
| Ollama tag | `gemma4:e4b` |
| Parameters | 4.5B effective (8B with embeddings) |
| Size on disk | 9.6 GB |
| Family | Gemma 4 |
| Context window | 128K |
| Architecture | Dense, edge-optimized, multimodal |
| Think parameter | `think: false` works (despite docs suggesting otherwise) |
| License | Apache 2.0 |

## Open Questions

- What are the actual scores with thinking disabled? Current eval may be 3-4x too slow and representing a ceiling, not operational performance.
- Does native function calling offer a better structured output path than JSON Schema constrained decoding?
- Is 9.6 GB justified over qwen3.5:4b (3.4 GB) for any Perian task?

## Cross-References

- [[models/qwen3.5-4b]] — primary competitor at the fast tier (3x smaller)
- [[models/gemma4-26b]] — same family, MoE variant
- [[techniques/kv-cache-quantization]] — biggest beneficiary in fleet
- [[techniques/json-schema-constrained-decoding]] — function calling as alternative

## Evidence Log

| Date | Observation | Source |
|------|-------------|--------|
| 2026-04-03 | Found by Legolas, downloaded | model-fleet.json |
| 2026-04-04 | First eval (with thinking): extraction 0.93, classification 1.00 | fleet-changelog |
| 2026-04-04 | Discovered think:false works on Gemma 4 (contrary to docs) | OB insight |
| 2026-04-05 | KV cache q8_0 gave 40% speedup — biggest in fleet | OB observation |
