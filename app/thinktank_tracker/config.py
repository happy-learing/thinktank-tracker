from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError

from .paths import PATHS


KEYRING_SERVICE = "ThinkTankTracker"
KEYRING_USERNAME = "model-api-key"


@dataclass
class AppConfig:
    api_base_url: str = ""
    model_name: str = ""
    api_style: str = "chat_completions"
    request_timeout_seconds: int = 120
    max_model_input_chars: int = 60_000
    crawl_concurrency: int = 3
    per_domain_delay_seconds: float = 2.0
    max_listing_pages: int = 25
    raw_cache_days: int = 90


def config_path() -> Path:
    return PATHS.config / "app.json"


def load_config() -> AppConfig:
    PATHS.ensure()
    path = config_path()
    if not path.exists():
        return AppConfig()
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig()
    allowed = {field.name for field in fields(AppConfig)}
    return AppConfig(**{key: value for key, value in raw.items() if key in allowed})


def save_config(config: AppConfig) -> None:
    PATHS.ensure()
    config_path().write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_api_key() -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except KeyringError:
        return None


def set_api_key(api_key: str) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, api_key.strip())


def delete_api_key() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except (KeyringError, keyring.errors.PasswordDeleteError):
        return

