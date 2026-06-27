# Perian — Public Surface

This repository is a curated public extract of the Perian monorepo: the **framework**
(model routing, evaluation methodology, model/technique knowledge base) and the
**ATB build/trust system**. It is published as a fresh, hand-authored history — no
legacy commits are carried over.

The **job-search** workflow that Perian was first built around ships separately as a
public reference (`perian-jobsearch`) on placeholder data. The maintainer's real
job-search application and personal data are private and are **not** in this repo.

## In the public surface
- `build-arm/` — the ATB build/trust engine: `tools/`, `schemas/`, `scripts/`, `config/`, top-level methodology docs.
- `spine/` — scheduler, memory governor, shared config.
- `perian-core/` — the generic framework engine: model router, Ollama utilities, fleet logging, the evaluation **stat libraries** (`stats`, `compare`, `calibration`, `semantic`), the `wiki/` knowledge base, and generic model/runtime config.
- Root scaffolding — `pyproject.toml`, `conftest.py`, `README.md`.

## Excluded from the public surface (and why)
| Excluded | Reason |
|---|---|
| The job-search pipeline (`runner`, `schemas`, `collect`, `feedback`, `triage_*`, `frontend/`, eval harness, `autoresearch/`) | Ships in `perian-jobsearch` as a sanitized reference |
| job-pipeline state files, `evaluation/results/`, run-logs, councils, postmortems, overseer records | Run-state & working artifacts; carry job-search and personal data (Decision #3) |
| `profile.md`, `config/watchlist.yaml`, prompts, feedback/enrich/triage state | Personal data (real profile + target companies) |
| `docs/plans/`, `brainstorms/`, working journals (`DEVLOG`, `TODO`, `ACTION_PLAN`, `IMPROVEMENT_LOG`) | Internal planning history; reference personal projects |

## Enforcement
- `.gitignore` (root) guards every excluded path so private paths cannot silently re-enter or be committed to the public export.
- A single-tree privacy clearance (content-string search + secret scanners + independent Codex review) gates publication — see the private inventory/clearance under `build-arm/docs/` (themselves never published).
- An owner-identity / local-path scrub (no home paths or machine-specific identifiers) is applied across the public surface before publish.
