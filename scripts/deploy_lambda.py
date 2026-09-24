"""Build and deploy the doc-intake Lambdas: the collector (Gmail -> S3) and the worker (loads).

    python scripts/deploy_lambda.py secret           put the Gmail key from .env into Secrets Manager
    python scripts/deploy_lambda.py config           upload index/pod_terminals.json for the worker
    python scripts/deploy_lambda.py seed-ledger      upload out/intake.sqlite3 as the worker's ledger - once
    python scripts/deploy_lambda.py bookmark         start the collector where this laptop's collection ended - once
    python scripts/deploy_lambda.py deploy           build, then create or update both functions
    python scripts/deploy_lambda.py invoke [--fn worker]         run one now and print what it did
    python scripts/deploy_lambda.py schedule on [--fn worker]    start its 15-minute schedule (or: off)
    python scripts/deploy_lambda.py status [--fn worker]         what exists, and the last few runs
    python scripts/deploy_lambda.py autoupload on|off|dry-run    switch the worker's auto-upload, nothing else

--fn defaults to the collector. Every step is create-or-update, so `deploy` is safe to re-run after
a code change. `deploy` never touches Secrets Manager: the Gmail key is placed by `secret`, run by
a person, and it never replaces one already there unless given --rotate. The TransportPro password
is the pay-status bot's own secret, which this script only ever grants read access to.
`seed-ledger` and `bookmark` refuse to replace what is already in S3: once a function is running,
its ledger or bookmark is its own and only it moves it.

The worker's auto-upload (intake/autofile.py) is ON after a deploy, for the terminals in
AUTO_TERMINALS. It reads these from .env: INTAKE_UPLOAD_SHEET_ID, the Google Sheet whose Upload log
tab it writes (shared with the service account as an editor); INTAKE_AUTO_UPLOAD, to deploy it
`dry-run` or `off` instead; and INTAKE_TPRO_UPLOAD_SECRET, the name of a Secrets Manager secret
holding the bot's own TransportPro login as JSON {username, password}, once one exists. `autoupload
off` stops uploads within one run without a redeploy.

What it creates, all in the account and region the .env's AWS_PROFILE points at:

    secrets     circle-doc-intake/gmail-service-account   the Gmail service-account key
                paybot/prod/tp-password                   the TransportPro password - the pay-status bot's, read only
    roles       circle-doc-intake-collector               S3 mail/ doc/ state/, the Gmail key, logs
                circle-doc-intake-worker                  S3 ledger/ (read+write), mail/ doc/ config/ (read), the TransportPro
                                                          login, the Gmail key (for the Upload log sheet), Bedrock reads, logs
                circle-doc-intake-scheduler               may invoke the circle-doc-intake-* functions
    functions   circle-doc-intake-collector               python3.12, 10 min, 512 MB, concurrency 1, no retries
                circle-doc-intake-worker                  python3.12, 10 min, 1 GB, concurrency 1, no retries
    logs        /aws/lambda/<function>                    90-day retention
    schedules   circle-doc-intake-every-15-min            the collector; rate(15 minutes)
                circle-doc-intake-worker-every-15-min     the worker; rate(15 minutes); created DISABLED
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

GMAIL_SECRET = "circle-doc-intake/gmail-service-account"
# The pay-status bot's TransportPro password, reused rather than copied (23 Sep 2026): one login,
# rotated in one place. The worker only reads it; nothing here may ever write to it.
TPRO_SECRET = "paybot/prod/tp-password"
SCHEDULER_ROLE = "circle-doc-intake-scheduler"
BOOKMARK_KEY = "state/gmail-bookmark.json"
LEDGER_KEY = "ledger/intake.sqlite3"
POD_CONFIG_KEY = "config/pod_terminals.json"
CODE_KEY = "deploy/collector.zip"
BUILD = HERE / "build" / "lambda"

# The versions the offline suite ran against, pinned so the Lambdas run what was tested. boto3 is
# bundled rather than taken from the runtime because the bookmark and ledger writes depend on S3
# If-Match, and a runtime's bundled boto3 lags behind. tzdata because the worker's working hours
# are Eastern and the runtime ships no time-zone database. The second line is the worker's
# auto-upload: the AI reader (anthropic, pydantic), PDFs and page images (pymupdf, pillow), and
# iPhone HEIC photos (pillow-heif). With it the unpacked package is ~200 MB of Lambda's 250.
DEPS = ["boto3==1.43.98", "google-auth==2.58.0", "cryptography==50.0.1", "tzdata==2026.4",
        "anthropic==1.5.0", "pydantic==2.13.5", "pymupdf==1.28.2", "pillow==12.3.0", "pillow-heif==1.7.0"]
# The auto-upload pilot's pods, by TransportPro terminal: Frankie Saiz, 24 Sep 2026.
AUTO_TERMINALS = "1160"


def _collector_env(env: dict) -> dict:
    return {"INTAKE_S3_BUCKET": env["INTAKE_S3_BUCKET"], "INTAKE_S3_PREFIX": env.get("INTAKE_S3_PREFIX", ""),
            "INTAKE_BOOKMARK_KEY": BOOKMARK_KEY, "INTAKE_GMAIL_SECRET": GMAIL_SECRET,
            "PAYBOT_GMAIL_USER": env["PAYBOT_GMAIL_USER"], "INTAKE_GROUP": "ratecon@circledelivers.com",
            "INTAKE_MAX_MESSAGES": "400"}


def _worker_env(env: dict) -> dict:
    # Working hours and the per-run cap are the conservative start: nobody has said yet how hard
    # TransportPro may be called. Raise them here, redeploy, nothing else changes.
    return {"INTAKE_S3_BUCKET": env["INTAKE_S3_BUCKET"], "INTAKE_S3_PREFIX": env.get("INTAKE_S3_PREFIX", ""),
            "INTAKE_LEDGER_KEY": LEDGER_KEY, "INTAKE_POD_CONFIG_KEY": POD_CONFIG_KEY,
            "INTAKE_TPRO_SECRET": TPRO_SECRET, "INTAKE_TPRO_USERNAME": env["PAYBOT_TP_USERNAME"],
            "INTAKE_TPRO_BASE_URL": env["PAYBOT_TP_BASE_URL"], "INTAKE_TZ": "America/New_York",
            "INTAKE_ACTIVE_HOURS": "06-20", "INTAKE_ACTIVE_DAYS": "mon-fri",
            # 150, raised from 100 on 23 Sep 2026: at 100 the loads due each working hour matched the
            # cap exactly, so any backlog made them late. TransportPro took 1,200 calls an hour cleanly.
            "INTAKE_LOAD_LIMIT": "150", "INTAKE_MAIL_DAYS": "7",
            # Auto-upload (intake/autofile.py). `autoupload off` stops it without a redeploy. The
            # sheet id is Circle's and lives in .env, not in this public repository.
            "INTAKE_AUTO_UPLOAD": env.get("INTAKE_AUTO_UPLOAD", "on"), "INTAKE_AUTO_TERMINALS": AUTO_TERMINALS,
            "INTAKE_AUTO_DAILY_USD": "10", "INTAKE_AUTO_MAX_READS": "60",
            "INTAKE_MODEL_PROVIDER": "bedrock", "INTAKE_READ_MODEL": "claude-opus-5",
            "INTAKE_GMAIL_SECRET": GMAIL_SECRET, "INTAKE_UPLOAD_SHEET_ID": env["INTAKE_UPLOAD_SHEET_ID"],
            # The bot's own TransportPro login, once an admin has made one and somebody has put it in
            # Secrets Manager as JSON {username, password}. Until then uploads go in as the reading login.
            **({"INTAKE_TPRO_UPLOAD_SECRET": env["INTAKE_TPRO_UPLOAD_SECRET"]}
               if env.get("INTAKE_TPRO_UPLOAD_SECRET") else {})}


def _collector_policy(b: str, secret_arns: list[str], region: str, account: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "Archive", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging"],
         "Resource": [f"{b}/mail/*", f"{b}/doc/*", f"{b}/state/*"]},
        # Without ListBucket a HEAD on a missing key answers 403, not 404, and every "is it already
        # there" check would read as an error instead of a no.
        {"Sid": "TellMissingFromForbidden", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": b},
        {"Sid": "GmailKey", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arns}]}


def _worker_policy(b: str, secret_arns: list[str], region: str, account: str) -> dict:
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "Ledger", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"{b}/ledger/*"},
        {"Sid": "ReadMailDocumentsAndConfig", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": [f"{b}/mail/*", f"{b}/doc/*", f"{b}/config/*"]},
        {"Sid": "ListNewMail", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": b},
        # The TransportPro login(s), and the Gmail key - used here only to write the Upload log sheet.
        {"Sid": "Logins", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arns},
        # The AI reader: the inference statement of AWS's AmazonBedrockMantleInferenceAccess, confined to
        # this account and region. Its web-search and marketplace-subscribe statements are left out.
        {"Sid": "ReadDocuments", "Effect": "Allow",
         "Action": ["bedrock-mantle:CreateInference", "bedrock-mantle:Get*", "bedrock-mantle:List*"],
         "Resource": f"arn:aws:bedrock-mantle:{region}:{account}:project/*"}]}


FUNCS = {
    "collector": {
        "name": "circle-doc-intake-collector", "schedule": "circle-doc-intake-every-15-min",
        "handler": "intake.aws_lambda.handler", "memory": 512, "secrets": lambda env: [GMAIL_SECRET],
        "env": _collector_env, "policy": _collector_policy, "log_filter": '"collect"',
        "description": "Every 15 min: new Gmail messages and their documents to S3, linked. "
                       "Never reads with a model, never touches TransportPro."},
    "worker": {
        "name": "circle-doc-intake-worker", "schedule": "circle-doc-intake-worker-every-15-min",
        "handler": "intake.aws_worker.handler", "memory": 1024,
        "secrets": lambda env: [TPRO_SECRET, GMAIL_SECRET] + ([env["INTAKE_TPRO_UPLOAD_SECRET"]]
                                                           if env.get("INTAKE_TPRO_UPLOAD_SECRET") else []),
        "env": _worker_env, "policy": _worker_policy, "log_filter": '"worker"',
        "description": "Every 15 min: new mail from S3 into the ledger; in working hours, dashboard sweep and "
                       "load checks against TransportPro; for the pilot pod, AI reads and BOL/POD uploads "
                       "logged to the Upload log sheet."},
}


def _aws():
    import os
    import boto3
    return boto3.session.Session(region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))


def _env(*need: str) -> dict:
    import os
    load_local_env()
    missing = [k for k in ("INTAKE_S3_BUCKET", *need) if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"set {missing} in .env first")
    return dict(os.environ)


# ---------------------------------------------------------------------------------- build ----

def build() -> Path:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    # Linux wheels, fetched from Windows: cryptography is compiled, and the one pip would pick for
    # this machine does not load on Lambda.
    # manylinux_2_28 as well: pymupdf publishes no manylinux2014 wheel after 1.26. Lambda's python3.12
    # runtime is Amazon Linux 2023, glibc 2.34, which runs both.
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(BUILD),
                    "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_28_x86_64",
                    "--implementation", "cp",
                    "--python-version", "3.12", "--only-binary=:all:", *DEPS], check=True)
    # The intake package, and pod_intake for the worker's AI reader. One zip serves both; each
    # function's handler picks its entry point.
    shutil.copytree(HERE / "intake", BUILD / "intake",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "__main__.py"))
    shutil.copytree(HERE / "pod_intake", BUILD / "pod_intake",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "reader_openai.py"))
    zpath = BUILD.parent / "collector.zip"
    zpath.unlink(missing_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(BUILD.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(BUILD).as_posix())
    print(f"built {zpath.name}: {zpath.stat().st_size / 1e6:.1f} MB")
    return zpath


# ----------------------------------------------------------------------------------- pieces ----

def secret_arn_pattern(sess, account: str, secret: str) -> str:
    """A secret's ARN without needing it to exist yet. Secrets Manager appends six random
    characters to every ARN; ?????? is the pattern AWS documents for granting access by name."""
    return f"arn:aws:secretsmanager:{sess.region_name}:{account}:secret:{secret}-??????"


def ensure_secret(sess, name: str, body: str, description: str, rotate: bool) -> None:
    sm = sess.client("secretsmanager")
    tags = [{"Key": "project", "Value": "circle-doc-intake"}]
    try:
        sm.describe_secret(SecretId=name)
        if rotate:
            sm.put_secret_value(SecretId=name, SecretString=body)
            print(f"secret    {name}: replaced with the value in .env")
        else:
            print(f"secret    {name}: exists, left as it is")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(Name=name, SecretString=body, Description=description, Tags=tags)
        print(f"secret    {name}: created")


def _ensure_role(iam, name: str, service: str, policy: dict, managed: list[str], account: str,
                 component: str) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        fresh = False
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Tags=[{"Key": "project", "Value": "circle-doc-intake"},
                                    {"Key": "component", "Value": component}])["Role"]["Arn"]
        fresh = True
    iam.put_role_policy(RoleName=name, PolicyName="access", PolicyDocument=json.dumps(policy))
    for m in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    print(f"role      {name}: {'created' if fresh else 'updated'}")
    if fresh:
        time.sleep(12)   # a new role is not assumable for a few seconds; create_function says so
    return arn


def ensure_function(sess, key: str, account: str, env: dict) -> str:
    spec = FUNCS[key]
    name = spec["name"]
    bucket = env["INTAKE_S3_BUCKET"]
    role = _ensure_role(sess.client("iam"), name, "lambda.amazonaws.com",
                        spec["policy"](f"arn:aws:s3:::{bucket}",
                                       [secret_arn_pattern(sess, account, x) for x in spec["secrets"](env)],
                                       sess.region_name, account),
                        ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"], account, key)
    lam = sess.client("lambda")
    # 10 minutes, not the 15 allowed: a run must end before the next one is due.
    config = dict(Role=role, Handler=spec["handler"], Runtime="python3.12", Timeout=600,
                  MemorySize=spec["memory"], Environment={"Variables": spec["env"](env)},
                  Description=spec["description"])
    tags = {"project": "circle-doc-intake", "component": key}
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_code(FunctionName=name, S3Bucket=bucket, S3Key=CODE_KEY)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, **config)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        print(f"function  {name}: code and configuration updated")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):
            try:
                lam.create_function(FunctionName=name, Code={"S3Bucket": bucket, "S3Key": CODE_KEY},
                                    Architectures=["x86_64"], Tags=tags, **config)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "assumed" not in str(e) or attempt == 5:
                    raise
                time.sleep(5)
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
        print(f"function  {name}: created")
    # One at a time, always: two collectors from one bookmark, or two workers holding one ledger,
    # is the one thing each is built to refuse. No retries, and a trigger that finds a run still going
    # is dropped after a minute rather than queued - the next quarter-hour is the retry.
    lam.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=1)
    lam.put_function_event_invoke_config(FunctionName=name, MaximumRetryAttempts=0, MaximumEventAgeInSeconds=60)
    logs = sess.client("logs")
    group = f"/aws/lambda/{name}"
    try:
        logs.create_log_group(logGroupName=group, tags=tags)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=group, retentionInDays=90)
    return lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


def ensure_schedule(sess, key: str, fn_arn: str, account: str) -> None:
    spec = FUNCS[key]
    # One scheduler role for both schedules, allowed to start any circle-doc-intake-* function. Naming
    # a single function here would silently stop the other function's schedule on the next deploy.
    fn_pattern = fn_arn.rsplit(":", 1)[0] + ":circle-doc-intake-*"
    role = _ensure_role(sess.client("iam"), SCHEDULER_ROLE, "scheduler.amazonaws.com",
                        {"Version": "2012-10-17", "Statement": [{
                            "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": fn_pattern}]},
                        [], account, "scheduler")
    sch = sess.client("scheduler")
    try:
        current = sch.get_schedule(Name=spec["schedule"])["State"]
        exists = True
    except sch.exceptions.ResourceNotFoundException:
        current, exists = "DISABLED", False
    body = dict(Name=spec["schedule"], ScheduleExpression="rate(15 minutes)", FlexibleTimeWindow={"Mode": "OFF"},
                Target={"Arn": fn_arn, "RoleArn": role, "RetryPolicy": {"MaximumRetryAttempts": 0}},
                State=current, Description=f"Runs the doc-intake {key} every 15 minutes")
    for attempt in range(6):
        try:
            (sch.update_schedule if exists else sch.create_schedule)(**body)
            break
        except sch.exceptions.ValidationException:
            # A just-created role is not assumable for a few seconds, and Scheduler checks up front.
            if attempt == 5:
                raise
            time.sleep(5)
    print(f"schedule  {spec['schedule']}: {'updated' if exists else 'created'}, {current}")


# --------------------------------------------------------------------------------- commands ----

def cmd_deploy(args) -> int:
    env = _env("PAYBOT_GMAIL_USER", "PAYBOT_TP_USERNAME", "PAYBOT_TP_BASE_URL", "INTAKE_UPLOAD_SHEET_ID")
    sess = _aws()
    account = sess.client("sts").get_caller_identity()["Account"]
    print(f"account {account}, region {sess.region_name}")
    zpath = build()
    sess.client("s3").upload_file(str(zpath), env["INTAKE_S3_BUCKET"], CODE_KEY)
    for key in FUNCS:
        fn_arn = ensure_function(sess, key, account, env)
        ensure_schedule(sess, key, fn_arn, account)
    sm = sess.client("secretsmanager")
    for secret, fix in ((GMAIL_SECRET, "run: python scripts/deploy_lambda.py secret"),
                        (TPRO_SECRET, "it belongs to the pay-status bot; ask its owner")):
        try:
            sm.describe_secret(SecretId=secret)
        except sm.exceptions.ResourceNotFoundException:
            print(f"\nNOTE: {secret} does not exist - {fix}")
    return 0


def cmd_secret(args) -> int:
    env = _env("PAYBOT_GOOGLE_SA_FILE")
    body = Path(env["PAYBOT_GOOGLE_SA_FILE"]).read_text(encoding="utf-8-sig")
    info = json.loads(body)
    if not info.get("client_email") or not info.get("private_key"):
        raise SystemExit(f"{env['PAYBOT_GOOGLE_SA_FILE']} is not a service-account JSON key")
    ensure_secret(_aws(), GMAIL_SECRET, body, "Gmail service-account key for the doc-intake collector "
                  "(domain-wide delegation, gmail.readonly)", args.rotate)
    return 0


def cmd_config(args) -> int:
    """The pod terminal map is Circle's data, kept out of the public repository; the worker reads it
    from S3. Re-run after the Load Management filter changes."""
    env = _env()
    src = Path(args.file)
    terminals = [t for t in json.loads(src.read_text(encoding="utf-8")).get("terminals", []) if t.get("in_current_view")]
    if not terminals:
        raise SystemExit(f"{src} has no terminal ticked as in the current view")
    _aws().client("s3").upload_file(str(src), env["INTAKE_S3_BUCKET"], POD_CONFIG_KEY)
    print(f"uploaded {src.name} -> s3://{env['INTAKE_S3_BUCKET']}/{POD_CONFIG_KEY} ({len(terminals)} terminals in view)")
    return 0


def cmd_seed_ledger(args) -> int:
    from intake import ledger_s3
    env = _env()
    path = Path(args.db)
    try:
        ledger_s3.seed(_aws().client("s3"), env["INTAKE_S3_BUCKET"], LEDGER_KEY, path)
    except ledger_s3.LedgerChanged as e:
        raise SystemExit(str(e)) from None
    print(f"seeded s3://{env['INTAKE_S3_BUCKET']}/{LEDGER_KEY} from {path} ({path.stat().st_size / 1e6:.1f} MB). "
          f"From now on that is the only live ledger; this laptop's copy is history.")
    return 0


def cmd_bookmark(args) -> int:
    """Start the collector exactly where this laptop's collection ended, so nothing is skipped or
    collected twice. Refused if a bookmark already exists - after that it belongs to the collector."""
    import sqlite3
    from intake import collector, store as s3store
    env = _env("PAYBOT_GMAIL_USER")
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    row = conn.execute("SELECT history_id, synced_at FROM mailbox_cursor WHERE mailbox=?",
                       (env["PAYBOT_GMAIL_USER"],)).fetchone()
    if not row:
        raise SystemExit(f"{args.db} has no Gmail cursor for {env['PAYBOT_GMAIL_USER']}")
    store = s3store.Store(env["INTAKE_S3_BUCKET"], env.get("INTAKE_S3_PREFIX", ""), client=_aws().client("s3"))
    bm = collector.Bookmark(history_id=str(row[0]), taken_at=str(row[1]))
    try:
        collector.write_bookmark(store, bm, BOOKMARK_KEY, create=True)
    except collector.BookmarkChanged:
        raise SystemExit(f"s3://{store.bucket}/{store.full(BOOKMARK_KEY)} already exists; the collector "
                         f"owns it now, and it was not replaced") from None
    print(f"bookmark set: history {bm.history_id} (collected up to {bm.taken_at}). The collector starts from there.")
    return 0


def lifecycle_rules(prefix: str, keep_days: int, archive_after: int, pii_days: int, snapshot_days: int) -> list[dict]:
    """How long the archive keeps things, as S3 lifecycle rules - S3 applies them itself, nightly.

    Chosen 23 Sep 2026 over deleting a load's mail when the load completes: "complete" is not final
    (loads reopen, POD disputes, billing corrections), claims arrive months later, a broker keeps
    shipment records for three years (49 CFR 371.3 - confirm the period with compliance), and one
    document can belong to more than one load.

    Glacier Instant Retrieval, not Deep Archive: objects here average ~0.6 MB, and Deep Archive's
    per-object transition fee costs more than its lower storage price saves at that size. GIR reads
    are still instant, so nothing that later needs a document has to wait for a restore. Objects
    under 128 KB are not transitioned at all - they would be billed as 128 KB there.

    The personal-ID rule removes the separate page in doc/ only. The same attachment is inside its
    archived email, which the mail rule keeps for the full period - stripping it from there is part
    of the reading step, which is where a page is first known to be personal ID.
    """
    p = prefix
    archive = {"Transitions": [{"Days": archive_after, "StorageClass": "GLACIER_IR"}], "Expiration": {"Days": keep_days}}
    return [
        {"ID": "intake-mail", "Status": "Enabled", "Filter": {"Prefix": f"{p}mail/"}, **archive},
        {"ID": "intake-documents", "Status": "Enabled", "Filter": {"Prefix": f"{p}doc/"}, **archive},
        {"ID": "intake-personal-id", "Status": "Enabled",
         "Filter": {"And": {"Prefix": f"{p}doc/", "Tags": [{"Key": "pii", "Value": "true"}]}},
         "Expiration": {"Days": pii_days}},
        {"ID": "intake-ledger-snapshots", "Status": "Enabled", "Filter": {"Prefix": "ledger/snapshots/"},
         "Expiration": {"Days": snapshot_days}},
        {"ID": "intake-abandoned-uploads", "Status": "Enabled", "Filter": {"Prefix": ""},
         "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}},
    ]


def cmd_lifecycle(args) -> int:
    """Show the retention rules; with --apply, put them on the bucket.

    Refuses to replace lifecycle rules it did not write: a bucket's lifecycle configuration is one
    document, and putting ours would silently delete anybody else's.
    """
    env = _env()
    bucket = env["INTAKE_S3_BUCKET"]
    prefix = (env.get("INTAKE_S3_PREFIX", "").strip("/") + "/") if env.get("INTAKE_S3_PREFIX", "").strip("/") else ""
    rules = lifecycle_rules(prefix, args.keep_days, args.archive_after, args.pii_days, args.snapshot_days)
    s3 = _aws().client("s3")
    try:
        current = s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    except s3.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") != "NoSuchLifecycleConfiguration":
            raise
        current = []
    foreign = [r.get("ID") for r in current if not str(r.get("ID", "")).startswith("intake-")]
    print(f"s3://{bucket}: {len(current)} lifecycle rule(s) now" + (f", not ours: {foreign}" if foreign else ""))
    print(f"\n  mail/ and doc/        normal storage for {args.archive_after} days, then Glacier Instant Retrieval;"
          f" deleted after {args.keep_days} days ({args.keep_days / 365:.1f} years)")
    print(f"  doc/ tagged pii=true  deleted after {args.pii_days} days")
    print(f"  ledger/snapshots/     deleted after {args.snapshot_days} days (the live ledger is never touched)")
    print(f"  unfinished uploads    cleaned up after 7 days")
    print("  state/, config/, ledger/intake.sqlite3, deploy/  kept as they are")
    if not args.apply:
        print("\nNothing changed. Run again with --apply to put these rules on the bucket.")
        return 0
    if foreign and not args.replace:
        raise SystemExit(f"the bucket already has lifecycle rules this script did not write ({foreign}); "
                         f"applying would delete them. Merge them by hand, or pass --replace if they can go.")
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": rules},
                                          TransitionDefaultMinimumObjectSize="all_storage_classes_128K")
    print(f"\napplied: {len(rules)} rule(s). S3 starts acting on them within a day.")
    return 0


def cmd_invoke(args) -> int:
    from botocore.config import Config
    name = FUNCS[args.target]["name"]
    lam = _aws().client("lambda", config=Config(read_timeout=660, retries={"max_attempts": 0}))
    started = time.time()
    r = lam.invoke(FunctionName=name, InvocationType="RequestResponse", LogType="Tail")
    print(f"{name} ran in {time.time() - started:.0f}s, status {r['StatusCode']}"
          + (f", FUNCTION ERROR {r['FunctionError']}" if r.get("FunctionError") else ""))
    print(base64.b64decode(r.get("LogResult", "")).decode("utf-8", "replace"))
    print(r["Payload"].read().decode("utf-8", "replace")[:4000])
    return 1 if r.get("FunctionError") else 0


def cmd_autoupload(args) -> int:
    """Switch the worker's auto-upload without a redeploy: only INTAKE_AUTO_UPLOAD changes."""
    lam = _aws().client("lambda")
    name = FUNCS["worker"]["name"]
    variables = lam.get_function_configuration(FunctionName=name)["Environment"]["Variables"]
    was = variables.get("INTAKE_AUTO_UPLOAD", "off")
    variables["INTAKE_AUTO_UPLOAD"] = args.mode
    lam.update_function_configuration(FunctionName=name, Environment={"Variables": variables})
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    print(f"{name}: auto-upload {was} -> {args.mode} (terminals {variables.get('INTAKE_AUTO_TERMINALS', '-')}); "
          f"the next run uses it")
    return 0


def cmd_schedule(args) -> int:
    name = FUNCS[args.target]["schedule"]
    sch = _aws().client("scheduler")
    cur = sch.get_schedule(Name=name)
    state = "ENABLED" if args.state == "on" else "DISABLED"
    sch.update_schedule(Name=name, ScheduleExpression=cur["ScheduleExpression"],
                        FlexibleTimeWindow=cur["FlexibleTimeWindow"], Target=cur["Target"],
                        Description=cur.get("Description", ""), State=state)
    print(f"schedule {name}: {state}")
    return 0


def cmd_status(args) -> int:
    spec = FUNCS[args.target]
    sess = _aws()
    c = sess.client("lambda").get_function(FunctionName=spec["name"])["Configuration"]
    print(f"function {spec['name']}: {c['State']}, last modified {c['LastModified']}")
    print(f"schedule {spec['schedule']}: {sess.client('scheduler').get_schedule(Name=spec['schedule'])['State']}")
    # Every page, not the first: CloudWatch splits even a handful of matches across pages (one per
    # log stream), and reading only the first showed runs an hour old as the latest on 23 Sep 2026.
    logs = sess.client("logs")
    kw = dict(logGroupName=f"/aws/lambda/{spec['name']}", filterPattern=spec["log_filter"],
              startTime=int((time.time() - 6 * 3600) * 1000))
    events: list[dict] = []
    while True:
        page = logs.filter_log_events(**kw)
        events += page.get("events", [])
        if not page.get("nextToken"):
            break
        kw["nextToken"] = page["nextToken"]
    events.sort(key=lambda e: e["timestamp"])
    for e in events[-args.runs:]:
        print(" ", time.strftime("%Y-%m-%d %H:%M", time.gmtime(e["timestamp"] / 1000)), e["message"].strip()[:900])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sec = sub.add_parser("secret", help="put the Gmail service-account key from .env into Secrets Manager")
    sec.add_argument("--rotate", action="store_true", help="replace a key that is already there")
    sec.set_defaults(fn=cmd_secret)
    cfg = sub.add_parser("config", help="upload the pod terminal map the worker reads")
    cfg.add_argument("--file", default=str(HERE / "index" / "pod_terminals.json"))
    cfg.set_defaults(fn=cmd_config)
    sl = sub.add_parser("seed-ledger", help="upload this laptop's ledger as the worker's - once")
    sl.add_argument("--db", default=str(HERE / "out" / "intake.sqlite3"))
    sl.set_defaults(fn=cmd_seed_ledger)
    bmk = sub.add_parser("bookmark", help="start the collector where this laptop's collection ended")
    bmk.add_argument("--db", default=str(HERE / "out" / "intake.sqlite3"))
    bmk.set_defaults(fn=cmd_bookmark)
    sub.add_parser("deploy", help="build, then create or update both functions").set_defaults(fn=cmd_deploy)
    lc = sub.add_parser("lifecycle", help="show, or with --apply set, how long the archive keeps things")
    lc.add_argument("--keep-days", type=int, default=1095, help="delete mail and documents after this many days "
                    "(default 3 years - the usual broker record period; confirm with compliance)")
    lc.add_argument("--archive-after", type=int, default=90, help="move to cheaper storage after this many days")
    lc.add_argument("--pii-days", type=int, default=90, help="delete pages tagged as personal ID after this many days")
    lc.add_argument("--snapshot-days", type=int, default=30, help="delete daily ledger snapshots after this many days")
    lc.add_argument("--apply", action="store_true", help="put the rules on the bucket (default: only show them)")
    lc.add_argument("--replace", action="store_true", help="also replace lifecycle rules this script did not write")
    lc.set_defaults(fn=cmd_lifecycle)
    au = sub.add_parser("autoupload", help="switch the worker's auto-upload: on, off or dry-run")
    au.add_argument("mode", choices=["on", "off", "dry-run"])
    au.set_defaults(fn=cmd_autoupload)
    for name, fn, extra in (("invoke", cmd_invoke, None), ("schedule", cmd_schedule, "state"),
                            ("status", cmd_status, "runs")):
        p = sub.add_parser(name)
        if extra == "state":
            p.add_argument("state", choices=["on", "off"])
        if extra == "runs":
            p.add_argument("--runs", type=int, default=6)
        p.add_argument("--fn", dest="target", choices=sorted(FUNCS), default="collector")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    load_local_env()   # the AWS_PROFILE and region in .env, for every command, not only deploy
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
