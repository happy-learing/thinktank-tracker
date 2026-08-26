from __future__ import annotations

from sqlalchemy import Connection


CURRENT_SCHEMA_VERSION = 1


def migrate(connection: Connection) -> None:
    current = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {current} 高于程序支持的版本 {CURRENT_SCHEMA_VERSION}，请使用更新版本程序"
        )
    # Version 1 is created idempotently by SQLAlchemy metadata before this call.
    if current < 1:
        connection.exec_driver_sql("PRAGMA user_version=1")
