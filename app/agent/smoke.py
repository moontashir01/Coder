"""Runtime smoke test — does the generated backend actually RUN?

The intent check reads a file; `check_file` parses it; nothing until now has
*executed* it. This is weaknesses.md #2's last-open item: a `server.py` that
imports a missing module, binds a taken port, or throws at startup passes every
static check and still doesn't run. This module starts the server as a
subprocess, waits to see whether it survives startup, probes it over HTTP on
localhost, then reliably kills the whole process tree.

Deliberately conservative and SAFE, because it executes model-generated code:
- Off by default (`settings.blueprint_smoke_test`), so it's always opt-in.
- Hard timeout; the process tree is killed no matter what (Windows `taskkill /T`,
  POSIX process-group kill).
- Localhost only, no arguments passed to the server, no network env granted.
- "Responded with any HTTP status" counts as up — a 404/405 still proves the
  server is serving; only connection-refused/timeout means it isn't.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_IS_WIN = sys.platform.startswith("win")

# Ports a generated server commonly binds, tried in order after source-detected
# ones. Kept short — each is a ~fast TCP connect attempt.
_COMMON_PORTS = (8000, 5000, 8080, 3000, 8888, 8081, 5001)

# Source patterns that reveal the port the server binds.
_PORT_PATTERNS = (
    re.compile(r"HTTPServer\(\s*\([^,]*,\s*(\d{2,5})\s*\)"),  # (('', 8000))
    re.compile(r"TCPServer\(\s*\([^,]*,\s*(\d{2,5})\s*\)"),
    re.compile(r"\.listen\(\s*(\d{2,5})"),  # node
    # `const PORT = process.env.PORT || 3000` — the idiomatic Node line, and
    # the one the Node scaffold writes. Without it the port is only found in
    # `_COMMON_PORTS`, so the probe tries 8000/5000/8080 first and would attach
    # to whatever else happens to be listening there.
    re.compile(r"process\.env\.PORT\s*(?:\|\||\?\?)\s*(\d{2,5})"),
    re.compile(r"run\([^)]*port\s*=\s*(\d{2,5})", re.IGNORECASE),  # flask
    re.compile(r"uvicorn\.run\([^)]*port\s*=\s*(\d{2,5})", re.IGNORECASE),
    re.compile(r"\bPORT\s*[:=]\s*(\d{2,5})"),
    re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})"),
)


@dataclass(frozen=True)
class ProbeCheck:
    """One functional assertion about the running app.

    ``owner`` says whose defect a failure is, and exists because Phase W5/W6
    checks ride in the same list: a table that scrolls sideways at 390px is
    fixed in a template or the stylesheet, and feeding it to
    `_smoke_repair_instruction` would send the model to rewrite the *server*
    file for a CSS problem. Only `owner == "app"` failures belong to the backend
    repair loop; everything else is repaired by whoever added it.
    """

    label: str
    ok: bool
    detail: str = ""
    owner: str = "app"

    def line(self) -> str:
        return f"  {'ok  ' if self.ok else 'FAIL'} {self.label}" + (
            f" — {self.detail}" if self.detail else ""
        )


@dataclass(frozen=True)
class SmokeResult:
    started: bool  # process survived startup (didn't crash immediately)
    responded: bool  # got an HTTP status back on some port
    status: int | None  # that HTTP status, if any
    port: int | None  # the port that responded
    detail: str  # one-line human summary
    stderr: str = ""  # captured server stderr on a startup crash
    checks: tuple[ProbeCheck, ...] = ()  # functional probe results (Phase 5)

    def failures(self) -> tuple[ProbeCheck, ...]:
        return tuple(c for c in self.checks if not c.ok)

    def note(self) -> str:
        """The line(s) appended to the build answer.

        "It started" and "it works" are different claims, and until Phase 5 only
        the first was ever made — a build whose every POST returned 500 still
        reported a passing smoke test, because any HTTP status counted as alive.
        When the functional probe ran, its result is what gets reported.
        """
        if not self.started:
            return f"may not meet: {self.detail}"
        if not self.checks:
            return f"Smoke test: {self.detail}"

        failed = self.failures()
        head = (
            f"Smoke test: {len(self.checks) - len(failed)}/{len(self.checks)} "
            "functional check(s) passed"
        )
        body = "\n".join(c.line() for c in self.checks)
        if failed:
            return f"may not meet: {head}\n{body}"
        return f"{head}\n{body}"


def detect_ports(source: str) -> list[int]:
    """Ports the server source appears to bind, de-duplicated, most-specific first,
    followed by the common fallbacks."""
    found: list[int] = []
    for pat in _PORT_PATTERNS:
        for m in pat.finditer(source or ""):
            try:
                port = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 1 <= port <= 65535 and port not in found:
                found.append(port)
    for port in _COMMON_PORTS:
        if port not in found:
            found.append(port)
    return found


def _runtime_command(server_file: Path) -> list[str] | None:
    ext = server_file.suffix.lower()
    if ext == ".py":
        return [sys.executable, server_file.name]
    if ext in (".js", ".mjs"):
        return ["node", server_file.name]
    return None


def _http_get(port: int, path: str, timeout: float) -> int | None:
    """One GET to 127.0.0.1:port; return the HTTP status or None if no response."""
    import http.client

    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", path or "/")
        return conn.getresponse().status
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _port_open(port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if _IS_WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def run_smoke_test(
    server_file: Path,
    workdir: Path,
    endpoint_paths: list[str] | None = None,
    timeout: float = 8.0,
    warmup: float = 1.5,
    spec=None,
    on_serving=None,
    adapter=None,
) -> SmokeResult:
    """Start ``server_file`` in ``workdir``, see if it runs, probe it, kill it.

    Synchronous (subprocess + sockets) — call it from async code via
    ``asyncio.to_thread``. Never raises: any failure becomes a SmokeResult.

    ``spec``: a ProjectSpec. When given, the server is not merely pinged — it is
    exercised against its own contract by `functional_probe` (Phase 5), and the
    result carries per-check outcomes. Without it the behaviour is exactly the
    old liveness test, so every existing caller is unaffected.

    ``on_serving``: ``callable(port) -> Sequence[ProbeCheck]``, run once the
    server is answering and before it is killed. This is the door the browser
    checks come through (Phase W5/W6): they need a live server, and starting a
    SECOND one would fight this one for :5000 and for `app.db`. One start, one
    teardown, every probe in between. Best-effort — a hook that raises costs its
    own observations and nothing else.
    """
    server_file = Path(server_file)
    workdir = Path(workdir)
    cmd = _runtime_command(server_file)
    if cmd is None:
        return SmokeResult(False, False, None, None, f"cannot run {server_file.name}")
    if not (workdir / server_file.name).is_file():
        return SmokeResult(
            False, False, None, None, f"{server_file.name} not found to run"
        )

    try:
        source = (workdir / server_file.name).read_text(
            encoding="utf-8", errors="ignore"
        )
    except Exception:
        source = ""
    ports = detect_ports(source)
    paths = list(endpoint_paths or [])
    if "/" not in paths:
        paths.append("/")

    popen_kwargs: dict = dict(
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if _IS_WIN:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError:
        return SmokeResult(
            False, False, None, None, f"runtime for {server_file.name} not installed"
        )
    except Exception as e:
        return SmokeResult(False, False, None, None, f"could not start server: {e}")

    deadline = time.monotonic() + timeout
    try:
        # 1) Did it survive startup? A quick crash (ImportError, syntax at import,
        #    port bind failure) exits within the warmup with a non-zero code.
        time.sleep(min(warmup, timeout))
        if proc.poll() is not None and proc.returncode not in (0, None):
            _, err = _drain(proc)
            first = _first_error_line(err)
            return SmokeResult(
                False,
                False,
                None,
                None,
                f"{server_file.name} crashed on startup ({first})",
                stderr=err[:2000],
            )

        # 2) It's alive (or exited 0). Probe for an HTTP response.
        while time.monotonic() < deadline:
            for port in ports:
                if not _port_open(port):
                    continue
                for path in paths:
                    status = _http_get(port, path, timeout=1.5)
                    if status is None:
                        continue
                    checks: tuple[ProbeCheck, ...] = ()
                    if spec is not None:
                        try:
                            checks = tuple(functional_probe(spec, port))
                        except Exception:
                            checks = ()  # best-effort: never fail a turn here
                        try:
                            # …and the rules, which is the only check in the
                            # pipeline that can fail on a behaviour rather than
                            # on a shape. Same best-effort rule: an exception
                            # here must never cost a turn whose files landed.
                            checks += tuple(
                                behaviour_probe(
                                    spec, port, adapter=adapter, root=workdir
                                )
                            )
                        except Exception:
                            pass
                    if on_serving is not None:
                        try:
                            checks += tuple(on_serving(port) or ())
                        except Exception:
                            pass  # same rule: an observation, never the turn
                    return SmokeResult(
                        True,
                        True,
                        status,
                        port,
                        f"{server_file.name} started; GET {path} -> "
                        f"{status} on :{port}",
                        checks=checks,
                    )
            if proc.poll() is not None:
                break  # server exited while we were probing
            time.sleep(0.4)

        # 3) Survived startup but nothing answered on a known port.
        if proc.poll() is None or proc.returncode == 0:
            return SmokeResult(
                True,
                False,
                None,
                None,
                f"{server_file.name} started but no HTTP response on tried ports "
                f"({', '.join(map(str, ports[:4]))})",
            )
        _, err = _drain(proc)
        return SmokeResult(
            False,
            False,
            None,
            None,
            f"{server_file.name} exited (code {proc.returncode})",
            stderr=err[:2000],
        )
    finally:
        _kill_tree(proc)


# ---------------------------------------------------------------------------
# Functional probe (Phase 5) — does it WORK, not merely answer a socket
# ---------------------------------------------------------------------------

# The value posted and then looked for. Distinctive enough that finding it in
# the HTML cannot be a coincidence.
PROBE_MARKER = "CoderProbe7f3a"


def _png_1x1() -> bytes:
    """A real 1×1 PNG, built with stdlib zlib+struct.

    An IMAGE field needs an actual file: a form posted without one exercises the
    "no file" branch, not the upload. Hand-rolled rather than adding Pillow —
    the agent stays dependency-light and offline.
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit truecolour
    idat = zlib.compress(b"\x00\xff\x00\x00")  # filter byte + one RGB pixel
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _encode_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """`(body, content_type)` for a multipart/form-data POST, stdlib only."""
    boundary = "----CoderProbeBoundary7f3a"
    out: list[bytes] = []
    for name, value in fields.items():
        out.append(f"--{boundary}\r\n".encode())
        out.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.append(f"{value}\r\n".encode())
    for name, (filename, payload, mime) in files.items():
        out.append(f"--{boundary}\r\n".encode())
        out.append(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'.encode()
        )
        out.append(f"Content-Type: {mime}\r\n\r\n".encode())
        out.append(payload)
        out.append(b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# Werkzeug's debug page announces the exception in its <title>. Lifting it turns
# "POST /x failed" into "POST /x -> 500 NameError: name 'Product' is not
# defined", which is a far better repair prompt — and it is the one piece of
# information the model needs to fix the handler.
_DEBUG_TITLE_RE = re.compile(
    r"<title>\s*(?P<err>[^<]*?(?:Error|Exception)[^<]*?)\s*(?://[^<]*)?</title>",
    re.IGNORECASE,
)


def server_error(text: str) -> str:
    """The exception behind a 5xx, when the response reveals one.

    Returns "" for a generic error page: "HTTP 500 — 500 Internal Server Error"
    repeats the status and tells the repair loop nothing it doesn't have.
    """
    match = _DEBUG_TITLE_RE.search(text or "")
    if match:
        title = " ".join(match.group("err").split())[:160]
        return "" if re.match(r"^\d{3}\b", title) else title
    for line in reversed((text or "").splitlines()):
        if re.match(r"\s*\w*(Error|Exception)\b", line):
            return line.strip()[:160]
    return ""


def _is_parameterised(route: str) -> bool:
    """Does this route carry a placeholder segment rather than a real value?

    Both spellings this codebase meets: Express's `/users/:id` and Flask's
    `/users/<int:id>`.
    """
    return ":" in route or "<" in route


def _request(
    port: int,
    method: str,
    path: str,
    timeout: float = 4.0,
    body=None,
    content_type="",
    attempts: int = 2,
) -> tuple[int | None, str]:
    """One HTTP request to localhost. Returns `(status, text)`; status None on error.

    Retried once by default: the dev server occasionally drops a connection
    mid-probe (observed as WinError 10054 on a POST that, repeated on its own,
    answers 500). Reporting "no response" when the truth is a named exception
    loses exactly the detail the repair loop needs.
    """
    import http.client

    last = "no response"
    for attempt in range(max(1, attempts)):
        conn = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            headers = {"Content-Type": content_type} if content_type else {}
            conn.request(method, path or "/", body=body, headers=headers)
            response = conn.getresponse()
            text = response.read(400_000).decode("utf-8", "replace")
            return response.status, text
        except Exception as e:
            last = str(e)
            if attempt + 1 < max(1, attempts):
                time.sleep(0.4)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return None, last


def _sample_value(field) -> str:
    """A postable value for one field, distinctive where it needs to be."""
    name = (field.name or "").lower()
    if field.type == "REAL":
        return "19.99"
    if field.type == "INTEGER":
        return "3"
    if "email" in name:
        return f"{PROBE_MARKER}@example.com"
    return f"{PROBE_MARKER} {field.name}"


def _post_form(port: int, path: str, fields: dict, timeout: float):
    """POST a urlencoded form. Returns (status, body)."""
    from urllib.parse import urlencode

    return _request(
        port,
        "POST",
        path,
        timeout,
        body=urlencode(fields).encode(),
        content_type="application/x-www-form-urlencoded",
    )


def _write_path_for(spec, table: str) -> str:
    """The POST route that creates a row in ``table``, or "".

    Read off the spec's own endpoints, by entity first and by path second. A
    guess would post the probe's payload at some unrelated handler and report
    its 500 as a broken rule.
    """
    endpoints = list(getattr(spec, "endpoints", ()) or ())
    entity = spec.entity(table)
    names = {table.lower()}
    if entity is not None:
        names.add(entity.name.lower())
    for endpoint in endpoints:
        if endpoint.method != "POST":
            continue
        if (endpoint.entity or "").lower() in names:
            return endpoint.path
    for endpoint in endpoints:
        if endpoint.method != "POST" or _is_parameterised(endpoint.path):
            continue
        if any(name in endpoint.path.lower() for name in names):
            return endpoint.path
    return ""


def _child_of(spec, table: str):
    """The entity whose rows point AT ``table`` and carry an amount.

    A rule about auctions is enforced when a *bid* is posted, not when an item
    is. The link is the foreign key, which `Field.references` already records.
    """
    from app.agent import rules as _rules

    for entity in getattr(spec, "entities", ()) or ():
        if entity.table == table:
            continue
        points_at = any(
            (f.references or "").split("(", 1)[0].strip() == table
            for f in entity.fields
        )
        if points_at and _rules.amount_column(entity):
            return entity
    return None


def _child_fields(child, parent_table: str, parent_id) -> dict:
    """A plausible body for creating one ``child`` row against ``parent_id``."""
    fields = {f.name: _sample_value(f) for f in child.fields if not f.pk}
    for f in child.fields:
        if (f.references or "").split("(", 1)[0].strip() == parent_table:
            fields[f.name] = str(parent_id)
    return fields


def behaviour_probe(
    spec, port: int, adapter=None, root=None, timeout: float = 4.0
) -> list[ProbeCheck]:
    """Exercise the spec's RULES against the running app.

    The gap this closes was measured end to end on the OpenBazaar build: a
    `/bids/new` that stores a bid without comparing it to
    `current_highest_bid + bid_increment_step` parses, routes, renders 200 and
    persists — identical under every check that existed to one that enforces the
    rule. With no failing check there is no error text, and Coder's repair loop
    is check → error → regenerate. It fixed dozens of structural defects on that
    build precisely because those had checks; the behaviours had none, so they
    were never written and nothing ever said so.

    Two shapes are exercised, the two `rules.classify_rule` will label:

    * `min_increment` — post a value that does NOT clear the step and require
      the app to refuse it. "Refuse" is a 4xx **or** no new row: a handler that
      answers 302 and silently drops the bid is enforcing it too.
    * `extend_deadline` — move one row's deadline to a minute from now, post a
      valid child, and require the deadline to have moved.

    **Every uncertainty is a stated skip, never a failure.** No adapter, no
    database, no rows, no write route, an unreadable column: each means the
    check could not run, and reporting one as a broken rule would send
    `_smoke_repair_instruction` at code that works — `functional_probe` step 3's
    lesson, which cost a live build a rewrite of a correct file.
    """
    from app.agent import rules as _rules

    checks: list[ProbeCheck] = []
    rules = list(getattr(spec, "rules", ()) or ())
    if not rules:
        return checks

    run_sql = getattr(adapter, "run_sql", None) if adapter is not None else None
    for rule in rules:
        label = f"RULE {rule.entity}: {rule.trigger[:48]}"
        entity = spec.entity(rule.entity)
        # Classify here when the stored rule carries no label: a spec written
        # before `kind` existed, or one built in code, is still checkable, and
        # the entity is in hand now — which is what makes the answer better than
        # the one `_load_rules` could give without it.
        kind = rule.kind or _rules.classify_rule(rule, entity)
        if not kind:
            continue
        if entity is None:
            checks.append(ProbeCheck(label, True, "not probed - no such table"))
            continue
        if run_sql is None or root is None:
            checks.append(
                ProbeCheck(
                    label, True, "not probed - no database access on this stack"
                )
            )
            continue

        if kind == _rules.KIND_MIN_INCREMENT:
            checks.append(
                _probe_min_increment(spec, rule, entity, port, run_sql, root, timeout)
            )
        elif kind == _rules.KIND_EXTEND_DEADLINE:
            checks.append(
                _probe_extend_deadline(spec, rule, entity, port, run_sql, root, timeout)
            )
    return checks


def _probe_min_increment(spec, rule, entity, port, run_sql, root, timeout):
    """A value that does not clear the step must be refused."""
    from app.agent import rules as _rules

    label = f"RULE {entity.table}: a value below the increment is refused"
    amount = _rules.amount_column(entity)
    step = _rules.increment_column(entity)
    child = _child_of(spec, entity.table)
    if not (amount and step and child is not None):
        return ProbeCheck(label, True, "not probed - the columns this needs are absent")
    path = _write_path_for(spec, child.table)
    if not path:
        return ProbeCheck(label, True, "not probed - no route creates a row here")

    rows = run_sql(
        root,
        f"SELECT id, {amount} AS amount, {step} AS step FROM {entity.table} LIMIT 1",
    )
    if not rows:
        return ProbeCheck(label, True, "not probed - could not read the table")
    row = rows[0]
    try:
        current = float(row.get("amount") or 0)
    except (TypeError, ValueError):
        return ProbeCheck(label, True, "not probed - the value is not a number")

    before = run_sql(root, f"SELECT count(*)::int AS n FROM {child.table}")
    if not before:
        return ProbeCheck(label, True, "not probed - could not count the rows")

    fields = _child_fields(child, entity.table, row.get("id"))
    child_amount = _rules.amount_column(child)
    if child_amount:
        # Equal to the current value, so it does NOT clear the step.
        fields[child_amount] = str(current)
    status, _body = _post_form(port, path, fields, timeout)

    after = run_sql(root, f"SELECT count(*)::int AS n FROM {child.table}")
    if not after:
        return ProbeCheck(label, True, "not probed - could not re-count the rows")
    stored = int(after[0].get("n") or 0) > int(before[0].get("n") or 0)
    if status is not None and status >= 500:
        # A crash is not a refusal. The route erroring is `functional_probe`'s
        # finding and it reports it already; counting it here would turn a
        # broken handler into evidence that the rule is enforced — the most
        # expensive kind of false pass, because it hides the defect behind a
        # green check.
        return ProbeCheck(
            label, True, f"not probed - the route errored (HTTP {status})"
        )
    if status is not None and 400 <= status < 500:
        return ProbeCheck(label, True, f"refused with HTTP {status}")
    if not stored:
        return ProbeCheck(label, True, "refused - nothing was written")
    return ProbeCheck(
        label,
        False,
        f"POST {path} accepted {child_amount}={current} while "
        f"{entity.table}.{amount} was already {current} and "
        f"{entity.table}.{step} must be cleared - the rule is not enforced "
        f"({rule.effect[:70]})",
    )


def _probe_extend_deadline(spec, rule, entity, port, run_sql, root, timeout):
    """An action close to the deadline must push the deadline out."""
    from app.agent import rules as _rules

    label = f"RULE {entity.table}: a late action moves the deadline"
    deadline = _rules.deadline_column(entity)
    child = _child_of(spec, entity.table)
    if not (deadline and child is not None):
        return ProbeCheck(label, True, "not probed - the columns this needs are absent")
    path = _write_path_for(spec, child.table)
    if not path:
        return ProbeCheck(label, True, "not probed - no route creates a row here")

    rows = run_sql(root, f"SELECT id FROM {entity.table} LIMIT 1")
    if not rows:
        return ProbeCheck(label, True, "not probed - could not read the table")
    row_id = rows[0].get("id")
    # One demo row is moved to a minute from now so the rule's window is real.
    # The probe already inserts rows through the app; this is the same licence,
    # and the value is a bound parameter, never interpolated.
    moved = run_sql(
        root,
        f"UPDATE {entity.table} SET {deadline} = now() + interval '60 seconds' "
        "WHERE id = $1 RETURNING id",
        [row_id],
    )
    if not moved:
        return ProbeCheck(label, True, "not probed - could not set up the deadline")

    amount = _rules.amount_column(entity)
    step = _rules.increment_column(entity)
    bump = 1.0
    if amount and step:
        current = run_sql(
            root,
            f"SELECT {amount} AS amount, {step} AS step FROM {entity.table} "
            "WHERE id = $1",
            [row_id],
        )
        if current:
            try:
                bump = (
                    float(current[0].get("amount") or 0)
                    + float(current[0].get("step") or 0)
                    + 1
                )
            except (TypeError, ValueError):
                bump = 1.0

    fields = _child_fields(child, entity.table, row_id)
    child_amount = _rules.amount_column(child)
    if child_amount:
        fields[child_amount] = str(bump)
    _post_form(port, path, fields, timeout)

    after = run_sql(
        root,
        f"SELECT extract(epoch from ({deadline} - now()))::int AS left_s "
        f"FROM {entity.table} WHERE id = $1",
        [row_id],
    )
    if not after:
        return ProbeCheck(label, True, "not probed - could not re-read the deadline")
    try:
        left = int(after[0].get("left_s") or 0)
    except (TypeError, ValueError):
        return ProbeCheck(label, True, "not probed - the deadline is not a time")
    if left > 70:
        return ProbeCheck(label, True, f"extended - {left}s left after the bid")
    return ProbeCheck(
        label,
        False,
        f"a bid one minute before {entity.table}.{deadline} left it at {left}s - "
        f"the rule is not enforced ({rule.effect[:70]})",
    )


def functional_probe(spec, port: int, timeout: float = 4.0) -> list[ProbeCheck]:
    """Exercise the running app against its own contract.

    Three kinds of assertion, in the order that makes a failure legible:

    1. **Every page in the spec renders.** 2xx and a non-empty body — a 200 that
       returns nothing is not a working page.
    2. **Every write endpoint accepts a real submission.** The body is
       synthesised from the entity's fields, including a genuine 1×1 PNG for an
       IMAGE field, posted as multipart so the upload branch is actually taken.
       Anything below 500 counts: a 302 redirect, a 200, even a 400 the app
       chose — a 5xx is the app breaking.
    3. **The posted value comes back.** The listing page is fetched again and
       the marker must appear in the HTML.

    Step 3 is the whole point of the phase. It is the difference between "the
    server started" and "adding a product works", and it is the only one of the
    three that a build with a silently-failing INSERT cannot pass.

    Pure HTTP against localhost; the caller owns the process. Never raises.
    """
    checks: list[ProbeCheck] = []
    if spec is None:
        return checks

    # 1. every page renders
    for page in getattr(spec, "pages", ()) or ():
        route = page.route or ""
        if not route.startswith("/"):
            continue
        if _is_parameterised(route):
            # `/users/:id` is a PATTERN, not a URL. Fetching it literally asks
            # for the user whose id is the string ":id", and the app correctly
            # answers 404 — reported here as a broken page, which then sends
            # the repair loop at code that works. Measured on the OpenBazaar
            # build: four detail pages, all of them fine, all of them failing.
            # Stated rather than skipped silently: a check that did not run has
            # verified nothing.
            checks.append(
                ProbeCheck(
                    f"GET {route}",
                    True,
                    "not probed - the route takes an id, and this check has no "
                    "real one to put in it",
                )
            )
            continue
        status, text = _request(port, "GET", route, timeout)
        if status is None:
            checks.append(
                ProbeCheck(f"GET {route}", False, f"no response ({text[:60]})")
            )
        elif status >= 400:
            checks.append(ProbeCheck(f"GET {route}", False, f"HTTP {status}"))
        elif len(text.strip()) < 20:
            checks.append(ProbeCheck(f"GET {route}", False, "empty page body"))
        else:
            checks.append(ProbeCheck(f"GET {route}", True, f"{status}"))

    # 2 + 3. every write endpoint accepts a submission, and it comes back
    for endpoint in getattr(spec, "endpoints", ()) or ():
        if endpoint.method != "POST":
            continue
        entity = spec.entity(endpoint.entity) if endpoint.entity else None
        fields: dict = {}
        files: dict = {}
        if entity is not None:
            for field in entity.fields:
                if field.pk and field.type == "INTEGER":
                    continue
                if field.is_upload():
                    files[field.name] = (
                        f"{PROBE_MARKER}.png",
                        _png_1x1(),
                        "image/png",
                    )
                else:
                    fields[field.name] = _sample_value(field)
        if not fields and not files:
            continue  # nothing sensible to post

        if files:
            body, content_type = _encode_multipart(fields, files)
        else:
            from urllib.parse import urlencode

            body = urlencode(fields).encode()
            content_type = "application/x-www-form-urlencoded"

        status, text = _request(
            port, "POST", endpoint.path, timeout, body=body, content_type=content_type
        )
        label = f"POST {endpoint.path}"
        if status is None:
            checks.append(ProbeCheck(label, False, f"no response ({text[:60]})"))
            continue
        if status >= 500:
            why = server_error(text)
            checks.append(
                ProbeCheck(
                    label, False, f"HTTP {status}" + (f" — {why}" if why else "")
                )
            )
            continue
        checks.append(ProbeCheck(label, True, f"{status}"))

        # 3. does it come back? THE point of the phase.
        #
        # Checked against EVERY page, not only those whose `reads` names the
        # entity. `reads` is inferred from the blueprint's prose and is often
        # empty on exactly the listing page that matters — measured live, the
        # storefront `/` had `reads: ()` while the *form* page `/add_product`
        # was tagged, so probing only the tagged page reported a failure for a
        # row that had persisted perfectly well. "Did what I posted appear
        # anywhere in the app?" is the question worth asking, and a false
        # failure here is worse than none: it sends the repair loop off to
        # rewrite code that works.
        if entity is None:
            continue
        seen_on = ""
        for page in getattr(spec, "pages", ()) or ():
            route = page.route or ""
            if not route.startswith("/"):
                continue
            _, listing = _request(port, "GET", route, timeout)
            if PROBE_MARKER in (listing or ""):
                seen_on = route
                break
        checks.append(
            ProbeCheck(
                f"{entity.name} comes back after POST",
                bool(seen_on),
                (
                    f"visible on {seen_on}"
                    if seen_on
                    else "the posted value is not shown on any page"
                ),
            )
        )
    return checks


def _drain(proc: subprocess.Popen) -> tuple[str, str]:
    try:
        out, err = proc.communicate(timeout=3)
        return (out or ""), (err or "")
    except Exception:
        return "", ""


def _first_error_line(stderr: str) -> str:
    """The most informative line of a Python traceback: the final Error line."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if re.match(r"\w*(Error|Exception)\b", ln) or ":" in ln and "Error" in ln:
            return ln[:160]
    return lines[-1][:160] if lines else "no output"
