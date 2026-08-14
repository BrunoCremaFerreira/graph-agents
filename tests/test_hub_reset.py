"""Contract tests (RED) for EventHub.reset: switching the observed project.

Motivation: the root is fixed at boot (``GRAPHAGENTS_PROJECT_ROOT`` reaches the
hub, the watcher, the tree scan and the branch poll), so looking at a second
project means restarting the daemon. The new feature repoints it at runtime, and
`reset` is the moment of the switch on the server side.

It cannot be a matter of assigning a new string. Every piece of state the hub
keeps belongs to the *old* project and is actively wrong for the new one:

  * ``_known_paths`` decides add-vs-modify. Left behind, the first Write to
    ``src/app.py`` in the new project is drawn as a modification of a node the
    page has never seen -- this is the concrete defect that makes the reset
    necessary at all, not an aesthetic cleanup.
  * ``_seed`` / ``_recent`` are the replay. Left behind, a browser connecting a
    second later is handed the previous project's tree and its recent activity.
  * ``_last_hook`` owns whatever the watcher reports next. Left behind, the first
    change in the new project is credited to an agent that was working somewhere
    else.
  * ``_hook_paths`` / ``_fs_paths`` suppress echoes of changes that happened
    before the switch, so the first real event in the new project can be silently
    swallowed as a duplicate.

The clients already on screen must be told: `reset` broadcasts
``{"kind": "reset", "root": ...}`` and keeps it in a slot of its own -- exactly
like ``_meta`` -- so a client connecting *after* the switch gets the same
instruction to clear its canvas, before the new tree arrives.

Unlike ``set_meta``, resetting to the same root is not a no-op: the caller is
asking for a clean slate, not announcing a difference.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import json

from websockets.asyncio.client import connect

from daemon.server import EventHub, start_server

OLD_ROOT = "/proj"
NEW_ROOT = "/srv/other"
SESSION = "sess-abc"


def _hook(file_path: str, tool_name: str = "Write") -> str:
    return json.dumps(
        {
            "session_id": SESSION,
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": {"file_path": file_path},
        }
    )


def _sent(hub: EventHub) -> list[dict]:
    """Every message a freshly connecting client would receive, in order."""
    return [json.loads(m) for m in hub.replay_messages()]


def _events(hub: EventHub) -> list[dict]:
    return [m for m in _sent(hub) if "kind" not in m]


def _resets(hub: EventHub) -> list[dict]:
    return [m for m in _sent(hub) if m.get("kind") == "reset"]


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


# --- 1. Nothing of the old project survives in the replay ------------------

def test_the_previous_projects_tree_is_gone_from_the_replay():
    hub = EventHub(project_root=OLD_ROOT)
    hub.seed_paths(["src/app.py", "README.md"])

    hub.reset(NEW_ROOT)

    assert [e["path"] for e in _events(hub)] == []


def test_the_previous_projects_recent_activity_is_gone_from_the_replay():
    hub = EventHub(project_root=OLD_ROOT)
    hub.ingest_line(_hook(f"{OLD_ROOT}/notes.md"))

    hub.reset(NEW_ROOT)

    assert [e["path"] for e in _events(hub)] == []


# --- 2. The reset frame itself ---------------------------------------------

def test_a_client_connecting_after_the_switch_is_told_to_clear():
    hub = EventHub(project_root=OLD_ROOT)
    hub.seed_paths(["src/app.py"])

    hub.reset(NEW_ROOT)

    assert _resets(hub) == [{"kind": "reset", "root": NEW_ROOT}]


def test_the_reset_frame_precedes_the_new_tree():
    # A client must empty its canvas before the new nodes arrive, or the two
    # projects are drawn on top of each other.
    hub = EventHub(project_root=OLD_ROOT)

    hub.reset(NEW_ROOT)
    hub.seed_paths(["lib/util.py"])

    kinds = [m.get("kind", "event") for m in _sent(hub)]
    assert kinds.index("reset") < kinds.index("event")


def test_repeated_switches_leave_exactly_one_reset_in_the_replay():
    # Appending instead of replacing would grow the replay with every switch.
    hub = EventHub(project_root=OLD_ROOT)

    hub.reset(NEW_ROOT)
    hub.reset("/srv/third")

    assert _resets(hub) == [{"kind": "reset", "root": "/srv/third"}]


# --- 3. The state that actually decides what is drawn ----------------------

def test_a_path_known_before_the_switch_is_added_again_not_modified():
    # The reason reset exists: a file of the same name in the new project has
    # never been on screen, so it must arrive as an addition.
    hub = EventHub(project_root=OLD_ROOT)
    hub.seed_paths(["src/app.py"])

    hub.reset(NEW_ROOT)
    hub.ingest_line(_hook(f"{NEW_ROOT}/src/app.py"))

    assert _events(hub)[-1]["type"] == "A"


def test_incoming_paths_are_relativized_against_the_new_root():
    hub = EventHub(project_root=OLD_ROOT)

    hub.reset(NEW_ROOT)
    hub.ingest_line(_hook(f"{NEW_ROOT}/lib/util.py"))

    assert _events(hub)[-1]["path"] == "lib/util.py"


def test_the_agent_of_the_previous_project_owns_nothing_after_the_switch():
    hub = EventHub(project_root=OLD_ROOT)
    hub.ingest_line(_hook(f"{OLD_ROOT}/anything.md", tool_name="Bash"))

    hub.reset(NEW_ROOT)
    hub.ingest_fs_change("lib/util.py", "A")

    assert _events(hub)[-1]["agent"] == ""


def test_a_hook_echo_pending_from_the_previous_project_does_not_swallow_a_new_change():
    hub = EventHub(project_root=OLD_ROOT)
    hub.ingest_line(_hook(f"{OLD_ROOT}/src/app.py"))

    hub.reset(NEW_ROOT)
    hub.ingest_fs_change("src/app.py", "M")

    assert [e["path"] for e in _events(hub)] == ["src/app.py"]


def test_a_watcher_echo_pending_from_the_previous_project_does_not_swallow_a_new_change():
    hub = EventHub(project_root=OLD_ROOT)
    hub.ingest_fs_change("src/app.py", "A")

    hub.reset(NEW_ROOT)
    hub.ingest_fs_change("src/app.py", "M")

    assert [e["type"] for e in _events(hub)] == ["M"]


def test_resetting_to_the_same_root_still_clears_everything():
    # Not a no-op by equality (set_meta dedupes; this does not): re-observing the
    # same directory is how a viewer asks for a fresh, re-seeded graph.
    hub = EventHub(project_root=OLD_ROOT)
    hub.seed_paths(["src/app.py"])

    hub.reset(OLD_ROOT)

    assert _events(hub) == []
    assert _resets(hub) == [{"kind": "reset", "root": OLD_ROOT}]


# --- 4. The clients already connected --------------------------------------

class TestBroadcast:
    def test_the_reset_reaches_a_client_that_is_already_on_screen(self):
        async def scenario():
            hub = EventHub(project_root=OLD_ROOT)
            server = await start_server(hub, host="127.0.0.1", port=0, static_root=None)
            port = next(iter(server.sockets)).getsockname()[1]
            async with server, connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hub.reset(NEW_ROOT)
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert message == {"kind": "reset", "root": NEW_ROOT}

        _run(scenario())
