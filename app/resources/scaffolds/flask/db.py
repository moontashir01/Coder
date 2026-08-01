"""SQLite connection and schema for {{PROJECT_NAME}}.

Two jobs, and nothing else:
  * `get_db()` — a connection whose rows behave like dicts (`row["title"]`).
  * `init_db()` — create tables that don't exist yet, and add columns that
    don't exist yet, every time the app starts.

`init_db()` is deliberately idempotent. Adding a field to an entity later must
NOT mean deleting the database — it means one more `ensure_column` call here,
which runs against the rows already stored.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "app.db"


def get_db():
    """A SQLite connection with dict-like row access.

    Callers are responsible for closing it — see the helpers in models.py for
    the pattern (`try: ... finally: conn.close()`).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table: str, column: str, decl: str) -> None:
    """Idempotent `ALTER TABLE ADD COLUMN`, guarded by PRAGMA table_info.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, and running a plain ALTER twice
    raises. This makes adding a field to an existing table safe to run on every
    startup, which is what keeps old data working after a schema change.

        ensure_column(conn, "products", "image_path", "TEXT")
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    """Create tables and apply column additions. Safe to call on every start."""
    conn = get_db()
    try:
        # --- tables -----------------------------------------------------
        # One CREATE TABLE IF NOT EXISTS per entity. Coder writes these for you
        # from the project's schema. Shape (a deliberately unrelated table, so
        # it is never mistaken for this project's own):
        #
        #     conn.execute(
        #         '''CREATE TABLE IF NOT EXISTS widgets (
        #                id INTEGER PRIMARY KEY AUTOINCREMENT,
        #                colour TEXT NOT NULL
        #            )'''
        #     )

        # --- added columns ----------------------------------------------
        # Fields added AFTER a table already shipped go here, so existing
        # databases pick them up without being recreated. Coder writes these
        # for you when you ask for a new field. Shape (a deliberately unrelated
        # table, so it is never mistaken for this project's own schema):
        #
        #     ensure_column(conn, "widgets", "colour", "TEXT")

        conn.commit()
    finally:
        conn.close()
