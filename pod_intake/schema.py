"""Structured-output schemas for the reader and the adjudicator (Pydantic v2)."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

NumberKind = Literal[
    "bol", "master_bill", "po", "pickup", "shipment", "delivery", "invoice",
    "load_or_trip", "order", "seal", "trailer", "tractor", "shipper_ref", "other",
]
DocType = Literal[
    "bill_of_lading", "proof_of_delivery", "lumper", "weight_ticket", "reefer_log",
    "carrier_invoice", "rate_confirmation", "photo", "unknown", "other", "shipping_document",
]


class NumberField(BaseModel):
    label: str = Field(description="The label printed next to the number, exactly as on the paper, e.g. 'Pickup Nbr', 'B/L No.', 'Trip #'.")
    kind: NumberKind
    value: str = Field(description="The number or code exactly as printed. Keep letters, digits, '#', '-' and '/'. No spaces.")
    handwritten: bool
    confidence: float = Field(ge=0, le=1)


class Party(BaseModel):
    name: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None


class Signatures(BaseModel):
    shipper_signed: bool
    driver_signed: bool
    receiver_signed: bool
    receiver_name: Optional[str] = None
    receiver_date: Optional[str] = Field(default=None, description="As written, e.g. '9-11-26'.")
    stamp_present: bool = Field(default=False, description="True if a receiving or company stamp (inked or printed) appears on the page.")


class Times(BaseModel):
    check_in: Optional[str] = None
    check_out: Optional[str] = None
    source: Literal["app_stamp", "handwritten", "printed", "none"]
    at_stop: Literal["shipper", "consignee", "unknown"] = Field(
        default="unknown",
        description="Which stop these times were recorded at. 'consignee' means they are evidence the "
                    "freight was delivered; 'shipper' means they are a pickup departure and are not. "
                    "'unknown' when the page does not say, and the value every extraction read before "
                    "this field existed carries - it must keep meaning what it meant then.")


# Fields that carry a Python default - so that extractions read before the field existed still
# validate - but that the model must answer all the same. Pydantic leaves a defaulted field out of
# `required`, which lets the model omit it, and the default then comes back indistinguishable from a
# real answer. For at_stop that is precisely the ambiguity the field was added to remove: "unknown"
# has to mean the reader looked at the page and could not tell, never that it was never asked.
MUST_ANSWER: dict[str, tuple[str, ...]] = {"Times": ("at_stop",)}


def reader_json_schema(model: type[BaseModel]) -> dict:
    """The model's JSON schema with MUST_ANSWER fields promoted into `required`.

    Every place that shows a schema to a model goes through this - the structured-output config and
    the prompt fallbacks on both the Anthropic and the OpenRouter path - so a field cannot be
    required down one route and optional down another. Promotion only; nothing is ever removed from
    `required`, and a name that does not match any definition is ignored rather than raising, so
    renaming a model degrades to the old behaviour instead of breaking every call.
    """
    schema = model.model_json_schema()
    for name, fields in MUST_ANSWER.items():
        for defn in ([schema] if schema.get("title") == name else []) + \
                    [d for k, d in (schema.get("$defs") or {}).items() if k == name]:
            required = list(defn.get("required") or [])
            required += [f for f in fields if f in (defn.get("properties") or {}) and f not in required]
            defn["required"] = required
    return schema


class PageInfo(BaseModel):
    page: int
    role: Literal["bol", "pod", "lumper", "weight_ticket", "reefer_log", "invoice", "rate_confirmation", "photo", "other"]
    legibility: float = Field(ge=0, le=1, description="1 = crisp and fully readable, 0 = unreadable.")


class Extraction(BaseModel):
    document_type: DocType
    document_type_confidence: float = Field(ge=0, le=1)
    numbers: List[NumberField]
    shipper: Party
    consignee: Party
    carrier_name: Optional[str] = None
    carrier_dot: Optional[str] = None
    carrier_mc: Optional[str] = None
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = Field(default=None, description="Digits only.")
    ship_date: Optional[str] = None
    delivery_date: Optional[str] = None
    signatures: Signatures
    times: Times
    pieces: Optional[str] = None
    weight_lbs: Optional[str] = None
    pages: List[PageInfo]
    notes: str = Field(description="Anything a reviewer should know: handwriting, glare, several documents in one file, unusual layout.")


class Adjudication(BaseModel):
    load_id: Optional[int] = Field(default=None, description="The chosen load, or null if none of the candidates fits.")
    document_type: DocType
    confidence: float = Field(ge=0, le=1)
    reasoning: str
    conflicts: List[str] = Field(description="Facts on the paper that disagree with the chosen load, if any.")
