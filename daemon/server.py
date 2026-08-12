#!/usr/bin/env python3
"""Aggregator daemon: fan-in from hooks, fan-out to browsers.

Two servers share one event loop:

  * **Ingest** -- a Unix domain socket (``GRAPHAGENTS_SOCKET``, default
    ``/tmp/graph-agents.sock``) that receives newline-delimited JSON hook
    payloads from :mod:`hooks.emit_event`. Each line is normalized here, which
    is also where the "already seen paths" set lives (single source of truth for
    add-vs-modify), so the hook stays a dumb, dependency-free forwarder.
  * **Broadcast** -- a WebSocket server (``GRAPHAGENTS_WS_PORT``, default 8765)
    that relays every normalized event to all connected browsers as JSON. A new
    client first receives a short replay of the most recent events so the graph
    never starts empty.

Optionally serves the built frontend from ``web/dist`` over plain HTTP
(``GRAPHAGENTS_HTTP_PORT``, default 8080) when that directory exists; otherwise
the Vite dev server is expected to host the front and connect to the WS port.

Unlike the hook, the daemon may use third-party dependencies (``websockets``).
Robustness rule: one misbehaving or disconnecting client must never take down
the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import signal
from collections import deque
from dataclasses import asdict
from pathlib import Path

from websockets.asyncio.server import ServerConnection, broadcast, serve

from graphagents.normalize import Event, normalize_event

LOGGER = logging.getLogger("graphagents.daemon")

DEFAULT_SOCKET_PATH = "/tmp/graph-agents.sock"
DEFAULT_WS_PORT = 8765
DEFAULT_HTTP_PORT = 8080
REPLAY_BUFFER_SIZE = 200

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = REPO_ROOT / "web" / "dist"


class EventHub:
    """Normalizes ingested payloads and fans them out to WebSocket clients.

    Owns the two pieces of shared state that must be consistent across all
    hooks and clients: the set of paths already seen (drives add-vs-modify) and
    the replay buffer of recent events.
    """

    def __init__(self, project_root: str, buffer_size: int = REPLAY_BUFFER_SIZE) -> None:
        self._project_root = project_root
        self._known_paths: set[str] = set()
        self._recent: deque[str] = deque(maxlen=buffer_size)
        self._clients: set[ServerConnection] = set()

    # -- WebSocket side ----------------------------------------------------

    async def register(self, websocket: ServerConnection) -> None:
        """Add a client and replay recent events so its graph is not empty."""
        self._clients.add(websocket)
        for message in list(self._recent):
            with contextlib.suppress(Exception):
                await websocket.send(message)

    def unregister(self, websocket: ServerConnection) -> None:
        self._clients.discard(websocket)

    # -- Ingest side -------------------------------------------------------

    def ingest_line(self, line: str) -> None:
        """Normalize one raw hook JSON line and broadcast the event, if any."""
        payload = self._safe_load(line)
        if payload is None:
            return

        event = normalize_event(
            payload,
            known_paths=self._known_paths,
            project_root=self._project_root,
        )
        if event is None:
            return

        self._remember_path(event)
        message = json.dumps(asdict(event), separators=(",", ":"))
        self._recent.append(message)
        broadcast(self._clients, message)

    def _remember_path(self, event: Event) -> None:
        # A deleted path may be re-added later; keep the set reflecting the
        # tree so a subsequent Write to the same path is an add, not a modify.
        if event.type == "D":
            self._known_paths.discard(event.path)
        else:
            self._known_paths.add(event.path)

    @staticmethod
    def _safe_load(line: str) -> dict | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None


async def _handle_ws_client(hub: EventHub, websocket: ServerConnection) -> None:
    """Serve one browser: replay history, then keep the connection alive."""
    await hub.register(websocket)
    try:
        # This front-to-back channel is broadcast-only; just wait for close and
        # ignore any inbound frames a client might send.
        async for _ in websocket:
            pass
    except Exception as exc:  # a broken client must not crash the server
        LOGGER.debug("ws client error: %s", exc)
    finally:
        hub.unregister(websocket)


async def _handle_ingest_client(
    hub: EventHub,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Read newline-delimited JSON events from one hook connection."""
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            hub.ingest_line(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        LOGGER.debug("ingest client error: %s", exc)
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _serve_static_http(port: int) -> asyncio.AbstractServer | None:
    """Serve ``web/dist`` over HTTP when it exists, else return ``None``."""
    if not WEB_DIST.is_dir():
        LOGGER.info(
            "web/dist not found; serving WebSocket only "
            "(let the Vite dev server host the front)."
        )
        return None

    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(WEB_DIST))
    httpd = ThreadingHTTPServer(("", port), handler)
    Thread(target=httpd.serve_forever, name="http-static", daemon=True).start()
    LOGGER.info("serving %s at http://localhost:%d", WEB_DIST, port)
    # Returned handle is informational; the thread owns the lifecycle.
    return None


async def run(
    socket_path: str,
    ws_port: int,
    http_port: int,
    project_root: str,
) -> None:
    hub = EventHub(project_root=project_root)

    if os.path.exists(socket_path):
        os.unlink(socket_path)

    ingest_server = await asyncio.start_unix_server(
        functools.partial(_handle_ingest_client, hub), path=socket_path
    )
    ws_server = await serve(
        functools.partial(_handle_ws_client, hub), host="", port=ws_port
    )
    await _serve_static_http(http_port)

    LOGGER.info("ingest on %s | websocket on :%d", socket_path, ws_port)

    stop = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(
                sig, lambda: stop.done() or stop.set_result(None)
            )

    async with ingest_server, ws_server:
        with contextlib.suppress(asyncio.CancelledError):
            await stop
    with contextlib.suppress(FileNotFoundError):
        os.unlink(socket_path)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GRAPHAGENTS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    socket_path = os.environ.get("GRAPHAGENTS_SOCKET", DEFAULT_SOCKET_PATH)
    ws_port = int(os.environ.get("GRAPHAGENTS_WS_PORT", DEFAULT_WS_PORT))
    http_port = int(os.environ.get("GRAPHAGENTS_HTTP_PORT", DEFAULT_HTTP_PORT))
    project_root = os.environ.get("GRAPHAGENTS_PROJECT_ROOT", os.getcwd())

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(socket_path, ws_port, http_port, project_root))


if __name__ == "__main__":
    main()
