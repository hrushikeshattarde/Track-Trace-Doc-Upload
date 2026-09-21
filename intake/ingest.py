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

# Gmail keeps history for about a week. When the cursor is older than that the gap has to be covered
# by a search window, and the window has to be at least as wide as the outage.
HISTORY_RETENTION_DAYS = 8
MAX_FULL_SYNC_DAYS = 30          # past this the per-load backfill is the right tool, not a wide search

# How many service-side failures in a row end a read pass. Three is enough to tell "this one file
# upset the API" from "the account has no credit", and small enough that a paused service costs
# three Gmail downloads rather than a hundred.
SERVICE_FAILURE_LIMIT = 3

# Which failures are worth retrying lives in db.permanent_error_text: it has to classify a stored
# error string as well as a live exception, so there is one rule and not two that drift.


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
    reopened_for_backfill: int = 0

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


def permanent_read_error(exc: Exception) -> bool:
    """Whether this failure is worth a second attempt. Classified from exactly the text that gets
    recorded, so a row's stored error and the live decision can never disagree."""
    return db.permanent_error_text(f"{type(exc).__name__}: {exc}")


def _outage_days(conn, mailbox: str, floor_days: int) -> int:
    """How far back a cursor-expiry sync has to look.

    The window is the outage itself - from when the cursor was last written - plus a day of margin,
    not a fixed --backfill-days. A 1-day window after a 3-day outage re-seeds the cursor past days 2
    and 3, and Loop A is incremental forever after, so nothing ever looks at them again. Reading too
    much is free here: every message it re-lists is already in the ledger and skipped before any
    fetch. Reading too little is silent loss.
    """
    row = db.get_cursor_row(conn, mailbox)
    synced = None
    if row and row["synced_at"]:
        try:
            synced = dt.datetime.fromisoformat(str(row["synced_at"]).replace("Z", "+00:00"))
        except ValueError:
            synced = None
    if synced is None:
        return max(floor_days, HISTORY_RETENTION_DAYS)
    gap = (dt.datetime.now(dt.timezone.utc) - synced).days + 1
    return max(floor_days, min(gap, MAX_FULL_SYNC_DAYS))


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
            # a long weekend. Re-read a window that covers the whole outage, then re-seed from the
            # mailbox's own historyId.
            days = _outage_days(conn, mailbox, backfill_days)
            print(f"  cursor {cursor} is older than Gmail's history retention: "
                  f"falling back to a {days}-day full sync")
            refs, new_cursor = _full_sync_refs(client, group, days)
            st.mode = f"full-sync (cursor expired, {days}d)"
            # A search window is not a history stream. `to:group newer_than:Nd` misses anything the
            # query does not match, and the cursor is about to be re-seeded past all of it, so this
            # is the last chance to look. Re-open the per-load backfill for every in-view load still
            # short paperwork: its targeted subject search is the only thing that can still find it.
            reopened = db.flag_history_gap(conn)
            st.reopened_for_backfill = reopened
            if reopened:
                print(f"  {reopened} in-view load(s) flagged for re-backfill - "
                      f"run 'python -m intake backfill' to close the gap")
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


def backfill_load(conn, client: gm.Delegated, *, group: str, load_id: int,
                  reader: Reader | None = None, st: Stats | None = None,
                  max_messages: int = 40, verbose: bool = False) -> int:
    """Ingest the mail history of ONE load, once.

    Loop A is incremental by design: it asks Gmail what arrived since the cursor, which is the right
    shape for the steady state and blind to everything older. A load that joins the dashboard with
    an email chain already behind it therefore looks like it has no paperwork at all.

    This closes that hole with a single targeted search per load - the query readiness.py used, but
    run once in the load's lifetime rather than on every pass. Messages already in the ledger cost
    nothing (the message_id check short-circuits before any fetch), so it is safe to re-run and safe
    to interrupt.
    """
    st = st or Stats()
    refs = client.search(f"to:{group} subject:{load_id}", cap=max_messages)
    fresh = [r for r in refs if not db.message_seen(conn, r["id"])]
    messages = []
    for ref in fresh[:max_messages]:
        messages.append(client.message(ref["id"]))
        st.fetched += 1
    st.already_seen += len(refs) - len(fresh)
    messages.sort(key=lambda m: int(m.get("internalDate") or 0))
    for msg in messages:
        try:
            conn.execute("BEGIN IMMEDIATE")
            _process(conn, client, msg, group=group, reader=reader, st=st, max_parts=12, verbose=verbose)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    # Stamp only after the work commits, for the same reason the cursor is written last.
    db.mark_backfilled(conn, load_id)
    return len(messages)


def read_pending(conn, client: gm.Delegated, *, reader: Reader, load_ids: list[int] | None = None,
                 in_view_only: bool = True, limit: int = 100, max_spend_usd: float | None = None,
                 verbose: bool = False) -> Stats:
    """Read documents already in the ledger that have never been read.

    The bytes are re-fetched from Gmail by (message_id, attachment_id) - the service stores no
    document bytes - and the hash is verified before the reading is trusted, so a re-fetch that
    returns something else can never be filed under this document's identity.
    """
    st = Stats()
    st.mode = "read-pending"
    service_failures = 0
    for row in db.unread_attachments(conn, load_ids=load_ids, in_view_only=in_view_only, limit=limit):
        if service_failures >= SERVICE_FAILURE_LIMIT:
            # Nothing is wrong with the documents and nothing will be right with the next one
            # either. Walking the rest of the list to collect the same answer wastes a Gmail
            # download per row and buries the actual problem under repeats of itself.
            print(f"  stopping: {service_failures} consecutive service-side failures (no credit, "
                  f"quota or key). The remaining documents are untouched and keep their retries")
            break
        if max_spend_usd is not None and st.cost_usd >= max_spend_usd:
            if not st.spend_capped:
                st.spend_capped = True
                print(f"  spend cap ${max_spend_usd:.2f} reached after {st.reads} read(s); stopping")
            break
        sha = row["sha256"]
        try:
            from . import filing
            data, name = filing.fetch_bytes(conn, client, sha)
        except Exception as e:  # noqa: BLE001 - a message deleted from the mailbox, or a hash mismatch
            # A hash mismatch is permanent: the bytes behind that attachment id are not this
            # document any more, and re-fetching keeps returning the same wrong thing. A network
            # failure is not, and gets its retries.
            perm = permanent_read_error(e)
            db.put_attachment(conn, sha, message_id="", filename=row["filename"], size=row["bytes"] or 0,
                              extraction=None, document_type=None, model=None, cost_usd=None,
                              error=f"re-fetch failed: {type(e).__name__}: {e}"[:300], permanent=perm)
            st.read_errors += 1
            print(f"  ! {sha[:12]}: {type(e).__name__}: {e}")
            continue
        st.downloads += 1
        try:
            extraction, doc_type, model, cost = reader(data, name)
        except Exception as e:  # noqa: BLE001
            st.read_errors += 1
            perm = permanent_read_error(e)
            db.put_attachment(conn, sha, message_id="", filename=row["filename"], size=len(data),
                              extraction=None, document_type=None, model=None, cost_usd=None,
                              error=f"{type(e).__name__}: {e}"[:300], permanent=perm)
            if db.paused_error_text(f"{type(e).__name__}: {e}"):
                service_failures += 1
                print(f"  ! {name}: the reader is unavailable, not the file - {type(e).__name__}: "
                      f"{str(e)[:120]}")
            else:
                service_failures = 0
                print(f"  ! {name}: reader failed{', not retryable' if perm else ', will retry'} "
                      f"- {type(e).__name__}: {e}")
            continue
        db.put_attachment(conn, sha, message_id="", filename=row["filename"], size=len(data),
                          extraction=extraction, document_type=doc_type, model=model, cost_usd=cost)
        st.reads += 1
        service_failures = 0
        st.cost_usd += cost or 0.0
        # The load's paperwork picture just changed; look at it now rather than on its old cadence.
        conn.execute("UPDATE load SET next_check_at=? WHERE load_id=?", (db.now_iso(), row["load_id"]))
        if verbose:
            print(f"  load {row['load_id']}  {name[:34]:36} -> {doc_type} (${cost:.4f})")
    return st


def backfill_pass(conn, client: gm.Delegated, *, group: str, limit: int = 100,
                  reader: Reader | None = None, max_spend_usd: float | None = None,
                  load_ids: list[int] | None = None, restale_hours: int | None = None,
                  verbose: bool = False) -> Stats:
    """Backfill in-view loads that have never been searched. Bounded, resumable, idempotent.

    load_ids targets a chosen set instead of taking the next N - useful for working one pod, or one
    batch, without pulling the whole queue forward. restale_hours additionally re-takes loads that
    are still short paperwork and were last searched a while ago, which is the safety net under a
    cursor gap: Loop A cannot look backwards and Loop B never calls Gmail.
    """
    st = Stats()
    st.mode = "backfill" if not restale_hours else f"backfill (re-stale {restale_hours}h)"
    todo = load_ids if load_ids else db.loads_needing_backfill(conn, limit, restale_hours=restale_hours)
    for load_id in todo:
        if max_spend_usd is not None and st.cost_usd >= max_spend_usd and not st.spend_capped:
            st.spend_capped = True
            print(f"  spend cap ${max_spend_usd:.2f} reached; still ingesting, no further reads this pass")
        n = backfill_load(conn, client, group=group, load_id=load_id,
                          reader=None if st.spend_capped else reader, st=st, verbose=verbose)
        if verbose and n:
            print(f"  load {load_id}: {n} message(s) from history")
    return st


def reconsider_parts(conn, client: gm.Delegated, *, load_ids: list[int], reader: Reader | None = None,
                     max_spend_usd: float | None = None, limit: int = 50, verbose: bool = False) -> Stats:
    """Take a second look at attachments the free filters threw away, for named loads only.

    The filters in filters.py are judgement calls made on a filename and an image header: under
    40 KB, under 300 px on a side, wider than 2.2:1. They were tuned against real traffic and they
    are right the overwhelming majority of the time - load 2573804's thread held 111 image parts and
    not one document. But the decision is made once and nothing re-examines a processed message, so
    when they are wrong - a BOL photographed badly, a page cropped to a strip, a scan saved small -
    the document is gone with no trace in the work queue.

    This is the way back. It is deliberately not automatic and not a pass: a human names the loads,
    having seen the dropped count on the load or in the queue, and accepts the spend. The bytes are
    fetched by (message_id, attachment_id), hashed, and the part is re-recorded as kept, so from
    there on the document is an ordinary ledger document - de-duplicated, filed, reviewed the same
    way as any other.
    """
    st = Stats()
    st.mode = "reconsider"
    for load_id in load_ids:
        for row in db.dropped_parts(conn, load_id)[:limit]:
            if max_spend_usd is not None and st.cost_usd >= max_spend_usd and not st.spend_capped:
                st.spend_capped = True
                print(f"  spend cap ${max_spend_usd:.2f} reached; recovering without reading")
            try:
                conn.execute("BEGIN IMMEDIATE")
                _reconsider_one(conn, client, row, reader=None if st.spend_capped else reader,
                                st=st, verbose=verbose)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        # Whatever came back changes the load's paperwork picture; look at it now, not on its cadence.
        conn.execute("UPDATE load SET next_check_at=? WHERE load_id=?", (db.now_iso(), load_id))
    return st


def _reconsider_one(conn, client: gm.Delegated, row, *, reader: Reader | None, st: Stats,
                    verbose: bool) -> None:
    data = client.attachment_bytes(row["message_id"], row["attachment_id"])
    st.downloads += 1
    digest = hashlib.sha256(data).hexdigest()
    _, dims = filters.geometry_decision(data)
    # Recorded as kept with the geometry that had it dropped, so the ledger still shows what the
    # filter saw and who overrode it.
    db.record_part(conn, row["message_id"], row["part_id"], filename=row["filename"], size=row["bytes"],
                   mime=row["mime"], decision=filters.KEEP, sha256=digest, dims=dims,
                   attachment_id=row["attachment_id"])
    st.parts[filters.KEEP] += 1
    existing = db.get_attachment(conn, digest)
    if existing is not None and existing["extraction_json"] is not None:
        st.reads_avoided += 1
        if verbose:
            print(f"  = {row['filename']} already read as {existing['document_type']}")
        return
    if db.read_blocked(existing):
        if verbose:
            print(f"  x {row['filename']} unreadable: {(existing['error'] or '')[:60]}")
        return
    if existing is None:
        st.new_files += 1
    if reader is None:
        if existing is None:
            db.put_attachment(conn, digest, message_id=row["message_id"], filename=row["filename"],
                              size=len(data), extraction=None, document_type=None, model=None, cost_usd=None)
        print(f"  + load {row['load_id']}  {(row['filename'] or '')[:36]:38} "
              f"{(row['bytes'] or 0) // 1024:5} KB {dims or ''}  recovered, not read")
        return
    try:
        extraction, doc_type, model, cost = reader(data, row["filename"] or digest[:12])
    except Exception as e:  # noqa: BLE001 - one unreadable file must not abort the batch
        st.read_errors += 1
        perm = permanent_read_error(e)
        db.put_attachment(conn, digest, message_id=row["message_id"], filename=row["filename"],
                          size=len(data), extraction=None, document_type=None, model=None, cost_usd=None,
                          error=f"{type(e).__name__}: {e}"[:300], permanent=perm)
        print(f"  ! {row['filename']}: reader failed - {type(e).__name__}: {e}")
        return
    db.put_attachment(conn, digest, message_id=row["message_id"], filename=row["filename"],
                      size=len(data), extraction=extraction, document_type=doc_type, model=model, cost_usd=cost)
    st.reads += 1
    st.cost_usd += cost or 0.0
    print(f"  + load {row['load_id']}  {(row['filename'] or '')[:36]:38} "
          f"was {row['decision']} -> {doc_type} (${cost:.4f})")


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

        if db.read_blocked(existing):
            # Failed in a way nothing will read differently. Recorded once; not paid for again.
            if verbose:
                print(f"    x {p['filename']} {digest[:12]} unreadable: {(existing['error'] or '')[:60]}")
            continue

        if existing is None:
            st.new_files += 1
        if reader is None:
            # Only ever create the placeholder. Rewriting an existing row here would reset the
            # failure and its attempt count every time a reply re-quoted the same bytes.
            if existing is None:
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
            perm = permanent_read_error(e)
            db.put_attachment(conn, digest, message_id=message_id, filename=p["filename"], size=len(data),
                              extraction=None, document_type=None, model=None, cost_usd=None,
                              error=f"{type(e).__name__}: {e}"[:300], permanent=perm)
            print(f"    ! {p['filename'] or digest[:12]}: reader failed"
                  f"{', not retryable' if perm else ', will retry'} - {type(e).__name__}: {e}")
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
    from pod_intake import provider, reader as claude_reader
    from pod_intake.localenv import load_local_env
    from pod_intake.normalize import load_document

    load_local_env()
    client, which = provider.make_client()
    print(f"  reader: {model} via {provider.describe()}")

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
