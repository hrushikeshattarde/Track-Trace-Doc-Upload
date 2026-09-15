"""The ledger.

SQLite, plain SQL, no ORM: one file on disk is enough for one mail worker, and every statement
here is portable to Postgres when the service outgrows a single box (the only dialect-specific
lines are the PRAGMAs and `INSERT OR IGNORE`, both flagged below).

Two design rules carry the whole thing:

1. `part` is one row per *occurrence*, `attachment` is one row per *unique SHA-256*. A photo
   quoted through nine replies makes nine part rows and one attachment row - so the ledger can
   prove the de-duplication worked instead of hoping it did.
2. `attachment.extraction_json` caches what is on the paper, which is a function of the bytes
   alone. The TransportPro document type is NOT cached: it depends on the dispatch stage, the
   delivery appointment and the time the file was emailed, all of which move. Cache the reading,
   recompute the filing.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2

SCHEMA = """
-- Where the Gmail history cursor stands. One row per impersonated mailbox.
CREATE TABLE IF NOT EXISTS mailbox_cursor (
    mailbox       TEXT PRIMARY KEY,
    history_id    TEXT,
    synced_at     TEXT,
    sync_mode     TEXT            -- 'history' | 'full' (after a cursor expiry)
);

-- One row per Gmail thread. The binding to a load is evidence, not truth: a message that
-- carries its own load number always wins (reps reuse old threads for new loads).
CREATE TABLE IF NOT EXISTS thread (
    thread_id        TEXT PRIMARY KEY,
    load_id          INTEGER,
    bound_by         TEXT,        -- routing tier that first bound it
    bound_at         TEXT,
    first_message_at TEXT,
    last_message_at  TEXT,
    message_count    INTEGER NOT NULL DEFAULT 0,
    conflict_flag    INTEGER NOT NULL DEFAULT 0
);

-- Append-only. The primary key is what makes a replayed batch free.
CREATE TABLE IF NOT EXISTS message (
    message_id           TEXT PRIMARY KEY,
    thread_id            TEXT NOT NULL,
    internal_date        TEXT,
    from_domain          TEXT,
    from_internal        INTEGER,
    subject_load_numbers TEXT,    -- comma separated, as found in this message's own subject
    load_id              INTEGER,
    routing_tier         TEXT,    -- subject | thread | paper | unresolved
    part_count           INTEGER NOT NULL DEFAULT 0,
    processed_at         TEXT
);

-- One row per attachment occurrence. `decision` records why a part was or was not read.
CREATE TABLE IF NOT EXISTS part (
    message_id  TEXT NOT NULL,
    part_id     TEXT NOT NULL,
    filename    TEXT,
    bytes       INTEGER,
    mime        TEXT,
    width       INTEGER,
    height      INTEGER,
    sha256      TEXT,             -- null when the part was dropped before download
    decision    TEXT NOT NULL,    -- keep | too_small | rate_confirmation | signature_or_logo | duplicate
    decided_at  TEXT,
    PRIMARY KEY (message_id, part_id)
);

-- One row per unique file. The only table a model call ever writes to.
CREATE TABLE IF NOT EXISTS attachment (
    sha256                TEXT PRIMARY KEY,
    first_seen_message_id TEXT,
    filename              TEXT,
    bytes                 INTEGER,
    extraction_json       TEXT,   -- pod_intake.schema.Extraction, verbatim
    document_type         TEXT,   -- what is on the paper; NOT the TransportPro filing type
    model                 TEXT,
    cost_usd              REAL,
    read_at               TEXT,
    error                 TEXT    -- set when the read failed; the next pass retries this file only
);

-- Idempotent filing. The UNIQUE key is the guard against double-filing, not a code path.
CREATE TABLE IF NOT EXISTS filing (
    load_id       INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    tpro_file_id  TEXT,
    document_type TEXT,
    comment       TEXT,
    filed_at      TEXT,
    PRIMARY KEY (load_id, sha256)
);

-- Mail that could not be routed. Listed, retried, escalated - never dropped.
CREATE TABLE IF NOT EXISTS unresolved (
    message_id    TEXT PRIMARY KEY,
    thread_id     TEXT,
    reason        TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    escalated_at  TEXT,
    created_at    TEXT
);

-- Every load in scope gets a row and keeps it. next_check_at replaces readiness.py's --max:
-- a backlog delays a load, it can never drop one.
CREATE TABLE IF NOT EXISTS load (
    load_id        INTEGER PRIMARY KEY,
    state          TEXT,
    stage          TEXT,
    terminal       INTEGER,
    customer       TEXT,
    doc_status     TEXT,
    service_level  TEXT,
    next_check_at  TEXT,
    last_checked_at TEXT,
    source         TEXT,          -- dashboard | mail (a document can arrive before the sweep sees the load)
    created_at     TEXT
);

CREATE INDEX IF NOT EXISTS ix_message_thread    ON message (thread_id);
CREATE INDEX IF NOT EXISTS ix_message_load      ON message (load_id);
CREATE INDEX IF NOT EXISTS ix_part_sha          ON part (sha256);
CREATE INDEX IF NOT EXISTS ix_load_due          ON load (next_check_at);
CREATE INDEX IF NOT EXISTS ix_unresolved_retry  ON unresolved (next_retry_at);
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the ledger, creating it if needed. WAL so a reader (status) never blocks the writer."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)   # autocommit; we manage transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")          # sqlite-specific
    conn.execute("PRAGMA synchronous=NORMAL")        # sqlite-specific
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = conn.execute("PRAGMA user_version").fetchone()[0]
    if have < 2:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(attachment)")}
        if "error" not in cols:
            conn.execute("ALTER TABLE attachment ADD COLUMN error TEXT")
    if have < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# ------------------------------------------------------------------ cursor ----

def get_cursor(conn: sqlite3.Connection, mailbox: str) -> str | None:
    row = conn.execute("SELECT history_id FROM mailbox_cursor WHERE mailbox=?", (mailbox,)).fetchone()
    return row["history_id"] if row else None


def set_cursor(conn: sqlite3.Connection, mailbox: str, history_id: str, mode: str = "history") -> None:
    """Write the cursor. Callers MUST do this only after the batch has committed: a cursor
    advanced before the work finishes is silent data loss, and the only failure here that
    nothing else catches."""
    conn.execute(
        "INSERT INTO mailbox_cursor (mailbox, history_id, synced_at, sync_mode) VALUES (?,?,?,?) "
        "ON CONFLICT(mailbox) DO UPDATE SET history_id=excluded.history_id, "
        "synced_at=excluded.synced_at, sync_mode=excluded.sync_mode",
        (mailbox, str(history_id), now_iso(), mode),
    )


# ------------------------------------------------------------------ messages ----

def message_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    return conn.execute("SELECT 1 FROM message WHERE message_id=?", (message_id,)).fetchone() is not None


def insert_message(conn: sqlite3.Connection, **f: Any) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO message (message_id, thread_id, internal_date, from_domain, from_internal, "
        "subject_load_numbers, load_id, routing_tier, part_count, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f["message_id"], f["thread_id"], f.get("internal_date"), f.get("from_domain"),
         int(bool(f.get("from_internal"))), f.get("subject_load_numbers"), f.get("load_id"),
         f.get("routing_tier"), f.get("part_count", 0), now_iso()),
    )


# ------------------------------------------------------------------ threads ----

def get_thread(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM thread WHERE thread_id=?", (thread_id,)).fetchone()


def touch_thread(conn: sqlite3.Connection, thread_id: str, internal_date: str | None) -> None:
    conn.execute(
        "INSERT INTO thread (thread_id, first_message_at, last_message_at, message_count) VALUES (?,?,?,1) "
        "ON CONFLICT(thread_id) DO UPDATE SET "
        "  last_message_at = MAX(COALESCE(thread.last_message_at,''), COALESCE(excluded.last_message_at,'')), "
        "  first_message_at = MIN(NULLIF(COALESCE(thread.first_message_at, excluded.first_message_at),''), "
        "                         COALESCE(excluded.first_message_at, thread.first_message_at)), "
        "  message_count = thread.message_count + 1",
        (thread_id, internal_date, internal_date),
    )


def bind_thread(conn: sqlite3.Connection, thread_id: str, load_id: int, tier: str) -> None:
    conn.execute("UPDATE thread SET load_id=?, bound_by=?, bound_at=? WHERE thread_id=? AND load_id IS NULL",
                 (load_id, tier, now_iso(), thread_id))


def flag_thread_conflict(conn: sqlite3.Connection, thread_id: str) -> None:
    conn.execute("UPDATE thread SET conflict_flag=1 WHERE thread_id=?", (thread_id,))


# ------------------------------------------------------------------ parts ----

def record_part(conn: sqlite3.Connection, message_id: str, part_id: str, *, filename: str | None,
                size: int | None, mime: str | None, decision: str, sha256: str | None = None,
                dims: tuple[int, int] | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO part (message_id, part_id, filename, bytes, mime, width, height, "
        "sha256, decision, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (message_id, part_id, filename, size, mime, dims[0] if dims else None, dims[1] if dims else None,
         sha256, decision, now_iso()),
    )


# ------------------------------------------------------------------ attachments ----

def get_attachment(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM attachment WHERE sha256=?", (sha256,)).fetchone()


def put_attachment(conn: sqlite3.Connection, sha256: str, *, message_id: str, filename: str | None,
                   size: int, extraction: dict | None, document_type: str | None,
                   model: str | None, cost_usd: float | None, error: str | None = None) -> None:
    """Write the reading of one unique file.

    The WHERE clause is the whole retry story: a successful read overwrites a placeholder (seen but
    not read) or a failed one, and a row that already carries an extraction is never overwritten -
    so a replayed batch can never pay for the same bytes twice.
    """
    conn.execute(
        "INSERT INTO attachment (sha256, first_seen_message_id, filename, bytes, extraction_json, "
        "document_type, model, cost_usd, read_at, error) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(sha256) DO UPDATE SET extraction_json=excluded.extraction_json, "
        "document_type=excluded.document_type, model=excluded.model, cost_usd=excluded.cost_usd, "
        "read_at=excluded.read_at, error=excluded.error WHERE attachment.extraction_json IS NULL",
        (sha256, message_id, filename, size, json.dumps(extraction) if extraction is not None else None,
         document_type, model, cost_usd, now_iso(), error),
    )


# ------------------------------------------------------------------ loads ----

def upsert_load(conn: sqlite3.Connection, load_id: int, *, source: str, due_now: bool = False, **f: Any) -> None:
    """Create the row if the load is new, then optionally pull its next check forward. A load can
    enter the ledger from the mail side before any dashboard sweep has produced it - never discard
    mail because the load is unknown."""
    conn.execute(
        "INSERT OR IGNORE INTO load (load_id, state, source, created_at, next_check_at) VALUES (?,?,?,?,?)",
        (load_id, f.get("state", "new"), source, now_iso(), now_iso()),
    )
    if due_now:
        conn.execute("UPDATE load SET next_check_at=? WHERE load_id=?", (now_iso(), load_id))


def due_loads(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Oldest due first. A backlog delays; it never drops. In Postgres this gets
    FOR UPDATE SKIP LOCKED so the load loop can scale out."""
    return conn.execute(
        "SELECT * FROM load WHERE next_check_at IS NOT NULL AND next_check_at <= ? "
        "ORDER BY next_check_at LIMIT ?", (now_iso(), limit)).fetchall()


# ------------------------------------------------------------------ unresolved ----

def add_unresolved(conn: sqlite3.Connection, message_id: str, thread_id: str, reason: str,
                   retry_in_minutes: int = 60) -> None:
    nxt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=retry_in_minutes)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO unresolved (message_id, thread_id, reason, attempts, next_retry_at, created_at) "
        "VALUES (?,?,?,1,?,?) ON CONFLICT(message_id) DO UPDATE SET "
        "attempts = unresolved.attempts + 1, next_retry_at = excluded.next_retry_at, reason = excluded.reason",
        (message_id, thread_id, reason, nxt, now_iso()),
    )


def clear_unresolved(conn: sqlite3.Connection, message_id: str) -> None:
    conn.execute("DELETE FROM unresolved WHERE message_id=?", (message_id,))


# ------------------------------------------------------------------ health ----

def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """The four numbers from the plan, plus what they are computed from.

    custody must be 0: every message seen is either bound to a load or on the unresolved list.
    dedup_saved is how many model calls the attachment table avoided.
    """
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    seen = q("SELECT COUNT(*) FROM message")
    bound = q("SELECT COUNT(*) FROM message WHERE load_id IS NOT NULL")
    unres = q("SELECT COUNT(*) FROM unresolved")
    occurrences = q("SELECT COUNT(*) FROM part WHERE decision='keep'")
    unique_files = q("SELECT COUNT(*) FROM attachment")
    files_read = q("SELECT COUNT(*) FROM attachment WHERE extraction_json IS NOT NULL")
    read_failures = q("SELECT COUNT(*) FROM attachment WHERE error IS NOT NULL AND extraction_json IS NULL")
    oldest_unres = q("SELECT MIN(created_at) FROM unresolved") or None
    overdue = q("SELECT MIN(next_check_at) FROM load WHERE next_check_at <= ?", now_iso()) or None
    return {
        "messages_seen": seen,
        "messages_bound": bound,
        "messages_unresolved": unres,
        "custody_gap": seen - bound - unres,           # must be 0
        "threads": q("SELECT COUNT(*) FROM thread"),
        "threads_conflicted": q("SELECT COUNT(*) FROM thread WHERE conflict_flag=1"),
        "loads": q("SELECT COUNT(*) FROM load"),
        "loads_overdue": q("SELECT COUNT(*) FROM load WHERE next_check_at <= ?", now_iso()),
        "oldest_overdue_check": overdue,
        "attachment_occurrences": occurrences,
        "unique_files": unique_files,
        "files_read": files_read,
        "files_unread": unique_files - files_read - read_failures,
        "read_failures": read_failures,
        "reads_avoided": max(0, occurrences - unique_files),
        "model_spend_usd": round(q("SELECT COALESCE(SUM(cost_usd),0) FROM attachment"), 4),
        "filings": q("SELECT COUNT(*) FROM filing"),
        "oldest_unresolved": oldest_unres,
    }


def part_decisions(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT decision, COUNT(*) FROM part GROUP BY decision ORDER BY 2 DESC").fetchall()]
