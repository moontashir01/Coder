"""Constraints survive the trip from a PRD to the database (`Field`, `Dialect`).

Measured against a 12.5 KB marketplace PRD that printed its PostgreSQL DDL in
full. Every `UNIQUE`, every `REFERENCES`, every `CHECK (status IN (…))` and
every `DEFAULT` was dropped between the document and the generated `db.js` — not
because the model ignored them, but because `Field` carried only
name/type/pk/required and there was nowhere to put them. What a model cannot
represent it silently discards.

Two things are being pinned here. First that the constraints arrive. Second, and
more important, that they arrive SAFE: each one is interpolated into DDL with no
binding, so `_safe_default` / `_safe_reference` / `_safe_check_values` are the
`_ident` rule applied to the parts of a schema that are not identifiers.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.agent.projectspec import (
    POSTGRES,
    SQLITE,
    Entity,
    Field,
    entities_from_data,
    entities_from_sql,
    parse_schema_line,
)

# --- the validators, which are the security-relevant half -------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("100.00", "100.00"),
        ("-5", "-5"),
        ("'PENDING'", "'PENDING'"),
        ("CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP"),
        ("current_timestamp", "CURRENT_TIMESTAMP"),
        ("NOW()", "NOW()"),
        ("FALSE", "FALSE"),
        ("PENDING", "'PENDING'"),  # an unquoted word is quoted for you
    ],
)
def test_safe_defaults_are_kept(raw, expected):
    assert Field(name="x", default=_default(raw)).default == expected


@pytest.mark.parametrize(
    "raw",
    [
        "'a'); DROP TABLE users; --",
        "(SELECT secret FROM users)",
        "'unterminated",
        "1; DELETE FROM items",
        "some expression with spaces",
    ],
)
def test_unsafe_defaults_are_dropped_not_escaped(raw):
    """Dropped, never passed through with quoting bolted on: a DEFAULT clause is
    written into DDL unparameterised, so an allowlist is the only version of
    this that is not an injection surface."""
    assert _default(raw) == ""


def _default(raw):
    from app.agent.projectspec import _safe_default

    return _safe_default(raw)


def _reference(raw):
    from app.agent.projectspec import _safe_reference

    return _safe_reference(raw)


def _check(raw):
    from app.agent.projectspec import _safe_check_values

    return _safe_check_values(raw)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("users(id)", "users(id)"),
        ("users", "users"),
        ("users.id", "users(id)"),
        ("users(user_id) ON DELETE CASCADE", ""),  # not spellable — dropped
        ("users; DROP TABLE x", ""),
        ("", ""),
    ],
)
def test_references_go_through_ident(raw, expected):
    assert _reference(raw) == expected


def test_check_values_are_a_validated_list():
    assert _check(["DRAFT", "ACTIVE", "SOLD"]) == ("DRAFT", "ACTIVE", "SOLD")
    assert _check("'DRAFT','ACTIVE'") == ("DRAFT", "ACTIVE")
    assert _check(["Like New", "For Parts / Repair"]) == (
        "Like New",
        "For Parts / Repair",
    )


def test_a_check_value_with_a_quote_in_it_is_dropped():
    """The values are emitted inside `'…'` in the DDL; one that could close the
    literal early cannot be allowed through."""
    assert "a'b" not in _check(["ok", "a'b", "fine"])


def test_one_allowed_value_is_not_a_constraint():
    assert _check(["ONLY"]) == ()


# --- the DDL ---------------------------------------------------------------


def test_constraints_reach_the_create_table():
    entity = Entity(
        name="item",
        table="items",
        fields=(
            Field(name="id", type="INTEGER", pk=True, required=True),
            Field(name="slug", type="TEXT", required=True, unique=True),
            Field(
                name="seller_id", type="INTEGER", required=True, references="users(id)"
            ),
            Field(
                name="status",
                type="TEXT",
                default="'ACTIVE'",
                check=("DRAFT", "ACTIVE", "SOLD"),
            ),
            Field(name="title", type="TEXT", required=True, max_length=150),
        ),
    )
    ddl = entity.to_ddl(POSTGRES)
    assert "slug TEXT NOT NULL UNIQUE" in ddl
    assert "seller_id INTEGER NOT NULL REFERENCES users(id)" in ddl
    assert "CHECK (status IN ('DRAFT', 'ACTIVE', 'SOLD'))" in ddl
    assert "DEFAULT 'ACTIVE'" in ddl
    assert "title VARCHAR(150) NOT NULL" in ddl


def test_a_field_with_no_constraints_emits_exactly_what_it_always_did():
    """The compatibility guarantee: every caller written before constraints
    existed produces byte-for-byte the DDL it produced then."""
    plain = Field(name="title", type="TEXT", required=True)
    assert plain.to_ddl(SQLITE) == "title TEXT NOT NULL"
    assert Field(name="id", type="INTEGER", pk=True).to_ddl(SQLITE) == (
        "id INTEGER PRIMARY KEY AUTOINCREMENT"
    )


def test_a_generated_primary_key_takes_no_extra_clauses():
    """`SERIAL PRIMARY KEY` and `TEXT PRIMARY KEY DEFAULT gen_random_uuid()`
    are complete declarations; a UNIQUE or a DEFAULT bolted on is redundant at
    best and a conflict with the one already there at worst."""
    field = Field(name="id", type="INTEGER", pk=True, unique=True, default="1")
    assert field.to_ddl(POSTGRES) == "id SERIAL PRIMARY KEY"


def test_a_length_only_attaches_to_text():
    assert Field(name="n", type="INTEGER", max_length=5).to_ddl(SQLITE) == "n INTEGER"


def test_the_generated_ddl_really_runs_and_really_enforces():
    """Executed, not inspected — `test_crud.py`'s rule. A CREATE TABLE that
    parses in our head proves nothing about the one the app runs."""
    entity = parse_schema_line(
        "items(id INTEGER PRIMARY KEY, slug TEXT UNIQUE, "
        "status VARCHAR(20) DEFAULT 'ACTIVE' "
        "CHECK (status IN ('DRAFT','ACTIVE','SOLD')))"
    )
    conn = sqlite3.connect(":memory:")
    conn.execute(entity.to_ddl(SQLITE))
    conn.execute("INSERT INTO items (slug) VALUES ('a')")
    assert conn.execute("SELECT status FROM items").fetchone() == ("ACTIVE",)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO items (slug, status) VALUES ('b', 'BOGUS')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO items (slug) VALUES ('a')")


# --- the three parsers ------------------------------------------------------


def test_a_prd_ddl_line_keeps_its_constraints():
    entity = parse_schema_line(
        "users(user_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), "
        "email VARCHAR(255) UNIQUE NOT NULL, "
        "cod_reliability_score DECIMAL(5,2) DEFAULT 100.00, "
        "is_verified BOOLEAN DEFAULT FALSE)"
    )
    email = entity.field("email")
    assert email.unique and email.required and email.max_length == 255
    assert entity.field("cod_reliability_score").default == "100.00"
    assert entity.field("is_verified").default == "FALSE"


def test_the_schema_calls_json_shape_carries_them():
    entities = entities_from_data(
        {
            "entities": [
                {
                    "name": "order",
                    "table": "orders",
                    "fields": [
                        {"name": "id", "type": "INTEGER", "pk": True},
                        {
                            "name": "buyer_id",
                            "type": "INTEGER",
                            "references": "users(id)",
                        },
                        {
                            "name": "order_status",
                            "type": "TEXT",
                            "check": ["PENDING_OTP", "CONFIRMED"],
                            "default": "PENDING_OTP",
                        },
                        {
                            "name": "shipping_phone",
                            "type": "TEXT",
                            "unique": True,
                            "max_length": 20,
                        },
                    ],
                }
            ]
        }
    )
    order = entities[0]
    assert order.field("buyer_id").references == "users(id)"
    assert order.field("order_status").check == ("PENDING_OTP", "CONFIRMED")
    assert order.field("order_status").default == "'PENDING_OTP'"
    assert order.field("shipping_phone").unique


def test_adoption_reads_them_back_off_disk():
    """`from_disk`'s promise — a project Coder did not build still gets memory —
    has to include the constraints, or the first amendment drops them."""
    source = (
        'DDL = """CREATE TABLE IF NOT EXISTS items (\n'
        "    id INTEGER PRIMARY KEY,\n"
        "    slug TEXT UNIQUE,\n"
        "    seller_id INTEGER REFERENCES users(id)\n"
        ')"""'
    )
    entities = entities_from_sql({"db": source})
    item = entities[0]
    assert item.field("slug").unique
    assert item.field("seller_id").references == "users(id)"


def test_the_summary_states_them_so_the_prompt_does_too():
    """Told only `status TEXT`, a model invents its own status words and the
    CHECK then rejects every insert the app makes."""
    entity = Entity(
        name="order",
        table="orders",
        fields=(
            Field(name="status", type="TEXT", check=("A", "B"), default="'A'"),
            Field(name="email", type="TEXT", unique=True),
        ),
    )
    summary = entity.summary()
    assert "in A|B" in summary and "default 'A'" in summary and "unique" in summary


# --- migrations -------------------------------------------------------------


def test_a_migration_never_emits_not_null_unique_or_check():
    """All three can be true of a new column and false of the rows already
    stored, so adding one raises against real data — and a migration that fails
    on startup takes the whole app down, which is worse than a column that is
    merely less constrained than the spec says."""
    call = SQLITE.migration_call(
        "items", "status", "TEXT", default="'ACTIVE'", max_length=20
    )
    assert "VARCHAR(20) DEFAULT 'ACTIVE'" in call
    assert "NOT NULL" not in call and "UNIQUE" not in call and "CHECK" not in call


def test_a_migration_with_no_constraints_is_unchanged():
    assert SQLITE.migration_call("items", "colour", "TEXT") == (
        'ensure_column(conn, "items", "colour", "TEXT")'
    )
