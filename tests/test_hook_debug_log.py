"""Contract tests (RED) for the hook's opt-in debug log.

The hook swallows every error so it can never disrupt a Claude Code session
(CLAUDE.md, non-negotiable). The cost is that a daemon that is simply not
running looks exactly like a working setup with nothing to show -- which is how
an empty screen goes undiagnosed for hours.

The escape hatch: when `GRAPHAGENTS_DEBUG_LOG` names a file, failures are
appended there. Unset, the hook stays as silent as before.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "emit_event.py"

PAYLOAD = json.dumps(
    {
        "session_id": "sess-abc",
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "/proj/src/app.py"},
    }
)


def _run_hook(payload: str, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload.encode(),
        capture_output=True,
        env=env,
    )


def test_unreachable_daemon_still_exits_zero_and_prints_nothing(tmp_path: Path):
    result = _run_hook(
        PAYLOAD, {"GRAPHAGENTS_SOCKET": str(tmp_path / "absent.sock")}
    )

    assert result.returncode == 0
    assert result.stdout == b""


def test_failure_is_recorded_when_a_debug_log_is_configured(tmp_path: Path):
    log = tmp_path / "hook.log"

    _run_hook(
        PAYLOAD,
        {
            "GRAPHAGENTS_SOCKET": str(tmp_path / "absent.sock"),
            "GRAPHAGENTS_DEBUG_LOG": str(log),
        },
    )

    assert log.exists()
    assert "absent.sock" in log.read_text()


def test_nothing_is_written_when_the_debug_log_is_not_configured(tmp_path: Path):
    log = tmp_path / "hook.log"

    _run_hook(PAYLOAD, {"GRAPHAGENTS_SOCKET": str(tmp_path / "absent.sock")})

    assert not log.exists()


def test_an_unwritable_debug_log_does_not_break_the_hook(tmp_path: Path):
    result = _run_hook(
        PAYLOAD,
        {
            "GRAPHAGENTS_SOCKET": str(tmp_path / "absent.sock"),
            "GRAPHAGENTS_DEBUG_LOG": str(tmp_path / "no" / "such" / "dir.log"),
        },
    )

    assert result.returncode == 0
