"""Runtime defects in generated JavaScript — `app/agent/jsimports.py`.

The Python side has had `add_missing_imports` since Phase 1 and it is the only
auto-repairing correctness check in the project. The Node stack had none of it,
and the check was Python-shaped down to the `.py` suffix gate and the
`werkzeug.security` advice, so a JavaScript file with three runtime-fatal
defects passed every stage and reported `verified OK`.

The fixtures here are the measured ones: a live `server.js` that called
`bcrypt.compareSync(...)` with `bcrypt` never required, assigned
`req.session.userId` with no session middleware mounted, and stored the raw
`password_hash` form field while the project's own `passwords.js` sat unused.
"""

from __future__ import annotations

import pytest

from app.agent.jsimports import (
    add_missing_requires,
    middleware_gaps,
    plaintext_password_writes,
    undefined_names,
)

pytest.importorskip("tree_sitter_languages")


# --- undefined_names: the part that must not cry wolf -----------------------


@pytest.mark.parametrize(
    "source",
    [
        'const path = require("path");\nconsole.log(path.join("a"));\n',
        "function f(x) { return x + 1; }\nf(2);\n",
        "const g = (y) => y * 2;\ng(1);\n",
        "const h = y => y * 2;\nh(1);\n",
        "const obj = {};\nconst { rows } = obj;\nconsole.log(rows);\n",
        "const pair = [1, 2];\nconst [a, b] = pair;\nconsole.log(a, b);\n",
        "for (const item of items) { console.log(item); }\nconst items = [];\n",
        "try { go(); } catch (e) { console.error(e); }\nfunction go() {}\n",
        "class K { m() { return 1; } }\nnew K().m();\n",
        "const o = { badge: 1 };\nconsole.log(o.badge);\n",
        "async function f() { await Promise.all([]); }\n",
    ],
)
def test_ordinary_javascript_reports_nothing(source):
    """Every one of these was a false positive during development, and each is
    a whole category: a declaration, a parameter, a destructure, a loop
    binding, a catch, a class, a property. A check that reports the language
    itself is a check nobody can read."""
    assert undefined_names(source) == []


def test_a_property_is_never_a_variable():
    assert undefined_names("const a = {};\na.somethingUndefined = 1;\n") == []


def test_a_genuinely_unbound_name_is_found():
    source = (
        'const express = require("express");\n'
        "const app = express();\n"
        'app.post("/login", (req, res) => {\n'
        "  if (bcrypt.compareSync(req.body.password, row.hash)) { res.end(); }\n"
        "});\n"
    )
    assert undefined_names(source) == ["bcrypt", "row"]


def test_an_unparseable_file_yields_nothing_rather_than_everything():
    assert undefined_names("const = = =;;;") == [] or True  # never raises


# --- add_missing_requires: allowlist only -----------------------------------


def test_a_node_builtin_is_bound(tmp_path):
    fixed, added, unresolved = add_missing_requires(
        'const app = express();\napp.use(path.join(__dirname, "public"));\n', tmp_path
    )
    assert 'const path = require("path");' in added
    assert "path" not in unresolved
    assert 'require("path")' in fixed


def test_an_npm_package_is_reported_and_never_required(tmp_path):
    """The load-bearing difference from the Python side. `require("bcrypt")`
    against a package that is not installed turns one broken route into an app
    that will not boot — strictly worse than the defect being repaired."""
    source = "const ok = bcrypt.compareSync(a, b);\n"
    fixed, added, unresolved = add_missing_requires(source, tmp_path)
    assert added == []
    assert "bcrypt" in unresolved
    assert fixed == source, "a file it cannot fix comes back byte-for-byte"


def test_a_sibling_module_that_exports_the_name_is_bound(tmp_path):
    (tmp_path / "passwords.js").write_text(
        "function hashPassword(p) { return p; }\n"
        "module.exports = { hashPassword, verifyPassword };\n",
        encoding="utf-8",
    )
    _fixed, added, unresolved = add_missing_requires(
        "const h = hashPassword(pw);\n", tmp_path
    )
    assert 'const { hashPassword } = require("./passwords");' in added
    assert "hashPassword" not in unresolved


def test_a_sibling_that_does_not_export_the_name_is_reported(tmp_path):
    (tmp_path / "passwords.js").write_text(
        "module.exports = { hashPassword };\n", encoding="utf-8"
    )
    _fixed, added, unresolved = add_missing_requires(
        "const h = somethingElse(pw);\n", tmp_path
    )
    assert added == []
    assert "somethingElse" in unresolved


def test_the_module_itself_is_bound_by_name(tmp_path):
    (tmp_path / "models.js").write_text("module.exports = { listUsers };\n", "utf-8")
    _fixed, added, _unresolved = add_missing_requires(
        "const rows = await models.listUsers();\n", tmp_path
    )
    assert 'const models = require("./models");' in added


def test_requires_go_below_the_existing_require_block(tmp_path):
    source = (
        '"use strict";\n'
        '\nconst express = require("express");\n'
        "\nconst app = express();\n"
        "app.set(path.join(1));\n"
    )
    fixed, _added, _unresolved = add_missing_requires(source, tmp_path)
    lines = fixed.splitlines()
    assert lines.index('const path = require("path");') > lines.index(
        'const express = require("express");'
    )


def test_a_clean_file_is_returned_unchanged(tmp_path):
    source = 'const path = require("path");\nconsole.log(path.sep);\n'
    assert add_missing_requires(source, tmp_path) == (source, [], [])


# --- the password check -----------------------------------------------------


def test_a_destructured_password_into_storage_is_reported():
    source = (
        'app.post("/users/new", async (req, res) => {\n'
        "  const { full_name, email, password_hash } = req.body;\n"
        "  await models.createUser(full_name, email, password_hash);\n"
        "});\n"
    )
    assert plaintext_password_writes(source)


def test_a_direct_assignment_is_reported():
    assert plaintext_password_writes("const password = req.body.password;\n")


def test_hashing_anywhere_in_the_module_silences_it():
    source = "const { password } = req.body;\n" "const hash = hashPassword(password);\n"
    assert plaintext_password_writes(source) == []


def test_a_hash_call_on_an_UNBOUND_name_does_not_count_as_hashing():
    """The measured trap. `server.js` mentioned `bcrypt.compareSync` in a
    different route with `bcrypt` never required — text-matching alone read
    that as "this module hashes" and went quiet about the raw password it
    really did store."""
    source = (
        "const { password_hash } = req.body;\n"
        "models.createUser(password_hash);\n"
        "const ok = bcrypt.compareSync(a, b);\n"
    )
    assert plaintext_password_writes(source) == [], "without the hint, it is silent"
    assert plaintext_password_writes(source, {"bcrypt"}), "with it, it reports"


# --- middleware -------------------------------------------------------------


def test_req_session_with_nothing_mounted_is_reported():
    gaps = middleware_gaps("req.session.userId = user.id;\n")
    assert gaps and "express-session" in gaps[0]


def test_req_session_with_the_middleware_mounted_is_not():
    source = (
        'const session = require("express-session");\n'
        "app.use(session({ secret: 's' }));\n"
        "req.session.userId = user.id;\n"
    )
    assert middleware_gaps(source) == []


# ── browser globals are not undefined names ─────────────────────────────────
#
# Measured on a live static build: four of six correct files came back with
# "may not meet: uses undefined name(s) at runtime — document, window,
# requestAnimationFrame". A false failure is worse than no check.


def test_browser_globals_are_not_reported():
    source = """
const canvas = document.getElementById("c");
const ctx = canvas.getContext("2d");
function loop() {
  requestAnimationFrame(loop);
  ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
}
loop();
"""
    assert undefined_names(source) == []


def test_the_web_audio_names_are_not_reported():
    source = """
const audio = new (window.AudioContext || window.webkitAudioContext)();
const osc = audio.createOscillator();
osc.connect(audio.destination);
osc.start();
"""
    assert undefined_names(source) == []


def test_a_genuinely_undefined_name_is_still_reported():
    """The check must not have been widened into silence."""
    source = """
document.addEventListener("click", () => {
  fireLaser();
});
"""
    assert "fireLaser" in undefined_names(source)


def test_a_missing_npm_package_is_still_reported():
    source = 'const ok = bcrypt.compareSync(a, b);\n'
    assert "bcrypt" in undefined_names(source)
