from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from thinktank_tracker.config import AppConfig
from thinktank_tracker.crawler import DiscoveredLink, FetchError, FetchedResource, WebCrawler
from thinktank_tracker.extractors import DiscoveredAttachment, ExtractedDocument


def test_zero_candidate_listing_is_incomplete(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setattr(crawler, "_render_with_playwright", lambda url: b"<html><main>Empty list</main></html>")
    monkeypatch.setattr(crawler, "_cache_file", lambda url, suffix: tmp_path / "empty.html")
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
    assert WebCrawler._slash_variant("https://example.org/publications/?page=2") == (
        "https://example.org/publications?page=2"
    )
    assert WebCrawler._slash_variant("https://example.org/report.pdf") is None


def test_legitimate_recaptcha_script_is_not_treated_as_block_page() -> None:
    legitimate = b"<html><head><title>Publications</title></head><script src='recaptcha.js'></script></html>"
    challenge = b'<html><head><title>Just a moment...</title></head><form id="challenge-form"></form></html>'
    verification = b"<html><head><title>One moment, please...</title></head><body>Please wait while your request is being verified...</body></html>"
    assert not WebCrawler._looks_like_block_page(legitimate)
    assert WebCrawler._looks_like_block_page(challenge)
    assert WebCrawler._looks_like_block_page(verification)


def test_network_access_denied_is_non_retriable_and_does_not_launch_browser(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    calls = 0

    def deny(_url):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("[WinError 10013] access denied", request=httpx.Request("GET", _url))

    monkeypatch.setattr(crawler, "_robots_allowed", lambda _url: True)
    monkeypatch.setattr(crawler.client, "get", deny)
    monkeypatch.setattr(
        crawler,
        "_render_with_playwright",
        lambda _url: pytest.fail("static network denial must not launch Chromium"),
    )
    try:
        with pytest.raises(FetchError) as exc_info:
            crawler.fetch("https://example.org/publications/")
    finally:
        crawler.close()

    assert calls == 1
    assert exc_info.value.code == "network_access_denied"
    assert exc_info.value.retriable is False


def test_transient_http_statuses_retry_three_times(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    statuses = iter([429, 503, 200])
    calls = 0

    def respond(url):
        nonlocal calls
        calls += 1
        status = next(statuses)
        return httpx.Response(status, content=b"ok", request=httpx.Request("GET", url))

    monkeypatch.setattr(crawler, "_robots_allowed", lambda _url: True)
    monkeypatch.setattr(crawler.client, "get", respond)
    monkeypatch.setattr("thinktank_tracker.crawler.time.sleep", lambda _seconds: None)
    try:
        response = crawler._request("https://example.org/publications/")
    finally:
        crawler.close()

    assert response.status_code == 200
    assert calls == 3


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
        body=b"%PDF-1.4 fake",
        content_type="application/pdf",
        cache_path=Path("report.pdf"),
    )
    monkeypatch.setattr(crawler, "fetch", lambda url, allow_browser_fallback=True: pdf_resource if url.endswith(".pdf") else html_resource)
    monkeypatch.setattr(
        "thinktank_tracker.crawler.extract_html_document",
        lambda raw, url, rules=None: ExtractedDocument(
            title="Official research title",
            text="short",
            english_word_count=1,
            published_date=date(2026, 8, 26),
            date_status="confirmed",
            date_evidence="HTML article:published_time",
            content_type="reports",
            content_type_evidence="HTML URL rule",
            headings=["HTML overview"],
        ),
    )
    monkeypatch.setattr(
        "thinktank_tracker.crawler.discover_pdf_links",
        lambda raw, url, title: [
            DiscoveredAttachment(pdf_resource.url, pdf_resource.url, "Download Publication", "primary", 100)
        ],
    )
    monkeypatch.setattr(
        "thinktank_tracker.crawler.extract_pdf_document",
        lambda raw, url, fallback, rules=None, pdf_parser_mode="anydoc_first": ExtractedDocument(
            title="Generic layout template",
            text="word " * 1600,
            english_word_count=1600,
            published_date=date(2020, 1, 1),
            date_status="metadata_only",
            date_evidence="PDF CreationDate",
            content_type="unknown",
            content_type_evidence="PDF URL",
            headings=["PDF findings", "PDF data"],
            extraction_audit={"final_engine": "anydoc"},
        ),
    )
    try:
        document, _, attachments = crawler.extract(
            DiscoveredLink(
                html_resource.url,
                "https://example.org/publication/official-title",
                "https://example.org/publications/",
                "Listing title",
            )
        )
    finally:
        crawler.close()

    assert document.title == "Official research title"
    assert document.english_word_count == 1600
    assert document.published_date == date(2026, 8, 26)
    assert document.date_evidence == "HTML article:published_time"
    assert document.content_type == "reports"
    assert document.content_type_evidence == "HTML URL rule"
    assert document.headings == ["HTML overview", "PDF findings", "PDF data"]
    assert document.extraction_audit == {"final_engine": "anydoc"}
    assert len(attachments) == 1
    assert attachments[0].used_as_body is True


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
                    "_embedded": {
                        "wp:term": [[
                            {
                                "taxonomy": "type-of-publication",
                                "name": "Policy Briefs",
                            }
                        ]]
                    },
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
    assert result.links[0].listing_content_type == "policy brief"
    assert result.links[0].listing_content_type_labels == ("Policy Briefs",)
    assert "publication taxonomy" in result.stop_reason
    assert result.earliest_date == result.latest_date == date(2026, 8, 24)


def test_unknown_date_cancels_boundary_early_stop(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0, max_listing_pages=10))

    def page(article: str, item_date: str | None, next_page: int | None, include_pin: bool = True) -> bytes:
        date_html = f'<time datetime="{item_date}">{item_date}</time>' if item_date else ""
        pin = (
            '<article><a href="/reports/pinned">Pinned</a><time datetime="2026-08-20">2026-08-20</time></article>'
            if include_pin
            else ""
        )
        nxt = f'<a rel="next" href="/publications/?page={next_page}">Next</a>' if next_page else ""
        return (
            f'<main>{pin}<article><a href="/reports/{article}">{article}</a>{date_html}</article>{nxt}</main>'
        ).encode()

    pages = {
        "https://example.org/publications/": page("old-one", "2026-07-20", 2),
        "https://example.org/publications/?page=2": page("unknown", None, 3),
        "https://example.org/publications/?page=3": page("old-three", "2026-07-18", 4),
        "https://example.org/publications/?page=4": page("old-four", "2026-07-17", None),
    }

    monkeypatch.setattr(
        crawler,
        "fetch",
        lambda url: FetchedResource(url, pages[url], "text/html", Path("listing.html")),
    )
    try:
        result = crawler.scan_listing("https://example.org/publications/", date(2026, 8, 2), True)
    finally:
        crawler.close()

    assert result.pages_scanned == 4
    assert result.stop_reason == "已到栏目末页"
    assert result.coverage_status == "incomplete"
    assert len(result.links) == 5


def test_persist_callback_releases_preflight_page_and_document(monkeypatch, tmp_path) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    listing_url = "https://example.org/publications/"
    detail_url = "https://example.org/reports/large-item"
    listing = b'<main><article><a href="/reports/large-item">Large report item</a></article></main>'
    detail = (
        b'<html><head><meta property="article:published_time" content="2026-08-08"></head>'
        b'<body><article><h1>Large item</h1><p>' + b"strategy " * 200_000 + b"</p></article></body></html>"
    )

    def fake_fetch(url: str, allow_browser_fallback: bool = True) -> FetchedResource:
        body = listing if url == listing_url else detail
        cache_path = tmp_path / ("listing.html" if url == listing_url else "detail.html")
        cache_path.write_bytes(body)
        return FetchedResource(url, body, "text/html", cache_path)

    seen_during_callback: list[tuple[bool, bool]] = []

    def persist(link: DiscoveredLink) -> None:
        seen_during_callback.append(
            (link.preflight_resource is not None, link.preflight_document is not None)
        )

    monkeypatch.setattr(crawler, "fetch", fake_fetch)
    try:
        result = crawler.scan_listing(
            listing_url,
            date(2026, 8, 2),
            True,
            end_date=date(2026, 8, 20),
            on_link=persist,
        )
    finally:
        crawler.close()

    assert seen_during_callback == [(True, True)]
    assert result.links[0].fetch_url == detail_url
    assert result.links[0].preflight_resource is None
    assert result.links[0].preflight_document is None


def test_ordered_listing_stops_after_ten_confirmed_old_items_beyond_boundary(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0, max_listing_pages=20))
    pages: dict[str, bytes] = {}
    for page_number in range(1, 13):
        url = (
            "https://example.org/publications/"
            if page_number == 1
            else f"https://example.org/publications/?page={page_number}"
        )
        next_page = page_number + 1
        day = 21 - page_number
        pages[url] = (
            '<main><article><a href="/reports/pinned-report">Pinned report</a>'
            '<time datetime="2026-08-20">2026-08-20</time></article>'
            f'<article><a href="/reports/old-report-{page_number}">Old report {page_number}</a>'
            f'<time datetime="2026-07-{day:02d}">2026-07-{day:02d}</time></article>'
            f'<a rel="next" href="/publications/?page={next_page}">Next</a></main>'
        ).encode()
    monkeypatch.setattr(
        crawler,
        "fetch",
        lambda url: FetchedResource(url, pages[url], "text/html", Path("listing.html")),
    )
    try:
        result = crawler.scan_listing(
            "https://example.org/publications/", date(2026, 8, 2), True
        )
    finally:
        crawler.close()

    assert result.pages_scanned == 11
    assert "连续 10 条" in result.stop_reason
    assert result.boundary_page_number == 1
    assert result.page_audits[-1].stop_evidence
    assert len(result.links) == 12


def test_large_boundary_page_does_not_preflight_more_than_ten_items_after_boundary(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0, max_listing_pages=5))
    articles = "".join(
        f'<article><a href="/reports/old-{index}">Old report {index}</a>'
        f'<time datetime="2026-07-{25-index:02d}">2026-07-{25-index:02d}</time></article>'
        for index in range(1, 21)
    )
    raw = f"<main>{articles}</main>".encode()
    monkeypatch.setattr(
        crawler,
        "fetch",
        lambda url: FetchedResource(url, raw, "text/html", Path("listing.html")),
    )
    try:
        result = crawler.scan_listing(
            "https://example.org/publications/", date(2026, 8, 2), True
        )
    finally:
        crawler.close()

    assert result.pages_scanned == 1
    assert len(result.links) == 11
    assert result.page_audits[0].cards_found == 20
    assert result.page_audits[0].accepted_count == 11
    assert "连续 10 条" in result.stop_reason


def test_featured_item_does_not_break_descending_order_or_boundary_stop(monkeypatch) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0, max_listing_pages=5))
    old_items = "".join(
        f'<article><h2><a href="/publication/old-{index}/">Old {index}</a></h2>'
        f'<time datetime="2026-07-{20-index:02d}">2026-07-{20-index:02d}</time></article>'
        for index in range(1, 13)
    )
    raw = (
        '<main><section class="featured-publication"><h2><a href="/publication/featured/">Featured</a></h2>'
        '<time datetime="2026-07-01">2026-07-01</time></section>'
        '<article><h2><a href="/publication/newest/">Newest</a></h2>'
        '<time datetime="2026-08-26">2026-08-26</time></article>'
        f"{old_items}</main>"
    ).encode()
    monkeypatch.setattr(
        crawler,
        "fetch",
        lambda url: FetchedResource(url, raw, "text/html", Path("listing.html")),
    )
    try:
        result = crawler.scan_listing(
            "https://example.org/publications/",
            date(2026, 8, 2),
            True,
            listing_selectors=(".featured-publication h2 a", "article h2 a"),
        )
    finally:
        crawler.close()

    assert result.ordering_status == "verified_descending"
    assert len(result.links) == 13  # featured + newest + boundary + ten verification items
    assert "连续 10 条" in result.stop_reason


def test_fetch_preserves_observed_epc_trailing_slash(monkeypatch, tmp_path) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    calls: list[str] = []

    def fake_request(url: str) -> httpx.Response:
        calls.append(url)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><title>Policy brief</title><main>content</main></html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(crawler, "_request", fake_request)
    monkeypatch.setattr(crawler, "_cache_file", lambda url, suffix: tmp_path / f"page{suffix}")
    url = "https://www.epc.eu/publication/from-sea-to-space/"
    try:
        resource = crawler.fetch(url, allow_browser_fallback=False)
    finally:
        crawler.close()

    assert calls == [url]
    assert resource.url == url
    assert resource.attempts[0].url == url


def test_fetch_retries_slash_after_hard_404(monkeypatch, tmp_path) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    calls: list[str] = []

    def fake_request(url: str) -> httpx.Response:
        calls.append(url)
        status = 200 if url.endswith("/") else 404
        return httpx.Response(
            status,
            headers={"content-type": "text/html"},
            content=b"<html><title>Report</title><main>content</main></html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(crawler, "_request", fake_request)
    monkeypatch.setattr(crawler, "_cache_file", lambda url, suffix: tmp_path / f"hard{suffix}")
    try:
        resource = crawler.fetch("https://example.org/report", allow_browser_fallback=False)
    finally:
        crawler.close()

    assert calls == ["https://example.org/report", "https://example.org/report/"]
    assert resource.url == "https://example.org/report/"
    assert [attempt.status_code for attempt in resource.attempts] == [404, 200]


def test_fetch_retries_slash_after_soft_404(monkeypatch, tmp_path) -> None:
    crawler = WebCrawler(AppConfig(per_domain_delay_seconds=0))
    calls: list[str] = []

    def fake_request(url: str) -> httpx.Response:
        calls.append(url)
        body = (
            b"<html><title>Page not found</title></html>"
            if not url.endswith("/")
            else b"<html><title>Working paper</title><main>content</main></html>"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=body,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(crawler, "_request", fake_request)
    monkeypatch.setattr(crawler, "_cache_file", lambda url, suffix: tmp_path / f"soft{suffix}")
    try:
        resource = crawler.fetch("https://example.org/paper", allow_browser_fallback=False)
    finally:
        crawler.close()

    assert calls == ["https://example.org/paper", "https://example.org/paper/"]
    assert [attempt.outcome for attempt in resource.attempts] == ["soft_404", "response"]
