---
type: concept
domain: techniques
created: 2026-04-05
updated: 2026-04-05
sources:
  - autoresearch/methodology/finding-2026-03-27-2.md
confidence: high
status: current
---

## Summary

Ollama v0.5+ accepts a JSON Schema object in the `format` parameter of `/api/chat`. Ollama converts it to a GBNF grammar via llama.cpp, guaranteeing structurally valid JSON at the decoding level. Zero-overhead (<80ms for typical outputs). **Status: implemented** — Perian now uses `"format": "json"` for constrained decoding with schemas defined in `scripts/schemas.py`.

## How It Works

```python
response = requests.post("http://localhost:11434/api/chat", json={
    "model": "qwen3:8b",
    "messages": [{"role": "user", "content": prompt}],
    "format": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "match_score": {"type": "number", "minimum": 0, "maximum": 100}
        },
        "required": ["title", "match_score"]
    },
    "options": {"logprobs": True},
    "think": False
})
# json.loads() always succeeds — output is guaranteed valid
```

The grammar masks invalid tokens at each generation step. XGrammar engine achieves <40 microseconds per token mask.

## What It Changed in Perian

**Eliminated:**
- JSON parse failures from defensive parsing in `helpers.py`
- Markdown fence stripping logic
- Math expression fixing in JSON values
- Retry logic for malformed outputs

**Improved:**
- Extraction quality — model focuses on content, not syntax
- Eval reliability — format is never the failure mode
- Logprob signal quality — grammar-forced tokens are identifiable

**Caveats:**
- Logprobs on grammar-forced tokens (brackets, colons, quotes) are artificially high — must filter when computing confidence scores
- Schema must exactly match prompt expectations

## Perian Implementation

- Schemas defined in `scripts/schemas.py` (extraction, scoring, triage, challenge)
- Pipeline uses `"format": "json"` in all Ollama calls per CLAUDE.md conventions
- `parse_json_response()` retained as fallback

## Cross-References

- [[techniques/cascade-routing-logprobs]] — logprob filtering needed for grammar-forced tokens
- Model entity pages in `models/` — json_output now 0.98-1.00 across all models
- [[evaluation/metric-definitions]] — json_parse_rate metric context

## History

| Date | Change | Source |
|------|--------|--------|
| 2026-03-27 | Gandalf finding (high actionability) | finding-2026-03-27-2 |
| 2026-03-27 | Implemented in pipeline (schemas.py, format parameter) | CLAUDE.md conventions |
| 2026-04-05 | Palantir page created | palantir seed |
