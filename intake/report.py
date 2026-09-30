"""The morning report: the loads the bot did not file, one line each.

Every morning at 07:00 Eastern the report Lambda (intake/aws_report.py) reads the ledger and mails the
pods' leads the loads the bot did not upload in the last day: the sheet's HELD rows, condensed to one
line per load - the documents counted and the reason in the bot's own words (the sheet's Status
without its "HELD - " and "a person should look" wrapping). Nothing else: the user asked for exactly
the sheet's held loads, not loads waiting for documents or anything TransportPro-side (30 Sep 2026).

The sheet is the pods' working surface, so the report follows it (`sheet_status`, by Ref): a held
document whose row a person has removed, or rewritten to say it was filed by hand, is left out and
counted in a footnote; a row the pod has marked in Correct? shows the mark. The first sample had
listed 20 texted pages on 2597516 that had been filed by hand as one POD that afternoon.

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
    resolved: int = 0                 # held in the window, but since filed or cleared by a person
    lines: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.held_loads == 0


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
          sheet_id: str = "", sheet_status: dict[str, tuple[str, str]] | None = None) -> Report:
    """The report as of `now` (UTC), covering the last `hours` for the pilot `terminals`. With
    `sheet_status` (Ref -> (Status, Correct?), sheets.UploadLog.statuses) the held section follows
    the sheet: rows a person removed or rewrote are left out."""
    pods = pods or {}
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(terminals)) or "NULL"
    terms = tuple(sorted(int(t) for t in terminals))

    # 1. Held in the window, one line per load.
    held: dict[int, dict] = {}
    rows = conn.execute(
        "SELECT a.load_id, a.sha256, a.status, a.row_json, a.decided_at, l.terminal, l.customer FROM autofile a "
        "LEFT JOIN load l ON l.load_id = a.load_id "
        f"WHERE a.outcome = ? AND a.decided_at >= ? AND (l.terminal IN ({marks}) OR l.terminal IS NULL) "
        "ORDER BY a.decided_at", (autofile.HELD, since, *terms)).fetchall()
    resolved = 0
    for r in rows:
        ref = autofile.ref(int(r["load_id"]), r["sha256"])
        mark = ""
        if sheet_status is not None:
            on_sheet = sheet_status.get(ref)
            if on_sheet is None or not on_sheet[0].upper().startswith("HELD"):
                resolved += 1                  # removed, or filed by hand: the pod has dealt with it
                continue
            mark = on_sheet[1].strip()
        row = json.loads(r["row_json"]) if r["row_json"] else []
        h = held.setdefault(int(r["load_id"]), {
            "pod": (row[3] if len(row) > 3 and row[3] else pods.get(int(r["terminal"] or 0), str(r["terminal"] or ""))),
            "customer": (row[2] if len(row) > 2 and row[2] else r["customer"]) or "",
            "docs": 0, "reasons": [], "when": r["decided_at"], "marks": []})
        h["docs"] += 1
        why = reason(r["status"])
        if why and why not in h["reasons"]:
            h["reasons"].append(why)
        if mark and mark not in h["marks"]:
            h["marks"].append(mark)
    held_lines = []
    for load_id, h in sorted(held.items()):
        what = "1 document" if h["docs"] == 1 else f"{h['docs']} documents"
        note = f" (reviewed by the pod: {'; '.join(h['marks'])})" if h["marks"] else ""
        held_lines.append(f"{load_id}  ({h['pod']}, {_short(h['customer'])}): {what} held - {' | '.join(h['reasons'])}{note}")

    day = autofile.eastern(now, suffix=False)[:10]
    docs = sum(h["docs"] for h in held.values())
    subject = (f"Doc Intake: {len(held)} load(s) the bot did not upload - {day}" if held
               else f"Doc Intake: nothing held - {day}")
    out = [f"Doc Intake Bot - loads the bot did not upload, {autofile.eastern(now)} (last {hours} hours)", ""]
    out.append(f"HELD IN THE LAST {hours} HOURS - {len(held)} load(s), {docs} document(s)")
    out += held_lines or ["  none"]
    if resolved:
        out.append(f"  ({resolved} held document(s) since filed or cleared by a person are not listed)")
    out += ["", "Every held document is on the Upload Log sheet with the page it read, what matched and why it was held."]
    if sheet_id:
        out.append(SHEET_URL.format(id=sheet_id))
    out += ["", "Sent by the Doc Intake bot. Times are US Eastern."]
    return Report(subject=subject, text="\n".join(out), held_loads=len(held), held_documents=docs,
                  resolved=resolved, lines=held_lines)


def _short(customer: str, n: int = 38) -> str:
    c = str(customer or "").strip()
    return c if len(c) <= n else c[: n - 3].rstrip() + "..."
