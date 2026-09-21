"""What state a load is in, and when to look at it again.

The rules are readiness.py's, which were established against live freight on 14 Sep 2026. They are
restated here rather than imported because readiness.py pulls the paystatus bot's checkout in at
module scope; when that prototype retires, this is the copy that stays.

The part that is new is the cadence. readiness.py checks whatever fits under --max and drops the
rest; this module gives every load a next_check_at matched to what it is waiting for, so a backlog
delays a load instead of losing it, and the 539 loads in the dashboard view do not all get polled
at the same rate:

    pod_expected          15 min   the money window - detention and deliver-out are decided here
    bol_expected           1 h     customers who gate on "BOL before leaving the shipper"
    wrong_doc_type         1 h     paperwork is on the load under a type that does not clear it
    filed_status_pending   1 h     filed under a clearing type and still Waiting: check by hand
    not_yet_due            6 h     no paperwork can exist; only the stage can change
    out_of_scope          24 h     service level does get corrected on a stop
    in_review              never   a person owns it; event-driven
    complete               never   terminal
"""
from __future__ import annotations

import datetime as dt
import re

# TransportPro document type ids, from GET /files/document_types.
BOL_TYPES = {12: "Bill Of Lading", 363: "Driver Supplied BOL"}
POD_TYPES = {360: "Proof of Delivery", 53: "Delivery Receipt"}

# Which types actually clear "Waiting for Documents". Measured 15 Sep 2026 over 266 loads:
#
#   of the 96 that reached Documents Received, 94 carry a type 12 Bill Of Lading and only 2 carry
#   nothing but 363;
#   of the 170 still Waiting, 145 have ONLY 363.
#
# The same run refutes the timing explanation this project had been working from (PRD OQ-3, "the
# status clears only on an upload made after the Delivered mark"): 58% of cleared loads had a
# filing after that mark against 69% of stuck ones, so it separates nothing. The type does.
# 363 "Driver Supplied BOL" - what TransportPro itself files driver MMS photos as - puts a document
# on the load without satisfying it.
CLEARING_TYPES = {12, 360, 53}
NON_CLEARING_TYPES = {363}

STAGE_ORDER = {"planned": 0, "dispatched": 1, "at shipper": 2, "loaded": 3, "in transit": 3,
               "at consignee": 4, "delivered": 5}

CADENCE_MINUTES: dict[str, int | None] = {
    # A load whose POD claim the page contradicts. Daily rather than never: a person has to act, but
    # if they file a real POD the next check should notice and close it without being told.
    "pod_unsigned": 1440,
    # Nobody has read the document the claim rests on. Six-hourly, because reading it resolves this
    # on its own and the state exists to make that worth doing.
    "pod_unverified": 360,
    "pod_expected": 15,
    "wrong_doc_type": 60,
    "bol_expected": 60,
    "filed_status_pending": 60,
    "not_yet_due": 360,
    "out_of_scope": 1440,
    "not_in_view": None,
    "in_review": None,
    "complete": None,
    "error": 60,
}


def utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def active_first(dispatches: list[dict]) -> list[dict]:
    """Cancelled dispatches last, then newest first, so dispatches[0] is the truck actually moving
    the load. Load 2576660 (14 Sep 2026) had a cancelled carrier first, which gave the wrong status,
    the wrong text thread and the wrong trailer number."""
    return sorted(dispatches or [],
                  key=lambda d: ((d.get("status") or "").lower() == "canceled", -(int(d.get("id") or 0))))


def stage_of(dispatch_status: str | None, load_status: str | None) -> str:
    s = (dispatch_status or "").lower()
    for key in ("delivered", "at consignee", "tr drop at consignee", "loaded", "tr drop in transit",
                "in transit", "at shipper", "dispatched", "planned"):
        if key in s:
            return {"tr drop at consignee": "at consignee", "tr drop in transit": "loaded",
                    "in transit": "loaded"}.get(key, key)
    ls = (load_status or "").lower()
    return "delivered" if "deliver" in ls else ("dispatched" if "dispatch" in ls else "planned")


def is_pod_file(f: dict) -> bool:
    """Reps file signed PODs under "Bill Of Lading" with the comment "POD" (seen on 2577037 and
    2577850), so a BOL-type file whose comment says POD counts as the POD."""
    if f.get("fileTypeId") in POD_TYPES:
        return True
    return (f.get("fileTypeId") in BOL_TYPES
            and re.search(r"\bpod\b|proof|deliver", f.get("comments") or "", re.I) is not None)


def split_files(files: list[dict]) -> tuple[list[dict], list[dict]]:
    pod = [f for f in files if is_pod_file(f)]
    bol = [f for f in files if f.get("fileTypeId") in BOL_TYPES and not is_pod_file(f)]
    return bol, pod


def service_levels(load: dict) -> set[str]:
    """Every SERVICE_LEVEL reference on the load's stops. One dashboard load carried Firm
    Appointment on one stop and Priority / OP8 on another, so this is a set, not a value."""
    out = set()
    for w in load.get("waypoints") or []:
        for ref in w.get("reference") or []:
            if (ref.get("type") or "").upper() == "SERVICE_LEVEL" and ref.get("value"):
                out.add(str(ref["value"]).strip().lower())
    return out


def next_check_at(state: str, now: dt.datetime | None = None) -> str | None:
    minutes = CADENCE_MINUTES.get(state, 60)
    if minutes is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def assess(load_id: int, load: dict, dispatches: list[dict], files: list[dict], *,
           ledger_docs: int = 0, ledger_unread: int = 0, ledger_dropped: int = 0,
           pod_claims: dict | None = None, scope_levels: set[str] | None = None) -> dict:
    """One load's state, the reason, and when to look again.

    ledger_docs / ledger_unread come from the intake ledger rather than a Gmail search: Loop A has
    already recorded every document-bearing message for this load, so this loop spends no Gmail
    calls at all.

    ledger_dropped is the distinct attachments the free filters rejected on size or shape. It only
    ever appears in the reason, never in the state, and only when nothing else is in the ledger for
    this load: the filters are right nearly always, and a thread full of email signatures must not
    read as a load with paperwork. But when a load is short its POD and the ONLY thing in the
    mailbox is something a size threshold threw away, that is what the person working the queue
    needs to be told - and on live data that is 4 of 761 in-view loads, not a banner on every row.
    """
    status = load.get("status") or {}
    found_levels = service_levels(load)
    scope_levels = scope_levels or set()
    if scope_levels and found_levels and not (found_levels & scope_levels):
        return _row(load_id, load, "out_of_scope", "",
                    f"service level {', '.join(sorted(found_levels))}: not in scope", files)

    dispatches = active_first(dispatches)
    disp = dispatches[0] if dispatches else {}
    stage = stage_of(disp.get("status"), status.get("loadStatus"))
    rank = STAGE_ORDER.get(stage, 1)
    bol, pod = split_files(files)
    filed = sorted(bol + pod, key=lambda f: f.get("dateCreated") or "")
    docs_received = (status.get("documentStatus") or "").lower().startswith("documents received")

    # What the truck's stage calls for right now: the BOL until it reaches the consignee, the POD after.
    expects_pod = rank >= STAGE_ORDER["at consignee"]

    # When TransportPro marked the load Delivered (proxy: lastUpdated of the Delivered dispatch; the
    # API exposes no status timestamp). Kept for reporting only - the 15 Sep 2026 measurement showed
    # it does not predict whether the status clears. See CLEARING_TYPES.
    delivered_at = (utc(disp.get("lastUpdated"))
                    if (disp.get("status") or "").lower() == "delivered" and disp.get("lastUpdated") else None)
    if docs_received:
        # "Documents Received" is TransportPro agreeing with whoever filed the document, and what it
        # agrees with is the TYPE and the COMMENT - never the page. Where the service has read the
        # page and the page disagrees, the load is not done, whatever the status says. This is the
        # only place in the service that contradicts documentStatus, and it earns that by having
        # looked: see db.filed_pod_claims and load 2580687.
        claims = pod_claims or {}
        if claims.get("unsigned"):
            where = f" (TransportPro file {claims['unsigned_file']})" if claims.get("unsigned_file") else ""
            why = (f"documents received, but the POD on file{where} has no receiver signature and no "
                   f"receiving stamp: the page does not show the consignee took the freight. Billing "
                   f"will reject this. A real POD is still needed")
            return _row(load_id, load, "pod_unsigned", stage, why, files)
        if claims.get("claimed") and claims.get("unread") and not claims.get("verified"):
            why = (f"documents received on the strength of a comment: {claims['unread']} file(s) are "
                   f"filed as the POD but none has been read, so nothing has checked that the "
                   f"consignee actually signed. 'intake tpro-scan --read --loads {load_id}' settles it")
            return _row(load_id, load, "pod_unverified", stage, why, files)
        return _row(load_id, load, "complete", stage, "documents received", files)

    if filed:
        # The type is what decides this, not the timing - see CLEARING_TYPES above.
        types = {f.get("fileTypeId") for f in filed}
        if not (types & CLEARING_TYPES):
            names = ", ".join(sorted({f.get("fileTypeName") or str(f.get("fileTypeId")) for f in filed}))
            why = (f"filed only as {names}, which does not clear Waiting for Documents. The paperwork is "
                   f"on the load; re-file it as Bill Of Lading or Proof of Delivery and the status clears")
            return _row(load_id, load, "wrong_doc_type", stage, why, files)
        why = ("filed under a clearing type and still Waiting: does not fit the measured pattern, "
               "check by hand" + (f"; Delivered mark {delivered_at:%m/%d %H:%M}Z" if delivered_at else
                                  f"; truck is {stage}, a second document may still be due"))
        return _row(load_id, load, "filed_status_pending", stage, why, files)

    if rank < STAGE_ORDER["loaded"]:
        return _row(load_id, load, "not_yet_due", stage, f"truck is {stage}; no paperwork can exist yet", files)

    state = "pod_expected" if expects_pod else "bol_expected"
    want = "POD" if expects_pod else "BOL"
    if ledger_docs:
        # Deliberately silent about ledger_dropped here. The load already has paperwork to work
        # from, so its dropped attachments are almost certainly the signature blocks they usually
        # are, and saying so on every such load is noise: measured 16 Sep 2026, that is 9 in-view
        # loads, against the 4 where nothing else is in the ledger and the count is worth acting on.
        unread = f", {ledger_unread} not read yet" if ledger_unread else ""
        why = f"{want} expected; {ledger_docs} document(s) in the ratecon thread{unread} and nothing filed"
    elif ledger_dropped:
        why = (f"{want} expected; nothing filed and no document in the mail ledger, but "
               f"{ledger_dropped} attachment(s) in the thread were dropped by the size/shape "
               f"filters - a small or badly cropped photo of the paperwork looks like this. "
               f"intake reconsider --loads {load_id}")
    else:
        why = f"{want} expected; nothing filed and nothing in the mail ledger - ask the driver or carrier"
    return _row(load_id, load, state, stage, why, files)


def _row(load_id: int, load: dict, state: str, stage: str, why: str, files: list[dict]) -> dict:
    status = load.get("status") or {}
    bol, pod = split_files(files)
    return {
        "load_id": load_id,
        "state": state,
        "stage": stage,
        "action": why,
        "doc_status": status.get("documentStatus"),
        "load_status": status.get("loadStatus"),
        "terminal": load.get("assignedTerminal"),
        "customer": ((load.get("billingInfo") or {}).get("customer") or {}).get("companyName"),
        "service_level": ", ".join(sorted(service_levels(load))) or None,
        "filed_types": "; ".join(sorted({f.get("fileTypeName") or str(f.get("fileTypeId")) for f in bol + pod})) or None,
        "next_check_at": next_check_at(state),
    }
