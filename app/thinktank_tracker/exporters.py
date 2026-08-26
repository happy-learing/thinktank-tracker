from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .constants import CATEGORY_LABELS, MICRO_CATEGORY_ORDER, CandidateStatus, CoverageStatus
from .models import Assessment, Candidate, CrawlRun, Source, SourceRun
from .paths import PATHS


NAVY = "1F4E79"
TEAL = "0F6B78"
LIGHT_BLUE = "D9EAF3"
LIGHT_GRAY = "F2F4F7"
MID_GRAY = "6B7280"
WHITE = "FFFFFF"
RED = "B42318"


def _set_run_font(run, latin: str = "Calibri", east_asia: str = "Microsoft YaHei", size: int = 10) -> None:
    run.font.name = latin
    run.font.size = Pt(size)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), east_asia)


def _set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _add_page_number(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = paragraph.add_run("第 ")
    _set_run_font(run, size=9)
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run._r.addnext(fld)
    tail = paragraph.add_run(" 页")
    _set_run_font(tail, size=9)
    run.font.color.rgb = RGBColor.from_string(MID_GRAY)
    tail.font.color.rgb = RGBColor.from_string(MID_GRAY)


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    relation_id = paragraph.part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relation_id)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), TEAL)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    properties.extend([color, underline])
    run.append(properties)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _configure_document(document: Document) -> None:
    section = document.sections[0]
    section.top_margin = Cm(2.1)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.3)
    section.right_margin = Cm(2.1)
    styles = document.styles
    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25
    for name, size, color in (("Title", 28, NAVY), ("Heading 1", 18, NAVY), ("Heading 2", 13, TEAL)):
        style = styles[name]
        style.font.name = "Calibri"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string(color)
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.space_before = Pt(12)
        style.paragraph_format.space_after = Pt(6)
    if "Article Title" not in [style.name for style in styles]:
        article_style = styles.add_style("Article Title", WD_STYLE_TYPE.PARAGRAPH)
        article_style.font.name = "Calibri"
        article_style.font.size = Pt(11.5)
        article_style.font.bold = True
        article_style.font.color.rgb = RGBColor.from_string(NAVY)
        article_style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        article_style.paragraph_format.keep_with_next = True
        article_style.paragraph_format.space_before = Pt(9)
        article_style.paragraph_format.space_after = Pt(3)
    header = section.header
    table = header.add_table(rows=1, cols=2, width=Inches(6.8))
    table.columns[0].width = Inches(4.8)
    table.columns[1].width = Inches(2.0)
    left = table.cell(0, 0).paragraphs[0]
    left_run = left.add_run("THINK TANK RESEARCH TRACKER")
    _set_run_font(left_run, size=8)
    left_run.bold = True
    left_run.font.color.rgb = RGBColor.from_string(NAVY)
    right = table.cell(0, 1).paragraphs[0]
    right.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    right_run = right.add_run("选题参考")
    _set_run_font(right_run, size=8)
    right_run.font.color.rgb = RGBColor.from_string(MID_GRAY)
    _add_page_number(section.footer.paragraphs[0])


def _article_title(candidate: Candidate) -> str:
    review = candidate.review
    english = (review.title_en or candidate.title_en or candidate.title_original).strip()
    chinese = (review.title_zh or candidate.title_zh).strip()
    if english and chinese and english.casefold() != chinese.casefold():
        return f"{english}\n{chinese}"
    return english or chinese or "（标题待补）"


def export_report(session: Session, run_id: int) -> Path:
    run = session.get(CrawlRun, run_id)
    if not run:
        raise ValueError("检索任务不存在")
    candidates = session.scalars(
        select(Candidate)
        .where(
            Candidate.run_id == run_id,
            Candidate.status == CandidateStatus.APPROVED,
            Candidate.date_status == "confirmed",
        )
        .options(selectinload(Candidate.source), selectinload(Candidate.review))
        .order_by(Candidate.published_date, Candidate.id)
    ).all()
    document = Document()
    _configure_document(document)
    title = document.add_paragraph(style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run_text = title.add_run("智库选题参考")
    _set_run_font(run_text, size=28)
    subtitle = document.add_paragraph()
    subtitle_run = subtitle.add_run(f"{run.start_date.isoformat()} - {run.end_date.isoformat()}")
    _set_run_font(subtitle_run, size=15)
    subtitle_run.bold = True
    subtitle_run.font.color.rgb = RGBColor.from_string(TEAL)
    meta = document.add_table(rows=2, cols=3)
    meta.style = "Table Grid"
    values = [
        ("发布日期范围", f"{run.start_date:%Y年%m月%d日} 至 {run.end_date:%Y年%m月%d日}"),
        ("已批准成果", str(len(candidates))),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    for index, (label, value) in enumerate(values):
        cell = meta.cell(0, index)
        _set_cell_shading(cell, NAVY)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(label)
        _set_run_font(r, size=9)
        r.bold = True
        r.font.color.rgb = RGBColor.from_string(WHITE)
        p = meta.cell(1, index).paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(value)
        _set_run_font(r, size=9)
    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def add_category(category: str, heading: str) -> None:
        document.add_heading(heading, level=1 if category == "macro" else 2)
        rows = [item for item in candidates if item.review and item.review.primary_category == category]
        if not rows:
            p = document.add_paragraph("本期暂无人工批准且日期确认的符合项。")
            p.runs[0].italic = True
            p.runs[0].font.color.rgb = RGBColor.from_string(MID_GRAY)
            return
        for index, candidate in enumerate(rows, 1):
            paragraph = document.add_paragraph(style="Article Title")
            paragraph.add_run(f"{index}. {_article_title(candidate)}")
            table = document.add_table(rows=0, cols=2)
            table.style = "Table Grid"
            fields = [
                ("来源智库", candidate.source.name),
                ("发布日期", candidate.published_date.isoformat()),
                ("体裁", candidate.review.content_type or candidate.content_type),
                ("核心观点", candidate.review.core_claim_zh),
                ("主题归类", CATEGORY_LABELS.get(category, category)),
            ]
            secondary = json.loads(candidate.review.secondary_categories_json or "[]")
            if secondary:
                fields.append(("副标签", "、".join(CATEGORY_LABELS.get(item, item) for item in secondary)))
            for label, value in fields:
                cells = table.add_row().cells
                _set_cell_shading(cells[0], LIGHT_BLUE)
                label_run = cells[0].paragraphs[0].add_run(label)
                _set_run_font(label_run, size=9)
                label_run.bold = True
                value_run = cells[1].paragraphs[0].add_run(str(value))
                _set_run_font(value_run, size=9)
            cells = table.add_row().cells
            _set_cell_shading(cells[0], LIGHT_BLUE)
            label_run = cells[0].paragraphs[0].add_run("原文链接")
            _set_run_font(label_run, size=9)
            label_run.bold = True
            _add_hyperlink(cells[1].paragraphs[0], candidate.original_url, candidate.original_url)
            document.add_paragraph()

    add_category("macro", "一、宏观")
    document.add_heading("二、微观", level=1)
    for number, category in enumerate(MICRO_CATEGORY_ORDER, 1):
        add_category(category, f"2.{number} {CATEGORY_LABELS[category]}")

    approved_source_ids = {candidate.source_id for candidate in candidates}
    no_match_sources = session.execute(
        select(Source, SourceRun)
        .join(SourceRun, SourceRun.source_id == Source.id)
        .where(SourceRun.run_id == run_id, Source.id.not_in(approved_source_ids or {-1}))
        .order_by(Source.id)
    ).all()
    document.add_heading("三、来源覆盖说明", level=1)
    if no_match_sources:
        for source, source_run in no_match_sources:
            if source_run.status == CoverageStatus.COMPLETE:
                statement = f"该智库在 {run.start_date:%Y.%m.%d}-{run.end_date:%m.%d} 期间暂无符合条件的深度研究。"
            else:
                statement = f"本期覆盖状态为“{source_run.status}”，不能判定暂无符合内容；详见独立审计表。"
            document.add_paragraph(f"{source.name}：{statement}", style="List Bullet")
    else:
        document.add_paragraph("所有启用来源均至少有一篇人工批准成果。")
    PATHS.exports.mkdir(parents=True, exist_ok=True)
    path = PATHS.exports / f"智库选题参考_{run.start_date.isoformat()}_{run.end_date.isoformat()}.docx"
    document.save(path)
    return path


def _sheet(ws, headers: list[str], rows: list[list[object]]) -> None:
    ws.append(headers)
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    thin = Side(style="thin", color="D0D5DD")
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.font = Font(name="Microsoft YaHei", size=10, bold=True, color=WHITE)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Microsoft YaHei", size=9)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = Border(bottom=thin)
    ws.row_dimensions[1].height = 28
    for index, header in enumerate(headers, 1):
        values = [str(ws.cell(row=row, column=index).value or "") for row in range(1, min(ws.max_row, 100) + 1)]
        width = min(max(len(header) + 2, max((len(value) for value in values), default=0) + 2), 55)
        ws.column_dimensions[get_column_letter(index)].width = max(10, width)


def export_audit_workbook(session: Session, run_id: int) -> Path:
    run = session.get(CrawlRun, run_id)
    if not run:
        raise ValueError("检索任务不存在")
    source_runs = session.scalars(
        select(SourceRun)
        .where(SourceRun.run_id == run_id)
        .options(selectinload(SourceRun.source))
        .order_by(SourceRun.id)
    ).all()
    candidates = session.scalars(
        select(Candidate)
        .where(Candidate.run_id == run_id)
        .options(selectinload(Candidate.source), selectinload(Candidate.assessment), selectinload(Candidate.review))
        .order_by(Candidate.source_id, Candidate.id)
    ).all()
    wb = Workbook()
    overview = wb.active
    overview.title = "概览"
    summary_rows = [
        ["日期范围", f"{run.start_date.isoformat()} 至 {run.end_date.isoformat()}"],
        ["任务状态", run.status],
        ["来源数量", len(source_runs)],
        ["候选总数", len(candidates)],
        ["人工批准", sum(item.status == CandidateStatus.APPROVED for item in candidates)],
        ["待人工处理", sum(item.status == CandidateStatus.NEEDS_REVIEW for item in candidates)],
        ["覆盖不完整/失败", sum(item.status != CoverageStatus.COMPLETE for item in source_runs)],
        ["生成时间", datetime.now().isoformat(timespec="seconds")],
    ]
    _sheet(overview, ["指标", "值"], summary_rows)
    coverage_rows = [
        [
            row.source.name,
            row.source.publication_url,
            row.status,
            row.pages_scanned,
            row.links_discovered,
            row.earliest_date.isoformat() if row.earliest_date else "",
            row.latest_date.isoformat() if row.latest_date else "",
            row.stop_reason,
            row.error_message,
        ]
        for row in source_runs
    ]
    _sheet(
        wb.create_sheet("来源覆盖"),
        ["智库", "研究栏目", "覆盖状态", "访问页数", "发现链接", "最早日期", "最晚日期", "停止依据", "错误"],
        coverage_rows,
    )
    no_match_rows = []
    for row in source_runs:
        approved = sum(item.source_id == row.source_id and item.status == CandidateStatus.APPROVED for item in candidates)
        if not approved:
            statement = (
                f"该智库在{run.start_date:%Y.%m.%d}-{run.end_date:%m.%d}期间暂无符合条件的深度研究"
                if row.status == CoverageStatus.COMPLETE
                else "覆盖不完整，不能判定暂无符合内容"
            )
            no_match_rows.append([row.source.name, row.status, statement, row.error_message])
    _sheet(wb.create_sheet("无符合项智库"), ["智库", "覆盖状态", "结论", "说明"], no_match_rows)
    date_rows = [
        [item.source.name, item.title_original, item.date_status, item.date_evidence, item.original_url]
        for item in candidates
        if item.date_status != "confirmed"
    ]
    _sheet(wb.create_sheet("日期待确认"), ["智库", "标题", "日期状态", "日期证据", "原文链接"], date_rows)
    failure_rows = [
        [item.source.name, item.title_original, item.extraction_status, item.hard_filter_reason, item.original_url]
        for item in candidates
        if item.extraction_status not in {"complete"}
    ]
    _sheet(wb.create_sheet("抓取失败"), ["智库", "标题", "提取状态", "问题", "原文链接"], failure_rows)
    candidate_rows = []
    for item in candidates:
        candidate_rows.append(
            [
                item.id,
                item.source.name,
                item.title_original,
                item.published_date.isoformat() if item.published_date else "",
                item.date_status,
                item.content_type,
                item.language,
                item.english_word_count,
                item.chinese_char_count,
                item.status,
                item.hard_filter_reason,
                item.assessment.decision if item.assessment else "",
                item.assessment.primary_category if item.assessment else "",
                item.review.decision if item.review else "",
                item.review.notes if item.review else "",
                item.content_hash,
                item.original_url,
            ]
        )
    _sheet(
        wb.create_sheet("全部候选"),
        [
            "ID", "智库", "标题", "发布日期", "日期状态", "体裁", "语言", "英文词数", "中文字符数",
            "当前状态", "硬过滤/异常原因", "模型决定", "模型主分类", "人工决定", "人工备注", "正文哈希", "原文链接",
        ],
        candidate_rows,
    )
    model_rows = [
        [
            item.source.name,
            item.title_original,
            item.assessment.provider_model,
            item.assessment.input_chars,
            item.assessment.confidence,
            item.assessment.error_message,
        ]
        for item in candidates
        if item.assessment
    ]
    _sheet(wb.create_sheet("模型调用统计"), ["智库", "标题", "模型", "输入字符", "置信度", "错误"], model_rows)
    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False
    PATHS.exports.mkdir(parents=True, exist_ok=True)
    path = PATHS.exports / f"智库检索审计_{run.start_date.isoformat()}_{run.end_date.isoformat()}.xlsx"
    wb.save(path)
    return path
