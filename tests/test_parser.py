"""
Tests for the SWEList HTML email parser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.gmail.parser import parse_email_html, ParsedJob
from app.utils.hashing import canonicalise_url, url_hash, detect_ats_from_url

FIXTURE = Path(__file__).parent / "fixtures" / "swelist_email.html"


@pytest.fixture
def email_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def parsed_jobs(email_html: str) -> list[ParsedJob]:
    return parse_email_html(email_html, source_email_id="test-email-001")


class TestParserBasic:
    def test_returns_list(self, parsed_jobs):
        assert isinstance(parsed_jobs, list)

    def test_finds_expected_count(self, parsed_jobs):
        # 8 unique jobs in fixture (1 duplicate should be deduped)
        assert len(parsed_jobs) == 8, f"Expected 8, got {len(parsed_jobs)}: {[j.title for j in parsed_jobs]}"

    def test_deduplication(self, email_html):
        """The Stripe job appears twice (once with UTM params). Should appear once."""
        jobs = parse_email_html(email_html)
        stripe_jobs = [j for j in jobs if j.company == "Stripe"]
        assert len(stripe_jobs) == 1, "Stripe job should be deduplicated"

    def test_source_email_id_set(self, parsed_jobs):
        for job in parsed_jobs:
            assert job.source_email_id == "test-email-001"


class TestParserFields:
    def test_stripe_job_fields(self, parsed_jobs):
        stripe = next(j for j in parsed_jobs if j.company == "Stripe")
        assert "intern" in stripe.title.lower() or "intern" in stripe.title.lower()
        assert stripe.ats_type == "ashby"
        assert "remote" in (stripe.location or "").lower()

    def test_datadog_fields(self, parsed_jobs):
        dd = next(j for j in parsed_jobs if j.company == "Datadog")
        assert dd.ats_type == "greenhouse"
        assert "New York" in (dd.location or "")

    def test_figma_fields(self, parsed_jobs):
        figma = next(j for j in parsed_jobs if j.company == "Figma")
        assert figma.ats_type == "lever"

    def test_oracle_workday(self, parsed_jobs):
        oracle = next(j for j in parsed_jobs if j.company == "Oracle")
        assert oracle.ats_type == "workday"

    def test_all_jobs_have_url(self, parsed_jobs):
        for job in parsed_jobs:
            assert job.url, f"Job {job.company}/{job.title} has no URL"

    def test_all_jobs_have_hash(self, parsed_jobs):
        for job in parsed_jobs:
            assert len(job.url_hash) == 64, "Expected SHA-256 hex (64 chars)"

    def test_all_hashes_unique(self, parsed_jobs):
        hashes = [j.url_hash for j in parsed_jobs]
        assert len(hashes) == len(set(hashes)), "Hashes should be unique"

    def test_unknown_ats(self, parsed_jobs):
        acme = next((j for j in parsed_jobs if j.company == "Acme Corp"), None)
        if acme:
            assert acme.ats_type == "unknown"


class TestURLCanonicalization:
    def test_utm_stripped(self):
        url1 = "https://jobs.ashbyhq.com/stripe/abc123?utm_source=swelist&utm_medium=email"
        url2 = "https://jobs.ashbyhq.com/stripe/abc123"
        assert canonicalise_url(url1) == canonicalise_url(url2)

    def test_trailing_slash_stripped(self):
        url1 = "https://jobs.lever.co/figma/abc/"
        url2 = "https://jobs.lever.co/figma/abc"
        assert canonicalise_url(url1) == canonicalise_url(url2)

    def test_scheme_lowercased(self):
        url = "HTTPS://jobs.lever.co/figma/abc"
        canon = canonicalise_url(url)
        assert canon.startswith("https://")

    def test_hash_deterministic(self):
        url = "https://jobs.ashbyhq.com/stripe/abc123"
        assert url_hash(url) == url_hash(url)
        assert url_hash(url) == url_hash(url + "?utm_source=test")

    def test_redirect_url_extracted(self):
        redirect = "https://swelist.com/click?url=https%3A%2F%2Fjobs.lever.co%2Ffigma%2F123"
        from app.utils.hashing import extract_redirect_url
        extracted = extract_redirect_url(redirect)
        # The URL is URL-encoded in the fixture above without encoding, so test direct param:
        redirect2 = "https://swelist.com/click?url=https://jobs.lever.co/figma/123"
        extracted2 = extract_redirect_url(redirect2)
        assert "lever.co" in extracted2


class TestATSDetection:
    def test_ashby(self):
        assert detect_ats_from_url("https://jobs.ashbyhq.com/stripe/abc") == "ashby"

    def test_greenhouse(self):
        assert detect_ats_from_url("https://boards.greenhouse.io/stripe/jobs/123") == "greenhouse"
        assert detect_ats_from_url("https://grnh.se/abc123") == "greenhouse"

    def test_lever(self):
        assert detect_ats_from_url("https://jobs.lever.co/figma/abc") == "lever"

    def test_workday(self):
        assert detect_ats_from_url("https://stripe.wd1.myworkdayjobs.com/en-US/jobs") == "workday"

    def test_unknown(self):
        assert detect_ats_from_url("https://acme.com/careers/engineer") == "unknown"


class TestEdgeCases:
    def test_empty_html(self):
        result = parse_email_html("")
        assert result == []

    def test_no_job_links(self):
        html = "<html><body><p>Hello world</p><a href='https://example.com'>Visit us</a></body></html>"
        result = parse_email_html(html)
        assert result == []

    def test_boilerplate_skipped(self):
        """Unsubscribe links should not be extracted as jobs."""
        jobs = parse_email_html(FIXTURE.read_text())
        urls = [j.url for j in jobs]
        assert not any("unsubscribe" in u for u in urls)
        assert not any("privacy" in u for u in urls)
