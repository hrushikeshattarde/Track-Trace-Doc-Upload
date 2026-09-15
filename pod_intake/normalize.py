"""Stage 0 - Normalize (no model).

Turns a PDF or photo into page images sized for the model, plus fingerprints and any text layer.
Uses PyMuPDF for both PDFs and images. Deskew is deliberately left out of the prototype.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

MAX_EDGE_PX = 1568          # Anthropic's recommended long edge; ~1,600 tokens per page
MIN_LOGO_PX = 300           # images smaller than this on both sides are treated as logos/icons


@dataclass
class PageImage:
    number: int
    png: bytes
    width: int
    height: int
    dhash: str
    text_layer: str = ""

    @property
    def b64(self) -> str:
        return base64.standard_b64encode(self.png).decode("ascii")

    @property
    def approx_tokens(self) -> int:
        return int(self.width * self.height / 750)


@dataclass
class Document:
    path: Path
    sha256: str
    producer: str
    pages: list[PageImage] = field(default_factory=list)

    @property
    def text_layer(self) -> str:
        return "\n".join(p.text_layer for p in self.pages if p.text_layer).strip()

    @property
    def approx_tokens(self) -> int:
        return sum(p.approx_tokens for p in self.pages)


def _dhash(page: pymupdf.Page) -> str:
    """64-bit difference hash: robust to recompression and small crops."""
    rect = page.rect
    pix = page.get_pixmap(matrix=pymupdf.Matrix(9 / rect.width, 8 / rect.height), colorspace=pymupdf.csGRAY, alpha=False)
    w, h, s = pix.width, pix.height, pix.samples
    bits = []
    for y in range(min(h, 8)):
        for x in range(min(w - 1, 8)):
            bits.append(1 if s[y * w + x] > s[y * w + x + 1] else 0)
    bits += [0] * (64 - len(bits))
    return f"{int(''.join(map(str, bits)), 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def load_document(path: str | Path, max_edge: int = MAX_EDGE_PX) -> Document:
    """max_edge: long-edge pixel size of the page images sent to the model. 1,568 is Anthropic's recommended
    ceiling; raise it to test whether a model's digit errors on long numbers come from resolution."""
    path = Path(path)
    raw = path.read_bytes()
    if path.suffix.lower() in (".heic", ".heif") or raw[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1"):
        # iPhone photos arrive as HEIC (load 2579013, 15 Sep 2026); PyMuPDF cannot open them, Pillow can with pillow-heif.
        import io
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()
        buf = io.BytesIO()
        Image.open(io.BytesIO(raw)).convert("RGB").save(buf, format="JPEG", quality=92)
        doc = pymupdf.open(stream=buf.getvalue(), filetype="jpg")
    else:
        doc = pymupdf.open(path)                  # PDFs and images alike
    meta = doc.metadata or {}
    out = Document(path=path, sha256=hashlib.sha256(raw).hexdigest(), producer=(meta.get("producer") or meta.get("creator") or "").strip())
    for i, page in enumerate(doc, start=1):
        rect = page.rect
        long_edge = max(rect.width, rect.height)
        if long_edge < MIN_LOGO_PX and doc.is_pdf is False:
            continue                              # tiny standalone image: logo or icon
        zoom = max_edge / long_edge
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False)
        out.pages.append(PageImage(
            number=i,
            png=pix.tobytes("png"),
            width=pix.width,
            height=pix.height,
            dhash=_dhash(page),
            text_layer=(page.get_text() or "").strip(),
        ))
    return out
