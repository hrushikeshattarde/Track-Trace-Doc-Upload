"""Moving what the ledger knows about into S3, and keeping it there.

Two passes, both resumable and both safe to interrupt:

    mail       every message the ledger has routed, as raw RFC822 plus the envelope it was routed on
    documents  every unique attachment kept by the filters, addressed by its sha256

Neither pass re-fetches what it has already stored: the ledger carries the S3 key, `pending_*`
only offers rows without one, and the keys are derived from the content, so an interrupted run
resumes rather than restarting. This is the property that answers "do not waste time recording
every mail again" - a message is fetched from Gmail once, ever.

The archive is what makes Gmail stop being the only copy. That matters for a reason that is easy to
miss until it bites: the Gmail history cursor this service runs on is good for about a week, so a
service down longer than that cannot ask Gmail what changed and must re-walk a date range. Once mail
is archived, the re-walk is a local operation against S3 instead of tens of thousands of API calls.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from . import db, gmail as gm, store as st


@dataclass
class ArchiveStats:
    fetched: int = 0
    stored: int = 0
    skipped: int = 0        # already in S3 at that exact key
    bytes_written: int = 0
    errors: int = 0
    pii_skipped: int = 0
    unchecked: int = 0      # stored before anyone read the page; tagged pii=unchecked
    retagged: int = 0       # read since being stored, so the tag now says what the reader found
    notes: list[str] = field(default_factory=list)

    def line(self, what: str) -> str:
        bits = [f"{what}: {self.stored} stored", f"{self.skipped} already there"]
        if self.pii_skipped:
            bits.append(f"{self.pii_skipped} withheld as personal ID")
        if self.unchecked:
            bits.append(f"{self.unchecked} unread, tagged pii=unchecked")
        if self.retagged:
            bits.append(f"{self.retagged} re-tagged now that they have been read")
        if self.errors:
            bits.append(f"{self.errors} error(s)")
        mb = self.bytes_written / 1e6
        bits.append(f"{mb:.1f} MB written" if mb >= 0.1 else f"{self.bytes_written} bytes written")
        return ", ".join(bits)


def _is_pii(extraction_json: str | None) -> bool:
    """Whether the reader found a personal ID on the page.

    Deliberately textual and conservative: the extraction schema has changed shape twice, and a
    missed flag here writes a driver's licence to S3. Anything that looks like the licence finding
    counts, and when the document has not been read at all the answer is False - nothing is known,
    and withholding every unread document would archive nothing on the first run. The document pass
    tags that case pii=unchecked rather than false; see _pii_finding.
    """
    if not extraction_json:
        return False
    low = extraction_json.lower()
    return any(k in low for k in ('"personal_id"', "driver's licence", "driver's license",
                                  "drivers license", "commercial driver", "cdl"))


def _pii_finding(extraction_json: str | None) -> bool | None:
    """True or False once the page has been read; None while nobody has."""
    return None if not extraction_json else _is_pii(extraction_json)


def archive_mail(conn, gmail: gm.Delegated, store: st.Store, *, limit: int = 500,
                 in_view_only: bool = False, rewrite: bool = False,
                 verbose: bool = False) -> ArchiveStats:
    """Store every message the ledger has not archived yet, oldest first.

    `rewrite` re-PUTs an object that is already at that key. Normally that is exactly what we do not
    want - the keys are content-derived, so an object already there holds the same raw message - but
    the JSON around the raw message can change, as it did on 22 Sep 2026 when the attachment manifest
    was added. Without this, clearing s3_key to force a re-archive would fetch every message from
    Gmail, skip the PUT because the key exists, and mark it archived with the old body: a silent
    no-op that looks exactly like success.
    """
    stats = ArchiveStats()
    for row in db.pending_mail_archive(conn, limit=limit, in_view_only=in_view_only):
        mid = row["message_id"]
        envelope = {k: row[k] for k in ("thread_id", "load_id", "from_domain", "routing_tier",
                                        "part_count", "internal_date")}
        raw = None
        try:
            # format=raw is the whole message as Gmail received it - headers, body, attachments,
            # signatures. It is the only form that still means something once the mailbox is gone.
            msg = gmail.message(mid, fmt="raw")
            stats.fetched += 1
            if msg.get("raw"):
                raw = base64.urlsafe_b64decode(msg["raw"] + "=" * (-len(msg["raw"]) % 4))
        except Exception as e:                                   # noqa: BLE001 - reported, not raised
            # One unreadable message must not stop an archive run: it is left with no S3 key and
            # offered again next pass, which is exactly the behaviour a transient 5xx needs.
            stats.errors += 1
            if verbose:
                print(f"  ! {mid}: {type(e).__name__} {str(e)[:100]}")
            continue
        try:
            put = store.put_mail(message_id=mid, internal_date=row["internal_date"], raw=raw,
                                 envelope=envelope, attachments=db.message_parts(conn, mid),
                                 if_absent=not rewrite)
        except Exception as e:                                   # noqa: BLE001
            stats.errors += 1
            if verbose:
                print(f"  ! {mid}: S3 {type(e).__name__} {str(e)[:100]}")
            continue
        db.mark_mail_archived(conn, mid, put.key)
        stats.bytes_written += put.bytes_written
        if put.skipped:
            stats.skipped += 1
        else:
            stats.stored += 1
        if verbose:
            print(f"  {mid}  ->  {put.key}{'  (already there)' if put.skipped else ''}")
    conn.commit()
    return stats


def archive_documents(conn, gmail: gm.Delegated, store: st.Store, *, limit: int = 200,
                      in_view_only: bool = True, skip_pii: bool = True, rewrite: bool = False,
                      verbose: bool = False) -> ArchiveStats:
    """Store every unique kept document, addressed by content.

    skip_pii withholds pages the reader found a personal ID on. It defaults to True because the
    service has never stored those bytes before and starting to is a retention decision: the ledger
    still records that the document exists, what it read as, and that it was withheld.

    A page nobody has read is stored and tagged pii=unchecked (decided 23 Sep 2026). Holding it back
    instead would keep it out of doc/ for good on any load already complete, since those are never
    read, and it would protect nothing: the same bytes are inside the archived mail, in the same
    bucket. When such a page is read later, the pass at the end re-tags it with the answer.
    """
    stats = ArchiveStats()
    for row in db.pending_doc_archive(conn, limit=limit, in_view_only=in_view_only):
        sha, filename = row["sha256"], row["filename"] or ""
        pii = _pii_finding(row["extraction_json"])
        if pii and skip_pii:
            stats.pii_skipped += 1
            if verbose:
                print(f"  - {sha[:12]} {filename[:40]}: withheld, personal ID on the page")
            continue
        try:
            data = gmail.attachment_bytes(row["message_id"], row["attachment_id"])
            stats.fetched += 1
        except Exception as e:                                   # noqa: BLE001
            stats.errors += 1
            if verbose:
                print(f"  ! {sha[:12]}: {type(e).__name__} {str(e)[:100]}")
            continue
        try:
            put = store.put_document(sha, data, filename=filename, pii=pii,
                                     message_id=row["message_id"], load_id=row["load_id"],
                                     if_absent=not rewrite)
            ex_key = None
            if row["extraction_json"]:
                ex_key = store.put_extraction(sha, json.loads(row["extraction_json"]), pii=pii).key
        except Exception as e:                                   # noqa: BLE001
            stats.errors += 1
            if verbose:
                print(f"  ! {sha[:12]}: S3 {type(e).__name__} {str(e)[:100]}")
            continue
        db.mark_doc_archived(conn, sha, put.key, ex_key)
        stats.bytes_written += put.bytes_written
        if put.skipped:
            stats.skipped += 1
        else:
            stats.stored += 1
            stats.unchecked += pii is None
        if verbose:
            print(f"  {sha[:12]} {filename[:38]:40} -> {put.key}")

    # Pages stored unread and read since: put the reading beside them and replace `unchecked` with
    # the answer. A page found to carry a licence is re-tagged, not deleted - removing an object is
    # a retention decision for whoever owns the bucket, and the tag is what their rule acts on.
    for row in db.pending_extraction_archive(conn, limit=limit):
        sha = row["sha256"]
        pii = _is_pii(row["extraction_json"])
        try:
            ex_key = store.put_extraction(sha, json.loads(row["extraction_json"]), pii=pii).key
            store.retag(st.doc_key(sha), {"pii": st.pii_tag(pii)})
        except Exception as e:                                   # noqa: BLE001
            stats.errors += 1
            if verbose:
                print(f"  ! {sha[:12]}: re-tag {type(e).__name__} {str(e)[:100]}")
            continue
        db.mark_doc_archived(conn, sha, row["s3_key"], ex_key)
        stats.retagged += 1
    conn.commit()
    return stats
