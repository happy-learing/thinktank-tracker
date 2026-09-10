from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .constants import (
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
from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class SourceConfiguration(Base):
    __tablename__ = "source_configurations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    sources: Mapped[list["Source"]] = relationship(
        back_populates="configuration", order_by="Source.id"
    )
    runs: Mapped[list["CrawlRun"]] = relationship(back_populates="configuration")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    configuration_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_configurations.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(300))
    group_name: Mapped[str] = mapped_column(String(300), default="")
    homepage_url: Mapped[str] = mapped_column(Text)
    publication_url: Mapped[str] = mapped_column(Text, default="")
    adapter_key: Mapped[str] = mapped_column(String(100), default="auto")
    ordered_by_date: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_successful_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    configuration: Mapped["SourceConfiguration | None"] = relationship(back_populates="sources")
    source_runs: Mapped[list["SourceRun"]] = relationship(back_populates="source")
    candidates: Mapped[list["Candidate"]] = relationship(back_populates="source")
    entries: Mapped[list["SourceEntry"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", order_by="SourceEntry.id"
    )

    __table_args__ = (
        UniqueConstraint(
            "configuration_id", "homepage_url", name="uq_sources_configuration_homepage"
        ),
    )


class SourceEntry(Base):
    __tablename__ = "source_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    adapter_key: Mapped[str] = mapped_column(String(100), default="auto")
    ordered_by_date: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_successful_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    listing_selectors_json: Mapped[str] = mapped_column(Text, default="[]")
    container_selectors_json: Mapped[str] = mapped_column(Text, default="[]")
    content_type_selectors_json: Mapped[str] = mapped_column(Text, default="[]")
    date_selectors_json: Mapped[str] = mapped_column(Text, default="[]")
    allowed_path_patterns_json: Mapped[str] = mapped_column(Text, default="[]")
    excluded_path_patterns_json: Mapped[str] = mapped_column(Text, default="[]")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_status: Mapped[str] = mapped_column(String(40), default="untested")
    last_test_final_url: Mapped[str] = mapped_column(Text, default="")
    last_test_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_test_discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    last_test_accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    source: Mapped[Source] = relationship(back_populates="entries")
    entry_runs: Mapped[list["SourceEntryRun"]] = relationship(
        back_populates="source_entry", cascade="all, delete-orphan"
    )
    discoveries: Mapped[list["CandidateDiscovery"]] = relationship(
        back_populates="source_entry", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("source_id", "canonical_url", name="uq_source_entry_url"),)


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    selection_policy_json: Mapped[str] = mapped_column(Text, default="{}")

    id: Mapped[int] = mapped_column(primary_key=True)
    configuration_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_configurations.id", ondelete="RESTRICT"), nullable=True
    )
    configuration_name_snapshot: Mapped[str] = mapped_column(
        String(200), default="升级前默认配置"
    )
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(40), default=RunStatus.QUEUED)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    configuration: Mapped["SourceConfiguration | None"] = relationship(back_populates="runs")
    source_runs: Mapped[list["SourceRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    candidates: Mapped[list["Candidate"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    source_entry_runs: Mapped[list["SourceEntryRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    source_name_snapshot: Mapped[str] = mapped_column(String(300), default="")
    source_homepage_snapshot: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default=CoverageStatus.PENDING)
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    links_discovered: Mapped[int] = mapped_column(Integer, default=0)
    earliest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    stop_reason: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped[CrawlRun] = relationship(back_populates="source_runs")
    source: Mapped[Source] = relationship(back_populates="source_runs")
    entry_runs: Mapped[list["SourceEntryRun"]] = relationship(
        back_populates="source_run", cascade="all, delete-orphan", order_by="SourceEntryRun.id"
    )

    __table_args__ = (UniqueConstraint("run_id", "source_id", name="uq_source_run"),)


class SourceEntryRun(Base):
    __tablename__ = "source_entry_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"))
    source_run_id: Mapped[int] = mapped_column(ForeignKey("source_runs.id", ondelete="CASCADE"))
    source_entry_id: Mapped[int] = mapped_column(ForeignKey("source_entries.id", ondelete="CASCADE"))
    entry_url_snapshot: Mapped[str] = mapped_column(Text, default="")
    entry_config_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(40), default=CoverageStatus.PENDING)
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    links_discovered: Mapped[int] = mapped_column(Integer, default=0)
    earliest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    stop_reason: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_page_url: Mapped[str] = mapped_column(Text, default="")
    last_completed_page_url: Mapped[str] = mapped_column(Text, default="")
    current_page_number: Mapped[int] = mapped_column(Integer, default=0)
    boundary_page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ordering_status: Mapped[str] = mapped_column(String(40), default="unchecked")

    run: Mapped[CrawlRun] = relationship(back_populates="source_entry_runs")
    source_run: Mapped[SourceRun] = relationship(back_populates="entry_runs")
    source_entry: Mapped[SourceEntry] = relationship(back_populates="entry_runs")
    page_audits: Mapped[list["ListingPageAudit"]] = relationship(
        back_populates="source_entry_run", cascade="all, delete-orphan", order_by="ListingPageAudit.page_number"
    )

    __table_args__ = (UniqueConstraint("run_id", "source_entry_id", name="uq_source_entry_run"),)


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    canonical_url: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str] = mapped_column(Text)
    title_original: Mapped[str] = mapped_column(Text, default="")
    title_en: Mapped[str] = mapped_column(Text, default="")
    title_zh: Mapped[str] = mapped_column(Text, default="")
    published_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_status: Mapped[str] = mapped_column(String(40), default="missing")
    date_evidence: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(100), default="unknown")
    content_type_evidence: Mapped[str] = mapped_column(Text, default="")
    content_type_raw_labels_json: Mapped[str] = mapped_column(Text, default="[]")
    content_type_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    authors_json: Mapped[str] = mapped_column(Text, default="[]")
    author_evidence: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(20), default="unknown")
    english_word_count: Mapped[int] = mapped_column(Integer, default=0)
    chinese_char_count: Mapped[int] = mapped_column(Integer, default=0)
    extraction_status: Mapped[str] = mapped_column(String(40), default="pending")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    cache_path: Mapped[str] = mapped_column(Text, default="")
    fetch_audit_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(40), default=CandidateStatus.DISCOVERED)
    hard_filter_reason: Mapped[str] = mapped_column(Text, default="")
    date_scope: Mapped[str] = mapped_column(String(40), default=DateScope.DATE_UNKNOWN)
    content_kind_status: Mapped[str] = mapped_column(
        String(40), default=ContentKindStatus.NEEDS_REVIEW
    )
    length_status: Mapped[str] = mapped_column(String(40), default=LengthStatus.UNKNOWN)
    topic_status: Mapped[str] = mapped_column(String(40), default=TopicStatus.PENDING)
    review_status: Mapped[str] = mapped_column(String(40), default=ReviewStatus.PENDING)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    run: Mapped[CrawlRun] = relationship(back_populates="candidates")
    source: Mapped[Source] = relationship(back_populates="candidates")
    assessment: Mapped["Assessment | None"] = relationship(
        back_populates="candidate", uselist=False, cascade="all, delete-orphan"
    )
    review: Mapped["ReviewDecision | None"] = relationship(
        back_populates="candidate", uselist=False, cascade="all, delete-orphan"
    )
    discoveries: Mapped[list["CandidateDiscovery"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["CandidateAttachment"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan", order_by="CandidateAttachment.id"
    )
    model_suggestions: Mapped[list["ModelSuggestion"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan", order_by="ModelSuggestion.id"
    )

    __table_args__ = (UniqueConstraint("run_id", "canonical_url", name="uq_run_candidate_url"),)


class CandidateDiscovery(Base):
    __tablename__ = "candidate_discoveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    source_entry_id: Mapped[int] = mapped_column(ForeignKey("source_entries.id", ondelete="CASCADE"))
    listing_url: Mapped[str] = mapped_column(Text, default="")
    fetch_url: Mapped[str] = mapped_column(Text, default="")
    listing_title: Mapped[str] = mapped_column(Text, default="")
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    listing_date_evidence: Mapped[str] = mapped_column(Text, default="")
    listing_content_type: Mapped[str] = mapped_column(String(100), default="")
    listing_content_type_evidence: Mapped[str] = mapped_column(Text, default="")
    listing_content_type_labels_json: Mapped[str] = mapped_column(Text, default="[]")
    listing_content_type_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    listing_authors_json: Mapped[str] = mapped_column(Text, default="[]")
    listing_author_evidence: Mapped[str] = mapped_column(Text, default="")
    card_position: Mapped[int] = mapped_column(Integer, default=0)
    discovery_decision: Mapped[str] = mapped_column(String(40), default="accepted")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="discoveries")
    source_entry: Mapped[SourceEntry] = relationship(back_populates="discoveries")

    __table_args__ = (
        UniqueConstraint("candidate_id", "source_entry_id", "listing_url", name="uq_candidate_discovery"),
    )


class CandidateAttachment(Base):
    __tablename__ = "candidate_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    link_text: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(40), default="other")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str] = mapped_column(String(200), default="")
    cache_path: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    extraction_status: Mapped[str] = mapped_column(String(40), default="pending")
    extraction_audit_json: Mapped[str] = mapped_column(Text, default="{}")
    used_as_body: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="attachments")

    __table_args__ = (
        UniqueConstraint("candidate_id", "canonical_url", name="uq_candidate_attachment"),
    )


class ModelSuggestion(Base):
    __tablename__ = "model_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"))
    decision: Mapped[str] = mapped_column(String(40), default=Decision.NEEDS_REVIEW)
    suggested_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_evidence: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(100), default="")
    primary_category: Mapped[str] = mapped_column(String(100), default="")
    secondary_categories_json: Mapped[str] = mapped_column(Text, default="[]")
    title_en: Mapped[str] = mapped_column(Text, default="")
    title_zh: Mapped[str] = mapped_column(Text, default="")
    core_claim_zh: Mapped[str] = mapped_column(Text, default="")
    reasoning_zh: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    field_confidences_json: Mapped[str] = mapped_column(Text, default="{}")
    provider_model: Mapped[str] = mapped_column(String(200), default="")
    input_chars: Mapped[int] = mapped_column(Integer, default=0)
    raw_response: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="model_suggestions")


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), unique=True
    )
    decision: Mapped[str] = mapped_column(String(40), default=Decision.NEEDS_REVIEW)
    primary_category: Mapped[str] = mapped_column(String(100), default="")
    secondary_categories_json: Mapped[str] = mapped_column(Text, default="[]")
    core_claim_zh: Mapped[str] = mapped_column(Text, default="")
    reasoning_zh: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    provider_model: Mapped[str] = mapped_column(String(200), default="")
    input_chars: Mapped[int] = mapped_column(Integer, default=0)
    raw_response: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    strategic_nexus: Mapped[str] = mapped_column(Text, default="")
    exclusion_code: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="assessment")


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), unique=True
    )
    decision: Mapped[str] = mapped_column(String(40), default=Decision.NEEDS_REVIEW)
    primary_category: Mapped[str] = mapped_column(String(100), default="")
    secondary_categories_json: Mapped[str] = mapped_column(Text, default="[]")
    title_en: Mapped[str] = mapped_column(Text, default="")
    title_zh: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(100), default="")
    authors_json: Mapped[str] = mapped_column(Text, default="[]")
    core_claim_zh: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="review")


class ListingPageAudit(Base):
    __tablename__ = "listing_page_audits"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_entry_run_id: Mapped[int] = mapped_column(
        ForeignKey("source_entry_runs.id", ondelete="CASCADE")
    )
    page_url: Mapped[str] = mapped_column(Text)
    page_number: Mapped[int] = mapped_column(Integer)
    cards_found: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    in_window_count: Mapped[int] = mapped_column(Integer, default=0)
    after_end_count: Mapped[int] = mapped_column(Integer, default=0)
    before_start_count: Mapped[int] = mapped_column(Integer, default=0)
    date_unknown_count: Mapped[int] = mapped_column(Integer, default=0)
    earliest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ordering_status: Mapped[str] = mapped_column(String(40), default="unchecked")
    is_boundary_page: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_evidence: Mapped[str] = mapped_column(Text, default="")
    sample_links_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source_entry_run: Mapped[SourceEntryRun] = relationship(back_populates="page_audits")

    __table_args__ = (
        UniqueConstraint("source_entry_run_id", "page_number", name="uq_listing_page_audit"),
    )


class DateAssistBatch(Base):
    __tablename__ = "date_assist_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"))
    source_entry_id: Mapped[int] = mapped_column(
        ForeignKey("source_entries.id", ondelete="CASCADE")
    )
    listing_url: Mapped[str] = mapped_column(Text, default="")
    item_url: Mapped[str] = mapped_column(Text, default="")
    provider_model: Mapped[str] = mapped_column(String(200), default="")
    suggested_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    evidence_quote: Mapped[str] = mapped_column(Text, default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_response: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
