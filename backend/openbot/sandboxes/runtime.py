"""Core policy, run admission and durable intent around worker execution."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from functools import wraps
from types import SimpleNamespace

from sqlalchemy import select, update

from openbot.db.models import Sandbox, SandboxExecution, new_id
from openbot.sandboxes import protocol
from openbot.sandboxes.policy import WORKER_TOOLS
from openbot.sandboxes.protocol import SandboxError, SandboxRejected


def resource(settings, name):
    if not settings.sandboxes_enabled or not settings.openbot_api_key:
        raise SandboxRejected("sandbox execution is disabled or API authentication is missing")
    slot = settings.sandbox_resources.get(name)
    if slot is None:
        raise SandboxRejected("sandbox resource is not provisioned")
    if not hasattr(os, "getuid") or os.getuid() in (0, slot.uid):
        raise SandboxRejected("sandbox controller and worker require distinct non-root UIDs")
    return slot


def validate_bot(bot, settings=None):
    from openbot.runtime.cli_agents import adapter_for
    from openbot.tools.builtin.scratchpad import scratchpad
    from openbot.tools.builtin.todo import todo_write
    allowed = {*WORKER_TOOLS, scratchpad.name, todo_write.name, "schedule_message", "list_scheduled_messages"}
    unknown = set(bot.bot.tool_names or ()) - allowed
    if unknown:
        raise SandboxRejected("tools unavailable in sandbox: " + ", ".join(sorted(unknown)))
    if (bot.bot.model_settings or {}).get("web_search"):
        raise SandboxRejected("provider-hosted web tools are not adapted for sandbox mode; use worker HTTP")
    if (bot.bot.provider not in ("auto", "openai", "anthropic", "openrouter", "xai", "ollama")
            and adapter_for(bot.bot.provider, settings) is None):
        raise SandboxRejected("unknown or disabled coding/model backend")


async def quarantine(services, sandbox_id):
    async with services.session_factory() as session:
        await session.execute(update(Sandbox).where(Sandbox.id == sandbox_id).values(state="quarantined"))
        await session.commit()


async def recover(services):
    """Interrupted state is never replayed. Worker lease EOF independently stops children."""
    async with services.session_factory() as session:
        pending = (await session.scalars(select(SandboxExecution).where(SandboxExecution.state == "pending"))).all()
        ids = {item.sandbox_id for item in pending}
        for item in pending:
            item.state = "unknown"
        rows = (await session.scalars(select(Sandbox).where(
            (Sandbox.active_run_id.is_not(None)) | Sandbox.id.in_(ids)))).all()
        for row in rows:
            row.state, row.active_run_id = "quarantined", None
        await session.commit()


@asynccontextmanager
async def binding(services, run, thread, bot=None, *, wait=True):
    # Old NULL bindings remain blocked in sandbox mode. Host access requires an
    # explicit immutable thread choice, a matching run snapshot and boot policy.
    host = bool(getattr(thread, "host_execution", False))
    if bool(getattr(run, "host_execution", False)) != host:
        raise SandboxRejected("run host selection does not match immutable thread binding")
    if host and (thread.sandbox_id or run.sandbox_id):
        raise SandboxRejected("host execution cannot have a sandbox binding")
    if host and not services.settings.sandbox_allow_host_execution:
        raise SandboxRejected("host execution is disabled by the operator")
    if not thread.sandbox_id:
        if run.sandbox_id or (services.settings.sandboxes_enabled and not host):
            raise SandboxRejected("sandbox mode requires a bound thread; local fallback refused")
        yield
        return
    if run.sandbox_id != thread.sandbox_id:
        raise SandboxRejected("run sandbox does not match immutable thread binding")
    if bot is not None:
        validate_bot(bot, services.settings)
    claimed = False
    try:
        while not claimed:
            async with services.session_factory() as session:
                box = await session.get(Sandbox, thread.sandbox_id)
                if box is None or box.state != "active" or box.generation != run.sandbox_generation:
                    raise SandboxRejected("sandbox binding is unavailable or stale")
                slot = resource(services.settings, box.resource)
                if box.resource_spec != slot.model_dump():
                    raise SandboxRejected("sandbox inventory changed; operator reconciliation required")
                changed = await session.execute(update(Sandbox).where(
                    Sandbox.id == box.id, Sandbox.state == "active", Sandbox.generation == run.sandbox_generation,
                    Sandbox.active_run_id.is_(None)).values(active_run_id=run.id))
                # Fence cancellation during commit: release even if the database
                # committed before the await raised CancelledError.
                claimed = changed.rowcount == 1
                await session.commit()
            if not claimed:
                if not wait:
                    raise SandboxRejected("sandbox is busy")
                await asyncio.sleep(0.05)
        try:
            async with protocol.run_lease(slot.socket_path, slot.uid, {
                    "id": new_id(), "run_id": run.id, "resource": box.resource,
                    "sandbox_id": box.id, "generation": box.generation}):
                await execute(services, box.id, run.id, "probe", {}, thread.working_directory)
                yield
        except (SandboxRejected, asyncio.CancelledError):
            # Known rejection or a cancellation with confirmed whole-run cleanup.
            # Individual uncertain operations have their own recorded outcome.
            raise
        except BaseException as exc:
            await quarantine(services, box.id)
            if isinstance(exc, Exception):
                raise SandboxError("sandbox run/cleanup failed; binding quarantined for inspection") from exc
            raise
    finally:
        if claimed:
            async with services.session_factory() as session:
                await session.execute(update(Sandbox).where(
                    Sandbox.id == thread.sandbox_id, Sandbox.active_run_id == run.id).values(active_run_id=None))
                await session.commit()


async def execute(services, sandbox_id, run_id, operation, args, directory, *, on_event=None, skill_names=()):
    ident = new_id()
    async with services.session_factory() as session:
        box = await session.get(Sandbox, sandbox_id)
        if box is None or box.state != "active" or box.active_run_id != run_id:
            raise SandboxRejected("sandbox is not active for this run")
        slot = resource(services.settings, box.resource)
        if box.resource_spec != slot.model_dump():
            raise SandboxRejected("sandbox resource configuration changed")
        session.add(SandboxExecution(id=ident, sandbox_id=box.id, generation=box.generation,
                                    run_id=run_id, operation=operation))
        await session.commit()
    outcome = "unknown"
    try:
        result = await protocol.request(slot.socket_path, slot.uid, {
            "id": ident, "run_id": run_id, "resource": box.resource, "sandbox_id": box.id,
            "generation": box.generation, "operation": operation, "args": args, "directory": directory,
            "tool_output_cap": services.settings.tool_output_cap,
            "shell_output_cap": services.settings.shell_output_cap, "skill_names": list(skill_names),
        }, on_event=on_event)
        outcome = "completed"  # includes reported tool errors, not a success assertion
        return result
    except SandboxRejected:
        outcome = "rejected"
        raise
    finally:
        async with services.session_factory() as session:
            item = await session.get(SandboxExecution, ident)
            item.state = outcome
            if outcome == "unknown":
                await session.execute(update(Sandbox).where(Sandbox.id == sandbox_id).values(state="quarantined"))
            await session.commit()


async def inspect(services, sandbox_id, operation, args, directory=None):
    """Operator discovery uses a short exclusive lease, never a server filesystem/probe."""
    async with services.session_factory() as session:
        box = await session.get(Sandbox, sandbox_id)
        if box is None:
            raise SandboxRejected("sandbox not found")
    run = SimpleNamespace(id=new_id(), sandbox_id=box.id, sandbox_generation=box.generation)
    thread = SimpleNamespace(sandbox_id=box.id, working_directory=directory)
    async with binding(services, run, thread, wait=False):
        return await execute(services, box.id, run.id, operation, args, directory)


def tools(names):
    from openbot.tools.builtin.scheduling import SCHEDULING_TOOLS
    from openbot.tools.builtin.scratchpad import scratchpad
    from openbot.tools.builtin.todo import todo_write
    host_tools = {t.name: t for t in (scratchpad, todo_write, *SCHEDULING_TOOLS)}
    result = []
    for name in names:
        if name in host_tools:
            result.append(host_tools[name])
            continue
        original = WORKER_TOOLS[name]

        def wrap(original):
            @wraps(original.coroutine)
            async def remote(*args, **kwargs):
                runtime = kwargs.pop("runtime", None)
                # All routed schemas carry an injected ToolRuntime, including HTTP;
                # never invoke the original handler in the controller.
                ctx = runtime.context
                if args:
                    raise SandboxRejected("sandbox tools require named arguments")
                try:
                    return await execute(ctx.services, ctx.sandbox_id, ctx.run_id, original.name,
                                         kwargs, ctx.working_directory, skill_names=ctx.skill_names or ())
                except SandboxRejected as exc:
                    return f"error: {exc}"
            return original.model_copy(update={"coroutine": remote})

        result.append(wrap(original))
    return result
