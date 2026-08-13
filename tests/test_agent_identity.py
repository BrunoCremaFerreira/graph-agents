"""Contract tests (RED) for per-subagent identity and its on-screen name.

Motivation, from real ``PostToolUse`` payloads captured on Claude Code 2.1.229:

  * a tool call made by the **orchestrator** carries ``session_id`` and nothing
    else -- the keys ``agent_id`` / ``agent_type`` simply do not exist;
  * a tool call made by a **subagent** carries the *same* ``session_id`` plus
    ``agent_id`` (e.g. ``"a747fec535c143044"``) and ``agent_type`` (e.g.
    ``"desenvolvedor-backend"``).

``normalize_event`` currently derives the actor from ``session_id`` alone, so
every subagent of a session collapses into one figure: five specialists working
in parallel draw as a single actor. The fix is two separate notions:

  * ``agent`` -- the **identity**, an opaque id. It is what splits two
    specialists into two figures, so it must come from ``agent_id`` when there
    is one.
  * ``label`` -- the **readable name** (``agent_type``), which is what a viewer
    reads on screen. It is presentation only and must never take part in
    identity, or the same subagent would fork into two actors the moment its
    label changed.

The project's hard rule still holds at both ends: garbage never invents an
actor, and an event with ``agent: ""`` must never create one.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import pytest

from graphagents.normalize import Event, fs_event, normalize_event, seed_event

ROOT = "/home/user/project"
SESSION = "sess-abc123"
SUBAGENT_ID = "a747fec535c143044"
SUBAGENT_TYPE = "desenvolvedor-backend"


def _orchestrator_hook(file_path: str = f"{ROOT}/src/app.py") -> dict:
    """What the hook receives when the main session calls a tool."""
    return {
        "session_id": SESSION,
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": file_path},
    }


def _subagent_hook(
    agent_id: object = SUBAGENT_ID,
    agent_type: object = SUBAGENT_TYPE,
    file_path: str = f"{ROOT}/src/app.py",
) -> dict:
    """What the hook receives when a Task subagent calls a tool."""
    payload = _orchestrator_hook(file_path)
    payload["agent_id"] = agent_id
    payload["agent_type"] = agent_type
    return payload


# --- 1. Identity: agent_id wins over session_id ----------------------------

def test_subagent_payload_is_attributed_to_its_agent_id():
    event = normalize_event(
        _subagent_hook(), known_paths=set(), project_root=ROOT
    )

    assert event is not None
    assert event.agent == SUBAGENT_ID


def test_orchestrator_payload_is_still_attributed_to_the_session_id():
    event = normalize_event(
        _orchestrator_hook(), known_paths=set(), project_root=ROOT
    )

    assert event is not None
    assert event.agent == SESSION


def test_two_subagents_of_one_session_are_two_distinct_actors():
    # The whole point of the feature: same session_id, different agent_id, and
    # the graph must draw two figures rather than one.
    tester = normalize_event(
        _subagent_hook(agent_id="id-tester", agent_type="desenvolvedor-tester"),
        known_paths=set(),
        project_root=ROOT,
    )
    backend = normalize_event(
        _subagent_hook(agent_id="id-backend", agent_type="desenvolvedor-backend"),
        known_paths=set(),
        project_root=ROOT,
    )

    assert tester is not None and backend is not None
    assert tester.agent != backend.agent


@pytest.mark.parametrize(
    "agent_id",
    [
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param(12345, id="number"),
        pytest.param({"id": "x"}, id="dict"),
        pytest.param(None, id="null"),
        pytest.param(["x"], id="list"),
    ],
)
def test_unusable_agent_id_falls_back_to_the_session_id(agent_id):
    # A junk agent_id must not become an actor of its own; the session is still
    # a truthful attribution.
    event = normalize_event(
        _subagent_hook(agent_id=agent_id), known_paths=set(), project_root=ROOT
    )

    assert event is not None
    assert event.agent == SESSION


def test_payload_with_no_usable_identity_at_all_has_an_empty_agent():
    # Hard project rule: agent == "" is legal and means "nobody did this on
    # camera"; inventing an id here would put a phantom figure on screen.
    payload = _orchestrator_hook()
    del payload["session_id"]

    event = normalize_event(payload, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.agent == ""


# --- 2. Label: the readable name, never the identity -----------------------

def test_event_label_defaults_to_empty():
    event = Event(ts=1.0, agent="a", type="A", path="a.py", color="33FF33")

    assert event.label == ""


def test_subagent_label_is_its_agent_type():
    event = normalize_event(
        _subagent_hook(), known_paths=set(), project_root=ROOT
    )

    assert event is not None
    assert event.label == SUBAGENT_TYPE


def test_orchestrator_event_carries_no_label():
    event = normalize_event(
        _orchestrator_hook(), known_paths=set(), project_root=ROOT
    )

    assert event is not None
    assert event.label == ""


@pytest.mark.parametrize(
    "agent_type",
    [
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param(7, id="number"),
        pytest.param({"type": "x"}, id="dict"),
        pytest.param(None, id="null"),
    ],
)
def test_unusable_agent_type_leaves_the_label_empty(agent_type):
    event = normalize_event(
        _subagent_hook(agent_type=agent_type),
        known_paths=set(),
        project_root=ROOT,
    )

    assert event is not None
    assert event.label == ""


def test_a_bad_agent_type_does_not_cost_the_identity():
    # Presentation failing must not degrade attribution: the figure is still
    # the subagent's, just unnamed.
    event = normalize_event(
        _subagent_hook(agent_type=None), known_paths=set(), project_root=ROOT
    )

    assert event is not None
    assert event.agent == SUBAGENT_ID


def test_the_label_is_not_part_of_the_identity():
    # Same subagent, renamed type: still one actor, or a rename would fork the
    # figure in two mid-session.
    first = normalize_event(
        _subagent_hook(agent_type="desenvolvedor-backend"),
        known_paths=set(),
        project_root=ROOT,
    )
    second = normalize_event(
        _subagent_hook(agent_type="general-purpose"),
        known_paths=set(),
        project_root=ROOT,
    )

    assert first is not None and second is not None
    assert first.agent == second.agent


# --- 3. Events nobody signed ------------------------------------------------

def test_seed_event_carries_no_label():
    # A seeded file was there before anyone connected: no actor, no name.
    event = seed_event("src/app.py", ts=1.0)

    assert event.label == ""


def test_fs_event_carries_no_label_of_its_own():
    # The watcher sees a change on disk and has no idea which agent type made
    # it; any name it gets is supplied by the daemon's attribution.
    event = fs_event("src/app.py", "M", agent=SUBAGENT_ID, ts=1.0)

    assert event is not None
    assert event.label == ""
