"""Contract tests (RED) for what a second instance does about port 8080.

Motivation: `:8080` is the default and the default is what everybody gets, so
the first collision is not an edge case -- it is what happens the second time
anybody starts this program. Today the second instance dies inside `asyncio`
with an `OSError: [Errno 98]` and a stack, which is the same defect
`tests/test_daemon_start_refusal.py` fixed for the ingest socket: an anticipated
condition presented as an accident.

The rule is one sentence, and both halves are load-bearing:

  **A default may be adjusted; an explicit request may not.**

Moving off a busy default is a kindness -- nobody chose 8080, they just did not
say. Moving off a port somebody *typed* is a lie: `--port 9000` answered on
9001 breaks an SSH forward already set up, a bookmark, a colleague who was told
which port to open, and it breaks them silently, because the program reports
success. `Settings.port_is_explicit` exists precisely to tell the two apart, and
it already counts an exported `RHIZOME_HTTP_PORT` as somebody asking.

`choose_port` takes `is_free` as an argument for the same reason `settings_from`
takes `cwd`: so that the decision can be examined without binding a socket. The
tests below bind nothing -- every port number here is fiction -- except the two at
the end, which drive a real process.

**The walk is bounded, and the bound is pinned.** On a busy machine an unbounded
search is a hang, and a hang at startup is indistinguishable from a program that
does not work. Twenty is chosen as "more attempts than a desk ever needs, fewer
than a person will wait for": twenty refused connects on loopback are
microseconds, and if 8080 through 8099 are all taken then the machine is telling
you something a twenty-first attempt will not change.

**Why the composition test reaches for the module's default.** The end-to-end
half -- a default that is busy really does move, and the URL printed really is the
one that answers -- is only reachable when the *default* is what is in the way,
and a test may not make the real `:8080` busy on the machine it runs on: that
port belongs to whatever the developer already has running there. So the
subprocess patches `cli.DEFAULT_HTTP_PORT` to a throwaway port the test itself
holds open, which works because `settings_from` resolves its defaults from the
module at call time. It is the one place these tests reach inside; the
alternative is leaving the whole point of the stage unexercised outside unit
tests of a pure function.

`choose_port`, `PortUnavailableError` and `PORT_SEARCH_LIMIT` are reached through
the module rather than imported by name, deliberately: while they do not exist, a
`from ... import` fails at *collection*, and a collection error takes the entire
suite down with it. Through the module, each test fails on its own line, naming
the attribute that is missing.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import pytest

from rhi_process import (
    REPO_ROOT,
    URL,
    clean_environment,
    entry_argv,
    free_port,
    get,
    start,
)
from rhizome_graph import cli

#: How many ports the search may try, counting the preferred one. See the module
#: docstring for why it is bounded and why the bound is this size.
EXPECTED_SEARCH_LIMIT = 20

#: The highest port number that exists. A walk that runs off the end asks the
#: operating system about 65536 and gets an error nobody anticipated.
MAX_PORT = 65535

STARTUP_TIMEOUT_SECONDS = 90.0
REFUSAL_TIMEOUT_SECONDS = 60.0


def _recording(busy: set[int]) -> tuple[list[int], Callable[[int], bool]]:
    """An `is_free` that answers from `busy` and remembers what it was asked."""
    asked: list[int] = []

    def is_free(port: int) -> bool:
        asked.append(port)
        return port not in busy

    return asked, is_free


def _holding(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    return listener


# --- 1. nothing in the way --------------------------------------------------


def test_a_free_default_port_is_the_one_chosen() -> None:
    _asked, is_free = _recording(busy=set())

    assert cli.choose_port(8080, explicit=False, is_free=is_free) == 8080


def test_a_free_explicit_port_is_the_one_chosen() -> None:
    _asked, is_free = _recording(busy=set())

    assert cli.choose_port(9000, explicit=True, is_free=is_free) == 9000


# --- 2. a default may be adjusted -------------------------------------------


def test_a_busy_default_moves_to_the_next_free_port() -> None:
    """Nobody chose 8080; they only failed to say otherwise."""
    _asked, is_free = _recording(busy={8080})

    assert cli.choose_port(8080, explicit=False, is_free=is_free) == 8081


def test_the_walk_steps_upward_one_port_at_a_time() -> None:
    """Contiguous and ascending, so the second instance lands next to the first."""
    asked, is_free = _recording(busy={8080, 8081, 8082})

    chosen = cli.choose_port(8080, explicit=False, is_free=is_free)

    assert chosen == 8083
    assert asked == [8080, 8081, 8082, 8083]


# --- 3. an explicit request may not be adjusted -----------------------------


def test_a_busy_explicit_port_is_refused_rather_than_moved() -> None:
    """`--port 9000` answering on 9001 is a success report that is not true."""
    _asked, is_free = _recording(busy={9000})

    with pytest.raises(cli.PortUnavailableError):
        cli.choose_port(9000, explicit=True, is_free=is_free)


def test_an_explicit_port_is_not_even_looked_past() -> None:
    """Separate from the raise: a walk that happens and is then discarded is
    still a walk, and the next reader will wire its result up."""
    asked, is_free = _recording(busy={9000})

    with pytest.raises(cli.PortUnavailableError):
        cli.choose_port(9000, explicit=True, is_free=is_free)

    assert asked == [9000]


def test_the_refusal_names_the_port_that_is_in_the_way() -> None:
    """The actionable part, exactly as the ingest-socket refusal names its path."""
    _asked, is_free = _recording(busy={9000})

    with pytest.raises(cli.PortUnavailableError) as raised:
        cli.choose_port(9000, explicit=True, is_free=is_free)

    assert "9000" in str(raised.value)


def test_the_refusal_is_an_ordinary_catchable_exception() -> None:
    """`main()` has to be able to catch it and print instead of unwinding."""
    assert issubclass(cli.PortUnavailableError, Exception)
    assert not issubclass(cli.PortUnavailableError, SystemExit)


# --- 4. the search is bounded -----------------------------------------------


def test_the_search_bound_is_the_number_this_project_settled_on() -> None:
    """Pinned so that raising it is a decision somebody makes, not a drift."""
    assert cli.PORT_SEARCH_LIMIT == EXPECTED_SEARCH_LIMIT


def test_an_exhausted_search_refuses_instead_of_walking_forever() -> None:
    """A hang at startup reads as a program that does not work."""
    _asked, is_free = _recording(busy=set(range(0, 70000)))

    with pytest.raises(cli.PortUnavailableError):
        cli.choose_port(8080, explicit=False, is_free=is_free)


def test_an_exhausted_search_probes_no_more_than_the_bound() -> None:
    asked, is_free = _recording(busy=set(range(0, 70000)))

    with pytest.raises(cli.PortUnavailableError):
        cli.choose_port(8080, explicit=False, is_free=is_free)

    assert len(asked) <= cli.PORT_SEARCH_LIMIT


def test_the_walk_never_asks_about_a_port_that_cannot_exist() -> None:
    """Starting near the top must not run off the end of the port space."""
    asked, is_free = _recording(busy=set(range(0, 70000)))

    with pytest.raises(cli.PortUnavailableError):
        cli.choose_port(MAX_PORT - 3, explicit=False, is_free=is_free)

    assert [port for port in asked if port > MAX_PORT] == []


# --- 5. composition: what is bound is what is printed -----------------------


def test_a_busy_default_port_is_moved_off_and_the_new_one_is_printed(
    tmp_path: Path,
) -> None:
    """The whole stage in one run: the default is taken, so `rhi` lands beside
    it -- and the URL on stdout is the one that answers, not the one it wanted."""
    taken = free_port()
    listener = _holding(taken)
    running = start(
        (str(tmp_path), "--no-window", "--socket", str(tmp_path / "ingest.sock")),
        prelude=f"cli.DEFAULT_HTTP_PORT = {taken}\n",
    )
    try:
        url = running.wait_for_line(URL, STARTUP_TIMEOUT_SECONDS).group(0).rstrip(".,")
        assert urlparse(url).port != taken, f"{url} names the port already held"
        status, body = _fetch(url)

        assert status == 200, f"{url} was printed but answered {status}"
    finally:
        running.stop()
        listener.close()


def test_an_explicit_busy_port_refuses_to_start(tmp_path: Path) -> None:
    """No move, no traceback, and a status a launcher can read."""
    taken = free_port()
    listener = _holding(taken)
    try:
        completed = subprocess.run(
            entry_argv(
                (
                    str(tmp_path),
                    "--no-window",
                    "--port",
                    str(taken),
                    "--socket",
                    str(tmp_path / "ingest.sock"),
                )
            ),
            cwd=str(REPO_ROOT),
            env=clean_environment(),
            capture_output=True,
            text=True,
            timeout=REFUSAL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "`rhi --port <busy>` neither started nor refused: an explicit port "
            "that is taken must not be silently moved"
        )
    finally:
        listener.close()

    assert 0 < completed.returncode < 128, completed.stderr
    assert "Traceback" not in completed.stderr, completed.stderr
    assert str(taken) in completed.stderr, completed.stderr


def _fetch(url: str) -> tuple[int, str]:
    deadline = time.monotonic() + 20.0
    status, body = 0, ""
    while time.monotonic() < deadline:
        try:
            return get(url)
        except Exception:  # noqa: BLE001 - not accepting yet is not an answer
            time.sleep(0.2)
    return status, body
