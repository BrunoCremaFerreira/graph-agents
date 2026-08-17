"""Contract tests (RED) for starting the daemon off the main thread.

Motivation: packaging this as an installed desktop application puts a GUI on the
main thread and asyncio on a worker thread, in one process. `run()` cannot do
that today. It installs its shutdown handlers with::

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, ...)

`NotImplementedError` is the *Windows* failure, and it is the only one
suppressed. On Linux, off the main thread, `add_signal_handler` reaches
`signal.set_wakeup_fd` and raises `RuntimeError` instead -- unsuppressed, so it
tears `run()` down after the ingest socket, the watcher, the polls and the
listener are all already up, leaving them to whatever closes the loop. Signals
are a main-thread facility by definition; a daemon that only knows how to stop
via a signal handler cannot be embedded.

What these specify is therefore two halves of one property: `run()` must *reach*
a serving state on a worker thread, and it must *leave* it cleanly on request
from another thread. The stop request used here is cancellation of the task
running `run()`, delivered with `loop.call_soon_threadsafe(task.cancel)` -- the
only handle another thread legitimately has on a coroutine, and the one an
embedding GUI would use on window close. `run()` may keep its signal handlers
for the command-line case; it must simply not require them.

Nothing here asserts *how* the fix is spelled (a broader `except`, a main-thread
check, an injected stop handle): only that a worker thread can start it and stop
it. Hermetic -- `tmp_path` root, `tmp_path` ingest socket, an ephemeral port.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import socket
import threading
import time
from pathlib import Path

import pytest

from daemon.server import run
from rhizome_graph.cli import build_parser, settings_from

#: How long the daemon is given to open its listener, and to shut down again.
#: Generous: seeding walks the (empty) project root and the watcher starts.
STARTUP_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0


def _free_port() -> int:
    """An ephemeral port, released before the daemon binds it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _port_accepts(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        return client.connect_ex(("127.0.0.1", port)) == 0


class DaemonThread:
    """`run()` on a worker thread, with its own event loop and a stop handle.

    The thread owns an `asyncio.run`, exactly as an embedding application would:
    the GUI keeps the main thread and never sees a loop. `run()` is wrapped in a
    task so the main thread has something to cancel; `stop()` posts that
    cancellation onto the worker's loop with `call_soon_threadsafe`, which is the
    only thread-safe door into a running loop.

    Any exception escaping `run()` is captured in `error` rather than left to the
    threading excepthook, where it would print during an unrelated test and the
    assertion here would fail with a bare timeout instead of the reason.
    """

    def __init__(self, socket_path: Path, http_port: int, project_root: Path) -> None:
        # `run()` is configured by one value now, so the three scalars this
        # class was given become the fields of a `Settings`
        # (`tests/test_cli_settings.py`). The arguments stay as they were: what
        # this class specifies is the *thread*, and none of that is affected by
        # how the daemon learns which port to bind.
        self._settings = dataclasses.replace(
            settings_from(
                build_parser().parse_args([str(project_root)]), {}, str(project_root)
            ),
            port=http_port,
            socket_path=str(socket_path),
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._thread_main, name="daemon", daemon=True)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            self.error = exc
        finally:
            self._running.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(run(self._settings))
        self._running.set()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    def start(self) -> None:
        self.thread.start()
        self._running.wait(timeout=STARTUP_TIMEOUT_SECONDS)

    def stop(self) -> None:
        loop, task = self._loop, self._task
        if loop is None or task is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(task.cancel)

    def wait_until_serving(self, port: int) -> bool:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.error is not None:
                return False
            if _port_accepts(port):
                return True
            time.sleep(0.05)
        return False


@pytest.fixture()
def daemon_thread(tmp_path: Path):
    """A daemon on a worker thread, always stopped and joined afterwards."""
    started: list[DaemonThread] = []

    def factory() -> tuple[DaemonThread, int]:
        port = _free_port()
        worker = DaemonThread(
            socket_path=tmp_path / "ingest.sock",
            http_port=port,
            project_root=tmp_path,
        )
        started.append(worker)
        worker.start()
        return worker, port

    yield factory

    for worker in started:
        worker.stop()
        worker.thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)


def test_the_daemon_serves_when_started_on_a_worker_thread(daemon_thread) -> None:
    """A GUI owns the main thread, so the loop has to live somewhere else."""
    worker, port = daemon_thread()

    serving = worker.wait_until_serving(port)

    assert worker.error is None, (
        "run() died on a worker thread instead of serving: "
        f"{type(worker.error).__name__}: {worker.error}"
    )
    assert serving, f"nothing accepted a connection on :{port}"


def test_the_daemon_stops_when_cancelled_from_another_thread(daemon_thread) -> None:
    """Window close arrives on the main thread; the loop is on the worker."""
    worker, port = daemon_thread()
    assert worker.wait_until_serving(port), (
        "the daemon never started, so this cannot specify how it stops: "
        f"{type(worker.error).__name__}: {worker.error}"
    )

    worker.stop()
    worker.thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    assert not worker.thread.is_alive(), "the daemon thread outlived its cancellation"
    assert worker.error is None, (
        "stopping the daemon raised instead of unwinding: "
        f"{type(worker.error).__name__}: {worker.error}"
    )
