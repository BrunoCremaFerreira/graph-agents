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

The WebSocket is no longer output-only. The observed root used to be frozen at
boot by ``GRAPHAGENTS_PROJECT_ROOT``, so watching a second project meant killing
the daemon; the page can now retype it, which means frames also travel *inbound*:
``{"kind":"complete"}`` (answer a ``Tab``, because only the daemon can read the
daemon's disk), ``{"kind":"setRoot"}`` (observe another project) and
``{"kind":"file"}`` (what is *inside* the node that was clicked: its diff, its
text, or a hex dump). All are answered to that client alone -- one viewer
pressing ``Tab`` or opening a panel must not repaint the screen of everybody else
watching the same daemon.

Inbound commands are **loopback-only by default** (:func:`control_allowed`):
``setRoot`` makes the daemon walk an arbitrary directory and re-seed from it, and
``file`` hands over file *contents*, so exempting the latter because "it only
reads" would turn an open port into a file server for the whole project. An
SSH tunnel and VS Code port forwarding both arrive as loopback, so the ordinary
remote setup keeps working untouched; ``GRAPHAGENTS_ALLOW_REMOTE_CONTROL=1``
deliberately opens it to the rest of the network.

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
import ipaddress
import json
import logging
import mimetypes
import os
import signal
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path

from websockets.asyncio.server import Server, ServerConnection, broadcast, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from graphagents.file_view import file_view
from graphagents.normalize import (
    Event,
    actor_of,
    fs_event,
    normalize_event,
    seed_event,
)
from graphagents.paths import complete_dir, resolve_root
from graphagents.repo import display_root, read_branch
from graphagents.tree import scan_tree

LOGGER = logging.getLogger("graphagents.daemon")

DEFAULT_SOCKET_PATH = "/tmp/graph-agents.sock"
DEFAULT_HTTP_PORT = 8080
REPLAY_BUFFER_SIZE = 200

#: How long after a hook fires its agent still owns the changes the watcher
#: reports. Long enough to cover a slow `cp -r`, short enough that a manual edit
#: minutes later stays anonymous.
ATTRIBUTION_WINDOW_SECONDS = 5.0

#: How long a hook-reported path suppresses the watcher's echo of the same
#: change, so one Write flashes once instead of twice.
DEDUPE_WINDOW_SECONDS = 2.0

#: How long after reporting a path a bare "modified" is treated as the tail of
#: that same write. Writing a file emits created+modified milliseconds apart.
COALESCE_WINDOW_SECONDS = 0.75

#: How often the observed repository is re-read for the HUD's branch. Polling is
#: the only way to see a checkout: the watcher filters paths through
#: ``tree.is_ignored``, which drops every dotted directory segment, so `.git/HEAD`
#: is invisible to it by design -- otherwise a single `git status` would flood the
#: graph with index churn. One small file read every couple of seconds is free.
REPO_POLL_INTERVAL_SECONDS = 2.0

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = REPO_ROOT / "web" / "dist"


class EventHub:
    """Normalizes ingested payloads and fans them out to WebSocket clients.

    Owns the state that must be consistent across all hooks, the watcher and
    every client:

      * ``_known_paths`` -- the tree as currently drawn. Drives add-vs-modify and
        lets a directory deletion prune the files under it.
      * ``_seed`` / ``_recent`` -- what a connecting client is replayed. The seed
        snapshot is kept apart from the ring buffer so ordinary traffic can never
        push the tree itself out of the replay.
      * ``_meta`` -- the HUD's context line (observed root, current branch). One
        replaceable slot of its own, for the same reason: it is re-published on
        every branch switch, and appending it to either list would let a busy
        session grow the replay or evict the tree from it.
      * ``_reset`` -- the last "the observed project changed, clear everything"
        frame, in a replaceable slot like ``_meta`` (see :meth:`reset`).
      * ``_last_hook`` -- the actor that acted most recently, as
        ``(agent, label, timestamp)``, which is how a filesystem change gets
        attributed to whoever caused it (see :meth:`ingest_fs_change`). The
        label is carried alongside the id rather than looked up later: the hub
        keeps no registry of actors, and an id with no name is a nameless figure
        on screen.
    """

    def __init__(
        self,
        project_root: str,
        buffer_size: int = REPLAY_BUFFER_SIZE,
        attribution_window: float = ATTRIBUTION_WINDOW_SECONDS,
        dedupe_window: float = DEDUPE_WINDOW_SECONDS,
        coalesce_window: float = COALESCE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._project_root = project_root
        self._known_paths: set[str] = set()
        self._seed: list[str] = []
        self._recent: deque[str] = deque(maxlen=buffer_size)
        self._meta: str | None = None
        self._reset: str | None = None
        self._clients: set[ServerConnection] = set()
        self._attribution_window = attribution_window
        self._dedupe_window = dedupe_window
        self._coalesce_window = coalesce_window
        self._clock = clock
        self._last_hook: tuple[str, str, float] | None = None
        self._hook_paths: dict[str, float] = {}
        self._fs_paths: dict[str, float] = {}

    # -- WebSocket side ----------------------------------------------------

    def replay_messages(self) -> list[str]:
        """Everything a client connecting right now must receive, in order.

        A pending reset goes first: it is an order to empty the canvas, so
        anything sent afterwards -- caption included -- must come *after* it or
        be wiped by it. The meta line follows, so the HUD is captioned before the
        first node appears; there is none until the daemon has looked at the
        repository.
        """
        reset = [self._reset] if self._reset is not None else []
        meta = [self._meta] if self._meta is not None else []
        return [*reset, *meta, *self._seed, *self._recent]

    async def register(self, websocket: ServerConnection) -> None:
        """Add a client and replay the tree plus recent activity."""
        self._clients.add(websocket)
        for message in self.replay_messages():
            with contextlib.suppress(Exception):
                await websocket.send(message)

    def unregister(self, websocket: ServerConnection) -> None:
        self._clients.discard(websocket)

    # -- Ingest side -------------------------------------------------------

    def set_meta(self, display_root: str, branch: str | None) -> None:
        """Publish the HUD's context line, but only when it actually changed.

        The daemon polls the repository every couple of seconds, so identical
        values arrive over and over; re-broadcasting them would be pure noise on
        the wire. ``branch`` is ``None`` when the observed directory is not a
        git checkout.
        """
        message = json.dumps(
            {"kind": "meta", "root": display_root, "branch": branch},
            separators=(",", ":"),
        )
        if message == self._meta:
            return
        self._meta = message
        broadcast(self._clients, message)

    def reset(self, project_root: str) -> None:
        """Point the hub at another project and forget the one before it.

        Not an assignment to ``_project_root``: every other piece of state here
        describes the *old* project and is actively wrong for the new one.

        ``_known_paths`` is the point of the whole method. It is what decides
        add-vs-modify, so a stale one draws the new project's ``src/app.py`` as a
        modification of a node no browser has ever seen -- a file that flashes
        orange and is never added. The rest follows: ``_seed``/``_recent`` would
        replay the previous tree to whoever connects next, ``_last_hook`` would
        credit the first change here to an agent working somewhere else, and
        ``_hook_paths``/``_fs_paths`` would swallow a genuine first event as the
        echo of a change that happened in another project.

        The frame is kept in a slot of its own so a client connecting *after* the
        switch is told to clear too. Unlike :meth:`set_meta` this does not dedupe
        on the value: resetting to the same root is a request for a clean slate,
        not an announcement that something differs.
        """
        self._project_root = project_root
        self._known_paths.clear()
        self._seed.clear()
        self._recent.clear()
        self._last_hook = None
        self._hook_paths.clear()
        self._fs_paths.clear()

        message = json.dumps(
            {"kind": "reset", "root": project_root}, separators=(",", ":")
        )
        self._reset = message
        broadcast(self._clients, message)

    def seed_paths(self, paths: Iterable[str]) -> None:
        """Publish the project's existing files as the graph's starting tree.

        Called once at boot with :func:`graphagents.tree.scan_tree`. Without it
        the page opens on a blank field and only ever shows the handful of files
        an agent happens to touch.
        """
        for path in paths:
            if not path or path in self._known_paths:
                continue
            event = seed_event(path)
            self._known_paths.add(path)
            message = _encode(event)
            self._seed.append(message)
            broadcast(self._clients, message)

    def ingest_line(self, line: str) -> None:
        """Normalize one raw hook JSON line and broadcast the event, if any."""
        payload = self._safe_load(line)
        if payload is None:
            return

        # Remember the actor even when the payload yields no drawable event: a
        # `find` or a glob-expanding `cp` still means this agent is the one at
        # work, and the changes the watcher is about to report are its doing.
        # Derived through `actor_of`, the same helper `normalize_event` uses, so
        # this path cannot credit a subagent's copies to the orchestrator while
        # the event it did produce carries the subagent.
        agent, label = actor_of(payload)
        if agent:
            self._last_hook = (agent, label, self._clock())

        event = normalize_event(
            payload,
            known_paths=self._known_paths,
            project_root=self._project_root,
        )
        if event is None:
            return

        self._hook_paths[event.path] = self._clock()
        self._publish(event)

    def ingest_fs_change(self, path: str, op_type: str) -> None:
        """Broadcast a change the watcher saw on disk, attributed if possible.

        Three filters keep this from being noise: a path a hook just reported is
        skipped (a Write fires both, and the browser must flash it once); a
        modification landing right after this file was already reported is the
        tail of the same write, not a second edit; and a directory deletion is
        expanded into the files known to live under it, so `rm -rf src/` empties
        that branch instead of leaving it floating.
        """
        if not path or self._recently_hooked(path):
            return
        if op_type == "M" and self._just_reported(path):
            return

        agent, label = self._active_agent()
        for target in self._expand(path, op_type):
            event = fs_event(target, op_type, agent=agent, label=label)
            if event is not None:
                self._fs_paths[target] = self._clock()
                self._publish(event)

    # -- internals ---------------------------------------------------------

    def _publish(self, event: Event) -> None:
        self._remember_path(event)
        message = _encode(event)
        self._recent.append(message)
        broadcast(self._clients, message)

    def _expand(self, path: str, op_type: str) -> list[str]:
        """A directory deletion also deletes everything known beneath it."""
        if op_type != "D":
            return [path]
        prefix = path.rstrip("/") + "/"
        children = sorted(p for p in self._known_paths if p.startswith(prefix))
        return [*children, path]

    def _recently_hooked(self, path: str) -> bool:
        stamped = self._hook_paths.get(path)
        return stamped is not None and self._clock() - stamped < self._dedupe_window

    def _just_reported(self, path: str) -> bool:
        stamped = self._fs_paths.get(path)
        return stamped is not None and self._clock() - stamped < self._coalesce_window

    def _active_agent(self) -> tuple[str, str]:
        """The ``(agent, label)`` still owning what the watcher reports.

        Both expire together: a name hovering over an actor the graph no longer
        credits is worse than an anonymous change.
        """
        if self._last_hook is None:
            return "", ""
        agent, label, stamped = self._last_hook
        if self._clock() - stamped >= self._attribution_window:
            return "", ""
        return agent, label

    def _remember_path(self, event: Event) -> None:
        # A deleted path may be re-added later; keep the set reflecting the
        # tree so a subsequent Write to the same path is an add, not a modify.
        if event.type == "D":
            self._known_paths.discard(event.path)
        else:
            self._known_paths.add(event.path)

    @staticmethod
    def _safe_load(line: str) -> dict | None:  # noqa: D401 - see class docstring
        stripped = line.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None


def _encode(event: Event) -> str:
    return json.dumps(asdict(event), separators=(",", ":"))


#: The only kinds a client may send. Anything else is a browser from another
#: version talking to this daemon, not an instruction.
COMMAND_KINDS = ("complete", "setRoot", "file")


def parse_command(raw: str) -> dict | None:
    """One frame off the network as a command, or ``None``.

    This is data typed by a human into a field and shipped over a socket, so it
    must **never raise**: an exception here kills the task serving that browser.
    Every unrecognized shape -- malformed JSON, a bare array, a missing or
    non-string ``path`` -- collapses to ``None``.

    The path is handed on exactly as typed: trimming and ``~`` expansion belong
    to :mod:`graphagents.paths`, and the answer echoes this text back so the page
    can tell whether it still matches what the viewer has in the field.
    """
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("kind")
    path = payload.get("path")
    if kind not in COMMAND_KINDS or not isinstance(path, str):
        return None
    return {"kind": kind, "path": path}


def control_allowed(remote_host: str, allow_remote: bool) -> bool:
    """May the peer at `remote_host` repoint this daemon?

    Loopback only unless explicitly opted in (see the module docstring). A peer
    whose address cannot be parsed -- ``getpeername`` can yield nothing usable --
    is refused: "no idea who this is" must not be read as "local".
    """
    if allow_remote:
        return True
    try:
        address = ipaddress.ip_address(remote_host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_loopback)


def completion_response(path: str, home: str) -> dict:
    """The answer to one ``Tab``, ready to be serialized as JSON.

    ``path`` is echoed intact: the viewer keeps typing while this travels, and
    the page drops an answer whose echo no longer matches the field -- otherwise
    a slow completion overwrites newer keystrokes.
    """
    completion = complete_dir(path, home)
    return {
        "kind": "completion",
        "path": path,
        "completed": completion.completed,
        # A `Completion` smuggled in whole would raise inside the send, on the
        # daemon's loop; only plain JSON types leave this function.
        "matches": list(completion.matches),
    }


class Session:
    """The observed project, and everything tied to it, in one place.

    Hub, watcher, seed scan and branch poll used to hang off a local variable in
    :func:`run` settled at boot, which is what made the root unswitchable. Owning
    them together lets :meth:`switch_root` perform the change as one ordered
    operation.

    ``home`` is a parameter rather than ``os.path.expanduser`` so the expansion
    of ``~`` -- both in the field and in the HUD caption -- is the caller's
    decision, and testable without a fixed ``$HOME``.
    """

    def __init__(self, project_root: str, home: str) -> None:
        self.home = home
        self.root = os.path.normpath(os.path.abspath(project_root))
        self.hub = EventHub(project_root=self.root)
        self._watcher = None

    # -- lifecycle ---------------------------------------------------------

    def start_watcher(self) -> None:
        if self._watcher is None:
            self._watcher = _start_watcher(self.hub, self.root)

    def stop(self) -> None:
        """Stop the watcher. Safe to call when there is none, or twice."""
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            with contextlib.suppress(Exception):
                watcher.stop()

    def publish_meta(self) -> None:
        """Caption the HUD with the *current* root and its branch."""
        self.hub.set_meta(
            display_root(self.root, self.home), read_branch(self.root)
        )

    # -- the switch --------------------------------------------------------

    async def switch_root(self, text: str) -> str | None:
        """Observe the project `text` names. Returns why it was refused, or ``None``.

        Validation comes first, on purpose: tearing the watcher down and clearing
        the hub before discovering the directory does not exist would leave the
        daemon observing nowhere, showing a blank page, with no way back. A
        refused switch changes nothing at all.

        The rest is ordered: stop the old observer (an abandoned project must not
        keep pushing events into a graph that no longer draws it), reset the hub
        (clear, and tell the browsers to clear), re-caption, re-seed, and only
        then watch the new root.

        The seed scan runs on a thread. Pointed at a home directory that walk
        takes seconds, and on the loop it would freeze every connected client for
        exactly that long.
        """
        resolved = resolve_root(text, self.home)
        if resolved is None:
            return f"not a directory: {text.strip() or '(empty)'}"

        self.stop()
        self.root = resolved
        self.hub.reset(resolved)
        self.publish_meta()

        seeded = await asyncio.to_thread(scan_tree, resolved)
        self.hub.seed_paths(seeded)
        LOGGER.info("observing %s (%d files)", resolved, len(seeded))

        self.start_watcher()
        return None

    # -- background --------------------------------------------------------

    async def poll_repo(self, interval: float = REPO_POLL_INTERVAL_SECONDS) -> None:
        """Keep the HUD's branch honest for the life of the daemon.

        Reads ``self.root`` on every turn rather than the root this task was
        created with: holding the latter, the poll would re-publish the abandoned
        project's branch seconds after a switch, overwriting the caption with the
        state of a project nobody is watching.

        `set_meta` filters out unchanged readings, so this loop can be dumb.
        """
        while True:
            await asyncio.sleep(interval)
            self.publish_meta()

    # -- inbound commands --------------------------------------------------

    async def handle_command(
        self, command: dict, websocket: ServerConnection
    ) -> None:
        """Run one parsed command and answer *that* client, nobody else.

        Dispatched explicitly on ``kind``. It used to read "``complete``, else
        treat it as a ``setRoot``", which was fine while those were the only two
        commands and actively wrong the moment a third existed: a ``file`` would
        have fallen through and swapped the observed project for a refusal about
        a path that is not a directory.
        """
        path = command["path"]
        kind = command["kind"]
        if kind == "complete":
            await _send(websocket, completion_response(path, self.home))
            return
        if kind == "file":
            await _send(websocket, await file_view(self.root, path))
            return
        if kind != "setRoot":
            return
        reason = await self.switch_root(path)
        if reason is not None:
            await _send(websocket, {"kind": "rootError", "path": path, "reason": reason})
        # On success there is nothing to answer directly: the `reset` and `meta`
        # frames already went to every client, this one included.


async def _send(websocket: ServerConnection, frame: dict) -> None:
    with contextlib.suppress(Exception):
        await websocket.send(json.dumps(frame, separators=(",", ":")))


def _allow_remote_control() -> bool:
    return os.environ.get("GRAPHAGENTS_ALLOW_REMOTE_CONTROL", "") not in ("", "0")


def _peer_host(websocket: ServerConnection) -> str:
    """The peer's address, or ``""`` when it cannot be determined."""
    try:
        remote = websocket.remote_address
        return str(remote[0]) if remote else ""
    except Exception:
        return ""


async def _handle_ws_client(
    hub: EventHub,
    session: Session | None,
    websocket: ServerConnection,
) -> None:
    """Serve one browser: replay history, then serve its commands.

    Each inbound frame is dispatched inside its own guard: a command that blows
    up loses that command and nothing else -- not the connection, and certainly
    not the daemon.
    """
    await hub.register(websocket)
    try:
        async for raw in websocket:
            command = parse_command(raw if isinstance(raw, str) else raw.decode())
            if command is None or session is None:
                continue
            if not control_allowed(_peer_host(websocket), _allow_remote_control()):
                await _send(
                    websocket,
                    {
                        "kind": "rootError",
                        "path": command["path"],
                        "reason": "remote control disabled",
                    },
                )
                continue
            try:
                await session.handle_command(command, websocket)
            except Exception as exc:
                LOGGER.debug("ws command error: %s", exc)
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
    session: Session | None = None,
) -> Server:
    """Start one listener answering both HTTP and WebSocket traffic.

    Without a `session` the socket stays what it used to be -- broadcast-only:
    inbound commands are parsed and dropped, because there is nothing here that
    owns a root to switch.
    """
    return await serve(
        functools.partial(_handle_ws_client, hub, session),
        host=host,
        port=port,
        process_request=functools.partial(_process_request, static_root),
    )


def _start_watcher(hub: EventHub, project_root: str):
    """Start the filesystem watcher, or return ``None`` if it cannot run.

    The watcher's callbacks arrive on watchdog's own thread, so they are handed
    back to the event loop with ``call_soon_threadsafe`` -- broadcasting from
    another thread would corrupt the WebSocket connections.

    Import and startup failures are tolerated: without the watcher the daemon
    still works from hooks alone, and refusing to boot over an optional
    dependency would be a worse outcome than a less complete graph.
    """
    try:
        from daemon.watcher import FsWatcher
    except Exception as exc:
        LOGGER.warning(
            "filesystem watcher unavailable (%s); falling back to hooks only. "
            "Install it with: pip install -e '.[daemon]'",
            exc,
        )
        return None

    loop = asyncio.get_running_loop()

    def on_change(path: str, op_type: str) -> None:
        loop.call_soon_threadsafe(hub.ingest_fs_change, path, op_type)

    watcher = FsWatcher(project_root, on_change)
    watcher.start()
    LOGGER.info("watching %s for filesystem changes", project_root)
    return watcher


async def run(
    socket_path: str,
    http_port: int,
    project_root: str,
) -> None:
    session = Session(project_root=project_root, home=os.path.expanduser("~"))
    hub = session.hub

    # Caption and seed before the listener opens, so the first client to connect
    # already finds a captioned tree in the replay rather than an empty field.
    session.publish_meta()
    seeded = scan_tree(session.root)
    hub.seed_paths(seeded)
    LOGGER.info("seeded %d existing files from %s", len(seeded), session.root)

    if os.path.exists(socket_path):
        os.unlink(socket_path)

    ingest_server = await asyncio.start_unix_server(
        functools.partial(_handle_ingest_client, hub), path=socket_path
    )

    session.start_watcher()
    repo_poll = asyncio.create_task(session.poll_repo())

    static_root: Path | None = WEB_DIST if WEB_DIST.is_dir() else None
    if static_root is None:
        LOGGER.info(
            "web/dist not found; serving WebSocket only "
            "(let the Vite dev server host the front)."
        )
    else:
        LOGGER.info("serving %s at http://localhost:%d", static_root, http_port)

    ws_server = await start_server(
        hub, host="", port=http_port, static_root=static_root, session=session
    )

    LOGGER.info(
        "ingest on %s | http + websocket on :%d", socket_path, http_port
    )

    stop = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(
                sig, lambda: stop.done() or stop.set_result(None)
            )

    try:
        async with ingest_server, ws_server:
            with contextlib.suppress(asyncio.CancelledError):
                await stop
    finally:
        repo_poll.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await repo_poll
        session.stop()
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
