"""Contract tests (RED) for the git-status panel on EventHub and Session.

Motivation: the HUD names the project and its branch, and stops there. Whether
the working tree is *dirty* -- the one question anyone watching agents edit a
checkout has -- is invisible. The graph flashes a node orange for a second when a
file is written and then forgets it, so a viewer who arrives thirty seconds after
an agent finished sees a calm tree over a working directory full of uncommitted
work, and cannot tell that from a clean one.

The daemon therefore publishes a second small frame, alongside `meta`:

    {"kind": "status", "repo": true, "truncated": false,
     "entries": [{"path": "a.txt", "state": "modified"}, ...]}

Everything that keeps `meta` from poisoning the stream applies here, and one
thing more:

  * **One replaceable slot, replayed in a fixed place.** The order becomes
    ``reset -> meta -> status -> seed -> recent``. Appended to the seed or the
    ring instead, a status republished every three seconds would grow the replay
    without bound and eventually push the project's own tree out of it.
  * **Broadcast only on a real change.** The poll runs every three seconds and
    the answer is usually byte-identical; re-sending it is pure noise on the wire
    for every connected browser.
  * **A reset drops it.** The status describes the *old* project. Surviving a
    ``ctrl+L`` switch it would caption the new graph with the previous project's
    pending changes -- and, worse, offer paths that do not exist under the new
    root, so clicking one is refused by `resolve_inside`.

`Session` owns the polling half. It must read ``self.root`` at the moment of the
call, exactly like `poll_repo`: a captured root keeps reporting the status of a
project nobody is watching, overwriting the panel a couple of seconds after every
switch. And the poll must never let two status queries overlap -- `git status` on
a large repository can take longer than the interval, and stacking them would
fork one `git` per tick forever.

The `Session` tests monkeypatch ``daemon.server.git_status``: what is specified
here is the orchestration -- which root is asked about, how often, and what
reaches the hub -- not git itself, which `tests/test_status.py` pins against a
real repository.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

import daemon.server as server
from daemon.server import EventHub, Session, start_server

ROOT = "/proj"

HOOK = json.dumps(
    {
        "session_id": "sess-xyz",
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "notes.md"},
    }
)

DIRTY = {
    "kind": "status",
    "repo": True,
    "truncated": False,
    "entries": [{"path": "a.txt", "state": "modified"}],
}

CLEAN = {"kind": "status", "repo": True, "truncated": False, "entries": []}

NO_REPO = {"kind": "status", "repo": False, "truncated": False, "entries": []}


def _sent(hub: EventHub) -> list[dict]:
    """Every message a freshly connecting client would receive, in order."""
    return [json.loads(m) for m in hub.replay_messages()]


def _statuses(hub: EventHub) -> list[dict]:
    return [m for m in _sent(hub) if m.get("kind") == "status"]


def _kinds(hub: EventHub) -> list[str]:
    return [m.get("kind", "event") for m in _sent(hub)]


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


# --- 1. The replay slot -----------------------------------------------------

def test_a_new_client_is_handed_the_current_status():
    hub = EventHub(project_root=ROOT)

    hub.set_status(DIRTY)

    assert _statuses(hub) == [DIRTY]


def test_before_any_status_there_is_none_in_the_replay():
    hub = EventHub(project_root=ROOT)
    hub.seed_paths(["src/app.py"])

    assert _statuses(hub) == []


def test_repeated_polls_leave_exactly_one_status_in_the_replay():
    # Appending instead of replacing would grow the replay every three seconds.
    hub = EventHub(project_root=ROOT)

    hub.set_status(CLEAN)
    hub.set_status(DIRTY)
    hub.set_status(NO_REPO)

    assert _statuses(hub) == [NO_REPO]


def test_the_status_follows_the_caption():
    # The panel belongs to the project the caption names; painted before it, it
    # is a list of changes with no project attached to them.
    hub = EventHub(project_root=ROOT)

    hub.set_meta("~/p", "main")
    hub.set_status(DIRTY)

    kinds = _kinds(hub)
    assert kinds.index("meta") < kinds.index("status")


def test_the_status_precedes_the_tree():
    hub = EventHub(project_root=ROOT)

    hub.set_status(DIRTY)
    hub.seed_paths(["src/app.py"])

    kinds = _kinds(hub)
    assert kinds.index("status") < kinds.index("event")


def test_a_pending_reset_still_comes_before_everything():
    # A client connecting mid-switch must clear before it is handed anything.
    hub = EventHub(project_root=ROOT)

    hub.reset("/srv/other")
    hub.set_meta("~/other", "main")
    hub.set_status(DIRTY)

    assert _kinds(hub)[0] == "reset"


def test_the_status_does_not_consume_the_recent_event_buffer():
    hub = EventHub(project_root=ROOT, buffer_size=2)
    hub.ingest_line(HOOK)

    for count in range(5):
        hub.set_status({**DIRTY, "entries": [{"path": f"f{count}.txt", "state": "added"}]})

    assert [m["path"] for m in _sent(hub) if "kind" not in m] == ["notes.md"]


def test_the_status_is_not_confusable_with_an_event():
    hub = EventHub(project_root=ROOT)
    hub.set_status(DIRTY)
    hub.ingest_line(HOOK)

    status = _statuses(hub)[0]

    assert not {"ts", "type", "agent"} & set(status)


def test_the_status_goes_on_the_wire_compactly():
    # `separators=(",", ":")`, like `set_meta`: this frame is republished for the
    # life of the session and can carry two hundred entries.
    hub = EventHub(project_root=ROOT)

    hub.set_status(DIRTY)

    raw = [m for m in hub.replay_messages() if '"status"' in m][0]
    assert raw == json.dumps(DIRTY, separators=(",", ":"))


# --- 2. Broadcasting only on a real change ----------------------------------

async def _serve():
    hub = EventHub(project_root=ROOT)
    listener = await start_server(hub, host="127.0.0.1", port=0, static_root=None)
    return hub, listener, next(iter(listener.sockets)).getsockname()[1]


class TestBroadcast:
    def test_a_status_reaches_a_client_already_on_screen(self):
        async def scenario():
            hub, listener, port = await _serve()
            async with listener, connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hub.set_status(DIRTY)
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert message == DIRTY

        _run(scenario())

    def test_a_changed_status_is_pushed_without_reconnecting(self):
        async def scenario():
            hub, listener, port = await _serve()
            async with listener, connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hub.set_status(CLEAN)
                await asyncio.wait_for(ws.recv(), timeout=5)

                hub.set_status(DIRTY)
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert message["entries"] == [{"path": "a.txt", "state": "modified"}]

        _run(scenario())

    def test_an_unchanged_status_is_not_rebroadcast(self):
        """The poll repeats the same answer every three seconds, for hours."""

        async def scenario():
            hub, listener, port = await _serve()
            async with listener, connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hub.set_status(DIRTY)
                await asyncio.wait_for(ws.recv(), timeout=5)

                hub.set_status(dict(DIRTY))  # same content, a different object
                hub.ingest_line(HOOK)  # a marker that must arrive next

                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert message.get("path") == "notes.md"

        _run(scenario())


# --- 3. A root switch drops the previous project's status -------------------

def test_the_previous_projects_status_is_gone_from_the_replay():
    # Its paths do not exist under the new root: clicking one would be refused.
    hub = EventHub(project_root=ROOT)
    hub.set_status(DIRTY)

    hub.reset("/srv/other")

    assert _statuses(hub) == []


def test_the_new_projects_status_takes_the_empty_slot():
    hub = EventHub(project_root=ROOT)
    hub.set_status(DIRTY)

    hub.reset("/srv/other")
    hub.set_status(CLEAN)

    assert _statuses(hub) == [CLEAN]


def test_a_status_identical_to_the_one_before_the_switch_is_still_published():
    # The dedupe must not outlive the reset: two projects can be dirty in exactly
    # the same way, and the second would then never be announced at all.
    hub = EventHub(project_root=ROOT)
    hub.set_status(DIRTY)

    hub.reset("/srv/other")
    hub.set_status(DIRTY)

    assert _statuses(hub) == [DIRTY]


# --- 4. Session: who asks, how often, about which root ----------------------

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


def _fake_git_status(calls: list[str], result=None, delay: float = 0.0):
    async def fake(root, *args, **kwargs):
        calls.append(str(root))
        if delay:
            await asyncio.sleep(delay)
        return result

    return fake


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=tester@example.invalid",
            "-c",
            "user.name=Tester",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _dirty_repo(root: Path) -> Path:
    """A real checkout with one uncommitted modification in it."""
    if shutil.which("git") is None:  # pragma: no cover - depends on the machine
        pytest.skip("git is not installed")
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / "a.txt").write_text("old\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    return root


def test_publishing_the_status_puts_the_working_tree_in_the_replay(
    tmp_path: Path, make_session
):
    async def scenario():
        session = make_session(_dirty_repo(tmp_path / "proj"), tmp_path)

        await session.publish_status()

        assert _statuses(session.hub)[0]["entries"] == [
            {"path": "a.txt", "state": "modified"}
        ]

    _run(scenario())


def test_a_directory_that_is_not_a_checkout_is_published_as_having_no_repository(
    tmp_path: Path, make_session
):
    # The page has to tell "no git here" from "git here, nothing pending".
    async def scenario():
        plain = tmp_path / "plain"
        plain.mkdir()
        session = make_session(plain, tmp_path)

        await session.publish_status()

        assert _statuses(session.hub) == [NO_REPO]

    _run(scenario())


def test_the_status_is_asked_about_the_root_that_is_observed_now(
    tmp_path: Path, make_session, monkeypatch: pytest.MonkeyPatch
):
    # Never a captured root: after a ctrl+L switch the panel would keep listing
    # the changes of a project nobody is watching.
    calls: list[str] = []
    monkeypatch.setattr(server, "git_status", _fake_git_status(calls), raising=False)

    async def scenario():
        first = tmp_path / "first"
        first.mkdir()
        second = tmp_path / "second"
        second.mkdir()
        session = make_session(first, tmp_path)
        session.root = str(second)

        await session.publish_status()

        assert calls == [str(second)]

    _run(scenario())


def test_the_poll_keeps_republishing_the_status(
    tmp_path: Path, make_session, monkeypatch: pytest.MonkeyPatch
):
    calls: list[str] = []
    monkeypatch.setattr(server, "git_status", _fake_git_status(calls), raising=False)

    async def scenario():
        plain = tmp_path / "plain"
        plain.mkdir()
        session = make_session(plain, tmp_path)

        poll = asyncio.create_task(session.poll_status(interval=0.01))
        await asyncio.sleep(0.15)
        poll.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll

        assert len(calls) >= 2

    _run(scenario())


def test_the_poll_never_runs_two_status_queries_at_once(
    tmp_path: Path, make_session, monkeypatch: pytest.MonkeyPatch
):
    # `git status` on a big repository outlasts the interval. Stacking rounds
    # would fork one `git` per tick until the machine gives up.
    in_flight = 0
    peak = 0

    async def slow(root, *args, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.1)
            return None
        finally:
            in_flight -= 1

    monkeypatch.setattr(server, "git_status", slow, raising=False)

    async def scenario():
        plain = tmp_path / "plain"
        plain.mkdir()
        session = make_session(plain, tmp_path)

        poll = asyncio.create_task(session.poll_status(interval=0.005))
        await asyncio.sleep(0.4)
        poll.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll

        assert peak == 1

    _run(scenario())


def test_the_poll_follows_the_root_that_is_on_screen_now(
    tmp_path: Path, make_session, monkeypatch: pytest.MonkeyPatch
):
    calls: list[str] = []
    monkeypatch.setattr(server, "git_status", _fake_git_status(calls), raising=False)

    async def scenario():
        first = tmp_path / "first"
        first.mkdir()
        second = tmp_path / "second"
        second.mkdir()
        session = make_session(first, tmp_path)

        poll = asyncio.create_task(session.poll_status(interval=0.01))
        await asyncio.sleep(0.05)
        session.root = str(second)
        await asyncio.sleep(0.1)
        poll.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll

        assert calls[-1] == str(second)

    _run(scenario())


def test_the_status_is_polled_every_three_seconds():
    # Slower than the branch poll on purpose: `git status` walks the worktree,
    # while the branch is a dozen bytes of `.git/HEAD`.
    assert getattr(server, "STATUS_POLL_INTERVAL_SECONDS", None) == 3.0


# --- 5. RHIZOME_STATUS_INTERVAL: the escape hatch -----------------------
#
# Motivation: on a large repository on a slow disk, forking `git status` every
# three seconds is the most expensive thing the daemon does, and somebody
# watching may want it rarer -- or off. The variable is set by hand in a shell,
# so every way a human mistypes one has to land somewhere sane: the daemon boots
# once, and refusing to start over a typo in an optional knob costs the whole
# session, not just the panel.

def test_without_the_variable_the_poll_runs_at_the_default_interval(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("RHIZOME_STATUS_INTERVAL", raising=False)

    assert server._status_poll_interval() == server.STATUS_POLL_INTERVAL_SECONDS


def test_a_number_in_the_variable_becomes_the_interval(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", "12.5")

    assert server._status_poll_interval() == 12.5


def test_zero_turns_the_poll_off(monkeypatch: pytest.MonkeyPatch):
    # "Off" is expressed as a non-positive interval, which `run` then refuses to
    # create a task for: a loop that only sleeps is still a loop to reason about.
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", "0")

    assert server._status_poll_interval() <= 0


def test_a_negative_interval_turns_the_poll_off_too(
    monkeypatch: pytest.MonkeyPatch,
):
    # Not clamped up to the default: a negative value is somebody disabling it.
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", "-1")

    assert server._status_poll_interval() <= 0


def test_an_empty_variable_reads_as_unset(monkeypatch: pytest.MonkeyPatch):
    # `export RHIZOME_STATUS_INTERVAL=` in a wrapper script is the common way
    # to get an empty one, and it means "I did not choose", not "off".
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", "")

    assert server._status_poll_interval() == server.STATUS_POLL_INTERVAL_SECONDS


def test_a_blank_variable_reads_as_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", "   ")

    assert server._status_poll_interval() == server.STATUS_POLL_INTERVAL_SECONDS


def test_garbage_falls_back_to_the_default_instead_of_crashing_the_boot(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", "3 seconds")

    assert server._status_poll_interval() == server.STATUS_POLL_INTERVAL_SECONDS


def test_a_number_with_surrounding_spaces_is_still_a_number(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", " 5 ")

    assert server._status_poll_interval() == 5.0


# --- 6. run(): no interval, no task -----------------------------------------

async def _boot(tmp_path: Path) -> asyncio.Task:
    """Start the daemon on a throwaway root, socket and ephemeral port."""
    root = tmp_path / "observed"
    root.mkdir(exist_ok=True)
    task = asyncio.create_task(
        server.run(str(tmp_path / "ingest.sock"), 0, str(root))
    )
    await asyncio.sleep(0.2)  # let the boot sequence get past task creation
    return task


async def _shutdown(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(task, timeout=10)


def _recording_poll_status(calls: list[float]):
    async def fake(self, interval: float = server.STATUS_POLL_INTERVAL_SECONDS):
        calls.append(interval)
        await asyncio.sleep(3600)

    return fake


def test_the_daemon_starts_no_status_poll_when_the_interval_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The whole point of `0`: not a loop that wakes up and skips, no loop.
    calls: list[float] = []
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", "0")
    monkeypatch.setattr(server.Session, "poll_status", _recording_poll_status(calls))

    async def scenario():
        task = await _boot(tmp_path)
        try:
            assert calls == []
        finally:
            await _shutdown(task)

    _run(scenario())


def test_the_daemon_polls_the_status_at_the_interval_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[float] = []
    monkeypatch.setenv("RHIZOME_STATUS_INTERVAL", "7.5")
    monkeypatch.setattr(server.Session, "poll_status", _recording_poll_status(calls))

    async def scenario():
        task = await _boot(tmp_path)
        try:
            assert calls == [7.5]
        finally:
            await _shutdown(task)

    _run(scenario())
