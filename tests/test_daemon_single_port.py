"""Contract tests (RED) for serving HTTP and WebSocket on ONE port.

Motivation: the front-end is reached through a forwarded port (SSH / VS Code
remote). Two ports meant the page loaded while the WebSocket silently failed,
leaving a black screen. Sharing one port makes a single forwarded port
sufficient, so remote setups work with no extra configuration.

These specify `daemon.server.start_server`, which must accept a normal HTTP GET
(serving the built front-end) and a WebSocket upgrade on the *same* listener.
Expected to FAIL until it is implemented. One failure reason per test.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest
from websockets.asyncio.client import connect

from daemon.server import EventHub, start_server

HOOK = {
    "session_id": "sess-xyz",
    "hook_event_name": "PostToolUse",
    "tool_name": "Write",
    "tool_input": {"file_path": "notes.md"},
}


def _static_site(tmp_path):
    """Build a minimal 'web/dist' lookalike and return its path."""
    (tmp_path / "index.html").write_text("<canvas id='stage'></canvas>", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("export const x = 1;\n", encoding="utf-8")
    return tmp_path


def _get(url: str) -> tuple[int, bytes, str]:
    """Blocking HTTP GET returning (status, body, content_type)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


async def _serve(tmp_path, project_root="/proj"):
    """Start the combined server on an ephemeral port; return (hub, server, port)."""
    hub = EventHub(project_root=project_root)
    server = await start_server(hub, host="127.0.0.1", port=0, static_root=_static_site(tmp_path))
    port = next(iter(server.sockets)).getsockname()[1]
    return hub, server, port


class TestHttpOnSharedPort:
    def test_serves_index_html_at_root(self, tmp_path):
        async def scenario():
            _, server, port = await _serve(tmp_path)
            async with server:
                status, body, ctype = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/")
                assert status == 200
                assert b"<canvas" in body
                assert "text/html" in ctype

        _run(scenario())

    def test_serves_nested_asset_with_javascript_content_type(self, tmp_path):
        async def scenario():
            _, server, port = await _serve(tmp_path)
            async with server:
                status, body, ctype = await asyncio.to_thread(
                    _get, f"http://127.0.0.1:{port}/assets/app.js"
                )
                assert status == 200
                assert b"export const x" in body
                assert "javascript" in ctype

        _run(scenario())

    def test_unknown_path_is_404(self, tmp_path):
        async def scenario():
            _, server, port = await _serve(tmp_path)
            async with server:
                status, _, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/nope.txt")
                assert status == 404

        _run(scenario())

    def test_path_traversal_is_refused(self, tmp_path):
        """A request escaping the static root must never read outside it."""

        async def scenario():
            _, server, port = await _serve(tmp_path)
            async with server:
                status, body, _ = await asyncio.to_thread(
                    _get, f"http://127.0.0.1:{port}/../../../../etc/passwd"
                )
                assert status in (400, 403, 404)
                assert b"root:" not in body

        _run(scenario())


class TestWebSocketOnSharedPort:
    def test_websocket_upgrade_on_the_same_port_receives_events(self, tmp_path):
        """The whole point: one port answers both HTTP and the WS upgrade."""

        async def scenario():
            hub, server, port = await _serve(tmp_path)
            async with server:
                async with connect(f"ws://127.0.0.1:{port}/ws") as ws:
                    hub.ingest_line(json.dumps(HOOK))
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    event = json.loads(raw)
                    assert event["path"] == "notes.md"
                    assert event["agent"] == "sess-xyz"
                    assert event["type"] == "A"

        _run(scenario())

    def test_new_client_gets_replay_of_recent_events(self, tmp_path):
        async def scenario():
            hub, server, port = await _serve(tmp_path)
            async with server:
                hub.ingest_line(json.dumps(HOOK))
                async with connect(f"ws://127.0.0.1:{port}/ws") as ws:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    assert json.loads(raw)["path"] == "notes.md"

        _run(scenario())


class TestWithoutStaticRoot:
    def test_websocket_still_works_when_no_static_root(self, tmp_path):
        """Vite dev-server mode: no dist to serve, but WS must still work."""

        async def scenario():
            hub = EventHub(project_root="/proj")
            server = await start_server(hub, host="127.0.0.1", port=0, static_root=None)
            port = next(iter(server.sockets)).getsockname()[1]
            async with server:
                async with connect(f"ws://127.0.0.1:{port}/ws") as ws:
                    hub.ingest_line(json.dumps(HOOK))
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    assert json.loads(raw)["path"] == "notes.md"

        _run(scenario())

    def test_http_reports_unavailable_when_no_static_root(self, tmp_path):
        async def scenario():
            hub = EventHub(project_root="/proj")
            server = await start_server(hub, host="127.0.0.1", port=0, static_root=None)
            port = next(iter(server.sockets)).getsockname()[1]
            async with server:
                status, _, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/")
                assert status in (404, 503)

        _run(scenario())
