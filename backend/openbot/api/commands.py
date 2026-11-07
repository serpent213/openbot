from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from openbot.api.deps import get_services, get_session
from openbot.runtime.commands import CommandContext, commands, dispatch
from openbot.runtime.delivery import human_actor
from openbot.services import Services

router = APIRouter(prefix="/commands", tags=["commands"])


class CommandInput(BaseModel):
    thread_id: str
    text: str = Field(max_length=10000)


@router.get("")
async def catalog(services: Services = Depends(get_services)):
    return [{"name": c.name, "description": c.description, "arguments": c.arguments.model_json_schema()}
            for c in commands(services).values()]


@router.post("")
async def execute(body: CommandInput, services: Services = Depends(get_services),
                  session: AsyncSession = Depends(get_session)):
    actor = await human_actor(session)
    ctx = CommandContext(services, session, actor, body.thread_id, operator=True)
    if not await ctx.visible_thread(body.thread_id):
        raise HTTPException(404, "thread not found")
    result = await dispatch(ctx, body.text)
    if result is None:
        raise HTTPException(422, "not a command")
    await session.commit()
    return {"text": result.text, "thread_id": result.thread_id}
