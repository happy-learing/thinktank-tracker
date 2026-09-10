from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from typing import Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from pypdf import PdfReader

from .url_utils import (
    ContentTypeEvidence,
    ContentTypeRules,
    canonicalize_url,
    collect_content_type_evidence,
    content_type_from_url,
    normalize_fetch_url,
    resolve_content_type,
)


DATE_KEYS = {
    "datepublished",
    "publicationdate",
    "publishdate",
    "article:published_time",
    "og:published_time",
    "citation_publication_date",
    "citation_date",
    "parsely-pub-date",
    "dc.date",
    "dcterms.date",
}
CONTENT_PATH_HINTS = (
    "/article",
    "/publication",
    "/publications",
    "/research",
    "/report",
    "/reports",
    "/paper",
    "/policy-brief",
    "/analysis",
    "/study",
    "/commentary",
)
LISTING_PATH_FRAGMENTS = ("/page/", "/tag/", "/category/", "/author/", "/topic/")
DATE_TEXT_RE = re.compile(
    r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b"
    r"|\b(?:0?[1-9]|[12]\d|3[01])\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|Ene(?:ro)?|Abr(?:il)?|Jun(?:io)?|Jul(?:io)?|Ago(?:sto)?|Sep(?:tiembre)?|Oct(?:ubre)?|Nov(?:iembre)?|Dic(?:iembre)?)\s+(?:19|20)\d{2}\b"
    r"|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01]),?\s+(?:19|20)\d{2}\b",
    re.IGNORECASE,
)
PDF_MIN_TEXT_CHARS = 500
_PDF_PARSE_SEMAPHORE = threading.Semaphore(1)


@dataclass(frozen=True)
class DateFact:
    value: date
    source: str
    raw: str
    priority: int = 50


@dataclass
class ExtractedDocument:
    title: str = ""
    text: str = ""
    language: str = "unknown"
    english_word_count: int = 0
    chinese_char_count: int = 0
    published_date: date | None = None
    date_status: str = "missing"
    date_evidence: str = ""
    content_type: str = "unknown"
    content_type_evidence: str = ""
    content_type_labels: list[str] = field(default_factory=list)
    content_type_evidence_items: list[ContentTypeEvidence] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    author_evidence: str = ""
    extraction_status: str = "complete"
    content_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    abstract: str = ""
    tags: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    extraction_audit: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ListingCard:
    fetch_url: str
    canonical_url: str
    title: str = ""
    published_date: date | None = None
    date_evidence: str = ""
    content_type: str = ""
    content_type_evidence: str = ""
    authors: tuple[str, ...] = ()
    author_evidence: str = ""
    position: int = 0
    is_featured: bool = False
    content_type_labels: tuple[str, ...] = ()
    content_type_evidence_items: tuple[ContentTypeEvidence, ...] = ()


@dataclass(frozen=True)
class DiscoveredAttachment:
    url: str
    canonical_url: str
    link_text: str = ""
    role: str = "other"
    score: int = 0


def _parse_date(raw: str) -> date | None:
    value = raw.strip()
    month_aliases = {
        "enero": "January", "febrero": "February", "marzo": "March",
        "abril": "April", "mayo": "May", "junio": "June", "julio": "July",
        "agosto": "August", "septiembre": "September", "setiembre": "September",
        "octubre": "October", "noviembre": "November", "diciembre": "December",
        "ene": "January", "abr": "April", "ago": "August", "dic": "December",
        "janvier": "January", "fevrier": "February", "février": "February",
        "mars": "March", "avril": "April", "mai": "May", "juin": "June",
        "juillet": "July", "aout": "August", "août": "August",
        "septembre": "September", "octobre": "October", "novembre": "November",
        "decembre": "December", "décembre": "December",
    }
    for source, target in month_aliases.items():
        value = re.sub(rf"\b{re.escape(source)}\b", target, value, flags=re.IGNORECASE)
    if not value or not re.search(r"(?:19|20)\d{2}", value):
        return None
    # dateutil fills omitted fields from today's date. Require a complete date so
    # strings such as "04 Ago 2026" cannot silently become April 27, 2026.
    numeric_complete = re.search(
        r"(?<!\d)(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])(?!\d)",
        value,
    )
    month_name = (
        r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
        r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    )
    day_month_year = re.search(
        rf"\b(?:0?[1-9]|[12]\d|3[01])\s+(?:{month_name})\s+(?:19|20)\d{{2}}\b",
        value,
        flags=re.IGNORECASE,
    )
    month_day_year = re.search(
        rf"\b(?:{month_name})\s+(?:0?[1-9]|[12]\d|3[01]),?\s+(?:19|20)\d{{2}}\b",
        value,
        flags=re.IGNORECASE,
    )
    if not (numeric_complete or day_month_year or month_day_year):
        return None
    try:
        parsed = date_parser.parse(value, fuzzy=True, dayfirst=bool(day_month_year))
    except (ValueError, OverflowError, TypeError):
        return None
    if not 1990 <= parsed.year <= datetime.now().year + 1:
        return None
    return parsed.date()


def _parse_pdf_creation_date(raw: str) -> date | None:
    value = raw[2:] if raw.startswith("D:") else raw
    if match := re.match(r"^(\d{4})(\d{2})(\d{2})", value):
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return _parse_date(value)


def parse_date_value(raw: str) -> date | None:
    return _parse_date(raw)


def _json_ld_objects(soup: BeautifulSoup) -> Iterable[dict]:
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.get_text(strip=True))
        except (json.JSONDecodeError, TypeError):
            continue
        queue = payload if isinstance(payload, list) else [payload]
        while queue:
            item = queue.pop(0)
            if not isinstance(item, dict):
                continue
            yield item
            graph = item.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)


def _normalize_author(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip(" \t\r\n,;|·")
    normalized = re.sub(
        r"^(?:written\s+by|authors?|by)\s*[:：]?\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized.strip(" \t\r\n,;|·")


def _dedupe_authors(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _normalize_author(str(raw))
        if not value or value in seen or len(value) > 200:
            continue
        seen.add(value)
        result.append(value)
    return result


def _author_values_from_node(node: Tag) -> list[str]:
    links = [
        re.sub(r"\s+", " ", item.get_text(" ", strip=True)).strip()
        for item in node.select("a")
        if item.get_text(" ", strip=True)
    ]
    if links:
        return links
    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    if not text:
        return []
    return [part for part in re.split(r"\s*(?:;|\||\band\b|\s&\s)\s*", text, flags=re.I) if part]


def extract_authors(
    soup: BeautifulSoup,
    preferred_selectors: tuple[str, ...] = (),
) -> tuple[list[str], str]:
    # A visible byline attached to the publication header is more specific than
    # a CMS account (frequently the institution itself) in generic JSON-LD.
    for selector in (".post-publication__header .list--authors a", "article > header .byline [rel='author']"):
        authors = _dedupe_authors(n.get_text(" ", strip=True) for n in soup.select(selector))
        if authors:
            return authors, f"文章署名 {selector}: " + "；".join(authors)
    for item in _json_ld_objects(soup):
        raw = item.get("author")
        if raw is None:
            continue
        rows = raw if isinstance(raw, list) else [raw]
        values: list[str] = []
        for row in rows:
            if isinstance(row, dict):
                if row.get("name"):
                    values.append(str(row["name"]))
            elif isinstance(row, str):
                values.append(row)
        authors = _dedupe_authors(values)
        if authors:
            return authors, "JSON-LD author: " + "；".join(authors)
    meta_values = [
        str(node.get("content", ""))
        for node in soup.select(
            "meta[name='author'][content], meta[property='article:author'][content], "
            "meta[name='citation_author'][content], meta[name='parsely-author'][content]"
        )
        if str(node.get("content", "")).strip()
    ]
    authors = _dedupe_authors(meta_values)
    if authors:
        return authors, "页面作者元标签: " + "；".join(meta_values)
    selectors = preferred_selectors + (
        "[rel='author']",
        ".byline .author",
        ".author-name",
        "[class*='author'] a",
    )
    for selector in selectors:
        values = [
            value
            for node in soup.select(selector)
            for value in _author_values_from_node(node)
        ]
        authors = _dedupe_authors(values)
        if authors:
            return authors, f"页面作者选择器 {selector}: " + "；".join(authors)
    return [], "页面未提供可确认作者"


def extract_date_facts(soup: BeautifulSoup) -> list[DateFact]:
    facts: list[DateFact] = []
    for item in _json_ld_objects(soup):
        for key, raw in item.items():
            if key.lower() not in DATE_KEYS or not isinstance(raw, str):
                continue
            parsed = _parse_date(raw)
            if parsed:
                normalized_key = key.lower()
                priority = 100 if normalized_key == "datepublished" else 95 if "publish" in normalized_key else 85
                facts.append(DateFact(parsed, f"JSON-LD {key}", raw, priority))
    for meta in soup.select("meta[content]"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if key not in DATE_KEYS and not any(part in key for part in ("publish", "publication_date")):
            continue
        raw = str(meta.get("content", ""))
        parsed = _parse_date(raw)
        if parsed:
            priority = 95 if key in {"datepublished", "article:published_time", "og:published_time"} else 85
            facts.append(DateFact(parsed, f"meta {key}", raw, priority))

    # Detail pages often render dates for related articles below the main record. Keep
    # those as evidence, but give the publication header and field selectors priority.
    seen_time_nodes: set[int] = set()
    for selector, priority in (
        (".node--view-mode-full .field--name-field-date time[datetime]", 84),
        ('[class*="view-mode-full"] .field--name-field-date time[datetime]', 84),
        (".page-metadata time[datetime]", 82),
        ("article header time[datetime]", 80),
        ("main header time[datetime]", 78),
        (".field--name-field-date time[datetime]", 76),
        (".publication-date time[datetime]", 76),
        (".published-date time[datetime]", 76),
        ("article time[datetime]", 60),
        ("main time[datetime]", 40),
        ("time[datetime]", 20),
    ):
        for node in soup.select(selector)[:8]:
            marker = id(node)
            if marker in seen_time_nodes:
                continue
            seen_time_nodes.add(marker)
            raw = str(node.get("datetime", ""))
            parsed = _parse_date(raw)
            if parsed:
                facts.append(DateFact(parsed, f"{selector}", raw, priority))

    for selector, priority in (
        ("main article > header .posted-on", 82),
        ("article > .entry-header .posted-on", 82),
        (".publication-item-date.pb-3", 84),
        (".page-banner .publication-item-date", 82),
        ("article header time:not([datetime])", 78),
        ("main header time:not([datetime])", 76),
        (".field--name-field-date", 74),
        (".publication-date", 74),
        (".published-date", 74),
        (".post-date", 30),
        (".entry-date", 70),
        ('[class*="date-publish"]', 70),
        ("article time:not([datetime])", 55),
        ("main time:not([datetime])", 35),
    ):
        for node in soup.select(selector)[:3]:
            if node.find_parent(class_=re.compile(r"(?:related|recommended|nav-prev|nav-next)", re.I)):
                continue
            raw_text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            match = DATE_TEXT_RE.search(raw_text)
            candidate = match.group(0) if match else raw_text
            parsed = _parse_date(candidate)
            if parsed:
                facts.append(DateFact(parsed, f"visible {selector}", candidate, priority))
    return facts


def resolve_date(facts: list[DateFact]) -> tuple[date | None, str, str]:
    if not facts:
        return None, "missing", "页面元数据和 time 元素均未提供可确认日期"
    highest_priority = max(fact.priority for fact in facts)
    authoritative = [fact for fact in facts if fact.priority == highest_priority]
    grouped: dict[date, list[DateFact]] = {}
    for fact in authoritative:
        grouped.setdefault(fact.value, []).append(fact)
    ordered_facts = sorted(facts, key=lambda fact: fact.priority, reverse=True)
    evidence = "; ".join(
        f"{fact.source}: {fact.raw}" for fact in ordered_facts[:8]
    )
    if len(grouped) > 1:
        return None, "conflict", evidence
    return authoritative[0].value, "confirmed", evidence


def extract_title(soup: BeautifulSoup) -> str:
    for selector in (
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
        "article h1",
        "main h1",
        "h1",
        "title",
    ):
        node = soup.select_one(selector)
        if node:
            value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
            if value:
                return re.sub(r"\s+", " ", str(value)).strip()
    return ""


def _main_node(soup: BeautifulSoup) -> Tag:
    for selector in (
        "article",
        "main article",
        '[itemprop="articleBody"]',
        ".publication-content",
        ".entry-content",
        ".post-content",
        ".article-content",
        "main",
    ):
        node = soup.select_one(selector)
        if isinstance(node, Tag) and len(node.get_text(" ", strip=True)) >= 400:
            return node
    return soup.body if isinstance(soup.body, Tag) else soup


def extract_html_text(soup: BeautifulSoup) -> str:
    node = _main_node(soup)
    for unwanted in node.select(
        "script, style, noscript, nav, footer, header, form, aside, .share, .sharing, "
        ".social, .newsletter, .cookie, .related, .recommended"
    ):
        unwanted.decompose()
    chunks: list[str] = []
    for child in node.select("h1, h2, h3, p, li, blockquote"):
        value = re.sub(r"\s+", " ", child.get_text(" ", strip=True)).strip()
        if len(value) >= 2 and (not chunks or value != chunks[-1]):
            chunks.append(value)
    if not chunks:
        chunks = [re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()]
    return "\n\n".join(chunks)


def detect_language_and_length(text: str, declared_language: str = "") -> tuple[str, int, int]:
    chinese_chars = len(re.findall(r"[\u3400-\u9fff]", text))
    words = [
        token
        for token in re.findall(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", text, flags=re.UNICODE)
        if not re.search(r"[\u3400-\u9fff]", token)
    ]
    word_count = len(words)
    declared = declared_language.strip().lower().replace("_", "-").split("-", 1)[0]
    if chinese_chars >= 300 and chinese_chars > word_count * 0.25:
        language = "zh"
    elif declared:
        language = declared
    else:
        language = "en" if word_count >= 100 else "unknown"
    if chinese_chars < 100 and word_count < 100:
        language = "unknown"
    return language, word_count, chinese_chars


def detect_content_type(
    soup: BeautifulSoup | None,
    url: str,
    title: str,
    text: str,
    content_type_rules: ContentTypeRules | None = None,
    content_type_selectors: tuple[str, ...] = (),
) -> tuple[str, str]:
    content_type, evidence, _, _ = _detect_content_type_details(
        soup,
        url,
        content_type_rules,
        content_type_selectors,
    )
    return content_type, evidence


def _content_type_scope(soup: BeautifulSoup) -> Tag:
    heading = soup.select_one("main h1, article h1, h1")
    if isinstance(heading, Tag):
        article = heading.find_parent("article")
        if isinstance(article, Tag):
            return article
        main = heading.find_parent("main")
        if isinstance(main, Tag):
            return main
    for selector in ("main article", "article", "main"):
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            return node
    return soup.body if isinstance(soup.body, Tag) else soup


def _detect_content_type_details(
    soup: BeautifulSoup | None,
    url: str,
    content_type_rules: ContentTypeRules | None,
    content_type_selectors: tuple[str, ...],
) -> tuple[str, str, list[str], list[ContentTypeEvidence]]:
    labels: list[str] = []
    if soup is not None:
        labels = _selected_texts(_content_type_scope(soup), content_type_selectors)
    content_type, evidence, _ = resolve_content_type(url, labels, content_type_rules)
    evidence_items = collect_content_type_evidence(
        url,
        labels,
        content_type_rules,
        label_location="detail_header",
    )
    return content_type, evidence, labels, evidence_items


def extract_html_document(
    raw: bytes,
    url: str,
    content_type_rules: ContentTypeRules | None = None,
    content_type_selectors: tuple[str, ...] = (),
    author_selectors: tuple[str, ...] = (),
) -> ExtractedDocument:
    soup = BeautifulSoup(raw, "lxml")
    title = extract_title(soup)
    published, date_status, date_evidence = resolve_date(extract_date_facts(soup))
    # Read structured fields before正文清理 removes headers, side metadata and
    # non-body tags from the shared BeautifulSoup tree.
    content_type, type_evidence, type_labels, type_evidence_items = _detect_content_type_details(
        soup, url, content_type_rules, content_type_selectors
    )
    authors, author_evidence = extract_authors(soup, author_selectors)
    abstract = ""
    for selector in (
        'meta[name="description"]',
        'meta[property="og:description"]',
        'meta[name="citation_abstract"]',
    ):
        if node := soup.select_one(selector):
            abstract = re.sub(r"\s+", " ", str(node.get("content", ""))).strip()
            if abstract:
                break
    tags: list[str] = []
    for node in soup.select('meta[name="keywords"], meta[property="article:tag"]'):
        tags.extend(
            value.strip()
            for value in re.split(r"[,;|]", str(node.get("content", "")))
            if value.strip()
        )
    main = soup.select_one("main, article") or soup
    headings = [
        re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        for node in main.select("h2, h3")[:30]
        if node.get_text(" ", strip=True)
    ]
    text = extract_html_text(soup)
    html_node = soup.select_one("html[lang]")
    declared_language = str(html_node.get("lang", "")) if html_node else ""
    language, word_count, chinese_count = detect_language_and_length(text, declared_language)
    status = "complete" if len(text) >= 400 else "incomplete"
    warnings = [] if status == "complete" else ["正文提取内容过短，可能受动态渲染或页面结构影响"]
    return ExtractedDocument(
        title=title,
        text=text,
        language=language,
        english_word_count=word_count,
        chinese_char_count=chinese_count,
        published_date=published,
        date_status=date_status,
        date_evidence=date_evidence,
        content_type=content_type,
        content_type_evidence=type_evidence,
        content_type_labels=type_labels,
        content_type_evidence_items=type_evidence_items,
        authors=authors,
        author_evidence=author_evidence,
        extraction_status=status,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        warnings=warnings,
        abstract=abstract,
        tags=list(dict.fromkeys(tags))[:30],
        headings=list(dict.fromkeys(headings))[:30],
    )


def _distribution_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _load_anydoc():  # type: ignore[no-untyped-def]
    return importlib.import_module("anydoc")


def _markdown_headings(markdown: str) -> list[str]:
    headings: list[str] = []
    fence = ""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = "" if fence == marker else marker if not fence else fence
            continue
        if fence:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", stripped)
        if not match:
            continue
        heading = re.sub(r"!?(?:\[([^]]+)\])\([^)]*\)", r"\1", match.group(1))
        heading = re.sub(r"<[^>]+>|[`*_~]", "", heading)
        heading = re.sub(r"\s+", " ", heading).strip()
        if heading and heading not in headings:
            headings.append(heading)
        if len(headings) == 30:
            break
    return headings


def _markdown_text_for_length(markdown: str) -> str:
    value = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", markdown)
    value = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", value, flags=re.MULTILINE)
    value = re.sub(
        r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$",
        "",
        value,
        flags=re.MULTILINE,
    )
    return re.sub(r"[`*_~|>]", " ", value)


def _open_pdf_reader(
    raw: bytes,
    warnings: list[str],
) -> tuple[PdfReader | None, object, str]:
    try:
        reader = PdfReader(BytesIO(raw))
    except Exception as exc:  # pypdf exposes several backend-specific exceptions
        detail = str(exc)
        warnings.append(f"PDF 元数据读取失败: {detail}")
        return None, {}, detail
    try:
        metadata = reader.metadata or {}
    except Exception as exc:
        detail = str(exc)
        warnings.append(f"PDF 元数据读取失败: {detail}")
        metadata = {}
    return reader, metadata, ""


def _extract_pypdf_text(reader: PdfReader) -> tuple[str, list[str]]:
    pages: list[str] = []
    errors: list[str] = []
    try:
        page_count = len(reader.pages)
    except Exception as exc:
        return "", [f"读取页面列表失败: {exc}"]
    for index in range(page_count):
        try:
            value = (reader.pages[index].extract_text() or "").strip()
        except Exception as exc:
            errors.append(f"第 {index + 1} 页: {exc}")
            continue
        if value:
            pages.append(value)
    return "\n\n".join(pages), errors


def _attempt(
    engine: str,
    version: str,
    started: float,
    outcome: str,
    detail: str = "",
) -> dict[str, object]:
    result: dict[str, object] = {
        "engine": engine,
        "version": version,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "outcome": outcome,
    }
    if detail:
        result["detail"] = detail
    return result


def extract_pdf_document(
    raw: bytes,
    url: str,
    fallback_title: str = "",
    content_type_rules: ContentTypeRules | None = None,
    *,
    pdf_parser_mode: str = "anydoc_first",
) -> ExtractedDocument:
    total_started = time.perf_counter()
    mode = pdf_parser_mode if pdf_parser_mode in {"anydoc_first", "pypdf_only"} else "anydoc_first"
    warnings: list[str] = []
    attempts: list[dict[str, object]] = []
    reader, metadata, reader_error = _open_pdf_reader(raw, warnings)
    metadata_get = getattr(metadata, "get", lambda _key, _default=None: _default)
    title = str(metadata_get("/Title") or fallback_title).strip()
    date_raw = str(metadata_get("/CreationDate") or "")
    parsed = _parse_pdf_creation_date(date_raw)

    text = ""
    headings: list[str] = []
    status = "failed"
    final_engine = "none"
    final_version = ""
    final_source = "none"
    fallback_reason = ""
    fallback_detail = ""
    ocr_pages: list[int] = []
    page_count: int | None = None
    should_use_pypdf = mode == "pypdf_only"
    needs_ocr = False

    if mode == "anydoc_first":
        anydoc_version = _distribution_version("firecrawl-anydoc")
        started = time.perf_counter()
        try:
            anydoc = _load_anydoc()
        except Exception as exc:
            attempts.append(_attempt("anydoc", anydoc_version, started, "unavailable", str(exc)))
            fallback_reason = "anydoc_unavailable"
            fallback_detail = f"AnyDoc 无法加载: {exc}"
            warnings.append(f"{fallback_detail}，已回退 pypdf")
            should_use_pypdf = True
        else:
            started = time.perf_counter()
            try:
                with _PDF_PARSE_SEMAPHORE:
                    markdown = anydoc.to_markdown_bytes(raw, "pdf", ocr="reject")
            except (anydoc.EncryptedError, anydoc.ResourceLimitError) as exc:
                outcome = (
                    "encrypted"
                    if isinstance(exc, anydoc.EncryptedError)
                    else "resource_limit"
                )
                attempts.append(_attempt("anydoc", anydoc_version, started, outcome, str(exc)))
                warnings.append(f"AnyDoc 安全拒绝，未尝试兼容解析: {exc}")
            except anydoc.NeedsOcrError as exc:
                attempts.append(_attempt("anydoc", anydoc_version, started, "needs_ocr", str(exc)))
                ocr_pages = [int(value) for value in (getattr(exc, "pages", []) or [])]
                raw_page_count = getattr(exc, "page_count", None)
                page_count = int(raw_page_count) if raw_page_count is not None else None
                fallback_reason = "anydoc_needs_ocr"
                fallback_detail = "AnyDoc 检测到需要 OCR 的页面"
                should_use_pypdf = True
                needs_ocr = True
            except (anydoc.MalformedError, anydoc.MissingPartError, anydoc.UnsupportedError) as exc:
                if isinstance(exc, anydoc.MalformedError):
                    code = "anydoc_malformed"
                elif isinstance(exc, anydoc.MissingPartError):
                    code = "anydoc_missing_part"
                else:
                    code = "anydoc_unsupported"
                attempts.append(_attempt("anydoc", anydoc_version, started, code, str(exc)))
                fallback_reason = code
                fallback_detail = f"AnyDoc 转换失败: {exc}"
                warnings.append(f"{fallback_detail}，已回退 pypdf")
                should_use_pypdf = True
            except Exception as exc:
                attempts.append(_attempt("anydoc", anydoc_version, started, "error", str(exc)))
                fallback_reason = "anydoc_runtime_error"
                fallback_detail = f"AnyDoc 运行失败: {exc}"
                warnings.append(f"{fallback_detail}，已回退 pypdf")
                should_use_pypdf = True
            else:
                if len(markdown.strip()) >= PDF_MIN_TEXT_CHARS:
                    attempts.append(_attempt("anydoc", anydoc_version, started, "success"))
                    text = markdown.strip()
                    headings = _markdown_headings(text)
                    status = "complete"
                    final_engine = "anydoc"
                    final_version = anydoc_version
                    final_source = "anydoc_markdown"
                else:
                    attempts.append(
                        _attempt(
                            "anydoc",
                            anydoc_version,
                            started,
                            "too_short",
                            f"仅 {len(markdown.strip())} 字符",
                        )
                    )
                    fallback_reason = "anydoc_text_too_short"
                    fallback_detail = f"AnyDoc 正文少于 {PDF_MIN_TEXT_CHARS} 字符"
                    warnings.append(f"{fallback_detail}，已回退 pypdf")
                    should_use_pypdf = True

    if should_use_pypdf:
        pypdf_version = _distribution_version("pypdf")
        started = time.perf_counter()
        if reader is None:
            attempts.append(_attempt("pypdf", pypdf_version, started, "failed", reader_error))
            warnings.append(f"PDF 兼容解析失败: {reader_error or '无法打开 PDF'}")
        else:
            pypdf_text, page_errors = _extract_pypdf_text(reader)
            if page_errors:
                detail = "; ".join(page_errors[:5])
                if len(page_errors) > 5:
                    detail += f"；另有 {len(page_errors) - 5} 页失败"
                warnings.append(f"pypdf 部分页面提取失败: {detail}")
            if len(pypdf_text) >= PDF_MIN_TEXT_CHARS:
                attempts.append(
                    _attempt(
                        "pypdf",
                        pypdf_version,
                        started,
                        "partial" if page_errors else "success",
                    )
                )
                text = pypdf_text
                final_engine = "pypdf"
                final_version = pypdf_version
                final_source = "pypdf_text"
                status = "partial_pdf" if needs_ocr or page_errors else "complete"
                if needs_ocr:
                    warnings.append("已保留 pypdf 可提取文本；包含 OCR 页面，转人工复核")
            else:
                outcome = "failed" if page_errors and not pypdf_text else "too_short"
                attempts.append(
                    _attempt(
                        "pypdf",
                        pypdf_version,
                        started,
                        outcome,
                        f"仅 {len(pypdf_text)} 字符",
                    )
                )
                text = pypdf_text
                final_engine = "pypdf" if pypdf_text else "none"
                final_version = pypdf_version if pypdf_text else ""
                final_source = "pypdf_text" if pypdf_text else "none"
                status = "failed" if outcome == "failed" else "scanned_pdf"
                warnings.append("PDF 可提取文本过少，疑似扫描件，需要人工处理")

    length_text = _markdown_text_for_length(text) if final_source == "anydoc_markdown" else text
    language, word_count, chinese_count = detect_language_and_length(length_text)
    content_type, evidence = detect_content_type(None, url, title, text, content_type_rules)
    audit: dict[str, object] = {
        "mode": mode,
        "final_engine": final_engine,
        "final_engine_version": final_version,
        "final_source": final_source,
        "duration_ms": round((time.perf_counter() - total_started) * 1000, 3),
        "fallback_reason": fallback_reason,
        "fallback_detail": fallback_detail,
        "ocr_pages": ocr_pages,
        "page_count": page_count,
        "attempts": attempts,
    }
    return ExtractedDocument(
        title=title,
        text=text,
        language=language,
        english_word_count=word_count,
        chinese_char_count=chinese_count,
        published_date=parsed,
        date_status="metadata_only" if parsed else "missing",
        date_evidence=f"PDF CreationDate: {date_raw}" if parsed else "PDF 元数据无可确认发布日期",
        content_type=content_type,
        content_type_evidence=evidence,
        extraction_status=status,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        warnings=warnings,
        headings=headings,
        extraction_audit=audit,
    )


def _card_date(
    anchor: Tag,
    preferred_selectors: tuple[str, ...] = (),
    container: Tag | None = None,
) -> tuple[date | None, str]:
    if preferred_selectors:
        current: Tag | None = anchor
        for _ in range(8):
            parent = current.parent if current else None
            if not isinstance(parent, Tag):
                break
            for selector in preferred_selectors:
                for node in parent.select(selector):
                    raw = str(node.get("datetime") or node.get_text(" ", strip=True)).strip()
                    if parsed := _parse_date(raw):
                        return parsed, f"栏目卡片自定义选择器 {selector}: {raw}"
            current = parent
    container = container or _card_container(anchor)
    if container is None:
        return None, ""
    for node in container.select("time[datetime], .publication-item-date, .publication-date, .post-date, .entry-date, [class*='date']"):
        raw = str(node.get("datetime") or node.get_text(" ", strip=True)).strip()
        match = DATE_TEXT_RE.search(raw)
        candidate = match.group(0) if match else raw
        parsed = _parse_date(candidate)
        if parsed:
            return parsed, f"栏目卡片 {node.name}: {candidate}"
    match = DATE_TEXT_RE.search(container.get_text(" ", strip=True))
    if match and (parsed := _parse_date(match.group(0))):
        return parsed, f"栏目卡片文本: {match.group(0)}"
    return None, ""


def _semantic_tokens(node: Tag) -> set[str]:
    values = [node.get("id", ""), *node.get("class", [])]
    return {
        token
        for value in values
        for token in re.split(r"[^a-z0-9]+", str(value).casefold())
        if token
    }


def _card_container(anchor: Tag, preferred_container_ids: set[int] | None = None) -> Tag | None:
    current: Tag = anchor
    for _ in range(8):
        if preferred_container_ids and id(current) in preferred_container_ids:
            return current
        parent = current.parent
        if not isinstance(parent, Tag):
            return None
        classes = {str(value).lower() for value in parent.get("class", [])}
        semantic = _semantic_tokens(parent)
        leaf_tokens = {
            "author",
            "body",
            "content",
            "date",
            "description",
            "footer",
            "header",
            "image",
            "link",
            "meta",
            "summary",
            "text",
            "thumbnail",
            "title",
        }
        semantic_container = bool(
            parent.name in {"div", "li"}
            and semantic.intersection({"card", "entry", "item", "post", "publication", "result"})
            and not semantic.intersection(leaf_tokens)
        )
        if (
            parent.name == "article"
            or classes.intersection(
                {
                    "publication-item", "featured-publication", "elcano-card", "post-card", "result-item",
                    "search-result", "content-card", "archive-item",
                }
            )
            or semantic_container
            or parent.name == "li"
            or parent.get("role") == "listitem"
        ):
            return parent
        current = parent
    return None


def _selected_texts(container: Tag, selectors: tuple[str, ...]) -> list[str]:
    values: list[str] = []

    def allowed(node: Tag) -> bool:
        current: Tag | None = node
        while current is not None and current is not container:
            if current.name in {"nav", "footer", "aside", "form"}:
                return False
            tokens = _semantic_tokens(current)
            if tokens.intersection(
                {"filter", "filters", "menu", "navigation", "recommended", "related", "sidebar"}
            ):
                return False
            current = current.parent if isinstance(current.parent, Tag) else None
        return True

    def add(value: object, *, slug: bool = False) -> None:
        text = re.sub(r"\s+", " ", str(value).strip())
        if slug:
            text = text.replace("-", " ")
        if text and len(text) <= 100 and len(text.split()) <= 10:
            values.append(text)

    def read_node(node: Tag, *, trusted: bool, include_text: bool = True) -> None:
        if not allowed(node):
            return
        if include_text:
            add(node.get_text(" ", strip=True))
        for attribute in (
            "data-publication-types",
            "data-publication_types",
            "data-publication-type",
            "data-type",
            "data-format",
            "data-category",
            "data-genre",
        ):
            if value := node.get(attribute):
                add(value, slug=not trusted)
        if trusted and (value := node.get("aria-label")):
            add(value)

    def is_topic_taxonomy(node: Tag) -> bool:
        links = [node] if node.name == "a" and node.get("href") else node.select("a[href]")
        if not links:
            return False
        topic_markers = ("/tag/", "/tags/", "/topic/", "/topics/", "/tema/", "/research-area/")
        paths = [urlsplit(str(link.get("href", ""))).path.casefold() for link in links]
        return bool(paths) and all(any(marker in path for marker in topic_markers) for path in paths)

    for selector in selectors:
        nodes = []
        try:
            if container.css.match(selector):
                nodes.append(container)
        except Exception:
            pass
        nodes.extend(container.select(selector))
        for node in nodes:
            read_node(node, trusted=True)

    attribute_selector = ", ".join(
        f"[{attribute}]"
        for attribute in (
            "data-publication-types",
            "data-publication_types",
            "data-publication-type",
            "data-type",
            "data-format",
            "data-category",
            "data-genre",
        )
    )
    for node in [container, *container.select(attribute_selector)]:
        if isinstance(node, Tag):
            read_node(node, trusted=False, include_text=False)

    for node in container.find_all(True):
        tokens = _semantic_tokens(node)
        raw_semantics = " ".join(
            [str(node.get("id", "")), *(str(value) for value in node.get("class", []))]
        ).casefold()
        technical_drupal_type = "field--type-" in raw_semantics and not re.search(
            r"(?:publication|content)[-_]+type", raw_semantics
        )
        if (
            tokens.intersection({"type", "format", "category", "genre"})
            and not technical_drupal_type
            and not is_topic_taxonomy(node)
            and not tokens.intersection(
            {"filter", "menu", "related", "recommended", "tag", "topic"}
            )
        ):
            href = str(node.get("href", ""))
            if node.name == "a" and re.search(r"[?&](?:types?|formats?|categories?|genres?)=", href, re.I):
                continue
            read_node(node, trusted=False)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = re.sub(r"\s+", " ", value).strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _card_authors(container: Tag, selectors: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    for attribute in ("data-authors", "data-author"):
        raw = str(container.get(attribute, "")).strip()
        if not raw:
            continue
        values = re.split(r"\s*[;|]\s*", raw)
        authors = _dedupe_authors(values)
        if authors:
            return tuple(authors), f"栏目卡片作者属性 {attribute}: " + "；".join(authors)
    candidate_selectors = selectors or (
        "[rel='author']",
        ".byline .author",
        ".author-name",
        "[class*='author'] a",
    )
    for selector in candidate_selectors:
        values = [
            value
            for node in container.select(selector)
            for value in _author_values_from_node(node)
        ]
        authors = _dedupe_authors(values)
        if authors:
            return tuple(authors), f"栏目卡片作者选择器 {selector}: " + "；".join(authors)
    return (), ""


def _card_is_featured(anchor: Tag) -> bool:
    current: Tag | None = anchor
    for _ in range(8):
        if current is None:
            break
        classes = " ".join(current.get("class", [])).lower()
        if any(marker in classes for marker in ("featured", "pinned", "sticky")):
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def discover_content_cards(
    raw: bytes,
    base_url: str,
    preferred_selectors: tuple[str, ...] = (),
    preferred_date_selectors: tuple[str, ...] = (),
    content_type_rules: ContentTypeRules | None = None,
    preferred_container_selectors: tuple[str, ...] = (),
    preferred_content_type_selectors: tuple[str, ...] = (),
    preferred_author_selectors: tuple[str, ...] = (),
) -> list[ListingCard]:
    soup = BeautifulSoup(raw, "lxml")
    base_host = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")
    found: dict[str, ListingCard] = {}
    preferred_container_ids = {
        id(node)
        for selector in preferred_container_selectors
        for node in soup.select(selector)
    }

    def add_anchor(anchor: Tag, *, preferred: bool, position: int) -> None:
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return
        rel = {str(value).lower() for value in (anchor.get("rel") or [])}
        classes = {str(value).lower() for value in (anchor.get("class") or [])}
        aria_label = str(anchor.get("aria-label") or "").strip().lower()
        if (
            rel & {"next", "prev", "previous"}
            or classes & {"next", "prev", "previous"}
            or aria_label in {"next", "previous", "next page", "previous page"}
            or anchor.find_parent(class_=re.compile(r"(?:^|\s)(?:pagination|pager)(?:\s|$)", re.I))
        ):
            return
        absolute = normalize_fetch_url(urljoin(base_url, href))
        parts = urlsplit(absolute)
        base_parts = urlsplit(base_url)
        target_host = (parts.hostname or "").lower().removeprefix("www.")
        if parts.scheme not in {"http", "https"} or target_host != base_host:
            return
        if parts.path.rstrip("/") == base_parts.path.rstrip("/"):
            return
        path = parts.path.lower()
        if any(fragment in path for fragment in LISTING_PATH_FRAGMENTS):
            return
        anchor_text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not anchor_text or len(anchor_text) < 4:
            image = anchor.find("img", alt=True)
            anchor_text = str(image.get("alt", "")).strip() if image else anchor_text
        if not anchor_text or len(anchor_text) < 4:
            return
        class_text = " ".join(anchor.get("class", []))
        context = f"{path} {anchor_text} {class_text}".lower()
        is_pdf = path.endswith(".pdf")
        container = _card_container(anchor, preferred_container_ids)
        current = anchor.parent if isinstance(anchor.parent, Tag) else None
        while current is not None and current is not container:
            if current.name in {"nav", "footer", "aside", "form"}:
                return
            if _semantic_tokens(current).intersection(
                {"filter", "filters", "menu", "navigation", "recommended", "related", "sidebar"}
            ):
                return
            current = current.parent if isinstance(current.parent, Tag) else None
        labels = _selected_texts(container, preferred_content_type_selectors) if container else []
        card_content_type, type_evidence, _ = resolve_content_type(
            absolute, labels, content_type_rules
        )
        type_evidence_items = collect_content_type_evidence(
            absolute,
            labels,
            content_type_rules,
            label_location="listing_card",
        )
        hinted = card_content_type != "unknown" or any(hint in context for hint in CONTENT_PATH_HINTS)
        if not preferred and container is None:
            return
        if not preferred and not is_pdf and not hinted and len(anchor_text) < 12:
            return
        canonical = canonicalize_url(absolute)
        if canonical and canonical.rstrip("/") != canonicalize_url(base_url).rstrip("/"):
            published_date, date_evidence = _card_date(anchor, preferred_date_selectors, container)
            authors, author_evidence = _card_authors(container, preferred_author_selectors) if container else ((), "")
            found.setdefault(
                canonical,
                ListingCard(
                    absolute,
                    canonical,
                    anchor_text,
                    published_date,
                    date_evidence,
                    "" if card_content_type == "unknown" else card_content_type,
                    type_evidence,
                    authors,
                    author_evidence,
                    position,
                    _card_is_featured(anchor),
                    tuple(labels),
                    tuple(type_evidence_items),
                ),
            )

    for selector in preferred_selectors:
        for position, anchor in enumerate(soup.select(selector), 1):
            add_anchor(anchor, preferred=True, position=position)
    if preferred_selectors:
        return list(found.values())
    generic_selector = (
        "main article a[href], main [role='listitem'] a[href], "
        "main .publication-item a[href], main .post-card a[href], "
        "main .result-item a[href], main .search-result a[href], "
        "main [class*='publication'] h2 a[href], main [class*='publication'] h3 a[href], "
        ".entry-list article a[href], .listing article a[href], .publications article a[href], "
        "[class~='results'] article a[href], [class~='archive'] article a[href]"
    )
    for position, anchor in enumerate(soup.select(generic_selector), 1):
        add_anchor(anchor, preferred=False, position=position)
    return list(found.values())


def discover_content_links(
    raw: bytes,
    base_url: str,
    preferred_selectors: tuple[str, ...] = (),
    content_type_rules: ContentTypeRules | None = None,
) -> list[tuple[str, str, str]]:
    return [
        (card.fetch_url, card.canonical_url, card.title)
        for card in discover_content_cards(
            raw,
            base_url,
            preferred_selectors,
            content_type_rules=content_type_rules,
        )
    ]


def discover_pdf_links(raw: bytes, base_url: str, title: str = "") -> list[DiscoveredAttachment]:
    soup = BeautifulSoup(raw, "lxml")
    found: dict[str, DiscoveredAttachment] = {}
    title_words = {
        word for word in re.findall(r"[a-z0-9]+", title.lower()) if len(word) >= 4
    }

    def add(raw_url: str, link_text: str, *, in_main: bool, source: str) -> None:
        if not raw_url.strip():
            return
        absolute = normalize_fetch_url(urljoin(base_url, raw_url.strip()))
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"}:
            return
        decoded = unquote(f"{parts.path}?{parts.query}").lower()
        text_value = re.sub(r"\s+", " ", link_text).strip()
        context = re.sub(r"[_-]+", " ", f"{decoded} {text_value}".lower())
        direct_pdf = ".pdf" in decoded
        download_terms = (
            "view pdf",
            "read pdf",
            "full text",
            "download publication",
            "download the publication",
            "download pdf",
            "download report",
            "download brief",
            "full report",
            "full publication",
            "read publication",
        )
        document_type_terms = (
            "policy brief",
            "working paper",
            "research paper",
        )
        primary_terms = download_terms + document_type_terms
        semantic = any(term in context for term in download_terms) or (
            direct_pdf and any(term in context for term in document_type_terms)
        )
        if not direct_pdf and not semantic and source != "citation_pdf_url":
            return
        role = "other"
        score = 20 if in_main else 0
        if direct_pdf:
            score += 30
        if source == "citation_pdf_url":
            score += 100
        if semantic:
            score += 80
            role = "primary"
        negative_terms = ("supplement", "supplementary", "appendix", "annex", "dataset", "data used")
        if any(term in context for term in negative_terms):
            score -= 80
            role = "supplementary"
        if text_value.lower() in {"here", "download", "pdf"}:
            score -= 15
        url_words = set(re.findall(r"[a-z0-9]+", decoded))
        if title_words:
            score += min(20, 4 * len(title_words & url_words))
        if role == "other" and score >= 70:
            role = "primary"
        canonical = canonicalize_url(absolute)
        candidate = DiscoveredAttachment(absolute, canonical, text_value, role, score)
        current = found.get(canonical)
        if current is None or candidate.score > current.score:
            found[canonical] = candidate

    for meta in soup.select('meta[name="citation_pdf_url"][content], meta[property="citation_pdf_url"][content]'):
        add(str(meta.get("content", "")), "citation_pdf_url", in_main=True, source="citation_pdf_url")

    scopes = soup.select(
        "article, main, #ceps-page, #content, .site-main, [itemprop='articleBody'], "
        ".publication-content, .entry-content, .post-content"
    )
    if not scopes and soup.body:
        scopes = [soup.body]
    seen_nodes: set[int] = set()
    for scope in scopes:
        for unwanted in scope.select("footer, nav, aside, .related, .recommended, .more-publications"):
            unwanted.extract()
        selector = "a[href], a[data-href], a[data-url], iframe[src], iframe[data-src], object[data], embed[src]"
        for node in scope.select(selector):
            marker = id(node)
            if marker in seen_nodes:
                continue
            seen_nodes.add(marker)
            value = (
                node.get("href")
                or node.get("data-href")
                or node.get("data-url")
                or node.get("src")
                or node.get("data-src")
                or node.get("data")
                or ""
            )
            add(str(value), node.get_text(" ", strip=True), in_main=True, source=node.name)
    return sorted(found.values(), key=lambda item: (-item.score, item.url))


def discover_listing_dates(raw: bytes) -> list[date]:
    soup = BeautifulSoup(raw, "lxml")
    values: set[date] = set()
    nodes = soup.select(
        ".publication-item, .elcano-card, .ut-post-card, article, "
        ".listing > *, .publications > *"
    )
    for node in nodes:
        for match in DATE_TEXT_RE.findall(node.get_text(" ", strip=True)):
            parsed = _parse_date(match)
            if parsed:
                values.add(parsed)
    return sorted(values, reverse=True)


def next_page_url(
    raw: bytes,
    current_url: str,
    listing_root_url: str | None = None,
    *,
    strict_listing_path: bool = False,
) -> str | None:
    soup = BeautifulSoup(raw, "lxml")

    def eligible(raw_href: str) -> str | None:
        value = normalize_fetch_url(urljoin(current_url, raw_href))
        if canonicalize_url(value) == canonicalize_url(current_url):
            return None
        if not strict_listing_path or not listing_root_url:
            return value
        root = urlsplit(normalize_fetch_url(listing_root_url))
        candidate = urlsplit(value)
        root_path = root.path if root.path.endswith("/") else f"{root.path}/"
        candidate_path = candidate.path if candidate.path.endswith("/") else f"{candidate.path}/"
        if candidate.hostname == root.hostname and (
            candidate_path == root_path or candidate_path.startswith(root_path)
        ):
            return value
        return None

    for selector in (
        "a.next",
        ".pagination-next a",
        '.pagination a[rel="next"]',
        ".pagination .next a",
        'a[aria-label*="Next" i]',
        'a[rel="next"]',
        'link[rel="next"]',
    ):
        node = soup.select_one(selector)
        if node and node.get("href"):
            if value := eligible(str(node.get("href"))):
                return value
    current_parts = urlsplit(current_url)
    current_query = dict(parse_qsl(current_parts.query, keep_blank_values=True))
    current_page = int(current_query.get("p", "1")) if current_query.get("p", "1").isdigit() else 1
    scripted_pages: list[int] = []
    for node in soup.select(".pagination a[href*='submit_filters']"):
        match = re.search(r"submit_filters\((\d+)\)", str(node.get("href", "")))
        if match and int(match.group(1)) > current_page:
            scripted_pages.append(int(match.group(1)))
    if scripted_pages:
        current_query.update({"q": "1", "p": str(min(scripted_pages))})
        path = current_parts.path if current_parts.path.endswith("/") else f"{current_parts.path}/"
        return urlunsplit(
            (
                current_parts.scheme,
                current_parts.netloc,
                path,
                urlencode(sorted(current_query.items())),
                "",
            )
        )
    return None
