"""Worker-only operations: reuse Openbot tools, discovery and installed CLI adapters.

Never construct Settings here (it would load .env). Configuration below is supplied
by the operator's worker launch, not by the model or the server environment.
"""
from __future__ import annotations

import importlib
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from openbot.sandboxes.policy import WORKER_TOOLS, directory
from openbot.sandboxes.protocol import SandboxRejected
from openbot.tools.builtin.workspace import browse_workspace_directory, resolve_in_workspace
from openbot.tools.context import RunContext


def cwd_for(root, payload):
    try:
        cwd = resolve_in_workspace(root, directory(payload.get("directory")))
        if not cwd.is_dir():
            raise ValueError("working_directory does not exist in sandbox; create it or choose an existing directory")
        return cwd
    except (ValueError, OSError) as exc:
        raise SandboxRejected(str(exc)) from exc


def cli_adapter(config, provider):
    from openbot.runtime.cli_agents import backend_registry
    try:
        plugins = [importlib.import_module(module).plugin for module in config.get("plugins", [])]
        adapter = backend_registry(plugins).get(provider)
    except (ImportError, AttributeError, ValueError) as exc:
        raise SandboxRejected("worker coding adapters are missing or incompatible; check installed --plugin modules") from exc
    if adapter is None:
        raise SandboxRejected(f"coding backend {provider!r} is not installed/enabled in this worker")
    return adapter


async def perform(root: Path, payload: dict, env: dict, emit):
    import httpx
    from pydantic import ValidationError

    name, args = payload["operation"], payload.get("args", {})
    if name == "provision":
        from openbot.sandboxes.home import provision
        return provision(Path.home(), payload.get("credentials", {}).get("files", {}))
    cwd = cwd_for(root, payload)
    config = payload.get("worker_config", {})
    context = RunContext("", "", "", "", payload["run_id"], cwd, None,
                         tool_output_cap=payload.get("tool_output_cap", 8000),
                         shell_output_cap=payload.get("shell_output_cap", 4000),
                         shell_login=False, skill_names=payload.get("skill_names", []))
    if name == "probe":
        return {"cwd": str(cwd)}
    if name == "prepare":
        from openbot.runtime.prompt import load_repository_instructions
        from openbot.skills import enabled_skills
        profile = SimpleNamespace(skill_names=args.get("skill_names", []))
        skills = enabled_skills(profile, cwd, SimpleNamespace(state_dir=None))
        return {"cwd": str(cwd), "skills": [asdict(skill) for skill in skills],
                "instructions": load_repository_instructions(str(cwd), enabled=args.get("instructions", False))}
    if name == "skills":
        from openbot.skills import discover_skills
        return {"skills": [asdict(skill) for skill in discover_skills(cwd)]}
    if name == "browse":
        try:
            here, parent, entries = browse_workspace_directory(cwd, directory(args.get("path")))
        except (ValueError, OSError) as exc:
            raise SandboxRejected(str(exc)) from exc
        return {"path": here, "parent": parent,
                "entries": [{"name": n, "path": p} for n, p in entries]}
    if name in ("cli_probe", "cli_turn"):
        from openbot.runtime.cli_agents.base import (
            CliAgentError,
            check_version,
            find_executable,
            login_status,
            run_turn,
        )
        adapter = cli_adapter(config, args["provider"])
        settings = SimpleNamespace(cli_paths=config.get("cli_paths", {}))
        executable = find_executable(adapter, settings)
        if executable is None:
            raise SandboxRejected(adapter.not_found)
        if name == "cli_probe":
            try:
                version = await check_version(adapter, executable, env)
                logged_in = await login_status(adapter, executable, env)
                return {"cwd": str(cwd), "path": executable, "version": version,
                        "logged_in": logged_in, "problem": None}
            except (CliAgentError, OSError) as exc:
                raise SandboxRejected(str(exc)) from exc
        if args.get("expected_cwd", str(cwd)) != str(cwd):
            raise SandboxRejected("working directory changed after CLI preflight; session reuse refused")
        await emit("operation_started", None)
        try:
            result = await run_turn(adapter, executable, cwd=str(cwd), env=env,
                system_prompt=args["system_prompt"], full_prompt=args["full_prompt"],
                resume=args.get("resume"), model=args.get("model", ""),
                model_settings=args.get("model_settings", {}), timeout=args["timeout"], on_event=emit)
            return asdict(result)
        except (CliAgentError, OSError) as exc:
            # A reported CLI failure is a tool result, not a lost transport reply.
            message = str(exc)
            if await login_status(adapter, executable, env) is False:
                message = f"error: {adapter.label} is not logged in. {adapter.login_hint}"
            return {"cli_error": message, "usage": getattr(exc, "usage", None), "totals": getattr(exc, "totals", None)}
    tool = WORKER_TOOLS.get(name)
    if tool is None:
        raise SandboxRejected(f"unsupported worker operation: {name}")
    try:
        args = tool.tool_call_schema.model_validate(args).model_dump()
    except ValidationError as exc:
        raise SandboxRejected(str(exc)) from exc
    await emit("operation_started", None)
    try:
        # Runtime is injected locally, never accepted as RPC/model arguments.
        return await tool.coroutine(**args, **({"runtime": SimpleNamespace(context=context)}
                                             if "runtime" in tool.args_schema.model_fields else {}))
    except (ValueError, OSError, httpx.HTTPError) as exc:
        # Match ordinary tool failures: actionable errors leave the binding usable.
        return f"error: {type(exc).__name__}: {exc}"
