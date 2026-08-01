"""{{PROJECT_NAME}} — Flask application entry point.

ROUTES ONLY. This file defines URL routes and renders templates; it does not
talk to SQLite directly. The connection and schema live in db.py, and every
query lives in models.py. Keeping those three apart is what lets a later change
("add a picture to a product") touch one query helper instead of the whole app.

Run it locally with:  python app.py
"""

import os
from pathlib import Path

import db
from flask import Flask, render_template

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"

app = Flask(__name__)

# Signs the session cookie. The fallback was generated once when this project
# was scaffolded; on a real host, set SECRET_KEY in the environment instead.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "{{SECRET_KEY}}")
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
# Refuse uploads bigger than this (bytes) — Flask returns 413 by itself.
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

# Create the database and apply any new columns on every start. Idempotent, so
# it is safe to run against an existing database.
db.init_db()


@app.route("/")
def index():
    """Home page. Replace the template body with this project's real content."""
    return render_template("index.html")


if __name__ == "__main__":
    # debug=True gives auto-reload and a traceback in the browser. The Procfile
    # uses gunicorn instead when deployed.
    app.run(debug=True, port=5000)
