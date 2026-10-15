r"""Offline replay: read archived pages again with a candidate reader configuration and compare each
new reading with the one the bot stored, on exactly what the judge uses.

    .venv\Scripts\python.exe scripts\replay.py --ledger out\ledger-copy\intake.sqlite3 --sample 100 --seed 1 --dry-run
    .venv\Scripts\python.exe scripts\replay.py --ledger ... --sample 100 --seed 1 --form compact --pair 20 --fetch-loads

Every change to what the model is asked - a shorter answer, another model, another effort - costs
money to get wrong twice: once on the day's reads and again on the paperwork those reads file. So it
is proved here first, on pages the bot has already read, before anything is deployed.

The sample is stratified - PODs, BOLs, lumper receipts, other paperwork, by the stored reading - and
seeded, so a run can be repeated page for page. Only emailed pages are replayed (their bytes are in
S3 at attachment.s3_key; a texted picture's are on the load in TransportPro), and only pages whose
stored reading came from the baseline model in one piece, so the comparison is form against form and
not model against model. Each page is fetched from S3 (checked against its SHA-256, cached under
--cache), read with the candidate configuration, and compared with the stored reading
(compare_readings):

    document_type; the side of the 85% bar and the 70% floor the confidence falls on; receiver_signed,
    stamp_present, receiver_date; the reference numbers, normalised as match_facts_detail does, and -
    when the load's JSON is known - the facts they match on the load; the page roles and the pages
    that would go up (paper_pages); times.at_stop and the delivery-time evidence; the parties' names
    and cities; pieces, weight and amount; what the judge would make of the page (POD, BOL, lumper
    receipt or not paperwork, from classify_type and the load's stage as the ledger has it now).

A page's FILING DECISION is counted as changed when the judge's kind flips, the confidence crosses the
85% bar or the 70% floor, a reference number that matched the load is lost (or the facts check flips),
the receiver's evidence (signature or stamp) is lost or gained, the pages that would go up change, or
a corroborated POD stops being one. The adoption rule for a candidate: ship only if at least 98 of
100 filing decisions are identical AND no POD loses its receiver evidence.

The ledger holds no copy of TransportPro's load JSON, so --fetch-loads GETs each sampled page's load
once (read-only) into --loads, a JSON cache; without it the numbers are compared on their own.

--pair N re-reads the first N sampled pages with the DEFAULT form as well. Two reads of one page by
one model do not agree perfectly even with nothing changed, so the pairs measure that noise floor,
and they calibrate the stored readings' output tokens (the ledger keeps a read's cost, not its
tokens). --dry-run prints the sample and the cost estimate and calls nothing.

Output: a table on stdout (agreement per field, the pages whose decision would change, cost per read
old against new), <out>/diffs.json with every page's diff, <out>/summary.json, and each new reading
under <out>/readings/<candidate>/<sha256>.json as soon as it comes back - a run that stops half way
keeps what it paid for, and a rerun reads only what is missing.

Nothing here writes to the ledger, to S3 or to TransportPro.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from intake import autofile  # noqa: E402
from intake.autofile import (AT_CONSIGNEE, LUMPER_ROLES, PAPER_ROLES, REFERENCE_KINDS, Settings,  # noqa: E402
                             _norm, _whole, corroborated_pod, is_lumper_receipt, match_facts_detail, paper_pages)
from pod_intake.matcher import classify_type  # noqa: E402
from pod_intake.schema import Extraction  # noqa: E402

STRATA = ("pod", "bol", "lumper", "other")
STRATUM_OF = {"proof_of_delivery": "pod", "bill_of_lading": "bol", "lumper": "lumper"}
DEFAULT_SHARES = "pod=35,bol=35,lumper=10,other=20"       # per cent of the sample; the adoption rule is about PODs and BOLs
DEFAULT_BUCKET = "circle-load-doc-intake"
# The judge's thresholds (intake.autofile.Settings): the 85% bar and the 70% floor a corroborated page may go up from.
BAR, FLOOR = Settings().min_confidence, Settings().pod_floor
# json.dumps characters per output token of a reading, when no paired full-form read has measured it.
# From 20 paired reads on 15 Oct 2026 (see the replay's own summary); a rerun with --pair measures it again.
DEFAULT_CHARS_PER_TOKEN = 2.6
WARM_READ_USD = {"full": 0.049, "compact": 0.035}         # a one-page read with the cache warm, for the dry run's estimate


# ------------------------------------------------------------------------------------ the sample ----

def stratum_of(document_type: str | None) -> str:
    return STRATUM_OF.get(document_type or "", "other")


def shares_of(text: str) -> dict[str, float]:
    """'pod=35,bol=35,lumper=10,other=20' -> {stratum: share}; the shares are normalised to 1."""
    out: dict[str, float] = {}
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        name, _, value = part.partition("=")
        if name not in STRATA:
            raise SystemExit(f"unknown stratum {name!r}; the strata are {', '.join(STRATA)}")
        out[name] = float(value or 0)
    total = sum(out.values()) or 1.0
    return {k: out.get(k, 0.0) / total for k in STRATA}


OUTCOME_RANK = {"uploaded": 0, "held": 1, "waiting": 2, "not_needed": 3, "on_file": 4, "outside_pilot": 5, "not_bol_pod": 6}


def candidates(conn: sqlite3.Connection, baseline_model: str) -> list[dict]:
    """Every emailed page with a stored reading by `baseline_model`, read in one piece, with its bytes
    in S3 - and the load it was judged on: the autofile row that says most about it (an upload before a
    hold before a not-needed...), else the load its email was routed to."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT a.sha256, a.filename, a.bytes, a.document_type, a.cost_usd, a.read_at, a.extraction_json, a.s3_key "
        "FROM attachment a WHERE a.extraction_json IS NOT NULL AND a.s3_key IS NOT NULL AND a.error IS NULL "
        "AND a.model = ? AND EXISTS (SELECT 1 FROM part p WHERE p.sha256 = a.sha256) ORDER BY a.read_at, a.sha256",
        (baseline_model,)).fetchall()
    out = []
    for r in rows:
        judged = conn.execute("SELECT load_id, outcome, kind, status FROM autofile WHERE sha256=?", (r["sha256"],)).fetchall()
        judged.sort(key=lambda j: (OUTCOME_RANK.get(j["outcome"], 9), -int(j["load_id"])))
        if judged:
            load_id, outcome, status = int(judged[0]["load_id"]), judged[0]["outcome"], judged[0]["status"] or ""
        else:
            got = conn.execute("SELECT m.load_id FROM part p JOIN message m ON m.message_id = p.message_id "
                               "WHERE p.sha256=? AND m.load_id IS NOT NULL LIMIT 1", (r["sha256"],)).fetchone()
            load_id, outcome, status = (int(got[0]) if got else None), None, ""
        stage = ""
        if load_id is not None:
            lr = conn.execute("SELECT stage FROM load WHERE load_id=?", (load_id,)).fetchone()
            stage = (lr["stage"] or "") if lr else ""
        out.append({"sha256": r["sha256"], "filename": r["filename"] or "", "bytes": r["bytes"] or 0,
                    "document_type": r["document_type"], "stratum": stratum_of(r["document_type"]),
                    "cost_usd": float(r["cost_usd"] or 0), "read_at": r["read_at"], "s3_key": r["s3_key"],
                    "stored": json.loads(r["extraction_json"]), "load_id": load_id, "stage": stage.lower(),
                    "outcome": outcome, "status": status})
    return out


def sample(pages: list[dict], n: int, seed: int, shares: dict[str, float]) -> list[dict]:
    """`n` pages, stratified by `shares` and shuffled by `seed` within each stratum. A stratum with
    fewer pages than its share gives all it has, and the shortfall is filled from the others in the
    order bol, pod, other, lumper - so the sample is always n long when the pool allows."""
    rng = random.Random(seed)
    pools = {s: [p for p in pages if p["stratum"] == s] for s in STRATA}
    for pool in pools.values():
        rng.shuffle(pool)
    want = {s: int(round(n * shares[s])) for s in STRATA}
    # Rounding can leave the total a page short or long; settle it on the largest share.
    biggest = max(STRATA, key=lambda s: shares[s])
    want[biggest] += n - sum(want.values())
    taken = {s: pools[s][:want[s]] for s in STRATA}
    short = sum(want[s] - len(taken[s]) for s in STRATA)
    for s in ("bol", "pod", "other", "lumper"):
        while short > 0 and len(taken[s]) < len(pools[s]):
            taken[s].append(pools[s][len(taken[s])])
            short -= 1
    out = [p for s in STRATA for p in taken[s]]
    rng.shuffle(out)
    return out


# ---------------------------------------------------------------------------------- the readings ----

def fetch_page(s3, bucket: str, page: dict, cache: Path) -> bytes:
    """The page's bytes, from the cache or from S3, checked against the SHA-256 that names them."""
    path = cache / page["sha256"]
    if path.exists():
        data = path.read_bytes()
    else:
        data = s3.get_object(Bucket=bucket, Key=page["s3_key"])["Body"].read()
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if hashlib.sha256(data).hexdigest() != page["sha256"]:
        raise RuntimeError(f"{page['s3_key']}: the bytes do not hash to {page['sha256'][:12]}")
    return data


def read_page(client, data: bytes, filename: str, cfg: dict) -> tuple[dict, dict]:
    """One read with the candidate configuration: (the reading as a dict, the usage). The same path
    as intake.ingest.make_reader - a temp file, normalize.load_document at CHUNK_PAGES pages, the
    reader - but keeping the usage, which the production adapter folds into a cost."""
    from pod_intake import reader as claude_reader
    from pod_intake.normalize import load_document

    suffix = Path(filename).suffix or (".pdf" if data[:5] == b"%PDF-" else ".png")
    tmp = Path(tempfile.mkdtemp(prefix="replay_")) / f"doc{suffix}"
    try:
        tmp.write_bytes(data)
        doc = load_document(tmp, max_pages=autofile.CHUNK_PAGES)
        ex, usage = claude_reader.read_document(client, doc, cfg["model"], effort=cfg["effort"], brief=cfg["brief"],
                                               form=cfg["form"])
        return ex.model_dump(), usage.as_dict()
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except OSError:
            pass


def candidate_name(cfg: dict) -> str:
    return f"{cfg['model']}-{cfg['effort'] or 'noeffort'}-{'brief' if cfg['brief'] else 'long'}-{cfg['form']}"


# -------------------------------------------------------------------------------- the comparison ----

def band(confidence: float) -> str:
    """Which side of the judge's thresholds a confidence falls on."""
    if confidence >= BAR:
        return f">={BAR:.0%}"
    if confidence >= FLOOR:
        return f"{FLOOR:.0%}-{BAR:.0%}"
    return f"<{FLOOR:.0%}"


def judge_kind(ex: Extraction, stage: str) -> str:
    """What intake.autofile.judge would make of the page on a load at `stage`: POD, BOL, LUMPER or
    'not paperwork'. The same steps as judge, without the ledger."""
    if ex.document_type in ("other", "unknown"):
        return "not paperwork"
    type_name, _ = classify_type(ex, {"dispatch_status": stage.title()})
    receiver = ex.signatures.receiver_signed or ex.document_type == "proof_of_delivery"
    if type_name == "Proof of Delivery":
        return "POD"
    if type_name == "Bill Of Lading":
        return "POD" if receiver and stage not in AT_CONSIGNEE else "BOL"
    if is_lumper_receipt(ex):
        return "LUMPER"
    return "not paperwork"


def reference_numbers(ex: Extraction) -> set[str]:
    """The reference-kind numbers on the page, normalised as match_facts_detail normalises them."""
    return {_norm(n.value) for n in ex.numbers if n.kind in REFERENCE_KINDS and _norm(n.value)}


def all_numbers(ex: Extraction) -> set[str]:
    return {_norm(n.value) for n in ex.numbers if _norm(n.value)}


def pages_up(ex: Extraction, kind: str) -> list[int]:
    total = max([p.page for p in ex.pages] + [len(ex.pages), 1])
    roles_up = LUMPER_ROLES if kind == "LUMPER" else PAPER_ROLES
    return paper_pages(ex, total, kind if kind in ("POD", "BOL", "LUMPER") else None, roles_up=roles_up)[0]


def compare_readings(old: dict, new: dict, load: dict | None = None, stage: str = "") -> dict:
    """The two readings side by side on what the judge uses, and whether the filing decision changes.

    Returns {"fields": {name: {"old", "new", "agree"}}, "changes": [what would change the decision],
    "notes": [differences worth a look that do not change it], "decision_changed": bool,
    "pod_lost_evidence": bool}. `load` is TransportPro's load JSON when known (then the facts the
    numbers match are compared), `stage` the dispatch stage as the ledger has it."""
    s = Settings()
    ex_o, ex_n = Extraction.model_validate(old), Extraction.model_validate(new)
    stage = (stage or "").lower()
    kind_o, kind_n = judge_kind(ex_o, stage), judge_kind(ex_n, stage)
    evidence_o = ex_o.signatures.receiver_signed or ex_o.signatures.stamp_present
    evidence_n = ex_n.signatures.receiver_signed or ex_n.signatures.stamp_present
    times_o = bool(ex_o.times.check_out) and ex_o.times.at_stop != "shipper"
    times_n = bool(ex_n.times.check_out) and ex_n.times.at_stop != "shipper"
    refs_o, refs_n = reference_numbers(ex_o), reference_numbers(ex_n)
    up_o, up_n = pages_up(ex_o, kind_o), pages_up(ex_n, kind_n)

    def party(ex, side):
        p = getattr(ex, side)
        return _norm(p.name or ""), _norm(p.city or "")

    fields = {
        "document_type": (ex_o.document_type, ex_n.document_type),
        "judge_kind": (kind_o, kind_n),
        "confidence_band": (band(ex_o.document_type_confidence), band(ex_n.document_type_confidence)),
        "receiver_signed": (ex_o.signatures.receiver_signed, ex_n.signatures.receiver_signed),
        "stamp_present": (ex_o.signatures.stamp_present, ex_n.signatures.stamp_present),
        "receiver_date": (_norm(ex_o.signatures.receiver_date or ""), _norm(ex_n.signatures.receiver_date or "")),
        "reference_numbers": (sorted(refs_o), sorted(refs_n)),
        "all_numbers": (sorted(all_numbers(ex_o)), sorted(all_numbers(ex_n))),
        "page_roles": ([(p.page, p.role) for p in ex_o.pages], [(p.page, p.role) for p in ex_n.pages]),
        "pages_up": (up_o, up_n),
        "at_stop": (ex_o.times.at_stop, ex_n.times.at_stop),
        "delivery_times": (times_o, times_n),
        "shipper": (party(ex_o, "shipper"), party(ex_n, "shipper")),
        "consignee": (party(ex_o, "consignee"), party(ex_n, "consignee")),
        "pieces": (_whole(ex_o.pieces or ""), _whole(ex_n.pieces or "")),
        "weight": (_whole(ex_o.weight_lbs or ""), _whole(ex_n.weight_lbs or "")),
        "amount": (_whole(ex_o.amount or ""), _whole(ex_n.amount or "")),
    }
    changes: list[str] = []
    notes: list[str] = []
    if kind_o != kind_n:
        changes.append(f"type flip: {kind_o} -> {kind_n} ({ex_o.document_type} -> {ex_n.document_type})")
    elif ex_o.document_type != ex_n.document_type:
        notes.append(f"document type {ex_o.document_type} -> {ex_n.document_type}, same for the judge")
    if fields["confidence_band"][0] != fields["confidence_band"][1]:
        changes.append(f"bar flip: {ex_o.document_type_confidence:.0%} -> {ex_n.document_type_confidence:.0%} "
                       f"({fields['confidence_band'][0]} -> {fields['confidence_band'][1]})")
    if evidence_o and not evidence_n:
        changes.append("lost receiver evidence: " + ("signature" if ex_o.signatures.receiver_signed else "stamp"))
    elif evidence_n and not evidence_o:
        changes.append("gained receiver evidence: " + ("signature" if ex_n.signatures.receiver_signed else "stamp"))
    if times_o != times_n:
        notes.append(f"delivery-time evidence {times_o} -> {times_n} (out {ex_o.times.check_out!r} at {ex_o.times.at_stop} "
                     f"-> out {ex_n.times.check_out!r} at {ex_n.times.at_stop})")
    if load is not None:
        facts_o, strong_o = match_facts_detail(ex_o, load)
        facts_n, strong_n = match_facts_detail(ex_n, load)
        ok_o = len(facts_o) >= s.min_facts and len(strong_o) >= 1
        ok_n = len(facts_n) >= s.min_facts and len(strong_n) >= 1
        fields["matched_references"] = (sorted(strong_o), sorted(strong_n))
        fields["matched_facts"] = (len(facts_o), len(facts_n))
        fields["facts_check"] = (ok_o, ok_n)
        lost = sorted(set(strong_o) - set(strong_n))
        gained = sorted(set(strong_n) - set(strong_o))
        if lost:
            changes.append("lost matching reference: " + "; ".join(lost))
        if ok_n and not ok_o:
            changes.append("facts check now passes: " + "; ".join(gained or [f"{len(facts_n)} facts"]))
        elif ok_o and not ok_n and not lost:
            changes.append(f"facts check now fails: {len(facts_n)} fact(s), {len(strong_n)} a reference (was {len(facts_o)}, {len(strong_o)})")
        elif gained:
            notes.append("gained matching reference: " + "; ".join(gained))
        if kind_o == "POD" or kind_n == "POD":
            cor_o = corroborated_pod(ex_o, facts_o, strong_o, load, s) is not None
            cor_n = corroborated_pod(ex_n, facts_n, strong_n, load, s) is not None
            fields["corroborated_pod"] = (cor_o, cor_n)
            if cor_o != cor_n and fields["confidence_band"][0] == fields["confidence_band"][1]:
                changes.append(f"corroborated POD {cor_o} -> {cor_n}")
    else:
        lost, gained = sorted(refs_o - refs_n), sorted(refs_n - refs_o)
        if lost:
            changes.append("lost reference number: " + "; ".join(lost))
        if gained:
            notes.append("gained reference number: " + "; ".join(gained))
    if up_o != up_n:
        changes.append(f"pages that go up: {up_o} -> {up_n}")
    elif fields["page_roles"][0] != fields["page_roles"][1]:
        notes.append(f"page roles {fields['page_roles'][0]} -> {fields['page_roles'][1]}, same pages go up")
    for name in ("receiver_date", "at_stop", "shipper", "consignee", "pieces", "weight", "amount"):
        a, b = fields[name]
        if a != b:
            notes.append(f"{name} {a!r} -> {b!r}")
    out_fields = {k: {"old": v[0], "new": v[1], "agree": v[0] == v[1]} for k, v in fields.items()}
    pod_lost = ex_o.document_type == "proof_of_delivery" and evidence_o and not evidence_n
    return {"fields": out_fields, "changes": changes, "notes": notes, "decision_changed": bool(changes),
            "pod_lost_evidence": pod_lost}


# ---------------------------------------------------------------------------------- the report ----

def agreement_table(diffs: list[dict]) -> str:
    names: list[str] = []
    for d in diffs:
        for k in d["compare"]["fields"]:
            if k not in names:
                names.append(k)
    lines = [f"{'field':22} {'agree':>7} {'of':>4} {'pct':>6}"]
    for k in names:
        have = [d for d in diffs if k in d["compare"]["fields"]]
        agree = sum(1 for d in have if d["compare"]["fields"][k]["agree"])
        lines.append(f"{k:22} {agree:7} {len(have):4} {agree / len(have):6.0%}" if have else f"{k:22} {'-':>7}")
    return "\n".join(lines)


def _mean(xs):
    return statistics.mean(xs) if xs else 0.0


def _median(xs):
    return statistics.median(xs) if xs else 0.0


def cost_table(diffs: list[dict], pairs: list[dict], chars_per_token: float) -> str:
    """Cost and output tokens per read: the stored reads (the ledger's cost, mixed warm and cold
    cache; tokens estimated from the stored JSON at `chars_per_token`), the paired full-form re-reads
    where there are any (measured, cache warm), and the candidate's reads (measured)."""
    lines = []
    by_stratum: dict[str, list[dict]] = {}
    for d in diffs:
        by_stratum.setdefault(d["stratum"], []).append(d)
    lines.append(f"{'':9} {'n':>4} {'stored $':>9} {'stored out tok*':>15} {'pair $':>8} {'pair out tok':>12} {'new $':>8} {'new out tok':>12} {'new/stored $':>13}")
    for name, group in [("all", diffs)] + sorted(by_stratum.items()):
        paired = [p for p in pairs if p["sha256"] in {d["sha256"] for d in group}]
        stored = [d["stored_cost_usd"] for d in group]
        stored_tok = [len(json.dumps(d["stored"])) / chars_per_token for d in group]
        new = [d["usage"]["estimated_cost_usd"] for d in group]
        new_tok = [d["usage"]["output_tokens"] for d in group]
        pair_c = [p["usage"]["estimated_cost_usd"] for p in paired]
        pair_t = [p["usage"]["output_tokens"] for p in paired]
        ratio = _mean(new) / _mean(stored) if _mean(stored) else 0
        lines.append(f"{name:9} {len(group):4} {_mean(stored):9.4f} {_mean(stored_tok):15.0f} "
                     f"{(f'{_mean(pair_c):.4f}' if paired else '-'):>8} {(f'{_mean(pair_t):.0f}' if paired else '-'):>12} "
                     f"{_mean(new):8.4f} {_mean(new_tok):12.0f} {ratio:13.0%}")
    lines.append(f"* estimated from the stored reading's JSON at {chars_per_token:.2f} characters per token"
                 + (" (measured on the paired full-form reads)" if pairs else " (the default; --pair measures it)"))
    return "\n".join(lines)


def changes_table(diffs: list[dict]) -> str:
    changed = [d for d in diffs if d["compare"]["decision_changed"]]
    if not changed:
        return "no page's filing decision changes"
    lines = [f"{len(changed)} page(s) whose filing decision would change:"]
    for d in changed:
        f = d["compare"]["fields"]
        lines.append(f"  {d['sha256'][:12]}  load {d['load_id'] or '-':<8} {d['stratum']:6} "
                     f"{f['document_type']['old']:>17} {f['confidence_band']['old']:>7}  {d['filename'][:40]}")
        for c in d["compare"]["changes"]:
            lines.append(f"      - {c}")
    return "\n".join(lines)


def noise_table(pairs: list[dict]) -> str:
    """How often two full-form reads of one page disagree: the noise floor the candidate is judged against."""
    if not pairs:
        return ""
    changed = [p for p in pairs if p["compare"]["decision_changed"]]
    lines = [f"noise floor: {len(pairs)} page(s) read again with the default form; {len(changed)} decision(s) changed"]
    for p in changed:
        lines.append(f"  {p['sha256'][:12]}  load {p['load_id'] or '-':<8} " + "; ".join(p["compare"]["changes"]))
    return "\n".join(lines)


def verdict(diffs: list[dict]) -> str:
    changed = sum(1 for d in diffs if d["compare"]["decision_changed"])
    pods_lost = [d for d in diffs if d["compare"]["pod_lost_evidence"]]
    same = len(diffs) - changed
    rule = (f"adoption rule: ship only if at least 98 of 100 filing decisions are identical and no POD loses its "
            f"receiver evidence. Here {same} of {len(diffs)} identical ({same / len(diffs):.0%}), "
            f"{len(pods_lost)} POD(s) lost receiver evidence")
    ok = len(diffs) > 0 and same / len(diffs) >= 0.98 and not pods_lost
    return rule + (" -> PASSES" if ok else " -> DOES NOT PASS")


# ------------------------------------------------------------------------------------- main ----

def _s3_client():
    import boto3
    return boto3.session.Session(profile_name=os.environ.get("AWS_PROFILE") or None,
                                 region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None).client("s3")


def load_loads(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def fetch_loads(path: Path, load_ids: list[int], have: dict[str, dict]) -> dict[str, dict]:
    """GET each missing load from TransportPro, once, into the cache file. Read-only."""
    from intake import tpro as _tpro
    client = _tpro.from_env()
    missing = [lid for lid in load_ids if str(lid) not in have]
    for i, lid in enumerate(missing, 1):
        try:
            have[str(lid)] = client.load(lid)
        except Exception as e:                                       # noqa: BLE001 - one load without JSON is compared on its numbers
            print(f"  ! load {lid}: {type(e).__name__}: {str(e)[:100]}")
            have[str(lid)] = None
        if i % 10 == 0 or i == len(missing):
            print(f"  loads fetched: {i}/{len(missing)}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(have), encoding="utf-8")
    return have


class Budget:
    """A hard cap on what a replay spends, at the ledger's prices (pod_intake.reader.Usage): every call
    counts - the warm-ups, each read, and the second request a read makes when the model answers in
    prose (its usage is folded into the read's). A read is started only while the cap has room for it
    at the dearest read seen so far (`reserve`, never under 5 cents), with every read in flight
    reserved the same, so the reads that finish together cannot carry the total past the cap. A read
    already sent is billed whatever happens, so the cap holds the START of work, not its end."""

    MIN_RESERVE = 0.05

    def __init__(self, cap: float | None) -> None:
        self.cap = cap
        self.spent = 0.0
        self.in_flight = 0
        self.reserve = self.MIN_RESERVE
        self.stopped = False
        self._lock = threading.Lock()

    def charge(self, usd: float) -> None:
        with self._lock:
            self.spent += usd

    def can_start(self) -> bool:
        if self.cap is None:
            return True
        with self._lock:
            return self.spent + (self.in_flight + 1) * self.reserve <= self.cap

    def start(self) -> None:
        with self._lock:
            self.in_flight += 1

    def finish(self, usd: float) -> None:
        with self._lock:
            self.in_flight -= 1
            self.spent += usd
            self.reserve = max(self.reserve, usd)

    def left(self) -> float | None:
        return None if self.cap is None else max(0.0, self.cap - self.spent)


def run_reads(client, s3, bucket: str, pages: list[dict], cfg: dict, out: Path, cache: Path, workers: int,
              force: bool, budget: Budget | None = None) -> dict[str, dict]:
    """Read every page with `cfg`, in parallel, caching each reading as it comes back. Returns
    {sha256: {"extraction", "usage"}}; a page whose read failed is left out and reported. With a
    `budget`, reads start only while its cap has room (Budget.can_start); the rest are left unread
    and said so."""
    from pod_intake import reader as claude_reader
    budget = budget or Budget(None)
    name = candidate_name(cfg)
    folder = out / "readings" / name
    folder.mkdir(parents=True, exist_ok=True)
    have: dict[str, dict] = {}
    todo = []
    for p in pages:
        path = folder / f"{p['sha256']}.json"
        if path.exists() and not force:
            have[p["sha256"]] = json.loads(path.read_text(encoding="utf-8"))
        else:
            todo.append(p)
    print(f"{name}: {len(have)} reading(s) cached, {len(todo)} to read")
    if not todo:
        return have
    try:
        warm = claude_reader.warm_cache(client, cfg["model"], effort=cfg["effort"], brief=cfg["brief"], form=cfg["form"])
        budget.charge(warm.cost_usd)
        print(f"  cache warmed: ${warm.cost_usd:.4f} ({warm.cache_write} written, {warm.cache_read} read)")
    except Exception as e:                                           # noqa: BLE001 - the reads then write the cache themselves
        print(f"  ! warm-up failed: {type(e).__name__}: {str(e)[:120]}")

    def one(p):
        data = fetch_page(s3, bucket, p, cache)
        ex, usage = read_page(client, data, p["filename"], cfg)
        return {"extraction": ex, "usage": usage, "form": cfg["form"], "model": cfg["model"], "effort": cfg["effort"],
                "brief": cfg["brief"], "read_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}

    started = time.monotonic()
    spent = 0.0
    queue = list(todo)
    futures: dict = {}
    done_count = 0
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        while queue or futures:
            while queue and len(futures) < workers and budget.can_start():
                p = queue.pop(0)
                budget.start()
                futures[pool.submit(one, p)] = p
            if not futures:
                break                                                # the cap has no room for another read
            finished, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for fut in finished:
                p = futures.pop(fut)
                done_count += 1
                try:
                    got = fut.result()
                except Exception as e:                               # noqa: BLE001 - one failed read stops nothing
                    budget.finish(0.0)
                    print(f"  ! {p['sha256'][:12]} {p['filename'][:40]}: {type(e).__name__}: {str(e)[:160]}")
                    continue
                cost = got["usage"]["estimated_cost_usd"]
                budget.finish(cost)
                have[p["sha256"]] = got
                (folder / f"{p['sha256']}.json").write_text(json.dumps(got), encoding="utf-8")
                spent += cost
                print(f"  {done_count:3}/{len(todo)}  {p['sha256'][:12]}  {got['extraction']['document_type']:>17} "
                      f"{got['extraction']['document_type_confidence']:.0%}  out {got['usage']['output_tokens']:5} tok  "
                      f"${cost:.4f}  (${spent:.2f} this batch, ${budget.spent:.2f} this run, {time.monotonic() - started:.0f}s)")
    finally:
        pool.shutdown(wait=True)
    if queue:
        budget.stopped = True
        print(f"  spend cap: {len(queue)} read(s) not started - ${budget.spent:.2f} of the ${budget.cap:.2f} cap spent, "
              f"and the next read is reserved at ${budget.reserve:.3f}")
    return have


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", required=True, help="a COPY of the ledger (never the worker's own)")
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--shares", default=DEFAULT_SHARES, help=f"the strata's shares of the sample, default {DEFAULT_SHARES}")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--baseline-model", default=None, help="which stored readings to compare against (default: --model)")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--no-brief", action="store_true", help="long notes (production reads are brief)")
    ap.add_argument("--form", default="full", choices=("full", "compact"))
    ap.add_argument("--pair", type=int, default=0, help="also re-read the first N pages with the default form (noise floor, token calibration)")
    ap.add_argument("--loads", default=None, help="JSON cache of TransportPro load JSON by load id (default <out>/loads.json)")
    ap.add_argument("--fetch-loads", action="store_true", help="GET missing loads from TransportPro into --loads (read-only)")
    ap.add_argument("--bucket", default=None, help=f"the archive bucket (default INTAKE_S3_BUCKET from .env, else {DEFAULT_BUCKET})")
    ap.add_argument("--out", default=str(HERE / "out" / "replay"))
    ap.add_argument("--cache", default=None, help="where fetched pages are kept (default <out>/pages)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="read again even when a reading is cached")
    ap.add_argument("--max-usd", type=float, default=None, help="a hard cap on this run's spend at the ledger's prices; reads stop starting when it has no room")
    ap.add_argument("--dry-run", action="store_true", help="print the sample and the cost estimate; call nothing")
    args = ap.parse_args(argv)

    from pod_intake.localenv import load_local_env
    load_local_env()
    bucket = args.bucket or os.environ.get("INTAKE_S3_BUCKET") or DEFAULT_BUCKET
    cfg = {"model": args.model, "effort": args.effort or None, "brief": not args.no_brief, "form": args.form}
    baseline = args.baseline_model or args.model
    out = Path(args.out)
    cache = Path(args.cache) if args.cache else out / "pages"
    loads_path = Path(args.loads) if args.loads else out / "loads.json"

    conn = sqlite3.connect(f"file:{Path(args.ledger).resolve().as_posix()}?mode=ro", uri=True)
    pool = candidates(conn, baseline)
    shares = shares_of(args.shares)
    pages = sample(pool, args.sample, args.seed, shares)
    counts = {s: sum(1 for p in pool if p["stratum"] == s) for s in STRATA}
    picked = {s: sum(1 for p in pages if p["stratum"] == s) for s in STRATA}
    print(f"pool: {len(pool)} emailed page(s) read by {baseline} in one piece, with bytes in S3: "
          + ", ".join(f"{s} {counts[s]}" for s in STRATA))
    print(f"sample (seed {args.seed}): {len(pages)}: " + ", ".join(f"{s} {picked[s]}" for s in STRATA))
    print(f"candidate: {candidate_name(cfg)}" + (f"; paired full-form re-reads: {args.pair}" if args.pair else ""))

    loads = load_loads(loads_path)
    load_ids = sorted({p["load_id"] for p in pages if p["load_id"] is not None})
    known = sum(1 for lid in load_ids if loads.get(str(lid)))
    if args.fetch_loads and known < len(load_ids):
        loads = fetch_loads(loads_path, load_ids, loads)
        known = sum(1 for lid in load_ids if loads.get(str(lid)))
    print(f"loads: {len(load_ids)} on the sample, {known} with JSON in {loads_path}"
          + ("" if known == len(load_ids) else (" (--fetch-loads gets the rest)" if not args.fetch_loads else "")))

    if args.dry_run:
        stored = sum(p["cost_usd"] for p in pages)
        est_new = len(pages) * WARM_READ_USD[cfg["form"]]
        est_pair = args.pair * WARM_READ_USD["full"]
        print(f"\n{'sha256':14} {'stratum':7} {'stored type':17} {'conf':>5} {'load':>8} {'stage':12} {'stored $':>8}  file")
        for p in pages:
            ex = p["stored"]
            print(f"{p['sha256'][:12]:14} {p['stratum']:7} {ex['document_type']:17} {ex['document_type_confidence']:5.0%} "
                  f"{p['load_id'] or '-':>8} {p['stage'][:12]:12} {p['cost_usd']:8.4f}  {p['filename'][:50]}")
        print(f"\nstored cost of these {len(pages)} reads: ${stored:.2f} (mixed warm and cold cache)")
        print(f"estimate: {len(pages)} {cfg['form']}-form read(s) at about ${WARM_READ_USD[cfg['form']]:.3f} warm = ${est_new:.2f}"
              + (f"; {args.pair} paired full-form read(s) = ${est_pair:.2f}; total about ${est_new + est_pair:.2f}" if args.pair else ""))
        print("dry run: nothing was called")
        return 0

    from pod_intake import provider
    client, which = provider.make_client()
    client = client.with_options(timeout=autofile.READ_TIMEOUT_S, max_retries=1)
    print(f"reader: {provider.describe()}")
    s3 = _s3_client()
    out.mkdir(parents=True, exist_ok=True)

    budget = Budget(args.max_usd)
    if args.max_usd is not None:
        print(f"spend cap: ${args.max_usd:.2f} for this run, every call counted")
    new = run_reads(client, s3, bucket, pages, cfg, out, cache, args.workers, args.force, budget)
    pairs_raw: dict[str, dict] = {}
    if args.pair:
        full_cfg = {**cfg, "form": "full"}
        pairs_raw = run_reads(client, s3, bucket, pages[:args.pair], full_cfg, out, cache, args.workers, args.force, budget)
    print(f"spent this run: ${budget.spent:.4f}" + (f" of the ${budget.cap:.2f} cap" if budget.cap is not None else "")
          + (" - the cap stopped the run" if budget.stopped else ""))

    diffs, pairs = [], []
    for p in pages:
        load = loads.get(str(p["load_id"])) if p["load_id"] is not None else None
        if p["sha256"] in new:
            got = new[p["sha256"]]
            diffs.append({"sha256": p["sha256"], "filename": p["filename"], "load_id": p["load_id"], "stage": p["stage"],
                          "stratum": p["stratum"], "outcome": p["outcome"], "status": p["status"],
                          "stored_cost_usd": p["cost_usd"], "stored_read_at": p["read_at"], "stored": p["stored"],
                          "new": got["extraction"], "usage": got["usage"], "load_known": load is not None,
                          "compare": compare_readings(p["stored"], got["extraction"], load, p["stage"])})
        if p["sha256"] in pairs_raw:
            got = pairs_raw[p["sha256"]]
            pairs.append({"sha256": p["sha256"], "load_id": p["load_id"], "stratum": p["stratum"], "usage": got["usage"],
                          "new": got["extraction"],
                          "compare": compare_readings(p["stored"], got["extraction"], load, p["stage"])})
    if not diffs:
        print("no readings came back")
        return 1
    chars_per_token = DEFAULT_CHARS_PER_TOKEN
    if pairs:
        ratios = [len(json.dumps(p["new"])) / p["usage"]["output_tokens"] for p in pairs if p["usage"]["output_tokens"]]
        chars_per_token = _median(ratios) or chars_per_token

    print(f"\n== {candidate_name(cfg)} against the stored readings: {len(diffs)} page(s) ==\n")
    print(agreement_table(diffs))
    print()
    print(changes_table(diffs))
    print()
    if pairs:
        print(noise_table(pairs))
        print()
    print(cost_table(diffs, pairs, chars_per_token))
    print()
    print(verdict(diffs))
    failed = len(pages) - len(diffs)
    if failed:
        print(f"{failed} page(s) could not be read; see the log above")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M")
    (out / f"diffs-{candidate_name(cfg)}-{stamp}.json").write_text(json.dumps({"candidate": cfg, "baseline_model": baseline,
                                                                             "seed": args.seed, "pages": diffs, "pairs": pairs}, indent=1),
                                                                 encoding="utf-8")
    summary = {"candidate": cfg, "baseline_model": baseline, "seed": args.seed, "sample": len(pages), "read": len(diffs),
               "decision_changed": sum(1 for d in diffs if d["compare"]["decision_changed"]),
               "pod_lost_evidence": sum(1 for d in diffs if d["compare"]["pod_lost_evidence"]),
               "agreement": {k: sum(1 for d in diffs if d["compare"]["fields"].get(k, {}).get("agree")) for k in diffs[0]["compare"]["fields"]},
               "chars_per_token": chars_per_token, "verdict": verdict(diffs),
               "spent_this_run_usd": round(budget.spent, 4), "cap_usd": budget.cap, "cap_stopped_the_run": budget.stopped,
               "cost": {"stored_mean": _mean([d["stored_cost_usd"] for d in diffs]),
                        "new_mean": _mean([d["usage"]["estimated_cost_usd"] for d in diffs]),
                        "new_output_tokens_mean": _mean([d["usage"]["output_tokens"] for d in diffs]),
                        "pair_mean": _mean([p["usage"]["estimated_cost_usd"] for p in pairs]),
                        "pair_output_tokens_mean": _mean([p["usage"]["output_tokens"] for p in pairs]),
                        "pair_decision_changed": sum(1 for p in pairs if p["compare"]["decision_changed"])}}
    (out / f"summary-{candidate_name(cfg)}-{stamp}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\nwritten: {out / f'diffs-{candidate_name(cfg)}-{stamp}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
