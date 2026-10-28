import json
from types import SimpleNamespace

import pytest

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from app.models.schemas import TransactionIn
from app.services.document_reader import DocumentReadError, read_document
from app.services.extractor import (
    ExtractedReceipt,
    ExtractionError,
    extract_receipt,
    to_minor,
    to_transaction_dict,
)

RECEIPT_TEXT = (
    "DEMO MART\nDate: 2026-10-12\nMilk 1L   2 x 60.00   120.00\n"
    "Bread     1 x 40.00    40.00\nTOTAL 160.00"
)

RECEIPT_JSON = json.dumps({
    "merchant": "Demo Mart", "date": "2026-10-12", "time": None,
    "total": 160.0, "currency": "INR", "tax": None, "discount": None,
    "other_charges": None, "payment_method": None, "invoice_number": None,
    "category": "Groceries",
    "items": [
        {"name": "Milk 1L", "quantity": 2, "unit_price": 60.0, "total_price": 120.0},
        {"name": "Bread", "quantity": 1, "unit_price": 40.0, "total_price": 40.0},
    ],
    "uncertain_fields": [],
})


class FakeModels:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error

    def generate_content(self, **kwargs):
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text)


class FakeClient:
    def __init__(self, text=None, error=None):
        self.models = FakeModels(text, error)


# ---------- extractor ----------

def test_to_minor_converts_exactly():
    assert to_minor(160.0) == 16000
    assert to_minor(19.99) == 1999
    assert to_minor(0.1 + 0.2) == 30
    assert to_minor(None) is None


def test_extract_receipt_parses_response():
    receipt = extract_receipt("some text", client=FakeClient(text=RECEIPT_JSON))
    assert receipt.merchant == "Demo Mart"
    assert len(receipt.items) == 2


def test_extract_receipt_bad_json_raises():
    with pytest.raises(ExtractionError):
        extract_receipt("some text", client=FakeClient(text="this is not json"))


def test_extract_receipt_api_failure_raises():
    with pytest.raises(ExtractionError):
        extract_receipt("some text", client=FakeClient(error=RuntimeError("boom")))


def test_to_transaction_dict_converts_and_validates():
    receipt = ExtractedReceipt.model_validate_json(RECEIPT_JSON)
    data = to_transaction_dict(receipt)
    assert data["total_minor"] == 16000
    assert data["txn_date"] == "2026-10-12"
    assert data["items"][0]["total_minor"] == 12000
    TransactionIn(**data)  # must be accepted by our database model


def test_missing_currency_defaults_and_is_flagged():
    data = to_transaction_dict(ExtractedReceipt(merchant="X", currency=None))
    assert data["currency"] == "INR"
    assert "currency" in data["uncertain_fields"]


def test_bad_date_becomes_none_and_is_flagged():
    data = to_transaction_dict(ExtractedReceipt(merchant="X", date="12/10/2026"))
    assert data["txn_date"] is None
    assert "txn_date" in data["uncertain_fields"]


def test_uncertain_names_are_mapped_to_database_columns():
    receipt = ExtractedReceipt(
        merchant="X", date="2026-10-12", currency="INR",
        uncertain_fields=["date", "total", "merchant"],
    )
    data = to_transaction_dict(receipt)
    assert data["uncertain_fields"] == ["merchant", "total_minor", "txn_date"]


# ---------- document reader ----------

def test_read_text_pdf(tmp_path):
    path = tmp_path / "receipt.pdf"
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), RECEIPT_TEXT, fontsize=14)
    pdf.save(path)
    pdf.close()

    result = read_document(path)
    assert result["method"] == "pdf_text"
    assert result["page_count"] == 1
    assert "TOTAL" in result["text"].upper()


def test_unsupported_file_type_raises(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(DocumentReadError):
        read_document(path)


def test_corrupt_image_raises(tmp_path):
    path = tmp_path / "bad.png"
    path.write_bytes(b"this is not an image")
    with pytest.raises(DocumentReadError):
        read_document(path)