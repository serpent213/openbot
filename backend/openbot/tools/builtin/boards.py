"""Generic board tools; use the same operations and validation as the web UI."""
import json

from langchain.tools import ToolRuntime, tool
from pydantic import ValidationError

from openbot import boards
from openbot.tools.context import RunContext


@tool
async def board_list(runtime: ToolRuntime[RunContext]) -> str:
    """List shared project boards and their IDs."""
    async with runtime.context.services.session_factory() as session:
        rows = await boards.list_boards(session)
        return json.dumps([boards.BoardOut.model_validate(b).model_dump(mode="json") for b in rows])


@tool
async def card_list(board_id: str, runtime: ToolRuntime[RunContext]) -> str:
    """List a board's cards, including current versions, assignments and results."""
    async with runtime.context.services.session_factory() as session:
        try:
            rows = await boards.list_cards(session, board_id)
            return json.dumps([boards.CardOut.model_validate(c).model_dump(mode="json") for c in rows])
        except boards.BoardError as e:
            return f"Error: {e}"


@tool
async def card_get(card_id: str, runtime: ToolRuntime[RunContext]) -> str:
    """Read a card and its change history before editing it or reviewing its result."""
    async with runtime.context.services.session_factory() as session:
        try:
            card = await boards.get_card(session, card_id)
            events = await boards.card_history(session, card_id)
            return json.dumps({"card": boards.CardOut.model_validate(card).model_dump(mode="json"),
                "history": [boards.EventOut.model_validate(e).model_dump(mode="json") for e in events]})
        except boards.BoardError as e:
            return f"Error: {e}"


@tool
async def card_create(board_id: str, title: str, runtime: ToolRuntime[RunContext],
                      description: str = "") -> str:
    """Create a backlog card linked to this discussion. Use card_update to make it ready."""
    ctx = runtime.context
    async with ctx.services.session_factory() as session:
        try:
            actor = await boards.actor_for(session, ctx.actor_handle)
            card = await boards.create_card(session, board_id,
                boards.CardCreate(title=title, description=description, thread_id=ctx.thread_id), actor)
            await boards.publish(ctx.services, board_id, card.id)
            return boards.CardOut.model_validate(card).model_dump_json()
        except (boards.BoardError, ValidationError) as e:
            return f"Error: {e}"


@tool
async def card_claim(card_id: str, runtime: ToolRuntime[RunContext]) -> str:
    """Atomically claim a ready, unassigned card (or one assigned to you) and start work.
    A conflict means another actor owns it; don't override their assignment.
    """
    ctx = runtime.context
    async with ctx.services.session_factory() as session:
        try:
            actor = await boards.actor_for(session, ctx.actor_handle)
            card = await boards.claim_card(session, card_id, actor)
            await boards.publish(ctx.services, card.board_id, card.id)
            return boards.CardOut.model_validate(card).model_dump_json()
        except boards.BoardError as e:
            return f"Error: {e}"


@tool
async def card_update(card_id: str, changes: boards.CardUpdate,
                      runtime: ToolRuntime[RunContext]) -> str:
    """Edit a card using its expected_version from card_get. Omit unchanged fields.
    Set assignee_handle to assign/handoff, or null to release. Statuses: backlog,
    ready, doing, review, done, blocked. Review/done need a result; blocked needs
    blocked_reason. Only assignee_handle and thread_id accept null. Clear text
    with an empty string; leaving blocked status clears blocked_reason automatically.
    Updates never wake bots: send a message separately to delegate.
    """
    ctx = runtime.context
    async with ctx.services.session_factory() as session:
        try:
            actor = await boards.actor_for(session, ctx.actor_handle)
            card = await boards.update_card(session, card_id, changes, actor)
            await boards.publish(ctx.services, card.board_id, card.id)
            return boards.CardOut.model_validate(card).model_dump_json()
        except boards.BoardError as e:
            return f"Error: {e}"


BOARD_TOOLS = [board_list, card_list, card_get, card_create, card_claim, card_update]
