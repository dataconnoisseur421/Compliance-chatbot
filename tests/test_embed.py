from __future__ import annotations

from compliance_assistant.embed import HashingEmbedder, embed_index, rrf, search_hybrid, search_vector
from test_index import build


def test_embed_is_incremental_and_search_respects_status(tmp_path):
    conn = build(tmp_path)
    emb = HashingEmbedder(256)
    assert embed_index(conn, emb) == 4
    assert embed_index(conn, emb) == 0  # nothing new
    hits = search_vector(conn, emb, "report misconduct within 14 days", k=5)
    assert hits[0].version_label == "v2"
    assert all(h.status == "in_force" and h.slug == "notice" for h in hits)  # not superseded, not extended


def test_hybrid_puts_cited_provision_first_and_dedupes(tmp_path):
    conn = build(tmp_path)
    emb = HashingEmbedder(256)
    embed_index(conn, emb)
    hits = search_hybrid(conn, emb, "paragraph 6.14A customer information", k=5)
    assert hits[0].locator == "para 6.14A"
    assert len({h.chunk_id for h in hits}) == len(hits)


def test_hybrid_works_without_an_embedder(tmp_path):
    assert search_hybrid(build(tmp_path), None, "misconduct")[0].slug == "notice"


def test_rrf_rewards_agreement():
    from compliance_assistant.index import Hit

    def h(i):
        return Hit(i, 0, "s", "v", "S", "t", "in_force", "l", 1, "", "", 1, 1)

    fused = rrf([[h(1), h(2), h(3)], [h(3), h(2), h(9)]])
    assert [x.chunk_id for x in fused][:2] == [3, 2]
