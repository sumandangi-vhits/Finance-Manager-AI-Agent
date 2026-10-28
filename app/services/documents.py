def create_document(conn, user_id, original_filename, stored_path,
                    file_hash, mime_type=None, page_count=None):
    with conn:
        cur = conn.execute(
            """INSERT INTO documents
               (user_id, original_filename, stored_path, file_hash, mime_type, page_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, original_filename, stored_path, file_hash, mime_type, page_count),
        )
    return cur.lastrowid


def get_document(conn, user_id, document_id):
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ? AND user_id = ?",
        (document_id, user_id),
    ).fetchone()
    return dict(row) if row else None