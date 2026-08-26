from __future__ import annotations

import html
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import AppConfig
from .extractors import (
    ExtractedDocument,
    discover_content_links,
    discover_listing_dates,
    discover_pdf_links,
    extract_html_document,
    extract_pdf_document,
    next_page_url,
)
from .adapters import get_adapter
from .paths import PATHS
from .url_utils import canonicalize_url


USER_AGENT = "ThinkTankTracker/0.1 (+local research audit tool)"


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    listing_title: str = ""


@dataclass
class ScanResult:
    links: list[DiscoveredLink] = field(default_factory=list)
    pages_scanned: int = 0
    earliest_date: date | None = None
    latest_date: date | None = None
    coverage_status: str = "complete"
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class FetchedResource:
    url: str
    body: bytes
    content_type: str
    cache_path: Path
    rendered: bool = False


class DomainRateLimiter:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        with self._lock:
            elapsed = time.monotonic() - self._last_request.get(host, 0.0)
            if elapsed < self.delay_seconds:
                time.sleep(self.delay_seconds - elapsed)
            self._last_request[host] = time.monotonic()


class WebCrawler:
    def __init__(self, config: AppConfig, limiter: DomainRateLimiter | None = None) -> None:
        self.config = config
        self.limiter = limiter or DomainRateLimiter(config.per_domain_delay_seconds)
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(35.0, connect=15.0),
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en,zh-CN;q=0.8"},
        )
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    def close(self) -> None:
        self.client.close()

    def _robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = robotparser.RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            try:
                self.limiter.wait(parser.url)
                response = self.client.get(parser.url)
                if response.status_code < 400:
                    parser.parse(response.text.splitlines())
                    self._robots[origin] = parser
                else:
                    self._robots[origin] = None
            except httpx.HTTPError:
                self._robots[origin] = None
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def _cache_file(self, url: str, suffix: str) -> Path:
        import hashlib

        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        path = PATHS.downloads / digest[:2] / f"{digest}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _request(self, url: str) -> httpx.Response:
        if not self._robots_allowed(url):
            raise PermissionError(f"robots.txt 禁止自动访问: {url}")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self.limiter.wait(url)
                response = self.client.get(url)
                slash_variant = self._slash_variant(url)
                if response.status_code == 404 and slash_variant:
                    self.limiter.wait(slash_variant)
                    retried = self.client.get(slash_variant)
                    if retried.status_code < 400:
                        response = retried
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        f"transient HTTP {response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"请求失败（重试 3 次）: {last_error}")

    @staticmethod
    def _slash_variant(url: str) -> str | None:
        parts = urlsplit(url)
        final_segment = parts.path.rsplit("/", 1)[-1]
        if not parts.path or parts.path.endswith("/") or "." in final_segment:
            return None
        return urlunsplit((parts.scheme, parts.netloc, f"{parts.path}/", parts.query, parts.fragment))

    @staticmethod
    def _looks_like_custom_404(body: bytes) -> bool:
        sample = body[:20_000].lower()
        return b"<title>error 404</title>" in sample or b"body-error_404" in sample

    @staticmethod
    def _looks_like_block_page(body: bytes) -> bool:
        sample = body[:20_000].lower()
        return any(
            marker in sample
            for marker in (
                b"cf-chl-",
                b'id="challenge-form"',
                b"<title>just a moment",
                b"<title>access denied",
                b"<title>enable javascript",
            )
        )

    def _render_with_playwright(self, url: str) -> bytes:
        browser_path = PATHS.root / "runtime" / "browsers"
        if browser_path.exists():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_path))
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(locale="en-US")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("load", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(1_500)
            body = page.content().encode("utf-8")
            browser.close()
        return body

    def fetch(self, url: str, allow_browser_fallback: bool = True) -> FetchedResource:
        try:
            response = self._request(url)
            final_url = str(response.url)
            content_type = response.headers.get("content-type", "").lower()
            body = response.content
            if "html" in content_type and self._looks_like_custom_404(body):
                slash_variant = self._slash_variant(final_url)
                if slash_variant:
                    response = self._request(slash_variant)
                    final_url = str(response.url)
                    content_type = response.headers.get("content-type", "").lower()
                    body = response.content
        except PermissionError:
            raise
        except Exception:
            if not allow_browser_fallback:
                raise
            body = self._render_with_playwright(url)
            final_url = url
            content_type = "text/html; rendered=playwright"
            response = None
        is_pdf = "application/pdf" in content_type or urlsplit(final_url).path.lower().endswith(".pdf")
        suffix = ".pdf" if is_pdf else ".html"
        rendered = response is None
        if not is_pdf and allow_browser_fallback:
            blocked = self._looks_like_block_page(body)
            too_empty = len(body) < 2000
            if blocked or too_empty:
                body = self._render_with_playwright(final_url)
                rendered = True
        path = self._cache_file(final_url, suffix)
        path.write_bytes(body)
        return FetchedResource(final_url, body, content_type, path, rendered)

    def _scan_wordpress_date_range(
        self,
        publication_url: str,
        start_date: date,
        end_date: date,
        adapter_key: str,
        post_type: str,
    ) -> ScanResult:
        result = ScanResult(stop_reason="官方 WordPress API 已覆盖指定日期闭区间")
        parts = urlsplit(publication_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        inclusive_start = datetime.combine(start_date, datetime_time.min) - timedelta(seconds=1)
        exclusive_end = datetime.combine(end_date + timedelta(days=1), datetime_time.min)
        start_value = inclusive_start.isoformat()
        end_value = exclusive_end.isoformat()
        page_number = 1
        total_pages = 1
        while page_number <= total_pages:
            if result.pages_scanned >= self.config.max_listing_pages:
                result.coverage_status = "incomplete"
                result.stop_reason = f"官方 API 达到安全上限 {self.config.max_listing_pages} 页"
                break
            request = self.client.build_request(
                "GET",
                f"{origin}/wp-json/wp/v2/{post_type}",
                params={
                    "per_page": 100,
                    "page": page_number,
                    "after": start_value,
                    "before": end_value,
                    "orderby": "date",
                    "order": "desc",
                    "_fields": "link,title,date",
                },
            )
            response = self._request(str(request.url))
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"{adapter_key} 官方 API 返回了非列表数据")
            result.pages_scanned += 1
            total_pages = max(1, int(response.headers.get("x-wp-totalpages", "1")))
            for item in payload:
                if not isinstance(item, dict):
                    continue
                url = canonicalize_url(str(item.get("link", "")))
                title_value = item.get("title", {})
                title = title_value.get("rendered", "") if isinstance(title_value, dict) else str(title_value)
                title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
                if url:
                    result.links.append(DiscoveredLink(url, title))
                raw_date = str(item.get("date", ""))
                try:
                    published = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
                except ValueError:
                    continue
                result.earliest_date = min(filter(None, [result.earliest_date, published]))
                result.latest_date = max(filter(None, [result.latest_date, published]))
            page_number += 1
        return result

    def _probe_page_dates(self, links: list[DiscoveredLink]) -> list[date]:
        dates: set[date] = set()
        for link in (links[0], links[-1]) if len(links) > 1 else links:
            try:
                resource = self.fetch(link.url)
                dates.update(discover_listing_dates(resource.body))
            except Exception:
                continue
        return sorted(dates, reverse=True)

    def scan_listing(
        self,
        publication_url: str,
        start_date: date,
        ordered_by_date: bool,
        adapter_key: str = "generic",
        end_date: date | None = None,
    ) -> ScanResult:
        adapter = get_adapter(adapter_key)
        if adapter and adapter.wordpress_post_type:
            try:
                return self._scan_wordpress_date_range(
                    publication_url,
                    start_date,
                    end_date or date.today(),
                    adapter_key,
                    adapter.wordpress_post_type,
                )
            except Exception as exc:
                return ScanResult(
                    coverage_status="failed",
                    stop_reason="官方 WordPress API 日期范围检索失败",
                    errors=[str(exc)],
                )
        result = ScanResult()
        seen_pages: set[str] = set()
        seen_links: dict[str, DiscoveredLink] = {}
        current = canonicalize_url(publication_url, preserve_trailing_slash=True)
        older_page_seen = False
        while current and current not in seen_pages:
            if result.pages_scanned >= self.config.max_listing_pages:
                result.coverage_status = "incomplete"
                result.stop_reason = f"达到安全上限 {self.config.max_listing_pages} 页"
                break
            seen_pages.add(current)
            try:
                resource = self.fetch(current)
            except Exception as exc:
                result.coverage_status = "failed" if result.pages_scanned == 0 else "incomplete"
                result.errors.append(str(exc))
                result.stop_reason = "栏目页访问失败"
                break
            result.pages_scanned += 1
            page_links: list[DiscoveredLink] = []
            selectors = adapter.listing_link_selectors if adapter else ()
            for url, title in discover_content_links(resource.body, resource.url, selectors):
                if adapter and not adapter.accepts(url):
                    continue
                item = DiscoveredLink(url, title)
                seen_links.setdefault(url, item)
                page_links.append(item)
            dates = discover_listing_dates(resource.body)
            if not dates and ordered_by_date and adapter and adapter.probe_listing_dates and page_links:
                dates = self._probe_page_dates(page_links)
            if dates:
                page_earliest, page_latest = min(dates), max(dates)
                result.earliest_date = min(filter(None, [result.earliest_date, page_earliest]))
                result.latest_date = max(filter(None, [result.latest_date, page_latest]))
                if ordered_by_date and page_earliest < start_date:
                    if older_page_seen:
                        result.stop_reason = "日期排序栏目越过起始日后额外检查一页"
                        break
                    older_page_seen = True
            nxt = next_page_url(resource.body, resource.url)
            if not nxt:
                result.stop_reason = "已到栏目末页"
                break
            current = nxt
        else:
            if current in seen_pages:
                result.coverage_status = "incomplete"
                result.stop_reason = "检测到分页循环"
        result.links = list(seen_links.values())
        if not result.links and result.coverage_status == "complete":
            result.coverage_status = "incomplete"
            result.stop_reason = "栏目页未识别到任何候选详情链接，不能据此判定无符合内容"
            result.errors.append("页面结构可能已变化，需要检查来源适配器")
        if not result.stop_reason:
            result.stop_reason = "栏目遍历完成"
        return result

    def extract(self, link: DiscoveredLink) -> tuple[ExtractedDocument, FetchedResource]:
        resource = self.fetch(link.url)
        is_pdf = "application/pdf" in resource.content_type or resource.cache_path.suffix == ".pdf"
        if is_pdf:
            document = extract_pdf_document(resource.body, resource.url, link.listing_title)
        else:
            document = extract_html_document(resource.body, resource.url)
            pdf_links = discover_pdf_links(resource.body, resource.url)
            if pdf_links and (document.english_word_count < 1500 and document.chinese_char_count < 3000):
                try:
                    pdf_resource = self.fetch(pdf_links[0], allow_browser_fallback=False)
                    pdf_document = extract_pdf_document(pdf_resource.body, pdf_resource.url, document.title)
                    if len(pdf_document.text) > len(document.text):
                        pdf_document.title = document.title or pdf_document.title
                        pdf_document.published_date = document.published_date
                        pdf_document.date_status = document.date_status
                        pdf_document.date_evidence = document.date_evidence
                        if document.content_type != "unknown":
                            pdf_document.content_type = document.content_type
                            pdf_document.content_type_evidence = document.content_type_evidence
                        pdf_document.warnings.extend(document.warnings)
                        document, resource = pdf_document, pdf_resource
                except Exception as exc:
                    document.warnings.append(f"详情页 PDF 全文跟进失败: {exc}")
        if not document.title:
            document.title = link.listing_title
        return document, resource
