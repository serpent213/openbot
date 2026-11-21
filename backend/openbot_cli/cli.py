"""Click-based command line interface for the OpenBot terminal client.

Commands mirror the core web-UI flows against a running OpenBot server:

    openbot connect <url> [--api-key KEY]   Save + verify connection settings
    openbot threads [list]                  List threads
    openbot thread <id> [watch]             Show a thread (or live-follow it)
    openbot send <thread> "<msg>" [--bot @h] Post a message, optionally @-ing a bot
    openbot bots                           List bots
    openbot runs [<thread>]                List runs (optionally for one thread)
    openbot run <id>                       Show one run and its event log
    openbot api [-X METHOD] <path> [-F k=v] Send any request to the server API

This is a frontend only — every command calls the existing ``/api/v1`` REST and
SSE endpoints. See README.md in this package for a full usage guide.
"""

from __future__ import annotations

import json
import sys
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
    format_run_detail,
    format_runs,
    format_thread_detail,
    format_threads,
)


def _make_client(base_url: str, api_key: str | None, actor: str | None = None) -> OpenBotClient:
    return OpenBotClient(base_url=base_url, api_key=api_key, actor=actor)


class _ClientErrorGroup(click.Group):
    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except OpenBotClientError as exc:
            raise click.ClickException(str(exc)) from exc
        except httpx.TransportError as exc:
            # Event streams bypass OpenBotClient._request.
            raise click.ClickException(f"network error: {exc}") from exc


def _resolve_thread_id(client: OpenBotClient, value: str) -> str:
    """Expand a displayed prefix; full UUIDs need no listing request."""
    try:
        UUID(value)
    except ValueError:
        pass
    else:
        return value
    ids = [thread["id"] for thread in client.list_threads()]
    if value in ids:
        return value
    matches = [tid for tid in ids if tid.startswith(value)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise click.ClickException(f"No thread matches {value!r}.")
    raise click.ClickException(
        f"Ambiguous thread ID {value!r}; use a longer prefix: " + ", ".join(matches)
    )


@click.group(cls=_ClientErrorGroup)
@click.option("--base-url", envvar="OPENBOT_URL", default=None, help="OpenBot base URL (env: OPENBOT_URL).")
@click.option("--api-key", envvar="OPENBOT_API_KEY", default=None, help="API key (env: OPENBOT_API_KEY).")
@click.option("--as", "actor", envvar="OPENBOT_ACTOR", default=None,
              help="Operator handle recorded for changes (env: OPENBOT_ACTOR; default: you).")
@click.pass_context
def cli(ctx: click.Context, base_url: str | None, api_key: str | None, actor: str | None) -> None:
    """OpenBot terminal client — talk to your bots and threads from the shell."""
    ctx.ensure_object(dict)
    # If a client was injected (tests, embedding, DI), use it as-is and skip the
    # config-resolve + client-build path so the injected one is not clobbered.
    if "client" in ctx.obj:
        return
    try:
        url, key = resolve(base_url, api_key)
        actor = resolve_actor(actor)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e
    ctx.obj["base_url"] = url
    ctx.obj["api_key"] = key
    ctx.obj["client"] = _make_client(url, key, actor)


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


def _watch(client: OpenBotClient, thread_id: str) -> None:
    for item in client.stream_events(thread_id=thread_id):
        event = item.get("event", "?")
        data = item.get("data", item)
        if event == "message.created":
            from openbot_cli.formatting import format_message

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
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
@click.pass_context
def send(ctx: click.Context, thread_id: str, message: str, bots: tuple[str, ...],
         sender: str | None, as_json: bool) -> None:
    """Send MESSAGE to THREAD_ID. Use --bot @handle to address a bot."""
    client: OpenBotClient = ctx.obj["client"]
    thread_id = _resolve_thread_id(client, thread_id)
    to = [b.lstrip("@") for b in bots]
    result = client.send_message(thread_id, message, to=to, sender=sender)
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    msg = result["message"]
    click.echo(f"Posted as {msg['sender_name']} at {msg.get('created_at')}")
    if result.get("addressed"):
        click.echo(f"Addressed: {', '.join('@' + a for a in result['addressed'])}")
    if result.get("unaddressed"):
        click.echo("(no bots were addressed — message may go unread)")


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
    """Show one RUN_ID with its event log."""
    client: OpenBotClient = ctx.obj["client"]
    detail = client.get_run(run_id)
    if as_json:
        click.echo(json.dumps(detail, indent=2, default=str))
        return
    click.echo(format_run_detail(detail))


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


def _read_data(value: str):
    if value == "@-":
        text = sys.stdin.read()
    elif value.startswith("@"):
        text = Path(value[1:]).read_text()
    else:
        text = value
    try:
        return json.loads(text)
    except ValueError as exc:
        raise click.UsageError(f"--data is not valid JSON: {exc}") from exc


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
        kwargs["json"] = _read_data(data)
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
