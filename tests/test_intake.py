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

from intake import (archive, collector, db, filing, filters, ingest, ledger_s3, loadloop,  # noqa: E402
                    mailsync, review, routing, state, store as s3store)

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


def test_pii_gate() -> None:
    """The block gate never files and is never retried, so a false positive is permanent."""
    print("the PII gate: an identity document, not the word")
    real = ["a cell-phone photo of two Florida Class A CDL driver's licenses (personal identification)",
            "photo of a passport page", "a scan of an identity card", "social security card"]
    for t in real:
        check(f"still blocked: {t[:34]}", filing.mentions_personal_id(t), t)

    benign = ["An Illinois semi-trailer license plate reading 1054078 ST is legible",
              "a Maine license plate at the bottom is partially visible",
              "an Ontario TRAILER licence plate reading 'Z74 76F'",
              "consignee and carrier signature lines are all blank; driver's license field empty",
              "driver signature line signed, driver's license # left blank"]
    for t in benign:
        check(f"no longer blocked: {t[:34]}", not filing.mentions_personal_id(t), t)

    check("a BOL that lists a licence NUMBER stays blocked, deliberately",
          filing.mentions_personal_id("driver Ryan Harper, cell 501-580-5442, license 945840493 AR"))
    check("a licence plate AND a real ID is still blocked",
          filing.mentions_personal_id("trailer license plate 1054078 ST, and a photo of a passport"))


def test_receiving_stamp_is_acknowledgement() -> None:
    """Load 2580959's Magna BOL carries a FORD NATIONAL PARTS / RECEIVED stamp and no signature
    anywhere. Every other part of the service counts a stamp; classify_type was the one that did
    not, so the page that would clear the load was typed as the pickup copy."""
    print("a receiving stamp is acknowledgement")
    from pod_intake.matcher import classify_type
    from pod_intake.schema import Extraction

    def page(doc_type, *, stamp=False, signed=False, check_out=None, at_stop="unknown"):
        d = _extraction(doc_type)
        d["signatures"] = {"shipper_signed": False, "driver_signed": False,
                           "receiver_signed": signed, "stamp_present": stamp}
        d["times"] = {"check_in": None, "check_out": check_out, "source": "none", "at_stop": at_stop}
        return Extraction.model_validate(d)

    stamped = page("proof_of_delivery", stamp=True)
    t, why = classify_type(stamped, {"dispatch_status": "Delivered"})
    check("a stamped POD at the consignee is a POD", t == "Proof of Delivery", f"{t}: {why}")
    check("and the reason names the stamp", "receiving stamp" in why, why)

    t, _ = classify_type(stamped, {"dispatch_status": "Loaded"})
    check("but never before the truck gets there", t == "Bill Of Lading", t)

    # A company stamp on a pickup copy must not promote it: the reader has to have called it a POD.
    t, _ = classify_type(page("bill_of_lading", stamp=True), {"dispatch_status": "Delivered"})
    check("a stamp on a page the reader calls a BOL promotes nothing", t == "Bill Of Lading", t)

    t, _ = classify_type(page("proof_of_delivery", signed=True), {"dispatch_status": "Delivered"})
    check("a signature still works on its own", t == "Proof of Delivery", t)
    t, _ = classify_type(page("proof_of_delivery"), {"dispatch_status": "Delivered"})
    check("and a page with neither is still the pickup copy", t == "Bill Of Lading", t)


def test_comment_never_prints_a_non_name() -> None:
    """The reader says what it cannot read. Quoting that back produced "POD, signed by illegible
    handwritten SEP 15" on a row somebody has to make sense of."""
    print("comments: a description of a signature is not a name")
    from pod_intake.matcher import brief_page_comment
    from pod_intake.schema import Extraction

    def page(name):
        d = _extraction("proof_of_delivery")
        d["signatures"] = {"shipper_signed": False, "driver_signed": True, "receiver_signed": True,
                           "receiver_name": name, "receiver_date": "SEP 15", "stamp_present": True}
        return Extraction.model_validate(d)

    check("a real name is used", "Kevin Washington" in brief_page_comment(page("Kevin Washington")))
    for described in ("illegible handwritten signature (possibly 'Mitchell Wi')", "unreadable",
                      "signature not legible", None):
        c = brief_page_comment(page(described), max_words=7)
        check(f"no gibberish for {str(described)[:26]!r}", "signed SEP 15" in c and "illegible" not in c, c)

    # A pickup BOL is signed by the shipper and nobody else yet. Calling that "unsigned" tells
    # billing the driver came back with a blank page - see load 2589536, 22 Sep 2026.
    def pickup_bol(shipper_signed=True):
        d = _extraction("bill_of_lading")
        d["signatures"] = {"shipper_signed": shipper_signed, "driver_signed": False,
                           "receiver_signed": False, "receiver_name": None, "receiver_date": None,
                           "stamp_present": False}
        return Extraction.model_validate(d)

    c = brief_page_comment(pickup_bol(), max_words=7)
    check("a shipper-signed pickup BOL is not called unsigned", "unsigned" not in c, c)
    check("and the comment says who signed it", "shipper signed" in c, c)
    c = brief_page_comment(pickup_bol(shipper_signed=False), max_words=7)
    check("a genuinely blank BOL is still unsigned", "unsigned" in c, c)
    pod = brief_page_comment(page("Kevin Washington"))
    check("a receiver-signed POD still reports the receiver, not the shipper",
          "signed by Kevin Washington SEP 15" in pod and "shipper signed" not in pod, pod)


def test_provider_selection() -> None:
    """Which platform the reader calls, and what the model is called there. No client is built and
    nothing is sent - these are the two decisions that have to be right before either happens."""
    print("provider: anthropic / bedrock / openrouter")
    import os

    from pod_intake import provider

    keys = ("INTAKE_MODEL_PROVIDER", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        def only(**env):
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update({k: v for k, v in env.items() if v})

        only(AWS_PROFILE="paybot-admin", AWS_REGION="us-east-1")
        check("an AWS profile with no Anthropic key means bedrock", provider.provider() == "bedrock")

        only(AWS_PROFILE="paybot-admin", AWS_REGION="us-east-1", ANTHROPIC_API_KEY="sk-ant-x")
        check("but an Anthropic key still wins over an ambient AWS profile",
              provider.provider() == "anthropic", provider.provider())

        only(AWS_PROFILE="paybot-admin", ANTHROPIC_API_KEY="sk-ant-x",
             INTAKE_MODEL_PROVIDER="bedrock")
        check("and an explicit setting beats both", provider.provider() == "bedrock")

        only(ANTHROPIC_BASE_URL="https://openrouter.ai/api", ANTHROPIC_AUTH_TOKEN="x")
        check("an openrouter base url is recognised", provider.provider() == "openrouter")

        only(ANTHROPIC_API_KEY="sk-ant-x")
        check("a plain key is the anthropic api", provider.provider() == "anthropic")

        only()
        check("and nothing set falls back to the anthropic api", provider.provider() == "anthropic")

        # The id sent over the wire is the only thing that changes per platform.
        check("bedrock prefixes the model id",
              provider.model_id("claude-opus-5", "bedrock") == "anthropic.claude-opus-5")
        check("and never prefixes it twice",
              provider.model_id("anthropic.claude-opus-5", "bedrock") == "anthropic.claude-opus-5")
        check("the anthropic api takes the bare id",
              provider.model_id("claude-opus-5", "anthropic") == "claude-opus-5")
        check("and the cost table can still find the model",
              provider.base_model("anthropic.claude-opus-5") == "claude-opus-5")

        from pod_intake.reader import PRICES, Usage
        u = Usage(model="anthropic.claude-opus-5", input_tokens=1_000_000, output_tokens=0,
                  cache_read=0, cache_write=0)
        check("so a bedrock usage row prices at the opus rate, not the fallback",
              abs(u.cost_usd - PRICES["claude-opus-5"][0]) < 1e-9, f"{u.cost_usd}")

        only(AWS_PROFILE="paybot-admin", INTAKE_MODEL_PROVIDER="bedrock")
        try:
            provider.make_client()
            check("bedrock without a region is refused", False, "no error raised")
        except RuntimeError as e:
            check("bedrock without a region is refused, by name", "region" in str(e).lower(), str(e))
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_notifications() -> None:
    """The team hears what happened on their loads, once, with the reason."""
    print("notifications: what the team is told")
    from intake import notify

    check("only a clearing type is claimed to clear the status",
          state.CLEARING_TYPE_NAMES == {"Bill Of Lading", "Proof of Delivery", "Delivery Receipt"},
          str(state.CLEARING_TYPE_NAMES))
    check("Driver Supplied BOL is not one of them",
          "Driver Supplied BOL" not in state.CLEARING_TYPE_NAMES)

    conn = fresh_db()
    conn.execute("INSERT INTO load (load_id, terminal, customer, in_view) VALUES (2578456, 1088, 'Acme Foods', 1)")
    conn.execute("INSERT INTO load (load_id, terminal, customer, in_view) VALUES (2578457, 1088, 'Acme Foods', 1)")
    conn.execute("INSERT INTO load (load_id, terminal, customer, in_view) VALUES (2578999, 1135, 'Other Co', 1)")

    notify.record(conn, load_id=2578456, event=notify.FILED, sha256="a" * 64,
                  document_type="Driver Supplied BOL", filename="bol.jpg")
    notify.record(conn, load_id=2578457, event=notify.REFUSED, sha256="b" * 64,
                  kind=review.NOT_NEEDED, document_type="Bill Of Lading", filename="x.pdf",
                  reason="the load is short a Proof of Delivery and this reads as a Bill Of Lading")
    notify.record(conn, load_id=2578999, event=notify.REFUSED, sha256="c" * 64,
                  kind=review.PII, document_type=None, filename="licence.jpg")

    rows = notify.pending(conn)
    check("every event is recorded", len(rows) == 3, str(len(rows)))
    check("a refusal carries the plain reason, not the kind name",
          "not short this document" in [r["detail"] for r in rows if r["kind"] == review.NOT_NEEDED][0],
          str([r["detail"] for r in rows]))
    check("and the specific reason is kept too",
          "short a Proof of Delivery" in [r["detail"] for r in rows if r["kind"] == review.NOT_NEEDED][0])
    check("a PII refusal says why without naming the document",
          "never file one" in [r["detail"] for r in rows if r["kind"] == review.PII][0])

    notify.record(conn, load_id=2578456, event=notify.FILED, sha256="a" * 64,
                  document_type="Driver Supplied BOL", filename="bol.jpg")
    check("recording the same event twice does not repeat it", len(notify.pending(conn)) == 3)

    # A stuck load produces one wrong_doc_type refusal per document in its thread, and most of them
    # cannot repair it. Only the ones that could are worth telling anybody.
    check("a BOL on a stuck load is news", notify.worth_telling(review.WRONG_TYPE, "Bill Of Lading"))
    check("so is a POD", notify.worth_telling(review.WRONG_TYPE, "Proof of Delivery"))
    check("a freight photo on the same load is not",
          not notify.worth_telling(review.WRONG_TYPE, "Photo"))
    check("a refile is judged the same way - a BOL can fix it",
          notify.worth_telling(review.REFILE, "Bill Of Lading"))
    check("and a photo cannot", not notify.worth_telling(review.REFILE, "Photo"))
    check("nor are shipping documents",
          not notify.worth_telling(review.WRONG_TYPE, "Shipping Documents"))
    check("a personal ID is news whatever the document type",
          notify.worth_telling(review.PII, None))
    check("a not_needed refusal never is", not notify.worth_telling(review.NOT_NEEDED, "Bill Of Lading"))

    groups = notify.digest(rows, group="terminal")
    check("one message per terminal", len(groups) == 2, str([g[0] for g in groups]))
    t1088 = next(g for g in groups if g[0] == "1088")
    body = "\n".join(t1088[1])
    check("it counts both outcomes", "1 document(s) filed, 1 not filed" in body, body)
    check("and names the load against each", "2578457" in body, body)

    n = notify.mark_delivered(conn, t1088[2], channel="test")
    check("marking delivered stamps only that group", n == 2, str(n))
    left = notify.pending(conn)
    check("the other terminal is still pending", len(left) == 1 and left[0]["terminal"] == 1135)
    check("a second mark cannot re-send it", notify.mark_delivered(conn, t1088[2], channel="test") == 0)

    notify.record(conn, load_id=2578456, event=notify.FILED, sha256="a" * 64,
                  document_type="CHANGED", filename="bol.jpg")
    sent = conn.execute("SELECT document_type FROM notification WHERE load_id=2578456").fetchone()[0]
    check("a delivered notice is never rewritten behind the reader", sent == "Driver Supplied BOL", sent)


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

    # The boundary is the shipper, not departure. A BOL is signed at the dock, so a truck standing
    # there is short a document, not waiting for one to become possible. This used to answer
    # not_yet_due, which made state.shortfall() say NOTHING and dismissed real pickup BOLs as
    # duplicates - load 2589536, 22 Sep 2026. 44 of 90 customer rows gate on this window.
    a = state.assess(1, tp_load(), [{"id": 9, "status": "At Shipper"}], [], ledger_docs=1)
    check("a truck AT the shipper is already short a BOL", a["state"] == "bol_expected", a["state"])
    check("and a document on it counts as work", state.shortfall(a["state"]) == ("Bill Of Lading", state.NEEDS),
          str(state.shortfall(a["state"])))
    due = state.utc(a["next_check_at"]) - dt.datetime.now(dt.timezone.utc)
    check("at-shipper is checked hourly, not six-hourly", due.total_seconds() < 1.5 * 3600, f"{due}")

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
          out["would"]["recordType"] == "Loads" and out["would"]["recordId"] == 2578456, str(out["would"]))
    # House rule since 24 Sep 2026 (manager's feedback): a POD is uploaded as Bill Of Lading, a BOL as
    # Driver Supplied BOL. What it IS stays on the row; only the upload type follows the rule.
    check("a POD is uploaded as Bill Of Lading", out["would"]["documentType"] == "Bill Of Lading"
          and out["would"].get("readAs") == "Proof of Delivery", str(out["would"]))
    check("and a BOL as Driver Supplied BOL", filing.file_as("Bill Of Lading") == "Driver Supplied BOL")
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



RAW_MSG = b"From: a@b" + bytes([13, 10]) + b"Subject: load 2589536"


class FakeS3:
    """Enough of the S3 client to test the key scheme and idempotence without AWS."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.meta: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}
        self.etags: dict[str, str] = {}
        self.puts = 0

    def _refused(self, op):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": "PreconditionFailed", "Message": "At least one of the "
                                      "pre-conditions you specified did not hold"}}, op)

    def put_object(self, Bucket, Key, Body, **kw):      # noqa: N803 - boto3's spelling
        if "IfMatch" in kw and self.etags.get(Key) != kw["IfMatch"]:
            raise self._refused("PutObject")
        if kw.get("IfNoneMatch") == "*" and Key in self.objects:
            raise self._refused("PutObject")
        self.puts += 1
        self.objects[Key] = Body
        self.meta[Key] = kw.get("Metadata", {})
        from urllib.parse import parse_qsl
        self.tags[Key] = dict(parse_qsl(kw.get("Tagging", "")))
        self.etags[Key] = '"' + hashlib.md5(Body).hexdigest() + '"'
        return {"ETag": self.etags[Key]}

    def put_object_tagging(self, Bucket, Key, Tagging):  # noqa: N803
        self.tags[Key] = {t["Key"]: t["Value"] for t in Tagging["TagSet"]}
        return {}

    def copy_object(self, Bucket, Key, CopySource, CopySourceIfMatch=None, **kw):  # noqa: N803
        src = CopySource["Key"]
        if CopySourceIfMatch and self.etags.get(src) != CopySourceIfMatch:
            raise self._refused("CopyObject")
        self.objects[Key] = self.objects[src]
        self.etags[Key] = self.etags[src]
        return {}

    def head_object(self, Bucket, Key):                  # noqa: N803
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):                   # noqa: N803
        import io
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "no such key"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": self.etags.get(Key, '"0"')}

    def delete_object(self, Bucket, Key):                # noqa: N803
        self.objects.pop(Key, None)
        return {}

    def get_paginator(self, name):
        assert name == "list_objects_v2", name
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix=""):       # noqa: N803
                keys = sorted(k for k in objects if k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), 1000):
                    yield {"Contents": [{"Key": k} for k in keys[i:i + 1000]]}
        return _Pages()



def test_no_group_is_a_configuration_not_a_crash() -> None:
    """A mailbox that is not behind a Google Group has nothing to unwrap.

    `intake cycle` shipped with --group defaulting to None and every message in its collect step
    died on None.lower(). The step wrapper caught it, so the cycle reported one failed step and
    carried on - which is the right behaviour, and is also why a whole broken collect could look
    like a small red line in an otherwise healthy log.
    """
    print("routing: no group configured")

    plain = {"from": "driver@carrier.example", "x-original-sender": "someone@else.example"}
    check("no group set: the From header is used as-is",
          routing.original_sender(plain, None) == "driver@carrier.example",
          routing.original_sender(plain, None))
    check("empty string behaves the same",
          routing.original_sender(plain, "") == "driver@carrier.example")

    wrapped = {"from": "Loads <loads@circledelivers.com>", "x-original-sender": "driver@carrier.example"}
    check("with a group, the real sender is still unwrapped",
          routing.original_sender(wrapped, "loads@circledelivers.com") == "driver@carrier.example",
          routing.original_sender(wrapped, "loads@circledelivers.com"))
    check("a message not from the group is left alone",
          routing.original_sender(plain, "loads@circledelivers.com") == "driver@carrier.example")


def test_every_subcommand_defaults_its_group() -> None:
    """Whatever a new subcommand forgets, it must not forget this one.

    The bug was not in routing; it was one argparse default out of step with the others.
    """
    print("cli: --group defaults")
    import re as _re

    src = (HERE / "intake" / "__main__.py").read_text(encoding="utf-8")
    defaults = _re.findall(r'add_argument\("--group", default=([^)]+)\)', src)
    check("every --group defaults to GROUP, never None",
          defaults and all(d.strip() == "GROUP" for d in defaults), str(defaults))




class _FakeSchema:
    __name__ = "_FakeSchema"

    @staticmethod
    def model_validate_json(t):
        return {"parsed": t}

    @staticmethod
    def model_json_schema():
        return {"type": "object"}




def test_a_satisfied_load_is_not_paid_to_read() -> None:
    """Ask whether the load wants a document BEFORE paying to read one, not after.

    Measured 23 Sep 2026: 177 documents read for $11.53, of which 4 landed on a load short exactly
    that document. The filing gate already applied state.shortfall() - it applied it after the money
    was gone.
    """
    print("reads: the check runs before the money")

    conn = fresh_db()

    def doc(sha, load_id, load_state):
        conn.execute("INSERT OR IGNORE INTO load (load_id, state, in_view) VALUES (?,?,1)",
                     (load_id, load_state))
        conn.execute("INSERT OR IGNORE INTO message (message_id, thread_id, load_id, part_count) "
                     "VALUES (?,?,?,1)", (f"m{load_id}", f"t{load_id}", load_id))
        conn.execute("INSERT OR IGNORE INTO attachment (sha256, filename, bytes) VALUES (?,?,100)",
                     (sha, f"{load_id}.pdf"))
        conn.execute("INSERT OR IGNORE INTO part (message_id, part_id, sha256, decision) "
                     "VALUES (?,?,?,'keep')", (f"m{load_id}", "p1", sha))

    doc("a" * 64, 101, "bol_expected")     # short a BOL      -> read
    doc("b" * 64, 102, "complete")         # done             -> defer
    doc("c" * 64, 103, "not_yet_due")      # truck not there  -> defer
    doc("d" * 64, 104, "new")              # UNASSESSED       -> read
    doc("e" * 64, 105, "")                 # no state at all  -> read
    doc("f" * 64, 106, "invented_state")   # unclassified     -> read
    conn.commit()

    got = {r["sha256"][0] for r in db.unread_attachments(conn, limit=99, skip_satisfied=True)}
    check("a load short a BOL is read", "a" in got, str(sorted(got)))
    check("a complete load is not", "b" not in got, str(sorted(got)))
    check("a not-yet-due load is not", "c" not in got, str(sorted(got)))
    # These three are the safety property. Skipping an unknown load loses documents silently.
    check("a load nobody has assessed IS read", "d" in got, str(sorted(got)))
    check("a load with no state IS read", "e" in got, str(sorted(got)))
    check("a state nobody classified IS read", "f" in got, str(sorted(got)))

    all_ = {r["sha256"][0] for r in db.unread_attachments(conn, limit=99, skip_satisfied=False)}
    check("without the check every one is offered", len(all_) == 6, str(sorted(all_)))

    # The whole design rests on this: deferral must not be a decision. Nothing is written to the
    # document, so the moment the truck reaches the shipper the same query offers it again.
    conn.execute("UPDATE load SET state='bol_expected' WHERE load_id=103")
    conn.commit()
    again = {r["sha256"][0] for r in db.unread_attachments(conn, limit=99, skip_satisfied=True)}
    check("a deferred document returns the moment its load needs one", "c" in again, str(sorted(again)))

    # And the reverse: a load that completes stops offering, without touching the document either.
    conn.execute("UPDATE load SET state='complete' WHERE load_id=101")
    conn.commit()
    done = {r["sha256"][0] for r in db.unread_attachments(conn, limit=99, skip_satisfied=True)}
    check("and stops being offered when the load completes", "a" not in done, str(sorted(done)))


def test_satisfied_states_are_derived_not_listed() -> None:
    """The skip list must come from SHORTFALL, or a new state silently becomes unreadable."""
    print("reads: the skip list cannot drift")
    from intake import state as _st

    check("every satisfied state is one SHORTFALL calls NOTHING",
          all(_st.SHORTFALL[k][1] == _st.NOTHING for k in _st.SATISFIED_STATES))
    check("every NOTHING state is in the skip list",
          {k for k, (_, v) in _st.SHORTFALL.items() if v == _st.NOTHING} == set(_st.SATISFIED_STATES))
    for s_ in ("new", "error", "", "a_state_invented_next_month"):
        check(f"{s_!r} is never treated as satisfied", s_ not in _st.SATISFIED_STATES)


def test_structured_output_refusal_is_learned_once() -> None:
    """Bedrock refuses output_config. Asking it again for every document doubles the request count.

    Measured 22 Sep 2026: one "endpoint rejected structured outputs" line per document read, each
    one a paid-for refused request before the request that worked.
    """
    print("reader: the endpoint is asked once, not once per document")
    import anthropic as _an
    from pod_intake import reader as _rd

    _rd._NO_STRUCTURED_OUTPUT.clear()
    calls = {"structured": 0, "prompt": 0}

    class Resp:
        content = [type("B", (), {"type": "text", "text": '{"ok": true}'})()]
        stop_reason = "end_turn"
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 1,
                               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})()

    class Messages:
        def create(self, **kw):
            if "output_config" in kw:
                calls["structured"] += 1
                raise _an.BadRequestError(
                    "output_config not supported",
                    response=type("R", (), {"status_code": 400, "headers": {}, "request": None})(),
                    body=None)
            calls["prompt"] += 1
            return Resp()

    class Client:
        messages = Messages()

    client = Client()
    for _ in range(5):
        try:
            _rd._structured_call(client, "claude-opus-5", "sys", [], _FakeSchema)
        except Exception:
            pass

    check("the endpoint is probed exactly once across five documents",
          calls["structured"] == 1, f"probed {calls['structured']} times")
    check("and every document still gets its request", calls["prompt"] == 5, str(calls))
    _rd._NO_STRUCTURED_OUTPUT.clear()


def test_a_transient_lock_does_not_end_a_long_job() -> None:
    """An archive run measured in hours must not die on a lock that clears in a second."""
    print("ledger: transient locks")

    import sqlite3 as _sq

    class FlakyConn:
        """A connection that is locked for its first two writes, then works. sqlite3.Connection
        will not let its own execute be replaced, so the retry is tested against a stand-in."""

        def __init__(self, fail_times: int, error: str = "database is locked"):
            self.calls = 0
            self.fail_times = fail_times
            self.error = error

        def execute(self, sql, params=()):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise _sq.OperationalError(self.error)
            class R:
                rowcount = 1
            return R()

    c = FlakyConn(fail_times=2)
    n = db.retry_write(c, "UPDATE x SET y=?", ("v",), base_delay=0.001)
    check("it retries through a lock and succeeds", c.calls == 3 and n == 1, f"calls={c.calls} n={n}")

    # A lock is transient; a broken statement is not. Retrying real errors would turn a typo into
    # twelve seconds of waiting and then the same failure.
    c = FlakyConn(fail_times=99, error="no such column: nope")
    try:
        db.retry_write(c, "SELECT nope", (), base_delay=0.001)
        raised = False
    except _sq.OperationalError:
        raised = True
    check("a real error is raised at once, not retried", raised and c.calls == 1, f"calls={c.calls}")

    c = FlakyConn(fail_times=99)
    try:
        db.retry_write(c, "UPDATE x SET y=?", ("v",), attempts=3, base_delay=0.001)
        gave_up = False
    except _sq.OperationalError:
        gave_up = True
    check("a lock that never clears is raised, not swallowed", gave_up and c.calls == 3,
          f"calls={c.calls}")


def test_archive_keys_are_derived_not_random() -> None:
    """A random id per object would make every re-run a duplicate and undo the read de-duplication."""
    print("archive: keys")

    sha = "a" * 64
    check("a document is addressed by its content",
          s3store.doc_key(sha) == f"doc/aa/{'a' * 64}", s3store.doc_key(sha))
    check("the same document asked for twice gives the same key",
          s3store.doc_key(sha) == s3store.doc_key(sha))
    check("its extraction sits beside it",
          s3store.extraction_key(sha) == s3store.doc_key(sha) + ".extraction.json")

    k = s3store.mail_key("18f2abc", "2026-09-22T12:34:40+00:00")
    check("mail is partitioned by the date it arrived", k == "mail/2026/09/22/18f2abc.json.gz", k)
    check("re-syncing the same message gives the same key",
          s3store.mail_key("18f2abc", "2026-09-22T12:34:40+00:00") == k)
    # A message with no internalDate must not land under today's date: today moves on every re-run,
    # which is the one thing the date prefix exists to prevent.
    check("a message with no date does not get today's",
          s3store.mail_key("18f2abc", None) == "mail/unknown/18f2abc.json.gz")


def test_archive_is_idempotent_and_resumable() -> None:
    """Interrupting an archive run must cost the next run nothing but what is genuinely missing."""
    print("archive: idempotence")

    fake = FakeS3()
    store = s3store.Store("a-bucket", "intake", client=fake)
    ok, why = store.writable()
    check("write access is proved before anything is fetched", ok, why)

    first = store.put_document("b" * 64, b"PDFBYTES", filename="bol.pdf")
    check("the first write stores the bytes", not first.skipped and first.bytes_written == 8)
    again = store.put_document("b" * 64, b"PDFBYTES", filename="bol.pdf")
    check("the second is skipped, not duplicated", again.skipped and again.key == first.key)
    check("and S3 was asked to store it exactly once",
          len([k for k in fake.objects if k.endswith("b" * 64)]) == 1, str(list(fake.objects)))

    check("the prefix is applied to the stored key", first.key.startswith("intake/doc/"), first.key)

    store.put_mail(message_id="m1", internal_date="2026-09-22T00:00:00+00:00",
                   raw=RAW_MSG, envelope={"load_id": 2589536})
    back = store.get_mail("m1", "2026-09-22T00:00:00+00:00")
    check("the raw message survives the round trip",
          base64.b64decode(back["raw_rfc822_b64"]) == RAW_MSG, str(back)[:90])
    check("and the routing that found its load rides along", back["envelope"]["load_id"] == 2589536)



def test_archive_links_mail_to_its_documents() -> None:
    """S3 must answer both directions on its own. The ledger is an index, not a dependency.

    Without this the join between a message and its documents lived only in the SQLite part table:
    an Athena query over the bucket could not say which mail a BOL arrived on without downloading
    every message and re-hashing its MIME parts.
    """
    print("archive: the link between mail and documents")

    conn = fresh_db()
    conn.executescript(
        "INSERT INTO message (message_id, thread_id, internal_date, load_id, part_count) "
        "VALUES ('m9','t9','2026-09-22T00:00:00+00:00',2589536,2);"
        "INSERT INTO attachment (sha256, filename, bytes, document_type) "
        "VALUES ('" + "c" * 64 + "','bol.pdf',1234,'bill_of_lading');"
        "INSERT INTO attachment (sha256, filename, bytes) "
        "VALUES ('" + "d" * 64 + "','sig.png',90);"
        "INSERT INTO part (message_id, part_id, sha256, decision) VALUES ('m9','p1','" + "c" * 64 + "','keep');"
        "INSERT INTO part (message_id, part_id, sha256, decision) VALUES ('m9','p2','" + "d" * 64 + "','signature_or_logo');")
    conn.commit()

    manifest = db.message_parts(conn, "m9")
    check("the manifest names every part the message carried", len(manifest) == 2, str(manifest))
    kept = [m for m in manifest if m["in_doc_prefix"]]
    check("and says which of them reached doc/", len(kept) == 1 and kept[0]["filename"] == "bol.pdf",
          str(kept))
    dropped = [m for m in manifest if not m["in_doc_prefix"]]
    check("a dropped part is still recorded, with why", dropped[0]["decision"] == "signature_or_logo",
          str(dropped))
    check("the manifest carries what the reader made of it",
          kept[0]["read_as"] == "bill_of_lading", str(kept))

    fake = FakeS3()
    store = s3store.Store("b", client=fake)
    store.put_mail(message_id="m9", internal_date="2026-09-22T00:00:00+00:00", raw=RAW_MSG,
                   envelope={"load_id": 2589536}, attachments=manifest)
    back = store.get_mail("m9", "2026-09-22T00:00:00+00:00")
    shas = [a["sha256"] for a in back["attachments"]]
    check("mail -> documents: the object names its attachments by the key they are stored under",
          ("c" * 64) in shas, str(shas)[:80])
    check("and that sha is the doc/ key", s3store.doc_key(shas[0]).startswith("doc/"))

    store.put_document("c" * 64, b"PDF", filename="bol.pdf", message_id="m9", load_id=2589536)
    meta = fake.meta[store.full(s3store.doc_key("c" * 64))]
    check("documents -> mail: the object names the message it arrived on",
          meta.get("first-seen-message") == "m9", str(meta))
    check("and the load it belongs to", meta.get("load-id") == "2589536", str(meta))


def test_archive_withholds_personal_id_by_default() -> None:
    """The service has never stored these bytes. Starting to is a decision, not a default."""
    print("archive: personal id")

    check("a CDL page is recognised",
          archive._is_pii('{"notes": "photo of an Ohio COMMERCIAL DRIVER LICENSE card"}'))
    check("so is the reader's own personal_id finding",
          archive._is_pii('{"personal_id": true}'))
    check("an ordinary BOL is not", not archive._is_pii('{"document_type": "bill_of_lading"}'))
    # An unread document flags nothing: withholding every unread page would archive nothing at all
    # on the first run, which is not a safety property, just an empty bucket.
    check("an unread document is not treated as personal ID", not archive._is_pii(None))


def test_archive_records_what_landed() -> None:
    """The ledger is the index. If it does not record the key, the next run re-uploads everything."""
    print("archive: the ledger records it")

    conn = fresh_db()
    c = db.archive_counts(conn)
    check("a fresh ledger has archived nothing",
          c["messages_archived"] == 0 and c["documents_archived"] == 0, str(c))

    conn.execute("INSERT INTO message (message_id, thread_id, internal_date, load_id, part_count) "
                 "VALUES ('m1','t1','2026-09-22T00:00:00+00:00',2589536,1)")
    conn.commit()
    check("an un-archived message is offered", len(db.pending_mail_archive(conn)) == 1)
    db.mark_mail_archived(conn, "m1", "mail/2026/09/22/m1.json.gz")
    conn.commit()
    check("and is not offered again once stored", len(db.pending_mail_archive(conn)) == 0)
    check("the count reflects it", db.archive_counts(conn)["messages_archived"] == 1)


def test_a_deleted_message_does_not_stop_collection() -> None:
    """History keeps naming a message after someone deletes it; fetching it then answers 404.

    Until 23 Sep 2026 that 404 ended the pass before anything committed, so the cursor never moved
    and every later pass died on the same deleted message. An hourly job would have collected
    nothing, ever, from the first deletion on.
    """
    print("ingest: a deleted message")
    blob = png(1200, 1600, b"D")
    msgs = [message(f"del{i}", f"td{i}", f"RE: Load 257810{i}", when_ms=1_700_000_000_000 + i,
                    parts=[(f"d{i}.png", blob)]) for i in range(3)]
    conn = fresh_db()
    fake = FakeGmail(msgs, {f"att-del{i}-0": blob for i in range(3)})
    real = fake.message

    def gone_or_real(message_id, fmt="full"):
        if message_id == "del1":
            raise ingest.gm.GmailError(404, f"/messages/{message_id}", "Requested entity was not found.")
        return real(message_id, fmt)

    fake.message = gone_or_real
    st = ingest.sync_once(conn, fake, group="g", reader=None)
    check("the pass completes", st.fetched == 2, st.line())
    check("the deleted one is counted, not hidden", st.vanished == 1 and "deleted from Gmail" in st.line(),
          st.line())
    check("and the cursor moves past it", db.get_cursor(conn, fake.subject) == "1000")

    def down(message_id, fmt="full"):
        raise ingest.gm.GmailError(503, f"/messages/{message_id}", "backend unavailable")

    fake.message = down
    conn2 = fresh_db()
    try:
        ingest.sync_once(conn2, fake, group="g", reader=None)
    except ingest.gm.GmailError:
        check("any other Gmail error still stops the pass - an outage is not an absence", True)
    else:
        check("any other Gmail error still stops the pass - an outage is not an absence", False)
    check("with the cursor untouched", db.get_cursor(conn2, fake.subject) is None)


def _doc_rows(conn, docs: list[tuple[str, str | None]]) -> None:
    """One message carrying one kept part per (sha256, extraction_json)."""
    conn.execute("INSERT INTO message (message_id, thread_id, internal_date, load_id, part_count) "
                 "VALUES ('md','td','2026-09-23T00:00:00+00:00',2589536,?)", (len(docs),))
    for i, (sha, extraction) in enumerate(docs):
        conn.execute("INSERT INTO attachment (sha256, filename, bytes, extraction_json) VALUES (?,?,?,?)",
                     (sha, f"page{i}.png", 10, extraction))
        conn.execute("INSERT INTO part (message_id, part_id, attachment_id, sha256, decision) "
                     "VALUES ('md',?,?,?,'keep')", (str(i), f"att-{i}", sha))
    conn.commit()


def test_unread_documents_are_tagged_unchecked() -> None:
    """Not read is not the same as read and found nothing, and the bucket tag must say which.

    Collection alone never reads a page. Tagging those pii=false claimed a check that never happened,
    so a licence collected that way would have been stored labelled as not being one.
    """
    print("archive: unread documents")
    check("an unread page has no finding", archive._pii_finding(None) is None)
    check("and is tagged unchecked", s3store.pii_tag(None) == "unchecked")
    check("a read page keeps its answer", s3store.pii_tag(False) == "false" and s3store.pii_tag(True) == "true")

    unread, plain, licence = "1" * 64, "2" * 64, "3" * 64
    conn = fresh_db()
    _doc_rows(conn, [(unread, None), (plain, '{"document_type": "bill_of_lading"}'),
                     (licence, '{"personal_id": true}')])
    fake = FakeS3()
    store = s3store.Store("b", client=fake)
    gmail = FakeGmail([], {f"att-{i}": b"PAGE%d" % i for i in range(3)})

    st = archive.archive_documents(conn, gmail, store, in_view_only=False)
    tag = lambda sha: fake.tags.get(s3store.doc_key(sha), {}).get("pii")  # noqa: E731
    check("the unread page is stored", s3store.doc_key(unread) in fake.objects, st.line("documents"))
    check("tagged unchecked, not false", tag(unread) == "unchecked", str(fake.tags))
    check("the read page says what the reader found", tag(plain) == "false", str(fake.tags))
    check("a licence is still withheld by default", s3store.doc_key(licence) not in fake.objects)
    check("and the run says how many went in unchecked", st.unchecked == 1 and "unchecked" in st.line("x"),
          st.line("documents"))

    # The page is read later. Nothing else would offer it again - it already has an s3_key - so
    # without the re-tag pass it would say `unchecked` for ever, even once a licence was found on it.
    conn.execute("UPDATE attachment SET extraction_json=? WHERE sha256=?",
                 ('{"notes": "COMMERCIAL DRIVER LICENSE"}', unread))
    conn.commit()
    st = archive.archive_documents(conn, gmail, store, in_view_only=False)
    check("once read, it is re-tagged with the answer", tag(unread) == "true", str(fake.tags))
    check("and its reading is stored beside it", s3store.extraction_key(unread) in fake.objects)
    check("the run counts it", st.retagged == 1, st.line("documents"))
    st = archive.archive_documents(conn, gmail, store, in_view_only=False)
    check("and does not do it twice", st.retagged == 0, st.line("documents"))


def raw_mail(mid: str, thread: str, subject: str, *, when_ms: int, snippet: str = "",
             attachments: list[tuple[str, bytes, str]] | None = None) -> dict:
    """A message as Gmail's format=raw returns it: the whole RFC822, base64url."""
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"] = "Ratecon <ratecon@circledelivers.com>"
    m["X-Original-Sender"] = "driver@carrier.example"
    m["To"] = "ratecon@circledelivers.com"
    m["Subject"] = subject
    m.set_content("paperwork attached")
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split("/")
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return {"id": mid, "threadId": thread, "internalDate": str(when_ms), "snippet": snippet,
            "labelIds": ["INBOX"], "_subject": subject,
            "raw": base64.urlsafe_b64encode(m.as_bytes()).decode("ascii").rstrip("=")}


class FakeRawGmail:
    """Gmail as the S3-only collector sees it: history, raw messages, thread metadata."""

    subject = "bot@circledelivers.com"

    def __init__(self, messages: list[dict], history_id: str = "2000") -> None:
        self._m = {m["id"]: m for m in messages}
        self.history_id = history_id
        self.calls = 0
        self.fetched: list[str] = []
        self.threads_asked: list[str] = []
        self.gone: set[str] = set()
        self.failing: set[str] = set()
        self.expired = False
        self.searched = ""

    def profile(self) -> dict:
        return {"historyId": "3000"}

    def _refs(self):
        return [{"id": m["id"], "threadId": m["threadId"]} for m in self._m.values()]

    def history_since(self, start, label_id=None, max_pages=50):
        if self.expired:
            raise collector.gm.CursorTooOld(404, "/history", "startHistoryId too old")
        return self._refs(), self.history_id

    def search(self, query: str, cap: int = 2000) -> list[dict]:
        self.searched = query
        return self._refs()

    def message(self, message_id: str, fmt: str = "full") -> dict:
        self.calls += 1
        self.fetched.append(message_id)
        if message_id in self.gone:
            raise collector.gm.GmailError(404, f"/messages/{message_id}", "Requested entity was not found.")
        if message_id in self.failing:
            raise collector.gm.GmailError(503, f"/messages/{message_id}", "backend unavailable")
        assert fmt == "raw", fmt
        return self._m[message_id]

    def thread(self, thread_id: str) -> dict:
        self.threads_asked.append(thread_id)
        return {"id": thread_id, "messages": [
            {"id": m["id"], "internalDate": m["internalDate"], "snippet": m["snippet"],
             "payload": {"headers": [{"name": "Subject", "value": m["_subject"]}]}}
            for m in self._m.values() if m["threadId"] == thread_id]}


def _bookmarked_store(history_id: str = "1000", taken_at: str = "2026-09-23T12:00:00+00:00"):
    fake = FakeS3()
    store = s3store.Store("b", client=fake)
    collector.write_bookmark(store, collector.Bookmark(history_id, taken_at), create=True)
    return fake, store


def _mail_obj(fake: FakeS3, store, mid: str) -> dict:
    import gzip
    import json
    key = next(k for k in fake.objects if k.startswith("mail/") and k.endswith(f"/{mid}.json.gz"))
    return json.loads(gzip.decompress(fake.objects[key]))


def test_collector_stores_mail_and_documents_linked() -> None:
    """The S3-only collector must do what the ledger loop and the archive did together, with no ledger."""
    print("collector: mail and documents, linked, without a ledger")
    page = png(1200, 1600, b"P")
    logo = png(900, 120, b"L")                      # passes the size test, fails the shape test
    pdf = b"%PDF-1.4\n" + b"x" * 50_000
    msgs = [
        raw_mail("m1", "t1", "Load 2589536 POD", when_ms=1_758_600_000_000,
                 attachments=[("pod.png", page, "image/png"), ("logo.png", logo, "image/png")]),
        raw_mail("m2", "t1", "RE: Load 2589536 POD", when_ms=1_758_600_100_000,
                 attachments=[("pod.png", page, "image/png")]),
        raw_mail("m3", "t1", "RE: paperwork", when_ms=1_758_600_200_000,
                 attachments=[("bol.pdf", pdf, "application/pdf")]),
        raw_mail("m4", "t9", "hello", when_ms=1_758_600_300_000),
    ]
    gmail = FakeRawGmail(msgs)
    fake, store = _bookmarked_store()

    st = collector.run(gmail, store, group="ratecon@circledelivers.com")
    check("every message is stored", st.mail_stored == 4 and not st.error, st.line())
    check("a page forwarded twice is one document", st.docs_stored == 2 and st.docs_already == 1, st.line())
    page_key = s3store.doc_key(hashlib.sha256(page).hexdigest())
    check("documents are addressed by content", page_key in fake.objects)
    check("a logo is not stored as a document",
          s3store.doc_key(hashlib.sha256(logo).hexdigest()) not in fake.objects)
    check("unread documents are tagged unchecked", fake.tags[page_key].get("pii") == "unchecked",
          str(fake.tags.get(page_key)))
    check("documents -> mail: the first message it arrived on", fake.meta[page_key].get("first-seen-message") == "m1",
          str(fake.meta[page_key]))

    m1 = _mail_obj(fake, store, "m1")
    shas = {a["sha256"]: a for a in m1["attachments"]}
    check("mail -> documents: the manifest names the stored page",
          hashlib.sha256(page).hexdigest() in shas and shas[hashlib.sha256(page).hexdigest()]["in_doc_prefix"])
    dropped = [a for a in m1["attachments"] if not a["in_doc_prefix"]]
    check("and records the dropped logo, with why", len(dropped) == 1 and dropped[0]["decision"] == "signature_or_logo",
          str(dropped))
    check("the raw message is kept whole", b"Load 2589536" in base64.b64decode(m1["raw_rfc822_b64"]))
    check("the group's rewrite is undone", m1["envelope"]["from"] == "driver@carrier.example", m1["envelope"]["from"])

    m3 = _mail_obj(fake, store, "m3")
    check("a reply with no load number takes its thread's", m3["envelope"]["load_id"] == 2589536
          and m3["envelope"]["routing_tier"] == "thread", str(m3["envelope"]))
    check("the thread is asked only when the subject cannot decide", gmail.threads_asked.count("t1") == 1,
          str(gmail.threads_asked))
    m4 = _mail_obj(fake, store, "m4")
    check("mail with no load number anywhere is still stored", m4["envelope"]["load_id"] is None)

    bm = collector.read_bookmark(store)
    check("the bookmark moves to the history head", bm.history_id == "2000" and bm.stored == [], str(bm))

    st = collector.run(gmail, store, group="ratecon@circledelivers.com")
    check("a re-run stores nothing twice", st.mail_stored == 0 and st.mail_already == 4 and st.docs_stored == 0,
          st.line())


def test_collector_resumes_where_it_stopped() -> None:
    """A run cut short must leave the rest for the next one, without redoing what it finished."""
    print("collector: stopping early")
    import time as _time
    msgs = [raw_mail(f"r{i}", f"tr{i}", f"Load 258900{i}", when_ms=1_758_600_000_000 + i) for i in range(5)]
    gmail = FakeRawGmail(msgs)
    fake, store = _bookmarked_store()

    st = collector.run(gmail, store, group="g", max_messages=2)
    bm = collector.read_bookmark(store)
    check("the cap defers the rest", st.mail_stored == 2 and st.deferred == 3, st.line())
    check("the bookmark does not move", bm.history_id == "1000", str(bm))
    check("but remembers what finished", sorted(bm.stored) == ["r0", "r1"], str(bm.stored))

    st = collector.run(gmail, store, group="g", max_messages=2)
    check("the next run takes the next two", st.mail_stored == 2 and st.done_before == 2, st.line())
    check("without fetching the finished ones again", len(gmail.fetched) == len(set(gmail.fetched)),
          str(gmail.fetched))
    st = collector.run(gmail, store, group="g", max_messages=2)
    bm = collector.read_bookmark(store)
    check("the last one lands and only then the bookmark moves", st.deferred == 0 and bm.history_id == "2000"
          and bm.stored == [], f"{st.line()} {bm}")

    fake2, store2 = _bookmarked_store()
    st = collector.run(FakeRawGmail(msgs), store2, group="g", deadline=_time.monotonic() - 1)
    check("past the time limit nothing new is started", st.fetched == 0 and st.stopped_by == "time limit",
          st.line())


def test_collector_errors_and_deletions() -> None:
    """An outage is not an absence; a deletion is."""
    print("collector: errors and deleted mail")
    msgs = [raw_mail(f"e{i}", f"te{i}", f"Load 258910{i}", when_ms=1_758_600_000_000 + i) for i in range(3)]

    gmail = FakeRawGmail(msgs)
    gmail.gone.add("e1")
    fake, store = _bookmarked_store()
    st = collector.run(gmail, store, group="g")
    check("a deleted message is counted and passed", st.vanished == 1 and st.mail_stored == 2 and not st.error,
          st.line())
    check("and the bookmark moves", collector.read_bookmark(store).history_id == "2000")

    gmail = FakeRawGmail(msgs)
    gmail.failing.add("e1")
    fake, store = _bookmarked_store()
    st = collector.run(gmail, store, group="g")
    bm = collector.read_bookmark(store)
    check("any other error stops the run", bool(st.error) and st.mail_stored == 1, st.line())
    check("keeping what finished, and not moving past the failure",
          bm.history_id == "1000" and bm.stored == ["e0"], str(bm))


def test_collector_bookmark_is_guarded() -> None:
    """The bookmark is the one thing every run depends on."""
    print("collector: the bookmark")
    fake = FakeS3()
    store = s3store.Store("b", client=fake)
    try:
        collector.read_bookmark(store)
    except Exception:                                            # noqa: BLE001
        check("no bookmark is an error, never a blank start", True)
    else:
        check("no bookmark is an error, never a blank start", False)

    bm = collector.Bookmark("1000", "2026-09-23T00:00:00+00:00")
    collector.write_bookmark(store, bm, create=True)
    try:
        collector.write_bookmark(store, bm, create=True)
    except collector.BookmarkChanged:
        check("seeding refuses to replace a live bookmark", True)
    else:
        check("seeding refuses to replace a live bookmark", False)

    held = collector.read_bookmark(store)
    moved = collector.read_bookmark(store)
    moved.history_id = "1500"
    collector.write_bookmark(store, moved)
    try:
        collector.write_bookmark(store, held)
    except collector.BookmarkChanged:
        check("a run holding a stale bookmark cannot move it", True)
    else:
        check("a run holding a stale bookmark cannot move it", False)
    check("and the newer one stands", collector.read_bookmark(store).history_id == "1500")

    msgs = [raw_mail("x1", "tx", "Load 2589201", when_ms=1_758_600_000_000)]
    gmail = FakeRawGmail(msgs)
    gmail.expired = True
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=9)).isoformat(timespec="seconds")
    fake, store = _bookmarked_store(taken_at=old)
    st = collector.run(gmail, store, group="ratecon@circledelivers.com")
    check("an expired bookmark re-walks the whole gap", "newer_than:10d" in gmail.searched, gmail.searched)
    check("and restarts from the mailbox head", collector.read_bookmark(store).history_id == "3000"
          and st.mail_stored == 1, st.line())


def test_lambda_wiring() -> None:
    """The handler reads its settings, runs one pass, and fails loudly only after saving the bookmark."""
    print("lambda: wiring")
    import os
    import boto3
    from intake import aws_lambda
    msgs = [raw_mail("w1", "tw", "Load 2589300", when_ms=1_758_600_000_000)]
    gmail = FakeRawGmail(msgs)
    fake, store = _bookmarked_store()
    saved = (boto3.client, aws_lambda._service_account, aws_lambda.gm.Delegated, dict(os.environ))
    try:
        boto3.client = lambda name, *a, **k: fake
        aws_lambda._service_account = lambda secret_id: {"client_email": "x", "private_key": "y"}
        aws_lambda.gm.Delegated = lambda info, subject: gmail
        os.environ.update({"INTAKE_S3_BUCKET": "b", "INTAKE_GMAIL_SECRET": "s", "PAYBOT_GMAIL_USER": "u"})
        out = aws_lambda.handler({}, None)
        check("one pass runs and reports", "1 mail stored" in out["collect"], out["collect"])
        gmail.failing.add("w2")
        gmail._m["w2"] = raw_mail("w2", "tw2", "Load 2589301", when_ms=1_758_600_000_001)
        try:
            aws_lambda.handler({}, None)
        except RuntimeError:
            check("a failed run raises, so the Errors metric sees it", True)
        else:
            check("a failed run raises, so the Errors metric sees it", False)
        check("after its bookmark was saved", collector.read_bookmark(store).stored == ["w1"],
              str(collector.read_bookmark(store)))
    finally:
        boto3.client, aws_lambda._service_account, aws_lambda.gm.Delegated = saved[:3]
        os.environ.clear()
        os.environ.update(saved[3])


def _collected_store():
    """A FakeS3 holding what the collector stores for four messages, three routed and one not."""
    page = png(1200, 1600, b"W")
    msgs = [
        raw_mail("k1", "tk", "Load 2589536 POD", when_ms=1_758_600_000_000,
                 attachments=[("pod.png", page, "image/png")]),
        raw_mail("k2", "tk", "RE: paperwork", when_ms=1_758_600_100_000,
                 attachments=[("pod.png", page, "image/png")]),
        raw_mail("k3", "tz", "Load 2589777 BOL", when_ms=1_758_600_200_000),
        raw_mail("k4", "tq", "hello", when_ms=1_758_600_300_000,
                 attachments=[("scan.png", png(1300, 1700, b"Q"), "image/png")]),
    ]
    fake, store = _bookmarked_store()
    collector.run(FakeRawGmail(msgs), store, group="ratecon@circledelivers.com")
    return fake, store, hashlib.sha256(page).hexdigest()


def test_mail_from_s3_reaches_the_ledger() -> None:
    """The load loop answers "is there paperwork for this load?" from the ledger, so what the
    collector stores must land there - from S3 alone, with no Gmail call."""
    print("worker: mail from S3 into the ledger")
    import time as _time
    fake, store, page_sha = _collected_store()
    day = dt.date(2025, 9, 23)                                   # when_ms above is 23 Sep 2025 UTC
    conn = fresh_db()

    st = mailsync.ingest_recent(conn, store, days=2, today=day)
    check("every stored message is recorded", st.ingested == 4, st.line())
    check("routed ones on their load, the rest listed unresolved", st.bound == 3 and st.unresolved == 1, st.line())
    m2 = conn.execute("SELECT * FROM message WHERE message_id='k2'").fetchone()
    check("a reply keeps the load its thread gave it", m2["load_id"] == 2589536 and m2["routing_tier"] == "thread",
          str(dict(m2)))
    check("the thread is bound to that load", db.get_thread(conn, "tk")["load_id"] == 2589536)
    check("its load is created and due now", conn.execute(
        "SELECT COUNT(*) FROM load WHERE load_id IN (2589536, 2589777)").fetchone()[0] == 2)
    docs, unread = db.load_doc_evidence(conn, 2589536)
    check("the load now has paperwork on file, unread", docs >= 1 and unread >= 1, f"{docs}/{unread}")
    att = db.get_attachment(conn, page_sha)
    check("one document row for a page that came twice", att is not None and
          conn.execute("SELECT COUNT(*) FROM attachment").fetchone()[0] == 1)
    check("already marked as archived, so nothing re-uploads it", att["s3_key"] == s3store.doc_key(page_sha))
    check("and the mail too", m2["s3_key"] is not None and m2["s3_key"].startswith("mail/"))
    pend = conn.execute("SELECT decision FROM part WHERE message_id='k4'").fetchall()
    check("an unrouted message's document waits as pending", [r[0] for r in pend] == [filters.PENDING],
          str([r[0] for r in pend]))

    st = mailsync.ingest_recent(conn, store, days=2, today=day)
    check("a second pass records nothing twice", st.ingested == 0 and st.known == 4, st.line())

    fake2, store2, _ = _collected_store()
    st = mailsync.ingest_recent(fresh_db(), store2, days=2, today=day, deadline=_time.monotonic() - 1)
    check("past its time limit it stops, and says so", st.out_of_time and st.ingested == 0, st.line())


def test_the_ledger_goes_back_only_over_what_was_taken() -> None:
    """In AWS the S3 copy is the only live ledger. A run may fail; it may never overwrite newer work."""
    print("worker: handing the ledger back")
    fake = FakeS3()
    seed_conn = fresh_db()
    seed_conn.execute("INSERT INTO message (message_id, thread_id) VALUES ('s1','t1')")
    seed_path = Path(seed_conn.execute("PRAGMA database_list").fetchone()["file"])
    seed_conn.close()
    ledger_s3.seed(fake, "b", "ledger/intake.sqlite3", seed_path)
    try:
        ledger_s3.seed(fake, "b", "ledger/intake.sqlite3", seed_path)
    except ledger_s3.LedgerChanged:
        check("seeding refuses to replace a live ledger", True)
    else:
        check("seeding refuses to replace a live ledger", False)

    work = Path(tempfile.mkdtemp(prefix="worker_test_")) / "intake.sqlite3"
    etag = ledger_s3.take(fake, "b", "ledger/intake.sqlite3", work)
    check("the ledger comes down with its ETag", work.exists() and etag.startswith('"'), etag)
    snap = ledger_s3.snapshot(fake, "b", "ledger/intake.sqlite3", etag, today="2026-09-23")
    check("the day's first run snapshots it", snap == "ledger/snapshots/intake-2026-09-23.sqlite3" and snap in fake.objects)
    check("and only the first", ledger_s3.snapshot(fake, "b", "ledger/intake.sqlite3", etag, today="2026-09-23") is None)

    conn = db.connect(work)
    conn.execute("INSERT INTO message (message_id, thread_id) VALUES ('s2','t2')")
    ledger_s3.give_back(fake, "b", "ledger/intake.sqlite3", conn, work, etag)
    back = Path(tempfile.mkdtemp(prefix="worker_test_")) / "back.sqlite3"
    back.write_bytes(fake.objects["ledger/intake.sqlite3"])
    check("the run's work travels, WAL folded in",
          db.connect(back).execute("SELECT COUNT(*) FROM message").fetchone()[0] == 2)

    stale = Path(tempfile.mkdtemp(prefix="worker_test_")) / "intake.sqlite3"
    stale.write_bytes(seed_path.read_bytes())
    try:
        ledger_s3.give_back(fake, "b", "ledger/intake.sqlite3", db.connect(stale), stale, etag)
    except ledger_s3.LedgerChanged:
        check("a stale hand-back is refused, not merged", True)
    else:
        check("a stale hand-back is refused, not merged", False)
    try:
        ledger_s3.take(fake, "b", "ledger/missing.sqlite3", work)
    except Exception:                                            # noqa: BLE001
        check("a missing ledger is an error, never an empty start", True)
    else:
        check("a missing ledger is an error, never an empty start", False)


def test_working_hours() -> None:
    """TransportPro is only called inside the configured window."""
    print("worker: working hours")
    from zoneinfo import ZoneInfo
    from intake import aws_worker
    et = ZoneInfo("America/New_York")
    wed_10 = dt.datetime(2026, 9, 23, 10, 0, tzinfo=et)
    check("a Wednesday morning is inside", aws_worker.working_hours(wed_10, "06-20", "mon-fri")[0])
    check("20:00 is outside", not aws_worker.working_hours(wed_10.replace(hour=20), "06-20", "mon-fri")[0])
    ok, why = aws_worker.working_hours(dt.datetime(2026, 9, 26, 10, 0, tzinfo=et), "06-20", "mon-fri")
    check("a Saturday is outside, and says why", not ok and "sat" in why, why)
    check("'all' covers the weekend", aws_worker.working_hours(dt.datetime(2026, 9, 26, 10, 0, tzinfo=et), "06-20", "all")[0])
    check("a list of days works", aws_worker.working_hours(wed_10, "06-20", "mon,wed")[0])


class _WorkerTPro:
    """TransportPro for the worker: one terminal with one in-scope load, reads only. `doc` is the
    load's document status, shared across instances so a test can change it between runs."""

    doc = {"status": "Waiting for Documents"}

    def __init__(self, **login):
        self.login = login
        self.calls = 0

    def search_all_pages(self, params, max_pages=20):
        self.calls += 1
        return [{**tp_load(doc_status=self.doc["status"]), "id": 2589536}]

    def load(self, load_id):
        self.calls += 1
        return tp_load(doc_status=self.doc["status"])

    def dispatches(self, load_id):
        self.calls += 1
        return [{"id": 1, "status": "At Consignee"}]

    def files(self, load_id):
        self.calls += 1
        return []


def test_worker_run() -> None:
    """One scheduled run end to end: ledger down, mail in, sweep or checks, ledger back."""
    print("worker: one run")
    import json as _json
    import os
    import boto3
    from zoneinfo import ZoneInfo
    from intake import aws_worker

    fake, store, _ = _collected_store()
    seed_conn = fresh_db()
    seed_path = Path(seed_conn.execute("PRAGMA database_list").fetchone()["file"])
    seed_conn.close()
    ledger_s3.seed(fake, "b", "ledger/intake.sqlite3", seed_path)
    fake.objects["config/pod_terminals.json"] = _json.dumps(
        {"terminals": [{"id": 1088, "in_current_view": True}],
         "dashboard_filter": {"service_level": ["Priority / OP8"]}}).encode()

    class _Secrets:
        def get_secret_value(self, SecretId):            # noqa: N803
            return {"SecretString": "p"}                 # the pay-status bot's shape: the password alone

    et = ZoneInfo("America/New_York")
    clock = {"now": dt.datetime(2026, 9, 23, 10, 0, tzinfo=et)}
    made: list[_WorkerTPro] = []
    saved = (boto3.client, aws_worker.tp.TransportPro, aws_worker.local_now, aws_worker.mailsync.ingest_recent,
             dict(os.environ))
    real_ingest = aws_worker.mailsync.ingest_recent

    def ledger_now():
        back = Path(tempfile.mkdtemp(prefix="worker_test_")) / "l.sqlite3"
        back.write_bytes(fake.objects["ledger/intake.sqlite3"])
        return db.connect(back)

    try:
        boto3.client = lambda name, *a, **k: fake if name == "s3" else _Secrets()
        aws_worker.tp.TransportPro = lambda **kw: made.append(_WorkerTPro(**kw)) or made[-1]
        aws_worker.local_now = lambda tz: clock["now"]
        aws_worker.mailsync.ingest_recent = lambda conn, store, **kw: real_ingest(
            conn, store, **{**kw, "today": dt.date(2025, 9, 23)})
        os.environ.update({"INTAKE_S3_BUCKET": "b", "INTAKE_TPRO_SECRET": "s",
                           "INTAKE_TPRO_USERNAME": "u", "INTAKE_TPRO_BASE_URL": "https://tp.example"})

        out = aws_worker.handler({}, None)
        check("mail is recorded every run", "4 new" in out["worker"]["mail"], out["worker"]["mail"])
        check("the day's first run is the full sweep", "full_sweep" in out["worker"] and
              "not this run" in out["worker"]["check"], str(out["worker"]))
        check("logged in with the secret's password and the settings' username",
              made[-1].login == {"base_url": "https://tp.example", "username": "u", "password": "p"})
        check("a JSON secret may carry all three", aws_worker.login_from(
            _json.dumps({"base_url": "https://x", "username": "a", "password": "b"}), {}) ==
            {"base_url": "https://x", "username": "a", "password": "b"})
        try:
            aws_worker.login_from("p", {})
        except ValueError as e:
            check("a password with no username is refused by name", "username" in str(e), str(e))
        else:
            check("a password with no username is refused by name", False)
        conn = ledger_now()
        check("the ledger went back with the mail in it",
              conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 4)
        check("and the dashboard load in view", conn.execute(
            "SELECT in_view FROM load WHERE load_id=2589536").fetchone()[0] == 1)

        out = aws_worker.handler({}, None)
        check("the next run checks the loads that are due", "checked" in out["worker"]["check"]
              and "full_sweep" not in out["worker"], str(out["worker"]))
        check("and sweeps the dashboard every run, not hourly", "sweep" in out["worker"], str(out["worker"]))
        state_now = ledger_now().execute("SELECT state FROM load WHERE load_id=2589536").fetchone()[0]
        check("the load now has a real state", state_now not in ("new", None), str(state_now))
        check("nothing changed yet, so nothing jumps the queue", "changed" not in out["worker"], str(out["worker"]))

        # Somebody else files the paperwork in TransportPro - load 2535232, 23 Sep 2026. The load's
        # timer is an hour away; the sweep sees the status move and it is checked in this run.
        _WorkerTPro.doc["status"] = "Documents Received"
        out = aws_worker.handler({}, None)
        check("a load whose status changed in TransportPro is spotted by the sweep",
              "1 changed document status" in out["worker"]["sweep"], out["worker"]["sweep"])
        check("and checked in the same run, ahead of the queue",
              out["worker"].get("changed", "").startswith("changed in TransportPro: 1 load(s) checked"),
              str(out["worker"]))
        check("and records what TransportPro says now", ledger_now().execute(
            "SELECT doc_status FROM load WHERE load_id=2589536").fetchone()[0] == "Documents Received")
        check("the regular checks do not check it twice", "drain: 0 load(s) checked" in out["worker"]["check"],
              out["worker"]["check"])

        clock["now"] = dt.datetime(2026, 9, 26, 10, 0, tzinfo=et)     # a Saturday
        calls_before = len(made)
        out = aws_worker.handler({}, None)
        check("outside working hours TransportPro is not called", len(made) == calls_before
              and "not checked" in out["worker"]["loads"], str(out["worker"]))
    finally:
        _WorkerTPro.doc["status"] = "Waiting for Documents"
        (boto3.client, aws_worker.tp.TransportPro, aws_worker.local_now,
         aws_worker.mailsync.ingest_recent) = saved[:4]
        os.environ.clear()
        os.environ.update(saved[4])


def test_a_load_the_sweep_sees_is_never_left_unscheduled() -> None:
    """Seen every run and checked never: ten dashboard loads sat as `new` with no next check from
    15 Sep to 23 Sep 2026, because only a sweep that CREATED a row scheduled its first check."""
    print("load loop: nothing in the view goes unscheduled")
    conn = fresh_db()
    for lid, st_ in ((2559369, "new"), (2560001, "not_in_view"), (2560002, "complete"), (2560003, "in_review")):
        conn.execute("INSERT INTO load (load_id, state, source, created_at, next_check_at) VALUES (?,?,?,?,NULL)",
                     (lid, st_, "dashboard", "2026-09-15T18:23:45+00:00"))
    for lid in (2559369, 2560001, 2560002, 2560003):
        db.mark_in_view(conn, lid)
    due = {int(r["load_id"]) for r in db.due_loads(conn, 100)}
    check("a load with no next check is due once the sweep sees it", 2559369 in due, str(due))
    check("so is one that dropped out of the view and came back", 2560001 in due, str(due))
    check("a complete load stays unscheduled", 2560002 not in due)
    check("and so does one a person owns", 2560003 not in due)
    conn.execute("UPDATE load SET next_check_at='2099-01-01T00:00:00+00:00' WHERE load_id=2559369")
    db.mark_in_view(conn, 2559369)
    check("a load that already has a next check keeps it", conn.execute(
        "SELECT next_check_at FROM load WHERE load_id=2559369").fetchone()[0] == "2099-01-01T00:00:00+00:00")


def test_a_dropped_connection_is_retried_not_fatal() -> None:
    """On 23 Sep 2026 TransportPro closed one connection without answering; the raw exception
    skipped the client's retry and stopped a whole check pass."""
    print("transportpro: a dropped connection")
    import http.client
    import io
    from intake import tpro as tp_mod

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    script: list = []

    def fake_urlopen(req, timeout=None):
        step_ = script.pop(0)
        if isinstance(step_, BaseException):
            raise step_
        return _Resp(step_)

    saved = (tp_mod.urllib.request.urlopen, tp_mod.time.sleep)
    try:
        tp_mod.urllib.request.urlopen = fake_urlopen
        tp_mod.time.sleep = lambda s: None
        client = tp_mod.TransportPro("https://tp.example", "u", "p")
        client._access = "token"                     # already logged in

        script[:] = [http.client.RemoteDisconnected("Remote end closed connection without response"),
                     b'{"id": 2591458}']
        check("a dropped connection is retried", client.get("/load/2591458") == {"id": 2591458})

        script[:] = [ConnectionResetError("reset")] * 3
        try:
            client.get("/load/2591458")
        except tp_mod.TProError as e:
            check("and if it keeps failing it is a TProError, which callers handle", "network" in str(e), str(e))
        else:
            check("and if it keeps failing it is a TProError, which callers handle", False)

        class _Flaky:
            calls = 0

            def load(self, load_id):
                if load_id == 2591458:
                    raise tp_mod.TProError(0, "/load/2591458", "network: RemoteDisconnected")
                return tp_load()

            def dispatches(self, load_id):
                return [{"id": 1, "status": "Loaded"}]

            def files(self, load_id):
                return []

        conn = fresh_db()
        for lid in (2591458, 2591459):
            db.upsert_load(conn, lid, source="dashboard", due_now=True)
            db.mark_in_view(conn, lid)
        ds = loadloop.drain(conn, _Flaky(), limit=10)
        check("one unreachable load is deferred and the pass carries on", ds.checked == 1 and ds.errors == 1, ds.line())
    finally:
        tp_mod.urllib.request.urlopen, tp_mod.time.sleep = saved


# ------------------------------------------------------------------ auto-upload (the pilot pod) ----

def _picture(seed: int, fmt: str = "PNG") -> bytes:
    """A page-sized image unlike any other seed's: a scatter of grey blocks on white."""
    import io
    from PIL import Image, ImageDraw
    im = Image.new("L", (400, 520), 255)
    d = ImageDraw.Draw(im)
    for i in range(14):
        x, y = (seed * 37 + i * 53) % 340, (seed * 91 + i * 41) % 460
        d.rectangle([x, y, x + 60, y + 50], fill=(seed * 29 + i * 17) % 180)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format=fmt, quality=90) if fmt == "JPEG" else im.save(buf, format=fmt)
    return buf.getvalue()


def _reading(load_id: int, doc_type: str = "bill_of_lading", *, receiver: bool = False, conf: float = 0.95,
             numbers: list[tuple[str, str]] | None = None, notes: str = "", city: str | None = "Atlanta") -> dict:
    numbers = [("Pickup #", f"P{load_id}"), ("PO", f"PO{load_id}")] if numbers is None else numbers
    return _extraction(doc_type, document_type_confidence=conf, notes=notes,
                       numbers=[{"label": lb, "kind": "other", "value": v, "handwritten": False, "confidence": 0.9}
                                for lb, v in numbers],
                       consignee={"city": city} if city else {},
                       signatures={"shipper_signed": True, "driver_signed": True, "receiver_signed": receiver,
                                   "receiver_name": "Kendyl" if receiver else None,
                                   "receiver_date": "9/24/26" if receiver else None, "stamp_present": False},
                       pages=[{"page": 1, "role": "pod" if receiver else "bol", "legibility": 0.9}])


def _tp_auto(load_id: int, doc: str = "Waiting for Documents") -> dict:
    return {"id": load_id, "status": {"documentStatus": doc},
            "reference": {"pickupNumber": f"P{load_id}", "poNumber": f"PO{load_id}", "numberOfPieces": 2180,
                          "weight": 42992.2, "equipmentType": "Van or Reefer"},
            "waypoints": [{"type": "SH", "location": {"city": "Tarrs"},
                           "reference": [{"type": "SERVICE_LEVEL", "value": "Priority / OP8"}]},
                          {"type": "CN", "location": {"city": "Atlanta"}}]}


class _AutoTPro:
    """TransportPro for the auto-upload: loads, File History with bytes, and an upload that files."""

    def __init__(self):
        self.loads: dict[int, dict] = {}
        self.uploads: list[dict] = []
        self.next_id = 900
        self.fail_uploads = 0
        self.calls = 0

    def add(self, load_id, doc="Waiting for Documents", files=()):
        self.loads[load_id] = {"load": _tp_auto(load_id, doc), "files": [], "bytes": {}}
        for f, data in files:
            self.loads[load_id]["files"].append(f)
            self.loads[load_id]["bytes"][f["id"]] = data

    def load(self, load_id):
        self.calls += 1
        return self.loads[load_id]["load"]

    def files(self, load_id):
        self.calls += 1
        return [dict(f) for f in self.loads[load_id]["files"]]

    def download_file(self, file_id):
        self.calls += 1
        for v in self.loads.values():
            if file_id in v["bytes"]:
                return v["bytes"][file_id], {}
        raise KeyError(file_id)

    def upload_file(self, *, record_type, record_id, document_type, comments, filename, data, content_type):
        from intake.tpro import TProError
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise TProError(500, "/files/upload", "backend hiccup")
        self.next_id += 1
        f = {"id": self.next_id, "fileTypeId": {"Bill Of Lading": 12, "Driver Supplied BOL": 363}[document_type],
             "fileTypeName": document_type, "comments": comments, "uploadById": 4211,
             "dateCreated": "2026-09-24T15:00:00Z", "fileName": filename}
        self.loads[record_id]["files"].append(f)
        self.loads[record_id]["bytes"][self.next_id] = data
        self.uploads.append({"load": record_id, "type": document_type, "comment": comments,
                             "filename": filename, "data": data, "content_type": content_type})
        return {"STATUS": "SUCCESS", "MESSAGE": "File uploaded", "result": {"id": self.next_id}}


class _AutoLog:
    def __init__(self):
        self.rows: dict[str, list] = {}
        self.writes = 0

    def write(self, entries):
        self.writes += 1
        for ref, values in entries:
            self.rows[ref] = values
        return len(entries)


def _auto_world():
    """A ledger, an S3 archive, TransportPro and a reader for the Frankie Saiz pod's loads."""
    import hashlib as _h
    conn = fresh_db()
    fake = FakeS3()
    store = s3store.Store("b", "", client=fake)
    tpro = _AutoTPro()
    readings: dict[str, dict] = {}
    reads: list[str] = []

    def read(data, filename):
        sha = _h.sha256(data).hexdigest()
        reads.append(sha)
        ex = readings[sha]
        return ex, ex["document_type"], "claude-opus-5", 0.05

    def load_row(load_id, state_, stage, terminal=1160):
        conn.execute("INSERT INTO load (load_id, state, stage, terminal, customer, in_view, last_checked_at, "
                     "next_check_at, source) VALUES (?,?,?,?,?,1,?,?,'dashboard')",
                     (load_id, state_, stage, terminal, "Spindrift Beverage Co Inc.", db.now_iso(), db.now_iso()))

    def mail(load_id, mid, pages: list[tuple[bytes, dict]]):
        conn.execute("INSERT INTO message (message_id, thread_id, internal_date, from_domain, load_id, "
                     "routing_tier, part_count) VALUES (?,?,?,?,?,?,?)",
                     (mid, "t" + mid, "2026-09-24T12:00:00+00:00", "shipwell.com", load_id, "subject", len(pages)))
        shas = []
        for i, (data, reading) in enumerate(pages):
            sha = _h.sha256(data).hexdigest()
            conn.execute("INSERT INTO part (message_id, part_id, attachment_id, filename, sha256, decision) "
                         "VALUES (?,?,?,?,?,'keep')", (mid, str(i), f"a{i}", f"{mid}_{i}.png", sha))
            fake.objects[s3store.doc_key(sha)] = data
            readings[sha] = reading
            shas.append(sha)
        return shas

    def on_load(data, reading):
        readings[_h.sha256(data).hexdigest()] = reading

    return conn, store, tpro, read, reads, load_row, mail, on_load


def test_auto_upload_facts_and_pictures() -> None:
    print("auto-upload: facts and pictures")
    from intake import autofile
    from pod_intake.schema import Extraction

    load = _tp_auto(2600001)
    ex = Extraction.model_validate(_reading(2600001))
    facts, strong = autofile.match_facts(ex, load)
    check("reference numbers on the page match the load", strong == 2 and "pickup # P2600001" in facts, str(facts))
    check("and the consignee city is a fact, but a weak one", "consignee city Atlanta" in facts, str(facts))
    ex = Extraction.model_validate(_reading(2600001, numbers=[("Cases", "2180")]))
    facts, strong = autofile.match_facts(ex, load)
    check("a piece count and a city are two facts with no reference number", len(facts) == 2 and strong == 0,
          str(facts))
    ex = Extraction.model_validate(_reading(2600001, numbers=[("Year", "2026"), ("Ref", "26000")]))
    check("a year or a fragment is not a reference number", autofile.match_facts(ex, load)[1] == 0)
    # Load 2562069: the BOL says "Cust PO 18650", TransportPro says PO18650; its reference block also
    # carries reeferTemperatureMode "Continuous", which is a setting, not a number anybody prints.
    reefer = {**_tp_auto(2562069), "reference": {"poNumber": "PO18650", "reeferTemperatureMode": "Continuous"}}
    ex = Extraction.model_validate(_reading(2562069, numbers=[("Cust PO", "18650")]))
    facts, strong = autofile.match_facts(ex, reefer)
    check("the same digits with TransportPro's letters round them match", strong == 1 and "PO # PO18650" in facts,
          str(facts))
    check("a setting in the reference block is never a fact",
          not any(f[3] == "Continuous" for f in autofile.load_facts(reefer)))

    a, b = autofile.picture_sig(_picture(1)), autofile.picture_sig(_picture(2))
    again = autofile.picture_sig(_picture(1, "JPEG"))
    check("two different pages are different pictures", autofile._diff(a[0], b[0]) > autofile.SAME_PICTURE * 3,
          f"{autofile._diff(a[0], b[0]):.1f}")
    check("the same page re-saved as JPEG is the same picture", autofile._diff(a[0], again[0]) < autofile.SAME_PICTURE,
          f"{autofile._diff(a[0], again[0]):.1f}")
    one = autofile.upload_payload([_picture(1, "JPEG")], "POD", 1)
    check("every upload is a PDF, a lone JPEG included", one[0][:5] == b"%PDF-" and one[1] == "POD_1.pdf"
          and autofile._diff(autofile.picture_sig(one[0])[0], autofile.picture_sig(_picture(1, "JPEG"))[0]) < autofile.SAME_PICTURE)
    pdf = autofile.combine_pdf([_picture(1), _picture(2)], "BOL - load 1")
    check("pages combine into one PDF, one page each", pdf[:5] == b"%PDF-" and len(autofile.picture_sig(pdf)) == 2)
    check("and its pages are still the same pictures",
          autofile._diff(autofile.picture_sig(pdf)[1], b[0]) < autofile.SAME_PICTURE)

    s = autofile.Settings.from_env({"INTAKE_AUTO_UPLOAD": "on", "INTAKE_AUTO_TERMINALS": "1160, 1088"})
    check("settings: mode and terminals", s.mode == "on" and s.terminals == {1160, 1088})
    check("settings: off unless set", autofile.Settings.from_env({}).mode == "off")
    try:
        autofile.Settings.from_env({"INTAKE_AUTO_UPLOAD": "yes"})
    except ValueError:
        check("settings: a mode that is not off/dry-run/on is refused", True)
    else:
        check("settings: a mode that is not off/dry-run/on is refused", False)
    # Load 2573776: the reader's aside on the year went into the comment, and a word cap left a stray "3".
    messy = _reading(2573776, "proof_of_delivery", receiver=True)
    messy["signatures"]["receiver_date"] = "9/24/24 (as written; likely 9/24/26)"
    messy["pages"] = [{"page": i, "role": "pod", "legibility": 0.9} for i in (1, 2, 3)]
    dec = autofile.Decision(autofile.Doc("x", "f.pdf", "email:m", "Email"), "ready", kind="POD",
                            ex=Extraction.model_validate(messy))
    check("a comment carries what is on the page, not the reader's asides",
          autofile.upload_comment([dec], "POD", 2573776) == "Doc Intake Bot: POD, signed by Kendyl 9/24/24, 3 pages - load 2573776",
          autofile.upload_comment([dec], "POD", 2573776))
    check("the pod's name comes from its terminal",
          autofile.pod_names({"terminals": [{"id": 1160, "name": "POD (Frankie Saiz)"}]}) == {1160: "Frankie Saiz"})


def test_auto_upload_pilot() -> None:
    """The Frankie Saiz pilot end to end: what is uploaded, as what, and what is only logged."""
    print("auto-upload: the pilot pod")
    import time as _time
    from intake import autofile

    conn, store, tpro, read, reads, load_row, mail, on_load = _auto_world()
    s = autofile.Settings(terminals=frozenset({1160}), mode="on", pods={1160: "Frankie Saiz"})
    texted = {"id": 501, "fileTypeId": 363, "fileTypeName": "Driver Supplied BOL", "uploadById": 1,
              "comments": "Driver Supplied Image - 2600001", "dateCreated": "2026-09-24T11:00:00Z"}
    by_rep = {"id": 502, "fileTypeId": 363, "fileTypeName": "Driver Supplied BOL", "uploadById": 2756,
              "comments": "bol in em", "dateCreated": "2026-09-23T14:25:38Z"}
    proper_pod = {"id": 503, "fileTypeId": 360, "fileTypeName": "Proof of Delivery", "uploadById": 4985,
                  "comments": "", "dateCreated": "2026-09-24T09:00:00Z"}

    # A: the driver texted the signed POD; TransportPro filed it as Driver Supplied BOL.
    load_row(2600001, "wrong_doc_type", "delivered")
    tpro.add(2600001, files=[(texted, _picture(1))])
    on_load(_picture(1), _reading(2600001, "proof_of_delivery", receiver=True))
    # B: two emailed pages of one pickup BOL.
    load_row(2600002, "bol_expected", "loaded")
    tpro.add(2600002)
    mail(2600002, "mb", [(_picture(2), _reading(2600002)), (_picture(3), _reading(2600002))])
    # C: a signed POD emailed while TransportPro still has the truck loaded.
    load_row(2600003, "pod_expected", "loaded")
    tpro.add(2600003)
    (c_sha,) = mail(2600003, "mc", [(_picture(4), _reading(2600003, "proof_of_delivery", receiver=True))])
    # D: the BOL a rep already filed, and the same page emailed as a JPEG.
    load_row(2600004, "wrong_doc_type", "delivered")
    tpro.add(2600004, files=[(by_rep, _picture(5))])
    on_load(_picture(5), _reading(2600004))
    mail(2600004, "md", [(_picture(5, "JPEG"), _reading(2600004))])
    # E: a BOL the AI is unsure of, one that matches only on a city, a freight photo, a licence.
    load_row(2600005, "bol_expected", "at shipper")
    tpro.add(2600005)
    mail(2600005, "me", [(_picture(6), _reading(2600005, conf=0.6)),
                         (_picture(7), _reading(2600005, numbers=[])),
                         (_picture(8), _reading(2600005, "photo")),
                         (_picture(9), _reading(2600005, notes="a photo of the driver's license"))])
    # F: another pod's load. G: already Documents Received. I: a proper POD already on file.
    load_row(2600006, "bol_expected", "loaded", terminal=1088)
    tpro.add(2600006)
    mail(2600006, "mf", [(_picture(10), _reading(2600006))])
    load_row(2600007, "bol_expected", "delivered")
    tpro.add(2600007, doc="Documents Received")
    mail(2600007, "mg", [(_picture(11), _reading(2600007))])
    load_row(2600009, "pod_expected", "delivered")
    tpro.add(2600009, files=[(proper_pod, _picture(12))])
    mail(2600009, "mi", [(_picture(13), _reading(2600009, "proof_of_delivery", receiver=True))])

    log = _AutoLog()
    run = lambda: autofile.run(conn, tpro, store, read, log, s, deadline=_time.monotonic() + 600)  # noqa: E731
    st1 = run()
    ups = {u["load"]: u for u in tpro.uploads}
    check("two uploads: the texted POD and the two-page BOL", sorted(ups) == [2600001, 2600002], str(st1.line()))
    check("a POD goes in as Bill Of Lading", ups[2600001]["type"] == "Bill Of Lading", ups[2600001]["type"])
    check("its comment says what it is, and which copy it re-files",
          ups[2600001]["comment"] == "Doc Intake Bot: POD, signed by Kendyl 9/24/26, copy of Driver Supplied BOL 501 "
                                     "- load 2600001", ups[2600001]["comment"])
    check("a single photo goes up as a one-page PDF", ups[2600001]["data"][:5] == b"%PDF-"
          and ups[2600001]["content_type"] == "application/pdf" and ups[2600001]["filename"].endswith(".pdf")
          and len(autofile.picture_sig(ups[2600001]["data"])) == 1)
    check("a BOL goes in as Driver Supplied BOL", ups[2600002]["type"] == "Driver Supplied BOL")
    check("its two pages as one PDF", ups[2600002]["data"][:5] == b"%PDF-"
          and len(autofile.picture_sig(ups[2600002]["data"])) == 2)
    check("with a comment that starts with the bot's name",
          ups[2600002]["comment"] == "Doc Intake Bot: BOL, shipper signed, 2 pages - load 2600002",
          ups[2600002]["comment"])
    status = {(r["load_id"], r["outcome"]) for r in conn.execute("SELECT load_id, outcome FROM autofile")}
    check("the early POD waits for the consignee", (2600003, "waiting") in status, str(status))
    check("a page already on the load is not uploaded again", (2600004, "on_file") in status
          and not any(u["load"] == 2600004 for u in tpro.uploads), str(status))
    check("an unsure read and a city-only match are held for a person",
          conn.execute("SELECT COUNT(*) FROM autofile WHERE load_id=2600005 AND outcome='held'").fetchone()[0] == 2)
    check("a POD is not uploaded over a proper POD already on file", (2600009, "not_needed") in status, str(status))
    check("another pod's load is never touched", not any(r[0] == 2600006 for r in status)
          and 2600006 not in {u["load"] for u in tpro.uploads})
    check("a load already showing Documents Received is not read", not any(r[0] == 2600007 for r in status))
    refs = set(log.rows)
    check("the Upload log holds the uploads and the holds, one row each", len(refs) == 5
          and {v[13].split(" ")[0] for v in log.rows.values()} == {"UPLOADED", "HELD"}, str(sorted(refs)))
    check("already on file, not needed and waiting stay in the ledger, not the sheet", conn.execute(
        "SELECT COUNT(*) FROM autofile WHERE outcome IN ('on_file','not_needed','waiting') AND logged=1").fetchone()[0] == 4
          and not any(v[1] in (2600003, 2600004, 2600009) for v in log.rows.values()))
    held = next(v for v in log.rows.values() if v[13].startswith("HELD"))
    check("a held row says which check failed", held[9].startswith("FAILED: "), held[9])
    logged_loads = {v[1] for v in log.rows.values()}
    check("the freight photo and the licence are not in it", conn.execute(
        "SELECT COUNT(*) FROM autofile WHERE load_id=2600005 AND logged=0").fetchone()[0] == 2
          and 2600006 not in logged_loads)
    row_a = next(v for v in log.rows.values() if v[1] == 2600001)
    check("a row says who, what, how sure, the checks and the upload",
          row_a[3] == "Frankie Saiz" and row_a[6].startswith("POD") and row_a[7] == "95%"
          and row_a[9] == "all passed" and row_a[10] == "Bill Of Lading" and row_a[12] == "901"
          and row_a[13].startswith("UPLOADED") and "Text message from the driver" in row_a[5], str(row_a))
    check("the upload is recorded as filed", conn.execute(
        "SELECT COUNT(*) FROM filing WHERE load_id IN (2600001, 2600002)").fetchone()[0] == 3)
    check("and the load is due for a check straight away",
          conn.execute("SELECT next_check_at <= ? FROM load WHERE load_id=2600001", (db.now_iso(),)).fetchone()[0] == 1)
    check("the day's AI spend is kept", float(db.get_state(
        conn, f"ai_spend:{dt.datetime.now(dt.timezone.utc).date().isoformat()}")) > 0)

    n_reads, n_uploads, n_writes = len(reads), len(tpro.uploads), log.writes
    st2 = run()
    check("a second run uploads nothing and reads nothing", len(tpro.uploads) == n_uploads
          and len(reads) == n_reads, st2.line())
    check("and rewrites no sheet row", st2.logged == 0, st2.line())

    # The truck reaches the consignee: the load check records it, and the waiting POD goes up.
    conn.execute("UPDATE load SET stage='at consignee', last_checked_at=? WHERE load_id=2600003",
                 ("9999-01-01T00:00:00+00:00",))
    st3 = run()
    last = tpro.uploads[-1]
    check("the POD goes up once the truck is at the consignee", len(tpro.uploads) == n_uploads + 1
          and last["load"] == 2600003 and last["type"] == "Bill Of Lading", st3.line())
    check("and it reaches the sheet once it is uploaded", st3.logged == 1
          and log.rows[autofile.ref(2600003, c_sha)][13].startswith("UPLOADED") and len(log.rows) == 6)

    # A copy of the texted POD then arrives by email, and a second pickup BOL for B.
    mail(2600001, "ma2", [(_picture(1, "JPEG"), _reading(2600001, "proof_of_delivery", receiver=True))])
    mail(2600002, "mb2", [(_picture(14), _reading(2600002))])
    before = len(tpro.uploads)
    run()
    status = {(r["load_id"], r["source"]): (r["outcome"], r["status"]) for r in conn.execute(
        "SELECT load_id, source, outcome, status FROM autofile")}
    check("the emailed copy of a POD the bot already filed is not uploaded again", len(tpro.uploads) == before
          and status[(2600001, "email:ma2")][0] == "on_file", str(status.get((2600001, "email:ma2"))))
    check("a second BOL for a load that has one is not needed",
          status[(2600002, "email:mb2")][0] == "not_needed", str(status.get((2600002, "email:mb2"))))

    # An upload that fails is tried again next run, after a fresh look at File History.
    load_row(2600008, "bol_expected", "loaded")
    tpro.add(2600008)
    (h_sha,) = mail(2600008, "mh", [(_picture(15), _reading(2600008))])
    tpro.fail_uploads = 1
    run()
    check("a failed upload waits and is retried", conn.execute(
        "SELECT outcome, attempts FROM autofile WHERE load_id=2600008").fetchone()[:] == ("waiting", 1))
    run()
    check("and goes up on the next run, once", sum(u["load"] == 2600008 for u in tpro.uploads) == 1
          and conn.execute("SELECT outcome FROM autofile WHERE load_id=2600008").fetchone()[0] == "uploaded")

    # J: the delivery copy photographed page by page - only the page the receiver signed reads as a
    # POD (load 2562005). K: a "POD" whose only delivery evidence is a time (load 2562069's dash clock).
    load_row(2600010, "wrong_doc_type", "delivered")
    tpro.add(2600010)
    j1, j2 = mail(2600010, "mj", [(_picture(16), _reading(2600010, conf=0.93)),
                                  (_picture(17), _reading(2600010, "proof_of_delivery", receiver=True))])
    load_row(2600011, "wrong_doc_type", "delivered")
    tpro.add(2600011)
    clock = _reading(2600011)
    clock["times"] = {"check_out": "12:34", "source": "handwritten", "at_stop": "unknown"}
    mail(2600011, "mk", [(_picture(18), clock)])
    run()
    up = tpro.uploads[-1]
    check("the POD's other pages from the same email go up with it, as one PDF", up["load"] == 2600010
          and up["type"] == "Bill Of Lading" and len(autofile.picture_sig(up["data"])) == 2, str(up["comment"]))
    check("and the comment is the POD's", up["comment"] == "Doc Intake Bot: POD, signed by Kendyl 9/24/26, "
          "2 pages - load 2600010", up["comment"])
    check("both pages' rows say uploaded", all(log.rows[autofile.ref(2600010, x)][13].startswith("UPLOADED")
                                               for x in (j1, j2)))
    check("a POD with no signature or stamp is held, not uploaded",
          not any(u["load"] == 2600011 for u in tpro.uploads) and conn.execute(
              "SELECT outcome FROM autofile WHERE load_id=2600011").fetchone()[0] == "held")


def test_auto_upload_texted_pages() -> None:
    """Load 2577917 (24 Sep 2026): the delivery copy texted as two pictures 11 seconds apart. Both
    pages go up together, as they would from one email - and a page logged before its POD arrived
    still joins it."""
    print("auto-upload: pages texted one by one")
    import time as _time
    from intake import autofile

    conn, store, tpro, read, reads, load_row, mail, on_load = _auto_world()
    s = autofile.Settings(terminals=frozenset({1160}), mode="on", pods={1160: "Frankie Saiz"})
    log = _AutoLog()

    def text(fid, when):
        return {"id": fid, "fileTypeId": 363, "fileTypeName": "Driver Supplied BOL", "uploadById": 1,
                "comments": "Driver Supplied Image - load", "dateCreated": when}

    run = lambda: autofile.run(conn, tpro, store, read, log, s, deadline=_time.monotonic() + 600)  # noqa: E731

    load_row(2600020, "wrong_doc_type", "delivered")
    tpro.add(2600020, files=[(text(600, "2026-09-23T20:09:15Z"), _picture(20)),      # the pickup BOL, hours before
                             (text(601, "2026-09-24T11:13:28Z"), _picture(21)),      # page 1 of the delivery copy
                             (text(603, "2026-09-24T11:13:33Z"), _picture(21, "JPEG")),   # page 1 again
                             (text(602, "2026-09-24T11:13:39Z"), _picture(22))])     # page 2, signed
    on_load(_picture(21, "JPEG"), _reading(2600020, conf=0.93))
    on_load(_picture(20), _reading(2600020))
    on_load(_picture(21), _reading(2600020, conf=0.93))
    on_load(_picture(22), _reading(2600020, "proof_of_delivery", receiver=True))
    run()
    up = [u for u in tpro.uploads if u["load"] == 2600020]
    check("the texted pages of the delivery copy go up as one PDF", len(up) == 1
          and len(autofile.picture_sig(up[0]["data"])) == 2, str([u["comment"] for u in up]))
    check("as Bill Of Lading, naming both texts it copies", up[0]["type"] == "Bill Of Lading" and up[0]["comment"] ==
          "Doc Intake Bot: POD, signed by Kendyl 9/24/26, 2 pages, copy of Driver Supplied BOL 601 + 602 - load 2600020",
          up[0]["comment"])
    check("the pickup BOL sent hours earlier is not part of it", conn.execute(
        "SELECT outcome FROM autofile WHERE source='tpro:600'").fetchone()[0] == "on_file")
    check("nor is a second shot of page 1", conn.execute(
        "SELECT outcome FROM autofile WHERE source='tpro:603'").fetchone()[0] == "on_file")

    # Page 1 arrives and is logged; page 2 comes in 20 seconds later, after that run.
    load_row(2600021, "wrong_doc_type", "delivered")
    tpro.add(2600021, files=[(text(611, "2026-09-24T12:00:00Z"), _picture(23))])
    on_load(_picture(23), _reading(2600021, conf=0.93))
    run()
    check("a lone first page is only logged", conn.execute(
        "SELECT outcome FROM autofile WHERE source='tpro:611'").fetchone()[0] == "on_file"
          and not any(u["load"] == 2600021 for u in tpro.uploads))
    tpro.loads[2600021]["files"].append(text(612, "2026-09-24T12:00:20Z"))
    tpro.loads[2600021]["bytes"][612] = _picture(24)
    on_load(_picture(24), _reading(2600021, "proof_of_delivery", receiver=True))
    conn.execute("UPDATE load SET last_checked_at=? WHERE load_id=2600021", ("9999-01-01T00:00:00+00:00",))
    run()
    up = [u for u in tpro.uploads if u["load"] == 2600021]
    check("when its signed page arrives, both go up together", len(up) == 1
          and len(autofile.picture_sig(up[0]["data"])) == 2, str([u["comment"] for u in up]))
    check("and the first page's row now says uploaded", conn.execute(
        "SELECT outcome FROM autofile WHERE source='tpro:611'").fetchone()[0] == "uploaded"
          and next(v for k, v in log.rows.items() if k.startswith("2600021-") and v[4].startswith("611"))[13]
          .startswith("UPLOADED"))


def test_auto_upload_bol_sets() -> None:
    """Load 2576831 (24 Sep 2026): a pick slip photographed front and back. The front has the load
    number and customer PO; the back, the driver's sign-out, has none - held on its own. Sent
    together, they go up together."""
    print("auto-upload: a BOL's pages sent together")
    import time as _time
    from intake import autofile

    conn, store, tpro, read, reads, load_row, mail, on_load = _auto_world()
    s = autofile.Settings(terminals=frozenset({1160}), mode="on", pods={1160: "Frankie Saiz"})
    log = _AutoLog()

    def page(load_id, *, conf, numbers, shipper_signed, city="Atlanta"):
        r = _reading(load_id, conf=conf, numbers=[(lb, v) for lb, v, _ in numbers], city=city)
        for n, (_, _, kind) in zip(r["numbers"], numbers):
            n["kind"] = kind
        r["signatures"].update({"shipper_signed": shipper_signed, "driver_signed": shipper_signed})
        return r

    def front(load_id):
        return page(load_id, conf=0.9, numbers=[("Load Number", f"P{load_id}", "load_or_trip"), ("Customer PO", f"PO{load_id}", "po")],
                    shipper_signed=False)

    def back(load_id, extra=()):
        return page(load_id, conf=0.72, numbers=[("Seal.", "1388444", "seal"), ("Trailer #", "147", "trailer"), *extra],
                    shipper_signed=True, city=None)

    run = lambda: autofile.run(conn, tpro, store, read, log, s, deadline=_time.monotonic() + 600)  # noqa: E731
    load_row(2600040, "bol_expected", "loaded")
    tpro.add(2600040)
    f40, b40 = mail(2600040, "m40", [(_picture(40), front(2600040)), (_picture(41), back(2600040))])
    load_row(2600041, "bol_expected", "loaded")                  # a back page naming another shipment
    tpro.add(2600041)
    mail(2600041, "m41", [(_picture(42), front(2600041)), (_picture(43), back(2600041, [("PO", "PO7777777", "po")]))])
    load_row(2600042, "bol_expected", "loaded")                  # the back sent in a different email
    tpro.add(2600042)
    mail(2600042, "m42a", [(_picture(44), front(2600042))])
    (b42,) = mail(2600042, "m42b", [(_picture(45), back(2600042))])
    run()
    up = {u["load"]: u for u in tpro.uploads}
    check("the front and its sign-out side go up together, as Driver Supplied BOL",
          up[2600040]["type"] == "Driver Supplied BOL" and len(autofile.picture_sig(up[2600040]["data"])) == 2,
          str(up.get(2600040, {}).get("comment")))
    check("and the comment says it was signed, from the sign-out side",
          up[2600040]["comment"] == "Doc Intake Bot: BOL, shipper signed, 2 pages - load 2600040", up[2600040]["comment"])
    check("the sign-out side's row says uploaded", log.rows[autofile.ref(2600040, b40)][13].startswith("UPLOADED"))
    check("a back page that names a different shipment does not join",
          len(autofile.picture_sig(up[2600041]["data"])) == 1 and conn.execute(
              "SELECT outcome FROM autofile WHERE load_id=2600041 AND source='email:m41' ORDER BY outcome").fetchall()[0][0] == "held")
    check("nor does one that came in another email", len(autofile.picture_sig(up[2600042]["data"])) == 1
          and conn.execute("SELECT outcome FROM autofile WHERE sha256=?", (b42,)).fetchone()[0] == "held")


def test_auto_upload_quick_look() -> None:
    """The cheap first look decides which pages get the full read (24 Sep 2026). It may set aside a
    photo, and a texted picture while the truck is not at the consignee; it is never trusted to say a
    page at the consignee is not a POD - it called 2577917's signed page 2 an unsigned BOL."""
    print("auto-upload: the quick look")
    import hashlib as _h
    import json
    import time as _time
    from intake import autofile

    conn, store, tpro, read, reads, load_row, mail, on_load = _auto_world()
    s = autofile.Settings(terminals=frozenset({1160}), mode="on", pods={1160: "Frankie Saiz"})
    log = _AutoLog()
    said: dict[str, tuple[str, float]] = {}
    looked: list[str] = []

    def quick(data, filename):
        sha = _h.sha256(data).hexdigest()
        looked.append(sha)
        kind, conf = said[sha]
        return kind, conf, "claude-haiku-4-5", 0.002

    def text(fid, when):
        return {"id": fid, "fileTypeId": 363, "fileTypeName": "Driver Supplied BOL", "uploadById": 1,
                "comments": "Driver Supplied Image - load", "dateCreated": when}

    def sha(data):
        return _h.sha256(data).hexdigest()

    run = lambda: autofile.run(conn, tpro, store, read, log, s, deadline=_time.monotonic() + 600, quick=quick)  # noqa: E731

    # A: at the shipper. An emailed freight photo, an unsure one, and the pickup BOL the driver texted.
    load_row(2600030, "bol_expected", "loaded")
    tpro.add(2600030, files=[(text(701, "2026-09-24T09:00:00Z"), _picture(30))])
    on_load(_picture(30), _reading(2600030))
    said[sha(_picture(30))] = ("bol", 0.95)
    mail(2600030, "ma", [(_picture(31), _reading(2600030, "photo")), (_picture(32), _reading(2600030, "photo"))])
    said[sha(_picture(31))] = ("photo", 0.97)
    said[sha(_picture(32))] = ("photo", 0.6)
    # B: at the consignee. The signed POD texted, which the quick look calls an unsigned BOL.
    load_row(2600031, "wrong_doc_type", "at consignee")
    tpro.add(2600031, files=[(text(711, "2026-09-24T11:13:39Z"), _picture(33))])
    on_load(_picture(33), _reading(2600031, "proof_of_delivery", receiver=True))
    said[sha(_picture(33))] = ("bol", 0.95)
    # C: an emailed copy of a picture already read on the load.
    load_row(2600032, "wrong_doc_type", "delivered")
    tpro.add(2600032, files=[(text(721, "2026-09-24T08:00:00Z"), _picture(34))])
    on_load(_picture(34), _reading(2600032))
    said[sha(_picture(34))] = ("bol", 0.9)
    run()
    full = {x for x in reads}
    check("a photo the quick look is sure of gets no full read", sha(_picture(31)) not in full
          and conn.execute("SELECT outcome FROM autofile WHERE sha256=?", (sha(_picture(31)),)).fetchone()[0] == "not_bol_pod")
    check("one it is unsure of does", sha(_picture(32)) in full)
    check("a BOL texted before the consignee is only looked at, and recorded as on file", sha(_picture(30)) not in full
          and conn.execute("SELECT outcome FROM autofile WHERE sha256=?", (sha(_picture(30)),)).fetchone()[0] == "on_file"
          and json.loads(conn.execute("SELECT row_json FROM autofile WHERE sha256=?", (sha(_picture(30)),)).fetchone()[0])[6]
          .startswith("BOL (quick look"))
    check("at the consignee a texted page gets the full read, whatever the quick look said",
          sha(_picture(33)) in full and any(u["load"] == 2600031 and u["type"] == "Bill Of Lading" for u in tpro.uploads))
    reads_before = len(reads)
    mail(2600032, "mc", [(_picture(34, "JPEG"), _reading(2600032))])
    said[sha(_picture(34, "JPEG"))] = ("bol", 0.9)
    run()
    check("the same picture already read is not read again", len(reads) == reads_before and conn.execute(
        "SELECT model, cost_usd FROM attachment WHERE sha256=?", (sha(_picture(34, "JPEG")),)).fetchone()[:] ==
          (f"reused from {sha(_picture(34))[:12]}", 0.0))
    check("and is decided on the reused reading", conn.execute(
        "SELECT outcome FROM autofile WHERE sha256=?", (sha(_picture(34, "JPEG")),)).fetchone()[0] in ("on_file", "not_needed"))

    # D: page 1 texted while TransportPro still says loaded - only looked at - then the signed page 2
    # 20 seconds later, once the truck shows at the consignee. Both pages go up, page 1 read in full.
    load_row(2600033, "wrong_doc_type", "loaded")
    tpro.add(2600033, files=[(text(731, "2026-09-24T12:00:00Z"), _picture(35))])
    on_load(_picture(35), _reading(2600033, conf=0.93))
    said[sha(_picture(35))] = ("bol", 0.95)
    run()
    check("page 1 is set aside by the quick look", sha(_picture(35)) not in reads)
    tpro.loads[2600033]["files"].append(text(732, "2026-09-24T12:00:20Z"))
    tpro.loads[2600033]["bytes"][732] = _picture(36)
    on_load(_picture(36), _reading(2600033, "proof_of_delivery", receiver=True))
    said[sha(_picture(36))] = ("pod", 0.9)
    conn.execute("UPDATE load SET stage='at consignee', last_checked_at=? WHERE load_id=2600033", ("9999-01-01T00:00:00+00:00",))
    run()
    up = [u for u in tpro.uploads if u["load"] == 2600033]
    check("when its signed page 2 arrives, page 1 gets its full read and both go up",
          sha(_picture(35)) in reads and len(up) == 1 and len(autofile.picture_sig(up[0]["data"])) == 2,
          str([u["comment"] for u in up]))
    spent = float(db.get_state(conn, f"ai_spend:{dt.datetime.now(dt.timezone.utc).date().isoformat()}"))
    check("the quick looks are counted in the day's spend", spent > 0.05 * len(reads) - 1e-9 and len(looked) >= 7)


def test_reader_is_brief_and_caches_the_schema() -> None:
    """On Bedrock the schema travels in the prompt: it goes in the cached system block, not after the
    images; low effort is sent on its own, and an endpoint that refuses it is asked once."""
    print("reader: brief, cached, low effort")
    import anthropic
    import httpx2 as httpx
    from pod_intake import reader as rd
    from pod_intake.schema import Extraction

    calls: list[dict] = []

    class _Msg:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.stop_reason = "end_turn"
            self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                                        "cache_creation_input_tokens": 0})()

    def refuse(what):
        req = httpx.Request("POST", "https://example")
        return anthropic.BadRequestError(what, response=httpx.Response(400, request=req), body=None)

    class _Client:
        def __init__(self, effort_ok=True):
            self.messages = self
            self.effort_ok = effort_ok

        def create(self, **kw):
            calls.append(kw)
            oc = kw.get("output_config") or {}
            if "format" in oc:
                raise refuse("output_config.format is not supported")
            if "effort" in oc and not self.effort_ok:
                raise refuse("effort is not supported")
            if kw.get("max_tokens") == 60:
                return _Msg('{"kind": "photo", "confidence": 0.97}')
            return _Msg(json.dumps(_extraction("bill_of_lading")))

    import json
    rd._NO_STRUCTURED_OUTPUT.clear()
    rd._NO_EFFORT.clear()
    ex, _ = rd._structured_call(_Client(), "anthropic.claude-opus-5", "SYSTEM", [{"type": "text", "text": "page"}],
                                Extraction, effort="low", brief=True)
    last = calls[-1]
    check("the schema goes in the cached system block", "JSON schema" in last["system"][0]["text"]
          and last["system"][0].get("cache_control") and all("JSON schema" not in str(b) for b in last["messages"][0]["content"]))
    check("with the brief-notes instruction", "two short sentences" in last["system"][0]["text"])
    check("and low effort, on its own", last.get("output_config") == {"effort": "low"})
    calls.clear()
    rd._NO_EFFORT.clear()
    rd._structured_call(_Client(effort_ok=False), "anthropic.claude-opus-5", "SYSTEM", [], Extraction, effort="low")
    rd._structured_call(_Client(effort_ok=False), "anthropic.claude-opus-5", "SYSTEM", [], Extraction, effort="low")
    check("an endpoint that refuses effort is asked once, then read without it",
          sum("effort" in (c.get("output_config") or {}) for c in calls) == 1 and "output_config" not in calls[-1])
    doc = type("D", (), {"pages": []})()
    kind, conf, _u = rd.quick_look(_Client(), doc, "claude-haiku-4-5")
    check("the quick look answers a kind and how sure", (kind, conf) == ("photo", 0.97))


def test_auto_upload_dry_run_and_off() -> None:
    print("auto-upload: dry run and off")
    import time as _time
    from intake import autofile

    class NoUploads(_AutoTPro):
        def upload_file(self, **kw):
            raise AssertionError("a dry run must never upload")

    conn, store, _, read, reads, load_row, mail, _on = _auto_world()
    tpro = NoUploads()
    load_row(2600002, "bol_expected", "loaded")
    tpro.add(2600002)
    mail(2600002, "mb", [(_picture(2), _reading(2600002))])
    off = autofile.run(conn, tpro, store, read, None, autofile.Settings(terminals=frozenset({1160})),
                       deadline=_time.monotonic() + 600)
    check("off does nothing at all", off.line() == "auto-upload: off" and tpro.calls == 0 and not reads)
    s = autofile.Settings(terminals=frozenset({1160}), mode="dry-run")
    dry = autofile.run(conn, tpro, store, read, None, s, deadline=_time.monotonic() + 600)
    check("a dry run reads and decides", len(reads) == 1 and conn.execute(
        "SELECT outcome FROM autofile").fetchone()[0] == "dry_run", dry.line())
    log = _AutoLog()
    check("and never puts a dry-run row in the sheet", autofile.flush_log(conn, log) == 0 and not log.rows)


def test_upload_log_sheet() -> None:
    """Rows are found by their Ref, so a sorted tab is never overwritten in the wrong place, and the
    pod's own columns are never written."""
    print("auto-upload: the Upload log sheet")
    import io
    import json as _json
    from urllib.parse import unquote
    from intake import sheets

    tab = {"Q": [["Ref"]], "A": [["Logged (UTC)"]]}
    sent: list[dict] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=60):
        url = unquote(req.full_url)
        if "fields=sheets.properties" in url:
            body = {"sheets": [{"properties": {"title": "Upload log", "sheetId": 7,
                                                "gridProperties": {"rowCount": 1000}}}]}
        elif req.get_method() == "GET":
            body = {"values": tab["Q" if url.endswith("!Q:Q") else "A"]}
        else:
            body = _json.loads(req.data)
            sent.append(body)
            for d in body.get("data", []):
                cell, values = d["range"].split("!")[1], d["values"][0]
                row = int("".join(ch for ch in cell.split(":")[0] if ch.isdigit()))
                col = "Q" if cell.startswith("Q") else "A"
                while len(tab[col]) < row:
                    tab[col].append([])
                tab[col][row - 1] = [values[0]]
            body = {}
        return _Resp(_json.dumps(body).encode())

    log = sheets.UploadLog("sheet", lambda: "token", opener=opener)
    log.write([("L1-a", ["t", 1] + [""] * 12), ("L2-b", ["t", 2] + [""] * 12)])
    check("new rows go under the header", tab["Q"][1] == ["L1-a"] and tab["Q"][2] == ["L2-b"], str(tab["Q"]))
    # A pod lead sorts the tab: L2 is now on row 2 and L1 on row 3.
    tab["Q"][1], tab["Q"][2] = ["L2-b"], ["L1-a"]
    log.write([("L1-a", ["t2", 1] + [""] * 12), ("L3-c", ["t", 3] + [""] * 12)])
    ranges = [d["range"] for d in sent[-1]["data"]]
    check("an update goes to the row carrying its Ref, wherever it now is",
          "'Upload log'!A3:N3" in ranges and "'Upload log'!A4:N4" in ranges, str(ranges))
    import re as _re
    check("and the pod's columns O and P are never written",
          all(_re.fullmatch(r"'Upload log'!(A\d+:N\d+|Q\d+)", r) for r in ranges), str(ranges))


def test_every_load_state_is_classified() -> None:
    """A new load state must be taught to state.shortfall(), or it silently becomes "not needed".

    On 21 Sep 2026 pod_unverified and pod_mislabelled existed in the state machine and in neither
    classifier. The export answered "not needed" for every document on those loads while the gate
    answered "would file" for the same documents, and the two were reconciled only because someone
    happened to compare them. This test is what makes that impossible: it reads the states the
    machine can actually emit and fails if the classifier has not been taught one.
    """
    print("classification: every load state has an answer")
    import re as _re

    src = (HERE / "intake" / "state.py").read_text(encoding="utf-8")
    emitted = set(_re.findall(r'_row\(load_id, load, "([a-z_]+)"', src))
    emitted |= set(state.CADENCE_MINUTES)      # anything given a cadence is a real state
    emitted -= {"in_review"}                    # event-driven; never carries a document decision
    missing = sorted(s for s in emitted if s not in state.SHORTFALL)
    check("every state the machine emits is classified", not missing, f"unclassified: {missing}")

    stray = {v for _, v in state.SHORTFALL.values()} - {
        state.NEEDS, state.REFILE, state.NOTHING, state.REVIEW, state.UNKNOWN}
    check("every verdict is one the callers handle", not stray, str(stray))

    check("an unseen state answers UNKNOWN, never NOTHING",
          state.shortfall("some_state_invented_next_month") == (None, state.UNKNOWN))
    check("pod_unverified is no longer silently 'nothing'",
          state.shortfall("pod_unverified")[1] != state.NOTHING)
    check("bol_expected still asks for a BOL",
          state.shortfall("bol_expected") == ("Bill Of Lading", state.NEEDS))
    check("complete really is nothing",
          state.shortfall("complete") == (None, state.NOTHING))


if __name__ == "__main__":
    test_routing()
    test_no_group_is_a_configuration_not_a_crash()
    test_every_subcommand_defaults_its_group()
    test_a_satisfied_load_is_not_paid_to_read()
    test_satisfied_states_are_derived_not_listed()
    test_structured_output_refusal_is_learned_once()
    test_a_transient_lock_does_not_end_a_long_job()
    test_archive_keys_are_derived_not_random()
    test_archive_is_idempotent_and_resumable()
    test_archive_links_mail_to_its_documents()
    test_archive_withholds_personal_id_by_default()
    test_archive_records_what_landed()
    test_a_deleted_message_does_not_stop_collection()
    test_unread_documents_are_tagged_unchecked()
    test_collector_stores_mail_and_documents_linked()
    test_collector_resumes_where_it_stopped()
    test_collector_errors_and_deletions()
    test_collector_bookmark_is_guarded()
    test_lambda_wiring()
    test_mail_from_s3_reaches_the_ledger()
    test_the_ledger_goes_back_only_over_what_was_taken()
    test_working_hours()
    test_worker_run()
    test_a_load_the_sweep_sees_is_never_left_unscheduled()
    test_a_dropped_connection_is_retried_not_fatal()
    test_auto_upload_facts_and_pictures()
    test_auto_upload_pilot()
    test_auto_upload_texted_pages()
    test_auto_upload_bol_sets()
    test_auto_upload_quick_look()
    test_reader_is_brief_and_caches_the_schema()
    test_auto_upload_dry_run_and_off()
    test_upload_log_sheet()
    test_every_load_state_is_classified()
    test_filters()
    test_photo_stamp()
    test_pii_gate()
    test_receiving_stamp_is_acknowledgement()
    test_comment_never_prints_a_non_name()
    test_provider_selection()
    test_notifications()
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
