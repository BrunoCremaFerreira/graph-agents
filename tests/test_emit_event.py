"""Contract tests (RED) for the hook entrypoint hooks/emit_event.py.

The hook reads a PostToolUse JSON payload on stdin, normalizes it, and forwards
the resulting event to the daemon. The single non-negotiable rule tested here:
the hook MUST ALWAYS exit 0 -- even on garbage stdin, empty stdin, or with the
daemon unavailable -- because a crashing hook disrupts the user's Claude Code
session (fail silently).

These run the script as a real subprocess. They are expected to FAIL now
because hooks/emit_event.py does not exist yet (RED phase).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_SCRIPT = os.path.join(REPO_ROOT, "hooks", "emit_event.py")


def _run_hook(stdin_bytes: bytes) -> subprocess.CompletedProcess:
    """Run the hook script feeding stdin, isolated from any real daemon."""
    env = dict(os.environ)
    # Point the hook at a socket path that cannot exist, to assert it still
    # exits 0 when the daemon is unavailable.
    env["GRAPHAGENTS_SOCKET"] = "/tmp/graphagents-nonexistent-daemon.sock"
    return subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        input=stdin_bytes,
        capture_output=True,
        env=env,
        timeout=10,
    )


def test_hook_script_exists():
    assert os.path.isfile(HOOK_SCRIPT), f"expected hook at {HOOK_SCRIPT}"


def test_exits_zero_on_garbage_stdin():
    result = _run_hook(b"this is not json at all \x00\xff")

    assert result.returncode == 0


def test_exits_zero_on_empty_stdin():
    result = _run_hook(b"")

    assert result.returncode == 0


def test_exits_zero_on_invalid_json_structure():
    result = _run_hook(b'{"not": "a valid hook"}')

    assert result.returncode == 0


def test_exits_zero_when_daemon_unavailable_for_valid_event():
    payload = {
        "session_id": "sess-1",
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/whatever/a.py", "content": "x"},
    }

    result = _run_hook(json.dumps(payload).encode("utf-8"))

    assert result.returncode == 0


def test_hook_stays_silent_on_stdout_for_bad_input():
    # A hook must not pollute the agent's tool output on failure.
    result = _run_hook(b"garbage")

    assert result.stdout == b""
