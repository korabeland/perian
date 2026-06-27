"""Behavior-preservation tests for tools/idea_digest.py.

These are build #4's render_brief + assemble_digest suites (state/builds/20260603-idea-digest/
test_idea_brief.py + test_idea_digest.py), ported VERBATIM with only the import repointed to
the graduated module. Re-running the original assertions against tools/idea_digest is the
proof that graduation preserved behavior — nothing in the renderer changed, only its home.
"""


from tools.idea_digest import (
    _decision_order,
    _is_valid_triple,
    _risk_key,
    assemble_digest,
    render_brief,
)


# ======================================================================================
# render_brief — ported from build #4 test_idea_brief.py
# ======================================================================================
def _make_idea_contract(**overrides):
    base = {
        "job_id": "test-job-001",
        "proposed_at": "2026-06-03T10:00:00Z",
        "what_should_exist": "A CLI tool that summarizes chain events",
        "who_benefits": "Operators monitoring the ATB pipeline",
        "why_now": "Recent latency spikes in event processing",
        "evidence_refs": ["events.jsonl", "runlog.py"],
        "out_of_scope": ["UI changes", "database migrations"],
        "where_it_lives": "~/.hermes/auto-think-build/tools/",
        "how_verifiable": "pytest -q exits 0 and output matches expected schema",
    }
    base.update(overrides)
    return base


def _make_research_input(**overrides):
    base = {
        "job_id": "test-job-001",
        "collected_at": "2026-06-03T11:00:00Z",
        "topic": "Event processing latency",
        "sources": [{"name": "logs", "ref": "/var/log/atb.log"}],
        "evidence": [
            {"claim": "Current processing takes 5s per batch", "source_ref": "/var/log/atb.log", "confidence": "high"},
            {"claim": "Swap events occur 12% of the time", "source_ref": "/var/log/atb.log", "confidence": "medium"},
        ],
        "watch_items": [{"observation": "Latency spikes correlate with large batches", "rationale": "batch_size > 100"}],
        "summary": "Processing latency is within acceptable bounds for small batches.",
    }
    base.update(overrides)
    return base


def _make_main_review(**overrides):
    base = {
        "job_id": "test-job-001",
        "reviewed_at": "2026-06-03T12:00:00Z",
        "decision": "approved_for_coder",
        "risk_band": "low",
        "risk_score": 2,
        "approved_by": "main",
    }
    base.update(overrides)
    return base


class TestRenderBriefSections:
    def test_first_section_is_definition_of_success(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "## Definition of success" in result

    def test_how_verifiable_applies_verbatim(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "pytest -q exits 0 and output matches expected schema" in result

    def test_problem_value_section_present(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "## Problem value" in result

    def test_evidence_strength_section_present(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "## Evidence strength" in result

    def test_feasibility_section_present(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "## Feasibility / fit-envelope" in result

    def test_differentiation_section_present(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "## Differentiation" in result

    def test_effort_vs_payoff_section_present(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "## Effort vs payoff" in result

    def test_end_state_verifiability_section_present(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "## End-state verifiability" in result

    def test_evidence_items_render_adjacent_to_claims(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        evidence_section = result.split("## Evidence strength", 1)[1].split("\n## ", 1)[0]
        assert "A CLI tool that summarizes chain events" in evidence_section
        assert "Current processing takes 5s per batch" in evidence_section
        assert "Swap events occur 12% of the time" in evidence_section

    def test_evidence_confidence_and_source_render(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "[high]" in result
        assert "[medium]" in result
        assert "source: /var/log/atb.log" in result


class TestRenderBriefEdgeCases:
    def test_empty_evidence_shows_no_evidence(self):
        result = render_brief(_make_idea_contract(), _make_research_input(evidence=[]), _make_main_review())
        assert "no evidence" in result

    def test_missing_optional_fields_render_gracefully(self):
        result = render_brief(
            _make_idea_contract(out_of_scope=[]), _make_research_input(), _make_main_review(risk_score=None)
        )
        assert "no out-of-scope items specified" in result

    def test_none_research_input(self):
        result = render_brief(_make_idea_contract(), None, _make_main_review())
        assert "Skipped sections" in result

    def test_none_main_review(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), None)
        assert "Skipped sections" in result

    def test_non_dict_idea_contract(self):
        result = render_brief("not a dict", _make_research_input(), _make_main_review())
        assert "Skipped sections" in result

    def test_missing_job_id(self):
        result = render_brief(_make_idea_contract(job_id=None), _make_research_input(), _make_main_review())
        assert "Skipped sections" in result


class TestRenderBriefDecisionInfo:
    def test_decision_and_risk_band_render(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "**Decision:** approved_for_coder" in result
        assert "**Risk band:** low" in result

    def test_risk_score_rendered(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review(risk_score=3))
        assert "**Risk score:** 3" in result

    def test_block_reason_rendered(self):
        result = render_brief(
            _make_idea_contract(),
            _make_research_input(),
            _make_main_review(decision="rejected", block_reason="Insufficient evidence for production deployment"),
        )
        assert "Insufficient evidence for production deployment" in result


class TestRenderBriefOutofScopeAndWatchItems:
    def test_out_of_scope_list_rendered(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "UI changes" in result
        assert "database migrations" in result

    def test_empty_out_of_scope(self):
        result = render_brief(_make_idea_contract(out_of_scope=[]), _make_research_input(), _make_main_review())
        assert "no out-of-scope items specified" in result

    def test_watch_items_rendered(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        assert "Latency spikes correlate with large batches" in result

    def test_empty_watch_items(self):
        result = render_brief(_make_idea_contract(), _make_research_input(watch_items=[]), _make_main_review())
        assert "## Feasibility / fit-envelope" in result
        assert "**Watch items:**" not in result


class TestRenderBriefRubricStructure:
    def test_each_rubric_criterion_renders_exactly_once(self):
        result = render_brief(_make_idea_contract(), _make_research_input(), _make_main_review())
        for heading in (
            "## Definition of success",
            "## Problem value",
            "## Evidence strength",
            "## Feasibility / fit-envelope",
            "## Differentiation",
            "## Effort vs payoff",
            "## End-state verifiability",
        ):
            assert result.count(heading) == 1, f"{heading} should appear exactly once"


class TestEvidenceRubricFloor:
    def test_evidence_section_renders_when_research_input_missing(self):
        result = render_brief(_make_idea_contract(), None, _make_main_review())
        assert "## Evidence strength" in result
        evidence_section = result.split("## Evidence strength", 1)[1].split("\n## ", 1)[0]
        assert "no evidence" in evidence_section

    def test_all_malformed_evidence_items_show_no_evidence(self):
        result = render_brief(
            _make_idea_contract(), _make_research_input(evidence=["just a string", 42, None]), _make_main_review()
        )
        evidence_section = result.split("## Evidence strength", 1)[1].split("\n## ", 1)[0]
        assert "no evidence" in evidence_section
        assert "Supporting evidence:" not in evidence_section

    def test_non_list_evidence_shows_no_evidence(self):
        result = render_brief(
            _make_idea_contract(), _make_research_input(evidence="not a list"), _make_main_review()
        )
        evidence_section = result.split("## Evidence strength", 1)[1].split("\n## ", 1)[0]
        assert "no evidence" in evidence_section


# ======================================================================================
# assemble_digest — ported from build #4 test_idea_digest.py
# ======================================================================================
def _make_triple(
    job_id="test-job-001",
    decision="approved_for_coder",
    risk_band="low",
    risk_score=2,
    how_verifiable=None,
    evidence=None,
    out_of_scope=None,
    watch_items=None,
):
    return {
        "idea_contract": {
            "job_id": job_id,
            "proposed_at": "2026-06-03T10:00:00Z",
            "what_should_exist": f"Idea for {job_id}",
            "who_benefits": "Operators",
            "why_now": "Urgent need",
            "evidence_refs": ["ref1"],
            "out_of_scope": out_of_scope or [],
            "where_it_lives": "/path/to/tool",
            "how_verifiable": how_verifiable or f"Tests pass for {job_id}",
        },
        "research_input": {
            "job_id": job_id,
            "collected_at": "2026-06-03T11:00:00Z",
            "topic": "Test topic",
            "sources": [{"name": "s1", "ref": "/ref"}],
            "evidence": evidence or [{"claim": f"Evidence for {job_id}", "source_ref": "/ref"}],
            "watch_items": watch_items or [],
            "summary": "Summary",
        },
        "main_review": {
            "job_id": job_id,
            "reviewed_at": "2026-06-03T12:00:00Z",
            "decision": decision,
            "risk_band": risk_band,
            "risk_score": risk_score,
        },
    }


class TestIsValidTriple:
    def test_valid_triple(self):
        assert _is_valid_triple(_make_triple()) is True

    def test_non_dict_input(self):
        assert _is_valid_triple("not a dict") is False

    def test_missing_idea_contract(self):
        assert _is_valid_triple({"research_input": {}, "main_review": {}}) is False

    def test_idea_contract_not_dict(self):
        assert _is_valid_triple({"idea_contract": "string", "research_input": {}, "main_review": {}}) is False

    def test_missing_job_id(self):
        assert _is_valid_triple(_make_triple(job_id=None)) is False

    def test_main_review_not_dict(self):
        t = _make_triple()
        t["main_review"] = "string"
        assert _is_valid_triple(t) is False

    def test_main_review_missing_decision(self):
        t = _make_triple()
        del t["main_review"]["decision"]
        assert _is_valid_triple(t) is False

    def test_main_review_missing_risk_band(self):
        t = _make_triple()
        del t["main_review"]["risk_band"]
        assert _is_valid_triple(t) is False

    def test_main_review_non_string_risk_band(self):
        t = _make_triple()
        t["main_review"]["risk_band"] = 99
        assert _is_valid_triple(t) is False


class TestDecisionOrder:
    def test_approved_comes_first(self):
        assert _decision_order("approved_for_coder") == 0

    def test_needs_more_comes_second(self):
        assert _decision_order("needs_more") == 1

    def test_rejected_comes_third(self):
        assert _decision_order("rejected") == 2

    def test_unknown_decision_sorts_last(self):
        assert _decision_order("unknown") == 3


class TestRiskKey:
    def test_scored_review(self):
        assert _risk_key({"risk_score": 5, "risk_band": "medium"}) == (0, 5, "medium")

    def test_unscored_review(self):
        assert _risk_key({"risk_band": "high"}) == (1, 0, "high")

    def test_non_dict(self):
        assert _risk_key("string") == (1, 0, "")


class TestAssembleDigest:
    def test_empty_list(self):
        result = assemble_digest([])
        assert "# Idea Digest" in result
        assert "0 brief(s) rendered" in result

    def test_non_list_input(self):
        assert "Error" in assemble_digest("not a list")

    def test_single_triple(self):
        assert "1 brief(s) rendered" in assemble_digest([_make_triple()])

    def test_multiple_triples_sorted_by_decision(self):
        triples = [
            _make_triple(job_id="rejected-job", decision="rejected", risk_score=5),
            _make_triple(job_id="approved-job", decision="approved_for_coder", risk_score=3),
            _make_triple(job_id="needs-more-job", decision="needs_more", risk_score=4),
        ]
        result = assemble_digest(triples)
        assert result.index("approved-job") < result.index("needs-more-job") < result.index("rejected-job")

    def test_sorted_by_risk_within_same_decision(self):
        triples = [
            _make_triple(job_id="high-risk", decision="approved_for_coder", risk_score=8),
            _make_triple(job_id="low-risk", decision="approved_for_coder", risk_score=1),
            _make_triple(job_id="mid-risk", decision="approved_for_coder", risk_score=5),
        ]
        result = assemble_digest(triples)
        assert result.index("low-risk") < result.index("mid-risk") < result.index("high-risk")

    def test_malformed_triples_are_skipped_and_counted(self):
        triples = [_make_triple(job_id="valid-job"), "not a dict", {"idea_contract": {}, "main_review": {}}]
        result = assemble_digest(triples)
        assert "1 brief(s) rendered" in result
        assert "2 malformed triple(s) skipped" in result

    def test_malformed_does_not_prevent_others_from_rendering(self):
        triples = [_make_triple(job_id="valid-job-1"), "bad", _make_triple(job_id="valid-job-2")]
        result = assemble_digest(triples)
        assert "valid-job-1" in result
        assert "valid-job-2" in result

    def test_idempotent(self):
        triples = [
            _make_triple(job_id="job-a", decision="approved_for_coder", risk_score=3),
            _make_triple(job_id="job-b", decision="rejected", risk_score=7),
        ]
        assert assemble_digest(triples) == assemble_digest(triples)

    def test_escalation_header_count_matches_briefs(self):
        triples = [_make_triple(job_id="job-1"), _make_triple(job_id="job-2"), _make_triple(job_id="job-3")]
        assert "3 brief(s) rendered" in assemble_digest(triples)

    def test_risk_band_tiebreaker(self):
        triples = [
            _make_triple(job_id="band-critical", decision="approved_for_coder", risk_score=None, risk_band="critical"),
            _make_triple(job_id="band-low", decision="approved_for_coder", risk_score=None, risk_band="low"),
        ]
        result = assemble_digest(triples)
        assert result.index("band-critical") < result.index("band-low")

    def test_none_input(self):
        assert "Error" in assemble_digest(None)

    def test_mixed_valid_and_malformed_sorted_correctly(self):
        triples = [
            _make_triple(job_id="valid-rejected", decision="rejected", risk_score=8),
            "bad string",
            _make_triple(job_id="valid-approved", decision="approved_for_coder", risk_score=1),
            {"incomplete": True},
        ]
        result = assemble_digest(triples)
        assert result.index("valid-approved") < result.index("valid-rejected")
        assert "2 malformed triple(s) skipped" in result


class TestAssembleDigestTotality:
    def test_unhashable_decision_is_skipped_not_raised(self):
        t = _make_triple(job_id="valid")
        bad = _make_triple(job_id="bad")
        bad["main_review"]["decision"] = ["unhashable"]
        result = assemble_digest([t, bad])
        assert "1 brief(s) rendered" in result
        assert "1 malformed triple(s) skipped" in result

    def test_mixed_risk_score_types_do_not_raise(self):
        a = _make_triple(job_id="a", risk_score=2)
        b = _make_triple(job_id="b", risk_score="high")
        c = _make_triple(job_id="c", risk_score=None)
        assert "3 brief(s) rendered" in assemble_digest([a, b, c])

    def test_malformed_main_review_is_skipped_not_rendered(self):
        good = _make_triple(job_id="good")
        bad = _make_triple(job_id="bad")
        del bad["main_review"]["risk_band"]
        result = assemble_digest([good, bad])
        assert "1 brief(s) rendered" in result
        assert "1 malformed triple(s) skipped" in result
        assert "bad" not in result.split("malformed triple(s) skipped", 1)[1]

    def test_missing_required_contract_field_is_skipped(self):
        incomplete = {
            "idea_contract": {"job_id": "only-id"},
            "research_input": {"evidence": []},
            "main_review": {"decision": "needs_more", "risk_band": "low"},
        }
        result = assemble_digest([_make_triple(job_id="complete"), incomplete])
        assert "1 brief(s) rendered" in result
        assert "1 malformed triple(s) skipped" in result
