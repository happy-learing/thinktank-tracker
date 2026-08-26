from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Source, utcnow
from .schemas import ImportedSource


SOURCE_CONFIG_SCHEMA_VERSION = 1
PROMPT_VERSION = "2026-08-26.v1"


def upsert_sources(session: Session, sources: list[ImportedSource]) -> tuple[int, int]:
    created = 0
    updated = 0
    for item in sources:
        homepage = str(item.homepage_url)
        existing = session.scalar(select(Source).where(Source.homepage_url == homepage))
        values = {
            "name": item.name,
            "group_name": item.group_name,
            "publication_url": str(item.publication_url or item.homepage_url),
            "adapter_key": item.adapter_key,
            "ordered_by_date": item.ordered_by_date,
            "enabled": item.enabled,
            "updated_at": utcnow(),
        }
        if existing is None:
            session.add(Source(homepage_url=homepage, **values))
            created += 1
        else:
            for key, value in values.items():
                setattr(existing, key, value)
            updated += 1
    session.flush()
    return created, updated


def serialize_sources(session: Session) -> list[dict[str, object]]:
    rows = session.scalars(select(Source).order_by(Source.id)).all()
    return [
        {
            "name": row.name,
            "group_name": row.group_name,
            "homepage_url": row.homepage_url,
            "publication_url": row.publication_url,
            "adapter_key": row.adapter_key,
            "ordered_by_date": row.ordered_by_date,
            "enabled": row.enabled,
        }
        for row in rows
    ]


def save_sources_json(session: Session, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": SOURCE_CONFIG_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "sources": serialize_sources(session),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def parse_sources_json(raw: bytes | str) -> list[ImportedSource]:
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    payload = json.loads(text)
    if isinstance(payload, dict) and int(payload.get("schema_version", 1)) > SOURCE_CONFIG_SCHEMA_VERSION:
        raise ValueError("sources.json 来自更高版本程序，当前版本无法可靠导入")
    rows = payload.get("sources", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("sources.json must contain a sources array")
    return [ImportedSource.model_validate(row) for row in rows]
