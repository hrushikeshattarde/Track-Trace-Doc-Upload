"""Loop B: the load side.

Two jobs, deliberately separate because they have different costs and different cadences.

`reconcile()` keeps the ledger's coverage honest. It reproduces the Load Management filter through
/load/search and makes sure every load in the view has a row. There is no --max and no truncation:
readiness.py's `sorted(set(loads), reverse=True)[: args.max]` kept the newest 150 of 539 and
dropped the oldest 389 - the aged, stuck ones - and that is the failure this whole design exists to
remove. Run the narrow window hourly and the full one nightly as an audit.

`drain()` does the work. It takes loads whose next_check_at has come round, oldest first, reads
their current state from TransportPro and writes back a state and the next due time. A backlog
therefore delays a load; it can never lose one. No model calls, and no Gmail calls either - whether
paperwork is sitting in the thread is answered from the ledger Loop A already filled.

Nothing here writes to TransportPro.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from . import db, state as st
from .tpro import TProError, TransportPro


@dataclass
class ReconcileStats:
    terminals: int = 0
    seen: int = 0
    created: int = 0
    already: int = 0
    per_terminal: dict[int, int] = field(default_factory=dict)
    left_view: int = 0
    wrong_level: int = 0
    stale_cleared: int = 0

    def line(self) -> str:
        return (f"reconcile: {self.seen} load(s) in the Load Management view across "
                f"{self.terminals} terminal(s) - {self.created} new to the ledger, "
                f"{self.already} already there, {self.left_view} evicted from the view"
                + (f"; {self.wrong_level} dropped by service level" if self.wrong_level else "")
                + (f"; {self.stale_cleared} stale non-view rows tidied" if self.stale_cleared else ""))


@dataclass
class DrainStats:
    checked: int = 0
    errors: int = 0
    states: dict[str, int] = field(default_factory=dict)
    tpro_calls: int = 0

    def line(self) -> str:
        by = ", ".join(f"{k} {v}" for k, v in sorted(self.states.items(), key=lambda kv: -kv[1])) or "none"
        return (f"drain: {self.checked} load(s) checked, {self.errors} deferred on error, "
                f"{self.tpro_calls} TransportPro calls | {by}")


def date_chunks(start: dt.date, end: dt.date, max_days: int) -> list[tuple[dt.date, dt.date]]:
    out, cur = [], start
    while cur <= end:
        nxt = min(cur + dt.timedelta(days=max_days), end)
        out.append((cur, nxt))
        if nxt >= end:
            break
        cur = nxt
    return out


def search_window(tpro: TransportPro, params: dict, start: dt.date, end: dt.date, depth: int = 0) -> list[dict]:
    """/load/search over [start, end]. The API requires a date range and rejects wide ones (HTTP 400
    somewhere around 60 days, probed 14 Sep 2026), so a rejected window is bisected rather than
    skipped - a silently dropped window is a silently dropped set of loads."""
    try:
        return tpro.search_all_pages({**params, "pickupDateStart": start.isoformat(),
                                      "pickupDateEnd": end.isoformat()})
    except TProError as e:
        if e.status != 400 or (end - start).days < 1 or depth > 8:
            print(f"  warning: /load/search {start}..{end} {params} failed: {e}")
            return []
        mid = start + (end - start) / 2
        return (search_window(tpro, params, start, mid, depth + 1)
                + search_window(tpro, params, mid + dt.timedelta(days=1), end, depth + 1))


def reconcile(conn, tpro: TransportPro, *, terminals: list[int], days_back: int = 3,
              days_forward: int = 45, statuses: tuple[str, ...] = ("Dispatched",),
              scope_levels: set[str] | None = None, authoritative: bool = False,
              verbose: bool = False) -> ReconcileStats:
    """Give every load in the dashboard view a ledger row.

    days_back is the knob that separates the hourly pass from the nightly audit: a narrow window
    catches everything newly dispatched cheaply, the full one reproduces the whole view.

    `authoritative` is the safety catch on that. Only a sweep that covered the WHOLE window may
    conclude a load has left the view - a narrow sweep simply did not look at long-haul loads whose
    pickup is older than the window, and evicting those would silently drop exactly the aged,
    still-moving freight this design exists to keep. Measured 15 Sep 2026: a 3-day sweep saw 456
    loads where the full window saw 527+.
    """
    today = dt.date.today()
    start, end = today - dt.timedelta(days=days_back), today + dt.timedelta(days=days_forward)
    rs = ReconcileStats(terminals=len(terminals))
    run_start = db.now_iso()          # anything not re-stamped by this sweep has left the view
    for tid in terminals:
        rows: list[dict] = []
        for status in statuses:
            for w_start, w_end in date_chunks(start, end, 45):
                rows += search_window(tpro, {"terminalId": str(tid), "loadStatus": status}, w_start, w_end)
        unique = {int(r["id"]): r for r in rows if r.get("id")}
        kept = 0
        for load_id, row in unique.items():
            status = row.get("status") or {}
            if (status.get("loadStatus") or "").lower().startswith("cancel"):
                continue
            # The saved filter is terminals + loadStatus + SERVICE LEVEL. /load/search honours the
            # first two; the service level lives on the stops, so it has to be applied here. Without
            # it the sweep returns every Flexible / FCFS and Firm Appointment load on the pods too
            # (179 extra on 14 Sep 2026), and they land in_view as though they were dashboard work.
            levels = st.service_levels(row)
            if scope_levels and levels and not (levels & scope_levels):
                rs.wrong_level += 1
                continue
            kept += 1
            rs.seen += 1
            existed = conn.execute("SELECT 1 FROM load WHERE load_id=?", (load_id,)).fetchone() is not None
            # due_now on creation only: an existing row keeps the cadence drain() gave it, so
            # reconciling never resets the clock on work already scheduled.
            db.upsert_load(conn, load_id, source="dashboard", due_now=not existed)
            db.mark_in_view(conn, load_id)
            rs.created += 0 if existed else 1
            rs.already += 1 if existed else 0
        rs.per_terminal[tid] = kept
        if verbose:
            print(f"  terminal {tid}: {kept} load(s) in view")
    rs.stale_cleared = db.clear_stale_out_of_view(conn)
    if authoritative:
        rs.left_view = db.drop_out_of_view(conn, run_start)
    return rs


def drain(conn, tpro: TransportPro, *, limit: int = 100, scope_levels: set[str] | None = None,
          with_sms: bool = False, verbose: bool = False) -> DrainStats:
    """Check every load whose next_check_at has come round, oldest first."""
    ds = DrainStats()
    before = tpro.calls
    for row in db.due_loads(conn, limit):
        load_id = int(row["load_id"])
        try:
            load = tpro.load(load_id)
            dispatches = tpro.dispatches(load_id)
            files = tpro.files(load_id)
        except TProError as e:
            # A load that cannot be read is pushed out and kept. It stays in the coverage count,
            # so a systematic API failure shows up as queue lag rather than as loads quietly gone.
            db.defer_load(conn, load_id, str(e))
            ds.errors += 1
            print(f"  ! load {load_id}: {e}")
            continue
        docs, unread = db.load_doc_evidence(conn, load_id)
        dropped = db.load_dropped_evidence(conn, load_id)
        assessment = st.assess(load_id, load, dispatches, files, ledger_docs=docs, ledger_unread=unread,
                               ledger_dropped=dropped, pod_claims=db.filed_pod_claims(conn, load_id),
                               scope_levels=scope_levels)
        db.update_load(conn, load_id, assessment)
        ds.checked += 1
        ds.states[assessment["state"]] = ds.states.get(assessment["state"], 0) + 1
        if verbose:
            print(f"  {load_id}  {assessment['state']:22} {assessment['stage'] or '':13} {assessment['action'][:88]}")
    ds.tpro_calls = tpro.calls - before
    return ds


def pod_terminals(path) -> tuple[list[int], set[str]]:
    """The ticked pod terminals and the service levels in scope, from index/pod_terminals.json -
    the same file readiness.py reads, written from the Load Management filter panel."""
    import json
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return [], set()
    data = json.loads(p.read_text(encoding="utf-8"))
    terminals = [int(t["id"]) for t in data.get("terminals", []) if t.get("in_current_view")]
    levels = {x.lower() for x in (data.get("dashboard_filter") or {}).get("service_level", [])}
    return terminals, levels


def work_queue(conn, limit: int = 40) -> list[Any]:
    """What a pod lead would work, most urgent first. Order matches the cadence: the POD window
    first, then BOLs, then the filed-but-stuck loads."""
    # pod_unsigned leads because it is the only state that is actively lying to everybody else: the
    # load reads Documents Received, billing is open, and the POD does not exist. Everything below it
    # is at least honest about being unfinished.
    order = ("pod_unsigned", "pod_expected", "bol_expected", "pod_unverified",
             "filed_status_pending", "wrong_doc_type", "error", "not_yet_due", "out_of_scope",
             "complete")
    cases = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(order))
    return conn.execute(
        f"SELECT * FROM load WHERE in_view = 1 AND state IS NOT NULL AND state != 'new' "
        f"ORDER BY CASE state {cases} ELSE 99 END, next_check_at LIMIT ?", (limit,)).fetchall()
