"""The durable copy of what arrived: mail and documents in S3.

The ledger is the index and S3 is the archive. That split is deliberate - the ledger stays a 12 MB
file you can copy, query and back up, and the 2 GB of paper it describes lives where storage is
cheap and lifecycle rules can reach it.

WHAT THE KEYS ARE, AND WHY THEY ARE NOT UUIDS
---------------------------------------------
Both key schemes are content- or source-derived, never random, because every one of them is written
more than once and a random id would turn a re-run into a duplicate:

    mail/<yyyy>/<mm>/<dd>/<gmail-message-id>.json
        Gmail's message id is globally unique, immutable, and already the ledger's primary key for
        a message. Syncing the same message twice writes the same key twice - the second PUT is a
        no-op overwrite rather than a second copy. The date prefix is what makes S3 lifecycle rules
        and Athena partitioning possible later; it comes from internalDate, so it does not move when
        a message is re-fetched.

    doc/<sha256[:2]>/<sha256>
        The document, addressed by its content. This is the same hash the ledger de-duplicates on,
        and it has to stay that way: the same BOL forwarded through five replies is ONE object here
        exactly as it is one read. Keying documents by message id or a UUID would store it five
        times and quietly undo the de-duplication that has avoided 2,647 reads so far. The two-char
        shard keeps any one prefix from becoming a hotspot.

    doc/<sha256[:2]>/<sha256>.extraction.json
        What the reader made of that document. Beside the bytes on purpose, so a document and its
        reading cannot be separated by a lifecycle rule that only knows about prefixes.

IDEMPOTENCE
-----------
Every key is a pure function of what it holds, so archiving is safe to re-run and safe to interrupt.
The ledger records the key once the PUT succeeds, and `pending_*` only offers rows that have none -
so a resumed run uploads what is missing and nothing else.

PERSONAL ID
-----------
An object here outlives the process that made it. Until now the service read a driver's licence,
recorded "personal ID on the page" and dropped the bytes; writing them to S3 means Circle is
knowingly storing them, which is a retention decision rather than an engineering one. `pii` is
carried on the ledger row and stamped on the object as metadata and a tag, so a bucket policy or a
lifecycle rule can act on it without re-reading a single page. Whether those documents are stored
at all is `--skip-pii`, and the caller decides.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from dataclasses import dataclass
from typing import Any

# What a mail object holds. The raw RFC822 is the point of the archive - it is the only form that
# survives Gmail retention, mailbox moves and the account itself being closed - but the parsed
# envelope rides along so a reader (Athena, a person, a later pipeline) never has to re-parse MIME
# to answer "which load was this, and who sent it".
MAIL_PREFIX = "mail"
DOC_PREFIX = "doc"


def mail_key(message_id: str, internal_date: str | None) -> str:
    """mail/<yyyy>/<mm>/<dd>/<message-id>.json.gz - stable across re-syncs of the same message."""
    day = (internal_date or "")[:10]
    if len(day) != 10 or day[4] != "-":
        # A message the API gave no internalDate for still has to land somewhere findable. "unknown"
        # rather than today's date, because today's date would be a lie that moves on every re-run
        # and would break the one property the date prefix exists for.
        return f"{MAIL_PREFIX}/unknown/{message_id}.json.gz"
    return f"{MAIL_PREFIX}/{day[:4]}/{day[5:7]}/{day[8:10]}/{message_id}.json.gz"


def doc_key(sha256: str) -> str:
    """doc/<ab>/<sha256> - the document, addressed by its content."""
    return f"{DOC_PREFIX}/{sha256[:2]}/{sha256}"


def extraction_key(sha256: str) -> str:
    return f"{doc_key(sha256)}.extraction.json"


@dataclass(frozen=True)
class Stored:
    key: str
    bytes_written: int
    skipped: bool = False   # the object was already there; nothing was sent


class Store:
    """S3 for one bucket and prefix. Holds no ledger state - the caller records what it returns."""

    def __init__(self, bucket: str, prefix: str = "", client: Any = None, region: str | None = None):
        if not bucket:
            raise ValueError("a bucket is required")
        self.bucket = bucket
        # A prefix is normalised once, here, so every key builder can stay a pure function.
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        if client is not None:
            self.s3 = client
        else:
            import boto3
            self.s3 = boto3.session.Session(region_name=region).client("s3")

    # -- keys ---------------------------------------------------------------------------------
    def full(self, key: str) -> str:
        return self.prefix + key

    # -- writes -------------------------------------------------------------------------------
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream",
            metadata: dict[str, str] | None = None, tags: dict[str, str] | None = None,
            if_absent: bool = True) -> Stored:
        """Write one object. With if_absent, an object already at that key is left alone.

        if_absent is not an optimisation: these keys are content-derived, so an object already there
        has the same bytes, and re-PUTting it would cost a request and a new version for nothing.
        """
        k = self.full(key)
        if if_absent and self.exists(key):
            return Stored(k, 0, skipped=True)
        extra: dict[str, Any] = {"ContentType": content_type}
        if metadata:
            # S3 user metadata must be ASCII header-safe; a filename with an em-dash or a Cyrillic
            # character would otherwise fail the PUT itself rather than just being unreadable.
            extra["Metadata"] = {m_k: _header_safe(m_v) for m_k, m_v in metadata.items()}
        if tags:
            from urllib.parse import urlencode
            extra["Tagging"] = urlencode(tags)
        self.s3.put_object(Bucket=self.bucket, Key=k, Body=data, **extra)
        return Stored(k, len(data))

    def put_mail(self, *, message_id: str, internal_date: str | None, raw: bytes | None,
                 envelope: dict, attachments: list[dict] | None = None,
                 if_absent: bool = True) -> Stored:
        """One message: the raw RFC822 (gzipped), the envelope it was routed on, and a manifest
        naming every attachment it carried by the sha256 those documents are stored under.

        The manifest is what joins the two halves of this archive. Without it the link between a
        message and its documents lives only in the SQLite ledger: S3 on its own could not answer
        "which mail did this BOL arrive on", and an Athena query over the bucket would have to
        download every message and re-hash its MIME parts to find out. With it, both directions are
        a lookup - `attachments[].sha256` points at doc/, and each doc object names the message it
        was first seen on.

        It also records the parts the filters DROPPED, and why. Those are not in doc/ at all, so the
        manifest is the only place that says a message carried six images and the service kept one.
        """
        body = {
            "message_id": message_id,
            "internal_date": internal_date,
            "archived_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "envelope": envelope,
            "attachments": attachments or [],
            # base64 rather than a side object: one GET returns a complete, self-describing record,
            # and nothing can lose the half of it that says which load it belonged to.
            "raw_rfc822_b64": _b64(raw) if raw else None,
        }
        data = gzip.compress(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        return self.put(mail_key(message_id, internal_date), data,
                        content_type="application/json", metadata={"content-encoding": "gzip"},
                        if_absent=if_absent)

    def put_document(self, sha256: str, data: bytes, *, filename: str = "", message_id: str = "",
                     load_id: int | None = None, pii: bool = False, if_absent: bool = True) -> Stored:
        """One document, addressed by content, naming the message it was first seen on.

        first-seen rather than "the" message: the same file forwarded through five replies is one
        object here, so it has five messages and only one can go in the metadata. The full set is in
        the mail manifests, which is why both sides carry the link - this end answers "where did
        this come from" cheaply, and the mail end answers "what did this carry" completely.
        """
        meta = {"filename": filename, "sha256": sha256}
        if message_id:
            meta["first-seen-message"] = message_id
        if load_id:
            meta["load-id"] = str(load_id)
        return self.put(doc_key(sha256), data, metadata=meta,
                        tags={"pii": "true" if pii else "false"}, if_absent=if_absent)

    def put_extraction(self, sha256: str, extraction: dict, *, pii: bool = False) -> Stored:
        data = json.dumps(extraction, ensure_ascii=False).encode("utf-8")
        return self.put(extraction_key(sha256), data, content_type="application/json",
                        tags={"pii": "true" if pii else "false"}, if_absent=False)

    # -- reads --------------------------------------------------------------------------------
    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self.full(key))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def get(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=self.full(key))["Body"].read()

    def get_mail(self, message_id: str, internal_date: str | None) -> dict:
        return json.loads(gzip.decompress(self.get(mail_key(message_id, internal_date))))

    # -- checks -------------------------------------------------------------------------------
    def writable(self) -> tuple[bool, str]:
        """Can this process actually write here? Answered before a run, not on the first failure.

        A HeadBucket that succeeds only proves the bucket exists and is readable; SSO roles that can
        list every bucket in the account but write to none are common, and finding that out after
        4,000 messages have been fetched is the expensive way to learn it.
        """
        from botocore.exceptions import ClientError
        probe = self.full(".intake-write-probe")
        try:
            self.s3.put_object(Bucket=self.bucket, Key=probe, Body=b"ok")
            self.s3.delete_object(Bucket=self.bucket, Key=probe)
            return True, f"s3://{self.bucket}/{self.prefix} is writable"
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "?")
            return False, f"s3://{self.bucket}/{self.prefix}: {code} - {e.response.get('Error', {}).get('Message', '')[:120]}"


def from_env(client: Any = None) -> Store | None:
    """The configured store, or None when the archive is switched off.

    Off is a legitimate configuration and the default: without INTAKE_S3_BUCKET the service behaves
    exactly as it did before, reading documents and keeping only the hash and the extraction.
    """
    bucket = os.environ.get("INTAKE_S3_BUCKET", "").strip()
    if not bucket:
        return None
    return Store(bucket, os.environ.get("INTAKE_S3_PREFIX", "").strip(), client=client,
                 region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _header_safe(s: str) -> str:
    """S3 user metadata travels in HTTP headers: ASCII, no newlines, and short."""
    return "".join(c for c in (s or "") if 32 <= ord(c) < 127)[:900]
