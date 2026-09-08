from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path

from sqlalchemy import select

import thinktank_tracker.run_service as run_service
from thinktank_tracker.crawler import DiscoveredLink, FetchedResource
from thinktank_tracker.extractors import ExtractedDocument
from thinktank_tracker.constants import DateScope
from thinktank_tracker.models import Candidate, CandidateDiscovery, Source, SourceEntry, SourceEntryRun, SourceRun, utcnow
from thinktank_tracker.schemas import ImportedSource, ImportedSourceEntry
from thinktank_tracker.source_service import serialize_sources, upsert_sources


def test_upsert_groups_multiple_entries_and_preserves_fetch_urls(db_session) -> None:
    imported = ImportedSource(
        name="European Policy Centre",
        homepage_url="https://www.epc.eu/",
        entries=[
            ImportedSourceEntry(
                url="https://www.epc.eu/publications/",
                adapter_key="epc",
            ),
            ImportedSourceEntry(
                url="https://www.epc.eu/publication/special-series/",
                adapter_key="epc",
            ),
        ],
    )

    assert upsert_sources(db_session, [imported]) == (1, 0)
    db_session.commit()

    sources = db_session.scalars(select(Source)).all()
    entries = db_session.scalars(select(SourceEntry).order_by(SourceEntry.id)).all()
    assert len(sources) == 1
    assert [entry.url for entry in entries] == [
        "https://www.epc.eu/publications/",
        "https://www.epc.eu/publication/special-series/",
    ]
    payload = serialize_sources(db_session)
    assert len(payload) == 1
    assert len(payload[0]["entries"]) == 2


def test_reimport_deduplicates_trailing_slash_identity(db_session) -> None:
    first = ImportedSource(
        name="Institute",
        homepage_url="https://example.org/",
        entries=[ImportedSourceEntry(url="https://example.org/research/")],
    )
    second = ImportedSource(
        name="Institute",
        homepage_url="https://example.org",
        entries=[ImportedSourceEntry(url="https://example.org/research")],
    )

    upsert_sources(db_session, [first])
    upsert_sources(db_session, [second])
    db_session.commit()

    assert len(db_session.scalars(select(Source)).all()) == 1
    assert len(db_session.scalars(select(SourceEntry)).all()) == 1
    assert db_session.scalar(select(SourceEntry)).url == "https://example.org/research"


def test_known_source_reimport_matches_legacy_homepage_variant(db_session) -> None:
    legacy = Source(
        name="European Policy Centre",
        homepage_url="https://www.epc.eu/en/",
        publication_url="https://www.epc.eu/publications/",
        adapter_key="epc",
    )
    db_session.add(legacy)
    db_session.commit()
    imported = ImportedSource(
        name="European Policy Centre",
        homepage_url="https://www.epc.eu/",
        entries=[ImportedSourceEntry(url="https://www.epc.eu/publications/", adapter_key="epc")],
    )

    assert upsert_sources(db_session, [imported]) == (0, 1)
    db_session.commit()

    assert len(db_session.scalars(select(Source)).all()) == 1
    assert db_session.scalar(select(Source)).homepage_url == "https://www.epc.eu/"


def test_run_creates_one_source_run_with_two_entry_runs(db_session, monkeypatch) -> None:
    imported = ImportedSource(
        name="Institute",
        homepage_url="https://example.org/",
        entries=[
            ImportedSourceEntry(url="https://example.org/research/"),
            ImportedSourceEntry(url="https://example.org/reports/"),
        ],
    )
    upsert_sources(db_session, [imported])
    db_session.commit()

    @contextmanager
    def fake_scope():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    monkeypatch.setattr(run_service, "session_scope", fake_scope)
    run_id = run_service.create_run(date(2026, 8, 2), date(2026, 8, 26))

    assert len(db_session.scalars(select(SourceRun).where(SourceRun.run_id == run_id)).all()) == 1
    assert len(db_session.scalars(select(SourceEntryRun).where(SourceEntryRun.run_id == run_id)).all()) == 2


def test_duplicate_candidate_keeps_both_entry_discoveries(db_session, monkeypatch, tmp_path) -> None:
    imported = ImportedSource(
        name="Institute",
        homepage_url="https://example.org/",
        entries=[
            ImportedSourceEntry(url="https://example.org/research/"),
            ImportedSourceEntry(url="https://example.org/reports/"),
        ],
    )
    upsert_sources(db_session, [imported])
    db_session.commit()

    @contextmanager
    def fake_scope():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    monkeypatch.setattr(run_service, "session_scope", fake_scope)
    run_id = run_service.create_run(date(2026, 8, 2), date(2026, 8, 26))
    source = db_session.scalar(select(Source))
    entries = db_session.scalars(select(SourceEntry).order_by(SourceEntry.id)).all()

    class FakeCrawler:
        def extract(self, link):
            return (
                ExtractedDocument(
                    title="Shared report",
                    text="short",
                    date_status="missing",
                    extraction_status="complete",
                ),
                FetchedResource(
                    url=link.fetch_url,
                    body=b"content",
                    content_type="text/html",
                    cache_path=Path(tmp_path) / "cache" / "downloads" / "page.html",
                ),
            )

    canonical = "https://example.org/report/shared"
    run_service._persist_candidate(
        run_id,
        source.id,
        entries[0].id,
        DiscoveredLink(
            "https://example.org/report/shared/",
            canonical,
            entries[0].url,
            "Shared report",
        ),
        FakeCrawler(),
    )
    run_service._persist_candidate(
        run_id,
        source.id,
        entries[1].id,
        DiscoveredLink(
            "https://example.org/report/shared",
            canonical,
            entries[1].url,
            "Shared report",
        ),
        FakeCrawler(),
    )

    assert len(db_session.scalars(select(Candidate)).all()) == 1
    assert len(db_session.scalars(select(CandidateDiscovery)).all()) == 2


def test_deleted_entry_is_not_exported_and_reimport_restores_it(db_session) -> None:
    imported = ImportedSource(
        name="Institute",
        homepage_url="https://example.org/",
        entries=[ImportedSourceEntry(url="https://example.org/reports/")],
    )
    upsert_sources(db_session, [imported])
    db_session.commit()
    entry = db_session.scalar(select(SourceEntry))
    entry.deleted_at = utcnow()
    entry.enabled = False
    db_session.commit()

    assert serialize_sources(db_session) == []

    upsert_sources(db_session, [imported])
    db_session.commit()
    assert entry.deleted_at is None
    assert entry.enabled is True


def test_reimport_changed_discovery_rules_resets_link_test_result(db_session) -> None:
    imported = ImportedSource(
        name="Rule source",
        homepage_url="https://rules.example/",
        entries=[ImportedSourceEntry(url="https://rules.example/reports/")],
    )
    upsert_sources(db_session, [imported])
    entry = db_session.scalar(select(SourceEntry))
    entry.last_test_status = "success"
    entry.last_test_message = "old result"
    entry.last_test_http_status = 200
    db_session.commit()

    changed = ImportedSource(
        name="Rule source",
        homepage_url="https://rules.example/",
        entries=[
            ImportedSourceEntry(
                url="https://rules.example/reports/",
                allowed_path_patterns=[r"/reports/.+"],
            )
        ],
    )
    upsert_sources(db_session, [changed])
    db_session.commit()

    assert entry.last_test_status == "untested"
    assert entry.last_test_message == ""
    assert entry.last_test_http_status is None


def test_listing_card_date_fills_missing_detail_date(db_session, monkeypatch, tmp_path) -> None:
    imported = ImportedSource(
        name="Institute",
        homepage_url="https://example.org/",
        entries=[ImportedSourceEntry(url="https://example.org/reports/")],
    )
    upsert_sources(db_session, [imported])
    db_session.commit()

    @contextmanager
    def fake_scope():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    monkeypatch.setattr(run_service, "session_scope", fake_scope)
    run_id = run_service.create_run(date(2026, 8, 2), date(2026, 8, 26))
    source = db_session.scalar(select(Source))
    entry = db_session.scalar(select(SourceEntry))

    class FakeCrawler:
        def extract(self, link):
            return (
                ExtractedDocument(
                    title="Dated report",
                    text="short",
                    date_status="missing",
                    content_type="reports",
                    extraction_status="complete",
                ),
                FetchedResource(
                    url=link.fetch_url,
                    body=b"content",
                    content_type="text/html",
                    cache_path=Path(tmp_path) / "page.html",
                ),
            )

    run_service._persist_candidate(
        run_id,
        source.id,
        entry.id,
        DiscoveredLink(
            "https://example.org/reports/dated/",
            "https://example.org/reports/dated",
            entry.url,
            "Dated report",
            date(2026, 8, 8),
            "栏目卡片 time[datetime]",
        ),
        FakeCrawler(),
    )

    candidate = db_session.scalar(select(Candidate))
    assert candidate.published_date == date(2026, 8, 8)
    assert candidate.date_status == "confirmed"


def test_out_of_window_candidate_is_metadata_only_and_never_extracts_pdf(db_session, monkeypatch) -> None:
    imported = ImportedSource(
        name="Institute",
        homepage_url="https://example.org/",
        entries=[ImportedSourceEntry(url="https://example.org/reports/")],
    )
    upsert_sources(db_session, [imported])
    db_session.commit()

    @contextmanager
    def fake_scope():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    monkeypatch.setattr(run_service, "session_scope", fake_scope)
    run_id = run_service.create_run(date(2026, 8, 2), date(2026, 8, 26))
    source = db_session.scalar(select(Source))
    entry = db_session.scalar(select(SourceEntry))

    class NoExtractCrawler:
        def extract(self, _link):
            raise AssertionError("范围外内容不得下载正文或 PDF")

    run_service._persist_candidate(
        run_id,
        source.id,
        entry.id,
        DiscoveredLink(
            "https://example.org/reports/old/",
            "https://example.org/reports/old",
            entry.url,
            "Old report",
            date(2026, 7, 31),
            "栏目卡片日期",
            resolved_date=date(2026, 7, 31),
            resolved_date_status="confirmed",
            date_scope=DateScope.BEFORE_START,
        ),
        NoExtractCrawler(),
    )
    candidate = db_session.scalar(select(Candidate))
    assert candidate.date_scope == DateScope.BEFORE_START
    assert candidate.extraction_status == "metadata_only"
    assert candidate.extracted_text == ""
    assert "栏目卡片" in candidate.date_evidence
