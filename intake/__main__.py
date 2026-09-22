r"""Command line for the intake service.

  python -m intake init                      create the ledger
  python -m intake sync                      Loop A once, no model spend
  python -m intake sync --read               ... and read new documents
  python -m intake status                    the four health numbers
  python -m intake unresolved                mail that could not be routed
  python -m intake load 2578456              everything the ledger knows about one load

  python -m intake reconcile                 Loop B: give every dashboard load a ledger row
  python -m intake reconcile --days-back 350 --authoritative   the nightly audit
  python -m intake backfill                  one-off mail history for loads new to the view
  python -m intake backfill --restale 24     ... and re-search loads still short paperwork
  python -m intake reconsider --loads 1,2    re-take attachments the free filters dropped
  python -m intake tpro-scan                 look at the paperwork already ON the stuck loads
  python -m intake tpro-scan --read          ... and read what has never been read
  python -m intake read --loads 1,2,3         read documents already ingested but never read
  python -m intake loads                     Loop B: check every load whose next check is due
  python -m intake queue                     the work queue, most urgent first

  python -m intake file                      judge read documents into the review queue (no writes)
  python -m intake review                    what is waiting for a person
  python -m intake notify                    what the team would be told, by terminal
  python -m intake review approve 12 --by me
  python -m intake file --execute            WRITES: upload what has been approved
  python -m intake export --out queue.csv    the review queue as a sheet, with the evidence

Read-only against Gmail. The ONLY command that writes to TransportPro is
`file --execute`; everything else, including plain `file`, is a dry run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from intake import (db, export, filing, gmail as gm, ingest, loadloop, notify, review,  # noqa: E402
                    tpro as tp, tprodocs)
from pod_intake.localenv import load_local_env  # noqa: E402

DEFAULT_DB = HERE / "out" / "intake.sqlite3"
GROUP = "ratecon@circledelivers.com"
POD_MAP = HERE / "index" / "pod_terminals.json"


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
                          backfill_days=args.backfill_days, max_spend_usd=args.max_spend,
                          verbose=args.verbose)
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
    on_load = tprodocs.summary(conn, lid)
    if on_load:
        print(f"\n{len(on_load)} file(s) attached to the load in TransportPro:")
        for f in on_load:
            known = (f"reads as {f['document_type']}" if f["read_"] else
                     ("bytes known, not read" if f["sha256"] else "not downloaded"))
            same = f"  [same bytes as {f['mail_copies']} mail copy/copies]" if f["mail_copies"] else ""
            print(f"  {(f['file_type_name'] or '')[:22]:24} {(f['filename'] or '')[:22]:24} "
                  f"{(f['comments'] or '')[:20]:22} {known}{same}")
    dropped = db.dropped_parts(conn, lid)
    if dropped:
        print(f"\n{len(dropped)} attachment(s) the free filters dropped before any download:")
        for d in dropped:
            dims = f"{d['width']}x{d['height']}" if d["width"] else ""
            print(f"  {(d['filename'] or '')[:40]:42} {(d['bytes'] or 0) // 1024:5} KB  {dims:11} {d['decision']}")
        print(f"  -> python -m intake reconsider --loads {lid}   (add --read to have them read)")
    return 0


def cmd_reconcile(args) -> int:
    conn = db.connect(args.db)
    terminals, levels = loadloop.pod_terminals(args.pod_map)
    if args.terminals:
        terminals = [int(x) for x in args.terminals.replace(",", " ").split()]
    if not terminals:
        print(f"no terminals: {args.pod_map} is missing or has none ticked. Pass --terminals.")
        return 1
    client = tp.from_env()
    print(f"reconciling {len(terminals)} terminal(s), pickups {args.days_back}d back to {args.days_forward}d ahead"
          + (f", service level {sorted(levels)}" if levels else ""))
    rs = loadloop.reconcile(conn, client, terminals=terminals, days_back=args.days_back,
                            days_forward=args.days_forward, scope_levels=levels,
                            authoritative=args.authoritative, verbose=args.verbose)
    print(rs.line() + f"   ({client.calls} TransportPro calls)")
    _print_health(conn)
    return 0


def cmd_backfill(args) -> int:
    """Close the history hole: one targeted Gmail search per in-view load that has never had one."""
    conn = db.connect(args.db)
    client = gm.from_env()
    reader = ingest.make_reader(args.model) if args.read else None
    todo = ([int(x) for x in args.loads.replace(",", " ").split()] if args.loads
            else db.loads_needing_backfill(conn, args.limit, restale_hours=args.restale))
    if not todo:
        print("every in-view load has had its mail history searched."
              + ("" if args.restale else "  (--restale N re-searches loads still short paperwork)"))
        return 0
    print(f"backfilling {len(todo)} load(s)"
          + (f", re-searching anything not looked at for {args.restale}h" if args.restale else "")
          + (f", reading new documents with {args.model}" if reader else ""))
    st = ingest.backfill_pass(conn, client, group=args.group, limit=args.limit, reader=reader,
                              max_spend_usd=args.max_spend, load_ids=todo,
                              restale_hours=args.restale, verbose=args.verbose)
    print(st.line() + f"   ({client.calls} Gmail calls)")
    c = db.counts(conn)
    print(f"  {c['in_view_unbackfilled']} in-view load(s) still to backfill")
    _print_health(conn)
    return 0


def cmd_reconsider(args) -> int:
    """Re-take the attachments the free filters dropped, for loads a person has named.

    Not a pass and not automatic: the filters are right nearly always, and reconsidering everything
    would pay to read hundreds of email signatures. It exists because the decision is otherwise
    final - nothing re-examines a processed message - so a badly photographed BOL is discarded with
    no trace in the work queue.
    """
    conn = db.connect(args.db)
    loads = [int(x) for x in args.loads.replace(",", " ").split()]
    rows = [r for lid in loads for r in db.dropped_parts(conn, lid)]
    if not rows:
        print(f"nothing to reconsider on load(s) {', '.join(str(x) for x in loads)}: "
              f"no attachment was dropped by the size/shape filters.")
        return 0
    print(f"{len(rows)} dropped attachment(s) across {len(loads)} load(s):")
    for r in rows:
        dims = f"{r['width']}x{r['height']}" if r["width"] else ""
        print(f"  load {r['load_id']}  {(r['filename'] or '')[:36]:38} {(r['bytes'] or 0) // 1024:5} KB "
              f"{dims:11} dropped as {r['decision']}")
    reader = ingest.make_reader(args.model) if args.read else None
    if not reader:
        print("\n  recovering without reading. Add --read to have the model read them (costs money).")
    st = ingest.reconsider_parts(conn, gm.from_env(), load_ids=loads, reader=reader,
                                 max_spend_usd=args.max_spend, limit=args.limit, verbose=True)
    print(f"\nrecovered {st.parts['keep']}, read {st.reads}, errors {st.read_errors}, "
          f"spend ${st.cost_usd:.3f}")
    _print_health(conn)
    return 0


def cmd_tpro_scan(args) -> int:
    """Loop C: look at the documents already attached to loads whose status has not cleared.

    Listing and downloading are free; only --read costs money, and the ledger's de-duplication means
    a file that also arrived by email is identified for nothing.
    """
    conn = db.connect(args.db)
    client = tp.from_env()
    loads = ([int(x) for x in args.loads.replace(",", " ").split()] if args.loads
             else tprodocs.due_loads_for_scan(conn, tprodocs.SCAN_STATES, args.limit))
    if not loads:
        print("no loads to scan: nothing in-view is carrying paperwork under a type that has not cleared.")
        return 0
    reader = ingest.make_reader(args.model) if args.read else None
    print(f"scanning {len(loads)} load(s) for paperwork already on the load"
          + (f", reading what is new with {args.model}" if reader else ", recording and hashing only"))
    st = tprodocs.scan_pass(conn, client, load_ids=loads, reader=reader, limit=args.limit,
                            max_spend_usd=args.max_spend, verbose=args.verbose)
    print("\n" + tprodocs.line(st, len(loads)) + f"   ({client.calls} TransportPro calls)")
    both = conn.execute(
        "SELECT COUNT(*) FROM tpro_file t WHERE t.sha256 IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM part p WHERE p.sha256 = t.sha256)").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM tpro_file WHERE sha256 IS NOT NULL").fetchone()[0]
    print(f"  {both} of {total} hashed file(s) on loads are byte-identical to something in the mail ledger")
    _print_health(conn)
    return 0


def cmd_read(args) -> int:
    conn = db.connect(args.db)
    loads = [int(x) for x in args.loads.replace(",", " ").split()] if args.loads else None
    if args.retry_now:
        n = db.clear_read_backoff(conn, loads)
        if n:
            print(f"cleared the retry delay on {n} document(s) - the cause is reported fixed")
    pend = db.unread_attachments(conn, load_ids=loads, in_view_only=not args.any_load, limit=args.limit)
    if not pend:
        waiting = db.reads_in_backoff(conn, load_ids=loads, in_view_only=not args.any_load)
        if waiting:
            print(f"nothing readable right now: {waiting} document(s) failed and are waiting out a "
                  f"retry delay. Re-run with --retry-now if the cause is fixed.")
        else:
            print("nothing to read: every document in scope already has an extraction.")
        return 0
    print(f"{len(pend)} document(s) to read with {args.model}. This costs money.")
    st = ingest.read_pending(conn, gm.from_env(), reader=ingest.make_reader(args.model),
                             load_ids=loads, in_view_only=not args.any_load, limit=args.limit,
                             max_spend_usd=args.max_spend, verbose=True)
    print("")
    print(f"read {st.reads}, errors {st.read_errors}, spend ${st.cost_usd:.3f}")
    _print_health(conn)
    return 0


def cmd_loads(args) -> int:
    conn = db.connect(args.db)
    _, levels = loadloop.pod_terminals(args.pod_map)
    client = tp.from_env()
    ds = loadloop.drain(conn, client, limit=args.limit, scope_levels=levels, verbose=args.verbose)
    print(ds.line())
    _print_health(conn)
    return 0


def cmd_queue(args) -> int:
    conn = db.connect(args.db)
    rows = loadloop.work_queue(conn, args.limit)
    if not rows:
        print("nothing checked yet - run 'python -m intake loads'")
        return 0
    print(f"{'load':>9}  {'state':22} {'stage':13} {'docs':7} {'customer':26} action")
    for r in rows:
        docs, unread = db.load_doc_evidence(conn, int(r["load_id"]))
        dropped = db.load_dropped_evidence(conn, int(r["load_id"]))
        mark = f"{docs}" + (f"/{unread}?" if unread else "") + (f" -{dropped}" if dropped else "")
        print(f"{r['load_id']:>9}  {(r['state'] or ''):22} {(r['stage'] or ''):13} {mark:7} "
              f"{(r['customer'] or '')[:26]:26} {(r['action'] or '')[:70]}")
    print("\n  docs column: documents in the mail ledger for that load; /N? = not read yet;")
    print("               -N = attachments the size/shape filters dropped (intake reconsider)")
    return 0


def cmd_propose(args) -> int:
    """Judge every read document against its load and put the result in the review queue.

    Writes nothing to TransportPro. With --execute it additionally files what a person has already
    approved (and, with --auto, what the gate cleared by itself).
    """
    conn = db.connect(args.db)
    pairs = filing.candidates(conn, limit=args.limit)
    if not pairs:
        print("nothing to judge: no read documents without a filing or a review decision.")
    gates: dict[str, int] = {}
    for load_id, sha in pairs:
        p = filing.propose(conn, load_id, sha, requirements_path=args.requirements, allow_auto=args.auto)
        gates[p.gate] = gates.get(p.gate, 0) + 1
        if p.gate != filing.AUTO:
            review.enqueue(conn, load_id=p.load_id, sha256=p.sha256, message_id=None, kind=p.kind,
                           reason=p.reason, proposed_type=p.document_type, proposed_comment=p.comment)
            # notify.NOTIFIABLE, not "anything that is not shadow". The queue keeps every
            # judgement; this only wakes somebody when there is something for them to do.
            if notify.worth_telling(p.kind, p.document_type):
                notify.record(conn, load_id=p.load_id, event=notify.REFUSED, sha256=p.sha256,
                              kind=p.kind, document_type=p.document_type, filename=p.filename,
                              reason=p.reason)
        if args.verbose or p.gate == filing.AUTO:
            print("  " + p.line())
    if pairs:
        print(f"judged {len(pairs)}: " + ", ".join(f"{k} {v}" for k, v in sorted(gates.items())))

    if not args.execute:
        c = review.counts(conn)
        print(f"\nreview queue: {c['_pending']} pending, {c['_approved']} approved and waiting to file, "
              f"{c['_filed']} filed, {c['_rejected']} rejected")
        if c["_approved"]:
            print("  run with --execute to file the approved ones (this writes to TransportPro)")
        return 0

    todo = [(r["id"], int(r["load_id"]), r["sha256"]) for r in review.approved(conn, args.limit)]
    if args.auto:
        todo += [(None, lid, sha) for lid, sha in pairs
                 if filing.propose(conn, lid, sha, requirements_path=args.requirements,
                                   allow_auto=True).gate == filing.AUTO]
    if not todo:
        print("\n--execute: nothing approved to file.")
        return 0

    print(f"\n*** WRITING TO TRANSPORTPRO: {len(todo)} document(s) will be uploaded to live loads ***")
    client = tp.from_env()
    gclient = gm.from_env()
    filed = failed = 0
    for review_id, load_id, sha in todo:
        p = filing.propose(conn, load_id, sha, requirements_path=args.requirements, allow_auto=True)
        try:
            out = filing.execute(conn, client, gclient, p, dry_run=False, review_id=review_id)
        except Exception as e:  # noqa: BLE001 - one upload failing must not strand the rest
            failed += 1
            review.enqueue(conn, load_id=load_id, sha256=sha, message_id=None, kind=review.ERROR,
                           reason=f"upload failed: {type(e).__name__}: {e}"[:250],
                           proposed_type=p.document_type, proposed_comment=p.comment)
            print(f"  ! load {load_id} {sha[:12]}: {type(e).__name__}: {e}")
            continue
        if out.get("filed"):
            filed += 1
            print(f"  + load {load_id} filed as {out['document_type']} (TransportPro file {out.get('tpro_file_id') or '?'})")
        else:
            print(f"  - load {load_id} {sha[:12]}: {out.get('why')}")
    print(f"\nfiled {filed}, failed {failed}")
    return 0


def cmd_notify(args) -> int:
    """What the team would be told, grouped the way it would be sent.

    Prints by default and writes nothing. --mark records the digest as delivered, which is what a
    real channel will call once it has confirmed the message went; it is separate so that building
    a digest and sending one can never be confused for each other.
    """
    conn = db.connect(args.db)
    rows = notify.pending(conn, limit=args.limit)
    if not rows:
        print("nothing to tell anybody: no filing or refusal has been recorded since the last digest.")
        return 0
    groups = notify.digest(rows, group=args.group_by)
    print(f"{len(rows)} notice(s) across {len(groups)} {args.group_by}(s)\n")
    for who, lines, ids in groups:
        print(notify.render(who, lines, args.group_by))
        print()
    if args.mark:
        n = notify.mark_delivered(conn, [i for _, _, ids in groups for i in ids], channel=args.channel)
        print(f"marked {n} notice(s) delivered via '{args.channel}'")
    else:
        print("  nothing was marked delivered. Re-run with --mark once a channel has actually sent it.")
    return 0


def cmd_review(args) -> int:
    conn = db.connect(args.db)
    if args.action in ("approve", "reject"):
        ok = review.decide(conn, args.id, approve=args.action == "approve", by=args.by, note=args.note)
        if not ok:
            print(f"review item {args.id} is not pending (already decided, or no such id).")
            return 1
        print(f"item {args.id} {args.action}d by {args.by}."
              + (" Run 'python -m intake file --execute' to file it." if args.action == "approve" else ""))
        return 0

    rows = review.pending(conn, limit=args.limit, kind=args.kind)
    c = review.counts(conn)
    if not rows:
        print(f"review queue empty. {c['_approved']} approved, {c['_filed']} filed, {c['_rejected']} rejected.")
        return 0
    print(f"{c['_pending']} pending  ({', '.join(f'{k} {v}' for k, v in sorted(c.items()) if not k.startswith('_'))})\n")
    for r in rows:
        print(f"  [{r['id']:>4}] {r['kind']:16} load {r['load_id']}  {(r['customer'] or '')[:30]}")
        print(f"         {r['reason'][:110]}")
        if r["proposed_type"]:
            print(f"         would file as: {r['proposed_type']}")
        print(f"         file {(r['sha256'] or '')[:12]} {(r['filename'] or '')[:40]}")
    print("\n  approve:  python -m intake review approve <id> --by yourname")
    print("  reject :  python -m intake review reject  <id> --by yourname --note why")
    for kind in sorted({r["kind"] for r in rows}):
        print(f"\n  {kind}: {review.KIND_HELP.get(kind, '')}")
    return 0


def cmd_export(args) -> int:
    conn = db.connect(args.db)
    client = tp.from_env()
    loads = [int(x) for x in args.loads.replace(",", " ").split()] if args.loads else None
    rows = export.build_rows(conn, client, limit=args.limit, load_ids=loads, verbose=args.verbose)
    out = export.write_csv(rows, Path(args.out))
    if loads:
        summary = export.write_load_summary(export.load_summary(conn, loads),
                                            Path(args.out).with_name(Path(args.out).stem + "_by_load.csv"))
        print(f"per-load summary -> {summary}")
    by = {}
    for r in rows:
        by[r["ready"]] = by.get(r["ready"], 0) + 1
    print(f"{len(rows)} row(s) -> {out}   ({client.calls} TransportPro calls)")
    for k, n in sorted(by.items(), key=lambda kv: -kv[1]):
        print(f"  {k:22} {n}")
    print("")
    print("  'ready' means every automatic gate passed and the page corroborates the load.")
    print("  It does NOT mean a person has looked at the image - that is the eyeballed_by column.")
    return 0


def _print_health(conn, full: bool = False) -> None:
    c = db.counts(conn)
    ok = "OK" if c["custody_gap"] == 0 else "BROKEN"
    print(f"\ncustody   {ok}: {c['messages_seen']} seen = {c['messages_bound']} bound + "
          f"{c['messages_unresolved']} unresolved + {c['custody_gap']} unaccounted")
    print(f"mail      {c['in_view'] - c['in_view_unbackfilled']}/{c['in_view']} in-view load(s) have had "
          f"their mail history searched; {c['in_view_unbackfilled']} still blind")
    print(f"coverage  {c['loads']} load(s) in the ledger, {c['loads_overdue']} due a check"
          + (f", oldest due {c['oldest_overdue_check']}" if c["oldest_overdue_check"] else ""))
    print(f"dedup     {c['attachment_occurrences']} attachment occurrence(s) -> {c['unique_files']} unique file(s); "
          f"{c['reads_avoided']} read(s) avoided")
    print(f"spend     ${c['model_spend_usd']:.3f} across {c['unique_files']} file(s)")
    if c["read_failures"]:
        print(f"failures  {c['read_failures_retrying']} read(s) waiting on a retry, "
              f"{c['read_failures_permanent']} that nothing will read again")
    if c["read_failures_paused"]:
        print(f"paused    {c['read_failures_paused']} read(s) held because the READER was "
              f"unavailable - no credit, quota or key. No retries are being spent; they resume "
              f"as soon as it works")
    if c["dropped_recoverable"]:
        print(f"dropped   {c['dropped_recoverable']} attachment(s) on in-view loads rejected by the "
              f"size/shape filters (see 'intake queue', recover with 'intake reconsider')")
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
                   help="window used on the first run. After a cursor expiry it is only a FLOOR: "
                        "the window is widened to cover the whole outage")
    s.add_argument("--read", action="store_true", help="read new unique documents (costs money)")
    s.add_argument("--model", default="claude-opus-5")
    s.add_argument("--max-spend", type=float, default=None,
                   help="with --read: stop reading once this pass has spent this much (USD). "
                        "Ingest continues; unread documents are picked up by a later pass")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_sync)

    r = sub.add_parser("reconcile", help="Loop B: give every dashboard load a ledger row")
    r.add_argument("--terminals", default=None, help="comma-separated terminal ids instead of the ticked pods")
    r.add_argument("--days-back", type=int, default=3,
                   help="pickup window start; 3 for the hourly pass, ~350 for the nightly audit")
    r.add_argument("--days-forward", type=int, default=45)
    r.add_argument("--pod-map", default=str(POD_MAP))
    r.add_argument("--authoritative", action="store_true",
                   help="this sweep covered the WHOLE window, so loads it did not return have left "
                        "the view and may be evicted. Never set it on a narrow sweep")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_reconcile)

    bf = sub.add_parser("backfill", help="one-off mail history for in-view loads never searched")
    bf.add_argument("--group", default=GROUP)
    bf.add_argument("--limit", type=int, default=100)
    bf.add_argument("--loads", default=None, help="comma-separated load numbers instead of the next N")
    bf.add_argument("--read", action="store_true", help="read new documents found (costs money)")
    bf.add_argument("--model", default="claude-opus-5")
    bf.add_argument("--max-spend", type=float, default=None)
    bf.add_argument("--restale", type=int, default=None, metavar="HOURS",
                    help="also re-search in-view loads that are still short paperwork and have not "
                         "been searched for this many hours. The safety net under a cursor gap: "
                         "Loop A cannot look backwards and Loop B never calls Gmail")
    bf.add_argument("-v", "--verbose", action="store_true")
    bf.set_defaults(fn=cmd_backfill)

    ts = sub.add_parser("tpro-scan", help="look at the paperwork already attached to stuck loads")
    ts.add_argument("--loads", default=None, help="comma-separated load numbers; default the stuck ones")
    ts.add_argument("--limit", type=int, default=50)
    ts.add_argument("--read", action="store_true", help="read what has never been read (costs money)")
    ts.add_argument("--model", default="claude-opus-5")
    ts.add_argument("--max-spend", type=float, default=None)
    ts.add_argument("-v", "--verbose", action="store_true")
    ts.set_defaults(fn=cmd_tpro_scan)

    rc = sub.add_parser("reconsider", help="re-take attachments the free filters dropped, by load")
    rc.add_argument("--loads", required=True, help="comma-separated load numbers. Required: this is "
                                                   "a deliberate override, never a sweep")
    rc.add_argument("--read", action="store_true", help="read what is recovered (costs money)")
    rc.add_argument("--model", default="claude-opus-5")
    rc.add_argument("--max-spend", type=float, default=None)
    rc.add_argument("--limit", type=int, default=50, help="cap per load")
    rc.set_defaults(fn=cmd_reconsider)

    rd = sub.add_parser("read", help="read documents already ingested but never read")
    rd.add_argument("--loads", default=None, help="comma-separated load numbers; default all in-view")
    rd.add_argument("--any-load", action="store_true", help="include loads outside the dashboard view")
    rd.add_argument("--limit", type=int, default=50)
    rd.add_argument("--model", default="claude-opus-5")
    rd.add_argument("--max-spend", type=float, default=None)
    rd.add_argument("--retry-now", action="store_true",
                    help="bring waiting retries forward: use after fixing credentials or credits")
    rd.set_defaults(fn=cmd_read)

    ld = sub.add_parser("loads", help="Loop B: check every load whose next check is due")
    ld.add_argument("--limit", type=int, default=100)
    ld.add_argument("--pod-map", default=str(POD_MAP))
    ld.add_argument("-v", "--verbose", action="store_true")
    ld.set_defaults(fn=cmd_loads)

    q = sub.add_parser("queue", help="the work queue, most urgent first")
    q.add_argument("--limit", type=int, default=40)
    q.set_defaults(fn=cmd_queue)

    f = sub.add_parser("file", help="judge read documents; with --execute, file the approved ones")
    f.add_argument("--limit", type=int, default=200)
    f.add_argument("--requirements", default=str(HERE / "index" / "customer_requirements.json"))
    f.add_argument("--auto", action="store_true",
                   help="allow the auto gate. Without it every proposal goes to review (shadow mode)")
    f.add_argument("--execute", action="store_true",
                   help="WRITES TO TRANSPORTPRO: upload what has been approved. Off by default")
    f.add_argument("-v", "--verbose", action="store_true")
    f.set_defaults(fn=cmd_propose)

    nt = sub.add_parser("notify", help="what the team would be told: filings and refusals, by team")
    nt.add_argument("--group-by", default="terminal", choices=["terminal", "customer"])
    nt.add_argument("--limit", type=int, default=500)
    nt.add_argument("--mark", action="store_true",
                    help="record these as delivered. Only for a channel that has actually sent them")
    nt.add_argument("--channel", default="printed")
    nt.set_defaults(fn=cmd_notify)

    rv = sub.add_parser("review", help="the review queue")
    rv.add_argument("action", nargs="?", choices=["list", "approve", "reject"], default="list")
    rv.add_argument("id", nargs="?", type=int)
    rv.add_argument("--by", default="", help="who is deciding (recorded against the item)")
    rv.add_argument("--note", default="")
    rv.add_argument("--kind", default=None, help="only this kind, e.g. pii, rules_failed, shadow")
    rv.add_argument("--limit", type=int, default=25)
    rv.set_defaults(fn=cmd_review)

    ex = sub.add_parser("export", help="the review queue as a CSV, with the evidence for each row")
    ex.add_argument("--out", default=str(HERE / "out" / "review_queue.csv"))
    ex.add_argument("--limit", type=int, default=500)
    ex.add_argument("--loads", default=None,
                    help="comma-separated load numbers; also writes a one-row-per-load summary")
    ex.add_argument("-v", "--verbose", action="store_true")
    ex.set_defaults(fn=cmd_export)

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
