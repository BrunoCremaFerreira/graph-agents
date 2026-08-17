"""Contract tests (RED) for the address the daemon binds being a decision.

Motivation: `run()` builds its listener with `host=""` -- every interface, on
every network the machine is attached to. For a repository you clone and start
deliberately, that is a considered choice: it is how a colleague on the LAN, or a
container's gateway, reaches a graph you meant to share.

For a command installed on `$PATH` and started casually in whatever directory
you happen to be in, it is a perimeter nobody asked for. The daemon injects its
control token into the `index.html` it serves, and it serves that page to
whoever asks: on a LAN-bound listener, any peer that can reach the port can
fetch the token. The loopback gate still refuses that peer's *commands*, so this
is defence in depth working exactly as designed and not a hole -- but the reach
is wider than the product needs, and the documented remote workflow does not
need it at all: `ssh -L` and VS Code port forwarding both arrive from loopback
and are unaffected by binding loopback only.

So the address stops being a literal in the middle of `run()` and becomes
`Settings.host`, with `127.0.0.1` as the default for `rhi`. **Nothing here pins
what `python -m daemon.server` defaults to**: whether that entry point's `host=""`
moves too is a security judgement reserved for `security-auditor`, and these
tests are written so that either answer keeps them green -- every one of them
sets `host` explicitly.

The tests come in two strengths, and the difference is stated rather than
implied:

  * Parts 1 and 2 are real network assertions: a second, non-loopback local
    address is used to prove that a loopback-bound daemon refuses it at the TCP
    level, and that the same daemon bound to `0.0.0.0` accepts it. The second is
    not decoration -- without it, "connection refused" is equally consistent with
    a daemon that never started.
  * Part 3 is a WEAKER substitute for machines with no second local address:
    it captures the value handed to `start_server` through an injected fake. It
    proves the value travels; it proves nothing about what the kernel then does
    with it. It is not a network test and must not be read as one.

Style: Arrange-Act-Assert, one property per test.
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

import daemon.server as server
from daemon.server import run
from rhizome_graph.cli import build_parser, settings_from

STARTUP_TIMEOUT_SECONDS = 20.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _outward_address() -> str | None:
    """A non-loopback address of this machine, or `None` if it has none.

    The UDP socket is never sent on: connecting a datagram socket only asks the
    routing table which local address would be used, which is exactly the
    address a peer on that network would reach us at.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, routed nowhere
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


def _accepts(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(1.0)
        return client.connect_ex((host, port)) == 0


def _settings(root: Path, host: str, port: int):
    return dataclasses.replace(
        settings_from(build_parser().parse_args([str(root)]), {}, str(root)),
        host=host,
        port=port,
        socket_path=str(root / "ingest.sock"),
    )


async def _serve(settings):
    """Start `run(settings)` and wait until loopback answers on its port."""
    task = asyncio.create_task(run(settings))
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if task.done():
            await task
            raise RuntimeError("run() returned before it served anything")
        if _accepts("127.0.0.1", settings.port):
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


@pytest.fixture()
def outward() -> str:
    address = _outward_address()
    if address is None:
        pytest.skip("this machine has no non-loopback address to test against")
    return address


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [key for key in os.environ if key.startswith("RHIZOME_")]:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


# --- 1. a loopback-bound daemon is not on the network ----------------------


def test_a_loopback_daemon_refuses_a_connection_to_the_machines_own_lan_address(
    project: Path, outward: str
) -> None:
    """The perimeter, measured at the TCP level rather than argued about."""
    settings = _settings(project, "127.0.0.1", _free_port())

    async def scenario():
        task = await _serve(settings)
        try:
            assert not _accepts(outward, settings.port), (
                f"a daemon bound to 127.0.0.1 accepted a connection on "
                f"{outward}:{settings.port}"
            )
        finally:
            await _shutdown(task)

    _run(scenario())


def test_a_loopback_daemon_still_answers_on_loopback(project: Path) -> None:
    """The documented SSH and VS Code forwards arrive here, and must keep working."""
    settings = _settings(project, "127.0.0.1", _free_port())

    async def scenario():
        task = await _serve(settings)
        try:
            assert _accepts("127.0.0.1", settings.port)
        finally:
            await _shutdown(task)

    _run(scenario())


# --- 2. the control: the harness can tell the two cases apart --------------


def test_a_daemon_bound_to_every_interface_does_answer_on_that_address(
    project: Path, outward: str
) -> None:
    """Without this, "refused" above would also pass for a daemon that never ran.

    It doubles as the guarantee that binding widely is still *possible*: the
    address became a setting, not a prohibition.
    """
    settings = _settings(project, "0.0.0.0", _free_port())

    async def scenario():
        task = await _serve(settings)
        try:
            assert _accepts(outward, settings.port), (
                f"a daemon bound to 0.0.0.0 refused {outward}:{settings.port}, "
                "so this machine cannot distinguish the two bindings"
            )
        finally:
            await _shutdown(task)

    _run(scenario())


# --- 3. the weaker substitute ----------------------------------------------


def test_the_host_on_the_settings_is_the_host_handed_to_the_listener(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WEAKER than the two above, and only a stand-in where they cannot run.

    It records the argument `run()` passes to `start_server` through an injected
    fake. That is a claim about one function call, not about a socket: a daemon
    that passed the right host and then bound something else would satisfy it.
    Kept because it runs on a machine with no second address, and because it
    names the exact seam that must carry the value.
    """
    recorded: dict[str, object] = {}
    real_start_server = server.start_server

    async def spy(hub, host="", port=server.DEFAULT_HTTP_PORT, **kwargs):
        recorded["host"] = host
        return await real_start_server(hub, host=host, port=port, **kwargs)

    monkeypatch.setattr(server, "start_server", spy)
    settings = _settings(project, "127.0.0.1", _free_port())

    async def scenario():
        task = await _serve(settings)
        try:
            assert recorded["host"] == "127.0.0.1"
        finally:
            await _shutdown(task)

    _run(scenario())
