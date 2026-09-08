from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .paths import PATHS


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    return f"sqlite:///{PATHS.database.as_posix()}"


engine = create_engine(
    _database_url(),
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def initialize_database() -> None:
    PATHS.ensure()
    from . import models  # noqa: F401
    from .migrations import CURRENT_SCHEMA_VERSION

    if PATHS.database.exists() and PATHS.database.stat().st_size:
        source = sqlite3.connect(PATHS.database)
        try:
            current = int(source.execute("PRAGMA user_version").fetchone()[0])
            has_tables = bool(
                source.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            )
            if has_tables and 0 < current < CURRENT_SCHEMA_VERSION:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                backup_path = PATHS.backups / f"tracker_pre_v{CURRENT_SCHEMA_VERSION}_{timestamp}.sqlite3"
                target = sqlite3.connect(backup_path)
                try:
                    source.backup(target)
                finally:
                    target.close()
        finally:
            source.close()

    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        from .migrations import migrate

        current = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
        connection.commit()
        if current < CURRENT_SCHEMA_VERSION:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        migrate(connection)
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"数据库迁移后外键检查失败：{violations[:5]}")
        connection.exec_driver_sql(
            "UPDATE crawl_runs SET status='interrupted', completed_at=CURRENT_TIMESTAMP, "
            "error_message=CASE WHEN error_message='' THEN '程序上次退出时任务仍在运行，可从断点恢复' "
            "ELSE error_message END WHERE status='running'"
        )
        connection.exec_driver_sql(
            "UPDATE source_entry_runs SET status='incomplete', completed_at=CURRENT_TIMESTAMP, "
            "error_message=CASE WHEN error_message='' THEN '程序上次退出时入口仍在运行' "
            "ELSE error_message END WHERE status='running'"
        )
        connection.exec_driver_sql(
            "UPDATE source_runs SET status='incomplete', completed_at=CURRENT_TIMESTAMP, "
            "error_message=CASE WHEN error_message='' THEN '程序上次退出时来源仍在运行' "
            "ELSE error_message END WHERE status='running'"
        )
        connection.commit()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
