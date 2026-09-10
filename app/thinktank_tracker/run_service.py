from __future__ import annotations

import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .config import AppConfig, load_config
from .configuration_service import ensure_default_configuration, get_active_configuration
from .constants import (
    ALLOWED_DEEP_TYPES,
    EXCLUDED_TYPES,
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
from .crawler import (
    AttachmentResult,
    DiscoveredLink,
    DomainRateLimiter,
    FetchError,
    PageScanAudit,
    WebCrawler,
)
from .database import SessionLocal, session_scope
from .extractors import ExtractedDocument, parse_date_value
from .llm import OpenAICompatibleProvider
from .selection import extraction_gate, fact_issues, policy_snapshot, run_policy
from .models import (
    Assessment,
    Candidate,
    CandidateAttachment,
    CandidateDiscovery,
    CrawlRun,
    DateAssistBatch,
    ListingPageAudit,
    Source,
    SourceEntry,
    SourceEntryRun,
    SourceRun,
    utcnow,
)
from .url_utils import (
    canonicalize_url,
    collect_content_type_evidence,
    normalize_fetch_url,
    resolve_content_type,
)


_run_lock = threading.Lock()
_worker: threading.Thread | None = None
logger = logging.getLogger(__name__)


def create_run(
    start_date: date,
    end_date: date,
    source_ids: list[int] | None = None,
    *,
    configuration_id: int | None = None,
    entry_ids: list[int] | None = None,
    mark_configuration_used: bool = True,
) -> int:
    if end_date < start_date:
        raise ValueError("结束日期不得早于起始日期")
    with session_scope() as session:
        configuration = (
            get_active_configuration(session, configuration_id)
            if configuration_id is not None
            else ensure_default_configuration(session)
        )
        query = select(Source).where(
            Source.configuration_id == configuration.id,
            Source.enabled.is_(True),
            Source.deleted_at.is_(None),
        )
        if source_ids:
            query = query.where(Source.id.in_(source_ids))
        sources = session.scalars(query.order_by(Source.id)).all()
        selected_entry_ids = set(entry_ids or [])
        active = [
            (
                source,
                [
                    entry
                    for entry in source.entries
                    if entry.enabled
                    and entry.deleted_at is None
                    and (not selected_entry_ids or entry.id in selected_entry_ids)
                ],
            )
            for source in sources
        ]
        active = [(source, entries) for source, entries in active if entries]
        if not active:
            raise ValueError(f"配置“{configuration.name}”中没有可用的智库检索入口")
        run = CrawlRun(
            selection_policy_json=json.dumps(policy_snapshot(load_config()), ensure_ascii=False),
            configuration_id=configuration.id,
            configuration_name_snapshot=configuration.name,
            start_date=start_date,
            end_date=end_date,
            status=RunStatus.QUEUED,
        )
        session.add(run)
        session.flush()
        for source, entries in active:
            source_run = SourceRun(
                run_id=run.id,
                source_id=source.id,
                source_name_snapshot=source.name,
                source_homepage_snapshot=source.homepage_url,
            )
            session.add(source_run)
            session.flush()
            for entry in entries:
                snapshot = {
                    "canonical_url": entry.canonical_url,
                    "adapter_key": entry.adapter_key,
                    "ordered_by_date": entry.ordered_by_date,
                    "listing_selectors": _json_string_list(entry.listing_selectors_json),
                    "container_selectors": _json_string_list(entry.container_selectors_json),
                    "content_type_selectors": _json_string_list(entry.content_type_selectors_json),
                    "date_selectors": _json_string_list(entry.date_selectors_json),
                    "allowed_path_patterns": _json_string_list(entry.allowed_path_patterns_json),
                    "excluded_path_patterns": _json_string_list(entry.excluded_path_patterns_json),
                }
                session.add(
                    SourceEntryRun(
                        run_id=run.id,
                        source_run_id=source_run.id,
                        source_entry_id=entry.id,
                        entry_url_snapshot=entry.url,
                        entry_config_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
                    )
                )
        if mark_configuration_used:
            configuration.last_used_at = utcnow()
        return run.id


def start_run_in_background(run_id: int) -> bool:
    global _worker
    if _worker and _worker.is_alive():
        return False
    _worker = threading.Thread(target=execute_run, args=(run_id,), name=f"crawl-run-{run_id}", daemon=True)
    _worker.start()
    return True


def run_is_active() -> bool:
    return bool(_worker and _worker.is_alive())


def _hard_filter(
    candidate: Candidate,
    start_date: date,
    end_date: date,
    allowed_content_types: tuple[str, ...] | frozenset[str] = ALLOWED_DEEP_TYPES,
    excluded_content_types: tuple[str, ...] | frozenset[str] = frozenset(EXCLUDED_TYPES),
) -> tuple[str, str]:
    policy = policy_snapshot(AppConfig())
    policy["allowed_types"] = list(allowed_content_types)
    policy["excluded_types"] = list(excluded_content_types)
    return extraction_gate(candidate, start_date, end_date, policy)


def _resolve_candidate_date(
    document: ExtractedDocument, link: DiscoveredLink
) -> tuple[date | None, str, str]:
    published = document.published_date
    status = document.date_status
    evidence = document.date_evidence or ""
    if link.listing_date is None:
        return published, status, evidence
    listing_evidence = link.listing_date_evidence or "栏目文章卡片日期"
    listing_text = f"{listing_evidence}: {link.listing_date.isoformat()}"
    if status == "conflict":
        return None, "conflict", "；".join(filter(None, [evidence, listing_text]))
    if published is None:
        return link.listing_date, "confirmed", listing_text
    if published != link.listing_date:
        conflict = (
            f"详情页日期 {published.isoformat()} 与栏目卡片日期 "
            f"{link.listing_date.isoformat()} 冲突"
        )
        return None, "conflict", "；".join(filter(None, [evidence, listing_text, conflict]))
    return published, "confirmed", "；".join(filter(None, [evidence, listing_text]))


def _resolve_candidate_content_type(
    document: ExtractedDocument | None,
    link: DiscoveredLink,
) -> tuple[str, str]:
    listing_type = link.listing_content_type.strip()
    listing_evidence = link.listing_content_type_evidence.strip()
    detail_type = document.content_type.strip() if document else "unknown"
    detail_evidence = document.content_type_evidence.strip() if document else ""
    if "体裁冲突" in listing_evidence or "体裁冲突" in detail_evidence:
        return "unknown", "；".join(filter(None, [listing_evidence, detail_evidence]))
    detail_known = bool(detail_type and detail_type != "unknown")
    listing_known = bool(listing_type and listing_type != "unknown")
    if detail_known and listing_known:
        if detail_type != listing_type:
            return (
                "unknown",
                f"体裁冲突：{listing_evidence or listing_type}；{detail_evidence or detail_type}",
            )
        return detail_type, "；".join(filter(None, [listing_evidence, detail_evidence]))
    if detail_known:
        return detail_type, "；".join(filter(None, [detail_evidence, listing_evidence]))
    if listing_known:
        return listing_type, listing_evidence or "栏目卡片体裁标签"
    return "unknown", "；".join(filter(None, [listing_evidence, detail_evidence]))


def reclassify_run_content_types(session, run_id: int, rules) -> int:
    """Re-evaluate one task from saved deterministic evidence without networking."""
    run = session.get(CrawlRun, run_id)
    if run is None:
        return 0
    policy = run_policy(run)
    refreshed = policy_snapshot(AppConfig(content_type_rules=rules))
    for key in ("allowed_types", "excluded_types", "content_type_rules"):
        policy[key] = refreshed[key]
    run.selection_policy_json = json.dumps(policy, ensure_ascii=False)
    candidates = session.scalars(
        select(Candidate)
        .where(Candidate.run_id == run_id)
        .options(
            selectinload(Candidate.discoveries),
            selectinload(Candidate.review),
            selectinload(Candidate.assessment),
        )
    ).all()
    changed = 0
    for candidate in candidates:
        if candidate.review is not None or candidate.status == CandidateStatus.APPROVED:
            continue
        listing_labels: list[str] = []
        listing_types: list[tuple[str, str, str]] = []
        structured_items: list[dict[str, object]] = []
        for discovery in candidate.discoveries:
            try:
                labels = json.loads(discovery.listing_content_type_labels_json or "[]")
            except (TypeError, json.JSONDecodeError):
                labels = []
            labels = [str(value).strip() for value in labels if str(value).strip()]
            content_type, evidence, action = resolve_content_type(
                discovery.fetch_url or candidate.original_url,
                labels,
                rules,
            )
            items = collect_content_type_evidence(
                discovery.fetch_url or candidate.original_url,
                labels,
                rules,
                label_location="listing_card",
            )
            discovery.listing_content_type = "" if content_type == "unknown" else content_type
            discovery.listing_content_type_evidence = evidence
            discovery.listing_content_type_evidence_json = json.dumps(
                [item.to_dict() for item in items], ensure_ascii=False
            )
            listing_labels.extend(labels)
            structured_items.extend(item.to_dict() for item in items)
            if content_type != "unknown":
                listing_types.append((content_type, evidence, action))

        listing_labels = list(dict.fromkeys(listing_labels))
        if listing_types:
            distinct = {(value, action) for value, _, action in listing_types}
            if len(distinct) == 1:
                listing_type, listing_action = next(iter(distinct))
                listing_evidence = "；".join(
                    dict.fromkeys(evidence for _, evidence, _ in listing_types if evidence)
                )
            else:
                listing_type, listing_action = "unknown", "review"
                listing_evidence = "体裁冲突：" + "；".join(
                    dict.fromkeys(evidence for _, evidence, _ in listing_types if evidence)
                )
        else:
            listing_type, listing_evidence, listing_action = resolve_content_type(
                candidate.original_url,
                listing_labels,
                rules,
            )

        try:
            old_items = json.loads(candidate.content_type_evidence_json or "[]")
        except (TypeError, json.JSONDecodeError):
            old_items = []
        detail_labels = list(
            dict.fromkeys(
                str(item.get("raw_value", "")).strip()
                for item in old_items
                if isinstance(item, dict)
                and item.get("location") == "detail_header"
                and str(item.get("raw_value", "")).strip()
            )
        )
        detail_type, detail_evidence, detail_action = resolve_content_type(
            candidate.original_url,
            detail_labels,
            rules,
        )
        detail_items = collect_content_type_evidence(
            candidate.original_url,
            detail_labels,
            rules,
            label_location="detail_header",
        )
        structured_items.extend(item.to_dict() for item in detail_items)

        listing_known = listing_type != "unknown"
        detail_known = detail_type != "unknown"
        if listing_known and detail_known and (
            listing_type != detail_type or listing_action != detail_action
        ):
            resolved_type = "unknown"
            action = "review"
            evidence = f"体裁冲突：{listing_evidence}；{detail_evidence}"
        elif listing_known:
            resolved_type, evidence, action = listing_type, listing_evidence, listing_action
            if detail_known:
                evidence = "；".join(filter(None, [listing_evidence, detail_evidence]))
        elif detail_known:
            resolved_type, evidence, action = detail_type, detail_evidence, detail_action
        else:
            resolved_type, evidence, action = "unknown", "；".join(
                filter(None, [listing_evidence, detail_evidence])
            ), "review"

        before = (candidate.content_type, candidate.content_type_evidence)
        candidate.content_type = resolved_type
        candidate.content_type_evidence = evidence
        candidate.content_type_raw_labels_json = json.dumps(
            list(dict.fromkeys([*listing_labels, *detail_labels])), ensure_ascii=False
        )
        candidate.content_type_evidence_json = json.dumps(
            structured_items, ensure_ascii=False
        )

        previous_topic = candidate.topic_status
        next_status, reason = extraction_gate(candidate, run.start_date, run.end_date, policy)
        candidate.topic_status = previous_topic
        candidate.hard_filter_reason = reason
        if next_status == CandidateStatus.HARD_EXCLUDED:
            candidate.status = next_status
        elif candidate.assessment is not None:
            audit = json.loads(candidate.metadata_json or "{}").get("selection", {})
            verified = audit.get("evidence_verified") and audit.get("input_complete") and audit.get("verification_status") == "supported"
            if verified and previous_topic == TopicStatus.MODEL_INCLUDED:
                candidate.status = CandidateStatus.MODEL_INCLUDED if not reason else CandidateStatus.NEEDS_REVIEW
            elif verified and previous_topic == TopicStatus.MODEL_EXCLUDED:
                candidate.status = CandidateStatus.MODEL_EXCLUDED
            else:
                candidate.status = CandidateStatus.NEEDS_REVIEW
                candidate.hard_filter_reason = "；".join(filter(None, [reason, "旧判断需按新证据流程重新核验"]))
        else:
            candidate.status = next_status
        if before != (candidate.content_type, candidate.content_type_evidence):
            changed += 1
    return changed


def _resolve_candidate_authors(
    document: ExtractedDocument | None,
    link: DiscoveredLink,
) -> tuple[list[str], str]:
    if document and document.authors:
        return list(document.authors), document.author_evidence
    if link.listing_authors:
        return list(link.listing_authors), link.listing_author_evidence
    return [], (document.author_evidence if document else "") or link.listing_author_evidence


def _add_discovery(
    session,
    candidate_id: int,
    source_entry_id: int,
    link: DiscoveredLink,
) -> None:
    existing = session.scalar(
        select(CandidateDiscovery).where(
            CandidateDiscovery.candidate_id == candidate_id,
            CandidateDiscovery.source_entry_id == source_entry_id,
            CandidateDiscovery.listing_url == link.listing_url,
        )
    )
    if existing is None:
        session.add(
            CandidateDiscovery(
                candidate_id=candidate_id,
                source_entry_id=source_entry_id,
                listing_url=link.listing_url,
                fetch_url=link.fetch_url,
                listing_title=link.listing_title,
                listing_date=link.listing_date,
                listing_date_evidence=link.listing_date_evidence,
                listing_content_type=link.listing_content_type,
                listing_content_type_evidence=link.listing_content_type_evidence,
                listing_content_type_labels_json=json.dumps(
                    list(link.listing_content_type_labels), ensure_ascii=False
                ),
                listing_content_type_evidence_json=json.dumps(
                    [item.to_dict() for item in link.listing_content_type_evidence_items],
                    ensure_ascii=False,
                ),
                listing_authors_json=json.dumps(list(link.listing_authors), ensure_ascii=False),
                listing_author_evidence=link.listing_author_evidence,
                card_position=link.card_position,
                discovery_decision="accepted",
            )
        )


def _persist_candidate(
    run_id: int,
    source_id: int,
    source_entry_id: int,
    link: DiscoveredLink,
    crawler: WebCrawler,
) -> None:
    crawler_config = getattr(crawler, "config", None)
    allowed_content_types = getattr(crawler_config, "allowed_content_types", ALLOWED_DEEP_TYPES)
    excluded_content_types = getattr(
        crawler_config, "excluded_content_types", frozenset(EXCLUDED_TYPES)
    )
    candidate_id: int | None = None
    source_name = ""
    run_start: date | None = None
    run_end: date | None = None
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        source = session.get(Source, source_id)
        if not run or not source:
            return
        source_run = session.scalar(
            select(SourceRun).where(
                SourceRun.run_id == run_id,
                SourceRun.source_id == source_id,
            )
        )
        if link.resolved_date is None and link.listing_date is not None:
            link.resolved_date = link.listing_date
            link.resolved_date_status = "confirmed"
            link.resolved_date_evidence = link.listing_date_evidence or "栏目卡片日期"
        elif (
            link.resolved_date_status == "confirmed"
            and not link.resolved_date_evidence
            and link.listing_date_evidence
        ):
            link.resolved_date_evidence = link.listing_date_evidence
        if link.resolved_date_status == "confirmed" and link.resolved_date is not None:
            if link.resolved_date < run.start_date:
                link.date_scope = DateScope.BEFORE_START
            elif link.resolved_date > run.end_date:
                link.date_scope = DateScope.AFTER_END
            else:
                link.date_scope = DateScope.IN_WINDOW
        canonical = link.canonical_url
        existing = session.scalar(
            select(Candidate).where(Candidate.run_id == run_id, Candidate.canonical_url == canonical)
        )
        if existing:
            _add_discovery(session, existing.id, source_entry_id, link)
            if existing.review is not None or existing.extraction_status not in {"pending", "failed"}:
                return
            candidate = existing
        else:
            candidate = Candidate(
                run_id=run_id,
                source_id=source_id,
                canonical_url=canonical,
                original_url=link.fetch_url,
                title_original=link.listing_title,
                published_date=link.resolved_date,
                date_status=link.resolved_date_status,
                date_evidence=link.resolved_date_evidence,
                date_scope=link.date_scope,
                content_type=link.listing_content_type or "unknown",
                content_type_evidence=link.listing_content_type_evidence,
                content_type_raw_labels_json=json.dumps(
                    list(link.listing_content_type_labels), ensure_ascii=False
                ),
                content_type_evidence_json=json.dumps(
                    [item.to_dict() for item in link.listing_content_type_evidence_items],
                    ensure_ascii=False,
                ),
                authors_json=json.dumps(list(link.listing_authors), ensure_ascii=False),
                author_evidence=link.listing_author_evidence,
                extraction_status=(
                    "metadata_only"
                    if link.date_scope in {DateScope.BEFORE_START, DateScope.AFTER_END}
                    else "pending"
                ),
                metadata_json=json.dumps(
                    {"preflight_error": link.preflight_error}, ensure_ascii=False
                ),
            )
            session.add(candidate)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(Candidate).where(
                        Candidate.run_id == run_id,
                        Candidate.canonical_url == canonical,
                    )
                )
                if existing:
                    _add_discovery(session, existing.id, source_entry_id, link)
                return
        candidate_id = candidate.id
        source_name = (
            source_run.source_name_snapshot if source_run and source_run.source_name_snapshot else source.name
        )
        run_start = run.start_date
        run_end = run.end_date
        selection_policy = run_policy(run)
        _add_discovery(session, candidate_id, source_entry_id, link)

        if link.date_scope in {DateScope.BEFORE_START, DateScope.AFTER_END}:
            candidate.status, candidate.hard_filter_reason = _hard_filter(
                candidate,
                run.start_date,
                run.end_date,
                allowed_content_types,
                excluded_content_types,
            )
            return

    assert candidate_id is not None and run_start is not None and run_end is not None
    try:
        extracted = crawler.extract(link)
        if len(extracted) == 2:  # compatibility with older adapters/tests
            document, resource = extracted
            attachments: list[AttachmentResult] = []
        else:
            document, resource, attachments = extracted
    except Exception as exc:
        with session_scope() as session:
            candidate = session.get(Candidate, candidate_id)
            if not candidate:
                return
            candidate.extraction_status = "failed"
            candidate.status = CandidateStatus.NEEDS_REVIEW
            candidate.hard_filter_reason = f"抓取或提取失败: {exc}"
            if isinstance(exc, FetchError):
                candidate.fetch_audit_json = json.dumps(
                    [
                        {
                            "url": attempt.url,
                            "status_code": attempt.status_code,
                            "outcome": attempt.outcome,
                        }
                        for attempt in exc.attempts
                    ],
                    ensure_ascii=False,
                )
        return

    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        if not candidate:
            return
        candidate.original_url = resource.url
        candidate.fetch_audit_json = json.dumps(
            [
                {
                    "url": attempt.url,
                    "status_code": attempt.status_code,
                    "outcome": attempt.outcome,
                }
                for attempt in resource.attempts
            ],
            ensure_ascii=False,
        )
        candidate.title_original = document.title or link.listing_title
        candidate.published_date, candidate.date_status, candidate.date_evidence = (
            _resolve_candidate_date(document, link)
        )
        candidate.content_type, candidate.content_type_evidence = (
            _resolve_candidate_content_type(document, link)
        )
        candidate.content_type_raw_labels_json = json.dumps(
            list(dict.fromkeys([
                *link.listing_content_type_labels,
                *document.content_type_labels,
            ])),
            ensure_ascii=False,
        )
        candidate.content_type_evidence_json = json.dumps(
            [
                item.to_dict()
                for item in (
                    *link.listing_content_type_evidence_items,
                    *document.content_type_evidence_items,
                )
            ],
            ensure_ascii=False,
        )
        authors, author_evidence = _resolve_candidate_authors(document, link)
        candidate.authors_json = json.dumps(authors, ensure_ascii=False)
        candidate.author_evidence = author_evidence
        candidate.language = document.language
        candidate.english_word_count = document.english_word_count
        candidate.chinese_char_count = document.chinese_char_count
        candidate.extraction_status = document.extraction_status
        candidate.content_hash = document.content_hash
        candidate.extracted_text = document.text
        candidate.metadata_json = json.dumps(
            {
                "preflight_error": link.preflight_error,
                "abstract": document.abstract,
                "tags": document.tags,
                "headings": document.headings,
                "extraction_audit": document.extraction_audit,
            },
            ensure_ascii=False,
        )
        candidate.cache_path = str(resource.cache_path)
        candidate.status, candidate.hard_filter_reason = extraction_gate(
            candidate, run_start, run_end, selection_policy,
        )
        if document.warnings:
            candidate.hard_filter_reason = "; ".join(
                filter(None, [candidate.hard_filter_reason, *document.warnings])
            )
        for attachment in attachments:
            _persist_attachment(session, candidate_id, attachment)
        candidate_status = candidate.status
        title_original = candidate.title_original
        content_type = candidate.content_type
        extracted_text = candidate.extracted_text
        model_metadata = json.loads(candidate.metadata_json or "{}")

    assess_stored_candidate(candidate_id)


def _assessment_record(candidate, **fields):
    record = candidate.assessment or Assessment(candidate_id=candidate.id)
    for key, value in fields.items():
        setattr(record, key, value)
    candidate.assessment = record
    return record


def assess_stored_candidate(candidate_id: int) -> None:
    """Retry content reasoning using the stored body; never overwrite a human decision."""
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None or candidate.review is not None:
            return
        run = candidate.run
        selection_policy = run_policy(run)
        run_start, run_end = run.start_date, run.end_date
        candidate_status, reason = extraction_gate(candidate, run_start, run_end, selection_policy)
        candidate.status, candidate.hard_filter_reason = candidate_status, reason
        source_name = next((r.source_name_snapshot for r in run.source_runs if r.source_id == candidate.source_id), candidate.source.name)
        title_original, content_type, extracted_text = candidate.title_original, candidate.content_type, candidate.extracted_text
        model_metadata = json.loads(candidate.metadata_json or "{}")
        if candidate.assessment:
            history = model_metadata.setdefault("assessment_history", [])
            history.append({"raw_response": candidate.assessment.raw_response,
                            "provider": candidate.assessment.provider_model,
                            "selection": model_metadata.get("selection", {})})
        model_metadata.pop("selection", None)
        candidate.metadata_json = json.dumps(model_metadata, ensure_ascii=False)
    if candidate_status != CandidateStatus.DISCOVERED:
        return
    provider = OpenAICompatibleProvider(load_config())
    if not provider.configured():
        with session_scope() as session:
            candidate = session.get(Candidate, candidate_id)
            if candidate:
                candidate.status = CandidateStatus.NEEDS_REVIEW
                candidate.hard_filter_reason = "；".join(filter(None, [candidate.hard_filter_reason, "模型未配置，正文已准备好"]))
                candidate.topic_status = TopicStatus.PENDING
        return
    result = provider.assess(
        title_original,
        source_name,
        content_type,
        extracted_text,
        model_metadata,
    )
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        if not candidate or candidate.review is not None:
            return
        if not result.payload:
            candidate.status = CandidateStatus.NEEDS_REVIEW
            candidate.hard_filter_reason = result.error
            candidate.topic_status = TopicStatus.NEEDS_REVIEW
            session.add(
                _assessment_record(candidate,
                    decision=Decision.NEEDS_REVIEW,
                    provider_model=provider.config.model_name,
                    input_chars=len(result.evidence_text),
                    raw_response=result.raw_response,
                    error_message=result.error,
                    primary_category="", secondary_categories_json="[]", core_claim_zh="",
                    reasoning_zh="", evidence_json="[]", confidence=0, strategic_nexus="", exclusion_code="",
                )
            )
            return
        payload = result.payload
        candidate.title_en = payload.title_en
        candidate.title_zh = payload.title_zh
        normalized_text = " ".join(extracted_text.split())
        actual_input = " ".join(result.evidence_text.split())
        evidence_verified = bool(payload.evidence) and bool(actual_input) and all(
            bool(item.strip()) and " ".join(item.split()) in normalized_text
            and " ".join(item.split()) in actual_input for item in payload.evidence
        )
        blocking = fact_issues(candidate, run_start, run_end, selection_policy)
        if payload.decision == Decision.INCLUDE:
            if not json.loads(candidate.authors_json or "[]"):
                blocking.append("作者待核实")
            if not payload.title_en.strip() or not payload.title_zh.strip():
                blocking.append("双语标题待补齐")
        audit = json.loads(candidate.metadata_json or "{}")
        audit["selection"] = {
            "policy": selection_policy, "evidence_verified": evidence_verified,
            "verification_status": result.verification_status,
            "verification_reason": result.verification_reason,
            "input_complete": result.input_complete, "model_calls": result.request_count,
            "input_chars": len(result.evidence_text), "input_sha256": hashlib.sha256(result.evidence_text.encode("utf-8")).hexdigest(),
            "segments": result.segment_audit, "fact_issues": blocking,
            "research_substance": payload.research_substance,
        }
        if not evidence_verified:
            candidate.status = CandidateStatus.NEEDS_REVIEW
            candidate.topic_status = TopicStatus.NEEDS_REVIEW
            blocking.append("模型证据无法在实际输入及提取正文中逐字回查")
        elif not result.input_complete or result.verification_status != "supported":
            candidate.status = CandidateStatus.NEEDS_REVIEW
            candidate.topic_status = TopicStatus.NEEDS_REVIEW
            blocking.append(result.verification_reason or "模型复核或全文覆盖尚未通过")
        elif payload.decision == Decision.EXCLUDE:
            candidate.status = CandidateStatus.MODEL_EXCLUDED
            candidate.topic_status = TopicStatus.MODEL_EXCLUDED
        elif payload.decision == Decision.INCLUDE and payload.research_substance == "substantive":
            candidate.status = CandidateStatus.NEEDS_REVIEW if blocking else CandidateStatus.MODEL_INCLUDED
            candidate.topic_status = TopicStatus.MODEL_INCLUDED
        else:
            candidate.status = CandidateStatus.NEEDS_REVIEW
            candidate.topic_status = TopicStatus.NEEDS_REVIEW
            blocking.append("研究深度或主题判断仍不明确")
        candidate.hard_filter_reason = "；".join(blocking)
        candidate.metadata_json = json.dumps(audit, ensure_ascii=False)
        session.add(
            _assessment_record(candidate,
                decision=payload.decision,
                primary_category=payload.primary_category or "",
                secondary_categories_json=json.dumps(payload.secondary_categories),
                core_claim_zh=payload.core_claim_zh,
                reasoning_zh=payload.reasoning_zh,
                evidence_json=json.dumps(payload.evidence, ensure_ascii=False),
                confidence=payload.confidence,
                provider_model=provider.config.model_name,
                input_chars=len(result.evidence_text),
                raw_response=result.raw_response,
                strategic_nexus=payload.strategic_nexus,
                exclusion_code=payload.exclusion_code,
                error_message=result.error,
            )
        )


def _persist_attachment(session, candidate_id: int, result: AttachmentResult) -> None:
    row = session.scalar(
        select(CandidateAttachment).where(
            CandidateAttachment.candidate_id == candidate_id,
            CandidateAttachment.canonical_url == result.link.canonical_url,
        )
    )
    if row is None:
        row = CandidateAttachment(
            candidate_id=candidate_id,
            url=result.link.url,
            canonical_url=result.link.canonical_url,
        )
        session.add(row)
    row.url = result.resource.url if result.resource else result.link.url
    row.link_text = result.link.link_text
    row.role = result.link.role
    row.status_code = result.resource.status_code if result.resource else result.status_code
    row.content_type = result.resource.content_type if result.resource else ""
    row.cache_path = str(result.resource.cache_path) if result.resource else ""
    row.content_hash = hashlib.sha256(result.resource.body).hexdigest() if result.resource else ""
    row.extraction_status = result.document.extraction_status if result.document else "failed"
    row.extraction_audit_json = json.dumps(
        result.document.extraction_audit if result.document else {},
        ensure_ascii=False,
    )
    row.used_as_body = result.used_as_body
    row.error_message = result.error


def _json_string_list(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in payload if isinstance(item, str) and item.strip())


def _persist_page_progress(entry_run_id: int, audit: PageScanAudit, completed: bool) -> None:
    with session_scope() as session:
        entry_run = session.get(SourceEntryRun, entry_run_id)
        if not entry_run:
            return
        entry_run.current_page_url = audit.page_url
        entry_run.current_page_number = audit.page_number
        entry_run.ordering_status = audit.ordering_status
        if audit.is_boundary_page:
            entry_run.boundary_page_number = audit.page_number
        row = session.scalar(
            select(ListingPageAudit).where(
                ListingPageAudit.source_entry_run_id == entry_run_id,
                ListingPageAudit.page_number == audit.page_number,
            )
        )
        if row is None:
            row = ListingPageAudit(
                source_entry_run_id=entry_run_id,
                page_url=audit.page_url,
                page_number=audit.page_number,
            )
            session.add(row)
        for name in (
            "cards_found", "accepted_count", "rejected_count", "in_window_count",
            "after_end_count", "before_start_count", "date_unknown_count",
            "earliest_date", "latest_date", "ordering_status", "is_boundary_page",
            "stop_evidence",
        ):
            setattr(row, name, getattr(audit, name))
        row.sample_links_json = json.dumps(audit.sample_links, ensure_ascii=False)
        if completed:
            entry_run.last_completed_page_url = audit.page_url
            entry_run.pages_scanned = max(entry_run.pages_scanned, audit.page_number)
            entry_run.links_discovered = int(
                session.scalar(
                    select(func.coalesce(func.sum(ListingPageAudit.accepted_count), 0)).where(
                        ListingPageAudit.source_entry_run_id == entry_run_id
                    )
                )
                or 0
            )
            dates = [value for value in (audit.earliest_date, audit.latest_date) if value]
            if dates:
                entry_run.earliest_date = min(
                    [entry_run.earliest_date, *dates] if entry_run.earliest_date else dates
                )
                entry_run.latest_date = max(
                    [entry_run.latest_date, *dates] if entry_run.latest_date else dates
                )


def _execute_source(
    run_id: int,
    source_run_id: int,
    limiter: DomainRateLimiter,
    only_entry_run_ids: set[int] | None = None,
) -> None:
    config = load_config()
    crawler = WebCrawler(config, limiter)
    provider = OpenAICompatibleProvider(config)
    try:
        with session_scope() as session:
            source_run = session.get(SourceRun, source_run_id)
            if not source_run:
                return
            source_run.status = CoverageStatus.RUNNING
            source_run.started_at = utcnow()
            run = session.get(CrawlRun, run_id)
            source_id = source_run.source_id
            config.content_type_rules = run_policy(run)["content_type_rules"]
            entry_run_ids = [row.id for row in source_run.entry_runs]
            if only_entry_run_ids is not None:
                entry_run_ids = [row_id for row_id in entry_run_ids if row_id in only_entry_run_ids]
            start_date = run.start_date
            end_date = run.end_date
        for entry_run_id in entry_run_ids:
            with session_scope() as session:
                entry_run = session.get(SourceEntryRun, entry_run_id)
                entry = session.get(SourceEntry, entry_run.source_entry_id) if entry_run else None
                if not entry_run or not entry:
                    continue
                is_retry = entry_run.status in {CoverageStatus.FAILED, CoverageStatus.INCOMPLETE}
                entry_run.status = CoverageStatus.RUNNING
                entry_run.started_at = utcnow()
                entry_id = entry.id
                try:
                    snapshot = json.loads(entry_run.entry_config_snapshot_json or "{}")
                except json.JSONDecodeError:
                    snapshot = {}
                entry_url = entry_run.entry_url_snapshot or entry.url
                ordered = bool(snapshot.get("ordered_by_date", entry.ordered_by_date))
                adapter_key = str(snapshot.get("adapter_key") or entry.adapter_key)
                listing_selectors = tuple(
                    snapshot["listing_selectors"]
                    if "listing_selectors" in snapshot
                    else _json_string_list(entry.listing_selectors_json)
                )
                container_selectors = tuple(
                    snapshot["container_selectors"]
                    if "container_selectors" in snapshot
                    else _json_string_list(entry.container_selectors_json)
                )
                content_type_selectors = tuple(
                    snapshot["content_type_selectors"]
                    if "content_type_selectors" in snapshot
                    else _json_string_list(entry.content_type_selectors_json)
                )
                date_selectors = tuple(
                    snapshot["date_selectors"]
                    if "date_selectors" in snapshot
                    else _json_string_list(entry.date_selectors_json)
                )
                allowed_patterns = tuple(
                    snapshot["allowed_path_patterns"]
                    if "allowed_path_patterns" in snapshot
                    else _json_string_list(entry.allowed_path_patterns_json)
                )
                excluded_patterns = tuple(
                    snapshot["excluded_path_patterns"]
                    if "excluded_path_patterns" in snapshot
                    else _json_string_list(entry.excluded_path_patterns_json)
                )
                resume_url = entry_run.last_completed_page_url if is_retry else ""
                page_number_offset = max(0, entry_run.current_page_number - 1) if resume_url else 0
            try:
                def assist_date(link, _resource, document):
                    if not provider.configured() or not document.text.strip():
                        return None, ""
                    result = provider.locate_publication_date(
                        document.title or link.listing_title,
                        link.fetch_url,
                        document.text,
                    )
                    payload = result.payload
                    verified = False
                    error = result.error
                    if payload and payload.published_date and payload.evidence_quote.strip():
                        normalized_quote = " ".join(payload.evidence_quote.split()).casefold()
                        normalized_text = " ".join(document.text.split()).casefold()
                        reparsed = parse_date_value(payload.evidence_quote)
                        verified = normalized_quote in normalized_text and reparsed == payload.published_date
                        if not verified:
                            error = "模型日期证据无法在原文中回查并确定性复解析"
                    with session_scope() as audit_session:
                        audit_session.add(
                            DateAssistBatch(
                                run_id=run_id,
                                source_entry_id=entry_id,
                                listing_url=link.listing_url,
                                item_url=link.fetch_url,
                                provider_model=provider.config.model_name,
                                suggested_date=payload.published_date if payload else None,
                                evidence_quote=payload.evidence_quote if payload else "",
                                verified=verified,
                                raw_response=result.raw_response,
                                error_message=error,
                            )
                        )
                    if verified and payload:
                        return payload.published_date, f"模型定位并经原文复核: {payload.evidence_quote}"
                    return None, ""

                scan = crawler.scan_listing(
                    entry_url,
                    start_date,
                    ordered,
                    adapter_key,
                    end_date,
                    listing_selectors=listing_selectors,
                    container_selectors=container_selectors,
                    content_type_selectors=content_type_selectors,
                    date_selectors=date_selectors,
                    allowed_path_patterns=allowed_patterns,
                    excluded_path_patterns=excluded_patterns,
                    resume_url=resume_url,
                    on_link=lambda link: _persist_candidate(
                        run_id, source_id, entry_id, link, crawler
                    ),
                    on_page=lambda audit, completed: _persist_page_progress(
                        entry_run_id, audit, completed
                    ),
                    date_assist=assist_date,
                    page_number_offset=page_number_offset,
                )
                with session_scope() as session:
                    entry_run = session.get(SourceEntryRun, entry_run_id)
                    entry = session.get(SourceEntry, entry_id)
                    entry_run.status = scan.coverage_status
                    entry_run.pages_scanned = max(entry_run.pages_scanned, page_number_offset + scan.pages_scanned)
                    entry_run.links_discovered = int(
                        session.scalar(
                            select(func.coalesce(func.sum(ListingPageAudit.accepted_count), 0)).where(
                                ListingPageAudit.source_entry_run_id == entry_run_id
                            )
                        )
                        or 0
                    )
                    entry_run.earliest_date = entry_run.earliest_date or scan.earliest_date
                    entry_run.latest_date = entry_run.latest_date or scan.latest_date
                    entry_run.ordering_status = scan.ordering_status
                    entry_run.boundary_page_number = scan.boundary_page_number or entry_run.boundary_page_number
                    entry_run.stop_reason = (
                        f"自动串行补跑后：{scan.stop_reason}" if is_retry else scan.stop_reason
                    )
                    entry_run.error_message = "; ".join(scan.errors)
                    entry_run.completed_at = utcnow()
                    if scan.coverage_status == CoverageStatus.COMPLETE:
                        entry.last_successful_date = end_date
            except Exception as exc:
                logger.exception("source entry run failed run_id=%s entry_run_id=%s", run_id, entry_run_id)
                with session_scope() as session:
                    entry_run = session.get(SourceEntryRun, entry_run_id)
                    if entry_run:
                        entry_run.status = CoverageStatus.FAILED
                        entry_run.error_message = str(exc)
                        entry_run.completed_at = utcnow()
        with session_scope() as session:
            source_run = session.get(SourceRun, source_run_id)
            source = session.get(Source, source_id)
            run = session.get(CrawlRun, run_id)
            entry_runs = list(source_run.entry_runs)
            statuses = {row.status for row in entry_runs}
            if entry_runs and statuses == {CoverageStatus.COMPLETE}:
                source_run.status = CoverageStatus.COMPLETE
            elif entry_runs and statuses == {CoverageStatus.FAILED}:
                source_run.status = CoverageStatus.FAILED
            else:
                source_run.status = CoverageStatus.INCOMPLETE
            source_run.pages_scanned = sum(row.pages_scanned for row in entry_runs)
            source_run.links_discovered = sum(row.links_discovered for row in entry_runs)
            earliest = [row.earliest_date for row in entry_runs if row.earliest_date]
            latest = [row.latest_date for row in entry_runs if row.latest_date]
            source_run.earliest_date = min(earliest) if earliest else None
            source_run.latest_date = max(latest) if latest else None
            source_run.stop_reason = "；".join(
                f"{row.entry_url_snapshot or row.source_entry.url}: {row.stop_reason}"
                for row in entry_runs
            )
            source_run.error_message = "；".join(
                f"{row.entry_url_snapshot or row.source_entry.url}: {row.error_message}"
                for row in entry_runs
                if row.error_message
            )
            source_run.completed_at = utcnow()
            if source_run.status == CoverageStatus.COMPLETE:
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
        retry_groups: list[tuple[int, set[int]]] = []
        with SessionLocal() as session:
            for source_run_id in source_run_ids:
                source_run = session.get(SourceRun, source_run_id)
                if not source_run:
                    continue
                retry_ids = {
                    row.id
                    for row in source_run.entry_runs
                    if row.status in {CoverageStatus.FAILED, CoverageStatus.INCOMPLETE}
                    and _is_retriable_error(row.error_message)
                }
                if retry_ids:
                    retry_groups.append((source_run_id, retry_ids))
        for source_run_id, entry_ids in retry_groups:
            _execute_source(
                run_id,
                source_run_id,
                DomainRateLimiter(config.per_domain_delay_seconds),
                entry_ids,
            )
        with session_scope() as session:
            run = session.get(CrawlRun, run_id)
            statuses = {row.status for row in run.source_runs}
            run.completed_at = utcnow()
            unresolved = session.scalar(select(func.count(Candidate.id)).where(
                Candidate.run_id == run_id,
                Candidate.status.in_([CandidateStatus.NEEDS_REVIEW, CandidateStatus.DISCOVERED]),
            )) or 0
            run.error_message = f"{unresolved} 条内容尚需处理，可先导出自动草稿查看已完成条目及异常" if unresolved else ""
            if unresolved or CoverageStatus.FAILED in statuses or CoverageStatus.INCOMPLETE in statuses:
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
        for source_run in session.scalars(
            select(SourceRun).where(SourceRun.status == CoverageStatus.RUNNING)
        ).all():
            source_run.status = CoverageStatus.INCOMPLETE
            source_run.error_message = source_run.error_message or "程序上次在来源完成前退出"
            source_run.completed_at = utcnow()
        for entry_run in session.scalars(
            select(SourceEntryRun).where(SourceEntryRun.status == CoverageStatus.RUNNING)
        ).all():
            entry_run.status = CoverageStatus.INCOMPLETE
            entry_run.error_message = entry_run.error_message or "程序上次在入口完成前退出"
            entry_run.completed_at = utcnow()
        return len(rows)


def _is_retriable_error(message: str) -> bool:
    value = (message or "").lower()
    if any(
        marker in value
        for marker in ("10013", "err_network_access_denied", "robots.txt", "验证码", "captcha", "http 404")
    ):
        return False
    return any(
        marker in value
        for marker in (
            "瞬时 http",
            "网络请求重试 3 次",
            "transient",
            "timeout",
            "timed out",
            "connection reset",
            "temporarily unavailable",
            "浏览器渲染失败",
        )
    )
