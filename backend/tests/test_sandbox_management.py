"""Inventory, metadata and credential grant tests; no live credentials or services."""
import asyncio

import pytest

from openbot.config import SandboxResource
from openbot.db.models import Sandbox, SandboxCredential
from openbot.sandboxes.credentials import material, validate_target
from openbot.sandboxes.inventory import register


async def setup(services, client):
    services.settings.sandboxes_enabled = True
    services.settings.openbot_api_key = "test-only"
    client.headers["X-API-Key"] = "test-only"
    services.settings.sandbox_resources = {
        f"openbot-sbx{i}": SandboxResource(socket_path=f"/tmp/not-live-{i}.sock", uid=62000+i)
        for i in (1, 2)
    }
    await register(services)
    return (await client.get("/api/v1/sandboxes")).json()["sandboxes"]


async def test_inventory_registers_preserves_names_and_never_recycles(client, services):
    rows = await setup(services, client)
    assert [r["name"] for r in rows] == ["openbot-sbx1", "openbot-sbx2"]
    box = rows[0]
    response = await client.patch(f"/api/v1/sandboxes/{box['id']}", json={"name": "Development"})
    assert response.status_code == 200
    await asyncio.gather(register(services), register(services))
    inventory = (await client.get("/api/v1/sandboxes")).json()
    assert len(inventory["sandboxes"]) == 2
    assert inventory["sandboxes"][0]["id"] == box["id"]
    assert inventory["sandboxes"][0]["name"] == "Development"
    assert (await client.patch(f"/api/v1/sandboxes/{box['id']}", json={"name": "openbot-sbx2"})).status_code == 409
    assert (await client.patch(f"/api/v1/sandboxes/{box['id']}", json={"name": " "})).status_code == 422
    del services.settings.sandbox_resources["openbot-sbx1"]
    await register(services)
    missing = (await client.get("/api/v1/sandboxes")).json()["sandboxes"][0]
    assert missing["id"] == box["id"] and missing["configured"] is False
    async with services.session_factory() as session:
        row = await session.get(Sandbox, box["id"])
        row.state = "quarantined"
        await session.commit()
    services.settings.sandbox_resources["openbot-sbx1"] = SandboxResource(socket_path="/changed", uid=62001)
    await register(services)
    assert len((await client.get("/api/v1/sandboxes")).json()["sandboxes"]) == 2
    async with services.session_factory() as session:
        assert (await session.get(Sandbox, box["id"])).state == "quarantined"


async def test_credential_values_are_write_only_encrypted_and_explicitly_granted(client, services):
    boxes = await setup(services, client)
    created = await client.post("/api/v1/sandbox-credentials", json={
        "name": "Exa", "kind": "env", "target": "EXA_API_KEY", "value": "dummy-exa-not-real"})
    assert created.status_code == 201, created.text
    credential = created.json()
    assert "dummy-exa" not in created.text and "encrypted" not in created.text
    assert credential["sandbox_ids"] == []
    async with services.session_factory() as session:
        row = await session.get(SandboxCredential, credential["id"])
        assert "dummy-exa" not in row.encrypted_value
        assert (await material(services, session, boxes[0]["id"])) == {"env": {}, "files": {}}
    attached = await client.put(f"/api/v1/sandboxes/{boxes[0]['id']}/credentials", json={"credential_ids": [credential["id"]]})
    assert attached.status_code == 200, attached.text
    async with services.session_factory() as session:
        assert (await material(services, session, boxes[0]["id"]))["env"] == {"EXA_API_KEY": "dummy-exa-not-real"}
        assert (await material(services, session, boxes[1]["id"]))["env"] == {}
        assert (await session.get(Sandbox, boxes[0]["id"])).generation == 2
    listing = await client.get("/api/v1/sandbox-credentials")
    assert "dummy-exa" not in listing.text
    assert listing.json()[0]["sandbox_ids"] == [boxes[0]["id"]]
    assert (await client.delete(f"/api/v1/sandbox-credentials/{credential['id']}")).status_code == 409
    await client.put(f"/api/v1/sandboxes/{boxes[0]['id']}/credentials", json={"credential_ids": []})
    assert (await client.delete(f"/api/v1/sandbox-credentials/{credential['id']}")).status_code == 204


async def test_busy_rotation_and_target_conflicts_are_atomic(client, services):
    boxes = await setup(services, client)
    ids = []
    for name in ("first", "second"):
        result = await client.post("/api/v1/sandbox-credentials", json={"name": name, "kind": "env", "target": "APP_TOKEN", "value": "dummy"})
        ids.append(result.json()["id"])
    url = f"/api/v1/sandboxes/{boxes[0]['id']}/credentials"
    assert (await client.put(url, json={"credential_ids": ids})).status_code == 422
    assert (await client.put(url, json={"credential_ids": [ids[0]]})).status_code == 200
    async with services.session_factory() as session:
        box = await session.get(Sandbox, boxes[0]["id"])
        box.active_run_id = "busy"
        generation = box.generation
        await session.commit()
    assert (await client.put(url, json={"credential_ids": []})).status_code == 409
    assert (await client.patch(f"/api/v1/sandbox-credentials/{ids[0]}", json={"value": "replacement"})).status_code == 409
    async with services.session_factory() as session:
        assert (await material(services, session, boxes[0]["id"]))["env"]["APP_TOKEN"] == "dummy"
        box = await session.get(Sandbox, boxes[0]["id"])
        assert box.generation == generation
        box.active_run_id = None
        await session.commit()
    response = await client.patch(f"/api/v1/sandbox-credentials/{ids[0]}", json={"value": "replacement"})
    assert response.status_code == 200 and "replacement" not in response.text


@pytest.mark.parametrize("kind,target", [("env", "PATH"), ("env", "LD_PRELOAD"), ("env", "PYTHONPATH"),
    ("env", "OPENBOT_API_KEY"), ("env", "BASH_ENV"), ("env", "NODE_OPTIONS"), ("env", "HTTP_PROXY"),
    ("file", "/etc/key"), ("file", "../key"), ("file", ".openbot-tmp/key"), ("file", "a/../key")])
def test_execution_controls_and_unsafe_paths_are_not_credentials(kind, target):
    with pytest.raises(ValueError):
        validate_target(kind, target)


async def test_startup_quarantines_interrupted_maintenance_without_replaying(client, services):
    from openbot.sandboxes.runtime import recover
    boxes = await setup(services, client)
    async with services.session_factory() as session:
        box = await session.get(Sandbox, boxes[0]["id"])
        box.state = "clearing"
        await session.commit()
    await recover(services)
    await register(services)
    async with services.session_factory() as session:
        assert (await session.get(Sandbox, boxes[0]["id"])).state == "quarantined"


async def test_new_credential_routes_remain_operator_authenticated(client, services):
    await setup(services, client)
    client.headers.pop("X-API-Key")
    assert (await client.get("/api/v1/sandbox-credentials")).status_code == 401
    assert (await client.post("/api/v1/sandbox-credentials", json={"name": "bad", "kind": "env", "target": "TOKEN", "value": "dummy"})).status_code == 401
