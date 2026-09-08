from __future__ import annotations

from pathlib import Path

import thinktank_tracker.link_test_service as link_tests
from thinktank_tracker.config import AppConfig
from thinktank_tracker.crawler import FetchAttempt, FetchError, FetchedResource
from thinktank_tracker.link_test_service import EntryTestSnapshot


def _snapshot(entry_id: int, url: str) -> EntryTestSnapshot:
    return EntryTestSnapshot(
        id=entry_id,
        url=url,
        adapter_key="euiss",
        listing_selectors=(),
        date_selectors=(),
        allowed_patterns=(),
        excluded_patterns=(),
    )


def test_entry_link_test_distinguishes_success_warning_and_failure(monkeypatch, tmp_path) -> None:
    class FakeCrawler:
        browser_fallbacks: list[str] = []

        def __init__(self, _config, _limiter):
            pass

        def fetch(self, url):
            if url.endswith("/failed"):
                raise FetchError(
                    "HTTP 503",
                    [FetchAttempt("https://example.org/final", 503, "http_error")],
                )
            body = (
                b'<main><article><div class="teaser-title"><a href="/publications/reports/example">Report</a></div></article></main>'
                if "success" in url
                else b"<html><main>No publication cards</main></html>"
            )
            return FetchedResource(
                url=url,
                body=body,
                content_type="text/html",
                cache_path=Path(tmp_path) / "page.html",
                status_code=200,
            )

        def _render_with_playwright(self, url):
            self.browser_fallbacks.append(url)
            if "browser" in url:
                return b'<main><article><div class="teaser-title"><a href="/publications/reports/rendered">Report</a></div></article></main>'
            if "render-failed" in url:
                raise RuntimeError("browser process exited")
            return b"<html><main>No publication cards after rendering</main></html>"

        def close(self):
            pass

    monkeypatch.setattr(link_tests, "WebCrawler", FakeCrawler)
    results = link_tests.test_entries(
        [
            _snapshot(1, "https://example.org/success"),
            _snapshot(2, "https://example.org/warning"),
            _snapshot(3, "https://example.org/failed"),
            _snapshot(4, "https://example.org/browser"),
            _snapshot(5, "https://example.org/render-failed"),
            EntryTestSnapshot(
                id=6,
                url="https://example.org/success-rejected",
                adapter_key="euiss",
                excluded_patterns=(r"/publications/reports/",),
            ),
        ],
        AppConfig(crawl_concurrency=1, per_domain_delay_seconds=0),
    )

    assert [row.status for row in results] == [
        "success",
        "warning",
        "failed",
        "success",
        "failed",
        "warning",
    ]
    assert results[0].accepted_count == 1
    assert results[0].http_status == 200
    assert "未识别" in results[1].message
    assert "HTTP 503" in results[2].message
    assert results[2].final_url == "https://example.org/final"
    assert results[2].http_status == 503
    assert results[3].accepted_count == 1
    assert results[4].final_url == "https://example.org/render-failed"
    assert results[4].http_status == 200
    assert "browser process exited" in results[4].message
    assert results[5].accepted_count == 0
    assert "拒绝样例" in results[5].message
    assert "https://example.org/publications/reports/example（命中排除路径规则）" in results[5].message
    assert FakeCrawler.browser_fallbacks == [
        "https://example.org/warning",
        "https://example.org/browser",
        "https://example.org/render-failed",
    ]
