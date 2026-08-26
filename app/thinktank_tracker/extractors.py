from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from pypdf import PdfReader

from .constants import ALLOWED_DEEP_TYPES, EXCLUDED_TYPES
from .url_utils import canonicalize_url


DATE_KEYS = {
    "datepublished",
    "datecreated",
    "publicationdate",
    "publishdate",
    "article:published_time",
    "og:published_time",
    "dc.date",
    "dcterms.date",
}
CONTENT_PATH_HINTS = (
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
    r"|\b(?:0?[1-9]|[12]\d|3[01])\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:19|20)\d{2}\b"
    r"|\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:0?[1-9]|[12]\d|3[01]),?\s+(?:19|20)\d{2}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DateFact:
    value: date
    source: str
    raw: str


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
    extraction_status: str = "complete"
    content_hash: str = ""
    warnings: list[str] = field(default_factory=list)


def _parse_date(raw: str) -> date | None:
    value = raw.strip()
    if not value or not re.search(r"(?:19|20)\d{2}", value):
        return None
    try:
        parsed = date_parser.parse(value, fuzzy=True, dayfirst=False)
    except (ValueError, OverflowError, TypeError):
        return None
    if not 1990 <= parsed.year <= datetime.now().year + 1:
        return None
    return parsed.date()


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


def extract_date_facts(soup: BeautifulSoup) -> list[DateFact]:
    facts: list[DateFact] = []
    for item in _json_ld_objects(soup):
        for key, raw in item.items():
            if key.lower() not in DATE_KEYS or not isinstance(raw, str):
                continue
            parsed = _parse_date(raw)
            if parsed:
                facts.append(DateFact(parsed, f"JSON-LD {key}", raw))
    for meta in soup.select("meta[content]"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if key not in DATE_KEYS and not any(part in key for part in ("publish", "publication_date")):
            continue
        raw = str(meta.get("content", ""))
        parsed = _parse_date(raw)
        if parsed:
            facts.append(DateFact(parsed, f"meta {key}", raw))
    for node in soup.select("article time[datetime], main time[datetime], time[datetime]")[:5]:
        raw = str(node.get("datetime", ""))
        parsed = _parse_date(raw)
        if parsed:
            facts.append(DateFact(parsed, "time[datetime]", raw))
    for selector in (
        "article time:not([datetime])",
        "main time:not([datetime])",
        ".publication-date",
        ".published-date",
        ".post-date",
        ".entry-date",
        '[class*="date-publish"]',
    ):
        for node in soup.select(selector)[:3]:
            raw_text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            match = DATE_TEXT_RE.search(raw_text)
            if not match:
                continue
            parsed = _parse_date(match.group(0))
            if parsed:
                facts.append(DateFact(parsed, f"visible {selector}", match.group(0)))
    return facts


def resolve_date(facts: list[DateFact]) -> tuple[date | None, str, str]:
    if not facts:
        return None, "missing", "页面元数据和 time 元素均未提供可确认日期"
    grouped: dict[date, list[DateFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.value, []).append(fact)
    evidence = "; ".join(f"{fact.source}: {fact.raw}" for fact in facts[:8])
    if len(grouped) > 1:
        return None, "conflict", evidence
    return facts[0].value, "confirmed", evidence


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


def detect_language_and_length(text: str) -> tuple[str, int, int]:
    chinese_chars = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_words = len(re.findall(r"\b[A-Za-z][A-Za-z'’-]*\b", text))
    language = "zh" if chinese_chars >= 300 and chinese_chars > latin_words * 0.25 else "en"
    if chinese_chars < 100 and latin_words < 100:
        language = "unknown"
    return language, latin_words, chinese_chars


def detect_content_type(soup: BeautifulSoup | None, url: str, title: str, text: str) -> tuple[str, str]:
    evidence_parts = [url, title]
    if soup is not None:
        for selector in (
            'meta[property="article:section"]',
            'meta[name="citation_document_type"]',
            ".content-type",
            ".publication-type",
            ".type",
        ):
            node = soup.select_one(selector)
            if node:
                evidence_parts.append(str(node.get("content") or node.get_text(" ", strip=True)))
    haystack = " ".join(evidence_parts).lower().replace("_", "-")
    ordered_types = sorted(ALLOWED_DEEP_TYPES | EXCLUDED_TYPES, key=len, reverse=True)
    aliases = {
        "policy-brief": "policy brief",
        "working-paper": "working paper",
        "research-paper": "research paper",
        "press-release": "press release",
        "op ed": "op-ed",
    }
    for raw_type in ordered_types:
        variants = {raw_type, raw_type.replace(" ", "-"), raw_type.replace("-", " ")}
        if any(re.search(rf"(?<![a-z]){re.escape(variant)}(?![a-z])", haystack) for variant in variants):
            normalized = aliases.get(raw_type, raw_type)
            return normalized, "; ".join(part for part in evidence_parts if part)[:1000]
    # A downloadable long PDF is usually a paper even if the site omits a type label.
    if urlsplit(url).path.lower().endswith(".pdf") and len(text) >= 8000:
        return "paper", "PDF 全文且页面未提供更具体体裁"
    return "unknown", "; ".join(part for part in evidence_parts if part)[:1000]


def extract_html_document(raw: bytes, url: str) -> ExtractedDocument:
    soup = BeautifulSoup(raw, "lxml")
    title = extract_title(soup)
    text = extract_html_text(soup)
    published, date_status, date_evidence = resolve_date(extract_date_facts(soup))
    language, word_count, chinese_count = detect_language_and_length(text)
    content_type, type_evidence = detect_content_type(soup, url, title, text)
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
        extraction_status=status,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        warnings=warnings,
    )


def extract_pdf_document(raw: bytes, url: str, fallback_title: str = "") -> ExtractedDocument:
    warnings: list[str] = []
    try:
        reader = PdfReader(BytesIO(raw))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        text = "\n\n".join(page for page in pages if page)
        metadata = reader.metadata or {}
    except Exception as exc:  # pypdf raises several backend-specific exceptions
        return ExtractedDocument(
            title=fallback_title,
            extraction_status="failed",
            warnings=[f"PDF 解析失败: {exc}"],
        )
    title = str(metadata.get("/Title") or fallback_title).strip()
    date_raw = str(metadata.get("/CreationDate") or "")
    parsed = _parse_date(date_raw[2:] if date_raw.startswith("D:") else date_raw)
    language, word_count, chinese_count = detect_language_and_length(text)
    if len(text) < 500:
        status = "scanned_pdf"
        warnings.append("PDF 可提取文本过少，疑似扫描件，需要人工处理")
    else:
        status = "complete"
    content_type, evidence = detect_content_type(None, url, title, text)
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
    )


def discover_content_links(
    raw: bytes,
    base_url: str,
    preferred_selectors: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    soup = BeautifulSoup(raw, "lxml")
    base_host = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")
    found: dict[str, str] = {}

    def add_anchor(anchor: Tag, *, preferred: bool) -> None:
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        target_host = (parts.hostname or "").lower().removeprefix("www.")
        if parts.scheme not in {"http", "https"} or target_host != base_host:
            return
        path = parts.path.lower()
        if any(fragment in path for fragment in LISTING_PATH_FRAGMENTS):
            return
        anchor_text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        class_text = " ".join(anchor.get("class", []))
        context = f"{path} {anchor_text} {class_text}".lower()
        is_pdf = path.endswith(".pdf")
        hinted = any(hint in context for hint in CONTENT_PATH_HINTS)
        if not preferred and not is_pdf and not hinted:
            return
        canonical = canonicalize_url(absolute)
        if canonical and canonical.rstrip("/") != canonicalize_url(base_url).rstrip("/"):
            found.setdefault(canonical, anchor_text)

    for selector in preferred_selectors:
        for anchor in soup.select(selector):
            add_anchor(anchor, preferred=True)
    for anchor in soup.select("main a[href], article a[href], .publications a[href], .listing a[href], body a[href]"):
        add_anchor(anchor, preferred=False)
    return list(found.items())


def discover_pdf_links(raw: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(raw, "lxml")
    found: list[str] = []
    for anchor in soup.select('article a[href], main a[href], a[type="application/pdf"]'):
        absolute = canonicalize_url(urljoin(base_url, str(anchor.get("href", ""))))
        path = urlsplit(absolute).path.lower()
        context = f"{path} {anchor.get_text(' ', strip=True)}".lower()
        if path.endswith(".pdf") or "download pdf" in context or "full report" in context:
            if absolute not in found:
                found.append(absolute)
    return found


def discover_listing_dates(raw: bytes) -> list[date]:
    soup = BeautifulSoup(raw, "lxml")
    facts = extract_date_facts(soup)
    values = {fact.value for fact in facts}
    for node in soup.select("main, article, .listing, .publications"):
        for match in DATE_TEXT_RE.findall(node.get_text(" ", strip=True)):
            parsed = _parse_date(match)
            if parsed:
                values.add(parsed)
    return sorted(values, reverse=True)


def next_page_url(raw: bytes, current_url: str) -> str | None:
    soup = BeautifulSoup(raw, "lxml")
    for selector in (
        'link[rel="next"]',
        'a[rel="next"]',
        "a.next",
        ".pagination-next a",
        ".pagination .next a",
        'a[aria-label*="Next" i]',
    ):
        node = soup.select_one(selector)
        if node and node.get("href"):
            value = canonicalize_url(
                urljoin(current_url, str(node.get("href"))),
                preserve_trailing_slash=True,
            )
            if canonicalize_url(value) != canonicalize_url(current_url):
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
