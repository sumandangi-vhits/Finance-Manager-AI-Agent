
from datetime import date

from app.db import get_connection


def get_spending_summary(user_id, db_path, start_date=None, end_date=None):
    """Calculate spending totals from the authenticated user's transactions."""

    conn = get_connection(db_path)

    try:
        sql = """
            SELECT
                COUNT(*) AS transaction_count,
                COALESCE(SUM(total_minor), 0) AS total_minor
            FROM transactions
            WHERE user_id = ?
              AND status = 'confirmed'
        """
        params = [user_id]

        if start_date:
            sql += " AND txn_date >= ?"
            params.append(start_date)

        if end_date:
            sql += " AND txn_date <= ?"
            params.append(end_date)

        row = conn.execute(sql, params).fetchone()

        return {
            "transaction_count": row["transaction_count"],
            "total_minor": row["total_minor"],
            "total_amount": row["total_minor"] / 100,
            "currency": "INR",
            "start_date": start_date,
            "end_date": end_date,
        }
    finally:
        conn.close()


def get_merchant_transactions(user_id, db_path, merchant):
    """Retrieve transactions for a particular merchant."""

    conn = get_connection(db_path)

    try:
        rows = conn.execute(
            """
            SELECT id, merchant, txn_date, total_minor, currency, status
            FROM transactions
            WHERE user_id = ? AND merchant LIKE ?
            ORDER BY txn_date DESC, id DESC
            LIMIT 100
            """,
            (user_id, f"%{merchant}%"),
        ).fetchall()

        return [
            {
                "id": row["id"],
                "merchant": row["merchant"],
                "date": row["txn_date"],
                "amount": row["total_minor"] / 100,
                "currency": row["currency"],
                "status": row["status"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def get_category_spending(
    user_id,
    db_path,
    start_date=None,
    end_date=None,
):
    """Calculate confirmed spending grouped by expense category."""

    conn = get_connection(db_path)

    try:
        sql = """
            SELECT
                COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized')
                    AS category,
                currency,
                COUNT(*) AS transaction_count,
                SUM(total_minor) AS total_minor
            FROM transactions
            WHERE user_id = ?
              AND status IN ('confirmed', 'corrected')
              AND total_minor IS NOT NULL
        """
        params = [user_id]

        if start_date:
            sql += " AND txn_date >= ?"
            params.append(start_date)

        if end_date:
            sql += " AND txn_date <= ?"
            params.append(end_date)

        sql += """
            GROUP BY COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized'),
                     currency
            ORDER BY total_minor DESC
        """

        rows = conn.execute(sql, params).fetchall()

        return [
            {
                "category": row["category"],
                "currency": row["currency"],
                "transaction_count": row["transaction_count"],
                "total_minor": row["total_minor"],
                "total_amount": row["total_minor"] / 100,
            }
            for row in rows
        ]
    finally:
        conn.close()
