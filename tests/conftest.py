"""Shared test helpers: build a tiny, self-contained register on disk."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from compliance_assistant.register import VERSION_FIELDS, write_csv

DOC_FIELDS = ["slug", "title", "short_name", "reference_no", "issuer", "doc_type", "category",
              "source_url", "review_interval_days", "notes"]


def doc_row(slug: str, **kw) -> dict:
    row = dict(slug=slug, title=f"Title {slug}", short_name=slug.upper(), reference_no="", issuer="MAS",
               doc_type="guidelines", category="test", source_url="https://example.invalid", review_interval_days="90",
               notes="")
    row.update(kw)
    return row


def version_row(slug: str, label: str, file_path: str, content: bytes, **kw) -> dict:
    row = {f: "" for f in VERSION_FIELDS}
    row.update(
        document_slug=slug, version_label=label, file_path=file_path,
        sha256=hashlib.sha256(content).hexdigest(), page_count="1", file_size_bytes=str(len(content)),
        issue_date_precision="day", status="in_force", date_added="2026-01-01",
    )
    row.update(kw)
    return row


class TinyRepo:
    """A throwaway repo layout: data/register/*.csv and data/sources/*.pdf."""

    def __init__(self, root: Path):
        self.root = root
        self.reg = root / "data" / "register"
        self.reg.mkdir(parents=True)
        (root / "data" / "sources" / "test").mkdir(parents=True)
        self.docs: list[dict] = []
        self.versions: list[dict] = []

    def add(self, slug: str, label: str = "v1", content: bytes = b"%PDF-1.4 test", doc_kw=None, **ver_kw):
        rel = f"data/sources/test/{slug}__{label}.pdf"
        (self.root / rel).write_bytes(content)
        if not any(d["slug"] == slug for d in self.docs):
            self.docs.append(doc_row(slug, **(doc_kw or {})))
        self.versions.append(version_row(slug, label, rel, content, **ver_kw))

    def write(self):
        write_csv(self.reg / "documents.csv", DOC_FIELDS, self.docs)
        write_csv(self.reg / "versions.csv", VERSION_FIELDS, self.versions)
        (self.reg / "review_log.csv").write_text("document_slug,version_label,reviewed_on,reviewer,outcome,notes\n")
        return self


@pytest.fixture
def tiny(tmp_path) -> TinyRepo:
    return TinyRepo(tmp_path)
