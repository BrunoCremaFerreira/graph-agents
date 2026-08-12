"""Contract tests (RED) for EventHub seeding, attribution and de-duplication.

Three behaviours make the graph feel live instead of showing two lonely dots:

  * **Seeding** -- the existing project tree is pushed to every client, past and
    future, so the page opens on a tree rather than a blank field.
  * **Attribution** -- the watcher knows *what* changed, the hook knows *who*
    did it. A filesystem change that lands right after a hook event inherits
    that hook's agent, which is what puts an actor and its beam on screen for
    changes hooks alone cannot see (globs, compound commands).
  * **De-duplication** -- a Write fires both a hook event and a filesystem
    event. Only one must reach the browser, or every edit double-flashes.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import json

from daemon.server import EventHub

ROOT = "/proj"
SESSION = "sess-abc"


def _hook(tool_name: str = "Write", file_path: str = f"{ROOT}/src/app.py") -> str:
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


# --- 1. Seeding the existing tree ------------------------------------------

def test_seeding_makes_the_existing_tree_available_to_new_clients():
    hub = EventHub(project_root=ROOT)

    hub.seed_paths(["src/app.py", "README.md"])

    paths = [event["path"] for event in _sent(hub)]
    assert paths == ["src/app.py", "README.md"]


def test_seeded_events_are_marked_as_seed_and_carry_no_actor():
    hub = EventHub(project_root=ROOT)

    hub.seed_paths(["src/app.py"])

    event = _sent(hub)[0]
    assert event["origin"] == "seed"
    assert event["agent"] == ""
    assert event["type"] == "A"


def test_seeded_paths_count_as_known_so_a_later_write_is_a_modification():
    hub = EventHub(project_root=ROOT)
    hub.seed_paths(["src/app.py"])

    hub.ingest_line(_hook())

    assert _sent(hub)[-1]["type"] == "M"


def test_seed_replay_survives_a_full_replay_buffer():
    # The tree must never be pushed out of the replay by ordinary traffic:
    # a client connecting an hour later still needs the whole tree.
    hub = EventHub(project_root=ROOT, buffer_size=3)
    hub.seed_paths(["src/app.py"])

    for i in range(10):
        hub.ingest_line(_hook(file_path=f"{ROOT}/f{i}.py"))

    assert _sent(hub)[0]["path"] == "src/app.py"


# --- 2. Filesystem changes ---------------------------------------------------

def test_filesystem_change_is_broadcast_as_an_event():
    hub = EventHub(project_root=ROOT)

    hub.ingest_fs_change("src/new.py", "A")

    event = _sent(hub)[-1]
    assert event["path"] == "src/new.py"
    assert event["type"] == "A"
    assert event["origin"] == "watch"


def test_unattributed_filesystem_change_has_no_agent():
    hub = EventHub(project_root=ROOT)

    hub.ingest_fs_change("src/new.py", "A")

    assert _sent(hub)[-1]["agent"] == ""


def test_filesystem_change_inherits_the_agent_of_a_recent_hook_event():
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_hook(tool_name="Bash", file_path=f"{ROOT}/ignored.py"))

    hub.ingest_fs_change("docs/copied.md", "A")

    assert _sent(hub)[-1]["agent"] == SESSION


def test_attribution_expires_so_unrelated_changes_stay_anonymous():
    hub = EventHub(project_root=ROOT, attribution_window=0.0)
    hub.ingest_line(_hook())

    hub.ingest_fs_change("docs/manual-edit.md", "M")

    assert _sent(hub)[-1]["agent"] == ""


# --- 3. Hook and watcher must not double-report the same change ------------

def test_a_path_just_reported_by_a_hook_is_not_reported_again_by_the_watcher():
    hub = EventHub(project_root=ROOT)
    hub.ingest_line(_hook())

    hub.ingest_fs_change("src/app.py", "M")

    paths = [event["path"] for event in _sent(hub)]
    assert paths.count("src/app.py") == 1


def test_the_same_path_is_reported_again_once_the_dedupe_window_passes():
    hub = EventHub(project_root=ROOT, dedupe_window=0.0)
    hub.ingest_line(_hook())

    hub.ingest_fs_change("src/app.py", "M")

    paths = [event["path"] for event in _sent(hub)]
    assert paths.count("src/app.py") == 2


def test_deleting_a_directory_prunes_the_files_under_it():
    hub = EventHub(project_root=ROOT)
    hub.seed_paths(["src/a.py", "src/b.py", "other.py"])

    hub.ingest_fs_change("src", "D")

    deleted = {e["path"] for e in _sent(hub) if e["type"] == "D"}
    assert deleted == {"src/a.py", "src/b.py", "src"}


def test_a_write_reported_as_created_then_modified_flashes_once():
    # Writing a file emits both "created" and "modified" on most platforms.
    # Two events a millisecond apart are one write, and drawing both doubles
    # every flash on screen.
    hub = EventHub(project_root=ROOT)

    hub.ingest_fs_change("src/new.py", "A")
    hub.ingest_fs_change("src/new.py", "M")

    assert [e["type"] for e in _sent(hub)] == ["A"]


def test_a_later_modification_of_the_same_file_is_reported():
    hub = EventHub(project_root=ROOT, coalesce_window=0.0)
    hub.ingest_fs_change("src/new.py", "A")

    hub.ingest_fs_change("src/new.py", "M")

    assert [e["type"] for e in _sent(hub)] == ["A", "M"]


def test_a_deletion_is_never_coalesced_away():
    hub = EventHub(project_root=ROOT)
    hub.ingest_fs_change("src/new.py", "A")

    hub.ingest_fs_change("src/new.py", "D")

    assert [e["type"] for e in _sent(hub)] == ["A", "D"]


def test_a_hook_event_still_reaches_clients_when_nothing_is_deduped():
    hub = EventHub(project_root=ROOT)

    hub.ingest_line(_hook())

    assert [e["path"] for e in _sent(hub)] == ["src/app.py"]
