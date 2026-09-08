from __future__ import annotations

import json
from dataclasses import replace

import pytest

import thinktank_tracker.config as config_module
from thinktank_tracker.config import (
    AppConfig,
    load_config,
    parse_content_type_rules,
    parse_url_content_type_rules,
)
from thinktank_tracker.paths import PATHS
from thinktank_tracker.url_utils import content_type_from_url


def test_default_url_rules_normalize_spanish_and_english_analysis() -> None:
    config = AppConfig()

    assert content_type_from_url(
        "https://example.org/analisis/geopolitica/",
        config.url_content_type_rules,
    )[0] == "analysis"
    assert content_type_from_url(
        "https://example.org/analysis/geopolitics/",
        config.url_content_type_rules,
    )[0] == "analysis"
    assert "analysis" in config.content_types
    assert "analisis" not in config.content_types


def test_custom_url_rule_matches_only_a_complete_path_segment() -> None:
    rules = parse_url_content_type_rules(["essays"], ["research essay"])

    assert content_type_from_url("https://example.org/essays/item", rules)[0] == "research essay"
    assert content_type_from_url("https://example.org/essays-and-ideas/item", rules)[0] == "unknown"
    assert content_type_from_url("https://example.org/item?type=essays", rules)[0] == "unknown"


def test_url_rule_parser_normalizes_and_rejects_duplicates() -> None:
    assert parse_url_content_type_rules(["Policy_Brief"], ["Policy   Brief"]) == [
        {"keyword": "policy-brief", "content_type": "policy brief"}
    ]

    with pytest.raises(ValueError, match="重复"):
        parse_url_content_type_rules(["report", "REPORT"], ["report", "reports"])


def test_unified_rule_parser_normalizes_all_fields_and_actions() -> None:
    parsed = parse_content_type_rules(
        ["url_path", "card_label"],
        ["Policy_Brief", "  Commentaries  "],
        ["Policy Briefs", "Commentary"],
        ["allow", "exclude"],
    )
    normalized = [
        {key: value for key, value in rule.items() if key != "rule_id"}
        for rule in parsed
    ]
    assert normalized[:2] == [
        {
            "match_kind": "url_path",
            "match_value": "policy-brief",
            "content_type": "policy brief",
            "action": "allow",
        },
        {
            "match_kind": "card_label",
            "match_value": "commentaries",
            "content_type": "commentary",
            "action": "exclude",
        },
    ]
    assert {
        "match_kind": "card_label",
        "match_value": "commissioned reports",
        "content_type": "report",
        "action": "allow",
    } in normalized
    assert all(rule["rule_id"].startswith("ctr-") for rule in parsed)
    assert parsed == parse_content_type_rules(
        ["url_path", "card_label"],
        ["Policy_Brief", "  Commentaries  "],
        ["Policy Briefs", "Commentary"],
        ["allow", "exclude"],
    )


def test_invalid_saved_pdf_parser_mode_is_repaired_to_anydoc_first(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    paths = replace(
        PATHS,
        root=tmp_path,
        workspace=workspace,
        config=workspace / "config",
        cache=workspace / "cache",
        downloads=workspace / "cache" / "downloads",
        exports=workspace / "exports",
        backups=workspace / "backups",
        logs=workspace / "logs",
        database=workspace / "tracker.sqlite3",
    )
    monkeypatch.setattr(config_module, "PATHS", paths)
    paths.ensure()
    (paths.config / "app.json").write_text(
        json.dumps({"pdf_parser_mode": ["hosted_ocr"]}),
        encoding="utf-8",
    )

    assert load_config().pdf_parser_mode == "anydoc_first"
