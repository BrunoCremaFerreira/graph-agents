#!/usr/bin/env python3
"""Fire a burst of read events at a running daemon, to look at the violet rings.

This exists because four things about the read marker are recorded in CLAUDE.md
as "not yet verified", and every one of them needs a real screen and a real
burst rather than a test:

  * whether violet reads clearly against the amber write flash at real zoom;
  * whether 24 rings at once -- the pool size -- is calm or noisy;
  * whether the 0.75 tint leaves enough amber on a file read right after it was
    written;
  * how the ring's pulse sits beside the search ring when both land on one node.

The `--scenario` flag produces each of those on purpose, so you are looking at
the case rather than waiting for it to happen.

It speaks the daemon's ingest socket directly -- newline-delimited raw hook
payloads, exactly what `rhizome_graph/hook.py` forwards -- rather than running
the hook once per event. That is a deliberate trade and worth knowing: it buys
the sub-millisecond spacing a *burst* needs (one hook process per event costs
~40 ms, which would spread 24 reads over a second and never show you 24 rings at
once), and it gives up covering the hook itself. If it is the hook you want to
exercise, `--via-hook` runs the real one, slowly and honestly.

The events are real in every other sense: real paths from the observed project,
`agent_id` and `agent_type` shaped the way a subagent's tool call carries them,
so the figures, the colours and the beams are the ones you would see in earnest.
Nothing is written to disk and the daemon's state is untouched -- a read is
`_broadcast_transient` and touches no `known_paths`, which is the point of it.

**The root is asked of the daemon, not assumed**, and that is not a convenience.
A read is accepted only when it lies strictly under the observed root -- there is
no watcher correction for a read, since nothing happened on disk -- so a burst
aimed at the wrong root is discarded in full, silently, and looks exactly like a
broken read marker. That is not hypothetical: it is what happened the first time
this tool was used, because `ctrl+L` had moved the root since the daemon was
started, so the command line said one thing and the daemon was watching another.
The daemon publishes its current root in the `meta` frame; this reads it from
there and only falls back to a positional argument when it cannot connect.

Usage:

    rhi ~/some/project &
    python3 tools/read_burst.py                       # asks the daemon what it watches
    python3 tools/read_burst.py --scenario write-then-read
    python3 tools/read_burst.py --agents 4 --reads 40 --rate 30
    python3 tools/read_burst.py ~/some/project        # override, warned about if it disagrees

If the daemon moved off the default ingest socket it says so on startup; pass
that path with `--socket`, or set `RHIZOME_SOCKET`.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SOCKET_PATH = "/tmp/rhizome-graph.sock"

#: The renderer pools this many read rings. Bursting exactly this many is how
#: you find out whether the pool is calm or a mess, so it is the default.
READ_MARKER_POOL = 24

#: Names in the shape `agent_type` really carries, so the labels under the
#: figures read like a session rather than like a fixture.
AGENT_TYPES = (
    "developer-backend",
    "developer-frontend",
    "developer-tester",
    "security-auditor",
    "software-architect",
)

#: Directories never worth reading from, mirroring what the watcher ignores.
SKIP_DIRECTORIES = {
    ".git", ".venv", "node_modules", "__pycache__", "dist", "build",
    ".npm-bootstrap", ".pytest_cache", ".mypy_cache", "venv",
}


def observed_root(url: str, timeout: float = 5.0) -> str | None:
    """The root the daemon is watching *now*, from its `meta` frame.

    `ctrl+L` moves the root at runtime, so the directory named on the command
    line that started the daemon is not the answer -- it is only the answer at
    boot. `meta` sits first in the replay a client is handed, so this costs one
    connection and one frame.

    Returns `None` rather than raising if the daemon cannot be reached: the
    caller then falls back to the positional argument and says so. Needs
    `websockets`, which the daemon's own extra installs; without it, `None`.
    """
    try:
        import asyncio

        from websockets.asyncio.client import connect
    except Exception:
        return None

    async def ask() -> str | None:
        async with connect(url) as ws:
            async with asyncio.timeout(timeout):
                async for raw in ws:
                    frame = json.loads(raw)
                    if frame.get("kind") == "meta":
                        root = frame.get("root")
                        return os.path.expanduser(root) if isinstance(root, str) else None
        return None

    try:
        return asyncio.run(ask())
    except Exception:
        return None


def collect_files(root: Path, limit: int = 400) -> list[str]:
    """Real paths from the observed project, as absolute strings.

    Absolute because that is what a hook payload carries; the daemon makes them
    relative to the root it is watching. Handing it paths it cannot relativize
    is the one way this tool could put a wrong node on screen that never leaves.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= limit:
            break
        if any(part in SKIP_DIRECTORIES or part.startswith(".") for part in path.parts):
            continue
        if path.is_file():
            found.append(str(path))
    return found


def read_payload(path: str, agent: str, agent_type: str, session: str) -> dict:
    """One `PostToolUse` payload for a subagent's `Read`.

    A subagent's call carries `session_id` *and* `agent_id` *and* `agent_type`;
    the orchestrator's carries only the session. `agent` is identity and
    `agent_type` is only text, so two agents of one type stay two figures --
    which is exactly what `--agents` is for.
    """
    return {
        "session_id": session,
        "agent_id": agent,
        "agent_type": agent_type,
        "tool_name": "Read",
        "tool_input": {"file_path": path},
    }


def write_payload(path: str, agent: str, agent_type: str, session: str) -> dict:
    """The same, for a `Write` -- the amber flash the violet has to be told from."""
    return {
        "session_id": session,
        "agent_id": agent,
        "agent_type": agent_type,
        "tool_name": "Write",
        "tool_input": {"file_path": path, "content": "x"},
    }


class Sender:
    """One connection to the ingest socket, or the real hook per event."""

    def __init__(self, socket_path: str, via_hook: bool) -> None:
        self.socket_path = socket_path
        self.via_hook = via_hook
        self.sent = 0
        self._sock: socket.socket | None = None
        if not via_hook:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(2.0)
            self._sock.connect(socket_path)

    def send(self, payload: dict) -> None:
        line = json.dumps(payload)
        if self.via_hook:
            hook = Path(__file__).resolve().parent.parent / "hooks" / "emit_event.py"
            subprocess.run(
                [sys.executable, str(hook)],
                input=line,
                text=True,
                env={**os.environ, "RHIZOME_SOCKET": self.socket_path},
                check=False,
            )
        else:
            assert self._sock is not None
            self._sock.sendall((line + "\n").encode("utf-8"))
        self.sent += 1

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()


def scenario_burst(files: list[str], agents: list[tuple[str, str]], count: int) -> list[dict]:
    """`count` reads spread over the agents -- the pool-size question."""
    session = "burst-session"
    events = []
    for index in range(count):
        agent, agent_type = agents[index % len(agents)]
        events.append(read_payload(files[index % len(files)], agent, agent_type, session))
    return events


def scenario_write_then_read(files: list[str], agents: list[tuple[str, str]], count: int) -> list[dict]:
    """Write a file, then read the same one -- the 0.75 tint question.

    The write's amber has barely begun to decay (0.9/s) when the read's violet
    lands on the same node. Whether enough amber survives the tint is the thing
    to look at, and it is the case a random burst almost never produces.
    """
    session = "tint-session"
    agent, agent_type = agents[0]
    events = []
    for index in range(count):
        path = files[index % len(files)]
        events.append(write_payload(path, agent, agent_type, session))
        events.append(read_payload(path, agent, agent_type, session))
    return events


def scenario_mixed(files: list[str], agents: list[tuple[str, str]], count: int) -> list[dict]:
    """Reads and writes interleaved across agents -- violet against amber.

    Roughly ten reads per write, which is about what an agent really does, so
    the ratio on screen is the ratio you will live with.
    """
    session = "mixed-session"
    events = []
    for index in range(count):
        agent, agent_type = agents[index % len(agents)]
        path = random.choice(files)
        if index % 10 == 9:
            events.append(write_payload(path, agent, agent_type, session))
        else:
            events.append(read_payload(path, agent, agent_type, session))
    return events


SCENARIOS = {
    "burst": scenario_burst,
    "write-then-read": scenario_write_then_read,
    "mixed": scenario_mixed,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fire read events at a running daemon so the violet rings can be looked at."
    )
    parser.add_argument(
        "root", nargs="?",
        help="the project the daemon is observing. Omit it and the daemon is asked, "
             "which is the only answer that survives a ctrl+L root switch",
    )
    parser.add_argument(
        "--url", default="ws://127.0.0.1:8080/ws",
        help="the daemon's WebSocket, used to ask which root it watches "
             "(default: ws://127.0.0.1:8080/ws)",
    )
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="burst",
        help="burst: many rings at once. write-then-read: amber under violet. "
             "mixed: the real read-to-write ratio. (default: burst)",
    )
    parser.add_argument(
        "--reads", type=int, default=READ_MARKER_POOL,
        help=f"how many events to send (default: {READ_MARKER_POOL}, the ring pool size)",
    )
    parser.add_argument(
        "--agents", type=int, default=3,
        help="how many distinct subagents are reading (default: 3)",
    )
    parser.add_argument(
        "--rate", type=float, default=60.0,
        help="events per second; 0 means as fast as possible (default: 60)",
    )
    parser.add_argument(
        "--socket", default=os.environ.get("RHIZOME_SOCKET", DEFAULT_SOCKET_PATH),
        help="the daemon's ingest socket (default: $RHIZOME_SOCKET or the standard path)",
    )
    parser.add_argument(
        "--via-hook", action="store_true",
        help="send through the real hooks/emit_event.py instead of the socket: "
             "honest end to end, far too slow for a burst",
    )
    args = parser.parse_args()

    live = observed_root(args.url)
    if live is None and args.root is None:
        print(
            f"could not ask {args.url} which root it watches, and no directory was given.\n"
            "Start the daemon, or name the root it is observing.",
            file=sys.stderr,
        )
        return 2

    if args.root is None:
        chosen = live
    else:
        chosen = str(Path(args.root).expanduser().resolve())
        if live is not None and os.path.normpath(chosen) != os.path.normpath(live):
            # Refuse rather than warn. Every event would be dropped by the read
            # rule, and a burst that vanishes reads as a broken read marker
            # rather than as a wrong argument -- which is exactly the hour this
            # check exists to save.
            print(
                f"the daemon is watching {live}, not {chosen}.\n"
                "A read outside the observed root is discarded, so every event would\n"
                "vanish and the graph would look broken. Drop the argument to use the\n"
                "root it actually watches, or point ctrl+L at this one first.",
                file=sys.stderr,
            )
            return 2

    root = Path(chosen).expanduser().resolve()
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 2

    files = collect_files(root)
    if not files:
        print(f"no readable files under {root}", file=sys.stderr)
        return 2

    if not Path(args.socket).exists():
        print(
            f"no ingest socket at {args.socket}.\n"
            "Is the daemon running? If it moved off the default it printed the\n"
            "RHIZOME_SOCKET to use on startup; pass it with --socket.",
            file=sys.stderr,
        )
        return 1

    agents = [
        (f"agent-{index:02d}", AGENT_TYPES[index % len(AGENT_TYPES)])
        for index in range(max(1, args.agents))
    ]
    events = SCENARIOS[args.scenario](files, agents, args.reads)

    print(f"{len(events)} events -> {args.socket}")
    print(f"  scenario : {args.scenario}")
    print(f"  agents   : {', '.join(a for a, _ in agents)}")
    print(f"  files    : {len(files)} under {root}")
    print("Watch the page now.")

    try:
        sender = Sender(args.socket, args.via_hook)
    except OSError as error:
        print(f"could not reach the daemon at {args.socket}: {error}", file=sys.stderr)
        return 1

    gap = 0.0 if args.rate <= 0 else 1.0 / args.rate
    started = time.monotonic()
    try:
        for payload in events:
            sender.send(payload)
            if gap:
                time.sleep(gap)
    except OSError as error:
        print(f"the daemon stopped listening after {sender.sent} events: {error}", file=sys.stderr)
        return 1
    finally:
        # Give the loop a moment to drain before the socket closes under it.
        time.sleep(0.2)
        sender.close()

    elapsed = time.monotonic() - started
    print(f"sent {sender.sent} events in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
