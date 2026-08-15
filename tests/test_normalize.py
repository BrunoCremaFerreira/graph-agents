"""Contract tests (RED) for rhizome_graph.normalize.normalize_event.

These specify the pure normalization function that maps one Claude Code hook
payload to a broadcastable Event. They are expected to FAIL until
`developer-backend` implements the function (currently a NotImplementedError
stub). Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import time

import pytest

from rhizome_graph.normalize import Event, normalize_event

# Colors fixed by the shared contract (hex, no leading '#').
COLOR_A = "33FF33"
COLOR_M = "FFAA00"
COLOR_D = "FF3333"

ROOT = "/home/user/project"
SESSION = "sess-abc123"


def _hook(tool_name: str, tool_input: dict, session_id: str = SESSION) -> dict:
    """Build a minimal well-formed PostToolUse hook payload."""
    return {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


# --- 1. Write: A vs M depending on prior existence -------------------------

def test_write_to_new_path_is_added():
    hook = _hook("Write", {"file_path": f"{ROOT}/src/app.py", "content": "x"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "A"
    assert event.path == "src/app.py"


def test_write_to_known_path_is_modified():
    hook = _hook("Write", {"file_path": f"{ROOT}/src/app.py", "content": "x"})

    event = normalize_event(
        hook, known_paths={"src/app.py"}, project_root=ROOT
    )

    assert event is not None
    assert event.type == "M"


# --- 2. Edit / MultiEdit are always modifications --------------------------

def test_edit_is_modified():
    hook = _hook("Edit", {"file_path": f"{ROOT}/src/app.py"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "M"


def test_multiedit_is_modified():
    hook = _hook("MultiEdit", {"file_path": f"{ROOT}/src/app.py"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "M"


# --- 3. Bash command parsing into filesystem ops ---------------------------

def test_bash_rm_file_is_deleted():
    hook = _hook("Bash", {"command": "rm notes.txt"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "D"
    assert event.path == "notes.txt"


def test_bash_rm_recursive_directory_is_deleted():
    hook = _hook("Bash", {"command": "rm -rf build"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "D"
    assert event.path == "build"


def test_bash_rmdir_is_deleted():
    hook = _hook("Bash", {"command": "rmdir emptydir"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "D"
    assert event.path == "emptydir"


def test_bash_mkdir_is_added():
    hook = _hook("Bash", {"command": "mkdir newdir"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "A"
    assert event.path == "newdir"


def test_bash_touch_is_added():
    hook = _hook("Bash", {"command": "touch created.txt"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "A"
    assert event.path == "created.txt"


def test_bash_cp_reports_destination_as_added():
    hook = _hook("Bash", {"command": "cp src.txt dst.txt"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "A"
    assert event.path == "dst.txt"


def test_bash_mv_reports_origin_as_deleted():
    # A single normalize_event returns the removal of the origin (the primary
    # observable change to the tree). The paired "A destination" belongs to the
    # multi-event API the developers must design (see report).
    hook = _hook("Bash", {"command": "mv old.txt new.txt"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.type == "D"
    assert event.path == "old.txt"


# --- 4. Tools that name no single concrete path produce no event -----------

@pytest.mark.parametrize("tool_name", ["Grep", "Glob", "WebFetch"])
def test_tools_that_pin_down_no_single_path_return_none(tool_name):
    # Not "everything that is not a write": `Read` used to be on this list and
    # is now an event of its own (`R`, violet -- see tests/test_read_events.py).
    # What these three still have in common is that none of them names one
    # concrete file in the observed tree: a Grep or a Glob spans a set the
    # parser cannot enumerate, and a WebFetch touches no file at all. The same
    # rule keeps `_parse_bash` silent over a glob.
    hook = _hook(tool_name, {"file_path": f"{ROOT}/src/app.py"})

    assert normalize_event(hook, known_paths=set(), project_root=ROOT) is None


# --- 5. Agent is derived from the top-level session_id ---------------------

def test_agent_is_derived_from_session_id():
    hook = _hook(
        "Write",
        {"file_path": f"{ROOT}/a.py", "content": ""},
        session_id="session-xyz",
    )

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.agent == "session-xyz"


# --- 6. Path is normalized relative to project_root ------------------------

def test_absolute_path_under_root_is_made_relative():
    hook = _hook(
        "Write", {"file_path": f"{ROOT}/deep/nested/module.py", "content": ""}
    )

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.path == "deep/nested/module.py"


# --- 7. Color matches the op type -----------------------------------------

def test_added_event_uses_green_color():
    hook = _hook("Write", {"file_path": f"{ROOT}/a.py", "content": ""})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.color == COLOR_A


def test_modified_event_uses_amber_color():
    hook = _hook("Edit", {"file_path": f"{ROOT}/a.py"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.color == COLOR_M


def test_deleted_event_uses_red_color():
    hook = _hook("Bash", {"command": "rm a.py"})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert event.color == COLOR_D


# --- 8. ts defaults to "now" (float unix seconds) --------------------------

def test_ts_defaults_to_current_unix_time():
    hook = _hook("Write", {"file_path": f"{ROOT}/a.py", "content": ""})

    before = time.time()
    event = normalize_event(hook, known_paths=set(), project_root=ROOT)
    after = time.time()

    assert event is not None
    assert isinstance(event.ts, float)
    assert before <= event.ts <= after


# --- 9. Robustness: malformed input returns None and never raises ----------

@pytest.mark.parametrize(
    "bad_input",
    [
        pytest.param({}, id="empty-dict"),
        pytest.param({"tool_input": {"file_path": "/x/a.py"}}, id="no-tool-name"),
        pytest.param({"tool_name": "Write"}, id="no-tool-input"),
        pytest.param(
            {"tool_name": "Write", "tool_input": {}}, id="write-without-file-path"
        ),
        pytest.param(
            {"tool_name": "Bash", "tool_input": {}}, id="bash-without-command"
        ),
        pytest.param(
            {"tool_name": 123, "tool_input": {"file_path": "/x/a.py"}},
            id="tool-name-wrong-type",
        ),
        pytest.param(
            {"tool_name": "Write", "tool_input": "not-a-dict"},
            id="tool-input-wrong-type",
        ),
        pytest.param(
            {"tool_name": "Bash", "tool_input": {"command": ""}},
            id="bash-empty-command",
        ),
    ],
)
def test_malformed_input_returns_none(bad_input):
    # Must never raise: normalize runs inside a hook and must fail silently.
    result = normalize_event(bad_input, known_paths=set(), project_root=ROOT)

    assert result is None


def test_returns_event_dataclass_instance_for_valid_input():
    hook = _hook("Write", {"file_path": f"{ROOT}/a.py", "content": ""})

    event = normalize_event(hook, known_paths=set(), project_root=ROOT)

    assert isinstance(event, Event)
