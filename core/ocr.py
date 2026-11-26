"""Turn an image or PDF into plain text, plus quality signals for validation.

OCR model: PP-OCRv5 mobile (detection + English recognition), run on CPU through
RapidOCR/ONNX Runtime. Pip-installable, no GPU, no system packages.
PDFs with a text layer skip OCR entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_PDF_PAGES = 3
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass
class OcrResult:
    text: str  # reading order: lines top-to-bottom, words left-to-right
    confidence: float  # mean recognition score 0-1 (1.0 for a PDF text layer)
    blur_score: float | None  # variance of the Laplacian; low = blurry (None for text PDFs)
    source: str  # "image", "pdf-text" or "pdf-scan"


@lru_cache(maxsize=1)
def _engine():
    """Load the OCR models once per process (first call downloads them, ~15 MB)."""
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    return RapidOCR(
        params={
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.MOBILE,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "Rec.lang_type": LangRec.EN,
            "Global.log_level": "warning",
        }
    )


def read_document(path: str | Path) -> OcrResult:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix not in IMAGE_TYPES:
        raise ValueError(f"Unsupported file type '{suffix}'. Use an image or a PDF.")
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")  # honour phone rotation
    return ocr_image(image)


def ocr_image(image: Image.Image, source: str = "image") -> OcrResult:
    pixels = np.array(image)
    out = _engine()(pixels)
    if _is_sideways(out):
        # The page is rotated 90°: try both directions and keep the more confident read.
        # (PP-OCR's own classifier only fixes upside-down text, not sideways pages.)
        tries = [_engine()(np.array(image.rotate(a, expand=True))) for a in (90, 270)]
        out = max(tries, key=lambda o: sum(o.scores) if o.scores else 0)
    if out.boxes is None or not out.txts:
        return OcrResult(text="", confidence=0.0, blur_score=blur_score(pixels), source=source)
    return OcrResult(
        text=_to_lines(out.boxes, out.txts),
        confidence=float(np.mean(out.scores)),
        blur_score=blur_score(pixels),
        source=source,
    )


def _is_sideways(out) -> bool:
    """Printed text lines are wider than tall. If most detected boxes are taller than wide,
    the page is lying on its side."""
    if out.boxes is None or len(out.boxes) < 3:
        return False
    tall = 0
    for box in out.boxes:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        tall += (max(ys) - min(ys)) > (max(xs) - min(xs))
    return tall > len(out.boxes) / 2


def blur_score(pixels: np.ndarray) -> float:
    """Sharpness = variance of the Laplacian on a fixed-width greyscale copy."""
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    scale = 800 / gray.shape[1]  # normalise size so the score is comparable between images
    gray = cv2.resize(gray, None, fx=scale, fy=scale)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _to_lines(boxes, texts) -> str:
    """Group OCR boxes into printed lines, so "TOTAL" and "9.50" end up on the same line."""
    items = []
    for box, text in zip(boxes, texts, strict=True):
        ys, xs = [p[1] for p in box], [p[0] for p in box]
        items.append((min(ys), max(ys), min(xs), text))
    items.sort(key=lambda it: (it[0] + it[1]) / 2)

    lines: list[dict] = []
    for top, bottom, left, text in items:
        center = (top + bottom) / 2
        if lines and abs(center - lines[-1]["center"]) < 0.5 * (bottom - top):
            lines[-1]["words"].append((left, text))
        else:
            lines.append({"center": center, "words": [(left, text)]})
    return "\n".join("  ".join(t for _, t in sorted(line["words"])) for line in lines)


def _read_pdf(path: Path) -> OcrResult:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        pages = [pdf[i] for i in range(min(len(pdf), MAX_PDF_PAGES))]
        text = "\n".join(p.get_textpage().get_text_range() for p in pages).strip()
        if len(text) > 20:  # digital PDF: trust the text layer, no OCR needed
            return OcrResult(text=text, confidence=1.0, blur_score=None, source="pdf-text")
        images = [p.render(scale=2).to_pil().convert("RGB") for p in pages]
    finally:
        pdf.close()

    results = [ocr_image(img, source="pdf-scan") for img in images]
    return OcrResult(
        text="\n".join(r.text for r in results),
        confidence=min(r.confidence for r in results),
        blur_score=min(r.blur_score or 0.0 for r in results),
        source="pdf-scan",
    )
