from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from sqlalchemy import select

from .config import load_config
from .constants import ALLOWED_DEEP_TYPES, EXCLUDED_TYPES, CandidateStatus, CoverageStatus, Decision, RunStatus
from .crawler import DiscoveredLink, DomainRateLimiter, WebCrawler
from .database import SessionLocal, session_scope
from .llm import OpenAICompatibleProvider
from .models import Assessment, Candidate, CrawlRun, Source, SourceRun, utcnow
from .url_utils import canonicalize_url


_run_lock = threading.Lock()
_worker: threading.Thread | None = None
logger = logging.getLogger(__name__)


def create_run(start_date: date, end_date: date, source_ids: list[int] | None = None) -> int:
    if end_date < start_date:
        raise ValueError("结束日期不得早于起始日期")
    with session_scope() as session:
        query = select(Source).where(Source.enabled.is_(True))
        if source_ids:
            query = query.where(Source.id.in_(source_ids))
        sources = session.scalars(query.order_by(Source.id)).all()
        if not sources:
            raise ValueError("没有已启用的智库来源")
        run = CrawlRun(start_date=start_date, end_date=end_date, status=RunStatus.QUEUED)
        session.add(run)
        session.flush()
        for source in sources:
            session.add(SourceRun(run_id=run.id, source_id=source.id))
        return run.id


def start_run_in_background(run_id: int) -> bool:
    global _worker
    if _worker and _worker.is_alive():
        return False
    _worker = threading.Thread(target=execute_run, args=(run_id,), name=f"crawl-run-{run_id}", daemon=True)
    _worker.start()
    return True


def _hard_filter(candidate: Candidate, start_date: date, end_date: date) -> tuple[str, str]:
    if candidate.date_status != "confirmed" or candidate.published_date is None:
        return CandidateStatus.NEEDS_REVIEW, "发布日期缺失、冲突或仅来自 PDF 文件元数据"
    if not start_date <= candidate.published_date <= end_date:
        return CandidateStatus.HARD_EXCLUDED, "发布日期不在指定闭区间"
    if candidate.extraction_status != "complete":
        return CandidateStatus.NEEDS_REVIEW, "正文提取不完整、失败或疑似扫描 PDF"
    if candidate.content_type in EXCLUDED_TYPES:
        return CandidateStatus.HARD_EXCLUDED, f"明确排除的体裁: {candidate.content_type}"
    if candidate.language == "zh":
        count, threshold = candidate.chinese_char_count, 3000
    else:
        count, threshold = candidate.english_word_count, 1500
    if count < threshold:
        if count >= threshold * 0.8 or candidate.content_type in ALLOWED_DEEP_TYPES:
            return CandidateStatus.NEEDS_REVIEW, f"篇幅临界或与体裁证据冲突: {count}/{threshold}"
        return CandidateStatus.HARD_EXCLUDED, f"篇幅低于门槛: {count}/{threshold}"
    if candidate.content_type == "unknown":
        return CandidateStatus.NEEDS_REVIEW, "无法确定体裁"
    if candidate.content_type not in ALLOWED_DEEP_TYPES:
        return CandidateStatus.NEEDS_REVIEW, f"体裁需要人工确认: {candidate.content_type}"
    return CandidateStatus.DISCOVERED, ""


def _persist_candidate(run_id: int, source_id: int, link: DiscoveredLink, crawler: WebCrawler) -> None:
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        source = session.get(Source, source_id)
        if not run or not source:
            return
        canonical = canonicalize_url(link.url)
        existing = session.scalar(
            select(Candidate).where(Candidate.run_id == run_id, Candidate.canonical_url == canonical)
        )
        if existing:
            return
        candidate = Candidate(
            run_id=run_id,
            source_id=source_id,
            canonical_url=canonical,
            original_url=link.url,
            title_original=link.listing_title,
        )
        session.add(candidate)
        session.flush()
        try:
            document, resource = crawler.extract(link)
            candidate.title_original = document.title or link.listing_title
            candidate.published_date = document.published_date
            candidate.date_status = document.date_status
            candidate.date_evidence = document.date_evidence
            candidate.content_type = document.content_type
            candidate.content_type_evidence = document.content_type_evidence
            candidate.language = document.language
            candidate.english_word_count = document.english_word_count
            candidate.chinese_char_count = document.chinese_char_count
            candidate.extraction_status = document.extraction_status
            candidate.content_hash = document.content_hash
            candidate.extracted_text = document.text
            candidate.cache_path = str(resource.cache_path.relative_to(resource.cache_path.parents[2]))
            candidate.status, candidate.hard_filter_reason = _hard_filter(candidate, run.start_date, run.end_date)
            if document.warnings:
                candidate.hard_filter_reason = "; ".join(filter(None, [candidate.hard_filter_reason, *document.warnings]))
        except Exception as exc:
            candidate.extraction_status = "failed"
            candidate.status = CandidateStatus.NEEDS_REVIEW
            candidate.hard_filter_reason = f"抓取或提取失败: {exc}"
        if candidate.status == CandidateStatus.DISCOVERED:
            provider = OpenAICompatibleProvider(load_config())
            if not provider.configured():
                candidate.status = CandidateStatus.NEEDS_REVIEW
                candidate.hard_filter_reason = "模型未配置，确定性硬过滤已通过"
                return
            result = provider.assess(
                candidate.title_original, source.name, candidate.content_type, candidate.extracted_text
            )
            if not result.payload:
                candidate.status = CandidateStatus.NEEDS_REVIEW
                candidate.hard_filter_reason = result.error
                session.add(
                    Assessment(
                        candidate_id=candidate.id,
                        decision=Decision.NEEDS_REVIEW,
                        provider_model=provider.config.model_name,
                        input_chars=min(len(candidate.extracted_text), provider.config.max_model_input_chars),
                        raw_response=result.raw_response,
                        error_message=result.error,
                    )
                )
                return
            payload = result.payload
            candidate.title_en = payload.title_en
            candidate.title_zh = payload.title_zh
            candidate.status = (
                CandidateStatus.MODEL_INCLUDED
                if payload.decision == Decision.INCLUDE
                else CandidateStatus.MODEL_EXCLUDED
                if payload.decision == Decision.EXCLUDE
                else CandidateStatus.NEEDS_REVIEW
            )
            session.add(
                Assessment(
                    candidate_id=candidate.id,
                    decision=payload.decision,
                    primary_category=payload.primary_category or "",
                    secondary_categories_json=json.dumps(payload.secondary_categories),
                    core_claim_zh=payload.core_claim_zh,
                    reasoning_zh=payload.reasoning_zh,
                    evidence_json=json.dumps(payload.evidence, ensure_ascii=False),
                    confidence=payload.confidence,
                    provider_model=provider.config.model_name,
                    input_chars=min(len(candidate.extracted_text), provider.config.max_model_input_chars),
                    raw_response=result.raw_response,
                )
            )


def _execute_source(run_id: int, source_run_id: int, limiter: DomainRateLimiter) -> None:
    config = load_config()
    crawler = WebCrawler(config, limiter)
    try:
        with session_scope() as session:
            source_run = session.get(SourceRun, source_run_id)
            if not source_run:
                return
            source_run.status = CoverageStatus.RUNNING
            source_run.started_at = utcnow()
            source = session.get(Source, source_run.source_id)
            run = session.get(CrawlRun, run_id)
            source_id = source.id
            publication_url = source.publication_url or source.homepage_url
            ordered = source.ordered_by_date
            adapter_key = source.adapter_key
            start_date = run.start_date
            end_date = run.end_date
        scan = crawler.scan_listing(publication_url, start_date, ordered, adapter_key, end_date)
        for link in scan.links:
            _persist_candidate(run_id, source_id, link, crawler)
        with session_scope() as session:
            source_run = session.get(SourceRun, source_run_id)
            source = session.get(Source, source_id)
            run = session.get(CrawlRun, run_id)
            source_run.status = scan.coverage_status
            source_run.pages_scanned = scan.pages_scanned
            source_run.links_discovered = len(scan.links)
            source_run.earliest_date = scan.earliest_date
            source_run.latest_date = scan.latest_date
            source_run.stop_reason = scan.stop_reason
            source_run.error_message = "; ".join(scan.errors)
            source_run.completed_at = utcnow()
            if scan.coverage_status == CoverageStatus.COMPLETE:
                source.last_successful_date = run.end_date
    except Exception as exc:
        logger.exception("source run failed run_id=%s source_run_id=%s", run_id, source_run_id)
        with session_scope() as session:
            source_run = session.get(SourceRun, source_run_id)
            if source_run:
                source_run.status = CoverageStatus.FAILED
                source_run.error_message = str(exc)
                source_run.completed_at = utcnow()
    finally:
        crawler.close()


def execute_run(run_id: int) -> None:
    if not _run_lock.acquire(blocking=False):
        return
    try:
        with session_scope() as session:
            run = session.get(CrawlRun, run_id)
            if not run:
                return
            run.status = RunStatus.RUNNING
            run.started_at = utcnow()
            source_run_ids = [row.id for row in run.source_runs]
        config = load_config()
        limiter = DomainRateLimiter(config.per_domain_delay_seconds)
        with ThreadPoolExecutor(max_workers=config.crawl_concurrency) as executor:
            futures = [executor.submit(_execute_source, run_id, source_run_id, limiter) for source_run_id in source_run_ids]
            for future in as_completed(futures):
                future.result()
        with session_scope() as session:
            run = session.get(CrawlRun, run_id)
            statuses = {row.status for row in run.source_runs}
            run.completed_at = utcnow()
            if CoverageStatus.FAILED in statuses or CoverageStatus.INCOMPLETE in statuses:
                run.status = RunStatus.COMPLETED_WITH_ISSUES
            else:
                run.status = RunStatus.COMPLETED
    except Exception as exc:
        logger.exception("crawl run failed run_id=%s", run_id)
        with session_scope() as session:
            run = session.get(CrawlRun, run_id)
            if run:
                run.status = RunStatus.FAILED
                run.error_message = str(exc)
                run.completed_at = utcnow()
    finally:
        _run_lock.release()


def mark_interrupted_runs() -> int:
    with session_scope() as session:
        rows = session.scalars(select(CrawlRun).where(CrawlRun.status == RunStatus.RUNNING)).all()
        for run in rows:
            run.status = RunStatus.INTERRUPTED
            run.error_message = "程序上次在任务完成前退出，可创建新任务或重跑单个来源"
        return len(rows)
