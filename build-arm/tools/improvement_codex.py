"""Tier-3 Codex comb + draft pass for the ATB improvement feeder.

Given the Tier-2 survivors, this invokes Codex (the frontier tier) to comb them, apply the
**universal-vs-build-specific** judgment — the one call only a frontier model makes — and DRAFT
candidate ``IMPROVEMENT_LOG.md`` entries to a review file. It NEVER writes the log: it drafts
*candidates* marked UNVERIFIED; the overseer + Korab certify (doer != certifier).

Fail-safe: a Codex error/timeout or a schema-invalid result writes ``<date>-tier3-FAILED.md`` (the
survivor digest, no candidates) and emits ``improvement-candidates-failed`` only AFTER that artifact
exists — it fabricates nothing. Codex's ``--output-schema`` enforces the shape Codex-side; this
module RE-validates the result itself (schema-valid != trusted; the doer != certifier rule binds
Codex too).

Mirrors ``tools/codex_review.run_codex`` (the ``codex exec`` invocation + strict output-schema +
exit/raise fail-safe semantics). Design: ``docs/plans/2026-06-18-002-improvement-feeder-PLAN.md`` (U4).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tools import improvement_harvest as ih
from tools.improvement_harvest import HarvestResult, _safe, emit_event
from tools.think_sweep import _as_data

ATB_ROOT = Path(__file__).resolve().parent.parent
EVENTS = ATB_ROOT / "state" / "events.jsonl"
DIGEST_DIR = ATB_ROOT / "state" / "improvement-digests"
SCHEMA_PATH = ATB_ROOT / "schemas" / "improvement-candidates.schema.json"

CODEX_EFFORT = "high"
CODEX_TIMEOUT = 600  # seconds

_TAGS = {"universal", "build-specific"}
_CONF = {"low", "medium", "high"}
_REQUIRED_STR = ("finding", "tag", "rationale", "proposed_fix", "where_it_lands", "confidence")
_REQUIRED = _REQUIRED_STR + ("source_class_ids",)  # source_class_ids is a list — checked separately

# The anti-over-fit guardrail — the operating-thesis law that a small model would over-fit the
# monoculture corpus, so a durable change must be a UNIVERSAL weakness, not corpus residue. Exposed
# as a module constant so the overseer meta-audit (tools/meta_audit_run.py) reuses the SAME guardrail
# text directly (plan 2026-06-20-002 §2/§4 "reuse the over-fit prompt") — single source, no drift.
OVERFIT_GUARDRAIL = (
    "ANTI-OVER-FIT GUARDRAIL (the operating thesis forbids hardening on the monoculture): do NOT "
    "propose hardening against monoculture-specific coder bugs, exact coach-round counts, or one "
    "contract's constraint tension. When a finding only makes sense for THIS corpus, tag it "
    "build-specific and say so. Frontier judgment is required precisely because a small local "
    "model would over-fit here.\n"
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------
# Codex invocation (mirror codex_review.run_codex). Raises on any failure -> caller fails safe.
# --------------------------------------------------------------------------------------------
def run_codex(prompt: str, effort: str = CODEX_EFFORT, timeout: int = CODEX_TIMEOUT,
              model: str | None = None) -> dict:
    """Invoke ``codex exec`` headless with the strict candidate output-schema. Raises RuntimeError
    on nonzero exit / timeout / missing output. stdin is DEVNULL (CLAUDE.md: codex exec hangs on
    stdin otherwise)."""
    with tempfile.TemporaryDirectory(prefix="improvement_codex_") as td:
        schema_path = Path(td) / "schema.json"
        out_path = Path(td) / "candidates.json"
        schema_path.write_text(json.dumps(_load_schema()))
        cmd = [
            "codex", "exec", "--skip-git-repo-check", "--ephemeral",
            "-s", "read-only",
            "--output-schema", str(schema_path),
            "-o", str(out_path),
            "-c", f'model_reasoning_effort="{effort}"',
        ]
        if model:
            cmd += ["-c", f'model="{model}"']
        cmd.append(prompt)
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"codex exec exit {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}"
            )
        if not out_path.exists():
            raise RuntimeError("codex produced no candidates file")
        return json.loads(out_path.read_text())


# --------------------------------------------------------------------------------------------
# Independent re-validation of the Codex result (schema-valid != trusted).
# --------------------------------------------------------------------------------------------
def validate_candidates(obj, valid_class_ids: set | None = None) -> tuple[bool, str | None]:
    """Independent re-validation (schema-valid != trusted; doer != certifier binds Codex too). When
    ``valid_class_ids`` is given, every candidate's source_class_ids must exist in THIS run's classes
    — Codex cannot cite a fabricated/unknown class (recurrence is tool-rendered from real classes)."""
    if not isinstance(obj, dict):
        return False, "candidates output is not a JSON object"
    # Match the strict schema's additionalProperties:false — the independent recheck must not be
    # looser than the contract it declares (schema-valid != trusted; Codex code-review MED #2).
    if set(obj.keys()) != {"candidates"}:
        return False, f"unexpected top-level keys: {sorted(set(obj) - {'candidates'})}"
    cands = obj.get("candidates")
    if not isinstance(cands, list):
        return False, "missing 'candidates' list"
    for i, c in enumerate(cands):
        if not isinstance(c, dict):
            return False, f"candidate {i} is not an object"
        extra = set(c.keys()) - set(_REQUIRED)
        if extra:
            return False, f"candidate {i} has unexpected keys: {sorted(extra)}"
        for k in _REQUIRED_STR:
            if k not in c:
                return False, f"candidate {i} missing required field '{k}'"
            if not isinstance(c[k], str):
                return False, f"candidate {i} field '{k}' is not a string"
        if c["tag"] not in _TAGS:
            return False, f"candidate {i} has invalid tag {c['tag']!r}"
        if c["confidence"] not in _CONF:
            return False, f"candidate {i} has invalid confidence {c['confidence']!r}"
        scids = c.get("source_class_ids")
        if not isinstance(scids, list) or not scids:
            return False, f"candidate {i} 'source_class_ids' must be a non-empty list"
        for cid in scids:
            if not isinstance(cid, str):
                return False, f"candidate {i} has a non-string source_class_id"
            if valid_class_ids is not None and cid not in valid_class_ids:
                return False, f"candidate {i} cites unknown source_class_id {cid!r}"
        if len(scids) != len(set(scids)):
            return False, f"candidate {i} has duplicate source_class_ids"
    return True, None


# --------------------------------------------------------------------------------------------
# Prompt — anti-over-fit guardrail + survivors fenced as DATA.
# --------------------------------------------------------------------------------------------
def _class_block_for_codex(classes: list, already_logged: set) -> str:
    """Enumerate the recurrence classes for Codex: class_id + tool-computed recurrence (count across
    builds/days) + a fenced sample reason + the advisory already-logged flag. Counts are tool facts;
    event-controlled text is fenced via _safe."""
    lines = []
    for c in classes:
        sample = _safe(c.sample_reasons[0]) if c.sample_reasons else '""'
        logged = " [already logged]" if c.class_id in already_logged else ""
        lines.append(
            f"- class_id={c.class_id} event={_safe(c.event)} recurrence: ×{c.count} across "
            f"{len(c.build_ids)} build(s)/{c.day_count} day(s); sample={sample}{logged}"
        )
    return "\n".join(lines)


def build_prompt(date: str, classes: list, already_logged: set | None = None) -> str:
    already_logged = already_logged or set()
    return (
        f"You are the Tier-3 pass of the ATB improvement feeder for {date}. Reason STATICALLY — "
        "your read-only sandbox cannot run pytest; do NOT execute tests or code.\n\n"
        "TASK: comb the system-stress RECURRENCE CLASSES below and DRAFT candidate IMPROVEMENT_LOG.md "
        "entries — learnings about ATB-THE-SYSTEM (gates, pipeline, trust spine, orchestration, "
        "plumbing), NOT about any build's own code (that is the diff-gate + coach loop's job). The "
        "build is the instrument; the system is the target. You DRAFT only — the overseer + Korab "
        "certify.\n\n"
        "THE ONE JUDGMENT THAT IS YOURS — tag every candidate:\n"
        "  - 'universal' = recurs across build TYPES, independent of the specific contract -> worth "
        "building into the scaffolding/gates.\n"
        "  - 'build-specific' = an artifact of ONE contract or of the current pure-function "
        "monoculture corpus -> must NOT be baked into the system.\n"
        + OVERFIT_GUARDRAIL
        + "RECURRENCE IS EVIDENCE, NOT PROOF: each class carries a deterministic recurrence count "
        "(occurrences across builds/days). A class recurring across many builds/days is SUGGESTIVE of "
        "a systemic/universal weakness — but it is EVIDENCE for your judgment, never an automatic "
        "verdict. 'across builds' is NOT 'across build TYPES' (v1 carries job_ids, not build-type "
        "metadata), and today's corpus is a monoculture — do NOT mechanically map a high count to "
        "'universal'. A class flagged '[already logged]' overlaps an existing IL finding; prefer not "
        "to re-draft it unless you have a genuinely new angle.\n\n"
        "CITATION: every candidate MUST cite the class_ids it draws on in 'source_class_ids' (>=1 "
        "real class_id from below). The recurrence numbers are TOOL-rendered from those classes — do "
        "NOT state occurrence counts in prose; cite the classes and let the tool render the verified "
        "count.\n\n"
        "OUTPUT: JSON matching the enforced schema — candidates[] each with "
        "{finding, tag, rationale, proposed_fix, where_it_lands, confidence, source_class_ids}. "
        "An empty candidates list is valid if nothing rises to a real system improvement.\n\n"
        f"{_as_data('STRESS RECURRENCE CLASSES (survivors of Tier-1 harvest + Tier-2 dedup)', _class_block_for_codex(classes, already_logged))}"
    )


# --------------------------------------------------------------------------------------------
# Renderers (TOTAL).
# --------------------------------------------------------------------------------------------
_UNVERIFIED = "**UNVERIFIED — overseer to certify before this enters IMPROVEMENT_LOG.md.**"


def _source_signals_section(survivor_ids: list[str]) -> list[str]:
    """A ready-to-paste certified-signals comment listing the reviewed survivor ids — so the
    certifier can suppress re-surfacing without cross-referencing the Tier-1/Tier-2 digests
    (Codex code-review LOW #3)."""
    return [
        "### Source signals reviewed",
        "Paste the ids of the signals you certify into a `<!-- certified-signals: ... -->` comment "
        "in IMPROVEMENT_LOG.md so the feeder stops re-surfacing them. All survivors reviewed:",
        "",
        f"<!-- certified-signals: {' '.join(survivor_ids)} -->",
        "",
    ]


def _recurrence_line(candidate: dict, by_class: dict) -> str:
    """TOOL-rendered recurrence for a candidate: look up each cited source_class_id in the
    deterministic class records and render its verified count — NEVER read a number from Codex prose
    (Codex U4: recurrence is a tool-rendered fact, not model-authored)."""
    parts = []
    for cid in candidate.get("source_class_ids", []):
        c = by_class.get(cid)
        if c is not None:
            parts.append(f"class `{cid}` ×{c.count} across {len(c.build_ids)} build(s)/{c.day_count} day(s)")
    return "; ".join(parts) if parts else "(no cited classes)"


def render_candidates(date: str, candidates: list[dict], survivor_ids: list[str], classes: list) -> str:
    by_class = {c.class_id: c for c in classes}
    lines = [f"# Improvement — Tier-3 candidate entries — {date}", "", _UNVERIFIED, ""]
    if not candidates:
        lines.append("_Codex combed the survivors and drafted no candidate system improvements._")
        lines.append("")
        lines.extend(_source_signals_section(survivor_ids))
        return "\n".join(lines).rstrip("\n") + "\n"
    lines.append(f"{len(candidates)} candidate(s) drafted:")
    lines.append("")
    for i, c in enumerate(candidates, 1):
        lines.append(f"## {i}. {_safe(c.get('finding'))} — `{c.get('tag')}` ({c.get('confidence')})")
        lines.append(f"- **Rationale:** {_safe(c.get('rationale'))}")
        lines.append(f"- **Proposed fix:** {_safe(c.get('proposed_fix'))}")
        lines.append(f"- **Where it lands:** {_safe(c.get('where_it_lands'))}")
        lines.append(f"- **Recurrence (tool-verified):** {_recurrence_line(c, by_class)}")
        lines.append("")
    lines.extend(_source_signals_section(survivor_ids))
    lines.append("---")
    lines.append(
        "_Tier-3 Codex draft. tag = the universal-vs-build-specific call (frontier-only); recurrence "
        "is tool-rendered from the cited classes. Certify into IMPROVEMENT_LOG.md only after overseer "
        "review._"
    )
    return "\n".join(lines).rstrip("\n") + "\n"


def render_failed(date: str, survivors: list[dict], reason: str) -> str:
    sr = HarvestResult(
        date=date, signals=survivors,
        status="build-day" if any(s.get("job_id") for s in survivors) else "no-build",
    )
    head = [
        f"# Improvement — Tier-3 FAIL-SAFE — {date}", "",
        f"> ⚠ The Codex draft pass failed; NO candidates were fabricated. Reason: {_safe(reason)}",
        f"> {len(survivors)} survivor signal(s) escalated UNCLUSTERED for overseer review below.", "",
    ]
    return "\n".join(head) + ih.render_digest(sr)


# --------------------------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------------------------
@dataclass
class Tier3Result:
    date: str
    status: str  # "no-survivors" | "drafted" | "failed-safe"
    candidates: list[dict] = field(default_factory=list)
    artifact: Path | None = None
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return {"no-survivors": 0, "drafted": 0, "failed-safe": 1}.get(self.status, 1)


def _fail_safe(date, survivors, digest_dir, events_path, reason, *, label=None) -> Tier3Result:
    """Write the fail-safe artifact FIRST, then emit the failed event (only after it exists).
    Fabricates no candidates. ``label`` overrides the filename base (windowed stem); the event keeps
    the clean ``date``."""
    digest_dir = Path(digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{(label or date)}-tier3-FAILED.md"
    path.write_text(render_failed(date, survivors, reason), encoding="utf-8")
    emit_event(
        {"event": "improvement-candidates-failed", "date": date,
         "survivors": len(survivors), "reason": reason[:300]},
        events_path,
    )
    return Tier3Result(date=date, status="failed-safe", artifact=path, reason=reason)


def run_tier3(
    date: str,
    survivors: list[dict],
    *,
    classes: list | None = None,
    already_logged: set | None = None,
    runner=None,
    digest_dir: Path = DIGEST_DIR,
    events_path: Path = EVENTS,
    stem: str | None = None,
    effort: str = CODEX_EFFORT,
    timeout: int = CODEX_TIMEOUT,
    model: str | None = None,
) -> Tier3Result:
    """Comb the survivor recurrence CLASSES with Codex and draft candidate entries. Fail-safe; fires
    only on survivors. ``classes``/``already_logged`` come from Tier-2; if ``classes`` is omitted they
    are derived deterministically from the survivors (ih.aggregate_classes) so the call stays usable
    standalone. Candidates must cite real source_class_ids; recurrence is tool-rendered. ``stem``
    overrides the artifact filename base (the sweep passes a windowed stem); default = date (v1)."""
    # Cost control (Korab's "don't pass to Codex every time"): no survivors -> no Codex call.
    if not survivors:
        return Tier3Result(date=date, status="no-survivors")
    if classes is None:
        classes = ih.aggregate_classes(survivors)
    already_logged = already_logged or set()
    label = stem or date
    valid_class_ids = {c.class_id for c in classes}

    runner = runner or (lambda prompt: run_codex(prompt, effort, timeout, model))
    prompt = build_prompt(date, classes, already_logged)
    try:
        raw = runner(prompt)
    except Exception as exc:  # noqa: BLE001 — any Codex fault -> fail safe, never fabricate
        return _fail_safe(date, survivors, digest_dir, events_path, f"codex error: {exc}", label=label)

    # Independent re-validation — schema-valid != trusted (doer != certifier binds Codex too); also
    # rejects any candidate citing a source_class_id that is not a real class from this run.
    ok, err = validate_candidates(raw, valid_class_ids)
    if not ok:
        return _fail_safe(date, survivors, digest_dir, events_path, f"invalid candidates: {err}", label=label)

    candidates = raw["candidates"]
    survivor_ids = [s.get("signal_id") for s in survivors if s.get("signal_id")]
    Path(digest_dir).mkdir(parents=True, exist_ok=True)
    path = Path(digest_dir) / f"{label}-candidates.md"
    path.write_text(render_candidates(date, candidates, survivor_ids, classes), encoding="utf-8")
    emit_event(
        {"event": "improvement-candidates", "date": date, "candidates": len(candidates)},
        events_path,
    )
    return Tier3Result(date=date, status="drafted", candidates=candidates, artifact=path)


if __name__ == "__main__":
    # Tier-3 is normally driven by the sweep (U5); no standalone CLI for v1.
    raise SystemExit("run via tools.improvement_sweep")
