"""Command line interface: ``register <command>``."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import select

from .checks import ERROR, INFO, WARN, check_register, has_errors
from .db import PROJECT_ROOT
from .models import REVIEW_OUTCOMES, STATUSES, Document, DocumentVersion
from .register import (
    RegisterError,
    open_register,
    rebuild_database,
    record_review,
    review_schedule,
    sha256_of,
)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths]), *[line(r) for r in rows]])


def cmd_build(args) -> int:
    docs, vers = rebuild_database(args.db)
    print(f"Database rebuilt from CSV: {docs} documents, {vers} versions.")
    return 0


def cmd_check(args) -> int:
    session = open_register()
    findings = check_register(session, PROJECT_ROOT, args.as_of)
    shown = [f for f in findings if args.verbose or f.level != INFO]
    for f in shown:
        print(f"{f.level:<5} [{f.code}] {f.message}")
    counts = {lvl: sum(1 for f in findings if f.level == lvl) for lvl in (ERROR, WARN, INFO)}
    print(f"\n{counts[ERROR]} error(s), {counts[WARN]} warning(s), {counts[INFO]} info (use -v to show info).")
    return 1 if has_errors(findings) else 0


def cmd_list(args) -> int:
    session = open_register()
    stmt = (
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .order_by(Document.category, Document.slug, DocumentVersion.version_label)
    )
    if args.status:
        stmt = stmt.where(DocumentVersion.status == args.status)
    rows = []
    for doc, ver in session.execute(stmt):
        key_date = ver.as_at_date or ver.last_revised_date or ver.issue_date
        rows.append([doc.category, doc.short_name, ver.version_label, ver.status, key_date.isoformat() if key_date else ""])
    print(_table(["category", "document", "version", "status", "key date"], rows))
    print(f"\n{len(rows)} version(s).")
    return 0


def cmd_overdue(args) -> int:
    session = open_register()
    today = args.as_of or date.today()
    items = review_schedule(session, today)
    shown = items if args.all else [i for i in items if i.days_overdue > 0]
    rows = [
        [i.document.short_name, i.version.version_label, i.due.isoformat(),
         f"{i.days_overdue}d overdue" if i.days_overdue > 0 else f"due in {-i.days_overdue}d"]
        for i in shown
    ]
    if rows:
        print(_table(["document", "version", "review due", "state"], rows))
    else:
        print(f"Nothing overdue as of {today.isoformat()}.")
    return 0


def cmd_review(args) -> int:
    try:
        record_review(args.slug, args.version, args.outcome, args.on or date.today(), args.reviewer, args.notes)
    except RegisterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Recorded review of {args.slug}@{args.version} ({args.outcome}). Commit the CSV changes to Git.")
    return 0


def cmd_fingerprint(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        return 2
    print(f"sha256={sha256_of(path)}")
    print(f"page_count={len(PdfReader(str(path)).pages)}")
    print(f"file_size_bytes={path.stat().st_size}")
    return 0


def cmd_ingest(args) -> int:
    from .extract import ExtractionError
    from .index import ingest

    try:
        results = ingest()
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rows = [[r.slug, r.version_label, r.status, str(r.stats["chunks"]), str(r.stats["median_tokens"]),
             f"{r.stats['pct_with_citation']:.0f}%", f"{r.stats['coverage']:.2f}"] for r in results]
    print(_table(["document", "version", "status", "chunks", "med tok", "cited", "coverage"], rows))
    print(f"\n{sum(int(r[3]) for r in rows)} chunks from {len(rows)} version(s) written to data/index/index.sqlite")
    return 0


def cmd_embed(args) -> int:
    from .embed import embed_index, get_embedder
    from .index import INDEX_PATH, connect

    if not INDEX_PATH.exists():
        print("error: no index yet. Run 'register ingest' first.", file=sys.stderr)
        return 2
    try:
        embedder = get_embedder(args.model)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    added = embed_index(connect(), embedder)
    print(f"Embedded {added} chunk(s) with {embedder.name} (dim {embedder.dim}).")
    return 0


def cmd_eval_search(args) -> int:
    from .embed import get_embedder, search_hybrid
    from .evaluate import load_eval, run_eval, summarise
    from .index import INDEX_PATH, connect, search

    if not INDEX_PATH.exists():
        print("error: no index yet. Run 'register ingest' first.", file=sys.stderr)
        return 2
    conn = connect()
    if args.model:
        emb = get_embedder(args.model)
        fn = lambda q, k: search_hybrid(conn, emb, q, k=k)
    else:
        fn = lambda q, k: search(conn, q, k=k)
    outcomes = run_eval(fn, load_eval(), k=10)
    for o in outcomes:
        print(f"{o.row.id}  {o.row.style:10}  rank={o.rank or '-':>2}  {o.row.question[:70]}")
    print()
    for key, val in summarise(outcomes).items():
        print(f"{key}: {val:.2f}")
    return 0


def cmd_ask(args) -> int:
    from .answer import answer_question, format_sources, render
    from .embed import get_embedder, search_hybrid
    from .index import INDEX_PATH, connect
    from .llm import AnthropicModel, LLMError, load_dotenv

    if not INDEX_PATH.exists():
        print("error: no index yet. Run 'register ingest' first.", file=sys.stderr)
        return 2
    load_dotenv()
    question = " ".join(args.question)
    conn = connect()
    emb = get_embedder(args.embed_model) if args.embed_model else None
    hits = search_hybrid(conn, emb, question, k=args.k, scope=args.scope)
    if args.dry_run:
        print(format_sources(hits))
        return 0
    try:
        model = AnthropicModel(args.llm)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ans = answer_question(question, hits, model, mode="email" if args.email else "answer", notes=args.notes or "")
    print(render(ans))
    return 0


def cmd_search(args) -> int:
    from .index import INDEX_PATH, connect, search
    from .embed import get_embedder, search_hybrid

    if not INDEX_PATH.exists():
        print("error: no index yet. Run 'register ingest' first.", file=sys.stderr)
        return 2
    conn = connect()
    question = " ".join(args.question)
    if args.model:
        hits = search_hybrid(conn, get_embedder(args.model), question, k=args.k, scope=args.scope,
                             include_superseded=args.superseded)
    else:
        hits = search(conn, question, k=args.k, scope=args.scope, include_superseded=args.superseded)
    if not hits:
        print("No matches.")
        return 0
    for n, h in enumerate(hits, start=1):
        flag = "" if h.status == "in_force" else f"  [{h.status.upper()}]"
        print(f"{n}. {h.citation}{flag}  ({h.via})")
        print(f"   {h.heading_path[:100]}")
        print("   " + h.text[: args.chars].replace("\n", " ") + ("..." if len(h.text) > args.chars else ""))
    return 0


def _iso(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"'{value}' is not a date in YYYY-MM-DD form") from exc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="register", description="Document register tools.")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Migrate the database and reload it from the CSV register.")
    b.add_argument("--db", help="Database URL (default: local SQLite, or $DATABASE_URL).")
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("check", help="Check the register against the files on disk.")
    c.add_argument("--as-of", type=_iso, help="Evaluate date-based checks as of this date.")
    c.add_argument("-v", "--verbose", action="store_true", help="Also show informational findings.")
    c.set_defaults(func=cmd_check)

    ls = sub.add_parser("list", help="List documents and versions.")
    ls.add_argument("--status", choices=STATUSES)
    ls.set_defaults(func=cmd_list)

    o = sub.add_parser("overdue", help="Show in-force documents due for review.")
    o.add_argument("--as-of", type=_iso)
    o.add_argument("--all", action="store_true", help="Show every in-force document, not just overdue ones.")
    o.set_defaults(func=cmd_overdue)

    r = sub.add_parser("review", help="Record that a document version has been reviewed.")
    r.add_argument("slug")
    r.add_argument("version", help="Version label, e.g. as-at-2026-10-23")
    r.add_argument("--outcome", choices=REVIEW_OUTCOMES, default="confirmed_current")
    r.add_argument("--on", type=_iso, help="Review date (default: today).")
    r.add_argument("--reviewer", default="")
    r.add_argument("--notes", default="")
    r.set_defaults(func=cmd_review)

    ig = sub.add_parser("ingest", help="Extract, chunk and index every registered document (rebuilds the index).")
    ig.set_defaults(func=cmd_ingest)

    em = sub.add_parser("embed", help="Compute vectors for every chunk (needs the 'embed' extra for real models).")
    em.add_argument("--model", default="BAAI/bge-small-en-v1.5", help="Model name, or 'hashing-512' for an offline test.")
    em.set_defaults(func=cmd_embed)

    ev = sub.add_parser("eval-search", help="Score retrieval against data/eval/retrieval.csv (recall@k, MRR).")
    ev.add_argument("--model", help="Embedding model for hybrid search; omit for keyword only.")
    ev.set_defaults(func=cmd_eval_search)

    ak = sub.add_parser("ask", help="Answer a question from the library with citations (needs ANTHROPIC_API_KEY).")
    ak.add_argument("question", nargs="+")
    ak.add_argument("-k", type=int, default=8, help="How many passages to give the model.")
    ak.add_argument("--scope", choices=["core", "all"], default="core")
    ak.add_argument("--email", action="store_true", help="Draft a client email instead of an internal answer.")
    ak.add_argument("--notes", help="Adviser notes for the email draft.")
    ak.add_argument("--llm", help="Model name (default: ANSWER_MODEL from .env, else claude-sonnet-5-5).")
    ak.add_argument("--embed-model", help="Embedding model for hybrid search; omit for keyword only.")
    ak.add_argument("--dry-run", action="store_true", help="Show the passages that would be sent, without calling the model.")
    ak.set_defaults(func=cmd_ask)

    sr = sub.add_parser("search", help="Keyword search over in-force text.")
    sr.add_argument("question", nargs="+")
    sr.add_argument("-k", type=int, default=8)
    sr.add_argument("--scope", choices=["core", "all"], default="core")
    sr.add_argument("--superseded", action="store_true", help="Also search superseded versions.")
    sr.add_argument("--model", help="Embedding model used by 'register embed' (enables hybrid search).")
    sr.add_argument("--chars", type=int, default=260)
    sr.set_defaults(func=cmd_search)

    f = sub.add_parser("fingerprint", help="Print the SHA-256, page count and size to paste into versions.csv.")
    f.add_argument("file")
    f.set_defaults(func=cmd_fingerprint)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except RegisterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
