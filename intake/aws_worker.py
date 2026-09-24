"""The worker, as an AWS Lambda, scheduled every 15 minutes: steps 2 and 3 of the pipeline.

One invocation:

    1. take the ledger from S3                      (ledger_s3 - one writer, If-Match on the way back)
    2. mail      record what the collector stored   (mailsync - S3 only, no Gmail, no TransportPro)
    3. loads     inside working hours only:
                   sweep     the dashboard, every run. The day's first is the full audit, the only one
                             that may drop loads from the view; the rest cover pickups 42 days back
                   changed   loads whose document status in TransportPro moved since their last
                             check - somebody else filed something - are checked straight away
                   check     then the loads whose next check has come round, oldest first
                 changed + check together stay within INTAKE_LOAD_LIMIT loads a run
                 auto_upload  for the pilot terminals only (INTAKE_AUTO_TERMINALS, the Frankie Saiz
                             pod from 24 Sep 2026): read new BOLs and PODs with the AI, upload what
                             passes every check, log every decision to the pod's Upload log sheet -
                             see autofile.py. INTAKE_AUTO_UPLOAD off / dry-run / on switches it
    4. hand the ledger back

Outside auto_upload nothing is read by a model and nothing is written to TransportPro. The load
checks' calls are all reads: /load/search for the sweeps, and /load, /dispatch/search and
/files/search per load checked. auto_upload adds /load, /files/search and /files/{id} reads on the
pilot loads and POST /files/upload, the only write.

WHY WORKING HOURS AND A CAP
---------------------------
Nobody has yet said how hard TransportPro may be called, and at full cadence the load checks come
to roughly 1,000 calls an hour. So the start is conservative - 06:00 to 20:00 Eastern, weekdays,
at most INTAKE_LOAD_LIMIT loads per run - and both are settings, not code. Outside those hours the
worker still records new mail, so a load that got paperwork overnight is due first thing.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import autofile, db, ledger_s3, loadloop, mailsync, store as s3store, tpro as tp

FULL_SWEEP_DAYS_BACK = 350      # the nightly audit window `intake reconcile` documents
# The every-run sweep. /load/search takes a pickup-date window of at most 45 days, so the old 3-day
# sweep already cost two windows per terminal; 42 days back fills the same two and sees the loads
# picked up weeks ago that are still waiting on paperwork, for the same ~32 calls.
STATUS_SWEEP_DAYS_BACK = 42
DAYS_FORWARD = 45
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Kept back from the timeout for handing the ledger back: a run killed before it uploads throws away
# everything it did.
RESERVE_S = 60


def handler(event: dict | None, context: Any) -> dict:
    started = time.monotonic()
    remaining = context.get_remaining_time_in_millis() / 1000 if context else 600.0
    deadline = started + remaining - RESERVE_S
    env = os.environ

    import boto3
    s3 = boto3.client("s3")
    bucket = env["INTAKE_S3_BUCKET"]
    ledger_key = env.get("INTAKE_LEDGER_KEY", ledger_s3.LEDGER_KEY)
    store = s3store.Store(bucket, env.get("INTAKE_S3_PREFIX", ""), client=s3)

    work = Path(tempfile.mkdtemp(prefix="ledger_"))
    path = work / "intake.sqlite3"
    etag = ledger_s3.take(s3, bucket, ledger_key, path)
    ledger_s3.snapshot(s3, bucket, ledger_key, etag)
    conn = db.connect(path)

    steps: dict[str, str] = {}
    failed: list[str] = []

    def step(name: str, fn) -> bool:
        # Steps fail independently, as in `intake cycle`: TransportPro being down must not stop mail
        # being recorded, and neither may stop the ledger going back with whatever did commit.
        try:
            steps[name] = fn()
            return True
        except Exception as e:                                   # noqa: BLE001 - reported, then raised
            failed.append(name)
            steps[name] = f"FAILED {type(e).__name__}: {str(e)[:300]}"
            return False

    # Mail gets the first 40% of the run at most, so a backlog can never leave the loads no time.
    step("mail", lambda: mailsync.ingest_recent(
        conn, store, days=int(env.get("INTAKE_MAIL_DAYS", "7")),
        deadline=started + (deadline - started) * 0.4).line())

    now_local = local_now(env.get("INTAKE_TZ", "America/New_York"))
    active, why = working_hours(now_local, env.get("INTAKE_ACTIVE_HOURS", "06-20"),
                                env.get("INTAKE_ACTIVE_DAYS", "mon-fri"))
    tpro = None
    if not active:
        steps["loads"] = f"not checked: {why}"
    else:
        try:
            tpro = tp.TransportPro(**_tpro_login(env["INTAKE_TPRO_SECRET"], env))
            terminals, levels = _pod_config(s3, bucket, env.get("INTAKE_POD_CONFIG_KEY", "config/pod_terminals.json"), work)
        except Exception as e:                                   # noqa: BLE001 - reported, then raised
            # Still hand the ledger back: the mail this run recorded is worth keeping.
            failed.append("setup")
            steps["loads"] = f"FAILED to start: {type(e).__name__}: {str(e)[:300]}"
            tpro = None
    if tpro is not None:
        today = now_local.date().isoformat()
        limit = int(env.get("INTAKE_LOAD_LIMIT", "150"))
        full = db.get_state(conn, "full_sweep_day") != today
        changed: list[int] = []
        # The day's first sweep is the audit: the only one allowed to decide a load has left the view,
        # because it is the only one that looked at the whole window.
        if step("full_sweep" if full else "sweep", lambda: loadloop.reconcile(
                conn, tpro, terminals=terminals,
                days_back=FULL_SWEEP_DAYS_BACK if full else STATUS_SWEEP_DAYS_BACK,
                days_forward=DAYS_FORWARD, scope_levels=levels, authoritative=full,
                changes=changed).line()):
            db.set_state(conn, "reconcile_at", db.now_iso())
            if full:
                db.set_state(conn, "full_sweep_day", today)
        # A load whose status moved in TransportPro goes first, ahead of the timer queue: its timer
        # can be an hour away, and a backlog can push it further.
        first = changed[:limit]
        if first:
            step("changed", lambda: loadloop.check_loads(conn, tpro, first, scope_levels=levels).line())
        if full:
            # The audit runs without the regular checks: together they can approach the timeout.
            steps["check"] = "not this run: the day's full sweep ran; loads are checked from the next run"
        else:
            step("check", lambda: loadloop.drain(
                conn, tpro, limit=max(0, limit - len(first)), scope_levels=levels).line())
        try:
            auto = autofile.Settings.from_env(env, pods=_pod_names(work))
        except ValueError as e:
            auto = None
            failed.append("auto_upload")
            steps["auto_upload"] = f"FAILED to start: {e}"
        if auto is not None and auto.mode != autofile.OFF and auto.terminals:
            step("auto_upload", lambda: _auto_upload(conn, tpro, store, auto, env, deadline, failed))
        steps["transportpro_calls"] = str(tpro.calls)

    states = dict(conn.execute("SELECT state, COUNT(*) FROM load WHERE in_view=1 GROUP BY state").fetchall())
    ledger_s3.give_back(s3, bucket, ledger_key, conn, path, etag)

    summary = {"worker": steps, "loads_in_view": states, "failed": failed,
               "seconds": round(time.monotonic() - started)}
    print(json.dumps(summary))
    if failed:
        # Raised AFTER the ledger is back, so the Errors metric sees a bad run without it costing the
        # parts of the run that worked.
        raise RuntimeError(f"step(s) failed: {', '.join(failed)} - {json.dumps(steps)[:900]}")
    return summary


# ------------------------------------------------------------------------------- settings ----

def local_now(tz: str) -> dt.datetime:
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo(tz))


def working_hours(now_local: dt.datetime, hours: str, days: str) -> tuple[bool, str]:
    """(inside the window?, why not). hours "06-20" = 06:00 up to 20:00; days "mon-fri", "mon,wed",
    or "all"."""
    start, end = (int(x) for x in hours.split("-"))
    allowed = _days(days)
    today = DAYS[now_local.weekday()]
    if today not in allowed:
        return False, f"{today} is outside the working days ({days})"
    if not (start <= now_local.hour < end):
        return False, f"{now_local:%H:%M} is outside the working hours ({hours}, {now_local.tzname()})"
    return True, ""


def _days(spec: str) -> set[str]:
    spec = spec.strip().lower()
    if spec in ("all", "*", ""):
        return set(DAYS)
    out: set[str] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = (DAYS.index(x.strip()[:3]) for x in part.split("-"))
            out.update(DAYS[i] for i in range(a, b + 1))
        else:
            out.add(part.strip()[:3])
    return out


def _tpro_login(secret_id: str, env) -> dict:
    """The TransportPro login. The password always comes from Secrets Manager, never from the
    function's settings, where anyone who can view the function can read it."""
    import boto3
    return login_from(boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"], env)


def login_from(secret: str, env) -> dict:
    """Two shapes. The pay-status bot's secret (paybot/prod/tp-password, used here since 23 Sep 2026)
    holds the password alone, with the username and API address as ordinary settings - the same
    split the bot itself uses. A JSON secret may carry all three."""
    try:
        info = json.loads(secret)
    except ValueError:
        info = None
    if not isinstance(info, dict):
        info = {"password": secret}
    login = {"base_url": info.get("base_url") or env.get("INTAKE_TPRO_BASE_URL", ""),
             "username": info.get("username") or env.get("INTAKE_TPRO_USERNAME", ""),
             "password": info.get("password") or ""}
    missing = [k for k, v in login.items() if not v]
    if missing:
        raise ValueError(f"TransportPro login is missing {missing}: set it in the secret or as "
                         f"INTAKE_TPRO_{'/'.join(m.upper() for m in missing)}")
    return login


def _auto_upload(conn, tpro, store, auto: autofile.Settings, env, deadline: float, failed: list[str]) -> str:
    """Step 4 for the pilot terminals: read, upload what passes, log every BOL and POD."""
    from . import aws_lambda, ingest, sheets
    # The brief full read (short notes, low effort) and the quick look that decides which pages need
    # it: measured 24 Sep 2026, the same decisions for about a third of the cost.
    read = ingest.make_reader(env.get("INTAKE_READ_MODEL", "claude-opus-5"), timeout=autofile.READ_TIMEOUT_S,
                              effort=env.get("INTAKE_READ_EFFORT", "low") or None, brief=True,
                              max_pages=autofile.MAX_READ_PAGES)
    quick = None
    if env.get("INTAKE_QUICK_LOOK", "on") != "off":
        quick = ingest.make_quick_reader(env.get("INTAKE_QUICK_MODEL", "claude-haiku-4-5"), timeout=60)
    log = None
    if auto.mode == autofile.ON:
        # The sheet is written as the Gmail service account itself, the key the collector already uses.
        log = sheets.from_service_account(env["INTAKE_UPLOAD_SHEET_ID"],
                                          aws_lambda._service_account(env["INTAKE_GMAIL_SECRET"]))
    uploader = tpro
    if env.get("INTAKE_TPRO_UPLOAD_SECRET"):
        # The bot's own TransportPro login, once one exists, so File History shows "Doc Intake Bot" as
        # the uploader. Its secret must name the user: the reading login's username is not borrowed.
        import boto3
        secret = boto3.client("secretsmanager").get_secret_value(
            SecretId=env["INTAKE_TPRO_UPLOAD_SECRET"])["SecretString"]
        uploader = tp.TransportPro(**login_from(secret, {"INTAKE_TPRO_BASE_URL": env.get("INTAKE_TPRO_BASE_URL", "")}))
    stats = autofile.run(conn, tpro, store, read, log, auto, deadline=deadline, uploader=uploader, quick=quick)
    if stats.sheet_error:
        failed.append("upload_log")         # the rows stay pending and go with the next run
    return stats.line()


def _pod_names(work: Path) -> dict[int, str]:
    try:
        return autofile.pod_names(json.loads((work / "pod_terminals.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _pod_config(s3, bucket: str, key: str, work: Path) -> tuple[list[int], set[str]]:
    """The ticked pod terminals and the service levels in scope - index/pod_terminals.json, kept in
    S3 because it is Circle's data and the repository is public."""
    p = work / "pod_terminals.json"
    p.write_bytes(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    terminals, levels = loadloop.pod_terminals(p)
    if not terminals:
        raise ValueError(f"s3://{bucket}/{key} has no terminal ticked as in the current view")
    return terminals, levels
