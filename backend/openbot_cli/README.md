# OpenBot Terminal Client

A command-line frontend for an running OpenBot server — a terminal alternative to
the web UI. It reuses the **existing** OpenBot REST + Server-Sent-Events API
(`/api/v1/...`); it does not invent a new protocol.

## Install

The client lives in the backend package. From the `backend/` directory:

```bash
uv pip install -e .        # or: pip install -e .
```

This installs the `openbot` console script (a click group named `openbot`).

Runtime deps: `httpx` (already in the backend) and `click>=8.1`.

## Connect

Save the server URL once (optional — every command also accepts `--base-url` /
`--api-key`, or the `OPENBOT_URL` / `OPENBOT_API_KEY` env vars):

```bash
openbot connect http://127.0.0.1:8000 --api-key YOUR_KEY
```

The config is stored at `~/.config/openbot-cli/config.json`.

## Commands

| Command | What it does |
| --- | --- |
| `openbot connect <url> [--api-key KEY]` | Save server URL (and optional key) for later commands. |
| `openbot threads [list]` | List threads (title, last activity, default bot, active flag). |
| `openbot thread <id> [watch]` | Print a thread's messages; add `watch` to follow live SSE events. |
| `openbot send <thread> "<msg>" [--bot @handle] [--wait]` | Post a message; `--bot` (repeatable) `@`-mentions a bot, `--wait` prints the replies once the runs are done. |
| `openbot bots` | List bots (handle, name, active/disabled state, description). |
| `openbot runs [<thread>]` | List runs; pass a `<thread>` id to scope to one thread. |
| `openbot run <id>` | Show one run and its event log; accepts the prefix `runs` shows. |
| `openbot api [-X METHOD] <path> [-f/-F key=value] [-d JSON\|@file\|@-]` | Send any request to the server API and print the JSON response. |
| `openbot plugins [--refresh]` | List active plugins and their generated commands. |
| `openbot <plugin> <command> ...` | Commands generated from a plugin's HTTP API, e.g. `openbot kanban list-cards <board>`. |

Every command except `api` accepts `--json` to emit the raw API payload; `api`
always prints the response as it comes.

## Examples

```bash
# List threads and pick one
openbot threads

# Read a thread
openbot thread 9f3a1c2e

# Follow it live (prints new messages + run progress as they arrive)
openbot thread 9f3a1c2e watch

# Ask the default bot a question; @-mention the engineer bot explicitly
openbot send 9f3a1c2e "Refactor cli.py and run the tests" --bot @engineer

# See what the bots are doing
openbot bots
openbot runs 9f3a1c2e
```

## Streaming / `watch`

`thread <id> watch` opens an SSE connection to `/api/v1/events?thread_id=<id>`
and prints, as they arrive:

- new `message.created` events (formatted like a normal thread message),
- `run.updated` status lines,
- `run.event` streaming text deltas (printed inline, like a typing bot).

Press `Ctrl+C` to disconnect.

## Scripts and agents

- `--json` on any command prints the raw API payload, and makes errors a JSON
  object on stderr: `{"error": ..., "exit_code": 1, "status": 404}` (`status`
  only for HTTP errors).
- `send --wait` blocks until every addressed bot has finished a run started by
  the message and the thread has stayed quiet for two polls, so chained replies
  count. It prints the replies in full, or with `--json` one object with
  `status`, `message`, `addressed`, `runs` and `replies`. Exit codes: 0 completed,
  1 a run failed or was cancelled, 3 a run waits for approval in the web UI,
  124 timed out (`--timeout`, default 1800 s, 0 = no limit). The runs keep going
  on the server either way. Progress lines go to stderr.
- `thread <id> watch --json` streams NDJSON: a `thread.snapshot` line, then one
  line per server event.
- Thread and run IDs accept the 8-character prefixes the tables show.
- Generated plugin commands with a request body take `-d JSON|@file|@-`;
  options given as well override its fields.

```bash
openbot send <thread> "Summarize the open review cards" --bot @engineer --wait --json \
  | jq -r '.replies[].content'
```

## Raw API access

`openbot api` works like `gh api`. `PATH` is relative to `/api/v1` unless it
starts with `/api/`, which reaches everything the server exposes, plugin routes
included:

```bash
openbot api /kanban/boards
openbot api -X GET /runs -f limit=5
openbot api -X PATCH /kanban/boards/cards/<id> -F expected_version=3 -f status=done
echo '{"name": "Ops"}' | openbot api /kanban/boards -d @-
```

`-f` adds a string field and `-F` a typed one (numbers, `true`/`false`/`null`,
JSON). Fields form the JSON body and make the default method POST; with
`-X GET` they become query parameters. `-H 'Name: value'` adds a header and
`-i` prints the status line. Error responses go to stderr with exit code 1.

## Plugin commands

Plugins that register HTTP routes get a command group of the same name, built
from the server's `/openapi.json` and `/api/v1/plugins`. Each route becomes a
command named after its route function: path parameters are arguments, query
parameters and JSON body fields are options (`--help` lists them). Lists print
as tables, single objects as fields, and `--json` gives the raw response.

```bash
openbot kanban list-boards
openbot kanban list-cards <board-id>
openbot kanban create-card <board-id> --title "Fix the thing"
openbot kanban update-card <card-id> --status review --result "Done in abc123"
```

For a nullable field, the value `null` sends JSON null (`--assignee-handle null`
unassigns). The schema is cached for five minutes in
`~/.cache/openbot-cli/`; `openbot plugins --refresh` reloads it after a deploy.

A plugin can refine its commands with an `x-cli` object on a route
(`openapi_extra={"x-cli": {...}}` in FastAPI): `name` renames the command,
`columns` picks the table columns, `fill` reads body fields from a GET of the
same path when they are not given (kanban's
`{"expected_version": "version"}`), and `hidden: true` leaves a route out,
for example an inbound webhook. See `openbot_cli/plugins.py`.

## Authentication

If the server requires an API key (`OPENBOT_API_KEY` setting), pass it via
`--api-key`, the `OPENBOT_API_KEY` env var, or `openbot connect --api-key`.
The `/health` probe used by `connect` is unauthenticated, so a connection test
works even before a key is configured.

Every request also carries `X-OpenBot-Actor`, the operator handle the server
records for plugin changes (kanban cards) and scheduled messages. Like the web UI
it defaults to `you`; set another with `--as`, `OPENBOT_ACTOR` or
`openbot connect --as <handle>`. It is an audit field, not authentication.

## Testing

Unit tests mock the HTTP client (no live server required):

```bash
cd backend && uv run pytest tests/test_openbot_cli.py
```

## Credits / prior art

The `thread`/`runs`/`watch` model is inspired by run/transcript UIs such as
[harnlang.com sessions](https://harnlang.com/sessions.html) and the
[kodr](https://github.com/paulkohler/kodr) CLI run/scratchpad conventions,
and the session/scratchpad/resume model described at
[terrarium.kohaku-lab.org](https://terrarium.kohaku-lab.org/en/docs/guides/sessions).
Code is original; no third-party code is vendored.
