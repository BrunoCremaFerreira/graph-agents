"""Contract tests (RED) for the fourth event type: `R`, a file being read.

Motivation: watching agents work, the graph only lights up when something
*changes*. Most of what an agent actually does is read -- it opens six files,
thinks, and edits one -- so the screen stays dark through the part of the work
that explains the edit when it finally lands. `R` puts that on camera, in
violet (`AA66FF`), alongside A/M/D.

The rule everything else follows from is that **a read is not a change**: it
travels the same wire but must never touch the shared state A/M/D mutate. The
half of that rule which lives here is narrower and just as load-bearing:

  * **A read outside the observed root yields nothing.** Agents read `/etc`,
    `~/.claude`, `node_modules` and other checkouts constantly, and
    `_make_relative` hands an absolute path that is not under the root straight
    back, unchanged. Fed to the graph, that becomes a permanent node hanging off
    the top of the tree named `/etc/hosts` -- and unlike a bad Write, no watcher
    event will ever come along to correct it, because nothing changed on disk.
    The prefix comparison has to be on a path boundary, or a sibling directory
    whose name merely starts with the root's characters (`/home/x/project-other`
    next to `/home/x/proj`) is misfiled *inside* the tree.

Nothing about A/M/D moves; the last test here is the guard on that.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import pytest

from graphagents.normalize import normalize_event

#: Violet, in the same shape as the existing `_COLOR_BY_TYPE` values.
COLOR_R = "AA66FF"

ROOT = "/home/user/project"
SESSION = "sess-abc123"
SUBAGENT_ID = "a747fec535c143044"
SUBAGENT_TYPE = "developer-backend"


def _read(file_path: object, **extra: object) -> dict:
    """A PostToolUse payload for the Read tool, as Claude Code delivers it."""
    payload: dict = {
        "session_id": SESSION,
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
    }
    payload.update(extra)
    return payload


def _event(payload: dict, root: str | None = ROOT):
    return normalize_event(payload, known_paths=set(), project_root=root)


# --- 1. A read is an event of its own ---------------------------------------

def test_reading_a_file_yields_a_read_event():
    event = _event(_read(f"{ROOT}/src/app.py"))

    assert event is not None
    assert event.type == "R"


def test_a_read_is_violet():
    event = _event(_read(f"{ROOT}/src/app.py"))

    assert event is not None
    assert event.color == COLOR_R


def test_a_read_comes_from_the_hook():
    event = _event(_read(f"{ROOT}/src/app.py"))

    assert event is not None
    assert event.origin == "hook"


def test_a_read_carries_the_path_relative_to_the_project_root():
    # Exactly what Write does: the graph's tree is rooted at the project.
    event = _event(_read(f"{ROOT}/src/app.py"))

    assert event is not None
    assert event.path == "src/app.py"


def test_a_read_is_attributed_to_the_session_that_made_it():
    event = _event(_read(f"{ROOT}/src/app.py"))

    assert event is not None
    assert event.agent == SESSION


def test_a_subagent_reading_is_attributed_to_the_subagent_not_the_session():
    # `actor_of`, unchanged: identity is the agent_id when there is one.
    event = _event(
        _read(
            f"{ROOT}/src/app.py",
            agent_id=SUBAGENT_ID,
            agent_type=SUBAGENT_TYPE,
        )
    )

    assert event is not None
    assert event.agent == SUBAGENT_ID


def test_a_subagent_read_carries_the_agent_type_as_its_label():
    event = _event(
        _read(
            f"{ROOT}/src/app.py",
            agent_id=SUBAGENT_ID,
            agent_type=SUBAGENT_TYPE,
        )
    )

    assert event is not None
    assert event.label == SUBAGENT_TYPE


# --- 2. Reads outside the observed root are not part of this tree -----------

def test_a_read_outside_the_project_root_yields_nothing():
    # The one that matters: an agent reads /etc/hosts and the graph would grow a
    # node no watcher event can ever remove.
    assert _event(_read("/etc/hosts")) is None


def test_a_read_of_another_checkout_yields_nothing():
    assert _event(_read("/home/user/other-project/src/main.rs")) is None


def test_a_read_of_a_sibling_whose_name_starts_like_the_root_yields_nothing():
    # A pure string prefix test misfiles this one *inside* the tree: the
    # comparison has to land on a path boundary.
    assert _event(_read("/home/user/project-other/a.py"), root="/home/user/project") is None


def test_a_relative_read_path_is_kept_as_it_is():
    event = _event(_read("src/app.py"))

    assert event is not None
    assert event.path == "src/app.py"


def test_a_read_of_the_root_itself_is_not_a_file_in_the_tree():
    # `_make_relative` cannot express it as a relative path, and the tree has no
    # node for its own root.
    assert _event(_read(ROOT)) is None


# --- 2b. ... and neither is a path that walks back out of it ----------------
#
# Same defect as above, second door. Refusing an absolute path that does not
# start with the root closes only half of it, because a path can start inside
# the root and leave: `/home/user/project/../other/a.py` satisfies the prefix
# test and comes out as `../other/a.py`, which is the junk node the whole rule
# exists to prevent -- now with a name that sorts above everything. A plain
# relative `../other/a.py` is the same node by the shorter road.
#
# The guard against over-correcting sits right below it: `..` in a path is not
# by itself an escape, and neither is a dot in a file name.


@pytest.mark.parametrize(
    "file_path",
    [
        "../other/a.py",
        "../../etc/hosts",
        "src/../../other/a.py",
        f"{ROOT}/../other/a.py",
        f"{ROOT}/src/../../other/a.py",
    ],
    ids=[
        "relative-parent",
        "relative-two-parents",
        "relative-in-then-out",
        "absolute-walks-out",
        "absolute-in-then-out",
    ],
)
def test_a_read_that_escapes_the_root_through_a_parent_segment_yields_nothing(
    file_path: str,
):
    assert _event(_read(file_path)) is None


@pytest.mark.parametrize(
    "file_path",
    [
        "src/../a.py",
        f"{ROOT}/src/../a.py",
        "..hidden.py",
        "a..b.py",
        f"{ROOT}/src/..hidden.py",
    ],
    ids=[
        "relative-resolves-inside",
        "absolute-resolves-inside",
        "leading-dots-in-a-name",
        "dots-inside-a-name",
        "absolute-leading-dots-in-a-name",
    ],
)
def test_a_read_that_stays_inside_the_root_is_kept_however_it_is_written(
    file_path: str,
):
    # Only "not refused" is asserted: whether the path is normalized on the way
    # through or kept verbatim is the implementer's call, and pinning one shape
    # here would pin it for no reason.
    assert _event(_read(file_path)) is not None


# --- 3. Garbage never raises, and never draws -------------------------------

def test_a_read_with_no_file_path_yields_nothing():
    payload = _read("x")
    payload["tool_input"] = {}

    assert _event(payload) is None


def test_a_read_with_a_blank_file_path_yields_nothing():
    assert _event(_read("")) is None


def test_a_read_with_a_whitespace_file_path_yields_nothing():
    assert _event(_read("   ")) is None


def test_a_read_with_a_non_string_file_path_yields_nothing():
    assert _event(_read(42)) is None


def test_a_read_with_a_structured_file_path_yields_nothing_and_does_not_raise():
    assert _event(_read({"path": f"{ROOT}/src/app.py"})) is None


# --- 4. Guard: A/M/D are untouched ------------------------------------------

def test_a_write_to_a_new_path_is_still_an_add():
    payload = {
        "session_id": SESSION,
        "tool_name": "Write",
        "tool_input": {"file_path": f"{ROOT}/src/app.py", "content": "x"},
    }

    event = normalize_event(payload, known_paths=set(), project_root=ROOT)

    assert event is not None
    assert (event.type, event.color) == ("A", "33FF33")


def test_a_write_to_a_known_path_is_still_a_modification():
    payload = {
        "session_id": SESSION,
        "tool_name": "Write",
        "tool_input": {"file_path": f"{ROOT}/src/app.py", "content": "x"},
    }

    event = normalize_event(
        payload, known_paths={"src/app.py"}, project_root=ROOT
    )

    assert event is not None
    assert (event.type, event.color) == ("M", "FFAA00")
