import json
from datetime import date

from app.models.schemas import LineItemIn, TransactionIn
from app.services.validator import validate_transaction

# Only these columns may be edited. Anything else (user_id, status, ...) is refused.
EDITABLE_FIELDS = {
    "merchant",
    "txn_date",
    "txn_time",
    "total_minor",
    "currency",
    "tax_minor",
    "discount_minor",
    "other_charges_minor",
    "payment_method",
    "invoice_number",
    "category",
}

ITEM_EDITABLE_FIELDS = {
    "name",
    "quantity",
    "unit_price_minor",
    "total_minor",
    "category",
}

def _db_value(value):
    """Convert Python values into something SQLite can store."""
    if isinstance(value, date):
        return value.isoformat()
    return value


def _row_to_transaction(row):
    txn = dict(row)
    txn["uncertain_fields"] = json.loads(txn["uncertain_fields"] or "[]")
    return txn


def _log_correction(
    conn,
    user_id,
    transaction_id,
    field_name,
    old,
    new,
    line_item_id=None,
):
    conn.execute(
        """INSERT INTO corrections
           (user_id, transaction_id, line_item_id, field_name, old_value, new_value)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            transaction_id,
            line_item_id,
            field_name,
            None if old is None else str(old),
            None if new is None else str(new),
        ),
    )

def create_transaction(conn, user_id, data, document_id=None):
    """Validate and save a transaction with its line items.
    New records always start as 'pending'. Returns the new transaction id."""
    txn = TransactionIn(**data)

    if document_id is not None:
        owned = conn.execute(
            "SELECT 1 FROM documents WHERE id = ? AND user_id = ?",
            (document_id, user_id),
        ).fetchone()

        if owned is None:
            raise ValueError("Document not found for this user.")

    with conn:
        cur = conn.execute(
            """INSERT INTO transactions
               (user_id, document_id, merchant, txn_date, txn_time, total_minor,
                currency, tax_minor, discount_minor, other_charges_minor,
                payment_method, invoice_number, category, status, uncertain_fields)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                user_id,
                document_id,
                txn.merchant,
                _db_value(txn.txn_date),
                txn.txn_time,
                txn.total_minor,
                txn.currency,
                txn.tax_minor,
                txn.discount_minor,
                txn.other_charges_minor,
                txn.payment_method,
                txn.invoice_number,
                txn.category,
                json.dumps(txn.uncertain_fields),
            ),
        )

        txn_id = cur.lastrowid

        for item in txn.items:
            conn.execute(
                """INSERT INTO line_items
                   (transaction_id, user_id, name, quantity, unit_price_minor,
                    total_minor, category)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    txn_id,
                    user_id,
                    item.name,
                    item.quantity,
                    item.unit_price_minor,
                    item.total_minor,
                    item.category,
                ),
            )

    return txn_id


# ---------- read ----------

def get_transaction(conn, user_id, txn_id):
    """Return one transaction with its items, or None if it is not this user's."""
    row = conn.execute(
        "SELECT * FROM transactions WHERE id = ? AND user_id = ?",
        (txn_id, user_id),
    ).fetchone()

    if row is None:
        return None

    txn = _row_to_transaction(row)

    items = conn.execute(
        """SELECT * FROM line_items
           WHERE transaction_id = ? AND user_id = ?
           ORDER BY id""",
        (txn_id, user_id),
    ).fetchall()

    txn["items"] = [dict(item) for item in items]
    return txn


def list_transactions(
    conn,
    user_id,
    status=None,
    merchant=None,
    start_date=None,
    end_date=None,
    min_total_minor=None,
    limit=100,
):
    """List a user's transactions (without items), newest first."""
    sql = "SELECT * FROM transactions WHERE user_id = ?"
    params = [user_id]

    if status:
        sql += " AND status = ?"
        params.append(status)

    if merchant:
        sql += " AND merchant LIKE ?"
        params.append(f"%{merchant}%")

    if start_date:
        sql += " AND txn_date >= ?"
        params.append(_db_value(start_date))

    if end_date:
        sql += " AND txn_date <= ?"
        params.append(_db_value(end_date))

    if min_total_minor is not None:
        sql += " AND total_minor >= ?"
        params.append(min_total_minor)

    sql += " ORDER BY txn_date DESC, id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_transaction(row) for row in rows]


def get_corrections(conn, user_id, txn_id):
    rows = conn.execute(
        """SELECT * FROM corrections
           WHERE transaction_id = ? AND user_id = ?
           ORDER BY id""",
        (txn_id, user_id),
    ).fetchall()

    return [dict(row) for row in rows]


# ---------- clarification management ----------

def create_clarification(conn, user_id, txn_id, field_name, question):
    """Create a pending clarification unless one already exists."""
    transaction = get_transaction(conn, user_id, txn_id)

    if transaction is None:
        raise ValueError("Transaction not found for this user.")

    with conn:
        existing = conn.execute(
            """
            SELECT id FROM clarifications
            WHERE user_id = ? AND transaction_id = ?
              AND field_name = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, txn_id, field_name),
        ).fetchone()

        if existing:
            return existing["id"]

        cur = conn.execute(
            """
            INSERT INTO clarifications
                (user_id, transaction_id, field_name, question, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (user_id, txn_id, field_name, question),
        )

        return cur.lastrowid


def get_clarifications(conn, user_id, txn_id):
    """Return clarification history for this user's transaction."""
    if get_transaction(conn, user_id, txn_id) is None:
        return []

    rows = conn.execute(
        """
        SELECT * FROM clarifications
        WHERE user_id = ? AND transaction_id = ?
        ORDER BY id
        """,
        (user_id, txn_id),
    ).fetchall()

    return [dict(row) for row in rows]


def answer_clarification(
    conn, user_id, clarification_id, answer, status="answered"
):
    """Save a clarification and apply valid answers to the transaction."""
    from decimal import Decimal, InvalidOperation

    if status not in {"answered", "unknown"}:
        raise ValueError("Status must be 'answered' or 'unknown'.")

    with conn:
        row = conn.execute(
            """
            SELECT * FROM clarifications
            WHERE id = ? AND user_id = ?
            """,
            (clarification_id, user_id),
        ).fetchone()

        if row is None:
            return False

        clarification = dict(row)

        if clarification["status"] != "pending":
            raise ValueError("This clarification has already been resolved.")

        if status == "unknown":
            conn.execute(
                """
                UPDATE clarifications
                SET status = 'unknown', answer = NULL,
                    updated_at = datetime('now')
                WHERE id = ? AND user_id = ?
                """,
                (clarification_id, user_id),
            )
            return True

        if answer is None or not str(answer).strip():
            raise ValueError("An answer is required.")

        answer = str(answer).strip()
        field = clarification["field_name"]

        # Record a duplicate decision without editing financial transaction data.
        if field == "duplicate":
            if answer not in {"duplicate", "separate_purchase"}:
                raise ValueError(
                    "Choose 'duplicate' or 'separate_purchase'."
                )

            conn.execute(
                """
                UPDATE clarifications
                SET status = 'answered', answer = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND user_id = ?
                """,
                (answer, clarification_id, user_id),
            )
            return True

        if field == "items":
            txn_id = clarification["transaction_id"]
            txn = get_transaction(conn, user_id, txn_id)

            if txn is None:
                raise ValueError("Transaction not found.")

            conn.execute(
                """
                UPDATE clarifications
                SET status = 'answered', answer = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND user_id = ?
                """,
                ("Confirmed after review", clarification_id, user_id),
            )

            conn.execute(
                """
                UPDATE transactions
                SET uncertain_fields = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    json.dumps([
                        item
                        for item in txn.get("uncertain_fields", [])
                        if item != "items"
                    ]),
                    txn_id,
                    user_id,
                ),
            )

            return True

        if field not in EDITABLE_FIELDS:
            raise ValueError(
                f"Clarification field '{field}' cannot be edited directly."
            )

        if field in {
            "total_minor",
            "tax_minor",
            "discount_minor",
            "other_charges_minor",
        }:
            try:
                amount = Decimal(
                    answer.replace(",", "").replace("₹", "").strip()
                )

                if not amount.is_finite() or amount < 0:
                    raise ValueError(
                        "Amount must be a non-negative number."
                    )

                minor_units = amount * 100

                if minor_units != minor_units.to_integral_value():
                    raise ValueError(
                        "Amount cannot have fractions of a paisa."
                    )

                value = int(minor_units)

            except InvalidOperation:
                raise ValueError(
                    "Enter a valid amount, such as 145.00."
                )

            changes = {field: value}

        elif field == "txn_date":
            try:
                changes = {field: date.fromisoformat(answer)}
            except ValueError:
                raise ValueError(
                    "Enter the date in YYYY-MM-DD format."
                )

        elif field == "currency":
            changes = {field: answer.upper()}

        else:
            changes = {field: answer}

        txn_id = clarification["transaction_id"]
        changed_fields = update_transaction(
            conn, user_id, txn_id, changes
        )

        if changed_fields is None:
            raise ValueError("Transaction not found.")

        conn.execute(
            """
            UPDATE clarifications
            SET status = 'answered', answer = ?,
                updated_at = datetime('now')
            WHERE id = ? AND user_id = ?
            """,
            (answer, clarification_id, user_id),
        )

    return True


# ---------- update ----------

def update_transaction(conn, user_id, txn_id, changes):
    """Edit transaction fields and log every real change."""
    unknown = set(changes) - EDITABLE_FIELDS

    if unknown:
        raise ValueError(
            f"These fields cannot be edited: {sorted(unknown)}"
        )

    current = get_transaction(conn, user_id, txn_id)

    if current is None:
        return None

    if not changes:
        return []

    validated = TransactionIn(**changes).model_dump(include=set(changes))

    changed = {}

    for field, new_value in validated.items():
        new_value = _db_value(new_value)

        if new_value != current[field]:
            changed[field] = new_value

    if not changed:
        return []

    remaining_uncertain = [
        field
        for field in current["uncertain_fields"]
        if field not in changed
    ]

    with conn:
        for field, new_value in changed.items():
            _log_correction(
                conn,
                user_id,
                txn_id,
                field,
                current[field],
                new_value,
            )

        set_clause = ", ".join(
            f"{field} = ?" for field in changed
        )

        conn.execute(
            f"""UPDATE transactions
                SET {set_clause}, status = 'corrected', uncertain_fields = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND user_id = ?""",
            (
                *changed.values(),
                json.dumps(remaining_uncertain),
                txn_id,
                user_id,
            ),
        )

    return list(changed)


def update_line_item(conn, user_id, item_id, changes):
    """Edit a line item and mark its parent transaction corrected."""
    unknown = set(changes) - ITEM_EDITABLE_FIELDS

    if unknown:
        raise ValueError(
            f"These fields cannot be edited: {sorted(unknown)}"
        )

    row = conn.execute(
        "SELECT * FROM line_items WHERE id = ? AND user_id = ?",
        (item_id, user_id),
    ).fetchone()

    if row is None:
        return None

    current = dict(row)

    if not changes:
        return []

    merged = {
        field: current[field]
        for field in ITEM_EDITABLE_FIELDS
    }
    merged.update(changes)

    validated = LineItemIn(**merged).model_dump()

    changed = {
        field: validated[field]
        for field in changes
        if validated[field] != current[field]
    }

    if not changed:
        return []

    with conn:
        for field, new_value in changed.items():
            _log_correction(
                conn,
                user_id,
                current["transaction_id"],
                field,
                current[field],
                new_value,
                line_item_id=item_id,
            )

        set_clause = ", ".join(
            f"{field} = ?" for field in changed
        )

        conn.execute(
            f"UPDATE line_items SET {set_clause} WHERE id = ? AND user_id = ?",
            (*changed.values(), item_id, user_id),
        )

        conn.execute(
            """UPDATE transactions
               SET status = 'corrected', updated_at = datetime('now')
               WHERE id = ? AND user_id = ?""",
            (current["transaction_id"], user_id),
        )

    return list(changed)


def confirm_transaction(conn, user_id, txn_id):
    """Confirm a transaction only when calculation checks pass."""
    current = get_transaction(conn, user_id, txn_id)

    if current is None:
        return False

    issues = validate_transaction(current)
    blocking_codes = {"item_math", "total_mismatch"}

    blocking_issues = [
        issue
        for issue in issues
        if issue["code"] in blocking_codes
    ]

    if blocking_issues:
        details = "; ".join(
            issue["message"] for issue in blocking_issues
        )
        raise ValueError(
            "Please resolve calculation issues before confirmation: "
            f"{details}"
        )

    with conn:
        if current["status"] != "confirmed":
            _log_correction(
                conn,
                user_id,
                txn_id,
                "status",
                current["status"],
                "confirmed",
            )

        conn.execute(
            """UPDATE transactions
               SET status = 'confirmed',
                   updated_at = datetime('now')
               WHERE id = ? AND user_id = ?""",
            (txn_id, user_id),
        )

    return True


def delete_transaction(conn, user_id, txn_id):
    """Delete a transaction and its dependent records by cascade."""
    with conn:
        cur = conn.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (txn_id, user_id),
        )

    return cur.rowcount > 0
