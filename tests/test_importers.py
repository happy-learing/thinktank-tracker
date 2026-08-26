from __future__ import annotations

import io
from pathlib import Path

from docx import Document

from thinktank_tracker.importers import parse_docx_sources
from thinktank_tracker.source_service import parse_sources_json


def test_parses_current_sample() -> None:
    sample = Path(__file__).resolve().parents[2] / "追踪智库.docx"
    sources = parse_docx_sources(sample)

    assert [source.name for source in sources] == [
        "欧盟安全事务研究所",
        "欧洲政策研究中心",
        "马腾斯欧洲事务研究中心",
        "艾格蒙特国际关系研究所",
        "欧洲政策中心",
        "西班牙埃尔卡诺皇家研究所",
    ]
    assert sources[0].adapter_key == "euiss"
    assert str(sources[1].publication_url).rstrip("/") == "https://www.ceps.eu/ceps-publications"


def test_parses_heading_inline_urls_and_table_rows() -> None:
    document = Document()
    document.add_heading("欧洲智库", level=1)
    document.add_paragraph("1. Example Institute")
    document.add_paragraph("https://example.com/")
    document.add_paragraph("2. Inline Centre https://inline.example.org/research")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Table Institute"
    table.cell(0, 1).text = "https://table.example.net/publications"
    buffer = io.BytesIO()
    document.save(buffer)

    sources = parse_docx_sources(buffer.getvalue())

    assert len(sources) == 3
    assert all(source.group_name == "欧洲智库" for source in sources)
    assert sources[1].name == "Inline Centre"
    assert sources[2].name == "Table Institute"


def test_deduplicates_urls() -> None:
    document = Document()
    document.add_paragraph("One")
    document.add_paragraph("https://example.com/?utm_source=test")
    document.add_paragraph("Duplicate")
    document.add_paragraph("https://example.com/")
    buffer = io.BytesIO()
    document.save(buffer)

    assert len(parse_docx_sources(buffer.getvalue())) == 1


def test_sources_json_preserves_enabled_and_rejects_future_schema() -> None:
    rows = parse_sources_json(
        '{"schema_version":1,"sources":[{"name":"One","homepage_url":"https://example.org","enabled":false}]}'
    )
    assert rows[0].enabled is False
    try:
        parse_sources_json('{"schema_version":99,"sources":[]}')
    except ValueError as exc:
        assert "更高版本" in str(exc)
    else:
        raise AssertionError("future schema must be rejected")
