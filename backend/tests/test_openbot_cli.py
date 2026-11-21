"""Tests for the OpenBot terminal client.

All HTTP is mocked via ``httpx.MockTransport`` — no live server is required. The
``OpenBotClient`` is constructed with a prebuilt ``httpx.Client`` so the tests
exercise the request/parse logic without sockets, and ``stream_events`` is
monkeypatched for the ``watch`` path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from click.testing import CliRunner
from httpx import SyncByteStream

from openbot_cli import cli as cli_mod
from openbot_cli.client import OpenBotClient, OpenBotClientError


class _SS(SyncByteStream):
    """Wrap a byte iterator as an httpx SyncByteStream for mocked streams."""

    def __init__(self, it):
        self._it = it

    def __iter__(self):
        return iter(self._it)


def _client(handler) -> OpenBotClient:
    # MockTransport responses aren't bound to their request, which makes httpx's
    # cookie extraction blow up ("unknown url type"). Bind it so the responses
    # behave like real ones — production code never hits this path. The injected
    # httpx.Client must carry base_url so relative paths resolve, mirroring how
    # OpenBotClient configures the client it builds itself.
    def _wrap(req: httpx.Request):
        resp = handler(req)
        resp.request = req
        return resp

    transport = httpx.MockTransport(_wrap)
    http_client = httpx.Client(base_url="http://localhost", transport=transport)
    return OpenBotClient(base_url="http://localhost", api_key="k", client=http_client)


def _json_response(payload, status=200):
    return httpx.Response(status, json=payload)


# ---------------------------------------------------------------------------
# Client unit tests
# ---------------------------------------------------------------------------

def test_list_threads_parses_payload():
    sample = [
        {
            "id": "thread_abc",
            "title": "Plan Rome trip",
            "last_message_at": "2026-10-20T10:00:00Z",
            "default_bot_handle": "eng",
            "active": True,
        }
    ]
    c = _client(lambda req: _json_response(sample) if req.url.path == "/api/v1/threads" else _json_response({}))
    assert c.list_threads() == sample


def test_get_thread_uses_limit_param():
    captured = {}

    def handler(req):
        if req.url.path.startswith("/api/v1/threads/") and req.method == "GET":
            captured["params"] = dict(req.url.params)
            return _json_response({"id": "t1", "messages": [], "participants": []})
        return _json_response({}, status=404)

    c = _client(handler)
    detail = c.get_thread("t1", limit=12)
    assert detail["id"] == "t1"
    assert captured["params"]["limit"] == "12"


def test_send_message_posts_body_and_returns_addressed():
    captured = {}

    def handler(req):
        if req.url.path.endswith("/messages") and req.method == "POST":
            captured["body"] = json.loads(req.content)
            return _json_response(
                {
                    "message": {"id": "m1", "sender_name": "you", "content": "hi", "created_at": "2026-10-20T10:00:00Z"},
                    "addressed": ["eng"],
                    "unaddressed": False,
                },
                status=201,
            )
        return _json_response({}, status=404)

    c = _client(handler)
    res = c.send_message("t1", "hi", to=["eng"], sender="you")
    assert captured["body"] == {"content": "hi", "to": ["eng"], "from": "you"}
    assert res["addressed"] == ["eng"]


def test_send_message_defaults_sender_to_human():
    captured = {}

    def handler(req):
        if req.url.path == "/api/v1/threads":
            return _json_response([{"id": "t1"}])
        if req.url.path.endswith("/messages"):
            captured["body"] = json.loads(req.content)
            return _json_response({"message": {"id": "m1"}, "addressed": [], "unaddressed": True}, status=201)
        return _json_response({}, status=404)

    c = _client(handler)
    c.send_message("t1", "hi")
    assert "from" not in captured["body"]


def test_list_bots_and_runs():
    bots = [{"id": "b1", "handle": "eng", "name": "Engineer", "enabled": True, "active": True}]
    runs = [{"id": "r1", "actor_id": "a1", "thread_id": "t1", "status": "completed", "total_tokens": 10}]

    def handler(req):
        if req.url.path == "/api/v1/bots":
            return _json_response(bots)
        if req.url.path == "/api/v1/runs" and req.method == "GET":
            assert dict(req.url.params).get("thread_id") == "t1"
            return _json_response(runs)
        return _json_response({}, status=404)

    c = _client(handler)
    assert c.list_bots() == bots
    assert c.list_runs("t1") == runs


def test_health_probe_is_public_path():
    captured = {}

    def handler(req):
        captured["path"] = req.url.path
        return _json_response({"status": "ok"})
    c = _client(handler)
    c.ping()
    assert captured["path"] == "/api/v1/health"


def test_stream_events_yields_parsed_events():
    def sse_body() -> Iterator[bytes]:
        yield b": connected\n\n"
        yield b"event: message.created\ndata: " + json.dumps({"id": "m1"}).encode() + b"\n\n"
        yield b"data: " + json.dumps({"plain": True}).encode() + b"\n\n"

    captured = {}

    def handler(req):
        captured["path"] = req.url.path
        captured["params"] = dict(req.url.params)
        return httpx.Response(200, stream=_SS(sse_body()), headers={"content-type": "text/event-stream"})

    c = _client(handler)
    events = list(c.stream_events(thread_id="t1"))
    assert captured["path"] == "/api/v1/events"
    assert captured["params"]["thread_id"] == "t1"
    assert events[0] == {"event": "message.created", "id": "m1"}
    assert events[1] == {"event": "message", "plain": True}


def test_client_error_on_4xx():
    c = _client(lambda req: _json_response({"detail": "nope"}, status=404))
    with pytest.raises(OpenBotClientError):
        c.list_threads()


def test_client_error_when_unreachable():
    def handler(req):
        raise httpx.ConnectError("[Errno 61] Connection refused", request=req)

    c = _client(handler)
    with pytest.raises(OpenBotClientError, match="cannot reach http://localhost"):
        c.list_runs()


# ---------------------------------------------------------------------------
# CLI integration tests (CliRunner with injected client)
# ---------------------------------------------------------------------------

def _runner_with_client(c: OpenBotClient) -> CliRunner:
    runner = CliRunner()

    def invoke(args=None, **kwargs):
        # Callers pass a single command list (e.g. ["threads"]); forward it
        # directly to click so we don't accidentally nest it inside a tuple,
        # which would make click iterate over a list and raise
        # "unhashable type: 'list'" while parsing.
        kwargs.setdefault("obj", {})
        kwargs["obj"]["client"] = c
        kwargs["obj"]["base_url"] = c.base_url
        kwargs["obj"]["api_key"] = c.api_key
        return runner.invoke(cli_mod.cli, args, **kwargs)

    return invoke


def test_cli_threads_lists():
    c = _client(lambda req: _json_response([{"id": "t1", "title": "Hi", "default_bot_handle": "eng", "active": False}]))
    run = _runner_with_client(c)
    result = run(["threads"])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output
    assert "@eng" in result.output


def test_cli_thread_shows_messages():
    detail = {
        "title": "Trip",
        "default_bot_handle": "eng",
        "participants": [],
        "has_more": False,
        "messages": [
            {"sender_name": "you", "created_at": "2026-10-20T10:00:00Z", "content": "hello", "mentions": ["eng"]}
        ],
    }
    c = _client(lambda req: _json_response(detail) if req.method == "GET" and "/threads/" in req.url.path else _json_response([{"id": "t1"}]))
    result = _runner_with_client(c)(["thread", "t1"])
    assert result.exit_code == 0, result.output
    assert "hello" in result.output
    assert "@eng" in result.output


def test_cli_send_posts_and_reports_addressed():
    captured = {}

    def handler(req):
        if req.url.path == "/api/v1/threads":
            return _json_response([{"id": "t1"}])
        if req.url.path.endswith("/messages"):
            captured["body"] = json.loads(req.content)
            return _json_response(
                {"message": {"id": "m1", "sender_name": "you", "created_at": "2026-10-20T10:00:00Z"},
                 "addressed": ["eng"], "unaddressed": False}, status=201)
        return _json_response({}, status=404)

    c = _client(handler)
    result = _runner_with_client(c)(["send", "t1", "do the thing", "--bot", "@eng"])
    assert result.exit_code == 0, result.output
    assert captured["body"]["to"] == ["eng"]
    assert "Addressed: @eng" in result.output


def test_cli_bots_and_runs():
    def handler(req):
        if req.url.path == "/api/v1/bots":
            return _json_response([{"id": "b1", "handle": "eng", "name": "Eng", "enabled": True, "active": True, "description": "d"}])
        if req.url.path == "/api/v1/runs":
            return _json_response([{"id": "r1", "actor_id": "a1", "thread_id": "t1", "status": "completed", "total_tokens": 5}])
        return _json_response({}, status=404)

    c = _client(handler)
    r1 = _runner_with_client(c)(["bots"])
    r2 = _runner_with_client(c)(["runs"])
    assert r1.exit_code == 0 and "eng" in r1.output
    assert r2.exit_code == 0 and "r1" in r2.output


def test_cli_reports_unreachable_server_without_traceback():
    def handler(req):
        raise httpx.ConnectError("[Errno 61] Connection refused", request=req)

    result = _runner_with_client(_client(handler))(["runs"])
    assert result.exit_code == 1
    assert "cannot reach http://localhost" in result.output
    assert "Traceback" not in result.output


def test_cli_api_get_prefixes_v1_and_sends_actor():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["actor"] = req.headers.get("X-OpenBot-Actor")
        return _json_response([{"id": "b1", "name": "Board"}])

    c = _client(handler)
    c.actor = "you"
    result = _runner_with_client(c)(["api", "kanban/boards"])
    assert result.exit_code == 0, result.output
    assert seen == {"path": "/api/v1/kanban/boards", "actor": "you"}
    assert json.loads(result.output) == [{"id": "b1", "name": "Board"}]


def test_cli_api_fields_become_body_or_query():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, dict(req.url.params), req.content))
        return _json_response({"ok": True})

    invoke = _runner_with_client(_client(handler))
    r1 = invoke(["api", "-X", "PATCH", "/api/v1/kanban/boards/cards/c1",
                 "-F", "expected_version=3", "-f", "status=done", "-F", "thread_id=null"])
    r2 = invoke(["api", "-X", "GET", "/runs", "-f", "limit=1"])
    r3 = invoke(["api", "/kanban/boards", "-d", '{"name": "B"}'])
    assert (r1.exit_code, r2.exit_code, r3.exit_code) == (0, 0, 0)
    assert seen[0][:2] == ("PATCH", "/api/v1/kanban/boards/cards/c1")
    assert json.loads(seen[0][3]) == {"expected_version": 3, "status": "done", "thread_id": None}
    assert seen[1][:3] == ("GET", "/api/v1/runs", {"limit": "1"})
    assert seen[2][0] == "POST" and json.loads(seen[2][3]) == {"name": "B"}


def test_cli_api_reads_body_from_stdin():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(204)

    result = _runner_with_client(_client(handler))(
        ["api", "-X", "PUT", "/x", "-d", "@-"], input='{"a": [1, 2]}'
    )
    assert result.exit_code == 0, result.output
    assert seen["body"] == {"a": [1, 2]}
    assert result.output == ""


def test_cli_api_error_prints_body_and_fails():
    c = _client(lambda req: _json_response({"detail": "card not found"}, status=404))
    result = _runner_with_client(c)(["api", "/kanban/boards/cards/nope"])
    assert result.exit_code == 1
    assert "card not found" in result.output
    assert "GET /api/v1/kanban/boards/cards/nope -> 404" in result.output


def test_cli_api_rejects_fields_with_data():
    result = _runner_with_client(_client(lambda req: _json_response({})))(
        ["api", "/x", "-f", "a=b", "-d", "{}"]
    )
    assert result.exit_code == 2
    assert "either fields" in result.output


def test_cli_connect_stores_actor(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"

    class _FakeClient:
        def __init__(self, base_url, api_key=None, **_):
            pass

        def ping(self):
            return {"status": "ok"}

    monkeypatch.setattr(cli_mod, "OpenBotClient", _FakeClient)
    result = CliRunner().invoke(
        cli_mod.cli, ["connect", "https://x.test", "--as", "@steffen", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(cfg.read_text())["actor"] == "steffen"


def test_cli_connect_saves_and_pings(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    captured = {}

    class _FakeClient:
        def __init__(self, base_url, api_key=None, **_):
            captured["base_url"] = base_url
            captured["api_key"] = api_key

        def ping(self):
            captured["pinged"] = True
            return {"status": "ok"}

    monkeypatch.setattr(cli_mod, "OpenBotClient", _FakeClient)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["connect", "https://example.test", "--api-key", "sek", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "Saved connection" in result.output
    assert "Connection OK" in result.output
    assert captured["base_url"] == "https://example.test"
    assert captured["api_key"] == "sek"
    assert captured["pinged"] is True
    saved = json.loads(cfg.read_text())
    assert saved["base_url"] == "https://example.test"
    assert saved["api_key"] == "sek"


def test_cli_connect_warns_when_server_unreachable(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"

    class _FakeClient:
        def __init__(self, base_url, api_key=None, **_):
            pass

        def ping(self):
            raise OpenBotClientError("GET /api/v1/health -> 500")

    monkeypatch.setattr(cli_mod, "OpenBotClient", _FakeClient)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["connect", "https://down.test", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "Warning:" in result.output
    # Config is still saved even though the probe failed.
    assert json.loads(cfg.read_text())["base_url"] == "https://down.test"


def test_cli_thread_watch_streams(monkeypatch):
    detail = {
        "title": "Trip",
        "default_bot_handle": "eng",
        "participants": [],
        "has_more": False,
        "messages": [],
    }

    def sse_body():
        yield b"event: message.created\ndata: " + json.dumps({"sender_name": "eng", "created_at": "2026-10-20T10:00:00Z", "content": "on it"}).encode() + b"\n\n"

    def handler(req):
        if req.url.path == "/api/v1/threads":
            return _json_response([{"id": "t1"}])
        # The `thread` command first fetches the detail (JSON), then opens the
        # SSE stream for `watch`; serve each with the right content type.
        if req.url.path.startswith("/api/v1/events"):
            return httpx.Response(200, stream=_SS(sse_body()), headers={"content-type": "text/event-stream"})
        return _json_response(detail)

    c = _client(handler)

    # Yield one event then stop, so `watch` exits; emulate Ctrl+C via StopIteration.
    original = c.stream_events

    def fake_stream(thread_id=None):
        for item in original(thread_id=thread_id):
            yield item
            break  # stop after the first event

    monkeypatch.setattr(c, "stream_events", fake_stream)
    result = _runner_with_client(c)(["thread", "t1", "watch"])
    assert result.exit_code == 0, result.output
    assert "on it" in result.output


@pytest.mark.parametrize("command", ["thread", "watch", "send", "runs"])
def test_cli_resolves_displayed_thread_prefix(command, monkeypatch):
    tid = "9124a607-1111-4222-8333-444444444444"
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/api/v1/threads":
            return _json_response([{"id": tid}])
        if req.url.path.endswith("/messages"):
            return _json_response({"message": {"sender_name": "you"}})
        if req.url.path == "/api/v1/runs":
            return _json_response([])
        return _json_response({"title": "Trip", "messages": []})

    client = _client(handler)
    watched = []
    monkeypatch.setattr(client, "stream_events", lambda thread_id: watched.append(thread_id) or iter(()))
    args = {
        "thread": ["thread", "9124a607"],
        "watch": ["thread", "9124a607", "watch"],
        "send": ["send", "9124a607", "hello"],
        "runs": ["runs", "9124a607"],
    }[command]
    result = _runner_with_client(client)(args)
    assert result.exit_code == 0, result.output
    assert len(seen) == 2
    if command == "runs":
        assert seen[1].url.params["thread_id"] == tid
    else:
        assert seen[1].url.path.startswith(f"/api/v1/threads/{tid}")
    if command == "watch":
        assert watched == [tid]


@pytest.mark.parametrize("ids, error", [
    ([], "No thread matches"),
    (["9124a607-one", "9124a607-two"], "Ambiguous thread ID"),
])
def test_cli_rejects_unresolved_prefix_before_sending(ids, error):
    seen = []

    def handler(req):
        seen.append(req)
        return _json_response([{"id": tid} for tid in ids])

    result = _runner_with_client(_client(handler))(["send", "9124a607", "hello"])
    assert result.exit_code == 1
    assert error in result.output
    assert len(seen) == 1 and seen[0].method == "GET"
    for tid in ids:
        assert tid in result.output


def test_cli_full_thread_uuid_skips_listing_and_reports_api_error():
    tid = "9124a607-1111-4222-8333-444444444444"
    seen = []

    def handler(req):
        seen.append(req)
        return _json_response({"detail": "thread not found"}, status=404)

    result = _runner_with_client(_client(handler))(["thread", tid])
    assert result.exit_code == 1
    assert "Error:" in result.output and "thread not found" in result.output
    assert isinstance(result.exception, SystemExit)
    assert len(seen) == 1 and seen[0].url.path == f"/api/v1/threads/{tid}"


# ---------------------------------------------------------------------------
# Plugin commands generated from /openapi.json
# ---------------------------------------------------------------------------

_CARD_PATH = "/api/v1/kanban/boards/cards/{card_id}"
_SPEC = {
    "paths": {
        "/api/v1/kanban/boards": {
            "get": {"operationId": "list_boards_api_v1_kanban_boards_get", "summary": "List Boards",
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "array", "items": {"$ref": "#/components/schemas/BoardOut"}}}}}}},
        },
        "/api/v1/kanban/boards/{board_id}/cards": {
            "get": {"operationId": "list_cards_api_v1_kanban_boards__board_id__cards_get", "summary": "List Cards",
                    "parameters": [{"name": "board_id", "in": "path", "required": True,
                                    "schema": {"type": "string", "title": "Board Id"}}],
                    "x-cli": {"columns": ["id", "status", "title"]},
                    "responses": {"200": {"content": {"application/json": {"schema": {"type": "array"}}}}}},
        },
        _CARD_PATH: {
            "patch": {
                "operationId": "update_card_api_v1_kanban_boards_cards__card_id__patch", "summary": "Update Card",
                "parameters": [
                    {"name": "card_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "X-OpenBot-Actor", "in": "header", "required": False, "schema": {"type": "string"}},
                ],
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CardUpdate"}}}},
                "x-cli": {"fill": {"expected_version": "version"}},
                "responses": {"200": {"content": {"application/json": {"schema": {}}}}},
            },
            "get": {"operationId": "get_card_api_v1_kanban_boards_cards__card_id__get", "summary": "Get Card",
                    "parameters": [{"name": "card_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {}}},
        },
        "/api/v1/telegram/webhook": {"post": {"operationId": "webhook", "x-cli": {"hidden": True}}},
        "/api/v1/threads": {"get": {"operationId": "list_threads_api_v1_threads_get"}},
    },
    "components": {"schemas": {
        "BoardOut": {"type": "object", "properties": {
            "name": {"type": "string"}, "description": {"type": "string"}, "id": {"type": "string"},
            "updated_at": {"type": "string"}}},
        "CardUpdate": {"type": "object", "required": ["expected_version"], "properties": {
            "expected_version": {"type": "integer", "title": "Expected Version"},
            "status": {"type": "string", "enum": ["backlog", "ready", "doing", "review", "done", "blocked"]},
            "assignee_handle": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Assignee Handle"},
            "result": {"type": "string", "description": "What was done."}}},
    }},
}
_PLUGINS = [{"id": "kanban", "version": "0.1.0", "tools": ["board_list"]},
            {"id": "telegram", "version": "0.1.0", "tools": []}]


@pytest.fixture
def plugin_server(tmp_path, monkeypatch):
    from openbot_cli import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "CACHE_DIR", tmp_path / "cache")
    calls = []
    card = {"id": "c1", "status": "doing", "title": "T", "version": 4, "assignee_handle": "pi"}

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body, req.headers.get("X-OpenBot-Actor")))
        path = req.url.path
        if path == "/openapi.json":
            return _json_response(_SPEC)
        if path == "/api/v1/plugins":
            return _json_response(_PLUGINS)
        if path == "/api/v1/kanban/boards":
            return _json_response([{"name": "Dev", "description": "x" * 100, "id": "b1",
                                    "updated_at": "2026-11-09T01:43:52Z"}])
        if path == "/api/v1/kanban/boards/b1/cards":
            return _json_response([card, {**card, "id": "c2", "title": "U"}])
        if path == "/api/v1/kanban/boards/cards/c1" and req.method == "GET":
            return _json_response(card)
        if path == "/api/v1/kanban/boards/cards/c1" and req.method == "PATCH":
            if body.get("status") == "review" and "result" not in body:
                return _json_response({"detail": [{"loc": ["body", "result"], "msg": "review requires a result"}]},
                                      status=422)
            return _json_response({**card, **body, "version": 5})
        return _json_response({"detail": "nope"}, status=404)

    c = _client(handler)
    c.actor = "you"
    return _runner_with_client(c), calls


def test_plugin_commands_are_listed_and_grouped(plugin_server):
    invoke, _ = plugin_server
    top = invoke(["--help"])
    assert top.exit_code == 0, top.output
    assert "kanban" in top.output
    assert "telegram" not in top.output  # its only route is hidden
    group = invoke(["kanban", "--help"])
    assert group.exit_code == 0, group.output
    for name in ("list-boards", "list-cards", "update-card", "get-card"):
        assert name in group.output
    assert "threads" not in group.output  # core routes stay out of plugin groups


def test_plugin_list_renders_table_without_long_fields(plugin_server):
    invoke, _ = plugin_server
    result = invoke(["kanban", "list-boards"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header == ["id", "name", "updated_at"]  # description is too long for a table
    cards = invoke(["kanban", "list-cards", "b1"])
    assert cards.output.splitlines()[0].split() == ["id", "status", "title"]  # x-cli columns


def test_plugin_update_fills_version_and_sends_body(plugin_server):
    invoke, calls = plugin_server
    result = invoke(["kanban", "update-card", "c1", "--status", "done", "--assignee-handle", "null"])
    assert result.exit_code == 0, result.output
    method, path, body, actor = calls[-1]
    assert (method, path, actor) == ("PATCH", "/api/v1/kanban/boards/cards/c1", "you")
    assert body == {"status": "done", "assignee_handle": None, "expected_version": 4}
    assert calls[-2][:2] == ("GET", "/api/v1/kanban/boards/cards/c1")
    assert "version" in result.output and "5" in result.output


def test_plugin_update_with_version_skips_lookup(plugin_server):
    invoke, calls = plugin_server
    result = invoke(["kanban", "update-card", "c1", "--expected-version", "4", "--status", "doing"])
    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls if c[1].startswith("/api/v1/kanban/boards/cards")] == ["PATCH"]


def test_plugin_validation_error_is_one_line(plugin_server):
    invoke, _ = plugin_server
    result = invoke(["kanban", "update-card", "c1", "--status", "review"])
    assert result.exit_code == 1
    assert "422: result: review requires a result" in result.output
    assert "Traceback" not in result.output


def test_plugin_schema_is_cached(plugin_server):
    invoke, calls = plugin_server
    invoke(["kanban", "list-boards"])
    invoke(["kanban", "list-boards"])
    assert sum(1 for c in calls if c[1] == "/openapi.json") == 1


def test_plugins_command_lists_generated_commands(plugin_server):
    invoke, _ = plugin_server
    result = invoke(["plugins"])
    assert result.exit_code == 0, result.output
    assert "kanban" in result.output and "update-card" in result.output


def test_builtin_commands_work_without_plugin_schema(tmp_path, monkeypatch):
    from openbot_cli import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "CACHE_DIR", tmp_path / "cache")

    def handler(req):
        if req.url.path == "/api/v1/bots":
            return _json_response([])
        raise httpx.ConnectError("refused", request=req)

    invoke = _runner_with_client(_client(handler))
    assert invoke(["bots"]).exit_code == 0
    assert invoke(["--help"]).exit_code == 0
    unknown = invoke(["kanban", "list-boards"])
    assert unknown.exit_code == 2
    assert "plugin commands unavailable" in unknown.output


# ---------------------------------------------------------------------------
# Agent-friendly behaviour: waiting, NDJSON, JSON errors, run prefixes, --data
# ---------------------------------------------------------------------------

_POSTED = {"message": {"id": "m1", "sender_name": "You", "created_at": "2026-11-21T10:00:00Z"},
           "addressed": ["eng"], "unaddressed": False}


def _wait_server(run_states, *, reply="done, see abc123"):
    """A thread whose eng run goes through `run_states`, one per poll."""
    polls = {"n": 0}

    def handler(req):
        path = req.url.path
        if path.endswith("/messages"):
            return _json_response(_POSTED, status=201)
        if path == "/api/v1/actors":
            return _json_response([{"id": "a-eng", "handle": "eng", "kind": "bot"},
                                   {"id": "a-you", "handle": "you", "kind": "human"}])
        if path == "/api/v1/runs":
            state = run_states[min(polls["n"], len(run_states) - 1)]
            polls["n"] += 1
            old = {"id": "r-old", "actor_id": "a-eng", "status": "running", "created_at": "2026-11-21T09:00:00Z"}
            if state is None:
                return _json_response([old])  # not picked up yet; an older run is ignored
            return _json_response([old, {"id": "r1", "actor_id": "a-eng", "status": state,
                                         "created_at": "2026-11-21T10:00:01Z", "error": None,
                                         "interrupt": {"tool": "x"} if state == "waiting_human" else None}])
        if path.startswith("/api/v1/threads/"):
            return _json_response({"messages": [
                {"id": "m1", "sender_name": "You", "content": "go", "run_id": None, "created_at": "2026-11-21T10:00:00Z"},
                {"id": "m2", "sender_name": "Eng", "content": reply, "run_id": "r1", "created_at": "2026-11-21T10:00:09Z"},
            ]})
        return _json_response({}, status=404)

    return handler, polls


def _no_sleep(monkeypatch):
    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: None)


def test_send_wait_prints_reply_after_runs_finish(monkeypatch):
    _no_sleep(monkeypatch)
    handler, polls = _wait_server([None, "queued", "running", "completed"], reply="x" * 900)
    t1 = "11111111-1111-1111-1111-111111111111"
    result = _runner_with_client(_client(handler))(["send", t1, "go", "--bot", "eng", "--wait"])
    assert result.exit_code == 0, result.output
    assert "x" * 900 in result.output  # replies are not truncated
    assert "run r1  completed" in result.output
    assert polls["n"] >= 5  # needed two quiet polls after completion


def test_send_wait_json_and_exit_codes(monkeypatch):
    _no_sleep(monkeypatch)
    t1 = "11111111-1111-1111-1111-111111111111"
    handler, _ = _wait_server(["running", "completed"])
    result = _runner_with_client(_client(handler))(["send", t1, "go", "--wait", "--json"])
    data = json.loads(result.stdout)
    assert result.exit_code == 0
    assert data["status"] == "completed"
    assert [r["id"] for r in data["runs"]] == ["r1"]
    assert [m["id"] for m in data["replies"]] == ["m2"]

    handler, _ = _wait_server(["running", "waiting_human"])
    result = _runner_with_client(_client(handler))(["send", t1, "go", "--wait"])
    assert result.exit_code == 3
    assert "approve or reject it in the web UI" in result.output

    handler, _ = _wait_server(["failed"])
    assert _runner_with_client(_client(handler))(["send", t1, "go", "--wait"]).exit_code == 1


def test_send_wait_times_out(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(cli_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    handler, _ = _wait_server(["running"])
    t1 = "11111111-1111-1111-1111-111111111111"
    result = _runner_with_client(_client(handler))(["send", t1, "go", "--wait", "--timeout", "5"])
    assert result.exit_code == 124
    assert "Timed out after 5s" in result.output


def test_watch_json_emits_ndjson(monkeypatch):
    detail = {"id": "t1", "title": "Trip", "messages": []}

    def handler(req):
        return _json_response(detail)

    c = _client(handler)
    monkeypatch.setattr(c, "stream_events", lambda thread_id=None: iter(
        [{"event": "run.updated", "status": "running"}, {"event": "message.created", "content": "hi"}]))
    result = _runner_with_client(c)(["thread", "11111111-1111-1111-1111-111111111111", "watch", "--json"])
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines()]
    assert [line["event"] for line in lines] == ["thread.snapshot", "run.updated", "message.created"]
    assert lines[0]["title"] == "Trip"


def test_run_accepts_displayed_prefix():
    full = "a58ddc62-e84f-4e9e-b88e-ccf2718b04d7"

    def handler(req):
        if req.url.path == "/api/v1/runs":
            return _json_response([{"id": full}, {"id": "b0000000-0000-0000-0000-000000000000"}])
        if req.url.path == f"/api/v1/runs/{full}":
            return _json_response({"id": full, "status": "completed", "actor_id": "a", "events": []})
        return _json_response({"detail": "run not found"}, status=404)

    invoke = _runner_with_client(_client(handler))
    result = invoke(["run", "a58ddc62", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["id"] == full
    missing = invoke(["run", "zz"])
    assert missing.exit_code == 1 and "No run matches 'zz'" in missing.output


def test_json_flag_makes_errors_json():
    c = _client(lambda req: _json_response({"detail": "nope"}, status=404))
    result = _runner_with_client(c)(["bots", "--json"])
    assert result.exit_code == 1
    err = json.loads(result.stderr)
    assert err["status"] == 404 and err["exit_code"] == 1 and "nope" in err["error"]
    plain = _runner_with_client(c)(["bots"])
    assert plain.exit_code == 1 and plain.stderr.startswith("Error:")


def test_plugin_data_merges_with_options(plugin_server):
    invoke, calls = plugin_server
    result = invoke(["kanban", "update-card", "c1", "-d", '{"status": "review", "result": "from data"}',
                     "--status", "done"])
    assert result.exit_code == 0, result.output
    body = calls[-1][2]
    assert body == {"status": "done", "result": "from data", "expected_version": 4}
    bad = invoke(["kanban", "update-card", "c1", "-d", "[1]"])
    assert bad.exit_code == 2 and "JSON object" in bad.output
