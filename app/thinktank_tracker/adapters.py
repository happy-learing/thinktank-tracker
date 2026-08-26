from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SiteAdapter:
    key: str
    content_path_patterns: tuple[str, ...]
    listing_link_selectors: tuple[str, ...] = ()
    probe_listing_dates: bool = False
    wordpress_post_type: str = ""

    def accepts(self, url: str) -> bool:
        path = urlsplit(url).path.lower().rstrip("/")
        return any(re.search(pattern, path) for pattern in self.content_path_patterns)


ADAPTERS: dict[str, SiteAdapter] = {
    "euiss": SiteAdapter("euiss", (r"/publications/(?:briefs|chaillot-papers|reports|books)/.+", r"/node/\d+")),
    "ceps": SiteAdapter(
        "ceps",
        (r"/ceps-publications/.+", r"/research-report/.+", r"/policy-brief/.+"),
        ("#ut-archive .ut-post-card h3 a[href]",),
        probe_listing_dates=True,
    ),
    "martens": SiteAdapter(
        "martens",
        (r"/publication/.+",),
        wordpress_post_type="publication",
    ),
    "egmont": SiteAdapter(
        "egmont",
        (r"^/[^/]+$",),
        ("article.post-publication a.post-publication__title[href]",),
    ),
    "epc": SiteAdapter(
        "epc",
        (r"/(?:en/)?publication/.+", r"/(?:en/)?publications/.+"),
        ("main a[href*='/publication/']", "article a[href*='/publication/']"),
    ),
    "elcano": SiteAdapter(
        "elcano",
        (
            r"/(?:en/)?analyses/.+",
            r"/(?:en/)?commentaries/.+",
            r"/(?:en/)?work-document/.+",
            r"/(?:en/)?policy-paper/.+",
            r"/(?:en/)?reports/.+",
        ),
        ("article.elcano-card h2.entry-title a[href]",),
    ),
}


def get_adapter(key: str) -> SiteAdapter | None:
    return ADAPTERS.get(key)
