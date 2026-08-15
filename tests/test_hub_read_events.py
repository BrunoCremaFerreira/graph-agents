"""Contract tests (RED) for how EventHub must handle an `R` (read) event.

Companion to ``tests/test_read_events.py``, which pins the pure normalization.
The design rule is one sentence -- **a read is not a change** -- and the hub is
where breaking it costs something. Every piece of shared state here exists to
describe *the tree and who changed it*, and a read must leave all of it alone:

  * **`_known_paths`.** It decides add-vs-modify. Read-then-Edit is the single
    commonest thing an agent does, so a read that marks the path as seen turns
    the very next Write into a modification of a node no browser was ever shown:
    a file that flashes orange and is never added.
  * **The replay buffer.** A read is a flash, not a fact about the project. A
    client connecting five minutes later must be handed the tree, not a
    re-enactment of somebody's reading. Worse, the ring is finite: an agent
    reading twenty files pushes the real changes out of it.
  * **The hook-dedupe map.** It suppresses the watcher's echo of a change a hook
    just reported. A read has no echo -- nothing happened on disk -- so stamping
    the path there swallows the genuine write that follows it.

One thing a read *must* do: refresh the active agent. A read is proof this agent
is the one at work, and the changes the watcher reports next are its doing.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import json

from websockets.asyncio.client import connect

from daemon.server import EventHub, start_server

ROOT = "/proj"
SESSION = "sess-abc"
SUBAGENT_ID = "a747fec535c143044"
SUBAGENT_TYPE = "developer-tester"


def _hook(
    tool_name: str,
    file_path: str,
    agent_id: str | None = None,
    agent_type: str | None = None,
) -> str:
    payload: dict = {
        "session_id": SESSION,
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return json.dumps(payload)


def _read(file_path: str = f"{ROOT}/src/app.py", **kwargs) -> str:
    return _hook("Read", file_path, **kwargs)


def _write(file_path: str = f"{ROOT}/src/app.py", **kwargs) -> str:
    return _hook("Write", file_path, **kwargs)


def _sent(hub: EventHub) -> list[dict]:
    """Every message a freshly connecting client would receive, in order."""
    return [json.loads(m) for m in hub.replay_messages()]


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


# --- 1. A read does not make a path known -----------------------------------

def test_a_read_then_a_write_of_the_same_path_is_still_an_add():
    # Read-then-Edit is what an agent does all day. If the read marked the path
    # as seen, the write it leads to would be drawn as a modification of a node
    # nobody has ever been shown.
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_read())

    hub.ingest_line(_write())

    assert _sent(hub)[-1]["type"] == "A"


# --- 2. A read is broadcast, but never replayed -----------------------------

class TestReplay:
    def test_a_read_reaches_the_clients_watching_now_and_nobody_later(self):
        async def scenario():
            hub = EventHub(project_root=ROOT)
            listener = await start_server(
                hub, host="127.0.0.1", port=0, static_root=None
            )
            port = next(iter(listener.sockets)).getsockname()[1]
            async with listener, connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hub.ingest_line(_read())
                # A frame that must arrive after it, so a read that is never
                # broadcast fails here instead of hanging until the timeout.
                hub.ingest_line(_write(f"{ROOT}/marker.py"))

                live = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert live["type"] == "R"
                assert live["path"] == "src/app.py"

                replayed = [m.get("type") for m in _sent(hub)]
                assert "R" not in replayed

        _run(scenario())


# --- 3. A read does not suppress the watcher --------------------------------

def test_the_watchers_report_of_a_file_just_read_is_still_published():
    # The dedupe exists because a Write fires both a hook and a filesystem
    # event. A read fires no filesystem event at all, so a read that stamps the
    # dedupe map swallows the next real write to that file.
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_read())

    hub.ingest_fs_change("src/app.py", "M")

    assert [m["path"] for m in _sent(hub) if m.get("origin") == "watch"] == [
        "src/app.py"
    ]


# --- 4. A read still says who is working ------------------------------------

def test_a_filesystem_change_after_a_read_belongs_to_the_agent_that_read():
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_read(agent_id=SUBAGENT_ID, agent_type=SUBAGENT_TYPE))

    hub.ingest_fs_change("docs/generated.md", "A")

    event = _sent(hub)[-1]
    assert (event["agent"], event["label"]) == (SUBAGENT_ID, SUBAGENT_TYPE)
