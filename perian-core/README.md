# Perian

> *A framework for local-first AI agents — plan once with a frontier model, execute locally at zero marginal cost, refine on demand.*

**Plan Once, Execute Locally, Refine on Demand.**

---

## Why Perian?

Cloud AI agents are expensive to run, lock you into provider APIs, and hide their reasoning behind opaque endpoints. Every execution costs money; every outage is your outage.

Most daily agent work — extracting structured data, scoring against criteria, classifying and ranking — doesn't need a frontier model. Small local models handle it. The insight at the core of Perian: separate **design-time intelligence** (where a frontier model shines — prompt design, evaluation, judging) from **run-time execution** (where small local models are sufficient and free).

Perian is the framework built around that split:

- A **multi-model router** picks the cheapest local model that meets each task's capability bar, escalating to a larger model only on measured low confidence.
- An **evaluation methodology** measures model capability with statistical rigor — bootstrap confidence intervals, calibration (ECE), pairwise significance — so routing is evidence-based, not guesswork.
- A **knowledge base** (`wiki/`) accumulates what's been learned about each model and technique over time.
- An **autoresearch loop** uses a frontier model to optimize prompts offline, gated so no regression ships.

## The job-search example

The framework was developed and optimized through a concrete use case — a **job-search agent** that collects listings from multiple sources, extracts and scores them against a profile, and produces a daily briefing. That agent ships separately as a runnable reference on placeholder data:

➡️ **[perian-jobsearch](https://github.com/korabeland/perian-jobsearch)** — the end-to-end pipeline (collection, extraction, scoring, nightly orchestration, adversarial challenge) built on this framework.

Job search is the learning context, not the end goal. Every pattern discovered there — adversarial scoring, measured routing, feedback-driven calibration — generalizes to any domain where structured data is collected, evaluated, and refined over time.

This repository is the reusable **framework engine** plus the **[ATB build/trust system](../build-arm/README.md)**.

## Components

| Path | What |
|---|---|
| `scripts/router.py` | Multi-model router — cheapest-capable selection + confidence escalation |
| `scripts/ollama_utils.py` | Model lifecycle (warm-up / residency / cooldown) on Ollama |
| `evaluation/stats.py` | Bootstrap confidence intervals (incl. two-level nested pairwise) |
| `evaluation/calibration.py` | Calibration / expected-calibration-error analysis |
| `evaluation/compare.py` | Pairwise model comparison with significance testing |
| `evaluation/semantic.py` | Semantic scoring helper |
| `wiki/` | Knowledge base — model pages + technique pages (the accumulated understanding) |
| `config/` | Fleet + router configuration |

## Engineering Principles

- **Daily execution never calls a cloud API.** The fleet runs on Ollama. Cloud APIs are development-time tools — prompt optimization, methodology research, plan review.
- **Smallest model, lightest process, least abstraction that solves the problem.** Small model first; escalate only on measured low confidence.
- **Everything is a file.** YAML config, JSON state, Markdown output. No databases — every artifact is inspectable with `cat` and trackable with `git`.
- **Fail visibly.** Log errors, skip items, never silently degrade.
- **Never pay for reasoning you've already done.** Prefix caching reuses the static prompt prefix; measured routing avoids re-evaluating model capabilities; feedback corrections flow into training data.

## Built With

- [Ollama](https://ollama.com) — local model serving on Apple Silicon
- [Qwen 3.5](https://huggingface.co/Qwen) + [Gemma 4](https://huggingface.co/google) — small dense models for run-time work, a cross-family MoE for adversarial challenge
- [Claude API](https://docs.anthropic.com) — development-time prompt optimization and methodology research only
- Python — no ML frameworks, just `requests` and `pyyaml`
