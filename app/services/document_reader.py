from pathlib import Path

try:
    import pymupdf as fitz  # newer PyMuPDF name
except ImportError:
    import fitz  # older name

import pytesseract
from PIL import Image, UnidentifiedImageError

from app.config import Config

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MAX_PAGES = 10
MIN_TEXT_CHARS = 30


class DocumentReadError(Exception):
    """Raised with a user-friendly message when a document cannot be read."""


def _ocr_image(image):
    if Config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = Config.TESSERACT_CMD
    try:
        return pytesseract.image_to_string(image)
    except pytesseract.TesseractNotFoundError as exc:
        raise DocumentReadError(
            "OCR is not available: Tesseract is not installed or was not found."
        ) from exc


def _finish(texts, page_count, method, warnings):
    text = "\n\n".join(t.strip() for t in texts).strip()
    if not text:
        raise DocumentReadError(
            "No readable text was found in this document. Try a clearer photo or scan."
        )
    return {
        "text": text,
        "page_count": page_count,
        "method": method,
        "warnings": warnings,
    }


def _read_pdf(path):
    try:
        pdf = fitz.open(path)
    except Exception as exc:
        raise DocumentReadError(
            "This PDF could not be opened. It may be corrupted."
        ) from exc

    try:
        if pdf.needs_pass:
            raise DocumentReadError("This PDF is password-protected.")

        page_count = pdf.page_count
        pages_to_read = min(page_count, MAX_PAGES)
        warnings = []
        if page_count > MAX_PAGES:
            warnings.append(
                f"Only the first {MAX_PAGES} of {page_count} pages were read."
            )

        texts = [pdf[i].get_text() for i in range(pages_to_read)]
        method = "pdf_text"

        if len("".join(texts).strip()) < MIN_TEXT_CHARS:
            method = "ocr"
            texts = []
            for i in range(pages_to_read):
                pix = pdf[i].get_pixmap(dpi=200)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                texts.append(_ocr_image(image))
    finally:
        pdf.close()

    return _finish(texts, page_count, method, warnings)


def _read_image(path):
    try:
        with Image.open(path) as img:
            image = img.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise DocumentReadError(
            "This image could not be read. It may be corrupted."
        ) from exc
    return _finish([_ocr_image(image)], 1, "ocr", [])


def read_document(path):
    """Return {"text", "page_count", "method", "warnings"} or raise DocumentReadError."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in IMAGE_EXTENSIONS:
        return _read_image(path)
    raise DocumentReadError(
        f"Unsupported file type '{suffix}'. Upload a PDF or an image (PNG, JPG, WEBP)."
    )