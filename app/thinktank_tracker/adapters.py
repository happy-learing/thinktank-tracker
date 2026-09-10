from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .source_catalog import known_source_for


@dataclass(frozen=True)
class SiteAdapter:
    key: str
    content_path_patterns: tuple[str, ...]
    listing_link_selectors: tuple[str, ...] = ()
    probe_listing_dates: bool = False
    wordpress_post_type: str = ""
    listing_container_selectors: tuple[str, ...] = ()
    listing_content_type_selectors: tuple[str, ...] = ()
    listing_author_selectors: tuple[str, ...] = ()
    detail_content_type_selectors: tuple[str, ...] = ()
    detail_author_selectors: tuple[str, ...] = ()
    strict_listing_path: bool = False

    def accepts(self, url: str) -> bool:
        path = urlsplit(url).path.lower().rstrip("/")
        return any(re.search(pattern, path) for pattern in self.content_path_patterns)


ADAPTERS: dict[str, SiteAdapter] = {
    "euiss": SiteAdapter(
        "euiss",
        (
            r"/publications/(?:analysis|commentary|briefs|chaillot-papers|reports|books)/.+",
            r"/node/\d+",
        ),
        (
            "main article .teaser-title a[href*='/publications/']",
            ".view-publications .views-row a[href*='/publications/']",
            ".publication-item a[href*='/publications/']",
        ),
        listing_container_selectors=("article.node--type-publication-page", "main article"),
        listing_content_type_selectors=(".field--name-field-publication-type", ".type"),
        listing_author_selectors=(".field--name-field-author",),
        detail_content_type_selectors=(".field--name-field-publication-type",),
        detail_author_selectors=(".field--name-field-author",),
    ),
    "ceps": SiteAdapter(
        "ceps",
        (r"/ceps-publications/.+", r"/research-report/.+", r"/policy-brief/.+"),
        ("#ut-archive .ut-post-card h3 a[href]",),
        probe_listing_dates=True,
        listing_container_selectors=("#ut-archive .ut-post-card",),
        listing_content_type_selectors=(".ut-label",),
        listing_author_selectors=(".ut-post-card-author", "[class*='author']"),
        detail_content_type_selectors=(".ut-label", ".publication-type", "[class*='publication-type']"),
        detail_author_selectors=("[rel='author']", ".author", "[class*='author']"),
    ),
    "martens": SiteAdapter(
        "martens",
        (r"/publication/.+",),
        (
            ".list-publications > li > a[href*='/publication/']",
            "article.publication-item a[href*='/publication/']",
        ),
        wordpress_post_type="publication",
        listing_container_selectors=(".list-publications > li", "article", ".publication-item"),
        listing_content_type_selectors=(
            ".publications-detail .category",
            ".publication-category",
            ".category",
            "[class*='publication-type']",
        ),
        listing_author_selectors=(".author", "[class*='author']"),
        detail_content_type_selectors=(".publication-category", ".post-category", ".entry-meta .cat-links", "[class*='publication-type']"),
        detail_author_selectors=("[rel='author']", ".author", "[class*='author']"),
    ),
    "egmont": SiteAdapter(
        "egmont",
        (r"^/[^/]+$",),
        ("article.post-publication a.post-publication__title[href]",),
        listing_container_selectors=("article.post-publication",),
        listing_content_type_selectors=(".post-publication__thumbnail__tag",),
        listing_author_selectors=(".post-publication__author",),
        detail_content_type_selectors=(".post-publication__header a[href*='/publication_parent/']", ".post-publication__thumbnail__tag", ".publication-category"),
        detail_author_selectors=(".post-publication__author", "[rel='author']", ".author"),
    ),
    "epc": SiteAdapter(
        "epc",
        (r"/(?:en/)?publication/.+", r"/(?:en/)?publications/.+"),
        (
            ".featured-publication-title a[href*='/publication/']",
            ".publication-item-title a[href*='/publication/']",
            "article h2 a[href*='/publication/']",
            "article h3 a[href*='/publication/']",
        ),
        listing_container_selectors=(".publication-item", ".featured-publication", "article"),
        listing_content_type_selectors=(
            ".topic-tag",
            ".publication-type",
            "[class*='publication-type']",
            ".type",
        ),
        listing_author_selectors=(".publication-author", "[class*='author']"),
        detail_content_type_selectors=(".publication-type", "[class*='publication-type']", ".type"),
        detail_author_selectors=(".page-authors-author", ".publication-author", "[rel='author']"),
    ),
    "elcano": SiteAdapter(
        "elcano",
        (
            r"/(?:en/)?analyses/.+",
            r"/(?:en/)?commentaries/.+",
            r"/(?:en/)?work-document/.+",
            r"/(?:en/)?policy-paper/.+",
            r"/(?:en/)?reports/.+",
            r"/analisis/.+",
            r"/comentarios/.+",
            r"/documento-de-trabajo/.+",
            r"/informes/.+",
        ),
        ("article.elcano-card h2.entry-title a[href]",),
        listing_container_selectors=("article.elcano-card",),
        listing_author_selectors=(".author", "[class*='author']"),
        detail_author_selectors=("[rel='author']", ".author", "[class*='author']"),
    ),
    "ecfr": SiteAdapter(
        "ecfr",
        (r"/article/.+", r"/publication/.+", r"/special/.+"),
        (".entry-list article h2.post-title a.card-main-link[href]",),
        listing_container_selectors=(".entry-list article",),
        listing_content_type_selectors=(".post-meta > li:nth-of-type(2)",),
        listing_author_selectors=(".post-meta [rel='author']", ".post-meta li:first-child"),
        detail_content_type_selectors=(
            ".post-meta > li:nth-of-type(2)",
            ".publication-type",
            "[class*='content-type']",
        ),
        detail_author_selectors=(".post-meta [rel='author']", ".post-meta li:first-child", "[rel='author']"),
        strict_listing_path=True,
    ),
}


def get_adapter(key: str) -> SiteAdapter | None:
    return ADAPTERS.get(key)


def resolve_effective_adapter(key: str, url: str) -> tuple[str, SiteAdapter | None]:
    """Resolve auto mode once and return the key used by the crawl itself."""
    requested = (key or "auto").strip().lower()
    if requested == "auto":
        known = known_source_for(url)
        requested = known.adapter_key if known else "generic"
    adapter = get_adapter(requested)
    return (requested if adapter else "generic"), adapter


def merge_selectors(
    custom: tuple[str, ...], defaults: tuple[str, ...]
) -> tuple[str, ...]:
    """Custom selectors add hints without disabling maintained adapter defaults."""
    return tuple(dict.fromkeys((*custom, *defaults)))
