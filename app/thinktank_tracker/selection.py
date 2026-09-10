from __future__ import annotations

import json
from .config import AppConfig
from .constants import EXCLUDED_TYPES, CandidateStatus, ContentKindStatus, DateScope, LengthStatus, TopicStatus, ReviewStatus

POLICY_VERSION = "research-evidence-v2"


def policy_snapshot(config: AppConfig) -> dict:
    return {
        "version": POLICY_VERSION,
        "mode": config.selection_mode if config.selection_mode in {"strict", "substance"} else "strict",
        "min_english_words": max(0, int(config.min_english_words)),
        "min_chinese_chars": max(0, int(config.min_chinese_chars)),
        "allowed_types": sorted(config.allowed_content_types),
        "excluded_types": sorted((set(EXCLUDED_TYPES) | set(config.excluded_content_types)) - set(config.allowed_content_types)),
        "content_type_rules": config.content_type_rules,
    }


def run_policy(run) -> dict:
    try:
        data = json.loads(run.selection_policy_json or "{}")
    except (ValueError, TypeError):
        data = {}
    return data if isinstance(data, dict) and data.get("version") == POLICY_VERSION else policy_snapshot(AppConfig())


def fact_issues(candidate, start, end, policy: dict) -> list[str]:
    """Compute independent factual axes. Never infer or alter a publisher fact."""
    issues = []
    if candidate.date_status != "confirmed" or candidate.published_date is None:
        candidate.date_scope = DateScope.DATE_UNKNOWN
        issues.append("发布日期缺失或冲突，待核实后才能进入报告")
    else:
        candidate.date_scope = (DateScope.BEFORE_START if candidate.published_date < start else
                                DateScope.AFTER_END if candidate.published_date > end else DateScope.IN_WINDOW)
        if candidate.date_scope != DateScope.IN_WINDOW:
            issues.append("发布日期不在指定闭区间")
    kind = candidate.content_type or "unknown"
    conflict = "体裁冲突" in (candidate.content_type_evidence or "")
    candidate.content_kind_status = (ContentKindStatus.NEEDS_REVIEW if conflict or kind == "unknown" else
        ContentKindStatus.EXCLUDED if kind in policy["excluded_types"] else
        ContentKindStatus.ELIGIBLE if kind in policy["allowed_types"] else ContentKindStatus.NEEDS_REVIEW)
    is_chinese = candidate.language == "zh"
    count = (candidate.chinese_char_count if is_chinese else candidate.english_word_count) or 0
    threshold = policy["min_chinese_chars" if is_chinese else "min_english_words"]
    candidate.length_status = LengthStatus.UNKNOWN if candidate.extraction_status != "complete" else (
        LengthStatus.PASS if count >= threshold else LengthStatus.BELOW)
    if candidate.extraction_status != "complete" or not (candidate.extracted_text or "").strip():
        issues.append("正文提取不完整或为空，不能据此完成研究判断")
    if policy["mode"] == "strict":
        if candidate.content_kind_status == ContentKindStatus.EXCLUDED:
            issues.append(f"明确排除的体裁: {kind}")
        elif candidate.content_kind_status != ContentKindStatus.ELIGIBLE:
            issues.append("体裁尚未满足任务规则，保留原始标签并等待核实")
        if candidate.length_status == LengthStatus.BELOW:
            issues.append(f"篇幅低于任务门槛: {count}/{threshold}")
    return issues


def extraction_gate(candidate, start, end, policy):
    candidate.topic_status = TopicStatus.PENDING
    candidate.review_status = candidate.review_status or ReviewStatus.PENDING
    issues = fact_issues(candidate, start, end, policy)
    outside = candidate.date_scope in {DateScope.BEFORE_START, DateScope.AFTER_END}
    definite_rule_failure = policy["mode"] == "strict" and (
        candidate.content_kind_status == ContentKindStatus.EXCLUDED or candidate.length_status == LengthStatus.BELOW)
    if outside or definite_rule_failure:
        candidate.topic_status = TopicStatus.NOT_REQUIRED
        return CandidateStatus.HARD_EXCLUDED, "；".join(issues)
    if candidate.extraction_status != "complete" or not (candidate.extracted_text or "").strip():
        candidate.topic_status = TopicStatus.NEEDS_REVIEW
        return CandidateStatus.NEEDS_REVIEW, "；".join(issues)
    # An unknown date/genre does not prevent understanding an otherwise available body.
    return CandidateStatus.DISCOVERED, "；".join(issues)
