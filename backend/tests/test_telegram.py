from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openbot.builtin_plugins.telegram import Settings, Telegram


def transport(**settings):
    config = Settings(**settings)
    host = SimpleNamespace(settings=SimpleNamespace(global_settings=AsyncMock(return_value=config)),
                           channels=SimpleNamespace(receive=AsyncMock()))
    return Telegram(host, AsyncMock()), host


def update(text="hello", user=42, chat_type="private"):
    return {"update_id": 123, "message": {"from": {"id": user, "first_name": "Frodo"},
        "chat": {"id": user, "type": chat_type}, "text": text}}


async def test_allowed_private_message_maps_to_host():
    tg, host = transport(allowed_users=["42"], operator_users=["42"])
    await tg.receive(update())
    host.channels.receive.assert_awaited_once()
    args = host.channels.receive.call_args.kwargs
    assert args["operator"] and args["chat_id"] == 42 and args["message_id"] == "123"


@pytest.mark.parametrize("message", [update(user=43), update(chat_type="group"), {"update_id": 1, "edited_message": update()["message"]}])
async def test_unapproved_or_unsupported_updates_do_not_enter_host(message):
    tg, host = transport(allowed_users=["42"])
    await tg.receive(message)
    host.channels.receive.assert_not_awaited()


async def test_bot_command_suffix():
    tg, host = transport(allowed_users=["42"])
    tg.api = AsyncMock(return_value={"username": "mybot"})
    await tg.receive(update("/help@otherbot"))
    host.channels.receive.assert_not_awaited()
    await tg.receive(update("/help@mybot"))
    assert host.channels.receive.call_args.kwargs["text"] == "/help"


async def test_send_chunks_and_checks_errors():
    tg, _host = transport(bot_token="123:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg")
    tg.api = AsyncMock()
    await tg.send("42", "x" * 8001)
    assert tg.api.await_count == 3
    assert max(len(c.args[1]["text"]) for c in tg.api.call_args_list) == 4000


async def test_webhook_requires_secret_and_allowlist(client, services):
    host = services.plugin_hosts["telegram"]
    body = update()
    assert (await client.post("/api/v1/telegram/webhook", json=body)).status_code == 503
    _, rev = await host.settings.read("global")
    await host.settings.write("global", "", {"transport": "webhook", "bot_token": "123:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg",
        "webhook_secret": "shared", "allowed_users": ["42"]}, rev)
    assert (await client.post("/api/v1/telegram/webhook", json=body)).status_code == 403
    host.state["transport"].receive = AsyncMock()
    assert (await client.post("/api/v1/telegram/webhook", json=body,
        headers={"X-Telegram-Bot-Api-Secret-Token": "shared"})).status_code == 200
    host.state["transport"].receive.assert_awaited_once_with(body)
