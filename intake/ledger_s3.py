"""The ledger's home when the service runs in AWS: one SQLite file in S3, one writer at a time.

The worker has no disk that outlives it, so each run downloads the ledger to /tmp, works on it and
uploads it. Two things keep that from ever losing work:

    The upload is conditional on the ETag the run downloaded (S3 If-Match). If anything replaced the
    ledger in between, the upload is refused and the run fails loudly instead of overwriting it.
    Reserved concurrency of 1 means that should never happen; the condition is what makes "should"
    safe to rely on.

    A run that fails - killed, refused on upload, TransportPro down - costs the next run nothing but
    a redo: nothing it did is visible until the upload succeeds, and every step is resumable.

Before the first upload of each UTC day the ledger is copied to ledger/snapshots/, because the bucket
has no versioning and this file is the index to everything else in it.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

LEDGER_KEY = "ledger/intake.sqlite3"


class LedgerChanged(RuntimeError):
    """Something replaced the ledger while this run held it. Its work is discarded, not merged."""


def take(s3, bucket: str, key: str, path: Path) -> str:
    """Download the ledger and return the ETag it was downloaded at.

    A missing ledger is an error, never an empty start. A fresh ledger has no load history and no
    reading history, so it would re-check every load as new and re-read every document - and then
    upload itself over nothing, replacing the index to everything in the bucket.
    """
    obj = s3.get_object(Bucket=bucket, Key=key)
    path.write_bytes(obj["Body"].read())
    return obj["ETag"]


def snapshot(s3, bucket: str, key: str, etag: str, today: str | None = None) -> str | None:
    """Copy the ledger to ledger/snapshots/intake-<day>.sqlite3, once per UTC day. Returns the key
    written, or None when today's snapshot already exists."""
    from botocore.exceptions import ClientError
    day = today or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    stem = key.rsplit("/", 1)
    snap = f"{stem[0]}/snapshots/intake-{day}.sqlite3" if len(stem) == 2 else f"snapshots/intake-{day}.sqlite3"
    try:
        s3.head_object(Bucket=bucket, Key=snap)
        return None
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise
    s3.copy_object(Bucket=bucket, Key=snap, CopySource={"Bucket": bucket, "Key": key},
                   CopySourceIfMatch=etag)
    return snap


def give_back(s3, bucket: str, key: str, conn, path: Path, etag: str) -> None:
    """Upload the ledger, but only over the version this run downloaded.

    The WAL is folded back into the main file first: the upload is one file, and anything left in
    intake.sqlite3-wal would be committed work that silently did not travel. A ledger that fails its
    own integrity check is not uploaded at all - the copy in S3 is older, but it is whole.
    """
    from botocore.exceptions import ClientError
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
    conn.close()
    if verdict != "ok":
        raise RuntimeError(f"ledger failed its integrity check ({verdict}); the S3 copy was left as it was")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=path.read_bytes(), IfMatch=etag,
                      ContentType="application/vnd.sqlite3")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict", "412", "409"):
            raise LedgerChanged(f"s3://{bucket}/{key} changed while this run held it; this run's "
                                f"changes were NOT uploaded, and the next run redoes them") from None
        raise


def seed(s3, bucket: str, key: str, path: Path) -> None:
    """Upload a local ledger as the live one - only where none exists yet."""
    import sqlite3
    from botocore.exceptions import ClientError
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
    conn.close()
    if verdict != "ok":
        raise RuntimeError(f"{path} failed its integrity check: {verdict}")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=path.read_bytes(), IfNoneMatch="*",
                      ContentType="application/vnd.sqlite3")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "412"):
            raise LedgerChanged(f"s3://{bucket}/{key} already exists and is the live ledger; "
                                f"it was not replaced") from None
        raise
