from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .constants import CandidateStatus, CoverageStatus, Decision, RunStatus
from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    group_name: Mapped[str] = mapped_column(String(300), default="")
    homepage_url: Mapped[str] = mapped_column(Text)
    publication_url: Mapped[str] = mapped_column(Text, default="")
    adapter_key: Mapped[str] = mapped_column(String(100), default="generic")
    ordered_by_date: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_successful_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    source_runs: Mapped[list["SourceRun"]] = relationship(back_populates="source")
    candidates: Mapped[list["Candidate"]] = relationship(back_populates="source")

    __table_args__ = (UniqueConstraint("homepage_url", name="uq_sources_homepage_url"),)


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(40), default=RunStatus.QUEUED)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")

    source_runs: Mapped[list["SourceRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    candidates: Mapped[list["Candidate"]] = relationship(back_populates="run")


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
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

    __table_args__ = (UniqueConstraint("run_id", "source_id", name="uq_source_run"),)


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
    language: Mapped[str] = mapped_column(String(20), default="unknown")
    english_word_count: Mapped[int] = mapped_column(Integer, default=0)
    chinese_char_count: Mapped[int] = mapped_column(Integer, default=0)
    extraction_status: Mapped[str] = mapped_column(String(40), default="pending")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    cache_path: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default=CandidateStatus.DISCOVERED)
    hard_filter_reason: Mapped[str] = mapped_column(Text, default="")
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

    __table_args__ = (UniqueConstraint("run_id", "canonical_url", name="uq_run_candidate_url"),)


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
    core_claim_zh: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="review")
