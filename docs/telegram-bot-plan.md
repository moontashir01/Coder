# Telegram bot + concurrent front-ends — implementation plan

Goal: satisfy the Demo-2 brief. Coder gains a second front-end (a Telegram bot) with real
authentication and authorization, both front-ends can drive the **same** project at the same time
without corrupting it, and two different projects can be driven independently. The brief also
requires two built applications and the full prompt/reasoning history for each, so transcript
export is part of the work, not an afterthought.

## What Coder is today, and why that shapes the plan

Three facts decide the whole design.

1. **`settings` is a process-global mutable singleton.** `sandbox_root`, `web_stack`,
   `auto_approve` and `denied_permissions` are read at call time from one `Settings` object.
   Two projects served concurrently from one process would fight over the path jail — a
   silent, security-relevant failure, not a cosmetic one.
2. **`AgentCore` carries per-turn state.** `chat()` resets `_blueprint`, `_spec_doc` and
   `_entry_routes` at the top of every turn and threads `_last_write_path` through the write
   paths. Two turns interleaved on one `AgentCore` would read each other's state: a build's
   verification would run against another turn's blueprint.
3. **Enforcement already lives below the prompt.** The executor's permission gate, the
   approval hook, the path jail and the shell denylist are the layers that actually stop a
   tool from running. Bot authorization must be expressed *in those layers*, never as prompt
   text — the same argument that makes `.coder/INSTRUCTIONS.md` safe.

So: the bot is a **transport**, the concurrency seam is a **session registry with per-project
serialization**, and roles are **projections onto the existing permission settings**.

## Status

- **T0 — done.** `app/memory/turnlog.py`, `turn_events`, `/export`, `settings.record_turns`.
- **T1 — done.** `app/agent/sessions.py`, `projectlock.py`, `scope.py`, WAL on `.coder.db`.
  One correction to the plan below, found by a test and worth recording: pinning
  `settings.sandbox_root` per turn is **not** enough for two projects at once. The pin is a
  process global, so the second turn's pin moves the first turn's jail while it is still
  running, and the per-project lock cannot help because different projects are meant to
  overlap. The scope is a `ContextVar` (`app/agent/scope.py`) instead; the setting remains the
  fallback for a single front-end.
- **T2 — done.** `app/bot/{render,transport,service,telegram_bot}.py`, the `telegram = [...]`
  extra, `tests/test_bot.py`. Shipped with the allowlist enforced (deny by default) rather
  than with no authorization at all, so the bot is never open between T2 and T3. Entry points
  (`--bot`, `--bot-only`, `/bot`) are still T4, so nothing starts it yet.
- **T3 — done.** `app/bot/auth.py`, `app/bot/audit.py`, `bot_users` / `bot_pairings`, roles
  enforced through `scope.effective_denied_permissions()` at the executor. One extension to
  the plan: pairing codes live in the DATABASE rather than in the minting process's memory,
  because `--bot-only` is a different process from the REPL and an in-memory code could only
  ever be redeemed against the process that minted it.
- **T4 — done.** `coder --bot`, `coder --bot-only`, `/bot start|stop|status|pair`,
  `sessions.session_registry()`, `tests/test_bot_runmodes.py`. The load-bearing change was not
  an entry point: the REPL's own turn now runs through `registry.turn(...)`, so the CLI takes
  the project lock. Until that landed, every "the bot waits for the terminal" claim was false
  in the direction that matters — the terminal never announced itself. A second fix found here:
  a bot turn must RESTORE the executor's approval hook rather than clear it, because in the
  embedded mode both front-ends share one executor and no hook means allow.
- T5 — not started.

## Phase T0 — the transcript the brief asks for

`conversation_turns` stores role/content per `session_id`, and nothing else. The tool trace and
the routing decision — which is where the "reasoning" actually is — are returned by `chat()` and
then discarded. A transcript built from what is stored today would show questions and answers
and none of the work.

- New table `turn_events(session_id, turn_id, source, task_type, flow, tool_trace_json,
  files_written, duration_ms, created_at)`. Written best-effort at the `chat()` seam, next to
  `memory.add_ai` — the same rule the spec save follows: a transcript that will not write must
  never cost a turn whose files landed.
- `source` is `"cli"` or `"telegram:<user_id>"`. Without it a joint session reads as one actor
  and the video's central claim is unverifiable from the record.
- `/export [--session ID] <file.md>` renders a readable Markdown transcript: every turn, the
  route it took, the tools it ran, the files it wrote. This is the deliverable file for both
  applications.

## Phase T1 — the concurrency seam (before any bot code)

The bot is only a second caller. Build the seam first; then "the app and the bot at the same
time" is a property of the seam, not of the bot.

**`app/agent/sessions.py` — `SessionRegistry`.** Keyed by the *resolved* project path. Each
entry holds one `AgentCore`, one `asyncio.Lock`, the conversation `session_id`, and a last-used
stamp.

- **One `AgentCore` per project, shared by every front-end on that project.** Two cores on one
  project would each hold their own `_spec`, and turn 2's amendment would plan against turn 1's
  stale memory — exactly the staleness the multi-turn eval suite exists to catch.
- **Turns are serialized per project** (`async with entry.lock`). Different projects hold
  different locks and run concurrently.
- **The turn's sandbox root is scoped, not pinned** (`project_settings` →
  `app/agent/scope.py`). The plan said "pinned under the lock, restored in a `finally`"; that
  was written and it fails, because the pin is itself a process global and two turns on two
  projects legitimately overlap. It is a `ContextVar` — each asyncio Task carries its own copy —
  and `settings.sandbox_root` stays the fallback for a single front-end. `web_stack` is not
  scoped at all: it is a user preference, and the project's own spec already outranks it per
  turn (`_select_stack`).
- Idle entries are closed (`AgentCore.close()` stops the watcher) after a timeout, so a
  long-running bot does not hold a watchdog observer per project it ever saw.

**Cross-process: `<project>/.coder/coder.lock`.** The REPL in one terminal and a `--bot-only`
daemon in another are two processes; an `asyncio.Lock` cannot see across them.

- An advisory lock file holding `{pid, front_end, started_at, message}`, taken around a turn and
  released in a `finally`.
- **A held lock makes the other front-end wait and say who holds it** — "the CLI is running a
  turn (started 8s ago)" — never fail silently and never barge. A second writer would interleave
  `write_file` calls with `_verify_and_repair` reads on the same file.
- **Stale locks are reclaimed by PID liveness**, not by age alone: a build turn legitimately runs
  for minutes, so a timeout-only rule would break the exact case this protects.
- `.coder/` is already a dot-directory, so the lock is never indexed, never adopted as a spec
  file and never picked as an edit target.

**SQLite.** `.coder.db` gets WAL mode and a busy timeout; two processes appending turns to the
same session are otherwise a `database is locked` on the write that records the answer.

**Tests (offline, `ScriptedLLM`).**

- two concurrent `chat()` calls on one project observe serialized entry/exit;
- two projects run concurrently and each turn sees its own `sandbox_root`;
- a turn that raises still releases both locks and restores settings;
- a stale lock whose PID is dead is reclaimed; one whose PID is alive is not.

## Phase T2 — the bot transport (`app/bot/`)

- `telegram_bot.py` — long polling (`python-telegram-bot`). **No webhook**: a webhook needs an
  inbound public port and a certificate, which is a worse security story and undemonstrable on a
  laptop.
- `render.py` — answer to Telegram: code fences to `<pre>`, 4096-character chunking on line
  boundaries, diffs truncated the way the REPL truncates them.
- `commands.py` — the slash commands that make sense remotely: `/load /project /spec /plan /run
  /undo /stack /history /export /whoami /help`. `/mcp` and `/model` are owner-only.
- **Streaming** reuses `chat(on_token=…)`: buffer tokens and edit one message about every 1.5s,
  which is inside Telegram's edit rate limit. Same seam the REPL's `Live` region uses.
- **Progress** comes from the existing `AgentCore.status_hook` (`[vision] Analyzing …`, scaffold
  and smoke lines) plus a periodic `typing` action, because a build turn is minutes long and a
  silent bot reads as a dead one.
- **Approvals** are the existing `executor.approval_hook`, answered by an inline keyboard
  (Allow / Allow for session / Deny). **A timeout is a Deny** — an unanswered write must never
  proceed on the theory that nobody objected.
- New settings: `telegram_enabled` (default False), `telegram_token` (env only, never logged or
  echoed back), `telegram_allowed_users`, `telegram_poll_timeout`,
  `telegram_max_concurrent_turns`, `telegram_turn_timeout`, `bot_audit_log`.
- `python-telegram-bot` is a new dependency and it talks to the network. State it plainly in the
  README: **the model stays local — nothing about generation reaches the network — but the chat
  channel is Telegram's**, so file contents shown in a reply do leave the machine. That is a
  property of the requested feature, not a defect, and it is off by default.

## Phase T3 — authentication and authorization

**Identity is the Telegram numeric `user_id`, never the `@username`** — usernames are
reassignable, so an allowlist of names is an impersonation surface.

**Bootstrap.** `TELEGRAM_ALLOWED_USERS` in `.env` is the root allowlist. **Empty means nobody** —
an unconfigured bot answers "not authorized" to everyone. Deny-by-default is the same rule as "a
skipped check must never read as a pass".

**Pairing.** `/bot pair` in the REPL prints a one-time code (single use, 5-minute TTL, stored
hashed). `/login <code>` in Telegram binds that `user_id` to a role in a new `bot_users` table in
`.coder.db`. So the person at the machine grants access; the bot never grants it to itself.

**Roles are projections onto settings that already exist.** No new enforcement layer:

| Role | How it is enforced |
| --- | --- |
| `viewer` | `denied_permissions` includes `fs:write`, `fs:delete`, `shell` — the executor hard-refuses before the tool runs |
| `developer` | approval gate on `fs:write` / `fs:delete` / `shell`, routed to the inline keyboard |
| `owner` | may `/load` another project, `/model`, `/mcp`, and allow-for-session |

Consequences worth stating, because they are what makes this real: a role cannot widen the path
jail (`allow_outside_root` is a process flag, not a chat command), cannot reach outside
`sandbox_root`, and cannot lift the shell denylist. The prompt layer is not part of the
authorization story at all.

**Audit.** Every bot turn appends `{when, user_id, role, chat_id, project, message,
files_written, approvals}` to `bot_audit.log`. `/whoami` prints the caller's own id and role,
which is what makes the demonstration checkable on camera.

**Rate limiting.** A per-user token bucket, plus `telegram_max_concurrent_turns` across the
process. A build turn is expensive; an unbounded queue is a denial of service on a single-GPU
machine.

**Tests.** Offline, against a fake Bot: an unknown id is refused; an expired or reused pairing
code is refused; a `viewer` write is refused *by the executor*, not by the bot's own check
(assert the refusal comes from the permission gate); an unanswered approval denies.

## Phase T4 — the two run modes the brief asks about

- **Same project, both front-ends: `coder --bot`.** The REPL starts the bot in its own event
  loop, sharing one `SessionRegistry`, hence one `AgentCore` and one `asyncio.Lock` for the
  loaded project. The REPL prints bot turns inline (`[telegram] @user: add a level 3`), so a
  single screen recording shows both actors and the serialization.
- **Different projects: `coder --bot-only`.** A headless daemon; each chat picks its project with
  `/load`, and the registry gives each one its own lock and sandbox. Demonstrated by running two
  chats (or the daemon plus a separate REPL on another folder) and showing both turns progressing
  at once with no cross-writes.
- `/bot start|stop|status|pair` in the REPL for the embedded case.

## Phase T5 — the deliverables

**Application 1 — browser asteroid game (3 levels, 3D, sound, 5 ships).** Built by Coder, and two
of Coder's own rules bite here:

- `strip_external_assets` removes CDN `<script src="http…">` in stage 0, so a three.js CDN tag is
  deleted from every page. Either vendor `three.module.js` into the project as a local file and
  prompt for an import map, or run that one build with `ALLOW_NETWORK=true`. Vendoring is the
  better demo — it is consistent with the offline promise.
- Missing **binary** assets are reported, never fabricated, so `.mp3`/`.wav` sound effects would
  come out as a report plus a dead reference. Synthesize the effects with WebAudio
  oscillators and noise buffers instead: no binary assets, works offline, and it is a legitimate
  technique rather than a workaround.
- Route it away from the full-stack gate with `wants_static_only()` wording ("static only, no
  backend") so `should_blueprint` plans a multi-page static build rather than an Express app.

**Application 2** is the Demo-1 application, rebuilt or amended through the same flow.

**History.** `/export` per application (Phase T0), committed next to the code.

**Video.** Three segments, matching the brief: (1) `/whoami`, a refused unknown user, then
pairing and a role-limited write refusal; (2) an ordinary build driven from Telegram end to end;
(3) the app and the bot on one project — a bot turn waiting on the CLI's lock, with the "who
holds it" message visible — then two projects progressing at once.

## Order of work

T0 → T1 → T2 → T3 → T4 → T5. T1 before T2 is the load-bearing ordering: written the other way
round, the bot gets its own `AgentCore` "just to get it working", and every concurrency guarantee
above becomes a retrofit.

## What this plan deliberately does not do

- No web dashboard, no multi-tenant server, no per-user sandboxes on one project.
- No second permission system. If a role needs a capability it is expressed in
  `denied_permissions` or the approval gate, or it does not exist.
- No relaxation of the path jail or the shell denylist for remote callers — a remote caller is
  strictly less trusted than the terminal, never more.
