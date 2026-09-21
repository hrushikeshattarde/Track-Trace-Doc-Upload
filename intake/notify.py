"""What the team is told, and why.

The service already decides well. What it has never done is SAY anything: a document is filed, or
refused, and the only way to find out is to open the review queue and read it. A pod lead working
fifteen loads does not do that, so the work the service does is invisible to the people whose loads
it is doing it on.

This module is the record of what should be said. Three deliberate choices:

1. Recording and delivering are separate. Every filing and every refusal writes a row here whether
   or not anything is ever sent, so the account of "what did the bot do on my loads today" exists
   from the first run, and turning delivery on later loses nothing that already happened.

2. A refusal carries its reason in the words a person can act on, not a kind name. "not_needed"
   tells a rep nothing; "the load is short a POD and this reads as a BOL" tells them to stop
   looking. The reason is written once, here, and every channel reuses it.

3. Nothing is sent twice. delivered_at is stamped only when a channel confirms it went, so a failed
   send is retried and a successful one is never repeated - the same discipline the filing table
   uses to guarantee a document is filed once.

Delivery itself is not in this module. Where these go - a note on the load in TransportPro, an email
digest, a file somebody opens - is a decision with consequences outside the code, and each channel
is a separate write path that has to be turned on deliberately.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from . import db, review, state as st

FILED = "filed"
REFUSED = "refused"

# Which refusals are worth somebody's attention. A notification channel earns its keep by being
# worth reading, and the fastest way to make one worthless is to send everything: run against the
# live queue on 21 Sep 2026, "tell them about every refusal" produced 233 notices, and the
# overwhelming majority were "not filed, the load is already complete" - the service working exactly
# as intended, reported as though it were news. A pod lead who reads that twice stops reading.
#
# The test is not "is this interesting" but "is there something for a person to DO". Everything left
# out is still in the review queue, which is the complete record; this is the subset that should
# reach somebody who is not looking.
NOTIFIABLE = frozenset({
    review.PII,            # a licence is sitting in a mail thread; somebody may have to remove it
    review.RULES_FAILED,   # the customer's requirements are not met - the driver may need a redo
    review.WRONG_TYPE,     # paperwork is on the load under a type that will not clear it
    review.REFILE,         # filed, delivered, still not cleared
    review.CONFLICT,       # the thread and the subject disagree about which load this is
    review.ERROR,          # the upload itself failed
})

# What a refusal means, in a sentence a rep can act on. The queue's KIND_HELP explains the kind to
# somebody working the queue; this explains the consequence to somebody working the load.
WHY_REFUSED = {
    review.PII: "it is a personal identity document (a licence or passport). The bot will never file one",
    review.NOT_A_DOCUMENT: "the page is not freight paperwork - a logo, a signature graphic or a screenshot",
    review.RULES_FAILED: "it does not meet the customer's documented requirements",
    review.NOT_NEEDED: "this load is not short this document, so filing it would add a duplicate",
    review.NOT_ASSESSED: "the load has not been checked in TransportPro yet, so nothing knows what it needs",
    review.WRONG_TYPE: "the load's paperwork is filed under a type that does not clear the status",
    review.REFILE: "it is already filed and the status has not cleared; re-filing is a person's call",
    review.CONFLICT: "a reply in the thread named a different load, so which load it belongs to is contested",
    review.LOW_CONFIDENCE: "the load was matched by something weaker than the email's own subject line",
    review.POD_TOO_EARLY: "it would be filed as a POD before the truck reached the consignee",
    review.SHADOW: "auto-filing is switched off, so every filing waits for a person",
    review.ERROR: "the upload failed",
}


def worth_telling(kind: str | None, document_type: str | None) -> bool:
    """Whether this refusal is news, as opposed to merely true.

    NOTIFIABLE says which kinds matter. This adds the one case where the kind is right and the
    document is not: a load stuck behind a Driver Supplied BOL produces one wrong_doc_type refusal
    for EVERY document in its thread, and most of them cannot fix it. Load 2580959 generated three
    notices this way - two about freight photos - and a photo cannot be re-filed into a type that
    clears the status. Only the document that could actually repair the load is worth waking
    somebody for; the rest are in the queue if anyone wants them.
    """
    if kind not in NOTIFIABLE:
        return False
    if kind == review.WRONG_TYPE:
        return document_type in st.CLEARING_TYPE_NAMES
    return True


def headline(event: str, load_id: int, document_type: str | None, filename: str | None) -> str:
    """One line, the way it would be read aloud."""
    what = document_type or "a document"
    name = f" ({filename})" if filename else ""
    if event == FILED:
        return f"Load {load_id}: filed {what}{name}"
    return f"Load {load_id}: did NOT file {what}{name}"


def record(conn: sqlite3.Connection, *, load_id: int, event: str, sha256: str | None = None,
           kind: str | None = None, document_type: str | None = None, filename: str | None = None,
           reason: str | None = None) -> None:
    """Note that something happened on a load. Idempotent on (load, document, event, kind).

    Called from the filing path, so it records what the service DID - never what it merely
    considered. A proposal that sits in the queue is not news; a refusal a person needs to act on is.
    """
    # Neither may be NULL. SQLite counts NULLs as DISTINCT in a UNIQUE constraint, so a filing -
    # which has no kind - would slip past (load_id, sha256, event, kind) every single time and the
    # team would be told the same thing on every pass. The review table carries the same warning in
    # its own docstring, and this walked straight into it: caught by the test, not by reading.
    kind = kind or event
    sha256 = sha256 or ""
    row = conn.execute("SELECT terminal, customer FROM load WHERE load_id=?", (load_id,)).fetchone()
    detail = reason or WHY_REFUSED.get(kind, "")
    if event == REFUSED and kind in WHY_REFUSED:
        # The kind's plain meaning first, then whatever specific reason the proposal carried.
        detail = WHY_REFUSED[kind] + (f". {reason}" if reason else "")
    conn.execute(
        "INSERT INTO notification (load_id, sha256, event, kind, headline, detail, document_type, "
        "filename, terminal, customer, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(load_id, sha256, event, kind) DO UPDATE SET headline=excluded.headline, "
        "detail=excluded.detail, document_type=excluded.document_type "
        "WHERE notification.delivered_at IS NULL",
        (load_id, sha256, event, kind, headline(event, load_id, document_type, filename), detail,
         document_type, filename, row["terminal"] if row else None,
         row["customer"] if row else None, db.now_iso()))


def pending(conn: sqlite3.Connection, limit: int = 500) -> list[sqlite3.Row]:
    """Everything recorded and not yet delivered, oldest first."""
    return conn.execute(
        "SELECT * FROM notification WHERE delivered_at IS NULL ORDER BY created_at, load_id LIMIT ?",
        (limit,)).fetchall()


def mark_delivered(conn: sqlite3.Connection, ids: list[int], channel: str) -> int:
    """Stamp only what a channel confirmed. A send that half-worked leaves the rest pending."""
    if not ids:
        return 0
    cur = conn.execute(
        "UPDATE notification SET delivered_at=?, channel=? WHERE delivered_at IS NULL AND id IN ("
        + ",".join("?" * len(ids)) + ")", (db.now_iso(), channel, *ids))
    return cur.rowcount


def digest(rows: list[sqlite3.Row], *, group: str = "terminal") -> list[tuple[str, list[str], list[int]]]:
    """Group the pending notifications into one message per team.

    Returns (who, lines, ids). `ids` is what mark_delivered is given once that message actually
    goes, which is why the grouping and the sending stay separate: a digest that is built but not
    sent must leave the ledger untouched.
    """
    buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = str(r[group] if r[group] is not None else "unassigned")
        buckets[key].append(r)

    out = []
    for who, items in sorted(buckets.items()):
        filed = [r for r in items if r["event"] == FILED]
        refused = [r for r in items if r["event"] == REFUSED]
        lines = [f"{len(filed)} document(s) filed, {len(refused)} not filed."]
        if filed:
            lines.append("")
            lines.append("Filed:")
            for r in filed:
                lines.append(f"  load {r['load_id']}  {r['document_type']}"
                             + (f"  ({r['customer']})" if r["customer"] else ""))
        if refused:
            lines.append("")
            lines.append("Not filed, and why:")
            for r in refused:
                lines.append(f"  load {r['load_id']}  {r['document_type'] or 'a document'}"
                             + (f"  ({r['customer']})" if r["customer"] else ""))
                lines.append(f"      {r['detail']}")
        out.append((who, lines, [int(r["id"]) for r in items]))
    return out


def render(who: str, lines: list[str], group: str = "terminal") -> str:
    label = {"terminal": "terminal", "customer": "customer"}.get(group, group)
    head = f"Driver document intake — {label} {who}"
    return head + "\n" + "-" * len(head) + "\n" + "\n".join(lines)
