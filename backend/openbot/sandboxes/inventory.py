"""Register preprovisioned resources without changing existing identities or grants."""
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from openbot.db.models import Sandbox

log = logging.getLogger(__name__)


def configured(settings, box):
    slot = settings.sandbox_resources.get(box.resource)
    return bool(settings.sandboxes_enabled and slot and box.resource_spec == slot.model_dump())


async def register(services):
    if not services.settings.sandboxes_enabled:
        return
    for name, slot in services.settings.sandbox_resources.items():
        async with services.session_factory() as session:
            exists = await session.scalar(select(Sandbox.id).where(
                (Sandbox.resource == name) | (Sandbox.resource_key == f"linux:uid:{slot.uid}")))
            if exists:
                continue  # never rename, rebind, reactivate, or recycle
            session.add(Sandbox(name=name, resource=name, resource_key=f"linux:uid:{slot.uid}",
                                resource_spec=slot.model_dump()))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                log.warning("sandbox resource %s could not be registered: conflicting name or resource", name)
