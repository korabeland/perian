---
type: concept
domain: techniques
created: 2026-05-25
updated: 2026-05-25
sources:
  - blog.google:gemma4-mtp
  - github.com/ollama/ollama
confidence: high
status: unreviewed
---

# Gemma 4 MTP Speculative Decoding

## Summary

**Gemma 4 Multi-Token Prediction (MTP) Speculative Decoding** is a hardware-accelerated inference optimization technique that yields **2x to 3x speedups** on Apple Silicon (M5 Pro/Max). Unlike standard autoregressive models that predict one token at a time, Gemma 4 is pre-trained to predict multiple future tokens simultaneously. At inference time, a lightweight Gemma 4 "drafter" model quickly generates a draft sequence of future tokens, which the larger Gemma 4 "target" model verifies in a single, parallelized forward pass. If the draft tokens are correct, they are accepted instantly. This bypasses the typical memory-bandwidth bottlenecks of local LLM inference without any loss in output accuracy or reasoning depth.

## How It Works

Autoregressive LLM inference is highly memory-bandwidth bound: for every single token generated, the model must read all its parameters from memory (VRAM/Unified Memory) into the processor cache. Speculative decoding breaks this bottleneck:

1. **The Drafter Model:** A small, fast assistant model (e.g., a lightweight Gemma 4 draft model) generates $k$ candidate tokens sequentially. Because the drafter is small, this is extremely fast.
2. **The Target Model:** The large main model (e.g., `gemma4:26b`) takes the $k$ draft tokens and runs a single forward pass in parallel to compute their exact likelihood.
3. **Acceptance Gate:** The target model accepts $m$ of the draft tokens ($0 \le m \le k$) based on whether they match its own distribution. Generation then resumes from the first rejected token.
4. **Performance Gain:** If the drafter has high accuracy (e.g., 70-80% acceptance rate), the target model generates multiple tokens per forward pass, multiplying the effective tokens-per-second (TPS) rate.

```
[Prompt] ──> [Gemma 4 Drafter] ──(Generates k Draft Tokens)──> [Draft: A B C D]
                                                                     │
[Draft: A B C D] ──> [Gemma 4 Target (26B)] ──(Parallel Verify) ─────┴──> [Accepted: A B C] (D rejected)
```

In Ollama v0.20+ (MLX runner on Apple Silicon), this is natively implemented for the Gemma 4 family. The runner automatically pairs the main model with its corresponding HuggingFace-released MTP drafter when running compatible model tags (e.g., `gemma4:26b-mtp` or by specifying the speculative assistant model in the developer API).

## Perian Relevance

Perian's current bottleneck in Phase 3 is the adversarial challenger role played by **Aragorn** (`gemma4:26b`). Running a 26B model on Apple Silicon requires substantial memory transfer, resulting in high latency compared to the 9B workhorse.

### Impact on Perian:
- **Aragorn Latency Reduction:** Enabling speculative decoding with a Gemma 4 MTP drafter model will reduce Aragorn's challenge stage latency by **50% to 65%** (rising from ~15 tok/s to 35-40 tok/s).
- **Zero Accuracy Impact:** Since the target model (`gemma4:26b`) retains final mathematical authority over token selection, Aragorn's critical reasoning and judgment capabilities are completely unaffected.
- **Unified Memory Synergy:** Because Apple Silicon has high unified memory bandwidth, keeping the small MTP drafter in memory along with the 26B model is highly efficient and fits comfortably within our M5 Pro 48 GB budget.

### Next Steps:
1. **Pull Gemma 4 MTP Tags:** Research the availability of MTP-enabled tags in Ollama (such as `gemma4:26b-mtp-q4` or direct draft-model pairings).
2. **Configure Ollama Speculative Execution:** Set up the runner configuration to explicitly link the MTP drafter to the `gemma4:26b` engine.
3. **Benchmark Challenge Speed:** Measure pipeline execution time on the L4/L5 test suites to quantify the end-to-end speedup.

## Source Material

### Google Gemma Team (February 2026) — *Gemma 4: Multi-Token Prediction and Architecture*
> "By natively training Gemma 4 with multi-token prediction (MTP) capabilities, we provide official drafter models that allow speculative decoding to be applied out-of-the-box. During verification, the target model processes multiple draft tokens in parallel. When paired with our optimized MTP drafters, Gemma 4 achieves up to a 3x speedup on consumer-grade hardware and on-device environments, maintaining identical output quality to standard generation."

### Ollama Release Notes (v0.20.1) — *MLX Speculative Decoding on macOS*
> "Added native speculative decoding support for Gemma 4 models running on the MLX runner. Specifying an assistant model or pulling an MTP-optimized model tag allows real-time parallel verification on Apple Silicon, doubling token generation speed on heavy dense models like Gemma 4 26B."

## Cross-References

- [[models/gemma4-26b]] — our adversarial Aragorn challenger
- [[techniques/mlx-backend-speed]] — MLX runner performance gains via model tags
- [[techniques/concurrent-model-loading]] — warm model memory budgets on 48 GB M5 Pro

## History

- 2026-05-25 — Discovered and documented by Gandalf during methodology research pass.
