"""Edit patches from coding CLIs for the run log (cli_agents/patch.py, ClaudeStream).

fixtures/claude_edit_real.jsonl is an anonymized capture of Claude Code 2.1.323: one Edit
tool_use and the user event with its tool_use_result (structuredPatch, no gitDiff outside git).
"""
import json
import re
from pathlib import Path

from openbot.runtime.cli_agents.claude_code import ClaudeStream
from openbot.runtime.cli_agents.patch import claude_patch, unified_added, unified_from_hunks

FIXTURE = Path(__file__).parent / "fixtures" / "claude_edit_real.jsonl"
HUNKS = [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 2, "lines": [" a", "-b", "+B"]},
         {"oldStart": 10, "oldLines": 1, "newStart": 10, "newLines": 2, "lines": [" x", "+y"]}]


def _stream():
    return ClaudeStream(re.compile(r"^[0-9a-f-]{36}$"))


def test_unified_from_hunks_exact_text():
    assert unified_from_hunks("src/a.py", HUNKS) == (
        "--- src/a.py\n+++ src/a.py\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -10,1 +10,2 @@\n x\n+y\n")


def test_unified_from_hunks_rejects_other_shapes():
    assert unified_from_hunks("a", []) is None
    assert unified_from_hunks("a", "nope") is None
    assert unified_from_hunks("a", [{**HUNKS[0], "oldStart": "1"}]) is None
    assert unified_from_hunks("a", [{**HUNKS[0], "lines": [" a", 3]}]) is None
    assert unified_from_hunks("a", [{**HUNKS[0], "oldLines": True}]) is None


def test_unified_added():
    assert unified_added("new.txt", "one\ntwo\n") == "--- /dev/null\n+++ new.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    assert unified_added("new.txt", "") is None


def test_claude_patch_prefers_git_diff_and_handles_new_files():
    out = {"filePath": "a.py", "structuredPatch": HUNKS, "gitDiff": {"patch": "diff --git a/a.py b/a.py\n..."}}
    assert claude_patch("Edit", out).startswith("diff --git")
    assert claude_patch("Edit", {**out, "gitDiff": {"patch": ""}}).startswith("--- a.py")
    created = {"type": "create", "filePath": "n.py", "content": "x = 1\n", "structuredPatch": []}
    assert claude_patch("Write", created) == "--- /dev/null\n+++ n.py\n@@ -0,0 +1,1 @@\n+x = 1\n"
    assert claude_patch("Read", out) is None
    assert claude_patch("Edit", "text") is None
    assert claude_patch("Edit", {"structuredPatch": HUNKS}) is None


def test_real_claude_edit_stream_attaches_the_patch():
    parser = _stream()
    out = [action for line in FIXTURE.read_text().splitlines() for action in parser.feed(json.loads(line))]
    [(_, call)] = [a for a in out if a[0] == "tool_call"]
    [(_, result)] = [a for a in out if a[0] == "tool_result"]
    assert call["name"] == "Edit" and result["tool_call_id"] == call["id"]
    assert result["patch"] == (
        '--- /work/a.py\n+++ /work/a.py\n@@ -1,5 +1,5 @@\n def greet(name):\n'
        '-    return "hi " + name\n+    return "hello " + name\n \n \n print(greet("x"))\n')


def test_tool_use_result_is_ignored_when_an_event_has_several_results():
    parser = _stream()
    parser.feed({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "Edit", "input": {}},
        {"type": "tool_use", "id": "b", "name": "Edit", "input": {}}]}})
    out = parser.feed({"type": "user", "tool_use_result": {"filePath": "x", "structuredPatch": HUNKS},
                       "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"},
                                               {"type": "tool_result", "tool_use_id": "b", "content": "ok"}]}})
    assert [p.get("patch") for _, p in out] == [None, None]
