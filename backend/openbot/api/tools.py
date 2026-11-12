from fastapi import APIRouter, Depends

from openbot.api.deps import get_services
from openbot.api.sandboxes import validate_inspection_target
from openbot.services import Services
from openbot.skills import discover_skills

router = APIRouter(prefix="/tools", tags=["tools"])


def _json_schema(schema) -> dict:
    """Built-in tools carry a pydantic model; MCP tools carry the server's JSON schema as a dict."""
    if schema is None:
        return {}
    if isinstance(schema, dict):
        return schema
    return schema.model_json_schema()


@router.get("")
async def list_tools(services: Services = Depends(get_services)):
    reg = services.registry
    return {
        "tools": [
            {"name": s.name, "description": s.description, "source": s.source,
             "args_schema": _json_schema(s.tool.tool_call_schema)}
            for s in reg.specs()
        ],
        "errors": reg.load_errors,
    }


@router.get("/skills")
async def list_skills(sandbox_id: str | None = None, host_execution: bool = False,
                      services: Services = Depends(get_services)):
    """Discovered skills for the bot editor's per-bot skill picker."""
    settings = services.settings
    validate_inspection_target(settings, sandbox_id, host_execution)
    if sandbox_id:
        from openbot.api.sandboxes import worker_inspect
        return await worker_inspect(services, sandbox_id, "skills", {})
    roots = [settings.workspace_root]
    state_dir = settings.state_dir
    if state_dir is not None and state_dir.resolve() != settings.workspace_root.resolve():
        roots.append(state_dir)
    return {
        "skills": [
            {"id": s.id, "name": s.name, "description": s.description, "path": s.path}
            for s in discover_skills(*roots)
        ]
    }
