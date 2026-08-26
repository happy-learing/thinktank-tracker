from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import urlsplit

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from .schemas import ImportedSource
from .source_catalog import known_source_for
from .url_utils import canonicalize_url, extract_urls


NUMBER_PREFIX_RE = re.compile(r"^\s*(?:[（(]?\d+[）).、．]\s*|[-–—•]\s*)")


@dataclass
class Block:
    text: str
    urls: list[str]
    style_name: str = ""


def _iter_blocks(parent: DocumentObject | _Cell) -> Iterator[Paragraph | Table]:
    parent_element = parent.element.body if isinstance(parent, DocumentObject) else parent._tc
    for child in parent_element.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _paragraph_text_and_urls(paragraph: Paragraph) -> tuple[str, list[str]]:
    text = "".join(node.text or "" for node in paragraph._p.xpath(".//w:t"))
    urls = extract_urls(text)
    for hyperlink in paragraph._p.xpath(".//w:hyperlink"):
        rel_id = hyperlink.get(qn("r:id"))
        if not rel_id:
            continue
        relation = paragraph.part.rels.get(rel_id)
        if relation and relation.target_ref.startswith(("http://", "https://")):
            urls.append(relation.target_ref)
    return text.strip(), list(dict.fromkeys(urls))


def _document_blocks(document: DocumentObject) -> Iterator[Block]:
    for item in _iter_blocks(document):
        if isinstance(item, Paragraph):
            text, urls = _paragraph_text_and_urls(item)
            style = item.style.name if item.style is not None else ""
            yield Block(text=text, urls=urls, style_name=style)
            continue
        for row in item.rows:
            cell_blocks: list[Block] = []
            for cell in row.cells:
                text_parts: list[str] = []
                urls: list[str] = []
                for paragraph in cell.paragraphs:
                    text, paragraph_urls = _paragraph_text_and_urls(paragraph)
                    if text:
                        text_parts.append(text)
                    urls.extend(paragraph_urls)
                cell_blocks.append(
                    Block(
                        text=" ".join(text_parts).strip(),
                        urls=list(dict.fromkeys(urls)),
                        style_name="table-cell",
                    )
                )
            row_urls = [url for block in cell_blocks for url in block.urls]
            row_text = " | ".join(block.text for block in cell_blocks if block.text)
            yield Block(text=row_text, urls=list(dict.fromkeys(row_urls)), style_name="table-row")


def _clean_name(text: str, urls: list[str]) -> str:
    result = text
    for url in urls:
        result = result.replace(url, " ")
    result = NUMBER_PREFIX_RE.sub("", result)
    result = result.replace("|", " ").strip(" \t:-–—")
    return re.sub(r"\s+", " ", result)


def _fallback_name(url: str) -> str:
    hostname = (urlsplit(url).hostname or url).removeprefix("www.")
    return hostname


def parse_docx_sources(source: str | Path | bytes | BinaryIO) -> list[ImportedSource]:
    if isinstance(source, bytes):
        document = Document(io.BytesIO(source))
    else:
        document = Document(source)

    current_group = ""
    pending_name = ""
    imported: list[ImportedSource] = []
    seen: set[str] = set()

    for block in _document_blocks(document):
        text = block.text.strip()
        urls = block.urls or extract_urls(text)
        is_heading = block.style_name.lower().startswith("heading") or block.style_name.startswith("标题")
        if is_heading and text and not urls:
            current_group = _clean_name(text, [])
            pending_name = ""
            continue
        if not urls:
            if text:
                pending_name = _clean_name(text, [])
            continue

        inline_name = _clean_name(text, urls)
        name = inline_name or pending_name
        for url in urls:
            canonical = canonicalize_url(url)
            if canonical in seen:
                continue
            seen.add(canonical)
            known = known_source_for(url)
            imported.append(
                ImportedSource(
                    name=name or _fallback_name(url),
                    group_name=current_group,
                    homepage_url=url,
                    publication_url=known.publication_url if known else url,
                    adapter_key=known.adapter_key if known else "generic",
                    ordered_by_date=known.ordered_by_date if known else True,
                )
            )
        pending_name = ""

    return imported

