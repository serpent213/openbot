import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import select

from openbot.boards import CardUpdate
from openbot.db.models import Actor, BoardCard, BoardCardEvent
from openbot.db.session import (
    downgrade_migrations,
    make_engine,
    make_session_factory,
    run_migrations,
)
from openbot.tools.builtin.boards import card_claim, card_get, card_update

HEADERS = {"X-OpenBot-Actor": "you"}


async def make_card(client):
    response = await client.post("/api/v1/boards", headers=HEADERS, json={"name": "Release"})
    assert response.status_code == 201, response.text
    board = response.json()
    response = await client.post(f"/api/v1/boards/{board['id']}/cards", headers=HEADERS,
                                 json={"title": "Review changes", "description": "Check the release."})
    assert response.status_code == 201, response.text
    return response.json()


async def patch(client, card, **changes):
    return await client.patch(f"/api/v1/boards/cards/{card['id']}", headers=HEADERS,
                               json={"expected_version": card["version"], **changes})


async def test_task_lifecycle_and_history(client):
    card = await make_card(client)
    assert card["status"] == "backlog"
    card = (await patch(client, card, status="ready")).json()
    path = f"/api/v1/boards/cards/{card['id']}"
    card = (await client.post(f"{path}/claim", headers=HEADERS)).json()
    assert card["status"] == "doing" and card["assignee_id"]
    # A retried claim is idempotent, including its audit history.
    assert (await client.post(f"{path}/claim", headers=HEADERS)).json() == card
    assert (await patch(client, card, status="review")).status_code == 422
    card = (await patch(client, card, status="review", result="Tests pass; see the discussion.")).json()
    card = (await patch(client, card, status="done")).json()
    history = (await client.get(f"{path}/history")).json()
    assert [e["version"] for e in history] == [1, 2, 3, 4, 5]
    assert [e["action"] for e in history] == ["created", "updated", "claimed", "updated", "updated"]
    assert all(e["actor_handle"] == "you" for e in history)
    assert (await client.get(f"/api/v1/boards/{card['board_id']}/cards")).json() == [card]


async def test_concurrent_claims_have_one_owner(client, services):
    async with services.session_factory() as session:
        session.add(Actor(kind="human", handle="other", name="Other"))
        await session.commit()
    card = await make_card(client)
    card = (await patch(client, card, status="ready")).json()
    path = f"/api/v1/boards/cards/{card['id']}"
    responses = await asyncio.gather(*[
        client.post(f"{path}/claim", headers={"X-OpenBot-Actor": handle})
        for handle in ("you", "other")
    ])
    assert sorted(r.status_code for r in responses) == [200, 409]
    history = (await client.get(f"{path}/history")).json()
    assert len([e for e in history if e["action"] == "claimed"]) == 1


async def test_stale_edit_does_not_overwrite_or_append_history(client):
    original = await make_card(client)
    changed = (await patch(client, original, title="New title")).json()
    assert (await patch(client, original, title="Stale title")).status_code == 409
    path = f"/api/v1/boards/cards/{original['id']}"
    assert (await client.get(path)).json() == changed
    assert len((await client.get(f"{path}/history")).json()) == 2


async def test_validation_and_blocked_recovery(client):
    card = await make_card(client)
    assert (await patch(client, card, status="blocked")).status_code == 422
    assert (await patch(client, card, status="doing")).status_code == 422
    assert (await patch(client, card, title="  ")).status_code == 422
    assert (await patch(client, card, title=None)).status_code == 422
    assert (await patch(client, card, status="unexpected")).status_code == 422
    assert (await patch(client, card, thread_id="missing")).status_code == 422
    assert (await patch(client, card, assignee_handle="missing")).status_code == 403
    card = (await patch(client, card, status="blocked", blocked_reason="Waiting for a document")).json()
    assert card["blocked_reason"]
    card = (await patch(client, card, status="ready")).json()
    assert card["blocked_reason"] == ""


async def test_api_key_and_explicit_actor(client, settings):
    settings.openbot_api_key = "test-private-key"
    assert (await client.get("/api/v1/boards")).status_code == 401
    auth = {"X-API-Key": "test-private-key"}
    assert (await client.get("/api/v1/boards", headers=auth)).status_code == 200
    assert (await client.post("/api/v1/boards", headers=auth, json={"name": "Work"})).status_code == 401
    assert (await client.post("/api/v1/boards", headers={**auth, "X-OpenBot-Actor": "unknown"},
                              json={"name": "Work"})).status_code == 403


async def test_tools_and_ui_share_operations_and_history(client, services):
    card = await make_card(client)
    card = (await patch(client, card, status="ready")).json()
    runtime = SimpleNamespace(context=SimpleNamespace(services=services, actor_handle="you"))
    claimed = json.loads(await card_claim.coroutine(card["id"], runtime))
    assert claimed["status"] == "doing"
    updated = json.loads(await card_update.coroutine(card["id"],
        CardUpdate(expected_version=claimed["version"], status="review", result="Reviewed the release"), runtime))
    assert updated["status"] == "review"
    inspected = json.loads(await card_get.coroutine(card["id"], runtime))
    assert inspected["card"] == updated and len(inspected["history"]) == 4
    assert (await client.get(f"/api/v1/boards/cards/{card['id']}")).json() == updated


async def test_migration_and_restart_preserve_cards_and_history(tmp_path):
    from openbot import boards
    url = f"sqlite+aiosqlite:///{tmp_path}/migration.db"
    await run_migrations(url)
    engine = make_engine(url)
    try:
        async with make_session_factory(engine)() as session:
            actor = Actor(kind="human", handle="operator", name="Operator")
            session.add(actor)
            await session.commit()
            board = await boards.create_board(session, boards.BoardCreate(name="Work"))
            card = await boards.create_card(session, board.id, boards.CardCreate(title="A task"), actor)
            card_id = card.id
    finally:
        await engine.dispose()
    engine = make_engine(url)
    try:
        async with make_session_factory(engine)() as session:
            assert (await session.get(BoardCard, card_id)).title == "A task"
            assert (await session.scalar(select(BoardCardEvent))).actor_handle == "operator"
    finally:
        await engine.dispose()
    await downgrade_migrations(url, "0022")
    await run_migrations(url)
