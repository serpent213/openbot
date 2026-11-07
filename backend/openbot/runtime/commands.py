"""Explicit human commands, shared by web chat and transport plugins."""
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from openbot.db.models import Actor, Thread, ThreadParticipant
from openbot.runtime.delivery import create_thread


@dataclass(frozen=True, kw_only=True)
class Command:
    name: str
    description: str
    arguments: type[BaseModel]
    handler: Callable[["CommandContext", BaseModel], Awaitable["CommandResult"]]


@dataclass(frozen=True)
class CommandResult:
    text: str
    thread_id: str | None = None


@dataclass
class CommandContext:
    services: object
    session: object
    actor: Actor
    thread_id: str
    operator: bool = False

    async def visible_thread(self, thread_id):
        query = select(Thread).where(Thread.id == thread_id, Thread.deleted_at.is_(None))
        if not self.operator:
            query = query.join(ThreadParticipant).where(ThreadParticipant.actor_id == self.actor.id)
        return await self.session.scalar(query)


class NoArguments(BaseModel):
    pass


class AttachArguments(BaseModel):
    thread_id: str


async def help_command(ctx, args):
    return CommandResult("Commands available on this road:\n" + "\n".join(
        f"/{c.name} — {c.description}" for c in commands(ctx.services).values()))


async def new_command(ctx, args):
    current = await ctx.visible_thread(ctx.thread_id)
    thread = await create_thread(ctx.services, ctx.session, title="New conversation", handles=[],
                                 created_by=ctx.actor, include_human=ctx.operator, kind="chat", commit=False)
    if current:
        thread.default_bot_actor_id = current.default_bot_actor_id
        thread.working_directory = current.working_directory
    return CommandResult("A new road opens; the earlier chronicle is preserved.", thread.id)


async def attach_command(ctx, args):
    thread = await ctx.visible_thread(args.thread_id)
    if not thread:
        return CommandResult("That conversation is unavailable to thee.")
    return CommandResult(f"Now following {thread.title} ({thread.id}).", thread.id)


async def threads_command(ctx, args):
    query = select(Thread).where(Thread.deleted_at.is_(None))
    if not ctx.operator:
        query = query.join(ThreadParticipant).where(ThreadParticipant.actor_id == ctx.actor.id)
    rows = (await ctx.session.scalars(query.order_by(Thread.updated_at.desc()).limit(30))).all()
    return CommandResult("Conversations:\n" + "\n".join(f"{t.id}  {t.title}" for t in rows))


async def status_command(ctx, args):
    thread = await ctx.visible_thread(ctx.thread_id)
    return CommandResult(f"Following {thread.title} ({thread.id})." if thread else "No conversation is attached.")


BUILTINS = (
    Command(name="help", description="Show commands", arguments=NoArguments, handler=help_command),
    Command(name="new", description="Create and follow a new conversation", arguments=NoArguments, handler=new_command),
    Command(name="attach", description="Follow an existing thread: /attach <thread-id>", arguments=AttachArguments, handler=attach_command),
    Command(name="threads", description="List accessible conversations (latest 30)", arguments=NoArguments, handler=threads_command),
    Command(name="status", description="Show the current conversation", arguments=NoArguments, handler=status_command),
)


def commands(services):
    from openbot.plugins import configured_plugins
    result = {}
    for command in (*BUILTINS, *(c for p in configured_plugins(services) for c in p.commands)):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", command.name) or command.name in result:
            raise ValueError(f"invalid or duplicate command: {command.name}")
        result[command.name] = command
    return result


async def dispatch(ctx, text):
    if not text.startswith("/") or text.startswith("//"):
        return None
    # Never invoke this function for model replies or tool output.
    try:
        words = shlex.split(text[1:])
        if not words:
            return CommandResult("Use /help to see the paths available.")
        command = commands(ctx.services).get(words[0])
        if command is None:
            return CommandResult(f"Unknown command: /{words[0]}. Use /help.")
        fields = list(command.arguments.model_fields)
        if len(words) - 1 > len(fields):
            raise ValueError("too many arguments")
        arguments = command.arguments.model_validate(dict(zip(fields, words[1:])), extra="forbid")
    except (ValueError, ValidationError):
        return CommandResult("Invalid arguments. Use /help; quote arguments containing spaces.")
    return await command.handler(ctx, arguments)
