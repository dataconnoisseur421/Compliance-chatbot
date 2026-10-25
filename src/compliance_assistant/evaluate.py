"""Retrieval evaluation: does the right provision appear in the top results?

Each question in ``data/eval/retrieval.csv`` lists one or more acceptable answers as
``slug|locator`` (``;`` separated). A locator matches by prefix, so ``para 6.14``
accepts ``para 6.14A`` only if you write it that way: write the locator you expect.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .db import PROJECT_ROOT
from .index import Hit

EVAL_PATH = PROJECT_ROOT / "data" / "eval" / "retrieval.csv"


@dataclass
class EvalRow:
    id: str
    style: str
    question: str
    expected: list[tuple[str, str]]


@dataclass
class EvalOutcome:
    row: EvalRow
    rank: int | None  # 1-based rank of the first acceptable hit, None if absent


def load_eval(path: Path = EVAL_PATH) -> list[EvalRow]:
    rows = []
    for r in csv.DictReader(open(path, newline="", encoding="utf-8")):
        exp = []
        for part in r["expected"].split(";"):
            slug, _, loc = part.partition("|")
            exp.append((slug.strip(), loc.strip()))
        rows.append(EvalRow(r["id"], r["style"], r["question"], exp))
    return rows


def locator_matches(locator: str, wanted: str) -> bool:
    if not wanted:
        return True
    if locator == wanted:
        return True
    return bool(re.match(re.escape(wanted) + r"(?:[ (\[]|$)", locator))


def is_acceptable(hit: Hit, row: EvalRow) -> bool:
    return any(hit.slug == slug and locator_matches(hit.locator, loc) for slug, loc in row.expected)


def run_eval(search_fn, rows: list[EvalRow], k: int = 10) -> list[EvalOutcome]:
    out = []
    for row in rows:
        hits = search_fn(row.question, k)
        rank = next((i for i, h in enumerate(hits, start=1) if is_acceptable(h, row)), None)
        out.append(EvalOutcome(row, rank))
    return out


def summarise(outcomes: list[EvalOutcome], ks=(1, 3, 5, 10)) -> dict:
    n = max(1, len(outcomes))
    s = {f"recall@{k}": sum(1 for o in outcomes if o.rank and o.rank <= k) / n for k in ks}
    s["mrr"] = sum(1 / o.rank for o in outcomes if o.rank) / n
    return s
