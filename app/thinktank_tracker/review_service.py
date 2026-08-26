from __future__ import annotations

import json
from sqlalchemy.orm import Session

from .constants import CATEGORY_LABELS, CandidateStatus, Decision
from .models import Candidate, ReviewDecision, utcnow


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
) -> ReviewDecision:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        raise ValueError("候选记录不存在")
    if decision not in {Decision.INCLUDE, Decision.EXCLUDE, Decision.NEEDS_REVIEW}:
        raise ValueError("无效的审核决定")
    if decision == Decision.INCLUDE:
        if candidate.date_status != "confirmed" or not candidate.published_date:
            raise ValueError("发布日期尚未确认，不能批准进入正式报告")
        if primary_category not in CATEGORY_LABELS:
            raise ValueError("批准记录必须选择一个主分类")
        if not title_zh.strip() or not core_claim_zh.strip():
            raise ValueError("批准记录必须填写中文标题和一句话核心观点")
    secondary = [item for item in secondary_categories if item in CATEGORY_LABELS and item != primary_category]
    review = candidate.review or ReviewDecision(candidate_id=candidate.id)
    review.decision = decision
    review.primary_category = primary_category
    review.secondary_categories_json = json.dumps(secondary, ensure_ascii=False)
    review.title_en = title_en.strip()
    review.title_zh = title_zh.strip()
    review.content_type = content_type.strip()
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
    session.flush()
    return review
