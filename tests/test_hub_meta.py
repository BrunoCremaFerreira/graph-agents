"""Contract tests (RED) for the HUD's context message on EventHub.

Motivation: the page shows a graph with no caption. Looking at a forwarded port
you cannot tell *which* checkout you are watching, and a branch switch changes
the meaning of every node without a single pixel saying so. The daemon therefore
publishes a small, separate message the HUD renders at the bottom of the screen:

    {"kind": "meta", "root": "~/projects/rhizome-graph", "branch": "development"}

Two properties keep it from poisoning the stream:

  * It occupies **one replaceable slot**, replayed first. Appending it to the
    seed or the recent-ring would make the replay grow with every branch switch
    and could push the tree out of the buffer.
  * It is broadcast **only when it actually changes**. The daemon polls the repo
    every couple of seconds; re-sending identical values would be pure noise.

`branch` is ``null`` when the observed directory is not a git checkout -- the
page must survive both that and never receiving a meta message at all.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from daemon.server import EventHub, start_server

ROOT = "/proj"

HOOK = json.dumps(
    {
        "session_id": "sess-xyz",
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "notes.md"},
    }
)


def _sent(hub: EventHub) -> list[dict]:
    """Every message a freshly connecting client would receive, in order."""
    return [json.loads(m) for m in hub.replay_messages()]


def _metas(hub: EventHub) -> list[dict]:
    return [m for m in _sent(hub) if m.get("kind") == "meta"]


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


# --- 1. The replay slot ----------------------------------------------------

def test_meta_is_the_first_message_a_new_client_receives():
    hub = EventHub(project_root=ROOT)
    hub.seed_paths(["src/app.py"])
    hub.ingest_line(HOOK)

    hub.set_meta("~/projects/rhizome-graph", "development")

    assert _sent(hub)[0]["kind"] == "meta"


def test_meta_carries_the_display_root_and_the_branch():
    hub = EventHub(project_root=ROOT)

    hub.set_meta("~/projects/rhizome-graph", "development")

    assert _metas(hub) == [
        {"kind": "meta", "root": "~/projects/rhizome-graph", "branch": "development"}
    ]


def test_a_project_without_git_reports_a_null_branch():
    hub = EventHub(project_root=ROOT)

    hub.set_meta("/srv/code/app", None)

    assert _metas(hub)[0]["branch"] is None


def test_repeated_updates_leave_exactly_one_meta_in_the_replay():
    # Appending instead of replacing would grow the replay on every poll.
    hub = EventHub(project_root=ROOT)

    for branch in ("main", "development", "feature/a", "feature/b", "feature/hud-contexto"):
        hub.set_meta("~/projects/rhizome-graph", branch)

    assert _metas(hub) == [
        {"kind": "meta", "root": "~/projects/rhizome-graph", "branch": "feature/hud-contexto"}
    ]


def test_meta_does_not_consume_the_recent_event_buffer():
    hub = EventHub(project_root=ROOT, buffer_size=2)
    hub.ingest_line(HOOK)

    for branch in ("a", "b", "c", "d", "e"):
        hub.set_meta("~/p", branch)

    assert [m["path"] for m in _sent(hub) if m.get("kind") != "meta"] == ["notes.md"]


def test_before_any_update_there_is_no_meta_message():
    hub = EventHub(project_root=ROOT)
    hub.seed_paths(["src/app.py"])

    assert _metas(hub) == []


def test_meta_is_not_confusable_with_an_event():
    hub = EventHub(project_root=ROOT)
    hub.set_meta("~/p", "main")
    hub.ingest_line(HOOK)

    meta, event = _sent(hub)[0], _sent(hub)[-1]

    assert not {"ts", "type", "path"} & set(meta)
    assert "kind" not in event


# --- 2. Broadcasting only on a real change ---------------------------------

async def _serve():
    hub = EventHub(project_root=ROOT)
    server = await start_server(hub, host="127.0.0.1", port=0, static_root=None)
    return hub, server, next(iter(server.sockets)).getsockname()[1]


class TestBroadcast:
    def test_a_changed_meta_reaches_connected_clients(self):
        async def scenario():
            hub, server, port = await _serve()
            async with server, connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hub.set_meta("~/projects/rhizome-graph", "development")
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert message == {
                    "kind": "meta",
                    "root": "~/projects/rhizome-graph",
                    "branch": "development",
                }

        _run(scenario())

    def test_a_branch_switch_is_pushed_without_reconnecting(self):
        async def scenario():
            hub, server, port = await _serve()
            async with server, connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hub.set_meta("~/p", "main")
                await asyncio.wait_for(ws.recv(), timeout=5)

                hub.set_meta("~/p", "feature/hud-contexto")
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert message["branch"] == "feature/hud-contexto"

        _run(scenario())

    def test_an_unchanged_meta_is_not_rebroadcast(self):
        """The poll runs every couple of seconds; only differences go on the wire."""

        async def scenario():
            hub, server, port = await _serve()
            async with server, connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hub.set_meta("~/p", "main")
                await asyncio.wait_for(ws.recv(), timeout=5)

                hub.set_meta("~/p", "main")
                hub.ingest_line(HOOK)  # a marker that must arrive next

                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert message.get("path") == "notes.md"

        _run(scenario())


@pytest.mark.parametrize("branch", ["main", None])
def test_meta_is_valid_json(branch):
    hub = EventHub(project_root=ROOT)

    hub.set_meta("~/p", branch)

    raw = hub.replay_messages()[0]
    assert json.loads(raw)["branch"] == branch
