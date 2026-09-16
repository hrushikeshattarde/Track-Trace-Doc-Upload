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

SCHEMA_VERSION = 7

# Backoff for a failed read, by attempt. After the last one the file is left alone and reported as
# a permanent failure: a .MOV or a corrupt part fails identically every time, and retrying it on a
# schedule forever is spend with no chance of a different answer.
READ_RETRY_MINUTES = (15, 60, 360, 1440)

# Failures that will fail again in exactly the same way: a container PyMuPDF cannot open (a .MOV
# from a driver's phone), a truncated or empty part, a re-fetch whose bytes are not this document.
# Anything else - a 429/529, a timeout, an overloaded API - is transient and comes back.
PERMANENT_READ_ERRORS = ("filedataerror", "emptyfileerror", "unsupportedformat", "filenotfounderror",
                         "cannot open", "unsupported", "no pages", "do not match")


def permanent_error_text(text: str | None) -> bool:
    """Whether a recorded read failure is worth another attempt.

    Classified from the stored text rather than the live exception so that one rule covers both the
    read path and a migration over rows written before any of this existed. Wrong in the safe
    direction: an unrecognised error is treated as transient, which costs a few retries, where
    calling a transient failure permanent strands the document for good.
    """
    t = (text or "").lower()
    return any(sig in t for sig in PERMANENT_READ_ERRORS)

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
-- attachment_id is what makes "store no bytes" workable: the service keeps the hash and the
-- reading, and re-fetches the document from Gmail by (message_id, attachment_id) at the moment it
-- files it or a reviewer opens it. Those ids stay valid for the life of the message.
CREATE TABLE IF NOT EXISTS part (
    message_id    TEXT NOT NULL,
    part_id       TEXT NOT NULL,
    attachment_id TEXT,
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
    error                 TEXT,   -- set when the read failed
    -- Retry state for a failed read. A transient failure (a 429/529, a timeout) must come back on
    -- its own; a format the reader can never open must not, or every pass pays to fail again.
    -- An error with next_read_at NULL is the permanent case: visible in `status`, never retried.
    read_attempts         INTEGER NOT NULL DEFAULT 0,
    next_read_at          TEXT
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

-- Everything the service will not file by itself, with the reason. A reviewer's decision is
-- recorded here and nowhere else, so "who approved this filing, and when" has one answer.
-- No bytes: `sha256` plus the part rows are enough to re-fetch the document from Gmail on demand.
CREATE TABLE IF NOT EXISTS review (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    load_id          INTEGER,
    sha256           TEXT,
    message_id       TEXT,
    kind             TEXT,      -- pii | not_a_document | low_confidence | rules_failed | pod_too_early | conflict | error
    reason           TEXT,
    proposed_type    TEXT,      -- the TransportPro document type the service would have used
    proposed_comment TEXT,
    state            TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | filed
    created_at       TEXT,
    decided_at       TEXT,
    decided_by       TEXT,
    decision_note    TEXT,
    UNIQUE (load_id, sha256, kind)
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
    created_at     TEXT,
    action         TEXT,          -- why it is in this state, in words a person can work from
    filed_types    TEXT,
    checks         INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    -- Scope. The Load Management filter decides what gets worked: the 16 ticked pod terminals,
    -- load status Dispatched, service level Priority / OP8. A row can exist for a load outside
    -- that view - Loop A creates one for any load number it sees in a subject line, and that is
    -- deliberate so mail is never discarded - but it is not work. in_view is set by reconcile and
    -- cleared for anything the sweep no longer returns.
    in_view        INTEGER NOT NULL DEFAULT 0,
    view_checked_at TEXT,
    -- Loop A only ever sees mail forward from the cursor. A load that joins the dashboard with
    -- email history behind it is invisible to it: measured 15 Sep 2026, 502 of 527 in-view loads
    -- had no message at all in the ledger, and a spot check found 8 of 12 really did have ratecon
    -- mail (load 2545432: 18 messages, 17 with attachments). One targeted search per load, once,
    -- closes that hole; this column is what makes it once.
    mail_backfilled_at TEXT
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
    if have < 3:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(load)")}
        for name, decl in (("action", "TEXT"), ("filed_types", "TEXT"),
                           ("checks", "INTEGER NOT NULL DEFAULT 0"), ("last_error", "TEXT")):
            if name not in cols:
                conn.execute(f"ALTER TABLE load ADD COLUMN {name} {decl}")
    if have < 4:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(part)")}
        if "attachment_id" not in cols:
            conn.execute("ALTER TABLE part ADD COLUMN attachment_id TEXT")
    if have < 5:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(load)")}
        for name, decl in (("in_view", "INTEGER NOT NULL DEFAULT 0"), ("view_checked_at", "TEXT")):
            if name not in cols:
                conn.execute(f"ALTER TABLE load ADD COLUMN {name} {decl}")
    if have < 6:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(load)")}
        if "mail_backfilled_at" not in cols:
            conn.execute("ALTER TABLE load ADD COLUMN mail_backfilled_at TEXT")
    if have < 7:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(attachment)")}
        for name, decl in (("read_attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_read_at", "TEXT")):
            if name not in cols:
                conn.execute(f"ALTER TABLE attachment ADD COLUMN {name} {decl}")
        # Rows that failed before these columns existed were unreachable: unread_attachments
        # excluded every error and nothing else looked. Give the transient ones their retries back,
        # starting now, and mark the hopeless ones hopeless so read_blocked() reads them correctly
        # rather than paying once more to fail the same way.
        for sha, err in conn.execute("SELECT sha256, error FROM attachment WHERE error IS NOT NULL "
                                     "AND extraction_json IS NULL AND read_attempts = 0").fetchall():
            if permanent_error_text(err):
                conn.execute("UPDATE attachment SET read_attempts=? WHERE sha256=?",
                             (len(READ_RETRY_MINUTES), sha))
            else:
                conn.execute("UPDATE attachment SET next_read_at=?, read_attempts=1 WHERE sha256=?",
                             (now_iso(), sha))
    if have < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# ------------------------------------------------------------------ cursor ----

def get_cursor(conn: sqlite3.Connection, mailbox: str) -> str | None:
    row = conn.execute("SELECT history_id FROM mailbox_cursor WHERE mailbox=?", (mailbox,)).fetchone()
    return row["history_id"] if row else None


def get_cursor_row(conn: sqlite3.Connection, mailbox: str) -> sqlite3.Row | None:
    """The whole cursor row. synced_at is what sizes the window when the cursor has expired: the
    outage is (now - synced_at), and a fixed window smaller than that is silent data loss."""
    return conn.execute("SELECT * FROM mailbox_cursor WHERE mailbox=?", (mailbox,)).fetchone()


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
                dims: tuple[int, int] | None = None, attachment_id: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO part (message_id, part_id, attachment_id, filename, bytes, mime, "
        "width, height, sha256, decision, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (message_id, part_id, attachment_id, filename, size, mime,
         dims[0] if dims else None, dims[1] if dims else None, sha256, decision, now_iso()),
    )


def source_part(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    """Where to re-fetch a document's bytes from. Any occurrence will do - they are the same
    bytes by definition - so take the newest, whose message is least likely to have been deleted."""
    return conn.execute(
        "SELECT p.message_id, p.attachment_id, p.filename, p.mime, p.bytes, m.load_id "
        "FROM part p JOIN message m ON m.message_id = p.message_id "
        "WHERE p.sha256 = ? AND p.attachment_id IS NOT NULL "
        "ORDER BY m.internal_date DESC LIMIT 1", (sha256,)).fetchone()


# ------------------------------------------------------------------ attachments ----

def unread_attachments(conn: sqlite3.Connection, *, load_ids: list[int] | None = None,
                       in_view_only: bool = True, limit: int = 100) -> list[sqlite3.Row]:
    """Documents in the ledger that have never been read.

    A pass run without --read stores the hash, the geometry and the filter decision but no
    extraction, and Loop A never revisits a message it has already processed - so those files would
    stay unread forever. This is what lets reading be turned on after the fact, or pointed at a
    chosen set of loads.

    A file whose last read FAILED is included once its backoff has come round. Excluding every
    error unconditionally meant a timeout or an overloaded API stranded a document as permanently
    as a corrupt one, with the only recovery being the same bytes turning up in a later message.
    The permanent case is still excluded, and it is the one with next_read_at NULL.
    """
    sql = ("SELECT DISTINCT a.sha256, a.filename, a.bytes, m.load_id FROM attachment a "
           "JOIN part p ON p.sha256 = a.sha256 AND p.decision = 'keep' "
           "JOIN message m ON m.message_id = p.message_id "
           "JOIN load l ON l.load_id = m.load_id "
           "WHERE a.extraction_json IS NULL "
           "  AND (a.error IS NULL OR (a.next_read_at IS NOT NULL AND a.next_read_at <= ?))")
    params: list = [now_iso()]
    if in_view_only:
        sql += " AND l.in_view = 1"
    if load_ids:
        sql += " AND m.load_id IN (" + ",".join("?" * len(load_ids)) + ")"
        params += list(load_ids)
    sql += " ORDER BY m.load_id, a.bytes DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_attachment(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM attachment WHERE sha256=?", (sha256,)).fetchone()


def read_blocked(row: sqlite3.Row | None) -> bool:
    """True when this file has failed in a way, or often enough, that nothing will read it again.

    Callers check this before spending: an unread row that is merely waiting out its backoff is not
    blocked, and a row that has already been read is not a failure at all.
    """
    if row is None or row["extraction_json"] is not None:
        return False
    return bool(row["error"]) and row["next_read_at"] is None and (row["read_attempts"] or 0) > 0


def put_attachment(conn: sqlite3.Connection, sha256: str, *, message_id: str, filename: str | None,
                   size: int, extraction: dict | None, document_type: str | None,
                   model: str | None, cost_usd: float | None, error: str | None = None,
                   permanent: bool = False) -> None:
    """Write the reading of one unique file, or the failure to read it.

    The WHERE clause is the first half of the retry story: a successful read overwrites a
    placeholder (seen but not read) or a failed one, and a row that already carries an extraction is
    never overwritten - so a replayed batch can never pay for the same bytes twice.

    The second half is next_read_at. A failure schedules its own retry on the READ_RETRY_MINUTES
    backoff and stops scheduling once the attempts run out or the caller says the failure is
    permanent; a success clears the whole thing. Without this a single transient error left the
    document stranded, because unread_attachments could not see it and Loop A never revisits a
    message it has processed.
    """
    prior = conn.execute("SELECT read_attempts, extraction_json FROM attachment WHERE sha256=?",
                         (sha256,)).fetchone()
    if prior is not None and prior["extraction_json"] is not None:
        return                                  # already read: never overwritten, never paid for twice
    attempts, next_read_at = 0, None
    if error:
        attempts = (prior["read_attempts"] if prior else 0) + 1
        if not permanent and attempts <= len(READ_RETRY_MINUTES):
            next_read_at = (dt.datetime.now(dt.timezone.utc)
                            + dt.timedelta(minutes=READ_RETRY_MINUTES[attempts - 1])).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO attachment (sha256, first_seen_message_id, filename, bytes, extraction_json, "
        "document_type, model, cost_usd, read_at, error, read_attempts, next_read_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(sha256) DO UPDATE SET extraction_json=excluded.extraction_json, "
        "document_type=excluded.document_type, model=excluded.model, cost_usd=excluded.cost_usd, "
        "read_at=excluded.read_at, error=excluded.error, read_attempts=excluded.read_attempts, "
        "next_read_at=excluded.next_read_at WHERE attachment.extraction_json IS NULL",
        (sha256, message_id, filename, size, json.dumps(extraction) if extraction is not None else None,
         document_type, model, cost_usd, now_iso(), error, attempts, next_read_at),
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


def load_doc_evidence(conn: sqlite3.Connection, load_id: int) -> tuple[int, int]:
    """(documents in the mail ledger for this load, how many are not read yet).

    Loop A already recorded every document-bearing message, so the load loop answers "is there
    paperwork in the thread?" from the database instead of a Gmail search per load - which is what
    made readiness.py cost one search for every one of the 539 dashboard loads.
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT p.sha256) AS docs, "
        "       COUNT(DISTINCT CASE WHEN a.extraction_json IS NULL THEN p.sha256 END) AS unread "
        "FROM message m JOIN part p ON p.message_id = m.message_id AND p.decision = 'keep' "
        "LEFT JOIN attachment a ON a.sha256 = p.sha256 WHERE m.load_id = ?", (load_id,)).fetchone()
    return (row["docs"] or 0, row["unread"] or 0)


# Filter decisions that could, in principle, have hidden a real document. rate_confirmation is not
# one of them: it matches TransportPro's own generated <fileId>_<typeId>.pdf naming, never a driver's
# photo. too_small and signature_or_logo are judgement calls made on size and shape alone.
RECOVERABLE_DECISIONS = ("too_small", "signature_or_logo")

# Both of the functions below count DISTINCT files, never part rows. A dropped part has no SHA-256 -
# it was rejected before the download that would have produced one - so its identity is
# (filename, bytes), which is as close to the hash as the part header gets. It matters: measured
# 16 Sep 2026, load 2575905 carried 216 dropped part rows and 7 distinct files, the remainder being
# one signature block re-quoted through every reply. Counting occurrences would have reported that
# load as 216 lost documents.
_DISTINCT_FILE = "COALESCE(p.filename,'') || '/' || COALESCE(p.bytes,0)"


def load_dropped_evidence(conn: sqlite3.Connection, load_id: int) -> int:
    """Distinct attachments on this load's mail that the free filters rejected before any download.

    Those decisions are final - nothing re-examines a processed message - so a BOL photographed at
    low resolution, or cropped wide, is discarded silently. There is no way to tell from the part
    header which of them that might be: measured over this ledger, `image.png` is the commonest
    filename among the files the filters KEPT as well as among the ones they dropped, so any
    filename rule here would be a guess dressed as a measurement. The count is reported and a person
    decides; `intake reconsider` is what acts on it.
    """
    row = conn.execute(
        f"SELECT COUNT(DISTINCT {_DISTINCT_FILE}) FROM part p "
        "JOIN message m ON m.message_id = p.message_id "
        "WHERE m.load_id = ? AND p.decision IN (" + ",".join("?" * len(RECOVERABLE_DECISIONS)) + ")",
        (load_id, *RECOVERABLE_DECISIONS)).fetchone()
    return row[0] or 0


def dropped_parts(conn: sqlite3.Connection, load_id: int, recoverable_only: bool = True) -> list[sqlite3.Row]:
    """One row per distinct dropped file, with what is needed to re-fetch it from Gmail.

    The bare columns beside MAX(m.internal_date) are SQLite's documented guarantee that they come
    from the row that max matched - so each file is re-fetched from its NEWEST message, the one
    least likely to have been deleted, exactly as source_part() does for kept files. This is the
    third dialect-specific statement in the module; Postgres would write it as DISTINCT ON.
    """
    sql = ("SELECT p.message_id, p.part_id, p.attachment_id, p.filename, p.bytes, p.mime, "
           "       p.width, p.height, p.decision, m.load_id, COUNT(*) AS occurrences, "
           "       MAX(m.internal_date) AS newest "
           "FROM part p JOIN message m ON m.message_id = p.message_id "
           "WHERE m.load_id = ? AND p.attachment_id IS NOT NULL AND p.sha256 IS NULL ")
    params: list = [load_id]
    if recoverable_only:
        sql += "AND p.decision IN (" + ",".join("?" * len(RECOVERABLE_DECISIONS)) + ") "
        params += list(RECOVERABLE_DECISIONS)
    else:
        sql += "AND p.decision != 'keep' "
    return conn.execute(sql + f"GROUP BY {_DISTINCT_FILE} ORDER BY p.bytes DESC", params).fetchall()


def update_load(conn: sqlite3.Connection, load_id: int, assessment: dict) -> None:
    conn.execute(
        "UPDATE load SET state=?, stage=?, action=?, doc_status=?, terminal=?, customer=?, "
        "service_level=?, filed_types=?, next_check_at=?, last_checked_at=?, checks=checks+1, "
        "last_error=NULL WHERE load_id=?",
        (assessment["state"], assessment["stage"], assessment["action"], assessment["doc_status"],
         assessment["terminal"], assessment["customer"], assessment["service_level"],
         assessment["filed_types"], assessment["next_check_at"], now_iso(), load_id))


def defer_load(conn: sqlite3.Connection, load_id: int, error: str, minutes: int = 60) -> None:
    """A load that could not be read is pushed out and kept, never dropped."""
    nxt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    conn.execute("UPDATE load SET state='error', last_error=?, next_check_at=?, last_checked_at=?, "
                 "checks=checks+1 WHERE load_id=?", (error[:300], nxt, now_iso(), load_id))


def due_loads(conn: sqlite3.Connection, limit: int = 100, in_view_only: bool = True) -> list[sqlite3.Row]:
    """Oldest due first, and only loads the dashboard filter actually shows.

    A backlog delays; it never drops. Scoping to in_view is what keeps aged loads out: a June load
    whose number happened to appear in a September subject line gets a ledger row (custody), but it
    is not on the Load Management view and is therefore not work.
    """
    sql = "SELECT * FROM load WHERE next_check_at IS NOT NULL AND next_check_at <= ?"
    if in_view_only:
        sql += " AND in_view = 1"
    return conn.execute(sql + " ORDER BY next_check_at LIMIT ?", (now_iso(), limit)).fetchall()


# States where a load is still short the paperwork, so a second look at the mailbox can find
# something. A complete load has nothing to recover and must not be re-searched.
WAITING_STATES = ("pod_expected", "bol_expected", "wrong_doc_type", "filed_status_pending", "new")


def loads_needing_backfill(conn: sqlite3.Connection, limit: int = 200,
                           restale_hours: int | None = None) -> list[int]:
    """In-view loads whose mail history has never been searched. Oldest rows first so a capped pass
    makes steady progress instead of re-taking the same head of the list.

    restale_hours additionally re-takes loads that HAVE been backfilled, are still short paperwork,
    and were last searched longer ago than that. Backfill was written as a once-per-lifetime repair
    and that is right for the steady state - but it is also the only thing in the service that can
    find a document Loop A never saw. Loop A is blind to anything that fell in a cursor gap, and
    Loop B never calls Gmail at all, so without a re-search that document is lost for good.
    Never-searched loads still sort first: a load with no mail history at all is the worse hole.
    """
    sql = "SELECT load_id FROM load WHERE in_view = 1 AND (mail_backfilled_at IS NULL"
    params: list = []
    if restale_hours:
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=restale_hours)).isoformat(timespec="seconds")
        sql += (" OR (mail_backfilled_at < ? AND state IN ("
                + ",".join("?" * len(WAITING_STATES)) + "))")
        params += [cutoff, *WAITING_STATES]
    sql += ") ORDER BY mail_backfilled_at IS NOT NULL, created_at, load_id LIMIT ?"
    params.append(limit)
    return [int(r["load_id"]) for r in conn.execute(sql, params).fetchall()]


def flag_history_gap(conn: sqlite3.Connection) -> int:
    """Loop A lost a window of mail; re-open the per-load backfill for anything it could matter to.

    When the history cursor outlives Gmail's retention, the messages in the gap never come back
    through the cursor - it is re-seeded past them - and no other code path looks backwards. Clearing
    the backfill stamp makes the next backfill pass re-search those loads by subject, which is the
    one query that can still find them. Loads already complete or out of view are left alone.
    """
    cur = conn.execute(
        "UPDATE load SET mail_backfilled_at=NULL WHERE in_view=1 AND mail_backfilled_at IS NOT NULL "
        "AND (state IS NULL OR state IN (" + ",".join("?" * len(WAITING_STATES)) + "))", WAITING_STATES)
    return cur.rowcount


def mark_backfilled(conn: sqlite3.Connection, load_id: int) -> None:
    conn.execute("UPDATE load SET mail_backfilled_at=? WHERE load_id=?", (now_iso(), load_id))


def mark_in_view(conn: sqlite3.Connection, load_id: int) -> None:
    conn.execute("UPDATE load SET in_view=1, view_checked_at=? WHERE load_id=?", (now_iso(), load_id))


def clear_stale_out_of_view(conn: sqlite3.Connection) -> int:
    """A load that has never been in the view but carries a state from an earlier drain reads as
    work when it is not. Make its row say what it is."""
    cur = conn.execute(
        "UPDATE load SET state='not_in_view', next_check_at=NULL "
        "WHERE in_view=0 AND state IS NOT NULL AND state NOT IN ('not_in_view','new')")
    return cur.rowcount


def drop_out_of_view(conn: sqlite3.Connection, since: str) -> int:
    """Anything still flagged in_view that this sweep did not return has left the Load Management
    view - delivered out, cancelled, documents received, or moved off a pod terminal. It keeps its
    row and its history; it just stops being work."""
    cur = conn.execute(
        "UPDATE load SET in_view=0, state='not_in_view', next_check_at=NULL "
        "WHERE in_view=1 AND (view_checked_at IS NULL OR view_checked_at < ?)", (since,))
    return cur.rowcount


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
    read_retrying = q("SELECT COUNT(*) FROM attachment WHERE error IS NOT NULL AND extraction_json IS NULL "
                      "AND next_read_at IS NOT NULL")
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
        "read_failures_retrying": read_retrying,          # waiting out a backoff; they come back
        "read_failures_permanent": read_failures - read_retrying,   # nothing will read these again
        "dropped_recoverable": q(
            f"SELECT COUNT(DISTINCT {_DISTINCT_FILE}) FROM part p "
            "JOIN message m ON m.message_id = p.message_id JOIN load l ON l.load_id = m.load_id "
            "WHERE l.in_view = 1 AND p.decision IN "
            "(" + ",".join("?" * len(RECOVERABLE_DECISIONS)) + ")", *RECOVERABLE_DECISIONS),
        "reads_avoided": max(0, occurrences - unique_files),
        "model_spend_usd": round(q("SELECT COALESCE(SUM(cost_usd),0) FROM attachment"), 4),
        "filings": q("SELECT COUNT(*) FROM filing"),
        "oldest_unresolved": oldest_unres,
        "in_view": q("SELECT COUNT(*) FROM load WHERE in_view=1"),
        "in_view_unbackfilled": q("SELECT COUNT(*) FROM load WHERE in_view=1 AND mail_backfilled_at IS NULL"),
    }


def part_decisions(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT decision, COUNT(*) FROM part GROUP BY decision ORDER BY 2 DESC").fetchall()]
