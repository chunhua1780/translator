"""Read text blocks out of a PDF page and write replacement text back in the same place.

Each text block keeps its bounding box, font size, colour and weight. When a block is
replaced, the original glyphs are removed with a redaction that leaves images and line
art alone, and the new text is laid into the same box, shrinking the font if needed.
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import asdict, dataclass

import pymupdf

CJK_RE = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")
LETTER_RE = re.compile(r"[A-Za-z㐀-鿿]")

OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "eng+chi_sim")


@dataclass
class TextBlock:
    id: int
    bbox: tuple[float, float, float, float]
    text: str
    size: float
    color: int
    bold: bool
    italic: bool
    align: str  # "left" or "center"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TextBlock":
        return cls(**{**d, "bbox": tuple(d["bbox"])})


@dataclass
class PageText:
    kind: str  # "text", "ocr" or "empty"
    blocks: list[TextBlock]


def _join_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and not out.endswith(" -"):
            out = out[:-1] + line  # re-join a hyphenated word
        elif CJK_RE.search(out[-1]) or CJK_RE.search(line[0]):
            out += line
        else:
            out += " " + line
    return out


def _is_scanned(page: pymupdf.Page) -> bool:
    """A page with no text layer whose area is mostly covered by images."""
    if page.get_text("text").strip():
        return False
    page_area = abs(page.rect)
    image_area = 0.0
    for info in page.get_image_info():
        image_area += abs(pymupdf.Rect(info["bbox"]) & page.rect)
    return page_area > 0 and image_area / page_area > 0.5


def extract_page(page: pymupdf.Page, allow_ocr: bool = True) -> PageText:
    kind = "text"
    textpage = None
    if _is_scanned(page):
        if not allow_ocr:
            return PageText("empty", [])
        try:
            textpage = page.get_textpage_ocr(language=OCR_LANGUAGES, dpi=300, full=True)
            kind = "ocr"
        except Exception:
            return PageText("empty", [])

    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    data = page.get_text("dict", flags=flags, textpage=textpage)
    page_cx = (page.rect.x0 + page.rect.x1) / 2
    blocks: list[TextBlock] = []
    for raw in data["blocks"]:
        if raw.get("type") != 0:
            continue
        lines = raw["lines"]
        # Leave rotated or vertical text untouched.
        if not lines or any(abs(l["dir"][0] - 1) > 0.01 for l in lines):
            continue
        spans = [s for l in lines for s in l["spans"] if s["text"].strip()]
        if not spans:
            continue
        text = _join_lines(["".join(s["text"] for s in l["spans"]) for l in lines])
        # Page numbers, codes and other blocks without words are kept as they are.
        if not LETTER_RE.search(text):
            continue
        main = max(spans, key=lambda s: len(s["text"]))
        bbox = tuple(round(v, 2) for v in raw["bbox"])
        cx = (bbox[0] + bbox[2]) / 2
        narrow = bbox[2] - bbox[0] < page.rect.width * 0.8
        align = "center" if len(lines) == 1 and narrow and abs(cx - page_cx) < page.rect.width * 0.02 else "left"
        blocks.append(
            TextBlock(
                id=len(blocks),
                bbox=bbox,
                text=text,
                size=round(main["size"], 2),
                color=0 if kind == "ocr" else main["color"],
                bold=bool(main["flags"] & 16) or "bold" in main["font"].lower(),
                italic=bool(main["flags"] & 2),
                align=align,
            )
        )
    if kind == "ocr":
        blocks = _merge_ocr_lines(blocks)
    return PageText(kind, blocks)


def _merge_ocr_lines(blocks: list[TextBlock]) -> list[TextBlock]:
    """OCR returns one block per line; join lines that belong to the same paragraph."""
    merged: list[TextBlock] = []
    for b in blocks:
        prev = merged[-1] if merged else None
        if (
            prev
            and abs(b.bbox[0] - prev.bbox[0]) < prev.size
            and 0 <= b.bbox[1] - prev.bbox[3] < prev.size * 0.8
            and abs(b.size - prev.size) < prev.size * 0.25
        ):
            prev.text = _join_lines([prev.text, b.text])
            prev.bbox = (
                min(prev.bbox[0], b.bbox[0]),
                prev.bbox[1],
                max(prev.bbox[2], b.bbox[2]),
                b.bbox[3],
            )
            prev.align = "left"
        else:
            merged.append(b)
    for i, b in enumerate(merged):
        b.id = i
    return merged


def _background(pix: pymupdf.Pixmap, rect: pymupdf.Rect, scale: float) -> tuple[float, float, float]:
    """Most common colour on the border of rect, i.e. the paper or panel behind the text."""
    x0, y0 = max(int(rect.x0 * scale), 0), max(int(rect.y0 * scale), 0)
    x1, y1 = min(int(rect.x1 * scale), pix.width - 1), min(int(rect.y1 * scale), pix.height - 1)
    counts: dict[tuple, int] = {}
    for x in range(x0, x1 + 1, 2):
        for y in (y0, y1):
            c = pix.pixel(x, y)[:3]
            counts[c] = counts.get(c, 0) + 1
    for y in range(y0, y1 + 1, 2):
        for x in (x0, x1):
            c = pix.pixel(x, y)[:3]
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return (1.0, 1.0, 1.0)
    r, g, b = max(counts, key=counts.get)
    return (r / 255, g / 255, b / 255)


def _css(block: TextBlock) -> str:
    return (
        "* {margin: 0; padding: 0; font-family: sans-serif; line-height: 1.15;"
        f" font-size: {block.size}px; color: #{block.color:06x};"
        f" font-weight: {'bold' if block.bold else 'normal'};"
        f" font-style: {'italic' if block.italic else 'normal'};"
        f" text-align: {block.align};}}"
    )


def apply_page(page: pymupdf.Page, kind: str, blocks: list[TextBlock], replacements: dict[int, str]) -> None:
    """Replace each block's text with replacements[block.id]; blocks without one stay as they are."""
    todo = [b for b in blocks if replacements.get(b.id, "").strip() and replacements[b.id] != b.text]
    if not todo:
        return
    if kind == "ocr":
        # Scanned text is part of an image: paint over it in the surrounding colour, then
        # write in black or white, whichever reads on that colour.
        scale = 1.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB)
        for b in todo:
            rect = pymupdf.Rect(b.bbox) + (-2, -2, 2, 2)
            bg = _background(pix, rect, scale)
            page.draw_rect(rect, color=None, fill=bg, overlay=True)
            luminance = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
            b.color = 0x000000 if luminance > 0.5 else 0xFFFFFF
    else:
        for b in todo:
            page.add_redact_annot(pymupdf.Rect(b.bbox), fill=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    for b in todo:
        body = html.escape(replacements[b.id].strip()).replace("\n", "<br>")
        rect = pymupdf.Rect(b.bbox)
        spare, _ = page.insert_htmlbox(rect, body, css=_css(b), scale_low=0.5)
        if spare < 0:
            # Still too long at half size: shrink as far as needed rather than drop text.
            page.insert_htmlbox(rect, body, css=_css(b), scale_low=0)


def build_output(src_path, pages: range, load_record, out_path, chunk_size: int = 25) -> None:
    """Write pages[...] of the source PDF with each page's replacements applied.

    insert_htmlbox embeds a full copy of the CJK font on every call, so pages are rebuilt
    in small chunks and each chunk's fonts are subset before moving on. That keeps memory
    and file size close to the original even for books of hundreds of pages.
    load_record(index) returns (kind, blocks, replacements) or None for an untouched page.
    """
    src = pymupdf.open(src_path)
    out = pymupdf.open()
    for start in range(pages.start, pages.stop, chunk_size):
        stop = min(start + chunk_size, pages.stop)
        part = pymupdf.open()
        part.insert_pdf(src, from_page=start, to_page=stop - 1)
        for offset, index in enumerate(range(start, stop)):
            record = load_record(index)
            if record:
                apply_page(part[offset], *record)
        part.subset_fonts()
        data = part.tobytes(garbage=3, deflate=True)
        part.close()
        with pymupdf.open("pdf", data) as packed:
            out.insert_pdf(packed)

    # Carry over the bookmarks that point inside the kept range.
    toc = [
        [level, title, page - pages.start]
        for level, title, page in src.get_toc(simple=True)
        if pages.start < page <= pages.stop
    ]
    if toc:
        top = min(level for level, _, _ in toc)
        fixed, prev = [], top - 1
        for level, title, page in toc:
            level = min(level - top + 1, prev + 1)  # set_toc rejects jumps of more than one level
            fixed.append([level, title, page])
            prev = level
        out.set_toc(fixed)
    src.close()
    out.save(out_path, garbage=4, deflate=True)
    out.close()
