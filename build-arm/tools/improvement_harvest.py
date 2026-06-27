"""Tier-1 deterministic harvester for the ATB improvement feeder.

Reads ``state/events.jsonl``, filters to a target day's *system-stress* signals, assigns each a
stable ``signal_id``, and renders a raw retro-digest to
``state/improvement-digests/<YYYY-MM-DD>.md``. Pure, deterministic, no model — the trust-safe
floor of the 3-tier feeder. Skips true no-activity days silently.

Design: ``docs/plans/2026-06-18-002-improvement-feeder-PLAN.md`` (U2). Mirrors the conventions of
``tools/decompose_events.read_events`` (total read), ``tools/stall_alert.STALL_EVENTS``,
``tools/idea_digest.assemble_digest`` (total renderer) and ``tools/gate_sweep.emit_event``.

Exit codes (mirror gate_sweep): 0 = clean / nothing to harvest, 2 = stress signals found
(digest written), 1 = error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import stall_alert
from tools.decompose_events import read_events

ATB_ROOT = Path(__file__).resolve().parent.parent
EVENTS = ATB_ROOT / "state" / "events.jsonl"
DIGEST_DIR = ATB_ROOT / "state" / "improvement-digests"

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Labeled "system under stress" events. A SUPERSET of stall_alert.STALL_EVENTS — the feeder
# additionally cares about every coach round (not just exhaustion), think-loop stress and the
# validator gate. test_improvement_harvest asserts STALL_EVENTS ⊆ STRESS_EVENTS so the two
# can't silently drift apart.
STRESS_EVENTS = (
    "coach",
    "coach-exhausted",
    "gate-held",
    "gate-error",
    "stall-alert",
    "validation-failed",
    "think-retry",
    "think-gate-fail",
    "think-parked",
    "verification-failed",
)

# Coach rounds are grouped by build (job_id) so N rounds on one build read as one line, not N
# separate "builds" (see U2 test scenario "multiple coach rounds same job_id").
_GROUP_BY_JOB = ("coach",)


# --------------------------------------------------------------------------------------------
# Pure helpers — signature / id / date. Deterministic; no I/O.
# --------------------------------------------------------------------------------------------
def signature(ev: dict) -> str:
    """The dedup signature of one event: ``event:job_id:ts``. The downstream dedup floor (U3)
    treats two signals with the same signature as the same signal. job_id may be empty."""
    return f"{ev.get('event', '')}:{ev.get('job_id', '') or ''}:{ev.get('ts', '')}"


def signal_id(ev: dict) -> str:
    """Stable, reproducible id for an event (same input -> same id). 12 hex chars of the
    sha256 of its signature — short enough to read, wide enough to not collide in practice."""
    return hashlib.sha256(signature(ev).encode("utf-8")).hexdigest()[:12]


def _parse_ts(ts) -> datetime | None:
    """Parse a UTC ``%Y-%m-%dT%H:%M:%SZ`` timestamp; None if missing/unparseable."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _event_date(ts) -> str | None:
    dt = _parse_ts(ts)
    return dt.date().isoformat() if dt else None


def _normalize(ev: dict) -> dict:
    """Project an event into a normalized signal dict with safe defaults — so the renderer
    never has to guard field access (totality)."""
    jid = ev.get("job_id")
    return {
        "signal_id": signal_id(ev),
        "signature": signature(ev),
        "event": ev.get("event", ""),
        # Coerce job_id to a safe, hashable scalar string. A non-scalar (list/dict) job_id in a
        # malformed event line would otherwise crash build_ids (set-of-unhashable) and the coach
        # grouping, breaking the totality promise (Codex code-review MED #1).
        "job_id": str(jid) if isinstance(jid, (str, int)) else "",
        "ts": ev.get("ts", ""),
        "round": ev.get("round"),
        "rounds": ev.get("rounds"),
        "reason": ev.get("reason", ""),
    }


def collect(events: list[dict], target_date: str) -> list[dict]:
    """Validated stress signals for ``target_date``, each normalized + given a signal_id.

    A signal is kept only if (a) its ``event`` is in STRESS_EVENTS and (b) its ``ts`` parses to
    ``target_date``. Events missing ``event`` or with an unparseable ``ts`` are silently skipped
    (Codex MED #5 — read_events returns any dict, so we shape-check here).

    Identity-duplicate events — those sharing a ``signal_id`` (= same ``event:job_id:ts``) — are
    collapsed to one signal, keeping the first occurrence. ``signal_id`` is THE identity key the
    rest of the feeder assumes is unique: the Tier-2 dedup floor drops by it, the Tier-2 partition
    validator requires every survivor placed exactly once, and certified-known suppresses by it. A
    duplicate survivor makes any clustering invalid (one id in >1 cluster), deterministically
    fail-closing Tier-2 before Codex — observed live on 2026-06-13, where a think-retry logged twice
    jammed the pass. Dedup is on IDENTITY, not byte-content: two events with the same
    event/job_id/ts but a differing ``reason``/``round`` also collapse (the later detail is dropped),
    because the whole pipeline already treats same-``signal_id`` as the same signal — keeping both
    would violate that invariant. If same-second same-job stress ever needs distinguishing, widen
    ``signal_id`` with a real occurrence discriminator rather than dedup on content. Recall-safe:
    only same-identity signals are merged."""
    out: list[dict] = []
    seen: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") not in STRESS_EVENTS:
            continue
        if _event_date(ev.get("ts")) != target_date:
            continue
        sig = _normalize(ev)
        if sig["signal_id"] in seen:
            continue  # identity-duplicate (same signal_id) -> keep first (see docstring)
        seen.add(sig["signal_id"])
        out.append(sig)
    return out


@dataclass
class HarvestResult:
    date: str
    signals: list[dict] = field(default_factory=list)
    # "no-activity" (no stress signals at all -> silent) | "no-build" (signals present but none
    # carry a job_id) | "build-day" (>=1 signal with a job_id). The build-day predicate is
    # deterministic = "any stress signal carrying a job_id" (Codex MED #6).
    status: str = "no-activity"

    @property
    def build_ids(self) -> list[str]:
        return sorted({s["job_id"] for s in self.signals if s.get("job_id")})

    @property
    def is_build_day(self) -> bool:
        return self.status == "build-day"


def harvest(events: list[dict], target_date: str) -> HarvestResult:
    """Filter + classify one day. Pure: no I/O."""
    signals = collect(events, target_date)
    if not signals:
        status = "no-activity"
    elif any(s.get("job_id") for s in signals):
        status = "build-day"
    else:
        status = "no-build"
    return HarvestResult(date=target_date, signals=signals, status=status)


# --------------------------------------------------------------------------------------------
# v2 — signal-class key + recurrence aggregation + rolling window. ADDITIVE: the v1 occurrence
# pipeline above (collect / harvest / signal_id) is UNTOUCHED. A class is a RANKING + CONTEXT view
# layered beside the occurrences; the class key is NEVER a drop/suppression key (the recall floor
# stays occurrence-exact, in the dreamer). A normalizer mistake can therefore only mis-RANK, never
# drop a signal. Design: docs/plans/2026-06-19-001-feeder-windowed-recurrence-PLAN.md (U1).
# --------------------------------------------------------------------------------------------
NORMALIZER_VERSION = 1

# Order is fixed and load-bearing: quoted strings first (before their contents are mangled), then
# path-like tokens (which contain digits/hex), then uuids (before the generic hex run eats their
# segments), then long-hex/ids, then bare digit runs. Placeholders are UPPERCASE so a later rule
# (all lowercase-anchored) can't re-match them.
_RE_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")
_RE_PATH = re.compile(r"\S*/\S*")
_RE_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RE_HEX = re.compile(r"\b[0-9a-f]{7,}\b")
_RE_NUM = re.compile(r"\d+")
_RE_WS = re.compile(r"\s+")
_REASON_SIG_CAP = 200
_REASON_RAW_CAP = 2000  # bound regex work BEFORE processing (Codex U1 LOW): a huge adversarial reason
_SAMPLE_REASONS_CAP = 3


def normalize_reason(reason) -> str:
    """Canonicalize an event ``reason`` into a recurrence signature: lowercase, then collapse the
    high-entropy parts that vary occurrence-to-occurrence but not failure-to-failure — quoted
    strings -> STR, path-like tokens -> PATH, uuids/long-hex/ids -> ID, digit runs -> N — then
    squeeze whitespace and truncate. Two reasons differing only in those varying parts (``rc=1``
    vs ``rc=2``; two build ids/paths/timestamps) map to the SAME signature, so they aggregate into
    one recurrence class. RANKING-ONLY: a miscount only mis-prioritizes, it can NEVER drop a signal
    (the floor is occurrence-exact). Deterministic + order-fixed (see the regex block)."""
    s = "" if reason is None else str(reason)
    s = s[:_REASON_RAW_CAP]  # cap raw input first so the regex pass can't be abused with a huge string
    s = s.lower()
    s = _RE_QUOTED.sub("STR", s)
    s = _RE_PATH.sub("PATH", s)
    s = _RE_UUID.sub("ID", s)
    s = _RE_HEX.sub("ID", s)
    s = _RE_NUM.sub("N", s)
    s = _RE_WS.sub(" ", s).strip()
    return s[:_REASON_SIG_CAP]


def class_signature(ev: dict) -> str:
    """The recurrence-class signature of an event/signal: ``event|normalized-reason``. Groups
    occurrences that represent the SAME KIND of stress, for counting recurrence."""
    return f"{ev.get('event', '')}|{normalize_reason(ev.get('reason', ''))}"


def class_id(ev: dict) -> str:
    """Stable 12-hex id for a signal class (sha256 of its signature). A ranking/context key only —
    never a drop key. Mirrors signal_id's shape so the two are visually distinguishable in context."""
    return hashlib.sha256(class_signature(ev).encode("utf-8")).hexdigest()[:12]


@dataclass
class SignalClass:
    """A recurrence class: occurrences sharing event+normalized-reason. Carries its FULL member
    signal_ids (never capped) so aggregation loses no occurrence — the provenance-complete VIEW."""
    class_id: str
    event: str
    reason_sig: str
    count: int
    build_ids: list[str]
    day_count: int
    first_ts: str
    last_ts: str
    member_signal_ids: list[str]
    sample_reasons: list[str]


class CompletenessError(Exception):
    """Raised when class aggregation would lose/duplicate/invent an occurrence (the completeness
    invariant: every occurrence appears in exactly one class). Aggregation is a provenance-complete
    VIEW — a violation means a code bug, so we FAIL CLOSED rather than silently drop. Recall guard."""


def assert_complete(signals: list[dict], classes: list[SignalClass]) -> None:
    """The completeness invariant, FAIL-CLOSED: class members must be an EXACT partition of the
    occurrence signal_ids — every occurrence placed once, none lost, invented, or duplicated. Raises
    CompletenessError otherwise. Split out so it can be tested directly with a broken (signals,
    classes) pair. Independently SOUND (Codex U1 MED): also rejects a degenerate INPUT set — a
    missing/empty signal_id or a non-deduped duplicate — so the check can't be fooled by malformed
    input rather than relying on the caller having deduped."""
    occ_ids = [s.get("signal_id", "") for s in signals if isinstance(s, dict)]
    if any(not sid for sid in occ_ids):
        raise CompletenessError("an occurrence is missing its signal_id")
    if len(occ_ids) != len(set(occ_ids)):
        raise CompletenessError("the occurrence set has duplicate signal_ids (not deduped before aggregation)")
    occ_set = set(occ_ids)
    placed: list[str] = []
    for c in classes:
        placed.extend(c.member_signal_ids)
    placed_set = set(placed)
    if len(placed) != len(placed_set):
        raise CompletenessError("an occurrence signal_id appears in more than one class")
    if placed_set != occ_set:
        missing = sorted(occ_set - placed_set)
        invented = sorted(placed_set - occ_set)
        raise CompletenessError(
            f"class aggregation lost/invented occurrences (missing={missing}, invented={invented})"
        )


def _hashable_job_id(jid) -> str:
    """Coerce a job_id to a safe hashable string (mirror _normalize): a non-scalar (list/dict) in a
    malformed signal becomes '' so the build_ids set can never crash on an unhashable element."""
    return str(jid) if isinstance(jid, (str, int)) else ""


def aggregate_classes(signals: list[dict]) -> list[SignalClass]:
    """Aggregate occurrence signals into recurrence classes — a provenance-complete VIEW. Each class
    carries its full member signal_ids; the completeness invariant is enforced (FAIL CLOSED). Sorted
    by count desc then class_id for deterministic ranking. TOTAL (Codex U1 LOW): non-dict entries are
    skipped (mirrors collect's discipline) and job_id is coerced hashable, so a malformed signal can't
    crash. Pure: no model, no I/O."""
    dict_signals = [s for s in signals if isinstance(s, dict)]
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for s in dict_signals:
        cid = class_id(s)
        if cid not in groups:
            groups[cid] = []
            order.append(cid)
        groups[cid].append(s)
    classes: list[SignalClass] = []
    for cid in order:
        members = groups[cid]
        tss = sorted(m.get("ts", "") for m in members if m.get("ts"))
        days = {_event_date(m.get("ts")) for m in members}
        days.discard(None)
        # distinct raw reasons, sorted deterministically then capped (order-INDEPENDENT; Codex U1
        # MED) — advisory display context, fenced downstream.
        sample = sorted({str(m.get("reason", "") or "") for m in members if str(m.get("reason", "") or "")})
        job_ids = [_hashable_job_id(m.get("job_id")) for m in members]
        classes.append(
            SignalClass(
                class_id=cid,
                event=members[0].get("event", ""),
                reason_sig=normalize_reason(members[0].get("reason", "")),
                count=len(members),
                build_ids=sorted({j for j in job_ids if j}),
                day_count=len(days),
                first_ts=tss[0] if tss else "",
                last_ts=tss[-1] if tss else "",
                member_signal_ids=[m.get("signal_id", "") for m in members],
                sample_reasons=sample[:_SAMPLE_REASONS_CAP],
            )
        )
    assert_complete(dict_signals, classes)
    classes.sort(key=lambda c: (-c.count, c.class_id))
    return classes


def window_dates(end_date: str, window_days: int) -> list[str]:
    """The complete-UTC-day strings (YYYY-MM-DD) ending at ``end_date`` inclusive, length
    max(1, window_days), oldest first. ``end_date`` is the most-recent complete UTC day (the v1
    yesterday-UTC rule holds at the caller)."""
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    n = max(1, int(window_days))
    return [(end - timedelta(days=i)).date().isoformat() for i in range(n - 1, -1, -1)]


def collect_window(events: list[dict], start_date: str, end_date: str) -> list[dict]:
    """Like collect() but keeps stress signals whose UTC date is in ``[start_date, end_date]``,
    deduped by signal_id across the whole window (identity-dedup, first kept — same recall-safe rule
    as collect; ISO date strings compare chronologically)."""
    out: list[dict] = []
    seen: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") not in STRESS_EVENTS:
            continue
        d = _event_date(ev.get("ts"))
        if d is None or d < start_date or d > end_date:
            continue
        sig = _normalize(ev)
        if sig["signal_id"] in seen:
            continue
        seen.add(sig["signal_id"])
        out.append(sig)
    return out


@dataclass
class WindowResult:
    """A windowed harvest: the v1-shape occurrence list PLUS the recurrence-class VIEW over it.
    ``date`` aliases ``end_date`` so single-day renderers/artifact-naming keep working."""
    start_date: str
    end_date: str
    window_days: int
    signals: list[dict] = field(default_factory=list)
    classes: list[SignalClass] = field(default_factory=list)
    status: str = "no-activity"  # "no-activity" | "no-build" | "build-day" (same predicate as v1)

    @property
    def date(self) -> str:
        return self.end_date

    @property
    def build_ids(self) -> list[str]:
        return sorted({s["job_id"] for s in self.signals if s.get("job_id")})

    @property
    def is_build_day(self) -> bool:
        return self.status == "build-day"


def harvest_window(events: list[dict], end_date: str, window_days: int = 14) -> WindowResult:
    """Harvest a rolling window of complete UTC days ending at ``end_date``. Returns the v1-shape
    occurrence list (identical to running v1 collect over each day, deduped across the window) PLUS a
    completeness-checked recurrence-class view. Pure: no I/O. ``window_days=1`` reads exactly one day
    — but classes are still added beside the occurrences (it is NOT a behavior-identical v1 superset;
    v1's single-day tests run against collect/harvest, which are untouched)."""
    dates = window_dates(end_date, window_days)
    signals = collect_window(events, dates[0], dates[-1])
    classes = aggregate_classes(signals)
    if not signals:
        status = "no-activity"
    elif any(s.get("job_id") for s in signals):
        status = "build-day"
    else:
        status = "no-build"
    return WindowResult(
        start_date=dates[0], end_date=dates[-1], window_days=len(dates),
        signals=signals, classes=classes, status=status,
    )


# --------------------------------------------------------------------------------------------
# Injection fence (Codex HIGH #4) — applied at the FIRST reader, here, before any digest text.
# --------------------------------------------------------------------------------------------
def _safe(text) -> str:
    """JSON-escape an event-controlled free-text value so a malicious ``reason``/``job_id``
    cannot inject Markdown (newlines/headers become escaped chars on one line) or forge the
    downstream ``_as_data`` boundary. ``_as_data`` (U3) is the second layer; this is the first.
    Mirrors think_sweep._as_data's UNTRUSTED-DATA marker neutralization."""
    s = json.dumps("" if text is None else str(text), ensure_ascii=False)
    return s.replace("UNTRUSTED DATA", "UNTRUSTED-DATA")


def _render_event_section(event: str, sigs: list[dict]) -> list[str]:
    """Render one event-type section. Total: a signal that fails to render is skipped + counted,
    never raised (idea_digest discipline)."""
    lines: list[str] = []
    skipped = 0
    if event in _GROUP_BY_JOB:
        by_job: dict[str, list[dict]] = {}
        order: list[str] = []
        for s in sigs:
            jid = s.get("job_id") or ""
            if jid not in by_job:
                by_job[jid] = []
                order.append(jid)
            by_job[jid].append(s)
        for jid in order:
            try:
                group = by_job[jid]
                rounds = sorted(
                    {g["round"] for g in group if g.get("round") is not None}
                )
                label = f"build {_safe(jid)}" if jid else "(no job_id)"
                detail = f" — rounds seen: {_safe(rounds)}" if rounds else ""
                ids = ", ".join(g.get("signal_id", "") for g in group)
                lines.append(f"- {label}: {len(group)} round(s){detail} [ids: {ids}]")
            except Exception:  # noqa: BLE001 — totality: never let one signal crash the digest
                skipped += 1
    else:
        for s in sigs:
            try:
                jid = s.get("job_id") or ""
                where = f"build {_safe(jid)}" if jid else "(no job_id)"
                reason = s.get("reason")
                rtail = f" — {_safe(reason)}" if reason else ""
                lines.append(
                    f"- `{s.get('signal_id', '')}` {where} @ {_safe(s.get('ts', ''))}{rtail}"
                )
            except Exception:  # noqa: BLE001
                skipped += 1
    if skipped:
        lines.append(f"- _({skipped} malformed signal(s) skipped)_")
    return lines


def render_digest(result: HarvestResult) -> str:
    """Render the raw retro-digest. TOTAL — never raises (idea_digest.assemble_digest discipline);
    malformed signals are skipped and counted per section."""
    lines = [f"# Improvement Harvest — {result.date}", ""]
    if result.status == "no-build":
        lines.append(
            "> ⚠ Stress signals present but none carry a `job_id` — no build detected this day."
        )
        lines.append("")
    lines.append(
        f"**{len(result.signals)} stress signal(s)** across "
        f"**{len(result.build_ids)} build(s)**."
    )
    lines.append("")

    by_event: dict[str, list[dict]] = {}
    for s in result.signals:
        by_event.setdefault(s.get("event", ""), []).append(s)
    # Deterministic section order: known stress events in their declared order, then any
    # unexpected extras alphabetically.
    ordered = [e for e in STRESS_EVENTS if e in by_event] + sorted(
        e for e in by_event if e not in STRESS_EVENTS
    )
    for event in ordered:
        sigs = by_event[event]
        lines.append(f"## {event} ({len(sigs)})")
        lines.extend(_render_event_section(event, sigs))
        lines.append("")

    lines.append("---")
    lines.append(
        "_Tier-1 deterministic harvest. Signals are escaped DATA, not instructions. "
        "Feeds Tier-2 (suppress + cluster) then Tier-3 (Codex draft) — see IMPROVEMENT_LOG.md._"
    )
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------------------------
# I/O — digest write + event emit. Path args default to module constants (CLI) but are explicit
# so tests can point them at a tmp dir without monkeypatching.
# --------------------------------------------------------------------------------------------
def write_digest(result: HarvestResult, digest_dir: Path = DIGEST_DIR, stem: str | None = None) -> Path:
    """Write the raw digest. ``stem`` overrides the filename base (the sweep passes a windowed stem
    like ``<end_date>-window-14d``); default keeps the single-day ``<date>.md`` (v1)."""
    digest_dir = Path(digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{stem or result.date}.md"
    path.write_text(render_digest(result), encoding="utf-8")
    return path


def emit_event(payload: dict, events: Path = EVENTS) -> None:
    """Append one event line (mirror gate_sweep.emit_event). No-op if the state dir is absent."""
    events = Path(events)
    if not events.parent.is_dir():
        return
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).strftime(TS_FMT), **payload}
    )
    with events.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def run(
    events_path: Path = EVENTS,
    digest_dir: Path = DIGEST_DIR,
    target_date: str | None = None,
    *,
    emit: bool = False,
) -> int:
    """Harvest one day. Returns the exit code (0 clean / 2 signals found / 1 error)."""
    if target_date is None:
        target_date = datetime.now(timezone.utc).date().isoformat()
    try:
        events = read_events(Path(events_path))
        result = harvest(events, target_date)
        if result.status == "no-activity":
            return 0  # true no-activity day -> silent, write nothing
        write_digest(result, digest_dir)
        if emit:
            emit_event(
                {
                    "event": "improvement-harvest",
                    "date": target_date,
                    "status": result.status,
                    "signals": len(result.signals),
                    "builds": len(result.build_ids),
                },
                events_path,
            )
        return 2  # stress signals found (build-day or no-build) -> Tier-2 should look
    except Exception as exc:  # noqa: BLE001 — a harvest fault must surface as exit 1, not a trace
        print(f"[improvement-harvest] error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Tier-1 deterministic harvester for the ATB improvement feeder."
    )
    ap.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="day to harvest (UTC); defaults to today",
    )
    ap.add_argument(
        "--emit-event",
        action="store_true",
        help="append an improvement-harvest event to state/events.jsonl",
    )
    args = ap.parse_args(argv)
    return run(EVENTS, DIGEST_DIR, args.date, emit=args.emit_event)


if __name__ == "__main__":
    raise SystemExit(main())
