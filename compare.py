r"""Compare two or more prototype runs side by side.

    python compare.py out\opus-5 out\sonnet-5
    python compare.py out\opus-5 out\sonnet-5 out\haiku-4-5 --save compare.md

Each folder holds the *.match.json files written by run.py --out <folder>. Documents are paired by source
file name. The script prints, per document and per run: tier, chosen load, document type, strong/medium
signal counts, how many numbers were extracted, whether a receiver signature and in/out times were found,
page legibility, tokens and estimated cost, then the numbers one run found that another did not.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_runs(folders: list[str]) -> dict[str, dict[str, dict]]:
    runs: dict[str, dict[str, dict]] = {}
    for f in folders:
        p = Path(f)
        docs = {}
        for mj in sorted(p.glob("*.match.json")):
            d = json.loads(mj.read_text(encoding="utf-8"))
            docs[d["source"]] = d
        if not docs:
            raise SystemExit(f"no *.match.json files in {p}")
        runs[p.name] = docs
    return runs


def model_of(d: dict) -> str:
    u = d.get("usage") or []
    return u[0]["model"].split("/")[-1] if u else "offline"


def tokens_of(d: dict) -> tuple[int, int, int]:
    u = d.get("usage") or []
    return (sum(x["input_tokens"] for x in u), sum(x["output_tokens"] for x in u), sum(x.get("cache_read_tokens", 0) for x in u))


def num_set(d: dict) -> set[str]:
    return {n["value"].upper().replace(" ", "") for n in d["extraction"]["numbers"]}


def row(label: str, values: list[str]) -> str:
    return f"| {label} | " + " | ".join(values) + " |"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--save", default=None, help="also write the comparison as Markdown to this file")
    args = ap.parse_args()

    runs = load_runs(args.folders)
    names = list(runs)
    sources = sorted({s for docs in runs.values() for s in docs})
    lines: list[str] = []

    for src in sources:
        docs = [runs[n].get(src) for n in names]
        if any(d is None for d in docs):
            lines.append(f"\n## {src}\n\nmissing in: {', '.join(n for n, d in zip(names, docs) if d is None)}")
            continue
        header = [f"{n} ({model_of(d)})" for n, d in zip(names, docs)]
        lines.append(f"\n## {src}\n")
        lines.append("| | " + " | ".join(header) + " |")
        lines.append("|---|" + "---|" * len(header))
        lines.append(row("Tier / decision", [f"{d['tier']} / {d['decision']}" for d in docs]))
        lines.append(row("Load", [str(d["load_id"]) for d in docs]))
        lines.append(row("Filed as", [d["document_type"] for d in docs]))
        lines.append(row("Reader type (conf)", [f"{d['extraction']['document_type']} ({d['extraction']['document_type_confidence']:.2f})" for d in docs]))
        lines.append(row("Top candidate signals", [f"{c[0]['load_id']}: {sum(1 for s in c[0]['signals'] if s['strength']=='strong')} strong, {sum(1 for s in c[0]['signals'] if s['strength']=='medium')} medium" if (c := d["candidates"]) else "none" for d in docs]))
        lines.append(row("Numbers extracted", [str(len(d["extraction"]["numbers"])) for d in docs]))
        lines.append(row("Handwritten flagged", [str(sum(1 for n in d["extraction"]["numbers"] if n["handwritten"])) for d in docs]))
        lines.append(row("Receiver signed / name", [f"{d['extraction']['signatures']['receiver_signed']} / {d['extraction']['signatures'].get('receiver_name') or '-'}" for d in docs]))
        lines.append(row("In / out times", [f"{d['extraction']['times'].get('check_in') or '-'} / {d['extraction']['times'].get('check_out') or '-'} ({d['extraction']['times']['source']})" for d in docs]))
        lines.append(row("Legibility (page 1)", [f"{d['extraction']['pages'][0]['legibility']:.2f}" if d["extraction"]["pages"] else "-" for d in docs]))
        lines.append(row("Tokens in / out / cached", ["{:,} / {:,} / {:,}".format(*tokens_of(d)) for d in docs]))
        lines.append(row("Estimated cost", [f"${d['estimated_cost_usd']:.4f}" for d in docs]))
        lines.append(row("Adjudicated", ["yes" if d.get("adjudication") else "no" for d in docs]))
        # numbers found by one run and not another
        sets = [num_set(d) for d in docs]
        union = set().union(*sets)
        diffs = []
        for i, n in enumerate(names):
            only = sets[i] - set().union(*(s for j, s in enumerate(sets) if j != i)) if len(sets) > 1 else set()
            if only:
                diffs.append(f"only {n}: {', '.join(sorted(only))}")
        lines.append("")
        lines.append("Numbers found by only one run: " + ("; ".join(diffs) if diffs else "none, all runs agree") + f" (union {len(union)})")

    # totals
    lines.append("\n## Totals\n")
    lines.append("| Run | Documents | High | Medium | Low | Adjudicated | Input tokens | Output tokens | Est. cost |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for n in names:
        docs = list(runs[n].values())
        tiers = [d["tier"] for d in docs]
        ti = sum(tokens_of(d)[0] for d in docs)
        to = sum(tokens_of(d)[1] for d in docs)
        cost = sum(d["estimated_cost_usd"] for d in docs)
        lines.append(f"| {n} ({model_of(docs[0])}) | {len(docs)} | {tiers.count('High')} | {tiers.count('Medium')} | {tiers.count('Low')} | {sum(1 for d in docs if d.get('adjudication'))} | {ti:,} | {to:,} | ${cost:.4f} |")

    text = "\n".join(lines)
    print(text)
    if args.save:
        Path(args.save).write_text(text, encoding="utf-8")
        print(f"\nsaved {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
