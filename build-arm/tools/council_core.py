#!/usr/bin/env python3
"""Cross-family seat-execution core — shared by the idea-vetting council and the meta-audit.

WHY THIS EXISTS (meta-layer Phase 3): the council (`council_run.py`) and the verify-the-verifier
meta-audit (`meta_audit_run.py`) both need the SAME hard part — fan a question out to N frontier
seats spread across model FAMILIES (Anthropic / OpenAI / Google), fail each seat closed to an
abstention, and guard the multi-family `agy` CLI so a Google seat can never silently vote as a
non-Google model. Duplicating that machinery would risk drift on the security-critical bits (the
env scrub, the model guard, the read-only flags), so it lives here ONCE. The two callers supply
their own seats, prompts, vote-shapes, and SYNTHESIS — the core only runs seats and offers the
synthesis primitives; it never decides a verdict.

DEPENDENCY-INJECTED ON PURPOSE: the patchable knobs a caller's tests monkeypatch (the model
lister, the pinned Gemini model name) are NOT core globals — they are passed IN. So a caller keeps
its own module-level `GEMINI_MODEL` / `default_model_lister` and threads them through `run_seats`,
and its existing test monkeypatches keep biting. The core stays a pure mechanism.

Trust shape: seats run read-only (codex -s read-only, agy --sandbox, claude --permission-mode
plan) from an empty scratch cwd with TMPDIR jailed and stdin closed; this is NOT a filesystem
sandbox (the CLIs keep HOME for their own auth). A seat that times out, errors, can't be launched,
or whose Google-family guard fails ABSTAINS — it never crashes the panel and never silently counts.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SEAT_TIMEOUT = 600  # seconds per advisor; frontier CLIs at high effort are slow


@dataclass(frozen=True)
class Seat:
    family: str  # CLI/model-family selector + binary picker: claude | codex | gemini
    slug: str  # filesystem-safe unique id (two personas can share a family, not a slug)
    persona: str  # display name
    brief: str  # the lens this seat judges through
    advocate: bool  # assigned a POSTURE — an honesty caveat is offered in the prompt

    @property
    def label(self) -> str:
        return f"{self.family}/{self.persona}"


@dataclass
class SeatResult:
    seat: Seat
    vote: dict | None  # the caller-parsed vote, or None for an abstention
    abstain_reason: str | None
    raw: str


def seat_cmd(family: str, prompt: str, *, gemini_model: str, seat_timeout: int = SEAT_TIMEOUT) -> list[str]:
    """Build one advisor CLI invocation. The Google seat runs on the multi-family `agy` CLI
    (successor to the sunset `gemini` CLI) pinned to ``gemini_model`` (the family guard verifies
    the pin); --sandbox restricts it; --print-timeout ends it gracefully before the outer kill."""
    if family == "codex":
        return [
            "codex", "exec", "-s", "read-only", "--skip-git-repo-check",
            "-c", 'model_reasoning_effort="high"', prompt,
        ]
    if family == "gemini":
        return [
            "agy", "-p", prompt, "--model", gemini_model,
            "--sandbox", "--print-timeout", f"{seat_timeout - 60}s",
        ]
    if family == "claude":
        return ["claude", "-p", "--permission-mode", "plan", prompt]
    raise ValueError(f"unknown seat family: {family}")


def seat_env(family: str) -> dict[str, str]:
    """The claude seat must NOT inherit this process's session plumbing (a parent Claude Code
    session exports ANTHROPIC_BASE_URL etc. that 401 a child CLI). Others pass through."""
    env = dict(os.environ)
    if family == "claude":
        env = {k: v for k, v in env.items() if not k.startswith(("ANTHROPIC", "CLAUDE"))}
    return env


def default_runner(cmd: list[str], *, env: dict, cwd: str, timeout: int):
    """Run one advisor CLI: stdin CLOSED (codex exec hangs reading stdin otherwise), empty scratch
    cwd (no repo context bleeds in). Returns (rc, out, err); rc<0: -1 timeout, -2 binary missing."""
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env=env, cwd=cwd, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return -1, (exc.stdout or ""), (exc.stderr or "")
    except FileNotFoundError:
        return -2, "", f"binary not found: {cmd[0]}"


def default_model_lister() -> list[str] | None:
    """Return the model display-names this `agy` build offers (one per line of `agy models`), or
    None if the list can't be obtained. None means 'cannot prove' -> the guard fails closed."""
    try:
        proc = subprocess.run(
            ["agy", "models"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def gemini_model_ok(model_lister, *, gemini_model: str) -> str | None:
    """Fail-closed family guard for the Google seat. `agy` bundles several model families (Gemini
    AND Claude AND GPT-OSS), so a build that renames/drops the pinned Gemini model could silently
    fall back to a NON-Google model while the seat is still counted as 'gemini' — collapsing
    cross-family independence. Return an abstain-reason if the guard fails, else None: the pinned
    model must be a Gemini-family name AND be currently offered by agy."""
    if not gemini_model.startswith("Gemini "):  # trailing space: reject e.g. "GeminiX"
        return f"model guard: pinned model {gemini_model!r} is not Gemini-family"
    try:
        available = model_lister()
    except Exception:  # noqa: BLE001 — the lister is an injected seam; a raising lister must fail
        # CLOSED (abstain), never crash the panel — run_seats computes this guard OUTSIDE the
        # per-seat catch-all, so an uncaught raise here would abort the whole council/audit.
        return "model guard: could not list agy models"
    if available is None:
        return "model guard: could not list agy models"
    if gemini_model not in available:
        return f"model guard: {gemini_model!r} not offered by this agy build"
    return None


def find_last_json(text: str, predicate):
    """Find the LAST JSON object in noisy CLI output that satisfies ``predicate``. Scans backwards
    from every '{' so banner lines, skill warnings and multi-line JSON all survive; an object the
    predicate rejects (wrong/missing keys, out-of-enum values) is skipped. Returns the dict or None.
    The caller's predicate defines what a valid vote looks like; the caller shapes the result."""
    dec = json.JSONDecoder()
    for i in range(len(text) - 1, -1, -1):
        if text[i] != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and predicate(obj):
            return obj
    return None


def severest(bands: list[str], order: tuple[str, ...]) -> str:
    """Most-severe band by ``order`` (least->most). The fail-closed gate value."""
    return max(bands, key=order.index)


def second_severest(bands: list[str], order: tuple[str, ...]) -> str:
    """2nd-most-severe band — an advisory consensus that resists a single seat spiking the risk."""
    ranked = sorted(bands, key=order.index, reverse=True)
    return ranked[1] if len(ranked) >= 2 else ranked[0]


def _attempt_seat(seat, *, prompt_for, parse_vote, out_dir, runner, gemini_model, seat_timeout, gemini_guard):
    """Run one seat. Each writes its own uniquely-named transcript to ``out_dir``."""
    # The Google seat runs on the multi-family agy CLI: refuse to count it unless the pinned model
    # is provably Gemini-family. Fail closed to ABSTAIN; the seat command never runs.
    if seat.family == "gemini" and gemini_guard:
        (out_dir / f"seat-{seat.slug}.txt").write_text(
            f"# model guard FAILED: {gemini_guard}\n", encoding="utf-8"
        )
        return SeatResult(seat, None, gemini_guard, gemini_guard)
    prompt = prompt_for(seat)
    with tempfile.TemporaryDirectory(prefix=f"council-{seat.slug}-") as scratch:
        env = seat_env(seat.family)
        env["TMPDIR"] = scratch  # seat temp files stay in the per-seat scratch
        rc, out, errtxt = runner(
            seat_cmd(seat.family, prompt, gemini_model=gemini_model, seat_timeout=seat_timeout),
            env=env, cwd=scratch, timeout=seat_timeout,
        )
    provenance = (
        f"# binary=agy model={gemini_model!r} (verified offered by agy models)\n"
        if seat.family == "gemini" else ""
    )
    raw = f"{provenance}# exit={rc}\n# --- stdout ---\n{out}\n# --- stderr ---\n{errtxt}\n"
    (out_dir / f"seat-{seat.slug}.txt").write_text(raw, encoding="utf-8")
    vote = parse_vote(out) if rc == 0 else None
    abstain = None
    if rc == -1:
        abstain = "timeout"
    elif rc == -2:
        abstain = "binary not found"
    elif rc != 0:
        abstain = f"exit {rc}"
    elif vote is None:
        abstain = "no parseable vote in output"
    return SeatResult(seat, vote, abstain, raw)


def _run_one_seat(seat, **kw) -> SeatResult:
    """Wrap _attempt_seat so ANY unexpected exception (custom-runner bug, parse/tempdir failure)
    becomes a fail-closed abstention — one seat can never crash the whole panel through pool.map."""
    try:
        return _attempt_seat(seat, **kw)
    except Exception as exc:  # noqa: BLE001 — deliberate fail-closed catch-all
        reason = f"seat error: {type(exc).__name__}"
        try:
            (kw["out_dir"] / f"seat-{seat.slug}.txt").write_text(
                f"# seat raised: {exc!r}\n", encoding="utf-8"
            )
        except OSError:
            pass
        return SeatResult(seat, None, reason, reason)


def run_seats(
    seats,
    *,
    prompt_for,
    parse_vote,
    out_dir: Path,
    runner=default_runner,
    model_lister=default_model_lister,
    gemini_model: str,
    seat_timeout: int = SEAT_TIMEOUT,
) -> list[SeatResult]:
    """Fan ``seats`` out concurrently and return their results in the FIXED ``seats`` order
    (so a caller's synthesis is deterministic regardless of completion order).

    prompt_for(seat) -> str : the per-seat prompt. parse_vote(stdout) -> dict|None : the caller's
    vote parser. The Google-seat model guard is computed ONCE (one `agy models` call). Seats are
    read-only and share no mutable state (each writes its own transcript), so concurrency is safe.
    Never raises: every seat resolves to a vote or an abstention.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    gemini_guard = gemini_model_ok(model_lister, gemini_model=gemini_model)
    with ThreadPoolExecutor(max_workers=max(1, len(seats))) as pool:
        return list(
            pool.map(
                lambda st: _run_one_seat(
                    st,
                    prompt_for=prompt_for,
                    parse_vote=parse_vote,
                    out_dir=out_dir,
                    runner=runner,
                    gemini_model=gemini_model,
                    seat_timeout=seat_timeout,
                    gemini_guard=gemini_guard,
                ),
                seats,
            )
        )
