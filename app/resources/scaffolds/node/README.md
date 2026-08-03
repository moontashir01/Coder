# {{PROJECT_NAME}}

An Express web application. One process serves the pages, the styles, the
uploaded images and the API — there is no separate frontend server and no build
step.

## Run it locally

You need Node 18+ and a running PostgreSQL server.

```bash
npm install
createdb {{PROJECT_SLUG}}     # once — or: psql -c "CREATE DATABASE {{PROJECT_SLUG}}"
node seed.js                  # optional: a few rows of demo data
node server.js
```

Then open <http://127.0.0.1:3000>.

The connection string is `DATABASE_URL` if it is set, otherwise
`postgres://postgres:postgres@localhost:5432/{{PROJECT_SLUG}}`. If the database
is unreachable the server **exits with an error instead of starting** — an app
that answers 200 while its tables are missing looks healthy and 500s on every
page that shows data.

## How it is laid out

| Path             | What belongs in it                                          |
| ---------------- | ----------------------------------------------------------- |
| `server.js`      | Express routes only — one handler per URL                    |
| `db.js`          | The connection pool, `initDb()`, and `ensureColumn()`        |
| `models.js`      | One query helper per operation (list / get / create / …)     |
| `seed.js`        | Demo rows, so no page is ever empty on first load            |
| `ui.js`          | The component helpers every view renders through (`ui.…`)    |
| `views/`         | EJS pages. All of them are wrapped by `layout.ejs`           |
| `public/css/`    | Stylesheets. Colours and fonts are variables in `theme.css`  |
| `public/js/`     | Progressive enhancement only — the site works without it     |
| `public/uploads/`| Files uploaded through the app                               |

Three rules keep this coherent as it grows:

1. **Every view is a fragment.** `layout.ejs` owns the `<html>` document and the
   navigation, and `express-ejs-layouts` wraps every `res.render()` in it. A view
   that writes its own `<html>` or `<nav>` renders two navbars.
2. **Values are `$1, $2, …` parameters, never string-concatenated.** That is what
   makes SQL injection impossible rather than merely unlikely.
3. **Schema changes are additive.** To add a field, add an `ensureColumn(...)`
   call in `db.js`. It runs on every start and leaves existing rows alone, so
   nobody has to drop the database.

## Deploy it

Anything that reads a Procfile will run it:

```
web: node server.js
```

Set `DATABASE_URL` from the host's environment, and note that the app listens on
`process.env.PORT` — a host that assigns a port will be ignored if you hardcode
one.

- **Render** — New Web Service, build `npm install`, start `node server.js`,
  plus a PostgreSQL instance whose connection string becomes `DATABASE_URL`.
- **Railway / Fly.io** — detected from the Procfile automatically.

<sub>Written by Coder from the project spec — scaffold.</sub>
