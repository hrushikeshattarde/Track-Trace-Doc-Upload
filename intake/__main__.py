r"""Command line for the intake service.

  python -m intake init                      create the ledger
  python -m intake sync                      Loop A once, no model spend
  python -m intake sync --read               ... and read new documents
  python -m intake status                    the four health numbers
  python -m intake unresolved                mail that could not be routed
  python -m intake load 2578456              everything the ledger knows about one load

Read-only against Gmail, and nothing here writes to TransportPro.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from intake import db, gmail as gm, ingest  # noqa: E402
from pod_intake.localenv import load_local_env  # noqa: E402

DEFAULT_DB = HERE / "out" / "intake.sqlite3"
GROUP = "ratecon@circledelivers.com"


def cmd_init(args) -> int:
    conn = db.connect(args.db)
    print(f"ledger ready at {args.db}")
    print("tables:", ", ".join(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")))
    return 0


def cmd_sync(args) -> int:
    conn = db.connect(args.db)
    client = gm.from_env()
    reader = None
    if args.read:
        reader = ingest.make_reader(args.model)
        print(f"reader enabled: {args.model}. New unique files will be read and billed.")
    st = ingest.sync_once(conn, client, group=args.group, reader=reader, max_messages=args.max,
                          backfill_days=args.backfill_days, verbose=args.verbose)
    print(st.line())
    if st.cursor_to:
        print(f"cursor {st.cursor_from or '(none)'} -> {st.cursor_to}   ({client.calls} Gmail calls)")
    _print_health(conn)
    return 0


def cmd_status(args) -> int:
    conn = db.connect(args.db)
    row = conn.execute("SELECT * FROM mailbox_cursor").fetchone()
    if row:
        print(f"cursor: {row['history_id']} for {row['mailbox']}, last synced {row['synced_at']} ({row['sync_mode']})")
    else:
        print("cursor: not seeded yet - run 'python -m intake sync'")
    _print_health(conn, full=True)
    return 0


def cmd_unresolved(args) -> int:
    conn = db.connect(args.db)
    rows = conn.execute(
        "SELECT u.message_id, u.thread_id, u.reason, u.attempts, u.created_at, m.from_domain, m.part_count "
        "FROM unresolved u LEFT JOIN message m USING (message_id) ORDER BY u.created_at LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("nothing unresolved.")
        return 0
    print(f"{len(rows)} unresolved message(s) - each needs a load number or a human:\n")
    for r in rows:
        print(f"  {r['created_at']}  {r['from_domain'] or '(unknown)':28}  {r['part_count']} part(s)  "
              f"attempts {r['attempts']}\n      {r['reason']}\n      thread {r['thread_id']}")
    return 0


def cmd_load(args) -> int:
    conn = db.connect(args.db)
    lid = args.load_id
    row = conn.execute("SELECT * FROM load WHERE load_id=?", (lid,)).fetchone()
    if not row:
        print(f"load {lid} is not in the ledger.")
        return 1
    print(f"load {lid}: state={row['state']} source={row['source']} next_check={row['next_check_at']}")
    msgs = conn.execute("SELECT * FROM message WHERE load_id=? ORDER BY internal_date", (lid,)).fetchall()
    print(f"\n{len(msgs)} message(s):")
    for m in msgs:
        print(f"  {m['internal_date']}  {m['from_domain'] or '':28}  via {m['routing_tier']:9}  {m['part_count']} part(s)")
    files = conn.execute(
        "SELECT a.sha256, a.filename, a.bytes, a.document_type, a.cost_usd, COUNT(p.message_id) AS seen "
        "FROM attachment a JOIN part p ON p.sha256 = a.sha256 "
        "JOIN message m ON m.message_id = p.message_id WHERE m.load_id=? GROUP BY a.sha256", (lid,)).fetchall()
    print(f"\n{len(files)} unique file(s):")
    for f in files:
        dup = f" (appeared {f['seen']}x in the thread)" if f["seen"] > 1 else ""
        typ = f["document_type"] or "not read yet"
        print(f"  {f['sha256'][:12]}  {(f['filename'] or '')[:40]:42} {f['bytes'] // 1024:5} KB  {typ}{dup}")
    return 0


def _print_health(conn, full: bool = False) -> None:
    c = db.counts(conn)
    ok = "OK" if c["custody_gap"] == 0 else "BROKEN"
    print(f"\ncustody   {ok}: {c['messages_seen']} seen = {c['messages_bound']} bound + "
          f"{c['messages_unresolved']} unresolved + {c['custody_gap']} unaccounted")
    print(f"coverage  {c['loads']} load(s) in the ledger, {c['loads_overdue']} due a check"
          + (f", oldest due {c['oldest_overdue_check']}" if c["oldest_overdue_check"] else ""))
    print(f"dedup     {c['attachment_occurrences']} attachment occurrence(s) -> {c['unique_files']} unique file(s); "
          f"{c['reads_avoided']} read(s) avoided")
    print(f"spend     ${c['model_spend_usd']:.3f} across {c['unique_files']} file(s)")
    if c["threads_conflicted"]:
        print(f"conflicts {c['threads_conflicted']} thread(s) where a reply named a different load")
    if full:
        print("\npart decisions:")
        for name, n in db.part_decisions(conn):
            print(f"  {name:20} {n}")


def main() -> int:
    load_local_env()
    ap = argparse.ArgumentParser(prog="intake", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)

    s = sub.add_parser("sync", help="one pass of Loop A")
    s.add_argument("--group", default=GROUP)
    s.add_argument("--max", type=int, default=500, help="cap on messages fetched this pass")
    s.add_argument("--backfill-days", type=int, default=1,
                   help="window used on the first run, or after the cursor expires")
    s.add_argument("--read", action="store_true", help="read new unique documents (costs money)")
    s.add_argument("--model", default="claude-opus-5")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_sync)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    u = sub.add_parser("unresolved")
    u.add_argument("--limit", type=int, default=25)
    u.set_defaults(fn=cmd_unresolved)

    l = sub.add_parser("load")
    l.add_argument("load_id", type=int)
    l.set_defaults(fn=cmd_load)

    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
