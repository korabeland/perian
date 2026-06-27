"""Tier-2 dreamer suppress + cluster pass for the ATB improvement feeder.

Given a Tier-1 harvest, this runs a **deterministic dedup floor** (which owns EVERY drop) and
then a grounded local-35B ``dreamer`` spawn that CLUSTERS the survivors and flags possible
duplicates for the overseer. Recall-biased + fail-closed.

The load-bearing trust property (Codex design-review BLOCKER): **the 35B can never drop a signal.**
Survivor membership is determined by the deterministic floor *before* the model runs; the model's
output is advisory clustering metadata only. A partition validator checks the model placed every
survivor ``signal_id`` exactly once (no missing / duplicated / invented id) and FAILS CLOSED to the
flat survivor list otherwise. The floor removes a signal ONLY if its ``signal_id`` exactly matches an
already-harvested runtime repeat (``seen_exact_runtime.json``) or a certified-log-derived known id
(``certified_known.json``, regenerated ONLY from the certified ``IMPROVEMENT_LOG.md`` — never from
model output). v1: exact-id drops only; semantic suppression waits for a deterministic
allowlisted-match validator.

Trust-spine reuse (plan Decision 8): the spawn / freshness-fence / ingest / data-fence machinery is
IMPORTED from ``think_sweep`` + ``think_gates`` rather than forked — you don't copy the polygraph.
Extracting a shared module is deferred until the feeder is proven.

Design: ``docs/plans/2026-06-18-002-improvement-feeder-PLAN.md`` (U3).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools import improvement_harvest as ih
from tools.improvement_harvest import _safe, emit_event
from tools.think_gates import spawn_fence
# Deliberate reuse of think_sweep's trust-reviewed spawn machinery (NOT a fork — see module docstring).
from tools.think_sweep import (
    DREAMER_PROFILE,
    MAX_TURNS,
    SPAWN_TIMEOUT,
    SpawnResult,
    _as_data,
    _ingest,
    default_spawn,
)

ATB_ROOT = Path(__file__).resolve().parent.parent
EVENTS = ATB_ROOT / "state" / "events.jsonl"
DIGEST_DIR = ATB_ROOT / "state" / "improvement-digests"
SYSTEM_MAP = ATB_ROOT / "docs" / "system-map.md"
IMPROVEMENT_LOG = ATB_ROOT / "IMPROVEMENT_LOG.md"
SCRATCH_ROOT = ATB_ROOT / "state" / "think-tmp"

# Runtime exact-repeat dedup — appended by the SWEEP (U5) with signal_ids it has already harvested.
# Recall-safe even if corrupt: loads empty -> at worst a known signal re-surfaces, never a novel drop.
SEEN_PATH = ATB_ROOT / "state" / "seen_exact_runtime.json"
# Certified-known ids — REGENERATED deterministically from the certified IMPROVEMENT_LOG.md only.
# Nothing the 35B emits ever writes either sidecar.
CERTIFIED_PATH = ATB_ROOT / "state" / "certified_known.json"

ARTIFACT_NAME = "tier2-clusters.json"

# The only cluster tags the model may use; the renderer whitelists to these so a model-controlled
# tag can't inject Markdown (Codex U2 LOW). The universal-vs-build-specific call is NOT the model's.
_CLUSTER_TAGS = ("escalate", "possible_duplicate")

# Overseer records which raw signals a certified log entry consumed via a machine-readable comment,
# e.g.  <!-- certified-signals: a1b2c3d4e5f6 0099aabbccdd -->  . regenerate_certified_known parses
# ONLY these. v1 log carries none -> certified_known stays empty until entries start citing ids.
import re

_CERTIFIED_RE = re.compile(r"<!--\s*certified-signals:\s*([^>]*?)-->", re.IGNORECASE)


# --------------------------------------------------------------------------------------------
# Sidecars — JSON list <-> set (mirror stall_alert._load_state/_save_state). Recall-safe loads.
# --------------------------------------------------------------------------------------------
def load_signatures(path: Path) -> set[str]:
    p = Path(path)
    if p.is_file():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return {str(x) for x in data}
        except (json.JSONDecodeError, ValueError, OSError):
            return set()
    return set()


def save_signatures(path: Path, sigs) -> None:
    p = Path(path)
    if not p.parent.is_dir():
        return
    p.write_text(json.dumps(sorted(str(s) for s in sigs), indent=2) + "\n")


def regenerate_certified_known(log_path: Path = IMPROVEMENT_LOG, out_path: Path = CERTIFIED_PATH) -> set[str]:
    """Parse certified signal-ids out of the certified IMPROVEMENT_LOG.md (the overseer's
    certification is the sole source) and (re)write the sidecar. Deterministic; reads ONLY the
    log; never reads model output. Missing/unreadable log -> empty set (recall-safe)."""
    ids: set[str] = set()
    try:
        text = Path(log_path).read_text(encoding="utf-8")
    except OSError:
        text = ""
    for m in _CERTIFIED_RE.finditer(text):
        for token in re.split(r"[\s,]+", m.group(1).strip()):
            if token:
                ids.add(token)
    save_signatures(out_path, ids)
    return ids


def load_known(seen_path: Path = SEEN_PATH, certified_path: Path = CERTIFIED_PATH) -> set[str]:
    return load_signatures(seen_path) | load_signatures(certified_path)


# --------------------------------------------------------------------------------------------
# The deterministic dedup floor — OWNS EVERY DROP. Pure; the model is not consulted.
# --------------------------------------------------------------------------------------------
def dedup_floor(signals: list[dict], known: set[str]) -> tuple[list[dict], list[dict]]:
    """Partition signals into (survivors, dropped). A signal is dropped IFF its ``signal_id`` is
    in ``known`` (an exact runtime repeat or a certified-known id). Nothing else is ever dropped —
    this is the recall guarantee, enforced in code, not by the model."""
    survivors, dropped = [], []
    for s in signals:
        (dropped if s.get("signal_id") in known else survivors).append(s)
    return survivors, dropped


# --------------------------------------------------------------------------------------------
# Partition validator — the model must place every survivor id exactly once. Fail-closed.
# --------------------------------------------------------------------------------------------
def validate_partition(model_out, valid_ids: set[str], key: str = "class_ids") -> tuple[bool, str | None]:
    """True iff model_out is ``{"clusters":[{<key>:[...], tag, ...}]}`` whose ``<key>`` ids are an
    EXACT partition of ``valid_ids`` (every id once, none missing, none invented, no string-type
    violations). v2 clusters CLASS ids (``key="class_ids"``); the validator stays generic over the
    id set + key. Any deviation -> (False, reason) -> caller FAILS CLOSED."""
    if not isinstance(model_out, dict):
        return False, "model output is not a JSON object"
    clusters = model_out.get("clusters")
    if not isinstance(clusters, list):
        return False, "model output missing a 'clusters' list"
    placed: list[str] = []
    for c in clusters:
        if not isinstance(c, dict):
            return False, "a cluster is not an object"
        ids = c.get(key)
        if not isinstance(ids, list):
            return False, f"a cluster is missing its '{key}' list"
        for i in ids:
            if not isinstance(i, str):
                return False, f"a {key} entry is not a string"
            placed.append(i)
    placed_set = set(placed)
    if len(placed) != len(placed_set):
        return False, f"a {key} id appears in more than one cluster"
    if placed_set != valid_ids:
        missing = sorted(valid_ids - placed_set)
        invented = sorted(placed_set - valid_ids)
        return False, f"partition mismatch (missing={missing}, invented={invented})"
    return True, None


# --------------------------------------------------------------------------------------------
# Prompt — flat survivor enumeration (by signal_id) fenced as untrusted DATA.
# --------------------------------------------------------------------------------------------
def _safe_list(items) -> str:
    """Fence a list of event-controlled strings (e.g. job_ids / sample reasons) as one escaped token
    list — every element JSON-escaped via _safe so none can break the data fence (Codex U2/U3)."""
    return "[" + ", ".join(_safe(x) for x in items) + "]"


def _class_block(classes: list, already_logged: set) -> str:
    """Enumerate the surviving recurrence classes as fenced DATA, MOST-RECURRENT FIRST (sorted
    defensively here by count desc then class_id, so the claim holds regardless of caller order).
    class_id + the recurrence counts are tool-generated facts the model must not alter; EVERY
    event-controlled field — event label, reason_sig, sample reasons, the build-id list — is
    JSON-escaped (via _safe / _safe_list). The advisory '[overlaps a logged finding]' note is
    context only — the model still clusters the class, never drops it."""
    lines = []
    for c in sorted(classes, key=lambda x: (-x.count, x.class_id)):
        logged = " [overlaps a logged finding]" if c.class_id in already_logged else ""
        lines.append(
            f"- class_id={c.class_id} event={_safe(c.event)} count={c.count} "
            f"builds={len(c.build_ids)} {_safe_list(c.build_ids)} days={c.day_count} "
            f"reason_sig={_safe(c.reason_sig)} samples={_safe_list(c.sample_reasons)}{logged}"
        )
    return "\n".join(lines)


def _dreamer_prompt(system_map_path: Path, classes: list, already_logged: set, out: Path) -> str:
    try:
        system_map = Path(system_map_path).read_text(encoding="utf-8")
    except OSError:
        system_map = "(system map unavailable)"
    cids = [c.class_id for c in classes]
    return (
        "You are the Tier-2 improvement-feeder pass. The system-stress signals have been aggregated "
        "into RECURRENCE CLASSES (one class = the same kind of stress, with a deterministic count). "
        "CLUSTER these classes into related groups so a human reviewer isn't drowning in repetition. "
        "You may ONLY annotate — you can NEVER drop a class or a signal (a deterministic validator "
        "owns removals), and the recurrence counts are tool-computed facts you must not alter.\n"
        f"WRITE your output as JSON to this EXACT path — this write is the ONLY thing that counts:\n  {out}\n"
        "Do NOT end your turn until that file exists.\n"
        "SHAPE — exactly this JSON object:\n"
        '  {"clusters": [{"label": <short string>, "tag": "escalate" | "possible_duplicate", '
        '"known_ref": <IL-id string, ONLY when tag=possible_duplicate>, "class_ids": [<id>, ...]}]}\n'
        "RULES:\n"
        f"  - Place EVERY one of these {len(cids)} class_ids EXACTLY once across the clusters: {json.dumps(cids)}\n"
        "  - Do NOT invent, omit, merge-away, or duplicate any class_id. An invalid partition is rejected.\n"
        "  - The classes are listed MOST-RECURRENT FIRST (by `count` across builds/days). A high-count "
        "class recurring across many builds/days is more likely a SYSTEMIC weakness — weight your labels "
        "and grouping toward those, but still place EVERY class_id exactly once.\n"
        "  - A class marked '[overlaps a logged finding]' is ADVISORY context — still cluster it; never drop it.\n"
        "  - tag a cluster 'possible_duplicate' (with a known_ref like 'IL-1') ONLY if it clearly restates a "
        "KNOWN issue from the system map; otherwise tag it 'escalate'. When unsure, ESCALATE.\n"
        "  - You do NOT decide universal-vs-build-specific — that is a frontier (Codex) + overseer call.\n"
        "\n"
        f"{_as_data('SYSTEM MAP (grounding — trusted reference)', system_map)}\n\n"
        f"{_as_data('SURVIVOR CLASSES (cluster these)', _class_block(classes, already_logged))}"
    )


# --------------------------------------------------------------------------------------------
# Clustered + fail-closed renderers (TOTAL — never raise).
# --------------------------------------------------------------------------------------------
def _class_one_line(c, already_logged: set) -> str:
    """One digest line for a recurrence class: id + fenced event/reason_sig + tool-computed
    recurrence (count across builds/days) + fenced build-id list + advisory flag. Counts are
    tool-generated integers; every event-controlled field is fenced."""
    logged = " — overlaps a logged finding" if c.class_id in already_logged else ""
    return (
        f"- `{c.class_id}` {_safe(c.event)} — {_safe(c.reason_sig)} — ×{c.count} across "
        f"{len(c.build_ids)} build(s) {_safe_list(c.build_ids)} / {c.day_count} day(s) "
        f"samples={_safe_list(c.sample_reasons)}{logged}"
    )


def render_clustered(date: str, classes: list, already_logged: set, model_out: dict, dropped: list[dict]) -> str:
    by_id = {c.class_id: c for c in classes}
    n_occ = sum(c.count for c in classes)
    lines = [f"# Improvement — Tier-2 clustered classes — {date}", ""]
    lines.append(
        f"**{len(classes)} recurrence class(es)** ({n_occ} occurrence(s)) in "
        f"{len(model_out.get('clusters', []))} cluster(s); "
        f"{len(dropped)} occurrence(s) dropped by the deterministic floor."
    )
    lines.append("")
    for c in model_out.get("clusters", []):
        if not isinstance(c, dict):
            continue
        tag = c.get("tag", "escalate")
        if tag not in _CLUSTER_TAGS:  # whitelist the model-controlled tag -> no raw Markdown injection
            tag = "escalate"
        ref = f" (possible duplicate of {_safe(c.get('known_ref'))})" if tag == "possible_duplicate" else ""
        lines.append(f"## {_safe(c.get('label', 'cluster'))} — `{tag}`{ref}")
        # rank the classes within a cluster most-recurrent first (deterministic: count desc, then id)
        present = [by_id[cid] for cid in c.get("class_ids", []) if cid in by_id]
        for cl in sorted(present, key=lambda x: (-x.count, x.class_id)):
            lines.append(_class_one_line(cl, already_logged))
            lines.append(f"  members: {', '.join(cl.member_signal_ids)}")  # full occurrence provenance
        lines.append("")
    lines.append("---")
    lines.append(
        "_Tier-2 clustering is ADVISORY. Drops are deterministic (occurrence-exact only); recurrence "
        "counts are tool-computed (classes ranked most-recurrent first); `possible_duplicate` / "
        "`overlaps a logged finding` are overseer hints, NOT removals. Surviving classes go to "
        "Tier-3 (Codex draft)._"
    )
    return "\n".join(lines).rstrip("\n") + "\n"


def render_failed(date: str, classes: list, already_logged: set, dropped: list[dict], reason: str) -> str:
    n_occ = sum(c.count for c in classes)
    lines = [
        f"# Improvement — Tier-2 FAIL-CLOSED — {date}", "",
        f"> ⚠ The Tier-2 clustering pass failed and was escalated UNCLUSTERED. Reason: {_safe(reason)}",
        f"> {len(classes)} recurrence class(es) / {n_occ} occurrence(s) preserved below; "
        f"{len(dropped)} occurrence(s) dropped by the deterministic floor. No signal was lost.", "",
    ]
    for c in classes:
        lines.append(_class_one_line(c, already_logged))
        lines.append(f"  members: {', '.join(c.member_signal_ids)}")  # full provenance, no occurrence lost
    lines.append("")
    lines.append("---")
    lines.append("_Recurrence + full per-class provenance preserved for overseer review._")
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------------------------
@dataclass
class Tier2Result:
    date: str
    status: str  # "all-known" | "clustered" | "failed-closed"
    survivors: list[dict] = field(default_factory=list)         # survivor OCCURRENCES (recall substrate)
    dropped: list[dict] = field(default_factory=list)
    surviving_classes: list = field(default_factory=list)        # list[ih.SignalClass] — what the model clusters
    already_logged_class_ids: set = field(default_factory=set)   # advisory overlap-with-log flags
    artifact: Path | None = None
    clusters: dict | None = None
    reason: str = ""

    # Exit-code mapping for a CLI / the sweep: 0 all-known, 2 clustered (-> Tier-3), 1 fail-closed.
    @property
    def exit_code(self) -> int:
        return {"all-known": 0, "clustered": 2, "failed-closed": 1}.get(self.status, 1)


def _fail_closed(date, classes, already_logged, survivors, dropped, digest_dir, events_path, reason, *, label=None) -> Tier2Result:
    """Write the fail-closed artifact FIRST (recurrence + full per-class provenance preserved), then
    emit the failed event (only after it exists). No occurrence is lost. ``label`` overrides the
    filename base (windowed stem); the event keeps the clean ``date``."""
    digest_dir = Path(digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{(label or date)}-tier2-FAILED.md"
    path.write_text(render_failed(date, classes, already_logged, dropped, reason), encoding="utf-8")
    emit_event(
        {
            "event": "improvement-cluster-failed",
            "date": date,
            "classes": len(classes),
            "survivors": len(survivors),
            "dropped": len(dropped),
            "reason": reason[:300],
        },
        events_path,
    )
    return Tier2Result(
        date=date, status="failed-closed", survivors=survivors, surviving_classes=classes,
        already_logged_class_ids=already_logged, dropped=dropped, artifact=path, reason=reason,
    )


def _render_failed_occurrences(date: str, survivors: list[dict], dropped: list[dict], reason: str) -> str:
    """Classless fail-closed digest — used ONLY if class aggregation itself trips the completeness
    invariant (a defensive branch that cannot fire in normal flow). Lists every survivor OCCURRENCE
    id so no signal is lost even when the class view is unavailable."""
    lines = [
        f"# Improvement — Tier-2 FAIL-CLOSED (pre-cluster) — {date}", "",
        f"> ⚠ Class aggregation failed; escalated UNCLUSTERED at the occurrence level. Reason: {_safe(reason)}",
        f"> {len(survivors)} survivor occurrence(s) preserved below; "
        f"{len(dropped)} occurrence(s) dropped by the deterministic floor. No signal was lost.", "",
    ]
    for s in survivors:
        lines.append(
            f"- `{s.get('signal_id')}` {_safe(s.get('event'))} build {_safe(s.get('job_id'))} "
            f"@ {_safe(s.get('ts'))} — {_safe(s.get('reason'))}"
        )
    lines.append("")
    lines.append("---")
    lines.append("_Occurrence-level provenance preserved for overseer review._")
    return "\n".join(lines).rstrip("\n") + "\n"


def _fail_closed_occurrences(date, survivors, dropped, digest_dir, events_path, reason, *, label=None) -> Tier2Result:
    """Fail closed at the OCCURRENCE level (class aggregation unavailable). Artifact first, event after.
    ``label`` overrides the filename base (windowed stem); the event keeps the clean ``date``."""
    digest_dir = Path(digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{(label or date)}-tier2-FAILED.md"
    path.write_text(_render_failed_occurrences(date, survivors, dropped, reason), encoding="utf-8")
    emit_event(
        {"event": "improvement-cluster-failed", "date": date, "classes": 0,
         "survivors": len(survivors), "dropped": len(dropped), "reason": reason[:300]},
        events_path,
    )
    return Tier2Result(date=date, status="failed-closed", survivors=survivors, dropped=dropped,
                       artifact=path, reason=reason)


def run_tier2(
    harvest_result,
    *,
    scratch: Path,
    spawn=default_spawn,
    seen_path: Path = SEEN_PATH,
    certified_path: Path = CERTIFIED_PATH,
    log_path: Path = IMPROVEMENT_LOG,
    system_map_path: Path = SYSTEM_MAP,
    digest_dir: Path = DIGEST_DIR,
    events_path: Path = EVENTS,
    stem: str | None = None,
    max_turns: int = MAX_TURNS,
    timeout: int = SPAWN_TIMEOUT,
) -> Tier2Result:
    """Run the deterministic OCCURRENCE-exact floor, aggregate the SURVIVING recurrence classes, then
    the (annotate-only) 35B pass that CLUSTERS the class_ids. The floor OWNS every drop (occurrence-
    exact, v1-identical); classes + recurrence are a ranking/context view; the advisory 'overlaps a
    logged finding' flag is context for Codex only and NEVER drops. Fail-closed. ``harvest_result`` is
    any object exposing ``.date`` + ``.signals`` (a v1 HarvestResult or a v2 WindowResult). ``stem``
    overrides the artifact filename base (the sweep passes a windowed stem); default = date (v1)."""
    date = harvest_result.date
    label = stem or date
    occurrences = harvest_result.signals
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)

    # 1. Refresh certified-known from the LOG (deterministic; never model output); load floor sets.
    regenerate_certified_known(log_path, certified_path)
    certified = load_signatures(certified_path)
    known = load_known(seen_path, certified_path)

    # 2. Deterministic floor OWNS every drop — OCCURRENCE-exact (v1, UNCHANGED). Fixed before any model.
    survivors, dropped = dedup_floor(occurrences, known)

    # 3. Aggregate the surviving recurrence classes over the survivor OCCURRENCES (deterministic,
    #    completeness-checked: every survivor occurrence lands in exactly one class). Defensive
    #    fail-closed if the completeness invariant ever trips — an unhandled raise must not bypass the
    #    fail-closed contract (Codex U2 LOW). Cannot fire in normal flow: survivors are floor-deduped.
    try:
        surviving_classes = ih.aggregate_classes(survivors)
    except ih.CompletenessError as exc:
        return _fail_closed_occurrences(date, survivors, dropped, digest_dir, events_path,
                                        f"class aggregation completeness violation: {exc}", label=label)
    surviving_class_ids = {c.class_id for c in surviving_classes}

    # 4. ADVISORY 'overlaps a logged finding': a SURVIVING class whose class had a CERTIFIED member in
    #    the window (the floor dropped that member). Context for Codex only — NEVER a drop (Decision 5);
    #    new occurrences of a previously-logged class survive and are merely flagged.
    already_logged = {ih.class_id(o) for o in occurrences if o.get("signal_id") in certified} & surviving_class_ids

    # 5. All-known short-circuit — nothing survives, no spawn, no Tier-3.
    if not survivors:
        emit_event(
            {"event": "improvement-cluster", "date": date, "status": "all-known",
             "survivors": 0, "classes": 0, "dropped": len(dropped)},
            events_path,
        )
        return Tier2Result(date=date, status="all-known", survivors=[], dropped=dropped)

    # 6. Spawn the 35B to CLUSTER the surviving class_ids (annotate only). Record spawn_start (M1 fence).
    out = scratch / ARTIFACT_NAME
    prompt = _dreamer_prompt(system_map_path, surviving_classes, already_logged, out)
    spawn_start = time.time()
    res = spawn(DREAMER_PROFILE, prompt, scratch, max_turns=max_turns, timeout=timeout)

    def fail(reason: str) -> Tier2Result:
        return _fail_closed(date, surviving_classes, already_logged, survivors, dropped,
                            digest_dir, events_path, reason, label=label)

    # 7. Fail-closed on spawn failure or a stale/no-write artifact (freshness fence).
    if not getattr(res, "ok", False):
        return fail(f"dreamer spawn failed: {getattr(res, 'error', '') or 'not ok'}")
    fence = spawn_fence(str(out), spawn_start)
    if not fence.ok:
        return fail(f"freshness fence: {'; '.join(fence.reasons)}")

    # 8. Ingest scratch -> sweep-owned canonical (never read a model-written path), then parse.
    canonical = Path(digest_dir) / f".{label}-tier2.ingested.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    _sha, err = _ingest(out, canonical)
    if err:
        return fail(f"ingest: {err}")
    try:
        model_out = json.loads(canonical.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return fail(f"unparseable model output: {exc}")

    # 9. Validate the CLASS partition (every surviving class_id placed exactly once). Invalid ->
    #    fail-closed (recurrence + provenance preserved; no class or occurrence lost).
    ok, perr = validate_partition(model_out, surviving_class_ids, key="class_ids")
    if not ok:
        return fail(f"partition invalid: {perr}")

    # 10. Success — render the clustered, annotated class digest.
    Path(digest_dir).mkdir(parents=True, exist_ok=True)
    path = Path(digest_dir) / f"{label}-tier2-clusters.md"
    path.write_text(
        render_clustered(date, surviving_classes, already_logged, model_out, dropped), encoding="utf-8"
    )
    emit_event(
        {"event": "improvement-cluster", "date": date, "status": "clustered",
         "survivors": len(survivors), "classes": len(surviving_classes), "dropped": len(dropped),
         "clusters": len(model_out.get("clusters", []))},
        events_path,
    )
    return Tier2Result(date=date, status="clustered", survivors=survivors, dropped=dropped,
                       surviving_classes=surviving_classes, already_logged_class_ids=already_logged,
                       artifact=path, clusters=model_out)


def _mint_scratch(date: str) -> Path:
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    p = SCRATCH_ROOT / f"improvement-tier2-{date}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def main(argv: list[str] | None = None) -> int:
    import argparse
    from datetime import datetime, timezone

    from tools.decompose_events import read_events

    ap = argparse.ArgumentParser(description="Tier-2 dreamer suppress+cluster (improvement feeder).")
    ap.add_argument("--date", default=None, metavar="YYYY-MM-DD", help="window END day; default today (UTC)")
    ap.add_argument("--window-days", type=int, default=14, help="rolling window length (complete UTC days)")
    args = ap.parse_args(argv)
    date = args.date or datetime.now(timezone.utc).date().isoformat()
    result = ih.harvest_window(read_events(EVENTS), date, args.window_days)
    if result.status == "no-activity":
        return 0
    stem = f"{date}-window-{result.window_days}d"  # same windowed stem as the sweep (Codex U5 LOW)
    res = run_tier2(result, scratch=_mint_scratch(date), stem=stem)
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
