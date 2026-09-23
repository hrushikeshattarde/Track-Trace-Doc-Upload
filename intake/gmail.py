"""Delegated Gmail access with a history cursor.

Self-contained on purpose. mail_survey.py and readiness.py reach into the payment bot's checkout
through a hard-coded laptop path (PAYBOT_DIR), which a server cannot have; this module needs only
the service-account key and google-auth's RSA signer, and talks to Google over urllib. No
requests, no urllib3, no google-auth transports - none of which are installed here anyway.

The unit of work is `history_since()`: Gmail is asked what arrived after a cursor, not what exists
in a window. A thread is therefore re-examined exactly when someone replies to it. Gmail keeps
history for roughly a week; past that the cursor is dead and `CursorTooOld` asks the caller to
fall back to `search()` and re-seed from `profile()`.

Read-only: the only scope requested is gmail.readonly.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

API = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_LIFETIME = 3600
EXPIRY_SKEW = 120


class GmailError(RuntimeError):
    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"Gmail API {status} on {path}: {body[:300]}")
        self.status = status


class CursorTooOld(GmailError):
    """startHistoryId is older than Gmail's history retention (about a week). Full sync required."""


def load_service_account_info(path: str | Path) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"service-account key not found: {p}")
    info = json.loads(p.read_text(encoding="utf-8-sig"))     # Windows editors add a BOM; json.loads rejects it
    missing = [k for k in ("client_email", "private_key") if not info.get(k)]
    if missing:
        raise ValueError(f"{p} is missing {missing}; download the service account's JSON key, not an OAuth client secret")
    return info


class Delegated:
    """Mints and caches an impersonated access token for one mailbox.

    Domain-wide delegation: the `sub` claim is what makes the service account act as the user.
    The admin must have authorised this client id for this exact scope, or Google answers
    `unauthorized_client` however valid the key is.
    """

    def __init__(self, info: dict[str, Any], subject: str, scopes: tuple[str, ...] = (SCOPE_READONLY,),
                 min_interval: float = 0.05) -> None:
        self.info = info
        self.subject = subject
        self.scopes = scopes
        self.token_uri = str(info.get("token_uri") or TOKEN_URI)
        self._token: str | None = None
        self._expires_at = 0.0
        self._min_interval = min_interval        # crude token bucket: the per-user-per-second cap is the real limit
        self._last_call = 0.0
        self.calls = 0

    # -- auth --------------------------------------------------------------
    def token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - EXPIRY_SKEW:
            return self._token
        from google.auth import jwt as google_jwt              # signing only; no HTTP stack pulled in
        from google.auth.crypt import RSASigner

        payload = {
            "iss": self.info["client_email"], "scope": " ".join(self.scopes),
            "aud": self.token_uri, "iat": int(now), "exp": int(now) + TOKEN_LIFETIME,
            "sub": self.subject,
        }
        assertion = google_jwt.encode(RSASigner.from_service_account_info(self.info), payload)
        body = urllib.parse.urlencode({"grant_type": JWT_BEARER,
                                       "assertion": assertion.decode("ascii") if isinstance(assertion, bytes) else assertion}).encode()
        req = urllib.request.Request(self.token_uri, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise GmailError(e.code, "token", _explain_token(e.read().decode("utf-8", "replace"))) from None
        self._token = str(data["access_token"])
        self._expires_at = now + float(data.get("expires_in") or TOKEN_LIFETIME)
        return self._token

    # -- transport ---------------------------------------------------------
    def get(self, path: str, params: list[tuple[str, str]] | dict[str, str] | None = None) -> dict:
        gap = self._min_interval - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        url = f"{API}{path}" + (("?" + urllib.parse.urlencode(params, doseq=True)) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    self._last_call = time.time()
                    self.calls += 1
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                self._last_call = time.time()
                if e.code == 404 and "/history" in path:
                    raise CursorTooOld(404, path, body) from None
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(2 ** attempt)     # the per-user rate cap is the one worth backing off for
                    continue
                raise GmailError(e.code, path, body) from None
        raise GmailError(0, path, "retries exhausted")

    # -- API ---------------------------------------------------------------
    @property
    def _me(self) -> str:
        return urllib.parse.quote(self.subject)

    def profile(self) -> dict:
        """Includes the mailbox's current historyId - how a full sync re-seeds the cursor."""
        return self.get(f"/users/{self._me}/profile")

    def history_since(self, start_history_id: str, label_id: str | None = None,
                      max_pages: int = 50) -> tuple[list[dict], str | None]:
        """Messages added since the cursor, plus the new cursor.

        Returns ({id, threadId} records, latest historyId). Gmail may report the same message in
        several history records; de-duplication here keeps the caller's loop simple, and the
        message_id primary key makes a miss harmless anyway.
        """
        seen: dict[str, dict] = {}
        latest = start_history_id
        page: str | None = None
        for _ in range(max_pages):
            params: list[tuple[str, str]] = [("startHistoryId", str(start_history_id)),
                                             ("historyTypes", "messageAdded"), ("maxResults", "500")]
            if label_id:
                params.append(("labelId", label_id))
            if page:
                params.append(("pageToken", page))
            payload = self.get(f"/users/{self._me}/history", params)
            latest = str(payload.get("historyId") or latest)
            for rec in payload.get("history") or []:
                for added in rec.get("messagesAdded") or []:
                    m = added.get("message") or {}
                    if m.get("id"):
                        seen.setdefault(m["id"], {"id": m["id"], "threadId": m.get("threadId"),
                                                  "labelIds": m.get("labelIds") or []})
            page = payload.get("nextPageToken")
            if not page:
                break
        return list(seen.values()), latest

    def search(self, query: str, cap: int = 2000) -> list[dict]:
        """Full-sync fallback when the cursor has expired. Ids only - cheap."""
        out: list[dict] = []
        page: str | None = None
        while len(out) < cap:
            params = {"q": query, "maxResults": "500"}
            if page:
                params["pageToken"] = page
            payload = self.get(f"/users/{self._me}/messages", params)
            out.extend(payload.get("messages") or [])
            page = payload.get("nextPageToken")
            if not page:
                break
        return out[:cap]

    def message(self, message_id: str, fmt: str = "full") -> dict:
        return self.get(f"/users/{self._me}/messages/{message_id}", {"format": fmt})

    def thread(self, thread_id: str) -> dict:
        """Every message of a thread, headers and snippet only - how the S3-only collector learns
        which load a reply belongs to without a ledger to remember it."""
        return self.get(f"/users/{self._me}/threads/{thread_id}",
                        [("format", "metadata"), ("metadataHeaders", "Subject")])

    def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        data = self.get(f"/users/{self._me}/messages/{message_id}/attachments/{attachment_id}")
        return base64.urlsafe_b64decode(data["data"] + "==")


def _explain_token(body: str) -> str:
    if "unauthorized_client" in body:
        return (body[:200] + "  -> the service account's client id is not authorised for this scope in "
                "Admin console > Security > API controls > Domain-wide delegation")
    if "invalid_grant" in body:
        return body[:200] + "  -> the impersonated user does not exist, or the key has been revoked"
    return body[:300]


def from_env() -> Delegated:
    """Build the client from PAYBOT_GMAIL_USER / PAYBOT_GOOGLE_SA_FILE, as the prototype does.

    Same variable names so a working .env keeps working; a server should inject these from its
    secret store instead, which needs no code change here.
    """
    user = os.environ.get("PAYBOT_GMAIL_USER") or ""
    key = os.environ.get("PAYBOT_GOOGLE_SA_FILE") or ""
    if not user or not key:
        raise RuntimeError("set PAYBOT_GMAIL_USER and PAYBOT_GOOGLE_SA_FILE (see .env.example)")
    return Delegated(load_service_account_info(key), subject=user)


def internal_date_iso(message: dict) -> str | None:
    """Gmail's internalDate is epoch milliseconds. Order by this, never by arrival: history
    pagination and retries deliver replies out of order."""
    raw = message.get("internalDate")
    if not raw:
        return None
    return dt.datetime.fromtimestamp(int(raw) / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def iter_parts(part: dict) -> Iterator[dict]:
    yield part
    for p in part.get("parts") or []:
        yield from iter_parts(p)


def headers_of(message: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in ((message.get("payload") or {}).get("headers") or [])}
