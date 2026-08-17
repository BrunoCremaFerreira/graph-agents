"""Contract tests (RED) for rhizome_graph.ipc: is a daemon already listening?

Motivation: `daemon/server.py` clears the way for its ingest listener with two
unconditional lines::

    if os.path.exists(socket_path):
        os.unlink(socket_path)

No probe, because until now a second daemon was an accident rather than a case:
`start.sh` in a terminal, and the only thing that path expected to find was the
socket file a crashed daemon left behind. Installed as a desktop application and
launched from a menu, two instances is the normal state, and the second unlinks
the first one's socket out from under it -- with the consequences spelled out in
`tests/test_ingest_socket_guard.py`, which specifies what `run()` does with the
answer. This file specifies only the question.

Where it lives: `rhizome_graph/`, beside `paths.py` and `repo.py`, for their
reason. It answers with the stdlib alone, needs no event loop, no `websockets`
and no `watchdog`, and is worth exercising without starting a server -- which is
exactly what these tests do. It is not *pure* in the arithmetic sense; neither is
`complete_dir`, which reads directories, nor `read_branch`, which reads
`.git/HEAD`. What it inherits from them instead is the rule that matters: it
never raises. Its caller is a daemon deciding at boot whether to start, and a
traceback there replaces a refusal the user could have acted on.

Why it has to connect: `os.path.exists` cannot tell a live socket from a stale
one -- that is precisely the file the current code deletes -- and `S_ISSOCK`
only says the file was once a socket, which is equally true of the corpse. So
the probe connects and hangs up. The ingest protocol is newline-delimited JSON,
so a connection that sends nothing costs the other daemon one empty read it
already handles.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import socket
from pathlib import Path

from rhizome_graph.ipc import socket_is_live


def _listener(path: Path) -> socket.socket:
    """A real AF_UNIX listener bound at `path`, as another daemon would leave."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(8)
    return listener


def _stale_socket_file(path: Path) -> None:
    """Bind and close: the file survives, nobody is behind it. A crashed daemon."""
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()


def test_a_bound_listener_is_live(tmp_path: Path) -> None:
    path = tmp_path / "live.sock"
    listener = _listener(path)

    try:
        assert socket_is_live(str(path)) is True
    finally:
        listener.close()


def test_a_stale_socket_file_is_not_live(tmp_path: Path) -> None:
    """The crash-recovery case: the file is there, the daemon is not."""
    path = tmp_path / "stale.sock"
    _stale_socket_file(path)

    assert socket_is_live(str(path)) is False


def test_a_path_that_does_not_exist_is_not_live(tmp_path: Path) -> None:
    assert socket_is_live(str(tmp_path / "never-existed.sock")) is False


def test_a_regular_file_in_the_way_is_not_live(tmp_path: Path) -> None:
    """Not a socket at all -- refusing to call it live is what lets it be moved."""
    path = tmp_path / "not-a-socket"
    path.write_text("hello\n", encoding="utf-8")

    assert socket_is_live(str(path)) is False


def test_a_socket_that_stopped_listening_is_no_longer_live(tmp_path: Path) -> None:
    """The probe reflects now, not history: same path, closed listener."""
    path = tmp_path / "was-live.sock"
    listener = _listener(path)
    assert socket_is_live(str(path)) is True

    listener.close()

    assert socket_is_live(str(path)) is False


def test_a_path_that_cannot_be_a_socket_answers_false_instead_of_raising(
    tmp_path: Path,
) -> None:
    """House rule: this runs at boot, where a traceback replaces a refusal.

    A NUL byte and an over-long path are the two ways `connect` throws something
    other than a plain `OSError`, and both reach here from `RHIZOME_SOCKET`.
    """
    assert socket_is_live(str(tmp_path / "nul\x00byte.sock")) is False
    assert socket_is_live("/" + "x" * 5000 + ".sock") is False
