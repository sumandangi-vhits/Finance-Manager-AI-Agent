import pytest
from pydantic import ValidationError

from app.db import get_connection, init_db
from app.services import documents, users
from app.services import transactions as tx

# Synthetic data only: no real receipts
SAMPLE = {
    "merchant": "Reliance Smart",
    "txn_date": "2026-10-12",
    "total_minor": 45000,
    "tax_minor": 2142,
    "payment_method": "UPI",
    "invoice_number": "RS-1001",
    "category": "Groceries",
    "uncertain_fields": ["merchant"],
    "items": [
        {"name": "Milk 1L", "quantity": 2, "unit_price_minor": 6000,
         "total_minor": 12000, "category": "Groceries"},
        {"name": "Detergent", "quantity": 1, "unit_price_minor": 33000,
         "total_minor": 33000, "category": "Household"},
    ],
}


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"  # a throwaway database for each test
    init_db(db_path)
    connection = get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def two_users(conn):
    a = users.create_user(conn, "a@example.com", "hash-a")
    b = users.create_user(conn, "b@example.com", "hash-b")
    return a, b


def test_create_multi_item_transaction(conn, two_users):
    a, _ = two_users
    tid = tx.create_transaction(conn, a, SAMPLE)
    txn = tx.get_transaction(conn, a, tid)
    assert txn["merchant"] == "Reliance Smart"
    assert txn["total_minor"] == 45000
    assert txn["txn_date"] == "2026-10-12"
    assert [i["name"] for i in txn["items"]] == ["Milk 1L", "Detergent"]


def test_new_transaction_starts_pending(conn, two_users):
    a, _ = two_users
    tid = tx.create_transaction(conn, a, SAMPLE)
    txn = tx.get_transaction(conn, a, tid)
    assert txn["status"] == "pending"
    assert txn["uncertain_fields"] == ["merchant"]


def test_missing_fields_stay_null(conn, two_users):
    a, _ = two_users
    tid = tx.create_transaction(conn, a, {"merchant": "Unknown Shop"})
    txn = tx.get_transaction(conn, a, tid)
    assert txn["total_minor"] is None
    assert txn["txn_date"] is None
    assert txn["items"] == []


def test_users_cannot_access_each_others_records(conn, two_users):
    a, b = two_users
    tid = tx.create_transaction(conn, a, SAMPLE)
    assert tx.get_transaction(conn, b, tid) is None
    assert tx.list_transactions(conn, b) == []
    assert tx.update_transaction(conn, b, tid, {"merchant": "Hacked"}) is None
    assert tx.confirm_transaction(conn, b, tid) is False
    assert tx.delete_transaction(conn, b, tid) is False
    assert tx.get_corrections(conn, b, tid) == []
    assert tx.get_transaction(conn, a, tid)["merchant"] == "Reliance Smart"


def test_update_logs_correction_and_marks_corrected(conn, two_users):
    a, _ = two_users
    tid = tx.create_transaction(conn, a, SAMPLE)

    changed = tx.update_transaction(conn, a, tid, {"merchant": "Reliance Smart Store"})
    assert changed == ["merchant"]

    txn = tx.get_transaction(conn, a, tid)
    assert txn["merchant"] == "Reliance Smart Store"
    assert txn["status"] == "corrected"
    assert txn["uncertain_fields"] == []  # the fixed field is no longer uncertain

    log = tx.get_corrections(conn, a, tid)
    assert len(log) == 1
    assert log[0]["field_name"] == "merchant"
    assert log[0]["old_value"] == "Reliance Smart"
    assert log[0]["new_value"] == "Reliance Smart Store"

    # Saving the same value again changes nothing and logs nothing
    assert tx.update_transaction(conn, a, tid, {"merchant": "Reliance Smart Store"}) == []
    assert len(tx.get_corrections(conn, a, tid)) == 1


def test_update_rejects_protected_fields(conn, two_users):
    a, _ = two_users
    tid = tx.create_transaction(conn, a, SAMPLE)
    with pytest.raises(ValueError):
        tx.update_transaction(conn, a, tid, {"status": "confirmed"})
    with pytest.raises(ValueError):
        tx.update_transaction(conn, a, tid, {"user_id": 999})


def test_invalid_data_rejected(conn, two_users):
    a, _ = two_users
    with pytest.raises(ValidationError):
        tx.create_transaction(conn, a, {"total_minor": -5})
    with pytest.raises(ValidationError):
        tx.create_transaction(conn, a, {"items": [{"name": "Bad", "quantity": 0}]})
    assert tx.list_transactions(conn, a) == []  # nothing was saved


def test_confirm_transaction(conn, two_users):
    a, _ = two_users
    tid = tx.create_transaction(conn, a, SAMPLE)
    assert tx.confirm_transaction(conn, a, tid) is True
    txn = tx.get_transaction(conn, a, tid)
    assert txn["status"] == "confirmed"
    assert txn["uncertain_fields"] == []
    assert any(c["field_name"] == "status" for c in tx.get_corrections(conn, a, tid))


def test_delete_cascades(conn, two_users):
    a, _ = two_users
    tid = tx.create_transaction(conn, a, SAMPLE)
    item_id = tx.get_transaction(conn, a, tid)["items"][0]["id"]
    tx.update_line_item(conn, a, item_id, {"quantity": 3})  # creates history

    assert tx.delete_transaction(conn, a, tid) is True
    assert tx.get_transaction(conn, a, tid) is None
    assert conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0
    assert tx.delete_transaction(conn, a, tid) is False  # already gone


def test_list_filters(conn, two_users):
    a, _ = two_users
    tx.create_transaction(conn, a, SAMPLE)
    tx.create_transaction(conn, a, {
        "merchant": "Cafe Coffee Day", "txn_date": "2026-09-30", "total_minor": 25000,
    })

    all_txns = tx.list_transactions(conn, a)
    assert len(all_txns) == 2
    assert all_txns[0]["merchant"] == "Reliance Smart"  # newest first

    assert len(tx.list_transactions(conn, a, merchant="reliance")) == 1
    assert len(tx.list_transactions(conn, a, min_total_minor=30000)) == 1
    assert len(tx.list_transactions(conn, a, start_date="2026-10-01")) == 1
    assert len(tx.list_transactions(conn, a, status="confirmed")) == 0


def test_update_line_item(conn, two_users):
    a, _ = two_users
    tid = tx.create_transaction(conn, a, SAMPLE)
    item_id = tx.get_transaction(conn, a, tid)["items"][0]["id"]

    changed = tx.update_line_item(conn, a, item_id, {"quantity": 3, "total_minor": 18000})
    assert sorted(changed) == ["quantity", "total_minor"]

    txn = tx.get_transaction(conn, a, tid)
    assert txn["status"] == "corrected"
    assert txn["items"][0]["quantity"] == 3
    log = tx.get_corrections(conn, a, tid)
    assert len(log) == 2
    assert all(c["line_item_id"] == item_id for c in log)


def test_cannot_attach_other_users_document(conn, two_users):
    a, b = two_users
    doc_a = documents.create_document(
        conn, a, "bill.pdf", "uploads/1/bill.pdf", "abc123", "application/pdf", 1
    )
    with pytest.raises(ValueError):
        tx.create_transaction(conn, b, SAMPLE, document_id=doc_a)