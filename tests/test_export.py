import csv
import io

import pytest

from app import create_app
from app.db import get_connection, init_db
from app.services import users
from app.services import transactions as tx


@pytest.fixture
def export_app(tmp_path):
    db_path = tmp_path / "export_test.db"
    init_db(db_path)

    app = create_app()
    app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        DATABASE_PATH=db_path,
        UPLOAD_FOLDER=tmp_path / "uploads",
    )

    conn = get_connection(db_path)
    try:
        user_a = users.create_user(
            conn, "export-a@example.com", "hash-a"
        )
        user_b = users.create_user(
            conn, "export-b@example.com", "hash-b"
        )

        transaction_a = tx.create_transaction(
            conn,
            user_a,
            {
                "merchant": "Test Grocery",
                "txn_date": "2026-10-12",
                "txn_time": "10:30",
                "total_minor": 12550,
                "currency": "INR",
                "category": "Groceries",
                "payment_method": "UPI",
                "invoice_number": "INV-A",
                "uncertain_fields": [],
                "items": [],
            },
        )
        tx.confirm_transaction(conn, user_a, transaction_a)

        tx.create_transaction(
            conn,
            user_b,
            {
                "merchant": "Private Shop",
                "txn_date": "2026-10-13",
                "total_minor": 99900,
                "currency": "INR",
                "category": "Shopping",
                "uncertain_fields": [],
                "items": [],
            },
        )
    finally:
        conn.close()

    app.config["TEST_USER_ID"] = user_a
    return app


@pytest.fixture
def authenticated_client(export_app):
    client = export_app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = export_app.config["TEST_USER_ID"]
    return client


def test_export_requires_login(export_app):
    response = export_app.test_client().get("/transactions/export")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_export_returns_csv_headers_and_amount(authenticated_client):
    response = authenticated_client.get("/transactions/export")

    assert response.status_code == 200
    assert "text/csv" in response.content_type
    assert "attachment;" in response.headers["Content-Disposition"]

    content = response.get_data(as_text=True).removeprefix("\ufeff")
    rows = list(csv.DictReader(io.StringIO(content)))

    assert rows
    assert rows[0]["Merchant"] == "Test Grocery"
    assert rows[0]["Amount"] == "125.50"
    assert rows[0]["Currency"] == "INR"
    assert rows[0]["Category"] == "Groceries"
    assert rows[0]["Payment Method"] == "UPI"
    assert rows[0]["Invoice Number"] == "INV-A"
    assert rows[0]["Status"] == "confirmed"


def test_export_does_not_include_another_users_transactions(
    authenticated_client,
):
    response = authenticated_client.get("/transactions/export")
    content = response.get_data(as_text=True)

    assert "Test Grocery" in content
    assert "Private Shop" not in content
    assert "INV-A" in content


def test_export_includes_expected_column_headers(authenticated_client):
    response = authenticated_client.get("/transactions/export")
    content = response.get_data(as_text=True).removeprefix("\ufeff")
    reader = csv.reader(io.StringIO(content))
    headers = next(reader)

    assert headers == [
        "Transaction ID",
        "Merchant",
        "Date",
        "Time",
        "Amount",
        "Currency",
        "Category",
        "Payment Method",
        "Invoice Number",
        "Status",
    ]
