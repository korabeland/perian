#!/usr/bin/env python3
"""Bounded, SSRF-hardened web-fetch primitive — the ONLY think-loop component that ever opens
an EXTERNAL network connection (gap #5).

WHY THIS EXISTS (the architecture decision):
The think-loop research stage must NOT have network access (a prompt-injected model with an
egress hole can exfiltrate). So the deterministic sweep fetches; the model only reasons over the
frozen result. This module is that fetch step. It is a TOOL, not a model surface: it is total
(returns a FetchResult, never raises), GET-only, and every destination is checked against a
caller-supplied allowlist BEFORE any socket is opened.

THE SSRF DEFENSE (why this is not a urllib one-liner):
`urllib.request.urlopen` re-resolves DNS at connect time (so a TOCTOU/DNS-rebind can point a
validated hostname at an internal IP), auto-follows redirects (a 302 to http://169.254.169.254/
slips past a one-shot check), and honors $HTTP(S)_PROXY. None of that is acceptable here. Instead:

  1. Resolve the hostname ONCE. Reject unless EVERY resolved address is globally routable
     (ipaddress.is_global — rejects loopback / private / link-local / ULA / reserved / CGNAT).
  2. Connect to the PINNED resolved IP, but set TLS server_hostname + the Host header to the
     ORIGINAL hostname (SNI + certificate verification against the real name). There is no second
     resolution between validate and connect, so a rebind cannot occur.
  3. Follow redirects MANUALLY (cap MAX_REDIRECTS): every hop re-parses, re-allowlist-checks,
     re-resolves, and re-validates from scratch.
  4. Use http.client directly (NOT urllib) so NO proxy env var is ever consulted.

The DNS resolver and the TLS GET are behind an injectable Transport (mirrors think_sweep.SpawnFn),
so the orchestration above — allowlist, IP validation, rebind-pinning, redirect re-checks, size /
content-type limits — is unit-tested with a FakeTransport and NO real network. The real Transport's
socket-level pinning is exercised by the Phase-4 live test.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Protocol
from urllib.parse import urljoin, urlsplit

# Limits — deliberately tight (a curated allowlist of text sources needs no more).
MAX_BYTES = 2_000_000          # reject (never truncate) bodies larger than this
MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 10.0         # seconds, per hop
ALLOWED_CONTENT_TYPES = ("text/plain", "text/html")
DEFAULT_PORT = 443             # HTTPS only; an explicit non-443 port is rejected
USER_AGENT = "hermes-atb-fetch/1.0 (+sweep-owned; GET-only)"
_REDIRECT_STATUS = (301, 302, 303, 307, 308)


@dataclass(frozen=True)
class FetchResult:
    """The outcome of one fetch. Total: ok=False carries the reason, never an exception.

    text          : extracted text (HTML->text, or text/plain verbatim) — what the sweep freezes.
    raw_bytes     : length of the RAW response body (for the fetch event's provenance).
    final_url     : the URL actually fetched (after any redirects).
    content_type  : the response's parsed content-type (no params).
    """

    ok: bool
    url: str
    final_url: str = ""
    status: int = 0
    text: str = ""
    raw_bytes: int = 0
    content_type: str = ""
    error: str = ""


@dataclass(frozen=True)
class _Response:
    """A raw HTTP response from the transport (one hop)."""

    status: int
    headers: dict          # lowercased header name -> value
    body: bytes


class Transport(Protocol):
    """The injectable network seam. The real implementation does DNS + an IP-pinned TLS GET;
    tests inject a fake that resolves to chosen IPs and returns canned responses, so the SSRF
    orchestration is exercised with zero real network."""

    def resolve(self, host: str) -> list[str]:
        """Return the resolved IP strings for host (raise on failure — fetch() catches)."""
        ...

    def get(self, ip: str, host: str, port: int, path: str, max_bytes: int) -> _Response:
        """IP-PINNED HTTPS GET: TCP-connect to `ip`, but TLS-verify + Host against `host`.
        Read at most max_bytes+1 bytes (so the caller can detect oversize). Raise on transport
        error (fetch() catches and returns ok=False)."""
        ...


# --------------------------------------------------------------------------------------
# HTML -> text (stdlib only; skips script/style/head so injected markup can't smuggle text)
# --------------------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            s = data.strip()
            if s:
                self._chunks.append(s)

    def text(self) -> str:
        return "\n".join(self._chunks)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - a malformed page must never crash the sweep
        pass
    return parser.text()


def _decode(body: bytes, content_type: str) -> str:
    """Decode response bytes using the content-type charset if present, else utf-8 (replace)."""
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"') or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return body.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------------------
# Real transport — IP-pinned HTTPS (the SSRF-safe connection)
# --------------------------------------------------------------------------------------
class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a PINNED IP while doing TLS SNI + cert verification against the original
    hostname. This is the standard SSRF-safe pin: TCP goes to the validated IP, the certificate
    is still checked against the hostname, and there is no second DNS resolution."""

    def __init__(self, host: str, pinned_ip: str, **kw):
        super().__init__(host, **kw)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # self.host is the original hostname -> SNI + cert verification use the real name.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _RealTransport:
    def resolve(self, host: str) -> list[str]:
        # getaddrinfo returns (family, type, proto, canonname, sockaddr); sockaddr[0] is the IP.
        infos = socket.getaddrinfo(host, DEFAULT_PORT, proto=socket.IPPROTO_TCP)
        return [info[4][0] for info in infos]

    def get(self, ip: str, host: str, port: int, path: str, max_bytes: int) -> _Response:
        ctx = ssl.create_default_context()
        conn = _PinnedHTTPSConnection(host, ip, port=port, timeout=CONNECT_TIMEOUT, context=ctx)
        try:
            # Fixed header set: GET-only, no Authorization, no Cookie. Connection:close so the
            # socket is not pooled. (http.client sets Host from `host` automatically.)
            conn.request(
                "GET",
                path or "/",
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html, text/plain",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            body = resp.read(max_bytes + 1)
            return _Response(status=resp.status, headers=headers, body=body)
        finally:
            conn.close()


_DEFAULT_TRANSPORT = _RealTransport()


# --------------------------------------------------------------------------------------
# Validation helpers (pure; the SSRF logic lives here, above the transport)
# --------------------------------------------------------------------------------------
def _parse_target(url: str, allowed: set[str]) -> tuple[str | None, str, str]:
    """Validate scheme/host/port/userinfo against the (pre-normalized) allowlist set.

    Returns (error_or_None, host, path). On any rejection error is a string and host/path are "".
    TOTAL: urlsplit / parts.port can raise ValueError on a malformed URL (`https://[::1`,
    `:bad`, `:999999`) — caught here so the sweep is never crashed by a bad URL or a malicious
    redirect Location (Codex Phase-2 totality HOLD)."""
    try:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return f"non-https scheme rejected: {parts.scheme!r}", "", ""
        if parts.username or parts.password:
            return "userinfo in URL rejected (auth/phishing vector)", "", ""
        host = (parts.hostname or "").lower()
        if not host:
            return "no host in URL", "", ""
        # Reject an IP-literal host outright: the allowlist is hostnames, and an attacker-supplied
        # IP host would otherwise skip the DNS+validate path.
        try:
            ipaddress.ip_address(host)
            return f"IP-literal host rejected: {host}", "", ""
        except ValueError:
            pass
        port = parts.port or DEFAULT_PORT  # parts.port raises ValueError on a bad/out-of-range port
        if port != DEFAULT_PORT:
            return f"non-443 port rejected: {port}", "", ""
        if host not in allowed:
            return f"domain not in allowlist: {host}", "", ""
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return None, host, path
    except ValueError as exc:
        return f"malformed URL rejected: {exc}", "", ""


def _all_ips_global(ips: list[str]) -> str | None:
    """None if every IP is globally routable; else a rejection reason. Rejects if ANY resolved
    address is non-global (an attacker controlling DNS could otherwise have the OS pick the bad
    one). Also rejects an empty result and any unparseable address (fail closed)."""
    if not ips:
        return "host resolved to no addresses"
    for ip in ips:
        try:
            obj = ipaddress.ip_address(ip)
        except ValueError:
            return f"unparseable resolved address: {ip!r}"
        if not obj.is_global or obj.is_multicast:
            return f"resolved to non-global address: {ip}"
    return None


def _content_type(headers: dict) -> str:
    return headers.get("content-type", "").split(";")[0].strip().lower()


# --------------------------------------------------------------------------------------
# The public entry point
# --------------------------------------------------------------------------------------
def fetch(
    url: str,
    *,
    allowed_domains,
    transport: Transport | None = None,
    max_bytes: int = MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchResult:
    """Fetch one allowlisted HTTPS URL, SSRF-safe, GET-only, bounded. Total — never raises.

    allowed_domains : iterable of permitted hostnames (exact match, case-insensitive).
    transport       : injectable network seam (defaults to the real IP-pinned HTTPS transport).

    Returns a FetchResult. ok=True only when the final hop was an allowlisted, globally-routable,
    in-size, text/* response that was decoded (and HTML-stripped) successfully.
    """
    tx = transport or _DEFAULT_TRANSPORT
    # Normalize the allowlist ONCE, here (the totality boundary): a non-iterable allowlist or
    # non-str entries can't crash the sweep (Codex Phase-2 totality HOLD). Non-str entries are
    # dropped rather than coerced.
    try:
        allowed = {d.lower() for d in allowed_domains if isinstance(d, str)}
    except TypeError as exc:
        return FetchResult(ok=False, url=url, error=f"invalid allowlist: {exc}")
    current = url
    seen = 0
    while True:
        err, host, path = _parse_target(current, allowed)
        if err:
            return FetchResult(ok=False, url=url, final_url=current, error=err)
        try:
            ips = tx.resolve(host)
        except Exception as exc:  # noqa: BLE001 - resolution failure is a fetch failure, not a crash
            return FetchResult(ok=False, url=url, final_url=current, error=f"dns resolve failed: {exc}")
        ip_err = _all_ips_global(ips)
        if ip_err:
            return FetchResult(ok=False, url=url, final_url=current, error=ip_err)
        try:
            # Pin to the FIRST validated IP. resolve() was called exactly once this hop, so there
            # is no window for a rebind between validation and connection.
            resp = tx.get(ips[0], host, DEFAULT_PORT, path, max_bytes)
        except Exception as exc:  # noqa: BLE001 - transport/TLS error -> fetch failure
            return FetchResult(ok=False, url=url, final_url=current, error=f"fetch failed: {exc}")

        if resp.status in _REDIRECT_STATUS:
            location = resp.headers.get("location")
            if not location:
                return FetchResult(ok=False, url=url, final_url=current, status=resp.status,
                                   error="redirect without Location header")
            seen += 1
            if seen > max_redirects:
                return FetchResult(ok=False, url=url, final_url=current, status=resp.status,
                                   error=f"too many redirects (> {max_redirects})")
            # Resolve the next hop against the current URL, then loop -> full re-validation.
            # urljoin can raise on a malformed (attacker-controlled) Location -> stay total.
            try:
                current = urljoin(current, location)
            except ValueError as exc:
                return FetchResult(ok=False, url=url, final_url=current, status=resp.status,
                                   error=f"malformed redirect Location rejected: {exc}")
            continue

        if resp.status != 200:
            return FetchResult(ok=False, url=url, final_url=current, status=resp.status,
                               error=f"non-200 status: {resp.status}")
        if len(resp.body) > max_bytes:
            return FetchResult(ok=False, url=url, final_url=current, status=resp.status,
                               error=f"response exceeds {max_bytes} bytes (rejected, not truncated)")
        ctype = _content_type(resp.headers)
        if ctype not in ALLOWED_CONTENT_TYPES:
            return FetchResult(ok=False, url=url, final_url=current, status=resp.status,
                               content_type=ctype, error=f"disallowed content-type: {ctype!r}")
        decoded = _decode(resp.body, resp.headers.get("content-type", ""))
        text = _html_to_text(decoded) if ctype == "text/html" else decoded
        return FetchResult(
            ok=True,
            url=url,
            final_url=current,
            status=200,
            text=text,
            raw_bytes=len(resp.body),
            content_type=ctype,
        )
