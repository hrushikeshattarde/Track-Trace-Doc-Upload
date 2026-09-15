"""Loop A: everything that arrived since the cursor.

One pass = ask Gmail what changed, append a row per message, hash and de-duplicate its
attachments, and set the load's next_check_at. That last line is the whole hand-off: this loop
never calls TransportPro, so the mail side stays fast and the two sides retry independently.

Every step is idempotent. The message primary key makes a replayed batch free, which is what lets
the cursor be written *after* the work instead of before - a cursor advanced early is silent data
loss and the one failure nothing else here would catch.

The reader is optional and off by default. With no reader the pass costs nothing but Gmail calls
and still builds the full ledger, which is how you run this against the live mailbox on day one.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import db, filters, gmail as gm, routing

# A reader takes (bytes, filename) and returns (extraction dict, document_type, model, cost_usd).
Reader = Callable[[bytes, str], "tuple[dict, str, str, float]"]


@dataclass
class Stats:
    fetched: int = 0
    already_seen: int = 0
    bound: int = 0
    unresolved: int = 0
    conflicts: int = 0
    parts: collections.Counter = field(default_factory=collections.Counter)
    downloads: int = 0
    new_files: int = 0
    reads: int = 0
    reads_avoided: int = 0
    read_errors: int = 0
    deferred: int = 0
    spend_capped: bool = False
    cost_usd: float = 0.0
    mode: str = "history"
    cursor_from: str | None = None
    cursor_to: str | None = None

    def line(self) -> str:
        dropped = ", ".join(f"{k} {v}" for k, v in self.parts.most_common() if k != filters.KEEP) or "none"
        return (f"{self.mode}: {self.fetched} message(s) fetched, {self.already_seen} already known, "
                f"{self.bound} bound to a load, {self.unresolved} unresolved, {self.conflicts} thread conflict(s) | "
                f"parts kept {self.parts[filters.KEEP]}, dropped: {dropped} | "
                f"downloads {self.downloads}, new files {self.new_files}, reads {self.reads}, "
                f"reads avoided by hash {self.reads_avoided}"
                + (f", read errors {self.read_errors}" if self.read_errors else "")
                + (f", {self.deferred} message(s) deferred to the next pass" if self.deferred else "")
                + (" [SPEND CAP HIT: some documents left unread]" if self.spend_capped else "")
                + f", spend ${self.cost_usd:.3f}")


def sync_once(conn, client: gm.Delegated, *, group: str, reader: Reader | None = None,
              max_messages: int = 500, backfill_days: int = 1, max_parts_per_message: int = 12,
              max_spend_usd: float | None = None, verbose: bool = False) -> Stats:
    """One pass of Loop A. Returns what it did; raises only on an unrecoverable Gmail error.

    max_spend_usd bounds what one pass can cost. When it is reached the pass keeps ingesting -
    messages, parts, hashes, routing all continue - but stops calling the reader, so the documents
    it did not read stay as placeholder rows and are read by a later pass. A cap that dropped the
    mail instead of deferring the reading would be the --max bug again, in the cost dimension.
    """
    mailbox = client.subject
    st = Stats()
    cursor = db.get_cursor(conn, mailbox)
    refs: list[dict]

    if cursor:
        st.cursor_from = cursor
        try:
            refs, new_cursor = client.history_since(cursor)
        except gm.CursorTooOld:
            # Gmail keeps history for about a week; the usual cause is the service being down over
            # a long weekend. Re-read a window, then re-seed from the mailbox's own historyId.
            print(f"  cursor {cursor} is older than Gmail's history retention: falling back to a {backfill_days}-day full sync")
            refs, new_cursor = _full_sync_refs(client, group, backfill_days)
            st.mode = "full-sync (cursor expired)"
    else:
        refs, new_cursor = _full_sync_refs(client, group, backfill_days)
        st.mode = f"full-sync (first run, {backfill_days}d)"

    # A cap on work per pass must never be a cap on coverage. If more arrived than this pass will
    # take, the cursor is HELD: the rest are picked up next pass, and the already-seen check below
    # makes redoing the processed ones free (no Gmail call, no download, no read). Advancing the
    # cursor here instead would skip them permanently, and the custody count could not catch it
    # because they would never get a message row - the same failure as readiness.py's --max.
    # The cap applies to NEW work, not to the listing. Slicing the raw refs would keep re-taking
    # the same already-processed head of the list and never make progress.
    fresh = [r for r in refs if not db.message_seen(conn, r["id"])]
    st.already_seen = len(refs) - len(fresh)
    st.deferred = max(0, len(fresh) - max_messages)
    messages = []
    for ref in fresh[:max_messages]:
        messages.append(client.message(ref["id"]))
        st.fetched += 1
    # internalDate, never arrival order: history pagination and retries deliver replies out of order,
    # and the thread binding should be set by the earliest message that can resolve it.
    messages.sort(key=lambda m: int(m.get("internalDate") or 0))

    for msg in messages:
        try:
            conn.execute("BEGIN IMMEDIATE")
            over = max_spend_usd is not None and st.cost_usd >= max_spend_usd
            if over and not st.spend_capped:
                st.spend_capped = True
                print(f"  spend cap ${max_spend_usd:.2f} reached after {st.reads} read(s); "
                      f"still ingesting, but no further documents are read this pass")
            _process(conn, client, msg, group=group, reader=None if over else reader, st=st,
                     max_parts=max_parts_per_message, verbose=verbose)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # Only now, and only if this pass took everything it was offered. If we crashed above, or left
    # messages behind, the cursor is untouched and the next run picks them up.
    if new_cursor and not st.deferred:
        db.set_cursor(conn, mailbox, new_cursor, mode="full" if "full" in st.mode else "history")
        st.cursor_to = new_cursor
    elif st.deferred:
        print(f"  cursor held at {cursor}: {st.deferred} message(s) beyond --max {max_messages} "
              f"are left for the next pass")
    return st


def _full_sync_refs(client: gm.Delegated, group: str, days: int) -> tuple[list[dict], str]:
    """Ids from a search window, plus the mailbox's current historyId to restart the cursor from.

    Read the profile FIRST: anything that arrives during the search is then still after the new
    cursor and will be picked up next pass, rather than falling into the gap between the two calls.
    """
    history_id = str(client.profile().get("historyId") or "")
    refs = client.search(f"to:{group} newer_than:{days}d")
    return refs, history_id


def _process(conn, client: gm.Delegated, msg: dict, *, group: str, reader: Reader | None,
             st: Stats, max_parts: int, verbose: bool) -> None:
    message_id = msg["id"]
    thread_id = msg.get("threadId") or message_id
    headers = gm.headers_of(msg)
    when = gm.internal_date_iso(msg)

    db.touch_thread(conn, thread_id, when)
    thread = db.get_thread(conn, thread_id)
    thread_load = thread["load_id"] if thread and thread["load_id"] is not None else None

    frm = routing.original_sender(headers, group)
    domain = routing.sender_domain(frm)
    route = routing.resolve(headers.get("subject"), msg.get("snippet"), thread_load)

    if route.conflict:
        db.flag_thread_conflict(conn, thread_id)
        st.conflicts += 1
        print(f"  ! thread {thread_id}: {route.reason}")

    # Parts are recorded whether or not the message routed: an unresolved message that someone
    # later binds by hand must not need its attachments re-discovered.
    parts = _candidate_parts(msg, max_parts)
    db.insert_message(conn, message_id=message_id, thread_id=thread_id, internal_date=when,
                      from_domain=domain, from_internal=domain.endswith("circledelivers.com"),
                      subject_load_numbers=",".join(str(x) for x in route.subject_loads) or None,
                      load_id=route.load_id, routing_tier=route.tier, part_count=len(parts))

    if route.load_id is None:
        # Record what the message carries, from the part headers only - no download, no hash, no
        # spend. The reviewer working the list needs to see "2 attachments, 900 KB" to judge it,
        # and binding the message later then leaves only the download to do.
        for p in parts:
            decision = filters.metadata_decision(p["filename"], p["size"])
            decision = filters.PENDING if decision == filters.KEEP else decision
            db.record_part(conn, message_id, p["part_id"], filename=p["filename"], size=p["size"],
                           mime=p["mime"], decision=decision, attachment_id=p["attachment_id"])
            st.parts[decision] += 1
        # Tier 3 would go here: read one pending part and let the matcher try the paper. It is
        # deliberately not wired yet - it is the only tier that costs money, and it needs the load
        # index to come from the ledger rather than the prototype's JSON snapshot.
        db.add_unresolved(conn, message_id, thread_id, route.reason)
        st.unresolved += 1
        if verbose:
            print(f"  ? {message_id} from {domain or '(unknown)'}: {route.reason}")
        return

    db.clear_unresolved(conn, message_id)
    if thread_load is None:
        db.bind_thread(conn, thread_id, route.load_id, route.tier)
    st.bound += 1

    kept = _handle_parts(conn, client, msg, parts, reader=reader, st=st, verbose=verbose)

    # A load can reach the ledger from the mail side before any dashboard sweep has produced it.
    db.upsert_load(conn, route.load_id, source="mail", due_now=True)
    if verbose:
        print(f"  + {message_id} -> load {route.load_id} via {route.tier}; {kept} part(s) kept")


def _candidate_parts(msg: dict, max_parts: int) -> list[dict]:
    """Attachment parts worth considering, largest first so the cap keeps the likeliest documents."""
    out = []
    for part in gm.iter_parts(msg.get("payload") or {}):
        body = part.get("body") or {}
        if not body.get("attachmentId"):
            continue
        out.append({"part_id": part.get("partId") or body["attachmentId"],
                    "attachment_id": body["attachmentId"],
                    "filename": part.get("filename") or "",
                    "size": int(body.get("size") or 0),
                    "mime": part.get("mimeType") or ""})
    out.sort(key=lambda p: -p["size"])
    return out[:max_parts]


def _handle_parts(conn, client: gm.Delegated, msg: dict, parts: list[dict], *,
                  reader: Reader | None, st: Stats, verbose: bool) -> int:
    message_id = msg["id"]
    kept = 0
    for p in parts:
        decision = filters.metadata_decision(p["filename"], p["size"])
        if decision != filters.KEEP:
            db.record_part(conn, message_id, p["part_id"], filename=p["filename"], size=p["size"],
                           mime=p["mime"], decision=decision, attachment_id=p["attachment_id"])
            st.parts[decision] += 1
            continue

        data = client.attachment_bytes(message_id, p["attachment_id"])
        st.downloads += 1
        decision, dims = filters.geometry_decision(data)
        if decision != filters.KEEP:
            db.record_part(conn, message_id, p["part_id"], filename=p["filename"], size=p["size"],
                           mime=p["mime"], decision=decision, dims=dims, attachment_id=p["attachment_id"])
            st.parts[decision] += 1
            continue

        digest = hashlib.sha256(data).hexdigest()
        existing = db.get_attachment(conn, digest)
        db.record_part(conn, message_id, p["part_id"], filename=p["filename"], size=p["size"],
                       mime=p["mime"], decision=filters.KEEP, sha256=digest, dims=dims,
                       attachment_id=p["attachment_id"])
        st.parts[filters.KEEP] += 1
        kept += 1

        if existing is not None and existing["extraction_json"] is not None:
            # The same bytes, quoted again in a reply or seen on another load. Never pay twice.
            st.reads_avoided += 1
            if verbose:
                print(f"    = {p['filename']} {digest[:12]} already read as {existing['document_type']}")
            continue

        if existing is None:
            st.new_files += 1
        if reader is None:
            db.put_attachment(conn, digest, message_id=message_id, filename=p["filename"],
                              size=len(data), extraction=None, document_type=None, model=None, cost_usd=None)
            continue

        try:
            extraction, doc_type, model, cost = reader(data, p["filename"] or digest[:12])
        except Exception as e:  # noqa: BLE001 - one unreadable file must not abort the batch
            # A format PyMuPDF cannot open (HEIC without pillow-heif), a corrupt part, a refusal, a
            # transient API error. Record the failure against the hash so the next pass retries this
            # file and only this file, and carry on with the rest of the message.
            st.read_errors += 1
            db.put_attachment(conn, digest, message_id=message_id, filename=p["filename"], size=len(data),
                              extraction=None, document_type=None, model=None, cost_usd=None,
                              error=f"{type(e).__name__}: {e}"[:300])
            print(f"    ! {p['filename'] or digest[:12]}: reader failed, left for the next pass - {type(e).__name__}: {e}")
            continue
        db.put_attachment(conn, digest, message_id=message_id, filename=p["filename"], size=len(data),
                          extraction=extraction, document_type=doc_type, model=model, cost_usd=cost)
        st.reads += 1
        st.cost_usd += cost or 0.0
        if verbose:
            print(f"    * {p['filename']} read as {doc_type} (${cost:.4f})")
    return kept


def make_reader(model: str) -> Reader:
    """Adapter over pod_intake.reader. Bytes go to a temp file because normalize.load_document
    works on paths (PyMuPDF opens PDFs and images the same way), and the file is deleted straight
    after - the ledger keeps the hash and the extraction, never the document."""
    import anthropic

    from pod_intake import reader as claude_reader
    from pod_intake.localenv import load_local_env
    from pod_intake.normalize import load_document

    load_local_env()
    client = anthropic.Anthropic()

    def read(data: bytes, filename: str) -> tuple[dict, str, str, float]:
        suffix = Path(filename).suffix or (".pdf" if data[:5] == b"%PDF-" else ".png")
        tmp = Path(tempfile.mkdtemp(prefix="intake_")) / f"doc{suffix}"
        try:
            tmp.write_bytes(data)
            doc = load_document(tmp)
            extraction, usage = claude_reader.read_document(client, doc, model)
            return extraction.model_dump(), extraction.document_type, model, round(usage.cost_usd, 5)
        finally:
            # Best-effort. On Windows PyMuPDF keeps a handle on the file it opened, so the unlink
            # can raise PermissionError (WinError 32) - and raising it from `finally` would throw
            # away a read that has already been paid for. The OS clears the temp directory anyway.
            try:
                tmp.unlink(missing_ok=True)
                tmp.parent.rmdir()
            except OSError:
                pass

    return read
