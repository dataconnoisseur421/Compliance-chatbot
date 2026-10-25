"""Load the CSV register into the database and report on it.

The CSV files in ``data/register`` are the source of truth. The database is
derived from them and can be rebuilt at any time. Changes (including review
records) are written back to the CSVs so every change shows up in Git.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .db import PROJECT_ROOT, get_engine, get_session, migrate
from .models import (
    DEFAULT_REVIEW_INTERVAL_DAYS,
    REVIEW_OUTCOMES,
    Base,
    Document,
    DocumentVersion,
    ReviewLog,
)

REGISTER_DIR = PROJECT_ROOT / "data" / "register"
SOURCES_SUBDIR = Path("data") / "sources"

VERSION_FIELDS = [
    "document_slug", "version_label", "file_path", "original_filename", "sha256", "page_count",
    "file_size_bytes", "issue_date", "issue_date_precision", "effective_date", "effective_to",
    "last_revised_date", "as_at_date", "status", "superseded_by", "transition_notes", "notes",
    "date_added", "last_reviewed_date",
]
REVIEW_FIELDS = ["document_slug", "version_label", "reviewed_on", "reviewer", "outcome", "notes"]


class RegisterError(Exception):
    """The CSV register is malformed or inconsistent."""


# --------------------------------------------------------------------------- CSV
def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _date(value: str, where: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RegisterError(f"{where}: '{value}' is not an ISO date (YYYY-MM-DD)") from exc


def _int(value: str, where: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RegisterError(f"{where}: '{value}' is not a whole number") from exc


def _blank_to_none(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


# ------------------------------------------------------------------------ loading
def load_register(session: Session, reg_dir: Path = REGISTER_DIR) -> tuple[int, int]:
    """Replace the database contents with the CSV register. Returns (documents, versions)."""
    docs = read_csv(reg_dir / "documents.csv")
    vers = read_csv(reg_dir / "versions.csv")
    logs = read_csv(reg_dir / "review_log.csv")
    if not docs:
        raise RegisterError(f"{reg_dir / 'documents.csv'} is missing or empty")

    session.execute(delete(ReviewLog))
    session.execute(update(DocumentVersion).values(superseded_by_version_id=None))
    session.execute(delete(DocumentVersion))
    session.execute(delete(Document))

    by_slug: dict[str, Document] = {}
    for n, row in enumerate(docs, start=2):
        where = f"documents.csv line {n}"
        slug = row["slug"].strip()
        if not slug or slug in by_slug:
            raise RegisterError(f"{where}: slug '{slug}' is empty or duplicated")
        interval = _int(row.get("review_interval_days", ""), where) or DEFAULT_REVIEW_INTERVAL_DAYS
        doc = Document(
            slug=slug,
            title=row["title"].strip(),
            short_name=row["short_name"].strip(),
            reference_no=_blank_to_none(row.get("reference_no")),
            issuer=row["issuer"].strip(),
            doc_type=row["doc_type"].strip(),
            category=row["category"].strip(),
            source_url=_blank_to_none(row.get("source_url")),
            review_interval_days=interval,
            search_scope=(row.get("search_scope") or "core").strip(),
            notes=_blank_to_none(row.get("notes")),
        )
        session.add(doc)
        by_slug[slug] = doc
    session.flush()

    by_key: dict[tuple[str, str], DocumentVersion] = {}
    for n, row in enumerate(vers, start=2):
        where = f"versions.csv line {n}"
        slug, label = row["document_slug"].strip(), row["version_label"].strip()
        if slug not in by_slug:
            raise RegisterError(f"{where}: unknown document_slug '{slug}'")
        if (slug, label) in by_key:
            raise RegisterError(f"{where}: duplicate version '{slug}@{label}'")
        added = _date(row.get("date_added", ""), where)
        if added is None:
            raise RegisterError(f"{where}: date_added is required")
        ver = DocumentVersion(
            document_id=by_slug[slug].id,
            version_label=label,
            file_path=row["file_path"].strip(),
            original_filename=_blank_to_none(row.get("original_filename")),
            sha256=row["sha256"].strip(),
            page_count=_int(row.get("page_count", ""), where),
            file_size_bytes=_int(row.get("file_size_bytes", ""), where),
            issue_date=_date(row.get("issue_date", ""), where),
            issue_date_precision=(row.get("issue_date_precision") or "day").strip(),
            effective_date=_date(row.get("effective_date", ""), where),
            effective_to=_date(row.get("effective_to", ""), where),
            last_revised_date=_date(row.get("last_revised_date", ""), where),
            as_at_date=_date(row.get("as_at_date", ""), where),
            status=row["status"].strip(),
            transition_notes=_blank_to_none(row.get("transition_notes")),
            notes=_blank_to_none(row.get("notes")),
            date_added=added,
            last_reviewed_date=_date(row.get("last_reviewed_date", ""), where),
        )
        session.add(ver)
        by_key[(slug, label)] = ver
    session.flush()

    for n, row in enumerate(vers, start=2):
        target = (row.get("superseded_by") or "").strip()
        if not target:
            continue
        slug, _, label = target.partition("@")
        if (slug, label) not in by_key:
            raise RegisterError(f"versions.csv line {n}: superseded_by '{target}' does not exist")
        by_key[(row["document_slug"].strip(), row["version_label"].strip())].superseded_by_version_id = (
            by_key[(slug, label)].id
        )
    session.flush()

    for n, row in enumerate(logs, start=2):
        where = f"review_log.csv line {n}"
        key = (row["document_slug"].strip(), row["version_label"].strip())
        if key not in by_key:
            raise RegisterError(f"{where}: unknown version '{key[0]}@{key[1]}'")
        reviewed_on = _date(row.get("reviewed_on", ""), where)
        if reviewed_on is None:
            raise RegisterError(f"{where}: reviewed_on is required")
        session.add(
            ReviewLog(
                version_id=by_key[key].id,
                reviewed_on=reviewed_on,
                reviewer=_blank_to_none(row.get("reviewer")),
                outcome=row["outcome"].strip(),
                notes=_blank_to_none(row.get("notes")),
            )
        )
    session.commit()
    return len(by_slug), len(by_key)


def open_register(reg_dir: Path = REGISTER_DIR) -> Session:
    """Load the CSV register into a fresh in-memory database for read-only use."""
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = get_session(engine)
    load_register(session, reg_dir)
    return session


def rebuild_database(url: str | None = None, reg_dir: Path = REGISTER_DIR) -> tuple[int, int]:
    """Migrate the on-disk database to the latest schema and reload it from the CSVs."""
    migrate(url)
    session = get_session(get_engine(url))
    try:
        return load_register(session, reg_dir)
    finally:
        session.close()


# ----------------------------------------------------------------------- queries
def next_review_due(version: DocumentVersion, document: Document) -> date:
    base = version.last_reviewed_date or version.date_added
    return base + timedelta(days=document.review_interval_days)


@dataclass
class DueItem:
    document: Document
    version: DocumentVersion
    due: date
    days_overdue: int


def review_schedule(session: Session, today: date) -> list[DueItem]:
    """In-force versions with their review due dates, most overdue first."""
    rows = session.execute(
        select(DocumentVersion, Document)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentVersion.status == "in_force")
    ).all()
    items = []
    for ver, doc in rows:
        due = next_review_due(ver, doc)
        items.append(DueItem(doc, ver, due, (today - due).days))
    return sorted(items, key=lambda i: (-i.days_overdue, i.document.slug))


# --------------------------------------------------------------------- file hash
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------- recording reviews
def record_review(
    slug: str,
    label: str,
    outcome: str,
    reviewed_on: date,
    reviewer: str = "",
    notes: str = "",
    reg_dir: Path = REGISTER_DIR,
) -> None:
    """Append to review_log.csv and stamp last_reviewed_date in versions.csv."""
    if outcome not in REVIEW_OUTCOMES:
        raise RegisterError(f"outcome must be one of {', '.join(REVIEW_OUTCOMES)}")
    versions_path = reg_dir / "versions.csv"
    rows = read_csv(versions_path)
    match = [r for r in rows if r["document_slug"] == slug and r["version_label"] == label]
    if not match:
        raise RegisterError(f"no version '{slug}@{label}' in versions.csv")
    current = _date(match[0].get("last_reviewed_date", ""), "versions.csv")
    if current is None or reviewed_on >= current:
        match[0]["last_reviewed_date"] = reviewed_on.isoformat()
    write_csv(versions_path, VERSION_FIELDS, rows)

    log_path = reg_dir / "review_log.csv"
    log_rows = read_csv(log_path)
    log_rows.append(
        {
            "document_slug": slug,
            "version_label": label,
            "reviewed_on": reviewed_on.isoformat(),
            "reviewer": reviewer,
            "outcome": outcome,
            "notes": notes,
        }
    )
    write_csv(log_path, REVIEW_FIELDS, log_rows)
