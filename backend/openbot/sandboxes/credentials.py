"""Named sandbox process credentials. Never import controller environment or return values to UI."""
import re
from pathlib import PurePosixPath

from sqlalchemy import select

from openbot.db.models import SandboxCredential, SandboxCredentialGrant
from openbot.sandboxes.protocol import SandboxRejected

# Execution/bootstrap controls are not application credentials. Values are applied
# only AFTER child isolation, never to the trusted supervisor or interpreter launch.
RESERVED = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "ENV", "TMPDIR", "TMP", "TEMP",
            "IFS", "CDPATH", "LANG", "DATABASE_URL", "SECRET_KEY", "CREDENTIALS_DIRECTORY"}
PREFIXES = ("LD_", "DYLD_", "PYTHON", "BASH", "NODE_", "NPM_", "GIT_", "NIX_", "SSL_", "LC_",
            "XDG_", "OPENBOT_", "SANDBOX", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def validate_target(kind, target):
    if kind == "env":
        if (not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", target) or target in RESERVED
                or target.startswith(PREFIXES)):
            raise ValueError("environment target is invalid or reserved for execution control")
    elif kind == "file":
        path = PurePosixPath(target)
        if (not target or len(target) > 1000 or path.is_absolute() or str(path) != target or
                any(part in ("..", ".openbot-tmp") for part in path.parts) or target == "." or
                "\\" in target or any(ord(char) < 32 for char in target)):
            raise ValueError("file target must be a normalized relative path inside the private home")
    else:
        raise ValueError("credential kind must be env or file")


async def material(services, session, sandbox_id):
    rows = (await session.scalars(select(SandboxCredential).join(SandboxCredentialGrant).where(
        SandboxCredentialGrant.sandbox_id == sandbox_id))).all()
    env, files = {}, {}
    for row in rows:
        validate_target(row.kind, row.target)
        destination = env if row.kind == "env" else files
        if row.target in destination:
            raise SandboxRejected("conflicting credential targets; inspect sandbox attachments")
        value = services.secrets.decrypt(row.encrypted_value)
        if value is None:
            raise SandboxRejected("sandbox credential cannot be decrypted")
        destination[row.target] = value
    return {"env": env, "files": files}
