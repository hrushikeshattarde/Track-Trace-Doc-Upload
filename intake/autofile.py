"""Step 4 for the pilot pods: read the new paperwork, upload what passes, log every BOL and POD.

Runs inside the worker, after the load checks, for the terminals in INTAKE_AUTO_TERMINALS - the
Frankie Saiz pod (1160) from 24 Sep 2026. For each of their loads that is not yet complete:

    look     the load in TransportPro and its File History, downloading paperwork not seen before
    read     new emailed documents (from the S3 archive) and Driver Supplied BOLs somebody else put on
             the load - a driver's text, a rep - with the AI, within a per-run and a daily spend cap
    decide   every check below, for every document the AI reads as a BOL or a POD
    upload   what passed. The manager's rule (24 Sep 2026): a POD goes in as Bill Of Lading, which
             clears Waiting for Documents, and a BOL as Driver Supplied BOL, which does not. Pages of
             one document from one email - or from one burst of texts, which TransportPro files one
             picture at a time - go up as one PDF. Every upload's comment starts "Doc Intake Bot:"
             and says what the page really is
    log      what it uploaded, and what it held back and which check failed, to the pod's Upload log
             sheet - one row per document, updated in place when its status changes. Every other
             decision (already on file, not needed, waiting) is kept in the ledger only

A document is uploaded only when all of these hold:

    - it is a BOL or a POD. Anything else - freight and seal photos included - is never uploaded
      and never logged (house rule: only BOLs and PODs go into File History)
    - there is no personal ID on it. Never uploaded, never logged
    - the AI is at least 85% sure what it is
    - at least two facts on the page match the load in TransportPro, one of them a reference number
      (a city or a piece count alone matches every load on a lane)
    - a receiver-signed page waits until TransportPro has the truck at the consignee
    - it is not already on the load: the same file, or the same picture. A BOL on file under any
      paperwork type counts; a POD counts only under a type that clears (Bill Of Lading, Proof of
      Delivery, Delivery Receipt). So a POD a driver texted in - which TransportPro files as Driver
      Supplied BOL - is uploaded once more as Bill Of Lading, and the comment names the copy it
      re-files. The API cannot retype or delete a file; the Driver Supplied copy stays
    - the load actually needs it: no BOL if the load already has one, no POD if it already has a
      proper POD, nothing once it shows Documents Received - checked again just before the upload

Duplicates are refused three ways: the filing table's key, a fresh File History check against
every page's picture straight before each upload, and the in-run list of what this run uploaded.

Modes (INTAKE_AUTO_UPLOAD): off, dry-run (decides and prints, uploads nothing, writes no sheet) and
on. Nothing else in the service uploads by itself.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db, filing, notify, state as st, store as s3store
from .tpro import TProError, uploaded_file_id

BOT = "Doc Intake Bot"
OFF, DRY_RUN, ON = "off", "dry-run", "on"

PAPERWORK_TYPES = (12, 363, 360, 53, 123)     # what a driver's paperwork can be filed under
DRIVER_SUPPLIED = 363
TYPE_IDS = {"Bill Of Lading": 12, "Driver Supplied BOL": 363}
REAL_TYPE = {"POD": "Proof of Delivery", "BOL": "Bill Of Lading"}
AT_CONSIGNEE = ("at consignee", "delivered")
# The states worth the bot's time, most urgent first. complete / out of scope need nothing, and a
# load not checked yet ('new', 'error') has no stage to judge a POD's timing by.
LOAD_ORDER = ("pod_expected", "pod_unsigned", "pod_mislabelled", "pod_unverified", "wrong_doc_type",
              "filed_status_pending", "bol_expected", "not_yet_due")
POD_COMMENT = re.compile(r"\bpod\b|proof|deliver", re.I)

# Mean absolute difference of two 32x32 greyscale thumbnails, 0-255, below which they are the same
# picture - the threshold the hand-run duplicate checks used on 24 Sep 2026. A starting value, not a
# measured boundary.
SAME_PICTURE = 3.0

READERS = 3                  # full reads in parallel
QUICK_LOOKERS = 4            # quick looks in parallel
READ_TIMEOUT_S = 120         # one read, in the SDK
EST_READ_USD = 0.04          # a brief full read, for the safety limit; the recorded cost is the real one
# How sure the quick look must be that a page is a photo before the full read is skipped. On the 24 Sep
# 2026 pages it never called a BOL or POD a photo at any confidence; 0.8 keeps a margin.
QUICK_TRUST = 0.8
LOOK_UNTIL_S = 240           # stop looking at more loads this long before the deadline
READ_UNTIL_S = 120           # ...stop waiting for reads
UPLOAD_NEEDS_S = 45          # ...and do not start an upload with less than this left
MAX_UPLOAD_ATTEMPTS = 3
# The longest document the bot reads. Load 2571670 (24 Sep 2026): a 27-page, 10.6 MB CamScanner
# packet sent every page to the AI - too large a request, and the rendering ran the 1 GB worker out
# of memory, which lost the whole run and would have lost every run after it. A longer file is held
# for a person, unread: uploading pages the bot never looked at could put anything into File History.
MAX_READ_PAGES = 10
MAX_READ_MB = 20
TOO_LONG = "too long for the bot"
# Pictures a driver texts this close together are one sending, the way one email is. Load 2577917
# (24 Sep 2026): the delivery copy came as two texts 11 seconds apart - page 1 at 11:13:28, the
# signed page 2 at 11:13:39 - and TransportPro filed each as its own Driver Supplied BOL. On the
# pod's loads that day the pictures of one sending landed 2 to 65 seconds apart (2555376's longest
# gap), and a pickup sending and a delivery sending hours apart.
TEXT_BATCH_GAP_S = 180

# Outcomes. The last three are never logged: not paperwork, personal ID, or unreadable.
UPLOADED, ON_FILE, NOT_NEEDED, HELD, WAITING, DRY = ("uploaded", "on_file", "not_needed", "held",
                                                    "waiting", "dry_run")
NOT_PAPERWORK, PERSONAL_ID, UNREADABLE = "not_bol_pod", "personal_id", "unreadable"
UNLOGGED = (NOT_PAPERWORK, PERSONAL_ID, UNREADABLE)
# What goes to the Upload log sheet: what the bot uploaded, and what it held back with the check that
# failed. Already on file, not needed and waiting stay in the ledger only (the pod's ask, 24 Sep 2026).
SHEET_OUTCOMES = (UPLOADED, HELD)


# ------------------------------------------------------------------------------ settings ----

@dataclass
class Settings:
    terminals: frozenset[int] = frozenset()
    mode: str = OFF
    min_confidence: float = 0.85
    min_facts: int = 2
    daily_usd: float = 50.0      # a safety limit against a runaway, not a budget: a normal day is ~$1
    max_reads: int = 60
    pods: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env, pods: dict[int, str] | None = None) -> "Settings":
        mode = (env.get("INTAKE_AUTO_UPLOAD") or OFF).strip().lower()
        if mode not in (OFF, DRY_RUN, ON):
            raise ValueError(f"INTAKE_AUTO_UPLOAD is {mode!r}; it must be off, dry-run or on")
        terminals = frozenset(int(x) for x in re.split(r"[,\s]+", env.get("INTAKE_AUTO_TERMINALS") or "") if x)
        return cls(terminals=terminals, mode=mode,
                   min_confidence=float(env.get("INTAKE_AUTO_MIN_CONFIDENCE") or 0.85),
                   min_facts=int(env.get("INTAKE_AUTO_MIN_FACTS") or 2),
                   daily_usd=float(env.get("INTAKE_AUTO_DAILY_USD") or 50),
                   max_reads=int(env.get("INTAKE_AUTO_MAX_READS") or 60), pods=pods or {})


@dataclass
class Stats:
    mode: str = OFF
    looked: int = 0
    read: int = 0
    read_failed: int = 0
    spent: float = 0.0
    spent_today: float = 0.0
    uploads: int = 0
    uploaded: int = 0
    quick: int = 0
    reused: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    logged: int = 0
    errors: int = 0
    stopped: str = ""
    sheet_error: str = ""
    started: str = ""

    def line(self) -> str:
        if self.mode == OFF:
            return "auto-upload: off"
        by = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(self.outcomes.items())) or "no decisions"
        return (f"auto-upload ({self.mode}): {self.looked} load(s) looked at, {self.quick} quick look(s), "
                f"{self.read} full read(s), {self.reused} reading(s) reused "
                f"(${self.spent:.2f}; ${self.spent_today:.2f} today), {self.uploads} upload(s) of "
                f"{self.uploaded} document(s) | {by} | {self.logged} sheet row(s) written"
                + (f" | {self.read_failed} read(s) failed" if self.read_failed else "")
                + (f" | {self.errors} error(s)" if self.errors else "")
                + (f" | stopped: {self.stopped}" if self.stopped else "")
                + (f" | SHEET FAILED: {self.sheet_error}" if self.sheet_error else ""))


# ------------------------------------------------------------------------------ the pieces ----

@dataclass
class OnFile:
    """A paperwork file on the load, hashed and thumbnailed."""
    file: dict
    sha256: str
    sig: list
    data: bytes | None = None

    @property
    def type_id(self) -> int:
        return int(self.file.get("fileTypeId") or 0)

    @property
    def by_bot(self) -> bool:
        return str(self.file.get("comments") or "").startswith(BOT)

    def named(self) -> str:
        when = str(self.file.get("dateCreated") or "")[:16].replace("T", " ")
        return f"{self.file.get('fileTypeName') or self.type_id} {self.file.get('id')} ({when} UTC)"


@dataclass
class Doc:
    """A document the bot has to decide about: emailed in, or already on the load."""
    sha256: str
    filename: str
    source: str                   # email:<message id> | tpro:<file id>
    arrived: str                  # the sheet's "Arrived by", in words
    message_id: str | None = None
    on_file: OnFile | None = None
    order: int = 0
    data: bytes | None = None
    batch: str | None = None      # what arrived together: email:<message id> | text:<first file id>


@dataclass
class Decision:
    doc: Doc
    outcome: str
    status: str = ""
    kind: str | None = None       # BOL | POD
    ex: Any = None
    conf: float | None = None
    facts: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    refiles: str = ""             # the non-clearing copy a POD upload re-files, e.g. "Driver Supplied BOL 31447880"
    final: bool = True
    tpro_file_id: str | None = None
    strong: list[str] = field(default_factory=list)   # the reference numbers among `facts`
    why: set[str] = field(default_factory=set)        # which checks failed: confidence, facts, conflict, pod_evidence
    on_clearing: bool = False     # already on the load under a type that clears
    companion: bool = False       # another page of a POD from the same email, going up with it
    quick: str = ""               # "BOL (quick look, 95% sure)" when only the quick look saw the page

    @property
    def ready(self) -> bool:
        return self.outcome == "ready"


# ------------------------------------------------------------------------------------ run ----

def run(conn: sqlite3.Connection, tpro, store, read: Callable | None, log, s: Settings, *,
        deadline: float, uploader=None, quick: Callable | None = None) -> Stats:
    """One pass over the pilot terminals' loads. `read` is ingest.make_reader's callable; `log` an
    sheets.UploadLog, or None in dry-run; `uploader` the TransportPro client that uploads (the bot's
    own login when it has one), defaulting to `tpro`; `quick` ingest.make_quick_reader's callable, or
    None to give every page the full read."""
    stats = Stats(mode=s.mode, started=db.now_iso())
    if s.mode == OFF or not s.terminals:
        stats.mode = OFF
        return stats
    uploader = uploader or tpro
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    stats.spent_today = float(db.get_state(conn, f"ai_spend:{day}") or 0)

    # 1. look
    plans: list[tuple[sqlite3.Row, dict, list[OnFile], list[Doc]]] = []
    for row in _loads(conn, s.terminals):
        if time.monotonic() > deadline - LOOK_UNTIL_S:
            stats.stopped = "out of time while looking at loads"
            break
        if not _worth_a_look(conn, row, s):
            continue
        try:
            plan = _look(conn, tpro, row)
        except TProError as e:
            stats.errors += 1
            print(f"  ! auto-upload: load {row['load_id']}: {e}")
            continue
        stats.looked += 1
        if plan is not None:
            plans.append(plan)

    # 2. read
    unread = _read(conn, store, tpro, read, quick, s, stats, plans, deadline, day)

    # 3. decide and upload
    for row, load, filed, docs in plans:
        load_id = int(row["load_id"])
        if time.monotonic() > deadline - UPLOAD_NEEDS_S:
            stats.stopped = "out of time before deciding; the rest go next run"
            break
        try:
            decisions = [d for d in (_decide(conn, store, tpro, s, row, load, filed, doc) for doc in docs) if d]
            groups: dict[tuple, list[Decision]] = {}
            for dec in decisions:
                if dec.ready:
                    groups.setdefault((dec.doc.batch or dec.doc.source, dec.kind), []).append(dec)
            # The other pages sent with each document join it - a POD's first, then a BOL's - and a
            # page one set claims leaves every other, so nothing goes up twice.
            claimed: dict[int, tuple] = {}
            for key in sorted(groups, key=lambda k: 0 if k[1] == "POD" else 1):
                group = groups[key]
                if not group[0].doc.batch:
                    continue
                pool = [d for d in decisions + _batch_mates(conn, store, tpro, s, row, load, filed, docs, group[0].doc.batch)
                        if id(d) not in claimed and not any(d is g for g in group)]
                for d in companions(conn, pool, group, s):
                    claimed[id(d)] = key
                    group.append(d)
            for key in list(groups):
                groups[key] = [d for d in groups[key] if claimed.get(id(d), key) == key]
                if not groups[key]:
                    del groups[key]
            for dec in decisions:
                if not dec.ready and not dec.companion:
                    _record(conn, s, row, dec)
            for group in groups.values():
                if time.monotonic() > deadline - UPLOAD_NEEDS_S:
                    stats.stopped = "out of time before an upload; it goes next run"
                    unread.add(load_id)
                    break
                _upload(conn, tpro, uploader, store, s, row, filed, group, stats)
        except Exception as e:                                   # noqa: BLE001 - one load never stops the rest
            stats.errors += 1
            unread.add(load_id)
            print(f"  ! auto-upload: load {load_id}: {type(e).__name__}: {str(e)[:200]}")
        if load_id not in unread:
            _mark_looked(conn, load_id)

    for k, v in conn.execute("SELECT outcome, COUNT(*) FROM autofile WHERE decided_at >= ? GROUP BY outcome",
                             (stats.started,)).fetchall():
        stats.outcomes[k] = v

    # 4. log
    if log is not None and s.mode == ON:
        try:
            stats.logged = flush_log(conn, log)
        except Exception as e:                                   # noqa: BLE001 - reported; rows stay pending
            stats.sheet_error = f"{type(e).__name__}: {str(e)[:200]}"
    return stats


def _mark_looked(conn, load_id: int) -> None:
    conn.execute("INSERT INTO autofile_load (load_id, looked_at) VALUES (?,?) "
                 "ON CONFLICT(load_id) DO UPDATE SET looked_at=excluded.looked_at", (load_id, db.now_iso()))


# The kinds of number that name a shipment. A page carrying none of them - only a seal, a trailer, a
# handwritten weight - cannot contradict the set it arrived with.
REFERENCE_KINDS = frozenset({"bol", "master_bill", "po", "pickup", "shipment", "delivery", "invoice",
                             "load_or_trip", "order", "shipper_ref"})
# How sure the AI must be of a sign-out side that joins a set. Its own checks cannot pass - it has no
# reference number to match - so the set's lead page carries them; 2576831's read at 72%.
SIGN_OUT_MIN_CONFIDENCE = 0.6


def can_join(dec: Decision, refs: set[str], kind: str, s: Settings) -> bool:
    """Whether a BOL-read page may go up inside a set of `kind` whose pages matched `refs`.

    Either it passes on its own and shares a reference number with the set, or it is a sign-out
    side: nothing on it names a shipment, so nothing can contradict the set, and its only failed
    checks are the facts it cannot have and a confidence the set's lead page makes up for."""
    if dec.kind != "BOL" or dec.on_clearing or dec.ex is None:
        return False
    if kind == "BOL" and dec.outcome == ON_FILE:
        return False                          # already on the load as paperwork: a BOL set adds nothing by repeating it
    if (dec.outcome in ("ready", ON_FILE, NOT_NEEDED) and not dec.failed
            and (dec.conf or 0) >= s.min_confidence and refs & set(dec.strong)):
        return True
    own = [n for n in dec.ex.numbers if n.kind in REFERENCE_KINDS]
    return (not own and dec.why <= {"facts", "confidence"} and (dec.conf or 0) >= SIGN_OUT_MIN_CONFIDENCE
            and dec.outcome in ("ready", HELD, ON_FILE, NOT_NEEDED))


def companions(conn, decisions: list[Decision], group: list[Decision], s: Settings) -> list[Decision]:
    """The other pages sent with a document - the same email, or the same burst of texts.

    A POD: a driver photographs the delivery copy page by page, and only the page the receiver
    signed reads as a POD. Load 2562005 (24 Sep 2026, email): page 1 read as an unsigned BOL, page 2
    as the signed POD, and the POD is both pages; load 2577917 the same, sent as two texts.
    A BOL: load 2576831's pick slip came as the front, with the load number and customer PO, and the
    back, the driver's sign-out, with no reference number at all - held on its own, and the BOL is both.
    Who may join is can_join(). A second shot of a page already in - 2560031's driver texted the same
    two pages eight times over - stays out."""
    ids = {g.doc.sha256 for g in group}
    refs = {x for g in group for x in g.strong}
    kind = group[0].kind
    kept = [cached_sig(conn, g.doc.sha256) or [] for g in group]
    out = []
    for dec in sorted(decisions, key=lambda x: x.doc.order):
        if (dec.doc.batch == group[0].doc.batch and dec.doc.sha256 not in ids and can_join(dec, refs, kind, s)):
            mine = cached_sig(conn, dec.doc.sha256) or []
            if mine and any(sig and all(any(_diff(p, q) < SAME_PICTURE for q in sig) for p in mine) for sig in kept):
                continue
            ids.add(dec.doc.sha256)
            kept.append(mine)
            dec.companion = True
            out.append(dec)
    return out


def _batch_mates(conn, store, tpro, s: Settings, row, load: dict, filed: list[OnFile], docs: list[Doc],
                 batch: str) -> list[Decision]:
    """Texted pages of this batch that were decided in an earlier run - page 1 read and logged
    before page 2 arrived - judged again, so they can still go up with their POD."""
    if not batch.startswith("text:"):
        return []
    have = {d.sha256 for d in docs}
    out = []
    for of in filed:
        if of.sha256 in have or text_batches(filed).get(str(of.file.get("id"))) != batch:
            continue
        att = db.get_attachment(conn, of.sha256)
        if att is None or att["extraction_json"] is None:
            continue
        d = file_doc(of, filed)
        dec = judge(conn, s, row, load, filed, d, json.loads(att["extraction_json"]),
                    sig=lambda d=d: doc_sig(conn, store, tpro, d))
        if dec is not None:
            out.append(dec)
    return out


def _loads(conn, terminals: frozenset[int]) -> list[sqlite3.Row]:
    cases = " ".join(f"WHEN '{x}' THEN {i}" for i, x in enumerate(LOAD_ORDER))
    marks = ",".join("?" * len(terminals))
    return conn.execute(
        f"SELECT * FROM load WHERE in_view=1 AND terminal IN ({marks}) AND state IN "
        f"({','.join('?' * len(LOAD_ORDER))}) ORDER BY CASE state {cases} ELSE 99 END, next_check_at",
        (*sorted(terminals), *LOAD_ORDER)).fetchall()


def _worth_a_look(conn, row, s: Settings) -> bool:
    """A load is looked at when it is new to the bot, when the load check has seen it again since
    (its stage or its File History may have moved), or when mail brought something undecided - and,
    the first time uploads are on, when a dry run left it with uploads it only described."""
    got = conn.execute("SELECT looked_at FROM autofile_load WHERE load_id=?", (row["load_id"],)).fetchone()
    if got is None or not got[0]:
        return True
    if (row["last_checked_at"] or "") > got[0]:
        return True
    if s.mode == ON and conn.execute("SELECT 1 FROM autofile WHERE load_id=? AND outcome=?",
                                     (row["load_id"], DRY)).fetchone():
        return True
    decided = _decided(conn, int(row["load_id"]))
    return any(_open(decided, d.sha256) for d in _email_docs(conn, int(row["load_id"])))


def _decided(conn, load_id: int) -> dict[str, sqlite3.Row]:
    return {r["sha256"]: r for r in conn.execute("SELECT * FROM autofile WHERE load_id=?", (load_id,))}


def _open(decided: dict, sha: str) -> bool:
    r = decided.get(sha)
    return r is None or not r["final"]


def _email_docs(conn, load_id: int) -> list[Doc]:
    rows = conn.execute(
        "SELECT p.sha256, p.filename, p.part_id, m.message_id, m.internal_date, m.from_domain "
        "FROM part p JOIN message m ON m.message_id = p.message_id "
        "WHERE m.load_id=? AND p.decision='keep' AND p.sha256 IS NOT NULL", (load_id,)).fetchall()

    def order(r):
        return (r["internal_date"] or "", r["message_id"],
                tuple((int(x), "") if x.isdigit() else (10 ** 9, x) for x in str(r["part_id"]).split(".")))

    out, seen = [], set()
    for i, r in enumerate(sorted(rows, key=order)):
        if r["sha256"] in seen:
            continue
        seen.add(r["sha256"])
        when = (r["internal_date"] or "")[:16].replace("T", " ")
        out.append(Doc(r["sha256"], r["filename"] or r["sha256"][:12], f"email:{r['message_id']}",
                       f"Email {when} UTC from {r['from_domain'] or 'an unknown sender'}",
                       message_id=r["message_id"], order=i, batch=f"email:{r['message_id']}"))
    return out


def _look(conn, tpro, row) -> tuple | None:
    load_id = int(row["load_id"])
    load = tpro.load(load_id)
    if _docs_received(load):
        # TransportPro is satisfied; the load check will mark it complete. Nothing to read or upload.
        _mark_looked(conn, load_id)
        return None
    filed = on_file(conn, tpro, load_id, tpro.files(load_id))
    decided = _decided(conn, load_id)
    emailed = _email_docs(conn, load_id)
    shas = {d.sha256 for d in emailed}
    docs = [d for d in emailed if _open(decided, d.sha256)]
    for of in filed:
        # Driver Supplied BOLs somebody else filed: a driver's text, a rep. Their bytes are on the
        # load, not in the mail, and one of them may be the POD this load is waiting for.
        if (of.type_id == DRIVER_SUPPLIED and not of.by_bot and of.sha256 not in shas
                and _open(decided, of.sha256)):
            shas.add(of.sha256)
            docs.append(file_doc(of, filed))
    if not docs:
        _mark_looked(conn, load_id)
        return None
    return row, load, filed, docs


def texted(f: dict) -> bool:
    """A driver's picture-only text: TransportPro files it itself, as System Admin (user 1)."""
    return f.get("uploadById") == 1 and str(f.get("comments") or "").startswith("Driver Supplied Image")


def text_batches(filed: list[OnFile]) -> dict[str, str]:
    """file id -> the sending it came in: texted pictures no more than TEXT_BATCH_GAP_S apart,
    chained, are one batch, named after its first file."""
    out: dict[str, str] = {}
    batch, last = None, None
    for of in sorted((x for x in filed if texted(x.file)), key=lambda x: str(x.file.get("dateCreated") or "")):
        when = _utc(of.file.get("dateCreated"))
        if batch is None or last is None or when is None or (when - last).total_seconds() > TEXT_BATCH_GAP_S:
            batch = f"text:{of.file.get('id')}"
        out[str(of.file.get("id"))] = batch
        last = when
    return out


def _utc(v) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def file_doc(of: OnFile, filed: list[OnFile]) -> Doc:
    """A file on the load as a document to decide about, in page order among the load's files."""
    rank = {str(x.file.get("id")): i for i, x in enumerate(
        sorted(filed, key=lambda x: (str(x.file.get("dateCreated") or ""), str(x.file.get("id")))))}
    fid = str(of.file.get("id"))
    return Doc(of.sha256, of.file.get("fileName") or fid, f"tpro:{fid}", _arrived_on_file(of.file), on_file=of,
               order=10_000 + rank.get(fid, 0), data=of.data, batch=text_batches(filed).get(fid))


def _docs_received(load: dict) -> bool:
    return "received" in str((load.get("status") or {}).get("documentStatus") or "").lower()


def _arrived_on_file(f: dict) -> str:
    when = str(f.get("dateCreated") or "")[:16].replace("T", " ")
    comment = str(f.get("comments") or "").strip()
    if f.get("uploadById") == 1 and comment.startswith("Driver Supplied Image"):
        # TransportPro files a driver's picture-only text reply itself, as System Admin, the same second.
        return f"Text message from the driver, {when} UTC (TransportPro filed it as {f.get('fileTypeName')})"
    return f"File History: {f.get('fileTypeName')} uploaded {when} UTC" + (f" ('{comment[:60]}')" if comment else "")


def on_file(conn, tpro, load_id: int, files: list[dict]) -> list[OnFile]:
    """Every paperwork file on the load, with its hash and thumbnails. A file is downloaded once:
    its hash is kept in tpro_file and its thumbnails in picture_sig."""
    out = []
    for f in files:
        if not f.get("id"):
            continue
        db.record_tpro_file(conn, load_id, f)
        if int(f.get("fileTypeId") or 0) not in PAPERWORK_TYPES:
            continue
        got = conn.execute("SELECT sha256 FROM tpro_file WHERE tpro_file_id=?", (int(f["id"]),)).fetchone()
        sha = got["sha256"] if got else None
        sig = cached_sig(conn, sha) if sha else None
        data = None
        if sha is None or sig is None:
            data, _ = tpro.download_file(int(f["id"]))
            sha = hashlib.sha256(data).hexdigest()
            db.record_tpro_file(conn, load_id, f, sha256=sha, size=len(data))
            sig = store_sig(conn, sha, data)
        out.append(OnFile(f, sha, sig, data))
    return out


# ------------------------------------------------------------------------------- reading ----

def _read(conn, store, tpro, read, quick, s: Settings, stats: Stats, plans, deadline: float, day: str) -> set[int]:
    """Give every document the reading it needs, as cheaply as that can be had:

        1. the same picture already read on this load: its reading is reused, for nothing
        2. a quick look (Claude Haiku, ~$0.002 a page): a photo is set aside, and so is a picture
           the driver texted while the truck is not at the consignee yet - TransportPro has already
           filed it as Driver Supplied BOL, which is where a BOL belongs, and no POD can be uploaded
           before the consignee anyway
        3. the full read (brief, ~$0.04) for everything else: every page that could be a POD or a
           BOL a load still needs, and every page the quick look is unsure of

    Measured on the 162 pages of 24 Sep 2026: this would have made 84 full reads instead of 162,
    skipping no POD and nothing the bot uploaded or held. The quick look is never trusted to say a
    page is NOT a POD - it called 2577917's signed page 2 an unsigned BOL - only that it is a photo.

    Returns the loads left with something unread, so they are looked at again next run."""
    unread: set[int] = set()
    todo = []
    for row, _, filed, docs in plans:
        for d in docs:
            att = db.get_attachment(conn, d.sha256)
            if att is not None and (att["extraction_json"] is not None or db.read_blocked(att)):
                continue
            if att is not None and att["next_read_at"] and att["next_read_at"] > db.now_iso():
                unread.add(int(row["load_id"]))
                continue                                   # waiting out a failed read's backoff
            todo.append((row, filed, d))
    if not todo:
        return unread
    ready = []
    for row, filed, d in todo:
        try:
            d.data = d.data or doc_bytes(store, tpro, d)   # here, not in the pool: the TransportPro client is not thread-safe
        except Exception as e:                             # noqa: BLE001 - one missing file stops nothing
            print(f"  ! auto-upload: {d.filename} on load {row['load_id']}: {type(e).__name__}: {str(e)[:120]}")
            unread.add(int(row["load_id"]))
            continue
        pages, mb = page_count(d.data), len(d.data) / 1e6
        if pages > MAX_READ_PAGES or mb > MAX_READ_MB:
            mid = d.message_id or f"tpro-file:{d.source.split(':', 1)[1]}"
            db.put_attachment(conn, d.sha256, message_id=mid, filename=d.filename, size=len(d.data), extraction=None,
                              document_type=None, model=None, cost_usd=None, permanent=True,
                              error=f"{TOO_LONG}: {pages} pages, {mb:.1f} MB (it reads up to {MAX_READ_PAGES} pages)")
            continue
        ready.append((row, filed, d))

    # 1. the same picture, already read
    left = []
    for row, filed, d in ready:
        if reuse_reading(conn, store, tpro, int(row["load_id"]), filed, d):
            stats.reused += 1
        else:
            left.append((row, filed, d))

    # 2. the quick look
    if quick is not None:
        _quick_looks(conn, quick, stats, [x for x in left if quick_of(conn, x[2].sha256) is None], deadline)
    full, shas = [], set()
    for row, filed, d in left:
        q = quick_of(conn, d.sha256) if quick is not None else None
        if needs_full_read(d, q, (row["stage"] or "").lower()):
            full.append((row, filed, d))
            shas.add(d.sha256)
    # A texted POD takes its other pages into the full read, even ones only looked at before - page 1
    # can be set aside as a BOL while the truck's stage still says loaded, and page 2 arrive signed.
    for row, filed, d in list(full):
        q = quick_of(conn, d.sha256) if quick is not None else None
        if not (d.batch or "").startswith("text:") or (q is not None and q[0] != "pod"):
            continue
        for of in filed:
            mq = quick_of(conn, of.sha256)
            if (of.sha256 in shas or text_batches(filed).get(str(of.file.get("id"))) != d.batch
                    or db.get_attachment(conn, of.sha256) is not None and db.get_attachment(conn, of.sha256)["extraction_json"]
                    or mq is not None and mq[0] in ("photo", "not_freight") and mq[1] >= QUICK_TRUST):
                continue
            mate = file_doc(of, filed)
            try:
                mate.data = mate.data or doc_bytes(store, tpro, mate)
            except Exception:                              # noqa: BLE001 - the POD still goes, alone
                continue
            full.append((row, filed, mate))
            shas.add(mate.sha256)

    # 3. the full read
    jobs = [(int(row["load_id"]), d) for row, _, d in full]
    if not jobs:
        _save_spend(conn, stats, day)
        return unread
    allowed = min(s.max_reads, max(0, int((s.daily_usd - stats.spent_today - stats.spent) / EST_READ_USD)))
    if read is None:
        allowed = 0
    if len(jobs) > allowed:
        stats.stopped = (f"read cap: {len(jobs) - allowed} document(s) left for later "
                         + (f"(daily ${s.daily_usd:.0f} safety limit reached)" if allowed < s.max_reads
                            else f"({s.max_reads} a run)"))
        for load_id, _ in jobs[allowed:]:
            unread.add(load_id)
        jobs = jobs[:allowed]
    if not jobs:
        _save_spend(conn, stats, day)
        return unread
    pool = ThreadPoolExecutor(max_workers=READERS)
    futures = {pool.submit(read, d.data, d.filename): (load_id, d) for load_id, d in jobs}
    done, late = wait(futures, timeout=max(1.0, deadline - READ_UNTIL_S - time.monotonic()))
    # Not `with`: its exit waits for every read, and a stuck one must not hold the ledger past the
    # timeout. Reads not started are cancelled; one still running is abandoned and tried next run.
    pool.shutdown(wait=False, cancel_futures=True)
    for fut in late:
        unread.add(futures[fut][0])
    if late:
        stats.stopped = f"{len(late)} read(s) not finished in time; next run"
    from .ingest import permanent_read_error
    for fut in done:
        load_id, d = futures[fut]
        mid = d.message_id or f"tpro-file:{d.source.split(':', 1)[1]}"
        try:
            ex, dtype, model, cost = fut.result()
        except Exception as e:                                   # noqa: BLE001 - recorded with its retry
            stats.read_failed += 1
            unread.add(load_id)
            db.put_attachment(conn, d.sha256, message_id=mid, filename=d.filename, size=len(d.data or b""),
                              extraction=None, document_type=None, model=None, cost_usd=None,
                              error=f"{type(e).__name__}: {str(e)[:400]}", permanent=permanent_read_error(e))
            continue
        db.put_attachment(conn, d.sha256, message_id=mid, filename=d.filename, size=len(d.data or b""),
                          extraction=ex, document_type=dtype, model=model, cost_usd=cost)
        stats.read += 1
        stats.spent += cost or 0
    _save_spend(conn, stats, day)
    return unread


def _save_spend(conn, stats: Stats, day: str) -> None:
    stats.spent_today += stats.spent
    db.set_state(conn, f"ai_spend:{day}", f"{stats.spent_today:.4f}")


def page_count(data: bytes) -> int:
    """Pages in a file without rendering any: a PDF's page count, 1 for a picture."""
    if data[:5] != b"%PDF-":
        return 1
    try:
        import pymupdf
        return pymupdf.open(stream=data, filetype="pdf").page_count
    except Exception:                                            # noqa: BLE001 - an unopenable PDF fails in the reader, as before
        return 1


def needs_full_read(d: Doc, q: tuple[str, float] | None, stage: str) -> bool:
    """Whether a page needs the full read, given its quick look. No quick look, or one that is
    unsure: always."""
    if q is None:
        return True
    kind, confidence = q
    if kind in ("photo", "not_freight") and confidence >= QUICK_TRUST:
        return False
    if d.on_file is not None and stage not in AT_CONSIGNEE and kind in ("bol", "other_paperwork", "photo", "not_freight"):
        return False
    return True


def quick_of(conn, sha: str) -> tuple[str, float] | None:
    got = conn.execute("SELECT kind, confidence FROM quicklook WHERE sha256=?", (sha,)).fetchone()
    return (got[0], float(got[1] or 0)) if got else None


def _quick_looks(conn, quick, stats: Stats, items: list, deadline: float) -> None:
    if not items:
        return
    pool = ThreadPoolExecutor(max_workers=QUICK_LOOKERS)
    futures = {pool.submit(quick, d.data, d.filename): d for _, _, d in items}
    done, _late = wait(futures, timeout=max(1.0, deadline - LOOK_UNTIL_S / 2 - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    for fut in done:
        d = futures[fut]
        try:
            kind, confidence, model, cost = fut.result()
        except Exception as e:                                   # noqa: BLE001 - no quick look: the full read decides
            print(f"  ! auto-upload: quick look on {d.filename}: {type(e).__name__}: {str(e)[:120]}")
            continue
        conn.execute("INSERT OR REPLACE INTO quicklook (sha256, kind, confidence, model, cost_usd, looked_at) "
                     "VALUES (?,?,?,?,?,?)", (d.sha256, kind, confidence, model, cost, db.now_iso()))
        stats.quick += 1
        stats.spent += cost or 0


def reuse_reading(conn, store, tpro, load_id: int, filed: list[OnFile], d: Doc) -> bool:
    """Copy the reading of a document already read on this load when this one is the same picture,
    page for page - a photo texted AND emailed, or re-saved by a scanning app. 13 of the 162 pages
    read on 24 Sep 2026 were such copies."""
    mine = doc_sig(conn, store, tpro, d)
    if not mine:
        return False
    shas = {of.sha256 for of in filed} | {r[0] for r in conn.execute(
        "SELECT DISTINCT p.sha256 FROM part p JOIN message m ON m.message_id = p.message_id "
        "WHERE m.load_id=? AND p.decision='keep' AND p.sha256 IS NOT NULL", (load_id,))}
    for sha in shas - {d.sha256}:
        src = db.get_attachment(conn, sha)
        theirs = cached_sig(conn, sha)
        if src is None or src["extraction_json"] is None or not theirs or len(theirs) != len(mine):
            continue
        if all(any(_diff(p, q) < SAME_PICTURE for q in theirs) for p in mine):
            mid = d.message_id or f"tpro-file:{d.source.split(':', 1)[1]}"
            db.put_attachment(conn, d.sha256, message_id=mid, filename=d.filename, size=len(d.data or b""),
                              extraction=json.loads(src["extraction_json"]), document_type=src["document_type"],
                              model=f"reused from {sha[:12]}", cost_usd=0.0)
            return True
    return False


def doc_bytes(store, tpro, d: Doc) -> bytes:
    if d.on_file is not None:
        return d.on_file.data or tpro.download_file(int(d.on_file.file["id"]))[0]
    data = store.get(s3store.doc_key(d.sha256))
    if hashlib.sha256(data).hexdigest() != d.sha256:
        raise ValueError(f"the archived copy of {d.sha256[:12]} does not match its hash")
    return data


# ------------------------------------------------------------------------------- deciding ----

def _decide(conn, store, tpro, s: Settings, row, load: dict, filed: list[OnFile], d: Doc) -> Decision | None:
    att = db.get_attachment(conn, d.sha256)
    if att is not None and att["extraction_json"] is None and str(att["error"] or "").startswith(TOO_LONG):
        # Not read, so what it is is unknown - it is logged, for a person, rather than dropped as not paperwork.
        return Decision(d, HELD, f"HELD - {att['error']}; not read or uploaded, a person should look",
                        kind="BOL/POD?", quick="not read (too long for the bot)", failed=[att["error"]])
    if att is None or att["extraction_json"] is None:
        if db.read_blocked(att):
            return Decision(d, UNREADABLE, "the file cannot be read")
        q = quick_of(conn, d.sha256)
        if q is not None and not needs_full_read(d, q, (row["stage"] or "").lower()):
            return quick_decision(d, q)
        return None                                   # not read yet: next run
    return judge(conn, s, row, load, filed, d, json.loads(att["extraction_json"]),
                 sig=lambda: doc_sig(conn, store, tpro, d))


def quick_decision(d: Doc, q: tuple[str, float]) -> Decision:
    """The decision for a page the quick look settled. A photo is never logged; a BOL the driver
    texted is on the load already, as Driver Supplied BOL, and is logged as that."""
    kind, confidence = q
    if kind == "bol" and d.on_file is not None:
        return Decision(d, ON_FILE, f"ALREADY ON FILE - {d.on_file.named()}, same file; nothing uploaded "
                                    f"(checked by quick look)", kind="BOL",
                        quick=f"BOL (quick look, {confidence:.0%} sure)")
    return Decision(d, NOT_PAPERWORK, f"quick look: {kind}")


def judge(conn, s: Settings, row, load: dict, filed: list[OnFile], d: Doc, reading: dict, *,
          sig: Callable[[], list]) -> Decision | None:
    """What should happen to this document on this load. Makes no TransportPro call and records
    nothing; `sig` may fetch the document from the archive once, to thumbnail it."""
    from pod_intake.matcher import classify_type
    from pod_intake.schema import Extraction

    ex = Extraction.model_validate(reading)
    if filing.mentions_personal_id(f"{ex.document_type} {ex.notes or ''} {d.filename or ''}"):
        return Decision(d, PERSONAL_ID, "personal ID on the page", ex=ex)
    if ex.document_type in ("other", "unknown"):
        return Decision(d, NOT_PAPERWORK, f"read as {ex.document_type}", ex=ex)

    stage = (row["stage"] or "").lower()
    type_name, _ = classify_type(ex, {"dispatch_status": stage.title()})
    receiver = ex.signatures.receiver_signed or ex.document_type == "proof_of_delivery"
    if type_name == "Proof of Delivery":
        kind = "POD"
    elif type_name == "Bill Of Lading":
        kind = "POD" if receiver and stage not in AT_CONSIGNEE else "BOL"
    else:
        return Decision(d, NOT_PAPERWORK, f"read as {type_name}", ex=ex)
    dec = Decision(d, "ready", kind=kind, ex=ex, conf=ex.document_type_confidence)
    dec.facts, dec.strong = match_facts_detail(ex, load)
    strong = len(dec.strong)

    # Already on the load? The same file, or the same picture under a type that counts.
    mine = sig()
    counts = [of for of in filed if kind == "BOL" or of.type_id in st.CLEARING_TYPES]
    hit = _covering(d.sha256, mine, counts)
    if hit is not None:
        ours = hit.by_bot and (hit.sha256 == d.sha256 or conn.execute(
            "SELECT 1 FROM filing WHERE load_id=? AND sha256=?", (int(row["load_id"]), d.sha256)).fetchone())
        dec.on_clearing = hit.type_id in st.CLEARING_TYPES
        if ours:
            # The bot's own upload of this very document - found here when a run's ledger was lost
            # after the upload. Said as what it is, not as a duplicate somebody else filed.
            dec.outcome, dec.tpro_file_id = UPLOADED, str(hit.file.get("id"))
            dec.status = f"UPLOADED - on file as {hit.named()}, by the bot"
        else:
            how = "same file" if hit.sha256 == d.sha256 else "same picture"
            dec.outcome = ON_FILE
            dec.status = (f"ALREADY ON FILE - {hit.named()}, {how}"
                          + (", uploaded by the bot for another copy" if hit.by_bot else "") + "; nothing uploaded")
        return dec
    if kind == "POD":
        copy = _covering(d.sha256, mine, [of for of in filed if of.type_id not in st.CLEARING_TYPES])
        dec.refiles = f"{copy.file.get('fileTypeName')} {copy.file.get('id')}" if copy is not None else ""

    # Does the load need it?
    has, unknown = (_has_bol if kind == "BOL" else _has_pod)(conn, filed)
    if has is not None:
        dec.outcome = NOT_NEEDED
        dec.status = f"NOT NEEDED - the load already has a {kind} on file: {has.named()}; nothing uploaded"
        return dec
    if unknown:
        return None                                   # a Driver Supplied BOL on the load is not read yet

    # The checks.
    if ex.document_type_confidence < s.min_confidence:
        dec.failed.append(f"AI only {ex.document_type_confidence:.0%} sure (needs {s.min_confidence:.0%})")
        dec.why.add("confidence")
    if len(dec.facts) < s.min_facts or strong < 1:
        dec.why.add("facts")
        dec.failed.append(f"{len(dec.facts)} fact(s) match TransportPro, {strong} a reference number "
                          f"(needs {s.min_facts}, one a reference number)")
    if d.message_id:
        routing, conflicted = filing._routing_of(conn, int(row["load_id"]), d.sha256)
        if conflicted:
            dec.failed.append("a reply in the email thread named a different load")
            dec.why.add("conflict")
    if kind == "BOL" and ex.document_type == "proof_of_delivery":
        dec.failed.append("the AI called it a POD but found no receiver signature, stamp or delivery time")
        dec.why.add("pod_evidence")
    if kind == "POD" and not (ex.signatures.receiver_signed or ex.signatures.stamp_present):
        # Uploaded as Bill Of Lading, a POD clears the load for billing, so an in/out time alone is not
        # enough to do that unattended. Load 2562069 (24 Sep 2026): the "out 12:34" was the truck's
        # dashboard clock in the photo, on a BOL whose consignee lines were blank.
        dec.failed.append("no receiver signature or stamp - the only delivery evidence is an in/out time")
        dec.why.add("pod_evidence")
    if dec.failed:
        dec.outcome = HELD
        dec.status = f"HELD - {'; '.join(dec.failed)}; not uploaded, a person should look"
        return dec
    if kind == "POD" and stage not in AT_CONSIGNEE:
        dec.outcome, dec.final = WAITING, False
        dec.status = (f"WAITING - receiver-signed, but TransportPro has the truck "
                      f"{stage or 'at an unknown stage'}; uploads once it reaches the consignee")
        return dec
    return dec


def _covering(sha: str, mine: list, files: list[OnFile]) -> OnFile | None:
    """The file already carrying this document: the same bytes, or every one of its pages."""
    for of in files:
        if of.sha256 == sha:
            return of
    if not mine:
        return None
    for of in files:
        if of.sig and all(any(_diff(p, q) < SAME_PICTURE for q in of.sig) for p in mine):
            return of
    pages = [q for of in files for q in (of.sig or [])]
    if pages and all(any(_diff(p, q) < SAME_PICTURE for q in pages) for p in mine):
        return next(of for of in files if of.sig and any(_diff(p, q) < SAME_PICTURE for p in mine for q in of.sig))
    return None


def _reads_as(conn, of: OnFile) -> str | None:
    """What a file on the load reads as; None only while it is still waiting to be read."""
    if of.by_bot:
        return "proof_of_delivery" if str(of.file.get("comments")).startswith(f"{BOT}: POD") else "bill_of_lading"
    att = db.get_attachment(conn, of.sha256)
    if att is not None and att["extraction_json"] is not None:
        return att["document_type"]
    q = quick_of(conn, of.sha256)
    if q is not None:
        return {"bol": "bill_of_lading", "pod": "proof_of_delivery"}.get(q[0], "other")
    return "unreadable" if db.read_blocked(att) else None


def _has_bol(conn, filed: list[OnFile]) -> tuple[OnFile | None, bool]:
    unknown = False
    for of in filed:
        if of.type_id == 12:
            return of, False
        if of.type_id == DRIVER_SUPPLIED:
            reads = _reads_as(conn, of)
            if reads in ("bill_of_lading", "proof_of_delivery"):
                return of, False
            unknown = unknown or reads is None
    return None, unknown


def _has_pod(conn, filed: list[OnFile]) -> tuple[OnFile | None, bool]:
    for of in filed:
        if of.type_id in st.POD_TYPES:
            return of, False
        if of.type_id == 12 and (POD_COMMENT.search(str(of.file.get("comments") or ""))
                                 or _reads_as(conn, of) == "proof_of_delivery"):
            return of, False
    return None, False


# --------------------------------------------------------------------------------- facts ----

# The load's reference fields a page can carry, by name. A list of what counts rather than of what
# does not: the reference block also holds settings like reeferTemperatureMode "Continuous", and
# anything not named here is never evidence.
_REF_LABELS = {"pickupNumber": "pickup #", "poNumber": "PO #", "referenceNumber": "reference #",
               "manifestNumber": "manifest #", "ediReferenceNumber": "EDI reference #",
               "billOfLading": "BOL #", "sealNumber": "seal #", "containerNumber": "container #"}
_WEAK_REFS = {"numberOfPieces": "pieces", "weight": "weight"}


def _norm(v: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(v).upper())


def _whole(v: Any) -> str:
    m = re.match(r"\s*([\d,]+)", str(v))
    return m.group(1).replace(",", "") if m else ""


def _core(v: str) -> str:
    """The digits a reference is built around, without the letters TransportPro or a shipper puts
    round them: "PO18650" -> "18650", "SO154611" -> "154611"."""
    return re.sub(r"^[A-Z]+|[A-Z]+$", "", v)


def load_facts(load: dict) -> list[tuple[str, str, bool, str]]:
    """(label, comparable value, strong?, as shown) for everything on the load a page can match."""
    facts: list[tuple[str, str, bool, str]] = []

    def add(label: str, raw: Any, strong: bool) -> None:
        if raw is None or isinstance(raw, bool):
            return
        v = _norm(raw) if strong else _whole(raw)
        if len(v) >= (4 if strong else 3) and all(v != f[1] for f in facts):
            facts.append((label, v, strong, str(raw)))

    add("load #", load.get("id"), True)
    for k, v in (load.get("reference") or {}).items():
        if isinstance(v, (str, int, float)):
            if k in _WEAK_REFS:
                add(_WEAK_REFS[k], v, False)
            elif k in _REF_LABELS:
                add(_REF_LABELS[k], v, True)
    for wp in load.get("waypoints") or []:
        for r in wp.get("reference") or []:
            t = str(r.get("type") or "").upper()
            if t == "SERVICE_LEVEL":
                continue
            if t in ("WEIGHT", "PIECE_COUNT"):
                add("weight" if t == "WEIGHT" else "pieces", r.get("value"), False)
            else:
                add(t.lower().replace("_", " ") + " #", r.get("value"), True)
    return facts


def match_facts(ex, load: dict) -> tuple[list[str], int]:
    """What on the page matches the load: ([words], how many are reference numbers)."""
    hits, strong = match_facts_detail(ex, load)
    return hits, len(strong)


def match_facts_detail(ex, load: dict) -> tuple[list[str], list[str]]:
    """([every fact that matches], [the reference numbers among them])."""
    page = [(_norm(n.value), _whole(n.value)) for n in ex.numbers]
    page += [("", _whole(x)) for x in (ex.pieces, ex.weight_lbs) if x]
    facts = load_facts(load)
    hits: list[str] = []
    strong: list[str] = []
    used: set[int] = set()          # one number on the page is one fact, however many fields it resembles
    matched: set[int] = set()

    def same(norm: str, whole: str, value: str, is_strong: bool, loose: bool) -> bool:
        if not is_strong:
            return bool(whole) and whole == value
        if norm == value:
            return bool(norm)
        if not loose or not norm:
            return False
        if min(len(norm), len(value)) >= 6 and (norm in value or value in norm):
            return True
        # "Cust PO 18650" on a Perricone Farms BOL is TransportPro's "PO18650" (load 2562069, 24 Sep
        # 2026). The same digits with only letters round them, five of them at least.
        core = _core(value)
        return core.isdigit() and len(core) >= 5 and _core(norm) == core

    # Exact matches first, so "P2600001" is the pickup number it equals and not the load number it contains.
    for loose in (False, True):
        for fi, (label, value, is_strong, shown) in enumerate(facts):
            if fi in matched:
                continue
            for pi, (norm, whole) in enumerate(page):
                if pi not in used and same(norm, whole, value, is_strong, loose):
                    used.add(pi)
                    matched.add(fi)
                    hits.append(f"{label} {shown}")
                    if is_strong:
                        strong.append(f"{label} {shown}")
                    break
    for side, wp_type in (("shipper", "SH"), ("consignee", "CN")):
        paper = _norm(getattr(ex, side).city or "")
        for wp in load.get("waypoints") or []:
            city = (wp.get("location") or {}).get("city")
            if paper and wp.get("type") == wp_type and city and _norm(city) == paper:
                hits.append(f"{side} city {city}")
                break
    return hits, strong


# -------------------------------------------------------------------------------- pictures ----

def picture_sig(data: bytes) -> list[list[int]]:
    """A 32x32 greyscale thumbnail of each page: the first image of each PDF page, or the image."""
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    def thumb(raw: bytes) -> list[int]:
        return list(Image.open(io.BytesIO(raw)).convert("L").resize((32, 32)).tobytes())

    out: list[list[int]] = []
    try:
        if data[:5] == b"%PDF-":
            import pymupdf
            doc = pymupdf.open(stream=data, filetype="pdf")
            for pno in range(doc.page_count):
                imgs = doc.get_page_images(pno)
                raw = doc.extract_image(imgs[0][0])["image"] if imgs else doc[pno].get_pixmap(dpi=40).tobytes("png")
                out.append(thumb(raw))
        else:
            out.append(thumb(data))
    except Exception:                                            # noqa: BLE001 - no picture: same-file check only
        return []
    return out


def _diff(a: list[int], b: list[int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


def cached_sig(conn, sha: str) -> list | None:
    got = conn.execute("SELECT sig FROM picture_sig WHERE sha256=?", (sha,)).fetchone()
    return json.loads(got[0]) if got else None


def store_sig(conn, sha: str, data: bytes) -> list:
    sig = picture_sig(data)
    conn.execute("INSERT OR REPLACE INTO picture_sig (sha256, sig) VALUES (?,?)", (sha, json.dumps(sig)))
    return sig


def doc_sig(conn, store, tpro, d: Doc) -> list:
    got = cached_sig(conn, d.sha256)
    if got is not None:
        return got
    d.data = d.data or doc_bytes(store, tpro, d)
    return store_sig(conn, d.sha256, d.data)


# ------------------------------------------------------------------------------- uploading ----

def _upload(conn, tpro, uploader, store, s: Settings, row, filed: list[OnFile], group: list[Decision],
            stats: Stats) -> None:
    load_id = int(row["load_id"])
    kind = group[0].kind
    upload_as = filing.file_as(REAL_TYPE[kind])
    # Straight before the upload, look again: somebody may have filed it, or the load cleared, since.
    load = tpro.load(load_id)
    fresh = on_file(conn, tpro, load_id, tpro.files(load_id))
    known = {str(of.file.get("id")) for of in filed}
    filed.extend(of for of in fresh if str(of.file.get("id")) not in known)
    members = []
    pages_along = []
    set_refs = {x for g in group if not g.companion for x in g.strong}
    for dec in sorted(group, key=lambda x: x.doc.order):
        if _docs_received(load):
            dec.outcome, dec.status = NOT_NEEDED, "NOT NEEDED - the load now shows Documents Received; nothing uploaded"
            dec.final, dec.companion = True, False
            _record(conn, s, row, dec)
            continue
        again = judge(conn, s, row, load, filed, dec.doc, json.loads(db.get_attachment(conn, dec.doc.sha256)["extraction_json"]),
                      sig=lambda d=dec.doc: doc_sig(conn, store, tpro, d))
        if dec.companion:
            # Still only a page of this set, and still not on the load under a type that clears.
            if again is not None and can_join(again, set_refs, kind, s):
                again.companion = True
                pages_along.append(again)
            elif again is not None:
                _record(conn, s, row, again)
            continue
        if again is None or not again.ready:
            if again is not None:
                _record(conn, s, row, again)
            continue
        members.append(again)
    if not members:
        for page in pages_along:
            page.companion = False
            _record(conn, s, row, page)          # without its POD, a page's own decision stands
        return
    members = sorted(members + pages_along, key=lambda x: x.doc.order)
    for m in members:
        m.doc.data = m.doc.data or doc_bytes(store, tpro, m.doc)
    data, filename, content_type = upload_payload([m.doc.data for m in members], kind, load_id)
    comment = upload_comment(members, kind, load_id)
    # One row per upload in the sheet (24 Sep 2026): the page the upload is judged on carries it -
    # the POD's signed page, a BOL set's front - and lists every page. The others are still recorded,
    # page by page, in the ledger.
    lead = next((m for m in members if m.kind == kind and not m.companion), members[0])
    if s.mode != ON:
        for m in members:
            m.outcome, m.final = DRY, False
            m.status = f"DRY RUN - would upload as {upload_as}"
            _record(conn, s, row, m, upload_as=upload_as, comment=comment, group=members)
        print(f"  auto-upload DRY RUN: load {load_id} {kind} x{len(members)} as {upload_as}: {comment}")
        return
    try:
        result = uploader.upload_file(record_type="Loads", record_id=load_id, document_type=upload_as,
                                      comments=comment, filename=filename, data=data, content_type=content_type)
    except TProError as e:
        stats.errors += 1
        for m in members:
            tries = _attempts(conn, load_id, m.doc.sha256) + 1
            if tries >= MAX_UPLOAD_ATTEMPTS:
                m.outcome, m.final = HELD, True
                m.status = f"HELD - the upload failed {tries} times ({str(e)[:90]}); a person should check File History"
            else:
                m.outcome, m.final = WAITING, False
                m.status = f"WAITING - the upload failed ({str(e)[:90]}); tried again next run after a fresh File History check"
            _record(conn, s, row, m, upload_as=upload_as, comment=comment, group=members, attempts=tries,
                    sheet=m is lead)
        return
    file_id = uploaded_file_id(result)
    now = db.now_iso()
    try:
        # The check after: is it in File History now? A failure here must not lose the record of an
        # upload that has already happened.
        listed = bool(file_id) and any(str(f.get("id")) == file_id for f in tpro.files(load_id))
    except TProError:
        listed = False
    status = (f"UPLOADED {now[:16].replace('T', ' ')} UTC as {upload_as}"
              + ("" if listed else " (TransportPro accepted it; not listed in File History yet)"))
    sha = hashlib.sha256(data).hexdigest()
    new_file = {"id": int(file_id) if file_id.isdigit() else file_id, "fileTypeId": TYPE_IDS.get(upload_as),
                "fileTypeName": upload_as, "comments": comment, "dateCreated": now}
    if file_id.isdigit():
        db.record_tpro_file(conn, load_id, new_file, sha256=sha, size=len(data))
    filed.append(OnFile(new_file, sha, store_sig(conn, sha, data), data))
    for m in members:
        m.outcome, m.status, m.tpro_file_id = UPLOADED, status, file_id
        _record(conn, s, row, m, upload_as=upload_as, comment=comment, group=members, sheet=m is lead)
        conn.execute("INSERT OR IGNORE INTO filing (load_id, sha256, tpro_file_id, document_type, comment, filed_at) "
                     "VALUES (?,?,?,?,?,?)", (load_id, m.doc.sha256, file_id, upload_as, comment, now))
        notify.record(conn, load_id=load_id, event=notify.FILED, sha256=m.doc.sha256, document_type=upload_as,
                      filename=m.doc.filename, reason=f"uploaded by the bot as {upload_as}; the page is a {kind}")
    conn.execute("UPDATE load SET next_check_at=? WHERE load_id=?", (now, load_id))
    stats.uploads += 1
    stats.uploaded += len(members)
    print(f"  auto-upload: load {load_id} {kind} x{len(members)} uploaded as {upload_as}, file {file_id}")


def _attempts(conn, load_id: int, sha: str) -> int:
    got = conn.execute("SELECT attempts FROM autofile WHERE load_id=? AND sha256=?", (load_id, sha)).fetchone()
    return int(got[0]) if got else 0


def _kind_of(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return "other"


def upload_payload(parts: list[bytes], kind: str, load_id: int) -> tuple[bytes, str, str]:
    """(bytes, filename, content type) - always one PDF. A PDF that arrives on its own goes up as it
    is; a photo, several pages, or an iPhone HEIC are made into one PDF first. File History is PDFs -
    TransportPro turns even a driver's texted picture into one - and the pod asked for the bot's
    uploads to match (2535235's emailed photo went up as a JPEG, 24 Sep 2026)."""
    if len(parts) == 1 and _kind_of(parts[0]) == "pdf":
        return parts[0], f"{kind}_{load_id}.pdf", "application/pdf"
    return combine_pdf(parts, f"{kind} - load {load_id}"), f"{kind}_{load_id}.pdf", "application/pdf"


def combine_pdf(parts: list[bytes], title: str) -> bytes:
    import pymupdf
    out = pymupdf.open()
    for data in parts:
        kind = _kind_of(data)
        if kind == "pdf":
            out.insert_pdf(pymupdf.open(stream=data, filetype="pdf"))
            continue
        if kind == "other":
            from PIL import Image
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            buf = io.BytesIO()
            Image.open(io.BytesIO(data)).convert("RGB").save(buf, format="JPEG", quality=92)
            data, kind = buf.getvalue(), "jpg"
        img = pymupdf.open(stream=data, filetype=kind)
        out.insert_pdf(pymupdf.open("pdf", img.convert_to_pdf()))
    out.set_metadata({"title": title, "producer": BOT, "creator": BOT})
    return out.tobytes(garbage=3, deflate=True)


def page_detail(ex) -> list[str]:
    """What the page shows beyond its kind - ["signed by Kendyl 9/24/26"] - for the comment and the sheet.

    The reader's own asides are dropped, and nothing is cut to a word count. Load 2573776 (24 Sep
    2026) went up as "POD, signed 9/24/24 (as written; likely 9/24/26), 3, 3 pages": the receiver
    date carried the reader's note on the year, and trimming the text to eight words left the "3" of
    "3 pages" behind. The page count is the caller's, from every page actually uploaded.
    """
    from pod_intake.matcher import brief_page_comment
    text = re.sub(r"\s*\([^)]*\)", "", brief_page_comment(ex, max_words=60))
    return [b.strip() for b in text.split(", ")[1:] if b.strip() and not re.fullmatch(r"\d+ pages?", b.strip())]


def upload_comment(members: list[Decision], kind: str, load_id: int) -> str:
    """"Doc Intake Bot: POD, signed by Kendyl 9/24/26, 2 pages - load 2562005": what the page is,
    whatever type it went in under."""
    lead = next((m for m in members if m.kind == kind and not m.companion), members[0])
    detail = page_detail(lead.ex)
    if kind == "BOL" and not any("signed" in d and "unsigned" not in d for d in detail):
        # The sign-out side carries the signatures on a pick slip; say so rather than "unsigned".
        signed = next((d for m in members for d in page_detail(m.ex) if "signed" in d and "unsigned" not in d), None)
        detail = [signed] + [d for d in detail if d != "unsigned"] if signed else detail
    bits = [kind] + detail
    pages = sum(max(1, len(m.ex.pages)) for m in members)
    if pages > 1:
        bits.append(f"{pages} pages")
    if lead.refiles:
        # Every page that is a copy of a file already on the load, in page order: "copy of Driver
        # Supplied BOL 31442466 + 31442467".
        kind_name, _, lead_id = lead.refiles.rpartition(" ")
        ids = [str(m.doc.on_file.file.get("id")) for m in members if m.doc.on_file is not None]
        ids = ids if lead_id in ids else [lead_id] + ids
        bits.append(f"copy of {kind_name} {' + '.join(dict.fromkeys(ids))}")
    return f"{BOT}: {', '.join(bits)} - load {load_id}"


# ------------------------------------------------------------------------------ recording ----

def _record(conn, s: Settings, row, dec: Decision, *, upload_as: str = "", comment: str = "",
            group: list[Decision] | None = None, attempts: int | None = None, sheet: bool = True) -> None:
    """Write the decision. A final one is never overwritten, so nothing decided is decided twice -
    except by an upload: a page logged "already on file" that then goes up as part of its POD."""
    d = dec.doc
    logged = 0 if dec.outcome in UNLOGGED or not sheet else 1
    values = sheet_row(s, row, dec, upload_as=upload_as, comment=comment, group=group) if logged else None
    conn.execute(
        "INSERT INTO autofile (load_id, sha256, source, outcome, final, status, row_json, logged, tpro_file_id, "
        "attempts, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(load_id, sha256) DO UPDATE SET source=excluded.source, outcome=excluded.outcome, "
        "final=excluded.final, status=excluded.status, row_json=excluded.row_json, logged=excluded.logged, "
        "tpro_file_id=COALESCE(excluded.tpro_file_id, autofile.tpro_file_id), attempts=excluded.attempts, "
        "decided_at=excluded.decided_at WHERE autofile.final = 0 OR excluded.outcome = 'uploaded'",
        (int(row["load_id"]), d.sha256, d.source, dec.outcome, 1 if dec.final else 0, dec.status or dec.outcome,
         json.dumps(values) if values is not None else None, logged, dec.tpro_file_id,
         attempts if attempts is not None else _attempts(conn, int(row["load_id"]), d.sha256), db.now_iso()))


def sheet_row(s: Settings, row, dec: Decision, *, upload_as: str = "", comment: str = "",
              group: list[Decision] | None = None) -> list:
    """Columns A..N of the Upload log. O and P are the pod's."""
    d, ex = dec.doc, dec.ex
    name = d.filename
    if group and len(group) > 1:
        names = [g.doc.filename for g in sorted(group, key=lambda g: g.doc.order)]
        pages = sum(max(1, len(g.ex.pages)) if g.ex is not None else 1 for g in group)
        name = " + ".join(names) + f" - one {pages}-page PDF"
    read_as = dec.quick
    if ex is not None and dec.kind:
        pages = len(ex.pages)
        detail = ", ".join(page_detail(ex) + ([f"{pages} pages"] if pages > 1 else []))
        read_as = dec.kind + (f" - {detail}" if detail else "")
    facts = ((f"{len(dec.facts)} fact(s): " + "; ".join(dec.facts)) if dec.facts
             else "not checked - not read in full" if ex is None else "nothing on the page matches")
    checks = ("FAILED: " + "; ".join(dec.failed)) if dec.failed else (
        "all passed" if dec.outcome in (UPLOADED, DRY) else "")
    shows_upload = dec.outcome in (UPLOADED, DRY) or (dec.outcome in (WAITING, HELD) and upload_as)
    return [db.now_iso()[:16].replace("T", " "), int(row["load_id"]), row["customer"] or "",
            s.pods.get(int(row["terminal"] or 0), str(row["terminal"] or "")), name, d.arrived, read_as,
            f"{dec.conf:.0%}" if dec.conf is not None else "", facts, checks,
            upload_as if shows_upload else "", comment if shows_upload else "",
            dec.tpro_file_id or "", dec.status]


def flush_log(conn, log) -> int:
    """Send every row whose status the sheet does not show yet. A failure leaves them pending.

    Only SHEET_OUTCOMES reach the sheet. Every decision is still recorded in the ledger - the audit
    trail is complete - but the pod asked (24 Sep 2026) for the sheet to hold what the bot uploaded
    and what it held back and why, not the pages it found already on file or not needed."""
    marks = ",".join("?" * len(SHEET_OUTCOMES))
    # Rows the sheet showed that it should not any more: a page that went up inside another page's
    # row. Removed by Ref; a row the pod has marked (Correct? / Pod note) is never removed.
    gone = conn.execute(
        "SELECT load_id, sha256 FROM autofile WHERE logged_status IS NOT NULL "
        f"AND (logged = 0 OR outcome NOT IN ({marks}))", SHEET_OUTCOMES).fetchall()
    if gone and hasattr(log, "remove"):
        log.remove({ref(r["load_id"], r["sha256"]) for r in gone})
        for r in gone:
            conn.execute("UPDATE autofile SET logged_status=NULL WHERE load_id=? AND sha256=?",
                         (r["load_id"], r["sha256"]))
    rows = conn.execute(
        "SELECT load_id, sha256, status, row_json FROM autofile WHERE logged=1 AND row_json IS NOT NULL "
        f"AND outcome IN ({marks}) AND (logged_status IS NULL OR logged_status != status) "
        "ORDER BY decided_at, load_id", SHEET_OUTCOMES).fetchall()
    if not rows:
        return 0
    n = log.write([(ref(r["load_id"], r["sha256"]), json.loads(r["row_json"])) for r in rows])
    for r in rows:
        conn.execute("UPDATE autofile SET logged_status=? WHERE load_id=? AND sha256=?",
                     (r["status"], r["load_id"], r["sha256"]))
    return n


def ref(load_id: int, sha: str) -> str:
    return f"{load_id}-{sha[:12]}"


def pod_names(config: dict) -> dict[int, str]:
    """'POD (Frankie Saiz)' -> 'Frankie Saiz', from the pod terminal map."""
    out = {}
    for t in config.get("terminals") or []:
        m = re.match(r"\s*POD\s*\((.*)\)\s*$", str(t.get("name") or ""))
        out[int(t["id"])] = (m.group(1) if m else str(t.get("name") or t["id"])).strip()
    return out
