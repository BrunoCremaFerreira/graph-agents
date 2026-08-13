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


def _log_failure(error: BaseException) -> None:
    """Append `error` to ``GRAPHAGENTS_DEBUG_LOG``, if that variable is set.

    Silence is the rule, but total silence made the commonest failure -- the
    daemon simply not running -- indistinguishable from a healthy setup with
    nothing to show. This opt-in log is the only way to tell the two apart, and
    it stays off unless the variable is set. Failing to write it is itself
    ignored: diagnostics must never become the thing that breaks the session.
    """
    path = os.environ.get("GRAPHAGENTS_DEBUG_LOG")
    if not path:
        return
    try:
        import datetime
        import traceback

        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        detail = "".join(traceback.format_exception_only(type(error), error)).strip()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} socket={_socket_path()} {detail}\n")
    except Exception:
        pass


def _trace(raw: str) -> None:
    """Append `raw` to ``GRAPHAGENTS_TRACE_LOG``, if that variable is set.

    Sibling of :func:`_log_failure`: that one records *failures*, this one
    records *arrivals*. It exists to answer a question the pipeline otherwise
    destroys -- what Claude Code actually puts in the ``PostToolUse`` JSON.
    ``normalize.py`` reads only the handful of fields it knows about, so any
    per-subagent identity would be dropped without ever being seen; with the
    variable set, the payload is preserved verbatim and can be inspected.

    Hence it runs *before* the parse and before the send: the most interesting
    payload to capture is the one that does not parse, and collection typically
    happens exactly when the daemon is down. One line per invocation, no
    timestamp prefix, so ``json.loads`` on a line hands back the original dict.
    Writing it is best-effort -- diagnostics must never become the thing that
    breaks the session.
    """
    path = os.environ.get("GRAPHAGENTS_TRACE_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(raw.rstrip("\n") + "\n")
    except Exception:
        pass


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
    _trace(raw)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return
    _send(payload, _socket_path())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Fail silently: a crashing hook must not break the agent loop.
        _log_failure(error)
    finally:
        sys.exit(0)
