"""Unit suite for tools/web_fetch.py — the SSRF-hardened fetch primitive.

A FakeTransport stands in for DNS + the TLS GET, so every guard (allowlist, IP validation,
DNS-rebind pinning, manual redirect re-checks, size / content-type limits, HTML->text) runs with
ZERO real network. The real transport's socket-level IP pinning is exercised by the Phase-4 live
test; here we prove the orchestration that decides WHETHER and WHERE to connect.
"""

import ast
from pathlib import Path

from tools.web_fetch import (
    MAX_BYTES,
    _Response,
    fetch,
)
import tools.web_fetch as web_fetch


def _resp(status=200, ctype="text/html", body=b"<html><body>hello</body></html>", location=None):
    headers = {}
    if ctype is not None:
        headers["content-type"] = ctype
    if location is not None:
        headers["location"] = location
    return _Response(status=status, headers=headers, body=body)


class FakeTransport:
    """resolver: host->list[str] (dict, or callable(host)->list, or None=public default).
    responses: a single _Response, or a callable(host, path)->_Response."""

    def __init__(self, *, resolver=None, responses=None):
        self._resolver = resolver
        self._responses = responses if responses is not None else _resp()
        self.resolve_calls: list[str] = []
        self.get_calls: list[tuple] = []

    def resolve(self, host):
        self.resolve_calls.append(host)
        r = self._resolver
        if callable(r):
            return r(host)
        if isinstance(r, dict):
            return list(r.get(host, []))
        return ["1.2.3.4"]  # default: one public address

    def get(self, ip, host, port, path, max_bytes):
        self.get_calls.append((ip, host, port, path))
        r = self._responses
        return r(host, path) if callable(r) else r


ALLOW = ["example.com"]


# --------------------------------------------------------------------------------------
# Happy paths + extraction
# --------------------------------------------------------------------------------------
def test_happy_path_html_to_text():
    tx = FakeTransport(responses=_resp(body=b"<html><body><h1>Title</h1><p>Body text</p></body></html>"))
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert res.ok and res.status == 200
    assert "Title" in res.text and "Body text" in res.text
    assert res.final_url == "https://example.com/a"
    assert res.raw_bytes > 0


def test_html_script_and_style_stripped():
    body = b"<html><head><style>.x{}</style></head><body><script>alert(1)</script>VISIBLE</body></html>"
    tx = FakeTransport(responses=_resp(body=body))
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert res.ok
    assert "VISIBLE" in res.text
    assert "alert(1)" not in res.text and ".x{}" not in res.text


def test_plain_text_passthrough():
    tx = FakeTransport(responses=_resp(ctype="text/plain", body=b"raw evidence line"))
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert res.ok and res.text == "raw evidence line" and res.content_type == "text/plain"


# --------------------------------------------------------------------------------------
# Allowlist / URL shape rejections (these reject BEFORE any DNS or connect)
# --------------------------------------------------------------------------------------
def test_off_allowlist_domain_rejected_before_network():
    tx = FakeTransport()
    res = fetch("https://evil.com/x", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "allowlist" in res.error
    assert tx.resolve_calls == [] and tx.get_calls == []  # never touched the network


def test_non_https_rejected():
    tx = FakeTransport()
    res = fetch("http://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "non-https" in res.error and tx.get_calls == []


def test_ip_literal_host_rejected_even_if_allowlisted():
    tx = FakeTransport()
    res = fetch("https://10.0.0.1/a", allowed_domains=["10.0.0.1"], transport=tx)
    assert not res.ok and "IP-literal" in res.error and tx.resolve_calls == []


def test_userinfo_rejected():
    tx = FakeTransport()
    res = fetch("https://user:pass@example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "userinfo" in res.error and tx.get_calls == []


def test_non_443_port_rejected():
    tx = FakeTransport()
    res = fetch("https://example.com:8443/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "port" in res.error and tx.get_calls == []


# --------------------------------------------------------------------------------------
# SSRF: IP validation + DNS-rebind pinning
# --------------------------------------------------------------------------------------
def test_private_ip_rejected():
    tx = FakeTransport(resolver={"example.com": ["10.0.0.5"]})
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "non-global" in res.error and tx.get_calls == []  # never connected


def test_loopback_ip_rejected():
    tx = FakeTransport(resolver={"example.com": ["127.0.0.1"]})
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "non-global" in res.error and tx.get_calls == []


def test_link_local_metadata_ip_rejected():
    # 169.254.169.254 — the classic cloud metadata SSRF target.
    tx = FakeTransport(resolver={"example.com": ["169.254.169.254"]})
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "non-global" in res.error and tx.get_calls == []


def test_mixed_public_private_rejected():
    # If ANY resolved address is non-global, reject — an attacker controlling DNS could make the
    # OS pick the bad one.
    tx = FakeTransport(resolver={"example.com": ["1.2.3.4", "192.168.1.1"]})
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "non-global" in res.error and tx.get_calls == []


def test_empty_resolution_rejected():
    tx = FakeTransport(resolver={"example.com": []})
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "no addresses" in res.error


def test_dns_rebind_pins_validated_ip():
    # Resolver returns a PUBLIC ip first; a second resolution would return a PRIVATE one. The
    # fetch must connect to the validated public IP and resolve EXACTLY once (no rebind window).
    state = {"n": 0}

    def rebind(host):
        state["n"] += 1
        return ["1.2.3.4"] if state["n"] == 1 else ["10.0.0.1"]

    tx = FakeTransport(resolver=rebind, responses=_resp(body=b"<p>ok</p>"))
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert res.ok
    assert len(tx.resolve_calls) == 1               # resolved once
    assert tx.get_calls[0][0] == "1.2.3.4"          # connected to the validated public IP


# --------------------------------------------------------------------------------------
# Manual redirect handling (each hop fully re-validated)
# --------------------------------------------------------------------------------------
def test_redirect_followed_within_cap():
    def responder(host, path):
        if path == "/a":
            return _resp(status=302, location="https://example.com/b")
        return _resp(body=b"<p>final</p>")

    tx = FakeTransport(responses=responder)
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert res.ok and res.final_url == "https://example.com/b"
    assert len(tx.get_calls) == 2


def test_redirect_to_off_allowlist_rejected():
    def responder(host, path):
        return _resp(status=302, location="https://evil.com/x")

    tx = FakeTransport(responses=responder)
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "allowlist" in res.error


def test_redirect_to_host_resolving_private_rejected():
    # Redirect re-runs the FULL validation, including IP checks, on the new hop.
    def responder(host, path):
        if host == "a.test":
            return _resp(status=302, location="https://b.test/x")
        return _resp(body=b"<p>nope</p>")

    tx = FakeTransport(
        resolver={"a.test": ["1.2.3.4"], "b.test": ["10.0.0.9"]},
        responses=responder,
    )
    res = fetch("https://a.test/a", allowed_domains=["a.test", "b.test"], transport=tx)
    assert not res.ok and "non-global" in res.error


def test_too_many_redirects_rejected():
    def loop(host, path):
        return _resp(status=302, location=f"https://example.com{path}x")

    tx = FakeTransport(responses=loop)
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx, max_redirects=3)
    assert not res.ok and "too many redirects" in res.error


def test_redirect_without_location_rejected():
    tx = FakeTransport(responses=_resp(status=302, location=None))
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "Location" in res.error


# --------------------------------------------------------------------------------------
# Size / content-type / status guards
# --------------------------------------------------------------------------------------
def test_oversize_rejected_not_truncated():
    tx = FakeTransport(responses=_resp(body=b"x" * 100))
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx, max_bytes=10)
    assert not res.ok and "exceeds" in res.error and res.text == ""  # nothing returned


def test_bad_content_type_rejected():
    tx = FakeTransport(responses=_resp(ctype="application/pdf", body=b"%PDF-1.4"))
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "content-type" in res.error


def test_non_200_rejected():
    tx = FakeTransport(responses=_resp(status=404, body=b"nope"))
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "non-200" in res.error and res.status == 404


# --------------------------------------------------------------------------------------
# Transport failures are handled (the fetch is total — never raises)
# --------------------------------------------------------------------------------------
def test_dns_failure_handled():
    def boom(host):
        raise OSError("name resolution failed")

    tx = FakeTransport(resolver=boom)
    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=tx)
    assert not res.ok and "dns resolve failed" in res.error


def test_transport_error_handled():
    class Boom(FakeTransport):
        def get(self, *a, **k):
            raise OSError("connection reset")

    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=Boom())
    assert not res.ok and "fetch failed" in res.error


# --------------------------------------------------------------------------------------
# Totality: malformed input returns ok=False, never raises (Codex Phase-2 HOLD fixes)
# --------------------------------------------------------------------------------------
def test_malformed_url_unterminated_ipv6_handled():
    res = fetch("https://[::1", allowed_domains=ALLOW, transport=FakeTransport())
    assert not res.ok and "malformed URL" in res.error


def test_malformed_url_bad_port_handled():
    res = fetch("https://example.com:bad/a", allowed_domains=ALLOW, transport=FakeTransport())
    assert not res.ok and "malformed URL" in res.error


def test_malformed_url_out_of_range_port_handled():
    res = fetch("https://example.com:999999/a", allowed_domains=ALLOW, transport=FakeTransport())
    assert not res.ok and "malformed URL" in res.error


def test_malformed_redirect_location_handled():
    # A malicious/broken ALLOWLISTED source returns a redirect to a URL urljoin can't parse.
    def responder(host, path):
        return _resp(status=302, location="https://[::1")

    res = fetch("https://example.com/a", allowed_domains=ALLOW, transport=FakeTransport(responses=responder))
    assert not res.ok and "malformed redirect Location" in res.error


def test_none_allowlist_handled():
    res = fetch("https://example.com/a", allowed_domains=None, transport=FakeTransport())
    assert not res.ok and "invalid allowlist" in res.error


def test_non_str_allowlist_entries_dropped_not_crashed():
    # [None] must not crash; the None is dropped, so example.com is simply not allowlisted.
    res = fetch("https://example.com/a", allowed_domains=[None], transport=FakeTransport())
    assert not res.ok and "allowlist" in res.error


# --------------------------------------------------------------------------------------
# Structural + functional guarantees: no urllib.request (would re-resolve + honor proxies),
# GET-only, no auth/cookie headers, IP pinned with real host for SNI/cert.
# --------------------------------------------------------------------------------------
def test_does_not_import_urllib_request():
    # urllib.request.urlopen re-resolves DNS and honors $HTTP(S)_PROXY — both unacceptable. We
    # use http.client directly, which consults no proxy env. (Asserted on real imports, via AST,
    # so the module's explanatory prose can't trip it.)
    tree = ast.parse(Path(web_fetch.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any(m.startswith("urllib.request") for m in imported)
    assert "urllib.parse" in imported  # we DO use urlsplit/urljoin


def test_real_transport_get_only_pins_ip_no_auth(monkeypatch):
    # Verify _RealTransport.get functionally (no network): GET-only, connects to the PINNED IP
    # while keeping the real host (for SNI/cert), and sends no Authorization/Cookie header.
    captured: dict = {}

    class FakeConn:
        def __init__(self, host, pinned_ip, **kw):
            captured["host"] = host
            captured["ip"] = pinned_ip

        def request(self, method, path, headers=None):
            captured["method"] = method
            captured["path"] = path
            captured["headers"] = headers or {}

        def getresponse(self):
            class _R:
                status = 200

                def getheaders(self):
                    return [("Content-Type", "text/plain")]

                def read(self, n):
                    return b"ok"

            return _R()

        def close(self):
            pass

    monkeypatch.setattr(web_fetch, "_PinnedHTTPSConnection", FakeConn)
    resp = web_fetch._RealTransport().get("1.2.3.4", "example.com", 443, "/a", 1000)
    assert resp.status == 200
    assert captured["method"] == "GET"
    assert captured["ip"] == "1.2.3.4" and captured["host"] == "example.com"
    lower = {k.lower() for k in captured["headers"]}
    assert "authorization" not in lower and "cookie" not in lower
