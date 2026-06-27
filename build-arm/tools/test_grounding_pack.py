"""Tests for tools/grounding_pack.py — the overseer grounding-pack assembler.

Covers the pure id core (reproducible / agent- & evidence-sensitive) and the file-IO
collection path (sections present, missing optional sources flagged not fatal, absent agent
SOUL is a usage error).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import grounding_pack as gp


def _make_profiles(root: Path, agent: str, soul: str = "soul text", with_prebak: bool = True):
    adir = root / agent
    (adir / "skills" / "writing-tests").mkdir(parents=True)
    (adir / "skills" / "debugging").mkdir(parents=True)
    (adir / "SOUL.md").write_text(soul)
    (adir / "config.yaml").write_text("model: x\n")
    if with_prebak:
        (adir / "SOUL.md.prebak.20260101T000000Z").write_text("old soul")
    return root


def _make_memory(root: Path, include=("project_atb_operating_thesis.md",
                                      "project_atb_idea_digest_probe.md",
                                      "project_atb_skill_curation_plan.md")):
    root.mkdir(parents=True, exist_ok=True)
    for slug in include:
        (root / slug).write_text(f"content of {slug}")
    return root


def test_self_test_passes():
    assert gp._self_test() == 0


def test_build_pack_is_reproducible_and_sensitive():
    base = {
        "agent": "coder",
        "sections": {"x": {"content": "a"}},
        "evidence": ["e1"],
        "missing": [],
    }
    id1, pack = gp.build_pack(base)
    id2, _ = gp.build_pack(json.loads(json.dumps(base)))
    assert id1 == id2
    assert id1.startswith("gp-")
    assert pack["grounding_pack_id"] == id1
    assert gp.build_pack({**base, "agent": "qa"})[0] != id1
    assert gp.build_pack({**base, "evidence": ["e2"]})[0] != id1
    # `missing` is content too — a different gap => a different id.
    assert gp.build_pack({**base, "missing": ["over_fit_guard"]})[0] != id1


def test_collect_sources_full_bundle(tmp_path: Path):
    profiles = _make_profiles(tmp_path / "profiles", "coder")
    memory = _make_memory(tmp_path / "memory")
    src = gp.collect_sources("coder", memory_dir=memory, profiles_dir=profiles, evidence=["coach:j1:r2"])

    assert src["agent"] == "coder"
    assert src["missing"] == []
    for role in ("over_fit_guard", "identity_first_law", "corpus_state"):
        assert src["sections"][role]["present"] is True
        assert src["sections"][role]["content"]
    ci = src["sections"]["current_identity"]
    assert ci["content"] == "soul text"
    assert ci["skills_enabled_dirs"] == ["debugging", "writing-tests"]
    assert any("SOUL.md.prebak" in n for n in src["sections"]["prior_promotions"]["trail"])
    assert src["evidence"] == ["coach:j1:r2"]


def test_missing_memory_is_flagged_not_fatal(tmp_path: Path):
    profiles = _make_profiles(tmp_path / "profiles", "coder")
    # Only one of the three memory files present.
    memory = _make_memory(tmp_path / "memory", include=("project_atb_operating_thesis.md",))
    src = gp.collect_sources("coder", memory_dir=memory, profiles_dir=profiles)
    assert "identity_first_law" in src["missing"]
    assert "corpus_state" in src["missing"]
    assert "over_fit_guard" not in src["missing"]
    # Still produces a pack.
    pack_id, _ = gp.build_pack(src)
    assert pack_id.startswith("gp-")


def test_absent_agent_soul_is_usage_error(tmp_path: Path):
    profiles = (tmp_path / "profiles")
    profiles.mkdir()
    memory = _make_memory(tmp_path / "memory")
    with pytest.raises(FileNotFoundError):
        gp.collect_sources("nope", memory_dir=memory, profiles_dir=profiles)


def test_write_pack_roundtrip(tmp_path: Path):
    profiles = _make_profiles(tmp_path / "profiles", "coder")
    memory = _make_memory(tmp_path / "memory")
    src = gp.collect_sources("coder", memory_dir=memory, profiles_dir=profiles)
    _, pack = gp.build_pack(src)
    out = gp.write_pack(pack, tmp_path / "packs")
    assert out.exists()
    written = json.loads(out.read_text())
    assert written["grounding_pack_id"] == pack["grounding_pack_id"]
    assert "created_ts" in written  # stamped outside the hashed content
