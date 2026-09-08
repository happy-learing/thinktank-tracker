from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any


TABLES = (
    "sources",
    "source_entries",
    "candidates",
    "candidate_discoveries",
    "review_decisions",
)
CUSTOM_ENTRY_COLUMNS = (
    "listing_selectors_json",
    "container_selectors_json",
    "content_type_selectors_json",
    "date_selectors_json",
    "allowed_path_patterns_json",
    "excluded_path_patterns_json",
)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    names = [str(item[0]) for item in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    candidate_columns = _columns(connection, "candidates")
    discovery_columns = _columns(connection, "candidate_discoveries")
    raw_labels = (
        "content_type_raw_labels_json"
        if "content_type_raw_labels_json" in candidate_columns
        else "'[]'"
    )
    listing_labels = (
        "listing_content_type_labels_json"
        if "listing_content_type_labels_json" in discovery_columns
        else "'[]'"
    )
    return {
        "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "counts": {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in TABLES
        },
        "ecfr_entries": _rows(
            connection,
            "SELECT id,url,adapter_key,"
            + ",".join(CUSTOM_ENTRY_COLUMNS)
            + " FROM source_entries WHERE lower(url) LIKE '%ecfr.eu/%' ORDER BY id",
        ),
        "to_the_point_candidates": _rows(
            connection,
            "SELECT c.id,c.content_type,c.content_type_evidence,c.status,"
            f"{raw_labels} AS raw_labels_json,"
            "EXISTS(SELECT 1 FROM review_decisions r WHERE r.candidate_id=c.id) AS reviewed "
            "FROM candidates c WHERE lower(c.content_type_evidence) LIKE '%to the point%' "
            "OR lower(c.content_type) LIKE '%to the point%' "
            f"OR lower({raw_labels}) LIKE '%to the point%' ORDER BY c.id",
        ),
        "to_the_point_discoveries": _rows(
            connection,
            "SELECT id,candidate_id,listing_content_type,listing_content_type_evidence,"
            f"{listing_labels} AS raw_labels_json FROM candidate_discoveries "
            "WHERE lower(listing_content_type_evidence) LIKE '%to the point%' "
            "OR lower(listing_content_type) LIKE '%to the point%' "
            f"OR lower({listing_labels}) LIKE '%to the point%' ORDER BY id",
        ),
    }


def _is_unconfigured(entry: dict[str, Any]) -> bool:
    return all(str(entry.get(column) or "[]").strip() == "[]" for column in CUSTOM_ENTRY_COLUMNS)


def verify(source_database: Path, target_workspace: Path) -> Path:
    source_database = source_database.resolve()
    target_workspace = target_workspace.resolve()
    target_database = target_workspace / "tracker.sqlite3"
    if target_database.exists():
        raise FileExistsError(f"Refusing to overwrite migration database: {target_database}")

    (target_workspace / "config").mkdir(parents=True, exist_ok=True)
    source_config = source_database.parent / "config" / "app.json"
    if source_config.exists():
        shutil.copy2(source_config, target_workspace / "config" / "app.json")

    source = sqlite3.connect(f"{source_database.as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(target_database)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    before_db = sqlite3.connect(target_database)
    try:
        before = _snapshot(before_db)
    finally:
        before_db.close()

    os.environ["THINKTANK_WORKSPACE"] = str(target_workspace)
    from thinktank_tracker.database import initialize_database

    initialize_database()

    after_db = sqlite3.connect(target_database)
    try:
        after = _snapshot(after_db)
        foreign_key_violations = [list(row) for row in after_db.execute("PRAGMA foreign_key_check")]
    finally:
        after_db.close()

    if before["counts"] != after["counts"]:
        raise AssertionError(f"Table counts changed: {before['counts']} -> {after['counts']}")
    if after["user_version"] != 10:
        raise AssertionError(f"Expected schema 10, got {after['user_version']}")
    if foreign_key_violations:
        raise AssertionError(f"Foreign-key violations: {foreign_key_violations[:5]}")

    qualifying_ecfr_ids = {
        int(entry["id"])
        for entry in before["ecfr_entries"]
        if entry["adapter_key"] == "generic" and _is_unconfigured(entry)
    }
    upgraded_ecfr_ids = {
        int(entry["id"])
        for entry in after["ecfr_entries"]
        if entry["adapter_key"] == "ecfr"
    }
    if not qualifying_ecfr_ids.issubset(upgraded_ecfr_ids):
        raise AssertionError(
            f"ECFR entries were not upgraded: {sorted(qualifying_ecfr_ids - upgraded_ecfr_ids)}"
        )

    protected_before = {
        int(item["id"]): (item["content_type"], item["content_type_evidence"])
        for item in before["to_the_point_candidates"]
        if item["reviewed"] or item["status"] == "approved"
    }
    protected_after = {
        int(item["id"]): (item["content_type"], item["content_type_evidence"])
        for item in after["to_the_point_candidates"]
        if int(item["id"]) in protected_before
    }
    if protected_before != protected_after:
        raise AssertionError("Reviewed or approved To the Point candidates changed during migration")

    report = {
        "source_database": str(source_database),
        "target_database": str(target_database),
        "before": before,
        "after": after,
        "qualifying_ecfr_ids": sorted(qualifying_ecfr_ids),
        "upgraded_ecfr_ids": sorted(upgraded_ecfr_ids),
        "foreign_key_violations": foreign_key_violations,
        "migration_backups": sorted(
            path.name for path in (target_workspace / "backups").glob("tracker_pre_v10_*.sqlite3")
        ),
    }
    report_path = target_workspace / "migration-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_database", type=Path)
    parser.add_argument("target_workspace", type=Path)
    args = parser.parse_args()
    print(verify(args.source_database, args.target_workspace))


if __name__ == "__main__":
    main()
