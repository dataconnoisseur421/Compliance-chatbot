from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from compliance_assistant.cli import main
from compliance_assistant.db import alembic_config, get_engine, migrate
from compliance_assistant.models import Base
from compliance_assistant.register import rebuild_database


def test_migrations_match_models(tmp_path):
    """If a model changes without a new migration, this fails."""
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    migrate(url)
    engine = get_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_migration_is_idempotent_and_reversible(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    migrate(url)
    migrate(url)  # second run is a no-op
    assert {"documents", "document_versions", "review_log"} <= set(inspect(get_engine(url)).get_table_names())
    command.downgrade(alembic_config(url), "base")
    assert "documents" not in inspect(get_engine(url)).get_table_names()


def test_rebuild_database_is_repeatable(tmp_path):
    url = f"sqlite:///{tmp_path / 'r.sqlite'}"
    assert rebuild_database(url) == (22, 23)
    assert rebuild_database(url) == (22, 23)  # reload replaces, never duplicates


def test_cli_check_and_list_run_cleanly(capsys):
    assert main(["check"]) == 0
    assert "0 error(s)" in capsys.readouterr().out
    assert main(["list", "--status", "superseded"]) == 0
    assert "2 version(s)" in capsys.readouterr().out
    assert main(["overdue", "--as-of", "2026-10-24"]) == 0
    assert "Nothing overdue" in capsys.readouterr().out


# ---------------------------------------------------------------------------------
# Alembic's comparison does not look at CHECK constraints, so exercise them directly
# on a database built purely from the migration (not from the models).
# ---------------------------------------------------------------------------------
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_DOC = ("INSERT INTO documents (slug, title, short_name, issuer, doc_type, category) "
        "VALUES ('d', 't', 's', 'MAS', :t, 'c')")
_VER = ("INSERT INTO document_versions (document_id, version_label, file_path, sha256, status, "
        "date_added, superseded_by_version_id, effective_date, effective_to) "
        "VALUES (1, :l, :p, :h, :s, '2026-01-01', :sup, :e1, :e2)")
_OK = dict(l="v0", p="p0", h="a" * 64, s="draft", sup=None, e1=None, e2=None)


@pytest.fixture
def migrated(tmp_path):
    url = f"sqlite:///{tmp_path / 'c.sqlite'}"
    migrate(url)
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(text(_DOC), {"t": "guidelines"})
        conn.execute(text(_VER), _OK)
    return engine


@pytest.mark.parametrize(
    "override",
    [
        dict(s="maybe"),                              # status not allowed
        dict(h="abc"),                                # fingerprint wrong length
        dict(s="draft", sup=1),                       # replacement only on superseded rows
        dict(e1="2026-05-01", e2="2026-04-01"),       # effective_to before effective_date
        dict(p="p0"),                                 # duplicate file path
        dict(l="v0"),                                 # duplicate (document, version label)
    ],
)
def test_migrated_database_rejects_bad_versions(migrated, override):
    row = {**_OK, "l": "v1", "p": "p1", **override}
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(text(_VER), row)


def test_migrated_database_allows_only_one_in_force(migrated):
    with migrated.begin() as conn:
        conn.execute(text(_VER), {**_OK, "l": "v1", "p": "p1", "s": "in_force"})
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(text(_VER), {**_OK, "l": "v2", "p": "p2", "s": "in_force"})


def test_migrated_database_rejects_bad_documents(migrated):
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(text(_DOC.replace("'d'", "'d2'")), {"t": "memo"})  # unknown doc_type
    with pytest.raises(IntegrityError), migrated.begin() as conn:
        conn.execute(text("UPDATE documents SET review_interval_days = 0"))


def test_cli_fingerprint_matches_registered_hash(capsys):
    from compliance_assistant.db import PROJECT_ROOT
    from compliance_assistant.register import read_csv

    row = read_csv(PROJECT_ROOT / "data/register/versions.csv")[0]
    assert main(["fingerprint", str(PROJECT_ROOT / row["file_path"])]) == 0
    out = capsys.readouterr().out
    assert f"sha256={row['sha256']}" in out and f"page_count={row['page_count']}" in out
    assert main(["fingerprint", "no-such-file.pdf"]) == 2


def test_build_creates_missing_database_folder(tmp_path):
    """Regression: a fresh checkout has no data/db folder yet."""
    url = f"sqlite:///{tmp_path / 'not' / 'yet' / 'there' / 'r.sqlite'}"
    assert rebuild_database(url) == (22, 23)
    assert (tmp_path / "not" / "yet" / "there" / "r.sqlite").is_file()
