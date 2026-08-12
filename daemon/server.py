#!/usr/bin/env python3
"""Aggregator daemon: fan-in from hooks, fan-out to browsers.

Two servers share one event loop:

  * **Ingest** -- a Unix domain socket (``GRAPHAGENTS_SOCKET``, default
    ``/tmp/graph-agents.sock``) that receives newline-delimited JSON hook
    payloads from :mod:`hooks.emit_event`. Each line is normalized here, which
    is also where the "already seen paths" set lives (single source of truth for
    add-vs-modify), so the hook stays a dumb, dependency-free forwarder.
  * **Broadcast** -- a WebSocket at ``/ws`` relaying every normalized event to
    all connected browsers as JSON. A new client first receives a short replay
    of the most recent events so the graph never starts empty.

Both the WebSocket and the built frontend in ``web/dist`` are served from a
*single* port (``GRAPHAGENTS_HTTP_PORT``, default 8080): a request arrives as a
WebSocket upgrade or as a plain GET, and one listener answers both. That means a
remote viewer (SSH or VS Code port forwarding) needs exactly one forwarded port,
and the page derives its socket URL from the origin it was loaded from -- a
separate WS port would resolve to the *viewer's* machine and never connect.
When ``web/dist`` is absent the Vite dev server hosts the front and proxies
``/ws`` here.

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
import mimetypes
import os
import signal
import urllib.parse
from collections import deque
from dataclasses import asdict
from pathlib import Path

from websockets.asyncio.server import Server, ServerConnection, broadcast, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from graphagents.normalize import Event, normalize_event

LOGGER = logging.getLogger("graphagents.daemon")

DEFAULT_SOCKET_PATH = "/tmp/graph-agents.sock"
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


def _resolve_static_file(static_root: Path, raw_path: str) -> Path | None:
    """Map a request path to a file inside ``static_root``, or ``None``.

    Refuses anything resolving outside the root, so a crafted path such as
    ``/../../etc/passwd`` can never escape the served directory.
    """
    path = urllib.parse.unquote(urllib.parse.urlsplit(raw_path).path)
    candidate = (static_root / path.lstrip("/")).resolve()
    root = static_root.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if candidate.is_file() else None


def _http_response(status: int, body: bytes, content_type: str) -> Response:
    reasons = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}
    headers = Headers(
        {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            # The page must never be cached stale against a rebuilt bundle.
            "Cache-Control": "no-cache",
        }
    )
    return Response(status, reasons.get(status, "Error"), headers, body)


def _process_request(
    static_root: Path | None,
    connection: ServerConnection,
    request: Request,
) -> Response | None:
    """Answer plain HTTP; return ``None`` to let a WebSocket upgrade through.

    This is what puts both protocols on one port: the browser loads the page
    and opens its WebSocket over the same origin, so a single forwarded port is
    enough for remote (SSH / VS Code) setups.
    """
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None

    if static_root is None:
        return _http_response(
            503,
            b"web/dist not built. Run: cd web && npm run build\n",
            "text/plain; charset=utf-8",
        )

    target = _resolve_static_file(static_root, request.path)
    if target is None:
        return _http_response(404, b"not found\n", "text/plain; charset=utf-8")

    try:
        body = target.read_bytes()
    except OSError:
        return _http_response(404, b"not found\n", "text/plain; charset=utf-8")

    content_type, _ = mimetypes.guess_type(target.name)
    return _http_response(200, body, content_type or "application/octet-stream")


async def start_server(
    hub: EventHub,
    host: str = "",
    port: int = DEFAULT_HTTP_PORT,
    static_root: Path | None = None,
) -> Server:
    """Start one listener answering both HTTP and WebSocket traffic."""
    return await serve(
        functools.partial(_handle_ws_client, hub),
        host=host,
        port=port,
        process_request=functools.partial(_process_request, static_root),
    )


async def run(
    socket_path: str,
    http_port: int,
    project_root: str,
) -> None:
    hub = EventHub(project_root=project_root)

    if os.path.exists(socket_path):
        os.unlink(socket_path)

    ingest_server = await asyncio.start_unix_server(
        functools.partial(_handle_ingest_client, hub), path=socket_path
    )

    static_root: Path | None = WEB_DIST if WEB_DIST.is_dir() else None
    if static_root is None:
        LOGGER.info(
            "web/dist not found; serving WebSocket only "
            "(let the Vite dev server host the front)."
        )
    else:
        LOGGER.info("serving %s at http://localhost:%d", static_root, http_port)

    ws_server = await start_server(hub, host="", port=http_port, static_root=static_root)

    LOGGER.info(
        "ingest on %s | http + websocket on :%d", socket_path, http_port
    )

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
    http_port = int(os.environ.get("GRAPHAGENTS_HTTP_PORT", DEFAULT_HTTP_PORT))
    project_root = os.environ.get("GRAPHAGENTS_PROJECT_ROOT", os.getcwd())

    if "GRAPHAGENTS_WS_PORT" in os.environ:
        LOGGER.warning(
            "GRAPHAGENTS_WS_PORT is obsolete and ignored: the WebSocket now "
            "shares the HTTP port (GRAPHAGENTS_HTTP_PORT=%d).",
            http_port,
        )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(socket_path, http_port, project_root))


if __name__ == "__main__":
    main()
