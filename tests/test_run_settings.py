"""Contract tests (RED) for run() being configured by a Settings, not by the air.

Motivation: `run()` today is configured by three scalars plus four ambient
reads it performs itself or delegates -- `_status_poll_interval()`,
`_allow_remote_control()`, `Session.__init__`'s token,
`default_web_dist(os.environ)` -- and two decisions it makes alone: the static
root and `host=""`. Nothing describes the resulting instance, so nothing can
start a second one differently in the same process, and every test of a
configured behaviour has to reach for `monkeypatch.setenv`.

The load-bearing test in this file is the first one, and what makes it load
bearing is what is *missing* from it: **`os.environ` is stripped of every
`RHIZOME_*` variable before `run()` is called, and the `Settings` is built from
an empty environ mapping.** If the daemon still serves the right page, on the
right port, from the right directory, then the ambient read is genuinely gone --
not merely shadowed by a value that happens to agree with it. An empty
environment IS the assertion.

The composition test in part 2 reads the opposite way on purpose, and the pair
is not a contradiction: `RHIZOME_WEB_DIST` remains a perfectly legitimate
*source* for that setting -- it simply has to travel through `settings_from` into
a `Settings` field, instead of being read from the air at the moment the
listener is built. Part 1 says the air is not consulted; part 2 says the
variable still arrives. Both have to hold, and only together do they describe
the seam.

`Settings` values are built with `dataclasses.replace` over a `settings_from`
answer rather than by calling the constructor with every field: these tests care
about four fields, and a fifth added later must not have to be spelled here.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import daemon.server as server
from daemon.server import run
from rhizome_graph.cli import build_parser, settings_from

#: How long the daemon is given to open its listener. Generous: it captions the
#: HUD, asks for the working tree's status and seeds the (empty) project root
#: before anything accepts a connection.
STARTUP_TIMEOUT_SECONDS = 20.0

#: What the served page is recognised by. Not `index.html`'s name -- the daemon
#: injects its token into the HTML on the way out, so the assertion has to be
#: about content that survives the injection.
MARKER = "<canvas id=\"stage\"></canvas>"

_ASSIGNMENT = re.compile(r'window\.__RHIZOME_TOKEN__\s*=\s*("(?:[^"\\]|\\.)*")')


def _free_port() -> int:
    """An ephemeral port, released before the daemon binds it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _site(root: Path) -> Path:
    """A minimal `web/dist` lookalike, distinguishable from the real one."""
    site = root / "dist"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(
        "<!doctype html>\n<html>\n  <head>\n    <title>rhizome-graph</title>\n"
        f"  </head>\n  <body>{MARKER}</body>\n</html>\n",
        encoding="utf-8",
    )
    return site


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _scrub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every `RHIZOME_*` variable from this process's environment.

    Not a tidy-up: this is the experiment. Anything `run()` still needs from the
    environment is now absent, so a daemon that serves correctly is one whose
    configuration arrived through its argument.
    """
    for name in [key for key in os.environ if key.startswith("RHIZOME_")]:
        monkeypatch.delenv(name, raising=False)


def _base_settings(root: Path, environ: dict | None = None):
    """A `Settings` for `root`, from the CLI's own parser and builder."""
    return settings_from(build_parser().parse_args([str(root)]), environ or {}, str(root))


async def _serve(settings):
    """Start `run(settings)` and wait until its port answers.

    Returns the task, which the caller cancels. A `run()` that dies on the way up
    is re-raised here rather than left to time out, so the failure names itself.
    """
    task = asyncio.create_task(run(settings))
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if task.done():
            await task  # re-raises whatever brought it down
            raise RuntimeError("run() returned before it served anything")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            if client.connect_ex(("127.0.0.1", settings.port)) == 0:
                return task
        await asyncio.sleep(0.05)
    task.cancel()
    raise AssertionError(f"nothing accepted a connection on :{settings.port}")


async def _shutdown(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=STARTUP_TIMEOUT_SECONDS)


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


# --- 1. the ambient read is gone -------------------------------------------


def test_the_daemon_serves_the_page_its_settings_named_with_no_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty environment is the assertion; the page is only the evidence."""
    _scrub(monkeypatch)
    site = _site(tmp_path)
    settings = dataclasses.replace(
        _base_settings(tmp_path),
        host="127.0.0.1",
        port=_free_port(),
        socket_path=str(tmp_path / "ingest.sock"),
        web_dist=str(site),
    )

    async def scenario():
        task = await _serve(settings)
        try:
            status, body = await asyncio.to_thread(
                _get, f"http://127.0.0.1:{settings.port}/"
            )
            assert status == 200
            assert MARKER.encode() in body
        finally:
            await _shutdown(task)

    _run(scenario())


def test_the_ingest_socket_is_the_one_the_settings_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RHIZOME_SOCKET` is absent, so a listening socket here came from Settings."""
    _scrub(monkeypatch)
    ingest = tmp_path / "ingest.sock"
    settings = dataclasses.replace(
        _base_settings(tmp_path),
        host="127.0.0.1",
        port=_free_port(),
        socket_path=str(ingest),
        web_dist=str(_site(tmp_path)),
    )

    async def scenario():
        task = await _serve(settings)
        try:
            assert ingest.exists(), f"no ingest socket at {ingest}"
        finally:
            await _shutdown(task)

    _run(scenario())


def test_the_page_carries_the_token_the_settings_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token is configuration now, and the served page must prove it arrived.

    With `RHIZOME_TOKEN` scrubbed, a daemon minting its own would hand the page a
    secret the caller does not know -- and `rhi`, which has to tell a browser or a
    probe what to send, would be talking about a token nobody honours.
    """
    _scrub(monkeypatch)
    settings = dataclasses.replace(
        _base_settings(tmp_path),
        host="127.0.0.1",
        port=_free_port(),
        socket_path=str(tmp_path / "ingest.sock"),
        web_dist=str(_site(tmp_path)),
        token="settings-carried-token",
    )

    async def scenario():
        task = await _serve(settings)
        try:
            _, body = await asyncio.to_thread(
                _get, f"http://127.0.0.1:{settings.port}/"
            )
            match = _ASSIGNMENT.search(body.decode())
            assert match is not None, "the page carries no token at all"
            assert json.loads(match.group(1)) == "settings-carried-token"
        finally:
            await _shutdown(task)

    _run(scenario())


def test_run_is_configured_by_one_settings_argument() -> None:
    """A crisp failure for the signature itself, ahead of the serving tests.

    `ready` joined it later and is not configuration: it is how `run()` tells a
    caller it is up, specified in `tests/test_ready_callback.py`. What this
    still pins is that everything the daemon *is* arrives in one value -- no
    second scalar has been smuggled in beside it.
    """
    import inspect

    parameters = list(inspect.signature(run).parameters)

    assert parameters == ["settings", "ready"]


# --- 2. the environment is still a legitimate source of that configuration --


def test_the_web_dist_from_the_environment_reaches_the_page_that_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composition nothing covers: `RHIZOME_WEB_DIST` -> Settings -> served.

    The opposite reading from part 1, and deliberately so. `tests/test_assets.py`
    pins `web_dist_candidates` and `find_web_dist` in isolation; every daemon test
    passes `static_root=` straight into `start_server`. Between those two lies the
    whole subject of this stage -- the variable becoming a field, and the field
    becoming what a browser downloads -- and it is exercised by nothing.

    The directory is a `tmp_path` one, so serving the checkout's real `web/dist`
    by accident cannot pass this.
    """
    _scrub(monkeypatch)
    site = _site(tmp_path)
    settings = dataclasses.replace(
        _base_settings(tmp_path, {"RHIZOME_WEB_DIST": str(site)}),
        host="127.0.0.1",
        port=_free_port(),
        socket_path=str(tmp_path / "ingest.sock"),
    )
    assert settings.web_dist == str(site), "settings_from dropped the override"

    async def scenario():
        task = await _serve(settings)
        try:
            status, body = await asyncio.to_thread(
                _get, f"http://127.0.0.1:{settings.port}/"
            )
            assert status == 200
            assert MARKER.encode() in body
        finally:
            await _shutdown(task)

    _run(scenario())


# --- 3. the status poll, configured by the same value ----------------------
#
# `tests/test_hub_status.py` pins these two against `RHIZOME_STATUS_INTERVAL`
# and the three-scalar `run()`. Both are properties of `run()` rather than of
# the variable, so they are restated here against the field that now carries it
# -- otherwise the escape hatch survives as a number nobody checks reaches the
# loop.


def _recording_poll_status(calls: list[float]):
    async def fake(self, interval: float = server.STATUS_POLL_INTERVAL_SECONDS):
        calls.append(interval)
        await asyncio.sleep(3600)

    return fake


def test_a_status_interval_of_zero_starts_no_poll_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of `0`: not a loop that wakes up and skips -- no loop."""
    _scrub(monkeypatch)
    calls: list[float] = []
    monkeypatch.setattr(server.Session, "poll_status", _recording_poll_status(calls))
    settings = dataclasses.replace(
        _base_settings(tmp_path),
        host="127.0.0.1",
        port=_free_port(),
        socket_path=str(tmp_path / "ingest.sock"),
        web_dist=str(_site(tmp_path)),
        status_interval=0.0,
    )

    async def scenario():
        task = await _serve(settings)
        try:
            assert calls == []
        finally:
            await _shutdown(task)

    _run(scenario())


def test_the_status_poll_runs_at_the_interval_the_settings_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch has to reach the loop, not merely be parsed correctly."""
    _scrub(monkeypatch)
    calls: list[float] = []
    monkeypatch.setattr(server.Session, "poll_status", _recording_poll_status(calls))
    settings = dataclasses.replace(
        _base_settings(tmp_path),
        host="127.0.0.1",
        port=_free_port(),
        socket_path=str(tmp_path / "ingest.sock"),
        web_dist=str(_site(tmp_path)),
        status_interval=7.5,
    )

    async def scenario():
        task = await _serve(settings)
        try:
            assert calls == [7.5]
        finally:
            await _shutdown(task)

    _run(scenario())
