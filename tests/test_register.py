from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from compliance_assistant.checks import ERROR, check_register, has_errors
from compliance_assistant.db import PROJECT_ROOT
from compliance_assistant.models import Document, DocumentVersion
from compliance_assistant.register import (
    RegisterError,
    open_register,
    read_csv,
    record_review,
    review_schedule,
)


def codes(findings, level=None):
    return {f.code for f in findings if level is None or f.level == level}


# ----------------------------------------------------------------- the real register
def test_real_register_loads_and_has_no_errors():
    session = open_register()
    assert session.scalar(select(Document.id).limit(1)) is not None
    docs = session.scalars(select(Document)).all()
    versions = session.scalars(select(DocumentVersion)).all()
    assert len(docs) == 22
    assert len(versions) == 23
    findings = check_register(session, PROJECT_ROOT, date(2026, 10, 24))
    assert not has_errors(findings), [f for f in findings if f.level == ERROR]


def test_superseded_versions_point_to_in_force_replacements():
    session = open_register()
    for v in session.scalars(select(DocumentVersion).where(DocumentVersion.status == "superseded")):
        replacement = session.get(DocumentVersion, v.superseded_by_version_id)
        assert replacement is not None and replacement.status == "in_force"


# ------------------------------------------------------------------- DB constraints
def test_invalid_status_is_rejected(tiny):
    tiny.add("a", status="maybe")
    tiny.write()
    with pytest.raises(IntegrityError):
        open_register(tiny.reg)


def test_only_one_in_force_version_per_document(tiny):
    tiny.add("a", "v1")
    tiny.add("a", "v2", content=b"%PDF other")
    tiny.write()
    with pytest.raises(IntegrityError):
        open_register(tiny.reg)


def test_replacement_requires_superseded_status(tiny):
    tiny.add("a", "v1", status="in_force", superseded_by="a@v2")
    tiny.add("a", "v2", content=b"%PDF other", status="draft")
    tiny.write()
    with pytest.raises(IntegrityError):
        open_register(tiny.reg)


def test_effective_to_cannot_precede_effective_date(tiny):
    tiny.add("a", effective_date="2026-05-01", effective_to="2026-04-01")
    tiny.write()
    with pytest.raises(IntegrityError):
        open_register(tiny.reg)


# ----------------------------------------------------------------------- CSV errors
def test_unknown_document_slug_in_versions(tiny):
    tiny.add("a")
    tiny.versions[0]["document_slug"] = "ghost"
    tiny.write()
    with pytest.raises(RegisterError, match="unknown document_slug"):
        open_register(tiny.reg)


def test_bad_date_is_reported_with_location(tiny):
    tiny.add("a", issue_date="24/10/2026")
    tiny.write()
    with pytest.raises(RegisterError, match=r"versions.csv line 2.*ISO date"):
        open_register(tiny.reg)


def test_superseded_by_must_exist(tiny):
    tiny.add("a", "v1", status="superseded", superseded_by="a@nope")
    tiny.write()
    with pytest.raises(RegisterError, match="does not exist"):
        open_register(tiny.reg)


# --------------------------------------------------------------------- file checks
def test_clean_tiny_repo_passes(tiny):
    tiny.add("a")
    tiny.write()
    assert not has_errors(check_register(open_register(tiny.reg), tiny.root))


def test_changed_file_is_flagged(tiny):
    tiny.add("a")
    tiny.write()
    (tiny.root / tiny.versions[0]["file_path"]).write_bytes(b"tampered")
    assert "hash-mismatch" in codes(check_register(open_register(tiny.reg), tiny.root), ERROR)


def test_missing_file_is_flagged(tiny):
    tiny.add("a")
    tiny.write()
    (tiny.root / tiny.versions[0]["file_path"]).unlink()
    assert "missing-file" in codes(check_register(open_register(tiny.reg), tiny.root), ERROR)


def test_unregistered_pdf_is_flagged(tiny):
    tiny.add("a")
    tiny.write()
    (tiny.root / "data" / "sources" / "test" / "stray.pdf").write_bytes(b"%PDF stray")
    assert "unregistered-file" in codes(check_register(open_register(tiny.reg), tiny.root), ERROR)


def test_superseded_without_replacement_warns(tiny):
    tiny.add("a", "v1", status="superseded")
    tiny.write()
    assert "no-replacement" in codes(check_register(open_register(tiny.reg), tiny.root))


def test_expired_in_force_version_warns(tiny):
    tiny.add("a", effective_date="2025-01-01", effective_to="2025-12-31")
    tiny.write()
    findings = check_register(open_register(tiny.reg), tiny.root, today=date(2026, 6, 1))
    assert "expired" in codes(findings)


# -------------------------------------------------------------------- review dates
def test_review_schedule_uses_last_review_then_date_added(tiny):
    tiny.add("a", doc_kw={"review_interval_days": "30"}, date_added="2026-01-01")
    tiny.add("b", doc_kw={"review_interval_days": "30"}, date_added="2026-01-01", last_reviewed_date="2026-03-01")
    tiny.add("c", status="superseded", date_added="2025-01-01")
    tiny.write()
    items = {i.document.slug: i for i in review_schedule(open_register(tiny.reg), date(2026, 3, 15))}
    assert set(items) == {"a", "b"}  # superseded versions are not scheduled
    assert items["a"].due == date(2026, 1, 31) and items["a"].days_overdue == 43
    assert items["b"].due == date(2026, 3, 31) and items["b"].days_overdue == -16


def test_review_schedule_sorts_most_overdue_first(tiny):
    tiny.add("a", doc_kw={"review_interval_days": "30"}, date_added="2026-02-01")
    tiny.add("b", doc_kw={"review_interval_days": "30"}, date_added="2026-01-01")
    tiny.write()
    order = [i.document.slug for i in review_schedule(open_register(tiny.reg), date(2026, 6, 1))]
    assert order == ["b", "a"]


def test_record_review_updates_both_csvs(tiny):
    tiny.add("a")
    tiny.write()
    record_review("a", "v1", "confirmed_current", date(2026, 5, 5), reviewer="MB", notes="ok", reg_dir=tiny.reg)
    assert read_csv(tiny.reg / "versions.csv")[0]["last_reviewed_date"] == "2026-05-05"
    log = read_csv(tiny.reg / "review_log.csv")
    assert log[0]["outcome"] == "confirmed_current" and log[0]["reviewer"] == "MB"
    # an older backdated review is logged but must not move last_reviewed_date backwards
    record_review("a", "v1", "other", date(2026, 4, 1), reg_dir=tiny.reg)
    assert read_csv(tiny.reg / "versions.csv")[0]["last_reviewed_date"] == "2026-05-05"
    assert len(read_csv(tiny.reg / "review_log.csv")) == 2
    open_register(tiny.reg)  # still loads cleanly


def test_record_review_rejects_bad_input(tiny):
    tiny.add("a")
    tiny.write()
    with pytest.raises(RegisterError):
        record_review("a", "v1", "bogus", date(2026, 5, 5), reg_dir=tiny.reg)
    with pytest.raises(RegisterError):
        record_review("a", "nope", "confirmed_current", date(2026, 5, 5), reg_dir=tiny.reg)
