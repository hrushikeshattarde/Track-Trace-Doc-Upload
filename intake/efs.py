"""The EFS payment requests on the fwefs@ chain, read from the mail the collector stores.

A pod raises an EFS (or Relay) payment - a lumper fee, most often - through a form. The form mails
fwefs@circledelivers.com as "EFS Request for <load>", one labelled line per field, and whoever issues
the code replies on the thread with the Money Transfer code, the Report Reference number and the
amount. Measured 14 Oct 2026 over the 12 messages the chain had ever carried: no attachment came on
it. What it carries is what the pod paid for the load - the reason, the amount, the PO, the payee -
which is exactly what ties a lumper receipt to its load when the receipt names none of TransportPro's
numbers (intake.autofile.judge_lumper). mailsync records each request and its payment in the ledger's
efs_request table; nothing here calls Gmail.
"""
from __future__ import annotations

import email
import html as html_mod
import re
from email import policy

SUBJECT_RE = re.compile(r"\bEFS Request for (\d{7})\b", re.I)
# The form's lines, as the 14 Oct 2026 requests print them, to the ledger's columns.
FIELDS = {"name of requester": "requester", "efs reason": "reason", "payee's name": "payee",
          "payment amount": "amount", "carrier's name": "carrier", "po #": "po", "efs or relay": "channel",
          "additional notes": "notes"}
FIELD_RE = re.compile(r"^\s*(" + "|".join(re.escape(k) for k in FIELDS) + r")\s*:\s*(.*?)\s*$", re.I)
CODE_RE = re.compile(r"Money Transfer code\W{0,3}(\d{6,})", re.I)
REFERENCE_RE = re.compile(r"Report Reference\W{0,4}(\d{6,})", re.I)
AMOUNT_RE = re.compile(r"\bAmount\W{0,3}\$?\s*([\d,]+(?:\.\d{1,2})?)", re.I)
QUOTED_RE = re.compile(r"^\s*>")
WROTE_RE = re.compile(r"^\s*On .{5,160} wrote:\s*$")
BLANK = ("", "n/a", "na", "none", "(blank)", "-")


def request_load(subject: str | None) -> int | None:
    """The load an EFS request's subject names, or None for any other mail."""
    m = SUBJECT_RE.search(subject or "")
    return int(m.group(1)) if m else None


def text_of(raw: bytes) -> str:
    """The message's own text: its text/plain parts, else its HTML with the tags stripped."""
    parsed = email.message_from_bytes(raw, policy=policy.default)
    plain: list[str] = []
    rich: list[str] = []
    for p in parsed.walk():
        if p.is_multipart():
            continue
        try:
            content = p.get_content()
        except Exception:                                    # noqa: BLE001 - an undecodable part is not text
            continue
        if not isinstance(content, str):
            continue
        if p.get_content_type() == "text/plain":
            plain.append(content)
        elif p.get_content_type() == "text/html":
            rich.append(content)
    if plain:
        return "\n".join(plain)
    text = "\n".join(rich)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html_mod.unescape(text)


def own_lines(text: str) -> list[str]:
    """The lines the sender wrote: quoted lines are dropped, and everything after an
    "On ... wrote:" line is an earlier message."""
    out: list[str] = []
    for line in (text or "").replace("’", "'").splitlines():
        if WROTE_RE.match(line):
            break
        if QUOTED_RE.match(line):
            continue
        out.append(line)
    return out


def parse_request(text: str) -> dict | None:
    """The form's fields from a request, or None when the text is not the form (a reply, say): it
    must at least say the reason and the amount."""
    found: dict[str, str | None] = {}
    for line in own_lines(text):
        m = FIELD_RE.match(line)
        if m:
            found.setdefault(FIELDS[m.group(1).lower()], m.group(2).strip())
    if not found.get("reason") or not found.get("amount"):
        return None
    found["amount"] = money(found["amount"]) or found["amount"]
    for key in ("po", "notes", "payee", "carrier"):
        if str(found.get(key) or "").strip().lower() in BLANK:
            found[key] = None
    return found


def parse_payment(text: str) -> dict:
    """The code, the reference and the amount from the reply that issued the payment; empty when the
    text carries none."""
    own = "\n".join(own_lines(text))
    out: dict[str, str] = {}
    m = CODE_RE.search(own)
    if m:
        out["code"] = m.group(1)
    m = REFERENCE_RE.search(own)
    if m:
        out["reference"] = m.group(1)
    m = AMOUNT_RE.search(own)
    if m and money(m.group(1)):
        out["amount"] = money(m.group(1))
    return out


def money(value) -> str | None:
    """'$1,250.5' -> '1250.50'; None when the text holds no amount."""
    s = re.sub(r"[^\d.]", "", str(value or ""))
    if not s or s.count(".") > 1 or not any(c.isdigit() for c in s):
        return None
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return None
