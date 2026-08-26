from __future__ import annotations

from dataclasses import replace
from datetime import date

from docx import Document
from openpyxl import load_workbook

from thinktank_tracker.constants import CandidateStatus, Decision
from thinktank_tracker.models import Candidate, CrawlRun, ReviewDecision, Source, SourceRun
from thinktank_tracker.paths import PATHS
import thinktank_tracker.exporters as exporters


def _seed(db_session):
    source = Source(name="示例智库", homepage_url="https://example.org/", publication_url="https://example.org/publications")
    empty_source = Source(name="无符合项智库", homepage_url="https://empty.example/", publication_url="https://empty.example/research")
    incomplete_source = Source(name="覆盖不完整智库", homepage_url="https://partial.example/", publication_url="https://partial.example/research")
    run = CrawlRun(start_date=date(2026, 8, 2), end_date=date(2026, 8, 26), status="completed")
    db_session.add_all([source, empty_source, incomplete_source, run])
    db_session.flush()
    db_session.add_all([
        SourceRun(run_id=run.id, source_id=source.id, status="complete", pages_scanned=2, links_discovered=1, stop_reason="已到栏目末页"),
        SourceRun(run_id=run.id, source_id=empty_source.id, status="complete", pages_scanned=1, links_discovered=0, stop_reason="已到栏目末页"),
        SourceRun(run_id=run.id, source_id=incomplete_source.id, status="incomplete", pages_scanned=25, links_discovered=0, stop_reason="达到安全上限"),
    ])
    item = Candidate(
        run_id=run.id, source_id=source.id, canonical_url="https://example.org/report",
        original_url="https://example.org/report", title_original="Strategic Competition",
        title_en="Strategic Competition", title_zh="战略竞争", published_date=date(2026, 8, 26),
        date_status="confirmed", date_evidence="meta", content_type="report", language="en",
        english_word_count=2200, extraction_status="complete", content_hash="a" * 64,
        extracted_text="full text", status=CandidateStatus.APPROVED,
    )
    db_session.add(item)
    db_session.flush()
    db_session.add(ReviewDecision(
        candidate_id=item.id, decision=Decision.INCLUDE, primary_category="macro",
        secondary_categories_json='["technology_competition"]', title_en="Strategic Competition",
        title_zh="战略竞争", content_type="report", core_claim_zh="竞争正在重塑国际秩序。",
    ))
    db_session.commit()
    return run


def test_docx_and_xlsx_exports_have_required_structure(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(exporters, "PATHS", replace(PATHS, exports=tmp_path))
    run = _seed(db_session)
    docx_path = exporters.export_report(db_session, run.id)
    xlsx_path = exporters.export_audit_workbook(db_session, run.id)
    assert docx_path.exists() and xlsx_path.exists()
    document = Document(docx_path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "智库选题参考" in text
    assert "Strategic Competition" in text
    assert "无符合项智库" in text
    assert "覆盖不完整智库" in text
    assert "不能判定暂无符合内容" in text
    workbook = load_workbook(xlsx_path, read_only=True)
    assert workbook.sheetnames == ["概览", "来源覆盖", "无符合项智库", "日期待确认", "抓取失败", "全部候选", "模型调用统计"]
    assert workbook["全部候选"].max_row == 2
