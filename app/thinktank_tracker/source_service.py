from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .configuration_service import ensure_default_configuration
from .models import Source, SourceConfiguration, SourceEntry, utcnow
from .schemas import ImportedConfiguration, ImportedSource
from .source_catalog import known_source_for
from .url_utils import canonicalize_url, normalize_fetch_url


SOURCE_CONFIG_SCHEMA_VERSION = 6
PROMPT_VERSION = "2026-09-01.v3"


def upsert_sources(
    session: Session,
    sources: list[ImportedSource],
    configuration_id: int | None = None,
) -> tuple[int, int]:
    if configuration_id is None:
        configuration_id = ensure_default_configuration(session).id
    created = 0
    updated = 0
    for item in sources:
        homepage = normalize_fetch_url(str(item.homepage_url))
        existing = session.scalar(
            select(Source).where(
                Source.configuration_id == configuration_id,
                Source.homepage_url == homepage,
            )
        )
        if existing is None:
            existing = next(
                (
                    row
                    for row in session.scalars(
                        select(Source)
                        .where(Source.configuration_id == configuration_id)
                        .order_by(Source.id)
                    ).all()
                    if canonicalize_url(row.homepage_url) == canonicalize_url(homepage)
                ),
                None,
            )
        known = known_source_for(homepage)
        if (
            existing is None
            and known is not None
            and canonicalize_url(homepage) == canonicalize_url(known.homepage_url)
        ):
            existing = next(
                (
                    row
                    for row in session.scalars(
                        select(Source)
                        .where(Source.configuration_id == configuration_id)
                        .order_by(Source.id)
                    ).all()
                    if (row_known := known_source_for(row.homepage_url)) is not None
                    and row_known.institution_key == known.institution_key
                ),
                None,
            )
        first_entry = item.entries[0]
        values = {
            "name": item.name,
            "group_name": item.group_name,
            "publication_url": normalize_fetch_url(str(first_entry.url)),
            "adapter_key": first_entry.adapter_key,
            "ordered_by_date": first_entry.ordered_by_date,
            "enabled": item.enabled,
            "deleted_at": None,
            "updated_at": utcnow(),
        }
        if existing is None:
            session.add(
                Source(
                    configuration_id=configuration_id,
                    homepage_url=homepage,
                    **values,
                )
            )
            created += 1
        else:
            existing.homepage_url = homepage
            for key, value in values.items():
                setattr(existing, key, value)
            updated += 1
        session.flush()
        source = existing or session.scalar(
            select(Source).where(
                Source.configuration_id == configuration_id,
                Source.homepage_url == homepage,
            )
        )
        assert source is not None
        for entry_item in item.entries:
            entry_url = normalize_fetch_url(str(entry_item.url))
            canonical = canonicalize_url(entry_url)
            entry = session.scalar(
                select(SourceEntry).where(
                    SourceEntry.source_id == source.id,
                    SourceEntry.canonical_url == canonical,
                )
            )
            entry_values = {
                "url": entry_url,
                "adapter_key": entry_item.adapter_key,
                "ordered_by_date": entry_item.ordered_by_date,
                "enabled": entry_item.enabled,
                "deleted_at": None,
                "updated_at": utcnow(),
                "listing_selectors_json": json.dumps(entry_item.listing_selectors),
                "container_selectors_json": json.dumps(entry_item.container_selectors),
                "content_type_selectors_json": json.dumps(entry_item.content_type_selectors),
                "date_selectors_json": json.dumps(entry_item.date_selectors),
                "allowed_path_patterns_json": json.dumps(entry_item.allowed_path_patterns),
                "excluded_path_patterns_json": json.dumps(entry_item.excluded_path_patterns),
            }
            if entry is None:
                session.add(
                    SourceEntry(
                        source_id=source.id,
                        canonical_url=canonical,
                        **entry_values,
                    )
                )
            else:
                reset_test_result = any(
                    getattr(entry, key) != entry_values[key]
                    for key in (
                        "url",
                        "adapter_key",
                        "listing_selectors_json",
                        "container_selectors_json",
                        "content_type_selectors_json",
                        "date_selectors_json",
                        "allowed_path_patterns_json",
                        "excluded_path_patterns_json",
                    )
                )
                for key, value in entry_values.items():
                    setattr(entry, key, value)
                if reset_test_result:
                    entry.last_tested_at = None
                    entry.last_test_status = "untested"
                    entry.last_test_final_url = ""
                    entry.last_test_http_status = None
                    entry.last_test_discovered_count = 0
                    entry.last_test_accepted_count = 0
                    entry.last_test_message = ""
    session.flush()
    return created, updated


def serialize_sources(
    session: Session, configuration_id: int | None = None
) -> list[dict[str, object]]:
    query = select(Source).where(Source.deleted_at.is_(None)).order_by(Source.id)
    if configuration_id is not None:
        query = query.where(Source.configuration_id == configuration_id)
    rows = session.scalars(query).all()
    return [
        {
            "name": row.name,
            "group_name": row.group_name,
            "homepage_url": row.homepage_url,
            "enabled": row.enabled,
            "entries": [
                {
                    "url": entry.url,
                    "adapter_key": entry.adapter_key,
                    "ordered_by_date": entry.ordered_by_date,
                    "enabled": entry.enabled,
                    "listing_selectors": json.loads(entry.listing_selectors_json or "[]"),
                    "container_selectors": json.loads(entry.container_selectors_json or "[]"),
                    "content_type_selectors": json.loads(entry.content_type_selectors_json or "[]"),
                    "date_selectors": json.loads(entry.date_selectors_json or "[]"),
                    "allowed_path_patterns": json.loads(entry.allowed_path_patterns_json or "[]"),
                    "excluded_path_patterns": json.loads(entry.excluded_path_patterns_json or "[]"),
                }
                for entry in row.entries
                if entry.deleted_at is None
            ],
        }
        for row in rows
        if any(entry.deleted_at is None for entry in row.entries)
    ]


def serialize_configurations(
    session: Session, configuration_id: int | None = None
) -> list[dict[str, object]]:
    query = select(SourceConfiguration).where(SourceConfiguration.deleted_at.is_(None))
    if configuration_id is not None:
        query = query.where(SourceConfiguration.id == configuration_id)
    rows = session.scalars(query.order_by(SourceConfiguration.id)).all()
    return [
        {
            "name": row.name,
            "sources": serialize_sources(session, row.id),
        }
        for row in rows
    ]


def save_sources_json(
    session: Session, path: Path, configuration_id: int | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": SOURCE_CONFIG_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "configurations": serialize_configurations(session, configuration_id),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def parse_sources_json(raw: bytes | str) -> list[ImportedSource]:
    configurations = parse_configurations_json(raw)
    return [source for configuration in configurations for source in configuration.sources]


def parse_configurations_json(raw: bytes | str) -> list[ImportedConfiguration]:
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    payload = json.loads(text)
    if isinstance(payload, dict) and int(payload.get("schema_version", 1)) > SOURCE_CONFIG_SCHEMA_VERSION:
        raise ValueError("sources.json 来自更高版本程序，当前版本无法可靠导入")
    if isinstance(payload, dict) and "configurations" in payload:
        rows = payload["configurations"]
        if not isinstance(rows, list):
            raise ValueError("sources.json 的 configurations 必须是数组")
        return [ImportedConfiguration.model_validate(row) for row in rows]
    rows = payload.get("sources", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("sources.json must contain a sources array")
    return [
        ImportedConfiguration(
            name="导入配置",
            sources=[ImportedSource.model_validate(row) for row in rows],
        )
    ]
