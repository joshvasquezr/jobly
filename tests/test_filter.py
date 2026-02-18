"""
Tests for the rules-based job scoring / filtering engine.
"""

from __future__ import annotations

import pytest

from app.utils.filter import FilterConfig, score_job, ScoringResult


@pytest.fixture
def default_cfg() -> FilterConfig:
    return FilterConfig()


class TestScoreJobInternships:
    def test_intern_title_scores_high(self, default_cfg):
        result = score_job("Software Engineer Intern", "Stripe", None, "ashby", default_cfg)
        assert result.score >= 0.50
        assert result.should_queue is True

    def test_internship_title_scores_high(self, default_cfg):
        result = score_job("Backend Internship", "Figma", None, "greenhouse", default_cfg)
        assert result.score >= 0.40
        assert result.should_queue is True

    def test_multiple_keywords_boost(self, default_cfg):
        # "intern" + "backend" + "infra" → more keywords = higher score
        single = score_job("Software Engineer Intern", "X", None, "unknown", default_cfg)
        multi = score_job("Backend Infrastructure Intern", "X", None, "unknown", default_cfg)
        assert multi.score >= single.score

    def test_senior_role_scores_lower(self, default_cfg):
        # "Senior SWE" has keyword "swe" but no "intern"
        result = score_job("Senior Software Engineer", "Netflix", None, "lever", default_cfg)
        # Should be lower than an intern role
        intern_result = score_job("Software Engineer Intern", "Netflix", None, "lever", default_cfg)
        assert intern_result.score > result.score


class TestATSBoosts:
    def test_ashby_boost(self, default_cfg):
        no_ats = score_job("Software Engineer Intern", "X", None, "unknown", default_cfg)
        ashby = score_job("Software Engineer Intern", "X", None, "ashby", default_cfg)
        assert ashby.score > no_ats.score
        assert ashby.score - no_ats.score == pytest.approx(0.20, abs=0.01)

    def test_greenhouse_boost(self, default_cfg):
        no_ats = score_job("Software Engineer Intern", "X", None, "unknown", default_cfg)
        gh = score_job("Software Engineer Intern", "X", None, "greenhouse", default_cfg)
        assert gh.score > no_ats.score

    def test_lever_boost(self, default_cfg):
        no_ats = score_job("Software Engineer Intern", "X", None, "unknown", default_cfg)
        lever = score_job("Software Engineer Intern", "X", None, "lever", default_cfg)
        assert lever.score > no_ats.score

    def test_workday_no_boost(self, default_cfg):
        no_ats = score_job("Software Engineer Intern", "X", None, "unknown", default_cfg)
        wd = score_job("Software Engineer Intern", "X", None, "workday", default_cfg)
        assert wd.score == no_ats.score


class TestLocationFiltering:
    def test_excluded_location_zeroes_score(self):
        cfg = FilterConfig(excluded_locations=["New York"])
        result = score_job("Software Engineer Intern", "X", "New York, NY", "ashby", cfg)
        assert result.score == 0.0
        assert result.should_queue is False
        assert "excluded location" in result.reason.lower()

    def test_preferred_location_boost(self):
        cfg = FilterConfig(preferred_locations=["San Francisco"])
        sf = score_job("Intern", "X", "San Francisco, CA", "ashby", cfg)
        other = score_job("Intern", "X", "Austin, TX", "ashby", cfg)
        assert sf.score >= other.score

    def test_remote_slight_boost(self, default_cfg):
        remote = score_job("Software Engineer Intern", "X", "Remote", "ashby", default_cfg)
        onsite = score_job("Software Engineer Intern", "X", "Palo Alto, CA", "ashby", default_cfg)
        assert remote.score >= onsite.score

    def test_no_location_filter_neutral(self, default_cfg):
        # No preferred_locations set — location doesn't block anything
        result = score_job("Software Engineer Intern", "X", "Timbuktu", "ashby", default_cfg)
        assert result.should_queue is True


class TestThresholds:
    def test_default_threshold_0_30(self, default_cfg):
        assert default_cfg.min_score == pytest.approx(0.30)

    def test_no_keywords_below_threshold(self, default_cfg):
        # "Accountant" with no matching keywords
        result = score_job("Accountant", "X", None, "unknown", default_cfg)
        assert result.should_queue is False

    def test_score_capped_at_1(self, default_cfg):
        result = score_job(
            "Backend Infrastructure Platform Distributed Systems Database Intern Internship SWE",
            "X", "Remote", "ashby", default_cfg,
        )
        assert result.score <= 1.0

    def test_score_non_negative(self, default_cfg):
        result = score_job("", "", None, None, default_cfg)
        assert result.score >= 0.0

    def test_custom_threshold(self):
        cfg = FilterConfig(min_score=0.60)
        result = score_job("Software Engineer Intern", "X", None, "unknown", cfg)
        # Score is 0.35 (one keyword match), below 0.60 threshold
        assert result.should_queue is False


class TestReasonString:
    def test_reason_non_empty_when_match(self, default_cfg):
        result = score_job("Software Engineer Intern", "Stripe", None, "ashby", default_cfg)
        assert result.reason
        assert len(result.reason) > 5

    def test_reason_mentions_keyword(self, default_cfg):
        result = score_job("Backend Intern", "X", None, "unknown", default_cfg)
        assert "intern" in result.reason.lower() or "backend" in result.reason.lower()

    def test_reason_mentions_ats(self, default_cfg):
        result = score_job("Intern", "X", None, "ashby", default_cfg)
        assert "ashby" in result.reason.lower()
