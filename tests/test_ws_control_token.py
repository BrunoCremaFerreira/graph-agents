"""Contract tests (RED) for requiring the boot token on every command frame.

Motivation (both halves reproduced against a scratch daemon): `control_allowed`
decides who may drive the daemon from the peer's IP alone, and the peer's IP
lies.

  * A WebSocket handshake is not subject to the same-origin policy, so any page
    loaded in a browser on this host can open `ws://127.0.0.1:8080/ws` and send
    `setRoot` followed by `file`. The frames arrive from loopback, the gate
    passes them, and a page from anywhere on the web reads any file the daemon
    can reach.
  * Any loopback-side proxy erases the real peer: `web/vite.config.ts` binds
    `host: true` and proxies `/ws`, so a LAN connection reaches the daemon as
    127.0.0.1 and passes the same gate.

The token closes both. The daemon mints one at boot, injects it into the
`index.html` it serves, and requires it on every command. A cross-site page
cannot read it -- same-origin is what stops it fetching the page -- and a proxy
has none to forward.

It is an ADDITIONAL condition, never a replacement. `control_allowed` and
`RHIZOME_ALLOW_REMOTE_CONTROL` keep their meaning exactly; a command must pass
the address gate AND carry the token. Two tests below pin each direction of
that, because "the token replaced the IP check" is the tempting simplification
and it would re-open the LAN to anyone behind a proxy.

Broadcast and replay are untouched: this gates the inbound channel only.
The pure module itself is specified in `tests/test_token.py`.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from daemon.server import (
    EventHub,
    Session,
    _handle_ws_client,
    parse_command,
    start_server,
)

_ASSIGNMENT = re.compile(r'window\.__RHIZOME_TOKEN__\s*=\s*("(?:[^"\\]|\\.)*")')


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


def _frame(kind: str, path: str, token: object = None) -> str:
    """One command frame, with the token omitted entirely when it is `None`."""
    payload: dict = {"kind": kind, "path": path}
    if token is not None:
        payload["token"] = token
    return json.dumps(payload)


def _embedded_token(html: str) -> str:
    """What a browser would end up with in `window.__RHIZOME_TOKEN__`."""
    match = _ASSIGNMENT.search(html)
    assert match is not None, f"no assignment to the global in: {html!r}"
    return json.loads(match.group(1))


class _FakeClient:
    """Just enough of a connection: an address, a `send`, and frames to deliver.

    A copy of the helper in `tests/test_ws_commands.py`, deliberately: it mocks
    only the socket underneath the daemon, so what runs here is the real
    dispatch.
    """

    def __init__(self, *frames: str, host: str = "127.0.0.1") -> None:
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

    def frames(self) -> list[dict]:
        return [json.loads(message) for message in self.sent]

    def kinds(self) -> list[str]:
        return [frame.get("kind") for frame in self.frames()]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("top secret\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def session(monkeypatch: pytest.MonkeyPatch, project: Path) -> Session:
    monkeypatch.delenv("RHIZOME_ALLOW_REMOTE_CONTROL", raising=False)
    monkeypatch.delenv("RHIZOME_TOKEN", raising=False)
    return Session(str(project), str(project))


# --- 1. parse_command carries the token through ----------------------------

def test_the_token_is_carried_through_to_the_command():
    assert parse_command('{"kind":"file","path":"a.txt","token":"s3cret"}') == {
        "kind": "file",
        "path": "a.txt",
        "token": "s3cret",
    }


def test_a_frame_with_no_token_yields_an_empty_one():
    # Parsed, not refused: the frame is well-formed and the refusal belongs to
    # the gate, which owes the browser a reason it can show.
    assert parse_command('{"kind":"file","path":"a.txt"}') == {
        "kind": "file",
        "path": "a.txt",
        "token": "",
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"kind":"file","path":"a.txt","token":42}',
        '{"kind":"file","path":"a.txt","token":null}',
        '{"kind":"file","path":"a.txt","token":["s3cret"]}',
        '{"kind":"file","path":"a.txt","token":{"value":"s3cret"}}',
    ],
)
def test_a_token_that_is_not_a_string_becomes_an_empty_one(raw: str):
    # It reaches `hmac.compare_digest`, which raises on most of these. An empty
    # string is refused by `token_matches`, which is the outcome wanted anyway.
    command = parse_command(raw)

    assert command is not None and command["token"] == ""


def test_the_kind_and_the_path_are_unchanged_by_the_new_field():
    command = parse_command('{"kind":"setRoot","path":"  ~/pro  ","token":"s3cret"}')

    assert command is not None
    assert command["kind"] == "setRoot"
    assert command["path"] == "  ~/pro  "


# --- 2. the session mints its token at boot --------------------------------

def test_the_session_has_a_token(session: Session):
    assert session.token != ""


def test_two_daemons_do_not_share_a_token(
    monkeypatch: pytest.MonkeyPatch, project: Path
):
    monkeypatch.delenv("RHIZOME_TOKEN", raising=False)

    assert Session(str(project), str(project)).token != Session(
        str(project), str(project)
    ).token


def test_the_environment_may_pin_the_token(
    monkeypatch: pytest.MonkeyPatch, project: Path
):
    # So a wrapper script or a second tool can be told what the daemon expects.
    monkeypatch.setenv("RHIZOME_TOKEN", "chosen-by-hand")

    assert Session(str(project), str(project)).token == "chosen-by-hand"


# --- 3. the gate: a command needs the token --------------------------------

def test_a_loopback_command_carrying_the_token_is_served(session: Session):
    client = _FakeClient(_frame("file", "a.txt", session.token))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" in client.kinds()


def test_a_command_with_no_token_is_refused(session: Session):
    # The cross-site page: it reaches the socket, it looks local, and it has no
    # way of knowing the token.
    client = _FakeClient(_frame("file", "a.txt"))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


def test_a_command_with_the_wrong_token_is_refused(session: Session):
    client = _FakeClient(_frame("file", "a.txt", "guessed-it"))

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


def test_a_refused_command_is_answered_with_a_reason(session: Session):
    # Silence would read as a hung page. The existing `rootError` shape is
    # reused, so the bar already knows how to show it.
    client = _FakeClient(_frame("file", "a.txt"))

    _run(_handle_ws_client(session.hub, session, client))

    assert client.kinds() == ["rootError"]
    assert client.frames()[0]["path"] == "a.txt"


def test_the_refusal_says_it_was_the_token(session: Session):
    # Distinct from the address gate's wording: "remote control disabled" sends
    # whoever reads it hunting for a network problem that is not there.
    client = _FakeClient(_frame("file", "a.txt", "guessed-it"))

    _run(_handle_ws_client(session.hub, session, client))

    reason = client.frames()[0]["reason"]
    assert "token" in reason.lower()
    assert reason != "remote control disabled"


def test_a_set_root_without_the_token_does_not_repoint_the_daemon(
    session: Session, tmp_path: Path
):
    # The expensive half of the attack: repoint the graph at `/`, then read.
    elsewhere = tmp_path.parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    root_before = session.root
    client = _FakeClient(_frame("setRoot", str(elsewhere)))

    _run(_handle_ws_client(session.hub, session, client))

    assert session.root == root_before


def test_a_completion_without_the_token_is_refused(session: Session):
    # `complete` lists the host's directories, which is reconnaissance for the
    # `file` that follows; the gate covers all three kinds alike.
    client = _FakeClient(_frame("complete", str(session.root)))

    _run(_handle_ws_client(session.hub, session, client))

    assert "completion" not in client.kinds()


# --- 4. the token is added to the address gate, not substituted for it -----

def test_the_right_token_does_not_let_a_remote_peer_through(session: Session):
    # A proxy cannot forward the token, but a curious colleague reading it off a
    # shared screen can paste it. Loopback-only still holds.
    client = _FakeClient(_frame("file", "a.txt", session.token), host="192.168.1.50")

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


def test_a_wrong_token_is_refused_even_with_remote_control_opened_up(
    monkeypatch: pytest.MonkeyPatch, session: Session
):
    # `RHIZOME_ALLOW_REMOTE_CONTROL=1` opts out of the address check, not out of
    # authentication.
    monkeypatch.setenv("RHIZOME_ALLOW_REMOTE_CONTROL", "1")
    client = _FakeClient(_frame("file", "a.txt", "guessed-it"), host="192.168.1.50")

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" not in client.kinds()
    assert "top secret" not in "".join(client.sent)


def test_the_opted_in_remote_peer_still_works_with_the_token(
    monkeypatch: pytest.MonkeyPatch, session: Session
):
    monkeypatch.setenv("RHIZOME_ALLOW_REMOTE_CONTROL", "1")
    client = _FakeClient(_frame("file", "a.txt", session.token), host="192.168.1.50")

    _run(_handle_ws_client(session.hub, session, client))

    assert "fileView" in client.kinds()


# --- 5. the page is handed its own token over HTTP -------------------------

def _static_site(root: Path) -> Path:
    """A minimal `web/dist` lookalike."""
    site = root / "dist"
    assets = site / "assets"
    assets.mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html>\n<html>\n  <head>\n    <title>rhizome-graph</title>\n"
        "  </head>\n  <body><canvas id=\"stage\"></canvas></body>\n</html>\n",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("export const x = 1;\n", encoding="utf-8")
    return site


def _get(url: str) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


async def _serve(session: Session, site: Path):
    server = await start_server(
        session.hub, host="127.0.0.1", port=0, static_root=site, session=session
    )
    return server, next(iter(server.sockets)).getsockname()[1]


def test_the_served_page_carries_the_daemons_token(session: Session, tmp_path: Path):
    site = _static_site(tmp_path)

    async def scenario():
        server, port = await _serve(session, site)
        async with server:
            _, body, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/")
            assert _embedded_token(body.decode()) == session.token

    _run(scenario())


def test_the_injected_page_declares_its_real_length(session: Session, tmp_path: Path):
    # A Content-Length left over from the file on disk truncates the page in the
    # browser, which is a black screen with no error in the console.
    site = _static_site(tmp_path)

    async def scenario():
        server, port = await _serve(session, site)
        async with server:
            _, body, headers = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/")
            assert int(headers["Content-Length"]) == len(body)

    _run(scenario())


def test_the_page_is_still_the_page_it_was_serving(session: Session, tmp_path: Path):
    site = _static_site(tmp_path)

    async def scenario():
        server, port = await _serve(session, site)
        async with server:
            status, body, headers = await asyncio.to_thread(
                _get, f"http://127.0.0.1:{port}/"
            )
            assert status == 200
            assert b"<canvas id=\"stage\"></canvas>" in body
            assert "text/html" in headers.get("Content-Type", "")

    _run(scenario())


def test_every_other_asset_is_served_byte_for_byte(session: Session, tmp_path: Path):
    # Injecting into a bundle would corrupt it, and its sourcemap with it.
    site = _static_site(tmp_path)
    on_disk = (site / "assets" / "app.js").read_bytes()

    async def scenario():
        server, port = await _serve(session, site)
        async with server:
            _, body, _ = await asyncio.to_thread(
                _get, f"http://127.0.0.1:{port}/assets/app.js"
            )
            assert body == on_disk

    _run(scenario())


def test_a_daemon_with_no_session_still_serves_the_page(tmp_path: Path):
    # The dev-server arrangement: no session to own a token, and the page must
    # still load rather than 500 on a missing one.
    site = _static_site(tmp_path)

    async def scenario():
        hub = EventHub(project_root=str(tmp_path))
        server = await start_server(
            hub, host="127.0.0.1", port=0, static_root=site, session=None
        )
        port = next(iter(server.sockets)).getsockname()[1]
        async with server:
            status, body, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/")
            assert status == 200
            assert b"<canvas id=\"stage\"></canvas>" in body

    _run(scenario())
