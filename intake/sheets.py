"""The pod's Upload log: one tab of a Google Sheet, one row per BOL or POD the bot decided about.

Written as the Gmail service account itself (no `sub`, so no mailbox is impersonated); the sheet is
shared with that account's address as an editor. Only the Sheets values API is used.

Rows are found again by their Ref column (Q), never by row number, so a pod lead who sorts or
filters the tab cannot make the bot overwrite somebody else's row. Columns O and P - Correct? and
Pod note - belong to the pod and the bot never writes them, not even when it updates a row's status.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"
TAB = "Upload log"
COLUMNS = ["Logged (UTC)", "Load", "Customer", "Pod", "Document(s)", "Arrived by", "AI read it as", "How sure",
           "Matches TransportPro on", "Checks", "Upload as", "Comment", "TransportPro file", "Status",
           "Correct? (pod)", "Pod note", "Ref"]
BOT_COLUMNS = 14            # A..N: what the bot writes on every row
GROW_BY = 500               # rows added when the tab runs out of grid


class SheetError(RuntimeError):
    pass


class UploadLog:
    def __init__(self, spreadsheet_id: str, token: Callable[[], str], *, tab: str = TAB,
                 opener: Callable[..., Any] | None = None) -> None:
        self.id = spreadsheet_id
        self._token = token
        self.tab = tab
        self._open = opener or urllib.request.urlopen
        self.calls = 0

    # -- transport ---------------------------------------------------------
    def _call(self, method: str, path: str, payload: Any = None) -> dict:
        req = urllib.request.Request(
            f"{API}/{self.id}{path}", method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with self._open(req, timeout=60) as r:
                    self.calls += 1
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise SheetError(f"Sheets {e.code} on {method} {path[:60]}: "
                                 f"{e.read().decode('utf-8', 'replace')[:200]}") from None
        raise SheetError(f"Sheets retries exhausted on {method} {path[:60]}")

    def _a1(self, cells: str) -> str:
        return urllib.parse.quote(f"'{self.tab}'!{cells}")

    # -- the tab -----------------------------------------------------------
    def _grid(self) -> tuple[int | None, int]:
        """(sheetId, rowCount) of the tab, creating it with its header row if it is not there."""
        meta = self._call("GET", "?fields=sheets.properties")
        for s in meta.get("sheets", []):
            p = s.get("properties") or {}
            if p.get("title") == self.tab:
                return p.get("sheetId"), int((p.get("gridProperties") or {}).get("rowCount") or 1000)
        made = self._call("POST", ":batchUpdate", {"requests": [{"addSheet": {"properties": {
            "title": self.tab, "gridProperties": {"frozenRowCount": 1}}}}]})
        self._call("PUT", f"/values/{self._a1('A1')}?valueInputOption=RAW", {"values": [COLUMNS]})
        props = ((made.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {}
        return props.get("sheetId"), int((props.get("gridProperties") or {}).get("rowCount") or 1000)

    def write(self, entries: list[tuple[str, list]]) -> int:
        """Write (ref, [14 values for A..N]) entries: a ref already on the tab is updated in place,
        a new one goes on the first empty row. Returns how many rows were written."""
        if not entries:
            return 0
        sheet_id, grid_rows = self._grid()
        refs = self._call("GET", f"/values/{self._a1('Q:Q')}").get("values", [])
        where = {row[0]: i + 1 for i, row in enumerate(refs) if row and row[0]}
        used = len(self._call("GET", f"/values/{self._a1('A:A')}").get("values", []))
        last = max(used, len(refs), 1)
        data = []
        for ref, values in entries:
            row = where.get(ref)
            if row is None:
                last += 1
                row = where[ref] = last
            data.append({"range": f"'{self.tab}'!A{row}:N{row}",
                         "values": [[_cell(v) for v in values[:BOT_COLUMNS]]]})
            data.append({"range": f"'{self.tab}'!Q{row}", "values": [[ref]]})
        if last > grid_rows and sheet_id is not None:
            self._call("POST", ":batchUpdate", {"requests": [{"appendDimension": {
                "sheetId": sheet_id, "dimension": "ROWS", "length": max(GROW_BY, last - grid_rows)}}]})
        self._call("POST", "/values:batchUpdate", {"valueInputOption": "RAW", "data": data})
        return len(entries)


    def remove(self, refs: set[str]) -> int:
        """Delete the rows carrying these Refs, bottom up so the row numbers stay right. A row the pod
        has marked - anything in O (Correct?) or P (Pod note) - is kept: that is their work."""
        if not refs:
            return 0
        sheet_id, _ = self._grid()
        values = self._call("GET", f"/values/{self._a1('A:Q')}").get("values", [])
        doomed = []
        for i, row in enumerate(values):
            row = row + [""] * (17 - len(row))
            if i and row[16] in refs and not (str(row[14]).strip() or str(row[15]).strip()):
                doomed.append(i)
        if doomed and sheet_id is not None:
            self._call("POST", ":batchUpdate", {"requests": [
                {"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": i, "endIndex": i + 1}}}
                for i in sorted(doomed, reverse=True)]})
        return len(doomed)


def _cell(v: Any) -> Any:
    return "" if v is None else v


def from_service_account(spreadsheet_id: str, info: dict, *, tab: str = TAB) -> UploadLog:
    """An Upload log signed in as the service account in `info` - the Gmail key, without `sub`."""
    from .gmail import Delegated
    signer = Delegated(info, subject="", scopes=(SCOPE,))
    return UploadLog(spreadsheet_id, signer.token, tab=tab)
