# Compliance-chatbot

A retrieval-augmented assistant for Singapore fund management company compliance. Answers questions from a versioned register of MAS and SFA source documents, with paragraph-level citations, and drafts client emails from the cited answers.

> **Not legal advice.** Outputs are for information only and need professional review. Do not enter client-identifying or confidential information.

## Status

- **Step 1 – document register** (done): which source documents the tool relies on, which edition is in force, what superseded what, when each is next due for review.
- **Step 2 – simple version** (done): extract and chunk each PDF by provision/paragraph, keyword + optional vector search over in-force text only, a retrieval test set, and `ask` for cited answers and client-email drafts.
- **Later:** a layer that understands multi-part scenarios, asks clarifying questions, splits them into issues and synthesises one answer; a friendlier interface; hosting for testers.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

register build      # create/refresh the local SQLite database from the CSV register
register check      # verify the register against the PDFs on disk
register list       # all documents and versions
register overdue    # in-force documents due for review (add --all for the full schedule)
pytest              # run the tests
```

Search and answering need **poppler** (`pdftotext`) to read the PDFs: `brew install poppler`, `apt install poppler-utils`, or the Windows build.

```bash
register ingest                       # extract + chunk + index every registered PDF -> data/index/index.sqlite
register search "misconduct report deadline"           # keyword search, in-force text, cited
register eval-search                  # score retrieval on data/eval/retrieval.csv (recall@k, MRR)

# Better search with a local open-source embedding model (one-off ~130 MB download):
pip install -e ".[embed]"
register embed --model BAAI/bge-small-en-v1.5
register eval-search --model BAAI/bge-small-en-v1.5     # compare with the keyword-only score above

# Answers need a hosted model: pip install -e ".[llm]", put ANTHROPIC_API_KEY in .env (see .env.example)
register ask "Within how many days must a misconduct report be filed?" --embed-model BAAI/bge-small-en-v1.5
register ask --email --notes "client is a Cayman fund manager" "How do we report misconduct?"
register ask --dry-run "..."          # show the passages that would be sent, without calling the model
```

The search index is a **separate, disposable file** (`data/index/`, not committed). It is rebuilt from the register and PDFs by `register ingest`; re-run `register embed` afterwards.

## How it is organised

| Path | What it holds |
|---|---|
| `data/register/documents.csv` | One row per instrument (e.g. the SFA). **Source of truth.** |
| `data/register/versions.csv` | One row per edition: dates, status, supersession, fingerprint. **Source of truth.** |
| `data/register/review_log.csv` | History of review checks. **Source of truth.** |
| `data/sources/<category>/` | The PDFs, named `<slug>__<version>.pdf`. |
| `src/compliance_assistant/` | Schema (`models.py`), loader (`register.py`), checks (`checks.py`), CLI (`cli.py`). |
| `migrations/` | Alembic schema migrations. |
| `data/db/` | Local SQLite database. Git-ignored: rebuilt from the CSVs at any time. |

The CSVs are plain text so every change appears as a readable line in Git history. The SQLite database is derived from them.

## Keeping the register current

When a document is replaced by a newer edition:

1. Add the new PDF under `data/sources/<category>/` as `<slug>__<new-version>.pdf`.
2. In `versions.csv`, add a row for it (status `in_force`) with its dates and fingerprint.
3. Change the old row's status to `superseded` and set `superseded_by` to `<slug>@<new-version>`. **Keep the old file**: it is needed to answer "what did the rule say at the time?".
4. Run `register check`, then commit.

To get the SHA-256, page count and size to paste into the row: `register fingerprint <file>`.

When you have checked a document against its source:

```bash
register review sfa-2001 as-at-2026-10-23 --outcome confirmed_current --reviewer "Your name"
```

This stamps `last_reviewed_date` and appends to the review log. Commit the CSV changes. The default review interval is 30 days for statutes and regulations and 90 days for notices and guidelines, set per document in `documents.csv`.

## What `register check` verifies

- Every registered file exists and matches its SHA-256 fingerprint.
- No PDF sits in `data/sources` without a register entry.
- Only one in-force edition per document (also enforced by the database).
- Superseded editions point to a replacement; in-force statutes carry an "as at" date; nothing in force has an expired `effective_to`.

Errors fail the check. Warnings flag things to look at. `-v` also lists missing source URLs.

## Design notes

- **Portable on purpose.** Schema uses standard types and `CHECK` constraints, all changes go through Alembic, and the database location comes from `DATABASE_URL`. Moving from SQLite to PostgreSQL later is a configuration change plus a reload from the CSVs.
- **Retire, do not delete.** Superseded editions stay in the register so retrospective questions can be answered.
- **Dates.** Real `DATE` columns. Where a document only states a month, the first of the month is stored with `issue_date_precision = month`.
- **Workspaces.** Documents carry a `workspace_id` (default `default`) so the tool can serve more than one user or firm later without a schema change.

- **Status-aware retrieval.** Searches return in-force text only by default; `--superseded` includes retired editions, and `core` scope excludes reference material such as the Companies Act unless `--scope all`.
- **Every answer cites numbered sources**, and the tool flags citations that do not match a retrieved passage. It does not yet check that a cited passage actually supports the sentence.

## Known gaps in the register

- Source URLs are not yet filled in.
- The FMC Licensing Guidelines (SFA 04-G05) refer to MAS Notice SFA04-N13 (risk-based capital), which is not in the library.
- Not yet included: the SF(ECBA)(FRC) and (FO) Regulations 2021, Notice SFA04-N18, and licence conditions.
- The Financial Services and Markets Act 2022 file is the as-enacted text; its currency against later amendments has not been verified.
- Statutes with older "as at" dates (Companies Act, Financial and Margin Requirements Regulations: 26 Jan 2026) will need refreshing.
