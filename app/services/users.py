def create_user(conn, email, password_hash):
    """Insert a user and return the new user id.
    Password hashing is added in Phase 10; here we just store what we are given."""
    with conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email.strip().lower(), password_hash),
        )
    return cur.lastrowid


def get_user_by_email(conn, email):
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    return dict(row) if row else None