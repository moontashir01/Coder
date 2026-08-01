# {{PROJECT_NAME}}

A Flask web application. One process serves the pages, the styles, the uploaded
images and the API — there is no separate frontend server and no build step.

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
python seed.py                # optional: a few rows of demo data
python app.py
```

Then open <http://127.0.0.1:5000>.

`python app.py` runs Flask's development server (auto-reload, tracebacks in the
browser). Do not use it in production — that is what the Procfile is for.

## How it is laid out

| Path             | What belongs in it                                          |
| ---------------- | ----------------------------------------------------------- |
| `app.py`         | Flask routes only — one function per URL                     |
| `db.py`          | The SQLite connection, `init_db()`, and column migrations    |
| `models.py`      | One query helper per operation (list / get / create / …)     |
| `seed.py`        | Demo rows, so no page is ever empty on first load            |
| `templates/`     | Jinja2 pages. All of them extend `base.html`                 |
| `static/css/`    | Stylesheets. Fonts are CSS variables in `style.css`          |
| `static/js/`     | Progressive enhancement only — the site works without it     |
| `static/uploads/`| Files uploaded through the app                               |

Two rules keep this coherent as it grows:

1. **Every page extends `base.html`.** The navigation is defined there once, so
   pages cannot drift out of sync. Never paste a `<nav>` into a child template.
2. **Schema changes are additive.** To add a field, add an `ensure_column(...)`
   call in `db.py`. It runs on every start and leaves existing rows alone, so
   nobody has to delete the database.

## Deploy it

The app is a standard WSGI application (`app:app`), so anything that reads a
Procfile will run it:

```
web: gunicorn app:app --bind 0.0.0.0:$PORT
```

Both halves of that bind matter. The host tells you which port to listen on via
`$PORT`, and it must be `0.0.0.0` — gunicorn's default is `127.0.0.1`, which
accepts only connections from inside the container, so the deploy comes up
healthy and is still unreachable from the internet.

- **Render** — New Web Service, build `pip install -r requirements.txt`, start
  `gunicorn app:app`.
- **Railway / Fly.io** — detected from the Procfile automatically.
- **PythonAnywhere** — point the WSGI config at `app` in `app.py`.

Set `SECRET_KEY` from the host's environment rather than shipping the generated
one, and note that SQLite lives on the instance's disk: hosts with an ephemeral
filesystem will reset it on redeploy.
