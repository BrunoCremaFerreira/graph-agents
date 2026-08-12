"""Contract tests (RED) for daemon.watcher.

Hooks give *authorship* but only see Claude's own file tools; they miss globs
(`cp *.md dest/`), compound commands, and every change made outside the agent.
The watcher closes that gap: it reports what actually happened on disk. The
mapping from a filesystem event to our A/M/D vocabulary is pure, so it is tested
here without spinning up an observer.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

from pathlib import Path

from daemon.watcher import FsWatcher, classify, relative_to_root


# --- 1. Filesystem event -> A / M / D --------------------------------------

def test_created_file_is_added():
    assert classify("created", is_directory=False) == "A"


def test_modified_file_is_modified():
    assert classify("modified", is_directory=False) == "M"


def test_deleted_file_is_deleted():
    assert classify("deleted", is_directory=False) == "D"


def test_directory_events_are_not_visualized_directly():
    # Directories are materialized by the frontend from their children's paths,
    # so a bare directory event carries no information the graph can use.
    assert classify("created", is_directory=True) is None
    assert classify("modified", is_directory=True) is None


def test_deleted_directory_is_reported_so_the_subtree_can_be_pruned():
    assert classify("deleted", is_directory=True) == "D"


def test_unknown_event_kind_is_dropped():
    assert classify("closed", is_directory=False) is None
    assert classify("opened", is_directory=False) is None


# --- 2. Path relativization -------------------------------------------------

def test_relative_to_root_strips_the_project_root():
    assert relative_to_root("/proj/src/app.py", "/proj") == "src/app.py"


def test_path_outside_the_root_is_rejected():
    assert relative_to_root("/elsewhere/app.py", "/proj") is None


def test_ignored_paths_are_rejected():
    assert relative_to_root("/proj/node_modules/three/x.js", "/proj") is None
    assert relative_to_root("/proj/.git/HEAD", "/proj") is None


def test_the_root_itself_is_rejected():
    assert relative_to_root("/proj", "/proj") is None


# --- 3. The observer wrapper reports real changes --------------------------

def test_watcher_reports_a_file_created_under_the_root(tmp_path: Path):
    seen: list[tuple[str, str]] = []
    watcher = FsWatcher(str(tmp_path), lambda path, op: seen.append((path, op)))

    watcher.start()
    try:
        (tmp_path / "new.py").write_text("x")
        watcher.wait_for(lambda: any(p == "new.py" for p, _ in seen), timeout=5.0)
    finally:
        watcher.stop()

    assert ("new.py", "A") in seen


def test_watcher_ignores_changes_inside_ignored_directories(tmp_path: Path):
    seen: list[tuple[str, str]] = []
    (tmp_path / "node_modules").mkdir()
    watcher = FsWatcher(str(tmp_path), lambda path, op: seen.append((path, op)))

    watcher.start()
    try:
        (tmp_path / "node_modules" / "junk.js").write_text("x")
        (tmp_path / "real.py").write_text("x")
        watcher.wait_for(lambda: any(p == "real.py" for p, _ in seen), timeout=5.0)
    finally:
        watcher.stop()

    assert not any(p.startswith("node_modules") for p, _ in seen)


def test_stop_is_safe_without_start(tmp_path: Path):
    # A daemon shutting down before the observer ever came up must not raise.
    FsWatcher(str(tmp_path), lambda path, op: None).stop()


def test_missing_root_does_not_raise_on_start(tmp_path: Path):
    watcher = FsWatcher(str(tmp_path / "nope"), lambda path, op: None)
    watcher.start()
    watcher.stop()
