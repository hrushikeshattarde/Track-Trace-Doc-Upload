"""Label sheet and scoring for the readiness reader.

    python evaluate.py template            -> out/readiness/labels_<stamp>.csv, one row per file the reader read or skipped
    python evaluate.py score <labels.csv>  -> detection precision/recall, type accuracy, per-load agreement

The template pre-fills what the agent decided; a person fills the three human_* columns:
    human_is_document  Y if the file is a freight document (BOL, POD, lumper, scale ticket...), N otherwise
    human_type         bol | pod | photo | other   (photo = picture of freight, not paperwork)
    human_would_file   Y if you would have filed this file in TransportPro for this load
Nothing here talks to TransportPro or Gmail; it only reads the run's JSON and the saved attachments.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "readiness"
ATT = OUT / "attachments"
DOC_TYPES = {"bill_of_lading": "bol", "proof_of_delivery": "pod", "lumper": "other_doc", "weight_ticket": "other_doc",
             "reefer_log": "other_doc", "carrier_invoice": "other_doc", "rate_confirmation": "ratecon", "photo": "photo",
             "shipping_document": "other_doc"}


def latest_run_with_verdicts() -> tuple[Path, list[dict]]:
    for p in sorted(OUT.glob("readiness_*.json"), reverse=True):
        rows = json.loads(p.read_text(encoding="utf-8"))
        if any(r.get("verification") for r in rows):
            return p, rows
    raise SystemExit("no readiness_*.json with reader verdicts under out/readiness; run readiness.py --read first")


def agent_verdicts(row: dict) -> dict[str, tuple[str, str]]:
    """file name -> (agent_result, agent_type) from the verification string."""
    out: dict[str, tuple[str, str]] = {}
    for part in (row.get("verification") or "").split(" || "):
        m = re.match(r"(?P<file>[^:]+): read as (?P<type>\w+) -> (?P<filed>[^;]+);", part)
        if m:
            out[m["file"].strip()] = (part.strip(), DOC_TYPES.get(m["type"], m["type"]))
            continue
        m = re.match(r"(?P<file>[^:]+): (not a freight document|PII \(personal ID\), never file)", part)
        if m:
            out[m["file"].strip()] = (part.strip(), "not_document")
            continue
        m = re.match(r"reader error on \d+ file\(s\) \((?P<files>[^)]+)\)", part)
        if m:
            for f in m["files"].split(","):
                out[f.strip().rstrip(".")] = (part.strip(), "error")
    return out


def template() -> Path:
    run, rows = latest_run_with_verdicts()
    by_load = {str(r["load"]): r for r in rows}
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    dest = OUT / f"labels_{stamp}.csv"
    n = 0
    with dest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["load", "customer", "stage", "agent_load_outcome", "file", "path", "agent_result", "agent_type",
                    "human_is_document", "human_type", "human_would_file", "human_notes"])
        for load_dir in sorted(p for p in ATT.iterdir() if p.is_dir()):
            r = by_load.get(load_dir.name, {})
            verdicts = agent_verdicts(r)
            outcome = (r.get("reader") or {}).get("outcome") or ""
            for f in sorted(load_dir.iterdir()):
                if f.is_dir():
                    continue
                res, typ = verdicts.get(f.name, ("left over from an earlier run; not in " + run.name + "; ignore", "stale"))
                w.writerow([load_dir.name, r.get("customer") or "", r.get("stage") or "", outcome, f.name, str(f.relative_to(HERE)), res[:160], typ, "", "", "", ""])
                n += 1
            skipped = load_dir / "skipped"
            if skipped.is_dir():
                for f in sorted(skipped.iterdir()):
                    dims = re.search(r"_(\d+x\d+)_", f.name)
                    w.writerow([load_dir.name, r.get("customer") or "", r.get("stage") or "", outcome, f.name, str(f.relative_to(HERE)),
                                f"skipped before reading as signature/logo ({dims.group(1) if dims else 'small'})", "skipped", "", "", "", ""])
                    n += 1
    print(f"{dest}  ({n} files from {run.name}). Fill human_is_document / human_type / human_would_file, then: python evaluate.py score {dest.name}")
    return dest


def score(path: Path) -> None:
    rows = [r for r in csv.DictReader(path.open(encoding="utf-8")) if r.get("agent_type") != "stale"]
    labeled = [r for r in rows if (r.get("human_is_document") or "").strip().upper() in ("Y", "N")]
    if not labeled:
        raise SystemExit("no rows labeled yet (human_is_document must be Y or N)")
    is_doc = lambda r: r["human_is_document"].strip().upper() == "Y"
    agent_doc = lambda r: r["agent_type"] in ("bol", "pod", "other_doc")
    tp = sum(1 for r in labeled if agent_doc(r) and is_doc(r))
    fp = sum(1 for r in labeled if agent_doc(r) and not is_doc(r))
    fn = sum(1 for r in labeled if not agent_doc(r) and is_doc(r))
    tn = sum(1 for r in labeled if not agent_doc(r) and not is_doc(r))
    print(f"labeled files: {len(labeled)} of {len(rows)}")
    print(f"document detection: precision {tp / (tp + fp):.0%} ({tp}/{tp + fp})  recall {tp / (tp + fn):.0%} ({tp}/{tp + fn})  correctly ignored {tn}")
    if fn:
        print("  missed documents (agent skipped or called non-document):")
        for r in labeled:
            if not agent_doc(r) and is_doc(r):
                print(f"    {r['load']} {r['file']}: agent said {r['agent_type'] or 'skipped'}; human {r['human_type']} {r['human_notes']}")
    if fp:
        print("  false documents (agent said document, human says not):")
        for r in labeled:
            if agent_doc(r) and not is_doc(r):
                print(f"    {r['load']} {r['file']}: agent {r['agent_type']}; human {r['human_type']} {r['human_notes']}")
    typed = [r for r in labeled if is_doc(r) and agent_doc(r) and (r.get("human_type") or "").strip()]
    if typed:
        ok = sum(1 for r in typed if r["agent_type"] == r["human_type"].strip().lower())
        print(f"type accuracy (bol vs pod) on true documents: {ok / len(typed):.0%} ({ok}/{len(typed)})")
        for r in typed:
            if r["agent_type"] != r["human_type"].strip().lower():
                print(f"    {r['load']} {r['file']}: agent {r['agent_type']}, human {r['human_type']}")
    # per load: would a person file something from this thread, and did the agent say so
    # Agent's per-load call: the run's reader outcome when present, else derived from its file verdicts (older runs).
    loads: dict[str, dict] = {}
    for r in labeled:
        d = loads.setdefault(r["load"], {"human_file": False, "agent_file": "document(s) to file" in (r.get("agent_load_outcome") or "")})
        d["agent_file"] |= agent_doc(r)
        d["human_file"] |= (r.get("human_would_file") or "").strip().upper() == "Y"
    agree = sum(1 for d in loads.values() if d["human_file"] == d["agent_file"])
    print(f"per-load agreement on 'something to file': {agree}/{len(loads)}")
    for lid, d in loads.items():
        if d["human_file"] != d["agent_file"]:
            print(f"    {lid}: agent {'to file' if d['agent_file'] else 'nothing'} / human {'to file' if d['human_file'] else 'nothing'}")
    cost = sum(float(x) for r in rows for x in re.findall(r"est \$([0-9.]+)", r.get("agent_result") or ""))
    print(f"model spend on these files: ${cost:.2f}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "template":
        template()
    elif len(sys.argv) >= 3 and sys.argv[1] == "score":
        p = Path(sys.argv[2])
        score(p if p.exists() else OUT / p.name)
    else:
        print(__doc__)
