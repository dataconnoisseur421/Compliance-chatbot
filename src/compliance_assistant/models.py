"""Register schema.

Design rules (so a later move from SQLite to PostgreSQL stays cheap):
  * Portable column types only: Integer, String, Text, Date, DateTime.
  * Allowed values are enforced with CHECK constraints, not database enums.
  * Dates are real DATE columns; month-only dates are stored as the first of
    the month with ``issue_date_precision = 'month'``.
  * Every document carries a ``workspace_id`` so the tool can serve more than
    one user or firm later without a schema change.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DOC_TYPES = (
    "act",
    "regulations",
    "amending_instrument",
    "notice",
    "guidelines",
    "code",
    "practice_note",
    "toolkit",
)
STATUSES = ("in_force", "superseded", "draft", "excluded")
DATE_PRECISIONS = ("day", "month")
REVIEW_OUTCOMES = ("confirmed_current", "update_needed", "superseded", "other")

DEFAULT_WORKSPACE = "default"
DEFAULT_REVIEW_INTERVAL_DAYS = 90


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    # Explicit constraint names keep Alembic migrations deterministic on
    # SQLite and PostgreSQL alike.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Document(Base):
    """One regulatory instrument (e.g. the SFA), independent of its editions."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_documents_workspace_slug"),
        CheckConstraint(_in("doc_type", DOC_TYPES), name="doc_type_valid"),
        CheckConstraint("review_interval_days > 0", name="review_interval_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_WORKSPACE, server_default=DEFAULT_WORKSPACE
    )
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[str] = mapped_column(String(80), nullable=False)
    reference_no: Mapped[str | None] = mapped_column(String(80))
    issuer: Mapped[str] = mapped_column(String(120), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(30), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    review_interval_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_REVIEW_INTERVAL_DAYS,
        server_default=str(DEFAULT_REVIEW_INTERVAL_DAYS),
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base):
    """One edition (file) of a document, with its status and key dates."""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_label", name="uq_versions_document_label"),
        UniqueConstraint("file_path", name="uq_versions_file_path"),
        CheckConstraint(_in("status", STATUSES), name="status_valid"),
        CheckConstraint(
            _in("issue_date_precision", DATE_PRECISIONS), name="issue_precision_valid"
        ),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint(
            "superseded_by_version_id IS NULL OR status = 'superseded'",
            name="replacement_only_if_superseded",
        ),
        CheckConstraint(
            "effective_date IS NULL OR effective_to IS NULL OR effective_to >= effective_date",
            name="effective_range_valid",
        ),
        # At most one in-force edition per document. Supported by SQLite and
        # PostgreSQL as a partial unique index.
        Index(
            "uq_versions_one_in_force",
            "document_id",
            unique=True,
            sqlite_where=text("status = 'in_force'"),
            postgresql_where=text("status = 'in_force'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    version_label: Mapped[str] = mapped_column(String(80), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)

    issue_date: Mapped[date | None] = mapped_column(Date)
    issue_date_precision: Mapped[str] = mapped_column(
        String(5), nullable=False, default="day", server_default="day"
    )
    effective_date: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    last_revised_date: Mapped[date | None] = mapped_column(Date)
    as_at_date: Mapped[date | None] = mapped_column(Date)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    superseded_by_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL")
    )
    transition_notes: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    date_added: Mapped[date] = mapped_column(Date, nullable=False)
    last_reviewed_date: Mapped[date | None] = mapped_column(Date)

    document: Mapped[Document] = relationship(back_populates="versions")
    reviews: Mapped[list[ReviewLog]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )


class ReviewLog(Base):
    """History of each time a version was checked against its source."""

    __tablename__ = "review_log"
    __table_args__ = (CheckConstraint(_in("outcome", REVIEW_OUTCOMES), name="outcome_valid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    reviewed_on: Mapped[date] = mapped_column(Date, nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(80))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    version: Mapped[DocumentVersion] = relationship(back_populates="reviews")
