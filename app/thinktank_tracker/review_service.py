from __future__ import annotations

import json
import re
from datetime import date

from sqlalchemy.orm import Session

from .constants import (
    CATEGORY_LABELS,
    EXCLUDED_TYPES,
    CandidateStatus,
    DateScope,
    Decision,
    ReviewStatus,
)
from .config import load_config
from .models import Candidate, ReviewDecision, utcnow


def _normalize_authors(authors: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in authors or []:
        value = re.sub(r"\s+", " ", str(raw)).strip(" \t\r\n,;，；|·")
        value = re.sub(
            r"^(?:written\s+by|authors?|by)\s*[:：]?\s*",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip(" \t\r\n,;，；|·")
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def save_review(
    session: Session,
    candidate_id: int,
    decision: str,
    primary_category: str,
    secondary_categories: list[str],
    title_en: str,
    title_zh: str,
    content_type: str,
    core_claim_zh: str,
    notes: str,
    authors: list[str] | None = None,
    published_date: date | None = None,
    confirm_date: bool = False,
) -> ReviewDecision:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise ValueError("候选记录不存在")
    if decision not in {Decision.INCLUDE, Decision.EXCLUDE, Decision.NEEDS_REVIEW}:
        raise ValueError("无效的审核决定")

    proposed_date = candidate.published_date
    proposed_date_status = candidate.date_status
    if confirm_date:
        if published_date is None:
            raise ValueError("勾选日期确认时必须填写发布日期")
        proposed_date = published_date
        proposed_date_status = "confirmed"
    elif published_date is not None and published_date != candidate.published_date:
        proposed_date = published_date
        proposed_date_status = "manual_unconfirmed"

    if decision == Decision.INCLUDE:
        missing: list[str] = []
        if proposed_date_status != "confirmed" or not proposed_date:
            missing.append("确认发布日期")
        elif not candidate.run.start_date <= proposed_date <= candidate.run.end_date:
            missing.append(
                f"将发布日期改为任务区间 {candidate.run.start_date.isoformat()} 至 {candidate.run.end_date.isoformat()} 内"
            )
        if primary_category not in CATEGORY_LABELS:
            missing.append("选择主分类")
        if not title_en.strip():
            missing.append("填写英文标题")
        if not title_zh.strip():
            missing.append("填写中文标题")
        normalized_type = content_type.strip()
        if not normalized_type or normalized_type.lower() == "unknown":
            missing.append("选择或填写体裁")
        elif len(normalized_type) > 100:
            missing.append("将体裁缩短到 100 个字符以内")
        elif normalized_type.lower() in (set(EXCLUDED_TYPES) | set(load_config().excluded_content_types)):
            missing.append("将明确排除的体裁改为真实深度研究体裁")
        normalized_authors = _normalize_authors(authors)
        if not normalized_authors:
            missing.append("填写至少一位作者")
        if not core_claim_zh.strip():
            missing.append("填写一句话核心观点")
        if missing:
            raise ValueError("无法批准：" + "；".join(missing))

    if proposed_date != candidate.published_date or proposed_date_status != candidate.date_status:
        previous_evidence = candidate.date_evidence.strip()
        action = "人工核对原文并确认" if confirm_date else "人工填写但尚未确认"
        candidate.published_date = proposed_date
        candidate.date_status = proposed_date_status
        candidate.date_evidence = f"{action}: {proposed_date.isoformat()}"
        if previous_evidence:
            candidate.date_evidence += f"；原自动证据: {previous_evidence}"
    if candidate.date_status == "confirmed" and candidate.published_date:
        candidate.date_scope = (
            DateScope.BEFORE_START
            if candidate.published_date < candidate.run.start_date
            else DateScope.AFTER_END
            if candidate.published_date > candidate.run.end_date
            else DateScope.IN_WINDOW
        )
    else:
        candidate.date_scope = DateScope.DATE_UNKNOWN

    secondary = [item for item in secondary_categories if item in CATEGORY_LABELS and item != primary_category]
    review = candidate.review or ReviewDecision(candidate_id=candidate.id)
    review.decision = decision
    review.primary_category = primary_category
    review.secondary_categories_json = json.dumps(secondary, ensure_ascii=False)
    review.title_en = title_en.strip()
    review.title_zh = title_zh.strip()
    review.content_type = content_type.strip()
    review.authors_json = json.dumps(_normalize_authors(authors), ensure_ascii=False)
    review.core_claim_zh = core_claim_zh.strip()
    review.notes = notes.strip()
    review.reviewed_at = utcnow()
    session.add(review)
    candidate.title_en = review.title_en
    candidate.title_zh = review.title_zh
    candidate.status = (
        CandidateStatus.APPROVED
        if decision == Decision.INCLUDE
        else CandidateStatus.REJECTED
        if decision == Decision.EXCLUDE
        else CandidateStatus.NEEDS_REVIEW
    )
    candidate.review_status = (
        ReviewStatus.APPROVED
        if decision == Decision.INCLUDE
        else ReviewStatus.REJECTED
        if decision == Decision.EXCLUDE
        else ReviewStatus.PENDING
    )
    session.flush()
    return review
