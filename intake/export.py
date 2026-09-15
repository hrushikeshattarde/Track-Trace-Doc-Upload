"""Export the review queue as a sheet a person can work from.

The queue in the terminal is fine for a handful of items; 150 of them want a spreadsheet, sorted so
the strongest candidates are at the top and the reason each one is there is written out in full.

The useful column is `corroboration`. The reader's verdict is about the PAPER - what type of
document it is and who signed it. That is a separate question from whether the paper belongs to
THIS load, which is answered by comparing the numbers transcribed off the page against what
TransportPro holds: reference fields, stop cities, and the dispatch's trailer and tractor. Two or
more independent matches is strong evidence; one is suggestive; none means the document routed on
the subject line alone and nothing on the page confirms it.

`ready` is deliberately conservative and says what kind of confidence it is:

  ready                every gate passed, corroborated on 2+ independent facts, signed
  check signature      corroborated, but the receiver signature is the only thing holding the POD
                       classification up - the judgement a person should make
  check match          gates passed but nothing on the page ties it to this load
  hold (delivered)     would be a POD before the Delivered mark; waits, does not file
  blocked              PII or not a freight document; never files

Nothing here is "verified" in the sense of a person having looked at the image. That column is
`eyeballed_by`, and it is empty until someone fills it in.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from . import db, review, state as st

COLUMNS = [
    "review_id", "load_id", "customer", "pod_terminal", "stage", "load_status", "doc_status",
    "currently_filed", "missing_document", "fills_gap", "proposed_type", "document_file", "file_size_kb",
    "ready", "corroboration", "match_count", "matched_on", "receiver_signed", "receiver_name",
    "receiver_date", "delivered_mark_utc", "oq3_ok", "customer_rule", "routing", "reasoning",
    "eyeballed_by", "decision", "sha256",
]

_norm = lambda v: re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def _tpro_index(load: dict, dispatches: list[dict]) -> dict[str, str]:
    """Every value on the TransportPro load a number on the paper could legitimately equal."""
    out: dict[str, str] = {}
    for key, value in (load.get("reference") or {}).items():
        if value and not isinstance(value, dict):
            out.setdefault(_norm(value), key)
    for w in load.get("waypoints") or []:
        for ref in w.get("reference") or []:
            if ref.get("value"):
                out.setdefault(_norm(ref["value"]), f"stop {ref.get('type')}")
        loc = w.get("location") or {}
        if loc.get("city"):
            out.setdefault(_norm(loc["city"]), f"{w.get('type')} city")
    assigned = (dispatches[0].get("assignedTo") or {}) if dispatches else {}
    for key in ("trailerNumber", "tractorNumber"):
        if assigned.get(key):
            out.setdefault(_norm(assigned[key]), key)
    return out


def _corroborate(extraction: dict, index: dict[str, str]) -> tuple[int, list[str]]:
    hits: list[str] = []
    seen: set[str] = set()
    for n in extraction.get("numbers") or []:
        v = _norm(n.get("value"))
        if v and v in index and index[v] not in seen:
            seen.add(index[v])
            hits.append(f"{n.get('label') or '?'}={n.get('value')} = TPro {index[v]}")
    for role, key in (("shipper", "SH city"), ("consignee", "CN city")):
        city = ((extraction.get(role) or {}).get("city"))
        if city and _norm(city) in index and index[_norm(city)] not in seen:
            seen.add(index[_norm(city)])
            hits.append(f"{role} city {city} = TPro {index[_norm(city)]}")
    return len(hits), hits


def build_rows(conn, tpro, *, limit: int = 500, verbose: bool = False) -> list[dict]:
    items = conn.execute(
        "SELECT r.*, a.extraction_json, a.filename, a.bytes, a.document_type, "
        "       l.customer, l.stage, l.state AS load_state, l.doc_status, l.terminal, l.filed_types "
        "FROM review r LEFT JOIN attachment a USING (sha256) LEFT JOIN load l USING (load_id) "
        "WHERE r.state='pending' ORDER BY r.load_id LIMIT ?", (limit,)).fetchall()

    cache: dict[int, tuple[dict, list[dict], list[dict]]] = {}
    rows: list[dict] = []
    for it in items:
        load_id = it["load_id"]
        ex = json.loads(it["extraction_json"]) if it["extraction_json"] else {}
        sig = ex.get("signatures") or {}

        if load_id not in cache:
            try:
                cache[load_id] = (tpro.load(load_id), st.active_first(tpro.dispatches(load_id)),
                                  tpro.files(load_id))
            except Exception as e:  # noqa: BLE001 - a load we cannot read still belongs on the sheet
                cache[load_id] = ({}, [], [])
                if verbose:
                    print(f"  ! load {load_id}: {e}")
        load, dispatches, files = cache[load_id]

        n_match, hits = _corroborate(ex, _tpro_index(load, dispatches))
        disp = dispatches[0] if dispatches else {}
        delivered_at = (disp.get("lastUpdated")
                        if (disp.get("status") or "").lower() == "delivered" else None)
        bol, pod = st.split_files(files)
        stage = it["stage"] or ""
        missing = {"pod_expected": "POD", "bol_expected": "BOL",
                   "filed_status_pending": "none (filed, status stuck)",
                   "complete": "none", "not_yet_due": "not due yet",
                   "out_of_scope": "out of scope"}.get(it["load_state"] or "", it["load_state"] or "")

        fills, gap_note = _gap(it["load_state"], it["proposed_type"], it["filed_types"],
                               stage, delivered_at)
        dup = _looks_already_filed(it, bol + pod, conn)
        if dup:
            gap_note += "; " + dup
        ready, why = _verdict(it, ex, sig, n_match, stage, delivered_at, bol, pod, fills, gap_note)
        rows.append({
            "review_id": it["id"], "load_id": load_id, "customer": it["customer"] or "",
            "pod_terminal": it["terminal"] or "", "stage": stage,
            "load_status": (load.get("status") or {}).get("loadStatus") or "",
            "doc_status": it["doc_status"] or "", "currently_filed": it["filed_types"] or "nothing",
            "missing_document": missing, "fills_gap": fills, "proposed_type": it["proposed_type"] or "",
            "document_file": it["filename"] or "", "file_size_kb": (it["bytes"] or 0) // 1024,
            "ready": ready, "corroboration": _strength(n_match), "match_count": n_match,
            "matched_on": " | ".join(hits[:5]),
            "receiver_signed": sig.get("receiver_signed"), "receiver_name": sig.get("receiver_name") or "",
            "receiver_date": sig.get("receiver_date") or "", "delivered_mark_utc": delivered_at or "",
            "oq3_ok": "yes" if (it["proposed_type"] != "Proof of Delivery" or delivered_at) else "no - not Delivered yet",
            "customer_rule": _rule_summary(it["customer"], it["terminal"]),
            "routing": it["kind"], "reasoning": why, "eyeballed_by": "", "decision": "",
            "sha256": it["sha256"] or "",
        })
    order = {"READY - fills the gap": 0, "check signature": 1, "check match": 2,
             "hold (delivered mark)": 3, "re-file to clear status": 4, "not needed": 5, "blocked": 6}
    # Gap first, then confidence, then how much the page corroborates. A perfectly corroborated
    # document on a load that already has that type filed is not work; it belongs below the fold.
    rows.sort(key=lambda r: (order.get(r["ready"], 9), -r["match_count"]))
    return rows


def _gap(load_state: str | None, proposed: str | None, filed_types: str | None,
         stage: str = "", delivered_at: str | None = None) -> tuple[str, str]:
    """Does this document supply what the load is actually short of? Returns (fills_gap, note)."""
    needed = {"pod_expected": "Proof of Delivery", "bol_expected": "Bill Of Lading"}.get(load_state or "")
    filed = (filed_types or "").lower()
    if load_state == "not_in_view":
        return "no", ("the load is not on the Load Management view - it is outside the dashboard "
                      "filter, so it is not Track & Trace work")
    if load_state == "out_of_scope":
        return "no", "load is not in the worked service level"
    if load_state == "complete":
        return "no", "load already shows Documents Received"
    if load_state == "not_yet_due":
        return "no", "truck has not loaded yet"
    if load_state == "filed_status_pending":
        if not delivered_at:
            return "no", (f"already filed and the truck is {stage or 'still in transit'}; "
                          "Waiting for Documents is expected until it delivers, so nothing to do yet")
        return "re-file", ("filed, Delivered, and the status is still Waiting; re-filing after the "
                           "Delivered mark is the OQ-3 fix")
    if needed and proposed == needed:
        return "yes", f"the load is short a {needed} and this is one"
    if needed:
        return "no", f"the load needs a {needed}, this is a {proposed}"
    return "no", "the load is not short a document"


def _looks_already_filed(it, filed: list[dict], conn) -> str:
    """Did someone file this very document by hand shortly after it arrived?

    A rep who files an emailed BOL leaves a File History entry minutes after the message. When that
    is what happened, the queued copy is a duplicate of what is already on the load, not a second
    document - worth saying plainly so nobody files it twice.
    """
    row = conn.execute(
        "SELECT MIN(m.internal_date) AS t FROM part p JOIN message m ON m.message_id = p.message_id "
        "WHERE p.sha256 = ? AND m.load_id = ?", (it["sha256"], it["load_id"])).fetchone()
    emailed = st.utc(row["t"] if row else None)
    if not emailed:
        return ""
    for f in filed:
        when = st.utc(f.get("dateCreated"))
        if when and 0 <= (when - emailed).total_seconds() <= 7200:
            mins = round((when - emailed).total_seconds() / 60)
            return (f"a {f.get('fileTypeName')} was filed {mins} min after this email arrived, so this "
                    "is very likely the same document already on the load")
    return ""


def _strength(n: int) -> str:
    return "strong (2+ facts)" if n >= 2 else ("weak (1 fact)" if n == 1 else "none on the page")


def _verdict(it, ex, sig, n_match, stage, delivered_at, bol, pod, fills, gap_note) -> tuple[str, str]:
    kind, proposed = it["kind"], it["proposed_type"]
    if kind == review.PII:
        return "blocked", "Personal identity document. Never file. " + (it["reason"] or "")
    if kind == review.NOT_A_DOCUMENT:
        return "blocked", "Not freight paperwork. " + (it["reason"] or "")
    if kind == review.POD_TOO_EARLY:
        return "hold (delivered mark)", (
            f"Reads as a POD but the load is {stage or 'not delivered'}. Filing before the Delivered mark "
            "leaves documentStatus stuck at Waiting for Documents, so it waits.")
    if kind == review.RULES_FAILED:
        return "check match", "Customer requirements not satisfied: " + (it["reason"] or "")
    if kind in (review.LOW_CONFIDENCE, review.CONFLICT):
        return "check match", it["reason"] or ""

    bits = []
    if n_match >= 2:
        bits.append(f"{n_match} independent facts on the page match this load in TransportPro")
    elif n_match == 1:
        bits.append("only one fact on the page matches this load")
    else:
        bits.append("nothing transcribed off the page matches this load; it routed on the subject line alone")
    if proposed == "Proof of Delivery":
        if sig.get("receiver_signed"):
            who = sig.get("receiver_name") or "unnamed"
            bits.append(f"receiver signature present ({who}, {sig.get('receiver_date') or 'no date'})")
        else:
            bits.append("no receiver signature found, which a POD needs")
        bits.append(f"load is Delivered (marked {delivered_at})" if delivered_at
                    else "load is NOT marked Delivered")
    if not (bol + pod):
        bits.append("nothing of this type is filed, so it fills a real gap")
    else:
        bits.append(f"already filed: {', '.join(sorted({f.get('fileTypeName') or '?' for f in bol + pod}))}")

    bits.append(gap_note)
    reason = "; ".join(bits) + "."

    if fills == "no":
        return "not needed", reason
    if fills == "re-file":
        return "re-file to clear status", reason
    if n_match < 2:
        return "check match", reason
    if proposed == "Proof of Delivery" and not delivered_at:
        return "hold (delivered mark)", reason
    if proposed == "Proof of Delivery" and not sig.get("receiver_signed"):
        return "check signature", reason + (
            " A POD is defined by the receiver's signature and none was found.")
    if proposed == "Proof of Delivery":
        return "check signature", reason + (
            " A POD is defined by the receiver's signature, so confirm it sits on a receiving line "
            "and is not the shipper's.")
    return "READY - fills the gap", reason


def _rule_summary(customer: str | None, terminal: int | None) -> str:
    path = Path(__file__).resolve().parent.parent / "index" / "customer_requirements.json"
    if not customer or not path.exists():
        return ""
    try:
        from pod_intake.requirements import Requirements
        rule = Requirements.from_file(path).for_customer(customer, terminal)
    except Exception:  # noqa: BLE001
        return ""
    if not rule:
        return "no rule on file"
    bits = [k for k in ("pod_required", "bol_before_leaving_shipper", "pod_before_deliver_out",
                        "upload_required_before_deliver_out", "seal_required_on_bol",
                        "in_out_times_required", "freight_photos_required") if rule.get(k)]
    if rule.get("pod_signatures"):
        bits.append("POD signatures: " + ", ".join(rule["pod_signatures"]))
    if rule.get("pages_required"):
        bits.append(f"pages: {rule['pages_required']}")
    out = "; ".join(bits) or "no gating rule"
    return out + (" [rule borrowed from another pod's sheet]" if rule.get("cross_pod") else "")


def write_csv(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:   # BOM so Excel opens UTF-8 correctly
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path
