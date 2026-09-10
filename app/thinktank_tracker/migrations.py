from __future__ import annotations

import json
import re

from sqlalchemy import Connection

from .config import load_config
from .url_utils import URLContentTypeRules
from .url_utils import (
    canonicalize_url,
    collect_content_type_evidence,
    content_type_from_url,
    normalize_fetch_url,
    resolve_content_type,
)


CURRENT_SCHEMA_VERSION = 11


def _legacy_content_type_labels(evidence: str) -> list[str]:
    text = str(evidence or "")
    labels: list[str] = []
    labels.extend(
        match.strip()
        for match in re.findall(r"(?:标签|label)\s*['‘’\"]([^'‘’\"]+)['‘’\"]", text, re.I)
        if match.strip()
    )
    for marker in ("体裁标签未映射：", "体裁标签未映射:", "label unmapped:"):
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1]
        tail = re.split(r"（数据库|\(database|；URL |; URL ", tail, maxsplit=1, flags=re.I)[0]
        labels.extend(
            value.strip()
            for value in re.split(r"[；;|]", tail)
            if value.strip()
        )
    return list(dict.fromkeys(labels))


def _table_exists(connection: Connection, table: str) -> bool:
    return bool(
        connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).first()
    )


def _columns(connection: Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        row[1]
        for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    }


def _add_columns(connection: Connection, table: str, additions: dict[str, str]) -> None:
    if not _table_exists(connection, table):
        return
    existing = _columns(connection, table)
    for name, definition in additions.items():
        if name not in existing:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _has_global_source_homepage_constraint(connection: Connection) -> bool:
    if not _table_exists(connection, "sources"):
        return False
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'"
    ).scalar_one_or_none()
    return bool(sql and "uq_sources_homepage_url" in sql)


def _rebuild_sources_for_configurations(connection: Connection) -> None:
    if not _has_global_source_homepage_constraint(connection):
        return
    foreign_keys = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if foreign_keys:
        raise RuntimeError("数据库v7迁移需要在关闭外键检查的迁移连接中执行")
    connection.exec_driver_sql(
        "CREATE TABLE sources_v7 ("
        "id INTEGER NOT NULL PRIMARY KEY, "
        "configuration_id INTEGER REFERENCES source_configurations(id) ON DELETE RESTRICT, "
        "name VARCHAR(300) NOT NULL, group_name VARCHAR(300) NOT NULL DEFAULT '', "
        "homepage_url TEXT NOT NULL, publication_url TEXT NOT NULL DEFAULT '', "
        "adapter_key VARCHAR(100) NOT NULL DEFAULT 'generic', "
        "ordered_by_date BOOLEAN NOT NULL DEFAULT 1, enabled BOOLEAN NOT NULL DEFAULT 1, "
        "deleted_at DATETIME, last_successful_date DATE, created_at DATETIME NOT NULL, "
        "updated_at DATETIME NOT NULL, "
        "CONSTRAINT uq_sources_configuration_homepage UNIQUE (configuration_id, homepage_url)"
        ")"
    )
    connection.exec_driver_sql(
        "INSERT INTO sources_v7 (id, configuration_id, name, group_name, homepage_url, "
        "publication_url, adapter_key, ordered_by_date, enabled, deleted_at, last_successful_date, "
        "created_at, updated_at) SELECT id, configuration_id, name, group_name, homepage_url, "
        "publication_url, adapter_key, ordered_by_date, enabled, deleted_at, last_successful_date, "
        "created_at, updated_at FROM sources"
    )
    connection.exec_driver_sql("DROP TABLE sources")
    connection.exec_driver_sql("ALTER TABLE sources_v7 RENAME TO sources")


def reclassify_url_content_types(
    connection: Connection,
    rules: URLContentTypeRules,
    evidence_suffix: str,
) -> int:
    candidate_count = 0
    if _table_exists(connection, "candidates"):
        rows = connection.exec_driver_sql(
            "SELECT id, original_url, canonical_url FROM candidates"
        ).fetchall()
        candidate_count = len(rows)
        for candidate_id, original_url, canonical_url in rows:
            content_type, evidence = content_type_from_url(
                original_url or canonical_url or "", rules
            )
            connection.exec_driver_sql(
                "UPDATE candidates SET content_type=?, content_type_evidence=? WHERE id=?",
                (content_type, f"{evidence}{evidence_suffix}", candidate_id),
            )
    if _table_exists(connection, "candidate_discoveries"):
        rows = connection.exec_driver_sql(
            "SELECT id, fetch_url FROM candidate_discoveries"
        ).fetchall()
        for discovery_id, fetch_url in rows:
            content_type, _ = content_type_from_url(fetch_url or "", rules)
            connection.exec_driver_sql(
                "UPDATE candidate_discoveries SET listing_content_type=? WHERE id=?",
                ("" if content_type == "unknown" else content_type, discovery_id),
            )
    if _table_exists(connection, "model_suggestions") and _table_exists(connection, "candidates"):
        rows = connection.exec_driver_sql(
            "SELECT model_suggestions.id, candidates.original_url, candidates.canonical_url "
            "FROM model_suggestions JOIN candidates "
            "ON candidates.id=model_suggestions.candidate_id"
        ).fetchall()
        for suggestion_id, original_url, canonical_url in rows:
            content_type, _ = content_type_from_url(
                original_url or canonical_url or "", rules
            )
            connection.exec_driver_sql(
                "UPDATE model_suggestions SET content_type=? WHERE id=?",
                ("" if content_type == "unknown" else content_type, suggestion_id),
            )
    return candidate_count


def migrate(connection: Connection) -> None:
    current = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {current} 高于程序支持的版本 {CURRENT_SCHEMA_VERSION}，请使用更新版本程序"
        )
    # Version 1 is created idempotently by SQLAlchemy metadata before this call.
    if current < 1:
        connection.exec_driver_sql("PRAGMA user_version=1")
        current = 1
    if current < 2:
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(candidates)").fetchall()
        }
        if "fetch_audit_json" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE candidates ADD COLUMN fetch_audit_json TEXT NOT NULL DEFAULT '[]'"
            )

        entry_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(source_entries)").fetchall()
        }
        rows = connection.exec_driver_sql(
            "SELECT id, homepage_url, publication_url, adapter_key, ordered_by_date, enabled, "
            "last_successful_date, created_at, updated_at FROM sources"
        ).fetchall()
        for row in rows:
            entry_url = normalize_fetch_url(row[2] or row[1])
            names = [
                "source_id", "url", "canonical_url", "adapter_key", "ordered_by_date",
                "enabled", "last_successful_date", "created_at", "updated_at",
            ]
            values: list[object] = [
                row[0], entry_url, canonicalize_url(entry_url), row[3] or "generic",
                row[4], row[5], row[6], row[7], row[8],
            ]
            for optional in (
                "listing_selectors_json", "container_selectors_json",
                "content_type_selectors_json", "date_selectors_json",
                "allowed_path_patterns_json", "excluded_path_patterns_json",
            ):
                if optional in entry_columns:
                    names.append(optional)
                    values.append("[]")
            for optional, default in (
                ("last_test_status", "untested"),
                ("last_test_final_url", ""),
                ("last_test_discovered_count", 0),
                ("last_test_accepted_count", 0),
                ("last_test_message", ""),
            ):
                if optional in entry_columns:
                    names.append(optional)
                    values.append(default)
            placeholders = ", ".join("?" for _ in names)
            connection.exec_driver_sql(
                f"INSERT OR IGNORE INTO source_entries ({', '.join(names)}) VALUES ({placeholders})",
                tuple(values),
            )

        connection.exec_driver_sql(
            "UPDATE candidates SET content_type='briefs', "
            "content_type_evidence=\"URL 路径段 'briefs' -> briefs（数据库 v2 修复）\" "
            "WHERE lower(canonical_url) LIKE '%/briefs/%' AND content_type='policy brief' "
            "AND NOT EXISTS (SELECT 1 FROM review_decisions "
            "WHERE review_decisions.candidate_id=candidates.id)"
        )
        connection.exec_driver_sql("PRAGMA user_version=2")
        current = 2
    if current < 3:
        entry_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(source_entries)").fetchall()
        }
        if "deleted_at" not in entry_columns:
            connection.exec_driver_sql("ALTER TABLE source_entries ADD COLUMN deleted_at DATETIME")

        run_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(crawl_runs)").fetchall()
        }
        if "deleted_at" not in run_columns:
            connection.exec_driver_sql("ALTER TABLE crawl_runs ADD COLUMN deleted_at DATETIME")
        connection.exec_driver_sql("PRAGMA user_version=3")
        current = 3
    if current < 4:
        additions = {
            "source_entries": {
                "listing_selectors_json": "TEXT NOT NULL DEFAULT '[]'",
                "date_selectors_json": "TEXT NOT NULL DEFAULT '[]'",
                "allowed_path_patterns_json": "TEXT NOT NULL DEFAULT '[]'",
                "excluded_path_patterns_json": "TEXT NOT NULL DEFAULT '[]'",
            },
            "source_entry_runs": {
                "current_page_url": "TEXT NOT NULL DEFAULT ''",
                "last_completed_page_url": "TEXT NOT NULL DEFAULT ''",
                "current_page_number": "INTEGER NOT NULL DEFAULT 0",
                "boundary_page_number": "INTEGER",
                "ordering_status": "VARCHAR(40) NOT NULL DEFAULT 'unchecked'",
            },
            "candidates": {
                "date_scope": "VARCHAR(40) NOT NULL DEFAULT 'date_unknown'",
                "content_kind_status": "VARCHAR(40) NOT NULL DEFAULT 'needs_review'",
                "length_status": "VARCHAR(40) NOT NULL DEFAULT 'unknown'",
                "topic_status": "VARCHAR(40) NOT NULL DEFAULT 'pending'",
                "review_status": "VARCHAR(40) NOT NULL DEFAULT 'pending'",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "candidate_discoveries": {
                "listing_date": "DATE",
                "listing_date_evidence": "TEXT NOT NULL DEFAULT ''",
                "listing_content_type": "VARCHAR(100) NOT NULL DEFAULT ''",
                "card_position": "INTEGER NOT NULL DEFAULT 0",
                "discovery_decision": "VARCHAR(40) NOT NULL DEFAULT 'accepted'",
            },
            "assessments": {
                "strategic_nexus": "TEXT NOT NULL DEFAULT ''",
                "exclusion_code": "VARCHAR(100) NOT NULL DEFAULT ''",
            },
        }
        for table, columns in additions.items():
            if not _table_exists(connection, table):
                continue
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )
        if _table_exists(connection, "candidates") and _table_exists(connection, "crawl_runs"):
            connection.exec_driver_sql(
                "UPDATE candidates SET date_scope=CASE "
                "WHEN published_date IS NULL OR date_status!='confirmed' THEN 'date_unknown' "
                "WHEN published_date < (SELECT start_date FROM crawl_runs WHERE id=candidates.run_id) "
                "THEN 'before_start' "
                "WHEN published_date > (SELECT end_date FROM crawl_runs WHERE id=candidates.run_id) "
                "THEN 'after_end' ELSE 'in_window' END"
            )
            connection.exec_driver_sql(
                "UPDATE candidates SET review_status=CASE status "
                "WHEN 'approved' THEN 'approved' WHEN 'rejected' THEN 'rejected' ELSE 'pending' END"
            )
        connection.exec_driver_sql("PRAGMA user_version=4")
        current = 4
    if current < 5:
        if _table_exists(connection, "candidates"):
            rows = connection.exec_driver_sql(
                "SELECT id, original_url, canonical_url FROM candidates"
            ).fetchall()
            for candidate_id, original_url, canonical_url in rows:
                content_type, evidence = content_type_from_url(original_url or canonical_url or "")
                connection.exec_driver_sql(
                    "UPDATE candidates SET content_type=?, content_type_evidence=? WHERE id=?",
                    (content_type, f"{evidence}（数据库 v5 按 URL 重算）", candidate_id),
                )
        if _table_exists(connection, "candidate_discoveries"):
            rows = connection.exec_driver_sql(
                "SELECT id, fetch_url FROM candidate_discoveries"
            ).fetchall()
            for discovery_id, fetch_url in rows:
                content_type, _ = content_type_from_url(fetch_url or "")
                connection.exec_driver_sql(
                    "UPDATE candidate_discoveries SET listing_content_type=? WHERE id=?",
                    ("" if content_type == "unknown" else content_type, discovery_id),
                )
        if (
            _table_exists(connection, "model_suggestions")
            and _table_exists(connection, "candidates")
        ):
            has_assessments = _table_exists(connection, "assessments")
            rows = connection.exec_driver_sql(
                "SELECT model_suggestions.id, candidates.original_url, candidates.canonical_url, "
                + (
                    "assessments.decision FROM model_suggestions "
                    "JOIN candidates ON candidates.id=model_suggestions.candidate_id "
                    "LEFT JOIN assessments ON assessments.candidate_id=candidates.id"
                    if has_assessments
                    else "NULL FROM model_suggestions "
                    "JOIN candidates ON candidates.id=model_suggestions.candidate_id"
                )
            ).fetchall()
            for suggestion_id, original_url, canonical_url, assessment_decision in rows:
                content_type, _ = content_type_from_url(original_url or canonical_url or "")
                decision = (
                    assessment_decision
                    if assessment_decision in {"include", "exclude"}
                    else "needs_review"
                )
                connection.exec_driver_sql(
                    "UPDATE model_suggestions SET decision=?, content_type=? WHERE id=?",
                    (
                        decision,
                        "" if content_type == "unknown" else content_type,
                        suggestion_id,
                    ),
                )
        connection.exec_driver_sql("PRAGMA user_version=5")
        current = 5
    if current < 6:
        reclassify_url_content_types(
            connection,
            load_config().url_content_type_rules,
            "（数据库 v6 按当前 URL 规则重算）",
        )
        connection.exec_driver_sql("PRAGMA user_version=6")
        current = 6
    if current < 7:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS source_configurations ("
            "id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(200) NOT NULL, "
            "last_used_at DATETIME, deleted_at DATETIME, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        default_id = connection.exec_driver_sql(
            "SELECT id FROM source_configurations WHERE deleted_at IS NULL ORDER BY id LIMIT 1"
        ).scalar_one_or_none()
        if default_id is None:
            result = connection.exec_driver_sql(
                "INSERT INTO source_configurations (name, created_at, updated_at) "
                "VALUES ('默认配置', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            default_id = int(result.lastrowid)

        _add_columns(
            connection,
            "sources",
            {
                "configuration_id": "INTEGER REFERENCES source_configurations(id) ON DELETE RESTRICT",
                "deleted_at": "DATETIME",
            },
        )
        if _table_exists(connection, "sources"):
            connection.exec_driver_sql(
                "UPDATE sources SET configuration_id=? WHERE configuration_id IS NULL",
                (default_id,),
            )
        _rebuild_sources_for_configurations(connection)

        _add_columns(
            connection,
            "source_entries",
            {
                "last_tested_at": "DATETIME",
                "last_test_status": "VARCHAR(40) NOT NULL DEFAULT 'untested'",
                "last_test_final_url": "TEXT NOT NULL DEFAULT ''",
                "last_test_http_status": "INTEGER",
                "last_test_discovered_count": "INTEGER NOT NULL DEFAULT 0",
                "last_test_accepted_count": "INTEGER NOT NULL DEFAULT 0",
                "last_test_message": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_columns(
            connection,
            "crawl_runs",
            {
                "configuration_id": "INTEGER REFERENCES source_configurations(id) ON DELETE RESTRICT",
                "configuration_name_snapshot": (
                    "VARCHAR(200) NOT NULL DEFAULT '升级前默认配置'"
                ),
            },
        )
        if _table_exists(connection, "crawl_runs"):
            connection.exec_driver_sql(
                "UPDATE crawl_runs SET configuration_id=? WHERE configuration_id IS NULL",
                (default_id,),
            )
        _add_columns(
            connection,
            "source_runs",
            {
                "source_name_snapshot": "VARCHAR(300) NOT NULL DEFAULT ''",
                "source_homepage_snapshot": "TEXT NOT NULL DEFAULT ''",
            },
        )
        if _table_exists(connection, "source_runs") and _table_exists(connection, "sources"):
            connection.exec_driver_sql(
                "UPDATE source_runs SET "
                "source_name_snapshot=COALESCE(NULLIF(source_name_snapshot, ''), "
                "(SELECT name FROM sources WHERE sources.id=source_runs.source_id), ''), "
                "source_homepage_snapshot=COALESCE(NULLIF(source_homepage_snapshot, ''), "
                "(SELECT homepage_url FROM sources WHERE sources.id=source_runs.source_id), '')"
            )
        _add_columns(
            connection,
            "source_entry_runs",
            {
                "entry_url_snapshot": "TEXT NOT NULL DEFAULT ''",
                "entry_config_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            },
        )
        if _table_exists(connection, "source_entry_runs") and _table_exists(connection, "source_entries"):
            rows = connection.exec_driver_sql(
                "SELECT source_entry_runs.id, source_entries.url, source_entries.canonical_url, "
                "source_entries.adapter_key, source_entries.ordered_by_date, "
                "source_entries.listing_selectors_json, source_entries.date_selectors_json, "
                "source_entries.allowed_path_patterns_json, source_entries.excluded_path_patterns_json "
                "FROM source_entry_runs JOIN source_entries "
                "ON source_entries.id=source_entry_runs.source_entry_id"
            ).fetchall()
            for row in rows:
                snapshot = {
                    "canonical_url": row[2] or "",
                    "adapter_key": row[3] or "generic",
                    "ordered_by_date": bool(row[4]),
                    "listing_selectors": json.loads(row[5] or "[]"),
                    "date_selectors": json.loads(row[6] or "[]"),
                    "allowed_path_patterns": json.loads(row[7] or "[]"),
                    "excluded_path_patterns": json.loads(row[8] or "[]"),
                }
                connection.exec_driver_sql(
                    "UPDATE source_entry_runs SET entry_url_snapshot=?, "
                    "entry_config_snapshot_json=? WHERE id=?",
                    (row[1] or "", json.dumps(snapshot, ensure_ascii=False), row[0]),
                )
        connection.exec_driver_sql("PRAGMA user_version=7")
        current = 7
    if current < 8:
        _add_columns(
            connection,
            "candidate_attachments",
            {"extraction_audit_json": "TEXT NOT NULL DEFAULT '{}'"},
        )
        if _table_exists(connection, "candidate_attachments"):
            legacy_audit = {
                "mode": "legacy",
                "final_engine": "pypdf_legacy",
                "final_engine_version": "",
                "final_source": "pypdf_legacy",
                "duration_ms": None,
                "fallback_reason": "",
                "fallback_detail": "数据库 v8 前历史记录，未重新解析",
                "ocr_pages": [],
                "page_count": None,
                "attempts": [],
            }
            connection.exec_driver_sql(
                "UPDATE candidate_attachments SET extraction_audit_json=? "
                "WHERE extraction_audit_json IS NULL "
                "OR TRIM(extraction_audit_json) IN ('', '{}')",
                (json.dumps(legacy_audit, ensure_ascii=False),),
            )
        connection.exec_driver_sql("PRAGMA user_version=8")
        current = 8
    if current < 9:
        _add_columns(
            connection,
            "source_entries",
            {
                "container_selectors_json": "TEXT NOT NULL DEFAULT '[]'",
                "content_type_selectors_json": "TEXT NOT NULL DEFAULT '[]'",
            },
        )
        _add_columns(
            connection,
            "candidates",
            {
                "authors_json": "TEXT NOT NULL DEFAULT '[]'",
                "author_evidence": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_columns(
            connection,
            "candidate_discoveries",
            {
                "listing_content_type_evidence": "TEXT NOT NULL DEFAULT ''",
                "listing_authors_json": "TEXT NOT NULL DEFAULT '[]'",
                "listing_author_evidence": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_columns(
            connection,
            "review_decisions",
            {"authors_json": "TEXT NOT NULL DEFAULT '[]'"},
        )
        source_entry_columns = _columns(connection, "source_entries")
        ecfr_upgrade_columns = {
            "adapter_key",
            "url",
            "listing_selectors_json",
            "container_selectors_json",
            "content_type_selectors_json",
            "date_selectors_json",
            "allowed_path_patterns_json",
            "excluded_path_patterns_json",
        }
        if ecfr_upgrade_columns.issubset(source_entry_columns):
            connection.exec_driver_sql(
                "UPDATE source_entries SET adapter_key='ecfr' "
                "WHERE adapter_key='generic' "
                "AND (lower(url) LIKE 'https://ecfr.eu/%' "
                "OR lower(url) LIKE 'http://ecfr.eu/%' "
                "OR lower(url) LIKE 'https://www.ecfr.eu/%' "
                "OR lower(url) LIKE 'http://www.ecfr.eu/%') "
                "AND COALESCE(listing_selectors_json, '[]')='[]' "
                "AND COALESCE(container_selectors_json, '[]')='[]' "
                "AND COALESCE(content_type_selectors_json, '[]')='[]' "
                "AND COALESCE(date_selectors_json, '[]')='[]' "
                "AND COALESCE(allowed_path_patterns_json, '[]')='[]' "
                "AND COALESCE(excluded_path_patterns_json, '[]')='[]'"
            )
        source_columns = _columns(connection, "sources")
        if (
            {"id", "adapter_key"}.issubset(source_columns)
            and {"source_id", "adapter_key"}.issubset(source_entry_columns)
        ):
            connection.exec_driver_sql(
                "UPDATE sources SET adapter_key='ecfr' WHERE adapter_key='generic' "
                "AND EXISTS (SELECT 1 FROM source_entries "
                "WHERE source_entries.source_id=sources.id "
                "AND source_entries.adapter_key='ecfr')"
            )
        connection.exec_driver_sql("PRAGMA user_version=9")
        current = 9
    if current < 10:
        _add_columns(
            connection,
            "candidates",
            {
                "content_type_raw_labels_json": "TEXT NOT NULL DEFAULT '[]'",
                "content_type_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            },
        )
        _add_columns(
            connection,
            "candidate_discoveries",
            {
                "listing_content_type_labels_json": "TEXT NOT NULL DEFAULT '[]'",
                "listing_content_type_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            },
        )

        # v9 only upgraded databases that happened to contain ECFR at migration
        # time. Repeat the guarded conversion for untouched sources added later.
        source_entry_columns = _columns(connection, "source_entries")
        ecfr_upgrade_columns = {
            "adapter_key",
            "url",
            "listing_selectors_json",
            "container_selectors_json",
            "content_type_selectors_json",
            "date_selectors_json",
            "allowed_path_patterns_json",
            "excluded_path_patterns_json",
        }
        if ecfr_upgrade_columns.issubset(source_entry_columns):
            connection.exec_driver_sql(
                "UPDATE source_entries SET adapter_key='ecfr' "
                "WHERE adapter_key='generic' "
                "AND (lower(url) LIKE 'https://ecfr.eu/%' "
                "OR lower(url) LIKE 'http://ecfr.eu/%' "
                "OR lower(url) LIKE 'https://www.ecfr.eu/%' "
                "OR lower(url) LIKE 'http://www.ecfr.eu/%') "
                "AND TRIM(COALESCE(listing_selectors_json, '[]'))='[]' "
                "AND TRIM(COALESCE(container_selectors_json, '[]'))='[]' "
                "AND TRIM(COALESCE(content_type_selectors_json, '[]'))='[]' "
                "AND TRIM(COALESCE(date_selectors_json, '[]'))='[]' "
                "AND TRIM(COALESCE(allowed_path_patterns_json, '[]'))='[]' "
                "AND TRIM(COALESCE(excluded_path_patterns_json, '[]'))='[]'"
            )
        source_columns = _columns(connection, "sources")
        if (
            {"id", "adapter_key"}.issubset(source_columns)
            and {"source_id", "adapter_key"}.issubset(source_entry_columns)
        ):
            connection.exec_driver_sql(
                "UPDATE sources SET adapter_key='ecfr' WHERE adapter_key='generic' "
                "AND EXISTS (SELECT 1 FROM source_entries "
                "WHERE source_entries.source_id=sources.id "
                "AND source_entries.adapter_key='ecfr')"
            )

        config = load_config()
        discovery_columns = _columns(connection, "candidate_discoveries")
        discovery_required = {
            "id",
            "candidate_id",
            "fetch_url",
            "listing_content_type",
            "listing_content_type_evidence",
            "listing_content_type_labels_json",
            "listing_content_type_evidence_json",
        }
        labels_by_candidate: dict[int, list[str]] = {}
        reviewed_candidates: set[int] = set()
        if _table_exists(connection, "review_decisions") and {
            "candidate_id"
        }.issubset(_columns(connection, "review_decisions")):
            reviewed_candidates = {
                int(row[0])
                for row in connection.exec_driver_sql(
                    "SELECT DISTINCT candidate_id FROM review_decisions"
                ).fetchall()
            }
        if discovery_required.issubset(discovery_columns):
            discovery_rows = connection.exec_driver_sql(
                "SELECT id,candidate_id,fetch_url,listing_content_type,"
                "listing_content_type_evidence FROM candidate_discoveries"
            ).fetchall()
            for discovery_id, candidate_id, fetch_url, old_type, old_evidence in discovery_rows:
                labels = _legacy_content_type_labels(old_evidence)
                if labels:
                    labels_by_candidate.setdefault(int(candidate_id), []).extend(labels)
                evidence_items = collect_content_type_evidence(
                    fetch_url or "",
                    labels,
                    config.content_type_rules,
                    label_location="legacy_listing_evidence",
                )
                content_type, evidence, _ = resolve_content_type(
                    fetch_url or "", labels, config.content_type_rules
                )
                new_type = old_type or ""
                new_evidence = old_evidence or ""
                if (
                    int(candidate_id) not in reviewed_candidates
                    and labels
                    and content_type != "unknown"
                ):
                    new_type = content_type
                    new_evidence = evidence
                connection.exec_driver_sql(
                    "UPDATE candidate_discoveries SET listing_content_type=?, "
                    "listing_content_type_evidence=?, listing_content_type_labels_json=?, "
                    "listing_content_type_evidence_json=? WHERE id=?",
                    (
                        new_type,
                        new_evidence,
                        json.dumps(labels, ensure_ascii=False),
                        json.dumps([item.to_dict() for item in evidence_items], ensure_ascii=False),
                        discovery_id,
                    ),
                )

        candidate_columns = _columns(connection, "candidates")
        candidate_required = {
            "id",
            "original_url",
            "canonical_url",
            "content_type",
            "content_type_evidence",
            "content_type_raw_labels_json",
            "content_type_evidence_json",
        }
        if candidate_required.issubset(candidate_columns):
            rows = connection.exec_driver_sql(
                "SELECT id,original_url,canonical_url,content_type,content_type_evidence"
                + (",status" if "status" in candidate_columns else ",''")
                + " FROM candidates"
            ).fetchall()
            for candidate_id, original_url, canonical_url, old_type, old_evidence, status in rows:
                discovery_labels = labels_by_candidate.get(int(candidate_id), [])
                labels = (
                    list(discovery_labels)
                    if discovery_labels
                    else _legacy_content_type_labels(old_evidence)
                )
                labels = list(dict.fromkeys(labels))
                url = original_url or canonical_url or ""
                evidence_items = collect_content_type_evidence(
                    url,
                    labels,
                    config.content_type_rules,
                    label_location="legacy_candidate_evidence",
                )
                content_type, evidence, _ = resolve_content_type(
                    url, labels, config.content_type_rules
                )
                protected = (
                    int(candidate_id) in reviewed_candidates
                    or str(status or "") == "approved"
                )
                new_type = old_type or "unknown"
                new_evidence = old_evidence or ""
                if not protected and labels and content_type != "unknown":
                    new_type = content_type
                    new_evidence = evidence
                connection.exec_driver_sql(
                    "UPDATE candidates SET content_type=?, content_type_evidence=?, "
                    "content_type_raw_labels_json=?, content_type_evidence_json=? WHERE id=?",
                    (
                        new_type,
                        new_evidence,
                        json.dumps(labels, ensure_ascii=False),
                        json.dumps([item.to_dict() for item in evidence_items], ensure_ascii=False),
                        candidate_id,
                    ),
                )
        connection.exec_driver_sql("PRAGMA user_version=10")
        current = 10
    if current <= 8:
        _add_columns(connection, "sources", {"deleted_at": "DATETIME"})

    if current < 11:
        _add_columns(connection, "crawl_runs", {"selection_policy_json": "TEXT NOT NULL DEFAULT '{}'"})
        connection.exec_driver_sql("PRAGMA user_version=11")
