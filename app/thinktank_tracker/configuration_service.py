from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Source, SourceConfiguration, SourceEntry, utcnow


DEFAULT_CONFIGURATION_NAME = "默认配置"


def active_configurations(session: Session) -> list[SourceConfiguration]:
    return list(
        session.scalars(
            select(SourceConfiguration)
            .where(SourceConfiguration.deleted_at.is_(None))
            .order_by(
                SourceConfiguration.last_used_at.desc().nullslast(),
                SourceConfiguration.id,
            )
        ).all()
    )


def ensure_default_configuration(session: Session) -> SourceConfiguration:
    configuration = session.scalar(
        select(SourceConfiguration)
        .where(SourceConfiguration.deleted_at.is_(None))
        .order_by(SourceConfiguration.id)
    )
    if configuration is None:
        configuration = SourceConfiguration(name=DEFAULT_CONFIGURATION_NAME)
        session.add(configuration)
        session.flush()
    session.query(Source).filter(Source.configuration_id.is_(None)).update(
        {Source.configuration_id: configuration.id}, synchronize_session=False
    )
    return configuration


def get_active_configuration(session: Session, configuration_id: int) -> SourceConfiguration:
    configuration = session.scalar(
        select(SourceConfiguration).where(
            SourceConfiguration.id == configuration_id,
            SourceConfiguration.deleted_at.is_(None),
        )
    )
    if configuration is None:
        raise ValueError("检索配置不存在或已删除")
    return configuration


def _validated_name(session: Session, name: str, exclude_id: int | None = None) -> str:
    value = " ".join(name.split()).strip()
    if not value:
        raise ValueError("配置名称不能为空")
    if len(value) > 200:
        raise ValueError("配置名称不能超过200个字符")
    duplicate = session.scalar(
        select(SourceConfiguration.id).where(
            SourceConfiguration.deleted_at.is_(None),
            func.lower(SourceConfiguration.name) == value.lower(),
            SourceConfiguration.id != (exclude_id or -1),
        )
    )
    if duplicate is not None:
        raise ValueError("已经存在同名配置")
    return value


def create_configuration(session: Session, name: str) -> SourceConfiguration:
    row = SourceConfiguration(name=_validated_name(session, name))
    session.add(row)
    session.flush()
    return row


def rename_configuration(
    session: Session, configuration_id: int, name: str
) -> SourceConfiguration:
    row = get_active_configuration(session, configuration_id)
    row.name = _validated_name(session, name, row.id)
    row.updated_at = utcnow()
    return row


def _copy_entry(source_id: int, entry: SourceEntry) -> SourceEntry:
    return SourceEntry(
        source_id=source_id,
        url=entry.url,
        canonical_url=entry.canonical_url,
        adapter_key=entry.adapter_key,
        ordered_by_date=entry.ordered_by_date,
        enabled=entry.enabled,
        listing_selectors_json=entry.listing_selectors_json,
        container_selectors_json=entry.container_selectors_json,
        content_type_selectors_json=entry.content_type_selectors_json,
        date_selectors_json=entry.date_selectors_json,
        allowed_path_patterns_json=entry.allowed_path_patterns_json,
        excluded_path_patterns_json=entry.excluded_path_patterns_json,
        last_test_status="untested",
    )


def duplicate_configuration(
    session: Session, configuration_id: int, name: str
) -> SourceConfiguration:
    original = get_active_configuration(session, configuration_id)
    duplicate = create_configuration(session, name)
    sources = session.scalars(
        select(Source)
        .where(
            Source.configuration_id == original.id,
            Source.deleted_at.is_(None),
        )
        .order_by(Source.id)
    ).all()
    for source in sources:
        copied_source = Source(
            configuration_id=duplicate.id,
            name=source.name,
            group_name=source.group_name,
            homepage_url=source.homepage_url,
            publication_url=source.publication_url,
            adapter_key=source.adapter_key,
            ordered_by_date=source.ordered_by_date,
            enabled=source.enabled,
        )
        session.add(copied_source)
        session.flush()
        for entry in source.entries:
            if entry.deleted_at is None:
                session.add(_copy_entry(copied_source.id, entry))
    session.flush()
    return duplicate


def delete_configuration(session: Session, configuration_id: int) -> SourceConfiguration:
    row = get_active_configuration(session, configuration_id)
    active_count = int(
        session.scalar(
            select(func.count(SourceConfiguration.id)).where(
                SourceConfiguration.deleted_at.is_(None)
            )
        )
        or 0
    )
    if active_count <= 1:
        raise ValueError("至少需要保留一个检索配置")
    row.deleted_at = utcnow()
    row.updated_at = utcnow()
    return row


def restore_configuration(session: Session, configuration_id: int) -> SourceConfiguration:
    row = session.get(SourceConfiguration, configuration_id)
    if row is None or row.deleted_at is None:
        raise ValueError("回收站中不存在该配置")
    _validated_name(session, row.name)
    row.deleted_at = None
    row.updated_at = utcnow()
    return row
