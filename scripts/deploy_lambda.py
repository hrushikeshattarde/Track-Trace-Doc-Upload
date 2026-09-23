"""Build and deploy the S3-only collector Lambda (intake/aws_lambda.py, intake/collector.py).

    python scripts/deploy_lambda.py secret          put the Gmail key from .env into Secrets Manager
    python scripts/deploy_lambda.py deploy          build, then create or update every other AWS piece
    python scripts/deploy_lambda.py bookmark        start the collector where this laptop's collection ended - once
    python scripts/deploy_lambda.py invoke          run it now and print what it did
    python scripts/deploy_lambda.py schedule on     start the 15-minute schedule (or: off)
    python scripts/deploy_lambda.py status          what exists, and the last few runs

Every step is create-or-update, so `deploy` is safe to re-run after a code change. `deploy` never
touches Secrets Manager: the key is placed by `secret`, run by a person, and `secret` never replaces
a key that is already there unless given --rotate. `bookmark` refuses to replace a bookmark that is
already in S3: once the collector is running, the bookmark is its own and only it moves it.

What it creates, all in the account and region the .env's AWS_PROFILE points at:

    secret      circle-doc-intake/gmail-service-account   the Gmail service-account key
    role        circle-doc-intake-collector               S3 on mail/ doc/ state/, that one secret, logs
    function    circle-doc-intake-collector               python3.12, 10 min, 512 MB, concurrency 1, no retries
    log group   /aws/lambda/circle-doc-intake-collector   90-day retention
    role        circle-doc-intake-scheduler               may invoke that one function
    schedule    circle-doc-intake-every-15-min            rate(15 minutes); created DISABLED
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pod_intake.localenv import load_local_env  # noqa: E402

NAME = "circle-doc-intake-collector"
SECRET = "circle-doc-intake/gmail-service-account"
SCHEDULER_ROLE = "circle-doc-intake-scheduler"
SCHEDULE = "circle-doc-intake-every-15-min"
BOOKMARK_KEY = "state/gmail-bookmark.json"
CODE_KEY = "deploy/collector.zip"
TAGS = {"project": "circle-doc-intake", "component": "collector"}
BUILD = HERE / "build" / "lambda"

# The versions the offline suite ran against, pinned so the Lambda runs what was tested. boto3 is
# bundled rather than taken from the runtime because the bookmark write depends on S3 If-Match,
# and a runtime's bundled boto3 lags behind.
DEPS = ["boto3==1.43.98", "google-auth==2.58.0", "cryptography==50.0.1"]


def _aws():
    import os
    import boto3
    return boto3.session.Session(region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def _env() -> dict:
    import os
    load_local_env()
    need = ["INTAKE_S3_BUCKET", "PAYBOT_GMAIL_USER", "PAYBOT_GOOGLE_SA_FILE"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"set {missing} in .env first")
    return dict(os.environ)


# ---------------------------------------------------------------------------------- build ----

def build() -> Path:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    # Linux wheels, fetched from Windows: cryptography is compiled, and the one pip would pick for
    # this machine does not load on Lambda.
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(BUILD),
                    "--platform", "manylinux2014_x86_64", "--implementation", "cp",
                    "--python-version", "3.12", "--only-binary=:all:", *DEPS], check=True)
    # Only the intake package: collection needs nothing from pod_intake, which would bring pydantic.
    # The ledger modules ride along unused; they are small, and leaving them out would make an
    # import of the package depend on which files happened to be copied.
    shutil.copytree(HERE / "intake", BUILD / "intake",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "__main__.py"))
    zpath = BUILD.parent / "collector.zip"
    zpath.unlink(missing_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(BUILD.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(BUILD).as_posix())
    print(f"built {zpath.name}: {zpath.stat().st_size / 1e6:.1f} MB")
    return zpath


# ----------------------------------------------------------------------------------- pieces ----

def secret_arn_pattern(sess, account: str) -> str:
    """The secret's ARN without needing it to exist yet. Secrets Manager appends six random
    characters to every ARN; ?????? is the pattern AWS documents for granting access by name."""
    return f"arn:aws:secretsmanager:{sess.region_name}:{account}:secret:{SECRET}-??????"


def ensure_secret(sess, sa_file: str, rotate: bool) -> str:
    sm = sess.client("secretsmanager")
    body = Path(sa_file).read_text(encoding="utf-8-sig")
    info = json.loads(body)
    if not info.get("client_email") or not info.get("private_key"):
        raise SystemExit(f"{sa_file} is not a service-account JSON key")
    try:
        arn = sm.describe_secret(SecretId=SECRET)["ARN"]
        if rotate:
            sm.put_secret_value(SecretId=SECRET, SecretString=body)
            print(f"secret    {SECRET}: replaced with the key in .env")
        else:
            print(f"secret    {SECRET}: exists, left as it is")
        return arn
    except sm.exceptions.ResourceNotFoundException:
        arn = sm.create_secret(Name=SECRET, SecretString=body,
                               Description="Gmail service-account key for the doc-intake collector "
                                           "(domain-wide delegation, gmail.readonly)",
                               Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["ARN"]
        print(f"secret    {SECRET}: created")
        return arn


def _ensure_role(iam, name: str, service: str, policy: dict, managed: list[str], account: str) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        fresh = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])["Role"]["Arn"]
        fresh = True
    iam.put_role_policy(RoleName=name, PolicyName="access", PolicyDocument=json.dumps(policy))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    print(f"role      {name}: {'created' if fresh else 'updated'}")
    if fresh:
        time.sleep(12)   # a new role is not assumable for a few seconds; create_function says so
    return arn


def ensure_function_role(sess, bucket: str, secret_arn: str, account: str) -> str:
    b = f"arn:aws:s3:::{bucket}"
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "Archive", "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging"],
         "Resource": [f"{b}/mail/*", f"{b}/doc/*", f"{b}/state/*"]},
        # Without ListBucket a HEAD on a missing key answers 403, not 404, and every "is it already
        # there" check would read as an error instead of a no.
        {"Sid": "TellMissingFromForbidden", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": b},
        {"Sid": "GmailKey", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
         "Resource": secret_arn},
    ]}
    return _ensure_role(sess.client("iam"), NAME, "lambda.amazonaws.com", policy,
                        ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"], account)


def ensure_function(sess, zpath: Path, role_arn: str, env: dict) -> str:
    s3 = sess.client("s3")
    lam = sess.client("lambda")
    bucket = env["INTAKE_S3_BUCKET"]
    s3.upload_file(str(zpath), bucket, CODE_KEY)
    variables = {
        "INTAKE_S3_BUCKET": bucket,
        "INTAKE_S3_PREFIX": env.get("INTAKE_S3_PREFIX", ""),
        "INTAKE_BOOKMARK_KEY": BOOKMARK_KEY,
        "INTAKE_GMAIL_SECRET": SECRET,
        "PAYBOT_GMAIL_USER": env["PAYBOT_GMAIL_USER"],
        "INTAKE_GROUP": "ratecon@circledelivers.com",
        "INTAKE_MAX_MESSAGES": "400",
    }
    # 10 minutes, not the 15 allowed: a run must end before the next one is due. The busiest
    # quarter-hour on record is ~190 messages, a few minutes' work; after an outage each run takes
    # what fits and hands the rest on.
    config = dict(Role=role_arn, Handler="intake.aws_lambda.handler", Runtime="python3.12",
                  Timeout=600, MemorySize=512, Environment={"Variables": variables},
                  Description="Every 15 min: new Gmail messages and their documents to S3, linked. "
                              "Never reads with a model, never touches TransportPro.")
    try:
        lam.get_function(FunctionName=NAME)
        lam.update_function_code(FunctionName=NAME, S3Bucket=bucket, S3Key=CODE_KEY)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        lam.update_function_configuration(FunctionName=NAME, **config)
        lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)
        print(f"function  {NAME}: code and configuration updated")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):
            try:
                lam.create_function(FunctionName=NAME, Code={"S3Bucket": bucket, "S3Key": CODE_KEY},
                                    Architectures=["x86_64"], Tags=TAGS, **config)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "assumed" not in str(e) or attempt == 5:
                    raise
                time.sleep(5)
        lam.get_waiter("function_active_v2").wait(FunctionName=NAME)
        print(f"function  {NAME}: created")
    # One at a time, always: two runs from the same bookmark would fetch the same mail twice, and
    # only one of them could move it. No retries, and a trigger that finds a run still going is
    # dropped after a minute rather than queued - the next quarter-hour is the retry.
    lam.put_function_concurrency(FunctionName=NAME, ReservedConcurrentExecutions=1)
    lam.put_function_event_invoke_config(FunctionName=NAME, MaximumRetryAttempts=0,
                                         MaximumEventAgeInSeconds=60)
    logs = sess.client("logs")
    group = f"/aws/lambda/{NAME}"
    try:
        logs.create_log_group(logGroupName=group, tags=TAGS)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=group, retentionInDays=90)
    return lam.get_function(FunctionName=NAME)["Configuration"]["FunctionArn"]


def ensure_schedule(sess, fn_arn: str, account: str, state: str | None) -> None:
    role = _ensure_role(sess.client("iam"), SCHEDULER_ROLE, "scheduler.amazonaws.com",
                        {"Version": "2012-10-17", "Statement": [{
                            "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": fn_arn}]},
                        [], account)
    sch = sess.client("scheduler")
    try:
        current = sch.get_schedule(Name=SCHEDULE)["State"]
        exists = True
    except sch.exceptions.ResourceNotFoundException:
        current, exists = "DISABLED", False
    spec = dict(Name=SCHEDULE, ScheduleExpression="rate(15 minutes)", FlexibleTimeWindow={"Mode": "OFF"},
                Target={"Arn": fn_arn, "RoleArn": role, "RetryPolicy": {"MaximumRetryAttempts": 0}},
                State=state or current,
                Description="Runs the doc-intake collector every 15 minutes")
    for attempt in range(6):
        try:
            (sch.update_schedule if exists else sch.create_schedule)(**spec)
            break
        except sch.exceptions.ValidationException as e:
            # A just-created role is not assumable for a few seconds, and Scheduler checks up front.
            if attempt == 5:
                raise
            time.sleep(5)
    print(f"schedule  {SCHEDULE}: {'updated' if exists else 'created'}, {spec['State']}")


# --------------------------------------------------------------------------------- commands ----

def cmd_deploy(args) -> int:
    env = _env()
    sess = _aws()
    account = sess.client("sts").get_caller_identity()["Account"]
    print(f"account {account}, region {sess.region_name}")
    zpath = build()
    role = ensure_function_role(sess, env["INTAKE_S3_BUCKET"], secret_arn_pattern(sess, account), account)
    fn_arn = ensure_function(sess, zpath, role, env)
    ensure_schedule(sess, fn_arn, account, None)
    sm = sess.client("secretsmanager")
    try:
        sm.describe_secret(SecretId=SECRET)
    except sm.exceptions.ResourceNotFoundException:
        print()
        print(f"NOTE: {SECRET} does not exist yet, so the function cannot reach Gmail. "
              f"Run: python scripts/deploy_lambda.py secret")
    return 0


def cmd_secret(args) -> int:
    env = _env()
    ensure_secret(_aws(), env["PAYBOT_GOOGLE_SA_FILE"], args.rotate)
    return 0


def cmd_bookmark(args) -> int:
    """Start the collector exactly where this laptop's collection ended, so nothing is skipped or
    collected twice. Refused if a bookmark already exists - after that it belongs to the collector."""
    import sqlite3
    from intake import collector, store as s3store
    env = _env()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    row = conn.execute("SELECT history_id, synced_at FROM mailbox_cursor WHERE mailbox=?",
                       (env["PAYBOT_GMAIL_USER"],)).fetchone()
    if not row:
        raise SystemExit(f"{args.db} has no Gmail cursor for {env['PAYBOT_GMAIL_USER']}")
    store = s3store.Store(env["INTAKE_S3_BUCKET"], env.get("INTAKE_S3_PREFIX", ""),
                          client=_aws().client("s3"))
    bm = collector.Bookmark(history_id=str(row[0]), taken_at=str(row[1]))
    try:
        collector.write_bookmark(store, bm, BOOKMARK_KEY, create=True)
    except collector.BookmarkChanged:
        raise SystemExit(f"s3://{store.bucket}/{store.full(BOOKMARK_KEY)} already exists; the collector "
                         f"owns it now, and it was not replaced") from None
    print(f"bookmark set: history {bm.history_id} (collected up to {bm.taken_at}). "
          f"The collector starts from there.")
    return 0


def cmd_invoke(args) -> int:
    from botocore.config import Config
    lam = _aws().client("lambda", config=Config(read_timeout=660, retries={"max_attempts": 0}))
    started = time.time()
    r = lam.invoke(FunctionName=NAME, InvocationType="RequestResponse", LogType="Tail")
    print(f"ran in {time.time() - started:.0f}s, status {r['StatusCode']}"
          + (f", FUNCTION ERROR {r['FunctionError']}" if r.get("FunctionError") else ""))
    print(base64.b64decode(r.get("LogResult", "")).decode("utf-8", "replace"))
    print(r["Payload"].read().decode("utf-8", "replace")[:4000])
    return 1 if r.get("FunctionError") else 0


def cmd_schedule(args) -> int:
    sess = _aws()
    sch = sess.client("scheduler")
    cur = sch.get_schedule(Name=SCHEDULE)
    state = "ENABLED" if args.state == "on" else "DISABLED"
    sch.update_schedule(Name=SCHEDULE, ScheduleExpression=cur["ScheduleExpression"],
                        FlexibleTimeWindow=cur["FlexibleTimeWindow"], Target=cur["Target"],
                        Description=cur.get("Description", ""), State=state)
    print(f"schedule {SCHEDULE}: {state}")
    return 0


def cmd_status(args) -> int:
    sess = _aws()
    lam = sess.client("lambda")
    c = lam.get_function(FunctionName=NAME)["Configuration"]
    print(f"function {NAME}: {c['State']}, last modified {c['LastModified']}")
    print(f"schedule {SCHEDULE}: {sess.client('scheduler').get_schedule(Name=SCHEDULE)['State']}")
    logs = sess.client("logs")
    events = logs.filter_log_events(logGroupName=f"/aws/lambda/{NAME}", filterPattern='"collect"',
                                    startTime=int((time.time() - 6 * 3600) * 1000)).get("events", [])
    for e in events[-args.runs:]:
        print(" ", time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["timestamp"] / 1000)), e["message"].strip()[:600])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sec = sub.add_parser("secret", help="put the Gmail service-account key from .env into Secrets Manager")
    sec.add_argument("--rotate", action="store_true", help="replace a key that is already there")
    sec.set_defaults(fn=cmd_secret)
    sub.add_parser("deploy").set_defaults(fn=cmd_deploy)
    bmk = sub.add_parser("bookmark", help="start the collector where this laptop's collection ended")
    bmk.add_argument("--db", default=str(HERE / "out" / "intake.sqlite3"))
    bmk.set_defaults(fn=cmd_bookmark)
    sub.add_parser("invoke").set_defaults(fn=cmd_invoke)
    sc = sub.add_parser("schedule")
    sc.add_argument("state", choices=["on", "off"])
    sc.set_defaults(fn=cmd_schedule)
    st = sub.add_parser("status")
    st.add_argument("--runs", type=int, default=6)
    st.set_defaults(fn=cmd_status)
    args = ap.parse_args()
    load_local_env()   # the AWS_PROFILE and region in .env, for every command, not only deploy
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
