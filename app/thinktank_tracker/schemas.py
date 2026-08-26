from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, HttpUrl, field_validator

from .constants import Decision, PrimaryCategory


class ImportedSource(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    group_name: str = Field(default="", max_length=300)
    homepage_url: HttpUrl
    publication_url: HttpUrl | None = None
    adapter_key: str = "generic"
    ordered_by_date: bool = True
    enabled: bool = True


class AssessmentPayload(BaseModel):
    decision: Decision
    primary_category: PrimaryCategory | None = None
    secondary_categories: list[PrimaryCategory] = Field(default_factory=list, max_length=8)
    title_en: str = Field(default="", max_length=800)
    title_zh: str = Field(default="", max_length=800)
    core_claim_zh: str = Field(default="", max_length=1000)
    reasoning_zh: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("primary_category")
    @classmethod
    def category_required_for_include(
        cls, value: PrimaryCategory | None, info
    ) -> PrimaryCategory | None:
        decision = info.data.get("decision")
        if decision == Decision.INCLUDE and value is None:
            raise ValueError("included assessments require a primary category")
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
