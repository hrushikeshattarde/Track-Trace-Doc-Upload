"""Deciding what to file, and filing it.

Two halves that must stay separate.

`propose()` is pure judgement and touches nothing: it takes the cached reading of a document and
the load's *current* state, and returns the TransportPro document type, the File History comment,
and a gate - auto, review or block. It recomputes the type every time rather than reading a cached
one, because classify_type depends on the dispatch stage, the delivery appointment and the time the
file was emailed, all of which move after the document was read.

`execute()` is the only code in this service that writes to TransportPro. It is off unless asked
twice: `--execute` on the command line, and a proposal that is either gated `auto` or approved by a
person in the review queue. Everything else is a dry run that prints what it would have done.

The gates, in the order they are checked:

  block   personal ID, or the reader says this is not freight paperwork. Never filed, and the
          bytes are never written to disk.
  hold    reserved. It used to withhold PODs until the Delivered mark on the OQ-3 timing theory,
          which the 15 Sep 2026 measurement refuted; classify_type already refuses to call anything
          a POD before the truck reaches the consignee.
  review  the customer's requirements are not met, the thread binding disagreed with the subject,
          or the load was resolved by something weaker than its own subject line.
  auto    high-confidence, rules satisfied, correctly timed. Filed only when auto-filing is on.
"""
from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, review, state as st

# The reader is told to return "other"/"unknown" for anything that is not freight paperwork, and to
# say what it is in notes. These are the words that mean a person's identity document.
PII_RE = re.compile(r"licen[cs]e|passport|social security|\bid card\b|identity card|driver'?s? lic", re.I)

BLOCK = "block"
HOLD = "hold"
REVIEW = "review"
AUTO = "auto"

# House policy: the TransportPro type a document is UPLOADED under, when that is deliberately not
# the type it reads as. Set 18 Sep 2026 at Circle's request: anything the service reads as a bill of
# lading is filed as Driver Supplied BOL, so a document the bot found is visibly a document the bot
# found and a person decides when it counts for billing.
#
# Be clear-eyed about the cost, because this project measured it. Type 363 does NOT clear "Waiting
# for Documents": over 266 loads on 15 Sep 2026, 94 of the 96 loads that cleared carried a type 12,
# and 145 of the 170 still waiting had only a 363. So every BOL filed under this policy leaves its
# load unbillable until somebody re-files it, and the service will go on reporting those loads as
# wrong_doc_type - correctly, because that is what they are. The policy buys human control over
# billing status and pays for it in loads that stay open.
#
# Emptying this dict restores filing under the type the document actually is. Nothing else needs to
# change: the mapping is applied at the upload boundary only.
FILE_AS: dict[str, str] = {"Bill Of Lading": "Driver Supplied BOL"}


def file_as(document_type: str | None) -> str | None:
    """The type to upload under. The document's real type everywhere else."""
    return FILE_AS.get(document_type or "", document_type)


@dataclass
class Proposal:
    load_id: int
    sha256: str
    gate: str
    kind: str | None            # review.KIND_* when the gate is not auto
    reason: str
    document_type: str | None   # what the document IS - what every decision here is made on
    comment: str | None
    filename: str | None = None
    notes: list[str] = field(default_factory=list)
    # What it will actually be UPLOADED as. Differs from document_type only where FILE_AS says so,
    # and it is kept separate on purpose: every judgement in propose() - does the load need this,
    # do the customer's rules pass - has to be made on what the document is, never on the label
    # policy puts on it. Conflating the two would make a BOL fail its own load's "needs a BOL" test.
    # LAST on purpose too: every existing call site passes filename and notes positionally, and
    # slotting a new field in front of notes sent the notes list into the upload's documentType.
    upload_as: str | None = None

    def line(self) -> str:
        shown = self.document_type or "-"
        if self.upload_as and self.upload_as != self.document_type:
            shown = f"{shown} as {self.upload_as}"
        return (f"load {self.load_id}  {self.sha256[:12]}  {self.gate:6} "
                f"{shown:42} {self.reason[:78]}")


def propose(conn: sqlite3.Connection, load_id: int, sha256: str, *, requirements_path: str | Path | None = None,
            allow_auto: bool = False) -> Proposal:
    """What should happen to this document on this load, judged against the load's state right now."""
    att = db.get_attachment(conn, sha256)
    if att is None or att["extraction_json"] is None:
        return Proposal(load_id, sha256, REVIEW, review.LOW_CONFIDENCE,
                        "not read yet: no extraction on file", None, None)

    from pod_intake.matcher import (brief_page_comment, classify_type, filing_comment,
                                     page_summary)
    from pod_intake.schema import Extraction

    ex = Extraction.model_validate(json.loads(att["extraction_json"]))
    load_row = conn.execute("SELECT * FROM load WHERE load_id=?", (load_id,)).fetchone()
    stage = (load_row["stage"] if load_row else "") or ""
    filename = att["filename"]

    # 1. Never file a person's identity document, whatever else is true.
    blob = f"{ex.document_type} {getattr(ex, 'notes', '') or ''} {filename or ''}"
    if PII_RE.search(blob):
        return Proposal(load_id, sha256, BLOCK, review.PII,
                        f"personal ID on the page ({(getattr(ex, 'notes', '') or '')[:80]}): never file",
                        None, None, filename)

    # 2. Not freight paperwork at all: a signature graphic, a logo, a screenshot of a chat.
    if ex.document_type in ("other", "unknown"):
        return Proposal(load_id, sha256, BLOCK, review.NOT_A_DOCUMENT,
                        f"reader classified {ex.document_type}: {(getattr(ex, 'notes', '') or '')[:90]}",
                        None, None, filename)

    # 3. The type, recomputed from the stage the truck is at NOW - never a cached one.
    doc_type, why = classify_type(ex, {"dispatch_status": stage.title()})
    notes = [why]

    # 4. The customer's requirements, when the workbook rules have been built.
    kind: str | None = None
    reason = why
    gate = AUTO
    verdict_summary = ""
    rules = _rules_for(conn, load_row, requirements_path)
    if rules is not None:
        from pod_intake.requirements import check_document
        verdict = check_document(ex, doc_type, rules, {"dispatch_status": stage.title()}, set())
        verdict_summary = verdict.summary
        notes.append(verdict_summary)
        # verdict.failed, not a regex over verdict.summary. Reading the prose meant any rule whose
        # DETAIL happened to contain "fail" or "missing" gated the document, whatever its status -
        # so an informational note could condemn a perfectly good BOL, and a genuine failure worded
        # without those words would have slipped through. The structured list is what the checker
        # actually decided.
        if verdict.failed:
            gate, kind = REVIEW, review.RULES_FAILED
            reason = ("customer requirements not met: "
                      + "; ".join(r.detail for r in verdict.failed))[:180]

    # 5. Nothing to hold for timing. This used to withhold any POD until the Delivered mark, on the
    #    OQ-3 theory that an earlier upload leaves the status stuck. Measured over 266 loads on
    #    15 Sep 2026 that is false: 58% of cleared loads had a filing after the mark against 69% of
    #    stuck ones. What decides it is the document TYPE (state.CLEARING_TYPES), and classify_type
    #    already refuses to call anything a POD before the truck reaches the consignee.

    # 5b. The comment, built here rather than at the end, because every branch below returns a
    #     proposal and a reviewer who approves one needs to know what will be written to File
    #     History. Passing None meant a re-file approved out of the queue - the wrong_doc_type
    #     repair, the whole point of the queue - was uploaded with an EMPTY comment, and
    #     TransportPro renames every upload to <fileId>_<typeId>, so nothing at all would have
    #     survived to say what the document was or where it came from.
    routing, conflicted = _routing_of(conn, load_id, sha256)
    tpro_src = db.tpro_file_for_sha(conn, sha256)
    channel = "email" if db.source_part(conn, sha256) is not None else "the load"
    was = (f"{tpro_src['file_type_name']} {tpro_src['tpro_file_id']}"
           if tpro_src is not None and tpro_src["file_type_name"] != doc_type else "")
    match_detail = _match_detail(conn, load_id, sha256, routing)
    page = page_summary(ex)

    # The long audited line no longer goes to TransportPro, but it is not thrown away: it names the
    # channel, the routing evidence, the type this was previously filed under and the customer
    # verdict, which is exactly what a reviewer needs and far too much for a column a person scans.
    # It lives in the proposal's notes, where the queue and the CSV export both show it.
    notes.append(filing_comment(doc_type, load_id, channel, _now(), "proposed",
                                match=match_detail, page=page, was=was))

    def mk_comment(decision: str) -> str:
        """The File History comment: what the page is, in under ten words.

        Short on purpose. It is the only thing on that row a billing or imaging specialist will
        actually read, and under FILE_AS it is the only thing saying whether the page is a signed
        POD or an unsigned BOL - because the type column will say Driver Supplied BOL either way.
        `decision` is accepted so the signature matches every caller, but a gate label is not
        something a specialist reading File History needs.

        The load number is on the end because every comment already in Circle's File History carries
        one ("Rate and Dispatch Confirmation for load - 2547060"), and a row that breaks the house
        convention reads as a row somebody got wrong. It costs three words, so the description gets
        seven and the whole comment stays inside ten.
        """
        return f"{brief_page_comment(ex, max_words=7)} - load {load_id}"

    # 6. Does this load actually need this document? Passing every safety gate is not the same as
    #    being work. Without this the auto gate fires on documents for loads that already show
    #    Documents Received, loads outside the worked service level, and BOLs for loads that are
    #    short a POD - 140 of the 153 that cleared the gates on the 15 Sep 2026 run.
    load_state = (load_row["state"] if load_row else "") or ""
    needed = {"pod_expected": "Proof of Delivery", "bol_expected": "Bill Of Lading"}.get(load_state)
    #    A load drain has not reached yet answers none of that. "new" is what upsert_load inserts and
    #    what every load created from the mail side carries until Loop B checks it, and "error" is a
    #    load TransportPro could not be read for; both mean the same thing here - nothing is KNOWN
    #    about what this load is short. Before this branch they matched no case above and `needed`
    #    was None, so they fell through every test in this step and reached the auto gate: the one
    #    state the needs-check does not cover was the commonest one in the ledger (408 of the in-view
    #    loads on 17 Sep 2026). Not knowing is a reason to ask a person, never a reason to file.
    #    Only downgrade a proposal that is otherwise clean - a failed requirements check above is a
    #    more specific finding and keeps the queue entry it earned.
    if load_state in ("", "new", "error") and gate == AUTO:
        return Proposal(load_id, sha256, REVIEW, review.NOT_ASSESSED,
                        f"the load has not been checked against TransportPro yet"
                        + (f" (state {load_state})" if load_state else " and has no ledger row")
                        + f", so whether it is short a {doc_type} is unknown. Run "
                        f"'python -m intake loads' and judge again",
                        doc_type, mk_comment("review"), filename, notes, file_as(doc_type))
    if load_state in ("complete", "out_of_scope", "not_yet_due", "not_in_view"):
        return Proposal(load_id, sha256, REVIEW, review.NOT_NEEDED,
                        f"load is {load_state}: it is not short a document, so filing this adds a "
                        f"duplicate rather than clearing anything",
                        doc_type, mk_comment("review"), filename, notes, file_as(doc_type))
    if load_state == "wrong_doc_type":
        return Proposal(load_id, sha256, REVIEW, review.WRONG_TYPE,
                        "the load's only paperwork is filed under a type that does not clear "
                        "Waiting for Documents (usually Driver Supplied BOL); filing this under a "
                        f"proper {doc_type} is what clears it",
                        doc_type, mk_comment("review"), filename, notes, file_as(doc_type))
    if load_state == "filed_status_pending":
        # Only worth re-filing once the load is actually Delivered. Before that there is no
        # Delivered mark to be after, so "Waiting for Documents" is simply what an in-transit load
        # with a BOL on it looks like - not a fault, and nothing to act on.
        if stage != "delivered":
            return Proposal(load_id, sha256, REVIEW, review.NOT_NEEDED,
                            f"already filed and the truck is {stage or 'still in transit'}: "
                            f"Waiting for Documents is expected until it delivers, so there is "
                            f"nothing to re-file yet",
                            doc_type, mk_comment("review"), filename, notes, file_as(doc_type))
        return Proposal(load_id, sha256, REVIEW, review.REFILE,
                        "filed, Delivered, and documentStatus is still Waiting: re-filing after the "
                        "Delivered mark is the OQ-3 fix, and that is a person's call",
                        doc_type, mk_comment("review"), filename, notes, file_as(doc_type))
    if needed and doc_type != needed:
        return Proposal(load_id, sha256, REVIEW, review.NOT_NEEDED,
                        f"the load is short a {needed} and this reads as a {doc_type}",
                        doc_type, mk_comment("review"), filename, notes, file_as(doc_type))

    # 7. How the document reached this load. Anything weaker than the message's own subject line,
    #    or a thread whose binding was contradicted, is a person's call.
    if conflicted:
        gate, kind = REVIEW, review.CONFLICT
        reason = "a reply in this thread named a different load; the binding is contested"
    elif routing and routing != "subject" and gate == AUTO:
        gate, kind = REVIEW, review.LOW_CONFIDENCE
        reason = f"load resolved by the {routing} tier, not this message's own subject line"

    comment = mk_comment("auto" if gate == AUTO else "review")

    if gate == AUTO and not allow_auto:
        # Shadow mode: the proposal is sound, but nothing files without the operator saying so.
        shown = doc_type if file_as(doc_type) == doc_type else f"{doc_type}, uploaded as {file_as(doc_type)}"
        return Proposal(load_id, sha256, REVIEW, review.SHADOW,
                        f"would file as {shown} ({why[:80]})", doc_type, comment, filename, notes,
                        file_as(doc_type))
    return Proposal(load_id, sha256, gate, kind, reason, doc_type, comment, filename, notes,
                    file_as(doc_type))


def _now():
    import datetime as dt
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))       # File History comments are written in ET


def _rules_for(conn, load_row, requirements_path):
    if not requirements_path or not Path(requirements_path).exists() or load_row is None:
        return None
    from pod_intake.requirements import Requirements
    reqs = Requirements.from_file(requirements_path)
    return reqs.for_customer(load_row["customer"], load_row["terminal"])


def _match_detail(conn: sqlite3.Connection, load_id: int, sha256: str, routing: str | None) -> str:
    """Why this document is believed to belong to this load, in words.

    The routing tier is the evidence on the mail side, and it is worth naming precisely: "load
    number in the subject line" is a different quality of claim from "the load an earlier message in
    the thread resolved to", and a reviewer reading File History months later cannot tell them apart
    from a tier name.
    """
    if routing == "subject":
        row = conn.execute(
            "SELECT m.subject_load_numbers FROM part p JOIN message m ON m.message_id = p.message_id "
            "WHERE p.sha256=? AND m.load_id=? LIMIT 1", (sha256, load_id)).fetchone()
        found = (row["subject_load_numbers"] if row else None) or str(load_id)
        return f"load number {found} in the email subject"
    if routing == "thread":
        return "the load this email thread was already bound to"
    if routing == "paper":
        return "reference numbers on the document itself"
    if db.tpro_file_for_sha(conn, sha256) is not None:
        return f"already attached to load {load_id} in TransportPro"
    return ""


def _routing_of(conn, load_id: int, sha256: str) -> tuple[str | None, bool]:
    row = conn.execute(
        "SELECT m.routing_tier, t.conflict_flag FROM part p "
        "JOIN message m ON m.message_id = p.message_id "
        "LEFT JOIN thread t ON t.thread_id = m.thread_id "
        "WHERE p.sha256=? AND m.load_id=? ORDER BY m.internal_date LIMIT 1", (sha256, load_id)).fetchone()
    if row is None:
        return None, False
    return row["routing_tier"], bool(row["conflict_flag"])


# ------------------------------------------------------------------ candidates ----

def candidates(conn: sqlite3.Connection, limit: int = 200) -> list[tuple[int, str]]:
    """(load, file) pairs that have been read, belong to a load, and are not filed or already
    decided. This is the work list `intake file` walks.

    A PENDING review row does not exclude a document. Pending is an open question, not a decision,
    and the answer moves underneath it: the load gets drained, delivers, completes, changes what it
    is short. Skipping those meant the queue froze the reason it was written with - 21 rows still
    read "would file as ..." for loads that have since become unassessed - and `execute` only
    refuses BLOCK and HOLD, so approving a stale row still filed on it. Re-judging is free (no
    model, no network), `enqueue` updates a pending row in place rather than adding another, and
    its ON CONFLICT clause already refuses to touch a row a person has decided.

    Every other state is a decision and is left alone - including `approved`, which used to be
    re-taken here. That gained nothing: `file --execute` re-proposes each approved item at the
    moment it files it, so the only effect was a competing pending row beside the approval.
    """
    rows = conn.execute(
        "SELECT DISTINCT m.load_id, p.sha256 FROM part p "
        "JOIN message m ON m.message_id = p.message_id "
        "JOIN attachment a ON a.sha256 = p.sha256 "
        "WHERE p.decision='keep' AND m.load_id IS NOT NULL AND a.extraction_json IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM filing f WHERE f.load_id=m.load_id AND f.sha256=p.sha256) "
        "  AND NOT EXISTS (SELECT 1 FROM review r WHERE r.load_id=m.load_id AND r.sha256=p.sha256 "
        "                  AND r.state IN ('approved','rejected','filed')) "
        "LIMIT ?", (limit,)).fetchall()
    return [(int(r["load_id"]), r["sha256"]) for r in rows]


# ------------------------------------------------------------------ the write ----

def fetch_bytes(conn: sqlite3.Connection, gmail, sha256: str, tpro=None) -> tuple[bytes, str]:
    """Re-fetch a document by its hash. The service stores the hash, never the bytes.

    Gmail first, because most documents arrive that way and the attachment ids stay valid for the
    life of the message. A document the service only ever saw ON the load - scanned by Loop C, never
    emailed - has no part row at all, and TransportPro is then the only place its bytes exist. That
    fallback needs a client, so a caller that cannot supply one still gets the old behaviour.

    Either way the hash is verified before the bytes are trusted: whichever source answered, if it
    hands back something else then the reading on file describes a different document and must not
    be used to type this one.
    """
    import hashlib

    src = db.source_part(conn, sha256)
    if src is not None:
        data, name = gmail.attachment_bytes(src["message_id"], src["attachment_id"]), src["filename"]
    else:
        row = db.tpro_file_for_sha(conn, sha256)
        if row is None:
            raise RuntimeError(f"no re-fetchable source for {sha256[:12]}: no Gmail part and no "
                               f"TransportPro file carries these bytes")
        if tpro is None:
            raise RuntimeError(f"{sha256[:12]} exists only on the load in TransportPro "
                               f"(file {row['tpro_file_id']}); this call was given no TransportPro client")
        data, _ = tpro.download_file(int(row["tpro_file_id"]))
        name = row["filename"]
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise RuntimeError(f"re-fetched bytes do not match: expected {sha256[:12]}, got {got[:12]}")
    return data, (name or f"{sha256[:12]}.bin")


def execute(conn: sqlite3.Connection, tpro, gmail, proposal: Proposal, *, dry_run: bool = True,
            review_id: int | None = None) -> dict:
    """File one document. Refuses anything not gated auto or approved by a person.

    dry_run is the default everywhere it is called from. The caller has to pass dry_run=False
    explicitly, and the CLI only does that for `--execute`.
    """
    if proposal.gate in (BLOCK, HOLD):
        return {"filed": False, "why": f"gate is {proposal.gate}: {proposal.reason}"}
    if not proposal.document_type:
        return {"filed": False, "why": "no document type resolved"}
    if conn.execute("SELECT 1 FROM filing WHERE load_id=? AND sha256=?",
                    (proposal.load_id, proposal.sha256)).fetchone():
        return {"filed": False, "why": "already filed on this load"}

    if dry_run:
        return {"filed": False, "dry_run": True, "why": "dry run",
                "would": {"recordType": "Loads", "recordId": proposal.load_id,
                          "documentType": proposal.upload_as or proposal.document_type,
                          "readAs": proposal.document_type, "comments": proposal.comment,
                          "filename": proposal.filename}}

    # tpro as well as gmail: a document the service only ever saw ON the load has no Gmail
    # part, and re-filing it under a clearing type is exactly the wrong_doc_type repair.
    data, filename = fetch_bytes(conn, gmail, proposal.sha256, tpro=tpro)
    # upload_as, not document_type: FILE_AS decides the label on the row, the reading decides
    # everything else. They are the same value unless house policy says otherwise.
    uploaded_as = proposal.upload_as or proposal.document_type
    result = tpro.upload_file(record_type="Loads", record_id=proposal.load_id,
                              document_type=uploaded_as, comments=proposal.comment or "",
                              filename=filename, data=data,
                              content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")
    file_id = str((result or {}).get("id") or (result or {}).get("fileId") or "")
    # The type it was UPLOADED as is what the filing record keeps, because that is what a later
    # reader of File History will see, and what decides whether the status cleared. What the
    # document reads as is already on the attachment row.
    conn.execute("INSERT OR IGNORE INTO filing (load_id, sha256, tpro_file_id, document_type, comment, "
                 "filed_at) VALUES (?,?,?,?,?,?)",
                 (proposal.load_id, proposal.sha256, file_id, uploaded_as,
                  proposal.comment, db.now_iso()))
    if review_id is not None:
        review.mark_filed(conn, review_id)
    # The load's state has changed; look at it again now rather than on its old cadence.
    conn.execute("UPDATE load SET next_check_at=? WHERE load_id=?", (db.now_iso(), proposal.load_id))
    return {"filed": True, "tpro_file_id": file_id, "document_type": proposal.document_type}
