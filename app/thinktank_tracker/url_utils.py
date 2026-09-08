from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .constants import DEFAULT_CONTENT_TYPE_LABEL_RULES, DEFAULT_URL_CONTENT_TYPE_RULES


URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）)\]}]+", re.IGNORECASE)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}

URLContentTypeRule = Mapping[str, str] | Sequence[str]
URLContentTypeRules = Iterable[URLContentTypeRule]
ContentTypeRule = Mapping[str, str] | Sequence[str]
ContentTypeRules = Iterable[ContentTypeRule]

CONTENT_TYPE_MATCH_KINDS = {"url_path", "card_label"}
CONTENT_TYPE_ACTIONS = {"allow", "exclude", "review"}
KNOWN_CANONICAL_TYPES = {
    "analysis": "analysis",
    "reports": "report",
    "report": "report",
    "commissioned report": "report",
    "commissioned reports": "report",
    "analisis": "analysis",
    "analyses": "analysis",
    "brief": "policy brief",
    "briefs": "policy brief",
    "policy brief": "policy brief",
    "policy briefs": "policy brief",
    "european policy briefs": "policy brief",
    "security policy briefs": "policy brief",
    "africa policy briefs": "policy brief",
    "research paper": "research paper",
    "research papers": "research paper",
    "papers": "research paper",
    "policy contributions": "policy contribution",
    "commentaries": "commentary",
    "comentarios": "commentary",
}


@dataclass(frozen=True)
class ContentTypeEvidence:
    raw_value: str
    location: str
    match_kind: str
    match_value: str = ""
    content_type: str = "unknown"
    action: str = "review"
    matched: bool = False
    conflict: bool = False

    def to_dict(self) -> dict[str, str | bool]:
        return asdict(self)


def stable_content_type_rule_id(match_kind: str, match_value: str) -> str:
    digest = sha256(f"{match_kind}\0{match_value}".encode("utf-8")).hexdigest()[:16]
    return f"ctr-{digest}"


def extract_urls(text: str) -> list[str]:
    return [match.rstrip(".,;:") for match in URL_RE.findall(text or "")]


def normalize_fetch_url(url: str) -> str:
    """Normalize transport-safe parts without changing path slash or query order."""
    split = urlsplit(url.strip())
    scheme = split.scheme.lower() or "https"
    hostname = (split.hostname or "").lower()
    port = split.port
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", split.path or "/")
    return urlunsplit((scheme, netloc, path, split.query, ""))


def canonicalize_url(url: str, *, preserve_trailing_slash: bool = False) -> str:
    split = urlsplit(normalize_fetch_url(url))
    scheme = split.scheme
    netloc = split.netloc
    path = re.sub(r"/{2,}", "/", split.path or "/")
    if path != "/" and not preserve_trailing_slash:
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(split.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query_pairs)), ""))


def normalize_url_content_type_keyword(value: str) -> str:
    normalized = unquote(str(value)).strip().lower().strip("/")
    normalized = re.sub(r"[\s_]+", "-", normalized)
    return re.sub(r"-{2,}", "-", normalized)


def normalize_content_type_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().casefold())


def normalize_content_type_value(value: str) -> str:
    normalized = normalize_content_type_label(value)
    return KNOWN_CANONICAL_TYPES.get(normalized, normalized)


def normalize_content_type_rules(
    rules: ContentTypeRules | None,
) -> list[dict[str, str]]:
    defaults: list[ContentTypeRule] = [
            {
                "match_kind": "url_path",
                "match_value": keyword,
                "content_type": content_type,
                "action": "allow",
            }
            for keyword, content_type in DEFAULT_URL_CONTENT_TYPE_RULES
        ]
    defaults.extend(
            {
                "match_kind": "card_label",
                "match_value": label,
                "content_type": content_type,
                "action": action,
            }
            for label, content_type, action in DEFAULT_CONTENT_TYPE_LABEL_RULES
    )
    if rules is None:
        source = defaults
    else:
        # A saved configuration may predate newly shipped aliases. User rules
        # stay first so an explicit rule wins; missing built-ins are appended.
        source = (
            [*list(rules), *defaults]
            if not isinstance(rules, (str, bytes))
            else defaults
        )
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rule in source:
        if isinstance(rule, Mapping):
            if "match_kind" in rule or "match_value" in rule:
                match_kind_raw = rule.get("match_kind", "")
                match_value_raw = rule.get("match_value", "")
                content_type_raw = rule.get("content_type", "")
                action_raw = rule.get("action", "allow")
            else:
                match_kind_raw = "url_path"
                match_value_raw = rule.get("keyword", "")
                content_type_raw = rule.get("content_type", "")
                action_raw = "allow"
        elif isinstance(rule, Sequence) and not isinstance(rule, (str, bytes)) and len(rule) >= 2:
            match_kind_raw = "url_path"
            match_value_raw, content_type_raw = rule[0], rule[1]
            action_raw = "allow"
        else:
            continue
        match_kind = str(match_kind_raw).strip().lower()
        action = str(action_raw).strip().lower()
        match_value = (
            normalize_url_content_type_keyword(str(match_value_raw))
            if match_kind == "url_path"
            else normalize_content_type_label(str(match_value_raw))
        )
        content_type = normalize_content_type_value(str(content_type_raw))
        key = (match_kind, match_value)
        if (
            match_kind not in CONTENT_TYPE_MATCH_KINDS
            or action not in CONTENT_TYPE_ACTIONS
            or not match_value
            or not content_type
            or content_type in {"unknown", "__custom__"}
            or key in seen
            or (match_kind == "url_path" and any(marker in match_value for marker in ("/", "?", "#")))
        ):
            continue
        normalized.append(
            {
                "rule_id": stable_content_type_rule_id(match_kind, match_value),
                "match_kind": match_kind,
                "match_value": match_value,
                "content_type": content_type,
                "action": action,
            }
        )
        seen.add(key)
    return normalized


def collect_content_type_evidence(
    url: str,
    labels: Iterable[str] = (),
    rules: ContentTypeRules | None = None,
    *,
    label_location: str = "card_label",
) -> list[ContentTypeEvidence]:
    """Return every non-empty deterministic content-type fact used for audit."""
    configured = normalize_content_type_rules(rules)
    label_rules = {
        rule["match_value"]: rule
        for rule in configured
        if rule["match_kind"] == "card_label"
    }
    url_rules = {
        rule["match_value"]: rule
        for rule in configured
        if rule["match_kind"] == "url_path"
    }
    items: list[ContentTypeEvidence] = []
    raw_labels: list[str] = []
    seen_labels: set[str] = set()
    for label in labels:
        raw = re.sub(r"\s+", " ", str(label).strip())
        normalized = normalize_content_type_label(raw)
        if not normalized or normalized in seen_labels:
            continue
        seen_labels.add(normalized)
        raw_labels.append(raw)
    for raw in raw_labels:
        normalized = normalize_content_type_label(raw)
        rule = label_rules.get(normalized)
        items.append(
            ContentTypeEvidence(
                raw_value=raw,
                location=label_location,
                match_kind="card_label",
                match_value=normalized,
                content_type=rule["content_type"] if rule else raw,
                action=rule["action"] if rule else "review",
                matched=rule is not None,
            )
        )
    for raw_segment in urlsplit(url).path.split("/"):
        segment = normalize_url_content_type_keyword(raw_segment)
        if not segment or segment not in url_rules:
            continue
        rule = url_rules[segment]
        items.append(
            ContentTypeEvidence(
                raw_value=raw_segment,
                location="url_path",
                match_kind="url_path",
                match_value=segment,
                content_type=rule["content_type"],
                action=rule["action"],
                matched=True,
            )
        )
        break
    resolved_type, evidence, _ = resolve_content_type(url, raw_labels, rules)
    conflict = resolved_type == "unknown" and "体裁冲突" in evidence
    if conflict:
        items = [
            ContentTypeEvidence(**{**item.to_dict(), "conflict": True})
            for item in items
        ]
    return items


def legacy_url_rules_from_content_type_rules(
    rules: ContentTypeRules | None,
) -> list[dict[str, str]]:
    return [
        {"keyword": rule["match_value"], "content_type": rule["content_type"]}
        for rule in normalize_content_type_rules(rules)
        if rule["match_kind"] == "url_path"
    ]


def normalize_url_content_type_rules(
    rules: URLContentTypeRules | None,
) -> list[dict[str, str]]:
    source: URLContentTypeRules = DEFAULT_URL_CONTENT_TYPE_RULES if rules is None else rules
    if isinstance(source, (str, bytes)):
        return []
    normalized: list[dict[str, str]] = []
    seen_keywords: set[str] = set()
    for rule in source:
        if isinstance(rule, Mapping):
            keyword_raw = rule.get("keyword", "")
            content_type_raw = rule.get("content_type", "")
        elif isinstance(rule, Sequence) and not isinstance(rule, (str, bytes)) and len(rule) >= 2:
            keyword_raw, content_type_raw = rule[0], rule[1]
        else:
            continue
        keyword = normalize_url_content_type_keyword(str(keyword_raw))
        content_type = normalize_content_type_value(str(content_type_raw))
        if (
            not keyword
            or not content_type
            or keyword in seen_keywords
            or any(marker in keyword for marker in ("/", "?", "#"))
            or content_type in {"unknown", "__custom__"}
        ):
            continue
        normalized.append({"keyword": keyword, "content_type": content_type})
        seen_keywords.add(keyword)
    return normalized


def content_types_from_rules(rules: URLContentTypeRules | None) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(rule["content_type"] for rule in normalize_url_content_type_rules(rules))
    )


def content_type_actions_from_rules(
    rules: ContentTypeRules | None,
) -> dict[str, str]:
    actions: dict[str, str] = {}
    conflicts: set[str] = set()
    for rule in normalize_content_type_rules(rules):
        content_type = rule["content_type"]
        action = rule["action"]
        previous = actions.get(content_type)
        if previous is not None and previous != action:
            conflicts.add(content_type)
        else:
            actions[content_type] = action
    for content_type in conflicts:
        actions[content_type] = "review"
    return actions


def content_types_from_content_type_rules(
    rules: ContentTypeRules | None,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(rule["content_type"] for rule in normalize_content_type_rules(rules))
    )


def _url_content_type_match(
    url: str,
    rules: ContentTypeRules | None,
) -> tuple[str, str, str]:
    configured_rules = [
        rule for rule in normalize_content_type_rules(rules) if rule["match_kind"] == "url_path"
    ]
    rule_by_segment = {rule["match_value"]: rule for rule in configured_rules}
    segments = [normalize_url_content_type_keyword(segment) for segment in urlsplit(url).path.split("/")]
    for segment in segments:
        if rule := rule_by_segment.get(segment):
            content_type = rule["content_type"]
            return content_type, f"URL 路径段 '{segment}' 命中规则 -> {content_type}", rule["action"]
    return "unknown", "URL 路径未命中已配置的体裁规则", "review"


def _label_content_type_match(
    labels: Iterable[str],
    rules: ContentTypeRules | None,
) -> tuple[str, str, str, bool]:
    configured = [
        rule for rule in normalize_content_type_rules(rules) if rule["match_kind"] == "card_label"
    ]
    rule_by_label = {rule["match_value"]: rule for rule in configured}
    raw_labels: list[str] = []
    seen_labels: set[str] = set()
    for label in labels:
        raw = re.sub(r"\s+", " ", str(label).strip())
        normalized = normalize_content_type_label(raw)
        if not normalized or normalized in seen_labels:
            continue
        seen_labels.add(normalized)
        raw_labels.append(raw)
    matches: list[tuple[str, dict[str, str]]] = []
    for raw in raw_labels:
        if rule := rule_by_label.get(normalize_content_type_label(raw)):
            matches.append((raw, rule))
    distinct_types = {rule["content_type"] for _, rule in matches}
    distinct_actions = {rule["action"] for _, rule in matches}
    if len(distinct_types) > 1 or len(distinct_actions) > 1:
        evidence = "；".join(
            f"栏目卡片标签 '{raw}' -> {rule['content_type']} ({rule['action']})"
            for raw, rule in matches
        )
        return "unknown", f"体裁冲突：{evidence}", "review", True
    if matches:
        content_type = matches[0][1]["content_type"]
        action = matches[0][1]["action"]
        evidence = "；".join(f"栏目卡片标签 '{raw}' -> {content_type}" for raw, _ in matches)
        return content_type, evidence, action, True
    if raw_labels:
        return raw_labels[0], f"栏目卡片体裁标签未映射：{'；'.join(raw_labels)}", "review", False
    return "unknown", "栏目卡片未提供可识别体裁标签", "review", False


def resolve_content_type(
    url: str,
    labels: Iterable[str] = (),
    rules: ContentTypeRules | None = None,
) -> tuple[str, str, str]:
    url_type, url_evidence, url_action = _url_content_type_match(url, rules)
    label_type, label_evidence, label_action, label_matched = _label_content_type_match(labels, rules)
    url_matched = url_type != "unknown"
    if url_matched and label_matched:
        if url_type != label_type or url_action != label_action:
            return "unknown", f"体裁冲突：{url_evidence}；{label_evidence}", "review"
        return url_type, f"{url_evidence}；{label_evidence}", url_action
    if url_matched:
        suffix = f"；{label_evidence}" if label_type != "unknown" else ""
        return url_type, f"{url_evidence}{suffix}", url_action
    if label_type != "unknown":
        return label_type, label_evidence, label_action
    return "unknown", f"{url_evidence}；{label_evidence}", "review"


def content_type_from_url(
    url: str,
    rules: ContentTypeRules | None = None,
) -> tuple[str, str]:
    content_type, evidence, _ = _url_content_type_match(url, rules)
    return content_type, evidence
