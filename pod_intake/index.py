"""The active-load index (PRD FR-18), read from a JSON snapshot for the prototype."""
from __future__ import annotations

import json
import re
from pathlib import Path


def norm_code(value: str | None) -> str:
    """Normalize a reference number for comparison: uppercase, letters and digits only."""
    if not value:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def norm_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def norm_name(value: str | None) -> str:
    v = (value or "").lower()
    v = re.sub(r"[^a-z0-9 ]", " ", v)
    v = re.sub(r"\b(inc|llc|ltd|co|corp|company|the|of|plant|dc|distribution center|warehouse|whse)\b", " ", v)
    return " ".join(v.split())


def edit_distance_le1(a: str, b: str) -> bool:
    """True when a and b differ by at most one substitution (same length only)."""
    if len(a) != len(b):
        return False
    return sum(1 for x, y in zip(a, b) if x != y) <= 1


class LoadIndex:
    def __init__(self, loads: list[dict]):
        self.loads = loads
        for ld in self.loads:
            ld["_ref_values"] = {norm_code(v) for v in (ld.get("refs") or {}).values() if v}
            ld["_note_values"] = {norm_code(v) for v in ld.get("note_numbers", []) if v}
            ld["_driver_phone"] = norm_phone(ld.get("driver_phone"))

    @classmethod
    def from_file(cls, path: str | Path) -> "LoadIndex":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["loads"])

    def by_id(self, load_id: int) -> dict | None:
        return next((ld for ld in self.loads if ld["load_id"] == load_id), None)

    def public(self, ld: dict) -> dict:
        """Load record without private helper fields, for prompts and reports."""
        return {k: v for k, v in ld.items() if not k.startswith("_")}
