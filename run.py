r"""Driver Document Intake - prototype runner.

Examples (PowerShell):
  python run.py samples\*.pdf
  python run.py samples\*.pdf --model claude-haiku-4-5           # PRD cascade reader
  python run.py --from-json fixtures\*.json                     # offline: skip the model, run the matcher
  python run.py --normalize-only samples\*.pdf                  # render pages, no model

Requires ANTHROPIC_API_KEY (or an `ant auth login` profile) for anything that calls the model.
Nothing here writes to TransportPro.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime
from pathlib import Path

from pod_intake.index import LoadIndex
from pod_intake.matcher import classify_type, decide, filing_comment, score_candidates
from pod_intake.normalize import load_document
from pod_intake.requirements import Requirements, check_document, parse_workbook
from pod_intake.schema import Extraction

HERE = Path(__file__).resolve().parent


def expand(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        hits = [Path(h) for h in glob.glob(p)]
        out.extend(hits or [Path(p)])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Read POD/BOL files and match them to loads.")
    ap.add_argument("files", nargs="*", help="PDF or image files (globs allowed)")
    ap.add_argument("--index", default=str(HERE / "index" / "loads.json"))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--model", default="claude-opus-5", help="reader model (default claude-opus-5; claude-haiku-4-5 for the PRD cascade)")
    ap.add_argument("--adjudicator", default="claude-opus-5")
    ap.add_argument("--no-adjudicate", action="store_true", help="never call the adjudicator; Medium stays Medium")
    ap.add_argument("--effort", default=None, choices=["low", "medium", "high"], help="thinking effort for Opus 5 / Sonnet 5 (omitted for Haiku)")
    ap.add_argument("--max-edge", type=int, default=1568, help="long-edge pixels of page images sent to the model (default 1568)")
    ap.add_argument("--from-json", nargs="*", default=None, help="extraction JSON files to match instead of calling the model")
    ap.add_argument("--normalize-only", action="store_true", help="render page images and fingerprints, then stop")
    ap.add_argument("--sender-phone", default=None, help="simulate the SMS sender's phone (identity signal)")
    ap.add_argument("--sender-email", default=None, help="simulate the email sender (carrier domain signal)")
    ap.add_argument("--channel", default="email", choices=["email", "SMS"])
    ap.add_argument("--subject", default=None, help="simulate the email subject/body text that arrived with the document")
    ap.add_argument("--ignore-filename", action="store_true", help="do not use load numbers found in the file name (shows the paper-only path)")
    ap.add_argument("--requirements", default=str(HERE / "index" / "customer_requirements.json"), help="customer requirements rules JSON (built with --build-rules)")
    ap.add_argument("--customer", default=None, help="override the load's customer name when checking requirements (demo / what-if)")
    ap.add_argument("--build-rules", default=None, metavar="XLSX", help="parse the Accounts/Customers Extra Requirements workbook into the rules JSON and exit")
    ap.add_argument("--pod-map", default=str(HERE / "index" / "pod_terminals.json"), help="TransportPro terminal IDs per pod and sheet -> terminal mapping, merged at --build-rules")
    args = ap.parse_args()

    if args.build_rules:
        data = parse_workbook(args.build_rules, pod_map_path=args.pod_map)
        Path(args.requirements).write_text(json.dumps(data, indent=1), encoding="utf-8")
        cs = data["customers"]
        sm = data["pods"].get("sheet_map", [])
        print(f"parsed {len(cs)} customer rows from {len({c['sheet'] for c in cs})} sheets -> {args.requirements}")
        print(f"  pod map: {len(data['pods'].get('terminals', []))} pod terminals; {sum(1 for m in sm if m['terminal_ids'])}/{len(sm)} sheets mapped to terminals; "
              f"{sum(1 for c in cs if c['terminal_ids'])} customer rows carry a terminal; unmapped sheets: {[m['sheet'] for m in sm if not m['terminal_ids']]}")
        print(f"  BOL required: {sum(c['bol_required'] for c in cs)} | POD required: {sum(c['pod_required'] for c in cs)} | "
              f"POD gates deliver-out: {sum(c['pod_before_deliver_out'] for c in cs)} | BOL before leaving shipper: {sum(c['bol_before_leaving_shipper'] for c in cs)}")
        print(f"  page counts: {sum(1 for c in cs if c['pages_required'])} | POD signatures: {sum(1 for c in cs if c['pod_signatures'])} | seal on BOL: {sum(c['seal_required_on_bol'] for c in cs)} | "
              f"freight photos: {sum(c['freight_photos_required'] for c in cs)} | in/out times: {sum(c['in_out_times_required'] for c in cs)} | type overrides: {sum(1 for c in cs if c['doc_type_overrides'])}")
        print(f"  deliver-out with detention known: {sum(1 for c in cs if c['deliver_out_with_detention'] is not None)} | detention sheet rows: {len(data['detention_layovers'])} | global SOP sheets: {list(data['global_sops'])}")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    index = LoadIndex.from_file(args.index)
    args.reqs = Requirements.from_file(args.requirements) if Path(args.requirements).exists() else None
    print(f"index: {len(index.loads)} active loads from {args.index}")
    print(f"customer requirements: {len(args.reqs.customers) + ' customers' if False else (str(len(args.reqs.customers)) + ' customers from ' + args.requirements) if args.reqs else 'none loaded (run --build-rules first)'}\n")

    # ---- offline path: match saved extractions ----
    if args.from_json is not None:
        for jp in expand(args.from_json):
            data = json.loads(Path(jp).read_text(encoding="utf-8"))
            ex = Extraction.model_validate(data["extraction"] if "extraction" in data else data)
            source_name = data.get("source", jp.name)
            report_match(source_name, ex, index, args, adjudicate_fn=None, doc=None, out_dir=out_dir, usage=[])
        return 0

    files = expand(args.files)
    if not files:
        ap.error("no input files")

    # ---- normalize ----
    docs = []
    for f in files:
        d = load_document(f, max_edge=args.max_edge)
        print(f"{f.name}: {len(d.pages)} page(s), producer={d.producer or 'n/a'}, ~{d.approx_tokens:,} image tokens, sha256={d.sha256[:12]}")
        for p in d.pages:
            print(f"   page {p.number}: {p.width}x{p.height} dhash={p.dhash} text_layer={'yes' if p.text_layer else 'no'}")
            if args.normalize_only:
                (out_dir / f"{f.stem}_p{p.number}.png").write_bytes(p.png)
        docs.append(d)
    if args.normalize_only:
        print(f"\npage images written to {out_dir}")
        return 0

    # ---- read + match (+ adjudicate) ----
    # Claude models use the Anthropic SDK (reader.py). Any other slug (openai/..., google/..., meta-llama/...)
    # goes through the OpenAI-compatible adapter (reader_openai.py) against OpenRouter's standard endpoint.
    from pod_intake.localenv import load_local_env
    load_local_env()                                      # ANTHROPIC_* / OPENROUTER_API_KEY from the project .env
    import anthropic
    from pod_intake import reader as claude_reader

    def is_claude(slug: str) -> bool:
        return "claude" in slug.lower() or slug.lower().startswith("anthropic/")

    backends: dict[str, tuple] = {}

    def backend(slug: str):
        kind = "anthropic" if is_claude(slug) else "openai-compatible"
        if kind not in backends:
            if kind == "anthropic":
                try:
                    backends[kind] = (claude_reader, anthropic.Anthropic())
                except anthropic.AnthropicError as e:
                    raise SystemExit(f"Cannot create the Anthropic client: {e}\nSet ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL as described in README.md.")
            else:
                from pod_intake import reader_openai
                try:
                    backends[kind] = (reader_openai, reader_openai.make_client())
                except RuntimeError as e:
                    raise SystemExit(str(e))
        return backends[kind]

    for d in docs:
        mod, client = backend(args.model)
        print(f"\n=== {d.path.name} -> reading with {args.model} [{'Anthropic SDK' if mod is claude_reader else 'OpenAI-compatible via OpenRouter'}]")
        try:
            ex, usage = mod.read_document(client, d, args.model, effort=args.effort)
        except (anthropic.AuthenticationError, TypeError) as e:
            # The Anthropic SDK raises TypeError("Could not resolve authentication method...") when no key or profile exists.
            if isinstance(e, TypeError) and "authentication" not in str(e).lower():
                raise
            print("No credentials found. Either set ANTHROPIC_API_KEY (PowerShell: $env:ANTHROPIC_API_KEY='sk-ant-...'),\n"
                  "run `ant auth login`, or for an OpenRouter key set ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN as described in README.md.")
            return 2
        except anthropic.APIStatusError as e:
            print(f"API error {e.status_code}: {e.message}")
            return 2
        cost_note = f"est ${usage.cost_usd:.4f}" if usage.cost_usd else "cost n/a (provider did not return it)"
        print(f"   read: {usage.input_tokens:,} in / {usage.output_tokens:,} out, cache read {usage.cache_read:,}, {cost_note}")
        (out_dir / f"{d.path.stem}.extraction.json").write_text(json.dumps({"source": d.path.name, "extraction": ex.model_dump()}, indent=1), encoding="utf-8")

        def adjudicate_fn(extraction, candidates):
            amod, aclient = backend(args.adjudicator)
            return amod.adjudicate(aclient, d, extraction, candidates, args.adjudicator, effort=args.effort)

        report_match(d.path.name, ex, index, args, adjudicate_fn if not args.no_adjudicate else None, d, out_dir, [usage])
    return 0


def report_match(name: str, ex: Extraction, index: LoadIndex, args, adjudicate_fn, doc, out_dir: Path, usage: list) -> None:
    hints = [] if args.ignore_filename else [name]
    if args.subject:
        hints.append(args.subject)
    cands = score_candidates(ex, index, sender_phone=args.sender_phone, sender_email=args.sender_email, text_hints=hints)
    result = decide(cands)
    load = index.by_id(result.load_id) if result.load_id else None
    doc_type, type_reason = classify_type(ex, load)
    decision = {"High": "auto-filed", "Medium": "queued for review", "Low": "held as unmatched"}[result.tier]
    adj = None

    if result.tier == "Medium" and adjudicate_fn is not None:
        top3 = [index.public(index.by_id(c.load_id)) for c in cands[:3]]
        print(f"   tier Medium -> adjudicating with {args.adjudicator} against {[c.load_id for c in cands[:3]]}")
        adj, adj_usage = adjudicate_fn(ex, top3)
        usage.append(adj_usage)
        print(f"   adjudicate: {adj_usage.input_tokens:,} in / {adj_usage.output_tokens:,} out, est ${adj_usage.cost_usd:.4f}")
        if adj.load_id and adj.confidence >= 0.85:
            result.tier, result.load_id, decision = "High", adj.load_id, "auto-filed after adjudication"
            load = index.by_id(adj.load_id)
            doc_type, type_reason = classify_type(ex, load)
        else:
            decision = "queued for review (adjudicator unsure)"

    # ---- customer-specific requirements (PRD addition): pod from the load's terminal, rules by customer within that pod ----
    customer_name = args.customer or (load or {}).get("customer")
    terminal_id = (load or {}).get("terminal_id")
    pod_ctx = args.reqs.pod_context(terminal_id) if getattr(args, "reqs", None) else None
    rules = args.reqs.for_customer(customer_name, terminal_id) if getattr(args, "reqs", None) else None
    if rules:
        ov = rules.get("doc_type_overrides") or {}
        if ov.get("receiver_signed") and ex.signatures.receiver_signed and doc_type in ("Proof of Delivery", "Bill Of Lading"):
            doc_type, type_reason = ov["receiver_signed"], f"{rules['customer']} rule: receiver-signed copy is filed as {ov['receiver_signed']}"
        elif ov.get("shipper_copy") and not ex.signatures.receiver_signed and doc_type == "Bill Of Lading":
            doc_type, type_reason = ov["shipper_copy"], f"{rules['customer']} rule: shipper copy is filed as {ov['shipper_copy']}"
    name_signals = {s.name for s in cands[0].signals} if cands else set()
    verdict = check_document(ex, doc_type, rules, load, name_signals)

    print(f"\n--- {name}")
    print(f"   reader type: {ex.document_type} ({ex.document_type_confidence:.2f}); filed as: {doc_type} [{type_reason}]")
    print(f"   numbers: " + ", ".join(f"{n.label}={n.value}{'*' if n.handwritten else ''}" for n in ex.numbers[:8]) + (" ..." if len(ex.numbers) > 8 else ""))
    print(f"   shipper: {ex.shipper.name} ({ex.shipper.city}) -> consignee: {ex.consignee.name} ({ex.consignee.city})")
    print(f"   receiver signed: {ex.signatures.receiver_signed} {ex.signatures.receiver_name or ''} {ex.signatures.receiver_date or ''} | times: {ex.times.check_in} / {ex.times.check_out} ({ex.times.source}, at the {ex.times.at_stop} stop)")
    for c in cands[:3]:
        print(f"   candidate {c.load_id}: score {c.score:.1f} ({c.strong} strong, {c.medium} medium)")
        for s in c.signals:
            print(f"       [{s.strength:6}] {s.name}: {s.detail}")
    print(f"   => tier {result.tier}: {result.reason}")
    if adj:
        print(f"   adjudicator: load {adj.load_id} conf {adj.confidence:.2f} - {adj.reasoning}")
        if adj.conflicts:
            print(f"   conflicts: {adj.conflicts}")
    comment = filing_comment(doc_type, result.load_id, args.channel, datetime.now(), decision, cands[0].signals if cands else []) if result.load_id else "(no load)"
    if pod_ctx:
        print(f"   pod: {pod_ctx['pod'] or 'unknown'} (terminal {pod_ctx['terminal_id']}) - {pod_ctx['status']}" + (f"; sheet '{pod_ctx['sheet']}' ({pod_ctx['map_confidence']} confidence mapping)" if pod_ctx.get('sheet') else ""))
    if rules:
        print(f"   customer requirements ({rules['customer']}, {rules['sheet']}, AM {rules['am']}):")
        if rules.get("cross_pod"):
            print(f"       note: this rule set comes from the {rules['sheet']} sheet, not this load's pod; confirm it applies")
        for r in verdict.results:
            print(f"       [{r.status:7}] {r.rule}: {r.detail}")
        print(f"   => {verdict.summary}")
        if result.load_id:
            comment += f" | requirements: {'FAIL' if verdict.failed else 'pass'}" + (f" ({'; '.join(r.detail for r in verdict.failed)[:120]})" if verdict.failed else "")
    elif customer_name:
        print(f"   customer requirements: none on file for '{customer_name}'" + (f" in {pod_ctx['sheet']}" if pod_ctx and pod_ctx.get('sheet') else ""))
    print(f"   File History comment: {comment}")
    if ex.notes:
        print(f"   notes: {ex.notes}")
    total = sum(u.cost_usd for u in usage)
    if usage:
        print(f"   model spend this document: ${total:.4f}")

    report = {
        "source": name, "tier": result.tier, "decision": decision, "load_id": result.load_id, "document_type": doc_type,
        "type_reason": type_reason, "reason": result.reason, "comment": comment,
        "candidates": [{"load_id": c.load_id, "score": c.score, "signals": [s.__dict__ for s in c.signals]} for c in cands[:5]],
        "adjudication": adj.model_dump() if adj else None,
        "pod": pod_ctx,
        "requirements": {"customer": customer_name, "matched_rule_set": rules["customer"] if rules else None, "rule_sheet": rules["sheet"] if rules else None,
                         "cross_pod": rules.get("cross_pod") if rules else None,
                         "results": [r.__dict__ for r in verdict.results], "summary": verdict.summary} if customer_name else None,
        "extraction": ex.model_dump(),
        "usage": [u.as_dict() for u in usage], "estimated_cost_usd": round(total, 5),
    }
    (out_dir / f"{Path(name).stem}.match.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
