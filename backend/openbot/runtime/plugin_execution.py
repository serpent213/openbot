"""Trusted plugin entrypoint: select the active run's execution target, never a caller-supplied UID."""
from __future__ import annotations

import os

from openbot.db.models import Run, Thread
from openbot.runtime.processes import (
    ProcessExecutionError,
    ProcessRequest,
    ProcessResult,
    run_process,
)
from openbot.sandboxes.protocol import SandboxRejected
from openbot.tools.builtin.workspace import resolve_in_workspace

# A useful process baseline, not the controller's provider/plugin secret environment.
HOST_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP",
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "NIX_SSL_CERT_FILE")


async def execute_process(runtime, argv: list[str], *, stdin: str = "", env: dict[str, str] | None = None,
                          cwd: str | None = None, timeout: float = 120, output_limit: int = 1_000_000) -> ProcessResult:
    """Execute argv in the active run's environment; cwd is workspace-relative.

    Env is an explicit command-only overlay. stdout/stderr are independently capped
    in bytes; inspect truncation/timeout before parsing structured output. Nonzero
    exit codes are results; transport/identity/start failures raise. No retries.
    Only agent tool runtimes are supported, not plugin background/UI callbacks.
    """
    request = ProcessRequest(argv=argv, stdin=stdin, env=env or {}, cwd=cwd,
                             timeout=timeout, output_limit=output_limit)
    ctx = runtime.context
    services = ctx.services
    async with services.session_factory() as session:
        run = await session.get(Run, ctx.run_id)
        thread = await session.get(Thread, ctx.thread_id)
        if (not run or not thread or thread.deleted_at is not None or run.status != "running"
                or run.actor_id != ctx.actor_id or run.thread_id != thread.id
                or run.sandbox_id != thread.sandbox_id or run.sandbox_id != ctx.sandbox_id
                or run.host_execution != thread.host_execution):
            raise SandboxRejected("process helper requires a matching active run context")
        sandbox_id = run.sandbox_id
        if sandbox_id and run.host_execution:
            raise SandboxRejected("invalid sandbox/Host binding")
        if not sandbox_id:
            if services.settings.sandboxes_enabled and not run.host_execution:
                raise SandboxRejected("unbound process execution refused; no Host fallback")
            if run.host_execution and not services.settings.sandbox_allow_host_execution:
                raise SandboxRejected("Host execution is disabled")
        directory = thread.working_directory
    if sandbox_id:
        from openbot.sandboxes.runtime import execute
        result = await execute(services, sandbox_id, ctx.run_id, "execute_process", request.model_dump(), directory)
        if "process_error" in result:
            raise ProcessExecutionError(result["process_error"])
        return ProcessResult(**result)
    from openbot.sandboxes.policy import directory as relative_directory
    try:
        workdir = resolve_in_workspace(ctx.workspace_root, relative_directory(cwd))
    except (ValueError, OSError) as exc:
        raise SandboxRejected(str(exc)) from exc
    baseline = {key: os.environ[key] for key in HOST_ENV if key in os.environ}
    try:
        return await run_process(request, cwd=str(workdir), env=baseline)
    except OSError as exc:
        raise ProcessExecutionError(str(exc)) from exc
