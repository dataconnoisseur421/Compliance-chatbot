"""Consistency checks between the register and the files on disk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document, DocumentVersion
from .register import SOURCES_SUBDIR, sha256_of

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str


def check_register(session: Session, repo_root: Path, today: date | None = None) -> list[Finding]:
    today = today or date.today()
    findings: list[Finding] = []
    docs = session.scalars(select(Document).order_by(Document.slug)).all()
    registered: set[Path] = set()

    for doc in docs:
        versions = sorted(doc.versions, key=lambda v: v.version_label)
        if not versions:
            findings.append(Finding(ERROR, "no-versions", f"{doc.slug}: document has no versions"))
            continue
        in_force = [v for v in versions if v.status == "in_force"]
        if not in_force and not all(v.status in ("superseded", "excluded") for v in versions):
            findings.append(Finding(WARN, "no-in-force", f"{doc.slug}: no in-force version"))
        elif not in_force and doc.doc_type != "amending_instrument":
            # An amending instrument is normally folded into a consolidation, so
            # being fully retired is expected for those and flagged for the rest.
            findings.append(Finding(WARN, "all-retired", f"{doc.slug}: every version is retired"))
        if not doc.source_url:
            findings.append(Finding(INFO, "no-source-url", f"{doc.slug}: no source URL recorded"))

        for v in versions:
            tag = f"{doc.slug}@{v.version_label}"
            path = (repo_root / v.file_path).resolve()
            registered.add(path)
            if not path.is_file():
                findings.append(Finding(ERROR, "missing-file", f"{tag}: file not found at {v.file_path}"))
            elif sha256_of(path) != v.sha256:
                findings.append(
                    Finding(ERROR, "hash-mismatch", f"{tag}: file differs from the registered fingerprint")
                )
            if v.status == "superseded" and v.superseded_by_version_id is None:
                findings.append(
                    Finding(WARN, "no-replacement", f"{tag}: superseded but no replacement is recorded")
                )
            if v.status == "in_force" and v.effective_to and v.effective_to < today:
                findings.append(
                    Finding(WARN, "expired", f"{tag}: marked in force but effective_to {v.effective_to} has passed")
                )
            if v.status == "in_force" and v.as_at_date is None and doc.doc_type in ("act", "regulations"):
                findings.append(
                    Finding(WARN, "no-as-at", f"{tag}: statute without an 'as at' date")
                )

    sources = (repo_root / SOURCES_SUBDIR).resolve()
    if sources.is_dir():
        for pdf in sorted(sources.rglob("*.pdf")):
            if pdf.resolve() not in registered:
                findings.append(
                    Finding(ERROR, "unregistered-file", f"{pdf.relative_to(repo_root.resolve())}: not in the register")
                )
    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == ERROR for f in findings)
