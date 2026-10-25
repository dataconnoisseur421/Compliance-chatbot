"""The search index: chunks of every registered document, kept in a separate SQLite file.

The register (documents, versions, status) is the source of truth; this index is
derived from it plus the PDFs and can be deleted and rebuilt at any time with
``register ingest``. Each chunk carries a copy of its version's status so a search
can restrict itself to in-force text without joining back to the register.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from . import chunking as C
from .db import PROJECT_ROOT
from .extract import clean_pages, extract_pages
from .models import Document, DocumentVersion
from .register import open_register

INDEX_PATH = PROJECT_ROOT / "data" / "index" / "index.sqlite"
INDEXED_STATUSES = ("in_force", "superseded")

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL,
    version_label TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    short_name TEXT NOT NULL,
    title TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    category TEXT NOT NULL,
    search_scope TEXT NOT NULL,
    status TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    locator TEXT NOT NULL,
    part INTEGER NOT NULL,
    heading_path TEXT NOT NULL,
    text TEXT NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    kind TEXT NOT NULL,
    UNIQUE (slug, version_label, ordinal)
);
CREATE INDEX ix_chunks_doc ON chunks (slug, version_label);
CREATE INDEX ix_chunks_locator ON chunks (slug, locator);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, heading_path, locator, short_name,
    content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TABLE vectors (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL,
    PRIMARY KEY (chunk_id, model)
);
"""


def connect(path: Path | str = INDEX_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _chunk_document(pdf: Path, doc_type: str) -> tuple[list[C.Chunk], dict, list]:
    if doc_type == "toolkit":
        lines = clean_pages(extract_pages(pdf, layout=False), keep_numeric_edges=True)
        chunks, stats = C.chunk_table_rows(lines)
    elif doc_type in ("act", "regulations", "amending_instrument"):
        lines = clean_pages(extract_pages(pdf), statute=True)
        chunks, stats = C.chunk_statute(lines, "s" if doc_type == "act" else "reg")
    else:
        lines = clean_pages(extract_pages(pdf))
        chunks, stats = C.chunk_paragraphs(lines)
    return C.finalize_chunks(chunks), stats, lines


def add_chunks(conn: sqlite3.Connection, slug: str, version_label: str, sha256: str, short_name: str, title: str,
               doc_type: str, category: str, search_scope: str, status: str, chunks: list[C.Chunk]) -> None:
    for c in chunks:
        cur = conn.execute(
            "INSERT INTO chunks (slug, version_label, sha256, short_name, title, doc_type, category, search_scope,"
            " status, ordinal, locator, part, heading_path, text, page_start, page_end, kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, version_label, sha256, short_name, title, doc_type, category, search_scope, status, c.ordinal,
             c.locator, c.part, c.heading_path, c.text, c.page_start, c.page_end, c.kind),
        )
        conn.execute(
            "INSERT INTO chunks_fts (rowid, text, heading_path, locator, short_name) VALUES (?,?,?,?,?)",
            (cur.lastrowid, c.text, c.heading_path, c.locator, short_name),
        )


def create_index(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn


@dataclass
class IngestResult:
    slug: str
    version_label: str
    status: str
    stats: dict


def ingest(path: Path | str = INDEX_PATH, reg_dir: Path | None = None, root: Path = PROJECT_ROOT) -> list[IngestResult]:
    """Rebuild the whole index from the register and the PDFs."""
    session = open_register(reg_dir) if reg_dir else open_register()
    rows = session.execute(
        select(DocumentVersion, Document)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentVersion.status.in_(INDEXED_STATUSES))
        .order_by(Document.slug, DocumentVersion.version_label)
    ).all()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    conn = create_index(tmp)
    results: list[IngestResult] = []
    for ver, doc in rows:
        chunks, stats, lines = _chunk_document(root / ver.file_path, doc.doc_type)
        stats = {**C.chunk_stats(chunks, lines), **stats}
        add_chunks(conn, doc.slug, ver.version_label, ver.sha256, doc.short_name, doc.title, doc.doc_type,
                   doc.category, doc.search_scope, ver.status, chunks)
        results.append(IngestResult(doc.slug, ver.version_label, ver.status, stats))
    conn.execute("INSERT INTO meta VALUES ('schema', '1')")
    conn.commit()
    conn.close()
    path.unlink(missing_ok=True)
    tmp.rename(path)
    return results


# ---------------------------------------------------------------------------- search

STOPWORDS = set(
    "a an and are as at be been but by can could do does for from had has have how i if in into is it its may "
    "me my no not of on or our shall should so such than that the their them then there these they this to "
    "under up us was we what when where which who whom will with would you your".split()
)
# "section 82", "s 82(1)", "reg 12", "regulation 5", "paragraph 6.14A", "para 3.2", "item 7"
CITE_RE = re.compile(
    r"\b(?P<kind>sections?|s|regulations?|regs?|reg|paragraphs?|paras?|para|items?)\.?\s*"
    r"(?P<num>\d+(?:[.\-]\d+)*[A-Z]{0,4})(?:\((?P<sub>\d+[A-Z]{0,3})\))?",
    re.IGNORECASE,
)


@dataclass
class Hit:
    chunk_id: int
    score: float
    slug: str
    version_label: str
    short_name: str
    title: str
    status: str
    locator: str
    part: int
    heading_path: str
    text: str
    page_start: int
    page_end: int
    via: str = "keyword"

    @property
    def citation(self) -> str:
        loc = self.locator if self.part == 1 else f"{self.locator} (part {self.part})"
        pages = f"p {self.page_start}" if self.page_start == self.page_end else f"pp {self.page_start}-{self.page_end}"
        return f"{self.short_name}, {loc} [{pages}]"


def fts_query(question: str) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", question)
    terms = []
    for w in words:
        lw = w.lower()
        if lw in STOPWORDS or (len(lw) < 2):
            continue
        terms.append(f'"{w}"')
    seen, uniq = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return " OR ".join(uniq)


def _scope_clause(scope: str, include_superseded: bool) -> tuple[str, list]:
    where, args = [], []
    if not include_superseded:
        where.append("c.status = 'in_force'")
    if scope == "core":
        where.append("c.search_scope = 'core'")
    return (" AND " + " AND ".join(where)) if where else "", args


def _row_to_hit(r: sqlite3.Row, score: float, via: str) -> Hit:
    return Hit(r["id"], score, r["slug"], r["version_label"], r["short_name"], r["title"], r["status"],
               r["locator"], r["part"], r["heading_path"], r["text"], r["page_start"], r["page_end"], via)


def search_keyword(conn: sqlite3.Connection, question: str, k: int = 10, scope: str = "core",
                   include_superseded: bool = False) -> list[Hit]:
    q = fts_query(question)
    if not q:
        return []
    clause, args = _scope_clause(scope, include_superseded)
    rows = conn.execute(
        "SELECT c.*, bm25(chunks_fts, 1.0, 0.6, 0.3, 0.3) AS score FROM chunks_fts "
        "JOIN chunks c ON c.id = chunks_fts.rowid "
        f"WHERE chunks_fts MATCH ?{clause} ORDER BY score LIMIT ?",
        [q, *args, k],
    ).fetchall()
    return [_row_to_hit(r, -r["score"], "keyword") for r in rows]


def search_citations(conn: sqlite3.Connection, question: str, scope: str = "core",
                     include_superseded: bool = False, limit: int = 6) -> list[Hit]:
    """If the question names a provision ("section 82", "paragraph 6.14A"), fetch it directly."""
    clause, args = _scope_clause(scope, include_superseded)
    hits: list[Hit] = []
    for m in CITE_RE.finditer(question):
        kind = m.group("kind").lower()
        num = m.group("num").upper()
        prefix = {"s": "s", "section": "s", "sections": "s"}.get(kind)
        if prefix is None:
            prefix = "reg" if kind.startswith("reg") else ("item" if kind.startswith("item") else "para")
        sub = m.group("sub")
        pats = [f"{prefix} {num}"]
        if sub:
            pats.insert(0, f"{prefix} {num}({sub})")
        for pat in pats:
            rows = conn.execute(
                f"SELECT c.* FROM chunks c WHERE (c.locator = ? OR c.locator LIKE ? OR c.locator LIKE ?){clause} "
                "ORDER BY c.slug, c.ordinal LIMIT ?",
                [pat, f"{pat}(%", f"{pat} %", *args, limit],
            ).fetchall()
            hits += [_row_to_hit(r, 100.0, "citation") for r in rows]
            if rows:
                break
    seen, out = set(), []
    for h in hits:
        if h.chunk_id not in seen:
            seen.add(h.chunk_id)
            out.append(h)
    return out[:limit]


def search(conn: sqlite3.Connection, question: str, k: int = 10, scope: str = "core",
           include_superseded: bool = False) -> list[Hit]:
    """Keyword search plus direct hits for any provision the question cites by number."""
    direct = search_citations(conn, question, scope, include_superseded)
    kw = search_keyword(conn, question, k, scope, include_superseded)
    kw_ids = {h.chunk_id for h in kw}
    direct.sort(key=lambda h: h.chunk_id not in kw_ids)  # a cited provision the words also match comes first
    out, seen = [], set()
    for h in [*direct, *kw]:
        if h.chunk_id not in seen:
            seen.add(h.chunk_id)
            out.append(h)
    return out[: max(k, len(direct))]
