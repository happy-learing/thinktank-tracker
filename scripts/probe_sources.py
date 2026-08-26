from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import select

from thinktank_tracker.adapters import get_adapter
from thinktank_tracker.config import load_config
from thinktank_tracker.crawler import WebCrawler
from thinktank_tracker.database import SessionLocal
from thinktank_tracker.extractors import discover_content_links, discover_listing_dates, next_page_url
from thinktank_tracker.models import Source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-scan", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 2))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 26))
    parser.add_argument("--adapter", default="")
    args = parser.parse_args()
    failures = 0
    with SessionLocal() as session:
        sources = session.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.id)).all()
    crawler = WebCrawler(load_config())
    try:
        for source in sources:
            if args.adapter and source.adapter_key != args.adapter:
                continue
            try:
                if args.full_scan:
                    scan = crawler.scan_listing(
                        source.publication_url or source.homepage_url,
                        args.start,
                        source.ordered_by_date,
                        source.adapter_key,
                        args.end,
                    )
                    print(
                        f"{source.name}\t{scan.coverage_status.upper()}\tpages={scan.pages_scanned}"
                        f"\tlinks={len(scan.links)}\tearliest={scan.earliest_date or '-'}"
                        f"\tlatest={scan.latest_date or '-'}\tstop={scan.stop_reason}"
                    )
                    for item in scan.links[:3]:
                        print(f"  - {item.listing_title[:80]} | {item.url}")
                    if scan.coverage_status != "complete":
                        for error in scan.errors:
                            print(f"  ! {error}")
                        failures += 1
                    continue
                resource = crawler.fetch(source.publication_url or source.homepage_url)
                adapter = get_adapter(source.adapter_key)
                selectors = adapter.listing_link_selectors if adapter else ()
                links = discover_content_links(resource.body, resource.url, selectors)
                accepted = [item for item in links if adapter is None or adapter.accepts(item[0])]
                dates = discover_listing_dates(resource.body)
                next_url = next_page_url(resource.body, resource.url)
                print(
                    f"{source.name}\tHTTP_OK\tfinal={resource.url}\tlinks={len(links)}\taccepted={len(accepted)}"
                    f"\tdates={len(dates)}\tnext={'yes' if next_url else 'no'}\trendered={resource.rendered}"
                )
                for url, title in accepted[:3]:
                    print(f"  - {title[:80]} | {url}")
                if not accepted:
                    for url, title in links[:10]:
                        print(f"  ? {title[:80]} | {url}")
                    failures += 1
            except Exception as exc:
                failures += 1
                print(f"{source.name}\tFAILED\t{exc}")
    finally:
        crawler.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
