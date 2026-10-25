"""Embeddings and hybrid (keyword + vector) search.

The embedder is pluggable. ``hashing`` needs nothing downloaded and only matches
shared words, so it is for plumbing tests; real semantic search uses a local
open-source model via the optional ``embed`` extra, for example
``register embed --model BAAI/bge-small-en-v1.5``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Protocol

import numpy as np

from .index import Hit, _row_to_hit, _scope_clause, search_citations, search_keyword

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


class HashingEmbedder:
    """Bag-of-words feature hashing. Deterministic, offline; no semantic understanding."""

    def __init__(self, dim: int = 512):
        self.name = f"hashing-{dim}"
        self.dim = dim

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.vstack([self._one(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._one(text)


class SentenceTransformerEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError("Install the embedding extra: pip install -e '.[embed]'") from exc
        self.name = model
        self._m = SentenceTransformer(model)
        self.dim = int(self._m.get_sentence_embedding_dimension())
        self._query_prefix = BGE_QUERY_PREFIX if "bge" in model.lower() and "m3" not in model.lower() else ""

    def embed_documents(self, texts: list[str]) -> np.ndarray:  # pragma: no cover
        return np.asarray(self._m.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=True),
                          dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:  # pragma: no cover
        return np.asarray(self._m.encode([self._query_prefix + text], normalize_embeddings=True)[0], dtype=np.float32)


def get_embedder(model: str) -> Embedder:
    if model.startswith("hashing"):
        parts = model.split("-")
        return HashingEmbedder(int(parts[1]) if len(parts) > 1 else 512)
    return SentenceTransformerEmbedder(model)


def _embedding_input(row: sqlite3.Row) -> str:
    """What gets embedded: the heading trail gives a bare paragraph its context."""
    return f"{row['short_name']} | {row['heading_path']} | {row['locator']}\n{row['text']}"


def embed_index(conn: sqlite3.Connection, embedder: Embedder, batch: int = 256) -> int:
    """Embed every chunk that has no vector for this model yet. Returns how many were added."""
    rows = conn.execute(
        "SELECT * FROM chunks WHERE id NOT IN (SELECT chunk_id FROM vectors WHERE model = ?) ORDER BY id",
        (embedder.name,),
    ).fetchall()
    for i in range(0, len(rows), batch):
        part = rows[i : i + batch]
        mat = embedder.embed_documents([_embedding_input(r) for r in part])
        conn.executemany(
            "INSERT INTO vectors (chunk_id, model, dim, vec) VALUES (?,?,?,?)",
            [(r["id"], embedder.name, embedder.dim, mat[j].astype(np.float32).tobytes()) for j, r in enumerate(part)],
        )
        conn.commit()
    return len(rows)


def search_vector(conn: sqlite3.Connection, embedder: Embedder, question: str, k: int = 10,
                  scope: str = "core", include_superseded: bool = False) -> list[Hit]:
    clause, args = _scope_clause(scope, include_superseded)
    rows = conn.execute(
        f"SELECT c.*, v.vec FROM chunks c JOIN vectors v ON v.chunk_id = c.id AND v.model = ? WHERE 1=1{clause}",
        [embedder.name, *args],
    ).fetchall()
    if not rows:
        return []
    mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
    sims = mat @ embedder.embed_query(question)
    top = np.argsort(-sims)[:k]
    return [_row_to_hit(rows[i], float(sims[i]), "vector") for i in top]


def rrf(rankings: list[list[Hit]], k: int = 60) -> list[Hit]:
    """Reciprocal rank fusion: a chunk scores 1/(k+rank) in every list that contains it."""
    score: dict[int, float] = {}
    keep: dict[int, Hit] = {}
    for lst in rankings:
        for rank, h in enumerate(lst, start=1):
            score[h.chunk_id] = score.get(h.chunk_id, 0.0) + 1.0 / (k + rank)
            keep.setdefault(h.chunk_id, h)
    out = []
    for cid in sorted(score, key=lambda c: -score[c]):
        h = keep[cid]
        h.score = score[cid]
        out.append(h)
    return out


def search_hybrid(conn: sqlite3.Connection, embedder: Embedder | None, question: str, k: int = 10,
                  scope: str = "core", include_superseded: bool = False) -> list[Hit]:
    """Cited provisions first, then keyword and vector rankings fused. Falls back to keyword only."""
    direct = search_citations(conn, question, scope, include_superseded)
    lists = [search_keyword(conn, question, 40, scope, include_superseded)]
    if embedder is not None:
        lists.append(search_vector(conn, embedder, question, 40, scope, include_superseded))
    fused = rrf(lists)
    seen, out = set(), []
    for h in [*direct, *fused]:
        if h.chunk_id not in seen:
            seen.add(h.chunk_id)
            out.append(h)
    return out[: max(k, len(direct))]
