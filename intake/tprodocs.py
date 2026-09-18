"""Loop C: the paperwork already attached to the load.

Loop A sees what arrives by email. Loop B sees what TransportPro says a load is short. Neither ever
looked at a document already on the load, and that is where the money is sitting: 74 in-view loads
carry nothing but a Driver Supplied BOL, which means the paperwork is physically there and the
status is stuck on its TYPE, not on anything wrong with the paper (measured 15 Sep 2026 over 266
loads - of the 96 that cleared, 94 carried a type 12).

Until now the service could see that such a file existed and never what was on it, so it could say
"re-file this properly" but never "and here is what it is". This closes that: list a load's File
History, record every file, download the ones that could carry driver paperwork, hash them, and let
the ledger do the rest.

The two sides meet at the SHA-256. A document that was emailed in AND filed on the load hashes the
same, so its reading is already paid for - and the ledger can then prove the filed file and the
emailed file are the same bytes, which is the question a reviewer actually has.

Listing is one call per load and free. Downloading is free. Only reading costs anything, and it is
off by default, exactly as on the mail side.
"""
from __future__ import annotations

import hashlib

from . import db, state as st
from .ingest import Reader, Stats, permanent_read_error
from .tpro import TProError, TransportPro

# The types that can carry a driver's paperwork. A load's File History is mostly rate confirmations
# and billing packets TransportPro generated itself; downloading those would be work with no
# possible outcome, so the filter is by type rather than by trying and discarding.
PAPERWORK_TYPES: tuple[int, ...] = tuple(sorted(set(st.BOL_TYPES) | set(st.POD_TYPES)))

# Loads worth scanning: the paperwork is on them and the status has not cleared. A complete load has
# nothing to recover and an undelivered one has nothing to re-file yet.
SCAN_STATES = ("wrong_doc_type", "filed_status_pending")


def scan_load(conn, tpro: TransportPro, load_id: int, *, reader: Reader | None = None,
              st_: Stats | None = None, download: bool = True, verbose: bool = False) -> int:
    """List one load's files, record them, and optionally fetch and read the paperwork.

    Returns how many files were newly hashed. Safe to re-run: a file whose bytes are already in the
    ledger is never downloaded again, and a reading is never paid for twice.
    """
    st_ = st_ or Stats()
    try:
        files = tpro.files(load_id)
    except TProError as e:
        print(f"  ! load {load_id}: {e}")
        return 0
    for f in files:
        if f.get("id"):
            db.record_tpro_file(conn, load_id, f)
    if not download:
        return 0

    hashed = 0
    for row in db.tpro_files_needing_download(conn, load_ids=[load_id], type_ids=PAPERWORK_TYPES):
        try:
            data, meta = tpro.download_file(int(row["tpro_file_id"]))
        except TProError as e:
            print(f"  ! file {row['tpro_file_id']} on load {load_id}: {e}")
            continue
        st_.downloads += 1
        digest = hashlib.sha256(data).hexdigest()
        db.record_tpro_file(conn, load_id, {**meta, "id": row["tpro_file_id"]},
                            sha256=digest, size=len(data))
        hashed += 1

        existing = db.get_attachment(conn, digest)
        if existing is not None and existing["extraction_json"] is not None:
            # Already read - almost always because the same document came in by email. This is the
            # whole point of hashing both sides: the filed file is identified for nothing.
            st_.reads_avoided += 1
            if verbose:
                came_by_mail = db.source_part(conn, digest) is not None
                print(f"  = load {load_id} {row['file_type_name']}: already read as "
                      f"{existing['document_type']}" + (" (same bytes as the emailed copy)" if came_by_mail else ""))
            continue
        if db.read_blocked(existing):
            if verbose:
                print(f"  x load {load_id} {row['file_type_name']}: unreadable, not retried")
            continue
        if existing is None:
            st_.new_files += 1
        if reader is None:
            if existing is None:
                db.put_attachment(conn, digest, message_id="", filename=row["filename"], size=len(data),
                                  extraction=None, document_type=None, model=None, cost_usd=None)
            if verbose:
                print(f"  + load {load_id} {row['file_type_name']:20} {len(data)//1024:5} KB  recorded, not read")
            continue
        try:
            extraction, doc_type, model, cost = reader(data, row["filename"] or f"{digest[:12]}.pdf")
        except Exception as e:  # noqa: BLE001 - one unreadable file must not end the pass
            st_.read_errors += 1
            db.put_attachment(conn, digest, message_id="", filename=row["filename"], size=len(data),
                              extraction=None, document_type=None, model=None, cost_usd=None,
                              error=f"{type(e).__name__}: {e}"[:300], permanent=permanent_read_error(e))
            print(f"  ! load {load_id} {row['filename']}: reader failed - {type(e).__name__}: {e}")
            continue
        db.put_attachment(conn, digest, message_id="", filename=row["filename"], size=len(data),
                          extraction=extraction, document_type=doc_type, model=model, cost_usd=cost)
        st_.reads += 1
        st_.cost_usd += cost or 0.0
        print(f"  + load {load_id} filed as {row['file_type_name']:20} reads as {doc_type} (${cost:.4f})")

    if hashed:
        # The load's paperwork picture just changed; look at it now rather than on its old cadence.
        conn.execute("UPDATE load SET next_check_at=? WHERE load_id=?", (db.now_iso(), load_id))
    return hashed


def scan_pass(conn, tpro: TransportPro, *, load_ids: list[int] | None = None,
              states: tuple[str, ...] = SCAN_STATES, limit: int = 50,
              reader: Reader | None = None, max_spend_usd: float | None = None,
              download: bool = True, verbose: bool = False) -> Stats:
    """Scan the loads whose paperwork is on the load and whose status has not cleared."""
    stats = Stats()
    stats.mode = "tpro-scan"
    todo = load_ids or due_loads_for_scan(conn, states, limit)
    for load_id in todo:
        if max_spend_usd is not None and stats.cost_usd >= max_spend_usd and not stats.spend_capped:
            stats.spend_capped = True
            print(f"  spend cap ${max_spend_usd:.2f} reached; still recording and hashing, no further reads")
        scan_load(conn, tpro, load_id, reader=None if stats.spend_capped else reader,
                  st_=stats, download=download, verbose=verbose)
    return stats


def due_loads_for_scan(conn, states: tuple[str, ...], limit: int) -> list[int]:
    """In-view loads in the given states, most repairable first.

    `states` is in priority order and the ordering honours it. That matters more than it looks: a
    wrong_doc_type load has its paperwork under a type that CANNOT clear the status, so looking at
    the file is exactly what makes the repair possible, while a filed_status_pending load already
    carries a clearing type and something subtler is wrong. Measured 18 Sep 2026, ordering only by
    next_check_at spent a whole 12-load pass on filed_status_pending and never reached one of the 74
    wrong_doc_type loads the loop exists for.

    Then loads never scanned before, then oldest due - so a capped pass makes progress instead of
    re-taking the same head of the list.
    """
    cases = " ".join(f"WHEN ? THEN {i}" for i in range(len(states)))
    rows = conn.execute(
        "SELECT l.load_id FROM load l WHERE l.in_view = 1 AND l.state IN ("
        + ",".join("?" * len(states)) + ") "
        f"ORDER BY CASE l.state {cases} ELSE 99 END, "
        "  (SELECT COUNT(*) FROM tpro_file t WHERE t.load_id = l.load_id), l.next_check_at "
        "LIMIT ?", (*states, *states, limit)).fetchall()
    return [int(r["load_id"]) for r in rows]


def line(stats: Stats, loads: int) -> str:
    """This loop's own one-liner. Stats is shared with Loop A, but Loop A's line() talks about
    messages, threads and parts, none of which exist here."""
    return (f"tpro-scan: {loads} load(s) scanned, {stats.downloads} file(s) downloaded and hashed, "
            f"{stats.new_files} new to the ledger, {stats.reads_avoided} already read "
            f"(no charge), {stats.reads} read"
            + (f", {stats.read_errors} read error(s)" if stats.read_errors else "")
            + (" [SPEND CAP HIT]" if stats.spend_capped else "")
            + f", spend ${stats.cost_usd:.3f}")


def summary(conn, load_id: int) -> list[dict]:
    """What is on the load, and what the service knows about each file. For `intake load`."""
    rows = conn.execute(
        "SELECT t.*, a.document_type, a.extraction_json IS NOT NULL AS read_, "
        "       (SELECT COUNT(*) FROM part p WHERE p.sha256 = t.sha256) AS mail_copies "
        "FROM tpro_file t LEFT JOIN attachment a ON a.sha256 = t.sha256 "
        "WHERE t.load_id = ? ORDER BY t.date_created", (load_id,)).fetchall()
    return [dict(r) for r in rows]
