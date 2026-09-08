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


class DateScope(StrEnum):
    IN_WINDOW = "in_window"
    AFTER_END = "after_end"
    BEFORE_START = "before_start"
    DATE_UNKNOWN = "date_unknown"


class ContentKindStatus(StrEnum):
    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"
    NEEDS_REVIEW = "needs_review"


class LengthStatus(StrEnum):
    PASS = "pass"
    BELOW = "below"
    BORDERLINE = "borderline"
    UNKNOWN = "unknown"


class TopicStatus(StrEnum):
    PENDING = "pending"
    NOT_REQUIRED = "not_required"
    MODEL_INCLUDED = "model_included"
    MODEL_EXCLUDED = "model_excluded"
    NEEDS_REVIEW = "needs_review"


class ReviewStatus(StrEnum):
    PENDING = "pending"
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

DEFAULT_URL_CONTENT_TYPE_RULES = (
    ("reports", "report"),
    ("report", "report"),
    ("analisis", "analysis"),
    ("analysis", "analysis"),
    ("policy-brief", "policy brief"),
    ("briefs", "policy brief"),
    ("articles", "article"),
    ("paper", "research paper"),
    ("research", "research"),
)

DEFAULT_CONTENT_TYPE_LABEL_RULES = (
    ("analysis", "analysis", "allow"),
    ("analyses", "analysis", "allow"),
    ("policy brief", "policy brief", "allow"),
    ("policy briefs", "policy brief", "allow"),
    ("european policy briefs", "policy brief", "allow"),
    ("security policy briefs", "policy brief", "allow"),
    ("africa policy briefs", "policy brief", "allow"),
    ("research paper", "research paper", "allow"),
    ("research papers", "research paper", "allow"),
    ("report", "report", "allow"),
    ("reports", "report", "allow"),
    ("commissioned report", "report", "allow"),
    ("commissioned reports", "report", "allow"),
    ("policy contribution", "policy contribution", "allow"),
    ("commentary", "commentary", "exclude"),
    ("commentaries", "commentary", "exclude"),
)

REVIEW_CONTENT_TYPES = tuple(
    dict.fromkeys(content_type for _, content_type in DEFAULT_URL_CONTENT_TYPE_RULES)
)

ALLOWED_DEEP_TYPES = frozenset(REVIEW_CONTENT_TYPES)

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
