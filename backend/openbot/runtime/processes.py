"""Bounded process execution shared by Host and isolated worker operations.

No shell interpolation and no implicit controller environment inheritance. This
module does not select an identity; plugin_api.execute_process does that first.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from openbot.runtime.cli_agents.base import ProcessTree


class ProcessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str] = Field(min_length=1, max_length=1024)
    stdin: str = Field(default="", max_length=1_000_000)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    timeout: FiniteFloat = Field(default=120, gt=0, le=86400)
    output_limit: int = Field(default=1_000_000, ge=1, le=2_000_000)

    @model_validator(mode="after")
    def valid(self):
        if not self.argv[0] or any("\0" in arg for arg in self.argv):
            raise ValueError("invalid process arguments")
        if sum(len(arg.encode()) for arg in self.argv) > 128_000:
            raise ValueError("process arguments too large")
        if any(not key or "=" in key or "\0" in key or "\0" in value for key, value in self.env.items()):
            raise ValueError("invalid process environment")
        if sum(len(key.encode()) + len(value.encode()) for key, value in self.env.items()) > 1_000_000:
            raise ValueError("process environment too large")
        return self


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False


class ProcessExecutionError(RuntimeError):
    """The process could not be started; not an invitation to try on the Host."""


async def run_process(request: ProcessRequest, *, cwd: str, env: dict[str, str]) -> ProcessResult:
    # Explicit overrides affect only this command, never the worker supervisor.
    tree = await ProcessTree.spawn(request.argv, cwd=cwd, env={**env, **request.env})
    buffers = [bytearray(), bytearray()]
    truncated = [False, False]

    async def read(stream, index):
        while chunk := await stream.read(65536):
            room = max(0, request.output_limit - len(buffers[index]))
            buffers[index].extend(chunk[:room])
            truncated[index] |= len(chunk) > room

    async def write():
        try:
            tree.proc.stdin.write(request.stdin.encode())
            await tree.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            tree.proc.stdin.close()

    tasks = [asyncio.create_task(read(tree.proc.stdout, 0)), asyncio.create_task(read(tree.proc.stderr, 1)),
             asyncio.create_task(write()), asyncio.create_task(tree.proc.wait())]
    completion = asyncio.gather(*tasks)
    timed_out = False
    try:
        # Keep draining while terminating, including on Windows where wait()
        # can depend on pipe closure. Cancellation must not strand a full pipe.
        await asyncio.wait_for(asyncio.shield(completion), request.timeout)
    except TimeoutError:
        timed_out = True
    finally:
        # Also clean up background children after an otherwise successful exit.
        try:
            await tree.stop()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(completion, return_exceptions=True)
            tree.close()
    return ProcessResult(tree.proc.returncode, *(bytes(buf).decode(errors="replace") for buf in buffers),
                         *truncated, timed_out)
