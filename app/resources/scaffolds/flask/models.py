"""Database queries for {{PROJECT_NAME}} — one small function per operation.

Every query lives here, so a route in app.py stays three lines long and a
schema change touches one file. Follow this shape for each entity (list / get /
create / update / delete):

    def list_products():
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM products ORDER BY id DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_product(product_id):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_product(title, price):
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO products (title, price) VALUES (?, ?)",
                (title, price),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

ALWAYS pass values as `?` parameters, as above. Never build SQL by formatting
or concatenating a value into the string — that is how SQL injection happens,
and the parameterised form is shorter anyway.
"""

from db import get_db  # noqa: F401  (used by the query helpers added below)
