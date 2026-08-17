"""The hook entrypoint, inside the package that actually gets installed.

This is what a `PostToolUse` hook runs: the raw JSON payload arrives on stdin
and is forwarded, untouched, to the daemon over a Unix socket. The
classification (`A`/`M`/`D`/`R`, the path, the actor) belongs to
:mod:`rhizome_graph.normalize` and the daemon, which owns the set of seen paths
that decides add-versus-modify; nothing here parses anything but the envelope.

Two rules, and they are not style preferences.

  1. **Standard library only, and fast.** This runs on *every* covered tool call
     and blocks the agent's loop. `rhizome_graph/__init__.py` is executed ahead
     of it now that the hook lives in a package, which is affordable only while
     that file imports nothing -- `tests/test_hook_dependencies.py` is the
     invoice.
  2. **Never fail loudly.** Garbage on stdin, no daemon, a broken socket:
     whatever happens, nothing reaches stdout and the exit status is 0. A
     dropped event is invisible; a traceback in the user's terminal is not.

It lives here rather than in `hooks/` because `hooks/` is not an installed
package: a hook block naming a script in somebody's checkout stops working the
day that checkout is renamed, moved or deleted, which is the failure `rhi
--doctor` was written to find. `hooks/emit_event.py` stays as the spelling every
settings file installed so far already carries, and defers to this module.
"""

from __future__ import annotations

import json
import os
import socket
import sys

DEFAULT_SOCKET_PATH = "/tmp/rhizome-graph.sock"

#: How long the daemon is given to accept. Short by design: this is on the agent
#: loop, and a daemon that is not answering is not worth waiting for.
CONNECT_TIMEOUT_SECONDS = 0.5


def main() -> int:
    """Forward one payload, and say nothing whatever happens.

    Always 0: the console script shim passes this to `sys.exit`, and a hook that
    exits non-zero is reported by Claude Code as a blocking error on the tool
    call it was watching.
    """
    try:
        _forward(_read_stdin())
    except Exception as error:  # noqa: BLE001 - silence is the contract
        _log_failure(error)
    return 0


def _forward(raw: str) -> None:
    """Trace, parse and send -- in that order, deliberately.

    The trace comes first because the most interesting payload to capture is the
    one that does not parse, and collection typically happens exactly when the
    daemon is down.
    """
    _trace(raw)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return
    _send(payload, _socket_path())


def _socket_path() -> str:
    return os.environ.get("RHIZOME_SOCKET", DEFAULT_SOCKET_PATH)


def _read_stdin() -> str:
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


def _send(payload: dict, path: str) -> None:
    """Send one newline-terminated JSON line to the daemon, best-effort."""
    line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(CONNECT_TIMEOUT_SECONDS)
        sock.connect(path)
        sock.sendall(line)


def _log_failure(error: BaseException) -> None:
    """Append `error` to ``RHIZOME_DEBUG_LOG``, if that variable is set.

    Silence is the rule, but total silence made the commonest failure -- the
    daemon simply not running -- indistinguishable from a healthy setup with
    nothing to show. This opt-in log is the only way to tell the two apart, and
    it stays off unless the variable is set. Failing to write it is itself
    ignored: diagnostics must never become the thing that breaks the session.
    """
    path = os.environ.get("RHIZOME_DEBUG_LOG")
    if not path:
        return
    try:
        import datetime
        import traceback

        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        detail = "".join(traceback.format_exception_only(type(error), error)).strip()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} socket={_socket_path()} {detail}\n")
    except Exception:  # noqa: BLE001 - a diagnostic may not break the session
        pass


def _trace(raw: str) -> None:
    """Append `raw` to ``RHIZOME_TRACE_LOG``, if that variable is set.

    Sibling of :func:`_log_failure`: that one records *failures*, this one records
    *arrivals*. It exists to answer a question the pipeline otherwise destroys --
    what Claude Code actually puts in the ``PostToolUse`` JSON. `normalize.py`
    reads only the handful of fields it knows about, so any per-subagent identity
    would be dropped without ever being seen; with the variable set, the payload
    is preserved verbatim and can be inspected.

    One line per invocation, no timestamp prefix, so ``json.loads`` on a line
    hands back the original dict. Best-effort, like the failure log.
    """
    path = os.environ.get("RHIZOME_TRACE_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(raw.rstrip("\n") + "\n")
    except Exception:  # noqa: BLE001 - a diagnostic may not break the session
        pass
