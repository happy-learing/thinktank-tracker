from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def application_root() -> Path:
    override = os.getenv("THINKTANK_APP_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppPaths:
    root: Path
    workspace: Path
    config: Path
    cache: Path
    downloads: Path
    exports: Path
    backups: Path
    logs: Path
    database: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        root = application_root()
        workspace_override = os.getenv("THINKTANK_WORKSPACE")
        workspace = (
            Path(workspace_override).expanduser().resolve()
            if workspace_override
            else root / "workspace"
        )
        return cls(
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

    def ensure(self) -> None:
        for directory in (
            self.workspace,
            self.config,
            self.cache,
            self.downloads,
            self.exports,
            self.backups,
            self.logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)


PATHS = AppPaths.discover()

