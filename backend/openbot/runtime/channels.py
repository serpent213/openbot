"""Single-process channel ingress and durable, at-least-once outgoing delivery.

Transport plugins authenticate senders before calling receive. An explicit operator
flag grants the same conversation access as the web UI; ordinary senders can attach
only to threads in which they already participate.
"""
import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import timedelta

from sqlalchemy import select

from openbot.db.models import (
    Actor,
    ChannelBinding,
    ChannelDelivery,
    ChannelReceipt,
    ExternalProfile,
    Thread,
    ThreadParticipant,
    utcnow,
)
from openbot.runtime.commands import CommandContext, dispatch
from openbot.runtime.delivery import create_thread, post_message

log = logging.getLogger(__name__)


class ChannelHost:
    def __init__(self, service, channel):
        self._service, self.channel = service, channel

    def command_catalog(self):
        from openbot.runtime.commands import commands
        return [{"name": c.name, "description": c.description} for c in commands(self._service.services).values()]

    def set_sender(self, sender):
        self._service.senders[self.channel] = sender

    async def receive(self, *, chat_id, sender_id, name, text, message_id, operator=False,
                      default_bot=None):
        return await self._service.receive(self.channel, str(chat_id), str(sender_id), name,
                                           text, str(message_id), operator, default_bot)


class ChannelService:
    def __init__(self, services):
        self.services = services
        self.senders = {}
        self.lock = asyncio.Lock()
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self._loop(), name="channel-delivery")

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def receive(self, channel, chat_id, sender_id, name, text, message_id, operator, default_bot):
        # Serialises attachment changes with sends and concurrent webhook deliveries.
        async with self.lock, self.services.session_factory() as session:
            if await session.get(ChannelReceipt, (channel, message_id)):
                return
            # Keep Telegram's existing actors and thread associations on upgrade.
            handle = f"tg_{sender_id}" if channel == "telegram" else "ch_" + hashlib.sha256(
                f"{channel}:{sender_id}".encode()).hexdigest()[:28]
            actor = await session.scalar(select(Actor).where(Actor.handle == handle))
            if actor is None:
                actor = Actor(kind="external", handle=handle, name=name[:120] or channel,
                              external=ExternalProfile(webhook_url=None, webhook_secret=None))
                session.add(actor)
                await session.flush()
            binding = await session.scalar(select(ChannelBinding).where(
                ChannelBinding.channel == channel, ChannelBinding.chat_id == chat_id))
            if binding is not None:
                attached = await session.get(Thread, binding.thread_id)
                if attached is None or attached.deleted_at:
                    await session.delete(binding)
                    await session.flush()
                    binding = None
            if binding is None:
                thread = await session.scalar(select(Thread).where(
                    Thread.external_ref == f"{channel}:{chat_id}", Thread.deleted_at.is_(None)))
                if thread is None:
                    thread = await create_thread(self.services, session, title=f"{channel}: {actor.name}",
                        handles=[], created_by=actor, include_human=operator, default_bot_handle=default_bot,
                        commit=False)
                binding = ChannelBinding(channel=channel, chat_id=chat_id, thread_id=thread.id, revision=1)
                session.add(binding)
                await session.flush()
            # Membership is established by an authenticated message in this external chat.
            member = await session.scalar(select(ThreadParticipant).where(
                ThreadParticipant.thread_id == binding.thread_id, ThreadParticipant.actor_id == actor.id))
            if member is None:
                session.add(ThreadParticipant(thread_id=binding.thread_id, actor_id=actor.id))
                await session.flush()
            session.add(ChannelReceipt(channel=channel, external_id=message_id))
            ctx = CommandContext(self.services, session, actor, binding.thread_id, operator)
            result = await dispatch(ctx, text)
            if result is not None:
                if result.thread_id and result.thread_id != binding.thread_id:
                    binding.thread_id = result.thread_id
                    binding.revision += 1
                session.add(ChannelDelivery(binding_id=binding.id, revision=binding.revision, content=result.text))
                await session.commit()
            else:
                await post_message(self.services, session, thread_id=binding.thread_id, sender=actor,
                                   content=text[1:] if text.startswith("//") else text)

    async def drain(self):
        async with self.lock, self.services.session_factory() as session:
            rows = (await session.scalars(select(ChannelDelivery).where(
                ChannelDelivery.next_attempt <= utcnow()).order_by(ChannelDelivery.created_at).limit(20))).all()
            for row in rows:
                binding = await session.get(ChannelBinding, row.binding_id)
                thread = await session.get(Thread, binding.thread_id) if binding else None
                if not binding or not thread or thread.deleted_at or binding.revision != row.revision:
                    await session.delete(row)
                elif sender := self.senders.get(binding.channel):
                    try:
                        await asyncio.wait_for(sender(binding.chat_id, row.content), timeout=30)
                    except Exception:  # noqa: BLE001 -- transport exceptions may contain credentials
                        row.attempts += 1
                        row.next_attempt = utcnow() + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
                        # Do not log protocol responses: they can contain tokens or message bodies.
                        log.warning("channel delivery failed: %s attempt %d", binding.channel, row.attempts)
                    else:
                        await session.delete(row)
                await session.commit()

    async def _loop(self):
        while True:
            try:
                await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("channel delivery worker failed; retrying")
            await asyncio.sleep(1)


async def enqueue_replies(session, messages):
    """Called inside the message transaction, independent of the event bus."""
    for message in messages:
        if message.sender_kind != "bot" or not message.content:
            continue
        bindings = (await session.scalars(select(ChannelBinding).where(
            ChannelBinding.thread_id == message.thread_id))).all()
        for binding in bindings:
            session.add(ChannelDelivery(binding_id=binding.id, revision=binding.revision, content=message.content))
