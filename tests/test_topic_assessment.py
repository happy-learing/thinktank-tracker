from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import thinktank_tracker.run_service as run_service
from thinktank_tracker.config import AppConfig
from thinktank_tracker.constants import CandidateStatus, Decision, TopicStatus
from thinktank_tracker.crawler import DiscoveredLink, FetchedResource
from thinktank_tracker.extractors import ExtractedDocument
from thinktank_tracker.llm import ModelResult
from thinktank_tracker.models import Assessment, Candidate, CrawlRun, Source, SourceEntry, SourceRun
from thinktank_tracker.schemas import AssessmentPayload


def _seed(db_session):
    source = Source(
        name="Institute",
        homepage_url="https://example.org/",
        publication_url="https://example.org/reports/",
    )
    run = CrawlRun(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
    db_session.add_all([source, run])
    db_session.flush()
    entry = SourceEntry(
        source_id=source.id,
        url="https://example.org/reports/",
        canonical_url="https://example.org/reports",
    )
    source_run = SourceRun(
        run_id=run.id,
        source_id=source.id,
        source_name_snapshot="Institute",
    )
    db_session.add_all([entry, source_run])
    db_session.commit()
    return source, entry, run


def _install_scope(monkeypatch, db_session) -> None:
    @contextmanager
    def scope():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    monkeypatch.setattr(run_service, "session_scope", scope)


class _Crawler:
    config = AppConfig()

    def __init__(self, content_type: str = "report") -> None:
        self.content_type = content_type

    def extract(self, link):
        body = "Exact strategic evidence. " + "strategy " * 1600
        return (
            ExtractedDocument(
                title="Strategic report",
                text=body,
                language="en",
                english_word_count=1603,
                published_date=date(2026, 8, 12),
                date_status="confirmed",
                date_evidence="meta article:published_time",
                content_type=self.content_type,
                content_type_evidence="detail selector" if self.content_type != "unknown" else "not found",
                authors=["Alice Example"],
                author_evidence="JSON-LD author",
                extraction_status="complete",
                content_hash="a" * 64,
                abstract="Official abstract",
                tags=["security"],
                headings=["Conclusion"],
            ),
            FetchedResource(
                url=link.fetch_url,
                body=b"html",
                content_type="text/html",
                cache_path=Path("page.html"),
            ),
        )


@pytest.mark.parametrize(
    ("decision", "confidence", "evidence", "expected_status", "expected_topic"),
    [
        (Decision.INCLUDE, 0.75, "Exact strategic evidence.", CandidateStatus.MODEL_INCLUDED, TopicStatus.MODEL_INCLUDED),
        (Decision.INCLUDE, 0.749, "Exact strategic evidence.", CandidateStatus.MODEL_INCLUDED, TopicStatus.MODEL_INCLUDED),
        (Decision.EXCLUDE, 0.85, "Exact strategic evidence.", CandidateStatus.MODEL_EXCLUDED, TopicStatus.MODEL_EXCLUDED),
        (Decision.EXCLUDE, 0.849, "Exact strategic evidence.", CandidateStatus.MODEL_EXCLUDED, TopicStatus.MODEL_EXCLUDED),
        (Decision.INCLUDE, 0.99, "exact strategic evidence.", CandidateStatus.NEEDS_REVIEW, TopicStatus.NEEDS_REVIEW),
    ],
)
def test_verified_evidence_and_review_replace_self_reported_confidence_thresholds(
    db_session,
    monkeypatch,
    decision,
    confidence,
    evidence,
    expected_status,
    expected_topic,
) -> None:
    source, entry, run = _seed(db_session)
    _install_scope(monkeypatch, db_session)
    captured: list[dict] = []
    payload = AssessmentPayload(
        decision=decision,
        research_substance="substantive",
        title_en="Title", title_zh="标题",
        primary_category="macro" if decision == Decision.INCLUDE else None,
        reasoning_zh="判断理由",
        evidence=[evidence],
        confidence=confidence,
        strategic_nexus="战略关联" if decision == Decision.INCLUDE else "",
        exclusion_code="not_core_topic" if decision == Decision.EXCLUDE else "",
    )

    class Provider:
        def __init__(self, _config):
            self.config = SimpleNamespace(model_name="test-model", max_model_input_chars=20_000)

        def configured(self):
            return True

        def assess(self, title, source_name, content_type, text, metadata):
            captured.append(metadata)
            return ModelResult(payload, '{"decision":"test"}', evidence_text=text, input_complete=True, verification_status="supported")

    monkeypatch.setattr(run_service, "OpenAICompatibleProvider", Provider)
    monkeypatch.setattr(run_service, "load_config", AppConfig)
    run_service._persist_candidate(
        run.id,
        source.id,
        entry.id,
        DiscoveredLink(
            "https://example.org/reports/item",
            "https://example.org/reports/item",
            entry.url,
            "Strategic report",
            listing_date=date(2026, 8, 12),
            listing_date_evidence="card time[datetime]",
        ),
        _Crawler(),
    )

    candidate = db_session.scalar(select(Candidate))
    assessment = db_session.scalar(select(Assessment))
    assert candidate.status == expected_status, candidate.hard_filter_reason
    assert candidate.topic_status == expected_topic, candidate.hard_filter_reason
    assert assessment.confidence == confidence
    assert assessment.strategic_nexus == payload.strategic_nexus
    assert assessment.exclusion_code == payload.exclusion_code
    assert captured == [
        {
            "preflight_error": "",
            "abstract": "Official abstract",
            "tags": ["security"],
            "headings": ["Conclusion"],
            "extraction_audit": {},
        }
    ]
    if evidence != "Exact strategic evidence.":
        assert "逐字回查" in candidate.hard_filter_reason


def test_unknown_genre_allows_content_analysis_but_stays_pending(db_session, monkeypatch) -> None:
    source, entry, run = _seed(db_session)
    _install_scope(monkeypatch, db_session)

    class Provider:
        def __init__(self, _config):
            self.config = _config

        def configured(self):
            return True

        def assess(self, *args):
            return ModelResult(None, error="测试已进入模型判断")

    monkeypatch.setattr(run_service, "OpenAICompatibleProvider", Provider)
    run_service._persist_candidate(
        run.id,
        source.id,
        entry.id,
        DiscoveredLink(
            "https://example.org/item",
            "https://example.org/item",
            entry.url,
            "Unknown genre",
            listing_date=date(2026, 8, 12),
            listing_date_evidence="card time[datetime]",
        ),
        _Crawler("unknown"),
    )

    candidate = db_session.scalar(select(Candidate))
    assert candidate.status == CandidateStatus.NEEDS_REVIEW
    assert candidate.topic_status == TopicStatus.NEEDS_REVIEW
