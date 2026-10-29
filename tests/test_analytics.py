from app.db import get_connection, init_db
from app.services import users
from app.services import transactions as tx
from app.agents.tools import (
    get_largest_purchases,
    get_average_spending,
    get_frequently_purchased_items,
)
from app.agents.tools import get_recurring_expenses

def test_largest_purchases_are_sorted_and_exclude_pending(tmp_path):
    db_path = tmp_path / "analytics.db"
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        user_id = users.create_user(
            conn, "analytics@example.com", "test-hash"
        )

        transaction_ids = []

        for merchant, amount in [
            ("Small Shop", 12000),
            ("Large Shop", 85000),
            ("Medium Shop", 45000),
        ]:
            txn_id = tx.create_transaction(
                conn,
                user_id,
                {
                    "merchant": merchant,
                    "txn_date": "2026-10-12",
                    "total_minor": amount,
                    "currency": "INR",
                    "uncertain_fields": [],
                    "items": [],
                },
            )
            transaction_ids.append(txn_id)

        # Confirm all three transactions.
        for txn_id in transaction_ids:
            tx.confirm_transaction(conn, user_id, txn_id)

        # Leave a larger transaction pending.
        tx.create_transaction(
            conn,
            user_id,
            {
                "merchant": "Pending Shop",
                "txn_date": "2026-10-13",
                "total_minor": 99900,
                "currency": "INR",
                "uncertain_fields": ["merchant"],
                "items": [],
            },
        )
    finally:
        conn.close()

    results = get_largest_purchases(user_id, db_path, limit=2)

    assert [item["merchant"] for item in results] == [
        "Large Shop",
        "Medium Shop",
    ]
    assert [item["amount"] for item in results] == [
        850.0,
        450.0,
    ]
    assert all(item["status"] in {"confirmed", "corrected"} for item in results)

def test_average_spending_excludes_pending_transactions(tmp_path):
    db_path = tmp_path / "average.db"
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        user_id = users.create_user(
            conn, "average@example.com", "test-hash"
        )

        for merchant, amount in [
            ("Shop A", 10000),
            ("Shop B", 30000),
            ("Pending Shop", 50000),
        ]:
            tx_id = tx.create_transaction(
                conn,
                user_id,
                {
                    "merchant": merchant,
                    "txn_date": "2026-10-12",
                    "total_minor": amount,
                    "currency": "INR",
                    "uncertain_fields": [],
                    "items": [],
                },
            )

            if merchant != "Pending Shop":
                tx.confirm_transaction(conn, user_id, tx_id)
    finally:
        conn.close()

    results = get_average_spending(user_id, db_path)

    assert len(results) == 1
    assert results[0]["currency"] == "INR"
    assert results[0]["transaction_count"] == 2
    assert results[0]["total_amount"] == 400.0
    assert results[0]["average_amount"] == 200.0


def test_frequently_purchased_items_counts_distinct_purchases(tmp_path):
    db_path = tmp_path / "frequent_items.db"
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        user_id = users.create_user(
            conn, "items@example.com", "test-hash"
        )

        transaction_ids = []

        for merchant, amount in [
            ("Shop A", 20000),
            ("Shop B", 30000),
        ]:
            tx_id = tx.create_transaction(
                conn,
                user_id,
                {
                    "merchant": merchant,
                    "txn_date": "2026-10-12",
                    "total_minor": amount,
                    "currency": "INR",
                    "uncertain_fields": [],
                    "items": [],
                },
            )
            tx.confirm_transaction(conn, user_id, tx_id)
            transaction_ids.append(tx_id)

        # Milk appears in two separate confirmed purchases.
        for tx_id, quantity, total_minor in [
            (transaction_ids[0], 2, 12000),
            (transaction_ids[1], 1, 6000),
        ]:
            conn.execute(
                """
                INSERT INTO line_items (
                    transaction_id, user_id, name, quantity,
                    unit_price_minor, total_minor, category
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_id,
                    user_id,
                    "Milk 1L",
                    quantity,
                    6000,
                    total_minor,
                    "Groceries",
                ),
            )

        conn.commit()
    finally:
        conn.close()

    results = get_frequently_purchased_items(user_id, db_path)

    assert len(results) == 1
    assert results[0]["item"] == "Milk 1L"
    assert results[0]["purchase_count"] == 2
    assert results[0]["total_quantity"] == 3.0
    assert results[0]["total_amount"] == 180.0

def test_recurring_expenses_groups_repeated_merchants(tmp_path):
    db_path = tmp_path / "recurring.db"
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        user_id = users.create_user(
            conn, "recurring@example.com", "test-hash"
        )

        for txn_date, amount in [
            ("2026-09-10", 25000),
            ("2026-10-10", 30000),
        ]:
            txn_id = tx.create_transaction(
                conn,
                user_id,
                {
                    "merchant": "Monthly Service",
                    "txn_date": txn_date,
                    "total_minor": amount,
                    "currency": "INR",
                    "uncertain_fields": [],
                    "items": [],
                },
            )
            tx.confirm_transaction(conn, user_id, txn_id)
    finally:
        conn.close()

    results = get_recurring_expenses(user_id, db_path)

    assert len(results) == 1
    assert results[0]["merchant"] == "Monthly Service"
    assert results[0]["occurrence_count"] == 2
    assert results[0]["total_amount"] == 550.0
    assert results[0]["average_amount"] == 275.0
    assert results[0]["possible_recurring"] is True