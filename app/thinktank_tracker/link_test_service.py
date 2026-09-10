from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import urlsplit

from .adapters import merge_selectors, resolve_effective_adapter
from .config import AppConfig
from .crawler import DomainRateLimiter, FetchError, WebCrawler
from .extractors import discover_content_cards, next_page_url
from .models import SourceEntry


@dataclass(frozen=True)
class EntryTestSnapshot:
    id: int
    url: str
    adapter_key: str
    listing_selectors: tuple[str, ...] = ()
    container_selectors: tuple[str, ...] = ()
    content_type_selectors: tuple[str, ...] = ()
    date_selectors: tuple[str, ...] = ()
    allowed_patterns: tuple[str, ...] = ()
    excluded_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntryTestResult:
    entry_id: int
    status: str
    final_url: str = ""
    http_status: int | None = None
    discovered_count: int = 0
    accepted_count: int = 0
    effective_adapter_key: str = "generic"
    collection_engine: str = "http"
    next_page_url: str = ""
    sample_urls: tuple[str, ...] = ()
    rejected_links: tuple[tuple[str, str], ...] = ()
    message: str = ""


def _json_tuple(value: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in parsed if str(item).strip())


def snapshot_entry(entry: SourceEntry) -> EntryTestSnapshot:
    return EntryTestSnapshot(
        id=entry.id,
        url=entry.url,
        adapter_key=entry.adapter_key,
        listing_selectors=_json_tuple(entry.listing_selectors_json),
        container_selectors=_json_tuple(entry.container_selectors_json),
        content_type_selectors=_json_tuple(entry.content_type_selectors_json),
        date_selectors=_json_tuple(entry.date_selectors_json),
        allowed_patterns=_json_tuple(entry.allowed_path_patterns_json),
        excluded_patterns=_json_tuple(entry.excluded_path_patterns_json),
    )


def _test_one(
    snapshot: EntryTestSnapshot,
    config: AppConfig,
    limiter: DomainRateLimiter,
) -> EntryTestResult:
    crawler = WebCrawler(config, limiter)
    final_url = snapshot.url
    http_status: int | None = None
    try:
        resource = crawler.fetch(snapshot.url)
        final_url = resource.url
        http_status = resource.status_code
        tested_body = resource.body
        rendered_used = resource.rendered
        effective_adapter_key, adapter = resolve_effective_adapter(
            snapshot.adapter_key, snapshot.url
        )
        selectors = merge_selectors(
            snapshot.listing_selectors, adapter.listing_link_selectors if adapter else ()
        )
        containers = merge_selectors(
            snapshot.container_selectors, adapter.listing_container_selectors if adapter else ()
        )
        type_selectors = merge_selectors(
            snapshot.content_type_selectors,
            adapter.listing_content_type_selectors if adapter else (),
        )
        resource, cards = crawler.listing_cards(
            resource, selectors, snapshot.date_selectors, containers, type_selectors,
            adapter.listing_author_selectors if adapter else (),
        )
        tested_body = resource.body
        rendered_used = resource.rendered
        accepted = 0
        rejected_links: list[tuple[str, str]] = []
        for card in cards:
            path = urlsplit(card.fetch_url).path.lower()
            reason = ""
            if snapshot.excluded_patterns and any(
                re.search(pattern, path) for pattern in snapshot.excluded_patterns
            ):
                reason = "命中排除路径规则"
            elif snapshot.allowed_patterns and not any(
                re.search(pattern, path) for pattern in snapshot.allowed_patterns
            ):
                reason = "未命中允许路径规则"
            elif not snapshot.allowed_patterns and adapter and not adapter.accepts(card.fetch_url):
                reason = "不符合适配器详情路径"
            if reason:
                rejected_links.append((card.fetch_url, reason))
                continue
            accepted += 1
        engine_label = "内置 Chromium" if rendered_used else "HTTP"
        next_url = next_page_url(
            tested_body,
            resource.url,
            snapshot.url,
            strict_listing_path=bool(adapter and adapter.strict_listing_path),
        ) or ""
        audit_parts = [
            f"配置适配器 {snapshot.adapter_key}",
            f"实际适配器 {effective_adapter_key}",
            f"引擎 {engine_label}",
            f"当前页 {resource.url}",
            f"下一页 {next_url or '无'}",
            f"识别 {len(cards)} 条",
            f"接收 {accepted} 条",
        ]
        if rejected_links:
            reason_counts: dict[str, int] = {}
            for _, reason in rejected_links:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            audit_parts.append(
                "拒绝 " + "、".join(f"{reason} {count} 条" for reason, count in reason_counts.items())
            )
            audit_parts.append(
                "拒绝样例 "
                + " | ".join(
                    f"{url}（{reason}）" for url, reason in rejected_links[:3]
                )
            )
        if cards:
            audit_parts.append("样例 " + " | ".join(card.fetch_url for card in cards[:3]))
        if accepted:
            status = "success"
            message = "连接成功；" + "；".join(audit_parts)
        elif cards:
            status = "warning"
            message = "连接成功，但全部被发现规则排除；" + "；".join(audit_parts)
        else:
            status = "warning"
            message = "连接成功，但首个栏目页未识别到文章卡片；" + "；".join(audit_parts)
        return EntryTestResult(
            entry_id=snapshot.id,
            status=status,
            final_url=resource.url,
            http_status=resource.status_code,
            discovered_count=len(cards),
            accepted_count=accepted,
            effective_adapter_key=effective_adapter_key,
            collection_engine="chromium" if rendered_used else "http",
            next_page_url=next_url,
            sample_urls=tuple(card.fetch_url for card in cards[:3]),
            rejected_links=tuple(rejected_links[:20]),
            message=message,
        )
    except Exception as exc:
        attempts = exc.attempts if isinstance(exc, FetchError) else []
        final_attempt = attempts[-1] if attempts else None
        attempted_http_status = next(
            (attempt.status_code for attempt in reversed(attempts) if attempt.status_code is not None),
            None,
        )
        return EntryTestResult(
            entry_id=snapshot.id,
            status="failed",
            final_url=final_attempt.url if final_attempt else final_url,
            http_status=(
                attempted_http_status
                if attempted_http_status is not None
                else http_status
            ),
            message=f"连接失败：{exc}",
        )
    finally:
        crawler.close()


def test_entries(
    snapshots: list[EntryTestSnapshot], config: AppConfig
) -> list[EntryTestResult]:
    if not snapshots:
        return []
    limiter = DomainRateLimiter(config.per_domain_delay_seconds)
    workers = max(1, min(config.crawl_concurrency, len(snapshots)))
    results: list[EntryTestResult] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_test_one, snapshot, config, limiter): snapshot.id
            for snapshot in snapshots
        }
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda row: row.entry_id)
