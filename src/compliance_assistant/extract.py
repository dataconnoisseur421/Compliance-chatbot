"""PDF text extraction and cleaning.

Uses ``pdftotext`` (poppler). Layout mode keeps indentation, which the
chunkers rely on to tell headings, numbered paragraphs and body text apart.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


class ExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Line:
    page: int  # 1-based PDF page
    text: str  # original indentation preserved, right-stripped; "" for a blank line


LEADER_RE = re.compile(r"\.{4,}\s*\d+\s*$")  # "Heading ........ 12" (contents pages)
AMEND_NOTE_RE = re.compile(r"^\s*\[[^\[\]]{1,160}\]\s*$")  # "[10/2013]", "[Amended on 30 May 2025]"
PAGE_NUM_RE = re.compile(r"^\s*(?:page\s+)?\d{1,4}(?:\s+of\s+\d{1,4})?\s*$", re.IGNORECASE)
BODY_START_RE = re.compile(r"^\s{0,8}1\.(?:—\(1\))?\s+\S")  # first section of an Act / Regulations


def extract_pages(pdf: Path, layout: bool = True) -> list[list[str]]:
    """Return one list of right-stripped lines per PDF page."""
    if shutil.which("pdftotext") is None:
        raise ExtractionError(
            "pdftotext not found. Install poppler: 'apt install poppler-utils', "
            "'brew install poppler', or the Windows poppler build."
        )
    cmd = ["pdftotext", *(["-layout"] if layout else []), str(pdf), "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return [[ln.rstrip() for ln in pg.splitlines()] for pg in pages]


def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.split())).lower()


def clean_pages(
    pages: list[list[str]],
    *,
    statute: bool = False,
    keep_numeric_edges: bool = False,
) -> list[Line]:
    """Flatten pages to lines, dropping contents pages, running headers/footers,
    page numbers and amendment-note lines.

    ``keep_numeric_edges`` is for tables, where a number alone on a line can be a
    row number; only a number equal to the page number is then removed.
    """
    drop: set[int] = set()
    for i, pg in enumerate(pages):
        nonblank = [ln for ln in pg if ln.strip()]
        if nonblank and sum(1 for ln in nonblank if LEADER_RE.search(ln)) / len(nonblank) >= 0.35:
            drop.add(i)
    if statute:
        for i, pg in enumerate(pages):
            if any(BODY_START_RE.match(ln) for ln in pg):
                drop |= set(range(i))
                break

    edge_keys: Counter[str] = Counter()
    edges: dict[int, set[int]] = {}
    for i, pg in enumerate(pages):
        if i in drop:
            continue
        idx = [j for j, ln in enumerate(pg) if ln.strip()]
        edges[i] = set(idx[:3] + idx[-3:])
        for j in edges[i]:
            edge_keys[_norm(pg[j])] += 1
    live = len(pages) - len(drop)
    threshold = max(3, int(0.3 * live))

    out: list[Line] = []
    for i, pg in enumerate(pages):
        if i in drop:
            continue
        for j, raw in enumerate(pg):
            if not raw.strip():
                out.append(Line(i + 1, ""))
                continue
            if j in edges[i]:
                key = _norm(raw)
                is_numeric = bool(PAGE_NUM_RE.match(raw))
                if is_numeric and not keep_numeric_edges:
                    continue
                if is_numeric and keep_numeric_edges:
                    if raw.strip() == str(i + 1) and j == max(edges[i]):
                        continue  # the printed page number at the foot of the page
                elif edge_keys[key] >= threshold:
                    continue
            if AMEND_NOTE_RE.match(raw):
                out.append(Line(i + 1, ""))
                continue
            out.append(Line(i + 1, raw))
    return out


LIST_MARK_RE = re.compile(r"^(?:\(\w{1,5}\)|\w{1,4}\)|[•▪\-–]\s|\d+\.\s)")


def reflow(lines: list[str]) -> str:
    """Join wrapped lines into readable text, keeping list items on their own lines."""
    out: list[str] = []
    new_line = True
    for raw in lines:
        t = raw.strip()
        if not t:
            new_line = True
            continue
        if out and not new_line and not LIST_MARK_RE.match(t):
            prev = out[-1]
            out[-1] = prev + t if (prev.endswith("-") and not prev.endswith(" -")) else prev + " " + t
        else:
            out.append(t)
        new_line = False
    return "\n".join(re.sub(r"[ \t]{2,}", " ", s) for s in out).strip()
