"""Stage 2 - Match (no model). Implements PRD Section 6 signals and tiers against the load index."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher

from .index import LoadIndex, edit_distance_le1, norm_code, norm_name, norm_phone
from .schema import Extraction

STRONG, MEDIUM, WEAK = 3.0, 2.0, 1.0


@dataclass
class Signal:
    name: str
    strength: str           # "strong" | "medium" | "weak"
    weight: float
    detail: str


@dataclass
class Candidate:
    load_id: int
    score: float = 0.0
    signals: list[Signal] = field(default_factory=list)

    @property
    def strong(self) -> int:
        return sum(1 for s in self.signals if s.strength == "strong")

    @property
    def medium(self) -> int:
        return sum(1 for s in self.signals if s.strength == "medium")

    def add(self, name: str, strength: str, detail: str) -> None:
        weight = {"strong": STRONG, "medium": MEDIUM, "weak": WEAK}[strength]
        self.signals.append(Signal(name, strength, weight, detail))
        self.score += weight


@dataclass
class MatchResult:
    tier: str                        # "High" | "Medium" | "Low"
    load_id: int | None
    candidates: list[Candidate]
    reason: str


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip().split(" ")[0].split("T")[0]          # drop any time of day the reader kept
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%m-%d-%y", "%m-%d-%Y", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _name_match(paper: str | None, load_party: dict | None) -> tuple[bool, bool]:
    """Returns (name_matches, _). Fuzzy on normalized names: containment or ratio >= 0.8."""
    if not load_party:
        return False, False
    a, b = norm_name(paper), norm_name(load_party.get("name"))
    name_ok = bool(a and b) and (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.8)
    return name_ok, False


def score_candidates(ex: Extraction, index: LoadIndex, sender_phone: str | None = None, sender_email: str | None = None,
                     text_hints: list[str] | None = None) -> list[Candidate]:
    """Score every indexed load against the extraction.

    text_hints carries text that arrived *with* the document rather than on it: the file name, and for email the
    subject and body. A load number there is strong evidence (PRD Section 11, "load signal").
    """
    paper_numbers = [(n, norm_code(n.value)) for n in ex.numbers if norm_code(n.value)]
    paper_phone = norm_phone(ex.driver_phone) or norm_phone(sender_phone)
    ship_dt, del_dt = _parse_date(ex.ship_date), _parse_date(ex.delivery_date) or _parse_date(ex.signatures.receiver_date)
    hint_numbers = set()
    for h in text_hints or []:
        hint_numbers.update(re.findall(r"(?<!\d)(\d{6,8})(?!\d)", h))   # Circle load IDs are 7 digits today
    out: list[Candidate] = []

    for ld in index.loads:
        c = Candidate(load_id=ld["load_id"])
        lid = str(ld["load_id"])

        # Load number in the file name, subject, or body - strong
        if lid in hint_numbers:
            c.add("load_number_in_filename_or_subject", "strong", f"{lid} appears in the file name or message text")

        # Load number printed on the paper (any label) - strong
        for n, v in paper_numbers:
            if v == lid and not n.handwritten:
                c.add("load_number_on_paper", "strong", f"'{n.label}' = {n.value}")
                break

        # Reference fields - strong (exact), strong-but-flagged (one edit on long numbers)
        for n, v in paper_numbers:
            if v == lid:
                continue
            if v in ld["_ref_values"]:
                c.add("reference_exact", "strong", f"'{n.label}' {n.value} equals a load reference field")
            elif len(v) >= 8 and v.isdigit() and any(edit_distance_le1(v, r) for r in ld["_ref_values"] if r.isdigit()):
                c.add("reference_one_edit", "strong", f"'{n.label}' {n.value} is one digit from a load reference field (possible OCR error)")
            elif v in ld["_note_values"]:
                c.add("stop_note_number", "medium", f"'{n.label}' {n.value} appears in stop notes")

        # Identity: driver phone on paper or from the sender - strong
        if paper_phone and paper_phone == ld["_driver_phone"]:
            c.add("driver_phone", "strong", f"phone ...{paper_phone[-4:]} equals dispatch driver contact")
        if sender_email and ld.get("dispatcher_email") and sender_email.lower().split("@")[-1] == ld["dispatcher_email"].lower().split("@")[-1]:
            c.add("sender_domain", "medium", f"sender domain matches carrier dispatcher {ld['dispatcher_email']}")

        # Carrier - medium
        carrier = ld.get("carrier") or {}
        if ex.carrier_dot and norm_code(ex.carrier_dot) == norm_code(carrier.get("dot")):
            c.add("carrier_dot", "medium", f"DOT {ex.carrier_dot}")
        elif ex.carrier_mc and norm_code(ex.carrier_mc) == norm_code(carrier.get("mc")):
            c.add("carrier_mc", "medium", f"MC {ex.carrier_mc}")
        elif ex.carrier_name and _name_match(ex.carrier_name, {"name": carrier.get("name")})[0]:
            c.add("carrier_name", "weak", f"carrier name '{ex.carrier_name}'")

        # Parties - medium each (name), city adds a weak corroboration
        for role, paper_party, load_party in (("shipper", ex.shipper, ld.get("shipper")), ("consignee", ex.consignee, ld.get("consignee"))):
            name_ok, _ = _name_match(paper_party.name, load_party)
            city_ok = bool(paper_party.city and load_party and norm_name(paper_party.city) == norm_name(load_party.get("city")))
            if name_ok:
                c.add(f"{role}_name", "medium", f"'{paper_party.name}' ~ '{load_party.get('name')}'")
            if city_ok:
                c.add(f"{role}_city", "weak", f"{paper_party.city}")

        # Customer (bill-to) - medium. On subcontracted freight the paper's shipper is often Circle's customer.
        customer = ld.get("customer")
        if customer and (_name_match(ex.shipper.name, {"name": customer})[0] or _name_match(ex.consignee.name, {"name": customer})[0]):
            c.add("customer_name", "medium", f"'{ex.shipper.name}' ~ customer '{customer}'")

        # Equipment - weak. Any number on the page (drivers often handwrite tractor/trailer at the foot) that equals the dispatch equipment.
        equipment = {norm_code(ld.get("trailer")): "trailer", norm_code(ld.get("tractor")): "tractor"}
        equipment.pop("", None)
        seen_equipment = set()
        for n, v in paper_numbers:
            if v in equipment and v not in seen_equipment and len(v) >= 2:
                seen_equipment.add(v)
                c.add("equipment", "weak", f"{equipment[v]} {n.value}" + (" (handwritten)" if n.handwritten else ""))

        # Dates - weak, and only as corroboration of a candidate that already has another signal
        if c.signals:
            for label, paper_dt, load_key in (("ship", ship_dt, "pickup_date"), ("delivery", del_dt, "delivery_date")):
                load_dt = _parse_date(ld.get(load_key))
                if paper_dt and load_dt and abs((paper_dt - load_dt).days) <= 2:
                    c.add(f"{label}_date", "weak", f"{paper_dt} within 2 days of {load_dt}")
            out.append(c)

    out.sort(key=lambda c: c.score, reverse=True)
    return out


def decide(candidates: list[Candidate]) -> MatchResult:
    """PRD Section 6 tiers."""
    if not candidates:
        return MatchResult("Low", None, [], "No signal matched any active load.")
    best = candidates[0]
    runner = candidates[1] if len(candidates) > 1 else None
    unique = runner is None or (runner.score <= best.score * 0.6 and runner.strong == 0)

    if unique and (best.strong >= 2 or (best.strong >= 1 and best.medium >= 1)):
        return MatchResult("High", best.load_id, candidates, f"{best.strong} strong + {best.medium} medium signals agree; no competing candidate.")
    if best.strong >= 1 or best.medium >= 2:
        why = "competing candidate also has strong evidence" if not unique else "only one strong signal, or medium signals only"
        return MatchResult("Medium", best.load_id, candidates, f"Best candidate {best.load_id}: {why}.")
    return MatchResult("Low", None, candidates, "No load or reference number matched; weak corroboration only. Held as unmatched.")


def classify_type(ex: Extraction, load: dict | None) -> tuple[str, str]:
    """PRD Section 7: trip stage first, then what is on the page. Returns (TransportPro type name, reason)."""
    if ex.document_type in ("other", "unknown"):
        return "None (not a freight document)", f"reader classified {ex.document_type}: do not file" + (f"; {ex.notes[:100]}" if getattr(ex, "notes", None) else "")
    if ex.document_type in ("lumper", "weight_ticket", "reefer_log", "carrier_invoice", "rate_confirmation", "photo", "shipping_document"):
        names = {"lumper": "Lumper", "weight_ticket": "Weight Ticket", "reefer_log": "Reefer Log",
                 "carrier_invoice": "Carrier Invoice", "rate_confirmation": "Rate Confirmation", "photo": "Photo",
                 "shipping_document": "Shipping Documents"}   # TransportPro type 369: packing lists, CofAs, customs paperwork
        return names[ex.document_type], f"reader classified {ex.document_type}"
    # In/out times are evidence of delivery only when they were recorded at the delivery stop.
    # Before Times.at_stop existed the reader could not say which stop they came from, and a
    # departure written at the SHIPPER read exactly like one written at the consignee: measured
    # 17 Sep 2026 over the 239 documents in the ledger, 10 of 84 bills of lading carried a check-out
    # time and no receiver signature, one of them a file named "Circle BOL NC-KY.pdf" with a
    # handwritten 2:10 pm that is almost certainly a pickup departure. Only an explicit "shipper"
    # withdraws the evidence - "unknown" is what every extraction read before this field existed
    # reports, and it has to keep meaning exactly what it meant then.
    delivery_times = bool(ex.times.check_out) and ex.times.at_stop != "shipper"
    signed = ex.signatures.receiver_signed or delivery_times
    if ex.signatures.receiver_signed:
        evidence = "receiver signature"
    elif delivery_times:
        evidence = ("in/out times recorded at the consignee" if ex.times.at_stop == "consignee"
                    else "in/out times, though the page does not say which stop")
    else:
        evidence = ""
    # Worth saying out loud wherever it changes the answer: it is the one case where the page holds
    # a time and the service is deliberately not counting it.
    pickup_times = ("; the in/out times on the page were recorded at the shipper"
                    if ex.times.check_out and ex.times.at_stop == "shipper" else "")
    stage = (load or {}).get("dispatch_status", "")
    if stage in ("Planned", "Dispatched", "At Shipper", "Loaded", "In Transit"):
        # The truck has not reached the consignee: a signed form here is the shipper's or the CFS's release, signed by the
        # driver (load 2576409: a Menzies "CFS DELIVERY" receipt read as a POD). If the dispatch status is simply stale,
        # the rep advances it and the next pass re-types the file.
        why = (f"{evidence} before the truck reached the consignee (dispatch {stage}): pickup paperwork, not a POD"
               if signed else f"dispatch {stage}; pickup copy{pickup_times}")
        if ex.document_type == "proof_of_delivery":
            why += "; reader called it a POD, overruled by the trip stage"
        return "Bill Of Lading", why
    if signed and stage in ("Delivered", "At Consignee", ""):
        return "Proof of Delivery", f"{evidence} present" + (f"; dispatch {stage}" if stage else "")
    if signed:
        return "Proof of Delivery", f"{evidence} although dispatch shows {stage}; reviewer should confirm"
    if stage in ("Delivered", "At Consignee"):
        return "Bill Of Lading", (f"no receiver signature although dispatch is {stage}; "
                                  f"likely the pickup copy{pickup_times}")
    return "Bill Of Lading", f"no receiver signature; pickup copy{pickup_times}"


COMMENT_MAX = 480          # defensive; TransportPro does not document a limit for file comments


def signals_detail(signals: list[Signal]) -> str:
    """The strongest evidence from the matcher, for the comment. Strong signals if there are any,
    otherwise whatever there is."""
    return ("; ".join(s.detail for s in signals if s.strength == "strong")[:120]
            or "; ".join(s.detail for s in signals)[:120])


def page_summary(ex: Extraction) -> str:
    """What is actually on the paper, in the few words a reviewer wants in the File History column.

    This is the half of the comment that was missing. "Bill Of Lading for load - 2572445" says what
    the service FILED it as; it never said what the service READ. Those differ in exactly the case
    that matters - a page filed as a Driver Supplied BOL that is a signed POD - and the comment is
    the only place that survives, because TransportPro renames every upload to <fileId>_<typeId>.
    """
    bits = [ex.document_type.replace("_", " ")]
    sig = ex.signatures
    if sig.receiver_signed:
        who, when_ = (sig.receiver_name or "").strip(), (sig.receiver_date or "").strip()
        bits.append("receiver signed" + (f" by {who}" if who else "") + (f" {when_}" if when_ else ""))
    elif sig.stamp_present:
        bits.append("receiving stamp, no signature")
    else:
        bits.append("no receiver signature")
    if ex.times.check_out:
        at = ex.times.at_stop
        bits.append(f"out {ex.times.check_out}" + (f" at the {at}" if at != "unknown" else ", stop unstated"))
    if len(ex.pages) > 1:
        bits.append(f"{len(ex.pages)} pages")
    return ", ".join(bits)


def filing_comment(doc_type: str, load_id: int, channel: str, when: datetime, decision: str,
                   match: str = "", page: str = "", was: str = "") -> str:
    """PRD Section 8 comment format, with what the page says and where it came from.

    `match` is why we believe the document belongs to this load, `page` is what the reader saw on
    it, and `was` is the type it was previously filed under when this is a re-file. All three are
    optional so a caller with less to say produces a shorter comment rather than empty fields - the
    service used to pass no evidence at all and every comment ended in a bare "match:".
    """
    parts = [f"{doc_type} for load - {load_id}",
             f"via {channel} {when.strftime('%m/%d/%Y %H:%M')} ET"]
    if was:
        parts.append(f"re-filed from {was}")
    if page:
        parts.append(f"page: {page}")
    if match:
        parts.append(f"match: {match}")
    parts.append(decision)
    out = " | ".join(parts)
    # TransportPro's own comments run to about 50 characters and its limit is not documented, so a
    # 300-character one could in principle be rejected and fail the upload. The fields are ordered
    # most-important-first precisely so that clipping the tail costs the least, and the cap is
    # defensive rather than measured - raise it once the real limit is known.
    return out if len(out) <= COMMENT_MAX else out[:COMMENT_MAX - 1] + "…"
