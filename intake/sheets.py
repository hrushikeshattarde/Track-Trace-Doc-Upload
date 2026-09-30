"""The pod's Upload log: one tab of a Google Sheet, one row per BOL or POD the bot decided about.

Written as the Gmail service account itself (no `sub`, so no mailbox is impersonated); the sheet is
shared with that account's address as an editor. Only the Sheets values API is used.

Rows are found again by their Ref column (Q), never by row number, so a pod lead who sorts or
filters the tab cannot make the bot overwrite somebody else's row. Columns O and P - Correct? and
Pod note - belong to the pod and the bot never writes them, not even when it updates a row's status.
"""
from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"
TAB = "Upload log"
COLUMNS = ["Logged (ET)", "Load", "Customer", "Pod", "Document(s)", "Arrived by", "AI read it as", "How sure",
           "Matches TransportPro on", "Checks", "Upload as", "Comment", "TransportPro file", "Status",
           "Correct? (pod)", "Pod note", "Ref"]
BOT_COLUMNS = 14            # A..N: what the bot writes on every row
GROW_BY = 500               # rows added when the tab runs out of grid


class SheetError(RuntimeError):
    pass


def template_tab(titles: list[str], new: str) -> str | None:
    """The tab a new pod tab is styled like: the first other tab whose name starts with the log's
    ("Upload Log - Saiz" for "Upload Log - Klinger"), else the default tab if it is there."""
    stem = TAB.split(" - ")[0].strip().lower()
    for t in titles:
        if t != new and t.strip().lower().startswith(stem):
            return t
    return None


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
        """(sheetId, rowCount) of the tab, creating it with its header row if it is not there. A new
        tab is styled like the pod tab already there (`template_tab`), so every pod's log looks the
        same: the pod formatted the first one by hand (30 Sep 2026)."""
        meta = self._call("GET", "?fields=sheets.properties")
        titles = []
        for s in meta.get("sheets", []):
            p = s.get("properties") or {}
            titles.append(str(p.get("title") or ""))
            if p.get("title") == self.tab:
                return p.get("sheetId"), int((p.get("gridProperties") or {}).get("rowCount") or 1000)
        made = self._call("POST", ":batchUpdate", {"requests": [{"addSheet": {"properties": {
            "title": self.tab, "gridProperties": {"frozenRowCount": 1}}}}]})
        self._call("PUT", f"/values/{self._a1('A1')}?valueInputOption=RAW", {"values": [COLUMNS]})
        props = ((made.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {}
        template = template_tab(titles, self.tab)
        if template:
            self.style_like(template)
        return props.get("sheetId"), int((props.get("gridProperties") or {}).get("rowCount") or 1000)

    def style_like(self, template: str, tab: str | None = None) -> None:
        """Give `tab` (the log's own by default) the look of `template`: cell formats and the
        Correct? dropdown (PASTE_FORMAT, PASTE_DATA_VALIDATION), the red and green status rules (PASTE_CONDITIONAL_FORMATTING),
        the column widths, and a frozen header row. Values are not touched."""
        with self._on(tab):
            meta = self._call("GET", "?fields=" + urllib.parse.quote(
                "sheets(properties(title,sheetId,gridProperties),data(columnMetadata(pixelSize)))")
                + "&ranges=" + self._a1_of(template, "A1:Q1") + "&ranges=" + self._a1("A1:Q1"))
            sheets_ = {(s.get("properties") or {}).get("title"): s for s in meta.get("sheets", [])}
            src, dst = sheets_.get(template), sheets_.get(self.tab)
            if not src or not dst:
                return
            sid, did = src["properties"]["sheetId"], dst["properties"]["sheetId"]
            rows = min(int((src["properties"].get("gridProperties") or {}).get("rowCount") or 1000),
                       int((dst["properties"].get("gridProperties") or {}).get("rowCount") or 1000))
            cols = len(COLUMNS)
            box = lambda i: {"sheetId": i, "startRowIndex": 0, "endRowIndex": rows, "startColumnIndex": 0, "endColumnIndex": cols}
            requests = [
                {"copyPaste": {"source": box(sid), "destination": box(did), "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL"}},
                {"copyPaste": {"source": box(sid), "destination": box(did), "pasteType": "PASTE_CONDITIONAL_FORMATTING", "pasteOrientation": "NORMAL"}},
                {"copyPaste": {"source": box(sid), "destination": box(did), "pasteType": "PASTE_DATA_VALIDATION", "pasteOrientation": "NORMAL"}},
                {"updateSheetProperties": {"properties": {"sheetId": did, "gridProperties": {"frozenRowCount": 1}},
                                           "fields": "gridProperties.frozenRowCount"}},
            ]
            widths = [c.get("pixelSize") for d in src.get("data", []) for c in d.get("columnMetadata", [])]
            for i, w in enumerate(widths[:cols]):
                if w:
                    requests.append({"updateDimensionProperties": {
                        "range": {"sheetId": did, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                        "properties": {"pixelSize": int(w)}, "fields": "pixelSize"}})
            self._call("POST", ":batchUpdate", {"requests": requests})

    def _a1_of(self, tab: str, cells: str) -> str:
        return urllib.parse.quote(f"'{tab}'!{cells}")

    @contextlib.contextmanager
    def _on(self, tab: str | None):
        """Address another tab for one call: a pod with a tab of its own (Jesse Klingler's pod,
        "Upload Log - Klinger", 30 Sep 2026). None means the log's own tab."""
        if not tab or tab == self.tab:
            yield
            return
        was, self.tab = self.tab, tab
        try:
            yield
        finally:
            self.tab = was

    def ensure(self, tab: str | None = None) -> None:
        """Create the tab, with its header row, if it is not there yet."""
        with self._on(tab):
            self._grid()

    def write(self, entries: list[tuple[str, list]], tab: str | None = None) -> int:
        """Write (ref, [14 values for A..N]) entries: a ref already on the tab is updated in place,
        a new one goes on the first empty row. Returns how many rows were written."""
        if not entries:
            return 0
        with self._on(tab):
            return self._write(entries)

    def _write(self, entries: list[tuple[str, list]]) -> int:
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


    def remove(self, refs: set[str], tab: str | None = None) -> int:
        """Delete the rows carrying these Refs, bottom up so the row numbers stay right. A row the pod
        has marked - anything in O (Correct?) or P (Pod note) - is kept: that is their work."""
        if not refs:
            return 0
        with self._on(tab):
            return self._remove(refs)

    def _remove(self, refs: set[str]) -> int:
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
