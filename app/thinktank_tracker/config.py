from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError

from .paths import PATHS
from .url_utils import (
    content_type_actions_from_rules,
    content_types_from_content_type_rules,
    legacy_url_rules_from_content_type_rules,
    normalize_content_type_label,
    normalize_content_type_rules,
    normalize_content_type_value,
    normalize_url_content_type_keyword,
    normalize_url_content_type_rules,
)


KEYRING_SERVICE = "ThinkTankTracker"
KEYRING_USERNAME = "model-api-key"
PDF_PARSER_MODES = {"anydoc_first", "pypdf_only"}

MODEL_PROVIDER_PRESETS = {
    "custom": {
        "label": "OpenAI 兼容服务（自定义）",
        "api_base_url": "",
        "model_name": "",
        "api_style": "chat_completions",
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_base_url": "https://api.deepseek.com",
        "model_name": "deepseek-v4-flash",
        "api_style": "chat_completions",
    },
}


def normalize_pdf_parser_mode(value: object) -> str:
    return value if isinstance(value, str) and value in PDF_PARSER_MODES else "anydoc_first"


@dataclass
class AppConfig:
    provider_kind: str = "deepseek"
    api_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    api_style: str = "chat_completions"
    request_timeout_seconds: int = 120
    max_model_input_chars: int = 20_000
    crawl_concurrency: int = 3
    per_domain_delay_seconds: float = 2.0
    max_listing_pages: int = 25
    raw_cache_days: int = 90
    pdf_parser_mode: str = "anydoc_first"
    content_type_rules: list[dict[str, str]] | None = None
    # Compatibility input for v0.5 app.json and callers. New saves omit this key.
    url_content_type_rules: list[dict[str, str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        source = self.content_type_rules
        if source is None:
            if self.url_content_type_rules:
                legacy_url_rules = normalize_content_type_rules(self.url_content_type_rules)
                default_label_rules = [
                    rule
                    for rule in normalize_content_type_rules(None)
                    if rule["match_kind"] == "card_label"
                ]
                source = [*legacy_url_rules, *default_label_rules]
            else:
                source = None
        self.content_type_rules = normalize_content_type_rules(source)
        self.url_content_type_rules = legacy_url_rules_from_content_type_rules(
            self.content_type_rules
        )

    @property
    def content_types(self) -> tuple[str, ...]:
        return content_types_from_content_type_rules(self.content_type_rules)

    @property
    def content_type_actions(self) -> dict[str, str]:
        return content_type_actions_from_rules(self.content_type_rules)

    @property
    def allowed_content_types(self) -> frozenset[str]:
        return frozenset(
            content_type
            for content_type, action in self.content_type_actions.items()
            if action == "allow"
        )

    @property
    def excluded_content_types(self) -> frozenset[str]:
        return frozenset(
            content_type
            for content_type, action in self.content_type_actions.items()
            if action == "exclude"
        )


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
    allowed = {item.name for item in fields(AppConfig)}
    values = {key: value for key, value in raw.items() if key in allowed}
    if "content_type_rules" in values and not isinstance(values["content_type_rules"], list):
        values.pop("content_type_rules")
    if "url_content_type_rules" in values and not isinstance(values["url_content_type_rules"], list):
        values.pop("url_content_type_rules")
    config = AppConfig(**values)
    config.pdf_parser_mode = normalize_pdf_parser_mode(config.pdf_parser_mode)
    return config


def save_config(config: AppConfig) -> None:
    PATHS.ensure()
    config.pdf_parser_mode = normalize_pdf_parser_mode(config.pdf_parser_mode)
    config.content_type_rules = normalize_content_type_rules(config.content_type_rules)
    config.url_content_type_rules = legacy_url_rules_from_content_type_rules(
        config.content_type_rules
    )
    payload = asdict(config)
    payload.pop("url_content_type_rules", None)
    config_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_content_type_rules(
    match_kinds: list[str],
    match_values: list[str],
    content_types: list[str],
    actions: list[str],
) -> list[dict[str, str]]:
    lengths = {len(match_kinds), len(match_values), len(content_types), len(actions)}
    if len(lengths) != 1:
        raise ValueError("体裁规则字段数量不一致")
    if len(match_kinds) > 100:
        raise ValueError("体裁规则最多允许 100 条")
    rules: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    action_by_type: dict[str, str] = {}
    for index, values in enumerate(zip(match_kinds, match_values, content_types, actions), 1):
        kind_raw, value_raw, content_type_raw, action_raw = values
        if not any(item.strip() for item in values):
            continue
        kind = kind_raw.strip().lower()
        action = action_raw.strip().lower()
        if kind not in {"url_path", "card_label"}:
            raise ValueError(f"第 {index} 条体裁规则的匹配方式无效")
        if action not in {"allow", "exclude", "review"}:
            raise ValueError(f"第 {index} 条体裁规则的处理方式无效")
        match_value = (
            normalize_url_content_type_keyword(value_raw)
            if kind == "url_path"
            else normalize_content_type_label(value_raw)
        )
        content_type = normalize_content_type_value(content_type_raw)
        if not match_value or not content_type:
            raise ValueError(f"第 {index} 条体裁规则不完整")
        if len(match_value) > 100 or len(content_type) > 100:
            raise ValueError(f"第 {index} 条体裁规则超过 100 个字符")
        if content_type in {"unknown", "__custom__"}:
            raise ValueError(f"第 {index} 条规则使用了保留体裁值")
        if kind == "url_path" and any(marker in match_value for marker in ("/", "?", "#")):
            raise ValueError(f"第 {index} 条 URL 路径词只能填写一个路径段")
        key = (kind, match_value)
        if key in seen:
            raise ValueError(f"体裁匹配值重复：{match_value}")
        previous_action = action_by_type.get(content_type)
        if previous_action is not None and previous_action != action:
            raise ValueError(f"同一规范体裁不能配置不同处理方式：{content_type}")
        action_by_type[content_type] = action
        seen.add(key)
        rules.append(
            {
                "match_kind": kind,
                "match_value": match_value,
                "content_type": content_type,
                "action": action,
            }
        )
    if not rules:
        raise ValueError("至少保留一条体裁规则")
    return normalize_content_type_rules(rules)


def parse_url_content_type_rules(
    keywords: list[str],
    content_types: list[str],
) -> list[dict[str, str]]:
    if len(keywords) != len(content_types):
        raise ValueError("URL 体裁规则字段数量不一致")
    if len(keywords) > 50:
        raise ValueError("URL 体裁规则最多允许 50 条")
    rules: list[dict[str, str]] = []
    seen_keywords: set[str] = set()
    for index, (keyword_raw, content_type_raw) in enumerate(zip(keywords, content_types), 1):
        keyword_value = keyword_raw.strip()
        content_type_value = normalize_content_type_value(content_type_raw)
        if not keyword_value and not content_type_value:
            continue
        if not keyword_value or not content_type_value:
            raise ValueError(f"第 {index} 条 URL 体裁规则不完整")
        keyword = normalize_url_content_type_keyword(keyword_value)
        if any(marker in keyword for marker in ("/", "?", "#")):
            raise ValueError(f"第 {index} 条 URL 路径词只能填写一个路径段")
        if len(keyword) > 100 or len(content_type_value) > 100:
            raise ValueError(f"第 {index} 条 URL 体裁规则超过 100 个字符")
        if content_type_value in {"unknown", "__custom__"}:
            raise ValueError(f"第 {index} 条规则使用了保留体裁值")
        if keyword in seen_keywords:
            raise ValueError(f"URL 路径词重复：{keyword}")
        seen_keywords.add(keyword)
        rules.append({"keyword": keyword, "content_type": content_type_value})
    if not rules:
        raise ValueError("至少保留一条 URL 体裁规则")
    return rules


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
