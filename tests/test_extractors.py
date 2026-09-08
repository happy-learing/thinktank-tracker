from __future__ import annotations

import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest

import thinktank_tracker.extractors as extractors

from thinktank_tracker.extractors import (
    detect_language_and_length,
    detect_content_type,
    discover_content_cards,
    discover_pdf_links,
    discover_listing_dates,
    discover_content_links,
    extract_html_document,
    extract_pdf_document,
    next_page_url,
    resolve_date,
    DateFact,
)


class _FakeConvertError(Exception):
    pass


class _FakeNeedsOcrError(_FakeConvertError):
    def __init__(self, pages: list[int], page_count: int) -> None:
        super().__init__(f"pages {pages} need OCR")
        self.pages = pages
        self.page_count = page_count


class _FakeMalformedError(_FakeConvertError):
    pass


class _FakeMissingPartError(_FakeConvertError):
    pass


class _FakeUnsupportedError(_FakeConvertError):
    pass


class _FakeEncryptedError(_FakeConvertError):
    pass


class _FakeResourceLimitError(_FakeConvertError):
    pass


def _fake_anydoc(action, calls: list[tuple[str, str]] | None = None):  # type: ignore[no-untyped-def]
    def convert(_raw: bytes, format_name: str, *, ocr: str) -> str:
        if calls is not None:
            calls.append((format_name, ocr))
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action()
        return str(action)

    return SimpleNamespace(
        to_markdown_bytes=convert,
        ConvertError=_FakeConvertError,
        NeedsOcrError=_FakeNeedsOcrError,
        MalformedError=_FakeMalformedError,
        MissingPartError=_FakeMissingPartError,
        UnsupportedError=_FakeUnsupportedError,
        EncryptedError=_FakeEncryptedError,
        ResourceLimitError=_FakeResourceLimitError,
    )


def _install_fake_reader(
    monkeypatch,
    texts: list[str] | None = None,
    *,
    metadata: dict[str, str] | None = None,
    page_errors: dict[int, Exception] | None = None,
) -> list[int]:  # type: ignore[no-untyped-def]
    extraction_calls: list[int] = []
    page_values = texts if texts is not None else ["legacy text " * 100]
    errors = page_errors or {}

    class FakePage:
        def __init__(self, index: int, value: str) -> None:
            self.index = index
            self.value = value

        def extract_text(self) -> str:
            extraction_calls.append(self.index)
            if self.index in errors:
                raise errors[self.index]
            return self.value

    class FakeReader:
        def __init__(self, _stream) -> None:  # type: ignore[no-untyped-def]
            self.metadata = metadata or {}
            self.pages = [FakePage(index, value) for index, value in enumerate(page_values, 1)]

    monkeypatch.setattr(extractors, "PdfReader", FakeReader)
    return extraction_calls


def test_extracts_confirmed_date_type_and_length() -> None:
    body = " ".join(["strategy"] * 1600)
    raw = f"""
    <html><head><meta property="article:published_time" content="2026-08-02T10:00:00Z">
    <meta property="article:section" content="Policy Brief"><meta property="og:title" content="Strategic test"></head>
    <body><article><h1>Strategic test</h1><p>{body}</p></article></body></html>
    """.encode()
    result = extract_html_document(raw, "https://example.org/policy-brief/strategic-test")
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


def test_json_ld_published_date_wins_over_created_date() -> None:
    raw = b"""
    <html><head><script type="application/ld+json">
    {"@type":"Report","datePublished":"2026-08-20","dateCreated":"2026-08-18","name":"Report"}
    </script></head><body><article><h1>Report</h1><p>""" + b"word " * 500 + b"""</p></article></body></html>
    """
    result = extract_html_document(raw, "https://example.org/reports/report")
    assert result.published_date == date(2026, 8, 20)
    assert result.date_status == "confirmed"


def test_extracts_visible_date_when_datetime_attribute_is_absent() -> None:
    raw = b'<article><h1>Paper</h1><div class="publication-date">August 26, 2026</div><p>' + b"word " * 500 + b"</p></article>"
    result = extract_html_document(raw, "https://example.org/publications/paper")
    assert result.published_date == date(2026, 8, 26)
    assert result.date_status == "confirmed"
    assert "visible" in result.date_evidence


def test_euiss_detail_uses_primary_date_and_briefs_url_type() -> None:
    raw = b"""
    <html><head>
      <meta property="article:published_time" content="2026-07-23T10:16:38+02:00">
      <meta property="og:updated_time" content="2026-07-24T17:12:32+02:00">
      <meta property="og:title" content="Mining for Europe's future">
    </head><body><main>
      <div class="node node--type-publication-page node--view-mode-full">
        <span class="date field field--name-field-date"><time datetime="2026-07-23T12:00:00Z">23 July 2026</time></span>
        <div class="field field--name-field-publication-type type">Briefs</div>
        <p>""" + b"strategy " * 1600 + b"""</p>
      </div>
      <section class="related">
        <article class="node--view-mode-teaser"><span class="field--name-field-date"><time datetime="2026-05-12T12:00:00Z">12 May 2026</time></span></article>
        <article class="node--view-mode-teaser"><span class="field--name-field-date"><time datetime="2026-02-17T12:00:00Z">17 February 2026</time></span></article>
      </section>
    </main></body></html>
    """
    url = "https://www.iss.europa.eu/publications/briefs/mining-europes-future"
    result = extract_html_document(raw, url)
    assert result.published_date == date(2026, 7, 23)
    assert result.date_status == "confirmed"
    assert result.content_type == "policy brief"
    assert "URL 路径段 'briefs'" in result.content_type_evidence


def test_primary_full_view_date_wins_without_published_meta() -> None:
    raw = b"""
    <main>
      <div class="node--view-mode-full"><span class="field--name-field-date"><time datetime="2026-08-08">8 August 2026</time></span><p>""" + b"word " * 500 + b"""</p></div>
      <article class="node--view-mode-teaser"><span class="field--name-field-date"><time datetime="2026-07-01">1 July 2026</time></span></article>
    </main>
    """
    result = extract_html_document(raw, "https://example.org/publications/reports/example")
    assert result.published_date == date(2026, 8, 8)
    assert result.date_status == "confirmed"
    assert result.content_type == "report"


def test_discovers_same_domain_research_links_and_deduplicates() -> None:
    raw = b"""
    <main><article><a href="/publications/paper-one?utm_source=x">Paper one</a>
    <a href="https://example.org/publications/paper-one">Duplicate</a></article>
    <a href="https://other.example/report">External</a><a href="/about">About</a></main>
    """
    links = discover_content_links(raw, "https://example.org/publications")
    assert links == [
        (
            "https://example.org/publications/paper-one?utm_source=x",
            "https://example.org/publications/paper-one",
            "Paper one",
        )
    ]


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
    assert links == [
        (
            "https://example.org/strategic-paper/",
            "https://example.org/strategic-paper",
            "Strategic paper",
        )
    ]


def test_only_requested_briefs_and_policy_brief_url_segments_are_recognized() -> None:
    body = b"<article><h1>Title</h1><p>" + b"word " * 500 + b"</p></article>"
    assert extract_html_document(body, "https://example.org/brief/example").content_type == "unknown"
    assert extract_html_document(body, "https://example.org/briefs/example").content_type == "policy brief"
    assert extract_html_document(body, "https://example.org/policy-brief/example").content_type == "policy brief"
    assert extract_html_document(body, "https://example.org/policy_brief/example").content_type == "policy brief"
    assert extract_html_document(body, "https://example.org/policy%20brief/example").content_type == "policy brief"
    assert extract_html_document(body, "https://example.org/policy-briefs/example").content_type == "unknown"


def test_elcano_preferred_selector_only_discovers_listing_cards() -> None:
    raw = b"""
    <main>
      <article class="elcano-card"><h2 class="entry-title"><a href="/en/analyses/direct-result/">Direct result</a></h2></article>
      <aside class="related"><a href="/en/analyses/unrelated-result/">Related result</a></aside>
    </main>
    """
    links = discover_content_links(
        raw,
        "https://www.realinstitutoelcano.org/en/publications/",
        ("article.elcano-card h2.entry-title a[href]",),
    )
    assert links == [
        (
            "https://www.realinstitutoelcano.org/en/analyses/direct-result/",
            "https://www.realinstitutoelcano.org/en/analyses/direct-result",
            "Direct result",
        )
    ]


def test_epc_detail_extracts_header_date_but_ignores_page_banner_type() -> None:
    raw = b"""
    <html><head><title>Arctic policy</title></head><body><main>
      <section class="page-banner">
        <h1>Arctic policy</h1>
        <div class="publication-item-date pb-3">Aug 03, 2026</div>
        <a href="/publications/?types=1" class="page-banner-type">POLICY BRIEF</a>
      </section>
      <div class="article-content"><p>""" + b"strategy " * 500 + b"""</p></div>
      <section class="related"><div class="publication-item-date">Aug 20, 2026</div></section>
    </main></body></html>
    """
    result = extract_html_document(
        raw,
        "https://www.epc.eu/publication/from-sea-to-space/",
    )
    assert result.published_date == date(2026, 8, 3)
    assert result.date_status == "confirmed"
    assert result.content_type == "unknown"
    assert "URL 路径未命中" in result.content_type_evidence


def test_spanish_abbreviated_month_uses_complete_day_month_year() -> None:
    raw = """
    <main><article><header class="entry-header"><span class="posted-on">Publicado el 04 Ago 2026 //</span></header>
    <h1>Aviso</h1><p>contenido de investigacion</p></article></main>
    """.encode()
    result = extract_html_document(raw, "https://example.org/analisis/aviso/")
    assert result.published_date == date(2026, 8, 4)
    assert result.date_status == "confirmed"
    assert "posted-on" in result.date_evidence


def test_elcano_primary_posted_date_ignores_related_and_navigation_dates() -> None:
    raw = """
    <main><article class="analysis"><header class="entry-header"><h1>Aranceles</h1>
      <span class="posted-on">Publicado el 06 Ago 2026 //</span></header><p>texto</p></article>
      <div class="post-related"><span class="posted-on">Publicado el 11 Ago 2026 //</span></div>
      <div class="nav nav-prev"><div class="post-date"><span class="posted-on">Publicado el 05 Ago 2026 //</span></div></div>
    </main>
    """.encode()
    result = extract_html_document(raw, "https://example.org/analisis/aranceles/")
    assert result.published_date == date(2026, 8, 6)
    assert result.date_status == "confirmed"


def test_featured_listing_card_is_marked_without_being_removed() -> None:
    raw = b"""
    <main><section class="featured-publication"><h2><a href="/publication/featured/">Featured item</a></h2>
      <div class="publication-item-date">Aug 12, 2026</div></section>
      <article><h2><a href="/publication/latest/">Latest item</a></h2>
      <div class="publication-item-date">Aug 26, 2026</div></article></main>
    """
    cards = discover_content_cards(
        raw,
        "https://example.org/publications/",
        (".featured-publication h2 a", "article h2 a"),
    )
    assert len(cards) == 2
    assert cards[0].is_featured is True
    assert cards[1].is_featured is False


def test_card_date_uses_outer_item_not_title_wrapper_substring() -> None:
    raw = b"""
    <main><div class="publication-item"><div class="publication-item-content">
      <div class="publication-item-title"><a href="/publication/latest/">Latest item</a></div>
      <div class="publication-item-date">Aug 26, 2026</div>
    </div></div></main>
    """
    cards = discover_content_cards(
        raw,
        "https://example.org/publications/",
        (".publication-item-title a",),
    )
    assert cards[0].published_date == date(2026, 8, 26)


def test_listing_dates_ignore_the_listing_pages_own_metadata() -> None:
    raw = b"""
    <html><head><meta property="article:published_time" content="2024-04-29"></head>
    <body><main><article><div class="publication-item-date">Aug 24, 2026</div></article></main></body></html>
    """
    assert discover_listing_dates(raw) == [date(2026, 8, 24)]


def test_next_page_preserves_required_trailing_slash() -> None:
    raw = b'<a class="next" href="/publications/page/2/">Next</a>'
    assert next_page_url(raw, "https://example.org/publications/") == "https://example.org/publications/page/2/"


def test_ecfr_visible_section_pagination_wins_over_wrong_head_next() -> None:
    raw = b"""
    <head><link rel="next" href="https://ecfr.eu/page/2/"></head>
    <main class="entry-list"><a class="next" href="/analysis/page/2/">Next</a></main>
    """
    assert next_page_url(
        raw,
        "https://ecfr.eu/analysis/",
        "https://ecfr.eu/analysis/",
        strict_listing_path=True,
    ) == "https://ecfr.eu/analysis/page/2/"


def test_strict_listing_pagination_rejects_head_link_outside_section() -> None:
    raw = b'<head><link rel="next" href="https://ecfr.eu/page/2/"></head>'
    assert next_page_url(
        raw,
        "https://ecfr.eu/analysis/",
        "https://ecfr.eu/analysis/",
        strict_listing_path=True,
    ) is None


def test_generic_adapter_discovers_entry_list_article() -> None:
    raw = b"""
    <main class="entry-list"><article><h2 class="post-title">
      <a href="/article/a-general-discovery/">A general discovery article</a>
    </h2><time datetime="2026-08-27">27 August 2026</time></article></main>
    """
    cards = discover_content_cards(raw, "https://example.org/analysis/")
    assert len(cards) == 1
    assert cards[0].fetch_url == "https://example.org/article/a-general-discovery/"


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


def test_all_requested_url_types_use_exact_decoded_path_segments() -> None:
    expected = {
        "reports": "report",
        "report": "report",
        "analisis": "analysis",
        "policy-brief": "policy brief",
        "policy_brief": "policy brief",
        "policy%20brief": "policy brief",
        "briefs": "policy brief",
        "articles": "article",
        "paper": "research paper",
        "research": "research",
    }
    for segment, content_type in expected.items():
        assert detect_content_type(None, f"https://example.org/{segment}/item", "", "")[0] == content_type
    for segment in ("brief", "analyses", "policy-briefs", "papers", "reporting", "research-paper"):
        assert detect_content_type(None, f"https://example.org/{segment}/item", "", "")[0] == "unknown"


def test_url_type_wins_and_official_label_is_ignored() -> None:
    raw = b'<meta property="article:section" content="Report"><article><h1>Title</h1></article>'
    result = extract_html_document(raw, "https://example.org/reports/item")
    assert result.content_type == "report"
    assert result.content_type_evidence == "URL 路径段 'reports' 命中规则 -> report"
    assert "冲突" not in result.content_type_evidence


def test_listing_cards_capture_own_dates_and_ignore_pagination_links() -> None:
    raw = b"""
    <main><article><a href="/reports/one">Report one</a><time datetime="2026-08-08">8 August 2026</time></article>
    <nav class="pagination"><a rel="next" href="/publications/?page=2">Next</a></nav></main>
    """
    cards = discover_content_cards(raw, "https://example.org/publications/")
    assert len(cards) == 1
    assert cards[0].published_date == date(2026, 8, 8)
    assert cards[0].fetch_url == "https://example.org/reports/one"


def test_stale_preferred_selector_does_not_fall_back_to_body_discovery() -> None:
    raw = b'<main><article><a href="/reports/one">Report one</a></article></main>'
    cards = discover_content_cards(raw, "https://example.org/publications/", (".old-layout a",))
    assert cards == []


def test_ceps_style_pdf_scoring_keeps_primary_and_supplement_but_not_footer() -> None:
    raw = b"""
    <div id="ceps-page">
      <a href="https://cdn.example.org/main.pdf">Download Publication</a>
      <a data-href="https://cdn.example.org/appendix.pdf">Supplementary appendix</a>
      <footer><a href="https://cdn.example.org/code-of-conduct.pdf">Code of conduct</a></footer>
    </div>
    """
    attachments = discover_pdf_links(raw, "https://www.ceps.eu/ceps-publications/example/", "Example")
    assert [(item.role, item.url) for item in attachments] == [
        ("primary", "https://cdn.example.org/main.pdf"),
        ("supplementary", "https://cdn.example.org/appendix.pdf"),
    ]


def test_explicit_policy_brief_here_link_is_a_primary_pdf() -> None:
    raw = b"""
    <main><article><h1>Arctic policy</h1>
      <a href="https://cdn.example.org/arctic-pb-v2.pdf">Policy Brief here</a>
      <a href="/publications/?types=1">Policy Brief</a>
    </article></main>
    """
    attachments = discover_pdf_links(raw, "https://example.org/publication/arctic/", "Arctic policy")
    assert attachments[0].role == "primary"
    assert attachments[0].score >= 100
    assert len(attachments) == 1


def test_ceps_primary_article_class_does_not_determine_publication_type() -> None:
    raw = b"""
    <article class="publications type-publications publication_types-policy-contribution">
      <h1>Nuclear energy</h1><p>Research body</p>
    </article>
    <section class="related"><article class="publication_types-commissioned-reports"></article></section>
    """
    result = extract_html_document(
        raw,
        "https://www.ceps.eu/ceps-publications/nuclear-energy/",
    )
    assert result.content_type == "unknown"
    assert "URL 路径未命中" in result.content_type_evidence


def test_anydoc_success_uses_markdown_headings_and_pypdf_metadata(monkeypatch) -> None:
    extraction_calls = _install_fake_reader(
        monkeypatch,
        ["pypdf body " * 100],
        metadata={"/Title": "Metadata title", "/CreationDate": "D:20260827"},
    )
    anydoc_calls: list[tuple[str, str]] = []
    markdown = "# Main report\n\n" + "strategy " * 600 + "\n\n## Data table\n\n| A | B |\n|---|---|\n| 1 | 2 |"
    monkeypatch.setattr(extractors, "_load_anydoc", lambda: _fake_anydoc(markdown, anydoc_calls))

    result = extract_pdf_document(b"%PDF-fake", "https://example.org/reports/item.pdf")

    assert result.extraction_status == "complete"
    assert result.title == "Metadata title"
    assert result.published_date == date(2026, 8, 27)
    assert result.text.startswith("# Main report")
    assert result.headings == ["Main report", "Data table"]
    assert result.extraction_audit["final_engine"] == "anydoc"
    assert result.extraction_audit["final_source"] == "anydoc_markdown"
    assert anydoc_calls == [("pdf", "reject")]
    assert extraction_calls == []


def test_anydoc_short_text_falls_back_to_pypdf(monkeypatch) -> None:
    _install_fake_reader(monkeypatch, ["strategy " * 600])
    monkeypatch.setattr(extractors, "_load_anydoc", lambda: _fake_anydoc("# Short\nToo short"))

    result = extract_pdf_document(b"%PDF-fake", "https://example.org/reports/item.pdf")

    assert result.extraction_status == "complete"
    assert result.extraction_audit["final_engine"] == "pypdf"
    assert result.extraction_audit["fallback_reason"] == "anydoc_text_too_short"
    assert [item["engine"] for item in result.extraction_audit["attempts"]] == [
        "anydoc",
        "pypdf",
    ]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (_FakeMalformedError("broken xref"), "anydoc_malformed"),
        (_FakeMissingPartError("missing stream"), "anydoc_missing_part"),
        (_FakeUnsupportedError("unsupported object"), "anydoc_unsupported"),
    ],
)
def test_anydoc_compatibility_errors_fall_back_to_pypdf(monkeypatch, error, reason) -> None:  # type: ignore[no-untyped-def]
    _install_fake_reader(monkeypatch, ["strategy " * 600])
    monkeypatch.setattr(extractors, "_load_anydoc", lambda: _fake_anydoc(error))

    result = extract_pdf_document(b"%PDF-fake", "https://example.org/reports/item.pdf")

    assert result.extraction_status == "complete"
    assert result.extraction_audit["final_engine"] == "pypdf"
    assert result.extraction_audit["fallback_reason"] == reason


def test_needs_ocr_keeps_extractable_pages_and_requires_review(monkeypatch) -> None:
    _install_fake_reader(monkeypatch, ["strategy " * 350, "policy " * 350])
    monkeypatch.setattr(
        extractors,
        "_load_anydoc",
        lambda: _fake_anydoc(_FakeNeedsOcrError([2, 4], 5)),
    )

    result = extract_pdf_document(b"%PDF-fake", "https://example.org/reports/item.pdf")

    assert result.extraction_status == "partial_pdf"
    assert result.extraction_audit["final_engine"] == "pypdf"
    assert result.extraction_audit["fallback_reason"] == "anydoc_needs_ocr"
    assert result.extraction_audit["ocr_pages"] == [2, 4]
    assert result.extraction_audit["page_count"] == 5


def test_needs_ocr_with_too_little_pypdf_text_is_scanned(monkeypatch) -> None:
    _install_fake_reader(monkeypatch, ["short text"])
    monkeypatch.setattr(
        extractors,
        "_load_anydoc",
        lambda: _fake_anydoc(_FakeNeedsOcrError([1], 1)),
    )

    result = extract_pdf_document(b"%PDF-fake", "https://example.org/reports/item.pdf")

    assert result.extraction_status == "scanned_pdf"
    assert result.extraction_audit["final_engine"] == "pypdf"
    assert result.extraction_audit["ocr_pages"] == [1]


@pytest.mark.parametrize(
    "error",
    [_FakeEncryptedError("password required"), _FakeResourceLimitError("node limit")],
)
def test_anydoc_security_rejections_do_not_fall_back(monkeypatch, error) -> None:  # type: ignore[no-untyped-def]
    extraction_calls = _install_fake_reader(monkeypatch, ["strategy " * 600])
    monkeypatch.setattr(extractors, "_load_anydoc", lambda: _fake_anydoc(error))

    result = extract_pdf_document(b"%PDF-fake", "https://example.org/reports/item.pdf")

    assert result.extraction_status == "failed"
    assert result.extraction_audit["final_engine"] == "none"
    assert result.extraction_audit["fallback_reason"] == ""
    assert [item["engine"] for item in result.extraction_audit["attempts"]] == ["anydoc"]
    assert extraction_calls == []


def test_pypdf_only_never_loads_anydoc(monkeypatch) -> None:
    _install_fake_reader(monkeypatch, ["strategy " * 600])

    def fail_load():  # type: ignore[no-untyped-def]
        raise AssertionError("AnyDoc must not load in pypdf_only mode")

    monkeypatch.setattr(extractors, "_load_anydoc", fail_load)
    result = extract_pdf_document(
        b"%PDF-fake",
        "https://example.org/reports/item.pdf",
        pdf_parser_mode="pypdf_only",
    )

    assert result.extraction_status == "complete"
    assert result.extraction_audit["mode"] == "pypdf_only"
    assert [item["engine"] for item in result.extraction_audit["attempts"]] == ["pypdf"]


def test_both_parsers_failing_returns_audited_failure(monkeypatch) -> None:
    _install_fake_reader(
        monkeypatch,
        [""],
        page_errors={1: RuntimeError("page stream failed")},
    )
    monkeypatch.setattr(
        extractors,
        "_load_anydoc",
        lambda: _fake_anydoc(_FakeMalformedError("broken xref")),
    )

    result = extract_pdf_document(b"%PDF-fake", "https://example.org/reports/item.pdf")

    assert result.extraction_status == "failed"
    assert result.extraction_audit["final_engine"] == "none"
    assert [item["outcome"] for item in result.extraction_audit["attempts"]] == [
        "anydoc_malformed",
        "failed",
    ]


def test_pypdf_metadata_failure_does_not_discard_anydoc_body(monkeypatch) -> None:
    class BrokenReader:
        def __init__(self, _stream) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("metadata xref failed")

    monkeypatch.setattr(extractors, "PdfReader", BrokenReader)
    monkeypatch.setattr(
        extractors,
        "_load_anydoc",
        lambda: _fake_anydoc("# Report\n\n" + "strategy " * 600),
    )

    result = extract_pdf_document(
        b"%PDF-fake",
        "https://example.org/reports/item.pdf",
        fallback_title="Fallback title",
    )

    assert result.extraction_status == "complete"
    assert result.title == "Fallback title"
    assert result.extraction_audit["final_engine"] == "anydoc"
    assert any("元数据读取失败" in warning for warning in result.warnings)


def test_anydoc_calls_are_serialized_by_single_concurrency_gate(monkeypatch) -> None:
    _install_fake_reader(monkeypatch, ["unused"])
    state_lock = threading.Lock()
    active = 0
    maximum = 0

    def convert() -> str:
        nonlocal active, maximum
        with state_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with state_lock:
            active -= 1
        return "# Report\n\n" + "strategy " * 600

    monkeypatch.setattr(extractors, "_load_anydoc", lambda: _fake_anydoc(convert))
    threads = [
        threading.Thread(
            target=extract_pdf_document,
            args=(b"%PDF-fake", f"https://example.org/reports/{index}.pdf"),
        )
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert maximum == 1


def test_markdown_markup_does_not_change_length_thresholds() -> None:
    english = " ".join(["strategy"] * 1500)
    english_markdown = f"# Strategic report\n\n{english}\n\n[Source](https://example.org/report)"
    plain_en = detect_language_and_length(english)
    markdown_en = detect_language_and_length(extractors._markdown_text_for_length(english_markdown))
    assert plain_en[1] == 1500
    assert 1500 <= markdown_en[1] <= 1503

    chinese = "战略" * 1500
    chinese_markdown = f"# 标题\n\n{chinese}\n\n| 字段 | 值 |\n|---|---|"
    plain_zh = detect_language_and_length(chinese)
    markdown_zh = detect_language_and_length(extractors._markdown_text_for_length(chinese_markdown))
    assert plain_zh[2] == 3000
    assert markdown_zh[2] >= 3000


def test_anydoc_reject_ocr_path_never_opens_network(monkeypatch) -> None:
    import anydoc

    network_calls: list[object] = []

    def fail_network(*args, **kwargs):  # type: ignore[no-untyped-def]
        network_calls.append((args, kwargs))
        raise AssertionError("network access is forbidden")

    def needs_ocr(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise anydoc.NeedsOcrError("page 1 needs OCR")

    monkeypatch.setattr(anydoc.urllib.request, "urlopen", fail_network)
    monkeypatch.setattr(anydoc, "_to_markdown_bytes", needs_ocr)

    with pytest.raises(anydoc.NeedsOcrError):
        anydoc.to_markdown_bytes(b"%PDF-fake", "pdf", ocr="reject")
    assert network_calls == []
