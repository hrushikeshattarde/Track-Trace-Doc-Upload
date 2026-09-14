"""Read one load's email attachments with the prototype reader: type, requirements verdict, seal reconciliation.

Used two ways, with ONE code path so the two never drift again (on 14 Sep 2026 the readiness job's in-process branch
lacked the seal check that this file had):
  - readiness.py imports read_files() when the model SDKs are importable (the project .venv);
  - readiness.py runs this file as a subprocess in another interpreter when they are not, and parses the JSON lines.
Read-only: nothing is uploaded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

NON_DOCUMENT_TYPES = ("other", "unknown")
_CLIENTS: dict[str, tuple] = {}


def _backend(model: str):
    """(module, client) for the model slug; Claude slugs use the Anthropic SDK, anything else the OpenAI-compatible adapter."""
    kind = "anthropic" if ("claude" in model.lower() or model.lower().startswith("anthropic/")) else "openai"
    if kind not in _CLIENTS:
        if kind == "anthropic":
            import anthropic
            from pod_intake import reader as mod
            _CLIENTS[kind] = (mod, anthropic.Anthropic())
        else:
            from pod_intake import reader_openai as mod
            _CLIENTS[kind] = (mod, mod.make_client())
    return _CLIENTS[kind]


EQUIPMENT_LABEL_RE = __import__("re").compile(r"trailer|tractor|truck|unit|plate|vin|mc#|mc |dot", __import__("re").I)
SEAL_SHAPE_RE = __import__("re").compile(r"^[A-Za-z]{0,3}[0-9][0-9A-Za-z-]{3,11}$")   # seals: 5-12 chars, mostly digits, no slashes or spaces
LOT_CODE_RE = __import__("re").compile(r"^.*-[0-9]{1,2}$")                              # 720210122-03: a manufacturer lot code printed under the seal number


def seal_values(ex, exclude: set[str] | None = None) -> set[str]:
    """Seal numbers on an extraction, whatever kind the model chose: kind "seal", a label mentioning seal, or, on a
    photo whose notes mention a seal, any 5-12 digit number. Model kind tagging varies run to run; labels do not.
    `exclude` holds the load's trailer and tractor numbers, which are painted on the same doors as the seal and appear
    on the BOL too (load 2578982: trailer 535605 was taken for a seal)."""
    exclude = {e.strip() for e in (exclude or set()) if e and e.strip()}
    out: set[str] = set()
    for n in ex.numbers:
        value = (n.value or "").strip().replace(" ", "")
        if not value or value in exclude or EQUIPMENT_LABEL_RE.search(n.label or "") or not SEAL_SHAPE_RE.match(value) or LOT_CODE_RE.match(value):
            continue
        if n.kind == "seal" or "seal" in (n.label or "").lower():
            out.add(value)
    # A photo of a placard, a registration or a container door yields a spray of numbers that the model may all tag
    # "seal" (load 2576660: six values including a date and two alphanumerics). More than two candidates on one file
    # means the file is not a seal photo; keep none rather than reconcile noise.
    return out if len(out) <= 2 else set()


def _utc(s: str | None):
    import datetime as dt
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_files(files: list[Path], model: str, requirements: str | Path | None, customer: str | None,
               terminal: int | None, stage: str, exclude_numbers: set[str] | None = None,
               file_times: dict[str, str] | None = None, pod_not_before: str | None = None) -> list[dict]:
    """One dict per file (document_type, filed_type, verdict, numbers, cost or error / not_document), then, when any seal
    was read, a final {"file": "*", "load_note": ...} reconciling the BOL's seal with the seal photo."""
    from pod_intake.localenv import load_local_env
    load_local_env()                                      # ANTHROPIC_* / OPENROUTER_API_KEY from the project .env
    from pod_intake.normalize import load_document
    from pod_intake.matcher import classify_type
    from pod_intake.requirements import Requirements, check_document

    mod, client = _backend(model)
    reqs = Requirements.from_file(requirements) if requirements and Path(requirements).exists() else None
    rules = reqs.for_customer(customer or None, terminal) if reqs else None
    load = {"dispatch_status": (stage or "").title()}
    bol_seals: set[str] = set()              # seal numbers read on BOL files (usually handwritten)
    photo_seals: set[str] = set()            # seal digits read on photos of the trailer seal
    bol_all: set[str] = set()                # every number on the BOL files, to recognise a second seal written under another label
    results: list[dict] = []
    for f in files:
        f = Path(f)
        out: dict = {"file": f.name}
        try:
            doc = load_document(f)
            ex, usage = mod.read_document(client, doc, model)
            cost = round(getattr(usage, "cost_usd", 0.0), 4)
            filed_type, why = classify_type(ex, load)
            import datetime as dt
            sent = _utc((file_times or {}).get(f.name))
            window = _utc(pod_not_before)
            if filed_type == "Proof of Delivery" and sent and window and sent < window - dt.timedelta(hours=24):
                # Load 2576660: the house bill scanned on pickup day was read as a POD; the truck delivered three days later.
                filed_type = "Bill Of Lading"
                why = f"emailed {sent:%m/%d %H:%M}Z, more than a day before the delivery window ({window:%m/%d}): pickup paperwork, not a POD"
            out.update({"document_type": ex.document_type, "cost_usd": cost, "notes": (getattr(ex, "notes", "") or "")[:200], "type_reason": why[:160],
                        "numbers": [{"label": n.label, "kind": n.kind, "value": n.value, "handwritten": bool(getattr(n, "handwritten", False))} for n in ex.numbers][:12]})
            if ex.document_type in NON_DOCUMENT_TYPES:
                out["not_document"] = why
                results.append(out)
                continue
            v = check_document(ex, filed_type, rules, load, set())
            found = seal_values(ex, exclude_numbers)
            # Any paper document (BOL, or a form the reader mistook for a POD) is the "BOL side"; only photos are the photo side.
            (photo_seals if ex.document_type == "photo" else bol_seals).update(found)
            if ex.document_type != "photo":
                bol_all.update((n.value or "").strip().replace(" ", "") for n in ex.numbers if n.value)
            out.update({"filed_type": filed_type, "verdict": v.summary, "seals": sorted(found)})
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the others
            out["error"] = f"{type(e).__name__}: {e}"
        results.append(out)
    note = seal_note(bol_seals, photo_seals, bol_all)
    if note:
        results.append({"file": "*", "load_note": note})
    return results


def seal_note(bol_seals: set[str], photo_seals: set[str], bol_all: set[str] | None = None) -> str | None:
    """Cross-file reconciliation for one load. Handwritten seal boxes and angled metal straps get misread by a digit or
    two (load 2575200: BOL read 796046 vs photo 4796046; load 2578211: strap read 1712308 vs BOL 17723084), so a close
    match counts as the same seal but is said out loud for the reviewer; a real difference is an alarm."""
    if not photo_seals and not bol_seals:
        return None
    if not photo_seals:
        return f"seal {', '.join(sorted(bol_seals))} written on the BOL; no seal photo"
    if not bol_seals:
        return f"seal {', '.join(sorted(photo_seals))} read from the seal photo but no seal number found on the BOL: confirm by eye"
    exact = bol_seals & photo_seals
    if exact:
        extra = sorted(photo_seals - exact)
        return f"seal {', '.join(sorted(exact))} confirmed: same digits on the BOL and the seal photo; seal requirement met" + _extra_note(extra, bol_all)
    near = [(b, p) for b in bol_seals for p in photo_seals if len(b) >= 5 and len(p) >= 5 and (p.endswith(b) or b.endswith(p) or _edits(b, p) <= 2)]
    if near:
        b, p = near[0]
        extra = sorted(photo_seals - {p})
        return (f"seal photo read as {p}, BOL says {b}: probably the same seal misread on the angled strap (2 digits or fewer differ); seal requirement met, confirm by eye"
                + _extra_note(extra, bol_all))


def _extra_note(extra: list[str], bol_all: set[str] | None) -> str:
    """Second seal photographed: say whether it is written on the BOL under some other label (Uline #, cable seal...)."""
    if not extra:
        return ""
    on_bol = [e for e in extra if e in (bol_all or set())]
    off_bol = [e for e in extra if e not in (bol_all or set())]
    parts = []
    if on_bol:
        parts.append(f"a second seal {', '.join(on_bol)} was photographed and is also written on the BOL")
    if off_bol:
        parts.append(f"a second seal {', '.join(off_bol)} was photographed but is not on the BOL")
    return "; " + "; ".join(parts)
    return f"SEAL MISMATCH: BOL says {', '.join(sorted(bol_seals))}, seal photo says {', '.join(sorted(photo_seals))}: check before delivery"


def _edits(a: str, b: str) -> int:
    """Levenshtein distance; the strings are seal numbers, so at most a dozen characters."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--requirements", default=str(HERE / "index" / "customer_requirements.json"))
    ap.add_argument("--customer", default="")
    ap.add_argument("--terminal", default="")
    ap.add_argument("--stage", default="", help="readiness stage of the truck, e.g. loaded, at consignee, delivered")
    ap.add_argument("--exclude", default="", help="comma-separated equipment numbers (trailer, tractor) that must not be taken for a seal")
    ap.add_argument("--pod-not-before", default="", help="ISO time of the delivery appointment; a file emailed >24 h earlier cannot be the POD")
    ap.add_argument("--file-time", action="append", default=[], help="NAME=ISO time the attachment was emailed (repeatable)")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()
    terminal = int(a.terminal) if a.terminal.isdigit() else None
    exclude = {x.strip() for x in a.exclude.split(",") if x.strip()}
    file_times = dict(ft.split("=", 1) for ft in a.file_time if "=" in ft)
    for out in read_files([Path(f) for f in a.files], a.model, a.requirements, a.customer, terminal, a.stage, exclude, file_times, a.pod_not_before or None):
        print(json.dumps(out, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
