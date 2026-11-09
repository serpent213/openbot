"""The deliberately small first-slice allowlist; no registry/plugin name substitution."""
from pathlib import PurePosixPath

from openbot.tools.builtin.files import edit_file, list_files, patch_file, read_file, write_file
from openbot.tools.builtin.http import fetch_url, http_request
from openbot.tools.builtin.search import find_files, search_code
from openbot.tools.builtin.shell import run_shell
from openbot.tools.builtin.skills import load_skill

WORKER_TOOLS = {t.name: t for t in (run_shell, read_file, write_file, edit_file, list_files,
                                   patch_file, search_code, find_files, load_skill, http_request, fetch_url)}


def directory(value: str | None) -> str | None:
    if not value or value == ".":
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("~") or "\\" in value:
        raise ValueError("sandbox working_directory must be relative, without '..' or '~'")
    if any(ord(c) < 32 for c in value):
        raise ValueError("invalid sandbox working_directory")
    return str(path)
