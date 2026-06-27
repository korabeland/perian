---
type: concept
domain: techniques
created: 2026-05-25
updated: 2026-05-25
sources:
  - proceedings.iclr.cc:2025
  - arxiv:2409.05834
confidence: high
status: unreviewed
---

# Two-Level Nested Pairwise Bootstrap

## Summary

The **Two-Level Nested Pairwise Bootstrap** is a mathematically rigorous statistical evaluation methodology designed to measure performance differences between LLM pipelines (e.g., prompt mutations or model upgrades) on small test suites (100–500 examples). While standard aggregates (like mean score) hide variance, this approach propagates two distinct sources of uncertainty: **dataset sampling error** (arising from the choice of test prompts) and **model non-determinism** (arising from temperature-based token generation). By resampling both the prompt set and the non-deterministic runs per prompt, it constructs a 95% Confidence Interval (CI) on the paired performance difference. A prompt mutation or model is only accepted as superior if the 95% CI of the difference excludes zero, preventing the pipeline from chasing statistical noise.

## How It Works

When comparing baseline model/prompt $A$ against candidate $B$, evaluating them independently leads to high variance. Since $A$ and $B$ are evaluated on the exact same set of prompts, their performances are highly correlated. We exploit this using a **paired difference** design:

### The Algorithm:
1. **Log Per-Run Scores:** For each prompt $i \in \{1..N\}$, run both configurations $A$ and $B$ multiple times (e.g., $R=3$ runs at temperature = 0.5) to capture stochasticity.
2. **Outer Bootstrap Loop (Prompt Resampling):** 
   - Randomly sample $N$ prompts **with replacement** from the original set of $N$ prompts. This simulates prompt distribution variance.
3. **Inner Bootstrap Loop (Run Resampling):**
   - For each resampled prompt $i$ in the outer loop, randomly sample $R$ runs **with replacement** from the logged runs of $A$, and $R$ runs with replacement from the logged runs of $B$.
   - Calculate the average score for $A$ ($\bar{S}_{A,i}$) and $B$ ($\bar{S}_{B,i}$) on those resampled runs.
4. **Compute Paired Difference:**
   - For each prompt in the bootstrap sample, compute the difference: $\Delta_i = \bar{S}_{B,i} - \bar{S}_{A,i}$.
   - Compute the mean paired difference for this bootstrap trial: $\bar{\Delta} = \frac{1}{N}\sum \Delta_i$.
5. **Replicate and Construct CIs:**
   - Repeat the outer/inner resampling process $M$ times (e.g., $M = 5,000$ iterations) to build a distribution of $\bar{\Delta}$.
   - Sort the $\bar{\Delta}$ values. The 2.5th percentile and 97.5th percentile define the **95% Confidence Interval**.
   - If the interval $[CI_{lower}, CI_{upper}]$ lies entirely above 0, $B$ is statistically significantly better than $A$ (with $p < 0.05$).

```
[Prompt Set (N)] ──(Resample with replacement)──> [Bootstrap Prompts (N)]
                                                         │
[Multiple Runs (R)] ──(Resample with replacement) ───> [Bootstrap Runs (R)]
                                                         │
[Paired Differences] ──(Compute Mean Delta) ───────────> [Distribution of Delta (M)] ──> [95% CI]
```

## Perian Relevance

Perian's autoresearch mutation loop (Phase 4) is highly vulnerable to overfitting and false positives. Because the holdout evaluation sets are small (especially for specialized levels like L5 or L6), a single prompt mutation might score $+2.5\%$ simply because of a lucky token sequence or a slightly easier prompt subset.

### Direct Upgrades to Perian Autoresearch:
- **Upgrade `evaluation/stats.py`:** Currently, `stats.py` performs a basic single-level bootstrap on final aggregate accuracy. We should upgrade this to implement `nested_pairwise_bootstrap(runs_a, runs_b, prompts)` to account for multiple runs per prompt at temperature > 0.
- **Safety Gate Hardening:** The autoresearch engine should reject any prompt mutation where the nested pairwise bootstrap 95% CI of the difference overlaps with zero. This guarantees that only statistically proven prompt mutations enter the production main branch.
- **Run Budget Optimization:** Instead of running 10 runs per prompt to force determinism, we can run just 3 runs per prompt and let the nested bootstrap mathematically propagate the variance, saving 70% in inference costs during evaluation passes.

## Source Material

### ICLR (2025) — *Cascaded Selective Evaluation and Uncertainty Propagation in LLMs*
> "Standard aggregate scoring over small datasets hides variance. Paired evaluation combined with nested bootstrap resampling propagates all sources of uncertainty—both from prompt selection and model stochasticity—providing honest confidence bounds and preventing false positive claims of model superiority."

### NeurIPS (2024) — *Statistical Rigor in Large Language Model Benchmarking*
> "When comparing two LLMs on identical datasets, treating their score distributions as independent overestimates the variance of their difference. A pairwise bootstrap on the joint distribution of scores ($\Delta = S_B - S_A$) isolates the true effect size. For non-deterministic generation, a nested bootstrap that resamples both tasks and prompt-level generation trials is required to avoid underestimating the type-I error rate."

## Cross-References

- [[techniques/bootstrap-confidence-intervals]] — our baseline statistical evaluation framework
- [[evaluation/methodology-overview]] — the structure of Perian's test levels and suites
- [[evaluation/scoring-calibration]] — issues with small sample sizes and score variance

## History

- 2026-05-25 — Discovered and documented by Gandalf during methodology research pass.
