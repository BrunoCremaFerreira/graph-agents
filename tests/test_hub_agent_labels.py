"""Contract tests (RED) for subagent identity surviving the daemon.

Companion to ``tests/test_agent_identity.py``, which pins the pure
normalization. Two things can still lose a subagent between the hook and the
browser:

  * **The wire.** ``_encode`` serializes the ``Event`` dataclass; if ``label``
    is not on the line, the page has an opaque id and nothing to print under
    the figure. The fields already on the wire must not move: an old tab must
    not break because a new key appeared.
  * **Attribution.** ``EventHub`` remembers the last hook in ``_last_hook`` and
    credits the filesystem changes that follow to it -- but it remembers the raw
    ``session_id``, so every watcher-reported change of a subagent lands on the
    orchestrator's figure. It must remember the actor **of the event** (the
    ``agent_id`` when there is one) *and* that actor's label, otherwise the
    specialist shows up unnamed for the events hooks cannot see at all
    (a glob-expanding ``cp``, a compound command), which is most of them.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import json

from daemon.server import EventHub

ROOT = "/proj"
SESSION = "sess-abc"
SUBAGENT_ID = "a747fec535c143044"
SUBAGENT_TYPE = "desenvolvedor-tester"

#: Every key the frontend already reads off the wire. New fields may be added;
#: none of these may disappear or be renamed.
LEGACY_FIELDS = {"ts", "agent", "type", "path", "color", "origin"}


def _hook(
    tool_name: str = "Write",
    file_path: str = f"{ROOT}/src/app.py",
    command: str | None = None,
    agent_id: str | None = None,
    agent_type: str | None = None,
) -> str:
    payload: dict = {
        "session_id": SESSION,
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": (
            {"command": command} if command is not None else {"file_path": file_path}
        ),
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return json.dumps(payload)


def _subagent_hook(**kwargs) -> str:
    return _hook(agent_id=SUBAGENT_ID, agent_type=SUBAGENT_TYPE, **kwargs)


def _sent(hub: EventHub) -> list[dict]:
    """Every message a freshly connecting client would receive, in order."""
    return [json.loads(m) for m in hub.replay_messages()]


# --- 1. The label reaches the browser --------------------------------------

def test_a_subagent_event_carries_its_label_on_the_wire():
    hub = EventHub(project_root=ROOT)

    hub.ingest_line(_subagent_hook())

    assert _sent(hub)[-1]["label"] == SUBAGENT_TYPE


def test_a_subagent_event_is_attributed_to_its_agent_id_on_the_wire():
    hub = EventHub(project_root=ROOT)

    hub.ingest_line(_subagent_hook())

    assert _sent(hub)[-1]["agent"] == SUBAGENT_ID


def test_an_orchestrator_event_goes_out_with_an_empty_label():
    hub = EventHub(project_root=ROOT)

    hub.ingest_line(_hook())

    assert _sent(hub)[-1]["label"] == ""


def test_a_seeded_file_goes_out_with_an_empty_label():
    hub = EventHub(project_root=ROOT)

    hub.seed_paths(["src/app.py"])

    assert _sent(hub)[-1]["label"] == ""


def test_the_fields_already_on_the_wire_are_all_still_there():
    # Adding `label` must be additive: a page built before this change reads
    # these six keys and must keep finding them.
    hub = EventHub(project_root=ROOT)

    hub.ingest_line(_subagent_hook())

    assert LEGACY_FIELDS <= set(_sent(hub)[-1])


def test_the_existing_fields_keep_their_values():
    hub = EventHub(project_root=ROOT)

    hub.ingest_line(_subagent_hook())

    event = _sent(hub)[-1]
    assert event["type"] == "A"
    assert event["path"] == "src/app.py"
    assert event["color"] == "33FF33"
    assert event["origin"] == "hook"


# --- 2. The watcher inherits the subagent, not the session -----------------

def test_a_filesystem_change_after_a_subagent_hook_belongs_to_that_subagent():
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_subagent_hook())

    hub.ingest_fs_change("docs/generated.md", "A")

    assert _sent(hub)[-1]["agent"] == SUBAGENT_ID


def test_a_filesystem_change_inherits_the_subagent_label_too():
    # Without this the specialist's figure is nameless for every change only
    # the watcher can see -- which is most of what a busy agent does.
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_subagent_hook())

    hub.ingest_fs_change("docs/generated.md", "A")

    assert _sent(hub)[-1]["label"] == SUBAGENT_TYPE


def test_a_subagent_command_the_parser_skips_still_owns_what_follows():
    # `cp *.md docs/` yields no event by design (the parser refuses to guess a
    # glob), yet it is exactly the case attribution exists for: the watcher
    # reports the copies and they belong to this subagent, by name.
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_subagent_hook(tool_name="Bash", command="cp *.md docs/"))

    hub.ingest_fs_change("docs/readme.md", "A")

    event = _sent(hub)[-1]
    assert event["agent"] == SUBAGENT_ID
    assert event["label"] == SUBAGENT_TYPE


def test_a_filesystem_change_after_an_orchestrator_hook_stays_on_the_session():
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_hook(tool_name="Bash", command="cp *.md docs/"))

    hub.ingest_fs_change("docs/readme.md", "A")

    event = _sent(hub)[-1]
    assert event["agent"] == SESSION
    assert event["label"] == ""


def test_the_next_actor_replaces_the_previous_one():
    # Attribution is a single "who acted last" slot: once the backend agent
    # writes, changes stop belonging to the tester.
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_hook(agent_id="id-tester", agent_type="desenvolvedor-tester"))
    hub.ingest_line(
        _hook(
            file_path=f"{ROOT}/other.py",
            agent_id="id-backend",
            agent_type="desenvolvedor-backend",
        )
    )

    hub.ingest_fs_change("docs/generated.md", "A")

    event = _sent(hub)[-1]
    assert event["agent"] == "id-backend"
    assert event["label"] == "desenvolvedor-backend"


def test_an_expired_attribution_drops_the_label_with_the_agent():
    # A nameless actor is bad; a name floating over no actor is worse.
    hub = EventHub(project_root=ROOT, attribution_window=0.0)
    hub.ingest_line(_subagent_hook())

    hub.ingest_fs_change("docs/manual-edit.md", "M")

    event = _sent(hub)[-1]
    assert event["agent"] == ""
    assert event["label"] == ""


def test_an_unattributed_filesystem_change_has_neither_agent_nor_label():
    hub = EventHub(project_root=ROOT)

    hub.ingest_fs_change("docs/manual-edit.md", "M")

    event = _sent(hub)[-1]
    assert event["agent"] == ""
    assert event["label"] == ""
