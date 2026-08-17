"""Contract tests (RED) for what run() does with an ingest socket in use.

Motivation: `run()` clears the way for its listener with two unconditional
lines::

    if os.path.exists(socket_path):
        os.unlink(socket_path)

There is no probe for a live listener, because until now there was never
expected to be one: the daemon was started by `start.sh` in a terminal, and a
stale socket file left by a crash was the only thing that path could plausibly
find. Installing this as a desktop application changes the arithmetic -- once
`rhi` is on `$PATH`, two windows is the normal case, not the accident -- and the
second one unlinks the first one's socket out from under it. The first daemon
keeps its file descriptor and goes on serving its browser, so nothing looks
wrong; but the *name* now belongs to the second daemon, and every hook,
including the ones fired by the agents the first window is watching, connects to
the new one. The first window then shows a tree updating with nobody on camera
-- this project's signature failure, produced here by a second copy of the
project itself and indistinguishable from hooks not being installed at all.

Given a live socket, `run()` must not unlink it, and must say so by raising
`IngestSocketInUseError`: named rather than generic, so the launcher can catch
exactly this and tell the user a daemon is already running instead of printing a
traceback. Failing loudly is the right trade even though most of this codebase
fails silently -- silence is for the hook, which must never disturb an agent's
session, whereas this is a process refusing to start, with the user present and
nothing to be silent for.

Given a *stale* socket file it must still unlink and listen. That recovery is
what the two lines above were written for, a SIGKILLed daemon leaves exactly
that behind, and a refusal there would make every crash need a manual `rm` --
a worse failure than the one being fixed.

The probe that answers "live or stale" is specified separately, in
`tests/test_ipc.py`. Nothing here imports it: these tests are about the decision,
and they must fail on their own assertions rather than on somebody else's
missing module.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import socket
from pathlib import Path

import pytest

import daemon.server as server
from daemon.server import run
from rhizome_graph.cli import build_parser, settings_from

#: How long `run()` is given to reach a serving state before a test pulls it
#: down. Also the ceiling that keeps a missing refusal from hanging the suite:
#: without one, `run()` waits on a stop signal that never comes.
RUN_TIMEOUT_SECONDS = 5.0

#: The exception `run()` must raise rather than take over a live socket. Looked
#: up by name so its absence fails as a readable assertion instead of an
#: ImportError at collection time, which would take the other tests with it.
ERROR_NAME = "IngestSocketInUseError"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _settings(socket_path: Path, project_root: Path):
    """What this daemon is: that root, that ingest socket, an ephemeral port.

    `run()` is configured by a value rather than by three scalars and four
    ambient reads (`tests/test_cli_settings.py`), so the scalars this file used
    to pass become fields. Nothing else here changes: the socket path is still
    the subject of every test below, and it still arrives from `tmp_path`.
    """
    parsed = build_parser().parse_args([str(project_root)])
    return dataclasses.replace(
        settings_from(parsed, {}, str(project_root)),
        port=_free_port(),
        socket_path=str(socket_path),
    )


def _listener(path: Path) -> socket.socket:
    """A real AF_UNIX listener bound at `path`: the daemon that got there first."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)
    return listener


def _stale_socket_file(path: Path) -> None:
    """Bind and close: the file survives, nobody is behind it. A crashed daemon."""
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()


def _connects(path: Path) -> bool:
    """Does anything at all accept a connection at `path`?"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1.0)
        try:
            client.connect(str(path))
        except OSError:
            return False
        return True


def _answers_the_path(listener: socket.socket, path: Path) -> bool:
    """Connect to `path` and prove it is `listener` that accepts.

    "Something accepts" is not the question: after the current code unlinks the
    path it binds its *own* socket there, so a bare connect succeeds while the
    first daemon has been cut off from every hook. Only accepting the connection
    on the original listener tells the two apart.
    """
    listener.settimeout(1.0)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(str(path))
            accepted, _ = listener.accept()
            accepted.close()
            return True
    except OSError:
        return False


async def _run_briefly(socket_path: Path, project_root: Path) -> None:
    """Let `run()` do whatever it does, then pull it down.

    The shutdown is swallowed and anything `run()` raises is not: a refusal has
    to reach the caller, while the cancellation this test uses to stop a daemon
    that did start is noise.
    """
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(
            run(_settings(socket_path, project_root)),
            timeout=RUN_TIMEOUT_SECONDS,
        )


async def _serve_then_stop(socket_path: Path, project_root: Path) -> BaseException | None:
    """Start `run()`, wait for its ingest socket to answer, then stop it.

    Returns whatever `run()` raised, or `None` if it was serving when it was
    cancelled -- which is the outcome the stale-socket test specifies.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(run(_settings(socket_path, project_root)))
    deadline = loop.time() + RUN_TIMEOUT_SECONDS
    failure: BaseException | None = TimeoutError("the ingest socket never accepted")
    while loop.time() < deadline:
        if task.done():
            return task.exception() or RuntimeError("run() returned without serving")
        if await asyncio.to_thread(_connects, socket_path):
            failure = None
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return failure


def test_a_second_daemon_leaves_the_first_daemons_socket_alone(tmp_path: Path) -> None:
    """The defect itself: after the second start, hooks must still find the first."""
    path = tmp_path / "ingest.sock"
    listener = _listener(path)

    try:
        with contextlib.suppress(Exception):
            asyncio.run(_run_briefly(path, tmp_path))

        assert _answers_the_path(listener, path), (
            "the running daemon's ingest socket was unlinked and replaced; every "
            "hook now reaches the second daemon, and the first window shows a "
            "tree updating with nobody on camera"
        )
    finally:
        listener.close()


def test_the_daemon_refuses_to_start_on_a_live_ingest_socket(tmp_path: Path) -> None:
    """Named, so the launcher can say "already running" instead of a traceback."""
    error = getattr(server, ERROR_NAME, None)
    assert isinstance(error, type) and issubclass(error, Exception), (
        f"daemon.server must expose {ERROR_NAME} -- the exception raised instead "
        f"of taking over a live ingest socket; found {error!r}"
    )

    path = tmp_path / "ingest.sock"
    listener = _listener(path)

    try:
        with pytest.raises(error):
            asyncio.run(_run_briefly(path, tmp_path))
    finally:
        listener.close()


def test_a_stale_socket_file_is_still_cleared_away(tmp_path: Path) -> None:
    """The refusal must not cost the recovery those two lines were written for."""
    path = tmp_path / "ingest.sock"
    _stale_socket_file(path)

    failure = asyncio.run(_serve_then_stop(path, tmp_path))

    assert failure is None, (
        "a stale socket file is a crashed daemon, not a running one; the new "
        f"daemon must unlink it and listen there. Instead: {failure!r}"
    )
