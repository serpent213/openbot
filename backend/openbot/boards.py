"""Shared board operations for the HTTP API and agent tools.

Claims and edits use a versioned SQL update; the history is committed in the same
transaction. Board transitions never dispatch agents or execute external work.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import select, update

from openbot.db.models import Actor, Board, BoardCard, BoardCardEvent, Thread, utcnow

CardStatus = Literal["backlog", "ready", "doing", "review", "done", "blocked"]


class BoardError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class BoardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=10_000)


class CardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    thread_id: str | None = None


class CardUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1)
    title: str | SkipJsonSchema[None] = Field(default=None, min_length=1, max_length=200)
    description: str | SkipJsonSchema[None] = Field(default=None, max_length=20_000)
    status: CardStatus | SkipJsonSchema[None] = None
    assignee_handle: str | None = Field(default=None, max_length=32)
    thread_id: str | None = None
    result: str | SkipJsonSchema[None] = Field(default=None, max_length=40_000)
    blocked_reason: str | SkipJsonSchema[None] = Field(default=None, max_length=2_000)

    @field_validator("title", "description", "status", "result", "blocked_reason", mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if value is None:
            raise ValueError("omit unchanged fields; this field cannot be null")
        return value


class BoardOut(BoardCreate):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime


class CardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    board_id: str
    title: str
    description: str
    status: CardStatus
    assignee_id: str | None
    thread_id: str | None
    result: str
    blocked_reason: str
    version: int
    created_at: datetime
    updated_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    actor_handle: str
    action: str
    version: int
    changes: dict
    created_at: datetime


async def actor_for(session, handle: str | None) -> Actor:
    if not handle:
        raise BoardError(401, "X-OpenBot-Actor is required")
    actor = await session.scalar(select(Actor).where(Actor.handle == handle, Actor.enabled.is_(True)))
    if actor is None:
        raise BoardError(403, "unknown or disabled actor")
    return actor


async def get_board(session, board_id: str) -> Board:
    board = await session.get(Board, board_id)
    if board is None:
        raise BoardError(404, "board not found")
    return board


async def get_card(session, card_id: str) -> BoardCard:
    card = await session.get(BoardCard, card_id)
    if card is None:
        raise BoardError(404, "card not found")
    return card


async def list_boards(session):
    return (await session.scalars(select(Board).order_by(Board.created_at, Board.id))).all()


async def list_cards(session, board_id: str):
    await get_board(session, board_id)
    return (await session.scalars(select(BoardCard).where(BoardCard.board_id == board_id)
                                 .order_by(BoardCard.created_at, BoardCard.id))).all()


async def card_history(session, card_id: str):
    await get_card(session, card_id)
    return (await session.scalars(select(BoardCardEvent).where(BoardCardEvent.card_id == card_id)
                                 .order_by(BoardCardEvent.version))).all()


async def _thread_exists(session, thread_id):
    if thread_id is not None:
        thread = await session.get(Thread, thread_id)
        if thread is None or thread.deleted_at is not None:
            raise BoardError(422, "thread not found")


async def create_board(session, body: BoardCreate):
    board = Board(**body.model_dump())
    session.add(board)
    await session.commit()
    await session.refresh(board)
    return board


async def create_card(session, board_id: str, body: CardCreate, actor: Actor):
    await get_board(session, board_id)
    await _thread_exists(session, body.thread_id)
    card = BoardCard(board_id=board_id, **body.model_dump())
    session.add(card)
    await session.flush()
    session.add(BoardCardEvent(card_id=card.id, actor_handle=actor.handle, action="created",
                               version=card.version, changes={**body.model_dump(), "status": card.status}))
    await session.commit()
    await session.refresh(card)
    return card


async def _write(session, card, actor, version, changes, action):
    result = await session.execute(update(BoardCard).where(
        BoardCard.id == card.id, BoardCard.version == version
    ).values(**changes, version=version + 1, updated_at=utcnow())
      .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await session.rollback()
        raise BoardError(409, "card changed; reload it before retrying")
    session.add(BoardCardEvent(card_id=card.id, actor_handle=actor.handle, action=action,
                               version=version + 1, changes=changes))
    await session.commit()
    await session.refresh(card)
    return card


async def claim_card(session, card_id: str, actor: Actor):
    card = await get_card(session, card_id)
    if card.status == "doing" and card.assignee_id == actor.id:
        return card  # A lost response may safely be retried by the same actor.
    if card.status != "ready" or card.assignee_id not in (None, actor.id):
        raise BoardError(409, "only a ready card assigned to you or unassigned can be claimed")
    return await _write(session, card, actor, card.version,
                        {"status": "doing", "assignee_id": actor.id}, "claimed")


async def update_card(session, card_id: str, body: CardUpdate, actor: Actor):
    card = await get_card(session, card_id)
    if card.version != body.expected_version:
        raise BoardError(409, "card changed; reload it before retrying")
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if "assignee_handle" in changes:
        handle = changes.pop("assignee_handle")
        assignee = await actor_for(session, handle) if handle else None
        changes["assignee_id"] = assignee.id if assignee else None
    if "thread_id" in changes:
        await _thread_exists(session, changes["thread_id"])
    status = changes.get("status", card.status)
    if status == "blocked":
        if not changes.get("blocked_reason", card.blocked_reason):
            raise BoardError(422, "a blocked card needs a reason")
    else:
        changes["blocked_reason"] = ""
    if status in ("doing", "review") and not changes.get("assignee_id", card.assignee_id):
        raise BoardError(422, "assign the card before starting work or requesting review")
    if status in ("review", "done") and not changes.get("result", card.result):
        raise BoardError(422, "describe the result before requesting review or completing the card")
    changes = {k: v for k, v in changes.items() if getattr(card, k) != v}
    if not changes:
        return card
    return await _write(session, card, actor, body.expected_version, changes, "updated")


async def publish(services, board_id: str, card_id: str | None = None):
    await services.bus.publish("board.updated", None, {"board_id": board_id, "card_id": card_id})
