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
