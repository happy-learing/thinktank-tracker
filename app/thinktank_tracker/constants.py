from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class CoverageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class CandidateStatus(StrEnum):
    DISCOVERED = "discovered"
    HARD_EXCLUDED = "hard_excluded"
    NEEDS_REVIEW = "needs_review"
    MODEL_INCLUDED = "model_included"
    MODEL_EXCLUDED = "model_excluded"
    APPROVED = "approved"
    REJECTED = "rejected"


class Decision(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
    NEEDS_REVIEW = "needs_review"


class PrimaryCategory(StrEnum):
    MACRO = "macro"
    CRITICAL_INFRASTRUCTURE = "critical_infrastructure"
    TECHNOLOGY_COMPETITION = "technology_competition"
    CRITICAL_MINERALS = "critical_minerals"
    GEOPOLITICAL_COMPETITION = "geopolitical_competition"
    STRATEGIC_MATERIALS = "strategic_materials"
    RULES_AND_INSTITUTIONS = "rules_and_institutions"
    STRATEGIC_MULTINATIONALS = "strategic_multinationals"
    PIVOT_STATES = "pivot_states"


CATEGORY_LABELS: dict[str, str] = {
    PrimaryCategory.MACRO: "宏观",
    PrimaryCategory.CRITICAL_INFRASTRUCTURE: "关键基础设施",
    PrimaryCategory.TECHNOLOGY_COMPETITION: "科技发展竞争",
    PrimaryCategory.CRITICAL_MINERALS: "关键矿产资源",
    PrimaryCategory.GEOPOLITICAL_COMPETITION: "地缘政治竞争",
    PrimaryCategory.STRATEGIC_MATERIALS: "粮食、能源等关键战略物资",
    PrimaryCategory.RULES_AND_INSTITUTIONS: "国际政治、经贸关键规则与制度博弈",
    PrimaryCategory.STRATEGIC_MULTINATIONALS: "重要跨国企业",
    PrimaryCategory.PIVOT_STATES: "战略支点国家",
}

MICRO_CATEGORY_ORDER = [
    PrimaryCategory.CRITICAL_INFRASTRUCTURE,
    PrimaryCategory.TECHNOLOGY_COMPETITION,
    PrimaryCategory.CRITICAL_MINERALS,
    PrimaryCategory.GEOPOLITICAL_COMPETITION,
    PrimaryCategory.STRATEGIC_MATERIALS,
    PrimaryCategory.RULES_AND_INSTITUTIONS,
    PrimaryCategory.STRATEGIC_MULTINATIONALS,
    PrimaryCategory.PIVOT_STATES,
]

ALLOWED_DEEP_TYPES = {
    "report",
    "paper",
    "research paper",
    "working paper",
    "policy brief",
    "policy paper",
    "analysis",
    "in-depth analysis",
    "brief",
    "study",
}

EXCLUDED_TYPES = {
    "commentary",
    "op-ed",
    "opinion",
    "news",
    "press release",
    "event",
    "podcast",
    "video",
    "blog",
}

