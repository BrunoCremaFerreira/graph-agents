"""Contract tests (RED) for what a refused start looks like to the person running it.

Motivation: the daemon now refuses to take over an ingest socket another daemon
is still listening on -- it raises `IngestSocketInUseError` instead of unlinking
the name out from under the first process (`tests/test_ingest_socket_guard.py`).
That is the right decision and the wrong presentation: the exception propagates
out of `asyncio.run` in `main()`, so starting a second daemon prints a full
Python traceback ending in a class name.

A traceback is what a program prints when it did not anticipate what happened.
This was anticipated -- it is the single most likely way a start fails, because
the ordinary way to get here is to double-click, or to run `rhi` twice, or to
forget the one already running in another terminal -- and the whole point of
giving the condition a name was to let the launcher say something a person can
act on. Nobody reads twenty frames of asyncio internals and concludes "a daemon
is already running".

What is pinned here is the user-visible half, and deliberately not the wording:

  * a non-zero exit status, so a launcher, a shell script or a desktop
    integration can tell that the daemon did not start;
  * the socket path, because that is the actionable part -- it names both what
    is in the way and the variable to change;
  * no traceback and no stack frames, which is the whole subject.

The other daemon is faked by the test holding the socket itself: the ingest
protocol is newline-delimited JSON over AF_UNIX, and `rhizome_graph.ipc`'s probe
only needs something that accepts a connection.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: How long the daemon is given to caption, seed an empty directory, discover
#: the live socket and refuse. Generous; the refusal happens before the listener.
REFUSAL_TIMEOUT_SECONDS = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def occupied_socket(tmp_path: Path):
    """A live AF_UNIX listener at a throwaway path -- the "other daemon"."""
    path = tmp_path / "ingest.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    try:
        yield path
    finally:
        listener.close()


@pytest.fixture()
def refusal(occupied_socket: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    """`python -m daemon.server` started against that live socket."""
    root = tmp_path / "observed"
    root.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("RHIZOME_")
    }
    environment.update(
        {
            "RHIZOME_SOCKET": str(occupied_socket),
            "RHIZOME_PROJECT_ROOT": str(root),
            "RHIZOME_HTTP_PORT": str(_free_port()),
        }
    )
    try:
        return subprocess.run(
            [sys.executable, "-m", "daemon.server"],
            cwd=str(REPO_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=REFUSAL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - a hang is its own bug
        pytest.fail(
            "the daemon neither started nor refused within "
            f"{REFUSAL_TIMEOUT_SECONDS:.0f}s against a live ingest socket"
        )


def test_a_refused_start_exits_non_zero(refusal: subprocess.CompletedProcess) -> None:
    """A launcher has to be able to tell that no daemon is now running."""
    assert refusal.returncode != 0


def test_a_refused_start_is_not_a_crash(refusal: subprocess.CompletedProcess) -> None:
    """A negative code is a signal death; this is a decision, not a fatality."""
    assert 0 < refusal.returncode < 128


def test_a_refused_start_prints_no_traceback(
    refusal: subprocess.CompletedProcess,
) -> None:
    """The condition has a name precisely so it never has to be printed as one."""
    assert "Traceback" not in refusal.stderr


def test_a_refused_start_prints_no_stack_frames(
    refusal: subprocess.CompletedProcess,
) -> None:
    """Separate from the header above: a chained exception can lose the word."""
    assert 'File "' not in refusal.stderr


def test_the_refusal_names_the_socket_that_is_in_the_way(
    refusal: subprocess.CompletedProcess, occupied_socket: Path
) -> None:
    """The actionable part: what is occupied, and which variable points at it."""
    assert str(occupied_socket) in refusal.stderr


def test_the_refusal_is_the_last_thing_the_user_reads(
    refusal: subprocess.CompletedProcess, occupied_socket: Path
) -> None:
    """One line, at the end -- not a message buried above pages of unwinding."""
    lines = [line for line in refusal.stderr.splitlines() if line.strip()]

    assert lines, "a refused start said nothing at all"
    assert str(occupied_socket) in lines[-1]
