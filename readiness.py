r"""Document Readiness job: join TransportPro loads, File History, ratecon@ email, and the customers'
verification steps into one row per load.

For every load it answers: what stage is the truck at, which documents does this customer require at this
stage (from the Accounts/Customers Extra Requirements workbook), what is already filed in File History, what
is sitting in the ratecon@ thread unfiled, and what a reviewer still has to verify on the paper itself.

Sources
  TransportPro   load detail, dispatch status, File History  (the paystatus bot's TransportPro client and PAYBOT_TP_* settings)
  Gmail          to:ratecon@ subject:<load> has:attachment    (the paystatus bot's service-account delegation; headers + attachment names only)
  Workbook       index/customer_requirements.json             (built by run.py --build-rules, includes the pod -> terminal map)

Modes
  live:     python readiness.py --loads 2573804,2577037 --days 14
            python readiness.py --from-survey out\mail-survey\ratecon_20260914.json   (loads = every load that received a document by email)
  offline:  python readiness.py --from-survey ... --tpro-fixture out\readiness\tpro_fixture_20260914.json --emails-from-survey
            (no credentials touched; TransportPro answers come from the fixture, emails from the survey JSON)
  optional: --read  downloads the unfiled email attachments and runs the reader + requirements check on them
            (needs the model credentials the prototype's run.py uses).

Run with the payment-bot virtualenv so google-auth and the TransportPro client import:
  C:\Users\hrushikesh.attarde_c\Desktop\payment-bot-intake-policies-and-hardening\.venv\Scripts\python.exe readiness.py ...

Read-only against TransportPro and Gmail. Nothing is uploaded, no status is changed.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import base64
import collections
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYBOT_DIR = Path(r"C:\Users\hrushikesh.attarde_c\Desktop\payment-bot-intake-policies-and-hardening")
sys.path.insert(0, str(PAYBOT_DIR / "src"))
sys.path.insert(0, str(HERE))

from pod_intake.requirements import Requirements  # noqa: E402
from pod_intake.localenv import load_local_env, missing, TPRO_KEYS, GMAIL_KEYS  # noqa: E402

GROUP = "ratecon@circledelivers.com"
BOL_TYPES = {12: "Bill Of Lading", 363: "Driver Supplied BOL"}
POD_TYPES = {360: "Proof of Delivery", 53: "Delivery Receipt"}
RATECON_TYPES = {23, 143, 358, 367}
LOAD_RE = re.compile(r"(?<!\d)(2[4-6]\d{5})(?!\d)")
STAGE_ORDER = {"planned": 0, "dispatched": 1, "at shipper": 2, "loaded": 3, "in transit": 3, "at consignee": 4, "delivered": 5}


def utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def active_first(dispatches: list[dict]) -> list[dict]:
    """Cancelled dispatches last, then newest first, so dispatches[0] is the truck that is actually moving the load."""
    return sorted(dispatches or [], key=lambda d: ((d.get("status") or "").lower() == "canceled", -(int(d.get("id") or 0))))


def stage_of(dispatch_status: str | None, load_status: str | None) -> str:
    s = (dispatch_status or "").lower()
    for key in ("delivered", "at consignee", "tr drop at consignee", "loaded", "tr drop in transit", "in transit", "at shipper", "dispatched", "planned"):
        if key in s:
            return {"tr drop at consignee": "at consignee", "tr drop in transit": "loaded", "in transit": "loaded"}.get(key, key)
    ls = (load_status or "").lower()
    return "delivered" if "deliver" in ls else ("dispatched" if "dispatch" in ls else "planned")


# ------------------------------------------------------------------ sources ----

class TProSource:
    """Live TransportPro reads through the paystatus bot's client (PAYBOT_TP_* settings)."""

    def __init__(self) -> None:
        load_local_env()                                  # this project's .env if present, else the payment-bot checkout's
        need = missing(TPRO_KEYS)
        if need:
            raise RuntimeError(f"TransportPro settings missing: {', '.join(need)}. Copy .env.example to .env in the project folder and fill it.")
        from payment_bot.clients.transport_pro_http import build_transport_pro_client
        # Environment variables win over the dotenv file in pydantic-settings, so the values loaded above are used;
        # chdir only lets any other PAYBOT_* setting the payment-bot Settings class needs come from its own .env.
        cwd = os.getcwd()
        os.chdir(PAYBOT_DIR if (PAYBOT_DIR / ".env").exists() else HERE)
        try:
            self.client = build_transport_pro_client()
        finally:
            os.chdir(cwd)

    def load(self, load_id: int) -> dict:
        """Public API path is the singular /load/{id} (Postman: 'Load Detail'). Fall back to the Voice AI detail."""
        from payment_bot.errors import ClientError
        try:
            return self.client._get(f"/load/{load_id}")
        except ClientError as e:
            if getattr(e, "status", None) not in (400, 404):
                raise
        return self.client._get(f"/voiceai/load/{load_id}")

    def dispatches(self, load_id: int) -> list[dict]:
        payload = self.client._get("/dispatch/search", {"loadId": str(load_id)})
        return self.client._results(payload, path="/dispatch/search")

    def files(self, load_id: int) -> list[dict]:
        payload = self.client._get("/files/search", {"recordType": "loads", "recordId": str(load_id)})
        return self.client._results(payload, path="/files/search")

    def text_messages(self, dispatch_id: int) -> list[dict]:
        """SMS thread for a dispatch (Postman: GET /dispatch/{id}/getTextMessages). Read-only."""
        try:
            payload = self.client._get(f"/dispatch/{dispatch_id}/getTextMessages")
        except Exception:  # noqa: BLE001 - optional evidence, never fatal
            return []
        return self.client._results(payload, path="/dispatch/getTextMessages")

    def search_loads(self, params: dict) -> tuple[list[dict], dict]:
        """GET /load/search (Postman 'Load Search'): terminalId, loadStatus, pickupDateStart/End, deliveryDateStart/End...
        A date range is required (terminalId alone is HTTP 400); documentStatus is filtered client-side because the
        server ignores every spelling tried on 14 Sep 2026."""
        payload = self.client._get("/load/search", params)
        pagination = (payload.get("pagination") or {}) if isinstance(payload, dict) else {}
        return self.client._results(payload, path="/load/search"), pagination

    def search_all_pages(self, params: dict, max_pages: int = 20) -> list[dict]:
        """Every page of a search. Probes currentPage / page / offset like missing_documents_set and stops when a page
        repeats page 0 (parameter ignored) or runs out."""
        first, pg = self.search_loads(params)
        total_pages = int(pg.get("totalPages") or 1)
        per_page = int(pg.get("perPage") or 200)
        out = list(first)
        if total_pages <= 1 or not first:
            return out
        first_id = first[0].get("id")
        styles = [lambda p: {"page": str(p)}, lambda p: {"currentPage": str(p)}, lambda p: {"offset": str(p * per_page)}]   # "page" is the one TransportPro honours (probed 14 Sep 2026)
        style = None
        for st in styles:
            rows, _ = self.search_loads({**params, **st(1)})
            if rows and rows[0].get("id") != first_id:
                style = st
                out += rows
                break
        if style is None:
            print(f"  warning: /load/search ignored every page parameter; only page 0 of {total_pages} read for {params}")
            return out
        for p in range(2, min(total_pages, max_pages)):
            rows, _ = self.search_loads({**params, **style(p)})
            if not rows:
                break
            out += rows
        return out

    def missing_documents(self, params: dict | None = None) -> tuple[list[dict], dict]:
        """Loads waiting for documents (Postman: GET /load/missing_documents). Returns (results, pagination).
        Page 0 is the OLDEST 200 of ~8,000 open loads (ids ascending); the collection documents no page parameter,
        so main() probes currentPage / page / offset and says which one the API honoured."""
        payload = self.client._get("/load/missing_documents", params or None)
        pagination = (payload.get("pagination") or {}) if isinstance(payload, dict) else {}
        return self.client._results(payload, path="/load/missing_documents"), pagination


class TProFixture:
    """Offline stand-in: {"loads": {"2573804": {"load": {...}, "dispatches": [...], "files": [...]}}}."""

    def __init__(self, path: Path) -> None:
        self.data = json.loads(path.read_text(encoding="utf-8"))["loads"]

    def load(self, load_id: int) -> dict:
        return self.data[str(load_id)]["load"]

    def dispatches(self, load_id: int) -> list[dict]:
        return self.data[str(load_id)].get("dispatches", [])

    def files(self, load_id: int) -> list[dict]:
        return self.data[str(load_id)].get("files", [])

    def text_messages(self, dispatch_id: int) -> list[dict]:
        for v in self.data.values():
            for d in v.get("dispatches", []):
                if d.get("id") == dispatch_id:
                    return v.get("text_messages", [])
        return []

    def missing_documents(self, params: dict | None = None) -> tuple[list[dict], dict]:
        return [{"id": int(k), "missingDocuments": ["Bill Of Lading"]} for k in self.data], {"totalPages": 1}

    def search_all_pages(self, params: dict, max_pages: int = 20) -> list[dict]:
        tid = int(params.get("terminalId") or 0)
        return [dict(v["load"], id=int(k)) for k, v in self.data.items() if not tid or v["load"].get("assignedTerminal") == tid]


class GmailSource:
    """Live Gmail search per load, headers and attachment names only (mail_survey helpers)."""

    def __init__(self, days: int) -> None:
        import mail_survey as ms
        from payment_bot.clients.google_auth import GMAIL_READONLY_SCOPES, ServiceAccountTokenSource, load_service_account_info
        load_local_env()
        need = missing(GMAIL_KEYS)
        if need:
            raise RuntimeError(f"Gmail settings missing: {', '.join(need)}. See .env.example.")
        self.user = os.environ["PAYBOT_GMAIL_USER"]
        info = load_service_account_info(file_path=os.environ["PAYBOT_GOOGLE_SA_FILE"])
        self.tokens = ServiceAccountTokenSource(info, subject=self.user, scopes=GMAIL_READONLY_SCOPES)
        self.ms = ms
        self.days = days

    def messages_for(self, load_id: int) -> list[dict]:
        q = f"to:{GROUP} subject:{load_id} has:attachment newer_than:{self.days}d"
        listing = self.ms.gget(self.tokens.token(), f"/users/{urllib.parse.quote(self.user)}/messages", {"q": q, "maxResults": "50"})
        out = []
        for item in listing.get("messages") or []:
            m = self.ms.gget(self.tokens.token(), f"/users/{urllib.parse.quote(self.user)}/messages/{item['id']}", {"format": "full"})
            out.append(self.ms.summarize_message(m, GROUP))
        return out

    def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        data = self.ms.gget(self.tokens.token(), f"/users/{urllib.parse.quote(self.user)}/messages/{message_id}/attachments/{attachment_id}")
        return base64.urlsafe_b64decode(data["data"] + "==")


class SurveyEmails:
    """Offline stand-in: document-bearing messages per load from a mail_survey JSON."""

    def __init__(self, path: Path) -> None:
        recs = json.loads(path.read_text(encoding="utf-8"))["records"]
        self.by_load: dict[str, list[dict]] = collections.defaultdict(list)
        for r in recs:
            for n in r.get("subject_load_numbers") or []:
                self.by_load[n].append(r)

    def messages_for(self, load_id: int) -> list[dict]:
        return self.by_load.get(str(load_id), [])


def document_messages(msgs: list[dict]) -> list[dict]:
    """Keep messages carrying something other than a TransportPro-generated rate confirmation."""
    out = []
    for r in msgs:
        atts = [a for a in r.get("attachments", []) if not a.get("likely_signature")]
        docs = [a for a in atts if a.get("kind") not in ("rate_confirmation",) and not re.match(r"^\d{7,9}_(23|143|358|367)", a.get("filename", ""))]
        if docs:
            out.append({"time": r.get("date_utc"), "internal": r.get("from_is_internal"), "domain": r.get("from_domain"),
                        "message_id": r.get("id"), "attachments": [{"filename": a["filename"], "size": a["size"], "is_image": a.get("is_image"), "is_pdf": a.get("is_pdf")} for a in docs]})
    return sorted(out, key=lambda x: x["time"] or "")


# ------------------------------------------------------------------ assessment ----

SMS_DOC_RE = re.compile(r"\bbol\b|\bpod\b|paperwork|picture|photo|attached|signed|receipt|lumper", re.I)


def sms_evidence(texts: list[dict]) -> dict:
    """Inbound driver texts (createdBy 1 = System Admin is how TransportPro attributes them). Text only; the API exposes no attachments."""
    inbound = [t for t in texts if t.get("createdBy") == 1]
    # An inbound text with message None is an MMS picture: the API carries no media, but TransportPro files the image
    # itself as "Driver Supplied BOL" (363, comment "Driver Supplied Image") about one second later (load 2578982,
    # 14 Sep 2026: texts 18:05:12 / 18:05:15, files 18:05:13 / 18:05:16). PRD OQ-1.
    media = [t for t in inbound if t.get("message") is None]
    doc_like = [t for t in inbound if SMS_DOC_RE.search(t.get("message") or "")] + media
    outbound_requests = [t for t in texts if t.get("createdBy") != 1 and SMS_DOC_RE.search(t.get("message") or "")]
    # Macropoint's automatic texts (user 2696) fire on geofence events; "looks like you are loaded" is a better stage
    # signal than a dispatch status a rep has not advanced yet.
    loaded_texts = [t for t in texts if "looks like you are loaded" in (t.get("message") or "").lower()]
    delivered_texts = [t for t in texts if re.search(r"(looks like you.{0,40}(deliver|receiver|consignee)|before departing the delivery)", (t.get("message") or ""), re.I)]
    return {"inbound": len(inbound), "inbound_doc_like": len(doc_like), "last_inbound": max((t.get("dateCreated") or "" for t in inbound), default=None),
            "requests_sent": len(outbound_requests), "last_request": max((t.get("dateCreated") or "" for t in outbound_requests), default=None),
            "macropoint_loaded_at": min((t.get("dateCreated") or "" for t in loaded_texts), default=None),
            "macropoint_delivered_at": min((t.get("dateCreated") or "" for t in delivered_texts), default=None),
            "inbound_media": len(media), "media_times": [t.get("dateCreated") for t in media if t.get("dateCreated")]}


def assess(load_id: int, load: dict, dispatches: list[dict], files: list[dict], emails: list[dict], reqs: Requirements | None, flags: list[str], sms: dict | None = None) -> dict:
    status = load.get("status") or {}
    ref = load.get("reference") or {}
    customer = ((load.get("billingInfo") or {}).get("customer") or {}).get("companyName")
    terminal = load.get("assignedTerminal")
    dispatches = active_first(dispatches)
    disp = dispatches[0] if dispatches else {}
    assigned = disp.get("assignedTo") or {}
    cn = next((w for w in (load.get("waypoints") or []) if (w.get("type") or "").upper() == "CN"), {})
    delivery_appt = (cn.get("appointmentTime") or {}).get("open")
    equipment = {str(assigned.get(k) or "").strip() for k in ("trailerNumber", "tractorNumber")} - {""}
    stage = stage_of(disp.get("status"), status.get("loadStatus"))
    rank = STAGE_ORDER.get(stage, 1)
    sms = sms or {}
    stage_source = "dispatch"
    if sms.get("macropoint_loaded_at") and rank < STAGE_ORDER["loaded"]:
        stage, rank, stage_source = "loaded", STAGE_ORDER["loaded"], "Macropoint text"
    if sms.get("macropoint_delivered_at") and rank < STAGE_ORDER["at consignee"]:
        stage, rank, stage_source = "at consignee", STAGE_ORDER["at consignee"], "Macropoint text"

    # Reps file signed PODs under "Bill Of Lading" with the comment "POD" (seen on 2577037 and 2577850), so a
    # BOL-type file whose comment says POD counts as the POD. The type/status question itself is PRD OQ-3.
    def is_pod_file(f: dict) -> bool:
        return f.get("fileTypeId") in POD_TYPES or (f.get("fileTypeId") in BOL_TYPES and re.search(r"\bpod\b|proof|deliver", f.get("comments") or "", re.I) is not None)
    filed_pod = [f for f in files if is_pod_file(f)]
    filed_bol = [f for f in files if f.get("fileTypeId") in BOL_TYPES and not is_pod_file(f)]
    filed_docs = sorted(filed_bol + filed_pod, key=lambda f: f.get("dateCreated") or "")
    first_email = utc(emails[0]["time"]) if emails else None
    filed_after_email = [f for f in filed_docs if first_email and (utc(f.get("dateCreated")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) >= first_email - dt.timedelta(minutes=5)]
    filed_before_email = bool(filed_docs) and not filed_after_email

    # When TransportPro marked the load Delivered (proxy: lastUpdated of the Delivered dispatch; the API exposes no
    # status timestamp). Observed 14 Sep 2026 on 2572128 / 2577037 / 2575004: an upload made BEFORE this moment leaves
    # documentStatus at "Waiting for Documents" and the later Delivered mark does not recompute it; an upload made
    # AFTER it flips the status to "Documents Received" the same second. PRD OQ-3.
    delivered_at = utc(disp.get("lastUpdated")) if (disp.get("status") or "").lower() == "delivered" and disp.get("lastUpdated") else None
    filed_before_delivered = [f for f in filed_docs if delivered_at and utc(f.get("dateCreated")) and utc(f.get("dateCreated")) < delivered_at]
    media_times = [utc(t) for t in sms.get("media_times", []) if utc(t)]
    auto_from_sms = [f for f in filed_docs if f.get("fileTypeId") == 363 and utc(f.get("dateCreated"))
                     and any(abs((utc(f["dateCreated"]) - m).total_seconds()) <= 10 for m in media_times)]
    all_filed_before_delivered = bool(filed_docs) and delivered_at is not None and len(filed_before_delivered) == len(filed_docs)

    pod_ctx = reqs.pod_context(terminal) if reqs else None
    rules = reqs.for_customer(customer, terminal) if reqs else None
    bol_required = rules["bol_required"] if rules else True                     # TransportPro itself lists BOL as the missing document
    pod_required = rules["pod_required"] if rules else False

    # ---- state ----
    docs_received = (status.get("documentStatus") or "").lower().startswith("documents received")
    # Which document the truck's stage calls for right now: the BOL until the truck reaches the consignee, the POD after.
    expected_is_pod = rank >= STAGE_ORDER["at consignee"]
    expected_filed = bool(filed_pod) if expected_is_pod else bool(filed_bol or filed_pod)
    email_is_duplicate = bool(emails) and not filed_after_email and expected_filed   # paper already came in by another channel
    if docs_received and (not pod_required or filed_pod or (rules and rules.get("doc_type_overrides", {}).get("receiver_signed") and filed_bol)):
        state = "complete"
    elif emails and not filed_after_email and not expected_filed:
        state = "email_unfiled"
    elif filed_docs and not docs_received:
        state = "filed_status_pending"
    elif rank >= STAGE_ORDER["loaded"] and not filed_docs and not emails and sms.get("inbound_doc_like"):
        state = "sms_possible_doc"
    elif rank >= STAGE_ORDER["loaded"] and not filed_docs and not emails:
        state = "missing_no_source"
    elif rank < STAGE_ORDER["loaded"]:
        state = "not_yet_due"
    else:
        state = "review"

    # ---- verification steps from the workbook, evaluated as far as metadata allows ----
    checks: list[dict] = []
    def add(step, result, detail): checks.append({"step": step, "result": result, "detail": detail})
    if rules:
        if rules.get("bol_before_leaving_shipper"):
            have = bool(filed_bol) or bool(emails)
            add("BOL before leaving the shipper", "ok" if have else ("MISSING" if rank >= STAGE_ORDER["loaded"] else "pending"),
                f"BOL {'filed' if filed_bol else 'in email, not filed' if emails else 'not received'}; truck is {stage}")
        if rules.get("pod_required"):
            have_pod = bool(filed_pod) or (rules.get("doc_type_overrides", {}).get("receiver_signed") and bool(filed_bol))
            if rank >= STAGE_ORDER["at consignee"]:
                add("POD required before delivering out", "ok" if have_pod else "BLOCKED", "POD filed" if have_pod else ("POD may be in email, unfiled" if emails else "no POD received"))
            else:
                add("POD required before delivering out", "pending", f"truck is {stage}")
        if rules.get("upload_required_before_deliver_out"):
            add("Document must be uploaded to TransportPro", "ok" if filed_docs else ("MISSING" if rank >= STAGE_ORDER["at consignee"] else "pending"), f"{len(filed_docs)} BOL/POD file(s) in File History")
        pr = rules.get("pages_required")
        if pr:
            add(f"All pages present ({pr})", "verify", "count pages on the document" + (f"; email carried {sum(len(e['attachments']) for e in emails)} attachment(s)" if emails else ""))
        if rules.get("pod_signatures"):
            add("Signatures on POD: " + ", ".join(rules["pod_signatures"]) + (" (stamp accepted)" if rules.get("stamp_acceptable") else ""), "verify", "check on the document image")
        if rules.get("bol_signatures"):
            add("Signatures on BOL: " + ", ".join(rules["bol_signatures"]), "verify", "check on the document image")
        if rules.get("seal_required_on_bol"):
            add("Seal number on BOL", "verify", "check on the document image")
        if rules.get("in_out_times_required"):
            add("In/out times on BOL/POD", "verify", "check on the document image; needed for detention")
        if rules.get("freight_photos_required"):
            n_img = sum(1 for e in emails for a in e["attachments"] if a.get("is_image"))
            add("Photos of loaded freight before leaving shipper", "likely" if n_img >= 2 else "verify", f"{n_img} photo attachment(s) in email")
        if rules.get("address_match_required"):
            add("Paper addresses/customer match the load", "verify", "matcher compares parties when the document is read")
        if rules.get("deliver_out_with_detention") is False:
            add("Hold open while detention/layover request is pending", "note", "do not deliver out until AM confirms")
        if rules.get("escalate_if_missing") and state in ("missing_no_source", "email_unfiled") and rank >= STAGE_ORDER["loaded"]:
            add("Escalate if document not received", "ACTION", rules.get("am") or "AM on the sheet")
        if rules.get("doc_type_overrides"):
            add("Filing type override", "note", "; ".join(f"{k} -> {v}" for k, v in rules["doc_type_overrides"].items()))
    else:
        add("Customer requirements", "none on file", f"default: BOL expected once loaded" + ("" if not pod_ctx or pod_ctx.get("sheet") else f"; pod {pod_ctx.get('pod') or terminal} has no requirements sheet" if pod_ctx else ""))

    filed_types = sorted({f.get("fileTypeName") or str(f.get("fileTypeId")) for f in filed_docs})
    email_ts = first_email.astimezone(dt.timezone(dt.timedelta(hours=-4))).strftime('%m/%d %H:%M') if first_email else None
    if all_filed_before_delivered:
        pending_note = f"; all {len(filed_docs)} filing(s) predate the Delivered mark ({delivered_at:%m/%d %H:%M} UTC): the pattern that leaves the status stuck (OQ-3)"
    elif delivered_at and filed_docs:
        pending_note = "; filed after the Delivered mark yet still Waiting: does not fit the OQ-3 pattern, check by hand"
    elif filed_docs and stage == "delivered":
        pending_note = "; Delivered, but no timestamp for the Delivered mark was available, so the OQ-3 timing could not be checked"
    elif filed_docs:
        pending_note = "; load not yet Delivered: by the OQ-3 pattern the status clears only on an upload made after the Delivered mark"
    else:
        pending_note = ""
    if auto_from_sms:
        pending_note += f"; {len(auto_from_sms)} of the Driver Supplied BOL file(s) were auto-filed from the driver's text-message photos (MMS)"
    action = {
        "email_unfiled": (f"FILE: document in ratecon thread since {email_ts} ET, " + (f"{', '.join(filed_types)} already filed but the truck is {stage}, so the POD is expected; the attachment may be the POD or a duplicate BOL (read it to tell)" if filed_docs else "not in File History")) if first_email else "FILE",
        "missing_no_source": "REQUEST: nothing filed, nothing in email" + (f", {sms.get('requests_sent')} text request(s) already sent, no driver reply" if sms.get("requests_sent") else "; ask driver / carrier"),
        "sms_possible_doc": f"CHECK SMS: driver texted about paperwork ({sms.get('inbound_doc_like')} message(s), last {str(sms.get('last_inbound') or '')[:16]}); photo may be in the SMS window",
        "filed_status_pending": f"CHECK: filed as {', '.join(filed_types)} but status still Waiting" + pending_note + ("; email copy is likely a duplicate of the filed document" if email_is_duplicate else ""),
        "complete": "-", "not_yet_due": "-", "review": "REVIEW",
    }[state]
    lag = None
    if filed_after_email and first_email:
        lag = round((utc(filed_after_email[0]["dateCreated"]) - first_email).total_seconds() / 60)
        if lag < 0:
            lag = 0

    return {
        "load": load_id, "state": state, "action": action, "stage": stage, "load_status": status.get("loadStatus"), "document_status": status.get("documentStatus"),
        "customer": customer, "terminal": terminal, "pod": (pod_ctx or {}).get("pod"), "rules_sheet": rules["sheet"] if rules else None, "cross_pod": rules.get("cross_pod") if rules else None,
        "bol_required": bol_required, "pod_required": pod_required,
        "filed": [{"type": f.get("fileTypeName"), "at": f.get("dateCreated"), "by": f.get("uploadById"), "comment": f.get("comments")} for f in filed_docs],
        "emails": emails, "email_first": emails[0]["time"] if emails else None, "email_senders": sorted({("Circle" if e["internal"] else e["domain"]) for e in emails}),
        "lag_minutes": lag, "filed_before_email": filed_before_email,
        "dashboard_flags": flags, "checks": checks, "sms": sms, "email_is_duplicate": email_is_duplicate, "stage_source": stage_source,
        "delivered_at": delivered_at.isoformat() if delivered_at else None, "all_filed_before_delivered": all_filed_before_delivered,
        "auto_filed_from_sms": len(auto_from_sms), "equipment_numbers": sorted(equipment), "delivery_appt": delivery_appt,
        "refs": {k: ref.get(k) for k in ("billOfLading", "poNumber", "pickupNumber", "referenceNumber")},
    }


# ------------------------------------------------------------------ dashboard text ----

def parse_dashboard(path: Path) -> dict[int, list[str]]:
    """Load numbers and flags from a saved Load Management page text (get_page_text output)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n(?=\d+ [A-Z].*\n)", text)
    out: dict[int, list[str]] = {}
    for b in blocks:
        m = re.search(r"Load #\s*(\d{7})", b)
        if not m:
            continue
        flags = [f for f in ("Late Pickup", "Late Delivery", "Missing Delivery", "At Risk", "Not Tracking", "Setup Tracking") if f in b]
        flags += re.findall(r"Escalated: ([^\n]+)", b)
        out[int(m.group(1))] = flags
    return out


# ------------------------------------------------------------------ main ----

def _ids(rows: list[dict]) -> str:
    ids = sorted(int(r["id"]) for r in rows if r.get("id"))
    return f"ids {ids[0]}..{ids[-1]}" if ids else "no ids"


def dashboard_load_set(tpro, args, reqs) -> dict[int, list[dict]]:
    """Reproduce the Load Management filter through the API: one /load/search per ticked terminal over a pickup-date
    window, then keep the loads a Track & Trace rep would still be working (not cancelled; Waiting for Documents
    unless --include-complete). Returns rows per terminal and prints the per-pod counts."""
    if args.terminals:
        terminals = [int(x) for x in re.findall(r"\d+", args.terminals)]
    else:
        pods = load_pod_map_terminals()
        terminals = [t["id"] for t in pods if t.get("in_current_view")]
    today = dt.date.today()
    end = today + dt.timedelta(days=45)                      # dispatched loads whose pickup is still ahead (the Future bucket)
    start = today - dt.timedelta(days=args.dashboard_days)
    # The dashboard's "In Transit" is loadStatus Dispatched (Delivered and Cancelled loads leave it); the server honours
    # this filter. Planned rows are requested only with --include-undispatched.
    statuses = ["Dispatched"] + (["Ready To Dispatch"] if args.include_undispatched else [])
    # Service level is part of the saved dashboard filter (serviceLevelSelect = "Priority / OP8" on 14 Sep 2026); without it
    # the API returns every Flexible / FCFS and Firm Appointment load on the pods as well (179 extra loads that day).
    levels = levels_in_scope(args)
    out: dict[int, list[dict]] = {}
    buckets: collections.Counter = collections.Counter()
    dropped_level: collections.Counter = collections.Counter()
    print(f"dashboard filter: {len(terminals)} terminal(s), In Transit (Dispatched), pickups {start} .. {end}"
          + (f", service level {sorted(levels)}" if levels else ", any service level") + ("" if args.include_complete else "; loads still Waiting for Documents are checked"))
    total_in_transit = 0
    for tid in terminals:
        rows: list[dict] = []
        for status in statuses:
            for w_start, w_end in date_chunks(start, end, 45):    # the API rejects wide ranges (HTTP 400 somewhere around 60 days; probed 14 Sep 2026)
                rows += search_window(tpro, {"terminalId": str(tid), "loadStatus": status}, w_start, w_end)
        rows = list({int(r["id"]): r for r in rows if r.get("id")}.values())
        kept = []
        in_view = 0
        for r in rows:
            st = r.get("status") or {}
            if (st.get("loadStatus") or "").lower().startswith("cancel"):
                continue
            if levels and not (service_levels(r) & levels):
                dropped_level[", ".join(sorted(service_levels(r))) or "none"] += 1
                continue
            buckets[delivery_bucket(r, today)] += 1
            total_in_transit += 1
            in_view += 1
            if not args.include_complete and not (st.get("documentStatus") or "").lower().startswith("waiting"):
                continue
            kept.append(r)
        out[tid] = kept
        name = reqs.pod_name(tid) if reqs else None
        print(f"  {name or tid}: {in_view} in the dashboard view ({len(rows)} dispatched on the pod), {len(kept)} waiting for documents")
    print(f"  In Transit total {total_in_transit}: past due {buckets['past due']}, today {buckets['today']}, tomorrow {buckets['tomorrow']}, future {buckets['future']}, no delivery date {buckets['none']}"
          "   (the header buckets by next check date, so only the total is comparable)")
    if dropped_level:
        print(f"  dropped by service level: {dict(dropped_level)}")
    return out


def search_window(tpro, params: dict, start: dt.date, end: dt.date, depth: int = 0) -> list[dict]:
    """/load/search over [start, end]; on HTTP 400 (range too wide, or a boundary the API dislikes) split the window in
    half and retry, down to single days. Never silently drops a window: an unrecoverable error is printed."""
    try:
        return tpro.search_all_pages({**params, "pickupDateStart": start.isoformat(), "pickupDateEnd": end.isoformat()})
    except Exception as e:  # noqa: BLE001 - payment_bot.errors.ClientError carries the HTTP status in its text
        if "400" not in str(e) or (end - start).days < 1 or depth > 8:
            print(f"  warning: /load/search {start}..{end} {params} failed: {str(e)[:100]}")
            return []
        mid = start + (end - start) / 2
        return search_window(tpro, params, start, mid, depth + 1) + search_window(tpro, params, mid + dt.timedelta(days=1), end, depth + 1)


def levels_in_scope(args) -> set[str]:
    """Service levels the job works: --service-level if given ('all' = no filter), else dashboard_filter.service_level
    in index/pod_terminals.json (Priority / OP8 on 14 Sep 2026). Applies to every mode, not only --from-dashboard."""
    raw = getattr(args, "service_level", None)
    if raw and raw.strip().lower() == "all":
        return set()
    if raw:
        return {x.strip().lower() for x in raw.split(",") if x.strip()}
    return {x.lower() for x in load_dashboard_filter().get("service_level", [])}


def service_levels(row: dict) -> set[str]:
    """Every SERVICE_LEVEL reference on the load's stops, lower-cased (one dashboard load carried Firm Appointment on one stop and Priority / OP8 on another)."""
    out = set()
    for w in row.get("waypoints") or []:
        for ref in w.get("reference") or []:
            if (ref.get("type") or "").upper() == "SERVICE_LEVEL" and ref.get("value"):
                out.add(str(ref["value"]).strip().lower())
    return out


def load_dashboard_filter() -> dict:
    path = HERE / "index" / "pod_terminals.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("dashboard_filter", {})


def date_chunks(start: dt.date, end: dt.date, max_days: int) -> list[tuple[dt.date, dt.date]]:
    """[start, end] cut into consecutive windows of at most max_days (inclusive ends overlap by a day; rows are de-duplicated by id)."""
    out = []
    cur = start
    while cur <= end:
        nxt = min(cur + dt.timedelta(days=max_days), end)
        out.append((cur, nxt))
        if nxt >= end:
            break
        cur = nxt
    return out


def delivery_bucket(row: dict, today: dt.date) -> str:
    """Past Due / Today / Tomorrow / Future by the consignee appointment date, as the Load Management header buckets them."""
    cn = next((w for w in (row.get("waypoints") or []) if (w.get("type") or "").upper() == "CN"), {})
    when = utc((cn.get("appointmentTime") or {}).get("open") or (cn.get("appointmentTime") or {}).get("close"))
    if not when:
        return "none"
    # the appointment is stored in UTC; the dashboard buckets by local date, so shift by the stop's timezone offset when given
    tz = cn.get("location", {}).get("timezone")
    local = (when + dt.timedelta(hours=int(tz))).date() if isinstance(tz, (int, float)) else when.date()
    if local < today:
        return "past due"
    if local == today:
        return "today"
    if local == today + dt.timedelta(days=1):
        return "tomorrow"
    return "future"


def load_pod_map_terminals() -> list[dict]:
    path = HERE / "index" / "pod_terminals.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("terminals", [])


def missing_documents_set(tpro, args) -> list[dict]:
    """Page 0 of /load/missing_documents is the oldest tail of the backlog. With --newest, probe for a page parameter
    and read the last page(s). Never silently substitutes page 0 for a newer page: it says what it got."""
    first, pg = tpro.missing_documents()
    total_pages = int(pg.get("totalPages") or 1)
    per_page = int(pg.get("perPage") or 200)
    print(f"missing-documents list: {pg.get('totalRecords', len(first))} loads, {total_pages} page(s) of {per_page}; page 0 = {_ids(first)} (oldest)")
    if not args.newest or total_pages <= 1:
        return first
    styles = [lambda p: {"page": str(p)}, lambda p: {"currentPage": str(p), "perPage": str(per_page)}, lambda p: {"offset": str(p * per_page)}]   # "page" works on /load/search; try it first here too
    style = None
    got: list[dict] = []
    last = total_pages - 1
    for st in styles:
        rows, _ = tpro.missing_documents(st(last))
        key = list(st(last).keys())[0]
        if rows and first and rows[0].get("id") != first[0].get("id"):
            style = st
            print(f"  page parameter honoured: {key}; page {last} = {_ids(rows)}")
            got = list(rows)
            break
        print(f"  {key}={st(last)[key]} ignored (same first record as page 0)")
    if style is None:
        print("  no page parameter worked: only page 0 (the oldest loads) is reachable through this endpoint; use --from-survey or --loads for today's work")
        return first
    for p in range(last - 1, max(last - args.md_pages, 0), -1):
        rows, _ = tpro.missing_documents(style(p))
        print(f"  page {p} = {_ids(rows)}")
        got += rows
    return got


def main() -> int:
    load_local_env()                                      # model + TransportPro + Gmail variables from the project .env
    ap = argparse.ArgumentParser()
    ap.add_argument("--loads", default=None, help="comma-separated load numbers")
    ap.add_argument("--from-missing-documents", action="store_true", help="use TransportPro's own missing-documents list as the load set (page 0 = the oldest 200 open loads)")
    ap.add_argument("--from-dashboard", action="store_true", help="load set = what the Load Management filter shows: loads on the ticked pods (in_current_view in index/pod_terminals.json), picked up in the last --dashboard-days, still Waiting for Documents")
    ap.add_argument("--terminals", default=None, help="with --from-dashboard: comma-separated terminal ids instead of the ticked pods")
    ap.add_argument("--dashboard-days", type=int, default=350, help="with --from-dashboard: how far back the pickup-date window reaches (the API needs a date range and refuses starts older than ~360 days; the dashboard itself has no age limit)")
    ap.add_argument("--service-level", default=None, help="service levels in scope, comma-separated (default: dashboard_filter in index/pod_terminals.json = 'Priority / OP8'); loads with another level are marked out_of_scope and skipped in every mode; 'all' disables the filter")
    ap.add_argument("--count-only", action="store_true", help="with --from-dashboard: print the per-pod counts and the delivery buckets (Past Due / Today / Tomorrow / Future) and stop")
    ap.add_argument("--include-complete", action="store_true", help="with --from-dashboard: keep loads whose documents are already received")
    ap.add_argument("--include-undispatched", action="store_true", help="with --from-dashboard: keep loads not yet dispatched (Ready To Dispatch, Planned); no paperwork can exist for them yet")
    ap.add_argument("--newest", action="store_true", help="with --from-missing-documents: read the LAST page(s) of the list (newest loads) by probing a page parameter")
    ap.add_argument("--md-pages", type=int, default=1, help="with --newest: how many pages from the end to read")
    ap.add_argument("--reader-python", default=None, help="with --read: interpreter that has anthropic/pymupdf installed, if this one does not (default: python on PATH)")
    ap.add_argument("--max", type=int, default=150, help="cap on loads per run (each costs 3-4 TransportPro reads and one Gmail search)")
    ap.add_argument("--no-sms", action="store_true", help="skip the dispatch text-message read")
    ap.add_argument("--from-survey", default=None, help="mail_survey JSON: use every load that received a document by email")
    ap.add_argument("--emails-from-survey", action="store_true", help="take email evidence from the survey JSON instead of live Gmail")
    ap.add_argument("--tpro-fixture", default=None, help="offline TransportPro answers (JSON) instead of the live API")
    ap.add_argument("--dashboard-text", default=None, help="saved Load Management page text to pull dashboard flags from")
    ap.add_argument("--requirements", default=str(HERE / "index" / "customer_requirements.json"))
    ap.add_argument("--days", type=int, default=14, help="Gmail search window")
    ap.add_argument("--out", default=str(HERE / "out" / "readiness"))
    ap.add_argument("--read", action="store_true", help="download unfiled email attachments and run the reader + requirements check (needs model credentials)")
    ap.add_argument("--read-all", action="store_true", help="with --read: also read the email attachments of loads that already have something filed (is the email copy a duplicate, or the POD?)")
    ap.add_argument("--model", default="claude-opus-5")
    args = ap.parse_args()

    reqs = Requirements.from_file(args.requirements) if Path(args.requirements).exists() else None
    loads: list[int] = []
    survey_emails = None
    if args.from_survey:
        survey_emails = SurveyEmails(Path(args.from_survey))
        if not args.loads:                                   # --loads narrows the set; the survey then only supplies email evidence
            loads += [int(k) for k, v in survey_emails.by_load.items() if document_messages(v)]
    if args.loads:
        loads += [int(x) for x in re.findall(r"\d{7}", args.loads)]
    if args.tpro_fixture and not args.loads:                 # offline: only loads the fixture knows
        known = set(json.loads(Path(args.tpro_fixture).read_text(encoding="utf-8"))["loads"])
        loads = [l for l in loads if str(l) in known]
    tpro = TProFixture(Path(args.tpro_fixture)) if args.tpro_fixture else TProSource()
    prefetched: dict[int, dict] = {}
    if args.from_missing_documents:
        md = missing_documents_set(tpro, args)
        loads += sorted((int(r["id"]) for r in md if r.get("id")), reverse=True)
    if args.from_dashboard:
        rows_by_terminal = dashboard_load_set(tpro, args, reqs)
        if args.count_only:
            return 0
        for tid, trows in rows_by_terminal.items():
            for row in trows:
                prefetched[int(row["id"])] = row
                loads.append(int(row["id"]))
    loads = sorted(set(loads), reverse=True)[: args.max]
    if not loads:
        ap.error("no loads: pass --loads, --from-survey or --from-missing-documents")
    if args.emails_from_survey:
        if survey_emails is None and args.from_survey is None:
            ap.error("--emails-from-survey needs --from-survey")
        gmail = survey_emails
    else:
        try:
            gmail = GmailSource(args.days)
        except Exception as e:  # noqa: BLE001 - TransportPro-only runs are still useful
            print(f"Gmail unavailable ({e}); continuing without email evidence")
            gmail = None
    flags = parse_dashboard(Path(args.dashboard_text)) if args.dashboard_text else {}

    rows = []
    scope_levels = levels_in_scope(args)
    if scope_levels:
        print(f"service levels in scope: {sorted(scope_levels)} (others are listed as out_of_scope and not checked)")
    for lid in loads:
        try:
            load = prefetched.get(lid) or tpro.load(lid)     # search rows carry the same fields as the detail call
        except Exception as e:  # noqa: BLE001
            rows.append({"load": lid, "state": "error", "action": f"TransportPro: {e}", "checks": [], "emails": [], "filed": []})
            continue
        found_levels = service_levels(load)
        if scope_levels and found_levels and not (found_levels & scope_levels):
            rows.append({"load": lid, "state": "out_of_scope", "stage": "", "document_status": (load.get("status") or {}).get("documentStatus"),
                         "pod": reqs.pod_name(load.get("assignedTerminal")) if reqs else None, "terminal": load.get("assignedTerminal"),
                         "customer": ((load.get("billingInfo") or {}).get("customer") or {}).get("companyName"),
                         "action": f"service level {', '.join(sorted(found_levels))}: not in scope ({', '.join(sorted(scope_levels))}); skipped", "checks": [], "emails": [], "filed": []})
            continue
        dispatches = active_first(tpro.dispatches(lid))
        files = tpro.files(lid)
        emails = document_messages(gmail.messages_for(lid) if gmail is not None else [])
        sms = None
        if not args.no_sms and dispatches and dispatches[0].get("id"):
            sms = sms_evidence(tpro.text_messages(int(dispatches[0]["id"])))
        rows.append(assess(lid, load, dispatches, files, emails, reqs, flags.get(lid, []), sms))

    order = {"email_unfiled": 0, "sms_possible_doc": 1, "missing_no_source": 2, "filed_status_pending": 3, "review": 4, "not_yet_due": 5, "complete": 6, "error": 7, "out_of_scope": 8}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r.get("email_first") or ""))

    if args.read:
        rows = run_reader(rows, gmail, args)

    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    (out_dir / f"readiness_{stamp}.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    with (out_dir / f"readiness_{stamp}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["load", "state", "action", "stage", "load_status", "document_status", "pod", "customer", "rules_sheet", "bol_required", "pod_required", "filed", "email_first", "email_senders", "lag_minutes", "dashboard_flags", "checks"])
        for r in rows:
            w.writerow([r["load"], r["state"], r["action"], r.get("stage"), r.get("load_status"), r.get("document_status"), r.get("pod"), r.get("customer"), r.get("rules_sheet"), r.get("bol_required"), r.get("pod_required"),
                        "; ".join(f"{f['type']} {f['at']}" for f in r.get("filed", [])), r.get("email_first"), ", ".join(r.get("email_senders", [])), r.get("lag_minutes"), ", ".join(r.get("dashboard_flags", [])),
                        " | ".join(f"{c['step']}: {c['result']}" for c in r.get("checks", []))])

    counts = collections.Counter(r["state"] for r in rows)
    lags = sorted(r["lag_minutes"] for r in rows if r.get("lag_minutes") is not None)
    median_lag = lags[len(lags) // 2] if lags else None
    waiting = [r for r in rows if (r.get("document_status") or "").lower().startswith("waiting")]
    w_files = [r for r in waiting if r.get("filed")]
    w_mail_only = [r for r in waiting if not r.get("filed") and r.get("emails")]
    pend = [r for r in rows if r["state"] == "filed_status_pending"]
    pend_delivered = [r for r in pend if r.get("delivered_at")]
    pend_before = [r for r in pend if r.get("all_filed_before_delivered")]
    by_pod = collections.defaultdict(collections.Counter)
    for r in rows:
        by_pod[r.get("pod") or str(r.get("terminal") or "unknown")][r["state"]] += 1
    lines = [f"# Document readiness - {dt.datetime.now():%Y-%m-%d %H:%M}", "", f"{len(rows)} loads. States: " + ", ".join(f"{k} {v}" for k, v in counts.items()), "",
             "## Headline numbers", "",
             f"- Loads still \"Waiting for Documents\": {len(waiting)}: {len(w_files)} have BOL/POD files in File History, {len(w_mail_only)} have paperwork only in the ratecon thread, {len(waiting) - len(w_files) - len(w_mail_only)} have nothing anywhere.",
             f"- Of the {len(pend)} filed-but-Waiting loads, {len(pend_delivered)} are Delivered; on {len(pend_before)} of those every filing predates the Delivered mark (the OQ-3 pattern: TransportPro appears to set Documents Received only for an upload made after the load is Delivered).",
             f"- Documents in email with nothing of the expected type filed (the bot's work queue): {counts.get('email_unfiled', 0)}.",
             f"- Filed but status still Waiting: {counts.get('filed_status_pending', 0)} (Driver Supplied BOL is the filed type on {sum(1 for r in rows if r['state']=='filed_status_pending' and any(f.get('type')=='Driver Supplied BOL' for f in r.get('filed', [])))} of them).",
             f"- Where a filing followed the first paperwork email: median {median_lag} min across {len(lags)} loads (first email to first filing; a large value can mean the BOL was emailed at pickup and the POD filed days later).",
             ]
    read_rows = [r for r in rows if r.get("reader")]
    if read_rows:
        oc = collections.Counter(r["reader"]["outcome"].split(" document(s)")[0] if "document(s) to file" in r["reader"]["outcome"] else r["reader"]["outcome"] for r in read_rows)
        to_file = sum(1 for r in read_rows if "document(s) to file" in r["reader"]["outcome"])
        lines.append(f"- Reader on the {len(read_rows)} unfiled loads: {to_file} have a BOL/POD to file, "
                     f"{oc.get('BOL again (already filed); POD still missing', 0)} hold only a BOL that is already filed, {oc.get('freight photos only', 0)} freight photos only, "
                     f"{oc.get('no document (signature, screenshot or rate con)', 0) + oc.get('no document-sized attachment', 0)} nothing but signatures, screenshots or rate cons, {oc.get('reader error', 0)} reader errors. "
                     f"Real work queue: {to_file}. Model spend ${sum(r['reader']['cost_usd'] for r in read_rows):.2f} over {sum(r['reader']['read'] for r in read_rows)} files.")
    lines += ["", "## By pod", "", "| Pod | " + " | ".join(k for k in order) + " |", "|---|" + "---|" * len(order)]
    for pod, c in sorted(by_pod.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(f"| {pod} | " + " | ".join(str(c.get(k, 0)) for k in order) + " |")
    lines += ["", "## Work queue", "",
             "| Load | State | Stage | Doc status | Pod | Customer | Filed | Email doc (first) | SMS in/req | Lag | Action |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        s = r.get("sms") or {}
        lines.append(f"| {r['load']} | **{r['state']}** | {r.get('stage','')}{' (Macropoint)' if r.get('stage_source') == 'Macropoint text' else ''} | {r.get('document_status','')} | {r.get('pod') or r.get('terminal') or ''} | {(r.get('customer') or '')[:34]} | "
                     f"{'; '.join(f['type'] for f in r.get('filed', [])) or '-'} | {(r.get('email_first') or '-')[:16]} {('(' + ', '.join(r.get('email_senders', [])) + ')') if r.get('emails') else ''} | "
                     f"{(str(s.get('inbound_doc_like', 0)) + '/' + str(s.get('requests_sent', 0)) + (' (' + str(s.get('inbound_media')) + ' pic)' if s.get('inbound_media') else '')) if s else '-'} | "
                     f"{'filed first (other channel)' if r.get('filed_before_email') else ('' if r.get('lag_minutes') is None else str(r['lag_minutes']) + ' min')} | {r.get('action','')} |")
    lines += ["", "## Verification steps per load (from the customers' sheets)", ""]
    for r in rows:
        if not r.get("checks") and not r.get("verification"):
            continue
        lines.append(f"**{r['load']}** - {r.get('customer') or ''} - sheet: {r.get('rules_sheet') or 'none'}" + (" (rule from another pod's sheet)" if r.get("cross_pod") else ""))
        for c in r.get("checks") or []:
            lines.append(f"- [{c['result']}] {c['step']}: {c['detail']}")
        if r.get("verification"):
            lines.append(f"- reader verdict: {r['verification']}")
        lines.append("")
    md = "\n".join(lines)
    (out_dir / f"readiness_{stamp}.md").write_text(md, encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(md)
    print(f"\nsaved {out_dir / f'readiness_{stamp}.md'} / .csv / .json")
    return 0


MIN_ATTACHMENT_BYTES = 40_000      # below this an image is a signature, a logo or an illegible thumbnail
TPRO_RATECON_RE = re.compile(r"^\d{7,9}_(23|143|358|367)(\D|$)")   # <fileId>_<typeId>.pdf as TransportPro names them
MIN_IMAGE_SIDE_PX = 300            # signature banners are ~500 x 150, logos ~200 x 200; a screenshot of a document is larger on both sides
MIN_IMAGE_AREA_PX = 150_000        # ~400 x 375; a phone photo is 1080 x 1920 or more, a phone screenshot ~1170 x 2500
MAX_IMAGE_ASPECT = 2.2             # wider than this is a signature banner or a logo strip, never a page
MAX_ATTACHMENTS_PER_LOAD = 6       # bounds model spend per load; the largest files are read first


def run_reader(rows: list[dict], gmail, args) -> list[dict]:
    """Optional: for email_unfiled loads, download the attachments and run the prototype's reader + requirements check."""
    if not hasattr(gmail, "attachment_bytes"):
        print("--read needs live Gmail (attachments are not in the survey JSON); skipping")
        return rows
    reader_py = None
    try:
        import anthropic  # noqa: F401
        import pymupdf  # noqa: F401
        from read_attachments import read_files   # same code the subprocess runs: one path, one behaviour
    except ModuleNotFoundError as e:
        # The payment-bot venv (needed for the TransportPro client and Gmail delegation) has no model SDKs.
        # Run the reader in another interpreter instead of installing into that venv.
        reader_py = _find_reader_python(args.reader_python)
        if not reader_py:
            print(f"--read: '{e.name}' is not installed in {sys.executable} and no other python with the prototype requirements was found. "
                  f"Either run '<python> -m pip install -r requirements.txt' in that interpreter or pass --reader-python <path>. Skipping the reader.")
            return rows
        print(f"--read: '{e.name}' is not installed in {sys.executable}; running the reader in {reader_py}")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("OPENROUTER_API_KEY")):
        print("--read: no model credentials in this shell. Set them in this PowerShell window first, for OpenRouter:\n"
              '  $env:ANTHROPIC_BASE_URL  = "https://openrouter.ai/api"\n'
              '  $env:ANTHROPIC_AUTH_TOKEN = "sk-or-..."\n'
              '  $env:ANTHROPIC_API_KEY   = ""\n'
              "or $env:ANTHROPIC_API_KEY for the Anthropic API directly. Skipping the reader; nothing was downloaded.")
        for r in rows:
            if r["state"] == "email_unfiled":
                r["verification"] = "reader skipped: no model credentials in this shell"
        return rows
    att_dir = Path(args.out) / "attachments"
    for r in rows:
        if r["state"] != "email_unfiled" and not (getattr(args, "read_all", False) and r.get("emails")):
            continue
        verdicts = []
        saved: list[Path] = []
        # Pass 1: list the attachment parts of the load's messages. Every reply in a chain quotes the earlier messages,
        # so the same image (a document photo as much as a signature) appears in message after message: keep ONE copy per
        # (name, size). Anything under MIN_ATTACHMENT_BYTES is too small to be a legible document photo. Each part is
        # resolved by its own attachmentId, so five inline "image.png" parts are five files.
        cands: list[tuple[str, int, str, str, str]] = []
        seen: set = set()
        dupes = 0
        ratecons = 0
        file_times: dict[str, str] = {}
        for e in r["emails"][:12]:
            # re-fetch the message to get attachment ids (survey records do not keep them)
            m = gmail.ms.gget(gmail.tokens.token(), f"/users/{urllib.parse.quote(gmail.user)}/messages/{e['message_id']}", {"format": "full"})
            for x in _iter_parts(m.get("payload") or {}):
                b = x.get("body") or {}
                if x.get("filename") and b.get("attachmentId") and (b.get("size") or 0) >= MIN_ATTACHMENT_BYTES:
                    if TPRO_RATECON_RE.match(x["filename"]):
                        ratecons += 1
                        continue
                    key = (x["filename"], int(b["size"]))
                    if key in seen:
                        dupes += 1
                        continue
                    seen.add(key)
                    cands.append((x["filename"], int(b["size"]), b["attachmentId"], e["message_id"], e.get("time") or ""))
        cands.sort(key=lambda c: -c[1])
        if dupes:
            verdicts.append(f"{dupes} repeated copy/copies of the same images skipped (quoted in replies)")
        if ratecons:
            verdicts.append(f"{ratecons} TransportPro rate confirmation(s) skipped")
        # Pass 2: download largest first; drop signatures and logos by pixel size (header parse, no image library needed).
        small = 0
        read_n = 0
        skipped_desc: list[str] = []
        for name, size, att_id, mid, mtime in cands:
            if read_n >= MAX_ATTACHMENTS_PER_LOAD:
                verdicts.append(f"{len(cands) - read_n - small} smaller attachment(s) not read (cap {MAX_ATTACHMENTS_PER_LOAD} per load)")
                break
            data = gmail.attachment_bytes(mid, att_id)
            dims = _image_dims(data)
            if dims and (min(dims) < MIN_IMAGE_SIDE_PX or dims[0] * dims[1] < MIN_IMAGE_AREA_PX or dims[0] > MAX_IMAGE_ASPECT * dims[1]):
                small += 1
                skip_dir = att_dir / str(r["load"]) / "skipped"; skip_dir.mkdir(parents=True, exist_ok=True)
                (skip_dir / f"{small:02d}_{dims[0]}x{dims[1]}_{re.sub(r'[^A-Za-z0-9._-]', '_', name)}").write_bytes(data)
                skipped_desc.append(f"{name} {size // 1024} KB {dims[0]}x{dims[1]}")
                continue
            read_n += 1
            dest = att_dir / str(r["load"]); dest.mkdir(parents=True, exist_ok=True)
            fpath = dest / (f"{read_n:02d}_" + re.sub(r"[^A-Za-z0-9._-]", "_", name)); fpath.write_bytes(data)
            saved.append(fpath)
            file_times[fpath.name] = mtime
        results: list[dict] = []
        if saved and reader_py is None:
            try:
                results = read_files(saved, args.model, args.requirements, r.get("customer"), r.get("terminal"), r.get("stage") or "",
                                     set(r.get("equipment_numbers") or []), file_times, r.get("delivery_appt"))
            except Exception as ex_:  # noqa: BLE001 - a client/setup failure must not stop the other loads
                verdicts.append(f"reader failed: {type(ex_).__name__}: {ex_}")
        elif saved:
            cmd = [reader_py, str(HERE / "read_attachments.py"), "--model", args.model, "--requirements", args.requirements,
                   "--customer", r.get("customer") or "", "--terminal", str(r.get("terminal") or ""), "--stage", r.get("stage") or "",
                   "--exclude", ",".join(r.get("equipment_numbers") or []), "--pod-not-before", r.get("delivery_appt") or "",
                   *[f"--file-time={n}={t}" for n, t in file_times.items() if t], *map(str, saved)]
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(HERE), encoding="utf-8", errors="replace")
            for line in res.stdout.splitlines():
                try:
                    results.append(json.loads(line))
                except ValueError:
                    continue
            if res.returncode != 0 and not results:
                verdicts.append(f"reader process failed: {(res.stderr or '').strip()[-400:]}")
        for o in results:
            if o.get("error"):
                verdicts.append(f"{o['file']}: reader error {o['error']}")
            elif o.get("load_note"):
                verdicts.append(f"note: {o['load_note']}")
            elif o.get("not_document"):
                verdicts.append(f"{o['file']}: not a freight document ({o['not_document'][:90]}); est ${o.get('cost_usd', 0):.3f}")
            else:
                verdicts.append(f"{o['file']}: read as {o.get('document_type')} -> {o.get('filed_type')}; {o.get('verdict')}; est ${o.get('cost_usd', 0):.3f}")
        r["reader_files"] = [o for o in results if o.get("file") != "*"]   # numbers, kinds, notes, seals per file, for audit and labelling
        if small:
            verdicts.append(f"{small} small image(s) skipped as signature/logo, saved under attachments/{r['load']}/skipped: " + "; ".join(skipped_desc[:10]))
        if read_n == 0:
            verdicts.append("no document-sized attachment found")
        verdicts = [re.sub(r"not a freight document", "PII (personal ID), never file", v) if re.search(r"licen|passport|social security|id card", v, re.I) else v for v in verdicts]
        r["verification"] = " || ".join(_collapse_errors(verdicts)) if verdicts else "no readable attachments"
        r["reader"] = _reader_outcome(verdicts, r)
        r["action"] = f"{r.get('action', '')} | reader: {r['reader']['outcome']}"
    return rows


def _reader_outcome(verdicts: list[str], r: dict) -> dict:
    """Counts by what the reader saw, and one phrase for the work queue."""
    joined = " || ".join(verdicts)
    types = re.findall(r"read as (\w+) ->", joined)
    docs = [t for t in types if t not in ("photo", "rate_confirmation", "unknown", "other")]
    n = {"read": len(types) + joined.count("not a freight document") + joined.count("PII (personal ID)"), "documents": len(docs),
         "photos": types.count("photo"), "rate_cons": types.count("rate_confirmation") + sum(int(m) for m in re.findall(r"(\d+) TransportPro rate confirmation", joined)),
         "non_documents": joined.count("not a freight document") + joined.count("PII (personal ID)"), "errors": len(re.findall(r"reader error", joined)),
         "cost_usd": round(sum(float(x) for x in re.findall(r"est \$([0-9.]+)", joined)), 3)}
    pod_expected = STAGE_ORDER.get(r.get("stage", ""), 0) >= STAGE_ORDER["at consignee"]
    if docs and r.get("filed") and all(t == "bill_of_lading" for t in docs):
        outcome = "BOL again (already filed); POD still missing" if pod_expected else "BOL again (already filed): email copy is a duplicate"
    elif docs:
        outcome = f"{len(docs)} document(s) to file"
    elif n["photos"]:
        outcome = "freight photos only"
    elif n["non_documents"] or n["rate_cons"]:
        outcome = "no document (signature, screenshot or rate con)"
    elif n["errors"]:
        outcome = "reader error"
    else:
        outcome = "no document-sized attachment"
    n["outcome"] = outcome
    return n


def _image_dims(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG / GIF / JPEG header; None for anything else (PDFs pass through)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if marker in sof:
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return w, h
            i += 2 + seg_len
    return None


def _collapse_errors(verdicts: list[str]) -> list[str]:
    """Thirteen files failing with the same message become one line."""
    errs: dict[str, list[str]] = {}
    keep: list[str] = []
    for v in verdicts:
        if ": reader error " in v:
            name, msg = v.split(": reader error ", 1)
            errs.setdefault(msg, []).append(name)
        else:
            keep.append(v)
    for msg, names in errs.items():
        keep.append(f"reader error on {len(names)} file(s) ({', '.join(names[:3])}{'...' if len(names) > 3 else ''}): {msg}")
    return keep


def _find_reader_python(explicit: str | None) -> str | None:
    """An interpreter that can import the prototype's model dependencies, other than the one running this script."""
    here = Path(sys.executable).resolve()
    cands = [explicit] if explicit else [shutil.which(n) for n in ("python", "python3", "py")]
    for c in cands:
        if not c or Path(c).resolve() == here:
            continue
        try:
            ok = subprocess.run([c, "-c", "import anthropic, pymupdf, pydantic"], capture_output=True, timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            return c
    return None


def _iter_parts(part: dict):
    yield part
    for p in part.get("parts") or []:
        yield from _iter_parts(p)


if __name__ == "__main__":
    raise SystemExit(main())
