from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from thinktank_tracker.constants import CandidateStatus, DateScope, Decision, LengthStatus
from thinktank_tracker.models import (
    Candidate,
    CandidateAttachment,
    CandidateDiscovery,
    CrawlRun,
    Source,
    SourceEntry,
)
from thinktank_tracker.crawler import AttachmentResult, DiscoveredLink, FetchedResource
from thinktank_tracker.extractors import DiscoveredAttachment, ExtractedDocument
from thinktank_tracker.review_service import save_review
from thinktank_tracker.run_service import (
    _hard_filter,
    _persist_attachment,
    _resolve_candidate_date,
    reclassify_run_content_types,
)


def candidate(**overrides) -> Candidate:
    defaults = dict(
        run_id=1,
        source_id=1,
        canonical_url="https://example.org/paper",
        original_url="https://example.org/paper",
        published_date=date(2026, 8, 2),
        date_status="confirmed",
        content_type="policy brief",
        language="en",
        english_word_count=1600,
        chinese_char_count=0,
        extraction_status="complete",
        extracted_text="Research evidence. " * 800,
    )
    defaults.update(overrides)
    return Candidate(**defaults)


def test_hard_filter_includes_both_boundary_dates() -> None:
    start, end = date(2026, 8, 2), date(2026, 8, 26)
    assert _hard_filter(candidate(published_date=start), start, end)[0] == CandidateStatus.DISCOVERED
    assert _hard_filter(candidate(published_date=end), start, end)[0] == CandidateStatus.DISCOVERED
    assert _hard_filter(candidate(published_date=date(2026, 8, 1)), start, end)[0] == CandidateStatus.HARD_EXCLUDED
    assert _hard_filter(candidate(content_type="policy brief"), start, end)[0] == CandidateStatus.DISCOVERED


def test_unknown_date_does_not_block_analysis_and_configured_length_is_a_hard_gate() -> None:
    start, end = date(2026, 8, 2), date(2026, 8, 26)
    assert _hard_filter(candidate(published_date=None, date_status="conflict"), start, end)[0] == CandidateStatus.DISCOVERED
    borderline = candidate(english_word_count=1400)
    assert _hard_filter(borderline, start, end)[0] == CandidateStatus.HARD_EXCLUDED
    assert borderline.length_status == LengthStatus.BELOW


def test_detail_and_listing_date_conflict_is_not_silently_resolved() -> None:
    published, status, evidence = _resolve_candidate_date(
        ExtractedDocument(
            published_date=date(2026, 8, 8),
            date_status="confirmed",
            date_evidence="详情页 meta",
        ),
        DiscoveredLink(
            "https://example.org/reports/item/",
            "https://example.org/reports/item",
            listing_date=date(2026, 8, 9),
            listing_date_evidence="栏目卡片",
        ),
    )
    assert published is None
    assert status == "conflict"
    assert "2026-08-08" in evidence and "2026-08-09" in evidence


def test_rule_reclassification_is_limited_to_requested_run(db_session) -> None:
    source = Source(
        name="Institute",
        homepage_url="https://example.org/",
        publication_url="https://example.org/publications/",
    )
    run_one = CrawlRun(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
    run_two = CrawlRun(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
    db_session.add_all([source, run_one, run_two])
    db_session.flush()
    entry = SourceEntry(
        source_id=source.id,
        url="https://example.org/publications/",
        canonical_url="https://example.org/publications",
    )
    db_session.add(entry)
    db_session.flush()
    items = []
    for run, slug in ((run_one, "one"), (run_two, "two")):
        item = candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url=f"https://example.org/item/{slug}",
            original_url=f"https://example.org/item/{slug}/",
            content_type="unknown",
            content_type_evidence="legacy URL-only result",
            content_type_raw_labels_json='["Future Memo"]',
            date_scope=DateScope.IN_WINDOW,
            length_status=LengthStatus.PASS,
        )
        db_session.add(item)
        db_session.flush()
        db_session.add(
            CandidateDiscovery(
                candidate_id=item.id,
                source_entry_id=entry.id,
                listing_url=entry.url,
                fetch_url=item.original_url,
                listing_content_type="Future Memo",
                listing_content_type_evidence="栏目卡片体裁标签未映射：Future Memo",
                listing_content_type_labels_json='["Future Memo"]',
            )
        )
        items.append(item)
    db_session.flush()
    rules = [
        {
            "match_kind": "card_label",
            "match_value": "Future Memo",
            "content_type": "strategic memo",
            "action": "allow",
        }
    ]

    changed = reclassify_run_content_types(db_session, run_one.id, rules)
    db_session.flush()

    assert changed == 1
    assert items[0].content_type == "strategic memo"
    assert items[1].content_type == "unknown"


def test_review_blocks_approval_until_date_confirmed(db_session) -> None:
    source = Source(name="Institute", homepage_url="https://example.org/", publication_url="https://example.org/publications")
    run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 26))
    db_session.add_all([source, run])
    db_session.flush()
    item = candidate(run_id=run.id, source_id=source.id, published_date=None, date_status="missing")
    db_session.add(item)
    db_session.flush()
    with pytest.raises(ValueError, match="发布日期"):
        save_review(db_session, item.id, Decision.INCLUDE, "macro", [], "English", "中文", "report", "核心观点", "")


def test_review_approves_confirmed_candidate(db_session) -> None:
    source = Source(name="Institute", homepage_url="https://example.org/", publication_url="https://example.org/publications")
    run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 26))
    db_session.add_all([source, run])
    db_session.flush()
    item = candidate(run_id=run.id, source_id=source.id)
    db_session.add(item)
    db_session.flush()
    review = save_review(
        db_session, item.id, Decision.INCLUDE, "macro", ["technology_competition"],
        "Strategic competition", "战略竞争", "report", "竞争正在重塑国际秩序。", "verified",
        authors=["Alice Example"],
    )
    assert item.status == CandidateStatus.APPROVED
    assert review.primary_category == "macro"


def test_review_can_confirm_missing_date_and_approve_in_one_save(db_session) -> None:
    source = Source(name="Institute", homepage_url="https://example.org/", publication_url="https://example.org/publications")
    run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 26))
    db_session.add_all([source, run])
    db_session.flush()
    item = candidate(run_id=run.id, source_id=source.id, published_date=None, date_status="missing")
    db_session.add(item)
    db_session.flush()

    save_review(
        db_session,
        item.id,
        Decision.INCLUDE,
        "macro",
        [],
        "English title",
        "中文标题",
        "policy brief",
        "核心观点",
        "",
        authors=["Alice Example"],
        published_date=date(2026, 8, 8),
        confirm_date=True,
    )

    assert item.status == CandidateStatus.APPROVED
    assert item.published_date == date(2026, 8, 8)
    assert item.date_status == "confirmed"
    assert "人工核对原文并确认" in item.date_evidence


def test_review_reports_all_missing_approval_fields_at_once(db_session) -> None:
    source = Source(name="Institute", homepage_url="https://example.org/", publication_url="https://example.org/publications")
    run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 26))
    db_session.add_all([source, run])
    db_session.flush()
    item = candidate(run_id=run.id, source_id=source.id, published_date=None, date_status="missing", content_type="unknown")
    db_session.add(item)
    db_session.flush()

    with pytest.raises(ValueError) as exc_info:
        save_review(db_session, item.id, Decision.INCLUDE, "", [], "", "", "unknown", "", "")

    message = str(exc_info.value)
    assert "确认发布日期" in message
    assert "选择主分类" in message
    assert "填写英文标题" in message
    assert "填写中文标题" in message
    assert "选择或填写体裁" in message
    assert "填写至少一位作者" in message
    assert "填写一句话核心观点" in message


def test_review_allows_custom_research_type_but_rejects_explicitly_excluded_type(db_session) -> None:
    source = Source(name="Institute", homepage_url="https://example.org/", publication_url="https://example.org/publications")
    run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 26))
    db_session.add_all([source, run])
    db_session.flush()
    item = candidate(run_id=run.id, source_id=source.id)
    db_session.add(item)
    db_session.flush()

    review = save_review(
        db_session, item.id, Decision.INCLUDE, "macro", [], "Title", "标题",
        "commissioned report", "核心观点", "",
        authors=["Alice Example"],
    )
    assert review.content_type == "commissioned report"

    with pytest.raises(ValueError, match="明确排除"):
        save_review(
            db_session, item.id, Decision.INCLUDE, "macro", [], "Title", "标题",
            "news", "核心观点", "",
            authors=["Alice Example"],
        )


def test_attachment_persistence_keeps_pdf_extraction_audit(db_session) -> None:
    source = Source(name="Institute", homepage_url="https://example.org/")
    run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 26))
    db_session.add_all([source, run])
    db_session.flush()
    item = candidate(run_id=run.id, source_id=source.id)
    db_session.add(item)
    db_session.flush()
    audit = {
        "final_engine": "pypdf",
        "fallback_reason": "anydoc_malformed",
        "duration_ms": 18.25,
    }
    link = DiscoveredAttachment(
        url="https://example.org/report.pdf",
        canonical_url="https://example.org/report.pdf",
        link_text="Report PDF",
        role="primary",
    )
    resource = FetchedResource(
        url=link.url,
        body=b"%PDF-1.7 test",
        content_type="application/pdf",
        cache_path=Path("report.pdf"),
        status_code=200,
    )
    _persist_attachment(
        db_session,
        item.id,
        AttachmentResult(
            link=link,
            resource=resource,
            document=ExtractedDocument(
                extraction_status="complete",
                extraction_audit=audit,
            ),
            used_as_body=True,
        ),
    )
    db_session.flush()

    attachment = db_session.query(CandidateAttachment).one()
    assert json.loads(attachment.extraction_audit_json) == audit
    assert attachment.used_as_body is True
