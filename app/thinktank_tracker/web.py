from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import desc, func, select
from sqlalchemy.orm import selectinload

from .config import AppConfig, get_api_key, load_config, save_config, set_api_key
from .constants import CATEGORY_LABELS, CandidateStatus, CoverageStatus, Decision, RunStatus
from .database import SessionLocal, initialize_database, session_scope
from .exporters import export_audit_workbook, export_report
from .importers import parse_docx_sources
from .maintenance import clean_expired_cache, create_database_backup, run_diagnostics
from .logging_setup import configure_logging
from .models import Candidate, CrawlRun, Source, SourceRun
from .paths import PATHS
from .review_service import save_review
from .run_service import create_run, mark_interrupted_runs, start_run_in_background
from .schemas import ImportedSource
from .source_service import parse_sources_json, save_sources_json, upsert_sources


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
    status_label=lambda value: STATUS_LABELS.get(value, value),
    category_label=lambda value: CATEGORY_LABELS.get(value, value),
    categories=CATEGORY_LABELS,
)
templates.env.filters["from_json"] = lambda value: json.loads(value or "[]")


def _render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=name, context=context)


def _redirect(path: str, message: str = "", level: str = "success") -> RedirectResponse:
    from urllib.parse import urlencode

    query = f"?{urlencode({'message': message, 'level': level})}" if message else ""
    return RedirectResponse(f"{path}{query}", status_code=303)


def _default_range(session) -> tuple[date, date]:
    today = date.today()
    last = session.scalar(select(func.max(Source.last_successful_date)).where(Source.enabled.is_(True)))
    next_unsearched = (last + timedelta(days=1)) if last else today
    return (min(next_unsearched, today), today)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with SessionLocal() as session:
        source_count = session.scalar(select(func.count(Source.id)).where(Source.enabled.is_(True))) or 0
        pending_count = session.scalar(
            select(func.count(Candidate.id)).where(Candidate.status.in_([CandidateStatus.NEEDS_REVIEW, CandidateStatus.MODEL_INCLUDED]))
        ) or 0
        approved_count = session.scalar(select(func.count(Candidate.id)).where(Candidate.status == CandidateStatus.APPROVED)) or 0
        recent_runs = session.scalars(
            select(CrawlRun)
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
            default_start=start_date,
            default_end=end_date,
        )


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    with SessionLocal() as session:
        sources = session.scalars(select(Source).order_by(Source.id)).all()
        return _render(request, "sources.html", page="sources", sources=sources)


@app.post("/sources/import")
async def preview_source_import(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        sources = parse_sources_json(raw) if (file.filename or "").lower().endswith(".json") else parse_docx_sources(raw)
    except (ValueError, ValidationError, json.JSONDecodeError, KeyError) as exc:
        return _redirect("/sources", f"导入失败：{exc}", "error")
    if not sources:
        return _redirect("/sources", "文件中未识别到智库链接", "error")
    PATHS.ensure()
    PREVIEW_PATH.write_text(
        json.dumps([item.model_dump(mode="json") for item in sources], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return _redirect("/sources/preview", f"已解析 {len(sources)} 个来源，请确认")


@app.get("/sources/preview", response_class=HTMLResponse)
def source_preview(request: Request):
    if not PREVIEW_PATH.exists():
        return _redirect("/sources", "没有待确认的导入清单", "error")
    rows = [ImportedSource.model_validate(item) for item in json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))]
    return _render(request, "source_preview.html", page="sources", rows=rows)


@app.post("/sources/confirm")
def confirm_source_import():
    if not PREVIEW_PATH.exists():
        return _redirect("/sources", "导入预览已失效", "error")
    rows = [ImportedSource.model_validate(item) for item in json.loads(PREVIEW_PATH.read_text(encoding="utf-8"))]
    with session_scope() as session:
        created, updated = upsert_sources(session, rows)
    PREVIEW_PATH.unlink(missing_ok=True)
    return _redirect("/sources", f"来源清单已确认：新增 {created}，更新 {updated}")


@app.post("/sources/save")
def save_source(
    source_id: Annotated[int | None, Form()] = None,
    name: Annotated[str, Form()] = "",
    group_name: Annotated[str, Form()] = "",
    homepage_url: Annotated[str, Form()] = "",
    publication_url: Annotated[str, Form()] = "",
    adapter_key: Annotated[str, Form()] = "generic",
    ordered_by_date: Annotated[str | None, Form()] = None,
    enabled: Annotated[str | None, Form()] = None,
):
    try:
        item = ImportedSource(
            name=name,
            group_name=group_name,
            homepage_url=homepage_url,
            publication_url=publication_url or homepage_url,
            adapter_key=adapter_key or "generic",
            ordered_by_date=ordered_by_date is not None,
            enabled=enabled is not None if source_id else True,
        )
    except ValidationError as exc:
        return _redirect("/sources", f"来源字段无效：{exc.errors()[0]['msg']}", "error")
    with session_scope() as session:
        if source_id:
            row = session.get(Source, source_id)
            if not row:
                return _redirect("/sources", "来源不存在", "error")
            row.name = item.name
            row.group_name = item.group_name
            row.homepage_url = str(item.homepage_url)
            row.publication_url = str(item.publication_url)
            row.adapter_key = item.adapter_key
            row.ordered_by_date = item.ordered_by_date
            row.enabled = enabled is not None
        else:
            upsert_sources(session, [item])
    return _redirect("/sources", "来源已保存")


@app.get("/sources/export")
def export_sources():
    with SessionLocal() as session:
        path = save_sources_json(session, PATHS.exports / "sources.json")
    return FileResponse(path, filename="sources.json", media_type="application/json")


@app.post("/runs")
def new_run(
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
    source_ids: Annotated[list[int] | None, Form()] = None,
):
    try:
        run_id = create_run(start_date, end_date, source_ids or [])
    except ValueError as exc:
        return _redirect("/", str(exc), "error")
    if not start_run_in_background(run_id):
        return _redirect(f"/runs/{run_id}", "已有任务正在运行；新任务已保留为等待状态", "warning")
    return _redirect(f"/runs/{run_id}", "任务已启动")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, status: str = ""):
    with SessionLocal() as session:
        run = session.scalar(
            select(CrawlRun).where(CrawlRun.id == run_id).options(selectinload(CrawlRun.source_runs).selectinload(SourceRun.source))
        )
        if not run:
            raise HTTPException(404, "任务不存在")
        query = (
            select(Candidate)
            .where(Candidate.run_id == run_id)
            .options(selectinload(Candidate.source), selectinload(Candidate.assessment), selectinload(Candidate.review))
            .order_by(Candidate.source_id, Candidate.published_date.desc(), Candidate.id)
        )
        if status:
            query = query.where(Candidate.status == status)
        candidates = session.scalars(query).all()
        return _render(request, "run_detail.html", page="runs", run=run, candidates=candidates, status_filter=status)


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: int):
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if not run:
            raise HTTPException(404, "任务不存在")
        if run.status not in {RunStatus.QUEUED, RunStatus.INTERRUPTED, RunStatus.FAILED}:
            return _redirect(f"/runs/{run_id}", "当前任务状态不能恢复", "warning")
        run.status = RunStatus.QUEUED
    started = start_run_in_background(run_id)
    return _redirect(f"/runs/{run_id}", "任务已恢复" if started else "已有任务正在运行", "success" if started else "warning")


@app.post("/runs/{run_id}/sources/{source_id}/rerun")
def rerun_source(run_id: int, source_id: int):
    with SessionLocal() as session:
        run = session.get(CrawlRun, run_id)
        source = session.get(Source, source_id)
        if not run or not source:
            raise HTTPException(404, "任务或来源不存在")
        start_date, end_date = run.start_date, run.end_date
    new_run_id = create_run(start_date, end_date, [source_id])
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
            .where(Candidate.id == candidate_id)
            .options(selectinload(Candidate.source), selectinload(Candidate.assessment), selectinload(Candidate.review))
        )
        if not candidate:
            raise HTTPException(404, "候选不存在")
        secondary = []
        if candidate.review:
            secondary = json.loads(candidate.review.secondary_categories_json or "[]")
        elif candidate.assessment:
            secondary = json.loads(candidate.assessment.secondary_categories_json or "[]")
        return _render(request, "candidate.html", page="review", candidate=candidate, secondary=secondary)


@app.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, status: str = "pending"):
    with SessionLocal() as session:
        query = (
            select(Candidate)
            .options(selectinload(Candidate.source), selectinload(Candidate.assessment))
            .order_by(desc(Candidate.created_at))
        )
        if status == "pending":
            query = query.where(Candidate.status.in_([CandidateStatus.NEEDS_REVIEW, CandidateStatus.MODEL_INCLUDED]))
        elif status:
            query = query.where(Candidate.status == status)
        candidates = session.scalars(query.limit(500)).all()
        return _render(request, "review_queue.html", page="review", candidates=candidates, status_filter=status)


@app.post("/candidates/{candidate_id}/review")
def review_candidate(
    candidate_id: int,
    decision: Annotated[str, Form()],
    primary_category: Annotated[str, Form()] = "",
    secondary_categories: Annotated[list[str] | None, Form()] = None,
    title_en: Annotated[str, Form()] = "",
    title_zh: Annotated[str, Form()] = "",
    content_type: Annotated[str, Form()] = "",
    core_claim_zh: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        if not candidate:
            raise HTTPException(404, "候选不存在")
        run_id = candidate.run_id
        try:
            save_review(
                session,
                candidate_id,
                decision,
                primary_category,
                secondary_categories or [],
                title_en,
                title_zh,
                content_type,
                core_claim_zh,
                notes,
            )
        except ValueError as exc:
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
def settings_page(request: Request):
    return _render(request, "settings.html", page="settings", config=load_config(), has_key=bool(get_api_key()))


@app.post("/settings")
def update_settings(
    api_base_url: Annotated[str, Form()] = "",
    model_name: Annotated[str, Form()] = "",
    api_style: Annotated[str, Form()] = "chat_completions",
    api_key: Annotated[str, Form()] = "",
    request_timeout_seconds: Annotated[int, Form()] = 120,
    max_model_input_chars: Annotated[int, Form()] = 60000,
    crawl_concurrency: Annotated[int, Form()] = 3,
    per_domain_delay_seconds: Annotated[float, Form()] = 2.0,
    max_listing_pages: Annotated[int, Form()] = 25,
    raw_cache_days: Annotated[int, Form()] = 90,
):
    config = AppConfig(
        api_base_url=api_base_url.strip(),
        model_name=model_name.strip(),
        api_style=api_style if api_style in {"chat_completions", "responses"} else "chat_completions",
        request_timeout_seconds=max(10, min(request_timeout_seconds, 600)),
        max_model_input_chars=max(5000, min(max_model_input_chars, 200000)),
        crawl_concurrency=max(1, min(crawl_concurrency, 3)),
        per_domain_delay_seconds=max(2.0, per_domain_delay_seconds),
        max_listing_pages=max(1, min(max_listing_pages, 200)),
        raw_cache_days=max(1, raw_cache_days),
    )
    save_config(config)
    if api_key.strip():
        try:
            set_api_key(api_key)
        except Exception as exc:
            return _redirect("/settings", f"配置已保存，但密钥写入凭据管理器失败：{exc}", "error")
    return _redirect("/settings", "设置已保存；API 密钥未写入配置文件")


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request, network: bool = False):
    with SessionLocal() as session:
        sources = session.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.id)).all()
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


@app.get("/health")
def health():
    return {"status": "ok", "database_version": 1}
