"""The free filters: everything that can reject an attachment without spending a model call.

Thresholds and the JPEG/PNG/GIF header parse are carried over unchanged from readiness.py, where
they were tuned against real traffic on 14 Sep 2026:

  - load 2573804's thread held 111 image parts and not one document: every one an email signature,
    letterhead or certification badge, re-quoted in each reply;
  - signature banners run about 500x150 and logos about 200x200, so a page never trips both the
    side and the area test;
  - TransportPro names its own generated files <fileId>_<typeId>.pdf, and type 23 is the Carrier
    Rate Agreement the thread started with - never a driver document.

Order matters: the metadata tests need only the part header Gmail already returned, so they cost
nothing. The geometry test needs the bytes, but downloading from Gmail is free - it is the model
call after it that is not.
"""
from __future__ import annotations

import re

MIN_ATTACHMENT_BYTES = 40_000      # below this an image is a signature, a logo or an illegible thumbnail
MIN_IMAGE_SIDE_PX = 300            # a screenshot or photo of a document is larger than this on both sides
MIN_IMAGE_AREA_PX = 150_000        # ~400x375; a phone photo is 1080x1920 or more
MAX_IMAGE_ASPECT = 2.2             # wider than this is a signature banner or a logo strip, never a page

TPRO_GENERATED_RE = re.compile(r"^(\d{7,9})_(\d{1,3})(?:\s*\(\d+\))?\.pdf$", re.I)
RATECON_TYPE_IDS = {"23", "143", "358", "367"}

KEEP = "keep"
TOO_SMALL = "too_small"
RATE_CONFIRMATION = "rate_confirmation"
SIGNATURE_OR_LOGO = "signature_or_logo"
DUPLICATE = "duplicate"
# Passed the metadata tests but the message has no load yet, so it was not downloaded. The reviewer
# working the unresolved list sees the filenames and sizes without the service having paid for
# anything; binding the message later leaves only the download and the hash to do.
PENDING = "pending"


def metadata_decision(filename: str | None, size: int | None) -> str:
    """Decide from the part header alone. Returns KEEP or the reason it was dropped."""
    name = (filename or "").strip()
    if not name:
        return TOO_SMALL                                  # an inline part with no filename is never a document
    if (size or 0) < MIN_ATTACHMENT_BYTES:
        return TOO_SMALL
    m = TPRO_GENERATED_RE.match(name)
    if m and m.group(2) in RATECON_TYPE_IDS:
        return RATE_CONFIRMATION
    return KEEP


def geometry_decision(data: bytes) -> tuple[str, tuple[int, int] | None]:
    """Decide from the image header. PDFs have no dimensions here and always pass through."""
    dims = image_dims(data)
    if dims is None:
        return KEEP, None
    w, h = dims
    if min(w, h) < MIN_IMAGE_SIDE_PX or w * h < MIN_IMAGE_AREA_PX or w > MAX_IMAGE_ASPECT * h:
        return SIGNATURE_OR_LOGO, dims
    return KEEP, dims


def image_dims(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG / GIF / JPEG header; None for anything else. No image library:
    the first few hundred bytes are enough and the whole point is to decide before doing work."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if marker in sof:
                return (int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big"))
            i += 2 + seg_len
    return None


def is_pdf(filename: str | None, mime: str | None) -> bool:
    return (mime or "").lower() == "application/pdf" or (filename or "").lower().endswith(".pdf")
