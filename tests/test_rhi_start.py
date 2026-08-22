"""Contract tests (RED) for `rhi <dir> --no-window` actually starting the thing.

Motivation: stage A made the configuration a value and stage B made `run()` take
it, but nothing yet joins a command line to a served page. `main()` in
`daemon/server.py` is reachable only as `python -m daemon.server` with variables
exported around it, which is what `start.sh` does and what nobody else will. The
behaviour specified here is the whole product from the outside: type a command,
get a URL, open it, see the graph.

The assertion that carries the file is the page one, and it is deliberately
indirect. `window.__RHIZOME_TOKEN__` in the response body proves three separate
things at once, each of which has failed on its own before:

  * the daemon **found** `web/dist` -- the search in `assets.py` ran over this
    installation's real layout, not over a `tmp_path` fixture handed in by a
    test, which is the one thing every existing daemon test bypasses with
    `static_root=`;
  * it **served `index.html`** rather than answering the WebSocket alone, which
    is the silent failure mode `assets.py` was written for -- a healthy daemon
    behind a blank page;
  * it **injected the control token**, without which the page draws the graph and
    then refuses every `ctrl+L`, every completion and every file click.

**The URL is parsed out of stdout and fetched, never assumed.** That is not
fussiness: the next step lets a default port move when it is busy, and a
launcher that prints one port while serving another is worse than one that
prints nothing -- the user follows the link, gets a connection refused, and
concludes the program is broken. What is pinned is that the printed URL answers.

**Nothing sets `PYTHONUNBUFFERED`** (see `tests/rhi_process.py`): a `print` to a
pipe is block-buffered, and this process runs until the user quits it, so a URL
that is not flushed is a URL nobody ever reads. A timeout waiting for the line is
that defect, not a slow machine.

**`--no-window` is a real flag today even though the window is stage D.** What it
means now is only what is pinned: the daemon runs in the foreground and no window
is opened -- there being no window to open yet, the flag is a promise about the
default rather than about itself. Its default is `False`, so that when a window
does exist, running without the flag opens one.

The instance is started **once**, module-scoped, and every test reads the record
of that one run. Booting a daemon per assertion would cost six starts to say six
things about one.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest

from rhi_process import URL, free_port, get, require_front_end, start
from rhizome_graph.cli import build_parser

#: How long the daemon is given to caption, seed a nearly empty directory, poll
#: the working tree's status, bind and print. Generous; the imports alone
#: (`websockets`, `watchdog`) dominate.
STARTUP_TIMEOUT_SECONDS = 90.0

#: How long the printed URL is retried before the fetch is called a failure. A
#: launcher may reasonably print the URL a beat before the listener accepts.
FETCH_TIMEOUT_SECONDS = 20.0

#: What proves the page came out of `index.html` with the token injected into it.
#: Not the token's value -- that is minted per boot -- only the assignment the
#: browser reads it from.
TOKEN_MARKER = "window.__RHIZOME_TOKEN__"


@dataclass
class Started:
    """Everything one full start/serve/stop cycle had to say."""

    url: str
    port: int
    status: int
    body: str
    alive_while_serving: bool
    socket_while_serving: bool
    socket_after_exit: bool
    returncode: int
    stdout: str
    stderr: str


def _fetch(url: str) -> tuple[int, str]:
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
    status, body = 0, ""
    while time.monotonic() < deadline:
        try:
            status, body = get(url)
        except Exception:  # noqa: BLE001 - not accepting yet is not an answer
            time.sleep(0.2)
            continue
        return status, body
    return status, body


@pytest.fixture(scope="module")
def started(tmp_path_factory: pytest.TempPathFactory) -> Started:
    """One `rhi <dir> --no-window`, run to completion, recorded."""
    root = tmp_path_factory.mktemp("observed")
    (root / "hello.txt").write_text("hello\n", encoding="utf-8")
    ingest = tmp_path_factory.mktemp("run") / "ingest.sock"
    port = free_port()

    running = start(
        (str(root), "--no-window", "--port", str(port), "--socket", str(ingest))
    )
    try:
        match = running.wait_for_line(URL, STARTUP_TIMEOUT_SECONDS)
        url = match.group(0).rstrip(".,")
        alive = running.is_alive()
        socket_while_serving = ingest.exists()
        status, body = _fetch(url)
    finally:
        returncode = running.stop()

    return Started(
        url=url,
        port=port,
        status=status,
        body=body,
        alive_while_serving=alive,
        socket_while_serving=socket_while_serving,
        socket_after_exit=ingest.exists(),
        returncode=returncode,
        stdout=running.out,
        stderr=running.err,
    )


# --- 1. the flag exists before anything is started with it ------------------


def test_the_parser_accepts_no_window() -> None:
    """A crisp failure for the flag itself, ahead of the subprocess tests."""
    args = build_parser().parse_args(["--no-window"])

    assert args.no_window is True


def test_a_window_is_the_default_once_there_is_one_to_open() -> None:
    """`--no-window` is an opt-out, so plain `rhi` must not already be one."""
    args = build_parser().parse_args([])

    assert args.no_window is False


# --- 2. what the user is told, and whether it is true -----------------------


def test_starting_prints_a_url(started: Started) -> None:
    """The one output that matters: where to look."""
    assert started.url.startswith("http://"), started.stdout


def test_the_printed_url_answers(started: Started) -> None:
    """Printed and served must be the same address -- see the module docstring.

    Stands down, rather than failing, when nothing here has built `web/dist`:
    the daemon then answers 503 by design and the page it would have served does
    not exist, which is an incomplete environment and not a broken program. The
    fixture is deliberately left alone, so every other assertion about that one
    run -- the URL, the port, the foreground, the socket, the exit status -- keeps
    being made. See `NO_FRONT_END_REASON` in `tests/rhi_process.py`.
    """
    require_front_end()

    assert started.status == 200, (
        f"{started.url} answered {started.status}\n"
        f"--- stdout ---\n{started.stdout}--- stderr ---\n{started.stderr}"
    )


def test_the_printed_url_carries_the_port_that_was_asked_for(started: Started) -> None:
    """An explicit `--port` is never moved, so the URL must name it."""
    assert urlparse(started.url).port == started.port


def test_the_served_page_is_the_built_front_end(started: Started) -> None:
    """`web/dist` was found by the real search, over the real installation.

    Which is only a question worth asking where there is a build to find: with
    none, this asks the same resolver the daemon asks and stands down, because
    "nobody ran `npm run build`" must not read as "the asset search regressed".
    """
    require_front_end()

    assert TOKEN_MARKER in started.body, (
        "the page served carries no token assignment, so either web/dist was "
        "not found, index.html was not served, or the token was not injected"
    )


# --- 3. foreground, and a clean exit ----------------------------------------


def test_the_daemon_stays_in_the_foreground(started: Started) -> None:
    """`--no-window` runs the daemon here, not detached: this process IS it."""
    assert started.alive_while_serving is True


def test_the_ingest_socket_exists_while_it_serves(started: Started) -> None:
    """The half of the pair below that makes the removal mean something."""
    assert started.socket_while_serving is True


def test_terminating_it_exits_zero(started: Started) -> None:
    """Quitting is not a failure; a launcher and a shell both read this."""
    assert started.returncode == 0, started.stderr


def test_it_leaves_no_ingest_socket_behind(started: Started) -> None:
    """A corpse at the socket path is what the next start has to reason about."""
    assert started.socket_after_exit is False


def test_starting_prints_no_traceback(started: Started) -> None:
    """A successful start says nothing a person has to decode."""
    assert "Traceback" not in started.stderr
