"""Tests for tools/decision_record_check.py — the completeness check that makes the charter bite.

The pure core (path classification + reconcile) is covered by --self-test, re-run here. The
integration tests stand up a REAL temporary profiles git repo + records dir and prove BOTH
bite directions end to end: an unrecorded agent-layer commit is HELD; a fully-bound one passes;
backups and non-layer files are ignored; a dangling record is caught.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools import decision_record_check as drc


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return cp.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "profiles"
    (r / "coder").mkdir(parents=True)
    _git(r.parent, "init", "-q", "profiles")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "coder" / "SOUL.md").write_text("v1\n")
    (r / "README.md").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _records_dir(tmp_path: Path) -> Path:
    d = tmp_path / "records"
    d.mkdir()
    return d


def _commit(repo: Path, relpath: str, content: str, msg: str) -> str:
    (repo / relpath).parent.mkdir(parents=True, exist_ok=True)
    (repo / relpath).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


def _valid_record(rid: str, decision="layer-rewrite", status="proposed",
                  commit_hash=None, layer="identity", expiry=None) -> dict:
    """A fully schema-valid overseer-decision-record (load_records validates against the schema)."""
    rec = {
        "id": rid,
        "created_ts": "2026-06-20T12:00:00Z",
        "target_agent": "coder",
        "grounding_pack_id": "gp-0123456789abcdef",
        "memory_refs": ["project_atb_operating_thesis"],
        "log_refs": [],
        "detector_source_refs": [],
        "layer_considered": layer,
        "decision": decision,
        "certifier": "codex+korab",
        "status": status,
        "summary": "test record",
    }
    if commit_hash is not None:
        rec["commit_hash"] = commit_hash
    if expiry is not None:
        rec["expiry"] = expiry
    return rec


def _write_record(records_dir: Path, rid: str, filename: str | None = None, **kw):
    rec = _valid_record(rid, **kw)
    (records_dir / (filename or f"{rid}.json")).write_text(json.dumps(rec))
    return rec


def test_self_test_passes():
    assert drc._self_test() == 0


def test_orphan_layer_commit_is_held(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "coder/SOUL.md", "v2 — promoted without a record\n", "feat(coder): tweak SOUL")
    res = drc.run_check(repo, base, _records_dir(tmp_path))
    assert res["ok"] is False
    assert len(res["orphans"]) == 1
    assert "no Decision-Record trailer" in res["orphans"][0]["reason"]


def test_fully_bound_layer_commit_passes(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    records = _records_dir(tmp_path)
    rid = "odr-20260620T120000Z-coder-x"
    # Commit the layer change WITH the binding trailer.
    (repo / "coder" / "SOUL.md").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"feat(coder): SOUL\n\nDecision-Record: {rid}")
    sha = _git(repo, "rev-parse", "HEAD")
    # Record points back to that commit.
    _write_record(records, rid, status="certified", commit_hash=sha)
    res = drc.run_check(repo, base, records)
    assert res["ok"] is True, res
    assert res["checked_commits"] == 1


def test_trailer_to_missing_record_is_held(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "coder" / "SOUL.md").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat(coder): SOUL\n\nDecision-Record: odr-ghost")
    res = drc.run_check(repo, base, _records_dir(tmp_path))
    assert res["ok"] is False
    assert "has no matching decision-record" in res["orphans"][0]["reason"]


def test_record_not_pointing_back_is_held(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    records = _records_dir(tmp_path)
    rid = "odr-x"
    (repo / "coder" / "SOUL.md").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"feat(coder): SOUL\n\nDecision-Record: {rid}")
    # A schema-valid 'proposed' layer-rewrite legitimately omits commit_hash; once a commit
    # links to it, the missing back-link is the violation reconcile must catch.
    _write_record(records, rid, status="proposed")  # no commit_hash
    res = drc.run_check(repo, base, records)
    assert res["ok"] is False
    assert "does not cite this commit" in res["orphans"][0]["reason"]


def test_backups_and_non_layer_files_are_ignored(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    # A SOUL backup + a log file — neither is a durable agent-layer change.
    _commit(repo, "coder/SOUL.md.prebak.20260620T0Z", "old\n", "chore: backup SOUL")
    _commit(repo, "coder/logs/run.log", "noise\n", "chore: log")
    res = drc.run_check(repo, base, _records_dir(tmp_path))
    assert res["ok"] is True
    assert res["checked_commits"] == 0


def test_skills_subtree_change_requires_a_record(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "coder/skills/new-skill/SKILL.md", "skill\n", "feat(coder): add skill")
    res = drc.run_check(repo, base, _records_dir(tmp_path))
    assert res["ok"] is False
    assert res["checked_commits"] == 1


def test_dangling_record_cites_nonexistent_commit(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    records = _records_dir(tmp_path)
    _write_record(records, "odr-dangle", status="certified", commit_hash="0" * 40)
    res = drc.run_check(repo, base, records)
    assert res["ok"] is False
    assert len(res["dangling"]) == 1
    assert "does not exist" in res["dangling"][0]["reason"]


def test_invalid_record_file_is_surfaced(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    records = _records_dir(tmp_path)
    (records / "broken.json").write_text("{not json")
    res = drc.run_check(repo, base, records)
    assert res["ok"] is False
    assert len(res["invalid_records"]) == 1


def test_merge_commit_touching_layer_is_surfaced_and_held(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    main = _git(repo, "branch", "--show-current")
    # A feature branch changes the SOUL; main moves a non-layer file; merge --no-ff.
    _git(repo, "checkout", "-qb", "feat")
    _commit(repo, "coder/SOUL.md", "from feat\n", "feat(coder): SOUL on branch")
    _git(repo, "checkout", "-q", main)
    _commit(repo, "README.md", "main moved\n", "docs: readme")
    _git(repo, "merge", "--no-ff", "-q", "-m", "merge feat", "feat")
    merge_sha = _git(repo, "rev-parse", "HEAD")
    # The fix: -m surfaces the merge's layer change (plain diff-tree would show NOTHING here).
    assert "coder/SOUL.md" in drc.changed_files_of(repo, merge_sha)
    res = drc.run_check(repo, base, _records_dir(tmp_path))
    assert res["ok"] is False
    assert res["checked_commits"] >= 1


def test_rename_soul_out_is_caught(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "coder/SOUL.md", "coder/SOUL_archived.md")
    _git(repo, "commit", "-qm", "chore(coder): archive SOUL")
    res = drc.run_check(repo, base, _records_dir(tmp_path))
    assert res["ok"] is False  # the deleted-side path coder/SOUL.md is a layer path
    assert res["checked_commits"] == 1


def test_move_into_layer_path_is_caught(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "main").mkdir()  # git mv won't create the destination dir
    _git(repo, "mv", "README.md", "main/SOUL.md")  # non-layer -> a new agent's SOUL
    _git(repo, "commit", "-qm", "chore: turn readme into a SOUL")
    res = drc.run_check(repo, base, _records_dir(tmp_path))
    assert res["ok"] is False  # the added-side path main/SOUL.md is a layer path
    assert res["checked_commits"] == 1


def test_delete_soul_is_caught(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "coder/SOUL.md")
    _git(repo, "commit", "-qm", "chore(coder): remove SOUL")
    res = drc.run_check(repo, base, _records_dir(tmp_path))
    assert res["ok"] is False
    assert res["checked_commits"] == 1


def test_duplicate_record_ids_are_held_and_untrusted(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    records = _records_dir(tmp_path)
    _write_record(records, "odr-dup", filename="a.json")
    _write_record(records, "odr-dup", filename="b.json")
    # And a real layer commit links to the duplicated id.
    (repo / "coder" / "SOUL.md").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat(coder): SOUL\n\nDecision-Record: odr-dup")
    res = drc.run_check(repo, base, records)
    assert res["ok"] is False
    assert any("duplicate id" in iv["reason"] for iv in res["invalid_records"])
    # Fail-closed: the duplicated id is untrusted, so the commit linking to it is an orphan.
    assert any(o["reason"].startswith("trailer 'odr-dup' has no matching") for o in res["orphans"])


def test_proposed_record_citing_a_commit_is_held(repo: Path, tmp_path: Path):
    # An uncertified (proposed) record must not be able to back a real layer commit. The schema
    # forbids the shape (proposed layer-rewrite + commit_hash), so the record fails to load and
    # the linking commit is left orphaned — fail-closed in both directions.
    base = _git(repo, "rev-parse", "HEAD")
    records = _records_dir(tmp_path)
    rid = "odr-proposed-cite"
    (records / f"{rid}.json").write_text(json.dumps(
        _valid_record(rid, status="proposed", commit_hash="a" * 40)))
    (repo / "coder" / "SOUL.md").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"feat(coder): SOUL\n\nDecision-Record: {rid}")
    res = drc.run_check(repo, base, records)
    assert res["ok"] is False
    assert any("schema-invalid" in iv["reason"] for iv in res["invalid_records"])
    assert any(rid in o["reason"] for o in res["orphans"])


def test_schema_invalid_record_is_held(repo: Path, tmp_path: Path):
    records = _records_dir(tmp_path)
    bad = _valid_record("odr-bad")
    del bad["grounding_pack_id"]  # drop a required field
    (records / "odr-bad.json").write_text(json.dumps(bad))
    res = drc.run_check(repo, _git(repo, "rev-parse", "HEAD"), records)
    assert res["ok"] is False
    assert any("schema-invalid" in iv["reason"] for iv in res["invalid_records"])


def test_layer_rewrite_record_citing_non_layer_commit_is_dangling(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD")
    sha = _commit(repo, "README.md", "changed\n", "docs: readme only (non-layer)")
    records = _records_dir(tmp_path)
    _write_record(records, "odr-mis", status="certified", commit_hash=sha)
    res = drc.run_check(repo, base, records)
    assert res["ok"] is False
    assert any("not an agent-layer change" in d["reason"] for d in res["dangling"])


def test_changed_files_of_handles_root_commit(repo: Path):
    root = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    files = drc.changed_files_of(repo, root)
    assert "coder/SOUL.md" in files
    assert "README.md" in files


class TestSchemaConditionals:
    """The schema's conditional invariants must bite (Codex finding #3)."""

    def _errs(self, rec):
        return list(drc._record_validator().iter_errors(rec))

    def test_good_certified_record_validates(self):
        assert self._errs(_valid_record("odr-x", status="certified", commit_hash="a" * 40)) == []

    def test_good_proposed_record_validates(self):
        assert self._errs(_valid_record("odr-x")) == []  # proposed, no commit_hash

    def test_applied_layer_rewrite_requires_commit_hash(self):
        assert self._errs(_valid_record("odr-x", status="applied")) != []

    def test_no_change_must_not_cite_commit(self):
        assert self._errs(_valid_record("odr-x", decision="no_change", commit_hash="a" * 40)) != []

    def test_defer_requires_expiry(self):
        assert self._errs(_valid_record("odr-x", decision="defer", layer="none")) != []

    def test_defer_with_expiry_validates(self):
        assert self._errs(_valid_record("odr-x", decision="defer", layer="none", expiry="2026-12-31")) == []

    def test_layer_none_only_for_non_rewrite(self):
        bad = _valid_record("odr-x", status="certified", commit_hash="a" * 40, layer="none")
        assert self._errs(bad) != []

    def test_proposed_layer_rewrite_must_not_cite_commit(self):
        assert self._errs(_valid_record("odr-x", status="proposed", commit_hash="a" * 40)) != []

    def test_reverted_layer_rewrite_must_not_cite_commit(self):
        assert self._errs(_valid_record("odr-x", status="reverted", commit_hash="a" * 40)) != []

    def test_bad_id_pattern_rejected(self):
        assert self._errs(_valid_record("not-an-odr-id")) != []

    def test_bad_commit_hash_pattern_rejected(self):
        assert self._errs(_valid_record("odr-x", status="certified", commit_hash="ZZZ")) != []


def test_clean_empty_range_passes(repo: Path, tmp_path: Path):
    head = _git(repo, "rev-parse", "HEAD")
    res = drc.run_check(repo, head, _records_dir(tmp_path))  # since==HEAD -> empty
    assert res["ok"] is True
    assert res["checked_commits"] == 0


def test_bad_since_ref_fails_safe(repo: Path, tmp_path: Path):
    rc = drc.main(["--since", "nonexistent-ref", "--profiles-repo", str(repo),
                   "--records-dir", str(_records_dir(tmp_path))])
    assert rc == 1  # fail-safe, never a clean pass
