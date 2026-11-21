"""Unified-diff text from the structured patches coding CLIs report, for the run log.

`tool_result` payloads may carry `patch`: unified diff text, whatever produced it, so the
frontend has one input format. run.py drops a patch longer than PATCH_CAP (never cuts one).
"""

from __future__ import annotations

from typing import Any

# A broken hunk header is worse than no patch, so longer ones are dropped, not cut.
PATCH_CAP = 64_000


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def unified_from_hunks(path: str, hunks: Any) -> str | None:
    """`--- path` / `+++ path` and one `@@` block per hunk of Claude's structuredPatch
    (`{oldStart, oldLines, newStart, newLines, lines}`). None if the shape is not that."""
    if not isinstance(hunks, list) or not hunks:
        return None
    out = [f"--- {path}", f"+++ {path}"]
    for hunk in hunks:
        if not isinstance(hunk, dict):
            return None
        nums = [_count(hunk.get(k)) for k in ("oldStart", "oldLines", "newStart", "newLines")]
        lines = hunk.get("lines")
        if None in nums or not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
            return None
        out.append(f"@@ -{nums[0]},{nums[1]} +{nums[2]},{nums[3]} @@")
        out.extend(lines)
    return "\n".join(out) + "\n"


def unified_added(path: str, content: str) -> str | None:
    """A patch that creates `path` with `content`, for a new file written in one go."""
    if not content:
        return None
    lines = content.split("\n")
    if lines[-1] == "":
        lines.pop()
    body = "\n".join("+" + line for line in lines)
    return f"--- /dev/null\n+++ {path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"


def claude_patch(tool_name: str, output: Any) -> str | None:
    """The patch for one Claude Edit/MultiEdit/Write result (its `tool_use_result`)."""
    if tool_name not in ("Edit", "MultiEdit", "Write") or not isinstance(output, dict):
        return None
    git = output.get("gitDiff")
    if isinstance(git, dict) and isinstance(git.get("patch"), str) and git["patch"].strip():
        return git["patch"]
    path = output.get("filePath")
    if not isinstance(path, str) or not path:
        return None
    patch = unified_from_hunks(path, output.get("structuredPatch"))
    if patch is None and tool_name == "Write" and output.get("type") == "create" \
            and isinstance(output.get("content"), str):
        patch = unified_added(path, output["content"])
    return patch
