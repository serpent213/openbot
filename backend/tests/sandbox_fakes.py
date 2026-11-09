"""Same-UID protocol tests only: NOT a replacement for real cgroup integration tests."""
import os
import shutil
import signal
from contextlib import asynccontextmanager, suppress


class FakeCgroups:
    def __init__(self, root):
        self.root = root
        root.mkdir()

    def begin(self, run_id):
        (self.root / run_id).mkdir()

    def operation(self, run_id, ident):
        path = self.root / run_id / ident
        path.mkdir()
        (path / "cgroup.procs").touch()
        return path

    async def kill(self, path):
        # Reap test subprocesses. This emulates neither kernel cgroup membership
        # nor adversarial process migration; those require the distinct-UID pilot.
        if (path / "cgroup.procs").exists():
            text = (path / "cgroup.procs").read_text()
            if text:
                pid = int(text)
                descendants = []

                def collect(pid):
                    from pathlib import Path
                    with suppress(FileNotFoundError, ProcessLookupError):
                        for child in (Path(f"/proc/{pid}/task/{pid}/children").read_text().split()):
                            collect(int(child))
                            descendants.append(int(child))
                collect(pid)
                for child in [*descendants, pid]:
                    with suppress(ProcessLookupError):
                        os.kill(child, signal.SIGKILL)
        else:
            for child in path.iterdir():
                if child.is_dir():
                    await self.kill(child)
        shutil.rmtree(path)

    async def end(self, run_id):
        await self.kill(self.root / run_id)


@asynccontextmanager
async def fake_lease(*args, **kwargs):
    yield {}
