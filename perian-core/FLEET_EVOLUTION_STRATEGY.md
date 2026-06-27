# Fleet Evolution Strategy

> **Operational policy for adopting, retiring, and assigning local LLMs in Perian.**
>
> This document synthesizes research from two deep analyses (see `Build Prompts/`)
> and three Gandalf findings (`autoresearch/methodology/finding-2026-04-05-*.md`)
> into actionable fleet decisions. For current fleet composition and routing logic,
> see [ARCHITECTURE.md](ARCHITECTURE.md). For eval methodology, see
> [evaluation/EVAL_METHODOLOGY.md](evaluation/EVAL_METHODOLOGY.md).

---

## 1. Principles

Six principles guide fleet decisions. Each was tested against peer-reviewed
research; annotations note where the evidence confirmed, refined, or corrected
our initial assumptions.

### P1: Adopt newer models from trusted producers

**Status: REFINED.**

Newer generations from established producers (Qwen, Gemma) should be adopted,
but not blindly. Research on prompt portability (Tam et al., 2024) found
performance varies up to 40% depending on prompt template format, and
within-family generation changes (e.g., Qwen 3 to 3.5) require an estimated
60-80% of initial evaluation work due to tokenizer and behavioral shifts.
Within-family size changes (e.g., 8B to 14B within the same generation) are
cheaper at 20-30%.

**Policy:** Assume improvement is likely. Use the eval suite to validate, not
gatekeep. But budget real eval time for generational changes.

### P2: Same-family coherence for the primary pipeline

**Status: CONFIRMED, with explicit caveat.**

Models from the same family share training data, tokenizers, and behavioral
patterns. This makes prompt optimization transferable across sizes and failure
modes predictable. The primary extraction and scoring pipeline benefits from
this coherence.

**The caveat:** This same coherence creates correlated failures. Kim et al.
(ICML 2025) found that same-provider models agree on the *same wrong answer*
approximately 60% of the time — far above chance. Same-family coherence is an
asset for extraction but a liability for verification.

**Policy:** Keep the primary pipeline in one family. Never use that family for
adversarial verification.

### P3: Cross-family verification is mandatory

**Status: STRONGLY SUPPORTED.**

The single highest-impact fleet change is replacing same-family adversarial
verification with cross-family verification. Three convergent lines of evidence:

- **Correlated errors are severe.** Same-provider models share ~60% of their
  errors (Kim et al., ICML 2025, 350+ LLMs evaluated).
- **Cross-family ensembles consistently win.** ReConcile (Chen et al., ACL 2024)
  found cross-family deliberation outperformed single-agent baselines by up to
  11.4%. Replacing the diverse panel with same-family copies dropped performance
  by 6.8%.
- **Diverse small models beat a single large judge.** The Panel of LLM Evaluators
  study (Verga et al., 2024) confirmed that smaller models from disjoint families
  outperform a single large judge while being 7x cheaper.

**Policy:** At least one critical verification role (adversarial challenger or
cross-model check) must use a different model family from the primary pipeline.

### P4: Models audition for roles, not rankings

**Status: STRONGLY SUPPORTED.**

Models don't compete on a single leaderboard — they audition for specific fleet
roles with distinct requirements. RouteLLM (LMSYS, 2024) demonstrated 85% cost
reduction on MT-Bench while maintaining quality through task-based routing.
LLM Cascades (ICLR 2024) achieved comparable task performance at 40% of cost
by starting with fast models and escalating uncertain cases.

**Policy:** Evaluate candidates against role requirements, not aggregate
benchmarks. A model that fails as a first-pass scorer might excel as a triage
gate or cross-family checker.

### P5: Evaluation infrastructure is the durable asset

**Status: REFINED.**

Initially framed as "accumulated knowledge about model families is the real
asset." Research refined this: behavioral knowledge depreciates every ~4 months
with each new generation (tokenizer changes, alignment shifts, behavioral
differences). The 60-80% re-evaluation cost per generation means specific model
knowledge has a short half-life.

What *doesn't* depreciate: the eval suite, the holdout data, the monitoring
pipelines, the feedback loop, the scoring rubrics. These let you rapidly assess
any new model from any family against your specific task distribution.

**Policy:** Invest in eval infrastructure (Galadriel), not in memorizing model
quirks. Track behavioral profiles in the knowledge base, but treat them as
perishable context, not permanent truth.

### P6: Sequential execution, all models pre-loaded

**Status: CORRECTED.**

Initially framed as "hardware should be fully utilized via concurrent
multi-model inference." Research contradicts this for the decode phase.

LLM token generation is memory-bandwidth-bound. The M5 Pro's 307 GB/s bandwidth
must be shared across all active models. Running two models simultaneously
roughly halves each model's throughput. True hardware utilization means
saturating the full bandwidth channel for one model at a time, not partially
saturating it for four.

However, prefill (prompt processing) is compute-bound, not bandwidth-bound.
Overlapping one model's prefill with another's decode could yield throughput
gains — a more nuanced strategy than pure sequential.

**Policy:** Keep all pipeline models pre-loaded to eliminate swap latency.
Process items sequentially so each model gets full bandwidth during its turn.
Do not attempt concurrent decode for throughput gains.

---

## 2. Fleet Roles

Each role has distinct requirements. A model earns a role by demonstrating
competence on that role's specific demands, not by general benchmark scores.

| Role | What it does | Key requirement | Family constraint | Perian component |
|------|-------------|-----------------|-------------------|------------------|
| **Triage gate** | Binary classification: relevant or skip | Speed, low memory, high recall | Same family OK | `runner.py` step_triage() |
| **Primary extraction** | Raw text to structured JSON | Instruction following, JSON compliance | Primary family | `runner.py` step_extract() |
| **Primary scoring** | Extracted JSON to 0-1 dimensional scores | Conditional reasoning, calibration | Primary family | `runner.py` step_score() |
| **Adversarial challenger** | Challenge high-scoring items per dimension | Independent judgment, boundedness | **Must be cross-family** | `aragorn_challenge.py` |
| **Cross-model check** | Replay extraction through different model | Field-level accuracy, schema compliance | Cross-family preferred | `cross_model_check.py` |
| **Escalation target** | Re-run on harder model when confidence is low | Highest capability in primary family | Primary family | `router.py` escalation |

### Role-specific audition criteria

- **Triage audition:** <500ms latency on title-only input. >90% recall (never
  miss a true positive). Precision secondary — false positives get caught later.
- **Extraction audition:** L1 field accuracy >= 0.90. JSON parse rate = 1.00.
  No "binding drift" (misattributing fields between entities).
- **Scoring audition:** L3 score correlation >= 0.80. Bonus/penalty accuracy
  >= 0.85. Calibration (ECE) < 0.30.
- **Challenger audition:** Must identify errors that the primary pipeline misses.
  Benchmark: run on items where human review found errors that the current
  same-family challenger approved. Cross-family must catch more.
- **Cross-check audition:** L1 field accuracy >= 0.88 (slightly lower bar than
  primary, since disagreements are investigated, not auto-accepted).

---

## 3. Family Selection

### Primary family: Qwen

**Rationale:**
- Widest size range from any single producer (0.6B to 235B+)
- Best instruction following at the 7-8B tier (IFBench leader, 2026)
- Stable Apache 2.0 licensing
- Fastest Ollama support for new releases
- Established prompt optimization from Perian's autoresearch (Phase 4)
- ~4 month release cadence with consistent quality improvement (2.5 → 3 → 3.5)

**Known weakness:** 80% boundary adherence in enterprise testing (vs Llama's
92%). Qwen occasionally complies with requests it should refuse — a specific
liability for the adversarial role, where the model must reject invalid inputs.

### Verification family: Gemma

**Rationale:**
- Apache 2.0 license (from Gemma 4 onward)
- Dedicated function-calling tokens (`<|tool>`, `<|tool_call>`, etc.) — most
  architecturally robust structured output of the four major families
- Strong reasoning density — Gemma 4 26B MoE achieves 97% of 31B dense quality
  at significantly less compute
- Fast Ollama support (Gemma 4 supported in Ollama 0.20, within days of launch)
- Different training data (Google vs Alibaba) ensures error decorrelation

**Current Gemma candidates in fleet:** gemma4:31b (20GB, dense), gemma4:26b
(18GB, MoE 3.8B active), gemma4:e4b (9.6GB, edge-optimized).

### Evaluating a new family

Before introducing a model from a new family into any fleet role:

1. **Full L1-L6 eval** with production prompts (measures prompt-fit)
2. **Full L1-L6 eval** with stripped prompts (measures raw capability)
3. **Prompt-fit gap** analysis (production score minus stripped score)
4. **Cross-family error detection benchmark**: run on items where human review
   found pipeline errors. Does the candidate catch them?
5. **Behavioral fingerprint**: note systematic tendencies (conservative scoring,
   hallucination patterns, boundary adherence)

### Producer reliability

| Producer | Release cadence | License | Quality trend | Rating |
|----------|----------------|---------|---------------|--------|
| Qwen (Alibaba) | ~4 months | Apache 2.0 (stable) | Strong upward | A- |
| Gemma (Google) | ~6 months | Apache 2.0 (from Gemma 4) | Dramatic improvement | B+ |
| Llama (Meta) | ~4-6 months | Llama License (restrictive) | Strong but Llama 4 controversial | B |
| Mistral | Variable | Apache 2.0 (has flip-flopped) | Good but uneven | B- |

---

## 4. Adoption & Retirement Cycle

### Scenario 1: New generation within primary family

*Example: Qwen 3.5 to Qwen 4.*

This is the most common and most expensive transition. Tokenizer changes, chat
template changes, and behavioral shifts mean prompts may not transfer cleanly.

**Process:**
1. Run full L1-L6 eval with existing production prompts
2. Run full L1-L6 eval with stripped prompts
3. Compare prompt-fit gap against current generation
4. If production eval shows regression > 5% on any L1-L4 capability: investigate
   prompt-fit before rejecting. Run autoresearch for 1-2 iterations if gap > 0.05.
5. If production eval passes: adopt. Update fleet.yaml, re-baseline.

**Budget:** ~60-80% of initial evaluation work.

### Scenario 2: New size within same generation

*Example: Qwen 3.5:4b alongside existing Qwen 3.5:27b.*

Same tokenizer, same chat template. Behavioral patterns scale predictably.

**Process:**
1. Run L1-L4 eval with production prompts (skip L5-L6 unless role requires them)
2. Compare against the model it would replace in the fleet
3. Apply upgrade decision framework from EVAL_METHODOLOGY.md §5

**Budget:** ~20-30% of initial evaluation work.

### Scenario 3: New family for verification role

*Example: Adding Gemma 4 as adversarial challenger.*

The bar is different: the model doesn't need to match the primary pipeline's
prompt optimization. It needs to produce valid JSON and make *independent*
judgments.

**Process:**
1. Full L1-L6 eval with production prompts
2. Cross-family error detection test: take items where human review found errors
   that the current challenger approved. Run the candidate on those items.
3. Measure independent error detection rate vs current same-family challenger
4. If cross-family catches more errors: adopt for verification role

**Budget:** Full eval + cross-family benchmark (~100% of initial eval work, but
a one-time cost that pays for itself through better error detection).

### Scenario 4: Retirement

A model exits the fleet when:
- It has been superseded within its family at its size tier
- No role specifically requires its unique characteristics
- It hasn't been used in any pipeline run for 30+ days

**Process:** Mark as `phasing_out` in fleet.yaml. Keep downloaded for 2 weeks.
Remove from Ollama after confirming no regressions.

### Delta-Evaluation for generational upgrades

For major generational changes, a full re-eval is expensive. Focus on three
metrics that predict real-world impact:

1. **Instruction drift:** Does the new model follow the same scoring rubrics,
   or has its alignment shifted its "conservatism"? Run L3 cases and compare
   score distributions.
2. **Ambiguity decomposition:** Does the new model maintain its dissimilarity to
   the verification family? If a new Qwen becomes more "Gemma-like" in behavior,
   the cross-family benefit weakens.
3. **Latency-accuracy Pareto:** Does the new model provide enough accuracy gain
   to justify its resource cost?

---

## 5. Verification Architecture

Research supports a three-layer verification stack, not debate.

### Layer 1: Deterministic validation

**Already implemented** via `scripts/schemas.py` (JSON Schema constrained
decoding) and floor penalties in `runner.py`.

- JSON parsing, schema conformance, type checking
- Dimension floor penalties (title_match < 0.3, seniority_match < 0.2)
- Business rules (compensation range sanity, enum validation)
- Catches 60-70% of structural errors at zero inference cost (Cleanlab
  structured output benchmark, Dec 2025)

### Layer 2: Cross-family per-field verification

**The highest-ROI improvement not yet implemented.**

Run a verification model from a different family on extraction and scoring
output. Compare per-field, not whole-document — per-field verification
consistently outperforms holistic LLM-as-judge approaches with 25% greater
precision/recall (Cleanlab, Dec 2025).

**Architecture:**
```
Primary model (Qwen) extracts → Verification model (Gemma) re-extracts
    ↓                               ↓
Per-field comparison:
  - All fields agree → Accept (high confidence)
  - 1-2 fields disagree → Weighted merge (weights from calibration data)
  - 3+ fields disagree → Escalate to Layer 3
  - Off-ontology output → Reject, use other model's output
```

**Key design decisions:**
- Verify ALL items, not a stratified sample. The verification model is small
  enough (gemma4:e4b at 4.5B effective) that full coverage is practical.
- Per-field, not whole-document. Each field gets independent comparison.
- Track per-model, per-field accuracy over time to calibrate merge weights.

### Layer 3: Escalation

For items with multi-field disagreement, escalate to the largest available model
(qwen3.5:27b) as tiebreaker. Load on demand — see Hardware Governance.

### What NOT to do

**Do not implement multi-agent debate for structured output.** A comprehensive
ICLR 2025 evaluation of five multi-agent debate frameworks across nine
benchmarks found that most debate frameworks fail to consistently outperform
simple self-consistency (resampling from the same model). Multi-Persona with
weaker models catastrophically underperformed baselines.

**Do not implement multi-round review.** Cross-Context Review (Song, arXiv
2603.12123) found that the optimal number of review rounds is exactly one.
Extending to multi-turn causes "false positive pressure" (reviewers fabricate
findings) and "review target drift" (reviewers shift focus from the artifact to
the conversation).

---

## 6. Hardware Governance

### Platform: 48GB Apple Silicon M5 Pro

| Spec | Value |
|------|-------|
| Total unified memory | 48 GB |
| Memory bandwidth | 307 GB/s |
| GPU cores | 20 |
| Neural Accelerators | Yes (M5 generation) |
| Ollama version | 0.20.0 |
| Inference backend | MLX (via Ollama 0.19+) |

### Memory budget

**Rule: Model weights must not exceed 60% of total RAM (28.8 GB).**

Exceeding this pushes the system into swap during long-context generation,
collapsing throughput from interactive speeds to unusable levels. With macOS and
system services consuming ~6-8 GB, the practical model budget is ~28-30 GB.

| Configuration | Weight budget | Fits? |
|---------------|--------------|-------|
| Current fleet (2B + 8B + 14B + 27B) | ~34 GB | Tight — requires KV cache quantization |
| Proposed (2B + 8B + Gemma 9.6B + 27B on-demand) | ~17.2 GB resident, 34.2 GB peak | Comfortable with on-demand 27B |
| All five resident with KV cache q8_0 | ~39-41 GB | Possible but <7 GB headroom |

### Execution model

**Sequential with all pipeline models pre-loaded.**

```bash
# Recommended Ollama configuration
export OLLAMA_MAX_LOADED_MODELS=4      # Keep pipeline models resident
export OLLAMA_KEEP_ALIVE=-1            # Never auto-unload
export OLLAMA_FLASH_ATTENTION=1        # Reduce KV cache memory
export OLLAMA_KV_CACHE_TYPE=q8_0       # Halve KV cache size
export OLLAMA_NUM_PARALLEL=1           # Minimize per-model KV allocation
```

**Why sequential, not concurrent:** Token generation requires reading full model
weights per token. With all four models decoding simultaneously, shared bandwidth
gives each model ~5-10 tok/s — severe degradation. Sequential execution gives
each model full 307 GB/s bandwidth during its turn.

**Exception:** Prefill (prompt processing) is compute-bound, not bandwidth-bound.
Overlapping the next model's prefill with the current model's decode could yield
throughput gains. Not yet implemented in Shadowfax.

### MLX backend

Ollama 0.19+ replaced llama.cpp with Apple's MLX framework on Apple Silicon.
Benchmarked gains: 57% faster prefill, 93% faster decode. MLX's zero-copy
unified memory makes concurrent model loading more efficient than llama.cpp.

**Action needed:** Verify `OLLAMA_RUNNERS=mlx` is active on our Ollama 0.20.0
installation. Check compatibility with all fleet model architectures (Qwen 3.5
confirmed, Gemma 4 requires 0.20+).

---

## 7. Knowledge Management

### Behavioral profiles

Track per-family behavioral characteristics in the knowledge base
(`wiki/models/`). Key dimensions:

- JSON compliance rate by field type
- Scoring distribution (conservative vs generous)
- Boundary adherence (does the model refuse invalid requests?)
- Hallucination rate by field type
- Instruction following precision

**These profiles are perishable.** Update when a new generation ships. Treat
them as context for role assignment decisions, not permanent truth.

### Prompt versioning

Treat prompts as **versioned, immutable artifacts**. When a new model generation
arrives, replay the exact prompt suite for comparison. This is already supported
by the eval suite's `--prompt-variant` flag and the prompt files in `prompts/`.

### Eval infrastructure investment priorities

The eval suite (Galadriel) is the durable asset. Invest in:

1. **Test case expansion** — 40+ cases per level (currently ~10-12)
2. **Cross-family error detection benchmark** — items where human review found
   errors, used to compare same-family vs cross-family verification
3. **Calibration tracking** — ECE over time per model, per role
4. **Regression CI** — weekly eval runs against the primary pipeline model
   (already enabled: weekly:SAT via Shadowfax)

### Source tracking

Research papers and methodology briefs live in `autoresearch/methodology/`.
Fleet decisions and their rationale are logged in `state/fleet-changelog.jsonl`.
Synthesized knowledge goes to the Palantir knowledge base (`wiki/`).

---

## 8. Open Questions

These are unresolved by current research and require experimentation:

1. **Custom cross-family benchmark.** No published study measures cross-family vs
   same-family verification gains on structured JSON extraction at the 2B-27B
   parameter range. Building a custom benchmark on Perian's actual task
   distribution is the highest-value next step.

2. **Quantization and diversity interaction.** Aggressive quantization (Q4) may
   compress model-specific behaviors, reducing between-family diversity. No study
   has tested this. Monitor whether cross-family disagreement rates change when
   moving between quantization levels.

3. **MLX runner stability on M5 Pro.** Some users report bfloat/half type
   mismatches on M5 chips (Ollama GitHub issue #14432). Verify stability before
   enabling MLX as the default runner for overnight processing.

4. **Vocabulary convergence across families.** As model producers increasingly
   use each other's high-quality synthetic data for training (e.g., Llama 4
   trained on Qwen outputs), behavioral signatures and error manifolds may
   converge. This could erode the cross-family ensemble benefit over 12-18
   months. Monitor cross-family disagreement rates longitudinally.

---

## References

Key papers cited in this document. Full analyses in `Build Prompts/`.

| Paper | Venue | Key finding |
|-------|-------|-------------|
| Kim et al., "Correlated Errors in LLMs" | ICML 2025 | Same-provider models share ~60% of errors |
| Chen et al., "ReConcile" | ACL 2024 | Cross-family +11.4%, same-family -6.8% |
| Tekin et al., "LLM-TOPLA" | EMNLP 2024 | Focal diversity metric; diversity-optimized ensembles outperform |
| ICLR 2025 MAD evaluation | ICLR 2025 | Multi-agent debate fails to beat self-consistency for structured output |
| Cleanlab structured output benchmark | Dec 2025 | Per-field verification 25% better precision/recall than holistic |
| Song, "Cross-Context Review" | arXiv 2603.12123 | Single-pass review optimal; multi-round degrades |
| Wu et al., "TensorGuard" | 2025 | 94% family identification accuracy across 58 models |
| RouteLLM | LMSYS 2024 | 85% cost reduction via task-based routing |
| Verga et al., "PoLL" | 2024 | Diverse small judges outperform single large judge, 7x cheaper |
| Tam et al., prompt portability | 2024 | Performance varies up to 40% by prompt template format |
