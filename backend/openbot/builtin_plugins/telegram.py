"""Bundled transport plugin; conversation commands and delivery live in the host."""
import asyncio
import hmac
import re
from contextlib import asynccontextmanager, suppress
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, field_validator

from openbot.plugin_api import Command, CommandResult, Plugin, PluginSettings, host_from_request
from openbot.runtime.commands import NoArguments


class Settings(BaseModel):
    bot_token: SecretStr | None = Field(default=None, json_schema_extra={"env": "TELEGRAM_BOT_TOKEN"})
    webhook_secret: SecretStr | None = Field(default=None, json_schema_extra={"env": "TELEGRAM_WEBHOOK_SECRET"})
    transport: Literal["long_polling", "webhook"] = Field(default="long_polling", json_schema_extra={"env": "TELEGRAM_TRANSPORT"})
    allowed_users: list[str] = Field(default_factory=list, description="Telegram user IDs allowed to send messages. Empty denies all.")
    operator_users: list[str] = Field(default_factory=list, description="Allowed users who may attach to any Openbot conversation.")
    default_bot: str | None = None

    @field_validator("bot_token")
    @classmethod
    def valid_token(cls, value):
        if value is not None and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", value.get_secret_value()):
            raise ValueError("invalid Telegram bot token")
        return value



async def welcome(ctx, args):
    return CommandResult("Welcome, traveller. Speak thy purpose, or use /help to choose a path.")


class Telegram:
    def __init__(self, host, client):
        self.host, self.client = host, client
        self.offset = 0
        self.menu_token = None

    async def api(self, method, payload, settings):
        if not settings.bot_token:
            raise ValueError("Telegram token is not configured")
        response = await self.client.post(
            f"https://api.telegram.org/bot{settings.bot_token.get_secret_value()}/{method}", json=payload)
        if response.status_code != 200:
            raise ValueError("Telegram request failed")
        body = response.json()
        if not body.get("ok"):
            raise ValueError("Telegram request failed")
        return body.get("result")

    async def send(self, chat_id, text):
        settings = await self.host.settings.global_settings()
        # Plain text avoids interpreting untrusted model output as markup.
        for start in range(0, len(text), 4000):
            await self.api("sendMessage", {"chat_id": chat_id, "text": text[start:start + 4000]}, settings)

    async def receive(self, update):
        settings = await self.host.settings.global_settings()
        message = update.get("message") or {}
        sender, chat = message.get("from", {}), message.get("chat", {})
        user_id = str(sender.get("id", ""))
        text = message.get("text")
        if (not isinstance(text, str) or not text or chat.get("type") != "private"
                or user_id not in settings.allowed_users or "update_id" not in update):
            return
        # Telegram command suffixes target a bot; only accept our own username.
        match = re.match(r"^/([a-zA-Z0-9_]+)@([a-zA-Z0-9_]+)(?=\s|$)", text)
        if match:
            me = await self.api("getMe", {}, settings)
            if match[2].lower() != me["username"].lower():
                return
            text = "/" + match[1] + text[match.end():]
        await self.host.channels.receive(chat_id=chat["id"], sender_id=user_id,
            name=sender.get("first_name", "Telegram"), text=text, message_id=str(update["update_id"]),
            operator=user_id in settings.operator_users, default_bot=settings.default_bot)

    async def poll(self):
        while True:
            try:
                settings = await self.host.settings.global_settings()
                if settings.bot_token and settings.bot_token != self.menu_token:
                    await self.api("setMyCommands", {"commands": [{"command": c["name"],
                        "description": c["description"][:256]} for c in self.host.channels.command_catalog()]}, settings)
                    self.menu_token = settings.bot_token
                if not settings.bot_token or settings.transport != "long_polling":
                    await asyncio.sleep(3)
                    continue
                updates = await self.api("getUpdates", {"timeout": 25, "offset": self.offset}, settings)
                for update in updates:
                    await self.receive(update)
                    self.offset = update["update_id"] + 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- never log token-bearing HTTP exceptions
                # Tokens are part of Telegram URLs; never log HTTP exception details.
                await asyncio.sleep(3)


@asynccontextmanager
async def background(host):
    # Import the former core UI settings once, without overriding deployment pins.
    _, revision = await host.settings.read("global")
    if not revision:
        old = host.settings.services.settings
        values = {"bot_token": old.telegram_bot_token, "webhook_secret": old.telegram_webhook_secret,
                  "transport": old.telegram_transport}
        values = {k: v for k, v in values.items() if v is not None and k not in host.settings.pins("global")}
        if values:
            await host.settings.write("global", "", values, 0)
    async with httpx.AsyncClient(timeout=35) as client:
        transport = Telegram(host, client)
        host.state["transport"] = transport
        host.channels.set_sender(transport.send)
        task = asyncio.create_task(transport.poll(), name="telegram-input")
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


public = APIRouter(prefix="/telegram")


@public.post("/webhook")
async def webhook(request: Request):
    host = host_from_request(request, "telegram")
    settings = await host.settings.global_settings()
    if settings.transport != "webhook" or not settings.webhook_secret or not settings.bot_token:
        raise HTTPException(503, "Telegram webhook is not configured")
    if not hmac.compare_digest(settings.webhook_secret.get_secret_value(),
                               request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")):
        raise HTTPException(403, "Invalid webhook secret")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Invalid JSON object")
    except ValueError:
        raise HTTPException(400, "Invalid JSON") from None
    await host.state["transport"].receive(body)
    return {"status": "ok"}


plugin = Plugin(id="telegram", settings=PluginSettings(global_=Settings), background=background,
                public_router=public, commands=(Command(name="start", description="Welcome message",
                arguments=NoArguments, handler=welcome),))
