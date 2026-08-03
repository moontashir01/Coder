/**
 * Database queries for {{PROJECT_NAME}} — one small function per operation.
 *
 * Every query lives here, so a route in server.js stays three lines long and a
 * schema change touches one file. Follow this shape for each entity
 * (list / get / create / update / delete):
 *
 *     async function listProducts() {
 *       const { rows } = await getPool().query(
 *         "SELECT * FROM products ORDER BY id DESC"
 *       );
 *       return rows;
 *     }
 *
 *     async function getProduct(id) {
 *       const { rows } = await getPool().query(
 *         "SELECT * FROM products WHERE id = $1",
 *         [id]
 *       );
 *       return rows[0] || null;
 *     }
 *
 *     async function createProduct(title, price) {
 *       const { rows } = await getPool().query(
 *         "INSERT INTO products (title, price) VALUES ($1, $2) RETURNING id",
 *         [title, price]
 *       );
 *       return rows[0].id;
 *     }
 *
 * ALWAYS pass values as `$1, $2, …` parameters, as above. Never build SQL by
 * concatenating or templating a value into the string — that is how SQL
 * injection happens, and the parameterised form is shorter anyway.
 *
 * Note `RETURNING id`: PostgreSQL has no `lastrowid`, so an insert that needs
 * to know the new id must ask for it.
 */

"use strict";

// eslint-disable-next-line no-unused-vars
const { getPool } = require("./db"); // used by the query helpers added below

module.exports = {};
