"""Contract tests (RED) for the control gate once its two inputs are carried.

Motivation: the gate on inbound commands has two conditions, and both currently
come out of the air. `Session.__init__` mints or reads its token with
`token_from_env(os.environ)`, and `_handle_ws_client` asks
`_allow_remote_control()`, which reads `RHIZOME_ALLOW_REMOTE_CONTROL` at the
moment of the frame. Moving both onto the `Settings` that configures the daemon
is a refactor of *where the values come from* -- and a refactor of an
authorization check is precisely the kind that quietly turns a conjunction into
a disjunction, or an unset value into a permissive one.

So the properties `tests/test_ws_control_token.py` pins for the environment-fed
gate are re-pinned here for the carried one, unchanged in meaning:

  * the empty token is refused, always, before any comparison;
  * a *right* token does not let a remote peer through (the address gate is not
    replaced by the token);
  * a *wrong* token is refused even with remote control opened up (the token is
    not replaced by the address gate).

Everything fails closed: a `Session` built with no token at all must refuse
every command rather than accept every tokenless one.

The seam pinned is `Session(project_root, home, token=..., allow_remote=...)`,
which is what `run()` fills from its `Settings` -- the two values travel together
because they are one decision made of two conditions, and splitting them across
two mechanisms is how one of them gets forgotten. The existing two-argument
construction keeps working, because `tests/test_ws_commands.py`,
`tests/test_hub_status.py` and `tests/test_root_switch.py` all use it.

Style: Arrange-Act-Assert, one refusal reason per test.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from daemon.server import Session, _handle_ws_client

LOOPBACK = "127.0.0.1"
REMOTE = "192.168.1.50"


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


def _frame(kind: str, path: str, token: object = None) -> str:
    payload: dict = {"kind": kind, "path": path}
    if token is not None:
        payload["token"] = token
    return json.dumps(payload)


class _FakeClient:
    """Just enough of a connection: an address, a `send`, and frames to deliver.

    The same helper as in `tests/test_ws_commands.py` and
    `tests/test_ws_control_token.py`, on purpose: it fakes the socket underneath
    the daemon and nothing above it, so what runs here is the real dispatch.
    """

    def __init__(self, *frames: str, host: str = LOOPBACK) -> None:
        self.remote_address = (host, 54321)
        self.sent: list[str] = []
        self._inbound = list(frames)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self):
        async def iterator():
            for frame in self._inbound:
                yield frame

        return iterator()

    def kinds(self) -> list[str]:
        return [json.loads(message).get("kind") for message in self.sent]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("top secret\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _no_ambient_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may be decided by a variable left in this process."""
    monkeypatch.delenv("RHIZOME_ALLOW_REMOTE_CONTROL", raising=False)
    monkeypatch.delenv("RHIZOME_TOKEN", raising=False)


# --- 1. the carried values are the ones the gate uses ----------------------


def test_a_session_uses_the_token_it_was_given(project: Path) -> None:
    session = Session(str(project), str(project), token="carried-token")
    client = _FakeClient(_frame("file", "a.txt", "carried-token"))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" in client.kinds()


def test_a_session_ignores_the_environments_token_when_it_was_given_one(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration beats ambience, and the ambient value must not be a way in."""
    monkeypatch.setenv("RHIZOME_TOKEN", "from-the-air")
    session = Session(str(project), str(project), token="carried-token")
    client = _FakeClient(_frame("file", "a.txt", "from-the-air"))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


def test_the_two_argument_session_still_works(project: Path) -> None:
    """The older construction is used by three existing test modules."""
    session = Session(str(project), str(project))
    client = _FakeClient(_frame("file", "a.txt", session.token))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" in client.kinds()


# --- 2. fail closed ---------------------------------------------------------


def test_a_session_with_an_empty_token_refuses_every_command(project: Path) -> None:
    """A daemon that failed to get a token refuses all, never accepts all."""
    session = Session(str(project), str(project), token="")
    client = _FakeClient(_frame("file", "a.txt", ""))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


def test_an_empty_token_is_refused_even_from_loopback_with_remote_control_open(
    project: Path,
) -> None:
    """Neither gate is a way around the other, and empty is not a credential."""
    session = Session(str(project), str(project), token="", allow_remote=True)
    client = _FakeClient(_frame("file", "a.txt", ""))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()


def test_a_command_with_no_token_at_all_is_refused(project: Path) -> None:
    """The cross-site page: it reaches the socket and looks local."""
    session = Session(str(project), str(project), token="carried-token")
    client = _FakeClient(_frame("file", "a.txt"))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


# --- 3. two conditions, never one ------------------------------------------


def test_the_right_token_does_not_let_a_remote_peer_through(project: Path) -> None:
    """A token read off a shared screen is not an address."""
    session = Session(str(project), str(project), token="carried-token")
    client = _FakeClient(_frame("file", "a.txt", "carried-token"), host=REMOTE)

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


def test_a_wrong_token_is_refused_even_with_remote_control_opened_up(
    project: Path,
) -> None:
    """`allow_remote` opts out of the address check, not out of authentication."""
    session = Session(
        str(project), str(project), token="carried-token", allow_remote=True
    )
    client = _FakeClient(_frame("file", "a.txt", "guessed-it"), host=REMOTE)

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


def test_the_opted_in_remote_peer_still_works_with_the_token(project: Path) -> None:
    """The deliberate opt-in keeps its meaning: both conditions met, served."""
    session = Session(
        str(project), str(project), token="carried-token", allow_remote=True
    )
    client = _FakeClient(_frame("file", "a.txt", "carried-token"), host=REMOTE)

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" in client.kinds()


def test_remote_control_is_closed_unless_the_settings_opened_it(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default is refusal, and a stray variable must not be able to open it.

    The mirror of the token test above: with the flag carried, the environment
    is no longer consulted for it, so a `RHIZOME_ALLOW_REMOTE_CONTROL` left in a
    shell that launched the daemon through `rhi` cannot widen a gate the caller
    configured shut.
    """
    monkeypatch.setenv("RHIZOME_ALLOW_REMOTE_CONTROL", "1")
    session = Session(
        str(project), str(project), token="carried-token", allow_remote=False
    )
    client = _FakeClient(_frame("file", "a.txt", "carried-token"), host=REMOTE)

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)
