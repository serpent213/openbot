from fastapi import APIRouter, Depends, Header

from openbot import boards
from openbot.api.deps import get_services, get_session

router = APIRouter(prefix="/boards", tags=["boards"])


async def operator(handle: str | None = Header(default=None, alias="X-OpenBot-Actor"),
                   session=Depends(get_session)):
    return await boards.actor_for(session, handle)


@router.get("", response_model=list[boards.BoardOut])
async def list_boards(session=Depends(get_session)):
    return await boards.list_boards(session)


@router.post("", response_model=boards.BoardOut, status_code=201)
async def create_board(body: boards.BoardCreate, actor=Depends(operator),
                       session=Depends(get_session), services=Depends(get_services)):
    board = await boards.create_board(session, body)
    await boards.publish(services, board.id)
    return board


@router.get("/cards/{card_id}", response_model=boards.CardOut)
async def get_card(card_id: str, session=Depends(get_session)):
    return await boards.get_card(session, card_id)


@router.get("/cards/{card_id}/history", response_model=list[boards.EventOut])
async def history(card_id: str, session=Depends(get_session)):
    return await boards.card_history(session, card_id)


@router.patch("/cards/{card_id}", response_model=boards.CardOut)
async def update_card(card_id: str, body: boards.CardUpdate, actor=Depends(operator),
                      session=Depends(get_session), services=Depends(get_services)):
    card = await boards.update_card(session, card_id, body, actor)
    await boards.publish(services, card.board_id, card.id)
    return card


@router.post("/cards/{card_id}/claim", response_model=boards.CardOut)
async def claim_card(card_id: str, actor=Depends(operator), session=Depends(get_session),
                     services=Depends(get_services)):
    card = await boards.claim_card(session, card_id, actor)
    await boards.publish(services, card.board_id, card.id)
    return card


@router.get("/{board_id}/cards", response_model=list[boards.CardOut])
async def list_cards(board_id: str, session=Depends(get_session)):
    return await boards.list_cards(session, board_id)


@router.post("/{board_id}/cards", response_model=boards.CardOut, status_code=201)
async def create_card(board_id: str, body: boards.CardCreate, actor=Depends(operator),
                      session=Depends(get_session), services=Depends(get_services)):
    card = await boards.create_card(session, board_id, body, actor)
    await boards.publish(services, card.board_id, card.id)
    return card
