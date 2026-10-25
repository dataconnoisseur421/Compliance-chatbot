"""Grounded question answering with citations, and client-email drafting.

Simple version: retrieve once, answer from what was retrieved, cite by source number,
then check the citations. The multi-step pipeline (clarify, break into issues, retrieve
per issue, synthesise) is meant to wrap this later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .index import Hit
from .llm import ChatModel

DISCLAIMER = ("Not legal advice. Prepared from the documents in the library only; check the cited provisions "
              "against the current official text before relying on them. Do not enter client-identifying or "
              "confidential information.")

SYSTEM_ANSWER = """You are a research assistant for a compliance professional advising fund management \
companies in Singapore. Answer ONLY from the numbered SOURCES provided.

Rules:
- Cite every statement of law or regulatory expectation with its source number in square brackets, e.g. [2]. \
Use only numbers that appear in SOURCES.
- If the sources do not answer the question, or only partly do, say exactly what is missing. Never fill the gap \
from memory and never invent a section, paragraph or date.
- Distinguish binding requirements (Acts, Regulations, Notices: "must", "shall") from guidance and \
expectations (Guidelines, Codes, Practice Notes: "should", "expected").
- State assumptions you had to make about the facts. If a missing fact would change the answer, say which one.
- Note when a source looks superseded or its version label suggests it may be out of date.
- Be concise and structured. End with a one-line list of what you could not verify from the sources."""

SYSTEM_EMAIL = """You draft emails from a compliance adviser to a client, using ONLY the numbered SOURCES \
and the adviser's notes. Write in plain, professional English. Keep citations as source numbers in square \
brackets after each regulatory statement so the adviser can check them before sending. Use placeholders such as \
[Client name] and [Adviser name]; never invent facts about the client. If the sources do not support a point, \
leave it out and add a line at the end beginning 'NOTE TO ADVISER:' saying what is missing."""


@dataclass
class Answer:
    text: str
    sources: list[Hit]
    cited: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def format_sources(hits: list[Hit], max_chars: int = 2200) -> str:
    blocks = []
    for n, h in enumerate(hits, start=1):
        status = "" if h.status == "in_force" else f" [{h.status.upper()}]"
        body = h.text if len(h.text) <= max_chars else h.text[:max_chars] + " …"
        blocks.append(f"[{n}] {h.title} ({h.version_label}){status} — {h.locator}"
                      f"{'' if not h.heading_path else ' — ' + h.heading_path}\n{body}")
    return "\n\n".join(blocks)


def check_citations(text: str, n_sources: int) -> tuple[list[int], list[str]]:
    """Return the source numbers the answer cites, and problems found."""
    nums = []
    for m in re.finditer(r"\[(\d+(?:\s*[,\-–]\s*\d+)*)\]", text):
        for part in re.split(r"\s*,\s*", m.group(1)):
            if re.search(r"[\-–]", part):
                a, b = (int(x) for x in re.split(r"\s*[\-–]\s*", part))
                nums.extend(range(a, b + 1))
            else:
                nums.append(int(part))
    cited = sorted(set(nums))
    warnings = []
    bad = [n for n in cited if not 1 <= n <= n_sources]
    if bad:
        warnings.append(f"Cites source number(s) {bad} that were not provided - treat those statements as unsupported.")
    if not cited:
        warnings.append("The answer cites no sources.")
    return [n for n in cited if 1 <= n <= n_sources], warnings


NO_SOURCES = "No relevant provisions were found in the library for this question, so I can't answer it from the sources."


def answer_question(question: str, hits: list[Hit], model: ChatModel, mode: str = "answer", notes: str = "") -> Answer:
    if not hits:
        return Answer(NO_SOURCES, [], [], ["No sources retrieved."])
    system = SYSTEM_EMAIL if mode == "email" else SYSTEM_ANSWER
    user = f"SOURCES\n\n{format_sources(hits)}\n\n"
    if mode == "email":
        user += f"CLIENT QUESTION OR SITUATION\n{question}\n\nADVISER NOTES\n{notes or '(none)'}\n\nDraft the email."
    else:
        user += f"QUESTION\n{question}"
    text = model.complete(system, user)
    cited, warnings = check_citations(text, len(hits))
    return Answer(text.strip(), hits, cited, warnings)


def render(ans: Answer) -> str:
    out = [ans.text, ""]
    used = ans.cited or []
    if used:
        out.append("Sources cited:")
        for n in used:
            h = ans.sources[n - 1]
            flag = "" if h.status == "in_force" else f" [{h.status.upper()}]"
            out.append(f"  [{n}] {h.citation} — {h.title}, version {h.version_label}{flag}")
    uncited = [n for n in range(1, len(ans.sources) + 1) if n not in used]
    if uncited:
        out.append("Retrieved but not cited: " + ", ".join(f"[{n}] {ans.sources[n - 1].citation}" for n in uncited))
    for w in ans.warnings:
        out.append(f"WARNING: {w}")
    out += ["", DISCLAIMER]
    return "\n".join(out)
