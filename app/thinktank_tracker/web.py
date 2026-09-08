from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import desc, func, select
from sqlalchemy.orm import selectinload

from . import __version__
from .config import (
    MODEL_PROVIDER_PRESETS,
    PDF_PARSER_MODES,
    AppConfig,
    get_api_key,
    load_config,
    parse_content_type_rules,
    save_config,
    set_api_key,
)
from .configuration_service import (
    active_configurations,
    create_configuration,
    delete_configuration,
    duplicate_configuration,
    ensure_default_configuration,
    get_active_configuration,
    rename_configuration,
    restore_configuration,
)
from .constants import (
    CATEGORY_LABELS,
    CandidateStatus,
    ContentKindStatus,
    CoverageStatus,
    DateScope,
    Decision,
    LengthStatus,
    ReviewStatus,
    RunStatus,
    TopicStatus,
)
from .adapters import merge_selectors, resolve_effective_adapter
from .crawler import WebCrawler
from .database import SessionLocal, initialize_database, session_scope
from .exporters import export_audit_workbook, export_report
from .example_files import create_json_example, create_word_example
from .importers import parse_docx_sources
from .extractors import discover_content_cards, next_page_url
from .maintenance import (
    clean_expired_cache,
    create_database_backup,
    purge_history_with_backup,
    run_diagnostics,
)
from .migrations import CURRENT_SCHEMA_VERSION
from .llm import OpenAICompatibleProvider
from .link_test_service import snapshot_entry, test_entries
from .logging_setup import configure_logging
from .models import (
    Candidate,
    CandidateAttachment,
    CrawlRun,
    ListingPageAudit,
    ModelSuggestion,
    Source,
    SourceConfiguration,
    SourceEntry,
    SourceEntryRun,
    SourceRun,
    utcnow,
)
from .paths import PATHS
from .review_service import save_review
from .run_service import (
    create_run,
    mark_interrupted_runs,
    reclassify_run_content_types,
    run_is_active,
    start_run_in_background,
)
from .schemas import ImportedConfiguration, ImportedSource, ImportedSourceEntry
from .source_service import (
    parse_configurations_json,
    parse_sources_json,
    save_sources_json,
    upsert_sources,
)
from .url_utils import (
    canonicalize_url,
    normalize_content_type_label,
    normalize_fetch_url,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
TEMPLATE_ROOT = PACKAGE_ROOT / "templates"
STATIC_ROOT = PACKAGE_ROOT / "static"
PREVIEW_PATH = PATHS.config / "source-import-preview.json"


STATUS_LABELS = {
    RunStatus.QUEUED: "等待运行",
    RunStatus.RUNNING: "运行中",
    RunStatus.COMPLETED: "已完成",
    RunStatus.COMPLETED_WITH_ISSUES: "完成但有异常",
    RunStatus.INTERRUPTED: "已中断",
    RunStatus.FAILED: "失败",
    CoverageStatus.PENDING: "待检查",
    CoverageStatus.COMPLETE: "覆盖完整",
    CoverageStatus.INCOMPLETE: "覆盖不完整",
    CoverageStatus.FAILED: "访问失败",
    CandidateStatus.DISCOVERED: "待模型判断",
    CandidateStatus.HARD_EXCLUDED: "硬规则排除",
    CandidateStatus.NEEDS_REVIEW: "待人工处理",
    CandidateStatus.MODEL_INCLUDED: "模型建议纳入",
    CandidateStatus.MODEL_EXCLUDED: "模型建议排除",
    CandidateStatus.APPROVED: "人工批准",
    CandidateStatus.REJECTED: "人工排除",
    DateScope.IN_WINDOW: "日期范围内",
    DateScope.AFTER_END: "晚于结束日",
    DateScope.BEFORE_START: "早于起始日",
    DateScope.DATE_UNKNOWN: "日期待确认",
    ContentKindStatus.ELIGIBLE: "体裁合格",
    ContentKindStatus.EXCLUDED: "体裁明确排除",
    LengthStatus.PASS: "达到篇幅门槛",
    LengthStatus.BELOW: "篇幅不足",
    LengthStatus.BORDERLINE: "篇幅临界",
    LengthStatus.UNKNOWN: "篇幅未知",
    TopicStatus.NOT_REQUIRED: "无需模型",
}

DATE_SCOPE_LABELS = {
    DateScope.IN_WINDOW: "日期范围内",
    DateScope.AFTER_END: "晚于结束日",
    DateScope.BEFORE_START: "早于起始日",
    DateScope.DATE_UNKNOWN: "日期待确认",
}
CONTENT_KIND_LABELS = {
    ContentKindStatus.ELIGIBLE: "体裁合格",
    ContentKindStatus.EXCLUDED: "体裁明确排除",
    ContentKindStatus.NEEDS_REVIEW: "体裁待确认",
}
LENGTH_LABELS = {
    LengthStatus.PASS: "达到门槛",
    LengthStatus.BELOW: "篇幅不足",
    LengthStatus.BORDERLINE: "篇幅临界",
    LengthStatus.UNKNOWN: "篇幅未知",
}
TOPIC_LABELS = {
    TopicStatus.PENDING: "等待模型",
    TopicStatus.NOT_REQUIRED: "无需模型",
    TopicStatus.MODEL_INCLUDED: "模型建议纳入",
    TopicStatus.MODEL_EXCLUDED: "模型建议排除",
    TopicStatus.NEEDS_REVIEW: "主题待人工",
}
REVIEW_LABELS = {
    ReviewStatus.PENDING: "未审核",
    ReviewStatus.APPROVED: "已批准",
    ReviewStatus.REJECTED: "已拒绝",
}

SUGGESTION_FIELD_LABELS = {
    "decision": "纳入倾向",
    "suggested_date": "发布日期",
    "date_evidence": "日期证据",
    "content_type": "体裁",
    "primary_category": "主分类",
    "secondary_categories": "副标签",
    "title_en": "英文标题",
    "title_zh": "中文标题",
    "core_claim_zh": "核心观点",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    initialize_database()
    mark_interrupted_runs()
    yield


app = FastAPI(title="智库研究追踪", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")
templates = Jinja2Templates(directory=TEMPLATE_ROOT)
templates.env.globals.update(
    app_version=__version__,
    status_label=lambda value: STATUS_LABELS.get(value, value),
    category_label=lambda value: CATEGORY_LABELS.get(value, value),
    categories=CATEGORY_LABELS,
    suggestion_field_label=lambda value: SUGGESTION_FIELD_LABELS.get(value, value),
    model_provider_presets=MODEL_PROVIDER_PRESETS,
    date_scope_label=lambda value: DATE_SCOPE_LABELS.get(value, value),
    content_kind_label=lambda value: CONTENT_KIND_LABELS.get(value, value),
    length_label=lambda value: LENGTH_LABELS.get(value, value),
    topic_label=lambda value: TOPIC_LABELS.get(value, value),
    review_label=lambda value: REVIEW_LABELS.get(value, value),
)
templates.env.filters["from_json"] = lambda value: json.loads(value or "[]")


def _render(request: Request, name: str, **context) -> HTMLResponse:
    page_config = context.get("config")
    config = page_config if isinstance(page_config, AppConfig) else load_config()
    context.setdefault("content_types", config.content_types)
    return templates.TemplateResponse(request=request, name=name, context=context)


def _redirect(path: str, message: str = "", level: str = "success") -> RedirectResponse:
    from urllib.parse import urlencode

    separator = "&" if "?" in path else "?"
    query = f"{separator}{urlencode({'message': message, 'level': level})}" if message else ""
    return RedirectResponse(f"{path}{query}", status_code=303)


def _sources_url(configuration_id: int | None = None) -> str:
    return f"/sources?configuration_id={configuration_id}" if configuration_id else "/sources"


def _rule_lines(value: str) -> list[str]:
    return [line.strip() for line in value.replace("\r", "").split("\n") if line.strip()]


def _json_lines(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _default_range(session) -> tuple[date, date]:
    today = date.today()
    last = session.scalar(
        select(func.max(CrawlRun.end_date))
        .join(SourceEntryRun, SourceEntryRun.run_id == CrawlRun.id)
        .where(
            CrawlRun.deleted_at.is_(None),
            SourceEntryRun.status == CoverageStatus.COMPLETE,
        )
    )
    next_unsearched = (last + timedelta(days=1)) if last else today
    return (min(next_unsearched, today), today)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with SessionLocal() as session:
        ensure_default_configuration(session)
        session.commit()
        configurations = active_configurations(session)
        selected_configuration = configurations[0]
        for configuration in configurations:
            configuration.active_source_count = int(
                session.scalar(
                    select(func.count(func.distinct(Source.id)))
                    .join(SourceEntry, SourceEntry.source_id == Source.id)
                    .where(
                        Source.configuration_id == configuration.id,
                        Source.enabled.is_(True),
                        Source.deleted_at.is_(None),
                        SourceEntry.enabled.is_(True),
                        SourceEntry.deleted_at.is_(None),
                    )
                )
                or 0
            )
            configuration.active_entry_count = int(
                session.scalar(
                    select(func.count(SourceEntry.id))
                    .join(Source, Source.id == SourceEntry.source_id)
                    .where(
                        Source.configuration_id == configuration.id,
                        Source.enabled.is_(True),
                        Source.deleted_at.is_(None),
                        SourceEntry.enabled.is_(True),
                        SourceEntry.deleted_at.is_(None),
                    )
                )
                or 0
            )
        source_count = selected_configuration.active_source_count
        pending_count = session.scalar(
            select(func.count(Candidate.id))
            .join(CrawlRun, CrawlRun.id == Candidate.run_id)
            .where(
                CrawlRun.deleted_at.is_(None),
                Candidate.status.in_([CandidateStatus.NEEDS_REVIEW, CandidateStatus.MODEL_INCLUDED]),
            )
        ) or 0
        approved_count = session.scalar(
            select(func.count(Candidate.id))
            .join(CrawlRun, CrawlRun.id == Candidate.run_id)
            .where(CrawlRun.deleted_at.is_(None), Candidate.status == CandidateStatus.APPROVED)
        ) or 0
        recent_runs = session.scalars(
            select(CrawlRun)
            .where(CrawlRun.deleted_at.is_(None))
            .options(selectinload(CrawlRun.source_runs))
            .order_by(desc(CrawlRun.created_at))
            .limit(8)
        ).all()
        start_date, end_date = _default_range(session)
        return _render(
            request,
            "dashboard.html",
            page="dashboard",
            source_count=source_count,
            pending_count=pending_count,
            approved_count=approved_count,
            recent_runs=recent_runs,
            configurations=configurations,
            selected_configuration=selected_configuration,
            has_runnable_configuration=any(
                row.active_entry_count > 0 for row in configurations
            ),
            default_start=start_date,
            default_end=end_date,
        )


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, configuration_id: int | None = None):
    with SessionLocal() as session:
        ensure_default_configuration(session)
        session.commit()
        configurations = active_configurations(session)
        selected_configuration = next(
            (row for row in configurations if row.id == configuration_id),
            configurations[0],
        )
        sources = session.scalars(
            select(Source)
            .where(
                Source.configuration_id == selected_configuration.id,
                Source.deleted_at.is_(None),
            )
            .options(selectinload(Source.entries))
            .order_by(Source.id)
        ).all()
        for source in sources:
            source.visible_entries = [entry for entry in source.entries if entry.deleted_at is None]
        return _render(
            request,
            "sources.html",
            page="sources",
            sources=sources,
            configurations=configurations,
            selected_configuration=selected_configuration,
        )


@app.post("/configurations")
def add_configuration(name: Annotated[str, Form()] = ""):
    try:
        with session_scope() as session:
            row = create_configuration(session, name)
            configuration_id = row.id
    except ValueError as exc:
        return _redirect("/sources", str(exc), "error")
    return _redirect(_sources_url(configuration_id), "配置已创建")


@app.post("/configurations/{configuration_id}/rename")
def update_configuration_name(
    configuration_id: int, name: Annotated[str, Form()] = ""
):
    try:
        with session_scope() as session:
            rename_configuration(session, configuration_id, name)
    except ValueError as exc:
        return _redirect(_sources_url(configuration_id), str(exc), "error")
    return _redirect(_sources_url(configuration_id), "配置名称已更新")


@app.post("/configurations/{configuration_id}/duplicate")
def copy_configuration(
    configuration_id: int, name: Annotated[str, Form()] = ""
):
    try:
        with session_scope() as session:
            row = duplicate_configuration(session, configuration_id, name)
            duplicate_id = row.id
    except ValueError as exc:
        return _redirect(_sources_url(configuration_id), str(exc), "error")
    return _redirect(_sources_url(duplicate_id), "配置及其来源已完整复制")


@app.post("/configurations/{configuration_id}/delete")
def remove_configuration(configuration_id: int):
    try:
        with session_scope() as session:
            delete_configuration(session, configuration_id)
            remaining = active_configurations(session)
            target_id = remaining[0].id if remaining else None
    except ValueError as exc:
        return _redirect(_sources_url(configuration_id), str(exc), "warning")
    return _redirect(_sources_url(target_id), "配置已移入回收站")


@app.get("/sources/examples/word")
def source_word_example():
    path = create_word_example(PATHS.exports / "智库来源导入示例.docx")
    return FileResponse(path, filename=path.name)


@app.get("/sources/examples/json")
def source_json_example():
    path = create_json_example(PATHS.exports / "sources.example.json")
    return FileResponse(path, filename=path.name, media_type="application/json")


@app.get("/sources/{source_id}/entries/{entry_id}/preview", response_class=HTMLResponse)
def preview_entry_discovery(request: Request, source_id: int, entry_id: int):
    with SessionLocal() as session:
        entry = session.get(SourceEntry, entry_id)
        if (
            not entry
            or entry.source_id != source_id
            or entry.deleted_at is not None
            or entry.source.deleted_at is not None
        ):
            raise HTTPException(404, "检索入口不存在")
        snapshot = {
            "url": entry.url,
            "adapter_key": entry.adapter_key,
            "listing_selectors": _json_lines(entry.listing_selectors_json),
            "container_selectors": _json_lines(entry.container_selectors_json),
            "content_type_selectors": _json_lines(entry.content_type_selectors_json),
            "date_selectors": _json_lines(entry.date_selectors_json),
            "allowed": _json_lines(entry.allowed_path_patterns_json),
            "excluded": _json_lines(entry.excluded_path_patterns_json),
        }
    config = load_config()
    crawler = WebCrawler(config)
    try:
        resource = crawler.fetch(snapshot["url"])
        effective_adapter_key, adapter = resolve_effective_adapter(
            snapshot["adapter_key"], snapshot["url"]
        )
        selectors = merge_selectors(
            tuple(snapshot["listing_selectors"]),
            adapter.listing_link_selectors if adapter else (),
        )
        containers = merge_selectors(
            tuple(snapshot["container_selectors"]),
            adapter.listing_container_selectors if adapter else (),
        )
        type_selectors = merge_selectors(
            tuple(snapshot["content_type_selectors"]),
            adapter.listing_content_type_selectors if adapter else (),
        )
        cards = discover_content_cards(
            resource.body,
            resource.url,
            selectors,
            tuple(snapshot["date_selectors"]),
            config.content_type_rules,
            containers,
            type_selectors,
            adapter.listing_author_selectors if adapter else (),
        )
        rows = []
        for card in cards:
            path = urlsplit(card.fetch_url).path.lower()
            reason = ""
            try:
                if snapshot["excluded"] and any(re.search(pattern, path) for pattern in snapshot["excluded"]):
                    reason = "命中排除路径"
                elif snapshot["allowed"] and not any(re.search(pattern, path) for pattern in snapshot["allowed"]):
                    reason = "未命中允许路径"
                elif not snapshot["allowed"] and adapter and not adapter.accepts(card.fetch_url):
                    reason = "不符合适配器成果路径"
            except re.error as exc:
                raise ValueError(f"路径规则不是有效正则表达式：{exc}") from exc
            rows.append({"card": card, "accepted": not reason, "reason": reason or "正式内容卡片"})
        warning = ""
        if not cards:
            warning = "入口页未识别到正式内容卡片。已知适配器不会退化为扫描整个页面，请检查选择器或网站结构。"
        accepted_count = sum(row["accepted"] for row in rows)
        return _render(
            request,
            "entry_preview.html",
            page="sources",
            entry=entry,
            final_url=resource.url,
            effective_adapter_key=effective_adapter_key,
            collection_engine="内置 Chromium" if getattr(resource, "rendered", False) else "HTTP",
            next_page_url=next_page_url(
                resource.body,
                resource.url,
                snapshot["url"],
                strict_listing_path=bool(adapter and adapter.strict_listing_path),
            ),
            rows=rows,
            warning=warning,
            discovered_count=len(cards),
            accepted_count=accepted_count,
        )
    except Exception as exc:
        return _render(
            request,
            "entry_preview.html",
            page="sources",
            entry=entry,
            final_url="",
            effective_adapter_key=snapshot["adapter_key"],
            collection_engine="未知",
            next_page_url="",
            rows=[],
            warning=f"预览失败：{exc}",
            discovered_count=0,
            accepted_count=0,
        )
    finally:
        crawler.close()


@app.post("/configurations/{configuration_id}/sources/{source_id}/test")
def test_source_links(configuration_id: int, source_id: int):
    with SessionLocal() as session:
        source = session.scalar(
            select(Source)
            .where(
                Source.id == source_id,
                Source.configuration_id == configuration_id,
                Source.deleted_at.is_(None),
            )
            .options(selectinload(Source.entries))
        )
        if source is None:
            raise HTTPException(404, "当前配置中不存在该智库")
        snapshots = [
            snapshot_entry(entry)
            for entry in source.entries
            if entry.enabled and entry.deleted_at is None
        ]
    if not snapshots:
        return _redirect(
            _sources_url(configuration_id), "该智库没有启用的检索入口", "warning"
        )
    results = test_entries(snapshots, load_config())
    tested_at = utcnow()
    with session_scope() as session:
        for result in results:
            entry = session.get(SourceEntry, result.entry_id)
            if entry is None or entry.source_id != source_id:
                continue
            entry.last_tested_at = tested_at
            entry.last_test_status = result.status
            entry.last_test_final_url = result.final_url
            entry.last_test_http_status = result.http_status
            entry.last_test_discovered_count = result.discovered_count
            entry.last_test_accepted_count = result.accepted_count
            entry.last_test_message = result.message[:2000]
    failed = sum(row.status == "failed" for row in results)
    warnings = sum(row.status == "warning" for row in results)
    if failed:
        level = "error"
        message = f"已测试 {len(results)} 个入口：{failed} 个失败，{warnings} 个警告"
    elif warnings:
        level = "warning"
        message = f"已测试 {len(results)} 个入口：连接均成功，{warnings} 个未识别到可接收文章"
    else:
        level = "success"
        message = f"已测试 {len(results)} 个入口，全部连接并识别成功"
    return _redirect(_sources_url(configuration_id), message, level)


@app.post("/sources/import")
async def preview_source_import(
    file: UploadFile = File(...),
    configuration_id: Annotated[int | None, Form()] = None,
):
    raw = await file.read()
    try:
        with SessionLocal() as session:
            target = (
                get_active_configuration(session, configuration_id)
                if configuration_id is not None
                else ensure_default_configuration(session)
            )
            target_id, target_name = target.id, target.name
        is_json = (file.filename or "").lower().endswith(".json")
        is_package = False
        if is_json:
            raw_payload = json.loads(raw.decode("utf-8-sig"))
            is_package = isinstance(raw_payload, dict) and "configurations" in raw_payload
            configurations = parse_configurations_json(raw)
            if not is_package:
                configurations = [
                    ImportedConfiguration(
                        name=target_name,
                        sources=[
                            source
                            for configuration in configurations
                            for source in configuration.sources
                        ],
                    )
                ]
        else:
            configurations = [
                ImportedConfiguration(name=target_name, sources=parse_docx_sources(raw))
            ]
    except (ValueError, ValidationError, json.JSONDecodeError, KeyError) as exc:
        return _redirect(_sources_url(configuration_id), f"导入失败：{exc}", "error")
    source_count = sum(len(row.sources) for row in configurations)
    if not source_count:
        return _redirect(_sources_url(configuration_id), "文件中未识别到智库链接", "error")
    PATHS.ensure()
    PREVIEW_PATH.write_text(
        json.dumps(
            {
                "mode": "package" if is_package else "current",
                "target_configuration_id": target_id,
                "configurations": [row.model_dump(mode="json") for row in configurations],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return _redirect("/sources/preview", f"已解析 {source_count} 个来源，请确认")


@app.get("/sources/preview", response_class=HTMLResponse)
def source_preview(request: Request):
    if not PREVIEW_PATH.exists():
        return _redirect("/sources", "没有待确认的导入清单", "error")
    state = _load_source_preview()
    return _render(
        request,
        "source_preview.html",
        page="sources",
        configurations=state["configurations"],
        mode=state["mode"],
        target_configuration_id=state["target_configuration_id"],
        source_count=sum(len(row.sources) for row in state["configurations"]),
    )


def _load_source_preview() -> dict[str, object]:
    if not PREVIEW_PATH.exists():
        raise ValueError("导入预览已失效")
    payload = json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {
            "mode": "current",
            "target_configuration_id": None,
            "configurations": [
                ImportedConfiguration(
                    name="默认配置",
                    sources=[ImportedSource.model_validate(item) for item in payload],
                )
            ],
        }
    return {
        "mode": str(payload.get("mode", "current")),
        "target_configuration_id": payload.get("target_configuration_id"),
        "configurations": [
            ImportedConfiguration.model_validate(item)
            for item in payload.get("configurations", [])
        ],
    }


def _save_source_preview(state: dict[str, object]) -> None:
    PREVIEW_PATH.write_text(
        json.dumps(
            {
                "mode": state["mode"],
                "target_configuration_id": state["target_configuration_id"],
                "configurations": [
                    item.model_dump(mode="json") for item in state["configurations"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@app.post("/sources/preview/merge")
def merge_preview_sources(
    source_index: Annotated[int, Form()],
    target_index: Annotated[int, Form()],
    configuration_index: Annotated[int, Form()] = 0,
):
    try:
        state = _load_source_preview()
        configurations = state["configurations"]
        if not 0 <= configuration_index < len(configurations):
            raise ValueError("导入配置不存在")
        rows = configurations[configuration_index].sources
        if source_index == target_index or not 0 <= source_index < len(rows) or not 0 <= target_index < len(rows):
            raise ValueError("请选择另一个有效智库作为合并目标")
        source = rows[source_index]
        target = rows[target_index]
        seen = {canonicalize_url(str(entry.url)) for entry in target.entries}
        for entry in source.entries:
            if canonicalize_url(str(entry.url)) not in seen:
                target.entries.append(entry)
        rows.pop(source_index)
        _save_source_preview(state)
    except ValueError as exc:
        return _redirect("/sources/preview", str(exc), "error")
    return _redirect("/sources/preview", "来源已在预览中合并")


@app.post("/sources/preview/split")
def split_preview_entry(
    source_index: Annotated[int, Form()],
    entry_index: Annotated[int, Form()],
    name: Annotated[str, Form()] = "",
    configuration_index: Annotated[int, Form()] = 0,
):
    try:
        state = _load_source_preview()
        configurations = state["configurations"]
        if not 0 <= configuration_index < len(configurations):
            raise ValueError("导入配置不存在")
        rows = configurations[configuration_index].sources
        if not 0 <= source_index < len(rows):
            raise ValueError("智库预览项不存在")
        source = rows[source_index]
        if len(source.entries) <= 1 or not 0 <= entry_index < len(source.entries):
            raise ValueError("该入口不能继续拆分")
        entry = source.entries.pop(entry_index)
        rows.insert(
            source_index + 1,
            ImportedSource(
                name=name.strip() or source.name,
                group_name=source.group_name,
                homepage_url=entry.url,
                entries=[entry],
            ),
        )
        _save_source_preview(state)
    except ValueError as exc:
        return _redirect("/sources/preview", str(exc), "error")
    return _redirect("/sources/preview", "入口已拆分为独立智库")


@app.post("/sources/confirm")
def confirm_source_import():
    if not PREVIEW_PATH.exists():
        return _redirect("/sources", "导入预览已失效", "error")
    state = _load_source_preview()
    created = updated = 0
    imported_ids: list[int] = []
    with session_scope() as session:
        if state["mode"] == "current":
            target_id = state["target_configuration_id"]
            target = (
                get_active_configuration(session, int(target_id))
                if target_id is not None
                else ensure_default_configuration(session)
            )
            for configuration in state["configurations"]:
                added, changed = upsert_sources(session, configuration.sources, target.id)
                created += added
                updated += changed
            imported_ids.append(target.id)
        else:
            existing_names = {row.name.casefold() for row in active_configurations(session)}
            for configuration in state["configurations"]:
                base = " ".join(configuration.name.split()).strip() or "导入配置"
                candidate = base
                suffix = 1
                while candidate.casefold() in existing_names:
                    candidate = f"{base}（导入{'' if suffix == 1 else f' {suffix}'}）"
                    suffix += 1
                target = create_configuration(session, candidate)
                existing_names.add(candidate.casefold())
                added, changed = upsert_sources(session, configuration.sources, target.id)
                created += added
                updated += changed
                imported_ids.append(target.id)
    PREVIEW_PATH.unlink(missing_ok=True)
    return _redirect(
        _sources_url(imported_ids[0] if imported_ids else None),
        f"来源清单已确认：新增 {created}，更新 {updated}",
    )


@app.post("/sources/save")
def save_source(
    configuration_id: Annotated[int | None, Form()] = None,
    source_id: Annotated[int | None, Form()] = None,
    name: Annotated[str, Form()] = "",
    group_name: Annotated[str, Form()] = "",
    homepage_url: Annotated[str, Form()] = "",
    publication_url: Annotated[str, Form()] = "",
    adapter_key: Annotated[str, Form()] = "auto",
    ordered_by_date: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
):
    try:
        entry = ImportedSourceEntry(
            url=publication_url or homepage_url,
            adapter_key=adapter_key or "auto",
            ordered_by_date=ordered_by_date is not None,
        )
        item = ImportedSource(
            name=name,
            group_name=group_name,
            homepage_url=homepage_url,
            entries=[entry],
            enabled=enabled is not None if source_id else True,
        )
    except ValidationError as exc:
        return _redirect(
            _sources_url(configuration_id),
            f"来源字段无效：{exc.errors()[0]['msg']}",
            "error",
        )
    with session_scope() as session:
        if source_id:
            row = session.get(Source, source_id)
            if not row or row.deleted_at is not None:
                return _redirect(_sources_url(configuration_id), "来源不存在", "error")
            configuration_id = row.configuration_id
            homepage = normalize_fetch_url(str(item.homepage_url))
            duplicate = session.scalar(
                select(Source.id).where(
                    Source.configuration_id == configuration_id,
                    Source.homepage_url == homepage,
                    Source.id != row.id,
                )
            )
            if duplicate is not None:
                return _redirect(
                    _sources_url(configuration_id),
                    "当前配置中已经存在相同官网地址",
                    "warning",
                )
            row.name = item.name
            row.group_name = item.group_name
            row.homepage_url = homepage
            row.enabled = enabled is not None
        else:
            if configuration_id is None:
                configuration_id = ensure_default_configuration(session).id
            get_active_configuration(session, configuration_id)
            upsert_sources(session, [item], configuration_id)
    return _redirect(_sources_url(configuration_id), "来源已保存")


@app.post("/sources/{source_id}/entries/save")
def save_source_entry(
    source_id: int,
    entry_id: Annotated[int | None, Form()] = None,
    url: Annotated[str, Form()] = "",
    adapter_key: Annotated[str, Form()] = "auto",
    ordered_by_date: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
    listing_selectors: Annotated[str, Form()] = "",
    container_selectors: Annotated[str, Form()] = "",
    content_type_selectors: Annotated[str, Form()] = "",
    date_selectors: Annotated[str, Form()] = "",
    allowed_path_patterns: Annotated[str, Form()] = "",
    excluded_path_patterns: Annotated[str, Form()] = "",
):
    try:
        item = ImportedSourceEntry(
            url=url,
            adapter_key=adapter_key or "auto",
            ordered_by_date=ordered_by_date is not None,
            enabled=enabled is not None if entry_id else True,
            listing_selectors=_rule_lines(listing_selectors),
            container_selectors=_rule_lines(container_selectors),
            content_type_selectors=_rule_lines(content_type_selectors),
            date_selectors=_rule_lines(date_selectors),
            allowed_path_patterns=_rule_lines(allowed_path_patterns),
            excluded_path_patterns=_rule_lines(excluded_path_patterns),
        )
    except ValidationError as exc:
        return _redirect("/sources", f"入口字段无效：{exc.errors()[0]['msg']}", "error")
    with session_scope() as session:
        source = session.get(Source, source_id)
        if not source or source.deleted_at is not None:
            return _redirect("/sources", "智库不存在", "error")
        configuration_id = source.configuration_id
        entry_url = normalize_fetch_url(str(item.url))
        canonical = canonicalize_url(entry_url)
        duplicate = session.scalar(
            select(SourceEntry).where(
                SourceEntry.source_id == source_id,
                SourceEntry.canonical_url == canonical,
                SourceEntry.id != (entry_id or -1),
            )
        )
        if duplicate:
            return _redirect(
                _sources_url(configuration_id), "该智库已经存在相同入口", "warning"
            )
        entry = session.get(SourceEntry, entry_id) if entry_id else None
        if entry_id and (not entry or entry.source_id != source_id):
            return _redirect(_sources_url(configuration_id), "入口不存在", "error")
        if entry is None:
            entry = SourceEntry(source_id=source_id, canonical_url=canonical)
            session.add(entry)
        entry.url = entry_url
        entry.canonical_url = canonical
        entry.adapter_key = item.adapter_key
        entry.ordered_by_date = item.ordered_by_date
        entry.enabled = item.enabled
        entry.listing_selectors_json = json.dumps(item.listing_selectors, ensure_ascii=False)
        entry.container_selectors_json = json.dumps(item.container_selectors, ensure_ascii=False)
        entry.content_type_selectors_json = json.dumps(item.content_type_selectors, ensure_ascii=False)
        entry.date_selectors_json = json.dumps(item.date_selectors, ensure_ascii=False)
        entry.allowed_path_patterns_json = json.dumps(item.allowed_path_patterns, ensure_ascii=False)
        entry.excluded_path_patterns_json = json.dumps(item.excluded_path_patterns, ensure_ascii=False)
        entry.deleted_at = None
        entry.last_tested_at = None
        entry.last_test_status = "untested"
        entry.last_test_final_url = ""
        entry.last_test_http_status = None
        entry.last_test_discovered_count = 0
        entry.last_test_accepted_count = 0
        entry.last_test_message = ""
    return _redirect(_sources_url(configuration_id), "检索入口已保存")


@app.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int):
    with session_scope() as session:
        source = session.get(Source, source_id)
        if not source or source.deleted_at is not None:
            raise HTTPException(404, "智库不存在")
        if not source.enabled:
            has_entry = any(entry.enabled and entry.deleted_at is None for entry in source.entries)
            if not has_entry:
                return _redirect("/sources", "至少启用一个检索入口后才能启用该智库", "warning")
        source.enabled = not source.enabled
        configuration_id = source.configuration_id
        enabled = source.enabled
    return _redirect(
        _sources_url(configuration_id), "智库已启用" if enabled else "智库已停用"
    )


@app.post("/sources/{source_id}/entries/{entry_id}/toggle")
def toggle_source_entry(source_id: int, entry_id: int):
    with session_scope() as session:
        entry = session.get(SourceEntry, entry_id)
        if (
            not entry
            or entry.source_id != source_id
            or entry.deleted_at is not None
            or entry.source.deleted_at is not None
        ):
            raise HTTPException(404, "检索入口不存在")
        entry.enabled = not entry.enabled
        enabled = entry.enabled
        configuration_id = entry.source.configuration_id
    return _redirect(
        _sources_url(configuration_id), "检索入口已启用" if enabled else "检索入口已停用"
    )


@app.post("/sources/{source_id}/entries/{entry_id}/delete")
def delete_source_entry(source_id: int, entry_id: int):
    with session_scope() as session:
        entry = session.get(SourceEntry, entry_id)
        if not entry or entry.source_id != source_id or entry.source.deleted_at is not None:
            raise HTTPException(404, "检索入口不存在")
        entry.enabled = False
        entry.deleted_at = utcnow()
        configuration_id = entry.source.configuration_id
    return _redirect(_sources_url(configuration_id), "检索入口已移入回收站")


@app.post("/sources/{source_id}/delete")
def delete_source(source_id: int):
    with session_scope() as session:
        source = session.get(Source, source_id)
        if not source or source.deleted_at is not None:
            raise HTTPException(404, "智库不存在")
        source.enabled = False
        source.deleted_at = utcnow()
        configuration_id = source.configuration_id
    return _redirect(_sources_url(configuration_id), "智库已移入回收站")


@app.get("/sources/export")
def export_sources(configuration_id: int | None = None, all_configurations: bool = False):
    with SessionLocal() as session:
        if not all_configurations and configuration_id is None:
            configuration_id = ensure_default_configuration(session).id
        filename = "sources.all-configurations.json" if all_configurations else "sources.current-config.json"
        path = save_sources_json(
            session,
            PATHS.exports / filename,
            None if all_configurations else configuration_id,
        )
    return FileResponse(path, filename=filename, media_type="application/json")


@app.post("/runs")
def new_run(
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
    configuration_id: Annotated[int, Form()],
):
    try:
        run_id = create_run(
            start_date,
            end_date,
            configuration_id=configuration_id,
        )
    except ValueError as exc:
        return _redirect("/", str(exc), "error")
    if not start_run_in_background(run_id):
        return _redirect(f"/runs/{run_id}", "已有任务正在运行；新任务已保留为等待状态", "warning")
    return _redirect(f"/runs/{run_id}", "任务已启动")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, content_filter: str = "all"):
    with SessionLocal() as session:
        run = session.scalar(
            select(CrawlRun)
            .where(CrawlRun.id == run_id, CrawlRun.deleted_at.is_(None))
            .options(
                selectinload(CrawlRun.source_runs).selectinload(SourceRun.source),
                selectinload(CrawlRun.source_runs)
                .selectinload(SourceRun.entry_runs)
                .selectinload(SourceEntryRun.source_entry),
                selectinload(CrawlRun.source_runs)
                .selectinload(SourceRun.entry_runs)
                .selectinload(SourceEntryRun.page_audits),
            )
        )
        if not run:
            raise HTTPException(404, "任务不存在")
        query = (
            select(Candidate)
            .where(Candidate.run_id == run_id)
            .options(selectinload(Candidate.source), selectinload(Candidate.assessment), selectinload(Candidate.review))
            .order_by(Candidate.source_id, Candidate.published_date.desc(), Candidate.id)
        )
        all_candidates = list(session.scalars(query).all())
        source_names = {
            row.source_id: (row.source_name_snapshot or row.source.name)
            for row in run.source_runs
        }
        for candidate in all_candidates:
            candidate.display_source_name = source_names.get(
                candidate.source_id, candidate.source.name
            )

        def matches(item: Candidate) -> bool:
            if content_filter == "all":
                return True
            if content_filter == "hard_eligible":
                return (
                    item.date_scope == DateScope.IN_WINDOW
                    and item.content_kind_status == ContentKindStatus.ELIGIBLE
                    and item.length_status in {LengthStatus.PASS, LengthStatus.BORDERLINE}
                )
            if content_filter == "type_excluded":
                return item.content_kind_status == ContentKindStatus.EXCLUDED
            if content_filter == "length_below":
                return item.length_status == LengthStatus.BELOW
            if content_filter == "model_included":
                return item.topic_status == TopicStatus.MODEL_INCLUDED
            if content_filter == "model_excluded":
                return item.topic_status == TopicStatus.MODEL_EXCLUDED
            if content_filter == "waiting_review":
                return item.review_status == ReviewStatus.PENDING and item.status in {
                    CandidateStatus.NEEDS_REVIEW,
                    CandidateStatus.MODEL_INCLUDED,
                }
            if content_filter == "reviewed":
                return item.review_status in {ReviewStatus.APPROVED, ReviewStatus.REJECTED}
            return True

        candidates = [item for item in all_candidates if matches(item)]
        in_window = [item for item in candidates if item.date_scope == DateScope.IN_WINDOW]
        date_unknown = [item for item in candidates if item.date_scope == DateScope.DATE_UNKNOWN]
        outside = [
            item
            for item in candidates
            if item.date_scope in {DateScope.AFTER_END, DateScope.BEFORE_START}
        ]
        return _render(
            request,
            "run_detail.html",
            page="runs",
            run=run,
            candidates=candidates,
            total_candidates=len(all_candidates),
            in_window=in_window,
            date_unknown=date_unknown,
            outside=outside,
            content_filter=content_filter,
        )


@app.post("/runs/{run_id}/content-types/reclassify")
def reclassify_run_content_type_rules(run_id: int):
    config = load_config()
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if not run or run.deleted_at is not None:
            raise HTTPException(404, "任务不存在")
        changed = reclassify_run_content_types(
            session, run_id, config.content_type_rules
        )
    return _redirect(
        f"/runs/{run_id}",
        f"已按现有规则重算本任务的 {changed} 条未审核候选；其他任务未改动",
    )


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: int):
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if not run or run.deleted_at is not None:
            raise HTTPException(404, "任务不存在")
        if run.status not in {RunStatus.QUEUED, RunStatus.INTERRUPTED, RunStatus.FAILED}:
            return _redirect(f"/runs/{run_id}", "当前任务状态不能恢复", "warning")
        run.status = RunStatus.QUEUED
    started = start_run_in_background(run_id)
    return _redirect(f"/runs/{run_id}", "任务已恢复" if started else "已有任务正在运行", "success" if started else "warning")


@app.post("/runs/{run_id}/delete")
def delete_run(run_id: int):
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if not run or run.deleted_at is not None:
            raise HTTPException(404, "任务不存在")
        if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            return _redirect(f"/runs/{run_id}", "排队或运行中的任务不能删除", "warning")
        run.deleted_at = utcnow()
    return _redirect("/", f"任务 #{run_id} 已移入回收站")


@app.get("/trash", response_class=HTMLResponse)
def trash_page(request: Request):
    with SessionLocal() as session:
        configurations = session.scalars(
            select(SourceConfiguration)
            .where(SourceConfiguration.deleted_at.is_not(None))
            .order_by(desc(SourceConfiguration.deleted_at))
        ).all()
        sources = session.scalars(
            select(Source)
            .where(Source.deleted_at.is_not(None))
            .options(selectinload(Source.configuration))
            .order_by(desc(Source.deleted_at))
        ).all()
        entries = session.scalars(
            select(SourceEntry)
            .where(SourceEntry.deleted_at.is_not(None))
            .options(selectinload(SourceEntry.source))
            .order_by(desc(SourceEntry.deleted_at))
        ).all()
        runs = session.scalars(
            select(CrawlRun)
            .where(CrawlRun.deleted_at.is_not(None))
            .options(selectinload(CrawlRun.source_runs))
            .order_by(desc(CrawlRun.deleted_at))
        ).all()
        return _render(
            request,
            "trash.html",
            page="trash",
            configurations=configurations,
            sources=sources,
            entries=entries,
            runs=runs,
        )


@app.post("/trash/configurations/{configuration_id}/restore")
def restore_deleted_configuration(configuration_id: int):
    try:
        with session_scope() as session:
            row = restore_configuration(session, configuration_id)
            restored_id = row.id
    except ValueError as exc:
        return _redirect("/trash", str(exc), "error")
    return _redirect(_sources_url(restored_id), "检索配置已恢复")


@app.post("/trash/sources/{source_id}/restore")
def restore_source(source_id: int):
    try:
        with session_scope() as session:
            source = session.get(Source, source_id)
            if not source or source.deleted_at is None:
                raise HTTPException(404, "回收站中不存在该智库")
            if source.configuration_id is None:
                source.configuration_id = ensure_default_configuration(session).id
            else:
                get_active_configuration(session, source.configuration_id)
            source.deleted_at = None
            source.enabled = any(
                entry.enabled and entry.deleted_at is None for entry in source.entries
            )
            configuration_id = source.configuration_id
            enabled = source.enabled
    except ValueError as exc:
        return _redirect("/trash", str(exc), "error")
    message = "智库已恢复" if enabled else "智库已恢复，但因没有启用入口而保持停用"
    return _redirect(_sources_url(configuration_id), message)


@app.post("/trash/entries/{entry_id}/restore")
def restore_source_entry(entry_id: int):
    try:
        with session_scope() as session:
            entry = session.get(SourceEntry, entry_id)
            if not entry or entry.deleted_at is None:
                raise HTTPException(404, "回收站中不存在该入口")
            if entry.source.deleted_at is not None:
                raise ValueError("请先恢复该入口所属的智库")
            if entry.source.configuration_id is None:
                entry.source.configuration_id = ensure_default_configuration(session).id
            else:
                get_active_configuration(session, entry.source.configuration_id)
            entry.deleted_at = None
            entry.enabled = True
    except ValueError as exc:
        return _redirect("/trash", str(exc), "error")
    return _redirect("/trash", "检索入口已恢复")


@app.post("/trash/entries/{entry_id}/purge")
def purge_source_entry(entry_id: int, confirm: Annotated[str, Form()] = ""):
    if confirm != "DELETE":
        return _redirect("/trash", "永久清除需要输入 DELETE", "warning")
    create_database_backup()
    with session_scope() as session:
        entry = session.get(SourceEntry, entry_id)
        if not entry or entry.deleted_at is None:
            raise HTTPException(404, "回收站中不存在该入口")
        session.delete(entry)
    return _redirect("/trash", "检索入口已永久清除；清除前备份已保存")


@app.post("/trash/runs/{run_id}/restore")
def restore_run(run_id: int):
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if not run or run.deleted_at is None:
            raise HTTPException(404, "回收站中不存在该任务")
        run.deleted_at = None
    return _redirect("/trash", f"任务 #{run_id} 已恢复")


@app.post("/trash/runs/{run_id}/purge")
def purge_run(run_id: int, confirm: Annotated[str, Form()] = ""):
    if confirm != "DELETE":
        return _redirect("/trash", "永久清除需要输入 DELETE", "warning")
    create_database_backup()
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if not run or run.deleted_at is None:
            raise HTTPException(404, "回收站中不存在该任务")
        session.delete(run)
    return _redirect("/trash", f"任务 #{run_id} 已永久清除；清除前备份已保存")


@app.post("/runs/{run_id}/sources/{source_id}/rerun")
def rerun_source(run_id: int, source_id: int):
    with SessionLocal() as session:
        run = session.scalar(
            select(CrawlRun)
            .where(CrawlRun.id == run_id)
            .options(
                selectinload(CrawlRun.source_runs).selectinload(SourceRun.entry_runs)
            )
        )
        source = session.get(Source, source_id)
        if not run or run.deleted_at is not None or not source:
            raise HTTPException(404, "任务或来源不存在")
        start_date, end_date = run.start_date, run.end_date
        source_run = next((row for row in run.source_runs if row.source_id == source_id), None)
        if source_run is None:
            raise HTTPException(404, "该任务不包含指定来源")
        configuration_id = run.configuration_id or source.configuration_id
    if configuration_id is None:
        return _redirect(f"/runs/{run_id}", "原任务没有可用的检索配置", "error")
    try:
        new_run_id = create_run(
            start_date,
            end_date,
            [source_id],
            configuration_id=configuration_id,
            mark_configuration_used=False,
        )
    except ValueError as exc:
        return _redirect(f"/runs/{run_id}", str(exc), "error")
    started = start_run_in_background(new_run_id)
    return _redirect(
        f"/runs/{new_run_id}",
        "单来源重跑已启动" if started else "单来源重跑已创建并等待运行",
        "success" if started else "warning",
    )


@app.get("/candidates/{candidate_id}", response_class=HTMLResponse)
def candidate_detail(request: Request, candidate_id: int):
    with SessionLocal() as session:
        candidate = session.scalar(
            select(Candidate)
            .join(CrawlRun, CrawlRun.id == Candidate.run_id)
            .where(Candidate.id == candidate_id, CrawlRun.deleted_at.is_(None))
            .options(
                selectinload(Candidate.source),
                selectinload(Candidate.assessment),
                selectinload(Candidate.review),
                selectinload(Candidate.attachments),
                selectinload(Candidate.model_suggestions),
            )
        )
        if not candidate:
            raise HTTPException(404, "候选不存在")
        source_run = session.scalar(
            select(SourceRun).where(
                SourceRun.run_id == candidate.run_id,
                SourceRun.source_id == candidate.source_id,
            )
        )
        candidate.display_source_name = (
            source_run.source_name_snapshot
            if source_run and source_run.source_name_snapshot
            else candidate.source.name
        )
        secondary = []
        if candidate.review:
            secondary = json.loads(candidate.review.secondary_categories_json or "[]")
        elif candidate.assessment:
            secondary = json.loads(candidate.assessment.secondary_categories_json or "[]")
        return _render(request, "candidate.html", page="review", candidate=candidate, secondary=secondary)


@app.post("/candidates/{candidate_id}/content-type-rule")
def add_candidate_content_type_rule(
    candidate_id: int,
    raw_label: Annotated[str, Form()],
    content_type: Annotated[str, Form()],
    action: Annotated[str, Form()] = "review",
):
    with SessionLocal() as session:
        candidate = session.scalar(
            select(Candidate)
            .join(CrawlRun, CrawlRun.id == Candidate.run_id)
            .where(Candidate.id == candidate_id, CrawlRun.deleted_at.is_(None))
        )
        if candidate is None:
            raise HTTPException(404, "候选不存在")
        try:
            stored_labels = json.loads(candidate.content_type_raw_labels_json or "[]")
        except (TypeError, json.JSONDecodeError):
            stored_labels = []
        matched_label = next(
            (
                str(value).strip()
                for value in stored_labels
                if normalize_content_type_label(value)
                == normalize_content_type_label(raw_label)
            ),
            "",
        )
        if not matched_label:
            return _redirect(
                f"/candidates/{candidate_id}",
                "该原始体裁标签不在候选证据中，未创建规则",
                "error",
            )
        run_id = candidate.run_id
    try:
        config, rule = _save_content_type_rule(
            "", "card_label", matched_label, content_type, action
        )
        message = f"已添加全局规则：{rule['match_value']} → {rule['content_type']}"
        level = "success"
    except ValueError as exc:
        config = load_config()
        message = str(exc)
        level = "warning"
    with session_scope() as session:
        changed = reclassify_run_content_types(
            session, run_id, config.content_type_rules
        )
    return _redirect(
        f"/candidates/{candidate_id}",
        f"{message}；仅重算当前任务的 {changed} 条未审核候选",
        level,
    )


@app.post("/candidates/{candidate_id}/model-suggestions", response_class=HTMLResponse)
def create_candidate_model_suggestion(request: Request, candidate_id: int):
    with SessionLocal() as session:
        candidate = session.scalar(
            select(Candidate)
            .join(CrawlRun, CrawlRun.id == Candidate.run_id)
            .where(Candidate.id == candidate_id, CrawlRun.deleted_at.is_(None))
            .options(selectinload(Candidate.source), selectinload(Candidate.assessment))
        )
        if not candidate:
            raise HTTPException(404, "候选不存在")
        source_run = session.scalar(
            select(SourceRun).where(
                SourceRun.run_id == candidate.run_id,
                SourceRun.source_id == candidate.source_id,
            )
        )
        source_name = (
            source_run.source_name_snapshot
            if source_run and source_run.source_name_snapshot
            else candidate.source.name
        )
        if not candidate.extracted_text.strip():
            return _render(
                request,
                "model_suggestion.html",
                suggestion=None,
                suggestion_error="尚无可供模型分析的正文，请先解决正文或 PDF 提取问题。",
                candidate_date_confirmed=candidate.date_status == "confirmed",
            )
        snapshot = {
            "title": candidate.title_original,
            "source_name": source_name,
            "original_url": candidate.original_url,
            "published_date": candidate.published_date.isoformat() if candidate.published_date else "",
            "date_status": candidate.date_status,
            "date_evidence": candidate.date_evidence,
            "content_type": candidate.content_type,
            "content_type_evidence": candidate.content_type_evidence,
            "authoritative_decision": (
                candidate.assessment.decision
                if candidate.assessment
                and candidate.assessment.decision in {Decision.INCLUDE, Decision.EXCLUDE}
                else ""
            ),
            "authoritative_reasoning": (
                candidate.assessment.reasoning_zh if candidate.assessment else ""
            ),
            "text": candidate.extracted_text,
        }
        date_confirmed = candidate.date_status == "confirmed"
        authoritative_decision = snapshot["authoritative_decision"]
        authoritative_reasoning = snapshot["authoritative_reasoning"]
    config = load_config()
    provider = OpenAICompatibleProvider(config)
    if not provider.configured():
        return _render(
            request,
            "model_suggestion.html",
            suggestion=None,
            suggestion_error="模型未完整配置。请先在设置页填写 API 密钥并测试连接。",
            candidate_date_confirmed=date_confirmed,
        )
    result = provider.suggest_review(**snapshot)
    with session_scope() as session:
        payload = result.payload
        row = ModelSuggestion(
            candidate_id=candidate_id,
            decision=authoritative_decision or Decision.NEEDS_REVIEW,
            suggested_date=payload.suggested_date if payload else None,
            date_evidence=payload.date_evidence if payload else "",
            content_type=(
                snapshot["content_type"]
                if snapshot["content_type"] in config.content_types
                else ""
            ),
            primary_category=payload.primary_category if payload and payload.primary_category else "",
            secondary_categories_json=json.dumps(payload.secondary_categories if payload else []),
            title_en=payload.title_en if payload else "",
            title_zh=payload.title_zh if payload else "",
            core_claim_zh=payload.core_claim_zh if payload else "",
            reasoning_zh=payload.reasoning_zh if payload else "",
            evidence_json=json.dumps(payload.evidence if payload else [], ensure_ascii=False),
            confidence=payload.confidence if payload else 0.0,
            field_confidences_json=json.dumps(
                payload.field_confidences if payload else {}, ensure_ascii=False
            ),
            provider_model=provider.config.model_name,
            input_chars=min(len(snapshot["text"]), provider.config.max_model_input_chars),
            raw_response=result.raw_response,
            error_message=result.error,
        )
        session.add(row)
        session.flush()
    return _render(
        request,
        "model_suggestion.html",
        suggestion=row,
        suggestion_error=result.error,
        candidate_date_confirmed=date_confirmed,
        authoritative_model_decision=authoritative_decision,
        authoritative_model_reasoning=authoritative_reasoning,
    )


def _review_candidate_filter(status: str):
    if status == "pending":
        return Candidate.status.in_(
            [CandidateStatus.NEEDS_REVIEW, CandidateStatus.MODEL_INCLUDED]
        )
    if status:
        return Candidate.status == status
    return True


@app.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, status: str = "pending", page_number: int = 1):
    page_number = max(1, page_number)
    page_size = 20
    with SessionLocal() as session:
        matching_runs = (
            select(Candidate.run_id)
            .join(CrawlRun, CrawlRun.id == Candidate.run_id)
            .where(
                CrawlRun.deleted_at.is_(None),
                _review_candidate_filter(status),
            )
            .distinct()
        )
        total_groups = int(
            session.scalar(select(func.count()).select_from(matching_runs.subquery())) or 0
        )
        run_ids = session.scalars(
            select(CrawlRun.id)
            .where(CrawlRun.id.in_(matching_runs))
            .order_by(desc(CrawlRun.created_at), desc(CrawlRun.id))
            .offset((page_number - 1) * page_size)
            .limit(page_size)
        ).all()
        runs_by_id = {
            row.id: row
            for row in session.scalars(
                select(CrawlRun).where(CrawlRun.id.in_(run_ids))
            ).all()
        }
        counts: dict[int, dict[str, int]] = {run_id: {} for run_id in run_ids}
        if run_ids:
            count_rows = session.execute(
                select(Candidate.run_id, Candidate.status, func.count(Candidate.id))
                .where(Candidate.run_id.in_(run_ids))
                .group_by(Candidate.run_id, Candidate.status)
            ).all()
            for run_id, candidate_status, count in count_rows:
                counts[run_id][candidate_status] = int(count)
        review_groups = []
        for run_id in run_ids:
            run = runs_by_id[run_id]
            run.review_counts = counts[run_id]
            run.review_total = sum(counts[run_id].values())
            run.review_pending = sum(
                counts[run_id].get(value, 0)
                for value in (CandidateStatus.NEEDS_REVIEW, CandidateStatus.MODEL_INCLUDED)
            )
            review_groups.append(run)
        page_count = max(1, (total_groups + page_size - 1) // page_size)
        return _render(
            request,
            "review_queue.html",
            page="review",
            review_groups=review_groups,
            total_groups=total_groups,
            status_filter=status,
            page_number=page_number,
            page_count=page_count,
        )


@app.get("/review/runs/{run_id}", response_class=HTMLResponse)
def review_run_rows(request: Request, run_id: int, status: str = "pending"):
    with SessionLocal() as session:
        run_exists = session.scalar(
            select(CrawlRun.id).where(
                CrawlRun.id == run_id,
                CrawlRun.deleted_at.is_(None),
            )
        )
        if run_exists is None:
            raise HTTPException(404, "任务不存在")
        candidates = session.scalars(
            select(Candidate)
            .where(
                Candidate.run_id == run_id,
                _review_candidate_filter(status),
            )
            .options(selectinload(Candidate.source), selectinload(Candidate.assessment))
            .order_by(desc(Candidate.created_at), desc(Candidate.id))
        ).all()
        source_names = {
            row.source_id: (row.source_name_snapshot or row.source.name)
            for row in session.scalars(
                select(SourceRun)
                .where(SourceRun.run_id == run_id)
                .options(selectinload(SourceRun.source))
            ).all()
        }
        for candidate in candidates:
            candidate.display_source_name = source_names.get(
                candidate.source_id, candidate.source.name
            )
        return _render(
            request,
            "review_rows.html",
            page="review",
            candidates=candidates,
        )


@app.post("/candidates/{candidate_id}/review")
def review_candidate(
    candidate_id: int,
    decision: Annotated[str, Form()],
    primary_category: Annotated[str, Form()] = "",
    secondary_categories: Annotated[list[str] | None, Form()] = None,
    title_en: Annotated[str, Form()] = "",
    title_zh: Annotated[str, Form()] = "",
    content_type: Annotated[str, Form()] = "",
    custom_content_type: Annotated[str, Form()] = "",
    authors: Annotated[str, Form()] = "",
    core_claim_zh: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    published_date: Annotated[str, Form()] = "",
    confirm_date: Annotated[str | None, Form()] = None,
):
    with session_scope() as session:
        candidate = session.scalar(
            select(Candidate)
            .join(CrawlRun, CrawlRun.id == Candidate.run_id)
            .where(Candidate.id == candidate_id, CrawlRun.deleted_at.is_(None))
        )
        if not candidate:
            raise HTTPException(404, "候选不存在")
        run_id = candidate.run_id
        try:
            manual_date = date.fromisoformat(published_date) if published_date.strip() else None
            resolved_content_type = (
                custom_content_type.strip() if content_type == "__custom__" else content_type
            )
            save_review(
                session,
                candidate_id,
                decision,
                primary_category,
                secondary_categories or [],
                title_en,
                title_zh,
                resolved_content_type,
                core_claim_zh,
                notes,
                authors=_rule_lines(authors),
                published_date=manual_date,
                confirm_date=confirm_date is not None,
            )
        except (TypeError, ValueError) as exc:
            return _redirect(f"/candidates/{candidate_id}", str(exc), "error")
    return _redirect(f"/runs/{run_id}", "人工审核已保存")


@app.post("/runs/{run_id}/export/report")
def report_export(run_id: int):
    with SessionLocal() as session:
        path = export_report(session, run_id)
    return FileResponse(path, filename=path.name)


@app.post("/runs/{run_id}/export/audit")
def audit_export(run_id: int):
    with SessionLocal() as session:
        path = export_audit_workbook(session, run_id)
    return FileResponse(path, filename=path.name)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    rule_page: int = 1,
    rule_q: str = "",
    rule_match_kind: str = "",
    rule_action: str = "",
    rule_sort: str = "match_value",
    return_run_id: int | None = None,
):
    config = load_config()
    rules = list(config.content_type_rules)
    query = rule_q.strip().casefold()
    if query:
        rules = [
            rule
            for rule in rules
            if query
            in " ".join(
                str(rule.get(key, ""))
                for key in ("match_value", "content_type", "match_kind", "action")
            ).casefold()
        ]
    if rule_match_kind in {"url_path", "card_label"}:
        rules = [rule for rule in rules if rule["match_kind"] == rule_match_kind]
    if rule_action in {"allow", "exclude", "review"}:
        rules = [rule for rule in rules if rule["action"] == rule_action]
    sort_key = rule_sort if rule_sort in {"match_value", "content_type", "match_kind", "action"} else "match_value"
    rules.sort(key=lambda rule: (str(rule.get(sort_key, "")).casefold(), rule["rule_id"]))
    page_size = 20
    rule_page_count = max(1, (len(rules) + page_size - 1) // page_size)
    rule_page = max(1, min(rule_page, rule_page_count))
    visible_rules = rules[(rule_page - 1) * page_size : rule_page * page_size]
    all_rules = list(config.content_type_rules)
    rule_counts = {
        action: sum(rule["action"] == action for rule in all_rules)
        for action in ("allow", "exclude", "review")
    }
    return _render(
        request,
        "settings.html",
        page="settings",
        config=config,
        has_key=bool(get_api_key()),
        visible_rules=visible_rules,
        rule_total=len(all_rules),
        filtered_rule_total=len(rules),
        rule_counts=rule_counts,
        rule_page=rule_page,
        rule_page_count=rule_page_count,
        rule_q=rule_q,
        rule_match_kind=rule_match_kind,
        rule_action=rule_action,
        rule_sort=sort_key,
        return_run_id=return_run_id,
    )


def _save_content_type_rule(
    original_rule_id: str,
    match_kind: str,
    match_value: str,
    content_type: str,
    action: str,
) -> tuple[AppConfig, dict[str, str]]:
    config = load_config()
    parsed = parse_content_type_rules(
        [match_kind], [match_value], [content_type], [action]
    )[0]
    rules = list(config.content_type_rules)
    duplicate = next(
        (
            rule
            for rule in rules
            if rule["match_kind"] == parsed["match_kind"]
            and rule["match_value"] == parsed["match_value"]
            and rule["rule_id"] != original_rule_id
        ),
        None,
    )
    if duplicate:
        raise ValueError(
            f"规则已存在：{parsed['match_kind']} + {parsed['match_value']}"
        )
    if original_rule_id:
        index = next(
            (index for index, rule in enumerate(rules) if rule["rule_id"] == original_rule_id),
            None,
        )
        if index is None:
            raise ValueError("待编辑规则不存在或已经改变")
        rules[index] = parsed
    else:
        if len(rules) >= 100:
            raise ValueError("体裁规则最多允许 100 条")
        rules.append(parsed)
    config.content_type_rules = rules
    save_config(config)
    return config, parsed


@app.post("/settings/content-type-rules/save")
def save_content_type_rule(
    rule_id: Annotated[str, Form()] = "",
    match_kind: Annotated[str, Form()] = "card_label",
    match_value: Annotated[str, Form()] = "",
    content_type: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "review",
    return_run_id: Annotated[int | None, Form()] = None,
):
    try:
        config, rule = _save_content_type_rule(
            rule_id, match_kind, match_value, content_type, action
        )
    except ValueError as exc:
        return _redirect(
            f"/settings{return_run_id and f'?return_run_id={return_run_id}' or ''}",
            str(exc),
            "error",
        )
    changed = 0
    if return_run_id is not None:
        with session_scope() as session:
            run = session.get(CrawlRun, return_run_id)
            if not run or run.deleted_at is not None:
                return _redirect("/settings", "来源任务不存在，规则已保存但未重算", "warning")
            changed = reclassify_run_content_types(
                session, return_run_id, config.content_type_rules
            )
    target = f"/settings?return_run_id={return_run_id}" if return_run_id else "/settings"
    message = f"规则已保存：{rule['match_value']} → {rule['content_type']}"
    if return_run_id is not None:
        message += f"；仅重算任务 #{return_run_id} 的 {changed} 条未审核候选"
    return _redirect(target, message)


@app.post("/settings/content-type-rules/{rule_id}/delete")
def delete_content_type_rule(
    rule_id: str,
    return_run_id: Annotated[int | None, Form()] = None,
):
    config = load_config()
    rules = [rule for rule in config.content_type_rules if rule["rule_id"] != rule_id]
    if len(rules) == len(config.content_type_rules):
        return _redirect("/settings", "规则不存在", "warning")
    if not rules:
        return _redirect("/settings", "至少保留一条体裁规则", "error")
    config.content_type_rules = rules
    save_config(config)
    changed = 0
    if return_run_id is not None:
        with session_scope() as session:
            changed = reclassify_run_content_types(
                session, return_run_id, config.content_type_rules
            )
    target = f"/settings?return_run_id={return_run_id}" if return_run_id else "/settings"
    message = "规则已删除"
    if return_run_id is not None:
        message += f"；仅重算任务 #{return_run_id} 的 {changed} 条未审核候选"
    return _redirect(target, message)


@app.get("/settings/models")
def available_models():
    provider = OpenAICompatibleProvider(load_config())
    if not provider.configured():
        return JSONResponse(
            {"models": [], "error": "请先保存 API Base URL、模型名和 API 密钥"},
            status_code=400,
        )
    try:
        models = provider.list_models()
    except Exception as exc:
        return JSONResponse({"models": [], "error": f"读取模型列表失败：{exc}"}, status_code=502)
    return {"models": models, "selected": provider.config.model_name}


@app.post("/settings")
def update_settings(
    provider_kind: Annotated[str, Form()] = "custom",
    api_base_url: Annotated[str, Form()] = "",
    model_name: Annotated[str, Form()] = "",
    api_style: Annotated[str, Form()] = "chat_completions",
    api_key: Annotated[str, Form()] = "",
    request_timeout_seconds: Annotated[int, Form()] = 120,
    max_model_input_chars: Annotated[int, Form()] = 20000,
    crawl_concurrency: Annotated[int, Form()] = 3,
    per_domain_delay_seconds: Annotated[float, Form()] = 2.0,
    max_listing_pages: Annotated[int, Form()] = 25,
    raw_cache_days: Annotated[int, Form()] = 90,
    pdf_parser_mode: Annotated[str, Form()] = "anydoc_first",
    content_type_rule_match_kind: Annotated[list[str] | None, Form()] = None,
    content_type_rule_match_value: Annotated[list[str] | None, Form()] = None,
    content_type_rule_content_type: Annotated[list[str] | None, Form()] = None,
    content_type_rule_action: Annotated[list[str] | None, Form()] = None,
    content_type_rules_present: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
):
    current_config = load_config()
    if provider_kind not in MODEL_PROVIDER_PRESETS:
        provider_kind = "custom"
    preset = MODEL_PROVIDER_PRESETS[provider_kind]
    resolved_base_url = api_base_url.strip() or preset["api_base_url"]
    resolved_model_name = model_name.strip() or preset["model_name"]
    resolved_api_style = (
        preset["api_style"]
        if provider_kind == "deepseek"
        else api_style if api_style in {"chat_completions", "responses"} else "chat_completions"
    )
    rules = current_config.content_type_rules
    if content_type_rules_present:
        try:
            rules = parse_content_type_rules(
                content_type_rule_match_kind or [],
                content_type_rule_match_value or [],
                content_type_rule_content_type or [],
                content_type_rule_action or [],
            )
        except ValueError as exc:
            return _redirect("/settings", str(exc), "error")
    config = AppConfig(
        provider_kind=provider_kind,
        api_base_url=resolved_base_url,
        model_name=resolved_model_name,
        api_style=resolved_api_style,
        request_timeout_seconds=max(10, min(request_timeout_seconds, 600)),
        max_model_input_chars=max(5000, min(max_model_input_chars, 200000)),
        crawl_concurrency=max(1, min(crawl_concurrency, 3)),
        per_domain_delay_seconds=max(2.0, per_domain_delay_seconds),
        max_listing_pages=max(1, min(max_listing_pages, 200)),
        raw_cache_days=max(1, raw_cache_days),
        pdf_parser_mode=(
            pdf_parser_mode
            if pdf_parser_mode in PDF_PARSER_MODES
            else current_config.pdf_parser_mode
        ),
        content_type_rules=rules,
    )
    save_config(config)
    if api_key.strip():
        try:
            set_api_key(api_key)
        except Exception as exc:
            return _redirect("/settings", f"配置已保存，但密钥写入凭据管理器失败：{exc}", "error")
    if action == "test":
        ok, message = OpenAICompatibleProvider(config).probe()
        return _redirect("/settings", message, "success" if ok else "error")
    return _redirect(
        "/settings",
        "设置已保存；历史任务未被改动；API 密钥未写入配置文件",
    )


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request, network: bool = False):
    with SessionLocal() as session:
        sources = session.scalars(
            select(Source)
            .where(
                Source.enabled.is_(True),
                Source.deleted_at.is_(None),
            )
            .options(selectinload(Source.entries))
            .order_by(Source.id)
        ).all()
        results = run_diagnostics(sources, include_network=network)
    return _render(request, "diagnostics.html", page="diagnostics", results=results, network=network)


@app.post("/maintenance/backup")
def backup_database():
    path = create_database_backup()
    return FileResponse(path, filename=path.name)


@app.post("/maintenance/clean-cache")
def clean_cache():
    count = clean_expired_cache()
    return _redirect("/settings", f"已清理 {count} 个过期缓存文件")


@app.post("/maintenance/history/purge")
def purge_history(confirm_text: Annotated[str, Form()] = ""):
    if confirm_text.strip() != "清空全部历史":
        return _redirect("/settings", "确认词不正确，未清理任何数据", "warning")
    if run_is_active():
        return _redirect("/settings", "有任务正在运行，不能清理历史", "warning")
    archive, removed = purge_history_with_backup()
    return _redirect(
        "/settings",
        f"已清理 {removed} 个历史任务；备份已保存为 {archive.name}",
    )


@app.get("/health")
def health():
    return {"status": "ok", "database_version": CURRENT_SCHEMA_VERSION}
