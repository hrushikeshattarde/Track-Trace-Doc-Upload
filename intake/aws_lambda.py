"""The collector, as an AWS Lambda, scheduled every 15 minutes.

One invocation runs one pass of intake.collector: read the bookmark in S3, ask Gmail what arrived
since, store each message and its documents in S3, move the bookmark. There is no ledger here - see
collector.py for why, and for what stands in for each thing the ledger used to remember.

Every 15 minutes rather than hourly because the work is the same either way - each message is
fetched once whenever it is fetched - so the schedule only decides how big each run is. The busiest
hour on record (22 Sep 2026, 765 messages) is ~190 per quarter-hour, a few minutes' work; as one
hourly run it sits close to the timeout, and anything slow pushes it over.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from . import collector, gmail as gm, store as s3store

GROUP = "ratecon@circledelivers.com"

# Kept back from the timeout so a run that is stopped by its deadline still writes its bookmark.
RESERVE_S = 30


def handler(event: dict | None, context: Any) -> dict:
    started = time.monotonic()
    remaining = context.get_remaining_time_in_millis() / 1000 if context else 600.0
    env = os.environ

    import boto3
    store = s3store.Store(env["INTAKE_S3_BUCKET"], env.get("INTAKE_S3_PREFIX", ""), client=boto3.client("s3"))
    gmail = gm.Delegated(_service_account(env["INTAKE_GMAIL_SECRET"]), subject=env["PAYBOT_GMAIL_USER"])

    st = collector.run(gmail, store, group=env.get("INTAKE_GROUP", GROUP),
                       max_messages=int(env.get("INTAKE_MAX_MESSAGES", "400")),
                       deadline=started + remaining - RESERVE_S,
                       bookmark_key=env.get("INTAKE_BOOKMARK_KEY", collector.BOOKMARK_KEY))

    summary = {"collect": st.line(), "gmail_calls": gmail.calls, "seconds": round(time.monotonic() - started)}
    print(json.dumps(summary))
    if st.error:
        # Raised after the bookmark is saved, so the Errors metric sees a bad run without it costing
        # the messages that did go through.
        raise RuntimeError(f"collector stopped on an error: {st.error}")
    return summary


def _service_account(secret_id: str) -> dict:
    """The Gmail service-account key, from Secrets Manager. It goes from there to the signer in
    memory and is never written to /tmp, where a later invocation of a reused container could find it."""
    import boto3
    raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
    info = json.loads(raw)
    missing = [k for k in ("client_email", "private_key") if not info.get(k)]
    if missing:
        raise ValueError(f"secret {secret_id} is missing {missing}; it must hold the service account's JSON key")
    return info
