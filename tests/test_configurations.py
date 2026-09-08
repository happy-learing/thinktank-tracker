from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine, select

import thinktank_tracker.run_service as run_service
from thinktank_tracker.config import AppConfig
from thinktank_tracker.configuration_service import (
    create_configuration,
    delete_configuration,
    duplicate_configuration,
    ensure_default_configuration,
    rename_configuration,
    restore_configuration,
)
from thinktank_tracker.constants import CoverageStatus
from thinktank_tracker.crawler import DomainRateLimiter, ScanResult
from thinktank_tracker.migrations import migrate
from thinktank_tracker.models import CrawlRun, Source, SourceConfiguration, SourceEntry, SourceEntryRun
from thinktank_tracker.schemas import ImportedSource, ImportedSourceEntry
from thinktank_tracker.source_service import save_sources_json, upsert_sources


def _source(url: str, entry_url: str) -> ImportedSource:
    return ImportedSource(
        name="Institute",
        homepage_url=url,
        entries=[ImportedSourceEntry(url=entry_url)],
    )


def test_configurations_own_independent_source_copies(db_session) -> None:
    first = ensure_default_configuration(db_session)
    second = create_configuration(db_session, "配置2")
    upsert_sources(
        db_session,
        [_source("https://example.org/", "https://example.org/reports/")],
        first.id,
    )
    upsert_sources(
        db_session,
        [_source("https://example.org/", "https://example.org/analysis/")],
        second.id,
    )
    db_session.commit()

    sources = db_session.scalars(select(Source).order_by(Source.configuration_id)).all()
    assert len(sources) == 2
    assert sources[0].homepage_url == sources[1].homepage_url
    sources[0].name = "Changed only in config 1"
    db_session.commit()
    assert sources[1].name == "Institute"
    assert sources[0].entries[0].url != sources[1].entries[0].url


def test_schema_v6_exports_current_or_all_configurations(db_session, tmp_path) -> None:
    first = ensure_default_configuration(db_session)
    second = create_configuration(db_session, "配置2")
    upsert_sources(
        db_session,
        [_source("https://one.example/", "https://one.example/reports/")],
        first.id,
    )
    upsert_sources(
        db_session,
        [_source("https://two.example/", "https://two.example/research/")],
        second.id,
    )
    db_session.commit()

    current_path = save_sources_json(db_session, tmp_path / "current.json", second.id)
    all_path = save_sources_json(db_session, tmp_path / "all.json")
    current = json.loads(current_path.read_text(encoding="utf-8"))
    all_configurations = json.loads(all_path.read_text(encoding="utf-8"))
    assert current["schema_version"] == 6
    assert [row["name"] for row in current["configurations"]] == ["配置2"]
    assert len(all_configurations["configurations"]) == 2
    assert all_configurations["configurations"][0]["sources"][0]["homepage_url"] == (
        "https://one.example/"
    )


def test_duplicate_configuration_deep_copies_and_resets_runtime_state(db_session) -> None:
    original = ensure_default_configuration(db_session)
    upsert_sources(
        db_session,
        [_source("https://example.org/", "https://example.org/reports/")],
        original.id,
    )
    entry = db_session.scalar(select(SourceEntry))
    source = entry.source
    entry.listing_selectors_json = '["article h2 a"]'
    entry.container_selectors_json = '["article.card"]'
    entry.content_type_selectors_json = '[".format"]'
    source.last_successful_date = date(2026, 8, 20)
    entry.last_successful_date = date(2026, 8, 20)
    entry.last_test_status = "success"
    entry.last_test_message = "old result"
    db_session.commit()

    copied = duplicate_configuration(db_session, original.id, "配置副本")
    db_session.commit()
    copied_source = db_session.scalar(
        select(Source).where(Source.configuration_id == copied.id)
    )
    copied_entry = copied_source.entries[0]
    assert copied_source.id != entry.source_id
    assert copied_entry.id != entry.id
    assert copied_entry.listing_selectors_json == '["article h2 a"]'
    assert copied_entry.container_selectors_json == '["article.card"]'
    assert copied_entry.content_type_selectors_json == '[".format"]'
    assert copied_entry.last_test_status == "untested"
    assert copied_entry.last_test_message == ""
    assert copied_source.last_successful_date is None
    assert copied_entry.last_successful_date is None


def test_configuration_names_and_soft_delete_restore_rules(db_session) -> None:
    first = ensure_default_configuration(db_session)
    second = create_configuration(db_session, "配置2")
    with pytest.raises(ValueError, match="同名"):
        create_configuration(db_session, "  配置2  ")
    with pytest.raises(ValueError, match="不能为空"):
        create_configuration(db_session, "   ")

    delete_configuration(db_session, second.id)
    assert second.deleted_at is not None
    with pytest.raises(ValueError, match="至少需要保留"):
        delete_configuration(db_session, first.id)

    replacement = create_configuration(db_session, "配置2")
    with pytest.raises(ValueError, match="同名"):
        restore_configuration(db_session, second.id)
    rename_configuration(db_session, replacement.id, "配置3")
    restored = restore_configuration(db_session, second.id)
    assert restored.deleted_at is None


def test_run_uses_only_selected_configuration_and_persists_entry_snapshot(
    db_session, monkeypatch
) -> None:
    first = ensure_default_configuration(db_session)
    second = create_configuration(db_session, "配置2")
    upsert_sources(
        db_session,
        [_source("https://one.example/", "https://one.example/reports/")],
        first.id,
    )
    upsert_sources(
        db_session,
        [_source("https://two.example/", "https://two.example/analysis/")],
        second.id,
    )
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
    run_id = run_service.create_run(
        date(2026, 8, 1),
        date(2026, 8, 28),
        configuration_id=second.id,
    )
    run = db_session.get(CrawlRun, run_id)
    entry_run = db_session.scalar(
        select(SourceEntryRun).where(SourceEntryRun.run_id == run_id)
    )
    assert run.configuration_id == second.id
    assert run.configuration_name_snapshot == "配置2"
    assert len(run.source_runs) == 1
    assert run.source_runs[0].source.configuration_id == second.id
    assert entry_run.entry_url_snapshot == "https://two.example/analysis/"
    assert json.loads(entry_run.entry_config_snapshot_json)["adapter_key"] == "auto"

    run.source_runs[0].entry_runs[0].source_entry.url = "https://two.example/changed/"
    db_session.commit()
    assert entry_run.entry_url_snapshot == "https://two.example/analysis/"


def test_existing_run_executes_saved_entry_snapshot_after_configuration_changes(
    db_session, monkeypatch
) -> None:
    configuration = ensure_default_configuration(db_session)
    upsert_sources(
        db_session,
        [
            ImportedSource(
                name="Snapshot institute",
                homepage_url="https://snapshot.example/",
                entries=[
                    ImportedSourceEntry(
                        url="https://snapshot.example/reports/",
                        adapter_key="euiss",
                        ordered_by_date=True,
                        listing_selectors=["article.old a"],
                        container_selectors=["article.old"],
                        content_type_selectors=[".old-format"],
                        date_selectors=["time.old"],
                        allowed_path_patterns=[r"/reports/.+"],
                        excluded_path_patterns=[r"/reports/archive/.+"],
                    )
                ],
            )
        ],
        configuration.id,
    )
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
    run_id = run_service.create_run(
        date(2026, 8, 1),
        date(2026, 8, 28),
        configuration_id=configuration.id,
    )
    source_run = db_session.scalar(
        select(run_service.SourceRun).where(run_service.SourceRun.run_id == run_id)
    )
    entry = source_run.entry_runs[0].source_entry
    entry.url = "https://snapshot.example/changed/"
    entry.adapter_key = "generic"
    entry.ordered_by_date = False
    entry.listing_selectors_json = '["article.new a"]'
    entry.container_selectors_json = '["article.new"]'
    entry.content_type_selectors_json = '[".new-format"]'
    entry.date_selectors_json = '["time.new"]'
    entry.allowed_path_patterns_json = '["/changed/.+"]'
    entry.excluded_path_patterns_json = "[]"
    db_session.commit()

    captured: dict[str, object] = {}

    class FakeCrawler:
        def __init__(self, _config, _limiter):
            pass

        def scan_listing(
            self,
            entry_url,
            _start_date,
            ordered_by_date,
            adapter_key,
            _end_date,
            **kwargs,
        ):
            captured.update(
                entry_url=entry_url,
                ordered_by_date=ordered_by_date,
                adapter_key=adapter_key,
                listing_selectors=kwargs["listing_selectors"],
                container_selectors=kwargs["container_selectors"],
                content_type_selectors=kwargs["content_type_selectors"],
                date_selectors=kwargs["date_selectors"],
                allowed_path_patterns=kwargs["allowed_path_patterns"],
                excluded_path_patterns=kwargs["excluded_path_patterns"],
            )
            return ScanResult(
                coverage_status=CoverageStatus.COMPLETE,
                stop_reason="snapshot verified",
            )

        def close(self):
            pass

    monkeypatch.setattr(run_service, "WebCrawler", FakeCrawler)
    monkeypatch.setattr(run_service, "load_config", lambda: AppConfig())
    run_service._execute_source(
        run_id,
        source_run.id,
        DomainRateLimiter(0),
    )

    assert captured == {
        "entry_url": "https://snapshot.example/reports/",
        "ordered_by_date": True,
        "adapter_key": "euiss",
        "listing_selectors": ("article.old a",),
        "container_selectors": ("article.old",),
        "content_type_selectors": (".old-format",),
        "date_selectors": ("time.old",),
        "allowed_path_patterns": (r"/reports/.+",),
        "excluded_path_patterns": (r"/reports/archive/.+",),
    }


def test_v6_source_unique_constraint_is_rebuilt_per_configuration(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'v6.sqlite3').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE sources ("
            "id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(300) NOT NULL, "
            "group_name VARCHAR(300) NOT NULL DEFAULT '', homepage_url TEXT NOT NULL, "
            "publication_url TEXT NOT NULL DEFAULT '', adapter_key VARCHAR(100) NOT NULL DEFAULT 'generic', "
            "ordered_by_date BOOLEAN NOT NULL DEFAULT 1, enabled BOOLEAN NOT NULL DEFAULT 1, "
            "last_successful_date DATE, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "CONSTRAINT uq_sources_homepage_url UNIQUE (homepage_url))"
        )
        connection.exec_driver_sql(
            "INSERT INTO sources (id,name,homepage_url,created_at,updated_at) "
            "VALUES (1,'One','https://example.org/',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql("PRAGMA user_version=6")
        migrate(connection)
        table_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'"
        ).scalar_one()
        assert "uq_sources_configuration_homepage" in table_sql
        first_id = connection.exec_driver_sql(
            "SELECT id FROM source_configurations ORDER BY id LIMIT 1"
        ).scalar_one()
        second_id = connection.exec_driver_sql(
            "INSERT INTO source_configurations (name,created_at,updated_at) "
            "VALUES ('配置2',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        ).lastrowid
        connection.exec_driver_sql(
            "INSERT INTO sources (configuration_id,name,homepage_url,created_at,updated_at) "
            "VALUES (?,'Two','https://example.org/',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (second_id,),
        )
        assert first_id != second_id
