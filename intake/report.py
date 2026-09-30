"""The morning report: the loads the bot did not file, one line each.

Every morning at 07:00 Eastern the report Lambda (intake/aws_report.py) reads the ledger and mails the
pods' leads what the bot held back in the last day, and which delivered loads are still waiting for
documents with nothing of the bot's on them. It is the sheet's HELD rows, condensed to one line per
load, sent to the inbox (the user's ask, 30 Sep 2026).

Two sections:

1. Held in the last 24 hours - every load with a document the bot held, the documents counted, and
   the reason in the bot's own words (the sheet's Status without its "HELD - " and "a person should
   look" wrapping).
2. Delivered, still waiting for documents - loads on the pilot pods that TransportPro shows delivered
   and Waiting for Documents, where the bot has filed no POD. A POD may have come to a person by
   another route; the line says only that the bot has nothing.

Plain text, so it reads the same in every mail client. Nothing here writes to the ledger.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field

from . import autofile

SHEET_URL = "https://docs.google.com/spreadsheets/d/{id}"


@dataclass
class Report:
    subject: str
    text: str
    held_loads: int = 0
    held_documents: int = 0
    waiting_loads: int = 0
    lines: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.held_loads == 0 and self.waiting_loads == 0


def reason(status: str) -> str:
    """'HELD - AI only 82% sure (needs 85%); 0 fact(s) match...; not uploaded, a person should look'
    -> 'AI only 82% sure (needs 85%); 0 fact(s) match...'"""
    s = str(status or "")
    if s.upper().startswith("HELD - "):
        s = s[7:]
    for tail in ("; not uploaded, a person should look", "; a person should check File History"):
        if s.endswith(tail):
            s = s[: -len(tail)]
    return s.strip()


def build(conn: sqlite3.Connection, *, now: dt.datetime | None = None, hours: int = 24,
          terminals: frozenset[int] | set[int] = frozenset(), pods: dict[int, str] | None = None,
          sheet_id: str = "") -> Report:
    """The report as of `now` (UTC), covering the last `hours` for the pilot `terminals`."""
    pods = pods or {}
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(terminals)) or "NULL"
    terms = tuple(sorted(int(t) for t in terminals))

    # 1. Held in the window, one line per load.
    held: dict[int, dict] = {}
    rows = conn.execute(
        "SELECT a.load_id, a.status, a.row_json, a.decided_at, l.terminal, l.customer FROM autofile a "
        "LEFT JOIN load l ON l.load_id = a.load_id "
        f"WHERE a.outcome = ? AND a.decided_at >= ? AND (l.terminal IN ({marks}) OR l.terminal IS NULL) "
        "ORDER BY a.decided_at", (autofile.HELD, since, *terms)).fetchall()
    for r in rows:
        row = json.loads(r["row_json"]) if r["row_json"] else []
        h = held.setdefault(int(r["load_id"]), {
            "pod": (row[3] if len(row) > 3 and row[3] else pods.get(int(r["terminal"] or 0), str(r["terminal"] or ""))),
            "customer": (row[2] if len(row) > 2 and row[2] else r["customer"]) or "",
            "docs": 0, "reasons": [], "when": r["decided_at"]})
        h["docs"] += 1
        why = reason(r["status"])
        if why and why not in h["reasons"]:
            h["reasons"].append(why)
    held_lines = []
    for load_id, h in sorted(held.items()):
        what = "1 document" if h["docs"] == 1 else f"{h['docs']} documents"
        held_lines.append(f"{load_id}  ({h['pod']}, {_short(h['customer'])}): {what} held - {' | '.join(h['reasons'])}")

    # 2. Delivered, still waiting, nothing of the bot's filed as the POD.
    waiting = conn.execute(
        "SELECT load_id, terminal, customer, state, last_checked_at FROM load "
        f"WHERE in_view = 1 AND terminal IN ({marks}) AND lower(stage) = 'delivered' "
        "AND doc_status = 'Waiting for Documents' "
        "AND load_id NOT IN (SELECT load_id FROM filing WHERE document_type = 'Bill Of Lading') "
        "ORDER BY terminal, load_id", terms).fetchall()
    waiting_lines = []
    for r in waiting:
        pod = pods.get(int(r["terminal"] or 0), str(r["terminal"] or ""))
        on_it = "held above" if int(r["load_id"]) in held else "nothing from the bot"
        waiting_lines.append(f"{int(r['load_id'])}  ({pod}, {_short(r['customer'] or '')}): delivered, still Waiting for "
                             f"Documents - {on_it}; last checked {autofile.eastern(r['last_checked_at'])}")

    day = autofile.eastern(now, suffix=False)[:10]
    subject = (f"Doc Intake: {len(held)} load(s) held, {len(waiting)} delivered and waiting - {day}"
               if held or waiting else f"Doc Intake: nothing held - {day}")
    out = [f"Doc Intake Bot - loads not filed, {autofile.eastern(now)} (last {hours} hours)", ""]
    out.append(f"HELD IN THE LAST {hours} HOURS - {len(held)} load(s), {sum(h['docs'] for h in held.values())} document(s)")
    out += held_lines or ["  none"]
    out += ["", f"DELIVERED, STILL WAITING FOR DOCUMENTS, NO POD FROM THE BOT - {len(waiting)} load(s)"]
    out += waiting_lines or ["  none"]
    out += ["", "Every held document is on the Upload Log sheet with the page it read, what matched and why it was held."]
    if sheet_id:
        out.append(SHEET_URL.format(id=sheet_id))
    out += ["", "Sent by the Doc Intake bot. Times are US Eastern."]
    return Report(subject=subject, text="\n".join(out), held_loads=len(held),
                  held_documents=sum(h["docs"] for h in held.values()), waiting_loads=len(waiting),
                  lines=held_lines + waiting_lines)


def _short(customer: str, n: int = 38) -> str:
    c = str(customer or "").strip()
    return c if len(c) <= n else c[: n - 3].rstrip() + "..."
