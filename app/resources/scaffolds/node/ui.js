/**
 * {{PROJECT_NAME}} — the component helpers every page is built from.
 *
 * The Node stack's answer to Flask's `templates/_macros.html`, and it carries
 * the SAME names on purpose (`page_header`, `table`, `card`, `field`, `badge`,
 * `empty_state`, `flash_messages`), so `ui_context()` can say the same thing to
 * the model on both stacks and the two sites stay one product.
 *
 * Why a .js module rather than the `views/_macros.ejs` the plan names: EJS has
 * no macro construct. `<%- include('_macros') %>` renders a partial, it does
 * not export callables — functions defined in an included file do not come back
 * out into the including scope. Plain functions returning HTML give the exact
 * call site Jinja has:
 *
 *     Flask:  {{ ui.table(rows, columns) }}
 *     Node:   <%- ui.table(rows, columns) %>
 *
 * server.js mounts this as `app.locals.ui`, so it is available in every view
 * with no import.
 *
 * These exist for the same reason db.js and models.js are generated rather than
 * written: a table, a form field and an empty state contain no decisions, and
 * hand-writing them per page is how six pages end up with six different tables.
 * Add a helper here rather than pasting markup into a view.
 *
 * Every helper emits only classes that style.css already defines, and escapes
 * every value it interpolates — `<%- %>` does not escape, so the escaping has
 * to happen in here or a product title containing `<script>` runs.
 */

"use strict";

const ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

/** HTML-escape a value. Everything user-supplied goes through this. */
function esc(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** `some_column` -> `Some Column`, for table headers. */
function humanize(name) {
  return String(name || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * An empty list is a finished page, not a broken one. Always render this
 * instead of nothing when a query comes back with no rows.
 */
function empty_state(message, action_url, action_label) {
  const action =
    action_url && action_label
      ? `<a class="button" href="${esc(action_url)}">${esc(action_label)}</a>`
      : "";
  return `<div class="empty"><p>${esc(
    message || "Nothing here yet."
  )}</p>${action}</div>`;
}

/** Page title, with an optional primary action on the right. */
function page_header(title, action_url, action_label, subtitle) {
  const lede = subtitle ? `<p class="lede">${esc(subtitle)}</p>` : "";
  const action =
    action_url && action_label
      ? `<a class="button" href="${esc(action_url)}">${esc(action_label)}</a>`
      : "";
  return `<div class="page-header"><div><h1>${esc(
    title
  )}</h1>${lede}</div>${action}</div>`;
}

/**
 * A list of rows as a table. `columns` are the column names to show, in order;
 * each is looked up on the row, so they must be real column names.
 *
 * The .table-wrap is not optional — it is what stops a wide table from making
 * the whole page scroll sideways on a phone.
 */
function table(rows, columns, empty) {
  const list = rows || [];
  const cols = columns || [];
  if (list.length === 0) {
    return empty_state(empty || "Nothing here yet.");
  }
  const head = cols
    .map((col) => `<th scope="col">${esc(humanize(col))}</th>`)
    .join("");
  const body = list
    .map(
      (row) =>
        "<tr>" + cols.map((col) => `<td>${esc(row[col])}</td>`).join("") + "</tr>"
    )
    .join("");
  return (
    '<div class="table-wrap"><table class="table">' +
    `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>` +
    "</table></div>"
  );
}

/** One item in a grid. `image` is a path under /public. */
function card(title, body, href, image, meta) {
  const media = image
    ? `<div class="card-media"><img src="${esc(image)}" alt="${esc(
        title
      )}" /></div>`
    : "";
  const metaLine = meta ? `<p class="card-meta">${esc(meta)}</p>` : "";
  const text = body ? `<p>${esc(body)}</p>` : "";
  const inner = `${media}<h3>${esc(title)}</h3>${metaLine}${text}`;
  return href
    ? `<a class="card card-link" href="${esc(href)}">${inner}</a>`
    : `<div class="card">${inner}</div>`;
}

/**
 * One labelled form control. `type="textarea"` renders a textarea,
 * `type="file"` an upload.
 *
 * The label is tied to the input with for/id: without that, clicking the label
 * does nothing and a screen reader announces the field unnamed.
 */
function field(name, label, type, value, required, placeholder, hint) {
  const kind = type || "text";
  const req = required ? " required" : "";
  const control =
    kind === "textarea"
      ? `<textarea id="${esc(name)}" name="${esc(name)}" placeholder="${esc(
          placeholder
        )}"${req}>${esc(value)}</textarea>`
      : `<input id="${esc(name)}" name="${esc(name)}" type="${esc(
          kind
        )}" value="${esc(value)}" placeholder="${esc(placeholder)}"${req} />`;
  const hintLine = hint ? `<span class="hint">${esc(hint)}</span>` : "";
  return (
    '<div class="field">' +
    `<label for="${esc(name)}">${esc(label || humanize(name))}</label>` +
    control +
    hintLine +
    "</div>"
  );
}

/** A small coloured label — a status, a category, a count. */
function badge(text, kind) {
  const extra = kind ? ` badge-${esc(kind)}` : "";
  return `<span class="badge${extra}">${esc(text)}</span>`;
}

/**
 * layout.ejs already renders flashed messages on every page. This is here for a
 * page that needs them somewhere other than the top of <main>.
 *
 * `messages` is a list of `{ category, message }`.
 */
function flash_messages(messages) {
  const list = messages || [];
  if (list.length === 0) {
    return "";
  }
  const items = list
    .map(
      (m) =>
        `<div class="alert alert-${esc(
          m.category || "info"
        )}" role="status">${esc(m.message)}</div>`
    )
    .join("");
  return `<div class="flashes">${items}</div>`;
}

module.exports = {
  esc,
  humanize,
  page_header,
  table,
  card,
  field,
  badge,
  empty_state,
  flash_messages,
  // camelCase aliases: the snake_case names above are canonical (they are the
  // Flask macro names, and `ui_context()` names those), but a model writing
  // JavaScript reaches for camelCase by habit and a wrong guess is a 500 on a
  // page that was otherwise correct.
  pageHeader: page_header,
  emptyState: empty_state,
  flashMessages: flash_messages,
};
