r"""Offline checks for the intake ledger. No network, no model, no credentials.

Run:  .venv\Scripts\python.exe tests\test_intake.py

The cases are the ones that actually cost money or lose paperwork if they regress:
de-duplication across runs, the custody invariant, the thread-conflict rule, and idempotent replay.
Plain asserts rather than pytest, which is not in the project virtualenv.
"""
from __future__ import annotations

import base64
import hashlib
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from intake import db, filters, ingest, routing  # noqa: E402

PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        raise SystemExit(1)


# ---------------------------------------------------------------- fixtures ----

def png(width: int, height: int, filler: bytes = b"") -> bytes:
    """A PNG header the geometry filter can read, padded so it passes the size test."""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
            + filler.ljust(60_000, b"\x01"))


def message(mid: str, thread: str, subject: str, *, when_ms: int, frm: str = "driver@example.com",
            parts: list[tuple[str, bytes]] | None = None, snippet: str = "") -> dict:
    payload_parts = []
    for i, (filename, data) in enumerate(parts or []):
        payload_parts.append({"partId": str(i), "filename": filename, "mimeType": "image/png",
                              "body": {"attachmentId": f"att-{mid}-{i}", "size": len(data)}})
    return {"id": mid, "threadId": thread, "internalDate": str(when_ms), "snippet": snippet,
            "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}],
                        "parts": payload_parts}}


class FakeGmail:
    """Stands in for intake.gmail.Delegated. Records every call so the tests can assert on traffic."""

    subject = "bot@circledelivers.com"

    def __init__(self, messages: list[dict], blobs: dict[str, bytes]) -> None:
        self._messages = {m["id"]: m for m in messages}
        self._blobs = blobs
        self.calls = 0
        self.downloads: list[str] = []

    def profile(self) -> dict:
        return {"historyId": "1000"}

    def search(self, query: str, cap: int = 2000) -> list[dict]:
        return [{"id": m["id"], "threadId": m["threadId"]} for m in self._messages.values()]

    def history_since(self, start, label_id=None, max_pages=50):
        return [{"id": m["id"], "threadId": m["threadId"]} for m in self._messages.values()], "2000"

    def message(self, message_id: str, fmt: str = "full") -> dict:
        self.calls += 1
        return self._messages[message_id]

    def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        self.downloads.append(attachment_id)
        return self._blobs[attachment_id]


def fresh_db():
    path = Path(tempfile.mkdtemp(prefix="intake_test_")) / "ledger.sqlite3"
    return db.connect(path)


# ---------------------------------------------------------------- tests ----

def test_routing() -> None:
    print("routing")
    r = routing.resolve("RE: Load 2578456 POD attached", None, None)
    check("subject load number wins", r.load_id == 2578456 and r.tier == routing.TIER_SUBJECT)

    r = routing.resolve("RE: paperwork", None, 2578456)
    check("thread binding covers a subject with no number", r.load_id == 2578456 and r.tier == routing.TIER_THREAD)

    r = routing.resolve("RE: Load 2578999 new load", None, 2578456)
    check("a reply naming another load trusts the message", r.load_id == 2578999 and r.conflict)

    r = routing.resolve("RE: loads 2578456 and 2578999", None, None)
    check("an ambiguous subject does not guess", r.load_id is None and r.tier == routing.TIER_UNRESOLVED)

    r = routing.resolve("Fwd: docs", "BOL for 2578456 attached", None)
    check("body is the last free signal", r.load_id == 2578456)

    r = routing.resolve("RE: BOL 1077041869", None, None)
    check("a long reference number is not a load number", r.load_id is None)


def test_filters() -> None:
    print("filters")
    check("small part dropped", filters.metadata_decision("image001.png", 12_000) == filters.TOO_SMALL)
    check("rate con dropped", filters.metadata_decision("31236019_23.pdf", 120_000) == filters.RATE_CONFIRMATION)
    check("a real photo is kept", filters.metadata_decision("IMG_4434.jpeg", 900_000) == filters.KEEP)
    check("signature banner dropped", filters.geometry_decision(png(500, 150))[0] == filters.SIGNATURE_OR_LOGO)
    check("logo dropped", filters.geometry_decision(png(200, 200))[0] == filters.SIGNATURE_OR_LOGO)
    check("page kept", filters.geometry_decision(png(1200, 1600))[0] == filters.KEEP)
    check("pdf passes geometry", filters.geometry_decision(b"%PDF-1.4" + b"x" * 1000)[0] == filters.KEEP)


def test_dedup_and_custody() -> None:
    print("ingest: dedup, custody, replay")
    bol = png(1200, 1600, b"BOL")
    sig = png(500, 150, b"SIG")
    blobs = {}
    msgs = []
    # One BOL photo quoted through three replies, plus a signature graphic in every message -
    # the shape every real ratecon@ chain has.
    for i in range(3):
        mid = f"m{i}"
        blobs[f"att-{mid}-0"] = bol
        blobs[f"att-{mid}-1"] = sig
        msgs.append(message(mid, "t1", "RE: Load 2578456 paperwork", when_ms=1_700_000_000_000 + i,
                            parts=[("BOL.png", bol), ("signature.png", sig)]))
    # A message no tier can route.
    msgs.append(message("m9", "t9", "RE: delivery note", when_ms=1_700_000_000_100,
                        parts=[("AIRLINE_NOTE.png", bol)]))
    blobs["att-m9-0"] = bol

    conn = fresh_db()
    fake = FakeGmail(msgs, blobs)
    reads: list[str] = []

    def reader(data: bytes, filename: str):
        reads.append(hashlib.sha256(data).hexdigest()[:12])
        return ({"document_type": "bill_of_lading"}, "bill_of_lading", "fake-model", 0.046)

    st = ingest.sync_once(conn, fake, group="ratecon@circledelivers.com", reader=reader)
    check("all four messages ingested", st.fetched == 4, f"got {st.fetched}")
    check("three bound to the load", st.bound == 3, f"got {st.bound}")
    check("one unresolved, not dropped", st.unresolved == 1, f"got {st.unresolved}")
    check("signature graphics dropped without a read", st.parts[filters.SIGNATURE_OR_LOGO] == 3)
    check("the same photo was read once", len(reads) == 1, f"reads={reads}")
    check("two later copies avoided a read", st.reads_avoided == 2, f"got {st.reads_avoided}")
    check("the unresolved message's attachment is listed, not downloaded",
          st.parts[filters.PENDING] == 1, f"got {st.parts[filters.PENDING]}")
    check("spend is one document", abs(st.cost_usd - 0.046) < 1e-9, f"got {st.cost_usd}")

    c = db.counts(conn)
    check("custody balances", c["custody_gap"] == 0, str(c))
    check("occurrences exceed unique files", c["attachment_occurrences"] == 3 and c["unique_files"] == 1, str(c))
    check("load row created from the mail side", c["loads"] == 1)

    # Replay: the whole batch again. This is what a crashed run or an at-least-once push delivery does.
    before_downloads = len(fake.downloads)
    st2 = ingest.sync_once(conn, fake, group="ratecon@circledelivers.com", reader=reader)
    check("replay fetches nothing", st2.fetched == 0 and st2.already_seen == 4, st2.line())
    check("replay downloads nothing", len(fake.downloads) == before_downloads)
    check("replay costs nothing", len(reads) == 1 and st2.cost_usd == 0.0)
    check("custody still balances after replay", db.counts(conn)["custody_gap"] == 0)


def test_cursor_written_last() -> None:
    print("ingest: cursor safety")
    blob = png(1200, 1600, b"X")
    msgs = [message("x1", "t1", "RE: Load 2578456", when_ms=1_700_000_000_000, parts=[("a.png", blob)])]
    conn = fresh_db()
    fake = FakeGmail(msgs, {"att-x1-0": blob})

    # A Gmail failure, not a reader failure: an unreadable document is isolated per file (see
    # test_one_bad_file_does_not_stop_the_batch), but losing the transport must abort the pass so
    # the cursor does not skip past work that never happened.
    def dead(message_id, attachment_id):
        raise ingest.gm.GmailError(503, "/attachments", "backend unavailable")

    fake.attachment_bytes = dead
    try:
        ingest.sync_once(conn, fake, group="g", reader=None)
    except ingest.gm.GmailError:
        pass
    else:
        check("an unrecoverable Gmail error propagates", False)
    check("cursor untouched after a failed batch", db.get_cursor(conn, fake.subject) is None)
    check("no half-written message row", db.counts(conn)["messages_seen"] == 0)

    fake = FakeGmail(msgs, {"att-x1-0": blob})
    st = ingest.sync_once(conn, fake, group="g", reader=None)
    check("the retry succeeds", st.fetched == 1 and st.bound == 1)
    # A first run seeds from profile(), read BEFORE the search so anything arriving between the two
    # calls still lands after the cursor and is picked up next pass.
    check("first run seeds the cursor from the profile", db.get_cursor(conn, fake.subject) == "1000")

    st = ingest.sync_once(conn, fake, group="g", reader=None)
    check("later runs use the history cursor", st.mode == "history", st.mode)
    check("cursor advances to the history head", db.get_cursor(conn, fake.subject) == "2000")


def test_one_bad_file_does_not_stop_the_batch() -> None:
    print("ingest: a failed read is isolated and retried")
    good, bad = png(1200, 1600, b"GOOD"), png(1100, 1500, b"BAD")
    msgs = [message("b1", "t1", "RE: Load 2578456", when_ms=1_700_000_000_000,
                    parts=[("good.png", good), ("broken.heic", bad)])]
    blobs = {"att-b1-0": good, "att-b1-1": bad}
    conn = fresh_db()
    fake = FakeGmail(msgs, blobs)
    attempts: list[str] = []

    def flaky(data: bytes, filename: str):
        attempts.append(filename)
        if filename == "broken.heic":
            raise RuntimeError("cannot open HEIC")
        return ({"document_type": "bill_of_lading"}, "bill_of_lading", "fake-model", 0.046)

    st = ingest.sync_once(conn, fake, group="g", reader=flaky)
    check("the good file was still read", st.reads == 1, st.line())
    check("the failure was counted, not raised", st.read_errors == 1, st.line())
    check("the message still committed", db.counts(conn)["messages_seen"] == 1)
    check("custody balances despite the failure", db.counts(conn)["custody_gap"] == 0)
    check("the failure is recorded against the hash", db.counts(conn)["read_failures"] == 1,
          str(db.counts(conn)))

    # A later pass must retry the failed file and only that file. Simulate the fix landing.
    def fixed(data: bytes, filename: str):
        attempts.append(filename)
        return ({"document_type": "proof_of_delivery"}, "proof_of_delivery", "fake-model", 0.046)

    msgs.append(message("b2", "t1", "RE: Load 2578456", when_ms=1_700_000_000_500,
                        parts=[("good.png", good), ("broken.heic", bad)]))
    blobs["att-b2-0"], blobs["att-b2-1"] = good, bad
    fake2 = FakeGmail(msgs, blobs)
    st2 = ingest.sync_once(conn, fake2, group="g", reader=fixed)
    check("only the previously failed file is re-read", st2.reads == 1, st2.line())
    check("the good file was not paid for twice", st2.reads_avoided == 1, st2.line())
    check("no failures left", db.counts(conn)["read_failures"] == 0, str(db.counts(conn)))
    check("both files now carry a reading", db.counts(conn)["files_read"] == 2, str(db.counts(conn)))


if __name__ == "__main__":
    test_routing()
    test_filters()
    test_dedup_and_custody()
    test_cursor_written_last()
    test_one_bad_file_does_not_stop_the_batch()
    print(f"\n{PASSED} checks passed")
