"""Lambda entry point for the morning report (intake/report.py).

Runs on a schedule at 07:00 US Eastern. Reads the ledger from S3 (a copy, never handed back), builds
the report for the pilot pods, and sends it through SES from INTAKE_REPORT_FROM to the addresses in
INTAKE_REPORT_TO. With no recipients it prints the report and sends nothing, so a deploy with the
list still empty is harmless.

Environment:
    INTAKE_S3_BUCKET, INTAKE_LEDGER_KEY, INTAKE_POD_CONFIG_KEY   where the ledger and the pod map are
    INTAKE_AUTO_TERMINALS                                         the pilot pods, e.g. "1160,1138"
    INTAKE_REPORT_FROM, INTAKE_REPORT_TO                          sender; recipients, comma-separated
    INTAKE_REPORT_HOURS                                           the window, default 24
    INTAKE_UPLOAD_SHEET_ID, INTAKE_GMAIL_SECRET                   the sheet: rows a person has changed are
                                                                  followed, and the link at the foot of the mail
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from . import autofile, report, sheets


def handler(event: dict | None, context: Any) -> dict:
    env = os.environ
    import boto3
    s3 = boto3.client("s3")
    bucket = env["INTAKE_S3_BUCKET"]
    path = Path(tempfile.gettempdir()) / "intake-report.sqlite3"
    s3.download_file(bucket, env.get("INTAKE_LEDGER_KEY", "ledger/intake.sqlite3"), str(path))
    pods: dict[int, str] = {}
    try:
        config = json.loads(s3.get_object(Bucket=bucket, Key=env.get("INTAKE_POD_CONFIG_KEY", "config/pod_terminals.json"))["Body"].read())
        pods = autofile.pod_names(config)
    except Exception as e:                                       # noqa: BLE001 - names are a nicety
        print(f"  ! report: pod names unavailable: {type(e).__name__}: {str(e)[:120]}")
    terminals = frozenset(int(x) for x in re.split(r"[,\s]+", env.get("INTAKE_AUTO_TERMINALS") or "") if x)
    sheet_status = None
    if env.get("INTAKE_UPLOAD_SHEET_ID") and env.get("INTAKE_GMAIL_SECRET"):
        try:
            from .aws_lambda import _service_account
            log = sheets.from_service_account(env["INTAKE_UPLOAD_SHEET_ID"], _service_account(env["INTAKE_GMAIL_SECRET"]))
            sheet_status = log.statuses()
        except Exception as e:                                   # noqa: BLE001 - the ledger alone still makes a report
            print(f"  ! report: the sheet could not be read, reporting from the ledger alone: {type(e).__name__}: {str(e)[:120]}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rep = report.build(conn, hours=int(env.get("INTAKE_REPORT_HOURS") or 24), terminals=terminals,
                           pods=pods, sheet_id=env.get("INTAKE_UPLOAD_SHEET_ID", ""), sheet_status=sheet_status)
    finally:
        conn.close()
    to = [a.strip() for a in re.split(r"[,;\s]+", env.get("INTAKE_REPORT_TO") or "") if a.strip()]
    sender = env.get("INTAKE_REPORT_FROM") or ""
    sent = False
    message_id = ""
    if to and sender:
        ses = boto3.client("sesv2")
        r = ses.send_email(FromEmailAddress=f"Doc Intake Bot <{sender}>", Destination={"ToAddresses": to},
                           Content={"Simple": {"Subject": {"Data": rep.subject, "Charset": "UTF-8"},
                                               "Body": {"Text": {"Data": rep.text, "Charset": "UTF-8"}}}})
        sent, message_id = True, r.get("MessageId", "")
    else:
        print(rep.text)
    summary = {"report": {"subject": rep.subject, "held_loads": rep.held_loads, "held_documents": rep.held_documents,
                          "resolved_by_a_person": rep.resolved, "sheet_read": sheet_status is not None,
                          "to": to, "sent": sent, "message_id": message_id}}
    print(json.dumps(summary))
    return summary
