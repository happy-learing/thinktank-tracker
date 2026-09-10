from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from .constants import Decision, PrimaryCategory


class ImportedSourceEntry(BaseModel):
    url: HttpUrl
    adapter_key: str = "auto"
    ordered_by_date: bool = True
    enabled: bool = True
    listing_selectors: list[str] = Field(default_factory=list)
    container_selectors: list[str] = Field(default_factory=list)
    content_type_selectors: list[str] = Field(default_factory=list)
    date_selectors: list[str] = Field(default_factory=list)
    allowed_path_patterns: list[str] = Field(default_factory=list)
    excluded_path_patterns: list[str] = Field(default_factory=list)


class ImportedSource(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    group_name: str = Field(default="", max_length=300)
    homepage_url: HttpUrl
    entries: list[ImportedSourceEntry] = Field(default_factory=list)
    # v1 compatibility fields. New code persists and crawls entries instead.
    publication_url: HttpUrl | None = None
    adapter_key: str = "auto"
    ordered_by_date: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def populate_entries(self) -> "ImportedSource":
        if not self.entries:
            self.entries = [
                ImportedSourceEntry(
                    url=self.publication_url or self.homepage_url,
                    adapter_key=self.adapter_key,
                    ordered_by_date=self.ordered_by_date,
                    enabled=self.enabled,
                )
            ]
        if self.publication_url is None:
            self.publication_url = self.entries[0].url
        return self


class ImportedConfiguration(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sources: list[ImportedSource] = Field(default_factory=list)


class AssessmentPayload(BaseModel):
    research_substance: Literal["substantive", "not_substantive", "uncertain"] = "uncertain"
    decision: Decision
    primary_category: PrimaryCategory | None = None
    secondary_categories: list[PrimaryCategory] = Field(default_factory=list, max_length=8)
    title_en: str = Field(default="", max_length=800)
    title_zh: str = Field(default="", max_length=800)
    core_claim_zh: str = Field(default="", max_length=1000)
    reasoning_zh: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)
    strategic_nexus: str = Field(default="", max_length=1200)
    exclusion_code: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def validate_topic_evidence(self) -> "AssessmentPayload":
        if not self.evidence:
            raise ValueError("assessment requires at least one verbatim evidence quote")
        if self.decision == Decision.INCLUDE:
            if self.primary_category is None:
                raise ValueError("included assessments require one primary category")
            if not self.strategic_nexus.strip():
                raise ValueError("included assessments require a strategic nexus")
        if self.decision == Decision.EXCLUDE and not self.exclusion_code.strip():
            raise ValueError("excluded assessments require an exclusion code")
        return self

    @field_validator("decision", mode="before")
    @classmethod
    def normalize_decision(cls, value):
        normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "accept": Decision.INCLUDE,
            "included": Decision.INCLUDE,
            "recommend_include": Decision.INCLUDE,
            "reject": Decision.EXCLUDE,
            "excluded": Decision.EXCLUDE,
            "recommend_exclude": Decision.EXCLUDE,
            "review": Decision.NEEDS_REVIEW,
            "manual_review": Decision.NEEDS_REVIEW,
            "uncertain": Decision.NEEDS_REVIEW,
        }
        return aliases.get(normalized, normalized)

    @field_validator("primary_category")
    @classmethod
    def category_required_for_include(
        cls, value: PrimaryCategory | None, info
    ) -> PrimaryCategory | None:
        decision = info.data.get("decision")
        if decision == Decision.INCLUDE and value is None:
            raise ValueError("included assessments require a primary category")
        return value


class VerificationPayload(BaseModel):
    verdict: Literal["supported", "contradicted", "uncertain"]
    reason: str = Field(min_length=1, max_length=1500)
    evidence: list[str] = Field(default_factory=list, max_length=3)


class DateAssistPayload(BaseModel):
    published_date: date | None = None
    evidence_quote: str = Field(default="", max_length=500)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ReviewSuggestionPayload(BaseModel):
    decision: Decision
    suggested_date: date | None = None
    date_evidence: str = Field(default="", max_length=1200)
    content_type: str = Field(default="", max_length=100)
    primary_category: PrimaryCategory | None = None
    secondary_categories: list[PrimaryCategory] = Field(default_factory=list, max_length=8)
    title_en: str = Field(default="", max_length=800)
    title_zh: str = Field(default="", max_length=800)
    core_claim_zh: str = Field(default="", max_length=1000)
    reasoning_zh: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)
    field_confidences: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_compatible_aliases(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        aliases = {
            "suggested_content_type": "content_type",
            "core_conclusion": "core_claim_zh",
            "core_claim": "core_claim_zh",
            "chinese_title": "title_zh",
            "english_title": "title_en",
            "confidences": "field_confidences",
        }
        for source, target in aliases.items():
            if not normalized.get(target) and normalized.get(source) is not None:
                normalized[target] = normalized[source]
        topics = normalized.get("suggested_topics") or normalized.get("topics")
        if isinstance(topics, list) and topics:
            if not normalized.get("primary_category"):
                normalized["primary_category"] = topics[0]
            if not normalized.get("secondary_categories"):
                normalized["secondary_categories"] = topics[1:]
        return normalized

    @field_validator("decision", mode="before")
    @classmethod
    def normalize_decision(cls, value):
        return AssessmentPayload.normalize_decision(value)

    @field_validator("field_confidences")
    @classmethod
    def validate_field_confidences(cls, value: dict[str, float]) -> dict[str, float]:
        if any(score < 0 or score > 1 for score in value.values()):
            raise ValueError("field confidence must be between 0 and 1")
        return value


class RunRequest(BaseModel):
    start_date: date
    end_date: date
    source_ids: list[int] = Field(default_factory=list)

    @field_validator("end_date")
    @classmethod
    def validate_range(cls, value: date, info) -> date:
        start = info.data.get("start_date")
        if start and value < start:
            raise ValueError("end_date must not be before start_date")
        return value
