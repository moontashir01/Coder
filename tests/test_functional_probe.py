"""Functional probe (app/agent/smoke.py) — Phase 5, closes Gap 3.

"It started" and "it works" are different claims. Until this phase only the
first was ever made: a build whose every POST returned 500 still reported a
passing smoke test, because any HTTP status counted as alive. Measured on live
builds throughout `docs/phase1-notes.md` … `phase4-notes.md`.

Offline, but NOT mocked: these start a real Flask app in a subprocess and talk
to it over HTTP, because a probe that is only tested against a fake has not been
tested at all. The fixture apps are written by the test, never generated.
"""

import socket
import textwrap
from pathlib import Path

import pytest

from app.agent.projectspec import Entity, Field, Page, ProjectSpec, SpecEndpoint
from app.agent.smoke import (
    PROBE_MARKER,
    ProbeCheck,
    SmokeResult,
    _encode_multipart,
    _png_1x1,
    functional_probe,
    run_smoke_test,
    server_error,
)

pytest.importorskip("flask")


def _free_port() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", 5000)) != 0


def _spec() -> ProjectSpec:
    return ProjectSpec(
        name="shop",
        entities=(
            Entity(
                "product",
                "products",
                (
                    Field("id", "INTEGER", pk=True, required=True),
                    Field("title", "TEXT", required=True),
                    Field("price", "REAL"),
                    Field("image_path", "IMAGE"),
                ),
            ),
        ),
        endpoints=(
            SpecEndpoint("GET", "/", template="templates/index.html"),
            SpecEndpoint("POST", "/products", entity="product"),
        ),
        pages=(Page("/", "templates/index.html", "Home", "storefront", ("product",)),),
    )


_WORKING_APP = """
import sqlite3
from pathlib import Path
from flask import Flask, request, render_template_string

app = Flask(__name__)
DB = Path(__file__).resolve().parent / "app.db"


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS products "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, price REAL, image_path TEXT)"
    )
    conn.commit()
    conn.close()


init_db()


@app.route("/")
def index():
    conn = get_db()
    rows = conn.execute("SELECT * FROM products").fetchall()
    conn.close()
    items = "".join("<li>%s</li>" % r["title"] for r in rows)
    return render_template_string(
        "<html><body><h1>Shop</h1><ul>{{ items|safe }}</ul></body></html>", items=items
    )


@app.route("/products", methods=["POST"])
def create():
    conn = get_db()
    conn.execute(
        "INSERT INTO products (title, price) VALUES (?, ?)",
        (request.form.get("title", ""), request.form.get("price", 0)),
    )
    conn.commit()
    conn.close()
    return "created", 302


if __name__ == "__main__":
    app.run(port=5000)
"""

# Same app, but the POST handler never writes. Starts fine, answers 302,
# passes every liveness check — and adding a product does nothing.
_SILENT_APP = _WORKING_APP.replace(
    "    conn.execute(\n"
    '        "INSERT INTO products (title, price) VALUES (?, ?)",\n'
    '        (request.form.get("title", ""), request.form.get("price", 0)),\n'
    "    )\n"
    "    conn.commit()\n",
    "    pass\n",
)

_CRASHING_APP = _WORKING_APP.replace(
    'return "created", 302', "raise ValueError('boom')"
)


def _write_app(tmp_path: Path, source: str) -> Path:
    app = tmp_path / "app.py"
    app.write_text(textwrap.dedent(source), encoding="utf-8")
    return app


# ---------------------------------------------------------------------------
# The building blocks
# ---------------------------------------------------------------------------


def test_the_probe_png_is_a_real_png():
    """An IMAGE field needs an actual file — posting without one exercises the
    "no file" branch, not the upload."""
    data = _png_1x1()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")  # PNG signature
    assert b"IHDR" in data and b"IDAT" in data
    assert data.endswith(b"IEND\xaeB`\x82")  # IEND chunk + its fixed CRC
    assert 60 < len(data) < 200


def test_png_is_decodable_if_pillow_is_around():
    Image = pytest.importorskip("PIL.Image")
    import io

    with Image.open(io.BytesIO(_png_1x1())) as img:
        assert img.size == (1, 1)


def test_multipart_encoding_carries_fields_and_the_file():
    body, content_type = _encode_multipart(
        {"title": "Dune"}, {"image": ("x.png", b"\x89PNG", "image/png")}
    )
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="title"' in body and b"Dune" in body
    assert b'filename="x.png"' in body and b"\x89PNG" in body
    assert body.rstrip().endswith(b"--")


# ---------------------------------------------------------------------------
# The probe, against real running apps
# ---------------------------------------------------------------------------


def test_a_working_app_passes_every_check(tmp_path):
    if not _free_port():
        pytest.skip("port 5000 in use")
    app = _write_app(tmp_path, _WORKING_APP)

    result = run_smoke_test(app, tmp_path, timeout=20.0, spec=_spec())

    assert result.started and result.responded, result.detail
    assert result.checks, "the probe did not run"
    assert result.failures() == (), [c.line() for c in result.checks]
    labels = {c.label for c in result.checks}
    assert "GET /" in labels
    assert "POST /products" in labels
    assert "product comes back after POST" in labels
    assert "functional check(s) passed" in result.note()
    assert not result.note().startswith("may not meet")


def test_an_app_whose_post_never_persists_is_caught(tmp_path):
    """The whole point of the phase. This app starts, answers, and returns 302 —
    every liveness check passes — but adding a product does nothing."""
    if not _free_port():
        pytest.skip("port 5000 in use")
    app = _write_app(tmp_path, _SILENT_APP)

    result = run_smoke_test(app, tmp_path, timeout=20.0, spec=_spec())

    assert result.started and result.responded  # it IS alive…
    failed = {c.label for c in result.failures()}
    assert "product comes back after POST" in failed  # …and it does NOT work
    assert "POST /products" not in failed  # the POST itself answered fine
    assert result.note().startswith("may not meet")


def test_the_value_is_looked_for_on_every_page_not_just_tagged_ones(tmp_path):
    """`reads` is inferred from blueprint prose and is routinely empty on the
    very listing page that matters. Measured live: the storefront `/` had
    `reads: ()` while the FORM page was tagged, so probing only tagged pages
    failed a row that had persisted — sending the repair loop after working code.
    """
    if not _free_port():
        pytest.skip("port 5000 in use")
    app = _write_app(tmp_path, _WORKING_APP)

    spec = _spec()
    # The listing page is untagged; the form page is the one tagged.
    spec.pages = (
        Page("/add", "templates/add.html", "Add", "form", ("product",)),
        Page("/", "templates/index.html", "Home", "storefront", ()),
    )

    result = run_smoke_test(app, tmp_path, timeout=20.0, spec=spec)

    came_back = next(c for c in result.checks if "comes back" in c.label)
    assert came_back.ok is True, came_back.detail
    assert "visible on /" in came_back.detail


def test_a_handler_that_raises_is_reported_as_a_5xx(tmp_path):
    if not _free_port():
        pytest.skip("port 5000 in use")
    app = _write_app(tmp_path, _CRASHING_APP)

    result = run_smoke_test(app, tmp_path, timeout=20.0, spec=_spec())

    post = next(c for c in result.checks if c.label == "POST /products")
    assert post.ok is False
    assert "500" in post.detail


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "<title>NameError: name 'Product' is not defined // Werkzeug Debugger</title>",
            "NameError: name 'Product' is not defined",
        ),
        ("<title>500 Internal Server Error</title>", ""),
        ("traceback...\nKeyError: 'image'\n", "KeyError: 'image'"),
        ("", ""),
    ],
)
def test_the_exception_behind_a_500_is_lifted_out(text, expected):
    """ "POST /x failed" is a poor repair prompt; "POST /x -> 500 NameError: name
    'Product' is not defined" is the line the model needs."""
    assert server_error(text) == expected


def test_without_a_spec_the_old_liveness_behaviour_is_unchanged(tmp_path):
    """Every existing caller must be unaffected."""
    if not _free_port():
        pytest.skip("port 5000 in use")
    app = _write_app(tmp_path, _WORKING_APP)

    result = run_smoke_test(app, tmp_path, timeout=20.0)

    assert result.started and result.responded
    assert result.checks == ()
    assert result.note().startswith("Smoke test:")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_a_failing_check_makes_the_note_honest():
    result = SmokeResult(
        True,
        True,
        200,
        5000,
        "app.py started",
        checks=(
            ProbeCheck("GET /", True, "200"),
            ProbeCheck("product visible on /", False, "posted value not shown"),
        ),
    )
    note = result.note()
    assert note.startswith("may not meet")
    assert "1/2" in note
    assert "posted value not shown" in note


def test_a_startup_crash_still_reports_as_before():
    result = SmokeResult(False, False, None, None, "app.py crashed on startup")
    assert result.note().startswith("may not meet: app.py crashed")


def test_probe_without_a_spec_returns_no_checks():
    assert functional_probe(None, 5000) == []


def test_the_marker_is_distinctive_enough_to_mean_something():
    """Finding it in the HTML must not be a coincidence."""
    assert len(PROBE_MARKER) >= 12 and PROBE_MARKER.isalnum()
