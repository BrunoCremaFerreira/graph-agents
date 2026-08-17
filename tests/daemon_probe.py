"""Running one daemon inside the test process, for the stage-D specifications.

Not a test file (the name keeps pytest from collecting it), and a sibling of
`tests/rhi_process.py` for the same reason that one exists: two modules --
`test_ready_callback.py` and `test_window_lifecycle.py` -- both need to start
`run()` in this process, watch it reach a serving state and then watch it leave
one, and a second copy of that machinery is how the two would drift apart.

`tests/rhi_process.py` drives `rhi` as a *process*, which is what the
user-facing behaviour is written in. This drives `run()` as a *coroutine*, which
is where the readiness callback and the shutdown future actually live: an
assertion about which future a window resolves cannot be made through a pipe.

Everything here is hermetic -- a `tmp_path` root, a `tmp_path` ingest socket, an
ephemeral port, and a `web/dist` lookalike that the real one cannot be mistaken
for -- so nothing observed depends on the machine, on the checkout's build state
or on the developer's shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import socket
import time
from pathlib import Path

import pytest

from rhizome_graph.cli import build_parser, settings_from

#: How long the daemon is given to caption, seed a nearly empty directory, ask
#: the working tree for its status and bind. Generous on purpose.
STARTUP_TIMEOUT_SECONDS = 20.0

#: How long a shutdown is given once it has been asked for. A daemon that has
#: been told to stop and has not is the defect these files are about.
SHUTDOWN_TIMEOUT_SECONDS = 20.0

#: What the served page is recognised by -- content that survives the token
#: injection the daemon performs on the way out.
MARKER = '<canvas id="stage"></canvas>'


def free_port() -> int:
    """An ephemeral port, released before the daemon binds it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def accepts(host: str, port: int, timeout: float = 0.5) -> bool:
    """Does something accept a TCP connection at `(host, port)` right now?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        return client.connect_ex((host, port)) == 0


def unix_socket_accepts(path: str, timeout: float = 0.5) -> bool:
    """Does something accept a connection on the AF_UNIX socket at `path`?

    Deliberately a connect rather than `os.path.exists`: the question a hook
    asks is whether it can hand an event over, and a socket file a crashed
    daemon left behind exists without answering.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def site(root: Path) -> Path:
    """A minimal `web/dist` lookalike, distinguishable from the real one."""
    built = root / "dist"
    built.mkdir(parents=True, exist_ok=True)
    (built / "index.html").write_text(
        "<!doctype html>\n<html>\n  <head>\n    <title>rhizome-graph</title>\n"
        f"  </head>\n  <body>{MARKER}</body>\n</html>\n",
        encoding="utf-8",
    )
    return built


def scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every `RHIZOME_*` variable from this process's environment.

    A developer shell that exports `RHIZOME_SOCKET` or `RHIZOME_PROJECT_ROOT`
    must not be able to change what any of this measures.
    """
    for name in [key for key in os.environ if key.startswith("RHIZOME_")]:
        monkeypatch.delenv(name, raising=False)


def settings_for(root: Path, **overrides):
    """A hermetic `Settings` for `root`, built by the CLI's own parser.

    `dataclasses.replace` over a `settings_from` answer rather than the
    constructor: these tests care about five fields, and a sixth added later
    must not have to be spelled in every one of them.
    """
    base = settings_from(build_parser().parse_args([str(root)]), {}, str(root))
    defaults = {
        "host": "127.0.0.1",
        "port": free_port(),
        "socket_path": str(root / "ingest.sock"),
        "web_dist": str(site(root)),
        "token": "probe-token",
    }
    defaults.update(overrides)
    return dataclasses.replace(base, **defaults)


async def wait_until(predicate, timeout: float, what: str) -> None:
    """Poll `predicate` until it is true, or fail naming what never happened."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


async def cancel_and_wait(task: asyncio.Task) -> None:
    """Stop a daemon task the way an embedding caller would, and wait for it."""
    task.cancel()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=SHUTDOWN_TIMEOUT_SECONDS)


def drive(coro, timeout: float = 90.0):
    """Run one scenario coroutine to completion under a hard ceiling."""
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))
