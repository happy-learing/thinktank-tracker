from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from thinktank_tracker.config import AppConfig
from thinktank_tracker.crawler import DiscoveredLink, FetchedResource, WebCrawler
from thinktank_tracker.extractors import ExtractedDocument


def test_zero_candidate_listing_is_incomplete(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    monkeypatch.setattr(
        crawler,
        "fetch",
        lambda url: FetchedResource(
            url=url,
            body=b"<html><main><a href='/about'>About</a></main></html>",
            content_type="text/html",
            cache_path=Path("empty.html"),
        ),
    )
    try:
        result = crawler.scan_listing(
            "https://example.org/publications/",
            date(2026, 8, 2),
            True,
        )
    finally:
        crawler.close()

    assert result.coverage_status == "incomplete"
    assert "未识别到任何候选" in result.stop_reason


def test_slash_variant_skips_files() -> None:
    assert WebCrawler._slash_variant("https://example.org/publications?page=2") == (
        "https://example.org/publications/?page=2"
    )
    assert WebCrawler._slash_variant("https://example.org/report.pdf") is None


def test_legitimate_recaptcha_script_is_not_treated_as_block_page() -> None:
    legitimate = b"<html><head><title>Publications</title></head><script src='recaptcha.js'></script></html>"
    challenge = b'<html><head><title>Just a moment...</title></head><form id="challenge-form"></form></html>'
    assert not WebCrawler._looks_like_block_page(legitimate)
    assert WebCrawler._looks_like_block_page(challenge)


def test_pdf_follow_up_keeps_official_html_title(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    html_resource = FetchedResource(
        url="https://example.org/publication/official-title/",
        body=b"html",
        content_type="text/html",
        cache_path=Path("page.html"),
    )
    pdf_resource = FetchedResource(
        url="https://example.org/report.pdf",
        body=b"pdf",
        content_type="application/pdf",
        cache_path=Path("report.pdf"),
    )
    monkeypatch.setattr(crawler, "fetch", lambda url, allow_browser_fallback=True: pdf_resource if url.endswith(".pdf") else html_resource)
    monkeypatch.setattr(
        "thinktank_tracker.crawler.extract_html_document",
        lambda raw, url: ExtractedDocument(title="Official research title", text="short", english_word_count=1),
    )
    monkeypatch.setattr("thinktank_tracker.crawler.discover_pdf_links", lambda raw, url: [pdf_resource.url])
    monkeypatch.setattr(
        "thinktank_tracker.crawler.extract_pdf_document",
        lambda raw, url, fallback: ExtractedDocument(
            title="Generic layout template",
            text="word " * 1600,
            english_word_count=1600,
        ),
    )
    try:
        document, _ = crawler.extract(DiscoveredLink(html_resource.url, "Listing title"))
    finally:
        crawler.close()

    assert document.title == "Official research title"


def test_martens_api_uses_closed_date_interval(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    requested_url = ""

    def fake_request(url: str) -> httpx.Response:
        nonlocal requested_url
        requested_url = url
        return httpx.Response(
            200,
            headers={"x-wp-totalpages": "1"},
            json=[
                {
                    "date": "2026-08-24T11:29:54",
                    "link": "https://www.martenscentre.eu/publication/report-one/",
                    "title": {"rendered": "Report &amp; One"},
                }
            ],
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(crawler, "_request", fake_request)
    try:
        result = crawler.scan_listing(
            "https://www.martenscentre.eu/publication/",
            date(2026, 8, 2),
            True,
            "martens",
            date(2026, 8, 26),
        )
    finally:
        crawler.close()

    query = httpx.QueryParams(httpx.URL(requested_url).query)
    assert query["after"] == "2026-08-01T23:59:59"
    assert query["before"] == "2026-08-27T00:00:00"
    assert result.coverage_status == "complete"
    assert result.links[0].listing_title == "Report & One"
    assert result.earliest_date == result.latest_date == date(2026, 8, 24)
