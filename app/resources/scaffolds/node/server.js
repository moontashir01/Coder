/**
 * {{PROJECT_NAME}} — Express application entry point.
 *
 * ROUTES ONLY. This file defines URL routes and renders views; it does not
 * write SQL. The connection pool and schema live in db.js, and every query
 * lives in models.js. Keeping those three apart is what lets a later change
 * ("add a picture to a product") touch one query helper instead of the whole
 * app.
 *
 * Run it locally with:  npm install && node server.js
 */

"use strict";

const path = require("path");
const express = require("express");
const expressLayouts = require("express-ejs-layouts");
const session = require("express-session");

const db = require("./db");
const models = require("./models");
const ui = require("./ui");

const app = express();
const PORT = process.env.PORT || 3000;

// EJS + a single layout. `views/layout.ejs` owns the <html> document, the nav
// and the footer, and every res.render() is wrapped in it automatically — the
// same contract base.html has on the Flask stack. A view must therefore NEVER
// contain <html>, <head> or its own <nav>: that renders two navbars.
app.set("views", path.join(__dirname, "views"));
app.set("view engine", "ejs");
app.use(expressLayouts);
app.set("layout", "layout");

// The component helpers, available in every view as `ui`. See ui.js.
app.locals.ui = ui;
app.locals.projectName = "{{PROJECT_NAME}}";

// Form posts arrive as application/x-www-form-urlencoded. No body-parser
// package needed — this is built into Express 4.16+.
app.use(express.urlencoded({ extended: false }));
app.use(express.json());

app.use(express.static(path.join(__dirname, "public")));

// Signed cookie sessions, mounted here so `req.session` exists on every route.
// A generated app that signs users in needs somewhere to keep who they are, and
// `req.session` is a property of a parameter — nothing static can see that it
// is undefined, so the failure is a 500 on the first successful login and not
// before. The store is in memory: fine for one process, and the thing to
// replace before this is deployed anywhere real.
app.use(
  session({
    secret: process.env.SESSION_SECRET || "{{SECRET_KEY}}",
    resave: false,
    saveUninitialized: false,
    cookie: { httpOnly: true, sameSite: "lax", maxAge: 1000 * 60 * 60 * 24 },
  })
);

// Whoever is signed in, available to every view as `currentUser` without each
// route having to remember to pass it.
app.use((req, res, next) => {
  res.locals.currentUser = req.session.user || null;
  next();
});

// Static files. `public/css/style.css` holds the components and
// `public/css/theme.css` the colour and font variables; theme.css is linked
// LAST by the layout, so it wins, and it is the only file a restyle touches.

// Express 4 does not forward a REJECTED promise from an `async` handler to the
// error middleware below: the rejection goes unhandled, and Node 15+ treats
// that as fatal. Measured on a generated app — one form posted with an empty
// optional number field returned `invalid input syntax for type integer: ""`
// and the whole site went down, rather than that one request returning 500.
//
// Wrapping the routing methods ONCE, here, covers every route defined after
// this point, including ones added by a later change. A four-argument function
// is error middleware and is left alone.
for (const method of ["get", "post", "put", "patch", "delete", "all"]) {
  const original = app[method].bind(app);
  app[method] = (routePath, ...handlers) =>
    original(
      routePath,
      ...handlers.map((handler) =>
        typeof handler === "function" && handler.length < 4
          ? (req, res, next) =>
              Promise.resolve(handler(req, res, next)).catch(next)
          : handler
      )
    );
}

app.get("/", (req, res) => {
  // Home page. Replace the view body with this project's real content.
  res.render("index", { title: "{{PROJECT_NAME}}" });
});

// 404 — a real page rather than Express's default text, so a wrong link still
// looks like this site.
app.use((req, res) => {
  res.status(404).render("index", {
    title: "Not found",
    notFound: req.originalUrl,
  });
});

// Any error a route throws lands here. Logged in full, shown short: a stack
// trace in the browser is a production leak, and "it worked" is a lie.
app.use((err, req, res, _next) => {
  console.error(err);
  res.status(500).send("Internal Server Error");
});

// Create the database schema and apply any new columns, then serve. The order
// matters: a server that answers 200 while its tables are missing is a build
// that reports success and 500s on every page that shows data.
db.initDb()
  .then(() => {
    app.listen(PORT, () => {
      console.log(`{{PROJECT_NAME}} listening on http://127.0.0.1:${PORT}`);
    });
  })
  .catch((err) => {
    // Loud and fatal, deliberately. Starting anyway would make the smoke test
    // pass on an app whose every data page is broken.
    console.error("Could not initialise the database:", err.message);
    console.error(
      "Start PostgreSQL and create the database, or set DATABASE_URL. See db.js."
    );
    process.exit(1);
  });

module.exports = app;
