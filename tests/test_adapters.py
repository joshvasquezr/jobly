"""
Unit tests for adapter can_handle() and URL detection logic.
No Playwright required — these test the static dispatch layer only.
"""

from __future__ import annotations

import pytest

from app.adapters import get_adapter, detect_ats
from app.adapters.ashby import AshbyAdapter
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.lever import LeverAdapter
from app.adapters.workday import WorkdayAdapter


class TestAshbyCanHandle:
    def test_ashbyhq_jobs_url(self):
        assert AshbyAdapter.can_handle("https://jobs.ashbyhq.com/stripe/abc123")

    def test_ashbyhq_app_url(self):
        assert AshbyAdapter.can_handle("https://app.ashbyhq.com/jobs/acme/123")

    def test_ashby_subdomain(self):
        assert AshbyAdapter.can_handle("https://stripe.ashby.com/jobs/abc")

    def test_not_ashby(self):
        assert not AshbyAdapter.can_handle("https://boards.greenhouse.io/stripe/jobs/123")
        assert not AshbyAdapter.can_handle("https://jobs.lever.co/figma/abc")


class TestGreenhouseCanHandle:
    def test_boards_url(self):
        assert GreenhouseAdapter.can_handle("https://boards.greenhouse.io/datadog/jobs/123")

    def test_job_boards_url(self):
        assert GreenhouseAdapter.can_handle("https://job-boards.greenhouse.io/stripe/jobs/456")

    def test_short_link(self):
        assert GreenhouseAdapter.can_handle("https://grnh.se/abc123")

    def test_not_greenhouse(self):
        assert not GreenhouseAdapter.can_handle("https://jobs.lever.co/figma/abc")
        assert not GreenhouseAdapter.can_handle("https://jobs.ashbyhq.com/stripe/abc")


class TestLeverCanHandle:
    def test_jobs_lever_url(self):
        assert LeverAdapter.can_handle("https://jobs.lever.co/figma/abc")

    def test_apply_url(self):
        assert LeverAdapter.can_handle("https://jobs.lever.co/netflix/abc/apply")

    def test_not_lever(self):
        assert not LeverAdapter.can_handle("https://boards.greenhouse.io/stripe/jobs/123")


class TestWorkdayCanHandle:
    def test_wd1_url(self):
        assert WorkdayAdapter.can_handle(
            "https://oracle.wd1.myworkdayjobs.com/en-US/oracle_jobs/job/abc"
        )

    def test_wd5_url(self):
        assert WorkdayAdapter.can_handle(
            "https://amazon.wd5.myworkdayjobs.com/en-US/Amazon_Jobs/job/abc"
        )

    def test_not_workday(self):
        assert not WorkdayAdapter.can_handle("https://jobs.lever.co/figma/abc")


class TestAdapterRegistry:
    def test_get_adapter_ashby(self):
        adapter = get_adapter("https://jobs.ashbyhq.com/stripe/abc")
        assert adapter is not None
        assert adapter.ats_type == "ashby"

    def test_get_adapter_greenhouse(self):
        adapter = get_adapter("https://boards.greenhouse.io/stripe/jobs/123")
        assert adapter is not None
        assert adapter.ats_type == "greenhouse"

    def test_get_adapter_lever(self):
        adapter = get_adapter("https://jobs.lever.co/figma/abc")
        assert adapter is not None
        assert adapter.ats_type == "lever"

    def test_get_adapter_workday(self):
        adapter = get_adapter("https://oracle.wd1.myworkdayjobs.com/en-US/jobs/abc")
        assert adapter is not None
        assert adapter.ats_type == "workday"

    def test_get_adapter_unknown_returns_none(self):
        adapter = get_adapter("https://acme.com/careers/engineer")
        assert adapter is None

    def test_detect_ats_matches_adapter(self):
        urls = [
            ("https://jobs.ashbyhq.com/stripe/abc", "ashby"),
            ("https://boards.greenhouse.io/stripe/jobs/123", "greenhouse"),
            ("https://jobs.lever.co/figma/abc", "lever"),
            ("https://oracle.wd1.myworkdayjobs.com/en-US/jobs/abc", "workday"),
        ]
        for url, expected in urls:
            assert detect_ats(url) == expected, f"Expected {expected} for {url}"


class TestAdapterInstantiation:
    """Ensure adapters instantiate without error and expose required attributes."""

    @pytest.mark.parametrize("cls", [AshbyAdapter, GreenhouseAdapter, LeverAdapter, WorkdayAdapter])
    def test_instantiates(self, cls):
        adapter = cls()
        assert adapter.ats_type

    @pytest.mark.parametrize("cls", [AshbyAdapter, GreenhouseAdapter, LeverAdapter, WorkdayAdapter])
    def test_has_required_methods(self, cls):
        adapter = cls()
        assert callable(adapter.open_and_prepare)
        assert callable(adapter.fill_form)
        assert callable(adapter.reach_review_step)
        assert callable(adapter.submit)
        assert callable(cls.can_handle)
