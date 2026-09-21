r"""Offline checks for the intake ledger. No network, no model, no credentials.

Run:  .venv\Scripts\python.exe tests\test_intake.py

The cases are the ones that actually cost money or lose paperwork if they regress:
de-duplication across runs, the custody invariant, the thread-conflict rule, and idempotent replay.
Plain asserts rather than pytest, which is not in the project virtualenv.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from intake import db, filing, filters, ingest, loadloop, review, routing, state  # noqa: E402

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


def _extraction(doc_type: str = "bill_of_lading", **over) -> dict:
    """A minimal valid extraction with NO photo_stamp key - exactly the shape of every row written
    before the field existed, which is what makes it the right fixture for the default."""
    d = {"document_type": doc_type, "document_type_confidence": 0.95, "numbers": [],
         "shipper": {}, "consignee": {},
         "signatures": {"shipper_signed": True, "driver_signed": True,
                        "receiver_signed": False, "stamp_present": False},
         "times": {"source": "none"}, "pages": [], "notes": ""}
    d.update(over)
    return d


def test_photo_stamp() -> None:
    """A camera overlay is the only evidence of where paperwork was photographed: load 2574983's
    BOL photo carries no Exif at all - JFIF and an ICC profile, nothing else, because the scanning
    apps strip it - while the camera's own text sits in the pixels naming the shipper's street."""
    print("photo stamp: where the paperwork was photographed")
    from pod_intake.reader import _output_format
    from pod_intake.requirements import check_document
    from pod_intake.schema import Extraction, PhotoStamp, reader_json_schema

    check("an extraction written before the field existed still validates",
          Extraction.model_validate(_extraction()).photo_stamp.present is False)
    for label, sch in (("structured output", _output_format(Extraction)["schema"]),
                       ("prompt fallback", reader_json_schema(Extraction))):
        check(f"the model must answer present ({label})",
              "present" in sch["$defs"]["PhotoStamp"]["required"], str(sch["$defs"]["PhotoStamp"]))

    rules = {"customer": "Kalustyan Corporation", "bol_before_leaving_shipper": True}
    load = {"dispatch_status": "Loaded"}

    def timing(stamp):
        d = _extraction()
        d["shipper"] = {"name": "Kalustyan Corp.", "city": "Kenilworth", "state": "NJ"}
        if stamp is not None:
            d["photo_stamp"] = stamp
        v = check_document(Extraction.model_validate(d), "Bill Of Lading", rules, load, set())
        return next((r for r in v.results if r.rule == "bol_timing"), None)

    check("no overlay leaves the rule unevaluated, as before", timing(None) is None)
    check("and so does a photo with no stamp on it",
          timing({"present": False}) is None)
    at_shipper = timing({"present": True, "place": "251 South 31st Street, Kenilworth, New Jersey",
                         "date": "16 Sep 2026", "time": "11:27:08 AM"})
    check("a stamp naming the shipper's city PASSES the rule on evidence",
          at_shipper is not None and at_shipper.status == "pass", str(at_shipper))
    check("and it quotes where and when", at_shipper and "11:27" in at_shipper.detail, str(at_shipper))
    elsewhere = timing({"present": True, "place": "Elyria, Ohio", "date": "17 Sep 2026"})
    check("a stamp naming somewhere else is unknown, never a failure",
          elsewhere is not None and elsewhere.status == "unknown", str(elsewhere))


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


def test_max_defers_it_does_not_drop() -> None:
    print("ingest: a cap on work is not a cap on coverage")
    blob = png(1200, 1600, b"X")
    msgs = [message(f"c{i}", f"t{i}", f"RE: Load 257800{i} paperwork", when_ms=1_700_000_000_000 + i,
                    parts=[(f"doc{i}.png", blob)]) for i in range(5)]
    blobs = {f"att-c{i}-0": blob for i in range(5)}
    conn = fresh_db()
    fake = FakeGmail(msgs, blobs)

    st = ingest.sync_once(conn, fake, group="g", reader=None, max_messages=2)
    check("only the capped number is processed", st.fetched == 2, st.line())
    check("the rest are counted as deferred", st.deferred == 3, st.line())
    # The whole point: advancing the cursor here would skip those three forever, and custody could
    # not catch it because they would never get a message row.
    check("the cursor is HELD while work is outstanding", db.get_cursor(conn, fake.subject) is None,
          str(db.get_cursor(conn, fake.subject)))

    st = ingest.sync_once(conn, fake, group="g", reader=None, max_messages=2)
    check("the next pass takes the NEXT two, not the same two", st.fetched == 2, st.line())
    check("the done ones are counted but do not consume the budget", st.already_seen == 2, st.line())

    st = ingest.sync_once(conn, fake, group="g", reader=None, max_messages=2)
    check("the last one lands", st.fetched == 1 and st.deferred == 0, st.line())
    check("only now does the cursor advance", db.get_cursor(conn, fake.subject) is not None)
    check("all five are in the ledger", db.counts(conn)["messages_seen"] == 5)
    check("custody balances", db.counts(conn)["custody_gap"] == 0)


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


# ---------------------------------------------------------------- load loop ----

def tp_load(stage_status="Dispatched", doc_status="Waiting for Documents", load_status="Dispatched",
            levels=("Priority / OP8",), customer="Acme Foods"):
    return {"status": {"loadStatus": load_status, "documentStatus": doc_status},
            "assignedTerminal": 1088,
            "billingInfo": {"customer": {"companyName": customer}},
            "waypoints": [{"type": "SH", "reference": [{"type": "SERVICE_LEVEL", "value": v} for v in levels]}]}


def tp_file(type_id, when="2026-09-15T10:00:00Z", comment=""):
    return {"fileTypeId": type_id, "fileTypeName": state.BOL_TYPES.get(type_id) or state.POD_TYPES.get(type_id),
            "dateCreated": when, "comments": comment}


def test_state_machine() -> None:
    print("state machine")
    a = state.assess(1, tp_load(), [{"id": 9, "status": "Dispatched"}], [])
    check("planned/dispatched load is not yet due", a["state"] == "not_yet_due", a["state"])
    due = state.utc(a["next_check_at"]) - dt.datetime.now(dt.timezone.utc)
    check("its next check is ~6 h out, not minutes", 5.5 * 3600 < due.total_seconds() < 6.5 * 3600,
          f"{due}")

    a = state.assess(1, tp_load(), [{"id": 9, "status": "Loaded"}], [], ledger_docs=2, ledger_unread=2)
    check("loaded with nothing filed wants the BOL", a["state"] == "bol_expected", a["state"])
    check("it names the documents waiting in the thread", "2 document(s)" in a["action"], a["action"])

    a = state.assess(1, tp_load(), [{"id": 9, "status": "At Consignee"}], [])
    check("at consignee it wants the POD", a["state"] == "pod_expected", a["state"])
    check("and says nothing is anywhere", "ask the driver" in a["action"], a["action"])

    # Cancelled dispatch first: readiness.py read the wrong status off load 2576660 this way.
    a = state.assess(1, tp_load(), [{"id": 1, "status": "Canceled"}, {"id": 2, "status": "Delivered"}], [])
    check("a cancelled dispatch does not decide the stage", a["stage"] == "delivered", a["stage"])

    # Reps file a signed POD under "Bill Of Lading" with the comment "POD" (loads 2577037, 2577850).
    a = state.assess(1, tp_load(), [{"id": 9, "status": "Delivered"}], [tp_file(12, comment="POD signed")])
    check("a BOL-typed file commented POD counts as the POD", a["state"] == "filed_status_pending", a["state"])

    a = state.assess(1, tp_load(doc_status="Documents Received"), [{"id": 9, "status": "Delivered"}],
                     [tp_file(360)])
    check("documents received is terminal", a["state"] == "complete")

    # Load 2580687, 18 Sep 2026: a rep filed the pickup BOL as type 12 with the comment "POD", the
    # status cleared, billing opened, and the page carries the shipper's and the carrier's signatures
    # and nothing from the consignee. "Documents Received" is TransportPro agreeing with the comment,
    # not with the paper, so where the service has READ the paper it has to be able to disagree.
    delivered = [{"id": 9, "status": "Delivered"}]
    received = tp_load(doc_status="Documents Received")
    claimed = [tp_file(12, comment="POD")]

    a = state.assess(1, received, delivered, claimed,
                     pod_claims={"claimed": 1, "verified": 0, "unsigned": 1, "unread": 0,
                                 "unsigned_file": 31334276})
    check("a POD claim the page contradicts is not complete", a["state"] == "pod_unsigned", a["state"])
    check("and it says billing will reject it", "Billing will reject" in a["action"], a["action"])
    check("and it names the file so a person can open it", "31334276" in a["action"], a["action"])
    due = state.utc(a["next_check_at"]) - dt.datetime.now(dt.timezone.utc)
    check("it is re-checked daily, not never", 23 * 3600 < due.total_seconds() < 25 * 3600, f"{due}")

    a = state.assess(1, received, delivered, claimed,
                     pod_claims={"claimed": 1, "verified": 0, "unsigned": 0, "unread": 1,
                                 "unsigned_file": None})
    check("a POD claim nobody has read is not complete either", a["state"] == "pod_unverified", a["state"])
    check("and it says how to settle it", "tpro-scan --read" in a["action"], a["action"])

    a = state.assess(1, received, delivered, claimed,
                     pod_claims={"claimed": 1, "verified": 1, "unsigned": 0, "unread": 0,
                                 "unsigned_file": None})
    check("a POD the page backs up IS complete", a["state"] == "complete", a["state"])

    a = state.assess(1, received, delivered, [tp_file(360)])
    check("no claim recorded yet still reads complete", a["state"] == "complete", a["state"])

    # The mirror of all that, from load 2576408 on 21 Sep 2026. Its receiver-signed POD - Joseph
    # Jarcimillo, 09/15/26 6:14 PM - sits on the load as a Driver Supplied BOL commented "Driver
    # Supplied Image", so NOTHING claims it is a POD. Over-claiming was caught; under-claiming left
    # a load reading as short a POD while the signed POD was on it.
    unclaimed = {"claimed": 0, "verified": 0, "unsigned": 0, "unread": 0, "unsigned_file": None,
                 "unclaimed": 1, "unclaimed_file": 31294705, "unclaimed_by": "Joseph Jarcimillo"}

    a = state.assess(1, tp_load(), [{"id": 9, "status": "Delivered"}], [tp_file(363, comment="Driver Supplied Image")],
                     pod_claims=unclaimed)
    check("a POD filed under a non-clearing type is not just wrong_doc_type",
          a["state"] == "pod_mislabelled", a["state"])
    check("and the action names the file to re-file", "31294705" in a["action"], a["action"])
    check("and who signed it", "Joseph Jarcimillo" in a["action"], a["action"])

    a = state.assess(1, tp_load(), [{"id": 9, "status": "At Consignee"}], [], pod_claims=unclaimed)
    check("a load 'short a POD' that already has one is not short a POD",
          a["state"] == "pod_mislabelled", a["state"])
    check("and it says not to chase the driver", "not chase the driver" in a["action"], a["action"])

    a = state.assess(1, tp_load(), [{"id": 9, "status": "At Consignee"}], [])
    check("without the evidence it still asks for the POD, as before",
          a["state"] == "pod_expected", a["state"])

    a = state.assess(1, received, delivered, [tp_file(360)])
    check("a complete load is never polled again", a["next_check_at"] is None)

    a = state.assess(1, tp_load(levels=("Flexible / FCFS",)), [{"id": 9, "status": "Loaded"}], [],
                     scope_levels={"priority / op8"})
    check("another service level is out of scope", a["state"] == "out_of_scope", a["state"])
    check("but it is still re-checked daily", a["next_check_at"] is not None)

    # Measured 15 Sep 2026 over 266 loads: type 363 Driver Supplied BOL does not clear the status;
    # type 12 does. Timing (the old OQ-3 theory) separates nothing.
    a = state.assess(1, tp_load(), [{"id": 9, "status": "Delivered", "lastUpdated": "2026-09-15T12:00:00Z"}],
                     [tp_file(363, when="2026-09-15T09:00:00Z")])
    check("a load whose only paperwork is Driver Supplied BOL is the wrong-type state",
          a["state"] == "wrong_doc_type", a["state"])
    check("and the action says re-filing it properly clears the status",
          "re-file it as Bill Of Lading" in a["action"], a["action"])

    a = state.assess(1, tp_load(), [{"id": 9, "status": "Delivered", "lastUpdated": "2026-09-15T12:00:00Z"}],
                     [tp_file(12, when="2026-09-15T09:00:00Z")])
    check("a clearing type filed before the Delivered mark is NOT called wrong-type",
          a["state"] == "filed_status_pending", a["state"])

    check("the POD window is the tightest cadence",
          state.CADENCE_MINUTES["pod_expected"] < state.CADENCE_MINUTES["bol_expected"]
          < state.CADENCE_MINUTES["not_yet_due"])


class FakeTPro:
    calls = 0

    def __init__(self, loads: dict, fail: set | None = None) -> None:
        self._loads = loads
        self._fail = fail or set()

    def load(self, load_id):
        FakeTPro.calls += 1
        if load_id in self._fail:
            from intake.tpro import TProError
            raise TProError(503, f"/load/{load_id}", "backend unavailable")
        return self._loads[load_id]["load"]

    def dispatches(self, load_id):
        return self._loads[load_id].get("dispatches", [])

    def files(self, load_id):
        return self._loads[load_id].get("files", [])


def test_drain_never_drops_a_load() -> None:
    print("load loop: drain, cadence, deferral")
    conn = fresh_db()
    loads = {}
    for i in range(5):
        lid = 2578000 + i
        loads[lid] = {"load": tp_load(), "dispatches": [{"id": i, "status": "At Consignee"}], "files": []}
        db.upsert_load(conn, lid, source="dashboard", due_now=True)
        db.mark_in_view(conn, lid)          # reconcile is what puts a load in scope
    # One load the API cannot serve this pass.
    fake = FakeTPro(loads, fail={2578003})

    ds = loadloop.drain(conn, fake, limit=100)
    check("every due load was checked or deferred", ds.checked + ds.errors == 5, ds.line())
    check("the failing load was deferred, not dropped", ds.errors == 1, ds.line())
    check("the ledger still holds all five", db.counts(conn)["loads"] == 5)
    row = conn.execute("SELECT * FROM load WHERE load_id=2578003").fetchone()
    check("the deferred load keeps a next check", row["next_check_at"] is not None)
    check("and records why", "503" in (row["last_error"] or ""), str(row["last_error"]))

    # The cadence must have pushed the healthy loads into the future, so a second drain is a no-op.
    ds2 = loadloop.drain(conn, fake, limit=100)
    check("checked loads are not re-polled immediately", ds2.checked == 0, ds2.line())

    # The limit delays work; it must never discard it. This is the --max bug the design removes.
    for lid in loads:
        conn.execute("UPDATE load SET next_check_at=? WHERE load_id=?", (db.now_iso(), lid))
    ds3 = loadloop.drain(conn, fake, limit=2)
    check("a limit checks only some", ds3.checked + ds3.errors == 2, ds3.line())
    check("the rest stay due, not dropped", db.counts(conn)["loads_overdue"] == 3,
          str(db.counts(conn)["loads_overdue"]))


def test_drain_reads_evidence_from_the_ledger() -> None:
    print("load loop: evidence comes from the ledger, not Gmail")
    conn = fresh_db()
    blob = png(1200, 1600, b"BOL")
    msgs = [message("e1", "t1", "RE: Load 2578456 BOL", when_ms=1_700_000_000_000, parts=[("BOL.png", blob)])]
    ingest.sync_once(conn, FakeGmail(msgs, {"att-e1-0": blob}), group="g", reader=None)
    check("Loop A created the load row", db.counts(conn)["loads"] == 1)

    db.mark_in_view(conn, 2578456)      # a mail-created load is only worked once the sweep sees it
    fake = FakeTPro({2578456: {"load": tp_load(), "dispatches": [{"id": 1, "status": "Loaded"}], "files": []}})
    loadloop.drain(conn, fake, limit=10)
    row = conn.execute("SELECT * FROM load WHERE load_id=2578456").fetchone()
    check("the load loop sees the document Loop A recorded",
          "1 document(s)" in (row["action"] or ""), str(row["action"]))
    check("and flags it as unread", "not read yet" in (row["action"] or ""), str(row["action"]))
    check("state is bol_expected", row["state"] == "bol_expected", str(row["state"]))


# ---------------------------------------------------------------- filing gates ----

def seed_document(conn, *, load_id=2578456, stage="delivered", doc_type="proof_of_delivery",
                  notes="", filename="POD.png", tier="subject", conflict=0):
    """One read document on one load, as Loop A + Loop B would have left it."""
    blob = png(1200, 1600, filename.encode()[:4])
    msgs = [message("f1", "t1", f"RE: Load {load_id} paperwork", when_ms=1_700_000_000_000,
                    parts=[(filename, blob)])]
    ingest.sync_once(conn, FakeGmail(msgs, {"att-f1-0": blob}), group="g", reader=None)
    sha = conn.execute("SELECT sha256 FROM part WHERE decision='keep'").fetchone()["sha256"]
    extraction = {
        "document_type": doc_type, "document_type_confidence": 0.95, "numbers": [],
        "shipper": {}, "consignee": {},
        "signatures": {"shipper_signed": False, "driver_signed": True,
                       "receiver_signed": True, "stamp_present": False},
        "times": {"source": "none"}, "pages": [], "notes": notes,
    }
    db.put_attachment(conn, sha, message_id="f1", filename=filename, size=1000,
                      extraction=extraction, document_type=doc_type, model="fake", cost_usd=0.046)
    conn.execute("UPDATE load SET stage=?, state='pod_expected', customer='Acme' WHERE load_id=?",
                 (stage, load_id))
    conn.execute("UPDATE message SET routing_tier=? WHERE message_id='f1'", (tier,))
    conn.execute("UPDATE thread SET conflict_flag=? WHERE thread_id='t1'", (conflict,))
    return sha


def test_filing_gates() -> None:
    print("filing gates")
    conn = fresh_db()
    sha = seed_document(conn, notes="photo of the driver's license", filename="license.jpg")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a driver's licence is blocked", p.gate == filing.BLOCK and p.kind == review.PII, p.reason)
    check("and no document type is proposed for it", p.document_type is None)

    conn = fresh_db()
    sha = seed_document(conn, doc_type="other", notes="email signature graphic")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a non-document is blocked", p.gate == filing.BLOCK and p.kind == review.NOT_A_DOCUMENT, p.reason)

    # A POD at the consignee is no longer withheld. The 15 Sep 2026 measurement refuted the timing
    # theory that justified holding it, and classify_type already refuses to call anything a POD
    # before the truck reaches the consignee - which is the rule that actually protects correctness.
    conn = fresh_db()
    sha = seed_document(conn, stage="at consignee")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a POD at the consignee is no longer held for timing", p.gate != filing.HOLD, f"{p.gate} {p.reason}")

    # The rule that does the protecting: before the consignee, the same page is pickup paperwork.
    conn = fresh_db()
    sha = seed_document(conn, stage="loaded")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("before the consignee it can never be typed a POD",
          p.document_type == "Bill Of Lading", str(p.document_type))

    conn = fresh_db()
    sha = seed_document(conn, stage="delivered")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a POD on a delivered load is auto-gated", p.gate == filing.AUTO, f"{p.gate} {p.reason}")
    check("its type is recomputed, not cached", p.document_type == "Proof of Delivery", str(p.document_type))
    check("the comment names the load", str(2578456) in (p.comment or ""), str(p.comment))

    # Shadow mode is the default: the same document must NOT be auto-gated without allow_auto.
    p2 = filing.propose(conn, 2578456, sha, allow_auto=False)
    check("shadow mode sends even a clean filing to review", p2.gate == filing.REVIEW, p2.gate)
    check("and says what it would have done", p2.kind == review.SHADOW and "would file" in p2.reason, p2.reason)

    conn = fresh_db()
    sha = seed_document(conn, stage="delivered", tier="thread")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a weaker routing tier needs a person",
          p.gate == filing.REVIEW and p.kind == review.LOW_CONFIDENCE, f"{p.gate} {p.kind}")

    conn = fresh_db()
    sha = seed_document(conn, stage="delivered", conflict=1)
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a contested thread binding needs a person", p.kind == review.CONFLICT, str(p.kind))

    # The stage decides the type: the same bytes on a loaded truck are pickup paperwork.
    conn = fresh_db()
    sha = seed_document(conn, stage="loaded")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("the same document on a loaded truck files as a BOL",
          p.document_type == "Bill Of Lading", str(p.document_type))


def test_execute_is_off_unless_asked() -> None:
    print("the write path: dry run is the default")
    conn = fresh_db()
    sha = seed_document(conn, stage="delivered")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)

    class RefuseToBeCalled:
        def upload_file(self, **kw):
            raise AssertionError("upload_file must never run during a dry run")

    out = filing.execute(conn, RefuseToBeCalled(), None, p)          # dry_run defaults to True
    check("execute is a dry run by default", out["dry_run"] and not out["filed"], str(out))
    check("but it says exactly what it would send",
          out["would"]["recordType"] == "Loads" and out["would"]["recordId"] == 2578456
          and out["would"]["documentType"] == "Proof of Delivery", str(out["would"]))
    check("nothing was recorded as filed", db.counts(conn)["filings"] == 0)

    blocked = filing.propose(conn, 2578456, sha, allow_auto=True)
    blocked.gate = filing.BLOCK
    out = filing.execute(conn, RefuseToBeCalled(), None, blocked, dry_run=False)
    check("a blocked proposal never uploads", not out["filed"] and "block" in out["why"], str(out))


def test_execute_files_once_and_refetches() -> None:
    print("the write path: upload, idempotency, re-fetch")
    conn = fresh_db()
    blob = png(1200, 1600, b"POD")
    msgs = [message("f1", "t1", "RE: Load 2578456 POD", when_ms=1_700_000_000_000, parts=[("POD.png", blob)])]
    gfake = FakeGmail(msgs, {"att-f1-0": blob})
    ingest.sync_once(conn, gfake, group="g", reader=None)
    sha = conn.execute("SELECT sha256 FROM part WHERE decision='keep'").fetchone()["sha256"]
    db.put_attachment(conn, sha, message_id="f1", filename="POD.png", size=len(blob),
                      extraction={"document_type": "proof_of_delivery", "document_type_confidence": 0.9,
                                  "numbers": [], "shipper": {}, "consignee": {},
                                  "signatures": {"shipper_signed": False, "driver_signed": True,
                                                 "receiver_signed": True, "stamp_present": False},
                                  "times": {"source": "none"}, "pages": [], "notes": ""},
                      document_type="proof_of_delivery", model="fake", cost_usd=0.046)
    conn.execute("UPDATE load SET stage='delivered', customer='Acme' WHERE load_id=2578456")

    uploads = []

    class FakeTPWriter:
        def upload_file(self, **kw):
            uploads.append(kw)
            return {"id": 987654}

    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    out = filing.execute(conn, FakeTPWriter(), gfake, p, dry_run=False)
    check("it filed", out["filed"] and out["tpro_file_id"] == "987654", str(out))
    check("the bytes were re-fetched from Gmail, not stored", len(gfake.downloads) >= 2, str(gfake.downloads))
    check("the upload used the documented field names",
          uploads[0]["record_type"] == "Loads" and uploads[0]["record_id"] == 2578456
          and uploads[0]["document_type"] == "Proof of Delivery", str(uploads[0]))
    check("the load is re-checked immediately after filing",
          conn.execute("SELECT next_check_at FROM load WHERE load_id=2578456").fetchone()[0] is not None)

    out2 = filing.execute(conn, FakeTPWriter(), gfake, p, dry_run=False)
    check("a second attempt does not double-file",
          not out2["filed"] and "already filed" in out2["why"], str(out2))
    check("exactly one upload happened", len(uploads) == 1, str(len(uploads)))


def test_refetch_verifies_the_bytes() -> None:
    print("the write path: the hash is the document's identity")
    conn = fresh_db()
    blob = png(1200, 1600, b"POD")
    msgs = [message("f1", "t1", "RE: Load 2578456 POD", when_ms=1_700_000_000_000, parts=[("POD.png", blob)])]
    gfake = FakeGmail(msgs, {"att-f1-0": blob})
    ingest.sync_once(conn, gfake, group="g", reader=None)
    sha = conn.execute("SELECT sha256 FROM part WHERE decision='keep'").fetchone()["sha256"]
    # Gmail hands back different bytes than the ones that were read and judged.
    gfake._blobs["att-f1-0"] = png(1200, 1600, b"DIFF")
    try:
        filing.fetch_bytes(conn, gfake, sha)
    except RuntimeError as e:
        check("mismatched bytes are refused", "do not match" in str(e), str(e))
    else:
        check("mismatched bytes are refused", False)


def test_review_queue() -> None:
    print("review queue")
    conn = fresh_db()
    sha = seed_document(conn, stage="at consignee")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    for _ in range(2):
        review.enqueue(conn, load_id=p.load_id, sha256=p.sha256, message_id=None, kind=p.kind,
                       reason=p.reason, proposed_type=p.document_type, proposed_comment=p.comment)
    check("enqueue is idempotent", review.counts(conn)["_pending"] == 1, str(review.counts(conn)))

    item = review.pending(conn)[0]
    check("the queue carries the proposed filing",
          item["proposed_type"] == "Proof of Delivery", str(item["proposed_type"]))
    check("and a kind, never NULL - a null kind defeats the UNIQUE de-duplication",
          bool(item["kind"]), str(item["kind"]))
    check("approving records who", review.decide(conn, item["id"], approve=True, by="frankie"))
    check("a second decision is refused", not review.decide(conn, item["id"], approve=False, by="someone"))
    row = conn.execute("SELECT * FROM review WHERE id=?", (item["id"],)).fetchone()
    check("the decision is attributed", row["decided_by"] == "frankie" and row["state"] == "approved")
    check("approved items are what --execute would act on", len(review.approved(conn)) == 1)

    # (queue idempotency continues below)
    # A later pass must not reset a decided item back to pending.
    review.enqueue(conn, load_id=p.load_id, sha256=p.sha256, message_id=None, kind=p.kind,
                   reason="re-judged", proposed_type=p.document_type, proposed_comment=p.comment)
    check("re-judging does not reopen a decided item",
          conn.execute("SELECT state FROM review WHERE id=?", (item["id"],)).fetchone()[0] == "approved")


def test_auto_gate_requires_a_real_gap() -> None:
    """The gate must ask 'does this load need this?', not only 'is this safe to file?'.

    On the 15 Sep 2026 run 153 documents cleared every safety gate, and 140 of them were not work:
    41 on loads already showing Documents Received, 18 out of scope, 7 not yet loaded, and the rest
    the wrong type for what the load was short. --auto --execute would have uploaded all of them.
    """
    print("filing gates: the load must actually be short the document")

    # A clean, corroborated POD on a load that is already complete is a duplicate, not work.
    conn = fresh_db()
    sha = seed_document(conn, stage="delivered")
    conn.execute("UPDATE load SET state='complete' WHERE load_id=2578456")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a document for a complete load never auto-files",
          p.gate == filing.REVIEW and p.kind == review.NOT_NEEDED, f"{p.gate}/{p.kind}")
    check("and the reason says it would be a duplicate", "duplicate" in p.reason, p.reason)

    for state in ("out_of_scope", "not_yet_due"):
        conn = fresh_db()
        sha = seed_document(conn, stage="delivered")
        conn.execute("UPDATE load SET state=? WHERE load_id=2578456", (state,))
        p = filing.propose(conn, 2578456, sha, allow_auto=True)
        check(f"a document for a {state} load never auto-files",
              p.gate == filing.REVIEW and p.kind == review.NOT_NEEDED, f"{p.gate}/{p.kind}")

    # Filed already but the status never cleared: re-filing is the OQ-3 fix, and a person's call.
    conn = fresh_db()
    sha = seed_document(conn, stage="delivered")
    conn.execute("UPDATE load SET state='filed_status_pending' WHERE load_id=2578456")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a filed-but-stuck load goes to a person, not the auto gate",
          p.gate == filing.REVIEW and p.kind == review.REFILE, f"{p.gate}/{p.kind}")
    check("and the reason names OQ-3", "OQ-3" in p.reason, p.reason)

    # Filed but stuck AND still in transit: there is no Delivered mark to re-file after, so
    # "Waiting for Documents" is simply what an in-transit load with a BOL looks like.
    conn = fresh_db()
    sha = seed_document(conn, stage="loaded", doc_type="bill_of_lading")
    conn.execute("UPDATE load SET state='filed_status_pending' WHERE load_id=2578456")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("filed-but-stuck while in transit is not a re-file",
          p.kind == review.NOT_NEEDED, f"{p.gate}/{p.kind}")
    check("and it says the status is expected", "expected until it delivers" in p.reason, p.reason)

    # Right document, wrong gap: the load wants a POD and this reads as a BOL.
    conn = fresh_db()
    sha = seed_document(conn, stage="loaded", doc_type="bill_of_lading")
    conn.execute("UPDATE load SET state='pod_expected' WHERE load_id=2578456")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a BOL does not satisfy a load that is short a POD",
          p.gate == filing.REVIEW and p.kind == review.NOT_NEEDED, f"{p.gate}/{p.kind}")

    # And the case that SHOULD still pass: the load is short exactly this.
    conn = fresh_db()
    sha = seed_document(conn, stage="loaded", doc_type="bill_of_lading")
    conn.execute("UPDATE load SET state='bol_expected' WHERE load_id=2578456")
    p = filing.propose(conn, 2578456, sha, allow_auto=True)
    check("a BOL for a load short a BOL still auto-gates", p.gate == filing.AUTO,
          f"{p.gate}/{p.kind} {p.reason}")


def test_scope_is_the_dashboard_view() -> None:
    """The Load Management filter decides what gets worked, not the mailbox.

    Loop A creates a row for any 7-digit load number it sees in a subject line - deliberately, so
    mail is never discarded. On 15 Sep 2026 that pulled in load 2451872, delivered in June, because
    two internal emails mentioned it. It is not on the dashboard, so it is not work.
    """
    print("scope: only what the Load Management view shows is work")
    conn = fresh_db()
    blob = png(1200, 1600, b"X")
    msgs = [message("s1", "t1", "RE: Load 2451872 question", when_ms=1_700_000_000_000,
                    parts=[("doc.png", blob)])]
    ingest.sync_once(conn, FakeGmail(msgs, {"att-s1-0": blob}), group="g", reader=None)
    check("Loop A still records the load (custody)", db.counts(conn)["loads"] == 1)

    fake = FakeTPro({2451872: {"load": tp_load(), "dispatches": [{"id": 1, "status": "Delivered"}], "files": []}})
    ds = loadloop.drain(conn, fake, limit=10)
    check("but a mail-only load is not drained", ds.checked == 0, ds.line())
    check("and it is not on the work queue", len(loadloop.work_queue(conn)) == 0)

    # Once a reconcile sees it on the dashboard, it becomes work.
    db.mark_in_view(conn, 2451872)
    conn.execute("UPDATE load SET next_check_at=? WHERE load_id=2451872", (db.now_iso(),))
    ds = loadloop.drain(conn, fake, limit=10)
    check("once the dashboard shows it, it is worked", ds.checked == 1, ds.line())

    # And when it leaves the view, it stops being work but keeps its row.
    import time
    time.sleep(1.1)                       # view_checked_at has second resolution
    left = db.drop_out_of_view(conn, db.now_iso())
    check("a load that leaves the view is dropped from scope", left == 1, str(left))
    check("its row survives", db.counts(conn)["loads"] == 1)
    row = conn.execute("SELECT * FROM load WHERE load_id=2451872").fetchone()
    check("and it is never polled again", row["state"] == "not_in_view" and row["next_check_at"] is None)


def test_backfill_closes_the_history_hole() -> None:
    """Loop A only sees mail forward from the cursor.

    A load that joins the dashboard with an email chain already behind it looks like it has no
    paperwork. Measured 15 Sep 2026: 502 of 527 in-view loads had no message in the ledger, and a
    spot check found 8 of 12 really did have ratecon mail - load 2545432 had 18 messages, 17 with
    attachments, none of them ingested.
    """
    print("backfill: mail that predates the cursor")
    bol = png(1200, 1600, b"OLD")
    old = [message(f"o{i}", "told", "RE: Load 2545432 BOL", when_ms=1_600_000_000_000 + i,
                   parts=[("BOL.png", bol)]) for i in range(3)]
    blobs = {f"att-o{i}-0": bol for i in range(3)}

    class CursorOnlyGmail(FakeGmail):
        """history_since returns nothing: everything here predates the cursor."""

        def history_since(self, start, label_id=None, max_pages=50):
            return [], "2000"

    conn = fresh_db()
    fake = CursorOnlyGmail(old, blobs)
    db.upsert_load(conn, 2545432, source="dashboard")
    db.mark_in_view(conn, 2545432)
    conn.execute("UPDATE mailbox_cursor SET history_id='1' WHERE 1=0")
    db.set_cursor(conn, fake.subject, "1000")

    st = ingest.sync_once(conn, fake, group="g", reader=None)
    check("the incremental loop sees none of it", st.fetched == 0, st.line())
    check("so the load looks like it has no paperwork", db.load_doc_evidence(conn, 2545432) == (0, 0))
    check("and it is listed as needing backfill", db.loads_needing_backfill(conn) == [2545432])

    n = ingest.backfill_load(conn, fake, group="g", load_id=2545432)
    check("backfill ingests the history", n == 3, str(n))
    docs, _ = db.load_doc_evidence(conn, 2545432)
    check("the load now has its document", docs == 1, str(docs))
    check("de-duplicated across the chain", db.counts(conn)["unique_files"] == 1)
    check("and it is no longer listed", db.loads_needing_backfill(conn) == [])

    before = fake.calls
    ingest.backfill_load(conn, fake, group="g", load_id=2545432)
    check("re-running fetches nothing", fake.calls == before, f"{fake.calls} vs {before}")


def test_narrow_sweep_never_evicts() -> None:
    """A narrow reconcile did not LOOK at long-haul loads, so it cannot conclude they have gone.

    Measured 15 Sep 2026: a 3-day pickup window returned 456 loads where the full window returned
    527+. Evicting on the narrow sweep drops exactly the aged, still-moving freight this design
    exists to keep.
    """
    print("reconcile: only an authoritative sweep may evict")
    conn = fresh_db()
    db.upsert_load(conn, 2500001, source="dashboard")
    db.mark_in_view(conn, 2500001)          # a long-haul load, picked up weeks ago

    class EmptyTPro:
        calls = 0

        def search_all_pages(self, params, max_pages=20):
            return []                        # the narrow window simply does not reach it

    rs = loadloop.reconcile(conn, EmptyTPro(), terminals=[1088], days_back=3, authoritative=False)
    check("a narrow sweep evicts nothing", rs.left_view == 0, str(rs.left_view))
    check("the load is still in view",
          conn.execute("SELECT in_view FROM load WHERE load_id=2500001").fetchone()[0] == 1)

    import time
    time.sleep(1.1)                          # view_checked_at has second resolution
    rs = loadloop.reconcile(conn, EmptyTPro(), terminals=[1088], days_back=350, authoritative=True)
    check("an authoritative sweep does evict", rs.left_view == 1, str(rs.left_view))
    check("and the row survives", db.counts(conn)["loads"] == 1)


if __name__ == "__main__":
    test_routing()
    test_filters()
    test_photo_stamp()
    test_dedup_and_custody()
    test_max_defers_it_does_not_drop()
    test_cursor_written_last()
    test_one_bad_file_does_not_stop_the_batch()
    test_state_machine()
    test_drain_never_drops_a_load()
    test_drain_reads_evidence_from_the_ledger()
    test_scope_is_the_dashboard_view()
    test_backfill_closes_the_history_hole()
    test_narrow_sweep_never_evicts()
    test_filing_gates()
    test_execute_is_off_unless_asked()
    test_execute_files_once_and_refetches()
    test_refetch_verifies_the_bytes()
    test_auto_gate_requires_a_real_gap()
    test_review_queue()
    print(f"\n{PASSED} checks passed")
