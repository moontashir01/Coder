"""Demo data for {{PROJECT_NAME}}.

An empty site reads as broken even when it is working perfectly, so give every
entity a few rows. Run it with:  python seed.py

Use `INSERT OR IGNORE` (or check first) so running it twice does not duplicate
anything.
"""

import db


def seed() -> None:
    """Insert a few demo rows per entity. Safe to run more than once."""
    conn = db.get_db()
    try:
        # e.g.
        # conn.execute(
        #     "INSERT OR IGNORE INTO products (id, title, price) VALUES (?, ?, ?)",
        #     (1, "Example product", 9.99),
        # )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    db.init_db()
    seed()
    print("Seeded the database.")
