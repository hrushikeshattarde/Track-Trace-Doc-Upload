"""Gmail -> S3, with nothing in between. What the scheduled Lambda runs every 15 minutes.

The ledger-based loop (ingest.sync_once) keeps its memory in SQLite: the Gmail cursor, which thread
belongs to which load, which files it has already hashed. A Lambda has no disk that outlives it, and
moving a SQLite file in and out of S3 four times an hour would make one file the single point every
run depends on. This keeps everything the collector needs to remember in S3 itself:

    where it left off        state/gmail-bookmark.json - Gmail's historyId, and nothing else that matters
    already stored?          the object keys: mail by Gmail message id, documents by sha256
    which load a reply is    the subject, else the earlier messages of the same Gmail thread
    what a message carried   the manifest inside the mail object, including what the filters dropped

Collection is the only job here. Nothing is read by a model and nothing touches TransportPro; the
later steps pick up what lands in S3 on their own schedules.

WHY THE BOOKMARK IS SAFE TO RELY ON
-----------------------------------
It only moves once every message Gmail listed since it has been stored - documents first, then the
mail object that points at them, so a mail object never names a document that is not there yet. A
run that stops early (its message cap, its time limit, a Gmail error) leaves the bookmark where it
was and records which messages of that window it did finish, so the next run neither loses the rest
nor fetches the finished ones again. Every write is keyed by content, so redoing any of it stores
nothing twice. The bookmark itself is written only over the version the run read (S3 If-Match): two
runs that somehow overlapped cannot both move it.
"""
from __future__ import annotations

import base64
import collections
import datetime as dt
import email
import hashlib
import json
import time
from dataclasses import dataclass, field
from email import policy

from . import filters, gmail as gm, routing, store as s3store

BOOKMARK_KEY = "state/gmail-bookmark.json"
MAX_PARTS = 12             # the same cap the ledger loop applies, largest first
OVER_CAP = "over_part_cap"


class BookmarkChanged(RuntimeError):
    """The bookmark moved while this run held it. Its stored objects stand; its bookmark does not."""


@dataclass
class Bookmark:
    history_id: str
    taken_at: str                        # when history_id was the mailbox head; sizes an expiry re-walk
    stored: list[str] = field(default_factory=list)   # finished message ids of an unfinished window
    etag: str | None = None


@dataclass
class Stats:
    mode: str = "history"
    listed: int = 0
    done_before: int = 0        # finished by an earlier run of the same window; not fetched again
    fetched: int = 0
    mail_stored: int = 0
    mail_already: int = 0       # already in S3 at that key
    vanished: int = 0
    bound: int = 0
    via_thread: int = 0
    unresolved: int = 0
    docs_stored: int = 0
    docs_already: int = 0
    parts: collections.Counter = field(default_factory=collections.Counter)
    deferred: int = 0
    stopped_by: str = ""
    error: str = ""
    cursor_from: str = ""
    cursor_to: str = ""

    def line(self) -> str:
        dropped = ", ".join(f"{k} {v}" for k, v in self.parts.most_common() if k != filters.KEEP) or "none"
        bits = [f"{self.mode}: {self.listed} listed",
                f"{self.fetched} fetched",
                f"{self.mail_stored} mail stored" + (f" ({self.mail_already} already there)" if self.mail_already else ""),
                f"{self.bound} on a load ({self.via_thread} via their thread), {self.unresolved} with no load number",
                f"documents {self.docs_stored} stored, {self.docs_already} already there",
                f"parts kept {self.parts[filters.KEEP]}, dropped: {dropped}"]
        if self.done_before:
            bits.append(f"{self.done_before} finished by an earlier run")
        if self.vanished:
            bits.append(f"{self.vanished} deleted from Gmail before they could be fetched")
        if self.deferred:
            bits.append(f"{self.deferred} left for the next run ({self.stopped_by})")
        if self.error:
            bits.append(f"STOPPED ON ERROR: {self.error}")
        bits.append(f"bookmark {self.cursor_from} -> {self.cursor_to}")
        return " | ".join(bits)


# ---------------------------------------------------------------------------------- bookmark ----

def read_bookmark(store: s3store.Store, key: str = BOOKMARK_KEY) -> Bookmark:
    """The bookmark, or an error. Never a blank start: with no bookmark there is no way to know
    where the archive ends, and guessing either re-walks weeks of mail or silently skips some."""
    obj = store.s3.get_object(Bucket=store.bucket, Key=store.full(key))
    body = json.loads(obj["Body"].read())
    return Bookmark(history_id=str(body["history_id"]), taken_at=body.get("taken_at") or "",
                    stored=list(body.get("stored") or []), etag=obj["ETag"])


def write_bookmark(store: s3store.Store, bm: Bookmark, key: str = BOOKMARK_KEY, *,
                   create: bool = False) -> str:
    """Write over the version that was read (If-Match), or only where none exists (create)."""
    from botocore.exceptions import ClientError
    body = json.dumps({"history_id": bm.history_id, "taken_at": bm.taken_at, "stored": bm.stored,
                       "written_at": _now()}, indent=1).encode("utf-8")
    cond = {"IfNoneMatch": "*"} if create else {"IfMatch": bm.etag}
    try:
        r = store.s3.put_object(Bucket=store.bucket, Key=store.full(key), Body=body,
                                ContentType="application/json", **cond)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
            raise BookmarkChanged(
                f"s3://{store.bucket}/{store.full(key)} " + ("already exists" if create else
                "changed while this run held it; the objects it stored stand, the next run carries on")) from None
        raise
    return r.get("ETag", "")


# -------------------------------------------------------------------------------------- run ----

def run(gmail: gm.Delegated, store: s3store.Store, *, group: str, max_messages: int = 400,
        deadline: float | None = None, bookmark_key: str = BOOKMARK_KEY, verbose: bool = False) -> Stats:
    """One collection pass. Returns what it did; st.error is set when it stopped on one."""
    st = Stats()
    bm = read_bookmark(store, bookmark_key)
    st.cursor_from = bm.history_id
    try:
        refs, latest = gmail.history_since(bm.history_id)
    except gm.CursorTooOld:
        # Gmail keeps history for about a week. Past that, re-list a window covering the whole gap;
        # everything already stored is found by its key and costs a fetch, never a second copy.
        days = _gap_days(bm.taken_at)
        latest = str(gmail.profile().get("historyId") or "")        # before the search: see ingest
        refs = gmail.search(f"to:{group} newer_than:{days}d", cap=100_000)
        st.mode = f"re-walk ({days}d, bookmark older than Gmail keeps history)"
        bm.stored = []
    st.listed = len(refs)

    done = set(bm.stored)
    todo = [r for r in refs if r["id"] not in done]
    st.done_before = len(refs) - len(todo)
    for i, ref in enumerate(todo):
        if i >= max_messages or (deadline is not None and time.monotonic() >= deadline):
            st.deferred = len(todo) - i
            st.stopped_by = "message cap" if i >= max_messages else "time limit"
            break
        try:
            _collect_one(gmail, store, ref, group=group, st=st, verbose=verbose)
        except Exception as e:                                   # noqa: BLE001 - saved, then reported
            # Stop here rather than skip it: a Gmail outage or an S3 refusal is not an absence, and
            # moving past this message would lose it. What finished before it is kept.
            st.deferred = len(todo) - i
            st.stopped_by = "error"
            st.error = f"{ref['id']}: {type(e).__name__} {str(e)[:200]}"
            break
        done.add(ref["id"])

    if st.deferred:
        nxt = Bookmark(bm.history_id, bm.taken_at, sorted(done), bm.etag)
    else:
        nxt = Bookmark(latest or bm.history_id, _now(), [], bm.etag)
    write_bookmark(store, nxt, bookmark_key)
    st.cursor_to = nxt.history_id + (f" (+{len(nxt.stored)} done)" if nxt.stored else "")
    return st


def _collect_one(gmail: gm.Delegated, store: s3store.Store, ref: dict, *, group: str, st: Stats,
                 verbose: bool) -> None:
    mid = ref["id"]
    try:
        msg = gmail.message(mid, fmt="raw")
    except gm.GmailError as e:
        if e.status != 404:
            raise
        st.vanished += 1           # deleted after it arrived; nothing is left to store
        return
    st.fetched += 1
    raw = base64.urlsafe_b64decode(msg["raw"] + "=" * (-len(msg["raw"]) % 4))
    internal_date = gm.internal_date_iso(msg)
    thread_id = msg.get("threadId") or ref.get("threadId") or mid
    parsed = email.message_from_bytes(raw, policy=policy.default)
    subject = str(parsed.get("Subject") or "")
    frm = routing.original_sender({"from": str(parsed.get("From") or ""),
                                   "x-original-sender": str(parsed.get("X-Original-Sender") or "")}, group)

    route = routing.resolve(subject, msg.get("snippet"), None)
    if len(routing.loads_in(subject)) != 1 and thread_id != mid:
        # The ledger remembered which thread belonged to which load. Gmail already knows the thread,
        # so ask it: the earliest earlier message that names exactly one load binds the reply.
        thread_load = _thread_load(gmail, thread_id, mid, msg.get("internalDate"))
        if thread_load is not None:
            route = routing.resolve(subject, msg.get("snippet"), thread_load)
            st.via_thread += route.tier == routing.TIER_THREAD
    if route.load_id is None:
        st.unresolved += 1
    else:
        st.bound += 1

    manifest = []
    for part in _parts(parsed):
        data, filename = part["data"], part["filename"]
        sha = hashlib.sha256(data).hexdigest()
        decision = part["decision"] or filters.metadata_decision(filename, len(data))
        if decision == filters.KEEP:
            decision, _ = filters.geometry_decision(data)
        st.parts[decision] += 1
        if decision == filters.KEEP:
            # Documents before the mail that names them. Unread here, so tagged pii=unchecked.
            put = store.put_document(sha, data, filename=filename, message_id=mid,
                                     load_id=route.load_id, pii=None)
            if put.skipped:
                st.docs_already += 1
            else:
                st.docs_stored += 1
        manifest.append({"sha256": sha, "decision": decision, "filename": filename,
                         "bytes": len(data), "mime": part["mime"], "read_as": None,
                         "in_doc_prefix": decision == filters.KEEP})

    envelope = {"thread_id": thread_id, "load_id": route.load_id, "routing_tier": route.tier,
                "routing_reason": route.reason, "subject": subject, "from": frm,
                "from_domain": routing.sender_domain(frm), "internal_date": internal_date,
                "labels": msg.get("labelIds") or [], "part_count": len(manifest),
                "collected_by": "collector"}
    put = store.put_mail(message_id=mid, internal_date=internal_date, raw=raw, envelope=envelope,
                         attachments=manifest)
    if put.skipped:
        st.mail_already += 1
    else:
        st.mail_stored += 1
    if verbose:
        print(f"  {mid} -> {put.key}  load {route.load_id} ({route.tier}), {len(manifest)} part(s)")


def _parts(parsed: email.message.EmailMessage) -> list[dict]:
    """Every attachment-like part, largest first, with the ledger loop's cap applied.

    The same test the ledger loop used, from the other side: Gmail gives an attachmentId to parts
    with a filename or a non-text body, and metadata_decision drops anything unnamed as too small.
    Parts beyond the cap are listed in the manifest - a reviewer must be able to see they existed -
    but are not considered, exactly as before.
    """
    out = []
    for p in parsed.walk():
        if p.is_multipart():
            continue
        filename = p.get_filename() or ""
        if not filename and p.get_content_maintype() == "text":
            continue                      # the message body itself
        data = p.get_payload(decode=True) or b""
        out.append({"data": data, "filename": filename, "mime": p.get_content_type(), "decision": None})
    out.sort(key=lambda x: -len(x["data"]))
    for extra in out[MAX_PARTS:]:
        extra["decision"] = OVER_CAP
    return out


def _thread_load(gmail: gm.Delegated, thread_id: str, message_id: str, internal_ms) -> int | None:
    thread = gmail.thread(thread_id)
    me = int(internal_ms or 0)
    earlier = sorted((m for m in thread.get("messages") or []
                      if m.get("id") != message_id and int(m.get("internalDate") or 0) <= me),
                     key=lambda m: int(m.get("internalDate") or 0))
    for m in earlier:
        r = routing.resolve(gm.headers_of(m).get("subject"), m.get("snippet"), None)
        if r.load_id is not None:
            return r.load_id
    return None


def _gap_days(taken_at: str) -> int:
    try:
        then = dt.datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
    except ValueError:
        return 30
    return max(1, (dt.datetime.now(dt.timezone.utc) - then).days + 1)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
