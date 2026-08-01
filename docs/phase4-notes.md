# Phase 4 — deterministic domain layer: what shipped and what four live builds corrected

Companion to `phase0-baseline.md` … `phase3-notes.md`. Recorded **2026-08-01**.

## What shipped

| Part | Piece | Where |
|---|---|---|
| 4a | CRUD helpers generated from entities | `app/agent/crud.py` |
| 4a | `CREATE TABLE` written into `db.py`'s `init_db()` | `crud.apply_table_block` |
| 4b | `save_upload()` — allowlist, `secure_filename`, collision-safe | `crud.upload_helper_source` |
| 4b | `enctype` repair (shipped early, in Phase 3) | `verify.fix_form_enctype` |
| 4c | Plaintext-password check — on the CODE, not in a prompt | `crud.plaintext_password_writes` |
| 4d | `seed.py` with 3 rows per entity, **and it is run** | `crud.seed_source`, `core._seed_demo_data` |
| — | The API description the model needs | `crud.api_context` |
| — | 32 tests | `tests/test_crud.py` |

Per the plan's trim guidance, **4c's auth scaffolding was cut** and only its
deterministic check kept — the plan is explicit that the plaintext-password rule must be
"a deterministic check on generated code, not a prompt instruction".

## The core move

`db.py`'s tables, `models.py` and `seed.py` are written **before generation** and dropped
from the plan. They contain no decisions: the table *is* the fields, the query *is* the
table, the demo row *is* the field types. Phase 2's structured entity list is what makes
this possible.

Two properties then hold by construction rather than by inspection:

- **SQL injection is impossible.** Values are bound as `?`; identifiers come from
  `projectspec._ident`, which admits only `[A-Za-z_][A-Za-z0-9_]*`.
- **Column lists cannot drift from the tables**, because both are printed from the same
  `Entity`. That is exactly the `models.get_all_posts` / `add_post` mismatch that broke
  earlier builds.

The tests execute the generated SQL against real in-memory sqlite3 — full
create/list/get/update/delete, plus an injection attempt that leaves the table standing.

## Four live builds, four corrections

The first Phase 4 build was **a regression**: the app died at import and served 0/3 pages,
worse than before the phase. Unit tests passed on that version. Every one of these was
visible only by running a build and reading what landed on disk.

1. **Taking the data layer away without describing it.** `app.py` opened with
   `from models import get_user_by_email, get_all_products, User, Product` — four invented
   names — and raised `ImportError` before rendering a page. Removing a file from the plan
   means the model never sees it written, so it guesses. `crud.api_context()` now states
   the exact signatures, and a test asserts the description and the generated code cannot
   drift.

2. **`db.py` created `users` but not `products`.** The idempotency check scanned raw text,
   so the scaffold's *commented* `# CREATE TABLE IF NOT EXISTS products (` counted as a
   real table and the real one was skipped — every product route 500'd with "no such
   table" while users worked. This is the same commented-example trap fixed for
   `ensure_column` in Phase 3 and missed here. `_creates_table` now scans string literals
   only, and the scaffold's example uses a deliberately unrelated table.

3. **A table called `the`.** `missing_tables` matched `FROM the` inside prose in my own
   generated docstring. A literal must now contain an actual SQL statement keyword before
   its table references count.

4. **A `seed.py` nobody ran.** 4d's promise is that the storefront is never empty on first
   load; generating a seed script guarded by `if __name__ == "__main__"` does not keep it.
   `_seed_demo_data` runs it once after the build. This is a deliberate exception to the
   rule about executing generated code, and the reason it is safe is specific: `seed.py`
   and `db.py`'s schema are written by `crud.py`, not by the model. Short timeout, output
   discarded, failure reported rather than raised.

A fifth bug came from the unit tests rather than a build: for a `users` table keyed on
`email`, the key is both the primary key and a writable column, so `update_user` came out
as `def update_user(email, email, password_hash)` — a `SyntaxError`. The key identifies the
row; it is never also a column being set.

## Final live result

```
Wrote the data layer from the declared schema rather than generating it — db.py, models.py, seed.py
Seeded the database with demo rows, so no page starts empty.
rows on first load: users 3, products 3
GET /login    -> 200        GET /products -> 200        GET / -> 200      (3/3)
POST /login   -> 200
/products renders 6 seeded values
```

## Still open

**The 4c check fired on a real bug and was left as a report:**

```
may not meet: stores a password without hashing it —
password_hash = request.form["password"]
```

The model assigned a raw request password to a field literally named `password_hash`.
Reported rather than rewritten, because fixing it means restructuring the login flow —
which is generation, not repair. This is the check doing its job.

**`/api/login` is still declared and never built** — the blueprint's contract names it, the
coverage check reports it, and no route appears. Unchanged since Phase 1.

**"Started" still is not "works."** The smoke test reported `GET /api/login -> 404` and
counted the build as up. Phase 5's functional probe is the fix; the scratchpad
`spec_probe.py` used throughout these notes is a preview of what it should do.

## Note for whoever runs this next

Killing `ollama.exe` does **not** kill its model runner. Four orphaned `llama-server.exe`
processes accumulated across this session's suite runs and held ~7.6 GB of VRAM, until a
build failed with `cudaMalloc failed: out of memory`. Clean up with:

```powershell
Get-Process llama-server, ollama* | Stop-Process -Force
```
