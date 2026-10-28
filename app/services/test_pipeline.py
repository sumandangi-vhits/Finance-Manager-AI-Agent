import json
from datetime import date
from types import SimpleNamespace
from app.services.validator import validate_transaction
import pytest

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from app.db import get_connection, init_db
from app.services import documents, users
from app.services import transactions as tx
from app.services.document_reader import DocumentReadError
from app.services.extractor import ExtractionError
from app.services.pipeline import ProcessingError, process_document
from app.services.validator import validate_transaction

RECEIPT_TEXT = (
    "DEMO MART\nDate: 2026-10-12\nMilk 1L   2 x 60.00   120.00\n"
    "Bread     1 x 40.00    40.00\nTOTAL 160.00"
)


def receipt_json(**overrides):
    base = {
        "merchant": "Demo Mart", "date": "2026-10-12", "time": None,
        "total": 160.0, "currency": "INR", "tax": None, "discount": None,
        "other_charges": None, "payment_method": None, "invoice_number": None,
        "category": "Groceries",
        "items": [
            {"name": "Milk 1L", "quantity": 2, "unit_price": 60.0, "total_price": 120.0},
            {"name": "Bread", "quantity": 1, "unit_price": 40.0, "total_price": 40.0},
        ],
        "uncertain_fields": [],
    }
    base.update(overrides)
    return json.dumps(base)


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


def make_pdf(path, text=RECEIPT_TEXT):
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), text, fontsize=14)
    pdf.save(path)
    pdf.close()
    return path


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    user_a = users.create_user(conn, "a@example.com", "x")
    user_b = users.create_user(conn, "b@example.com", "x")
    yield SimpleNamespace(
        conn=conn, a=user_a, b=user_b, root=tmp_path / "uploads", tmp=tmp_path
    )
    conn.close()


def run(env, user, path, client, **kwargs):
    return process_document(
        env.conn, user, path, path.name,
        client=client, upload_root=env.root, **kwargs,
    )


def count(env, table):
    return env.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_clean_receipt_is_saved_as_pending(env):
    pdf = make_pdf(env.tmp / "r.pdf")
    result = run(env, env.a, pdf, FakeClient(receipt_json()))

    assert result["status"] == "saved"
    assert result["issues"] == []
    assert result["needs_clarification"] is False

    txn = tx.get_transaction(env.conn, env.a, result["transaction_id"])
    assert txn["status"] == "pending"
    assert txn["total_minor"] == 16000
    assert len(txn["items"]) == 2

    doc = documents.get_document(env.conn, env.a, result["document_id"])
    assert doc["mime_type"] == "application/pdf"
    assert (env.root / doc["stored_path"]).exists()  # original is preserved


def test_same_file_is_detected_and_can_be_allowed(env):
    pdf = make_pdf(env.tmp / "r.pdf")
    run(env, env.a, pdf, FakeClient(receipt_json()))

    second = run(env, env.a, pdf, FakeClient(receipt_json()))
    assert second["status"] == "duplicate_file"
    assert len(second["existing"]) == 1
    assert count(env, "transactions") == 1  # nothing new was saved

    forced = run(env, env.a, pdf, FakeClient(receipt_json()), allow_duplicate=True)
    assert forced["status"] == "saved"
    assert "possible_duplicate" in [i["code"] for i in forced["issues"]]
    assert count(env, "transactions") == 2


def test_total_mismatch_is_flagged(env):
    pdf = make_pdf(env.tmp / "r.pdf")
    result = run(env, env.a, pdf, FakeClient(receipt_json(total=200.0)))

    assert "total_mismatch" in [i["code"] for i in result["issues"]]
    assert result["needs_clarification"] is True
    txn = tx.get_transaction(env.conn, env.a, result["transaction_id"])
    assert "total_minor" in txn["uncertain_fields"]
    assert txn["status"] == "pending"


def test_item_math_error_is_flagged(env):
    items = [
        {"name": "Milk 1L", "quantity": 2, "unit_price": 60.0, "total_price": 100.0},
        {"name": "Bread", "quantity": 1, "unit_price": 40.0, "total_price": 40.0},
    ]
    pdf = make_pdf(env.tmp / "r.pdf")
    result = run(env, env.a, pdf, FakeClient(receipt_json(items=items, total=140.0)))

    codes = [i["code"] for i in result["issues"]]
    assert "item_math" in codes
    assert "total_mismatch" not in codes


def test_missing_merchant_and_date_stay_empty_and_are_flagged(env):
    pdf = make_pdf(env.tmp / "r.pdf")
    result = run(env, env.a, pdf, FakeClient(receipt_json(merchant=None, date=None)))

    fields = {i["field"] for i in result["issues"]}
    assert {"merchant", "txn_date"} <= fields
    txn = tx.get_transaction(env.conn, env.a, result["transaction_id"])
    assert txn["merchant"] is None   # never invented
    assert txn["txn_date"] is None
    assert "merchant" in txn["uncertain_fields"]


def test_unreadable_document_saves_nothing(env):
    bad = env.tmp / "bad.png"
    bad.write_bytes(b"this is not an image")
    with pytest.raises(DocumentReadError):
        run(env, env.a, bad, FakeClient(receipt_json()))
    assert count(env, "documents") == 0
    assert count(env, "transactions") == 0
    assert not env.root.exists()


def test_ai_failure_saves_nothing(env):
    pdf = make_pdf(env.tmp / "r.pdf")
    with pytest.raises(ExtractionError):
        run(env, env.a, pdf, FakeClient(error=RuntimeError("boom")))
    assert count(env, "documents") == 0
    assert count(env, "transactions") == 0
    assert not env.root.exists()


def test_invalid_extracted_values_are_rejected(env):
    pdf = make_pdf(env.tmp / "r.pdf")
    with pytest.raises(ProcessingError):
        run(env, env.a, pdf, FakeClient(receipt_json(total=-5.0)))
    assert count(env, "transactions") == 0


def test_other_users_uploads_are_not_duplicates(env):
    pdf = make_pdf(env.tmp / "r.pdf")
    run(env, env.a, pdf, FakeClient(receipt_json()))
    result = run(env, env.b, pdf, FakeClient(receipt_json()))
    assert result["status"] == "saved"
    assert result["issues"] == []  # user A's records are invisible to user B


def test_repeat_purchase_is_flagged_but_not_rejected(env):
    first = make_pdf(env.tmp / "one.pdf")
    second = make_pdf(env.tmp / "two.pdf", RECEIPT_TEXT + "\nThank you")  # different file
    run(env, env.a, first, FakeClient(receipt_json()))
    result = run(env, env.a, second, FakeClient(receipt_json()))

    assert result["status"] == "saved"
    assert "possible_duplicate" in [i["code"] for i in result["issues"]]
    assert count(env, "transactions") == 2


# ---------- validator ----------

def base_data(**overrides):
    data = {
        "merchant": "Demo Mart", "txn_date": "2026-10-12", "total_minor": 16000,
        "currency": "INR", "uncertain_fields": [],
        "items": [{"name": "A", "quantity": 1, "unit_price_minor": 16000,
                   "total_minor": 16000}],
    }
    data.update(overrides)
    return data


def test_future_date_is_flagged():
    issues = validate_transaction(base_data(txn_date="2027-01-01"), today=date(2026, 10, 27))
    assert [i["code"] for i in issues] == ["future_date"]


def test_small_rounding_difference_is_accepted():
    assert validate_transaction(base_data(total_minor=16050)) == []


def test_tax_added_on_top_is_accepted():
    data = base_data(tax_minor=800, total_minor=16800)
    assert validate_transaction(data) == []


def test_currency_question_mentions_the_assumption():
    issues = validate_transaction(base_data(uncertain_fields=["currency"]))
    assert len(issues) == 1
    assert "INR" in issues[0]["message"]


def test_clean_transaction_can_be_confirmed(env):
    data = {
        "merchant": "Demo Mart",
        "txn_date": "2026-10-12",
        "total_minor": 16000,
        "currency": "INR",
        "uncertain_fields": [],
        "items": [
            {
                "name": "Milk",
                "quantity": 1,
                "unit_price_minor": 16000,
                "total_minor": 16000,
            }
        ],
    }
    txn_id = tx.create_transaction(env.conn, env.a, data)

    assert tx.confirm_transaction(env.conn, env.a, txn_id) is True

    txn = tx.get_transaction(env.conn, env.a, txn_id)
    assert txn["status"] == "confirmed"


def test_confirmation_does_not_erase_unresolved_uncertainty(env):
    data = {
        "merchant": "Demo Mart",
        "txn_date": "2026-10-12",
        "total_minor": 16000,
        "currency": "INR",
        "uncertain_fields": ["currency"],
        "items": [],
    }
    txn_id = tx.create_transaction(env.conn, env.a, data)

    tx.confirm_transaction(env.conn, env.a, txn_id)

    txn = tx.get_transaction(env.conn, env.a, txn_id)
    assert "currency" in txn["uncertain_fields"]


def test_confirmation_rejects_item_calculation_mismatch(env):
    data = {
        "merchant": "Demo Mart",
        "txn_date": "2026-10-12",
        "total_minor": 10000,
        "currency": "INR",
        "uncertain_fields": [],
        "items": [
            {
                "name": "Milk",
                "quantity": 2,
                "unit_price_minor": 6000,
                "total_minor": 10000,
            }
        ],
    }
    txn_id = tx.create_transaction(env.conn, env.a, data)

    with pytest.raises(ValueError, match="calculation"):
        tx.confirm_transaction(env.conn, env.a, txn_id)

    txn = tx.get_transaction(env.conn, env.a, txn_id)
    assert txn["status"] == "pending"


def test_editing_line_item_preserves_transaction_uncertainty(env):
    data = {
        "merchant": "Demo Mart",
        "txn_date": "2026-10-12",
        "total_minor": 16000,
        "currency": "INR",
        "uncertain_fields": ["currency"],
        "items": [
            {
                "name": "Milk 1L",
                "quantity": 2,
                "unit_price_minor": 6000,
                "total_minor": 12000,
            },
        ],
    }
    txn_id = tx.create_transaction(env.conn, env.a, data)
    txn = tx.get_transaction(env.conn, env.a, txn_id)
    item_id = txn["items"][0]["id"]

    tx.update_line_item(
        env.conn,
        env.a,
        item_id,
        {"name": "Milk 1L"},
    )

    updated = tx.get_transaction(env.conn, env.a, txn_id)
    assert "currency" in updated["uncertain_fields"]



def test_editing_transaction_field_clears_only_that_uncertainty(env):
    data = {
        "merchant": "Demo Mart",
        "txn_date": "2026-10-12",
        "total_minor": 16000,
        "currency": "INR",
        "uncertain_fields": ["merchant", "currency"],
        "items": [],
    }
    txn_id = tx.create_transaction(env.conn, env.a, data)

    tx.update_transaction(
        env.conn,
        env.a,
        txn_id,
        {"merchant": "Demo Super Mart"},
    )

    updated = tx.get_transaction(env.conn, env.a, txn_id)
    assert "merchant" not in updated["uncertain_fields"]
    assert "currency" in updated["uncertain_fields"]

def test_missing_merchant_and_date_are_detected_again_on_review(env):
    pdf = make_pdf(env.tmp / "missing-fields.pdf")
    result = run(
        env,
        env.a,
        pdf,
        FakeClient(receipt_json(merchant=None, date=None)),
    )

    txn = tx.get_transaction(env.conn, env.a, result["transaction_id"])
    review_issues = validate_transaction(txn)
    review_fields = {issue["field"] for issue in review_issues}

    assert {"merchant", "txn_date"} <= review_fields


def test_create_clarification_starts_pending(env):
    txn_id = tx.create_transaction(
        env.conn,
        env.a,
        {
            "merchant": "Demo Mart",
            "txn_date": "2026-10-12",
            "total_minor": 16000,
            "currency": "INR",
        },
    )

    clarification_id = tx.create_clarification(
        env.conn, env.a, txn_id,
        "txn_date", "What is the transaction date?"
    )

    records = tx.get_clarifications(env.conn, env.a, txn_id)

    assert len(records) == 1
    assert records[0]["id"] == clarification_id
    assert records[0]["status"] == "pending"
    assert records[0]["answer"] is None


def test_create_clarification_does_not_duplicate_pending_question(env):
    txn_id = tx.create_transaction(
        env.conn, env.a,
        {"merchant": "Demo Mart", "currency": "INR"},
    )

    first_id = tx.create_clarification(
        env.conn, env.a, txn_id,
        "txn_date", "What is the transaction date?"
    )
    second_id = tx.create_clarification(
        env.conn, env.a, txn_id,
        "txn_date", "Please provide the transaction date."
    )

    records = tx.get_clarifications(env.conn, env.a, txn_id)

    assert first_id == second_id
    assert len(records) == 1


def test_answer_clarification_saves_answer(env):
    txn_id = tx.create_transaction(
        env.conn, env.a,
        {"merchant": "Demo Mart", "currency": "INR"},
    )
    clarification_id = tx.create_clarification(
        env.conn, env.a, txn_id,
        "txn_date", "What is the transaction date?"
    )

    result = tx.answer_clarification(
        env.conn, env.a, clarification_id, "2026-10-20"
    )

    records = tx.get_clarifications(env.conn, env.a, txn_id)
    transaction = tx.get_transaction(env.conn, env.a, txn_id)

    assert result is True
    assert records[0]["status"] == "answered"
    assert records[0]["answer"] == "2026-10-20"
    assert str(transaction["txn_date"]) == "2026-10-20"
    assert "txn_date" not in transaction["uncertain_fields"]


def test_unknown_clarification_has_no_answer(env):
    txn_id = tx.create_transaction(
        env.conn, env.a,
        {"merchant": "Demo Mart", "currency": "INR"},
    )
    clarification_id = tx.create_clarification(
        env.conn, env.a, txn_id,
        "txn_date", "What is the transaction date?"
    )

    result = tx.answer_clarification(
        env.conn, env.a, clarification_id, None, status="unknown"
    )

    records = tx.get_clarifications(env.conn, env.a, txn_id)

    assert result is True
    assert records[0]["status"] == "unknown"
    assert records[0]["answer"] is None


def test_user_cannot_answer_another_users_clarification(env):
    txn_id = tx.create_transaction(
        env.conn, env.a,
        {"merchant": "Demo Mart", "currency": "INR"},
    )
    clarification_id = tx.create_clarification(
        env.conn, env.a, txn_id,
        "txn_date", "What is the transaction date?"
    )

    result = tx.answer_clarification(
        env.conn, env.b, clarification_id, "2026-10-20"
    )

    records = tx.get_clarifications(env.conn, env.a, txn_id)
    transaction = tx.get_transaction(env.conn, env.a, txn_id)
    assert result is False
    assert records[0]["status"] == "pending"
    assert records[0]["answer"] is None
