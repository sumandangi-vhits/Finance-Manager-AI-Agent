import pytest

from app import create_app
from app.db import get_connection, init_db
from app.services import users
from app.services import transactions as tx


@pytest.fixture
def security_setup(tmp_path):
    db_path = tmp_path / "security_test.db"
    init_db(db_path)

    app = create_app()
    app.config.update(
        TESTING=True,
        SECRET_KEY="security-test-secret",
        DATABASE_PATH=db_path,
        UPLOAD_FOLDER=tmp_path / "uploads",
    )

    conn = get_connection(db_path)
    try:
        user_a = users.create_user(
            conn, "security-a@example.com", "hash-a"
        )
        user_b = users.create_user(
            conn, "security-b@example.com", "hash-b"
        )

        transaction_id = tx.create_transaction(
            conn,
            user_a,
            {
                "merchant": "Private Test Shop",
                "txn_date": "2026-10-29",
                "total_minor": 25000,
                "currency": "INR",
                "uncertain_fields": [],
                "items": [
                    {
                        "name": "Test Item",
                        "quantity": 1,
                        "unit_price_minor": 25000,
                        "total_minor": 25000,
                    }
                ],
            },
        )
        tx.confirm_transaction(conn, user_a, transaction_id)

        item_id = tx.get_transaction(
            conn, user_a, transaction_id
        )["items"][0]["id"]
    finally:
        conn.close()

    return app, user_a, user_b, transaction_id, item_id


def login_as(client, user_id):
    with client.session_transaction() as session:
        session["user_id"] = user_id


def test_other_user_cannot_view_transaction(security_setup):
    app, _, user_b, transaction_id, _ = security_setup
    client = app.test_client()
    login_as(client, user_b)

    response = client.get(f"/transactions/{transaction_id}")

    assert response.status_code == 404


def test_other_user_cannot_edit_transaction(security_setup):
    app, _, user_b, transaction_id, _ = security_setup
    client = app.test_client()
    login_as(client, user_b)

    response = client.post(
        f"/transactions/{transaction_id}/edit",
        data={
            "merchant": "Hacked Shop",
            "txn_date": "2026-10-29",
            "currency": "INR",
            "total": "9999",
        },
    )

    assert response.status_code == 404

    conn = get_connection(app.config["DATABASE_PATH"])
    try:
        transaction = tx.get_transaction(
            conn, security_setup[1], transaction_id
        )
        assert transaction["merchant"] == "Private Test Shop"
        assert transaction["total_minor"] == 25000
    finally:
        conn.close()


def test_other_user_cannot_confirm_transaction(security_setup):
    app, _, user_b, transaction_id, _ = security_setup
    client = app.test_client()
    login_as(client, user_b)

    response = client.post(
        f"/transactions/{transaction_id}/confirm"
    )

    assert response.status_code == 404


def test_other_user_cannot_edit_line_item(security_setup):
    app, _, user_b, transaction_id, item_id = security_setup
    client = app.test_client()
    login_as(client, user_b)

    response = client.post(
        f"/transactions/{transaction_id}/items/{item_id}/edit",
        data={
            "name": "Hacked Item",
            "quantity": "1",
            "unit_price": "9999",
            "item_total": "9999",
        },
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/transactions",
        "/transactions/export",
        "/transactions/1",
        "/ask-finance",
    ],
)
def test_protected_routes_require_login(security_setup, path):
    app, *_ = security_setup
    response = app.test_client().get(path)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
