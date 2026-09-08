from __future__ import annotations

import html
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from urllib import robotparser
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import AppConfig
from .constants import DateScope
from .extractors import (
    ExtractedDocument,
    DiscoveredAttachment,
    discover_content_cards,
    discover_listing_dates,
    discover_pdf_links,
    extract_html_document,
    extract_pdf_document,
    next_page_url,
)
from .adapters import merge_selectors, resolve_effective_adapter
from .paths import PATHS
from .url_utils import canonicalize_url, normalize_fetch_url
from .url_utils import ContentTypeEvidence, collect_content_type_evidence, resolve_content_type


USER_AGENT = "ThinkTankTracker/0.6.1 (+local research audit tool)"
BOUNDARY_VALIDATION_ITEMS = 10


@dataclass
class DiscoveredLink:
    fetch_url: str
    canonical_url: str
    listing_url: str = ""
    listing_title: str = ""
    listing_date: date | None = None
    listing_date_evidence: str = ""
    listing_content_type: str = ""
    listing_content_type_evidence: str = ""
    listing_authors: tuple[str, ...] = ()
    listing_author_evidence: str = ""
    card_position: int = 0
    is_featured: bool = False
    resolved_date: date | None = None
    resolved_date_status: str = "missing"
    resolved_date_evidence: str = ""
    date_scope: str = DateScope.DATE_UNKNOWN
    preflight_resource: "FetchedResource | None" = None
    preflight_document: ExtractedDocument | None = None
    preflight_error: str = ""
    adapter_key: str = "generic"
    listing_content_type_labels: tuple[str, ...] = ()
    listing_content_type_evidence_items: tuple[ContentTypeEvidence, ...] = ()
    detail_content_type_selectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchAttempt:
    url: str
    status_code: int | None
    outcome: str


@dataclass
class ScanResult:
    links: list[DiscoveredLink] = field(default_factory=list)
    pages_scanned: int = 0
    earliest_date: date | None = None
    latest_date: date | None = None
    coverage_status: str = "complete"
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)
    page_audits: list["PageScanAudit"] = field(default_factory=list)
    ordering_status: str = "unchecked"
    boundary_page_number: int | None = None
    next_page_url: str = ""


@dataclass
class PageScanAudit:
    page_url: str
    page_number: int
    cards_found: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    in_window_count: int = 0
    after_end_count: int = 0
    before_start_count: int = 0
    date_unknown_count: int = 0
    earliest_date: date | None = None
    latest_date: date | None = None
    ordering_status: str = "unchecked"
    is_boundary_page: bool = False
    stop_evidence: str = ""
    sample_links: list[str] = field(default_factory=list)


@dataclass
class FetchedResource:
    url: str
    body: bytes
    content_type: str
    cache_path: Path
    rendered: bool = False
    attempts: list[FetchAttempt] = field(default_factory=list)
    status_code: int | None = None


@dataclass
class AttachmentResult:
    link: DiscoveredAttachment
    resource: FetchedResource | None = None
    document: ExtractedDocument | None = None
    used_as_body: bool = False
    error: str = ""
    status_code: int | None = None


class FetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        attempts: list[FetchAttempt] | None = None,
        *,
        retriable: bool = False,
        code: str = "fetch_error",
    ) -> None:
        super().__init__(message)
        self.attempts = attempts or []
        self.retriable = retriable
        self.code = code


_BROWSER_SEMAPHORE = threading.Semaphore(1)


def bundled_chromium_executable(browser_root: Path) -> Path | None:
    candidates = list(browser_root.glob("chromium-*/chrome-win64/chrome.exe"))
    if not candidates:
        return None

    def revision(path: Path) -> int:
        match = re.search(r"chromium-(\d+)", str(path))
        return int(match.group(1)) if match else 0

    return max(candidates, key=revision)


class DomainRateLimiter:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, threading.Lock] = {}

    @contextmanager
    def slot(self, url: str):
        host = urlsplit(url).hostname or ""
        with self._lock:
            host_lock = self._host_locks.setdefault(host, threading.Lock())
        with host_lock:
            elapsed = time.monotonic() - self._last_request.get(host, 0.0)
            if elapsed < self.delay_seconds:
                time.sleep(self.delay_seconds - elapsed)
            try:
                yield
            finally:
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
                with self.limiter.slot(parser.url):
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
                with self.limiter.slot(url):
                    response = self.client.get(url)
                if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    if attempt < 2:
                        time.sleep(2**attempt)
                        continue
                    raise FetchError(
                        f"瞬时 HTTP 错误重试 3 次仍失败: {response.status_code}",
                        [FetchAttempt(url, response.status_code, "transient_http")],
                        retriable=True,
                        code="transient_http",
                    )
                if response.status_code != 404 and response.status_code >= 400:
                    raise FetchError(
                        f"HTTP {response.status_code}: {url}",
                        [FetchAttempt(url, response.status_code, "http_error")],
                        code="http_error",
                    )
                return response
            except FetchError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
                last_error = exc
                message = str(exc)
                if "10013" in message or "access denied" in message.lower():
                    raise FetchError(
                        f"本机拒绝后台进程联网: {message}",
                        [FetchAttempt(url, None, f"network_access_denied: {message}")],
                        code="network_access_denied",
                    ) from exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise FetchError(
            f"网络请求重试 3 次仍失败: {last_error}",
            [FetchAttempt(url, None, f"transient_network: {last_error}")],
            retriable=True,
            code="transient_network",
        )

    @staticmethod
    def _slash_variant(url: str) -> str | None:
        parts = urlsplit(url)
        final_segment = parts.path.rsplit("/", 1)[-1]
        if not parts.path or parts.path == "/":
            return None
        if parts.path.endswith("/"):
            trimmed = parts.path.rstrip("/")
            final_segment = trimmed.rsplit("/", 1)[-1]
            if "." in final_segment:
                return None
            path = trimmed
        else:
            if "." in final_segment:
                return None
            path = f"{parts.path}/"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

    @staticmethod
    def _looks_like_custom_404(body: bytes) -> bool:
        sample = body[:20_000].lower()
        title_404 = re.search(
            rb"<title[^>]*>\s*(?:error\s*)?404(?:\s*[-|:]\s*[^<]*)?</title>", sample
        )
        title_missing = re.search(
            rb"<title[^>]*>[^<]*(?:page not found|not found)[^<]*</title>", sample
        )
        return bool(title_404 or title_missing) or any(
            marker in sample
            for marker in (b"body-error_404", b'body class="error404', b'body class="error-404', b'id="page-404')
        )

    @staticmethod
    def _looks_like_block_page(body: bytes) -> bool:
        sample = body[:20_000].lower()
        return any(
            marker in sample
            for marker in (
                b"cf-chl-",
                b'id="challenge-form"',
                b"<title>just a moment",
                b"<title>one moment, please",
                b"<title>access denied",
                b"<title>enable javascript",
                b"request is being verified",
            )
        )

    def _render_with_playwright(self, url: str) -> bytes:
        browser_path = PATHS.root / "runtime" / "browsers"
        if browser_path.exists():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_path))
        from playwright.sync_api import sync_playwright

        with _BROWSER_SEMAPHORE:
            with sync_playwright() as playwright:
                launch_options: dict[str, object] = {
                    "headless": True,
                    "args": ["--disable-blink-features=AutomationControlled"],
                    "ignore_default_args": ["--enable-automation"],
                }
                chromium_executable = bundled_chromium_executable(browser_path)
                if chromium_executable:
                    launch_options["executable_path"] = str(chromium_executable)
                browser = playwright.chromium.launch(**launch_options)
                try:
                    browser_version = browser.version
                    context = browser.new_context(
                        locale="en-US",
                        viewport={"width": 1366, "height": 768},
                        screen={"width": 1366, "height": 768},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            f"Chrome/{browser_version} Safari/537.36"
                        ),
                    )
                    context.add_init_script(
                        "Object.defineProperty(Navigator.prototype, 'webdriver', "
                        "{get: () => undefined});"
                    )
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)

                    deadline = time.monotonic() + 20
                    body = b""
                    while time.monotonic() < deadline:
                        try:
                            body = page.content().encode("utf-8")
                        except Exception:
                            page.wait_for_timeout(250)
                            continue
                        if not self._looks_like_block_page(body):
                            break
                        page.wait_for_timeout(250)
                    if self._looks_like_block_page(body):
                        raise RuntimeError("浏览器仍停留在站点验证页")
                    try:
                        page.wait_for_load_state("load", timeout=15_000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2_000)
                    body = page.content().encode("utf-8")
                finally:
                    browser.close()
        return body

    def fetch(self, url: str, allow_browser_fallback: bool = True) -> FetchedResource:
        requested_url = normalize_fetch_url(url)
        attempt_url = requested_url
        attempts: list[FetchAttempt] = []
        try:
            response = self._request(requested_url)
            is_soft_404 = "html" in response.headers.get("content-type", "").lower() and self._looks_like_custom_404(response.content)
            attempts.append(
                FetchAttempt(requested_url, response.status_code, "soft_404" if is_soft_404 else "response")
            )
            if response.status_code == 404 or is_soft_404:
                slash_variant = self._slash_variant(requested_url)
                if slash_variant:
                    attempt_url = slash_variant
                    retried = self._request(slash_variant)
                    retry_soft_404 = "html" in retried.headers.get("content-type", "").lower() and self._looks_like_custom_404(retried.content)
                    attempts.append(
                        FetchAttempt(
                            slash_variant,
                            retried.status_code,
                            "soft_404" if retry_soft_404 else "response",
                        )
                    )
                    if retried.status_code != 404 and not retry_soft_404:
                        response = retried
                        is_soft_404 = False
            if response.status_code == 404 or is_soft_404:
                raise FetchError(
                    "页面不存在；原始链接及末尾斜杠备用链接均未返回有效正文",
                    attempts,
                )
            final_url = str(response.url)
            content_type = response.headers.get("content-type", "").lower()
            body = response.content
        except PermissionError:
            raise
        except FetchError:
            raise
        except Exception as exc:
            if not attempts or attempts[-1].url != attempt_url or attempts[-1].status_code is not None:
                attempts.append(FetchAttempt(attempt_url, None, f"error: {type(exc).__name__}: {exc}"))
            raise FetchError(str(exc), attempts, retriable=False) from exc
        has_pdf_magic = body.lstrip().startswith(b"%PDF-")
        is_pdf = (
            "application/pdf" in content_type
            or urlsplit(final_url).path.lower().endswith(".pdf")
            or has_pdf_magic
        )
        if has_pdf_magic and "application/pdf" not in content_type:
            content_type = "application/pdf; detected=magic"
        suffix = ".pdf" if is_pdf else ".html"
        rendered = response is None
        if not is_pdf and allow_browser_fallback:
            blocked = self._looks_like_block_page(body)
            too_empty = len(body) < 2000
            if blocked or too_empty:
                try:
                    body = self._render_with_playwright(final_url)
                    rendered = True
                    attempts.append(FetchAttempt(final_url, response.status_code if response else None, "playwright_fallback"))
                except Exception as exc:
                    message = str(exc)
                    code = "network_access_denied" if "ERR_NETWORK_ACCESS_DENIED" in message else "browser_render_failed"
                    raise FetchError(
                        f"浏览器渲染失败: {message}",
                        attempts + [FetchAttempt(final_url, None, code)],
                        retriable=code != "network_access_denied",
                        code=code,
                    ) from exc
        path = self._cache_file(final_url, suffix)
        path.write_bytes(body)
        return FetchedResource(
            final_url,
            body,
            content_type,
            path,
            rendered,
            attempts,
            response.status_code if response is not None else None,
        )

    def _scan_wordpress_date_range(
        self,
        publication_url: str,
        start_date: date,
        end_date: date,
        adapter_key: str,
        post_type: str,
        on_link: Callable[[DiscoveredLink], None] | None = None,
        on_page: Callable[[PageScanAudit, bool], None] | None = None,
        detail_content_type_selectors: tuple[str, ...] = (),
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
                    "_embed": "wp:term",
                    "_fields": "link,title,date,type-of-publication,_links,_embedded",
                },
            )
            response = self._request(str(request.url))
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"{adapter_key} 官方 API 返回了非列表数据")
            result.pages_scanned += 1
            total_pages = max(1, int(response.headers.get("x-wp-totalpages", "1")))
            page_links: list[DiscoveredLink] = []
            for position, item in enumerate(payload, 1):
                if not isinstance(item, dict):
                    continue
                url = normalize_fetch_url(str(item.get("link", "")))
                title_value = item.get("title", {})
                title = title_value.get("rendered", "") if isinstance(title_value, dict) else str(title_value)
                title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
                raw_date = str(item.get("date", ""))
                try:
                    published = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
                except ValueError:
                    published = None
                labels = self._wordpress_content_type_labels(item)
                content_type, type_evidence, _ = resolve_content_type(
                    url,
                    labels,
                    self.config.content_type_rules,
                )
                type_evidence = type_evidence.replace(
                    "栏目卡片", "WordPress API taxonomy"
                )
                type_evidence_items = collect_content_type_evidence(
                    url,
                    labels,
                    self.config.content_type_rules,
                    label_location="wordpress_taxonomy",
                )
                if url:
                    link = DiscoveredLink(
                        fetch_url=url,
                        canonical_url=canonicalize_url(url),
                        listing_url=publication_url,
                        listing_title=title,
                        listing_date=published,
                        listing_date_evidence=f"WordPress API date: {raw_date}",
                        listing_content_type=(
                            "" if content_type == "unknown" else content_type
                        ),
                        listing_content_type_evidence=type_evidence,
                        card_position=position,
                        resolved_date=published,
                        resolved_date_status="confirmed" if published else "missing",
                        resolved_date_evidence=f"WordPress API date: {raw_date}",
                        date_scope=DateScope.IN_WINDOW if published else DateScope.DATE_UNKNOWN,
                        adapter_key=adapter_key,
                        listing_content_type_labels=tuple(labels),
                        listing_content_type_evidence_items=tuple(type_evidence_items),
                        detail_content_type_selectors=detail_content_type_selectors,
                    )
                    result.links.append(link)
                    page_links.append(link)
                if published is None:
                    continue
                result.earliest_date = min(filter(None, [result.earliest_date, published]))
                result.latest_date = max(filter(None, [result.latest_date, published]))
            dates = [link.resolved_date for link in page_links if link.resolved_date]
            audit = PageScanAudit(
                page_url=str(request.url),
                page_number=page_number,
                cards_found=len(payload),
                accepted_count=len(page_links),
                in_window_count=sum(link.date_scope == DateScope.IN_WINDOW for link in page_links),
                date_unknown_count=sum(link.date_scope == DateScope.DATE_UNKNOWN for link in page_links),
                earliest_date=min(dates) if dates else None,
                latest_date=max(dates) if dates else None,
                ordering_status="api_ordered",
                sample_links=[link.fetch_url for link in page_links[:5]],
            )
            result.page_audits.append(audit)
            if on_page:
                on_page(audit, False)
            if on_link:
                for link in page_links:
                    on_link(link)
            if on_page:
                on_page(audit, True)
            page_number += 1
        result.ordering_status = "api_ordered"
        typed_count = sum(
            bool(link.listing_content_type_labels) for link in result.links
        )
        result.stop_reason = (
            "官方 WordPress API 已完整覆盖指定日期闭区间；"
            f"共识别 {len(result.links)} 篇，其中 {typed_count} 篇带 publication taxonomy 体裁证据"
        )
        return result

    @staticmethod
    def _wordpress_content_type_labels(item: dict[str, object]) -> list[str]:
        embedded = item.get("_embedded")
        if not isinstance(embedded, dict):
            return []
        term_groups = embedded.get("wp:term")
        if not isinstance(term_groups, list):
            return []
        accepted_taxonomies = {
            "format",
            "genre",
            "publication-format",
            "publication-type",
            "type-of-publication",
        }
        labels: list[str] = []
        for group in term_groups:
            if not isinstance(group, list):
                continue
            for term in group:
                if not isinstance(term, dict):
                    continue
                taxonomy = re.sub(
                    r"[^a-z0-9]+", "-", str(term.get("taxonomy", "")).casefold()
                ).strip("-")
                if taxonomy not in accepted_taxonomies:
                    continue
                name = html.unescape(
                    re.sub(r"<[^>]+>", "", str(term.get("name", "")))
                ).strip()
                if name:
                    labels.append(name)
        return list(dict.fromkeys(labels))

    def _probe_page_dates(self, links: list[DiscoveredLink]) -> list[date]:
        dates: set[date] = set()
        for link in (links[0], links[-1]) if len(links) > 1 else links:
            try:
                resource = self.fetch(link.fetch_url)
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
        *,
        listing_selectors: tuple[str, ...] = (),
        container_selectors: tuple[str, ...] = (),
        content_type_selectors: tuple[str, ...] = (),
        date_selectors: tuple[str, ...] = (),
        allowed_path_patterns: tuple[str, ...] = (),
        excluded_path_patterns: tuple[str, ...] = (),
        resume_url: str = "",
        on_link: Callable[[DiscoveredLink], None] | None = None,
        on_page: Callable[[PageScanAudit, bool], None] | None = None,
        date_assist: Callable[[DiscoveredLink, FetchedResource, ExtractedDocument], tuple[date | None, str]] | None = None,
        page_number_offset: int = 0,
    ) -> ScanResult:
        adapter_key, adapter = resolve_effective_adapter(adapter_key, publication_url)
        if adapter and adapter.wordpress_post_type:
            try:
                return self._scan_wordpress_date_range(
                    publication_url,
                    start_date,
                    end_date or date.today(),
                    adapter_key,
                    adapter.wordpress_post_type,
                    on_link=on_link,
                    on_page=on_page,
                    detail_content_type_selectors=content_type_selectors,
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
        current = normalize_fetch_url(resume_url or publication_url)
        entry_host = (urlsplit(current).hostname or "").lower().removeprefix("www.")
        boundary_page_number: int | None = None
        boundary_seen = False
        verified_after_boundary = 0
        ordering_broken = False
        last_known_date: date | None = None
        saw_unknown_date = False
        effective_end = end_date or date.today()

        def accepted_by_rules(url: str) -> bool:
            path = urlsplit(url).path.lower()
            if excluded_path_patterns and any(re.search(pattern, path) for pattern in excluded_path_patterns):
                return False
            if allowed_path_patterns:
                return any(re.search(pattern, path) for pattern in allowed_path_patterns)
            return adapter.accepts(url) if adapter else True

        def classify(published: date | None, status: str) -> str:
            if status != "confirmed" or published is None:
                return DateScope.DATE_UNKNOWN
            if published < start_date:
                return DateScope.BEFORE_START
            if published > effective_end:
                return DateScope.AFTER_END
            return DateScope.IN_WINDOW

        def preflight(item: DiscoveredLink) -> None:
            if item.listing_date is not None:
                item.resolved_date = item.listing_date
                item.resolved_date_status = "confirmed"
                item.resolved_date_evidence = item.listing_date_evidence
                item.date_scope = classify(item.resolved_date, item.resolved_date_status)
                return
            if urlsplit(item.fetch_url).path.lower().endswith(".pdf"):
                item.preflight_error = "直接 PDF 链接缺少栏目日期，需要人工确认"
                return
            try:
                resource = self.fetch(item.fetch_url, allow_browser_fallback=False)
                detail_type_selectors = merge_selectors(
                    content_type_selectors,
                    adapter.detail_content_type_selectors if adapter else (),
                )
                detail_author_selectors = adapter.detail_author_selectors if adapter else ()
                if detail_type_selectors or detail_author_selectors:
                    document = extract_html_document(
                        resource.body,
                        resource.url,
                        self.config.content_type_rules,
                        detail_type_selectors,
                        detail_author_selectors,
                    )
                else:
                    document = extract_html_document(
                        resource.body,
                        resource.url,
                        self.config.content_type_rules,
                    )
                item.preflight_resource = resource
                item.preflight_document = document
                item.resolved_date = document.published_date
                item.resolved_date_status = document.date_status
                item.resolved_date_evidence = document.date_evidence
                if item.listing_content_type == "" and document.content_type != "unknown":
                    item.listing_content_type = document.content_type
                    item.listing_content_type_evidence = document.content_type_evidence
                    item.listing_content_type_labels = tuple(document.content_type_labels)
                    item.listing_content_type_evidence_items = tuple(
                        document.content_type_evidence_items
                    )
                if not item.listing_authors and document.authors:
                    item.listing_authors = tuple(document.authors)
                    item.listing_author_evidence = document.author_evidence
                if not item.listing_title and document.title:
                    item.listing_title = document.title
                if item.resolved_date_status != "confirmed" and date_assist:
                    suggested, evidence = date_assist(item, resource, document)
                    if suggested:
                        item.resolved_date = suggested
                        item.resolved_date_status = "confirmed"
                        item.resolved_date_evidence = evidence
                item.date_scope = classify(item.resolved_date, item.resolved_date_status)
            except Exception as exc:
                item.preflight_error = str(exc)
                item.resolved_date_status = "missing"
                item.date_scope = DateScope.DATE_UNKNOWN

        while current:
            page_key = canonicalize_url(current)
            if page_key in seen_pages:
                result.coverage_status = "incomplete"
                result.stop_reason = "检测到分页循环"
                break
            if result.pages_scanned >= self.config.max_listing_pages:
                result.coverage_status = "incomplete"
                result.stop_reason = f"达到安全上限 {self.config.max_listing_pages} 页"
                break
            seen_pages.add(page_key)
            try:
                resource = self.fetch(current)
            except Exception as exc:
                result.coverage_status = "failed" if result.pages_scanned == 0 else "incomplete"
                result.errors.append(str(exc))
                result.stop_reason = "栏目页访问失败"
                break
            result.pages_scanned += 1
            page_links: list[DiscoveredLink] = []
            new_page_links: list[DiscoveredLink] = []
            pending_canonicals: set[str] = set()
            selectors = merge_selectors(
                listing_selectors, adapter.listing_link_selectors if adapter else ()
            )
            containers = merge_selectors(
                container_selectors, adapter.listing_container_selectors if adapter else ()
            )
            type_selectors = merge_selectors(
                content_type_selectors,
                adapter.listing_content_type_selectors if adapter else (),
            )
            cards = discover_content_cards(
                resource.body,
                resource.url,
                selectors,
                date_selectors,
                self.config.content_type_rules,
                containers,
                type_selectors,
                adapter.listing_author_selectors if adapter else (),
            )
            rejected_count = 0
            for card in cards:
                if not accepted_by_rules(card.fetch_url):
                    rejected_count += 1
                    continue
                item = DiscoveredLink(
                    card.fetch_url,
                    card.canonical_url,
                    resource.url,
                    card.title,
                    card.published_date,
                    card.date_evidence,
                    card.content_type,
                    card.content_type_evidence,
                    card.authors,
                    card.author_evidence,
                    card.position,
                    card.is_featured,
                )
                item.adapter_key = adapter_key
                item.listing_content_type_labels = card.content_type_labels
                item.listing_content_type_evidence_items = card.content_type_evidence_items
                item.detail_content_type_selectors = content_type_selectors
                if card.canonical_url not in seen_links and card.canonical_url not in pending_canonicals:
                    pending_canonicals.add(card.canonical_url)
                    new_page_links.append(item)
                page_links.append(item)
            processed_links: list[DiscoveredLink] = []
            page_number = page_number_offset + result.pages_scanned
            for item in new_page_links:
                preflight(item)
                processed_links.append(item)
                seen_links[item.canonical_url] = item
                # Featured/pinned content is still persisted and date-classified,
                # but its placement is editorial and says nothing about chronology.
                if item.is_featured:
                    continue
                if item.resolved_date is None or item.resolved_date_status != "confirmed":
                    saw_unknown_date = True
                    continue
                if last_known_date is not None and item.resolved_date > last_known_date:
                    ordering_broken = True
                last_known_date = item.resolved_date
                if ordered_by_date and not ordering_broken:
                    if not boundary_seen:
                        if item.date_scope == DateScope.BEFORE_START:
                            boundary_seen = True
                            boundary_page_number = page_number
                        continue
                    if item.date_scope == DateScope.BEFORE_START:
                        verified_after_boundary += 1
                    else:
                        verified_after_boundary = 0
                    if not saw_unknown_date and verified_after_boundary >= BOUNDARY_VALIDATION_ITEMS:
                        break
            new_page_links = processed_links
            dates = [item.resolved_date for item in new_page_links if item.resolved_date]
            if dates:
                page_earliest, page_latest = min(dates), max(dates)
                result.earliest_date = min(filter(None, [result.earliest_date, page_earliest]))
                result.latest_date = max(filter(None, [result.latest_date, page_latest]))
            scopes = [item.date_scope for item in new_page_links]
            audit = PageScanAudit(
                page_url=resource.url,
                page_number=page_number,
                cards_found=len(cards),
                accepted_count=len(new_page_links),
                rejected_count=rejected_count,
                in_window_count=sum(scope == DateScope.IN_WINDOW for scope in scopes),
                after_end_count=sum(scope == DateScope.AFTER_END for scope in scopes),
                before_start_count=sum(scope == DateScope.BEFORE_START for scope in scopes),
                date_unknown_count=sum(scope == DateScope.DATE_UNKNOWN for scope in scopes),
                earliest_date=min(dates) if dates else None,
                latest_date=max(dates) if dates else None,
                ordering_status="unordered" if ordering_broken else "verified_descending" if dates else "unknown",
                is_boundary_page=boundary_page_number == page_number,
                sample_links=[item.fetch_url for item in new_page_links[:5]],
            )
            stop_after_page = (
                ordered_by_date
                and not ordering_broken
                and not saw_unknown_date
                and verified_after_boundary >= BOUNDARY_VALIDATION_ITEMS
            )
            if stop_after_page:
                audit.stop_evidence = (
                    f"越过起始日边界后连续验证 {BOUNDARY_VALIDATION_ITEMS} 条新发现正式内容，"
                    "日期均已确认且早于起始日"
                )
                result.stop_reason = (
                    f"越过起始日边界后连续 {BOUNDARY_VALIDATION_ITEMS} 条正式内容均早于起始日"
                )
            result.page_audits.append(audit)
            if on_page:
                on_page(audit, False)
            if on_link:
                for item in new_page_links:
                    try:
                        on_link(item)
                    finally:
                        item.preflight_resource = None
                        item.preflight_document = None
            if on_page:
                on_page(audit, True)
            if stop_after_page:
                break
            nxt = next_page_url(
                resource.body,
                resource.url,
                publication_url,
                strict_listing_path=bool(adapter and adapter.strict_listing_path),
            )
            if not nxt:
                result.stop_reason = "已到栏目末页"
                break
            next_host = (urlsplit(nxt).hostname or "").lower().removeprefix("www.")
            if next_host != entry_host:
                result.coverage_status = "incomplete"
                result.stop_reason = "分页链接跳转到入口站点之外，已停止"
                result.errors.append(f"拒绝站外分页链接: {nxt}")
                break
            current = nxt
        result.links = list(seen_links.values())
        result.boundary_page_number = boundary_page_number
        result.ordering_status = (
            "unordered"
            if ordering_broken
            else "unknown"
            if last_known_date is None
            else "verified_descending"
        )
        result.next_page_url = current or ""
        if saw_unknown_date:
            result.coverage_status = "incomplete"
            result.errors.append("存在无法确认发布日期的正式内容，已完整扫描可达分页")
        if ordering_broken:
            result.errors.append("检测到发布日期逆序，已取消提前停止")
        if not result.links and result.coverage_status == "complete":
            result.coverage_status = "incomplete"
            result.stop_reason = "栏目页未识别到任何候选详情链接，不能据此判定无符合内容"
            result.errors.append("页面结构可能已变化，需要检查来源适配器")
        if not result.stop_reason:
            result.stop_reason = "栏目遍历完成"
        return result

    def extract(
        self, link: DiscoveredLink
    ) -> tuple[ExtractedDocument, FetchedResource, list[AttachmentResult]]:
        resource = link.preflight_resource or self.fetch(link.fetch_url)
        _, adapter = resolve_effective_adapter(link.adapter_key, link.fetch_url)
        is_pdf = "application/pdf" in resource.content_type or resource.cache_path.suffix == ".pdf"
        attachments: list[AttachmentResult] = []
        if is_pdf:
            if not resource.body.lstrip().startswith(b"%PDF-"):
                raise FetchError(
                    "PDF 地址未返回 %PDF 文件头，已拒绝作为正文",
                    resource.attempts,
                    code="invalid_pdf",
                )
            document = extract_pdf_document(
                resource.body,
                resource.url,
                link.listing_title,
                self.config.content_type_rules,
                pdf_parser_mode=self.config.pdf_parser_mode,
            )
            direct = DiscoveredAttachment(
                resource.url,
                canonicalize_url(resource.url),
                link.listing_title,
                "primary",
                100,
            )
            attachments.append(AttachmentResult(direct, resource, document, True))
        else:
            detail_type_selectors = merge_selectors(
                link.detail_content_type_selectors,
                adapter.detail_content_type_selectors if adapter else (),
            )
            detail_author_selectors = adapter.detail_author_selectors if adapter else ()
            if detail_type_selectors or detail_author_selectors:
                document = link.preflight_document or extract_html_document(
                    resource.body,
                    resource.url,
                    self.config.content_type_rules,
                    detail_type_selectors,
                    detail_author_selectors,
                )
            else:
                # Preserve the generic extractor call contract used by local adapters.
                document = link.preflight_document or extract_html_document(
                    resource.body,
                    resource.url,
                    self.config.content_type_rules,
                )
            pdf_links = discover_pdf_links(resource.body, resource.url, document.title or link.listing_title)
            for pdf_link in pdf_links[:5]:
                pdf_resource: FetchedResource | None = None
                try:
                    pdf_resource = self.fetch(pdf_link.url, allow_browser_fallback=False)
                    if not pdf_resource.body.lstrip().startswith(b"%PDF-"):
                        raise FetchError(
                            "附件地址未返回 %PDF 文件头",
                            pdf_resource.attempts,
                            code="invalid_pdf",
                        )
                    pdf_document = extract_pdf_document(
                        pdf_resource.body,
                        pdf_resource.url,
                        document.title,
                        self.config.content_type_rules,
                        pdf_parser_mode=self.config.pdf_parser_mode,
                    )
                    attachments.append(
                        AttachmentResult(
                            pdf_link,
                            pdf_resource,
                            pdf_document,
                            status_code=pdf_resource.status_code,
                        )
                    )
                except Exception as exc:
                    attempts = exc.attempts if isinstance(exc, FetchError) else []
                    status_code = (
                        pdf_resource.status_code
                        if pdf_resource is not None
                        else attempts[-1].status_code
                        if attempts
                        else None
                    )
                    attachments.append(
                        AttachmentResult(
                            pdf_link,
                            resource=pdf_resource,
                            error=str(exc),
                            status_code=status_code,
                        )
                    )
            primary_results = [
                item
                for item in attachments
                if item.link.role == "primary"
                and item.document is not None
                and item.document.extraction_status == "complete"
            ]
            if primary_results:
                best = max(
                    primary_results,
                    key=lambda item: (item.link.score, len(item.document.text) if item.document else 0),
                )
                pdf_document = best.document
                assert pdf_document is not None
                if len(pdf_document.text) > len(document.text) or document.extraction_status != "complete":
                    best.used_as_body = True
                    html_document = document
                    if html_document.title:
                        pdf_document.title = html_document.title
                    if html_document.published_date:
                        pdf_document.published_date = html_document.published_date
                        pdf_document.date_status = html_document.date_status
                        pdf_document.date_evidence = html_document.date_evidence
                    if html_document.content_type != "unknown":
                        pdf_document.content_type = html_document.content_type
                        pdf_document.content_type_evidence = html_document.content_type_evidence
                    if html_document.authors:
                        pdf_document.authors = html_document.authors
                        pdf_document.author_evidence = html_document.author_evidence
                    pdf_document.abstract = html_document.abstract
                    pdf_document.tags = html_document.tags
                    pdf_document.headings = list(
                        dict.fromkeys([*html_document.headings, *pdf_document.headings])
                    )[:30]
                    pdf_document.warnings.extend(html_document.warnings)
                    document = pdf_document
            for item in attachments:
                if item.error:
                    document.warnings.append(
                        f"PDF 附件跟进失败（{item.link.link_text or item.link.url}）: {item.error}"
                    )
        if not document.title:
            document.title = link.listing_title
        return document, resource, attachments
