from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import PATHS


def configure_logging() -> None:
    PATHS.ensure()
    root = logging.getLogger()
    if any(getattr(handler, "name", "") == "tracker-file" for handler in root.handlers):
        return
    handler = RotatingFileHandler(
        PATHS.logs / "tracker.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.name = "tracker-file"
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
