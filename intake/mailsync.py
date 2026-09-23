"""S3 -> the ledger: what the collector stored, told to the load loop.

The collector writes mail and documents to S3 and keeps no ledger. The load loop still needs the
mail side of the ledger - drain() asks "is there paperwork in the thread for this load?" from it,
and every message routed to a load pulls that load's next check forward. This pass is the bridge:
it reads the mail objects the collector wrote and records them exactly as Loop A would have, from
the envelope and manifest alone. No Gmail call, no download, no model call.

It finds new objects by listing the last few days of mail/<yyyy>/<mm>/<dd>/ and skipping every
message the ledger already has. Listing is cheap (a thousand keys per call) and needs nothing
configured on the bucket; a message stored under an older day - a re-walk after a long outage -
is caught by widening `days`, which is safe because a message already seen costs one lookup.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import time
from dataclasses import dataclass

from . import db, filters, routing, store as s3store


@dataclass
class IngestStats:
    listed: int = 0
    known: int = 0
    ingested: int = 0
    bound: int = 0
    unresolved: int = 0
    documents: int = 0
    out_of_time: bool = False

    def line(self) -> str:
        return (f"mail from S3: {self.listed} listed, {self.known} already in the ledger, "
                f"{self.ingested} new ({self.bound} on a load, {self.unresolved} with no load number), "
                f"{self.documents} new document(s)"
                + (" - stopped at the time limit, the rest next run" if self.out_of_time else ""))


def ingest_recent(conn, store: s3store.Store, *, days: int = 7, deadline: float | None = None,
                  today: dt.date | None = None) -> IngestStats:
    """Record every mail object from the last `days` days that the ledger does not have yet."""
    st = IngestStats()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    prefixes = [f"{s3store.MAIL_PREFIX}/{(today - dt.timedelta(days=d)):%Y/%m/%d}/" for d in range(days - 1, -1, -1)]
    prefixes.append(f"{s3store.MAIL_PREFIX}/unknown/")
    for prefix in prefixes:
        for key in _keys(store, prefix):
            st.listed += 1
            mid = key.rsplit("/", 1)[1].split(".", 1)[0]
            if db.message_seen(conn, mid):
                st.known += 1
                continue
            if deadline is not None and time.monotonic() >= deadline:
                st.out_of_time = True
                return st
            body = json.loads(gzip.decompress(store.s3.get_object(Bucket=store.bucket, Key=store.full(key))["Body"].read()))
            record(conn, body, key, st)
    return st


def record(conn, body: dict, key: str, st: IngestStats) -> None:
    """One mail object into the ledger, as ingest._process would have recorded the message."""
    env = body.get("envelope") or {}
    mid = body["message_id"]
    thread_id = env.get("thread_id") or mid
    when = env.get("internal_date") or body.get("internal_date")
    load_id = env.get("load_id")
    domain = env.get("from_domain") or ""
    parts = body.get("attachments") or []

    conn.execute("BEGIN IMMEDIATE")
    try:
        db.touch_thread(conn, thread_id, when)
        db.insert_message(conn, message_id=mid, thread_id=thread_id, internal_date=when,
                          from_domain=domain, from_internal=domain.endswith("circledelivers.com"),
                          subject_load_numbers=",".join(str(x) for x in routing.loads_in(env.get("subject"))) or None,
                          load_id=load_id, routing_tier=env.get("routing_tier"), part_count=len(parts))
        for i, a in enumerate(parts):
            keep = a.get("decision") == filters.KEEP
            # An unrouted message's documents wait as PENDING, exactly as Loop A leaves them: they are
            # not work until somebody binds the message to a load.
            decision = filters.PENDING if keep and load_id is None else a.get("decision")
            db.record_part(conn, mid, f"s3:{i}", filename=a.get("filename"), size=a.get("bytes"),
                           mime=a.get("mime"), decision=decision, sha256=a.get("sha256") if keep else None)
            if keep and load_id is not None:
                if db.get_attachment(conn, a["sha256"]) is None:
                    db.put_attachment(conn, a["sha256"], message_id=mid, filename=a.get("filename"),
                                      size=a.get("bytes") or 0, extraction=None, document_type=None,
                                      model=None, cost_usd=None)
                    st.documents += 1
                # The collector stored it before the mail that names it, so it is already archived.
                db.mark_doc_archived(conn, a["sha256"], s3store.doc_key(a["sha256"]))
        if load_id is None:
            db.add_unresolved(conn, mid, thread_id, env.get("routing_reason") or "no load number")
            st.unresolved += 1
        else:
            thread = db.get_thread(conn, thread_id)
            if thread is None or thread["load_id"] is None:
                db.bind_thread(conn, thread_id, int(load_id), env.get("routing_tier") or "subject")
            db.upsert_load(conn, int(load_id), source="mail", due_now=True)
            st.bound += 1
        db.mark_mail_archived(conn, mid, key)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    st.ingested += 1


def _keys(store: s3store.Store, prefix: str):
    for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=store.bucket, Prefix=store.full(prefix)):
        for o in page.get("Contents", []):
            yield o["Key"][len(store.prefix):] if store.prefix else o["Key"]
