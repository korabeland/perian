"""Tests for tools/council_core.py — the shared cross-family seat-execution core.

council_run's suite already exercises the core end-to-end through the live council (and passes
unchanged after the extraction — the zero-regression guard). These add DIRECT unit coverage of
the core's pieces in isolation: the JSON scan, the seat command/env builders, the fail-closed
Google model guard, the synthesis primitives, and run_seats' fan-out + abstention behavior.
"""

from __future__ import annotations

import threading

import pytest

from tools import council_core as cc

GEM = "Gemini 3.1 Pro (High)"
ORDER = ("low", "medium", "high", "critical")


def _seat(family, persona, advocate=False):
    return cc.Seat(family, f"{family}-{persona.lower()}", persona, "judge it", advocate)


SEATS = (
    _seat("claude", "A"), _seat("claude", "B"),
    _seat("codex", "C"), _seat("codex", "D"),
    _seat("gemini", "E"), _seat("gemini", "F"),
)


class TestFindLastJson:
    def test_finds_last_matching(self):
        text = '{"verdict":"X"}\nnoise\n{"verdict":"Y"}\ntokens'
        assert cc.find_last_json(text, lambda o: "verdict" in o)["verdict"] == "Y"

    def test_skips_nonmatching(self):
        text = '{"other":1}\n{"verdict":"Z"}\n{"nope":2}'
        assert cc.find_last_json(text, lambda o: "verdict" in o)["verdict"] == "Z"

    def test_multiline(self):
        text = 'banner\n{"verdict":\n "M",\n "r":[1]}\nafter'
        assert cc.find_last_json(text, lambda o: o.get("verdict") == "M")["r"] == [1]

    def test_none_when_no_match(self):
        assert cc.find_last_json("no json here", lambda o: True) is None

    def test_predicate_filters_out_of_enum(self):
        assert cc.find_last_json('{"verdict":"SHIP"}', lambda o: o.get("verdict") in ("OK",)) is None


class TestSeatCmd:
    def test_codex_prompt_last_and_readonly(self):
        cmd = cc.seat_cmd("codex", "PROMPT", gemini_model=GEM)
        assert cmd[-1] == "PROMPT" and "read-only" in cmd

    def test_agy_prompt_at_index_2_with_model_and_timeout(self):
        cmd = cc.seat_cmd("gemini", "PROMPT", gemini_model=GEM, seat_timeout=600)
        assert cmd[0] == "agy" and cmd[2] == "PROMPT"
        assert cmd[cmd.index("--model") + 1] == GEM
        assert cmd[cmd.index("--print-timeout") + 1] == "540s"
        assert "--sandbox" in cmd

    def test_claude_prompt_last_and_plan(self):
        cmd = cc.seat_cmd("claude", "PROMPT", gemini_model=GEM)
        assert cmd[-1] == "PROMPT" and "--permission-mode" in cmd

    def test_unknown_family_raises(self):
        with pytest.raises(ValueError):
            cc.seat_cmd("mistral", "P", gemini_model=GEM)


class TestSeatEnv:
    def test_claude_scrubs_session_plumbing(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "x")
        monkeypatch.setenv("CLAUDE_CODE_X", "y")
        env = cc.seat_env("claude")
        assert not any(k.startswith(("ANTHROPIC", "CLAUDE")) for k in env)

    def test_other_family_passes_through(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "x")
        assert "ANTHROPIC_BASE_URL" in cc.seat_env("codex")


class TestGeminiGuard:
    def test_ok_when_listed(self):
        assert cc.gemini_model_ok(lambda: [GEM], gemini_model=GEM) is None

    def test_abstain_when_not_listed(self):
        assert "not offered" in cc.gemini_model_ok(lambda: ["Other"], gemini_model=GEM)

    def test_abstain_when_list_unavailable(self):
        assert "could not list" in cc.gemini_model_ok(lambda: None, gemini_model=GEM)

    def test_abstain_when_not_gemini_family(self):
        assert "not Gemini-family" in cc.gemini_model_ok(lambda: ["Claude X"], gemini_model="Claude X")

    def test_abstain_when_lister_raises(self):
        def boom():
            raise RuntimeError("agy models blew up")

        assert "could not list" in cc.gemini_model_ok(boom, gemini_model=GEM)


class TestSeverity:
    def test_severest_is_max(self):
        assert cc.severest(["low", "high", "medium"], ORDER) == "high"

    def test_second_severest_resists_spike(self):
        assert cc.second_severest(["low", "critical", "low"], ORDER) == "low"

    def test_second_severest_single_element(self):
        assert cc.second_severest(["high"], ORDER) == "high"


class FakeRunner:
    """Answers per PERSONA (parsed from the prompt) and records every call. Thread-safe."""

    def __init__(self, by_persona):
        self.by_persona = by_persona
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, cmd, *, env, cwd, timeout):
        prompt = cmd[2] if cmd[0] == "agy" else cmd[-1]
        persona = prompt.split("::")[1]
        with self._lock:
            self.calls.append({"cmd": cmd, "persona": persona, "cwd": cwd, "env": env})
        ans = self.by_persona[persona]
        if isinstance(ans, BaseException):
            raise ans
        return ans


def _prompt_for(seat):
    return f"PROMPT::{seat.persona}::"


def _parse(out):
    return cc.find_last_json(out, lambda o: o.get("verdict") in ("SOUND", "FLAGGED"))


def _runner(**over):
    ok = (0, '{"verdict":"SOUND"}', "")
    ans = {s.persona: ok for s in SEATS}
    ans.update(over)
    return FakeRunner(ans)


def _run(tmp_path, runner, model_lister=lambda: [GEM]):
    res = cc.run_seats(
        SEATS, prompt_for=_prompt_for, parse_vote=_parse, out_dir=tmp_path,
        runner=runner, model_lister=model_lister, gemini_model=GEM,
    )
    return {x.seat.persona: x for x in res}, res


class TestRunSeats:
    def test_fans_out_all_seats_in_fixed_order(self, tmp_path):
        _, res = _run(tmp_path, _runner())
        assert [x.seat.persona for x in res] == [s.persona for s in SEATS]
        assert all(x.vote for x in res)

    def test_transcripts_written_per_seat(self, tmp_path):
        _run(tmp_path, _runner())
        for s in SEATS:
            assert (tmp_path / f"seat-{s.slug}.txt").exists()

    def test_negative_rc_abstains(self, tmp_path):
        by, _ = _run(tmp_path, _runner(C=(-1, "", ""), D=(-2, "", "nope")))
        assert by["C"].abstain_reason == "timeout"
        assert by["D"].abstain_reason == "binary not found"

    def test_gemini_guard_short_circuits_without_spawning_agy(self, tmp_path):
        r = _runner()
        by, _ = _run(tmp_path, r, model_lister=lambda: ["Other Model"])
        assert not any(c["cmd"][0] == "agy" for c in r.calls)  # guard fired before spawn
        for p in ("E", "F"):
            assert by[p].vote is None and "not offered" in by[p].abstain_reason

    def test_runner_exception_becomes_abstention(self, tmp_path):
        by, _ = _run(tmp_path, _runner(C=RuntimeError("boom")))
        assert by["C"].vote is None and "seat error: RuntimeError" in by["C"].abstain_reason

    def test_raising_model_lister_fails_closed(self, tmp_path):
        """A raising model_lister (an injected seam) must NOT abort the panel — the Google guard
        fails closed and only the gemini seats abstain; agy is never spawned (Codex Step-A r1)."""
        def boom():
            raise RuntimeError("agy models blew up")

        r = _runner()
        by, _ = _run(tmp_path, r, model_lister=boom)
        assert not any(c["cmd"][0] == "agy" for c in r.calls)
        for p in ("E", "F"):
            assert by[p].vote is None and "could not list" in by[p].abstain_reason
        for p in ("A", "B", "C", "D"):  # non-Google seats still voted — panel survived
            assert by[p].vote is not None

    def test_nonzero_exit_with_vote_abstains(self, tmp_path):
        by, _ = _run(tmp_path, _runner(C=(1, '{"verdict":"SOUND"}', "crash")))
        assert by["C"].vote is None and by["C"].abstain_reason == "exit 1"

    def test_unparseable_vote_abstains(self, tmp_path):
        by, _ = _run(tmp_path, _runner(C=(0, "no json", "")))
        assert by["C"].abstain_reason == "no parseable vote in output"

    def test_tmpdir_jailed_to_scratch(self, tmp_path):
        r = _runner()
        _run(tmp_path, r)
        for c in r.calls:
            assert c["env"]["TMPDIR"] == c["cwd"] and "council-" in c["cwd"]
