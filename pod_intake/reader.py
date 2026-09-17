"""Stage 1 (read) and Stage 3 (adjudicate): the two Claude calls.

Both use structured outputs via client.messages.parse(output_format=<Pydantic model>).
The system prompt is cached; only the document content changes per call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

from .normalize import Document
from .schema import Adjudication, Extraction, reader_json_schema

# Anthropic list prices per million tokens (input, output). Cache reads are billed at ~0.1x input,
# cache writes at ~1.25x input. Source: claude-api skill reference, 24 Jun 2026. Verify before budgeting.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

READER_SYSTEM = """You read freight documents for a truckload brokerage: bills of lading (BOL), proofs of delivery (POD), lumper receipts, scale tickets, reefer logs, carrier invoices, rate confirmations, other shipment paperwork (packing lists, certificates of analysis, customs and commercial invoices, temperature-recorder sheets), and photos of freight.

Your job is transcription and classification, not matching. Extract exactly what is printed or written; never infer a number that is not on the page.

Rules:
- Capture EVERY reference number with the label printed beside it: BOL / B/L No., master bill (MB / MST), PO, pickup number, shipment number, delivery number (Del.#), invoice number, trip or load number, order numbers, shipper's number, seal, trailer, tractor/truck. Long numbers are common (up to 17 digits); transcribe digit by digit.
- Mark handwritten values as handwritten and lower their confidence.
- Seals: a 6-9 digit number handwritten in or beside the Seal, Vehicle No. or Carrier box of a BOL is the trailer seal; list it with kind "seal" and handwritten true, even when nothing printed says "seal". A photo of a plastic or cable seal on trailer doors is document_type "photo"; transcribe the digits printed on the seal as a number with kind "seal" and say so in notes. Load 2575200 on 14 Sep 2026 had both: the reader missed the handwritten seal.
- Unlabeled handwritten numbers (for example a string scrawled at the foot of the page like "27060 / 24379 / 114") must ALSO be listed in numbers[], one entry per number, with label "(unlabeled handwriting)" and kind "other". Drivers often write their tractor and trailer numbers this way, and those are matching evidence. Mention them in notes as well.
- Dates: give the date only, without a time of day, in the format written on the page.
- A sheet is a proof of delivery only if a receiver has signed or stamped it (a name, date, "Received", or in/out times at the consignee). Otherwise it is a bill of lading. Signature lines that are blank are not signed. Report each signature line separately (shipper, driver, receiver) and set stamp_present when an inked or printed receiving stamp appears anywhere on the page.
- If the file carries typed text such as "check in 06:00 / check out 12:00", treat it as an app stamp and report those times with source app_stamp.
- In/out times: say WHICH STOP they belong to in times.at_stop. Use "consignee" when they were recorded at the delivery stop, "shipper" when they were recorded at pickup, and "unknown" when the page does not make it clear (a bare handwritten time in a margin with nothing naming the stop). This is what decides whether the times are evidence of delivery: a departure time written at the shipper is not, however much it looks like one written at the consignee. Read the stop from the section of the form the times sit in, the address printed beside them, or a header such as "SHIPPER" / "CONSIGNEE" / "RECEIVED AT". If the page carries in/out times for BOTH stops, report the CONSIGNEE pair in check_in / check_out, set at_stop "consignee", and give the pickup times in notes.
- Describe legibility honestly. Glare, blur, skew, and cut-off edges belong in notes.
- If several different documents are in one file, classify each page in pages[] and set document_type to the primary one.
- If the image is not a freight document at all (an email signature, a company logo or letterhead, a certification badge strip, a screenshot of a chat or a web page, a selfie), set document_type to "other", say what it is in notes, and leave numbers, parties and signatures empty. Use "unknown" only for a freight document you cannot read. Never describe such an image in prose: still return the JSON object. A photo or phone screenshot OF a freight document, even one page of several and even with handwritten remarks on it, is that document, never "other" (load 2576409: a screenshot of delivery-order page 2 was wrongly called other).
- Packing lists, certificates of analysis (CofA), commercial or customs invoices, shipper's letters of instruction, temperature-recorder and reefer sheets, and similar paperwork that travels with the freight but is neither a BOL nor a POD: document_type "shipping_document" (it is filed in TransportPro as "Shipping Documents"). Still transcribe its numbers (lot, PO, order, batch). Never call such paperwork "other".
- Origin paperwork signed by the DRIVER when collecting the freight (a CFS or warehouse "delivery" or release receipt, gate pass, pickup receipt, an airline or forwarder delivery order) is pickup paperwork: document_type bill_of_lading, and say in notes what the form is. proof_of_delivery needs the CONSIGNEE's signature, stamp or printed name at the destination; a signature line filled at the origin does not make a POD, and "delivery" in a form's title does not either.
- Phone numbers: digits only. Dates: as written."""

ADJUDICATOR_SYSTEM = """You resolve ambiguous freight documents to the correct load for a truckload brokerage.

You receive: the page images, the structured extraction from the document, and up to three candidate loads from the brokerage's TMS with their reference numbers, stops, dates, carrier, driver phone, and equipment.

Decide which candidate the document belongs to, or none. Weigh evidence like a careful dispatcher:
- A load or trip number printed on the paper that equals a candidate's load ID is decisive unless the rest of the page contradicts it.
- Shipper reference numbers (BOL, PO, pickup, shipment) that equal a candidate's reference fields are strong. One digit off on a long number is plausible OCR error only if every other fact agrees.
- Shipper and consignee names and cities, dates, carrier, and driver phone corroborate but rarely decide alone.
- Trailer and tractor numbers are weak: trailers get swapped.
- A stale email subject is not evidence.
Return the load, the document type (POD only if a receiver signed), your confidence, your reasoning in two or three sentences, and any conflicts."""


@dataclass
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int

    @property
    def cost_usd(self) -> float:
        key = self.model.split("/")[-1]                      # tolerate provider-prefixed slugs like anthropic/claude-opus-5
        inp, out = PRICES.get(key, PRICES.get(key.replace(".", "-"), (5.00, 25.00)))
        return (self.input_tokens * inp + self.cache_read * inp * 0.1 + self.cache_write * inp * 1.25 + self.output_tokens * out) / 1_000_000

    def as_dict(self) -> dict:
        return {"model": self.model, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read, "cache_write_tokens": self.cache_write, "estimated_cost_usd": round(self.cost_usd, 5)}


def _usage(model: str, response) -> Usage:
    u = response.usage
    return Usage(model=model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                 cache_read=getattr(u, "cache_read_input_tokens", 0) or 0, cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0)


def _check_stop(response) -> None:
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise RuntimeError(f"Model refused the request: {getattr(details, 'category', None)} {getattr(details, 'explanation', '')}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Response hit max_tokens; increase max_tokens.")


def _page_blocks(doc: Document) -> list[dict]:
    return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": p.b64}} for p in doc.pages]


def _output_format(schema_model) -> dict:
    """Same JSON-schema format the SDK's messages.parse() builds, so the Anthropic API enforces the schema."""
    schema = reader_json_schema(schema_model)
    try:  # the SDK's transform adds the strictness the API expects; private helper, so degrade gracefully
        from anthropic.lib._parse._transform import transform_schema
        schema = transform_schema(schema)
    except Exception:
        pass
    return {"type": "json_schema", "schema": schema}


def _extract_json(text: str) -> str:
    """Tolerate proxies that ignore structured outputs: strip code fences and any prose around the object."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start != -1 and end != -1 else t


def _structured_call(client: anthropic.Anthropic, model: str, system: str, content: list[dict], schema_model, effort: str | None = None):
    """One call, schema-constrained where the endpoint supports it, tolerant where it does not.

    Against the Anthropic API, output_config.format guarantees valid JSON. Third-party Anthropic-compatible
    endpoints (for example OpenRouter's) may ignore the constraint or reject it; the first case is handled
    by parsing the text ourselves, the second by retrying with the schema pasted into the prompt.

    effort ("low" | "medium" | "high") trades thinking depth for cost on Opus 5 / Sonnet 5. It is not sent
    for Haiku 4.5, which rejects the parameter.
    """
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": content}]
    schema_hint = {"type": "text", "text": "Respond with ONLY a JSON object that validates against this JSON schema. No prose, no code fences.\n" + json.dumps(reader_json_schema(schema_model))}
    output_config: dict = {"format": _output_format(schema_model)}
    if effort and "haiku" not in model.lower():
        output_config["effort"] = effort
    try:
        response = client.messages.create(
            model=model, max_tokens=8000, system=system_blocks, messages=messages,
            output_config=output_config,
        )
    except anthropic.BadRequestError as e:
        if not any(k in str(e).lower() for k in ("output_config", "output_format", "format", "schema")):
            raise
        print("   note: endpoint rejected structured outputs; retrying with the schema in the prompt")
        response = client.messages.create(
            model=model, max_tokens=8000, system=system_blocks,
            messages=[{"role": "user", "content": content + [schema_hint]}],
        )
    _check_stop(response)
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        parsed = schema_model.model_validate_json(_extract_json(text))
    except Exception:
        # Seen through OpenRouter on an email-signature graphic: the model explained in Markdown that the image is not a
        # freight document instead of returning JSON. Ask once more, JSON only, then give up loudly.
        print("   note: model answered in prose; retrying once with a JSON-only instruction")
        retry_hint = {"type": "text", "text": "Your previous answer was prose. Respond with ONLY the JSON object for this schema, no prose, no code fences. "
                      "If the image is not a freight document (a logo, an email signature, a screenshot of something else), "
                      "set document_type to \"other\", explain in notes, and leave the lists empty. "
                      + json.dumps(reader_json_schema(schema_model))}
        response2 = client.messages.create(model=model, max_tokens=8000, system=system_blocks,
                                           messages=[{"role": "user", "content": content + [retry_hint]}])
        _check_stop(response2)
        text2 = next((b.text for b in response2.content if b.type == "text"), "")
        try:
            parsed = schema_model.model_validate_json(_extract_json(text2))
        except Exception as e:
            raise RuntimeError(f"Model response did not validate against {schema_model.__name__} (twice): {e}\n--- first response (600 chars) ---\n{text[:600]}") from e
        u1, u2 = _usage(model, response), _usage(model, response2)
        u1.input_tokens += u2.input_tokens
        u1.output_tokens += u2.output_tokens
        return parsed, u1
    return parsed, _usage(model, response)


def read_document(client: anthropic.Anthropic, doc: Document, model: str, effort: str | None = None) -> tuple[Extraction, Usage]:
    content = _page_blocks(doc)
    if doc.text_layer:
        content.append({"type": "text", "text": f"The file also carries this typed text layer (likely an app stamp, not part of the printed form):\n{doc.text_layer}"})
    content.append({"type": "text", "text": f"File name: {doc.path.name}. {len(doc.pages)} page(s). Extract the document per the schema."})
    return _structured_call(client, model, READER_SYSTEM, content, Extraction, effort=effort)


def adjudicate(client: anthropic.Anthropic, doc: Document, extraction: Extraction, candidates: list[dict], model: str, effort: str | None = None) -> tuple[Adjudication, Usage]:
    content = _page_blocks(doc)
    content.append({"type": "text", "text": "Structured extraction of this document:\n" + extraction.model_dump_json(indent=1)})
    content.append({"type": "text", "text": "Candidate loads from the TMS (JSON):\n" + json.dumps(candidates, indent=1)})
    content.append({"type": "text", "text": "Which candidate does this document belong to, if any? Answer per the schema."})
    return _structured_call(client, model, ADJUDICATOR_SYSTEM, content, Adjudication, effort=effort)
