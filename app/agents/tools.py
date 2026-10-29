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

def get_largest_purchases(
    user_id,
    db_path,
    limit=5,
    start_date=None,
    end_date=None,
):
    """Return the largest recorded transactions for the current user."""

    limit = max(1, min(int(limit), 100))
    conn = get_connection(db_path)

    try:
        sql = """
            SELECT
                id,
                merchant,
                txn_date,
                total_minor,
                currency,
                category,
                status
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
            ORDER BY total_minor DESC, txn_date DESC, id DESC
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

        return [
            {
                "id": row["id"],
                "merchant": row["merchant"],
                "date": row["txn_date"],
                "amount": row["total_minor"] / 100,
                "total_minor": row["total_minor"],
                "currency": row["currency"],
                "category": row["category"],
                "status": row["status"],
            }
            for row in rows
        ]
    finally:
        conn.close()



def get_average_spending(
    user_id,
    db_path,
    start_date=None,
    end_date=None,
):
    """Calculate average spending per transaction, separately by currency."""

    conn = get_connection(db_path)

    try:
        sql = """
            SELECT
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
            GROUP BY currency
            ORDER BY total_minor DESC
        """

        rows = conn.execute(sql, params).fetchall()

        return [
            {
                "currency": row["currency"],
                "transaction_count": row["transaction_count"],
                "total_minor": row["total_minor"],
                "total_amount": row["total_minor"] / 100,
                "average_amount": (
                    row["total_minor"] / row["transaction_count"] / 100
                ),
                "start_date": start_date,
                "end_date": end_date,
            }
            for row in rows
        ]

    finally:
        conn.close()


def get_frequently_purchased_items(
    user_id,
    db_path,
    limit=5,
    start_date=None,
    end_date=None,
):
    """Return frequently purchased items from confirmed transactions."""

    limit = max(1, min(int(limit), 100))
    conn = get_connection(db_path)

    try:
        sql = """
            SELECT
                li.name,
                li.category,
                t.currency,
                COUNT(DISTINCT t.id) AS purchase_count,
                SUM(li.quantity) AS total_quantity,
                SUM(li.total_minor) AS total_minor
            FROM line_items AS li
            JOIN transactions AS t
                ON t.id = li.transaction_id
            WHERE t.user_id = ?
              AND li.user_id = ?
              AND t.status IN ('confirmed', 'corrected')
              AND li.total_minor IS NOT NULL
        """
        params = [user_id, user_id]

        if start_date:
            sql += " AND t.txn_date >= ?"
            params.append(start_date)

        if end_date:
            sql += " AND t.txn_date <= ?"
            params.append(end_date)

        sql += """
            GROUP BY LOWER(TRIM(li.name)), li.category, t.currency
            ORDER BY purchase_count DESC, total_minor DESC
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

        return [
            {
                "item": row["name"],
                "category": row["category"],
                "currency": row["currency"],
                "purchase_count": row["purchase_count"],
                "total_quantity": row["total_quantity"],
                "total_amount": row["total_minor"] / 100,
            }
            for row in rows
        ]

    finally:
        conn.close()

def get_recurring_expenses(
    user_id,
    db_path,
    start_date=None,
    end_date=None,
    min_occurrences=2,
):
    """Identify merchants with repeated confirmed/corrected transactions."""

    min_occurrences = max(2, min(int(min_occurrences), 100))
    conn = get_connection(db_path)

    try:
        sql = """
            SELECT
                merchant,
                currency,
                COUNT(*) AS occurrence_count,
                SUM(total_minor) AS total_minor,
                AVG(total_minor) AS average_minor,
                MIN(txn_date) AS first_date,
                MAX(txn_date) AS last_date
            FROM transactions
            WHERE user_id = ?
              AND status IN ('confirmed', 'corrected')
              AND total_minor IS NOT NULL
              AND merchant IS NOT NULL
              AND TRIM(merchant) != ''
        """
        params = [user_id]

        if start_date:
            sql += " AND txn_date >= ?"
            params.append(start_date)

        if end_date:
            sql += " AND txn_date <= ?"
            params.append(end_date)

        sql += """
            GROUP BY LOWER(TRIM(merchant)), currency
            HAVING COUNT(*) >= ?
            ORDER BY occurrence_count DESC, total_minor DESC
        """
        params.append(min_occurrences)

        rows = conn.execute(sql, params).fetchall()

        return [
            {
                "merchant": row["merchant"],
                "currency": row["currency"],
                "occurrence_count": row["occurrence_count"],
                "total_amount": row["total_minor"] / 100,
                "average_amount": row["average_minor"] / 100,
                "first_date": row["first_date"],
                "last_date": row["last_date"],
                "possible_recurring": True,
            }
            for row in rows
        ]

    finally:
        conn.close()
