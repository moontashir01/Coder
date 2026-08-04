/**
 * PostgreSQL connection pool and schema for {{PROJECT_NAME}}.
 *
 * Two jobs, and nothing else:
 *   * `getPool()` — one shared connection pool for the whole process.
 *   * `initDb()`  — create tables that don't exist yet, and add columns that
 *                   don't exist yet, every time the app starts.
 *
 * `initDb()` is deliberately idempotent. Adding a field to an entity later must
 * NOT mean dropping the database — it means one more `ensureColumn` call here,
 * which runs against the rows already stored.
 *
 * Connection: DATABASE_URL if set, else a local server and a database named
 * after this project. Create it once with:
 *
 *     createdb {{PROJECT_SLUG}}
 *     # or:  psql -c "CREATE DATABASE {{PROJECT_SLUG}}"
 */

"use strict";

const { Pool } = require("pg");

const DATABASE_URL = process.env.DATABASE_URL || "{{DATABASE_URL}}";

let pool = null;

/** The one connection pool for this process. Created on first use. */
function getPool() {
  if (pool === null) {
    pool = new Pool({ connectionString: DATABASE_URL });
  }
  return pool;
}

/**
 * Idempotent `ALTER TABLE ADD COLUMN`.
 *
 * PostgreSQL has `IF NOT EXISTS` for this (unlike SQLite), so adding a field to
 * an existing table is safe to run on every startup — which is what keeps old
 * data working after a schema change.
 *
 *     await ensureColumn(client, "products", "image_path", "TEXT");
 *
 * `table` and `column` are interpolated, so they must be identifiers this
 * project generated — never anything that came from a request.
 */
async function ensureColumn(client, table, column, decl) {
  await client.query(
    `ALTER TABLE ${table} ADD COLUMN IF NOT EXISTS ${column} ${decl}`
  );
}

/** Create tables and apply column additions. Safe to call on every start. */
async function initDb() {
  const client = await getPool().connect();
  try {
    // --- tables -----------------------------------------------------
    // One CREATE TABLE IF NOT EXISTS per entity. Shape (a deliberately
    // unrelated table, so it is never mistaken for this project's own):
    //
    //     await client.query(`
    //         CREATE TABLE IF NOT EXISTS widgets (
    //             id SERIAL PRIMARY KEY,
    //             colour TEXT NOT NULL
    //         )
    //     `);

    // --- added columns ----------------------------------------------
    // Fields added AFTER a table already shipped go here, so existing
    // databases pick them up without being recreated. Shape:
    //
    //     await ensureColumn(client, "widgets", "colour", "TEXT");
  } finally {
    client.release();
  }
}

/** Close the pool. Used by seed.js so the script exits instead of hanging. */
async function close() {
  if (pool !== null) {
    await pool.end();
    pool = null;
  }
}

module.exports = { getPool, initDb, ensureColumn, close, DATABASE_URL };
