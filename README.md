# Perian

A framework for **local-first AI agents** — plan once with a frontier model, execute locally at zero marginal cost, refine on demand — together with **ATB**, a self-improving build/trust pipeline that builds and hardens software with local models under a strict *doer ≠ certifier* discipline.

> **Frozen snapshot (June 2026).** A curated, manually-published snapshot of the
> Perian framework — not a live mirror. Active development happens in a private
> lab and is **not** automatically synced here, so this tree may lag it.

## Layout

| Path | What |
|------|------|
| `perian-core/` | The **Perian** framework — multi-model router, evaluation methodology, model/technique knowledge base. → [`perian-core/README.md`](perian-core/README.md) |
| `build-arm/`   | **ATB** — the self-improving build/trust system: a thinking loop and a building loop with a stack of judges between them. → [`build-arm/README.md`](build-arm/README.md) |
| `spine/`       | Shared substrate — scheduler, memory governor, model server, shared config. |
| `discovery/` `products/` `shared/` | Reserved for future components. |

The job-search agent Perian was developed around ships separately as a runnable reference on placeholder data: **[perian-jobsearch](https://github.com/korabeland/perian-jobsearch)**.

## Test

```bash
python -m pytest        # runs the ATB suite (build-arm/) + spine tests from the repo root
```

## License

[MIT](LICENSE)
