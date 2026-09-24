"""TransportPro Public API, read-only and self-contained.

Same reason gmail.py is self-contained: readiness.py reaches the paystatus bot's client through a
hard-coded path to a folder on one laptop, which a server cannot have. The auth contract is the
paystatus bot's, which is proven against the live API:

  POST /auth with HTTP Basic  ->  {access_token, refresh_token}
  every other call            ->  Authorization: Bearer <access_token>
  on 401                      ->  refresh once, else re-login, then replay the request once

GETs, plus one write: upload_file, the POST /files/upload that intake/autofile.py and
filing.execute() file documents with. There is no call here that edits or deletes a file - the
Public API has none.

Endpoints, from the Public API Postman collection, with the quirks readiness.py established on
14 Sep 2026:
  GET /load/{id}                              (falls back to /voiceai/load/{id})
  GET /load/search                            terminalId + a MANDATORY pickup date range; paging is
                                              page=N, zero-based; the server-side documentStatus
                                              filter matches nothing for the dashboard's own wording,
                                              so document status is filtered client-side
  GET /dispatch/search?loadId=
  GET /files/search?recordType=loads&recordId=
  GET /files/{id}                             the same object, plus the bytes in a base64 fileData
                                              field (probed 18 Sep 2026). There is no raw-bytes
                                              endpoint and no URL in the search response
  GET /dispatch/{id}/getTextMessages
"""
from __future__ import annotations

import base64
import gzip
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Failures of the connection itself rather than answers from TransportPro: a dropped or reset
# connection, a timeout, a DNS blip. HTTPError is a URLError too, so every handler catches it first.
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError)

# Ask for an uncompressed body. TransportPro gzips its /auth and /load/search responses even when
# urllib advertises no encoding (seen 15 Sep 2026: a gzip magic number where JSON was expected),
# so _body() also decompresses defensively - the header is a request, not a guarantee.
JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json",
                "Accept-Encoding": "identity"}


GZIP_MAGIC = bytes((0x1F, 0x8B))


def _body(response) -> str:
    raw = response.read()
    if raw[:2] == GZIP_MAGIC:
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


class TProError(RuntimeError):
    def __init__(self, status: int, path: str, body: str = "") -> None:
        super().__init__(f"TransportPro {status} on {path}" + (f": {body[:200]}" if body else ""))
        self.status = status


class TransportPro:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30.0,
                 min_interval: float = 0.05) -> None:
        self.base = base_url.rstrip("/")
        self._user = username
        self._password = password
        self._timeout = timeout
        self._access: str | None = None
        self._refresh_token: str | None = None
        self._min_interval = min_interval
        self._last_call = 0.0
        self.calls = 0

    # -- auth --------------------------------------------------------------
    def _login(self) -> None:
        basic = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
        data = self._post("/auth", headers={**JSON_HEADERS, "Authorization": f"Basic {basic}"})
        self._store(data, "login")

    def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        try:
            data = self._post("/auth", body=json.dumps(
                {"grant_type": "refresh_token", "refresh_token": self._refresh_token}).encode())
        except TProError:
            return False
        self._store(data, "refresh")
        return True

    def _store(self, data: Any, context: str) -> None:
        if not isinstance(data, dict) or not data.get("access_token"):
            raise TProError(0, "/auth", f"{context} returned no access_token")
        self._access = str(data["access_token"])
        self._refresh_token = str(data["refresh_token"]) if data.get("refresh_token") else None

    def _post(self, path: str, *, headers: dict | None = None, body: bytes | None = None) -> Any:
        req = urllib.request.Request(f"{self.base}{path}", data=body, method="POST",
                                     headers=headers or dict(JSON_HEADERS))
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                return json.loads(_body(r) or "{}")
        except urllib.error.HTTPError as e:
            raise TProError(e.code, path, e.read().decode("utf-8", "replace")) from None
        except NETWORK_ERRORS as e:
            raise TProError(0, path, f"network: {type(e).__name__}: {e}") from None

    # -- transport ---------------------------------------------------------
    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        gap = self._min_interval - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        if self._access is None:
            self._login()
        url = f"{self.base}{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={**JSON_HEADERS, "Authorization": f"Bearer {self._access}"})
                with urllib.request.urlopen(req, timeout=self._timeout) as r:
                    self._last_call = time.time()
                    self.calls += 1
                    return json.loads(_body(r) or "null")
            except urllib.error.HTTPError as e:
                self._last_call = time.time()
                body = e.read().decode("utf-8", "replace")
                if e.code == 401 and attempt == 0:
                    if not self._refresh():
                        self._login()
                    continue
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise TProError(e.code, path, body) from None
            except NETWORK_ERRORS as e:
                # A dropped connection is not an answer, so it never reached the status handling
                # above: on 23 Sep 2026 one "Remote end closed connection without response" escaped
                # as a raw exception and stopped a whole check pass. Retried like a 503; if it
                # persists it becomes a TProError, which callers already handle - drain() defers
                # that one load and carries on.
                self._last_call = time.time()
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise TProError(0, path, f"network: {type(e).__name__}: {e}") from None
        raise TProError(0, path, "retries exhausted")

    @staticmethod
    def results(payload: Any) -> list[dict]:
        """Unwrap the {pagination, results} envelope. A bare list comes back as-is."""
        if payload is None:
            return []
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if not isinstance(payload, dict):
            return []
        rows = payload.get("results")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    # -- reads -------------------------------------------------------------
    def load(self, load_id: int) -> dict:
        """The singular /load/{id}. Some loads answer only on the Voice AI detail path."""
        try:
            return self.get(f"/load/{load_id}")
        except TProError as e:
            if e.status not in (400, 404):
                raise
        return self.get(f"/voiceai/load/{load_id}")

    def dispatches(self, load_id: int) -> list[dict]:
        return self.results(self.get("/dispatch/search", {"loadId": str(load_id)}))

    def files(self, load_id: int) -> list[dict]:
        return self.results(self.get("/files/search", {"recordType": "loads", "recordId": str(load_id)}))

    def download_file(self, file_id: int) -> tuple[bytes, dict]:
        """The bytes of a file already attached to a load, with its metadata.

        This is what lets the service look at paperwork it did not receive by email. 74 in-view
        loads carry nothing but a Driver Supplied BOL: the document is physically there and the
        status is stuck because of its type, and until now the service could see that the file
        existed but never what was on it.

        Free, like every other read here - it is the model that costs money, not the download. The
        response is the search object with a base64 `fileData` field added, so the metadata comes
        back with the bytes and the caller needs no second call to know the type it was filed under.
        """
        payload = self.get(f"/files/{int(file_id)}")
        raw = payload.get("fileData") if isinstance(payload, dict) else None
        if not raw:
            raise TProError(0, f"/files/{file_id}", "response carries no fileData")
        return base64.b64decode(raw), payload

    def text_messages(self, dispatch_id: int) -> list[dict]:
        """Optional evidence; a failure here must never fail a load check."""
        try:
            return self.results(self.get(f"/dispatch/{dispatch_id}/getTextMessages"))
        except Exception:  # noqa: BLE001
            return []

    def search_loads(self, params: dict) -> tuple[list[dict], dict]:
        payload = self.get("/load/search", params)
        pagination = (payload.get("pagination") or {}) if isinstance(payload, dict) else {}
        return self.results(payload), pagination

    def search_all_pages(self, params: dict, max_pages: int = 20) -> list[dict]:
        """Every page, not just the first. `page` is the parameter TransportPro honours (probed
        14 Sep 2026); if a page repeats page 0 the parameter was ignored and we say so rather than
        silently returning a fifth of the loads."""
        first, pg = self.search_loads(params)
        total_pages = int(pg.get("totalPages") or 1)
        out = list(first)
        if total_pages <= 1 or not first:
            return out
        first_id = first[0].get("id")
        for page in range(1, min(total_pages, max_pages)):
            rows, _ = self.search_loads({**params, "page": str(page)})
            if not rows or rows[0].get("id") == first_id:
                print(f"  warning: /load/search ignored the page parameter; only page 0 of {total_pages} read")
                break
            out += rows
        return out


    # -- the write ---------------------------------------------------------
    def upload_file(self, *, record_type: str, record_id: int, document_type: str, comments: str,
                    filename: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
        """POST /files/upload - the only call in this service that changes TransportPro.

        Field names and shape are taken from the TPro MCP server's own tpro_file_upload, which is
        proven against the live API: multipart/form-data with recordType, recordId, documentType,
        comments and a `file` part carrying the filename.

        Deliberately not wrapped in a retry. A 500 after the document has already been stored would
        file it twice, and the read paths' idempotency does not extend here; a failure is reported
        so the caller can check File History before trying again.
        """
        if self._access is None:
            self._login()
        body, content_type_header = _multipart(
            {"recordType": record_type, "recordId": str(record_id),
             "documentType": document_type, "comments": comments},
            file_field="file", filename=filename, data=data, file_content_type=content_type)
        for attempt in range(2):
            req = urllib.request.Request(
                f"{self.base}/files/upload", data=body, method="POST",
                headers={"Accept": "application/json", "Authorization": f"Bearer {self._access}",
                         "Content-Type": content_type_header, "Accept-Encoding": "identity"})
            try:
                with urllib.request.urlopen(req, timeout=max(self._timeout, 120)) as r:
                    self.calls += 1
                    return json.loads(_body(r) or "{}")
            except urllib.error.HTTPError as e:
                # The one retry allowed here: a 401 is TransportPro refusing the token before it
                # looks at the file, so nothing was stored and sending it again cannot file it twice.
                # A worker run is long enough for its token to expire between the reads and this.
                if e.code == 401 and attempt == 0:
                    if not self._refresh():
                        self._login()
                    continue
                raise TProError(e.code, "/files/upload", _body(e)) from None
            except NETWORK_ERRORS as e:
                raise TProError(0, "/files/upload", f"network: {type(e).__name__}: {e}") from None
        raise TProError(401, "/files/upload", "refused after signing in again")


def uploaded_file_id(result: Any) -> str:
    """The new file's id from an upload's answer. TransportPro nests it:
    {"STATUS": "SUCCESS", "MESSAGE": "File uploaded", "result": {"id": 31447880}} (24 Sep 2026)."""
    if not isinstance(result, dict):
        return ""
    inner = result.get("result") if isinstance(result.get("result"), dict) else {}
    return str(inner.get("id") or inner.get("fileId") or result.get("id") or result.get("fileId") or "")


def _multipart(fields: dict[str, str], *, file_field: str, filename: str, data: bytes,
               file_content_type: str) -> tuple[bytes, str]:
    """Build a multipart/form-data body. Hand-rolled because this client has no HTTP library
    beyond urllib, and the boundary has to be chosen here rather than by a framework."""
    import uuid

    boundary = "----intake" + uuid.uuid4().hex
    crlf = b"\r\n"
    out = bytearray()
    for key, value in fields.items():
        out += b"--" + boundary.encode() + crlf
        out += f'Content-Disposition: form-data; name="{key}"'.encode() + crlf + crlf
        out += str(value).encode("utf-8") + crlf
    safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
    out += b"--" + boundary.encode() + crlf
    out += f'Content-Disposition: form-data; name="{file_field}"; filename="{safe}"'.encode() + crlf
    out += f"Content-Type: {file_content_type}".encode() + crlf + crlf
    out += data + crlf
    out += b"--" + boundary.encode() + b"--" + crlf
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def from_env() -> TransportPro:
    """Build from PAYBOT_TP_* so an existing .env keeps working; a server injects the same three
    names from its secret store with no code change."""
    base = os.environ.get("PAYBOT_TP_BASE_URL") or ""
    user = os.environ.get("PAYBOT_TP_USERNAME") or ""
    pwd = os.environ.get("PAYBOT_TP_PASSWORD") or ""
    missing = [n for n, v in (("PAYBOT_TP_BASE_URL", base), ("PAYBOT_TP_USERNAME", user),
                              ("PAYBOT_TP_PASSWORD", pwd)) if not v]
    if missing:
        raise RuntimeError(f"TransportPro settings missing: {', '.join(missing)} (see .env.example)")
    return TransportPro(base, user, pwd)
