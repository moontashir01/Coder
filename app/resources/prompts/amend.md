You are changing a project that ALREADY EXISTS. Its current contract is given
below. Your job is to describe **only what changes** — not to restate the
project, and not to redesign it.

Reply with ONLY a JSON object, no prose and no markdown fences, in exactly this
shape. Every key is optional: include a key only when the request actually
changes that thing, and use `[]` or omit it otherwise.

{
  "summary": "<one short phrase describing the change>",
  "entities": [
    {"name": "<existing or new entity>",
     "table": "<table name, only for a NEW entity>",
     "add_fields": [{"name": "<column>", "type": "TEXT|INTEGER|REAL|IMAGE", "required": false}]}
  ],
  "endpoints": [
    {"method": "POST", "path": "/admin/widgets",
     "request": "{title, price, image}", "response": "302 -> /admin/widgets",
     "template": "templates/admin_widgets.html", "entity": "widget"}
  ],
  "pages": [
    {"route": "/admin/widgets", "template": "templates/admin_widgets.html",
     "nav_label": "Admin", "purpose": "form to add a widget", "reads": ["widget"]}
  ],
  "new_files": [
    {"filename": "templates/admin_widgets.html",
     "instruction": "<what this NEW file must contain>"}
  ]
}

Rules:

- **Only the delta.** Do not repeat tables, routes or pages that the contract
  below already lists. If the request adds a field to an existing entity, list
  that entity with `add_fields` only — not its existing columns.

- **Do NOT list existing files to edit.** You are not asked which files break;
  that is worked out from the contract automatically, and far more reliably than
  by guessing. `new_files` is for files that do not exist yet, and nothing else.

- **Reuse the exact names in the contract.** The same table names, the same
  route paths, the same entity names. A change that renames things silently is
  the single most damaging thing you can do here.

- **A picture/photo/image field is `"type": "IMAGE"`.** It stores the path to an
  uploaded file. The upload handling is added for you.

- **A page needs a route and a template.** If the request adds a page, give it
  both, plus a `nav_label` so it can be linked from the shared navigation.

- **A form that submits needs an endpoint.** If the new page has a form, add the
  `POST` endpoint it submits to, and name that page as its `template`.

- Keep it minimal: the smallest set of changes that satisfies the request. If the
  request changes nothing structural, return `{"summary": "..."}` and nothing
  else.

- **The shapes above are a FORMAT, not a suggestion.** `admin_widgets`,
  `/admin/widgets` and `product` are there to show where each value goes.
  Never copy one into your answer: use the names this request and the contract
  below actually use.

- Output ONLY the JSON. No prose, no markdown fences.
