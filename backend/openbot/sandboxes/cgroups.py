"""Linux cgroup-v2 cleanup for exclusive worker identities.

Infrastructure must delegate the worker service's cgroup (Delegate=yes). No root
helper, sudo or systemd D-Bus access. A process group is NOT an alternative here.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from openbot.sandboxes.protocol import SandboxError


class Cgroups:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        if not str(self.root).startswith("/sys/fs/cgroup/"):
            raise SandboxError("worker requires its delegated cgroup-v2 service path")
        if str(os.getpid()) not in (self.root / "cgroup.procs").read_text().split():
            raise SandboxError("worker must reside in the configured service cgroup")
        if not (self.root / "cgroup.kill").exists():
            raise SandboxError("worker requires cgroup.kill (cgroup v2, Linux 5.14+)")
        self.runs = self.root / "openbot-runs"
        self.runs.mkdir(exist_ok=True)

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise SandboxError("invalid execution identity")
        return value

    def begin(self, run_id):
        path = self.runs / self._id(run_id)
        path.mkdir()  # a stale group must be reconciled, never silently reused
        return path

    def operation(self, run_id, operation_id):
        path = self.runs / self._id(run_id) / self._id(operation_id)
        path.mkdir()
        return path

    async def kill(self, path):
        (path / "cgroup.kill").write_text("1")
        async with asyncio.timeout(10):
            while "populated 1" in (path / "cgroup.events").read_text():
                await asyncio.sleep(0.02)
        self._remove(path)

    def _remove(self, path):
        for child in path.iterdir():
            if child.is_dir():
                self._remove(child)
        path.rmdir()

    async def end(self, run_id):
        # Agent processes have a kernel-locked read-only cgroup mount. Membership
        # cannot be changed by the operation, so this also reaches setsid/double-fork
        # descendants without killing siblings or unrelated operator processes.
        await self.kill(self.runs / self._id(run_id))
