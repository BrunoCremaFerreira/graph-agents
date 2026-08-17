"""Contract tests (RED) for `run()` telling its caller when it is actually up.

Motivation: `rhizome_graph/launch.py` has to print a URL that is true the moment
it is printed, and stage D has to open a window over a daemon that is already
serving. It learns both by **opening its own TCP connection to the HTTP port in
a loop** (`_announce_when_ready` -> `_accepts`), and that probe is wrong in two
separate ways.

  * **It makes an internal ordering a load-bearing contract nobody wrote down.**
    The probe is only a valid readiness test because `run()` happens to bind the
    ingest socket *before* the HTTP listener. Reorder those two lines -- a
    perfectly reasonable refactor, since nothing states the dependency -- and the
    URL is announced while hooks still have nowhere to connect. A caller that
    opens a window at that moment gets a page whose graph never receives an
    attributed event.
  * **It fabricates a client.** Every boot logs a `connection closed` with no
    matching open, because the probe connects and hangs up. That line is
    indistinguishable from a browser that failed, which is exactly the class of
    confusion this project has paid for before.

So readiness becomes something `run()` **states**, once, from inside, at the one
moment it knows to be true. `run(settings, ready=None)`: the parameter is
optional because `python -m daemon.server` has nobody to tell.

**What `ready` receives is the design decision in this file.** It is one
argument, and it carries exactly two things:

  * ``.url`` -- the page, spelled the way a browser accepts it. Not the host and
    the port for the caller to reassemble: the port may have moved (`choose_port`
    walks off a busy default) and the host may be a wildcard that nothing can be
    pointed at, and a launcher that prints or opens a *different* address from
    the one it serves is the failure `tests/test_rhi_start.py` was written
    around. One spelling, produced by the thing that did the binding.
  * ``.stop`` -- how to end this daemon. Stage D's window closing must resolve
    the *same* future SIGTERM resolves, rather than growing a second teardown
    path in `cli.py`; handing the caller that resolution is what makes one path
    possible. `tests/test_window_lifecycle.py` specifies its behaviour.

**And what it deliberately does NOT carry: the `Settings`, and therefore the
token.** The daemon injects the control token into the `index.html` it serves,
so a webview issuing `GET /` from loopback inherits it exactly as a browser
does. A readiness value that carried the whole configuration would put the
credential in the hands of every window backend for no purpose at all, which is
precisely the second-place-a-secret-lives that `rhizome_graph/window.py` is
forbidden from becoming (see `tests/test_window_backend.py`). Two fields, and
the URL is a bare origin with no query string and no fragment, so nothing can be
smuggled through it either.

The shape is asserted by attribute rather than by class, on purpose: whether it
is a frozen dataclass in `daemon/server.py` or a `NamedTuple` somewhere else is
not a property anybody depends on.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import socket
from pathlib import Path
from urllib.parse import urlparse

import pytest

import rhizome_graph.launch as launch
from daemon.server import IngestSocketInUseError, run
from daemon_probe import (
    STARTUP_TIMEOUT_SECONDS,
    accepts,
    cancel_and_wait,
    drive,
    scrub,
    settings_for,
    unix_socket_accepts,
)
from rhizome_graph.cli import reachable_host


class Recorder:
    """A readiness callback that remembers everything about its own call.

    It probes the two listeners *from inside the callback*, because the property
    is not "these were up at some point" but "these were up when the caller was
    told they were".
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.calls: list[tuple[tuple, dict]] = []
        self.port_accepted: list[bool] = []
        self.ingest_accepted: list[bool] = []

    def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))
        self.port_accepted.append(
            accepts(reachable_host(self.settings.host), self.settings.port)
        )
        self.ingest_accepted.append(unix_socket_accepts(self.settings.socket_path))

    @property
    def value(self):
        """The single argument the one call carried."""
        assert len(self.calls) == 1, f"ready was called {len(self.calls)} times"
        args, kwargs = self.calls[0]
        assert kwargs == {}, f"ready was called with keywords: {sorted(kwargs)}"
        assert len(args) == 1, f"ready was called with {len(args)} positional arguments"
        return args[0]


async def _serve_until_ready(settings, ready) -> asyncio.Task:
    """Start `run(settings, ready=ready)` and wait until `ready` has fired.

    A `run()` that dies on the way up is re-raised here rather than left to time
    out, so the failure names itself.
    """
    task = asyncio.create_task(run(settings, ready=ready))
    deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if ready.calls:
            return task
        if task.done():
            await task  # re-raises whatever brought it down
            raise RuntimeError("run() returned without ever announcing readiness")
        await asyncio.sleep(0.05)
    task.cancel()
    raise AssertionError("run() never called its readiness callback")


# --- 1. the signature -------------------------------------------------------


def test_run_accepts_a_readiness_callback() -> None:
    """A crisp failure for the signature itself, ahead of the serving tests."""
    parameters = list(inspect.signature(run).parameters)

    assert parameters == ["settings", "ready"]


def test_the_readiness_callback_is_optional() -> None:
    """`python -m daemon.server` has nobody to tell, and must not have to lie."""
    ready = inspect.signature(run).parameters["ready"]

    assert ready.default is None


# --- 2. when it fires -------------------------------------------------------


def test_readiness_is_announced_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window opened twice is two windows; a URL printed twice is noise."""
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)

    async def scenario():
        task = await _serve_until_ready(settings, ready)
        try:
            await asyncio.sleep(0.5)
            assert len(ready.calls) == 1
        finally:
            await cancel_and_wait(task)

    drive(scenario())


def test_the_http_listener_already_accepts_when_readiness_is_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL is true at the moment it is handed over, not a beat later."""
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)

    async def scenario():
        task = await _serve_until_ready(settings, ready)
        try:
            assert ready.port_accepted == [True]
        finally:
            await cancel_and_wait(task)

    drive(scenario())


def test_the_ingest_socket_already_accepts_when_readiness_is_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half the old TCP probe could only assume -- see the module docstring.

    A window opened over a daemon whose ingest socket is not yet bound draws a
    tree with nobody on camera, which is indistinguishable from hooks that were
    never installed.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)

    async def scenario():
        task = await _serve_until_ready(settings, ready)
        try:
            assert ready.ingest_accepted == [True]
        finally:
            await cancel_and_wait(task)

    drive(scenario())


# --- 3. when it does not fire ----------------------------------------------


def test_a_refused_start_announces_no_readiness_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window opened over a daemon that never started is the worst outcome.

    The refusal here is the one `rhi` is built to expect: another instance
    already owns the ingest socket.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)
    occupant = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    occupant.bind(settings.socket_path)
    occupant.listen(1)

    async def scenario():
        with pytest.raises(IngestSocketInUseError):
            await run(settings, ready=ready)
        assert ready.calls == []

    try:
        drive(scenario())
    finally:
        occupant.close()


def test_a_port_that_cannot_be_bound_announces_no_readiness_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other refusal: something else is already on the HTTP port.

    `choose_port` normally answers this ahead of `run()`, but the race is real
    -- a port free when it was probed can be taken before it is bound -- and the
    rule is the same either way: nothing is announced that is not true.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((settings.host, settings.port))
    listener.listen(1)

    async def scenario():
        with pytest.raises(OSError):
            await run(settings, ready=ready)
        assert ready.calls == []

    try:
        drive(scenario())
    finally:
        listener.close()


# --- 4. what it carries -----------------------------------------------------


def test_readiness_carries_the_page_url_and_a_way_to_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enough to open the right window, and nothing else -- see the docstring."""
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)

    async def scenario():
        task = await _serve_until_ready(settings, ready)
        try:
            value = ready.value
            assert isinstance(getattr(value, "url", None), str)
            assert callable(getattr(value, "stop", None))
        finally:
            await cancel_and_wait(task)

    drive(scenario())


def test_the_announced_url_names_the_port_that_was_actually_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-deriving it is what lets a launcher advertise the wrong address."""
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)

    async def scenario():
        task = await _serve_until_ready(settings, ready)
        try:
            parsed = urlparse(ready.value.url)
            assert parsed.scheme == "http"
            assert parsed.port == settings.port
            assert parsed.hostname == reachable_host(settings.host)
        finally:
            await cancel_and_wait(task)

    drive(scenario())


def test_the_announced_url_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Printed, opened and served must all be the same address."""
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)

    async def scenario():
        task = await _serve_until_ready(settings, ready)
        try:
            parsed = urlparse(ready.value.url)
            assert accepts(parsed.hostname or "", parsed.port or 0)
        finally:
            await cancel_and_wait(task)

    drive(scenario())


def test_the_announced_url_carries_no_query_string_and_no_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare origin, so no caller can smuggle a credential through it.

    The window needs no token -- it inherits the injected one by fetching the
    page -- and a URL with room for parameters is an invitation to hand it one
    anyway.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    ready = Recorder(settings)

    async def scenario():
        task = await _serve_until_ready(settings, ready)
        try:
            url = ready.value.url
            parsed = urlparse(url)
            assert parsed.query == "", url
            assert parsed.fragment == "", url
            assert "?" not in url and "#" not in url, url
        finally:
            await cancel_and_wait(task)

    drive(scenario())


def test_readiness_does_not_hand_over_the_control_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential lives in the page the daemon serves, and only there.

    A readiness value that carried the `Settings` would put the token in the
    hands of every window backend that will ever be written, for no purpose:
    a loopback `GET /` already inherits it.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path, token="ready-must-not-carry-this")
    ready = Recorder(settings)

    async def scenario():
        task = await _serve_until_ready(settings, ready)
        try:
            value = ready.value
            assert settings.token not in repr(value), repr(value)
            assert not hasattr(value, "token")
            assert not hasattr(value, "settings")
        finally:
            await cancel_and_wait(task)

    drive(scenario())


# --- 5. the launcher stops probing and starts listening ---------------------


def test_the_launcher_hands_its_own_callback_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`serve`'s job shrinks to forwarding: `run()` decides when, not `launch`."""
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    announced = object()
    seen: list[object] = []

    async def fake_run(settings_argument, ready=None):
        if ready is not None:
            ready(announced)

    monkeypatch.setattr(launch, "run", fake_run)

    launch.serve(settings, seen.append)

    assert seen == [announced]


def _attribute_names(source: str) -> set[str]:
    tree = ast.parse(source)
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_the_launcher_no_longer_opens_a_connection_of_its_own() -> None:
    """The phantom `connection closed` on every boot is the probe itself.

    A structural assertion, in the spirit of `tests/test_daemon_environment_
    boundary.py` and of the front end's "no shiki outside `highlight.ts`": the
    behavioural tests above prove readiness is announced today, but only this
    stops the poll from surviving beside it, still fabricating a client.
    """
    source = Path(launch.__file__).read_text(encoding="utf-8")

    assert "open_connection" not in _attribute_names(source), (
        "rhizome_graph/launch.py still connects to the HTTP port to find out "
        "whether it is up. Readiness is announced by run(); the probe logs a "
        "connection with no matching open on every single boot."
    )
