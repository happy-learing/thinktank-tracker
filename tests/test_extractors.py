from __future__ import annotations

from datetime import date

from thinktank_tracker.extractors import (
    discover_content_links,
    extract_html_document,
    next_page_url,
    resolve_date,
    DateFact,
)


def test_extracts_confirmed_date_type_and_length() -> None:
    body = " ".join(["strategy"] * 1600)
    raw = f"""
    <html><head><meta property="article:published_time" content="2026-08-02T10:00:00Z">
    <meta property="article:section" content="Policy Brief"><meta property="og:title" content="Strategic test"></head>
    <body><article><h1>Strategic test</h1><p>{body}</p></article></body></html>
    """.encode()
    result = extract_html_document(raw, "https://example.org/publications/strategic-test")
    assert result.published_date == date(2026, 8, 2)
    assert result.date_status == "confirmed"
    assert result.content_type == "policy brief"
    assert result.english_word_count >= 1500
    assert result.extraction_status == "complete"


def test_date_conflicts_are_not_resolved_silently() -> None:
    value, status, evidence = resolve_date(
        [DateFact(date(2026, 8, 2), "meta", "2026-08-02"), DateFact(date(2026, 8, 3), "json", "2026-08-03")]
    )
    assert value is None
    assert status == "conflict"
    assert "2026-08-02" in evidence and "2026-08-03" in evidence


def test_extracts_visible_date_when_datetime_attribute_is_absent() -> None:
    raw = b'<article><h1>Paper</h1><div class="publication-date">August 26, 2026</div><p>' + b"word " * 500 + b"</p></article>"
    result = extract_html_document(raw, "https://example.org/publications/paper")
    assert result.published_date == date(2026, 8, 26)
    assert result.date_status == "confirmed"
    assert "visible" in result.date_evidence


def test_discovers_same_domain_research_links_and_deduplicates() -> None:
    raw = b"""
    <main><a href="/publications/paper-one?utm_source=x">Paper one</a>
    <a href="https://example.org/publications/paper-one">Duplicate</a>
    <a href="https://other.example/report">External</a><a href="/about">About</a></main>
    """
    links = discover_content_links(raw, "https://example.org/publications")
    assert links == [("https://example.org/publications/paper-one", "Paper one")]


def test_preferred_selector_discovers_root_level_detail_and_normalizes_www() -> None:
    raw = b"""
    <article class="post-publication">
      <a class="post-publication__title" href="https://example.org/strategic-paper/"><h3>Strategic paper</h3></a>
    </article>
    """
    links = discover_content_links(
        raw,
        "https://www.example.org/publications/",
        ("article.post-publication a.post-publication__title[href]",),
    )
    assert links == [("https://example.org/strategic-paper", "Strategic paper")]


def test_next_page_preserves_required_trailing_slash() -> None:
    raw = b'<a class="next" href="/publications/page/2/">Next</a>'
    assert next_page_url(raw, "https://example.org/publications/") == "https://example.org/publications/page/2/"


def test_next_page_converts_scripted_filter_pagination() -> None:
    raw = b"""
    <ul class="pagination">
      <li class="active"><span>1</span></li>
      <li><a href="javascript:submit_filters(2)">2</a></li>
      <li><a href="javascript:submit_filters(5)">5</a></li>
    </ul>
    """
    assert next_page_url(raw, "https://example.org/publications/") == (
        "https://example.org/publications/?p=2&q=1"
    )
