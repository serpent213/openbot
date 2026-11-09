"""Scoped retrieval, tools, reflection and scratchpads; legacy global defaults remain intact."""
import sys
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import HumanMessage
from sqlalchemy import select

from openbot.db.models import Actor, BotScratchpad, Run, Sandbox, SandboxScratchpad
from openbot.main import create_app
from openbot.runtime import memory
from openbot.runtime.actors import BotActor
from openbot.runtime.delivery import human_actor, post_message
from openbot.tools.builtin.scratchpad import load_scratchpad, scratchpad
from tests.test_sandbox_lifecycle import seed
from tests.test_sandboxes import configure

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux sandbox allocation fixtures")


async def test_api_memory_opt_in_is_false_by_default_and_snapshotted_on_new_runs(settings, tmp_path, monkeypatch):
    services, box = await configure(settings, str(tmp_path / "unused"), monkeypatch)
    bot, thread, _ = await seed(services, box)
    actor = BotActor(services.actors, bot.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(services=services)),
                               base_url="http://test", headers={"X-API-Key": "test-only"}) as client:
            listing = (await client.get("/api/v1/sandboxes")).json()
            assert listing["sandboxes"][0]["share_global_memory"] is False
            async with services.session_factory() as session:
                await post_message(services, session, thread_id=thread.id, sender=await human_actor(session), content="scope A", to_handles=["eng"])
            batch = await actor._pick()
            async with services.session_factory() as session:
                first = await session.get(Run, batch.run_id)
                assert memory.run_namespace(first) == memory.sandbox_namespace(bot.id, box.id)
            reply = await client.patch(f"/api/v1/sandboxes/{box.id}", json={"share_global_memory": True})
            assert reply.status_code == 200 and reply.json()["share_global_memory"] is True
            async with services.session_factory() as session:
                await post_message(services, session, thread_id=thread.id, sender=await human_actor(session), content="scope global", to_handles=["eng"])
            batch = await actor._pick()
            async with services.session_factory() as session:
                second = await session.get(Run, batch.run_id)
                assert memory.run_namespace(second) == memory.bot_namespace(bot.id)
                assert memory.run_namespace(await session.get(Run, first.id)) == memory.sandbox_namespace(bot.id, box.id)
            assert (await client.patch(f"/api/v1/sandboxes/{box.id}", json={"resource": "other"})).status_code == 422
    finally:
        await services.http_client.aclose()


async def test_memory_tools_and_retrieval_cannot_cross_scope(services):
    a, b, global_ns = memory.sandbox_namespace("eng", "A"), memory.sandbox_namespace("eng", "B"), memory.bot_namespace("eng")
    for namespace, value in ((a, "A-only"), (b, "B-only"), (global_ns, "global-only")):
        manage, _ = memory.memory_tools("eng", services.store, namespace=namespace)
        await manage.ainvoke({"content": value, "action": "create"})
    assert await memory.relevant_memories(services.store, "eng", "", namespace=a) == ["A-only"]
    assert await memory.relevant_memories(services.store, "eng", "", namespace=b) == ["B-only"]
    assert await memory.relevant_memories(services.store, "eng", "") == ["global-only"]
    _, search = memory.memory_tools("eng", services.store, namespace=a)
    result = str(await search.ainvoke({"query": "only"}))
    assert "A-only" in result and "B-only" not in result and "global-only" not in result


async def test_reflection_fallback_and_debounce_keep_scope(services, monkeypatch):
    bot = Actor(id="bot", kind="bot", handle="eng", name="Engineer")
    scopes = [memory.sandbox_namespace(bot.id, "A"), memory.sandbox_namespace(bot.id, "B")]
    attempted = []
    monkeypatch.setattr(services, "small_model_factory", lambda bot: "small")
    monkeypatch.setattr(services, "model_factory", lambda bot: "large")

    def manager(model, **kwargs):
        namespace = kwargs["namespace"]
        attempted.append((model, namespace))

        async def invoke(*args, **kw):
            if model == "small":
                raise RuntimeError("exercise fallback")
            await kwargs["store"].aput(namespace, "reflected", {"content": namespace[1]})
        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(memory, "create_memory_store_manager", manager)
    reflector = memory.MemoryReflector(services, delay=100)
    for scope in scopes:
        reflector.schedule(bot, [HumanMessage("remember this")], thread_id="same-thread", namespace=scope)
    await reflector.flush()
    assert attempted == [(model, scope) for scope in scopes for model in ("small", "large")]
    assert await memory.relevant_memories(services.store, bot.id, "", namespace=scopes[0]) == ["A"]
    assert await memory.relevant_memories(services.store, bot.id, "", namespace=scopes[1]) == ["B"]
    assert await memory.relevant_memories(services.store, bot.id, "") == []
    await reflector.shutdown()


async def test_scratchpad_is_independently_scoped_even_when_global_memory_is_shared(settings, tmp_path, monkeypatch):
    services, box = await configure(settings, str(tmp_path / "unused"), monkeypatch)
    bot, _, _ = await seed(services, box)
    async with services.session_factory() as session:
        other = Sandbox(name="other", resource="other", resource_key="other", resource_spec={})
        session.add_all([other, BotScratchpad(actor_id=bot.id, content="legacy global notes")])
        row = await session.get(Sandbox, box.id)
        row.share_global_memory = True
        await session.commit()
    try:
        def context(sandbox_id):
            return SimpleNamespace(context=SimpleNamespace(services=services, actor_id=bot.id, sandbox_id=sandbox_id))
        assert await scratchpad.coroutine("read", context(box.id)) == "Scratchpad is empty."
        await scratchpad.coroutine("write", context(box.id), "A notes")
        await scratchpad.coroutine("write", context(other.id), "B notes")
        assert await scratchpad.coroutine("read", context(box.id)) == "A notes"
        assert await scratchpad.coroutine("read", context(other.id)) == "B notes"
        assert await scratchpad.coroutine("read", context(None)) == "legacy global notes"
        async with services.session_factory() as session:
            assert await load_scratchpad(session, bot.id, box.id) == "A notes"
            assert len((await session.scalars(select(SandboxScratchpad))).all()) == 2
    finally:
        await services.http_client.aclose()
