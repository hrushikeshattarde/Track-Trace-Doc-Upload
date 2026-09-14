r"""Survey a Google Group's mail (e.g. ratecon@circledelivers.com) for document-intake patterns.

Read-only. Reuses the paystatus bot's Google service-account delegation (impersonating the configured member
user) and Gmail search, exactly as that bot does. Pulls message HEADERS and ATTACHMENT NAMES only; message
bodies are never stored, only a handful of derived flags (e.g. "a 7-digit load number appears in the
subject").

Run with the payment-bot virtualenv, which has google-auth installed:

  C:\Users\hrushikesh.attarde_c\Desktop\payment-bot-intake-policies-and-hardening\.venv\Scripts\python.exe mail_survey.py --group ratecon@circledelivers.com --days 30 --max 600

Outputs out\mail-survey\<group>_<date>.json (per-message header/attachment records) and a Markdown summary.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import json
import re
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

PAYBOT_DIR = Path(r"C:\Users\hrushikesh.attarde_c\Desktop\payment-bot-intake-policies-and-hardening")
sys.path.insert(0, str(PAYBOT_DIR / "src"))
from payment_bot.clients.google_auth import (  # noqa: E402
    GMAIL_READONLY_SCOPES, ServiceAccountTokenSource, load_service_account_info,
)

GMAIL = "https://gmail.googleapis.com/gmail/v1"
INTERNAL_DOMAIN = "circledelivers.com"
LOAD_RE = re.compile(r"(?<!\d)(2[4-6]\d{5})(?!\d)")          # Circle load IDs are 7 digits, currently 24xxxxx-26xxxxx
AUTOMATION_HINTS = ("noreply", "no-reply", "donotreply", "notification", "mailer", "docusign", "transportpro", "macropoint", "highway", "ratecon@", "postmaster", "bounce")
DOC_KINDS = [  # order matters: first match wins
    ("rate_confirmation", re.compile(r"rate\s*_?-?con|ratecon|rate\s*_?confirm|\bRC\b|carrier\s*rate", re.I)),
    ("pod",               re.compile(r"\bpod\b|proof\s*of\s*delivery|\bdelivered\b|signed", re.I)),
    ("bol",               re.compile(r"\bbol\b|bill\s*of\s*lading|\bb/?l\b|lading", re.I)),
    ("invoice",           re.compile(r"invoice|\binv\b|billing|statement|remit", re.I)),
    ("lumper",            re.compile(r"lumper|receipt", re.I)),
    ("scale",             re.compile(r"scale|weight\s*ticket", re.I)),
    ("insurance_carrier", re.compile(r"insurance|\bcoi\b|w-?9|authority|packet|setup", re.I)),
    ("paperwork_generic", re.compile(r"paperwork|\bppw\b|docs?\b|documents?", re.I)),
]
IMAGE_MIMES = ("image/jpeg", "image/png", "image/heic", "image/heif", "image/tiff", "image/webp")
# TransportPro names files it generates "<fileId>_<fileTypeId>.pdf"; the type id is the File History document type.
TPRO_FILE_RE = re.compile(r"^(\d{7,9})_(\d{1,3})(?:\s*\(\d+\))?\.pdf$", re.I)
TPRO_TYPES = {"23": "rate_confirmation", "143": "rate_confirmation", "358": "rate_confirmation", "367": "rate_confirmation",
              "12": "bol", "363": "bol", "360": "pod", "53": "pod", "22": "invoice", "81": "invoice", "319": "invoice", "109": "invoice"}


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def gget(token: str, path: str, params: dict | None = None) -> dict:
    url = f"{GMAIL}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Gmail API {e.code} on {path}: {body}") from None


def walk_parts(part: dict, out: list[dict]) -> None:
    fn = part.get("filename") or ""
    mime = part.get("mimeType") or ""
    body = part.get("body") or {}
    if fn or body.get("attachmentId"):
        out.append({"filename": fn, "mimeType": mime, "size": body.get("size", 0), "inline": "attachmentId" not in body and not fn})
    for p in part.get("parts") or []:
        walk_parts(p, out)


def classify_name(name: str) -> str:
    m = TPRO_FILE_RE.match((name or "").strip())
    if m:
        return TPRO_TYPES.get(m.group(2), f"tpro_type_{m.group(2)}")
    for kind, rx in DOC_KINDS:
        if rx.search(name or ""):
            return kind
    low = (name or "").lower()
    if re.search(r"camscanner|scan|img_|photo_|image\.(png|jpe?g)|\.(jpe?g|png|heic)$|unnamed|^[0-9a-f-]{20,}\.(jpe?g|png)$", low):
        return "photo_or_scan_unlabeled"
    return "unclassified"


def summarize_message(m: dict, group: str) -> dict:
    headers = {h["name"].lower(): h["value"] for h in (m.get("payload", {}).get("headers") or [])}
    frm_name, frm_addr = parseaddr(headers.get("from", ""))
    frm_addr = frm_addr.lower()
    domain = frm_addr.split("@")[-1] if "@" in frm_addr else ""
    via_group = ("via" in frm_name.lower()) and (frm_addr == group.lower())
    if via_group:  # Google Groups rewrote the From; original sender hides in the display name or X-Original-Sender
        orig = headers.get("x-original-sender", "")
        if orig:
            frm_addr = orig.lower(); domain = frm_addr.split("@")[-1]
    subject = headers.get("subject", "")
    try:
        when = parsedate_to_datetime(headers.get("date", "")).astimezone(dt.timezone.utc)
    except Exception:
        when = None
    parts: list[dict] = []
    walk_parts(m.get("payload") or {}, parts)
    candidates = [p for p in parts if p["filename"] and not p["inline"] and p["size"] > 20_000]   # drop tiny icons outright
    # Signature/logo heuristic: the same byte size appearing 3+ times in one message is a repeated signature image
    # (quoted replies re-embed it), not three documents. Keep one copy flagged, drop the rest.
    size_counts = collections.Counter(p["size"] for p in candidates)
    attachments, seen_sizes = [], set()
    for p in candidates:
        repeated = size_counts[p["size"]] >= 3 and p["mimeType"].startswith("image/")
        if repeated and p["size"] in seen_sizes:
            continue
        seen_sizes.add(p["size"])
        attachments.append({**p, "likely_signature": repeated})
    small_images = [p for p in parts if p["filename"] and p["size"] <= 20_000]
    return {
        "id": m["id"], "threadId": m.get("threadId"), "date_utc": when.isoformat() if when else None,
        "hour_local": (when.astimezone(dt.timezone(dt.timedelta(hours=-4))).hour if when else None),
        "from_domain": domain, "from_is_internal": domain == INTERNAL_DOMAIN, "via_group_rewrite": via_group,
        "from_looks_automated": any(h in frm_addr for h in AUTOMATION_HINTS),
        "to_group_direct": group.lower() in headers.get("to", "").lower(), "group_in_cc": group.lower() in headers.get("cc", "").lower(),
        "subject_prefix": (re.match(r"^\s*((?:re|fw|fwd)\s*:)", subject, re.I) or [None, None])[1],
        "subject_kind": classify_name(subject), "subject_has_load_number": bool(LOAD_RE.search(subject)),
        "subject_load_numbers": LOAD_RE.findall(subject)[:3],
        "is_reply": bool(headers.get("in-reply-to") or headers.get("references")),
        "labels": m.get("labelIds", []),
        "attachment_count": sum(1 for a in attachments if not a["likely_signature"]), "small_image_count": len(small_images),
        "likely_signature_images": sum(1 for a in attachments if a["likely_signature"]),
        "attachments": [{"filename": a["filename"], "mimeType": a["mimeType"], "size": a["size"], "kind": classify_name(a["filename"]),
                         "likely_signature": a["likely_signature"],
                         "is_pdf": a["mimeType"] == "application/pdf" or a["filename"].lower().endswith(".pdf"),
                         "is_image": a["mimeType"] in IMAGE_MIMES or a["filename"].lower().endswith((".jpg", ".jpeg", ".png", ".heic"))} for a in attachments if not a["likely_signature"]],
        "snippet_has_load_number": bool(LOAD_RE.search(m.get("snippet") or "")),
    }


def pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.0f}%" if d else "n/a"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="ratecon@circledelivers.com")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max", type=int, default=600)
    ap.add_argument("--query", default=None, help="override the Gmail query (default: to:<group> newer_than:<days>d)")
    ap.add_argument("--user", default=None, help="member mailbox to impersonate (default: PAYBOT_GMAIL_USER from the paystatus .env)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "out" / "mail-survey"))
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    from pod_intake.localenv import load_local_env
    load_local_env()                                      # project .env first, payment-bot .env as fallback
    user = args.user or os.environ.get("PAYBOT_GMAIL_USER")
    sa_path = Path(os.environ.get("PAYBOT_GOOGLE_SA_FILE", ""))
    info = load_service_account_info(file_path=str(sa_path))
    tokens = ServiceAccountTokenSource(info, subject=user, scopes=GMAIL_READONLY_SCOPES)
    token = tokens.token()
    profile = gget(token, f"/users/{urllib.parse.quote(user)}/profile")
    print(f"delegation OK: reading as {profile.get('emailAddress')} ({profile.get('messagesTotal'):,} messages in mailbox)")

    def list_ids(q: str, cap: int | None = None) -> list[dict]:
        found: list[dict] = []
        page = None
        while True:
            params = {"q": q, "maxResults": "500"}
            if page:
                params["pageToken"] = page
            listing = gget(token, f"/users/{urllib.parse.quote(user)}/messages", params)
            found.extend(listing.get("messages") or [])
            page = listing.get("nextPageToken")
            if not page or (cap and len(found) >= cap):
                break
        return found

    # Count every day cheaply (ids only), then sample evenly within each day so one busy morning cannot
    # crowd out the rest of the window. Gmail's after:/before: take YYYY/MM/DD in the mailbox's time zone.
    daily: list[dict] = []
    ids: list[dict] = []
    if args.query:
        ids = list_ids(args.query, args.max)
        query = args.query
    else:
        query = f"to:{args.group} newer_than:{args.days}d"
        per_day_cap = max(20, args.max // args.days)
        today = dt.date.today()
        for back in range(args.days, 0, -1):
            day = today - dt.timedelta(days=back - 1)
            q = f"to:{args.group} after:{(day - dt.timedelta(days=1)).strftime('%Y/%m/%d')} before:{(day + dt.timedelta(days=1)).strftime('%Y/%m/%d')}"
            day_ids = list_ids(q)
            # after:/before: are date-granular and overlap by design; de-duplicate across days later by id
            step = max(1, len(day_ids) // per_day_cap)
            sample = day_ids[::step][:per_day_cap]
            daily.append({"day": day.isoformat(), "weekday": day.strftime("%a"), "messages": len(day_ids), "sampled": len(sample)})
            ids.extend(sample)
        seen = set(); ids = [x for x in ids if not (x["id"] in seen or seen.add(x["id"]))]
        print("daily volume (all messages to the group):")
        for d in daily:
            print(f"  {d['day']} {d['weekday']}: {d['messages']:>5} messages, sampled {d['sampled']}")
    print(f"query: {query!r} -> {len(ids)} messages to fetch")
    if not ids:
        print("No messages matched. Either the impersonated user is not a member of the group, or the window is empty.")
        return 1

    def fetch(mid: str) -> dict:
        m = gget(tokens.token(), f"/users/{urllib.parse.quote(user)}/messages/{mid}", {"format": "full"})
        return summarize_message(m, args.group)

    records: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, rec in enumerate(ex.map(fetch, [x["id"] for x in ids]), start=1):
            records.append(rec)
            if i % 100 == 0:
                print(f"  ... {i}/{len(ids)}")

    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")      # date + time: two samples on one day must not overwrite each other (14 Sep 2026 lost the morning file)
    slug = args.group.split("@")[0]
    (out_dir / f"{slug}_{stamp}.json").write_text(json.dumps({"query": query, "user": user, "count": len(records), "daily": daily, "records": records}, indent=1), encoding="utf-8")

    # thread context: did the thread start with an internal (rate confirmation) message?
    by_thread: dict[str, list[dict]] = collections.defaultdict(list)
    for r in records:
        by_thread[r["threadId"]].append(r)
    for tid, msgs in by_thread.items():
        msgs.sort(key=lambda r: r["date_utc"] or "")
        first_internal = msgs[0]["from_is_internal"]
        for r in msgs:
            r["thread_started_internal"] = first_internal

    # ---------------- aggregate ----------------
    N = len(records)
    threads = collections.Counter(r["threadId"] for r in records)
    with_att = [r for r in records if r["attachment_count"]]
    external = [r for r in records if not r["from_is_internal"]]
    ext_with_att = [r for r in external if r["attachment_count"]]
    dom = collections.Counter(r["from_domain"] for r in records)
    dom_att = collections.Counter(r["from_domain"] for r in with_att)
    att_kinds = collections.Counter(a["kind"] for r in records for a in r["attachments"])
    att_types = collections.Counter(("pdf" if a["is_pdf"] else "image" if a["is_image"] else a["mimeType"] or "other") for r in records for a in r["attachments"])
    subj_kind = collections.Counter(r["subject_kind"] for r in records)
    prefix = collections.Counter((r["subject_prefix"] or "(none)").upper().replace(" ", "") for r in records)
    hours = collections.Counter(r["hour_local"] for r in records if r["hour_local"] is not None)
    per_att = collections.Counter(min(r["attachment_count"], 5) for r in records)

    def count(pred) -> int:
        return sum(1 for r in records if pred(r))

    doc_like = lambda r: any(a["kind"] in ("pod", "bol", "paperwork_generic", "lumper", "scale") or (a["is_image"]) for a in r["attachments"])
    ext_att = lambda r: r["attachment_count"] > 0 and not r["from_is_internal"]
    only_ratecon = lambda r: all(a["kind"] == "rate_confirmation" for a in r["attachments"])
    filters = [
        ("has:attachment (signature images removed)", lambda r: r["attachment_count"] > 0),
        ("has:attachment -from:circledelivers.com", ext_att),
        ("- minus messages whose only attachments are TransportPro rate confirmations (<id>_23.pdf)", lambda r: ext_att(r) and not only_ratecon(r)),
        ("- and at least one PDF or photo attachment", lambda r: ext_att(r) and not only_ratecon(r) and any(a["is_pdf"] or a["is_image"] for a in r["attachments"])),
        ("- and the message replies in a thread Circle started (the rate-con chain)", lambda r: ext_att(r) and not only_ratecon(r) and r.get("thread_started_internal")),
        ("- and subject or filename says BOL/POD/paperwork (rarely true; names are generic)", lambda r: ext_att(r) and (r["subject_kind"] in ("pod", "bol", "paperwork_generic") or doc_like(r))),
        ("- and a load number is in the subject", lambda r: ext_att(r) and r["subject_has_load_number"]),
    ]

    lines = [f"# Mail survey: {args.group}", "", f"Query `{query}` read as {user} on {dt.date.today()}. {N} sampled messages in {len(threads)} threads. Headers and attachment names only; no bodies stored.", ""]
    if daily:
        lines += ["## Daily volume (every message to the group, counted from ids)", "", "| Day | Messages | Sampled |", "|---|---|---|"] + [f"| {d['day']} {d['weekday']} | {d['messages']:,} | {d['sampled']} |" for d in daily] + [f"| **Total** | **{sum(d['messages'] for d in daily):,}** | {sum(d['sampled'] for d in daily)} |", ""]
    lines += ["## Shape of the traffic (sample)", "", "| Measure | Count | Share |", "|---|---|---|",
              f"| Messages | {N} | 100% |",
              f"| - from outside Circle | {len(external)} | {pct(len(external), N)} |",
              f"| - From rewritten by Google Groups ('via') | {count(lambda r: r['via_group_rewrite'])} | {pct(count(lambda r: r['via_group_rewrite']), N)} |",
              f"| - replies inside an existing thread | {count(lambda r: r['is_reply'])} | {pct(count(lambda r: r['is_reply']), N)} |",
              f"| - group in To (vs only Cc) | {count(lambda r: r['to_group_direct'])} | {pct(count(lambda r: r['to_group_direct']), N)} |",
              f"| - with at least one real attachment (>20 KB, signatures removed) | {len(with_att)} | {pct(len(with_att), N)} |",
              f"| - external AND with attachment | {len(ext_with_att)} | {pct(len(ext_with_att), N)} |",
              f"| - external with attachment, replying in a thread Circle started | {count(lambda r: (not r['from_is_internal']) and r['attachment_count'] and r.get('thread_started_internal'))} | {pct(count(lambda r: (not r['from_is_internal']) and r['attachment_count'] and r.get('thread_started_internal')), max(1, len(ext_with_att)))} of those |",
              f"| - messages where repeated signature images were removed | {count(lambda r: r['likely_signature_images'])} | {pct(count(lambda r: r['likely_signature_images']), N)} |",
              f"| - 7-digit load number in subject | {count(lambda r: r['subject_has_load_number'])} | {pct(count(lambda r: r['subject_has_load_number']), N)} |",
              f"| - load number in subject or first lines | {count(lambda r: r['subject_has_load_number'] or r['snippet_has_load_number'])} | {pct(count(lambda r: r['subject_has_load_number'] or r['snippet_has_load_number']), N)} |",
              f"| - sender looks automated | {count(lambda r: r['from_looks_automated'])} | {pct(count(lambda r: r['from_looks_automated']), N)} |",
              f"| Threads with more than one sampled message | {sum(1 for t, c in threads.items() if c > 1)} | of {len(threads)} threads |", ""]
    lines += ["## Subject prefixes", "", "| Prefix | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in prefix.most_common()] + [""]
    lines += ["## What the subject line is about (keyword guess)", "", "| Kind | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in subj_kind.most_common()] + [""]
    lines += ["## Attachments", "", f"Messages by attachment count: " + ", ".join(f"{k if k < 5 else '5+'}: {v}" for k, v in sorted(per_att.items())), "",
              "| Attachment type | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in att_types.most_common()] + ["",
              "| Attachment name suggests | Count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in att_kinds.most_common()] + [""]
    lines += ["## Top sender domains (all messages / with attachments)", "", "| Domain | Messages | With attachment |", "|---|---|---|"] + [f"| {d or '(none)'} | {c} | {dom_att.get(d, 0)} |" for d, c in dom.most_common(20)] + [""]
    lines += ["## Time of day (Eastern)", "", "| Hour | Messages |", "|---|---|"] + [f"| {h:02d}:00 | {c} |" for h, c in sorted(hours.items())] + [""]
    lines += ["## Candidate filters and what each keeps", "", "| Filter | Messages kept | Share of all | Share of external-with-attachment |", "|---|---|---|---|"]
    for name, pred in filters:
        k = count(pred)
        lines.append(f"| {name} | {k} | {pct(k, N)} | {pct(k, len(ext_with_att))} |")
    lines += ["", "## Sample of external messages with attachments (names only)", "", "| Date | Domain | Subject kind | Prefix | Attachments |", "|---|---|---|---|---|"]
    for r in sorted(ext_with_att, key=lambda r: r["date_utc"] or "", reverse=True)[:25]:
        lines.append(f"| {(r['date_utc'] or '')[:10]} | {r['from_domain']} | {r['subject_kind']} | {r['subject_prefix'] or ''} | " + "; ".join(f"{a['filename']} ({a['kind']}, {a['size']//1024} KB)" for a in r["attachments"])[:160] + " |")
    md = "\n".join(lines)
    (out_dir / f"{slug}_{stamp}.md").write_text(md, encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("\n" + md)
    print(f"\nsaved {out_dir / f'{slug}_{stamp}.json'} and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
