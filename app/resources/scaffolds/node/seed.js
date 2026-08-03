/**
 * Demo data for {{PROJECT_NAME}}.
 *
 * An empty site reads as broken even when it is working perfectly, so give
 * every entity a few rows. Run it with:  node seed.js
 *
 * Use `ON CONFLICT DO NOTHING` (or check first) so running it twice does not
 * duplicate anything.
 */

"use strict";

const db = require("./db");

async function seed() {
  const client = await db.getPool().connect();
  try {
    // e.g.
    // await client.query(
    //   "INSERT INTO products (id, title, price) VALUES ($1, $2, $3) " +
    //     "ON CONFLICT DO NOTHING",
    //   [1, "Example product", 9.99]
    // );
  } finally {
    client.release();
  }
}

if (require.main === module) {
  db.initDb()
    .then(seed)
    .then(() => {
      console.log("Seeded the database.");
      return db.close();
    })
    .catch(async (err) => {
      // Reported, never swallowed: a seed that failed silently is a demo whose
      // pages are empty for a reason nobody can see.
      console.error("Seeding failed:", err.message);
      await db.close();
      process.exit(1);
    });
}

module.exports = { seed };
