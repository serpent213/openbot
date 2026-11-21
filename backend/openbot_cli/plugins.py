"""Terminal commands generated from the plugins' HTTP API.

Plugins that register a router expose it under ``/api/v1/<plugin id>/...``, and
FastAPI describes those routes in ``/openapi.json``. This module turns each such
route into a click command (``openbot kanban list-cards BOARD_ID``): path
parameters become arguments, query parameters and JSON body fields become
options. ``/api/v1/plugins`` says which plugins are active.

A route can refine its command through an ``x-cli`` object in its OpenAPI
operation (FastAPI: ``openapi_extra={"x-cli": {...}}``):

``name``
    Command name instead of the one derived from the route function.
``columns``
    Fields shown when the response is a list (default: its short scalar fields).
``fill``
    Body fields read from a GET of the same path when not given, as
    ``{"body_field": "response_field"}``; e.g. ``{"expected_version": "version"}``
    turns an optimistic-locking field into an optional one.
``hidden``
    ``true`` for routes that are not meant for people, such as inbound webhooks.

The schema and plugin list are cached per server for a few minutes, so a
command costs one request once the cache is warm.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from openbot_cli.client import OpenBotClient, OpenBotClientError
from openbot_cli.formatting import format_ts

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "openbot-cli"
CACHE_TTL = 300.0
FETCH_TIMEOUT = 10.0
ACTOR_HEADER = "x-openbot-actor"
MAX_CELL = 60


# -- schema loading ----------------------------------------------------------


def _cache_path(base_url: str) -> Path:
    return CACHE_DIR / f"schema-{hashlib.sha256(base_url.encode()).hexdigest()[:16]}.json"


def _fetch(client: OpenBotClient, path: str) -> Any:
    resp = client.request_raw("GET", path, timeout=FETCH_TIMEOUT)
    if resp.status_code >= 400:
        raise OpenBotClientError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def load_schema(client: OpenBotClient, *, refresh: bool = False) -> dict[str, Any]:
    """Return ``{"plugins": [...], "openapi": {...}}``, cached for CACHE_TTL seconds.

    A stale cache is used when the server cannot be reached.
    """
    path = _cache_path(client.base_url)
    cached = None
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    if cached and not refresh and time.time() - cached.get("fetched_at", 0) < CACHE_TTL:
        return cached
    try:
        fresh = {
            "fetched_at": time.time(),
            "plugins": _fetch(client, "/api/v1/plugins"),
            "openapi": _fetch(client, "/openapi.json"),
        }
    except OpenBotClientError:
        if cached and not refresh:
            return cached
        raise
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fresh))
    except OSError:
        pass
    return fresh


# -- operations --------------------------------------------------------------


@dataclass
class Param:
    name: str
    location: str  # path, query, header or body
    schema: dict[str, Any]
    required: bool
    help: str = ""


@dataclass
class Operation:
    name: str
    method: str
    path: str
    summary: str
    params: list[Param] = field(default_factory=list)
    has_body: bool = False
    columns: list[str] | None = None
    fill: dict[str, str] = field(default_factory=dict)
    response_items: dict[str, Any] | None = None  # item schema of a list response

    @property
    def path_params(self) -> set[str]:
        return {p.name for p in self.params if p.location == "path"}


def _deref(spec: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    while schema and "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        schema = node
    return schema or {}


def _nullable(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Unwrap ``anyOf: [X, null]`` (Pydantic's ``X | None``)."""
    options = schema.get("anyOf")
    if options:
        rest = [o for o in options if o.get("type") != "null"]
        if len(rest) == 1 and len(rest) < len(options):
            return {**schema, **rest[0], "anyOf": None}, True
    return schema, False


def _help(name: str, node: dict[str, Any]) -> str:
    """A description, or a title unless it only repeats the name (Pydantic's default)."""
    if node.get("description"):
        return node["description"]
    title = (node.get("schema") or node).get("title") or ""
    return "" if re.sub(r"[\W_]", "", title).lower() == re.sub(r"[\W_]", "", name).lower() else title


def _default_name(op: dict[str, Any], method: str, path: str) -> str:
    """Route function name from FastAPI's default operationId, kebab-cased."""
    op_id = op.get("operationId") or ""
    suffix = re.sub(r"\W", "_", path) + "_" + method.lower()
    name = op_id[: -len(suffix)] if op_id.endswith(suffix) and len(op_id) > len(suffix) else op_id
    name = name or method.lower() + re.sub(r"\W", "_", path)
    return name.strip("_").replace("_", "-")


def plugin_operations(spec: dict[str, Any], plugin_id: str) -> list[Operation]:
    prefix = f"/api/v1/{plugin_id}"
    ops: list[Operation] = []
    for path, methods in spec.get("paths", {}).items():
        if path != prefix and not path.startswith(prefix + "/"):
            continue
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            hints = op.get("x-cli") or {}
            if hints.get("hidden"):
                continue
            o = Operation(
                name=hints.get("name") or _default_name(op, method, path),
                method=method.upper(),
                path=path,
                summary=op.get("description") or op.get("summary") or "",
                columns=hints.get("columns"),
                fill=dict(hints.get("fill") or {}),
            )
            path_order = re.findall(r"{(\w+)}", path)
            params = op.get("parameters", [])
            params = sorted(params, key=lambda p: path_order.index(p["name"]) if p["name"] in path_order else 99)
            for p in params:
                if p.get("in") == "header" and p["name"].lower() == ACTOR_HEADER:
                    continue  # sent on every request by the client
                if p.get("in") not in ("path", "query", "header"):
                    continue
                o.params.append(Param(p["name"], p["in"], _deref(spec, p.get("schema")),
                                      bool(p.get("required")), _help(p["name"], p)))
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                o.has_body = True
                schema = _deref(spec, body.get("schema"))
                required = set(schema.get("required", []))
                taken = {p.name for p in o.params}
                for name, prop in schema.get("properties", {}).items():
                    if name in taken:
                        continue
                    prop = _deref(spec, prop)
                    o.params.append(Param(name, "body", prop, name in required and name not in o.fill,
                                          _help(name, prop)))
            ok = op.get("responses", {}).get("200") or op.get("responses", {}).get("201") or {}
            resp = _deref(spec, ok.get("content", {}).get("application/json", {}).get("schema"))
            if resp.get("type") == "array":
                o.response_items = _deref(spec, resp.get("items"))
            ops.append(o)
    names: dict[str, int] = {}
    for o in ops:
        names[o.name] = names.get(o.name, 0) + 1
    for o in ops:  # same function name on two methods: disambiguate
        if names[o.name] > 1:
            o.name = f"{o.name}-{o.method.lower()}"
    return sorted(ops, key=lambda o: o.name)


# -- values and output -------------------------------------------------------


def _click_type(schema: dict[str, Any]):
    if schema.get("enum"):
        return click.Choice([str(v) for v in schema["enum"]])
    return {"integer": int, "number": float}.get(schema.get("type"), str)


def _convert(param: Param, value: Any) -> Any:
    schema, nullable = _nullable(param.schema)
    if nullable and value == "null":
        return None
    if schema.get("type") in ("array", "object") and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise click.BadParameter(f"expects JSON: {exc}", param_hint=f"--{_opt(param.name)}") from exc
    return value


def _opt(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


ISO_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d")


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str) and ISO_TIME.match(value):
        return format_ts(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def _default_columns(rows: list[dict], item_schema: dict[str, Any] | None, skip: set[str]) -> list[str]:
    """Short scalar fields, id first, without the ones the caller passed in the path."""
    keys = list((item_schema or {}).get("properties") or rows[0].keys())
    keys = sorted((k for k in keys if k not in skip), key=lambda k: k != "id")
    cols = []
    for key in keys:
        values = [r.get(key) for r in rows]
        if any(isinstance(v, (dict, list)) for v in values):
            continue
        if any(isinstance(v, str) and (len(v) > MAX_CELL or "\n" in v) for v in values):
            continue
        cols.append(key)
    return cols[:7] or keys[:4]


def format_table(rows: list[dict], columns: list[str]) -> str:
    cells = [[_cell(r.get(c)) for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([line(columns), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def format_record(record: dict) -> str:
    width = max((len(k) for k in record), default=0)
    out = []
    for key, value in record.items():
        if isinstance(value, str) and "\n" in value:
            out.append(f"{key}:")
            out.extend("  " + line for line in value.splitlines())
        else:
            if value is None:
                text = "—"
            elif isinstance(value, str):
                text = format_ts(value) if ISO_TIME.match(value) else value
            else:
                text = json.dumps(value, ensure_ascii=False)
            out.append(f"{key.ljust(width)}  {text}")
    return "\n".join(out)


def format_result(op: Operation, data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, list):
        if not data:
            return "(none)"
        if all(isinstance(r, dict) for r in data):
            return format_table(data, op.columns or _default_columns(data, op.response_items, op.path_params))
    if isinstance(data, dict):
        return format_record(data)
    return json.dumps(data, indent=2, ensure_ascii=False)


def _error_detail(resp) -> str:
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        return resp.text[:300]
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(
            f"{'.'.join(str(x) for x in e.get('loc', [])[1:]) or 'request'}: {e.get('msg')}" for e in detail
        )
    return str(detail if detail is not None else resp.text[:300])


# -- click wiring ------------------------------------------------------------


def _expand(path: str, values: dict[str, Any]) -> str:
    return re.sub(r"{(\w+)}", lambda m: str(values[m.group(1)]), path)


def _build_command(op: Operation, get_client) -> click.Command:
    by_key: dict[str, Param] = {}
    params: list[click.Parameter] = []
    for p in op.params:
        key = re.sub(r"\W", "_", p.name).lower()
        by_key[key] = p
        schema, nullable = _nullable(p.schema)
        if p.location == "path":
            params.append(click.Argument([key], metavar=p.name.upper(), type=_click_type(schema)))
            continue
        help_text = p.help or ""
        if nullable and p.location == "body":
            help_text = (help_text + " ('null' clears it)").strip()
        if p.name in op.fill:
            help_text = (help_text + " (default: current value)").strip()
        if schema.get("type") == "boolean":
            flag = _opt(p.name)
            params.append(click.Option([f"--{flag}/--no-{flag}", key], default=None, help=help_text,
                                       required=p.required))
        else:
            params.append(click.Option([f"--{_opt(p.name)}", key], type=_click_type(schema), default=None,
                                       required=p.required, help=help_text))
    params.append(click.Option(["--json", "as_json"], is_flag=True, help="Emit raw JSON."))

    def callback(as_json: bool = False, **values: Any) -> None:
        client: OpenBotClient = get_client()
        path_values: dict[str, Any] = {}
        query: dict[str, Any] = {}
        headers: dict[str, str] = {}
        body: dict[str, Any] = {}
        for key, value in values.items():
            p = by_key[key]
            if value is None:
                continue
            if p.location == "path":
                path_values[p.name] = value
            elif p.location == "query":
                query[p.name] = value
            elif p.location == "header":
                headers[p.name] = str(value)
            else:
                body[p.name] = _convert(p, value)
        path = _expand(op.path, path_values)
        missing = [k for k in op.fill if k not in body]
        if missing:
            current = client.request_raw("GET", path)
            if current.status_code >= 400:
                raise OpenBotClientError(f"GET {path} -> {current.status_code}: {_error_detail(current)}")
            record = current.json()
            for key in missing:
                body[key] = record.get(op.fill[key])
        kwargs: dict[str, Any] = {"params": query, "headers": headers}
        if op.has_body:
            kwargs["json"] = body
        resp = client.request_raw(op.method, path, **kwargs)
        if resp.status_code >= 400:
            raise OpenBotClientError(f"{op.method} {path} -> {resp.status_code}: {_error_detail(resp)}")
        data = resp.json() if resp.content else None
        if as_json:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
            return
        text = format_result(op, data)
        if text:
            click.echo(text)

    summary = op.summary.strip() or f"{op.method} {op.path}"
    return click.Command(op.name, params=params, callback=callback,
                         help=f"{summary}\n\n\b\n{op.method} {op.path}", short_help=summary.splitlines()[0])


def build_group(plugin: dict[str, Any], spec: dict[str, Any], get_client) -> click.Group | None:
    ops = plugin_operations(spec, plugin["id"])
    if not ops:
        return None
    group = click.Group(
        plugin["id"],
        help=f"Commands of the {plugin['id']} plugin ({plugin.get('version') or 'unversioned'}), "
        "generated from the server's API.",
        short_help=f"{plugin['id']} plugin commands",
    )
    for op in ops:
        group.add_command(_build_command(op, get_client))
    return group
