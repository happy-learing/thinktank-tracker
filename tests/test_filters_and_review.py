from __future__ import annotations

from datetime import date

import pytest

from thinktank_tracker.constants import CandidateStatus, Decision
from thinktank_tracker.models import Candidate, CrawlRun, Source
from thinktank_tracker.review_service import save_review
from thinktank_tracker.run_service import _hard_filter


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
    )
    defaults.update(overrides)
    return Candidate(**defaults)


def test_hard_filter_includes_both_boundary_dates() -> None:
    start, end = date(2026, 8, 2), date(2026, 8, 26)
    assert _hard_filter(candidate(published_date=start), start, end)[0] == CandidateStatus.DISCOVERED
    assert _hard_filter(candidate(published_date=end), start, end)[0] == CandidateStatus.DISCOVERED
    assert _hard_filter(candidate(published_date=date(2026, 8, 1)), start, end)[0] == CandidateStatus.HARD_EXCLUDED


def test_ambiguous_date_and_borderline_length_require_review() -> None:
    start, end = date(2026, 8, 2), date(2026, 8, 26)
    assert _hard_filter(candidate(published_date=None, date_status="conflict"), start, end)[0] == CandidateStatus.NEEDS_REVIEW
    assert _hard_filter(candidate(english_word_count=1400), start, end)[0] == CandidateStatus.NEEDS_REVIEW


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
    )
    assert item.status == CandidateStatus.APPROVED
    assert review.primary_category == "macro"
