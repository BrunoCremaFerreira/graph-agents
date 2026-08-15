"""Contract tests (RED) for switching the observed project at runtime.

Motivation: `run()` ties the hub, the watcher, the seed scan and the branch poll
to one local variable settled at boot from ``RHIZOME_PROJECT_ROOT``. Watching
a second project therefore means killing the daemon and starting over. The pieces
underneath already exist (`rhizome_graph.paths.resolve_root`, `EventHub.reset`);
what is missing is the small object that owns them together -- `Session` -- and
performs the switch as one ordered operation:

  1. **validate first.** An unusable root must change *nothing*. Stopping the
     watcher and clearing the hub before discovering the directory does not exist
     would leave the daemon observing nowhere, with a blank page and no way back.
  2. stop the old watcher, or the abandoned project keeps pushing events into a
     graph that no longer draws it -- an inotify observer nobody can see.
  3. `hub.reset(new_root)` -- clear, and tell the browsers to clear.
  4. re-caption (`set_meta`) with the new root and *its* branch.
  5. re-seed from `scan_tree` **off the event loop**: pointed at ``~`` that walk
     takes seconds, and it would freeze every connected client while it runs.
  6. start the watcher on the new root.

The branch poll has to read the *current* root, not the one captured when the
task was created, or the caption keeps reporting the branch of the project that
was abandoned.

`switch_root` answers with the reason it refused, or ``None`` on success: the
page shows that reason next to the field, so the refusal cannot be silent.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path

import pytest

import daemon.server as server
from daemon.server import Session


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


def _project(root: Path, *files: str) -> Path:
    """A directory with some files in it, ready to be observed."""
    root.mkdir(parents=True, exist_ok=True)
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    return root


def _repo(root: Path, branch: str) -> Path:
    """Make `root` look like a git checkout sitting on `branch`."""
    git_dir = root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    return root


def _frames(session: Session) -> list[dict]:
    return [json.loads(m) for m in session.hub.replay_messages()]


def _paths(session: Session) -> list[str]:
    return [f["path"] for f in _frames(session) if "kind" not in f]


def _last(session: Session, kind: str) -> dict | None:
    matching = [f for f in _frames(session) if f.get("kind") == kind]
    return matching[-1] if matching else None


def _has_path(session: Session, path: str) -> bool:
    return path in _paths(session)


async def _eventually(predicate, timeout: float = 5.0) -> bool:
    """Await `predicate` becoming true without blocking the event loop.

    The watcher hands its changes back through `call_soon_threadsafe`, so a
    blocking wait here would keep the very callbacks it waits for from running.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.fixture
def make_session():
    """Build sessions and make sure their watcher threads are stopped."""
    made: list[Session] = []

    def _make(project_root: Path, home: Path) -> Session:
        session = Session(project_root=str(project_root), home=str(home))
        made.append(session)
        return session

    yield _make

    for session in made:
        with contextlib.suppress(Exception):
            session.stop()


# --- 1. A successful switch -------------------------------------------------

def test_the_new_projects_files_become_the_graph(tmp_path: Path, make_session):
    async def scenario():
        old = _project(tmp_path / "old", "old_only.py")
        new = _project(tmp_path / "new", "new_only.py", "lib/mod.py")
        session = make_session(old, tmp_path)

        await session.switch_root(str(new))

        assert set(_paths(session)) >= {"new_only.py", "lib/mod.py"}

    _run(scenario())


def test_no_file_of_the_abandoned_project_survives_the_switch(tmp_path: Path, make_session):
    # The replay is what a browser connecting one second later receives; a
    # leftover path there draws a file that is not in the project on screen.
    async def scenario():
        old = _project(tmp_path / "old", "old_only.py")
        new = _project(tmp_path / "new", "new_only.py")
        session = make_session(old, tmp_path)
        await session.switch_root(str(old))

        await session.switch_root(str(new))

        assert "old_only.py" not in _paths(session)

    _run(scenario())


def test_the_switch_tells_clients_to_clear_before_the_new_tree_arrives(
    tmp_path: Path, make_session
):
    async def scenario():
        old = _project(tmp_path / "old", "old_only.py")
        new = _project(tmp_path / "new", "new_only.py")
        session = make_session(old, tmp_path)

        await session.switch_root(str(new))

        assert _last(session, "reset") == {"kind": "reset", "root": str(new)}

    _run(scenario())


def test_the_caption_points_at_the_new_root(tmp_path: Path, make_session):
    # `home` is the session's, not the process's: the HUD shows "~/new".
    async def scenario():
        old = _project(tmp_path / "old")
        new = _project(tmp_path / "new")
        session = make_session(old, tmp_path)

        await session.switch_root(str(new))

        assert (_last(session, "meta") or {}).get("root") == "~/new"

    _run(scenario())


def test_the_caption_reports_the_branch_of_the_new_repository(tmp_path: Path, make_session):
    async def scenario():
        old = _repo(_project(tmp_path / "old"), "main")
        new = _repo(_project(tmp_path / "new"), "feature/x")
        session = make_session(old, tmp_path)
        await session.switch_root(str(old))

        await session.switch_root(str(new))

        assert (_last(session, "meta") or {}).get("branch") == "feature/x"

    _run(scenario())


def test_a_successful_switch_reports_no_reason(tmp_path: Path, make_session):
    async def scenario():
        new = _project(tmp_path / "new", "a.py")
        session = make_session(_project(tmp_path / "old"), tmp_path)

        assert await session.switch_root(str(new)) is None

    _run(scenario())


def test_the_current_root_becomes_the_new_one(tmp_path: Path, make_session):
    async def scenario():
        new = _project(tmp_path / "new")
        session = make_session(_project(tmp_path / "old"), tmp_path)

        await session.switch_root(str(new))

        assert session.root == str(new)

    _run(scenario())


def test_a_tilde_is_expanded_against_the_sessions_home(tmp_path: Path, make_session):
    async def scenario():
        _project(tmp_path / "typed", "there.py")
        session = make_session(_project(tmp_path / "old"), tmp_path)

        await session.switch_root("~/typed")

        assert session.root == str(tmp_path / "typed")

    _run(scenario())


# --- 2. A refused switch changes nothing ------------------------------------

def test_a_directory_that_does_not_exist_is_refused_with_a_reason(
    tmp_path: Path, make_session
):
    async def scenario():
        session = make_session(_project(tmp_path / "old", "old_only.py"), tmp_path)

        reason = await session.switch_root(str(tmp_path / "nowhere"))

        assert isinstance(reason, str) and reason.strip()

    _run(scenario())


def test_a_file_is_not_a_root(tmp_path: Path, make_session):
    # Typing a path one Tab too far lands on a file; observing one is meaningless.
    async def scenario():
        old = _project(tmp_path / "old", "old_only.py")
        session = make_session(old, tmp_path)

        reason = await session.switch_root(str(old / "old_only.py"))

        assert isinstance(reason, str) and reason.strip()

    _run(scenario())


def test_an_empty_field_is_refused_rather_than_falling_back_to_the_cwd(
    tmp_path: Path, make_session
):
    async def scenario():
        old = _project(tmp_path / "old")
        session = make_session(old, tmp_path)

        reason = await session.switch_root("   ")

        assert isinstance(reason, str) and reason.strip()
        assert session.root == str(old)

    _run(scenario())


def test_a_refused_switch_leaves_the_current_root_alone(tmp_path: Path, make_session):
    async def scenario():
        old = _project(tmp_path / "old", "old_only.py")
        session = make_session(old, tmp_path)
        await session.switch_root(str(old))

        await session.switch_root(str(tmp_path / "nowhere"))

        assert session.root == str(old)

    _run(scenario())


def test_a_refused_switch_leaves_the_graph_alone(tmp_path: Path, make_session):
    # Validation comes first precisely so a typo cannot wipe the page: no reset
    # frame, no re-seed, not one message different.
    async def scenario():
        old = _project(tmp_path / "old", "old_only.py")
        session = make_session(old, tmp_path)
        await session.switch_root(str(old))
        before = session.hub.replay_messages()

        await session.switch_root(str(tmp_path / "nowhere"))

        assert session.hub.replay_messages() == before

    _run(scenario())


# --- 3. The watcher follows the root ----------------------------------------

class TestTheWatcherFollowsTheRoot:
    """The observer is remounted, not left behind on the old directory."""

    @pytest.fixture(autouse=True)
    def _needs_watchdog(self):
        pytest.importorskip("watchdog")

    def test_a_change_in_the_new_root_reaches_the_graph(self, tmp_path: Path, make_session):
        async def scenario():
            old = _project(tmp_path / "old", "old_only.py")
            new = _project(tmp_path / "new", "new_only.py")
            session = make_session(old, tmp_path)
            await session.switch_root(str(new))

            (new / "fresh.py").write_text("x", encoding="utf-8")

            assert await _eventually(lambda: _has_path(session, "fresh.py"))

        _run(scenario())

    def test_a_change_in_the_abandoned_root_no_longer_reaches_the_graph(
        self, tmp_path: Path, make_session
    ):
        # The point of stopping the old observer: a build running in the project
        # nobody is looking at must not paint nodes into the one on screen.
        async def scenario():
            old = _project(tmp_path / "old", "old_only.py")
            new = _project(tmp_path / "new", "new_only.py")
            session = make_session(old, tmp_path)
            await session.switch_root(str(old))
            await session.switch_root(str(new))

            (old / "ghost.py").write_text("x", encoding="utf-8")
            (new / "witness.py").write_text("x", encoding="utf-8")
            await _eventually(lambda: _has_path(session, "witness.py"))

            assert not _has_path(session, "ghost.py")

        _run(scenario())

    def test_a_refused_switch_leaves_the_watcher_running(self, tmp_path: Path, make_session):
        async def scenario():
            old = _project(tmp_path / "old", "old_only.py")
            session = make_session(old, tmp_path)
            await session.switch_root(str(old))

            await session.switch_root(str(tmp_path / "nowhere"))
            (old / "still_watched.py").write_text("x", encoding="utf-8")

            assert await _eventually(lambda: _has_path(session, "still_watched.py"))

        _run(scenario())


# --- 4. The switch must not freeze the connected clients --------------------

def test_seeding_the_new_root_does_not_block_the_event_loop(
    tmp_path: Path, make_session, monkeypatch
):
    # Pointed at a home directory the walk takes seconds. Run on the loop, it
    # stops every WebSocket in the daemon for exactly that long, so the scan
    # belongs on a thread (`asyncio.to_thread`).
    def slow_scan(root, *args, **kwargs):
        time.sleep(0.3)
        return ["seeded.py"]

    monkeypatch.setattr(server, "scan_tree", slow_scan)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        new = _project(tmp_path / "new")
        session = make_session(_project(tmp_path / "old"), tmp_path)
        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)

        await session.switch_root(str(new))

        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        assert ticks >= 5

    _run(scenario())


# --- 5. The status panel is republished by the switch -----------------------

def test_the_switch_publishes_the_status_of_the_new_project(tmp_path: Path, make_session):
    # Left to the poll alone, the panel spends up to three seconds listing the
    # pending changes of the project that was just abandoned -- paths that do not
    # exist under the new root, so clicking one is refused.
    async def scenario():
        old = _project(tmp_path / "old", "old_only.py")
        new = _project(tmp_path / "new", "new_only.py")
        session = make_session(old, tmp_path)

        await session.switch_root(str(new))

        assert _last(session, "status") is not None

    _run(scenario())


def test_the_status_published_by_the_switch_is_the_new_roots(
    tmp_path: Path, make_session, monkeypatch
):
    asked: list[str] = []

    async def fake_status(root, *args, **kwargs):
        asked.append(str(root))
        return None

    monkeypatch.setattr(server, "git_status", fake_status, raising=False)

    async def scenario():
        old = _project(tmp_path / "old")
        new = _project(tmp_path / "new")
        session = make_session(old, tmp_path)

        await session.switch_root(str(new))

        assert asked and asked[-1] == str(new)

    _run(scenario())


def test_a_plain_directory_is_published_as_having_no_repository(
    tmp_path: Path, make_session
):
    async def scenario():
        old = _project(tmp_path / "old")
        new = _project(tmp_path / "new", "a.py")
        session = make_session(old, tmp_path)

        await session.switch_root(str(new))

        assert (_last(session, "status") or {}).get("repo") is False

    _run(scenario())


# --- 6. The branch poll reads the current root ------------------------------

def test_the_branch_poll_follows_the_root_that_is_on_screen_now(
    tmp_path: Path, make_session
):
    # The poll task is created at boot, like `run()` does, and outlives the
    # switch. Holding the root it was started with, it keeps re-publishing the
    # abandoned project's branch -- overwriting the caption the switch just set,
    # a couple of seconds later, with the branch of a project nobody is watching.
    async def scenario():
        old = _repo(_project(tmp_path / "old"), "main")
        new = _repo(_project(tmp_path / "new"), "feature/x")
        session = make_session(old, tmp_path)

        poll = asyncio.create_task(session.poll_repo(interval=0.01))
        try:
            await asyncio.sleep(0.05)
            await session.switch_root(str(new))
            (new / ".git" / "HEAD").write_text(
                "ref: refs/heads/hotfix\n", encoding="utf-8"
            )
            reached = await _eventually(
                lambda: (_last(session, "meta") or {}).get("branch") == "hotfix"
            )
        finally:
            poll.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll

        assert reached

    _run(scenario())
