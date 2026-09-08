from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from thinktank_tracker.adapters import merge_selectors, resolve_effective_adapter
from thinktank_tracker.config import load_config
from thinktank_tracker.crawler import WebCrawler
from thinktank_tracker.database import SessionLocal, initialize_database
from thinktank_tracker.extractors import discover_content_cards, discover_listing_dates, next_page_url
from thinktank_tracker.models import Source


def main() -> int:
    initialize_database()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-scan", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 2))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 26))
    parser.add_argument("--adapter", default="")
    args = parser.parse_args()
    failures = 0
    with SessionLocal() as session:
        sources = session.scalars(
            select(Source)
            .where(Source.enabled.is_(True))
            .options(selectinload(Source.entries))
            .order_by(Source.id)
        ).all()
    crawler = WebCrawler(load_config())
    try:
        for source in sources:
            entries = [entry for entry in source.entries if entry.enabled and entry.deleted_at is None]
            for entry_index, entry in enumerate(entries, 1):
                label = source.name if len(entries) == 1 else f"{source.name} #{entry_index}"
                try:
                    effective_adapter_key, adapter = resolve_effective_adapter(
                        entry.adapter_key, entry.url
                    )
                    if args.adapter and effective_adapter_key != args.adapter:
                        continue
                    if args.full_scan or (adapter and adapter.wordpress_post_type):
                        scan = crawler.scan_listing(
                            entry.url,
                            args.start,
                            entry.ordered_by_date,
                            entry.adapter_key,
                            args.end,
                            listing_selectors=tuple(json.loads(entry.listing_selectors_json or "[]")),
                            container_selectors=tuple(json.loads(entry.container_selectors_json or "[]")),
                            content_type_selectors=tuple(json.loads(entry.content_type_selectors_json or "[]")),
                            date_selectors=tuple(json.loads(entry.date_selectors_json or "[]")),
                            allowed_path_patterns=tuple(json.loads(entry.allowed_path_patterns_json or "[]")),
                            excluded_path_patterns=tuple(json.loads(entry.excluded_path_patterns_json or "[]")),
                        )
                        print(
                            f"{label}\t{scan.coverage_status.upper()}\tadapter={effective_adapter_key}"
                            f"\tpages={scan.pages_scanned}"
                            f"\tlinks={len(scan.links)}\tearliest={scan.earliest_date or '-'}"
                            f"\tlatest={scan.latest_date or '-'}\tstop={scan.stop_reason}"
                        )
                        for item in scan.links[:3]:
                            print(
                                f"  - {item.listing_title[:80]} | {item.fetch_url}"
                                f" | type={item.listing_content_type or 'unmapped'}"
                                f" | labels={','.join(item.listing_content_type_labels) or '-'}"
                            )
                        if scan.coverage_status != "complete":
                            for error in scan.errors:
                                print(f"  ! {error}")
                            failures += 1
                        continue
                    resource = crawler.fetch(entry.url)
                    configured_selectors = tuple(json.loads(entry.listing_selectors_json or "[]"))
                    selectors = merge_selectors(
                        configured_selectors,
                        adapter.listing_link_selectors if adapter else (),
                    )
                    containers = merge_selectors(
                        tuple(json.loads(entry.container_selectors_json or "[]")),
                        adapter.listing_container_selectors if adapter else (),
                    )
                    type_selectors = merge_selectors(
                        tuple(json.loads(entry.content_type_selectors_json or "[]")),
                        adapter.listing_content_type_selectors if adapter else (),
                    )
                    date_selectors = tuple(json.loads(entry.date_selectors_json or "[]"))
                    links = discover_content_cards(
                        resource.body,
                        resource.url,
                        selectors,
                        date_selectors,
                        crawler.config.content_type_rules,
                        containers,
                        type_selectors,
                        adapter.listing_author_selectors if adapter else (),
                    )
                    accepted = [
                        item for item in links if adapter is None or adapter.accepts(item.fetch_url)
                    ]
                    dates = discover_listing_dates(resource.body)
                    next_url = next_page_url(
                        resource.body,
                        resource.url,
                        entry.url,
                        strict_listing_path=bool(adapter and adapter.strict_listing_path),
                    )
                    print(
                        f"{label}\tHTTP_OK\tadapter={effective_adapter_key}"
                        f"\tfinal={resource.url}\tlinks={len(links)}\taccepted={len(accepted)}"
                        f"\tdates={len(dates)}\tnext={next_url or '-'}\trendered={resource.rendered}"
                    )
                    for item in accepted[:3]:
                        print(
                            f"  - {item.title[:80]} | {item.fetch_url}"
                            f" | type={item.content_type or 'unmapped'}"
                            f" | labels={','.join(item.content_type_labels) or '-'}"
                        )
                    if not accepted:
                        for item in links[:10]:
                            print(f"  ? {item.title[:80]} | {item.fetch_url}")
                        failures += 1
                except Exception as exc:
                    failures += 1
                    print(f"{label}\tFAILED\t{exc}")
    finally:
        crawler.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
