"""Unit tests for tools.ollama_client (U2 residency mechanism).

No live Ollama: HTTP is monkeypatched. These pin the residency-control contract
(empty-messages load path, keep_alive semantics, name canonicalization).
"""
import tools.ollama_client as oc


def test_canonical_strips_only_latest():
    assert oc.canonical("qwen3.6-35b-a3b:latest") == "qwen3.6-35b-a3b"
    assert oc.canonical("qwen3.5-9b") == "qwen3.5-9b"
    assert oc.canonical("model:q4_k_m") == "model:q4_k_m"  # non-latest tag preserved


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example:9999")
    assert oc.base_url() == "http://example:9999"


def test_base_url_default(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert oc.base_url() == "http://127.0.0.1:11434"


def test_list_resident_parses_models(monkeypatch):
    monkeypatch.setattr(oc, "_get", lambda path, timeout=10.0: {"models": [{"name": "a:latest", "size": 1}]})
    assert oc.list_resident() == [{"name": "a:latest", "size": 1}]


def test_list_resident_empty_when_no_models_key(monkeypatch):
    monkeypatch.setattr(oc, "_get", lambda path, timeout=10.0: {})
    assert oc.list_resident() == []


def test_list_available_parses_models(monkeypatch):
    monkeypatch.setattr(oc, "_get", lambda path, timeout=10.0: {"models": [{"name": "x", "size": 2}]})
    assert oc.list_available()[0]["name"] == "x"


def test_warm_uses_empty_messages_and_keepalive(monkeypatch):
    captured = {}
    monkeypatch.setattr(oc, "_post", lambda path, payload, timeout: captured.update(path=path, payload=payload) or {})
    oc.warm("m", keep_alive=-1)
    assert captured["path"] == "/api/chat"
    assert captured["payload"]["messages"] == []          # load-only, no generation
    assert captured["payload"]["keep_alive"] == -1        # pinned forever
    assert "options" not in captured["payload"]


def test_warm_with_num_ctx_sets_options(monkeypatch):
    captured = {}
    monkeypatch.setattr(oc, "_post", lambda path, payload, timeout: captured.update(payload) or {})
    oc.warm("m", keep_alive=-1, num_ctx=80000)
    assert captured["options"] == {"num_ctx": 80000}


def test_unload_uses_keepalive_zero(monkeypatch):
    captured = {}
    monkeypatch.setattr(oc, "_post", lambda path, payload, timeout: captured.update(payload=payload) or {})
    oc.unload("m")
    assert captured["payload"]["keep_alive"] == 0
    assert captured["payload"]["messages"] == []
