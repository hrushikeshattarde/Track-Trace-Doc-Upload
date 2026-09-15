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
    filed_status_pending   1 h     retry once the Delivered mark lands
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

STAGE_ORDER = {"planned": 0, "dispatched": 1, "at shipper": 2, "loaded": 3, "in transit": 3,
               "at consignee": 4, "delivered": 5}

CADENCE_MINUTES: dict[str, int | None] = {
    "pod_expected": 15,
    "bol_expected": 60,
    "filed_status_pending": 60,
    "not_yet_due": 360,
    "out_of_scope": 1440,
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
           ledger_docs: int = 0, ledger_unread: int = 0, scope_levels: set[str] | None = None) -> dict:
    """One load's state, the reason, and when to look again.

    ledger_docs / ledger_unread come from the intake ledger rather than a Gmail search: Loop A has
    already recorded every document-bearing message for this load, so this loop spends no Gmail
    calls at all.
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
    # API exposes no status timestamp). Observed 14 Sep 2026 on 2572128 / 2577037 / 2575004: an upload
    # made BEFORE this moment leaves documentStatus at "Waiting for Documents" and the later Delivered
    # mark does not recompute it; an upload made AFTER flips it the same second. PRD OQ-3.
    delivered_at = (utc(disp.get("lastUpdated"))
                    if (disp.get("status") or "").lower() == "delivered" and disp.get("lastUpdated") else None)
    all_filed_before_delivered = bool(filed) and delivered_at is not None and all(
        (utc(f.get("dateCreated")) or delivered_at) < delivered_at for f in filed)

    if docs_received:
        return _row(load_id, load, "complete", stage, "documents received", files)

    if filed:
        why = "filed but status still Waiting"
        if all_filed_before_delivered:
            why += (f"; every filing predates the Delivered mark ({delivered_at:%m/%d %H:%M}Z) - the OQ-3 pattern. "
                    "Re-file after the Delivered mark, or re-trigger the status")
        elif delivered_at:
            why += "; filed after the Delivered mark yet still Waiting: does not fit the OQ-3 pattern, check by hand"
        elif stage != "delivered":
            why += "; load not yet Delivered, so by the OQ-3 pattern the status clears only on an upload made after it"
        return _row(load_id, load, "filed_status_pending", stage, why, files)

    if rank < STAGE_ORDER["loaded"]:
        return _row(load_id, load, "not_yet_due", stage, f"truck is {stage}; no paperwork can exist yet", files)

    state = "pod_expected" if expects_pod else "bol_expected"
    want = "POD" if expects_pod else "BOL"
    if ledger_docs:
        unread = f", {ledger_unread} not read yet" if ledger_unread else ""
        why = f"{want} expected; {ledger_docs} document(s) in the ratecon thread{unread} and nothing filed"
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
