from __future__ import annotations

from pathlib import Path

import pytest

from thinktank_tracker.adapters import ADAPTERS, merge_selectors, resolve_effective_adapter
from thinktank_tracker.config import AppConfig
from thinktank_tracker.crawler import DiscoveredLink
from thinktank_tracker.extractors import ExtractedDocument, discover_content_cards, extract_html_document
from thinktank_tracker.run_service import _resolve_candidate_authors, _resolve_candidate_content_type
from thinktank_tracker.url_utils import resolve_content_type


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("adapter_key", "base_url", "expected_type", "expected_author"),
    [
        ("ecfr", "https://ecfr.eu/analysis/", "commentary", "Alice Example"),
        ("euiss", "https://www.iss.europa.eu/publications", "analysis", "Clotilde Example"),
        ("ceps", "https://www.ceps.eu/ceps-publications/", "policy contribution", "Johan Example"),
        ("martens", "https://www.martenscentre.eu/publication/", "policy brief", "Elena Example"),
        ("egmont", "https://egmontinstitute.be/publications/", "policy brief", "Paulina Example"),
        ("epc", "https://www.epc.eu/publications/", "commentary", "Tobias Example"),
        ("elcano", "https://www.realinstitutoelcano.org/publicaciones/", "analysis", "Miguel Example"),
    ],
)
def test_fixed_listing_samples(adapter_key, base_url, expected_type, expected_author) -> None:
    adapter = ADAPTERS[adapter_key]
    cards = discover_content_cards(
        (FIXTURES / f"{adapter_key}.html").read_bytes(),
        base_url,
        adapter.listing_link_selectors,
        (),
        AppConfig().content_type_rules,
        adapter.listing_container_selectors,
        adapter.listing_content_type_selectors,
        adapter.listing_author_selectors,
    )

    assert len(cards) == 1
    assert cards[0].content_type == expected_type
    assert expected_author in cards[0].authors
    assert cards[0].published_date is not None


def test_ceps_hidden_hover_label_and_data_author_are_read_from_dom() -> None:
    adapter = ADAPTERS["ceps"]
    card = discover_content_cards(
        (FIXTURES / "ceps.html").read_bytes(),
        "https://www.ceps.eu/ceps-publications/",
        adapter.listing_link_selectors,
        (),
        AppConfig().content_type_rules,
        adapter.listing_container_selectors,
        adapter.listing_content_type_selectors,
        adapter.listing_author_selectors,
    )[0]

    assert card.content_type == "policy contribution"
    assert "Policy Contribution" in card.content_type_evidence
    assert card.authors == ("Johan Example",)
    assert "data-authors" in card.author_evidence
    assert card.content_type_labels == ("Policy Contribution",)


def test_saved_old_rules_gain_new_aliases_and_normalize_known_values() -> None:
    old_rules = [
        {
            "match_kind": "url_path",
            "match_value": "comentarios",
            "content_type": "comentarios",
            "action": "allow",
        }
    ]
    commissioned = resolve_content_type(
        "https://www.ceps.eu/ceps-publications/study/",
        ["Commissioned reports"],
        old_rules,
    )
    commentary = resolve_content_type(
        "https://www.realinstitutoelcano.org/comentarios/example/",
        [],
        old_rules,
    )
    assert commissioned[0] == "report"
    assert commentary[0] == "commentary"


def test_elcano_topic_taxonomy_is_not_treated_as_content_type() -> None:
    adapter = ADAPTERS["elcano"]
    card = discover_content_cards(
        (FIXTURES / "elcano.html").read_bytes(),
        "https://www.realinstitutoelcano.org/publicaciones/",
        adapter.listing_link_selectors,
        (),
        AppConfig().content_type_rules,
        adapter.listing_container_selectors,
        adapter.listing_content_type_selectors,
        adapter.listing_author_selectors,
    )[0]
    assert card.content_type == "analysis"
    assert card.content_type_labels == ()


def test_commissioned_reports_maps_to_report_and_related_label_is_ignored() -> None:
    raw = b"""
    <main><article><h1>Commissioned study</h1>
      <span class="ut-label" style="display:none;opacity:0">Commissioned reports</span>
      <p>Research body</p></article>
      <section class="related"><span class="ut-label">Policy Contribution</span></section>
    </main>
    """
    document = extract_html_document(
        raw,
        "https://www.ceps.eu/ceps-publications/commissioned-study/",
        AppConfig().content_type_rules,
        ADAPTERS["ceps"].detail_content_type_selectors,
    )
    assert document.content_type == "report"
    assert document.content_type_labels == ["Commissioned reports"]
    assert "Policy Contribution" not in document.content_type_evidence


def test_auto_adapter_and_selector_merge_are_deterministic() -> None:
    key, adapter = resolve_effective_adapter("auto", "https://ecfr.eu/analysis/")
    assert key == "ecfr"
    assert adapter is ADAPTERS["ecfr"]
    assert resolve_effective_adapter("generic", "https://ecfr.eu/analysis/")[0] == "generic"
    assert merge_selectors((".custom a",), (".built-in a", ".custom a")) == (
        ".custom a",
        ".built-in a",
    )


def test_content_type_resolution_matrix_is_deterministic() -> None:
    rules = AppConfig().content_type_rules
    assert resolve_content_type("https://example.org/analysis/item", (), rules)[0] == "analysis"
    assert resolve_content_type("https://example.org/item", ("Policy Briefs",), rules)[0] == "policy brief"
    assert resolve_content_type("https://example.org/analysis/item", ("Analyses",), rules)[0] == "analysis"

    conflict = resolve_content_type(
        "https://example.org/analysis/item", ("Policy Brief",), rules
    )
    assert conflict[0] == "unknown"
    assert conflict[2] == "review"
    assert "体裁冲突" in conflict[1]

    unknown = resolve_content_type("https://example.org/item", ("Future Format",), rules)
    assert unknown == (
        "Future Format",
        "栏目卡片体裁标签未映射：Future Format",
        "review",
    )


def test_custom_card_selector_and_exact_alias_matching() -> None:
    rules = [
        {
            "match_kind": "card_label",
            "match_value": "strategic memo",
            "content_type": "strategic memo",
            "action": "allow",
        }
    ]
    raw = b"""
    <main><div class="new-card"><a class="new-title" href="/item/one">New item</a>
    <span class="format">  Strategic   Memo </span></div></main>
    """
    card = discover_content_cards(
        raw,
        "https://new.example/publications/",
        (".new-card .new-title",),
        (),
        rules,
        (".new-card",),
        (".format",),
    )[0]
    assert card.content_type == "strategic memo"
    assert resolve_content_type("https://new.example/item/two", ("Strategic Memos",), rules)[2] == "review"


def test_detail_unknown_never_overwrites_listing_type_or_authors() -> None:
    link = DiscoveredLink(
        "https://example.org/item",
        "https://example.org/item",
        listing_content_type="policy brief",
        listing_content_type_evidence="栏目卡片标签 'Policy Brief' -> policy brief",
        listing_authors=("Listing Author",),
        listing_author_evidence="栏目卡片作者",
    )
    document = ExtractedDocument(
        content_type="unknown",
        content_type_evidence="URL 路径未命中已配置的体裁规则",
    )
    assert _resolve_candidate_content_type(document, link)[0] == "policy brief"
    assert _resolve_candidate_authors(document, link)[0] == ["Listing Author"]


def test_author_priority_json_ld_normalization_and_exact_deduplication() -> None:
    raw = b"""
    <html><head><meta name="author" content="Ignored Meta"><script type="application/ld+json">
    {"@type":"Article","author":[{"name":"By Alice Example"},{"name":"Alice Example"},{"name":"ALICE EXAMPLE"},{"name":"Bob Example"}]}
    </script></head><body><article><h1>Authors</h1><p>""" + b"word " * 500 + b"""</p></article></body></html>
    """
    result = extract_html_document(raw, "https://example.org/analysis/authors")
    assert result.authors == ["Alice Example", "ALICE EXAMPLE", "Bob Example"]
    assert result.author_evidence.startswith("JSON-LD author")
