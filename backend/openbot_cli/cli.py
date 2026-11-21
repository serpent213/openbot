"""Click-based command line interface for the OpenBot terminal client.

Commands mirror the core web-UI flows against a running OpenBot server:

    openbot connect <url> [--api-key KEY]   Save + verify connection settings
    openbot threads [list]                  List threads
    openbot thread <id> [watch]             Show a thread (or live-follow it)
    openbot send <thread> "<msg>" [--bot @h] [--wait]  Post a message, optionally wait for replies
    openbot bots                           List bots
    openbot runs [<thread>]                List runs (optionally for one thread)
    openbot run <id>                       Show one run and its event log
    openbot api [-X METHOD] <path> [-F k=v] Send any request to the server API
    openbot plugins                        List active plugins
    openbot <plugin> <command> ...         Commands generated from a plugin's API

This is a frontend only — every command calls the existing ``/api/v1`` REST and
SSE endpoints. See README.md in this package for a full usage guide.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import click
import httpx

from openbot_cli.client import OpenBotClient, OpenBotClientError
from openbot_cli.config import (
    CONFIG_PATH,
    ConfigError,
    load_config,
    resolve,
    resolve_actor,
    save_config,
)
from openbot_cli.formatting import (
    format_bots,
    format_message,
    format_run_detail,
    format_runs,
    format_thread_detail,
    format_threads,
)
from openbot_cli.plugins import build_group, load_schema, read_json_arg

# Run statuses that may still change (backend: db/models.py OPEN_RUN_STATUSES).
OPEN_RUN_STATUSES = ("queued", "running", "waiting_human")
# send --wait exit codes besides 0 (completed) and 1 (a run failed or was cancelled).
EXIT_WAITING_HUMAN = 3
EXIT_TIMEOUT = 124


def _make_client(base_url: str, api_key: str | None, actor: str | None = None) -> OpenBotClient:
    return OpenBotClient(base_url=base_url, api_key=api_key, actor=actor)


def _ensure_client(ctx: click.Context) -> OpenBotClient:
    """The root context's client, built from flags, env and config on first use.

    Plugin commands are looked up before the root callback runs, so this can be
    called early, when --help has cut option parsing short; hence the env fallback.
    """
    root = ctx.find_root()
    obj = root.ensure_object(dict)
    # An injected client (tests, embedding, DI) is used as-is.
    if "client" not in obj:
        params = root.params
        try:
            url, key = resolve(params.get("base_url") or os.environ.get("OPENBOT_URL"),
                               params.get("api_key") or os.environ.get("OPENBOT_API_KEY"))
            actor = resolve_actor(params.get("actor") or os.environ.get("OPENBOT_ACTOR"))
        except ConfigError as e:
            raise click.ClickException(str(e)) from e
        obj.update(base_url=url, api_key=key, client=_make_client(url, key, actor))
    return obj["client"]


def _current_client() -> OpenBotClient:
    return _ensure_client(click.get_current_context())


class _ClientError(click.ClickException):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _ClientErrorGroup(click.Group):
    """Maps client errors to one-line messages and adds one group per plugin.

    With --json anywhere on the command line, errors are a JSON object on stderr
    ({"error", "exit_code", and "status" for HTTP errors}), so scripts and agents
    parse a single format.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta["json_errors"] = "--json" in args
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context):
        try:
            try:
                return super().invoke(ctx)
            except OpenBotClientError as exc:
                raise _ClientError(str(exc), exc.status) from exc
            except httpx.TransportError as exc:
                # Event streams bypass OpenBotClient._request.
                raise _ClientError(f"network error: {exc}") from exc
        except click.ClickException as exc:
            if not ctx.meta.get("json_errors"):
                raise
            payload = {"error": exc.format_message(), "exit_code": exc.exit_code}
            if getattr(exc, "status", None) is not None:
                payload["status"] = exc.status
            click.echo(json.dumps(payload, ensure_ascii=False), err=True)
            raise click.exceptions.Exit(exc.exit_code) from exc

    def _plugin_groups(self, ctx: click.Context) -> dict[str, click.Group]:
        if "plugin_groups" not in ctx.meta:
            groups: dict[str, click.Group] = {}
            try:
                schema = load_schema(_ensure_client(ctx))
            except (OpenBotClientError, click.ClickException) as exc:
                ctx.meta["plugin_error"] = str(exc)
            else:
                builtin = set(super().list_commands(ctx))
                for plugin in schema.get("plugins", []):
                    if plugin.get("id") in builtin:
                        continue
                    group = build_group(plugin, schema.get("openapi", {}), _current_client)
                    if group is not None:
                        groups[plugin["id"]] = group
            ctx.meta["plugin_groups"] = groups
        return ctx.meta["plugin_groups"]

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), *self._plugin_groups(ctx)})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        cmd = self._plugin_groups(ctx).get(cmd_name)
        if cmd is None and "plugin_error" in ctx.meta:
            raise click.UsageError(
                f"No such command {cmd_name!r} (plugin commands unavailable: {ctx.meta['plugin_error']})"
            )
        return cmd


def _resolve_prefix(kind: str, value: str, list_ids: Callable[[], Iterable[str]]) -> str:
    """Expand a displayed ID prefix; full UUIDs need no listing request."""
    try:
        UUID(value)
    except ValueError:
        pass
    else:
        return value
    ids = list(list_ids())
    if value in ids:
        return value
    matches = [i for i in ids if i.startswith(value)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise click.ClickException(f"No {kind} matches {value!r}.")
    raise click.ClickException(
        f"Ambiguous {kind} ID {value!r}; use a longer prefix: " + ", ".join(matches)
    )


def _resolve_thread_id(client: OpenBotClient, value: str) -> str:
    return _resolve_prefix("thread", value, lambda: (t["id"] for t in client.list_threads()))


def _resolve_run_id(client: OpenBotClient, value: str) -> str:
    # The server lists the latest 200 runs; older ones need the full ID.
    return _resolve_prefix("run", value, lambda: (r["id"] for r in client.list_runs()))


@click.group(cls=_ClientErrorGroup)
@click.option("--base-url", envvar="OPENBOT_URL", default=None, help="OpenBot base URL (env: OPENBOT_URL).")
@click.option("--api-key", envvar="OPENBOT_API_KEY", default=None, help="API key (env: OPENBOT_API_KEY).")
@click.option("--as", "actor", envvar="OPENBOT_ACTOR", default=None,
              help="Operator handle recorded for changes (env: OPENBOT_ACTOR; default: you).")
@click.pass_context
def cli(ctx: click.Context, base_url: str | None, api_key: str | None, actor: str | None) -> None:
    """OpenBot terminal client — talk to your bots and threads from the shell."""
    _ensure_client(ctx)


@cli.command("connect")
@click.argument("url", required=False)
@click.option("--api-key", default=None, help="API key to store alongside the URL.")
@click.option("--as", "actor", default=None, help="Operator handle to store (default: you).")
@click.option("--config", "config_path", default=None, type=click.Path(), help="Config file (advanced).")
def connect(url: str | None, api_key: str | None, actor: str | None, config_path: str | None) -> None:
    """Save the OpenBot server URL (and optional API key) and verify it responds.

    Persists the connection to the config file, then probes the server's public
    ``/api/v1/health`` route so a bad URL or key surfaces immediately. A failed
    probe is only a warning — the config is still saved so the user can retry.
    """
    path = CONFIG_PATH if config_path is None else Path(config_path)
    data = load_config(path)
    if url:
        data["base_url"] = url.rstrip("/")
    if api_key:
        data["api_key"] = api_key
    if actor:
        data["actor"] = actor.lstrip("@")
    saved = save_config(data, path)
    click.echo(f"Saved connection to {data['base_url']} ({saved})")
    probe = OpenBotClient(base_url=data["base_url"], api_key=data.get("api_key"))
    try:
        probe.ping()
    except OpenBotClientError as e:
        click.echo(f"Warning: could not reach the server: {e}", err=True)
        return
    click.echo("Connection OK (/api/v1/health)")


@cli.command("threads")
@click.argument("subcommand", required=False, type=click.Choice(["list"]))
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@click.pass_context
def threads(ctx: click.Context, subcommand: str | None, as_json: bool) -> None:
    """List threads (alias: `threads list`)."""
    client: OpenBotClient = ctx.obj["client"]
    data = client.list_threads()
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return
    click.echo(format_threads(data))


@cli.command("thread")
@click.argument("thread_id")
@click.argument("subcommand", required=False, type=click.Choice(["watch"]))
@click.option("--limit", default=50, show_default=True, help="Messages to load.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@click.pass_context
def thread(ctx: click.Context, thread_id: str, subcommand: str | None, limit: int, as_json: bool) -> None:
    """Show a thread's messages. Add `watch` to follow live events."""
    client: OpenBotClient = ctx.obj["client"]
    thread_id = _resolve_thread_id(client, thread_id)
    detail = client.get_thread(thread_id, limit=limit)
    if as_json and subcommand == "watch":
        # NDJSON: a snapshot line, then one line per server event.
        _emit({"event": "thread.snapshot", **detail})
        try:
            for item in client.stream_events(thread_id=thread_id):
                _emit(item)
        except KeyboardInterrupt:
            pass
        return
    if as_json:
        click.echo(json.dumps(detail, indent=2, default=str))
        return
    click.echo(format_thread_detail(detail))
    if subcommand == "watch":
        click.echo("\n--- watching (Ctrl+C to stop) ---")
        try:
            _watch(client, thread_id)
        except KeyboardInterrupt:
            click.echo("\nDisconnected.")


def _emit(item: dict) -> None:
    click.echo(json.dumps(item, default=str, ensure_ascii=False))
    sys.stdout.flush()


def _watch(client: OpenBotClient, thread_id: str) -> None:
    for item in client.stream_events(thread_id=thread_id):
        event = item.get("event", "?")
        data = item.get("data", item)
        if event == "message.created":
            msg = data if isinstance(data, dict) else {}
            click.echo(format_message(msg))
            click.echo("─" * 60)
        elif event == "run.updated":
            status = (data or {}).get("status", "?")
            click.echo(f"[run] {status}")
        elif event == "run.event":
            payload = (data or {}).get("payload") or {}
            delta = payload.get("delta")
            if delta:
                click.echo(delta, nl=False)
        else:
            click.echo(f"[{event}]")


@cli.command("send")
@click.argument("thread_id")
@click.argument("message")
@click.option("--bot", "bots", multiple=True, help="Bot handle to @-mention (repeatable).")
@click.option("--from", "sender", default=None, help="Sender handle (default: you/human).")
@click.option("--wait", is_flag=True, help="Wait until the addressed bots have answered and the thread is quiet.")
@click.option("--timeout", default=1800, show_default=True, type=click.IntRange(min=0),
              help="Seconds to wait with --wait; 0 waits forever.")
@click.option("--poll", default=2.0, show_default=True, type=click.FloatRange(min=0.1), hidden=True)
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@click.pass_context
def send(ctx: click.Context, thread_id: str, message: str, bots: tuple[str, ...],
         sender: str | None, wait: bool, timeout: int, poll: float, as_json: bool) -> None:
    """Send MESSAGE to THREAD_ID. Use --bot @handle to address a bot.

    \b
    With --wait, prints the bots' replies once their runs have finished. Exit codes:
    0 completed, 1 a run failed or was cancelled, 3 a run waits for approval in the
    web UI, 124 timed out. The runs keep going on the server in every case.
    """
    client: OpenBotClient = ctx.obj["client"]
    thread_id = _resolve_thread_id(client, thread_id)
    to = [b.lstrip("@") for b in bots]
    result = client.send_message(thread_id, message, to=to, sender=sender)
    if wait:
        _send_and_wait(ctx, client, thread_id, result, timeout, poll, as_json)
        return
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    msg = result["message"]
    click.echo(f"Posted as {msg['sender_name']} at {msg.get('created_at')}")
    if result.get("addressed"):
        click.echo(f"Addressed: {', '.join('@' + a for a in result['addressed'])}")
    if result.get("unaddressed"):
        click.echo("(no bots were addressed — message may go unread)")


def _ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _wait_for_runs(client: OpenBotClient, thread_id: str, since: str, actor_ids: set[str],
                   timeout: int, poll: float) -> tuple[str, list[dict]]:
    """Poll the thread's runs created since the message until they are done.

    Done means: every addressed bot has a run, none is open, and that held for two
    polls in a row, so replies that address further bots are waited for as well.
    """
    start = _ts(since)
    deadline = time.monotonic() + timeout if timeout else None
    quiet = 0
    while True:
        runs = [r for r in client.list_runs(thread_id) if _ts(r["created_at"]) >= start]
        runs.sort(key=lambda r: r["created_at"])
        if any(r["status"] == "waiting_human" for r in runs):
            return "waiting_human", runs
        started = {r["actor_id"] for r in runs}
        if actor_ids <= started and not any(r["status"] in OPEN_RUN_STATUSES for r in runs):
            quiet += 1
            if quiet >= 2:
                failed = any(r["status"] in ("failed", "cancelled") for r in runs)
                return ("failed" if failed else "completed"), runs
        else:
            quiet = 0
        if deadline is not None and time.monotonic() >= deadline:
            return "timeout", runs
        time.sleep(poll)


def _send_and_wait(ctx: click.Context, client: OpenBotClient, thread_id: str, result: dict,
                   timeout: int, poll: float, as_json: bool) -> None:
    msg = result["message"]
    addressed = result.get("addressed") or []
    if not as_json:
        click.echo(f"Posted as {msg['sender_name']} at {msg.get('created_at')}", err=True)
    if not addressed:
        status, runs = "unaddressed", []
    else:
        ids = {a["id"] for a in client.list_actors() if a.get("handle") in addressed}
        if not as_json:
            click.echo("Waiting for " + ", ".join("@" + a for a in addressed) + " …", err=True)
        try:
            status, runs = _wait_for_runs(client, thread_id, msg["created_at"], ids, timeout, poll)
        except KeyboardInterrupt:
            click.echo("\nStopped waiting; the runs continue on the server.", err=True)
            ctx.exit(130)
    run_ids = {r["id"] for r in runs}
    messages = client.get_thread(thread_id, limit=200).get("messages", []) if run_ids else []
    replies = [m for m in messages if m.get("run_id") in run_ids]
    if as_json:
        click.echo(json.dumps({"status": status, "message": msg, "addressed": addressed,
                               "runs": runs, "replies": replies}, indent=2, default=str))
    else:
        for m in replies:
            click.echo(format_message(m, full=True))
            click.echo("─" * 60)
        for r in runs:
            line = f"run {r['id'][:8]}  {r['status']}"
            if r.get("error"):
                line += f"  {r['error']}"
            if r["status"] == "waiting_human" and r.get("interrupt"):
                line += "  (approve or reject it in the web UI)"
            click.echo(line, err=True)
        if status == "unaddressed":
            click.echo("(no bots were addressed — nothing to wait for)", err=True)
        elif status == "timeout":
            click.echo(f"Timed out after {timeout}s; the runs continue on the server.", err=True)
    codes = {"failed": 1, "waiting_human": EXIT_WAITING_HUMAN, "timeout": EXIT_TIMEOUT}
    if status in codes:
        ctx.exit(codes[status])


@cli.command("bots")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@click.pass_context
def bots(ctx: click.Context, as_json: bool) -> None:
    """List bots on the connected server."""
    client: OpenBotClient = ctx.obj["client"]
    data = client.list_bots()
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return
    click.echo(format_bots(data))


@cli.command("runs")
@click.argument("thread_id", required=False)
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@click.pass_context
def runs(ctx: click.Context, thread_id: str | None, as_json: bool) -> None:
    """List runs, optionally for one THREAD_ID."""
    client: OpenBotClient = ctx.obj["client"]
    if thread_id is not None:
        thread_id = _resolve_thread_id(client, thread_id)
    data = client.list_runs(thread_id)
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return
    click.echo(format_runs(data))


@cli.command("run")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@click.pass_context
def run(ctx: click.Context, run_id: str, as_json: bool) -> None:
    """Show one RUN_ID (or a prefix of it, as `runs` shows) with its event log."""
    client: OpenBotClient = ctx.obj["client"]
    detail = client.get_run(_resolve_run_id(client, run_id))
    if as_json:
        click.echo(json.dumps(detail, indent=2, default=str))
        return
    click.echo(format_run_detail(detail))


@cli.command("plugins")
@click.option("--refresh", is_flag=True, help="Reload the plugin list and API schema now.")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@click.pass_context
def plugins(ctx: click.Context, refresh: bool, as_json: bool) -> None:
    """List the server's active plugins and their generated commands."""
    schema = load_schema(ctx.obj["client"], refresh=refresh)
    data = schema.get("plugins", [])
    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return
    if not data:
        click.echo("No plugins.")
        return
    builtin = set(click.Group.list_commands(cli, ctx))
    for plugin in data:
        group = None if plugin["id"] in builtin else build_group(plugin, schema.get("openapi", {}), _current_client)
        commands = ", ".join(group.commands) if group else "—"
        click.echo(f"{plugin['id']:<12} {plugin.get('version') or '':<10} tools: {len(plugin.get('tools', []))}")
        click.echo(f"{'':<12} commands: {commands}")


def _api_path(path: str) -> str:
    """Make PATH absolute; paths outside /api/ and /openapi.json are under /api/v1."""
    path = "/" + path.lstrip("/")
    if path.startswith("/api/") or path.split("?", 1)[0] == "/openapi.json":
        return path
    return "/api/v1" + path


def _typed(value: str):
    """-F values: JSON literals (numbers, true/false/null, arrays, objects), else a string."""
    try:
        return json.loads(value)
    except ValueError:
        return value


def _fields(raw: tuple[str, ...], typed: tuple[str, ...]) -> dict:
    out: dict = {}
    for items, convert in ((raw, str), (typed, _typed)):
        for item in items:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise click.UsageError(f"field {item!r} is not key=value")
            out[key] = convert(value)
    return out


@cli.command("api")
@click.argument("path")
@click.option("-X", "--method", default=None, help="HTTP method (default: GET, or POST with fields/data).")
@click.option("-f", "--raw-field", "raw_fields", multiple=True, metavar="KEY=VALUE",
              help="String field (repeatable). Sent as the JSON body; as query parameters with -X GET.")
@click.option("-F", "--field", "typed_fields", multiple=True, metavar="KEY=VALUE",
              help="Like -f, but numbers, true/false/null and JSON are parsed.")
@click.option("-d", "--data", default=None, metavar="JSON|@FILE|@-", help="JSON request body.")
@click.option("-H", "--header", "headers", multiple=True, metavar="NAME:VALUE", help="Extra header (repeatable).")
@click.option("-i", "--include", is_flag=True, help="Print the status line first.")
@click.pass_context
def api(ctx: click.Context, path: str, method: str | None, raw_fields: tuple[str, ...],
        typed_fields: tuple[str, ...], data: str | None, headers: tuple[str, ...], include: bool) -> None:
    """Send a request to the server API and print the response.

    PATH is relative to /api/v1 unless it starts with /api/, e.g.
    `openbot api /kanban/boards` or
    `openbot api -X PATCH /kanban/boards/cards/ID -F expected_version=3 -f status=done`.
    The API key and operator handle are sent as for every other command.
    """
    client: OpenBotClient = ctx.obj["client"]
    fields = _fields(raw_fields, typed_fields)
    if fields and data is not None:
        raise click.UsageError("use either fields (-f/-F) or --data, not both")
    method = (method or ("POST" if fields or data is not None else "GET")).upper()
    extra: dict[str, str] = {}
    for header in headers:
        name, sep, value = header.partition(":")
        if not sep or not name.strip():
            raise click.UsageError(f"header {header!r} is not NAME:VALUE")
        extra[name.strip()] = value.strip()
    kwargs: dict = {"headers": extra}
    if fields and method in ("GET", "HEAD", "DELETE"):
        kwargs["params"] = fields
    elif fields:
        kwargs["json"] = fields
    elif data is not None:
        kwargs["json"] = read_json_arg(data)
    resp = client.request_raw(method, _api_path(path), **kwargs)
    failed = resp.status_code >= 400
    if include:
        click.echo(f"{resp.status_code} {resp.reason_phrase}", err=failed)
    if resp.content:
        try:
            body = json.dumps(resp.json(), indent=2, ensure_ascii=False)
        except ValueError:
            body = resp.text
        click.echo(body, err=failed)
    if failed:
        if not include:
            click.echo(f"{method} {resp.request.url.path} -> {resp.status_code}", err=True)
        ctx.exit(1)


def main() -> int:
    """Console-script entry point."""
    try:
        cli()  # pragma: no cover - click drives sys.exit
    except click.ClickException as e:
        e.show()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
