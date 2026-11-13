"""Operator-only named credentials; responses never contain plaintext or ciphertext."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from openbot.api.deps import get_services, get_session
from openbot.db.models import Sandbox, SandboxCredential, SandboxCredentialGrant, utcnow
from openbot.sandboxes.credentials import validate_target

router = APIRouter(tags=["sandbox credentials"])


class CredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["env", "file"]
    target: str = Field(min_length=1, max_length=1000)
    value: SecretStr


class CredentialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    value: SecretStr | None = None


class Grants(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_ids: list[str] = Field(max_length=100)


def metadata(row):
    return {"id": row.id, "name": row.name, "kind": row.kind, "target": row.target, "is_set": True}


def encoded(services, value):
    value = value.get_secret_value()
    if not value or len(value.encode()) > 65536 or "\0" in value:
        raise HTTPException(422, "credential must be nonempty, at most 64 KiB, and contain no NUL")
    return services.secrets.encrypt(value)


async def idle(session, ids):
    """Serialize grant writers (including SQLite), fence queued runs and CLI reuse."""
    ids = set(ids)
    if not ids:
        return
    changed = await session.execute(update(Sandbox).where(
        Sandbox.id.in_(ids), Sandbox.state == "active", Sandbox.active_run_id.is_(None)
    ).values(generation=Sandbox.generation + 1, updated_at=utcnow()))
    if changed.rowcount != len(ids):
        await session.rollback()
        raise HTTPException(409, "all affected sandboxes must be active and idle; wait for runs and probes to finish")


@router.get("/sandbox-credentials")
async def listing(session=Depends(get_session)):
    rows = (await session.scalars(select(SandboxCredential).order_by(SandboxCredential.name))).all()
    grants = (await session.scalars(select(SandboxCredentialGrant))).all()
    return [{**metadata(row), "sandbox_ids": [g.sandbox_id for g in grants if g.credential_id == row.id]} for row in rows]


@router.post("/sandbox-credentials", status_code=201)
async def create(body: CredentialCreate, session=Depends(get_session), services=Depends(get_services)):
    try:
        validate_target(body.kind, body.target)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not body.name.strip():
        raise HTTPException(422, "credential name cannot be blank")
    row = SandboxCredential(name=body.name.strip(), kind=body.kind, target=body.target,
                            encrypted_value=encoded(services, body.value))
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "credential name already exists") from exc
    return {**metadata(row), "sandbox_ids": []}


@router.patch("/sandbox-credentials/{credential_id}")
async def change(credential_id: str, body: CredentialUpdate, session=Depends(get_session), services=Depends(get_services)):
    # First write locks grant changes/deletion on this credential across processes.
    changed = await session.execute(update(SandboxCredential).where(SandboxCredential.id == credential_id)
                                    .values(updated_at=utcnow()))
    if not changed.rowcount:
        raise HTTPException(404, "credential not found")
    row = await session.get(SandboxCredential, credential_id)
    ids = (await session.scalars(select(SandboxCredentialGrant.sandbox_id).where(
        SandboxCredentialGrant.credential_id == credential_id))).all()
    if body.value is not None:
        await idle(session, ids)
        row.encrypted_value = encoded(services, body.value)
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(422, "credential name cannot be blank")
        row.name = body.name.strip()
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "credential name already exists") from exc
    return {**metadata(row), "sandbox_ids": ids}


@router.delete("/sandbox-credentials/{credential_id}", status_code=204)
async def remove(credential_id: str, session=Depends(get_session)):
    changed = await session.execute(update(SandboxCredential).where(SandboxCredential.id == credential_id)
                                    .values(updated_at=utcnow()))
    if not changed.rowcount:
        raise HTTPException(404, "credential not found")
    if await session.scalar(select(SandboxCredentialGrant.sandbox_id).where(
            SandboxCredentialGrant.credential_id == credential_id)):
        raise HTTPException(409, "detach this credential from all sandboxes first")
    await session.execute(delete(SandboxCredential).where(SandboxCredential.id == credential_id))
    await session.commit()


@router.put("/sandboxes/{sandbox_id}/credentials")
async def attach(sandbox_id: str, body: Grants, session=Depends(get_session)):
    ids = sorted(set(body.credential_ids))
    # Same lock order as rotation: credentials, then sandbox. No secret values leave here.
    if ids:
        await session.execute(update(SandboxCredential).where(SandboxCredential.id.in_(ids)).values(updated_at=utcnow()))
    await idle(session, [sandbox_id])
    rows = (await session.scalars(select(SandboxCredential).where(SandboxCredential.id.in_(ids)))).all()
    if len(rows) != len(ids):
        raise HTTPException(422, "credential not found")
    targets = [(r.kind, r.target) for r in rows]
    if len(set(targets)) != len(targets):
        raise HTTPException(422, "multiple credentials target the same environment variable or file")
    paths = [r.target for r in rows if r.kind == "file"]
    if any(a != b and b.startswith(a + "/") for a in paths for b in paths):
        raise HTTPException(422, "credential file destinations overlap")
    await session.execute(delete(SandboxCredentialGrant).where(SandboxCredentialGrant.sandbox_id == sandbox_id))
    session.add_all(SandboxCredentialGrant(sandbox_id=sandbox_id, credential_id=ident) for ident in ids)
    await session.commit()
    return {"credential_ids": ids, "notice": "Applies to future runs. Detaching does not erase file copies or revoke tokens."}
