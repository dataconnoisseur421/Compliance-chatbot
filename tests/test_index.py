from __future__ import annotations

from compliance_assistant import chunking as C
from compliance_assistant.index import add_chunks, create_index, fts_query, search


def ch(locator, text, heading="Heading", n=0):
    return C.Chunk(locator, heading, text, 1, 1, "paragraph", ordinal=n)


def build(tmp_path):
    conn = create_index(tmp_path / "i.sqlite")
    add_chunks(conn, "notice", "v2", "a" * 64, "Notice", "A Notice", "notice", "c", "core", "in_force",
               [ch("para 4", "A licence holder shall report misconduct to the Authority within 14 days.", n=0),
                ch("para 6.14A", "A CMI shall obtain the following customer information.", n=1)])
    add_chunks(conn, "notice", "v1", "b" * 64, "Notice", "A Notice", "notice", "c", "core", "superseded",
               [ch("para 4", "A licence holder shall report misconduct within 30 days.", n=0)])
    add_chunks(conn, "act", "v1", "c" * 64, "Companies Act", "Companies Act", "act", "c", "extended", "in_force",
               [ch("s 6", "Misconduct by a director of a company is dealt with here.", n=0)])
    conn.commit()
    return conn


def test_fts_query_drops_stopwords():
    assert fts_query("What is the time for the report?") == '"time" OR "report"'


def test_search_returns_only_in_force_core_by_default(tmp_path):
    conn = build(tmp_path)
    hits = search(conn, "report misconduct")
    assert [(h.slug, h.version_label) for h in hits] == [("notice", "v2")]
    assert "14 days" in hits[0].text


def test_scope_all_and_superseded_are_opt_in(tmp_path):
    conn = build(tmp_path)
    assert {h.slug for h in search(conn, "misconduct", scope="all")} == {"notice", "act"}
    versions = {(h.slug, h.version_label) for h in search(conn, "misconduct", include_superseded=True)}
    assert ("notice", "v1") in versions


def test_cited_provision_is_fetched_directly(tmp_path):
    conn = build(tmp_path)
    hits = search(conn, "what does paragraph 6.14A require?")
    assert hits[0].via == "citation" and hits[0].locator == "para 6.14A"
    assert hits[0].citation == "Notice, para 6.14A [p 1]"
