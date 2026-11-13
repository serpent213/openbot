"""Operator-only allocation and quarantine. No deletion, rebinding or recycling."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from openbot.api.deps import get_services, get_session
from openbot.db.models import Sandbox, SandboxExecution
from openbot.sandboxes.policy import WORKER_TOOLS
from openbot.sandboxes.runtime import SandboxError, inspect, resource

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


class SandboxCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    resource: str = Field(min_length=1, max_length=120)
    share_global_memory: bool = False


class SandboxUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    share_global_memory: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)


def output(box):
    return {key: getattr(box, key) for key in
            ("id", "name", "backend", "resource", "generation", "state", "active_run_id", "share_global_memory")}


@router.get("")
async def listing(session=Depends(get_session), services=Depends(get_services)):
    rows = (await session.scalars(select(Sandbox).order_by(Sandbox.name))).all()
    allocated = {row.resource for row in rows}
    from openbot.sandboxes.inventory import configured
    return {"enabled": services.settings.sandboxes_enabled,
            "default_sandbox_id": services.settings.sandbox_default_id,
            "host_execution_allowed": services.settings.sandbox_allow_host_execution,
            "capabilities": sorted(WORKER_TOOLS), "coding_cli": True,
            "concurrent_operations": True, "cleanup_requires": "delegated cgroup v2 and user/mount namespaces",
            "create_resources": False, "recycle": False,
            "resources": [{"id": name, "allocated": name in allocated, "externally_managed": True}
                          for name in services.settings.sandbox_resources],
            "sandboxes": [{**output(row), "configured": configured(services.settings, row)} for row in rows]}


@router.post("", status_code=201)
async def allocate(body: SandboxCreate, session=Depends(get_session), services=Depends(get_services)):
    try:
        slot = resource(services.settings, body.resource)
    except SandboxError as exc:
        raise HTTPException(422, str(exc)) from exc
    box = Sandbox(name=body.name, resource=body.resource, resource_key=f"linux:uid:{slot.uid}",
                  resource_spec=slot.model_dump(), share_global_memory=body.share_global_memory)
    session.add(box)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "sandbox name or resource already allocated; no recycling") from exc
    return output(box)


@router.patch("/{sandbox_id}")
async def configure(sandbox_id: str, body: SandboxUpdate, session=Depends(get_session)):
    box = await session.get(Sandbox, sandbox_id)
    if box is None:
        raise HTTPException(404, "sandbox not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None or (key == "name" and not value.strip()):
            raise HTTPException(422, "name and memory sharing cannot be empty")
        setattr(box, key, value.strip() if key == "name" else value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "sandbox name already exists") from exc
    return {**output(box), "notice": "memory policy applies to newly created runs; existing run snapshots are unchanged"}


def validate_inspection_target(settings, sandbox_id, host_execution):
    """Operator discovery must select a target; never infer host from a missing ID."""
    if sandbox_id and host_execution:
        raise HTTPException(422, "select either sandbox_id or host_execution, not both")
    if host_execution and not settings.sandbox_allow_host_execution:
        raise HTTPException(403, "host execution is disabled by the operator")
    if settings.sandboxes_enabled and not sandbox_id and not host_execution:
        raise HTTPException(409, "select sandbox_id or explicitly request allowed host_execution")


class HomeClear(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_dotfiles: bool
    confirmation: str
    generation: int = Field(ge=1)


@router.post("/{sandbox_id}/clear-home")
async def clear_home(sandbox_id: str, body: HomeClear, services=Depends(get_services)):
    from openbot.sandboxes.maintenance import clear_home as clear
    try:
        return await clear(services, sandbox_id, keep_dotfiles=body.keep_dotfiles,
                           confirmation=body.confirmation, generation=body.generation)
    except SandboxError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, TimeoutError) as exc:
        raise HTTPException(409, "home cleanup unconfirmed; sandbox quarantined, inspect before retrying") from exc


async def worker_inspect(services, sandbox_id, operation, args, directory=None):
    try:
        return await inspect(services, sandbox_id, operation, args, directory)
    except SandboxError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{sandbox_id}/skills")
async def skills(sandbox_id: str, directory: str | None = None, services=Depends(get_services)):
    return await worker_inspect(services, sandbox_id, "skills", {}, directory)


@router.post("/{sandbox_id}/retire")
async def retire(sandbox_id: str, session=Depends(get_session), services=Depends(get_services)):
    box = await session.get(Sandbox, sandbox_id)
    if box is None:
        raise HTTPException(404, "sandbox not found")
    box.state = "quarantined"
    await session.commit()
    if box.active_run_id and services.actors:
        await services.actors.cancel_run(box.active_run_id)
    return {**output(box), "cleanup_required": True,
            "notice": "quarantined, not reusable; operator must stop unit/cgroup and inspect effects/state"}


@router.get("/{sandbox_id}/executions")
async def executions(sandbox_id: str, session=Depends(get_session)):
    rows = (await session.scalars(select(SandboxExecution).where(
        SandboxExecution.sandbox_id == sandbox_id).order_by(SandboxExecution.created_at.desc()).limit(100))).all()
    return [{key: getattr(row, key) for key in
             ("id", "sandbox_id", "generation", "run_id", "operation", "state")} for row in rows]
