"""Contract tests (RED) for the hook's opt-in raw-payload trace log.

The defect that motivates this file: when the specialist agents work, the tree
updates but no agent figure ever appears. Every event arrives with `agent: ""`,
and an empty agent must never create an actor (CLAUDE.md). Installing the hook
buys exactly *one* figure per session; whether we can have one figure per
*subagent* depends on what Claude Code actually puts in the `PostToolUse` JSON --
a question nobody can answer, because the hook forwards the payload and forgets
it. `normalize.py` only looks at the handful of fields it knows, so any
`subagent_id`-shaped field would be dropped without a trace.

`RHIZOME_TRACE_LOG` is the instrument: when it names a file, the hook
appends the payload **exactly as it arrived on stdin**, before any parsing. It
is the sibling of `RHIZOME_DEBUG_LOG` (which records *failures*); this one
records *arrivals*.

Format pinned here: one entry per invocation, one line, the raw stdin text
verbatim plus a trailing newline -- so `json.loads(line)` on a well-formed
payload hands back precisely the dict Claude Code sent, unknown fields included.

The module's hard rules still win over the diagnostic: unset variable means
total silence, an unwritable path must not raise, and the process exits 0 with
an empty stdout no matter what -- the hook runs on every tool call and blocks
the agent loop.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "emit_event.py"

PAYLOAD = {
    "session_id": "sess-abc",
    "hook_event_name": "PostToolUse",
    "tool_name": "Write",
    "tool_input": {"file_path": "/proj/src/app.py"},
}


def _run_hook(payload: str, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload.encode(),
        capture_output=True,
        env=env,
        timeout=10,
    )


class _FakeDaemon:
    """Minimal AF_UNIX listener that records the lines the hook sends it."""

    def __init__(self, path: Path):
        self.path = str(path)
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
            with conn:
                chunks = []
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                self.received.extend(b"".join(chunks).splitlines())
        except OSError:
            pass

    def __enter__(self) -> "_FakeDaemon":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._thread.join(timeout=5)
        self._sock.close()


def test_nothing_is_written_when_the_trace_variable_is_unset(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"

    _run_hook(
        json.dumps(PAYLOAD), {"RHIZOME_SOCKET": str(tmp_path / "absent.sock")}
    )

    assert not trace.exists()
    assert list(tmp_path.iterdir()) == []


def test_an_empty_trace_variable_is_treated_as_unset(tmp_path: Path):
    _run_hook(
        json.dumps(PAYLOAD),
        {
            "RHIZOME_SOCKET": str(tmp_path / "absent.sock"),
            "RHIZOME_TRACE_LOG": "",
        },
    )

    assert list(tmp_path.iterdir()) == []


def test_the_raw_payload_is_appended_as_one_recoverable_line(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"

    with _FakeDaemon(tmp_path / "d.sock"):
        _run_hook(
            json.dumps(PAYLOAD),
            {
                "RHIZOME_SOCKET": str(tmp_path / "d.sock"),
                "RHIZOME_TRACE_LOG": str(trace),
            },
        )

    lines = trace.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == PAYLOAD


def test_a_second_invocation_appends_instead_of_overwriting(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    first = {**PAYLOAD, "session_id": "sess-first"}
    second = {**PAYLOAD, "session_id": "sess-second"}
    env = {
        "RHIZOME_SOCKET": str(tmp_path / "absent.sock"),
        "RHIZOME_TRACE_LOG": str(trace),
    }

    _run_hook(json.dumps(first), env)
    _run_hook(json.dumps(second), env)

    lines = trace.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["session_id"] for line in lines] == [
        "sess-first",
        "sess-second",
    ]


def test_a_field_normalize_ignores_survives_into_the_trace(tmp_path: Path):
    # The entire point: discover whether Claude Code ships per-subagent identity.
    trace = tmp_path / "trace.jsonl"
    payload = {**PAYLOAD, "subagent_id": "sub-42", "cwd": "/proj"}

    _run_hook(
        json.dumps(payload),
        {
            "RHIZOME_SOCKET": str(tmp_path / "absent.sock"),
            "RHIZOME_TRACE_LOG": str(trace),
        },
    )

    recorded = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])
    assert recorded["subagent_id"] == "sub-42"
    assert recorded["cwd"] == "/proj"


def test_the_payload_is_traced_even_when_the_daemon_is_unreachable(tmp_path: Path):
    # Collection happens precisely when the daemon is down; a failed send must
    # not swallow the observation.
    trace = tmp_path / "trace.jsonl"

    result = _run_hook(
        json.dumps(PAYLOAD),
        {
            "RHIZOME_SOCKET": str(tmp_path / "absent.sock"),
            "RHIZOME_TRACE_LOG": str(trace),
        },
    )

    assert result.returncode == 0
    assert json.loads(trace.read_text(encoding="utf-8").splitlines()[0]) == PAYLOAD


def test_malformed_stdin_is_traced_verbatim_and_the_hook_stays_silent(tmp_path: Path):
    # Trace what *arrived*, not what parsed: an unparseable payload is the most
    # interesting thing this instrument can catch.
    trace = tmp_path / "trace.jsonl"

    result = _run_hook(
        "this is not json at all",
        {
            "RHIZOME_SOCKET": str(tmp_path / "absent.sock"),
            "RHIZOME_TRACE_LOG": str(trace),
        },
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert trace.read_text(encoding="utf-8").splitlines() == ["this is not json at all"]


def test_tracing_does_not_stop_the_payload_from_reaching_the_daemon(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"

    with _FakeDaemon(tmp_path / "d.sock") as daemon:
        _run_hook(
            json.dumps(PAYLOAD),
            {
                "RHIZOME_SOCKET": str(tmp_path / "d.sock"),
                "RHIZOME_TRACE_LOG": str(trace),
            },
        )

    assert [json.loads(line) for line in daemon.received] == [PAYLOAD]


def test_tracing_does_not_disable_the_debug_log(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    debug = tmp_path / "hook.log"

    _run_hook(
        json.dumps(PAYLOAD),
        {
            "RHIZOME_SOCKET": str(tmp_path / "absent.sock"),
            "RHIZOME_DEBUG_LOG": str(debug),
            "RHIZOME_TRACE_LOG": str(trace),
        },
    )

    assert "absent.sock" in debug.read_text(encoding="utf-8")
    assert trace.exists()


def test_an_unwritable_trace_path_does_not_break_the_hook(tmp_path: Path):
    result = _run_hook(
        json.dumps(PAYLOAD),
        {
            "RHIZOME_SOCKET": str(tmp_path / "absent.sock"),
            "RHIZOME_TRACE_LOG": str(tmp_path / "no" / "such" / "dir.jsonl"),
        },
    )

    assert result.returncode == 0
    assert result.stdout == b""


def test_a_directory_as_trace_path_does_not_break_the_hook(tmp_path: Path):
    target = tmp_path / "somedir"
    target.mkdir()

    result = _run_hook(
        json.dumps(PAYLOAD),
        {
            "RHIZOME_SOCKET": str(tmp_path / "absent.sock"),
            "RHIZOME_TRACE_LOG": str(target),
        },
    )

    assert result.returncode == 0
    assert result.stdout == b""
