"""Structure-aware chunking.

Each chunk is a provision, numbered paragraph, definition or table row, keeping
the locator a lawyer would cite (``s 82(1)``, ``reg 12(2)``, ``para 6.14A``,
``item 5``), the heading trail, and the PDF pages it came from.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .extract import LEADER_RE, Line, reflow

MAX_CHARS = 1800  # about 450 tokens

# --------------------------------------------------------------------------- model


@dataclass
class Chunk:
    locator: str
    heading_path: str
    text: str
    page_start: int
    page_end: int
    kind: str  # provision | definition | paragraph | table_row | text
    part: int = 1
    ordinal: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return max(1, len(self.text) // 4)

    @property
    def display_locator(self) -> str:
        return self.locator if self.part == 1 else f"{self.locator} (part {self.part})"


def _pages(lines: list[Line]) -> tuple[int, int]:
    pages = [ln.page for ln in lines if ln.text.strip()] or [lines[0].page if lines else 0]
    return min(pages), max(pages)


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def split_lines(lines: list[Line], cap: int = MAX_CHARS) -> list[list[Line]]:
    """Split a long run of lines into groups of at most ``cap`` characters, preferring
    to break after a sentence end or before a list item."""
    body = [ln for ln in lines if ln.text.strip()]
    if sum(len(ln.text.strip()) + 1 for ln in body) <= cap:
        return [lines]
    groups: list[list[Line]] = []
    cur: list[Line] = []
    size = 0
    last_break = -1
    list_item = re.compile(r"^\s*(?:\(\w{1,5}\)|\w{1,4}\)|\d+\.)\s")
    for ln in body:
        n = len(ln.text.strip()) + 1
        if size + n > cap and cur:
            if last_break >= 0 and sum(len(x.text.strip()) + 1 for x in cur[: last_break + 1]) >= 0.4 * cap:
                groups.append(cur[: last_break + 1])
                cur = cur[last_break + 1 :]
            else:
                groups.append(cur)
                cur = []
            size = sum(len(x.text.strip()) + 1 for x in cur)
            last_break = -1
        if cur and list_item.match(ln.text):
            last_break = len(cur) - 1
        cur.append(ln)
        size += n
        if ln.text.rstrip().endswith((".", ";")):
            last_break = len(cur) - 1
    if cur:
        groups.append(cur)
    return groups


def _emit(
    out: list[Chunk],
    lines: list[Line],
    locator: str,
    heading: str,
    kind: str,
    cap: int = MAX_CHARS,
) -> None:
    for k, grp in enumerate(split_lines(lines, cap), start=1):
        text = reflow([ln.text for ln in grp])
        if not text:
            continue
        p0, p1 = _pages(grp)
        out.append(Chunk(locator, heading, text, p0, p1, kind, part=k))


# ------------------------------------------------------------------------- statutes

SECTION_RE = re.compile(r"^\s{1,8}(\d+[A-Z]{0,4})\.(?:—\((\d+[A-Z]{0,3})\)|\s)\s*(.*)$")
SUB_RE = re.compile(r"^\s{2,5}\((\d+[A-Z]{0,3})\)\s+\S")
DEF_RE = re.compile(r"^\s{0,4}[“\"]([^”\"]{1,90})[”\"]")
PART_RE = re.compile(r"^\s*(?:PART|Part)\s+(\d+[A-Z]{0,3})\s*$")
DIV_RE = re.compile(r"^\s*(?:Division|DIVISION)\s+(\d+[A-Z]{0,3})\s*[—–-]\s*(.*)$")
SCHEDULE_RE = re.compile(r"^\s*((?:THE\s+)?(?:[A-Z]+\s+)?SCHEDULES?)\s*$")


def _sec_key(num: str) -> tuple[int, str]:
    m = re.match(r"(\d+)([A-Z]*)", num)
    return int(m.group(1)), m.group(2)


def _collect_block(lines: list[Line], i: int) -> tuple[list[str], int]:
    """Consecutive non-blank lines from ``i``; returns (texts, next index)."""
    texts = []
    while i < len(lines) and lines[i].text.strip():
        texts.append(lines[i].text.strip())
        i += 1
    return texts, i


@dataclass
class _Section:
    num: str
    first_sub: str | None
    title: str | None
    path: list[str]
    schedule: str | None
    lines: list[Line]


def chunk_statute(lines: list[Line], label: str) -> tuple[list[Chunk], dict]:
    """``label`` is ``s`` for Acts and ``reg`` for subsidiary legislation."""
    chunks: list[Chunk] = []
    stats = {"sections": 0, "rejected_starts": 0}
    part = division = schedule = None
    last_key: tuple[int, str] = (0, "")
    cur: _Section | None = None

    def finalize():
        nonlocal cur
        if cur is not None:
            while cur.lines and not cur.lines[-1].text.strip():
                cur.lines.pop()
            _emit_section(chunks, cur, label)
            stats["sections"] += 1
        cur = None

    i = 0
    n = len(lines)
    while i < n:
        t = lines[i].text
        if not t.strip():
            if cur is not None:
                cur.lines.append(lines[i])
            i += 1
            continue

        if PART_RE.match(t) and (i == 0 or not lines[i - 1].text.strip()):
            m = PART_RE.match(t)
            block, j = _collect_block(lines, i + 1)
            finalize()
            title = " ".join(block)
            part = f"Part {m.group(1)} {title}".strip()
            division = None
            if schedule is None:
                last_key = last_key  # numbering continues across Parts
            i = j
            continue
        m = DIV_RE.match(t)
        if m and (i == 0 or not lines[i - 1].text.strip()):
            finalize()
            title = m.group(2).strip()
            j = i + 1
            if j < n and lines[j].text.strip() and _indent(lines[j].text) > 2 and not title.endswith("."):
                title += " " + lines[j].text.strip()
                j += 1
            division = f"Division {m.group(1)} — {title}"
            i = j
            continue
        m = SCHEDULE_RE.match(t)
        if m and (i == 0 or not lines[i - 1].text.strip()):
            block, j = _collect_block(lines, i + 1)
            finalize()
            name = m.group(1).title().replace("The ", "")
            schedule = name
            part = division = None
            last_key = (0, "")
            i = j
            continue

        m = SECTION_RE.match(t)
        if m:
            key = _sec_key(m.group(1))
            gap_ok = key > last_key and (key[0] - last_key[0] <= (40 if schedule else 15))
            if gap_ok:
                # section heading = unindented non-blank lines directly above, after a blank line
                heading_lines: list[str] = []
                if cur is not None:
                    k = len(cur.lines) - 1
                    block: list[str] = []
                    while k >= 0 and cur.lines[k].text.strip() and _indent(cur.lines[k].text) <= 2 and len(block) < 3:
                        block.append(cur.lines[k].text.strip())
                        k -= 1
                    block.reverse()
                    preceded_by_blank = k < 0 or not cur.lines[k].text.strip()
                    if block and preceded_by_blank and not block[-1].endswith((".", ";", ",", ":", "—", "-")):
                        heading_lines = block
                        del cur.lines[len(cur.lines) - len(block) :]
                finalize()
                title = " ".join(heading_lines) or None
                path = [p for p in (part, division) if p]
                cur = _Section(m.group(1), m.group(2), title, path, schedule, [lines[i]])
                last_key = key
                i += 1
                continue
            stats["rejected_starts"] += 1
        if cur is not None:
            cur.lines.append(lines[i])
        i += 1
    finalize()
    return chunks, stats


def _emit_section(out: list[Chunk], sec: _Section, label: str) -> None:
    lab = f"{sec.schedule} para" if sec.schedule else label
    base = f"{lab} {sec.num}"
    heading = " > ".join([*sec.path, *([sec.schedule] if sec.schedule else []), *([f"{base} {sec.title}"] if sec.title else [base])])
    body = [ln for ln in sec.lines if ln.text.strip()]
    if not body:
        return
    total = sum(len(ln.text.strip()) + 1 for ln in body)
    if total <= MAX_CHARS:
        _emit(out, sec.lines, base, heading, "provision")
        return

    # split into subsection units, then definitions inside them
    units: list[tuple[str | None, list[Line]]] = []
    sub = sec.first_sub
    run: list[Line] = []
    for ln in sec.lines:
        m = SUB_RE.match(ln.text)
        if m and run and ln is not sec.lines[0]:
            units.append((sub, run))
            sub, run = m.group(1), []
        run.append(ln)
    units.append((sub, run))

    pack: list[tuple[str | None, list[Line]]] = []

    def flush_pack():
        nonlocal pack
        if not pack:
            return
        subs = [s for s, _ in pack if s]
        loc = base
        if subs:
            loc = f"{base}({subs[0]})" if len(subs) == 1 else f"{base}({subs[0]})-({subs[-1]})"
        _emit(out, [ln for _, ls in pack for ln in ls], loc, heading, "provision")
        pack = []

    for sub, ls in units:
        defs = [k for k, ln in enumerate(ls) if DEF_RE.match(ln.text)]
        if len(defs) >= 3:
            flush_pack()
            subloc = f"{base}({sub})" if sub else base
            pre = ls[: defs[0]]
            bounds = [*defs, len(ls)]
            for d in range(len(defs)):
                seg = ls[bounds[d] : bounds[d + 1]]
                term = DEF_RE.match(seg[0].text).group(1).strip()
                if d == 0 and any(x.text.strip() for x in pre):
                    seg = pre + seg
                _emit(out, seg, f"{subloc} “{term}”", heading, "definition")
            continue
        size = sum(len(ln.text.strip()) + 1 for ln in ls if ln.text.strip())
        psize = sum(len(ln.text.strip()) + 1 for _, x in pack for ln in x if ln.text.strip())
        if pack and psize + size > MAX_CHARS:
            flush_pack()
        pack.append((sub, ls))
        if size > MAX_CHARS:
            flush_pack()
    flush_pack()


# ------------------------------------------------------------- notices and guidelines

DOTTED_RE = re.compile(r"^\s{0,14}(\d+(?:[.\-]\d+)+[A-Z]{0,2})\.?\s+(\S.*)$")
INT_RE = re.compile(r"^\s{0,8}(\d{1,3}[A-Z]?)\s{2,}(\S.*)$")
NUMHEAD_RE = re.compile(r"^\s{0,14}(\d{1,2}(?:[.\-]\d{1,2}){0,2})\.?\s+(\S.*)$")
APPENDIX_RE = re.compile(r"^\s{0,14}(APPENDIX|Appendix|ANNEX|Annex|ANNEXURE|Annexure)\s+([A-Z0-9]{1,3})\b\s*[:\-–—]?\s*(.*)$")
UNNUMBERED_DEPTH = 50
FUNCTION_ENDINGS = {"the", "and", "to", "of", "a", "an", "in", "for", "or", "by", "with", "on", "that", "is", "be", "as"}


def _clean_heading(text: str) -> str:
    text = re.sub(r"\s{3,}\d{1,3}\s*$", "", text)  # contents-style page number
    return re.sub(r"\s+", " ", text).strip()


def _plausible_start(s: str) -> bool:
    return bool(s) and (s[0].isupper() or s[0] in "“\"‘'(" or s[0].isdigit())


def _short_title(s: str) -> bool:
    s = s.strip()
    if not s or len(s) > 70 or s[-1] in ".;:,—-" or " – " in s:
        return False
    last = re.sub(r"\W+$", "", s.split()[-1]).lower() if s.split() else ""
    return last not in FUNCTION_ENDINGS and (s[0].isupper() or s[0].isdigit())


def _next_nonblank(lines: list[Line], i: int) -> str:
    j = i + 1
    while j < len(lines) and not lines[j].text.strip():
        j += 1
    return lines[j].text if j < len(lines) else ""


def detect_integer_style(lines: list[Line]) -> bool:
    # judge the main text only: forms in appendices often use dotted item numbers
    for k, ln in enumerate(lines):
        if APPENDIX_RE.match(ln.text) and k > 0 and not lines[k - 1].text.strip():
            lines = lines[:k]
            break
    ints = sum(1 for ln in lines if INT_RE.match(ln.text) and _plausible_start(INT_RE.match(ln.text).group(2)))
    dots = sum(1 for ln in lines if DOTTED_RE.match(ln.text) and _plausible_start(DOTTED_RE.match(ln.text).group(2)))
    return ints >= 4 and ints >= dots


def chunk_paragraphs(lines: list[Line]) -> tuple[list[Chunk], dict]:
    chunks: list[Chunk] = []
    stats = {"headings": 0, "units": 0}
    integer_style = detect_integer_style(lines)
    stack: list[tuple[int, str]] = []
    prefix = ""  # "App 2 " inside an appendix
    cur_num: str | None = None
    cur: list[Line] = []
    text_run: list[Line] = []

    def heading_path() -> str:
        return " > ".join(h for _, h in stack)

    def flush_text():
        nonlocal text_run
        if any(ln.text.strip() for ln in text_run):
            first_page = _pages(text_run)[0]
            _emit(chunks, text_run, f"{prefix}p.{first_page}", heading_path(), "text")
        text_run = []

    def flush_unit():
        nonlocal cur, cur_num
        if cur_num is not None and cur:
            while cur and not cur[-1].text.strip():
                cur.pop()
            _emit(chunks, cur, f"{prefix}para {cur_num}", heading_path(), "paragraph")
            stats["units"] += 1
        cur, cur_num = [], None

    def set_heading(depth: int, text: str):
        nonlocal stack
        text = _clean_heading(text)
        stack = [(d, h) for d, h in stack if d < depth] + [(depth, text)]
        stats["headings"] += 1

    for i, ln in enumerate(lines):
        t = ln.text
        if not t.strip():
            (cur if cur_num is not None else text_run).append(ln)
            continue
        prev_blank = i == 0 or not lines[i - 1].text.strip()
        nxt = _next_nonblank(lines, i)

        am = APPENDIX_RE.match(t)
        if am and prev_blank and len(t.strip()) < 100 and not LEADER_RE.search(t):
            flush_unit(); flush_text()
            noun = "Annex" if am.group(1).lower().startswith("annex") else "Appendix"
            stack = [(0, _clean_heading(f"{noun} {am.group(2)} {am.group(3)}"))]
            prefix = f"{'Annex' if noun == 'Annex' else 'App'} {am.group(2)} "
            stats["headings"] += 1
            continue

        nh = NUMHEAD_RE.match(t)
        if (
            nh
            and _short_title(nh.group(2))
            and not (nxt and nxt.strip()[:1].islower())
            and not (integer_style and re.fullmatch(r"\d+", nh.group(1)))
            and not (cur_num is not None and nh.group(1) == cur_num)
        ):
            depth = re.sub(r"-", ".", nh.group(1)).count(".") + 1
            # a numbered line is a heading when it is short, unpunctuated and not a wrapped paragraph start
            flush_unit(); flush_text()
            set_heading(depth, f"{nh.group(1)} {nh.group(2).strip()}")
            continue

        start = None
        dm = DOTTED_RE.match(t)
        if dm and _plausible_start(dm.group(2)):
            start = dm.group(1)
        elif integer_style:
            im = INT_RE.match(t)
            if im and _plausible_start(im.group(2)):
                start = im.group(1)
        if start is not None and start == cur_num:
            start = None  # form items such as "1.3 (a)", "1.3 (b)" stay with paragraph 1.3
        elif start is not None:
            flush_unit(); flush_text()
            cur_num, cur = start, [ln]
            continue

        # unnumbered sub-heading directly above a numbered paragraph
        if prev_blank and _short_title(t) and _indent(t) <= 6 and cur_num is None or (
            prev_blank and _short_title(t) and _indent(t) <= 6 and _starts_unit(nxt, integer_style)
        ):
            if _starts_unit(nxt, integer_style):
                flush_unit(); flush_text()
                set_heading(UNNUMBERED_DEPTH, t.strip())
                continue

        (cur if cur_num is not None else text_run).append(ln)
    flush_unit(); flush_text()
    return chunks, stats


def _starts_unit(text: str, integer_style: bool) -> bool:
    dm = DOTTED_RE.match(text or "")
    if dm and _plausible_start(dm.group(2)):
        return True
    if integer_style:
        im = INT_RE.match(text or "")
        return bool(im and _plausible_start(im.group(2)))
    return False


# ----------------------------------------------------------------------- table rows

ITEM_RE = re.compile(r"^\s*(\d{1,3})\s*$")


def chunk_table_rows(lines: list[Line]) -> tuple[list[Chunk], dict]:
    """Rows of a numbered table extracted in reading order (item number alone on a line)."""
    chunks: list[Chunk] = []
    stats = {"items": 0}
    last = 0
    cur_num: str | None = None
    cur: list[Line] = []
    front: list[Line] = []

    def flush():
        nonlocal cur, cur_num
        if cur_num is not None:
            _emit(chunks, cur, f"item {cur_num}", "Compliance Toolkit > table of approvals, notifications and submissions", "table_row")
            stats["items"] += 1
        cur, cur_num = [], None

    for i, ln in enumerate(lines):
        m = ITEM_RE.match(ln.text)
        prev_blank = i == 0 or not lines[i - 1].text.strip()
        if m and prev_blank and last + 1 <= int(m.group(1)) <= last + 3:
            flush()
            if front:
                _emit(chunks, front, f"p.{_pages(front)[0]}", "Compliance Toolkit > introduction", "text")
                front = []
            cur_num, cur = m.group(1), [ln]
            last = int(m.group(1))
            continue
        (cur if cur_num is not None else front).append(ln)
    flush()
    if front:
        _emit(chunks, front, f"p.{_pages(front)[0]}", "Compliance Toolkit > introduction", "text")
    return chunks, stats


# ---------------------------------------------------------------------------- entry


def finalize_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """Number chunks and make locators unique within the document."""
    seen: dict[tuple[str, int], int] = {}
    for n, c in enumerate(chunks):
        c.ordinal = n
        key = (c.locator, c.part)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            c.locator = f"{c.locator} [{seen[key]}]"
    return chunks


def chunk_stats(chunks: list[Chunk], lines: list[Line]) -> dict:
    toks = [c.tokens for c in chunks] or [0]
    numbered = sum(1 for c in chunks if not re.match(r"^(?:App \w+ )?p\.\d+", c.locator))
    src = sum(len(re.sub(r"\s+", "", ln.text)) for ln in lines)
    got = sum(len(re.sub(r"\s+", "", c.text)) for c in chunks)
    return {
        "chunks": len(chunks),
        "median_tokens": int(statistics.median(toks)),
        "p95_tokens": sorted(toks)[int(0.95 * (len(toks) - 1))],
        "max_tokens": max(toks),
        "pct_with_citation": round(100 * numbered / max(1, len(chunks)), 1),
        "coverage": round(got / max(1, src), 3),
    }
