#!/usr/bin/env python3
"""Ollama residency primitives for the trust spine (Perian x ATB merge, U2).

Adapts (Perian) scripts/ollama_utils.py to what swap_orchestrator + aux_watchdog
need. Inference rides Ollama's OpenAI-compatible /v1 endpoint (the crew/aux model
calls Hermes makes); RESIDENCY control rides the native /api endpoints, which /v1
does not expose:

  - keep_alive=-1  -> pin resident forever (the aux-9B-always-resident keep-set)
  - keep_alive=0   -> evict now
  - /api/ps        -> what is resident now (name + loaded byte size)
  - /api/tags      -> what is available on disk (name + disk byte size)

Unlike LM Studio's `lms load` (additive, explicit budget), Ollama natively
co-resides multiple models within its own memory budget; this module is the thin
mechanism (load/pin/evict/inspect), and swap_orchestrator keeps the deterministic
eviction policy on top of it.

Stdlib-only (urllib) so the residency path adds no dependency. Endpoint resolves
through config.paths.ollama_base_url() (OLLAMA_BASE_URL override, else :11434).
"""
from __future__ import annotations

import json
import urllib.request

from config import paths


def base_url() -> str:
    return paths.ollama_base_url()


def _get(path: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(base_url() + path, timeout=timeout) as resp:
        return json.loads(resp.read() or "{}")


def _post(path: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        base_url() + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or "{}")


def canonical(name: str) -> str:
    """Strip Ollama's implicit ':latest' so 'qwen3.6-35b-a3b' and
    'qwen3.6-35b-a3b:latest' compare equal."""
    return name[: -len(":latest")] if name.endswith(":latest") else name


def list_resident(timeout: float = 10.0) -> list[dict]:
    """Currently-resident models from /api/ps: [{'name','size',...}, ...]."""
    return _get("/api/ps", timeout).get("models", [])


def list_available(timeout: float = 10.0) -> list[dict]:
    """All pulled models from /api/tags: [{'name','size',...}, ...]."""
    return _get("/api/tags", timeout).get("models", [])


def warm(model: str, *, keep_alive: int | str = -1, num_ctx: int | None = None,
         timeout: float = 600.0) -> None:
    """Load + pin `model` (keep_alive=-1 = forever). Empty `messages` is Ollama's
    load-only path: it makes the model resident WITHOUT running a generation (so a
    thinking model doesn't burn a long reasoning pass just to warm up). Raises on
    HTTP/URL failure."""
    payload: dict = {
        "model": model,
        "messages": [],
        "stream": False,
        "keep_alive": keep_alive,
    }
    if num_ctx:
        payload["options"] = {"num_ctx": num_ctx}
    _post("/api/chat", payload, timeout)


def unload(model: str, timeout: float = 60.0) -> None:
    """Evict `model` now (empty messages + keep_alive=0)."""
    _post(
        "/api/chat",
        {"model": model, "messages": [], "stream": False, "keep_alive": 0},
        timeout,
    )
