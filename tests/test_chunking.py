from __future__ import annotations

import shutil

import pytest

from compliance_assistant import chunking as C
from compliance_assistant.db import PROJECT_ROOT
from compliance_assistant.extract import Line, clean_pages, extract_pages, reflow


def L(text: str, page: int = 1) -> list[Line]:
    return [Line(page, ln) for ln in text.splitlines()]


def test_reflow_joins_wrapped_lines_and_keeps_list_items():
    out = reflow(["A CMI shall do the", "thing.", "(a) first;", "(b) second."])
    assert out == "A CMI shall do the thing.\n(a) first;\n(b) second."


def test_clean_pages_removes_repeated_headers_and_page_numbers():
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    pages = [["Running header", "", f"{w} text", "", str(i + 1)] for i, w in enumerate(words)]
    lines = [ln.text for ln in clean_pages(pages) if ln.text.strip()]
    assert lines == [f"{w} text" for w in words]


def test_statute_chunker_splits_sections_and_rejects_cross_references():
    text = """
Short title
  1.—(1) This Act is the Test Act.
  (2) It applies everywhere.

Interpretation
  2. In this Act, unless stated otherwise, the following
      applies. See
  99. of the other Act for more.

Licensing
  3. A person must not do it.
"""
    chunks, stats = C.chunk_statute(L(text), "s")
    locs = [c.locator for c in chunks]
    assert locs == ["s 1", "s 2", "s 3"]
    assert "s 3 Licensing" in chunks[2].heading_path
    assert stats["rejected_starts"] == 1


def test_paragraph_chunker_dotted_numbers_and_headings():
    text = """
6   CUSTOMER DUE DILIGENCE

6.14  A CMI shall obtain the following
      information.

6.14A For the purposes of paragraph 6.14,
      a CMI shall also obtain more.
"""
    chunks, _ = C.chunk_paragraphs(L(text))
    assert [c.locator for c in chunks] == ["para 6.14", "para 6.14A"]
    assert chunks[0].heading_path.startswith("6 CUSTOMER DUE DILIGENCE")


def test_hyphenated_paragraph_numbers():
    text = "\n1-4-6   Terrorists require funds to carry out acts.\n\n1-4-7   Another paragraph here.\n"
    chunks, _ = C.chunk_paragraphs(L(text))
    assert [c.locator for c in chunks] == ["para 1-4-6", "para 1-4-7"]


def test_long_units_are_split_into_parts_with_unique_locators():
    body = "\n".join(["      This is a sentence that makes the paragraph long."] * 80)
    chunks, _ = C.chunk_paragraphs(L(f"\n5.1   Start of it.\n{body}\n"))
    chunks = C.finalize_chunks(chunks)
    assert len(chunks) > 1 and all(c.tokens <= 500 for c in chunks)
    assert len({c.display_locator for c in chunks}) == len(chunks)


def test_table_rows():
    text = "\n1\nApproval for X\nwithin 14 days\n\n2\nNotify Y\n\n3\nFile Z\n"
    chunks, stats = C.chunk_table_rows(L(text))
    assert [c.locator for c in chunks] == ["item 1", "item 2", "item 3"]


# ----- anchors in the real documents (skipped where poppler or the PDFs are absent) -----
real = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler not installed")


@real
def test_real_documents_anchor_provisions():
    import csv

    reg = {(r["document_slug"], r["status"]): r for r in csv.DictReader(open(PROJECT_ROOT / "data/register/versions.csv"))}
    pdf = PROJECT_ROOT / reg[("sfa04-n02", "in_force")]["file_path"]
    chunks, _ = C.chunk_paragraphs(clean_pages(extract_pages(pdf)))
    assert "para 6.14A" in {c.locator for c in chunks}
    pdf = PROJECT_ROOT / reg[("sfa-2001", "in_force")]["file_path"]
    chunks, _ = C.chunk_statute(clean_pages(extract_pages(pdf), statute=True), "s")
    s82 = [c for c in chunks if c.locator.startswith("s 82")]
    assert s82 and "carry on business in any regulated activity" in s82[0].text.replace("\n", " ")
