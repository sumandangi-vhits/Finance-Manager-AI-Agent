def find_same_file(conn, user_id, file_hash):
    """Earlier uploads of the identical file by this user that still have a transaction."""
    rows = conn.execute(
        """SELECT d.id AS document_id, d.original_filename, d.uploaded_at,
                  t.id AS transaction_id
           FROM documents d
           JOIN transactions t ON t.document_id = d.id AND t.user_id = d.user_id
           WHERE d.user_id = ? AND d.file_hash = ?
           ORDER BY d.id""",
        (user_id, file_hash),
    ).fetchall()
    return [dict(r) for r in rows]


def find_similar_transactions(conn, user_id, data):
    """Transactions that look like the same purchase. These are only warnings:
    a genuine repeat purchase must never be rejected automatically."""
    merchant = data.get("merchant")
    txn_date = data.get("txn_date")
    total = data.get("total_minor")
    invoice = data.get("invoice_number")
    matches = {}

    if merchant and txn_date and total is not None:
        rows = conn.execute(
            """SELECT id, merchant, txn_date, total_minor, invoice_number, status
               FROM transactions
               WHERE user_id = ? AND LOWER(merchant) = LOWER(?)
                 AND txn_date = ? AND total_minor = ?""",
            (user_id, merchant, txn_date, total),
        ).fetchall()
        for row in rows:
            matches[row["id"]] = dict(row)

    if merchant and invoice:
        rows = conn.execute(
            """SELECT id, merchant, txn_date, total_minor, invoice_number, status
               FROM transactions
               WHERE user_id = ? AND LOWER(merchant) = LOWER(?)
                 AND invoice_number = ?""",
            (user_id, merchant, invoice),
        ).fetchall()
        for row in rows:
            matches[row["id"]] = dict(row)

    return list(matches.values())