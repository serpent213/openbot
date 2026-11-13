"""Trusted home operations. No login-path knowledge; no traversal of links or mounts."""
import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from openbot.sandboxes.protocol import SandboxRejected


def mountpoints():
    # Same-device bind mounts need mountinfo, not st_dev checks alone.
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)
    return {Path(unescape(line.split()[4])) for line in Path("/proc/self/mountinfo").read_text().splitlines()}


def _directory(name, parent):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _write(home, target, value, mounts):
    parts = Path(target).parts
    here = home
    fd = _directory(home, None)
    try:
        for part in parts[:-1]:
            here = here / part
            if here in mounts:
                raise SandboxRejected("credential target crosses an attached mount")
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = _directory(part, fd)
            os.close(fd)
            fd = child
        if here / parts[-1] in mounts:
            raise SandboxRejected("credential target is an attached mount")
        try:
            old = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(old.st_mode) or old.st_nlink != 1:
                raise SandboxRejected("credential target must be a regular, non-hardlinked file")
        temporary = f".openbot-credential-{uuid.uuid4()}"
        output = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(output, "wb") as stream:
                stream.write(value.encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


def provision(home, files):
    from openbot.sandboxes.credentials import validate_target
    mounts = mountpoints()
    for target, value in files.items():
        try:
            validate_target("file", target)
            _write(home, target, value, mounts)
        except (ValueError, OSError) as exc:
            raise SandboxRejected("cannot provision credential file; check private-home path and permissions") from exc
    return {"files_provisioned": len(files)}


def clear(home, root, keep_dotfiles, *, mounts=None):
    """Called only with exclusive maintenance fencing and no running children.

    Preserve every mount and its ancestors. dir_fd + O_NOFOLLOW prevents symlink
    traversal; different-device directories also fail closed. Do not remove home itself.
    """
    if not shutil.rmtree.avoids_symlink_attacks:
        raise SandboxRejected("safe fd-relative home cleanup is unavailable")
    mounts = mountpoints() if mounts is None else mounts
    if home.is_symlink() or not home.is_dir() or not root.is_relative_to(home) or root == home:
        raise SandboxRejected("invalid private home/workspace layout")
    protected = {path for path in mounts if path != home and path.is_relative_to(home)}
    skipped = []
    skipped_count = 0
    device = home.stat().st_dev

    def keep(child):
        nonlocal skipped_count
        skipped_count += 1
        if len(skipped) < 100:
            skipped.append(str(child.relative_to(home)))

    def clean(fd, path, top=False):
        for name in os.listdir(fd):
            child = path / name
            if (top and keep_dotfiles and name.startswith(".")) or child in protected:
                keep(child)
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev != device:
                    keep(child)
                    continue
                sub = _directory(name, fd)
                try:
                    clean(sub, child)
                    # If a mount or protected ancestor remains, retain its directory.
                    empty = not os.listdir(sub)
                finally:
                    os.close(sub)
                if empty and not any(p.is_relative_to(child) for p in protected):
                    os.rmdir(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)  # unlink links themselves, never their targets

    fd = _directory(home, None)
    try:
        clean(fd, home, top=True)
    finally:
        os.close(fd)
    # Only trusted paths defined by provisioning; never follow a surviving link.
    for target in (root, home / ".openbot-tmp"):
        relative = target.relative_to(home)
        parent = _directory(home, None)
        try:
            for name in relative.parts:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = _directory(name, parent)
                os.close(parent)
                parent = child
        finally:
            os.close(parent)
    return {"kept": skipped, "kept_count": skipped_count, "keep_dotfiles": keep_dotfiles}
