#!/usr/bin/env python3
"""Claude Code hook entrypoint: PostToolUse JSON on stdin -> event to the daemon.

This script is wired into a target project's ``.claude/settings.json`` (see
``config/settings.json``). It runs on *every* covered tool call, so it obeys two
hard rules:

  1. **Dependency-free.** Python 3 standard library only; fast startup.
  2. **Never fail loudly.** Whatever happens -- garbage stdin, no daemon, a
     broken socket -- it writes nothing to stdout and always exits 0, so it can
     never disrupt the user's Claude Code session.

The path/op classification lives in :mod:`graphagents.normalize`; the daemon
owns the "already seen" set that decides add-vs-modify, so this hook forwards the
raw payload untouched and lets the daemon normalize and de-duplicate.

Transport: one JSON line per event over a Unix domain socket whose path comes
from ``GRAPHAGENTS_SOCKET`` (default ``/tmp/graph-agents.sock``).
"""

from __future__ import annotations

import json
import os
import socket
import sys

DEFAULT_SOCKET_PATH = "/tmp/graph-agents.sock"
_CONNECT_TIMEOUT_SECONDS = 0.5


def _socket_path() -> str:
    return os.environ.get("GRAPHAGENTS_SOCKET", DEFAULT_SOCKET_PATH)


def _read_stdin() -> str:
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


def _send(payload: dict, socket_path: str) -> None:
    """Send one newline-terminated JSON line to the daemon, best-effort."""
    line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_CONNECT_TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(line)


def main() -> None:
    raw = _read_stdin()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return
    _send(payload, _socket_path())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail silently: a crashing hook must not break the agent loop.
        pass
    finally:
        sys.exit(0)
