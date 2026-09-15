"""The review queue: everything the service will not file by itself.

The queue is not an error log. It is the mechanism that lets the service be wrong safely: anything
it is not certain about lands here with the reason and the filing it *would* have made, and a
person decides. In shadow mode every proposal comes here, which is how the rollout measures
agreement before anything is unblocked.

It stores no document bytes. A row carries the SHA-256, and `filing.fetch_bytes()` re-fetches the
document from Gmail when a reviewer opens it - so the queue can hold a photo of a driver's licence
without the service ever having written one to disk.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db

# Why a document is here. Ordered by how much it matters to a person working the list.
PII = "pii"
NOT_A_DOCUMENT = "not_a_document"
RULES_FAILED = "rules_failed"
POD_TOO_EARLY = "pod_too_early"
CONFLICT = "conflict"
LOW_CONFIDENCE = "low_confidence"
SHADOW = "shadow"                      # auto-filing is off; this would have been filed
ERROR = "error"

KIND_ORDER = [PII, NOT_A_DOCUMENT, RULES_FAILED, CONFLICT, POD_TOO_EARLY, LOW_CONFIDENCE, ERROR, SHADOW]

KIND_HELP = {
    PII: "personal ID (licence, passport). Never file; delete from the thread if policy says so.",
    NOT_A_DOCUMENT: "the reader says this is not freight paperwork (signature, logo, screenshot).",
    RULES_FAILED: "the customer's requirements sheet is not satisfied by this document.",
    POD_TOO_EARLY: "would file as a POD before the load is marked Delivered, which leaves the status stuck (OQ-3).",
    CONFLICT: "the thread's load binding and the message's own subject disagree.",
    LOW_CONFIDENCE: "the load was resolved by a weaker signal than the subject line.",
    SHADOW: "auto-filing is off. This is what the service would have filed.",
    ERROR: "the filing attempt failed.",
}


@dataclass
class Item:
    id: int
    load_id: int | None
    sha256: str | None
    kind: str
    reason: str
    proposed_type: str | None
    proposed_comment: str | None
    state: str


def enqueue(conn: sqlite3.Connection, *, load_id: int | None, sha256: str | None, message_id: str | None,
            kind: str, reason: str, proposed_type: str | None = None,
            proposed_comment: str | None = None) -> None:
    """Add a document to the queue. Idempotent on (load, file, kind): re-running the loop must not
    grow the queue, and a reviewer's decision must not be reset by a later pass."""
    conn.execute(
        "INSERT INTO review (load_id, sha256, message_id, kind, reason, proposed_type, proposed_comment, "
        "state, created_at) VALUES (?,?,?,?,?,?,?,'pending',?) "
        "ON CONFLICT(load_id, sha256, kind) DO UPDATE SET reason=excluded.reason, "
        "proposed_type=excluded.proposed_type, proposed_comment=excluded.proposed_comment "
        "WHERE review.state = 'pending'",
        (load_id, sha256, message_id, kind, reason, proposed_type, proposed_comment, db.now_iso()))


def pending(conn: sqlite3.Connection, limit: int = 50, kind: str | None = None) -> list[sqlite3.Row]:
    cases = " ".join(f"WHEN '{k}' THEN {i}" for i, k in enumerate(KIND_ORDER))
    sql = ("SELECT r.*, l.customer, l.stage, l.state AS load_state, a.filename, a.bytes "
           "FROM review r LEFT JOIN load l USING (load_id) LEFT JOIN attachment a USING (sha256) "
           "WHERE r.state='pending'")
    params: list = []
    if kind:
        sql += " AND r.kind=?"
        params.append(kind)
    sql += f" ORDER BY CASE r.kind {cases} ELSE 99 END, r.created_at LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def decide(conn: sqlite3.Connection, item_id: int, *, approve: bool, by: str, note: str = "") -> bool:
    """Record a person's decision. Returns False if the item was already decided - a second
    approval must not silently re-open a closed item."""
    cur = conn.execute(
        "UPDATE review SET state=?, decided_at=?, decided_by=?, decision_note=? "
        "WHERE id=? AND state='pending'",
        ("approved" if approve else "rejected", db.now_iso(), by, note, item_id))
    return cur.rowcount > 0


def mark_filed(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE review SET state='filed' WHERE id=?", (item_id,))


def approved(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Approved but not yet filed - what `intake file --execute` would act on."""
    return conn.execute("SELECT * FROM review WHERE state='approved' ORDER BY decided_at LIMIT ?",
                        (limit,)).fetchall()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT kind, COUNT(*) FROM review WHERE state='pending' GROUP BY kind").fetchall()
    out = {r[0]: r[1] for r in rows}
    out["_pending"] = sum(out.values())
    out["_approved"] = conn.execute("SELECT COUNT(*) FROM review WHERE state='approved'").fetchone()[0]
    out["_filed"] = conn.execute("SELECT COUNT(*) FROM review WHERE state='filed'").fetchone()[0]
    out["_rejected"] = conn.execute("SELECT COUNT(*) FROM review WHERE state='rejected'").fetchone()[0]
    return out
