#!/usr/bin/env python3
"""Deterministic THINK-side gates — the trust spine of the thinking loop.

These are the pure check functions for the think-loop sweep (Research -> Dreamer ->
flesh-out -> Main -> digest). They certify TRUST, never taste: a gate decides whether an
artifact may cross to the next stage based ONLY on what the sweep and the OS can witness
from outside the model — never on a field the model wrote about itself.

WHY THIS EXISTS (doer != certifier):
The local models control every byte INSIDE their JSON — including their own `collected_at`
and any "I passed" claim. So in-artifact fields are not trust signals. Only the unforgeable
layer is: filesystem mtime vs a sweep-recorded spawn time (M1), a content hash the sweep
computed and anchored (M2), the schema-known field set (M3), and a containment primitive
the build-side scope gate already trusts (M4). This module implements those four mechanisms
and the gates (T1/T1b/T2/T2h/T3) that lean on them.

PURITY CONTRACT (the reason these are testable without a model):
Every function here is SIDE-EFFECT-FREE. Gates READ artifacts/anchors/events and RETURN a
Verdict. They never write a file, append an event, snapshot evidence, or mutate state. All
of that — the M1 preclear, the T1.9 manifest append, the T1b.8 ledger write, the M2
snapshot-copy, event emission, the literal per-run write boundary (Codex v5 nit) — belongs
to the orchestrator (tools/think_sweep.py, Phase A2). Keeping the gates pure is what lets the
Phase A1 fixture suite plant a defect and assert the exact gate holds, with no model in the
loop. (Mirrors tools/scope_check.py: pure core + an I/O wrapper that lives elsewhere.)

REUSE (no re-implementation of trusted primitives):
- tools.scope_check._is_under — the component-wise containment check (defeats `<job>-evil`
  prefix tricks) — is reused verbatim for the M4 declared-path preflight. The build-side
  scope_check on the real diff at enqueue-time stays the BINDING path enforcer; M4 is only an
  early declaration sanity check.
- tools.validator.schema_loader.load_schema + jsonschema.Draft202012Validator — the same
  schema source and validator class the validator_gate cron uses.
- tools.council_gate.evaluate — the deterministic council-trigger (risk keywords / unbounded
  scope / thin evidence / unmeasurable), run by the sweep instead of by Main's SOUL prose.

NOTE — a verified plan-vs-reality gap (logged for the build summary): the plan calls for
"graduating build #4's check_sanitized"; that file (state/builds/20260603-idea-digest/
check_sanitized.py) is 0 bytes — there is no reusable sanitized-check to graduate. It is
implemented here directly (is_sanitized), which is where the gates need it anyway.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from tools.council_gate import evaluate as council_evaluate
from tools.scope_check import _is_under
from tools.validator.schema_loader import load_schema
from tools.web_fetch import ALLOWED_CONTENT_TYPES as FETCH_CONTENT_TYPES
from tools.web_fetch import MAX_BYTES as FETCH_MAX_BYTES

FIVE_MIN = timedelta(minutes=5)

# Sweep-emitted pass-event names that corroborate an anchor line (M2 write-protection).
T1_PASS = "t1-pass"
T1B_PASS = "t1b-pass"

VALID_LANES = ("new-thing", "self-improvement", "human")


# --------------------------------------------------------------------------------------
# Verdict — the uniform gate return. disposition is the only thing callers branch on.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Verdict:
    """A gate outcome.

    disposition:
      - "pass"  -> the stage may cross to the next gate.
      - "hold"  -> a TRUST failure (forgery, mutation, missing/stale write, injection,
                   path escape, schema break). Sweep emits [THINK-GATE-FAIL] + exits 2.
      - "park"  -> a RECOVERABLE stop (dedup collision, over-cap, council-requested,
                   needs_more exhausted). The idea waits; nothing is deleted.
    """

    disposition: str
    gate: str
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.disposition == "pass"

    @property
    def held(self) -> bool:
        return self.disposition == "hold"

    @property
    def parked(self) -> bool:
        return self.disposition == "park"


def _decide(gate: str, holds: list[str], parks: list[str]) -> Verdict:
    """A hold dominates a park (a trust failure outranks a recoverable stop)."""
    if holds:
        return Verdict("hold", gate, tuple(holds))
    if parks:
        return Verdict("park", gate, tuple(parks))
    return Verdict("pass", gate)


# --------------------------------------------------------------------------------------
# Small total helpers
# --------------------------------------------------------------------------------------
def _load_json(path: str):
    """Return (data, None) or (None, error_message). Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f"file not found: {path}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable/invalid JSON: {exc}"


def _parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 string (accepting a trailing Z) to an aware datetime, else None."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _within(value, lo: datetime, hi: datetime) -> bool:
    """True iff value parses and lo <= value <= hi. Unparseable -> False (fail closed)."""
    dt = _parse_iso(value)
    return dt is not None and lo <= dt <= hi


def _ordered(earlier, later) -> bool:
    """True iff both parse and earlier <= later. Unparseable -> False."""
    a, b = _parse_iso(earlier), _parse_iso(later)
    return a is not None and b is not None and a <= b


# --------------------------------------------------------------------------------------
# M3 — recursive canonicalize / unknown-key detection (kills field injection)
# --------------------------------------------------------------------------------------
def unknown_key_paths(data, schema, prefix: str = "") -> list[str]:
    """Dotted paths of keys in `data` absent from `schema.properties`, recursively.

    Recurses into nested object properties AND array `items` (so a smuggled
    `evidence[0].instructions_for_main` is caught, not just top-level extras). Our schemas
    omit `additionalProperties:false`, so jsonschema itself would ALLOW these extras — this
    is exactly why M3 exists: the gate rejects them structurally regardless of the schema's
    leniency. Leaves (scalars, or arrays of scalars) contribute nothing.
    """
    out: list[str] = []
    if isinstance(data, dict):
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in props:
                out.append(path)
            else:
                out.extend(unknown_key_paths(value, props[key], path))
    elif isinstance(data, list):
        items = schema.get("items", {}) if isinstance(schema, dict) else {}
        if isinstance(items, dict):
            for i, element in enumerate(data):
                out.extend(unknown_key_paths(element, items, f"{prefix}[{i}]"))
    return out


def canonicalize(data, schema):
    """Recursively project `data` to its schema-known fields (the M3 strip).

    The sweep (A2) calls this before embedding any artifact into a downstream prompt, so an
    unknown field can never reach a model. The gates call unknown_key_paths to HOLD on the
    same. Free-text VALUES are preserved verbatim (M3 does not make a malicious string inside
    a legit `claim` safe — that is mitigated downstream by delimited-untrusted-data framing).
    """
    if isinstance(data, dict) and isinstance(schema, dict):
        props = schema.get("properties", {})
        return {k: canonicalize(v, props[k]) for k, v in data.items() if k in props}
    if isinstance(data, list) and isinstance(schema, dict):
        items = schema.get("items", {})
        if isinstance(items, dict):
            return [canonicalize(el, items) for el in data]
    return data


def _schema_errors(data, artifact_name: str) -> list[str]:
    """jsonschema errors for `data` against the named schema (same validator as the cron)."""
    schema = load_schema(artifact_name)
    if schema is None:
        return [f"no schema on disk for artifact '{artifact_name}'"]
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    errs = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    out = []
    for e in errs:
        ref = ".".join(str(p) for p in e.absolute_path) or "(root)"
        out.append(f"{ref}: {e.message}")
    return out


# --------------------------------------------------------------------------------------
# M1 — spawn fence (filesystem freshness; the sweep can witness an mtime, a model cannot)
# --------------------------------------------------------------------------------------
def spawn_fence(path: str, spawn_start: float) -> Verdict:
    """Hold unless `path` exists AND its fs-mtime strictly post-dates `spawn_start`.

    The sweep preclears the canonical path before spawning, so present-after-spawn is the
    primary proof of a fresh write; mtime > spawn_start guards a same-path overwrite that did
    not actually happen (the F9 no-write / stale-valid-artifact mode). `spawn_start` is a
    sweep-recorded epoch (time.time() before the subprocess) — never an in-JSON timestamp.
    """
    if not os.path.exists(path):
        return _decide("M1", [f"artifact absent after spawn (no write): {path}"], [])
    mtime = os.stat(path).st_mtime
    if mtime <= spawn_start:
        return _decide(
            "M1",
            [f"artifact mtime {mtime:.6f} <= spawn_start {spawn_start:.6f} (stale, not "
             f"freshly written): {path}"],
            [],
        )
    return Verdict("pass", "M1")


# --------------------------------------------------------------------------------------
# M2 — sweep-owned hash anchors (provenance + immutability + anti-forgery)
# --------------------------------------------------------------------------------------
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path: str) -> list[dict]:
    """Parse a JSONL anchor store (manifest or ledger). Missing file -> []. Skips blanks."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def find_anchor(anchors: list[dict], packet_path: str) -> dict | None:
    """The anchor line whose `path` matches `packet_path` (component-normalized), else None."""
    target = os.path.normpath(packet_path)
    for a in anchors:
        if os.path.normpath(a.get("path", "")) == target:
            return a
    return None


def pass_event_exists(events: list[dict], anchor: dict, event_names) -> bool:
    """True iff some event corroborates the anchor by (event-name, path, sha256).

    This is the write-protection check: a forged anchor line that no T1/T1b pass-event in
    events.jsonl backs is rejected, so a hand-placed packet cannot be laundered past T2.2.
    """
    apath = os.path.normpath(anchor.get("path", ""))
    asha = anchor.get("sha256")
    names = set(event_names)
    for ev in events:
        if (
            ev.get("event") in names
            and ev.get("sha256") == asha
            and os.path.normpath(ev.get("path", "")) == apath
        ):
            return True
    return False


@dataclass(frozen=True)
class ResolvedRef:
    """One evidence reference, resolved by the sweep to what the gate must verify.

    snapshot_path : the track-local copy the contract now cites (M2 snapshot).
    anchor        : the sweep-owned anchor line for the SOURCE packet (manifest or ledger),
                    or None if the cited packet has no anchor (never gated / ghost).
    event_names   : which pass-event kind(s) corroborate this anchor — {T1_PASS} for
                    pool/seed packets (evidence-manifest), {T1B_PASS} for flesh-out (ledger).
    label         : the original ref string (for readable hold reasons).
    """

    snapshot_path: str
    anchor: dict | None
    event_names: tuple[str, ...]
    label: str = ""


def verify_ref(ref: ResolvedRef, events: list[dict]) -> str | None:
    """Return a hold reason if this reference fails M2, else None.

    Order matters — each branch maps to exactly one planted defect:
      anchor is None        -> cited packet never gated / ghost ref.
      snapshot missing       -> ghost ref (the file the contract cites is gone).
      hash != anchor.sha256  -> snapshot mutated after its gate.
      no corroborating event -> forged anchor line (no T1/T1b pass-event backs it).
    """
    name = ref.label or ref.snapshot_path
    if ref.anchor is None:
        return f"evidence ref has no sweep-owned anchor (never gated): {name}"
    if not os.path.exists(ref.snapshot_path):
        return f"evidence snapshot missing (ghost ref): {name}"
    if sha256_file(ref.snapshot_path) != ref.anchor.get("sha256"):
        return f"evidence snapshot hash != anchor (mutated post-gate): {name}"
    if not pass_event_exists(events, ref.anchor, ref.event_names):
        return f"anchor line has no corroborating pass-event (forged): {name}"
    return None


def verify_refs(refs: list[ResolvedRef], events: list[dict]) -> list[str]:
    """All M2 hold reasons across a set of references (empty list == all sound)."""
    return [r for r in (verify_ref(ref, events) for ref in refs) if r]


# --------------------------------------------------------------------------------------
# Fetch gate (gap #5) — certifies the sweep's web-fetch step + supplies the citeable-URL set
# --------------------------------------------------------------------------------------
# A `fetch` ANCHOR (one line in the sweep-owned fetch manifest) describes one frozen artifact:
#   {"path": "<frozen .txt>", "sha256": "<hash>", "url": "<requested url>",
#    "domain": "<host>", "bytes": <raw body len>, "content_type": "text/html"}
# It is corroborated by a sweep-written `fetch` EVENT in events.jsonl carrying the same
# (path, sha256) — the model is fs-jailed out of events.jsonl, so the event is the unforgeable
# half (mirrors pass_event_exists for T1/T1b anchors).
def _domain_in_allowlist(domain, allowed_domains) -> bool:
    if not isinstance(domain, str):
        return False
    try:
        return domain.lower() in {str(d).lower() for d in (allowed_domains or [])}
    except TypeError:  # allowed_domains not iterable -> not allowlisted (fail closed, never raise)
        return False


def _fetch_host(url) -> str | None:
    """The lowercased host of a fetch URL, or None if it does not parse to one. Total."""
    if not isinstance(url, str) or not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _fetch_event_corroborates(events: list[dict], anchor: dict) -> bool:
    """True iff a sweep-written `fetch` event binds the anchor's security-relevant fields
    (path, sha256, url, content_type, bytes). Unlike pass_event_exists this ALSO binds `url` and
    the rest, because for a fetch anchor the URL/content-type are security-relevant: a hand-placed
    anchor reusing a real (path, sha256) event must not be able to swap the URL or content-type.
    events.jsonl is sweep-written (the model is fs-jailed out of it), so a matching event is the
    unforgeable half. Total: a non-dict anchor / non-dict events / non-string paths all return
    False rather than raising (so the helper is safe even called directly, not only via the
    dict-guarded _fetch_anchor_hold path)."""
    if not isinstance(anchor, dict) or not isinstance(events, (list, tuple)):
        return False
    apath = anchor.get("path")
    if not isinstance(apath, str):
        return False
    napath = os.path.normpath(apath)
    for ev in events:
        if not isinstance(ev, dict) or ev.get("event") != "fetch":
            continue
        epath = ev.get("path")
        if not isinstance(epath, str) or os.path.normpath(epath) != napath:
            continue
        if (
            ev.get("sha256") == anchor.get("sha256")
            and ev.get("url") == anchor.get("url")
            and ev.get("content_type") == anchor.get("content_type")
            and ev.get("bytes") == anchor.get("bytes")
        ):
            return True
    return False


def _fetch_anchor_hold(
    anchor, events: list[dict], allowed_domains, max_bytes: int, allowed_content_types
) -> str | None:
    """Return a hold reason if a fetch anchor fails integrity, else None. TOTAL — never raises.

    Order maps each branch to one failure: anchor is a dict with a real file path; current sha ==
    anchor sha (not mutated after the freeze); the URL's HOST (derived from the URL itself, NOT a
    separately-claimed `domain` field — Codex Phase-3 HIGH) is in the COMMITTED allowlist, and any
    declared `domain` agrees with it; bytes/content-type within the SAME bounds web_fetch enforced
    (defense-in-depth — the gate re-checks, it does not trust the fetcher); and a corroborating
    sweep-written `fetch` event that binds the URL + content-type, not just (path, sha256)."""
    if not isinstance(anchor, dict):
        return f"fetch anchor is not an object: {anchor!r}"
    # Total even on direct misuse: coerce malformed optional bounds to their defaults so the
    # comparisons below (nbytes > max_bytes; ctype in allowed_content_types) never raise.
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        max_bytes = FETCH_MAX_BYTES
    if not isinstance(allowed_content_types, (list, tuple, set, frozenset)):
        allowed_content_types = FETCH_CONTENT_TYPES
    path = anchor.get("path")
    if not isinstance(path, str) or not path:
        return f"fetch anchor missing path: {path!r}"
    if not os.path.isfile(path):  # isfile, not exists: a directory must HOLD, not raise on open
        return f"fetch artifact missing: {path}"
    try:
        actual = sha256_file(path)
    except OSError as exc:
        return f"fetch artifact unreadable: {path} ({exc})"
    if actual != anchor.get("sha256"):
        return f"fetch artifact hash != anchor (mutated post-freeze): {path}"
    # The URL is the security-relevant field. Derive its host and allowlist-check THAT (never a
    # separately-claimed domain field); require any declared domain to agree (internal consistency).
    host = _fetch_host(anchor.get("url"))
    if host is None:
        return f"fetch anchor URL has no parseable host: {anchor.get('url')!r}"
    if not _domain_in_allowlist(host, allowed_domains):
        return f"fetch URL host not in committed allowlist: {host!r}"
    declared = anchor.get("domain")
    if declared is not None and str(declared).lower() != host:
        return f"fetch anchor domain {declared!r} != URL host {host!r}"
    nbytes = anchor.get("bytes")
    if not isinstance(nbytes, int) or isinstance(nbytes, bool) or nbytes < 0 or nbytes > max_bytes:
        return f"fetch bytes out of bounds: {nbytes!r}"
    ctype = anchor.get("content_type")
    if not (isinstance(ctype, str) and ctype.lower() in allowed_content_types):
        return f"fetch content-type not allowed: {ctype!r}"
    if not _fetch_event_corroborates(events, anchor):
        return f"fetch anchor has no corroborating fetch event (path, sha256, url, type): {path}"
    return None


def gate_fetch(
    fetch_anchors: list[dict],
    *,
    events: list[dict],
    allowed_domains,
    max_bytes: int = FETCH_MAX_BYTES,
    allowed_content_types=FETCH_CONTENT_TYPES,
) -> Verdict:
    """Certify the sweep's fetch step: every frozen artifact is intact, allowlisted, in-bounds,
    and corroborated. Pure (reads only). A hold == a real trust failure (mutation / off-allowlist
    / forged anchor). An empty anchor list passes (a cycle may legitimately fetch nothing). TOTAL:
    a non-list anchors/events argument is treated as empty rather than raising."""
    anchors = fetch_anchors if isinstance(fetch_anchors, (list, tuple)) else []
    evs = events if isinstance(events, (list, tuple)) else []
    holds = [
        r
        for r in (
            _fetch_anchor_hold(a, evs, allowed_domains, max_bytes, allowed_content_types)
            for a in anchors
        )
        if r
    ]
    return _decide("fetch", holds, [])


def fetched_urls(
    fetch_anchors: list[dict],
    events: list[dict],
    allowed_domains,
    *,
    max_bytes: int = FETCH_MAX_BYTES,
    allowed_content_types=FETCH_CONTENT_TYPES,
) -> set[str]:
    """The set of URLs whose fetch anchor passes FULL integrity — the ONLY URLs a research packet
    may cite under the fetched regime (the T1/T1b URL-ref binding). Derived from sweep-owned
    anchors + events, so the model cannot forge membership. TOTAL: a non-list anchors/events
    argument is treated as empty rather than raising."""
    out: set[str] = set()
    anchors = fetch_anchors if isinstance(fetch_anchors, (list, tuple)) else []
    evs = events if isinstance(events, (list, tuple)) else []
    for a in anchors:
        if (
            _fetch_anchor_hold(a, evs, allowed_domains, max_bytes, allowed_content_types)
            is None
        ):
            url = a.get("url")
            if isinstance(url, str) and url:
                out.add(url)
    return out


# --------------------------------------------------------------------------------------
# M4 — declared-path preflight (reuses ONLY scope_check._is_under)
# --------------------------------------------------------------------------------------
def declared_path_under_build(entry: str, job_id: str, builds_root: str) -> bool:
    """True iff a product-plan allowed_path declaration stays under state/builds/<job_id>/.

    No symlink resolution (the build dir does not exist at declaration time — Path.resolve
    would be meaningless and non-total); normpath only, matching scope_check's philosophy.
    Component-wise containment via _is_under defeats the `<job-id>-evil` prefix trick; a `..`
    escape or an absolute path elsewhere normalizes out from under base and is rejected.
    """
    base = os.path.normpath(os.path.join(builds_root, job_id))
    if not isinstance(entry, str) or not entry.strip():
        return False
    if os.path.isabs(entry):
        cand = os.path.normpath(entry)
    else:
        cand = os.path.normpath(os.path.join(base, entry))
    return _is_under(cand, base)


def declared_paths_ok(allowed_paths, job_id: str, builds_root: str):
    """(ok, violations) for a product-plan's allowed_paths. Mirrors scope_check.files_in_scope."""
    entries = [a for a in (allowed_paths or []) if isinstance(a, str) and a.strip()]
    violations = [e for e in entries if not declared_path_under_build(e, job_id, builds_root)]
    return (not violations and bool(entries)), violations


# --------------------------------------------------------------------------------------
# Content predicates reused across gates
# --------------------------------------------------------------------------------------
def is_sanitized(packet) -> bool:
    """True iff the packet declares sanitized=true (fabricated/demo data). Absent/false -> False."""
    return isinstance(packet, dict) and packet.get("sanitized") is True


def _evidence_ref_holds(
    packet: dict, prefix: str, evidence_base: str | None, fetch_refs: set[str] | None = None
) -> list[str]:
    """Verify a packet's evidence references.

    ALWAYS (both regimes): every sources[].ref and evidence[].source_ref must be a non-empty
    string.

    UNDER THE FETCHED-EVIDENCE REGIME (evidence_base is set — gap #5):
      - BINDING: every evidence[].source_ref must equal some declared sources[].ref in the
        SAME packet (a claim cannot cite a source the packet never declared). This closes the
        gap that nothing bound evidence to its sources (Codex finding 3).
      - CONTAINMENT: a LOCAL (non-URL) ref must resolve INSIDE evidence_base (via the reused
        scope_check._is_under) AND exist. The old os.path.exists check let an ABSOLUTE path
        like /etc/passwd pass (Codex finding 2); component-wise containment rejects absolute
        escapes and ../ traversal, then existence is checked.
      - URL BINDING (Phase 3): when `fetch_refs` is supplied, a URL ref must be a member — i.e.
        a URL the fetch step actually fetched, allowlisted, and hash-matched (the
        gate_fetch-certified set). A cited URL that was never fetched HOLDs, closing the
        URL-ref bypass. When `fetch_refs` is None (Phase 1 / no-fetch caller) URL refs pass
        unbound; the binding turns on with the fetch path (Phase 4).

    When evidence_base is None (no fetch context — today's live scan / flesh-out, and any
    non-fetch caller) ONLY the non-empty floor applies, so free-form URL / name / path refs
    stay valid. The stricter regime turns on together with the fetch path (Phase 4), never
    before — so wiring this in does not change live behavior on its own.
    """
    holds: list[str] = []
    fetched = bool(evidence_base)

    declared: set[str] = set()
    sources = packet.get("sources")
    if isinstance(sources, list):
        for i, s in enumerate(sources):
            if isinstance(s, dict):
                ref = s.get("ref")
                if not isinstance(ref, str) or not ref.strip():
                    holds.append(f"{prefix} empty source ref at sources[{i}].ref")
                    continue
                declared.add(ref)
                if fetched:
                    holds += _ref_regime_holds(
                        ref, f"sources[{i}].ref", prefix, evidence_base, fetch_refs
                    )

    evidence = packet.get("evidence")
    if isinstance(evidence, list):
        for i, e in enumerate(evidence):
            if isinstance(e, dict):
                ref = e.get("source_ref")
                if not isinstance(ref, str) or not ref.strip():
                    holds.append(f"{prefix} empty source ref at evidence[{i}].source_ref")
                    continue
                if fetched:
                    if ref not in declared:
                        holds.append(
                            f"{prefix} evidence[{i}].source_ref not declared in sources[]: {ref!r}"
                        )
                    holds += _ref_regime_holds(
                        ref, f"evidence[{i}].source_ref", prefix, evidence_base, fetch_refs
                    )
    return holds


def _ref_regime_holds(
    ref: str, where: str, prefix: str, evidence_base: str, fetch_refs: set[str] | None
) -> list[str]:
    """Per-ref check under the fetched-evidence regime: a URL ref must bind to a fetched +
    allowlisted + hash-matched artifact (membership in fetch_refs); a LOCAL ref must be
    contained in evidence_base and present. fetch_refs=None leaves URL refs unbound (Phase 1)."""
    if _looks_like_url(ref):
        if fetch_refs is not None and ref not in fetch_refs:
            return [f"{prefix} URL ref not among fetched, allowlisted sources at {where}: {ref}"]
        return []
    return _local_ref_containment(ref, where, prefix, evidence_base)


def _looks_like_url(ref: str) -> bool:
    return ref.startswith(("http://", "https://"))


def _local_ref_containment(ref: str, where: str, prefix: str, evidence_base: str) -> list[str]:
    """A LOCAL (non-URL) ref under the fetched-evidence regime must be CONTAINED in
    evidence_base and present. URL refs are skipped here — they are bound to the fetch
    manifest by the caller. Reuses scope_check._is_under (component-wise), so an absolute
    `/etc/passwd` or a `../` escape is rejected, not merely a missing file (Codex finding 2)."""
    if _looks_like_url(ref):
        return []
    cand = ref if os.path.isabs(ref) else os.path.join(evidence_base, ref)
    cand = os.path.normpath(cand)
    base = os.path.normpath(evidence_base)
    if not _is_under(cand, base):
        return [f"{prefix} local ref escapes evidence_base at {where}: {ref}"]
    if not os.path.exists(cand):
        return [f"{prefix} local ref does not exist at {where}: {ref}"]
    return []


def fingerprint(text) -> str:
    """Normalized dedup fingerprint: lowercase, alphanumeric tokens, first 12 joined."""
    if not isinstance(text, str):
        return ""
    tokens = "".join(c if c.isalnum() else " " for c in text.lower()).split()
    return " ".join(tokens[:12])


# --------------------------------------------------------------------------------------
# Lane sidecar (T2.8) — modeled as a value object the sweep/fixture populates
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LaneInfo:
    lane: str
    sidecar_mtime: float
    contract_mtime: float  # the contract's M1 spawn-fence mtime
    track_dir_name: str


def _lane_holds(lane_info: LaneInfo, contract_job_id, allow_human: bool) -> list[str]:
    holds: list[str] = []
    allowed = VALID_LANES if allow_human else ("new-thing", "self-improvement")
    if lane_info.lane not in allowed:
        holds.append(f"T2.8 illegal lane: {lane_info.lane!r} (allowed {allowed})")
    if lane_info.sidecar_mtime > lane_info.contract_mtime:
        holds.append("T2.8 lane sidecar mutated after proposal (mtime > contract mtime)")
    if contract_job_id != lane_info.track_dir_name:
        holds.append(
            f"T2.8 job_id/track mismatch: {contract_job_id!r} != dir {lane_info.track_dir_name!r}"
        )
    return holds


# --------------------------------------------------------------------------------------
# T1 — pool gate (per broad-scan packet)
# --------------------------------------------------------------------------------------
def gate_t1(
    packet_path: str,
    *,
    topic: str,
    cycle_start: datetime,
    now: datetime,
    spawn_start: float,
    evidence_base: str | None = None,
    fetch_refs: set[str] | None = None,
) -> Verdict:
    holds: list[str] = []
    fence = spawn_fence(packet_path, spawn_start)  # T1.1
    if not fence.ok:
        return _decide("T1", [f"T1.1 {r}" for r in fence.reasons], [])
    data, err = _load_json(packet_path)
    if err:
        return _decide("T1", [f"T1.2 {err}"], [])

    holds += [f"T1.2 schema: {e}" for e in _schema_errors(data, "research-input")]
    holds += [f"T1.8 unknown key (M3): {p}"
              for p in unknown_key_paths(data, load_schema("research-input"))]
    if not _within(data.get("collected_at"), cycle_start - FIVE_MIN, now + FIVE_MIN):
        holds.append(f"T1.3 collected_at out of window: {data.get('collected_at')!r}")
    if is_sanitized(data):
        holds.append("T1.4 sanitized=true")
    ev = data.get("evidence")
    if not (isinstance(ev, list) and len(ev) >= 1):
        holds.append("T1.6 evidence[] empty")
    holds += _evidence_ref_holds(data, "T1.5", evidence_base, fetch_refs)
    if data.get("topic") != topic:
        holds.append(f"T1.7 topic drift: {data.get('topic')!r} != assigned {topic!r}")
    return _decide("T1", holds, [])


# --------------------------------------------------------------------------------------
# T1b — flesh-out gate (per fresh idea)
# --------------------------------------------------------------------------------------
def gate_t1b(
    packet_path: str,
    *,
    job_id: str,
    proposed_at: str,
    now: datetime,
    spawn_start: float,
    evidence_base: str | None = None,
    fetch_refs: set[str] | None = None,
) -> Verdict:
    holds: list[str] = []
    fence = spawn_fence(packet_path, spawn_start)  # T1b.1
    if not fence.ok:
        return _decide("T1b", [f"T1b.1 {r}" for r in fence.reasons], [])
    data, err = _load_json(packet_path)
    if err:
        return _decide("T1b", [f"T1b.2 {err}"], [])

    holds += [f"T1b.2 schema: {e}" for e in _schema_errors(data, "research-input")]
    holds += [f"T1b.7 unknown key (M3): {p}"
              for p in unknown_key_paths(data, load_schema("research-input"))]
    # T1b.3 proposed_at <= collected_at <= now + 5min (secondary ordering)
    p_dt, c_dt = _parse_iso(proposed_at), _parse_iso(data.get("collected_at"))
    if p_dt is None or c_dt is None or not (p_dt <= c_dt <= now + FIVE_MIN):
        holds.append(
            f"T1b.3 ordering: proposed_at {proposed_at!r} <= collected_at "
            f"{data.get('collected_at')!r} <= now+5m violated (backdated/forward-dated flesh-out)"
        )
    if data.get("topic") != job_id:
        holds.append(f"T1b.4 topic must name job_id: {data.get('topic')!r} != {job_id!r}")
    if is_sanitized(data):
        holds.append("T1b.5 sanitized=true")
    ev = data.get("evidence")
    if not (isinstance(ev, list) and len(ev) >= 1):
        holds.append("T1b.6 evidence[] empty")
    holds += _evidence_ref_holds(data, "T1b.6", evidence_base, fetch_refs)
    return _decide("T1b", holds, [])


# --------------------------------------------------------------------------------------
# T2 — idea gate (per Dreamer contract; non-human lanes)
# --------------------------------------------------------------------------------------
def gate_t2(
    contract_path: str,
    *,
    resolved_refs: list[ResolvedRef],
    events: list[dict],
    now: datetime,
    spawn_start: float,
    seen_fingerprints,
    open_count: int,
    lane_count: int,
    lane_info: LaneInfo,
) -> Verdict:
    holds: list[str] = []
    parks: list[str] = []
    fence = spawn_fence(contract_path, spawn_start)  # T2.1
    if not fence.ok:
        return _decide("T2", [f"T2.1 {r}" for r in fence.reasons], [])
    contract, err = _load_json(contract_path)
    if err:
        return _decide("T2", [f"T2.1 {err}"], [])

    # T2.1 schema + M3
    holds += [f"T2.1 schema: {e}" for e in _schema_errors(contract, "idea-contract")]
    holds += [f"T2.1 unknown key (M3): {p}"
              for p in unknown_key_paths(contract, load_schema("idea-contract"))]
    # T2.2 every ref resolves to a sweep-owned anchor + immutable snapshot (pool packets)
    holds += [f"T2.2 {r}" for r in verify_refs(resolved_refs, events)]
    # T2.3 each referenced packet collected_at <= proposed_at <= now + 5min
    proposed_at = contract.get("proposed_at")
    for ref in resolved_refs:
        c_at = _snapshot_collected_at(ref.snapshot_path)
        if c_at is not None and not _ordered(c_at, proposed_at):
            holds.append(f"T2.3 ref collected_at {c_at!r} after proposed_at {proposed_at!r}")
    proposed_dt = _parse_iso(proposed_at)
    if proposed_dt is None:
        holds.append(f"T2.3 proposed_at unparseable: {proposed_at!r}")
    elif proposed_dt > now + FIVE_MIN:
        holds.append(f"T2.3 proposed_at in the future: {proposed_at!r}")
    # T2.7 slop floor: out_of_scope and how_verifiable non-empty after strip
    holds += _slop_floor_holds(contract)
    # T2.8 lane sidecar
    holds += _lane_holds(lane_info, contract.get("job_id"), allow_human=False)
    # T2.4 dedup -> PARK
    fp = fingerprint(contract.get("what_should_exist"))
    if fp and fp in set(seen_fingerprints):
        parks.append(f"T2.4 duplicate idea (fingerprint already seen): {fp!r}")
    # T2.5 caps -> PARK
    if lane_count > 1:
        parks.append(f"T2.5 over lane cap ({lane_count} contracts this lane this cycle, max 1)")
    if open_count > 5:
        parks.append(f"T2.5 over open cap ({open_count} open un-reviewed, max 5)")
    # T2.6 council -> PARK (council-requested)
    reasons = council_evaluate(contract)
    if reasons:
        parks.append("T2.6 council-requested: " + "; ".join(reasons))
    return _decide("T2", holds, parks)


def _snapshot_collected_at(path: str):
    data, err = _load_json(path)
    if err or not isinstance(data, dict):
        return None
    return data.get("collected_at")


def _slop_floor_holds(contract: dict) -> list[str]:
    holds: list[str] = []
    oos = contract.get("out_of_scope")
    if not (isinstance(oos, list) and any(isinstance(s, str) and s.strip() for s in oos)):
        holds.append("T2.7 out_of_scope empty after strip")
    hv = contract.get("how_verifiable")
    if not (isinstance(hv, str) and hv.strip()):
        holds.append("T2.7 how_verifiable empty after strip")
    return holds


# --------------------------------------------------------------------------------------
# T2h — human-lane split gate
# --------------------------------------------------------------------------------------
def gate_t2h_pre(
    contract_path: str,
    seed_path: str,
    *,
    seed_ref: ResolvedRef,
    events: list[dict],
    now: datetime,
    contract_spawn_start: float,
    seed_spawn_start: float,
    seen_fingerprints,
    lane_info: LaneInfo,
    cycle_start: datetime,
    evidence_base: str | None = None,
    fetch_refs: set[str] | None = None,
) -> Verdict:
    """Before flesh-out: T1-gate + manifest the seed; T2.1/T2.8/T2.4/T2.7 + M2 seed snapshot.

    No council, no >=2-ref check (only the seed exists yet). A sanitized seed, or a duplicate
    idea, parks/holds HERE — before any flesh-out research is spent.
    """
    holds: list[str] = []
    parks: list[str] = []

    # The seed is gated exactly like a T1 scan packet (its topic must name the job_id).
    seed_v = gate_t1(
        seed_path,
        topic=lane_info.track_dir_name,
        cycle_start=cycle_start,
        now=now,
        spawn_start=seed_spawn_start,
        evidence_base=evidence_base,
        fetch_refs=fetch_refs,
    )
    if seed_v.held:
        holds += [f"T2h-pre seed: {r}" for r in seed_v.reasons]

    fence = spawn_fence(contract_path, contract_spawn_start)  # T2.1
    if not fence.ok:
        holds += [f"T2h-pre.T2.1 {r}" for r in fence.reasons]
        return _decide("T2h-pre", holds, parks)
    contract, err = _load_json(contract_path)
    if err:
        return _decide("T2h-pre", holds + [f"T2h-pre.T2.1 {err}"], parks)

    holds += [f"T2h-pre.T2.1 schema: {e}" for e in _schema_errors(contract, "idea-contract")]
    holds += [f"T2h-pre.T2.1 unknown key (M3): {p}"
              for p in unknown_key_paths(contract, load_schema("idea-contract"))]
    holds += _slop_floor_holds(contract)
    holds += _lane_holds(lane_info, contract.get("job_id"), allow_human=True)
    if lane_info.lane != "human":
        holds.append("T2h-pre requires lane=human")
    # M2 seed snapshot binding (seed must be manifest-anchored once it passes its T1 check)
    if not seed_v.held:
        holds += [f"T2h-pre.M2 {r}" for r in verify_refs([seed_ref], events)]
    # T2.4 dedup -> PARK (before research is spent)
    fp = fingerprint(contract.get("what_should_exist"))
    if fp and fp in set(seen_fingerprints):
        parks.append(f"T2h-pre.T2.4 duplicate idea: {fp!r}")
    return _decide("T2h-pre", holds, parks)


def gate_t2h_post(
    contract_path: str,
    *,
    resolved_refs: list[ResolvedRef],
    events: list[dict],
    now: datetime,
) -> Verdict:
    """After flesh-out: T2.2 on BOTH refs (seed -> manifest, flesh-out #2 -> ledger), T2.3,
    and T2.6 council (now >=2 real refs exist, so thin-evidence will not false-trip)."""
    holds: list[str] = []
    parks: list[str] = []
    contract, err = _load_json(contract_path)
    if err:
        return _decide("T2h-post", [f"T2h-post.T2.1 {err}"], [])

    holds += [f"T2h-post.T2.2 {r}" for r in verify_refs(resolved_refs, events)]
    proposed_at = contract.get("proposed_at")
    for ref in resolved_refs:
        c_at = _snapshot_collected_at(ref.snapshot_path)
        if c_at is not None and not _ordered(c_at, proposed_at):
            # the flesh-out legitimately post-dates the contract; only the SEED must precede it
            if ref.event_names and T1_PASS in ref.event_names:
                holds.append(f"T2h-post.T2.3 seed collected_at {c_at!r} after proposed_at")
    reasons = council_evaluate(contract)
    if reasons:
        parks.append("T2h-post.T2.6 council-requested: " + "; ".join(reasons))
    return _decide("T2h-post", holds, parks)


# --------------------------------------------------------------------------------------
# T3 — review gate (per reviewed contract)
# --------------------------------------------------------------------------------------
def needs_more_action(decision: str, ledger_entries: list[dict]) -> str:
    """Map a Main decision + the flesh-out ledger to the retry action.

    The ledger records the INITIAL flesh-out (T1b.8) plus each needs_more retry. So:
      decision != needs_more       -> "none"
      needs_more, <2 ledger lines  -> "retry" (initial + at most one retry)
      needs_more, >=2 ledger lines -> "park"  (initial + 1 retry already spent)
    Counting the initial entry is the v3 off-by-one fix.
    """
    if decision != "needs_more":
        return "none"
    return "retry" if len(ledger_entries) < 2 else "park"


def gate_t3(
    review_path: str,
    intent_path: str,
    *,
    now: datetime,
    spawn_start: float,
    flesh_out_mtime: float,
    research_input_ref: ResolvedRef,
    ledger_entries: list[dict],
    events: list[dict],
    council_tripped: bool,
    proposed_at: str,
    product_plan_path: str | None = None,
    job_id: str = "",
    builds_root: str = "",
) -> Verdict:
    holds: list[str] = []
    parks: list[str] = []

    # T3.0 precondition: the initial flesh-out ledger anchor must already exist...
    if len(ledger_entries) < 1:
        holds.append("T3.0 no flesh-out ledger entry (T1b never passed before Main)")
    # ...and Main's review must post-date the latest flesh-out (no stale review reused).
    fence = spawn_fence(review_path, spawn_start)  # T3.1 M1
    if not fence.ok:
        return _decide("T3", holds + [f"T3.1 {r}" for r in fence.reasons], parks)
    review_mtime = os.stat(review_path).st_mtime
    if review_mtime <= flesh_out_mtime:
        holds.append(
            f"T3.0 stale review: review mtime {review_mtime:.6f} <= flesh-out mtime "
            f"{flesh_out_mtime:.6f} (a pre-flesh-out review cannot be the re-review)"
        )

    review, err = _load_json(review_path)
    if err:
        return _decide("T3", holds + [f"T3.1 main-review {err}"], parks)
    # T3.1 fence the intent-review too: it is a Main output from the SAME spawn, so a fresh
    # main-review must never pass alongside a stale/no-write intent-review (Codex A1 HOLD).
    intent_fence = spawn_fence(intent_path, spawn_start)
    if not intent_fence.ok:
        holds += [f"T3.1 intent-review {r}" for r in intent_fence.reasons]
        intent = {}
    else:
        intent, ierr = _load_json(intent_path)
        if ierr:
            holds.append(f"T3.1 intent-review {ierr}")
            intent = {}

    # T3.1 schema + enum + M3 for both review artifacts
    holds += [f"T3.1 main-review schema: {e}" for e in _schema_errors(review, "main-review")]
    holds += [f"T3.1 main-review unknown key (M3): {p}"
              for p in unknown_key_paths(review, load_schema("main-review"))]
    if intent:
        holds += [f"T3.1 intent-review schema: {e}"
                  for e in _schema_errors(intent, "intent-review")]
        holds += [f"T3.1 intent-review unknown key (M3): {p}"
                  for p in unknown_key_paths(intent, load_schema("intent-review"))]
    decision = review.get("decision")
    # T3.1 verify research-input current hash == its flesh-out ledger anchor
    holds += [f"T3.1 {r}" for r in verify_refs([research_input_ref], events)]
    # T3.2 reviewed_at >= proposed_at, <= now + 5min (secondary)
    reviewed_at = review.get("reviewed_at")
    r_dt, p_dt = _parse_iso(reviewed_at), _parse_iso(proposed_at)
    if r_dt is None or p_dt is None or not (p_dt <= r_dt <= now + FIVE_MIN):
        holds.append(f"T3.2 reviewed_at {reviewed_at!r} not in [proposed_at, now+5min]")
    # T3.3 council consistency: a council trip + an approval must escalate, never pass silently
    if council_tripped and decision in ("approved_for_coder", "promote"):
        holds.append("T3.3 council tripped but Main approved -> escalate to Korab (disagreement)")
    # needs_more accounting (T3.0): exhausted -> park
    action = needs_more_action(decision, ledger_entries)
    if action == "park":
        parks.append("T3.0 needs_more exhausted (initial + 1 retry) -> parked-needs-more")
    # T3.4 approved_for_coder -> product-plan + M4 declared-path preflight
    if decision == "approved_for_coder":
        holds += _t3_4_holds(product_plan_path, job_id, builds_root, spawn_start)
    return _decide("T3", holds, parks)


def _t3_4_holds(product_plan_path, job_id, builds_root, spawn_start) -> list[str]:
    holds: list[str] = []
    if not product_plan_path:
        return ["T3.4 approved_for_coder but no product-plan provided"]
    # product-plan is a Main output from the same spawn — fence it for freshness too, so a
    # stale/no-write plan cannot ride an approval through (Codex A1 HOLD).
    fence = spawn_fence(product_plan_path, spawn_start)
    if not fence.ok:
        return [f"T3.4 product-plan {r}" for r in fence.reasons]
    plan, err = _load_json(product_plan_path)
    if err:
        return [f"T3.4 product-plan {err}"]
    holds += [f"T3.4 product-plan schema: {e}" for e in _schema_errors(plan, "product-plan")]
    # M3 unknown-key hold, same as main-review/intent-review get at T3.1: the product-plan
    # feeds the CODER, so a smuggled key (e.g. instructions_for_coder) must hold here, not
    # ride an approval into the build loop (Codex T3-reconciliation HOLD #1).
    holds += [f"T3.4 product-plan unknown key (M3): {p}"
              for p in unknown_key_paths(plan, load_schema("product-plan"))]
    ok, violations = declared_paths_ok(plan.get("allowed_paths"), job_id, builds_root)
    if not ok:
        holds.append(f"T3.4 allowed_paths escape state/builds/{job_id}/ (M4): {violations}")
    vc = plan.get("verification_commands")
    if not (isinstance(vc, list) and len(vc) >= 1):
        holds.append("T3.4 verification_commands empty")
    return holds
