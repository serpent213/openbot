"""Prevent agent processes from migrating out of their operation cgroup.

Requires unprivileged user/mount namespaces. No privilege escalation or host mounts:
all mount changes occur in child-owned namespaces. A second user namespace locks
inherited read-only mounts, so commands cannot simply remount cgroupfs writable.
"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path

from openbot.sandboxes.protocol import SandboxRejected


def _user_mount_namespace():
    uid, gid = os.getuid(), os.getgid()
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
    Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1\n")
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1\n")


def lock_cgroup_mount():
    try:
        libc = ctypes.CDLL(None, use_errno=True)

        def mount(source, target, flags):
            if libc.mount(source, target, None, ctypes.c_ulong(flags), None):
                raise OSError(ctypes.get_errno(), "cannot isolate cgroup mount")

        # Reject unusual aliases rather than leaving a writable cgroup mount behind.
        mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()
                  if " - cgroup2 " in line]
        if mounts != ["/sys/fs/cgroup"]:
            raise SandboxRejected("worker requires one unified cgroup-v2 mount at /sys/fs/cgroup")
        _user_mount_namespace()
        mount(None, b"/", (1 << 14) | (1 << 18))  # MS_REC | MS_PRIVATE
        mount(b"/sys/fs/cgroup", b"/sys/fs/cgroup", 4096)  # MS_BIND
        mount(None, b"/sys/fs/cgroup", 4096 | 32 | 1 | 2 | 4 | 8)  # bind,remount,ro,nosuid,nodev,noexec
        _user_mount_namespace()  # inherited read-only flag is now kernel-locked
        # Verify the boundary before any agent code. If this succeeds, only this
        # disposable child's private mount changed; refuse rather than assuming a lock.
        unlocked = libc.mount(None, b"/sys/fs/cgroup", None, ctypes.c_ulong(4096 | 32 | 2 | 4 | 8), None)
        if unlocked == 0 or ctypes.get_errno() not in (errno.EPERM, errno.EACCES):
            raise SandboxRejected("cgroup read-only mount is not kernel-locked; execution refused")
    except (OSError, AttributeError) as exc:
        raise SandboxRejected("worker requires delegated cgroup v2 and enabled user/mount namespaces; "
                              "no process-group fallback is permitted") from exc


def protect_supervisor():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0):  # PR_SET_DUMPABLE=0: block /proc/<supervisor>/root/fd bypasses
        raise SandboxRejected("cannot protect worker supervisor proc access")
