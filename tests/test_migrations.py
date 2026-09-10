from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from thinktank_tracker.database import Base
import thinktank_tracker.database as database
from thinktank_tracker.config import AppConfig
import thinktank_tracker.migrations as migrations
from thinktank_tracker.migrations import CURRENT_SCHEMA_VERSION, migrate
from thinktank_tracker.models import (
    Assessment,
    Candidate,
    CandidateAttachment,
    CandidateDiscovery,
    CrawlRun,
    ModelSuggestion,
    Source,
    SourceConfiguration,
    SourceEntry,
    SourceEntryRun,
    SourceRun,
    ReviewDecision,
)
from thinktank_tracker.paths import AppPaths


def test_v1_migration_creates_entry_and_repairs_unreviewed_briefs(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'v1.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            name="EUISS",
            homepage_url="https://www.iss.europa.eu/",
            publication_url="https://www.iss.europa.eu/publications/",
            adapter_key="euiss",
        )
        run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 26))
        session.add_all([source, run])
        session.flush()
        candidate = Candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url="https://www.iss.europa.eu/publications/briefs/example",
            original_url="https://www.iss.europa.eu/publications/briefs/example/",
            content_type="policy brief",
        )
        session.add(candidate)
        session.flush()
        session.add_all(
            [
                Assessment(candidate_id=candidate.id, decision="exclude"),
                ModelSuggestion(
                    candidate_id=candidate.id,
                    decision="include",
                    content_type="commissioned reports",
                    raw_response='{"decision":"include"}',
                ),
            ]
        )
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM source_entries")
        connection.exec_driver_sql("PRAGMA user_version=1")
        migrate(connection)
    with Session(engine) as session:
        entry = session.scalar(select(SourceEntry))
        candidate = session.scalar(select(Candidate))
        suggestion = session.scalar(select(ModelSuggestion))
        assert entry.url == "https://www.iss.europa.eu/publications/"
        assert candidate.content_type == "policy brief"
        assert suggestion.decision == "exclude"
        assert suggestion.content_type == "policy brief"
        assert suggestion.raw_response == '{"decision":"include"}'
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == CURRENT_SCHEMA_VERSION


def test_v2_to_v3_adds_soft_delete_columns(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'v2.sqlite3').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE source_entries (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE crawl_runs (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("PRAGMA user_version=2")
        migrate(connection)
        entry_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(source_entries)")}
        run_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(crawl_runs)")}
        assert "deleted_at" in entry_columns
        assert "deleted_at" in run_columns
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == CURRENT_SCHEMA_VERSION


def test_v5_to_v6_reclassifies_urls_and_preserves_manual_custom_type(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(migrations, "load_config", lambda: AppConfig())
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'v5.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            name="Spanish institute",
            homepage_url="https://example.org/",
            publication_url="https://example.org/analisis/",
        )
        run = CrawlRun(start_date=date(2026, 8, 1), end_date=date(2026, 8, 28))
        session.add_all([source, run])
        session.flush()
        spanish = Candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url="https://example.org/analisis/one",
            original_url="https://example.org/analisis/one/",
            content_type="unknown",
        )
        english = Candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url="https://example.org/analysis/two",
            original_url="https://example.org/analysis/two/",
            content_type="analysis",
        )
        session.add_all([spanish, english])
        session.flush()
        session.add(
            ReviewDecision(
                candidate_id=english.id,
                decision="include",
                content_type="manual monograph",
            )
        )
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version=5")
        migrate(connection)
    with Session(engine) as session:
        candidates = session.scalars(select(Candidate).order_by(Candidate.id)).all()
        review = session.scalar(select(ReviewDecision))
        assert candidates[0].content_type == "analysis"
        assert candidates[1].content_type == "analysis"
        assert review.content_type == "manual monograph"


def _temporary_paths(root: Path) -> AppPaths:
    workspace = root / "workspace"
    return AppPaths(
        root=root,
        workspace=workspace,
        config=workspace / "config",
        cache=workspace / "cache",
        downloads=workspace / "cache" / "downloads",
        exports=workspace / "exports",
        backups=workspace / "backups",
        logs=workspace / "logs",
        database=workspace / "tracker.sqlite3",
    )


def test_initialize_database_backs_up_v6_before_v11_upgrade(tmp_path, monkeypatch) -> None:
    paths = _temporary_paths(tmp_path)
    paths.ensure()
    engine = create_engine(f"sqlite+pysqlite:///{paths.database.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Source(
                name="Pre-upgrade source",
                homepage_url="https://backup.example/",
                publication_url="https://backup.example/reports/",
            )
        )
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version=6")

    monkeypatch.setattr(database, "PATHS", paths)
    monkeypatch.setattr(database, "engine", engine)
    database.initialize_database()

    backups = list(paths.backups.glob("tracker_pre_v11_*.sqlite3"))
    assert len(backups) == 1
    backup_engine = create_engine(f"sqlite+pysqlite:///{backups[0].as_posix()}")
    with backup_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 6
        assert connection.exec_driver_sql("SELECT name FROM sources").scalar_one() == "Pre-upgrade source"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == CURRENT_SCHEMA_VERSION


def test_v7_to_v8_backfills_legacy_attachment_audit_without_reparsing(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'attachments-v7.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(name="Legacy source", homepage_url="https://legacy.example/")
        run = CrawlRun(start_date=date(2026, 8, 1), end_date=date(2026, 8, 28))
        session.add_all([source, run])
        session.flush()
        candidate = Candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url="https://legacy.example/report",
            original_url="https://legacy.example/report",
            title_original="Legacy report",
        )
        session.add(candidate)
        session.flush()
        session.add(
            CandidateAttachment(
                candidate_id=candidate.id,
                url="https://legacy.example/report.pdf",
                canonical_url="https://legacy.example/report.pdf",
                extraction_status="complete",
            )
        )
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version=7")
        migrate(connection)
    with Session(engine) as session:
        attachment = session.scalar(select(CandidateAttachment))
        audit = json.loads(attachment.extraction_audit_json)
        assert audit["final_engine"] == "pypdf_legacy"
        assert audit["final_source"] == "pypdf_legacy"
        assert "未重新解析" in audit["fallback_detail"]
        assert attachment.extraction_status == "complete"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == CURRENT_SCHEMA_VERSION


def test_v6_upgrade_preserves_history_and_backfills_task_snapshots(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'history-v6.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            name="Historical source",
            homepage_url="https://history.example/",
            publication_url="https://history.example/reports/",
        )
        run = CrawlRun(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 20),
            status="completed",
        )
        session.add_all([source, run])
        session.flush()
        entry = SourceEntry(
            source_id=source.id,
            url="https://history.example/reports/",
            canonical_url="https://history.example/reports",
            adapter_key="generic",
            listing_selectors_json='["article a"]',
        )
        source_run = SourceRun(run_id=run.id, source_id=source.id, status="complete")
        session.add_all([entry, source_run])
        session.flush()
        session.add(
            SourceEntryRun(
                run_id=run.id,
                source_run_id=source_run.id,
                source_entry_id=entry.id,
                status="complete",
            )
        )
        candidate = Candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url="https://history.example/reports/item",
            original_url="https://history.example/reports/item/",
            title_original="Historical candidate",
            status="approved",
        )
        session.add(candidate)
        session.flush()
        session.add(
            ReviewDecision(
                candidate_id=candidate.id,
                decision="include",
                content_type="reports",
            )
        )
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE sources SET configuration_id=NULL")
        connection.exec_driver_sql("UPDATE crawl_runs SET configuration_id=NULL")
        connection.exec_driver_sql(
            "UPDATE source_runs SET source_name_snapshot='', source_homepage_snapshot=''"
        )
        connection.exec_driver_sql(
            "UPDATE source_entry_runs SET entry_url_snapshot='', entry_config_snapshot_json='{}'"
        )
        connection.exec_driver_sql("PRAGMA user_version=6")
        migrate(connection)

    with Session(engine) as session:
        configuration = session.scalar(select(SourceConfiguration))
        source = session.scalar(select(Source))
        run = session.scalar(select(CrawlRun))
        source_run = session.scalar(select(SourceRun))
        entry_run = session.scalar(select(SourceEntryRun))
        candidate = session.scalar(select(Candidate))
        review = session.scalar(select(ReviewDecision))
        assert configuration.name == "默认配置"
        assert source.configuration_id == configuration.id
        assert run.configuration_id == configuration.id
        assert run.configuration_name_snapshot == "升级前默认配置"
        assert source_run.source_name_snapshot == "Historical source"
        assert source_run.source_homepage_snapshot == "https://history.example/"
        assert entry_run.entry_url_snapshot == "https://history.example/reports/"
        assert candidate.title_original == "Historical candidate"
        assert review.decision == "include"
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == CURRENT_SCHEMA_VERSION


def test_v8_to_v10_upgrades_only_unmodified_ecfr_entries_and_preserves_history(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'ecfr-v8.sqlite3').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE sources (id INTEGER PRIMARY KEY, adapter_key VARCHAR(100) NOT NULL DEFAULT 'generic')"
        )
        connection.exec_driver_sql(
            "CREATE TABLE source_entries ("
            "id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, url TEXT NOT NULL, "
            "adapter_key VARCHAR(100) NOT NULL DEFAULT 'generic', "
            "listing_selectors_json TEXT NOT NULL DEFAULT '[]', "
            "date_selectors_json TEXT NOT NULL DEFAULT '[]', "
            "allowed_path_patterns_json TEXT NOT NULL DEFAULT '[]', "
            "excluded_path_patterns_json TEXT NOT NULL DEFAULT '[]')"
        )
        connection.exec_driver_sql(
            "CREATE TABLE candidates (id INTEGER PRIMARY KEY, title_original TEXT NOT NULL DEFAULT '')"
        )
        connection.exec_driver_sql(
            "CREATE TABLE candidate_discoveries (id INTEGER PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE review_decisions (id INTEGER PRIMARY KEY)"
        )
        connection.exec_driver_sql("INSERT INTO sources (id,adapter_key) VALUES (1,'generic'),(2,'generic')")
        connection.exec_driver_sql(
            "INSERT INTO source_entries "
            "(id,source_id,url,adapter_key,listing_selectors_json,date_selectors_json,"
            "allowed_path_patterns_json,excluded_path_patterns_json) VALUES "
            "(1,1,'https://ecfr.eu/analysis/','generic','[]','[]','[]','[]'),"
            "(2,2,'https://ecfr.eu/analysis/','generic','[\".custom a\"]','[]','[]','[]')"
        )
        connection.exec_driver_sql(
            "INSERT INTO candidates (id,title_original) VALUES (1,'Historical ECFR article')"
        )
        connection.exec_driver_sql("PRAGMA user_version=8")
        migrate(connection)

        entries = connection.exec_driver_sql(
            "SELECT id,adapter_key,container_selectors_json,content_type_selectors_json "
            "FROM source_entries ORDER BY id"
        ).fetchall()
        sources = connection.exec_driver_sql(
            "SELECT id,adapter_key FROM sources ORDER BY id"
        ).fetchall()
        assert entries == [
            (1, "ecfr", "[]", "[]"),
            (2, "generic", "[]", "[]"),
        ]
        assert sources == [(1, "ecfr"), (2, "generic")]
        assert connection.exec_driver_sql(
            "SELECT title_original FROM candidates WHERE id=1"
        ).scalar_one() == "Historical ECFR article"
        assert "authors_json" in {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(candidates)")
        }
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == CURRENT_SCHEMA_VERSION


def test_v9_to_v10_restores_to_the_point_from_saved_listing_evidence(
    tmp_path, monkeypatch
) -> None:
    rules = [
        {
            "match_kind": "card_label",
            "match_value": "To the Point",
            "content_type": "to the point",
            "action": "allow",
        }
    ]
    monkeypatch.setattr(migrations, "load_config", lambda: AppConfig(content_type_rules=rules))
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'to-the-point-v9.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            name="EPC",
            homepage_url="https://www.epc.eu/",
            publication_url="https://www.epc.eu/publications/",
        )
        run = CrawlRun(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
        session.add_all([source, run])
        session.flush()
        entry = SourceEntry(
            source_id=source.id,
            url="https://www.epc.eu/publications/",
            canonical_url="https://www.epc.eu/publications",
        )
        session.add(entry)
        session.flush()
        candidate = Candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url="https://www.epc.eu/publication/example",
            original_url="https://www.epc.eu/publication/example/",
            content_type="unknown",
            content_type_evidence="URL 路径未命中已配置的体裁规则（设置页按确定性体裁规则重算）",
        )
        protected = Candidate(
            run_id=run.id,
            source_id=source.id,
            canonical_url="https://www.epc.eu/publication/protected",
            original_url="https://www.epc.eu/publication/protected/",
            content_type="manual final",
            status="approved",
        )
        session.add_all([candidate, protected])
        session.flush()
        candidate_id = candidate.id
        protected_id = protected.id
        session.add_all(
            [
                CandidateDiscovery(
                    candidate_id=candidate.id,
                    source_entry_id=entry.id,
                    listing_url=entry.url,
                    fetch_url=candidate.original_url,
                    listing_content_type="",
                    listing_content_type_evidence="栏目卡片体裁标签未映射：To the Point",
                ),
                CandidateDiscovery(
                    candidate_id=protected.id,
                    source_entry_id=entry.id,
                    listing_url=entry.url,
                    fetch_url=protected.original_url,
                    listing_content_type="",
                    listing_content_type_evidence="栏目卡片体裁标签未映射：To the Point",
                ),
                ReviewDecision(
                    candidate_id=protected.id,
                    decision="include",
                    content_type="manual final",
                ),
            ]
        )
        session.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA user_version=9")
        migrate(connection)
    with Session(engine) as session:
        restored = session.get(Candidate, candidate_id)
        unchanged = session.get(Candidate, protected_id)
        assert restored.content_type == "to the point"
        assert json.loads(restored.content_type_raw_labels_json) == ["To the Point"]
        assert unchanged.content_type == "manual final"
