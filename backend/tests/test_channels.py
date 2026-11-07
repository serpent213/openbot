from unittest.mock import AsyncMock

from sqlalchemy import func, select

from openbot.db.models import (
    Actor,
    BotProfile,
    ChannelBinding,
    ChannelDelivery,
    ChannelReceipt,
    Message,
    Thread,
)
from openbot.runtime.channels import ChannelService
from openbot.runtime.delivery import create_thread, human_actor, post_message


async def receive(ch, text, message_id="1", sender="alice", operator=False):
    await ch.receive("test", sender, sender, sender, text, message_id, operator, None)


async def binding(services, chat_id="alice"):
    async with services.session_factory() as s:
        return await s.scalar(select(ChannelBinding).where(ChannelBinding.chat_id == chat_id))


async def test_duplicate_message_and_command_are_not_replayed(services):
    ch = ChannelService(services)
    await receive(ch, "Hello")
    await receive(ch, "Hello")
    async with services.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Message)) == 1
    old = (await binding(services)).thread_id
    await receive(ch, "/new", "2")
    new = (await binding(services)).thread_id
    await receive(ch, "/new", "2")
    assert old != new == (await binding(services)).thread_id
    async with services.session_factory() as s:
        assert await s.get(Thread, old)
        assert await s.scalar(select(func.count()).select_from(ChannelReceipt)) == 2


async def test_attach_requires_membership_unless_explicit_operator(services):
    ch = ChannelService(services)
    await receive(ch, "Private", sender="bob")
    target = (await binding(services, "bob")).thread_id
    await receive(ch, f"/attach {target}", "2")
    assert (await binding(services)).thread_id != target
    await receive(ch, f"/attach {target}", "3", operator=True)
    assert (await binding(services)).thread_id == target


async def test_switch_discards_old_queued_replies_and_retries_failures(services):
    ch = ChannelService(services)
    sender = AsyncMock(side_effect=OSError("offline"))
    ch.senders["test"] = sender
    await receive(ch, "/status")
    await ch.drain()
    async with services.session_factory() as s:
        row = await s.scalar(select(ChannelDelivery))
        assert row.attempts == 1
    await receive(ch, "/new", "2")
    sender.side_effect = None
    await ch.drain()
    assert sender.await_count == 2
    assert "new road" in sender.call_args.args[1]


async def test_model_slash_output_is_delivered_as_text(services):
    ch = ChannelService(services)
    await receive(ch, "hello")
    old = (await binding(services)).thread_id
    async with services.session_factory() as s:
        bot = Actor(kind="bot", handle="coder", name="Coder", bot=BotProfile(provider="auto", model=""))
        s.add(bot)
        await s.commit()
        await post_message(services, s, thread_id=old, sender=bot, content="/new")
    assert (await binding(services)).thread_id == old
    # Simulate restart: delivery is loaded from the database, not the event bus.
    restarted = ChannelService(services)
    sender = AsyncMock()
    restarted.senders["test"] = sender
    await restarted.drain()
    sender.assert_awaited_once_with("alice", "/new")


async def test_literal_slash_and_deleted_threads(services):
    ch = ChannelService(services)
    await receive(ch, "//status")
    async with services.session_factory() as s:
        message = await s.scalar(select(Message))
        assert message.content == "/status"
        thread = await s.get(Thread, message.thread_id)
        from openbot.db.models import utcnow
        thread.deleted_at = utcnow()
        await s.commit()
    # The next message must not disappear into a deleted conversation.
    await receive(ch, "hello again", "2")
    assert (await binding(services)).thread_id != message.thread_id


async def test_web_commands_and_registered_plugin_command(client, services):
    from pydantic import BaseModel

    from openbot.plugin_api import Command, CommandResult, Plugin
    class Args(BaseModel):
        text: str
    handler = AsyncMock(return_value=CommandResult("A message from the guild."))
    services.plugins.append(Plugin(id="example", commands=(Command(name="example_echo", description="Echo",
        arguments=Args, handler=handler),)))
    async with services.session_factory() as s:
        thread = await create_thread(services, s, title="Test", handles=[], created_by=await human_actor(s))
    result = await client.post("/api/v1/commands", json={"thread_id": thread.id, "text": '/example_echo "two words"'})
    assert result.status_code == 200
    assert handler.call_args.args[1].text == "two words"
    assert "example_echo" in {c["name"] for c in (await client.get("/api/v1/commands")).json()}
    result = await client.post("/api/v1/commands", json={"thread_id": thread.id, "text": "/new"})
    assert result.json()["thread_id"] != thread.id
