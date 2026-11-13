"""Explicit operator home clearing. Never retry partial/uncertain destructive work."""
import asyncio

from sqlalchemy import select, update

from openbot.db.models import (
    Actor,
    InboxItem,
    Run,
    Sandbox,
    SandboxExecution,
    Thread,
    new_id,
    utcnow,
)
from openbot.sandboxes import protocol, runtime


async def clear_home(services, sandbox_id, *, keep_dotfiles, confirmation, generation=None):
    ident = new_id()
    async with services.session_factory() as session:
        box = await session.get(Sandbox, sandbox_id)
        if (box is None or box.state != "active" or confirmation != box.name
                or (generation is not None and generation != box.generation)):
            raise protocol.SandboxRejected("select an active sandbox and confirm its current name and generation")
        slot = runtime.resource(services.settings, box.resource)
        if slot.model_dump() != box.resource_spec:
            raise protocol.SandboxRejected("sandbox inventory changed")
        changed = await session.execute(update(Sandbox).where(
            Sandbox.id == box.id, Sandbox.state == "active", Sandbox.generation == box.generation,
            Sandbox.name == confirmation).values(state="clearing", generation=Sandbox.generation + 1))
        if changed.rowcount != 1:
            raise protocol.SandboxRejected("sandbox changed; reload before clearing")
        threads = select(Thread.id).where(Thread.sandbox_id == box.id)
        await session.execute(update(InboxItem).where(InboxItem.thread_id.in_(threads), InboxItem.status == "queued",
                              InboxItem.actor_id.in_(select(Actor.id).where(Actor.kind == "bot")))
                              .values(status="cancelled", processed_at=utcnow()))
        runs = (await session.scalars(select(Run.id).where(Run.sandbox_id == box.id,
                    Run.status.in_(("queued", "running", "waiting_human"))))).all()
        session.add(SandboxExecution(id=ident, sandbox_id=box.id, generation=box.generation,
                                    operation="clear_home_keep_dotfiles" if keep_dotfiles else "clear_home_all"))
        await session.commit()
    outcome = "unknown"
    try:
        if services.actors:
            for run_id in runs:
                await services.actors.cancel_run(run_id)
        async with asyncio.timeout(60):
            while True:
                async with services.session_factory() as session:
                    current = await session.get(Sandbox, sandbox_id)
                    if current.active_run_id is None:
                        break
                await asyncio.sleep(.05)
        result = await protocol.request(slot.socket_path, slot.uid, {
            "id": ident, "resource": box.resource, "operation": "clear_home", "keep_dotfiles": keep_dotfiles,
        })
        outcome = "completed"
        return result
    except protocol.SandboxRejected:
        outcome = "rejected"
        raise
    finally:
        async with services.session_factory() as session:
            item = await session.get(SandboxExecution, ident)
            item.state = outcome
            if outcome == "completed":
                # Cancellation may interrupt a waiter's status transaction before it
                # ever acquires a worker lease. After proven quiescence settle those
                # old snapshots too, rather than leaving resumable queued rows.
                await session.execute(update(Run).where(Run.id.in_(runs),
                    Run.status.in_(("queued", "running", "waiting_human"))).values(status="cancelled", finished_at=utcnow()))
                await session.execute(update(InboxItem).where(InboxItem.run_id.in_(runs),
                    InboxItem.status.in_(("queued", "processing"))).values(status="cancelled", processed_at=utcnow()))
            await session.execute(update(Sandbox).where(Sandbox.id == sandbox_id).values(
                state="active" if outcome == "completed" else "quarantined"))
            await session.commit()
