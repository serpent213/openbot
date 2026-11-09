"""Core policy and durable intent around an untrusted, replaceable worker backend."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from functools import wraps

from sqlalchemy import select, update

from openbot.db.models import Sandbox, SandboxExecution, new_id
from openbot.sandboxes import protocol
from openbot.sandboxes.policy import WORKER_TOOLS
from openbot.sandboxes.protocol import SandboxError


def resource(settings, name):
    if not settings.sandboxes_enabled or not settings.openbot_api_key:
        raise SandboxError("sandbox execution is disabled or API authentication is missing")
    slot = settings.sandbox_resources.get(name)
    if slot is None:
        raise SandboxError("sandbox resource is not provisioned")
    if not hasattr(os, "getuid") or os.getuid() in (0, slot.uid):
        raise SandboxError("sandbox controller and worker require distinct non-root UIDs")
    return slot


def validate_bot(bot):
    unknown = set(bot.bot.tool_names or ()) - WORKER_TOOLS.keys()
    if unknown:
        raise SandboxError("tools unavailable in sandbox prototype: " + ", ".join(sorted(unknown)))
    if (bot.bot.load_repository_instructions or bot.bot.skill_names
            or (bot.bot.model_settings or {}).get("web_search")):
        raise SandboxError("sandbox prototype disables repository/skill discovery and hosted web tools")
    if bot.bot.provider not in ("auto", "openai", "anthropic", "openrouter", "xai", "ollama"):
        raise SandboxError("coding CLI and custom model backends are unavailable in sandbox prototype")


async def recover(services):
    """Single-controller startup: uncertain work quarantines its binding, never retries."""
    async with services.session_factory() as session:
        pending = (await session.scalars(select(SandboxExecution).where(
            SandboxExecution.state == "pending"))).all()
        ids = {item.sandbox_id for item in pending}
        for item in pending:
            item.state = "unknown"
        rows = (await session.scalars(select(Sandbox).where(
            (Sandbox.active_run_id.is_not(None)) | (Sandbox.id.in_(ids))))).all()
        for row in rows:
            row.state = "quarantined"
            row.active_run_id = None
        await session.commit()


@asynccontextmanager
async def binding(services, run, thread, bot):
    if not thread.sandbox_id:
        if services.settings.sandboxes_enabled or run.sandbox_id:
            raise SandboxError("sandbox mode requires a bound thread; local fallback refused")
        yield
        return
    if run.sandbox_id != thread.sandbox_id:
        raise SandboxError("run sandbox does not match immutable thread binding")
    validate_bot(bot)
    claimed = False
    try:
        while not claimed:
            async with services.session_factory() as session:
                box = await session.get(Sandbox, thread.sandbox_id)
                if box is None or box.state != "active" or box.generation != run.sandbox_generation:
                    raise SandboxError("sandbox binding is unavailable or stale")
                slot = resource(services.settings, box.resource)
                if box.resource_spec != slot.model_dump():
                    raise SandboxError("sandbox inventory changed; binding requires operator reconciliation")
                changed = await session.execute(update(Sandbox).where(
                    Sandbox.id == box.id, Sandbox.state == "active",
                    Sandbox.generation == run.sandbox_generation,
                    Sandbox.active_run_id.is_(None)).values(active_run_id=run.id))
                await session.commit()
                claimed = changed.rowcount == 1
            if not claimed:
                await asyncio.sleep(0.1)
        await execute(services, box.id, run.id, "probe", {}, thread.working_directory)
        yield
    finally:
        if claimed:
            async with services.session_factory() as session:
                await session.execute(update(Sandbox).where(
                    Sandbox.id == thread.sandbox_id, Sandbox.active_run_id == run.id
                ).values(active_run_id=None))
                await session.commit()


async def execute(services, sandbox_id, run_id, operation, args, directory):
    # LangGraph can invoke several tools in one model turn concurrently. Serialize
    # those operations as well as runs; a worker's busy refusal is not a retry signal.
    lock = services._sandbox_operations.setdefault(sandbox_id, asyncio.Lock())
    async with lock:
        return await _execute(services, sandbox_id, run_id, operation, args, directory)


async def _execute(services, sandbox_id, run_id, operation, args, directory):
    ident = new_id()
    async with services.session_factory() as session:
        box = await session.get(Sandbox, sandbox_id)
        if box is None or box.state != "active" or box.active_run_id != run_id:
            raise SandboxError("sandbox is not active for this run")
        slot = resource(services.settings, box.resource)
        if box.resource_spec != slot.model_dump():
            raise SandboxError("sandbox resource configuration changed")
        session.add(SandboxExecution(id=ident, sandbox_id=box.id, generation=box.generation,
                                     run_id=run_id, operation=operation))
        await session.commit()
    try:
        result = await protocol.request(slot.socket_path, slot.uid, {
            "id": ident, "resource": box.resource, "sandbox_id": box.id,
            "generation": box.generation, "operation": operation,
            "args": args, "directory": directory,
        })
        async with services.session_factory() as session:
            item = await session.get(SandboxExecution, ident)
            item.state = "completed"
            await session.commit()
        return result
    except BaseException:
        # A cancellation or lost response says nothing about external effects. Do not
        # save arguments/output here: they may contain credentials or private content.
        async with services.session_factory() as session:
            item = await session.get(SandboxExecution, ident)
            item.state = "unknown"
            box = await session.get(Sandbox, sandbox_id)
            box.state = "quarantined"
            await session.commit()
        raise


def tools(names):
    result = []
    for name in names:
        original = WORKER_TOOLS[name]

        def wrap(original):
            @wraps(original.coroutine)
            async def remote(*args, **kwargs):
                runtime = kwargs.pop("runtime")
                ctx = runtime.context
                if args:
                    raise SandboxError("sandbox tools require named arguments")
                return await execute(ctx.services, ctx.sandbox_id, ctx.run_id, original.name,
                                     kwargs, ctx.working_directory)
            return original.model_copy(update={"coroutine": remote})

        result.append(wrap(original))
    return result
