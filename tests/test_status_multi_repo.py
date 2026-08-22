"""Contract tests (RED) for the multi-repository branch of rhizome_graph.status.

Motivation, and it is a silence rather than a wrong answer: `git_status` asks one
question and gets one answer. `find_checkout_root(root)` walks *upward*, so a
workspace root of the `~/projects/{a,b,c}` shape -- no `.git` at or above it, five
of them directly below -- returns `None` before forking anything, and the panel
that exists to say "there is uncommitted work here" is not on screen at all. That
reads exactly like a clean tree, which is the one thing it must never be confused
with. Everything after the early return -- the fork, the parse, the relativize --
is reachable only in the single-repo case.

This file is R2 of `docs/features/todo/multi-repo-git-status.md`. It is a **new
file, deliberately**: `tests/test_status.py`'s 62 assertions stay byte-identical,
so a reviewer can see that nothing about today's single-repo behaviour moved.

Five properties are what these tests are really about, because they are the ones
a later change is most likely to break:

  * **Upward wins, structurally.** If the root is a checkout, or sits inside one,
    the downward walk does not happen at all -- not "happens and is discarded".
    That is what makes backwards compatibility a shape rather than a list of
    regression tests, and it is pinned with a spy that must record *zero* calls.
  * **A map, not a filter.** `relativize` strips a prefix and drops what falls
    outside; the multi-repo case is the inverse and prepends. They are two moods
    of one idea and are deliberately two functions, because one function with two
    moods is a function whose tests you have to re-read to know which mood each
    one pins.
  * **Fairness is the whole point of the interleave.** `status_frame` cuts the
    head at 200. Over a list ordered repo-by-repo, one repository with 300
    untracked files fills the entire cut and hides every other repository --
    which is this feature's own failure mode, moved one level up. Round-robin
    makes the *existing* cut fair with no new constant and no signature change.
  * **The walk precedes forks, so it is bounded and it is off the loop.** Four
    concurrent `git status` calls at a 5 s timeout each is the ceiling on a poll
    round; and discovery itself is filesystem work, which on the loop's own
    thread would freeze every connected browser for as long as a network mount
    feels like taking.
  * **The per-repository cap must not silence `truncated`.** It is a memory
    bound and nothing else, so it is set one entry *above* what a frame can
    show. Cut to exactly `DEFAULT_MAX_ENTRIES`, a lone sub-repository of 300
    pending changes would make `status_frame`'s `len(entries) > len(shown)`
    answer `False` and the panel would call a cut list complete -- while the
    same repository observed directly reports it cut. The frame must make one
    claim about completeness, not two decided by where the root is pointed.
  * **The semaphore is created inside the call.** A module-level
    `asyncio.Semaphore` binds to the first loop that blocks on it and raises on
    every loop after that. It passes every single-loop functional test and fails
    the second time a daemon's loop is created, so there is a test here whose only
    job is to run the same call under two successive loops.

Real repositories under `tmp_path` wherever the behaviour is "what `git` says",
following `tests/test_status.py`'s own `_repo` idiom. Where the behaviour under
test is orchestration -- concurrency, budgets, ordering -- `run_git` is stubbed
instead, because a fork tells you nothing about how many forks were in flight.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from rhizome_graph import status
from rhizome_graph.status import (
    DEFAULT_MAX_ENTRIES,
    StatusEntry,
    git_status,
    status_frame,
)

# `prefix_entries`, `interleave` and `MAX_CONCURRENT_STATUS` are reached through
# the module rather than imported by name on purpose: none of them exists yet,
# and a failing `from ... import` at the top would collapse this whole file into
# one collection error, hiding which steps already pass (2.4 does) from the very
# first run.


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


def _require_git() -> None:
    if shutil.which("git") is None:  # pragma: no cover - depends on the machine
        pytest.skip("git is not installed")


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


def _repo(root: Path, **files: str) -> Path:
    """A real repository with `files` committed on HEAD."""
    _require_git()
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _make_checkout(path: Path) -> Path:
    """A directory that *looks* like the top of a working tree, without `git`.

    Enough for discovery to find it, and no more. Used only by the tests that
    stub `run_git`, where a real repository would buy nothing and cost an `init`.
    """
    git_dir = path / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return path


def _z(*records: str) -> str:
    """`records` as `git status -z` writes them: each terminated by a NUL."""
    return "".join(record + "\0" for record in records)


def _untracked(count: int, first: int = 0) -> str:
    """`count` untracked records, as one `git status -z` stdout."""
    return _z(*(f"?? f{index:04d}.txt" for index in range(first, first + count)))


def _reported(entries) -> list:
    """`entries`, with `None` read as "nothing was reported at all".

    Only so that a missing multi-repo answer fails as the rows it did not
    contain, instead of as a `TypeError` raised inside a helper three frames
    away. The `None` versus `[]` distinction is a property in its own right and
    has a test of its own, which does not go through here.
    """
    return list(entries or [])


def _pairs(entries) -> list[tuple[str, str]]:
    return [(e.path, e.state) for e in _reported(entries)]


def _states(entries) -> dict[str, str]:
    return {e.path: e.state for e in _reported(entries)}


def _repositories(entries) -> dict[str, str]:
    """Each reported path against the checkout the entry says it came from."""
    return {e.path: e.repo for e in _reported(entries)}


def _paths(entries) -> list[str]:
    return sorted(e.path for e in _reported(entries))


def _leading(path: str) -> str:
    """The first path segment -- which sub-repository a prefixed row belongs to."""
    return path.split("/", 1)[0]


def _by_repository(paths) -> dict[str, int]:
    """How many of `paths` each sub-repository contributed."""
    counts: dict[str, int] = {}
    for path in paths:
        counts[_leading(path)] = counts.get(_leading(path), 0) + 1
    return counts


def _entry(path: str, state: str = "modified") -> StatusEntry:
    return StatusEntry(path=path, state=state)


# --- 2.1 prefix_entries: the map that `relativize` deliberately is not -------

def test_a_sub_repository_entry_gains_the_prefix_of_its_checkout(tmp_path: Path):
    """An entry from a sub-repo is re-expressed relative to the observed root.

    `git` reports `src/x` because that is where the file sits in *its own*
    checkout. The graph, `resolve_inside` and every node on screen speak in
    paths relative to the observed root, where the same file is `a/src/x`.
    """
    entries = [StatusEntry("src/x", "modified")]

    assert _pairs(status.prefix_entries(entries, "a")) == [("a/src/x", "modified")]


def test_the_state_is_carried_through_the_prefixing_untouched():
    """Prefixing is about the path and nothing else."""
    entries = [
        StatusEntry("one.txt", "untracked"),
        StatusEntry("two.txt", "deleted"),
        StatusEntry("three.txt", "added"),
    ]

    assert _states(status.prefix_entries(entries, "a")) == {
        "a/one.txt": "untracked",
        "a/two.txt": "deleted",
        "a/three.txt": "added",
    }


def test_a_deep_prefix_is_prepended_whole():
    """`~/src/github.com/org/repo` is a layout people use; depth 3 must work."""
    entries = [StatusEntry("src/app.ts", "modified")]

    assert _pairs(status.prefix_entries(entries, "github.com/org/repo")) == [
        ("github.com/org/repo/src/app.ts", "modified")
    ]


def test_the_prefixed_entry_records_which_checkout_it_came_from():
    """The repo boundary is not derivable from a flat path, so it is recorded.

    `a/b/c.ts` may belong to checkout `a` or to checkout `a/b`; only the daemon
    knows which, and only at the moment it prefixes the entry.
    """
    prefixed = status.prefix_entries([StatusEntry("src/x", "modified")], "a")

    assert [entry.repo for entry in prefixed] == ["a"]


def test_the_entries_handed_to_prefix_entries_are_not_mutated():
    """The caller keeps its originals; a new list of new entries comes back."""
    entries = [StatusEntry("src/x", "modified")]

    result = status.prefix_entries(entries, "a")

    assert _pairs(entries) == [("src/x", "modified")]
    assert result[0] is not entries[0]


def test_the_empty_prefix_leaves_every_path_exactly_as_it_was():
    """`[""]` is what discovery answers for a root that is itself a checkout.

    Joining an empty prefix must be the identity and not produce `/src/x`, which
    would be an absolute path and match no node at all.
    """
    entries = [StatusEntry("src/x", "modified"), StatusEntry("a.txt", "added")]

    assert _pairs(status.prefix_entries(entries, "")) == [
        ("src/x", "modified"),
        ("a.txt", "added"),
    ]


def test_prefixing_nothing_yields_nothing():
    """A clean sub-repository contributes an empty group, never an exception."""
    assert status.prefix_entries([], "a") == []


def test_a_status_entry_remembers_a_repository_and_defaults_to_none():
    """`repo` is defaulted, so every existing construction still type-checks.

    `tests/test_status.py` builds `StatusEntry(path=..., state=...)` sixty-odd
    times and pins the pair as frozen; a required third field would rewrite all
    of it for a value only the multi-repo branch has.
    """
    assert StatusEntry(path="a.txt", state="modified").repo == ""


def test_a_status_entry_with_a_repository_is_still_frozen():
    """Still handed around and cached between polls; still must not mutate."""
    entry = StatusEntry(path="a/x.txt", state="modified", repo="a")

    with pytest.raises(Exception):
        entry.repo = "b"  # type: ignore[misc]


# --- 2.2 interleave: fairness, made structural ------------------------------

def test_groups_are_taken_round_robin_one_entry_at_a_time():
    """The head-cut at 200 is only fair if the list is fair before it.

    Ordered repo-by-repo, one repository with 300 untracked files fills the whole
    cut and hides every other one -- this feature's own failure mode, moved one
    level up.
    """
    a1, a2, a3 = _entry("a/1"), _entry("a/2"), _entry("a/3")
    b1 = _entry("b/1")
    c1, c2 = _entry("c/1"), _entry("c/2")

    assert status.interleave([[a1, a2, a3], [b1], [c1, c2]]) == [
        a1,
        b1,
        c1,
        a2,
        c2,
        a3,
    ]


def test_a_group_that_runs_out_simply_stops_contributing():
    """Exhaustion is not padding: no placeholder, no hole, no repetition."""
    a1, a2 = _entry("a/1"), _entry("a/2")
    b1 = _entry("b/1")

    assert status.interleave([[a1, a2], [b1]]) == [a1, b1, a2]


def test_an_empty_group_vanishes_rather_than_shifting_the_rotation():
    """A clean repository among dirty ones costs nothing and skews nothing."""
    a1 = _entry("a/1")
    b1 = _entry("b/1")

    assert status.interleave([[a1], [], [b1]]) == [a1, b1]


def test_a_single_group_comes_back_in_its_own_order():
    """The single-repo invariant: with one checkout, the interleave is identity.

    `git` orders its own output, `tests/test_status.py` pins that order, and one
    repository must not be reordered on the way through a function that exists
    for the case where there are several.
    """
    entries = [_entry("src/a.ts"), _entry("src/b.ts"), _entry("c.txt")]

    assert status.interleave([entries]) == entries


def test_interleaving_no_groups_at_all_is_an_empty_list():
    """`[]` is an answer here, and never `None`: the caller goes on to frame it."""
    assert status.interleave([]) == []


def test_interleaving_only_empty_groups_is_an_empty_list():
    """Several checkouts, all clean, is `[]` -- "clean", not "no repository"."""
    assert status.interleave([[], []]) == []


def test_the_entries_themselves_are_passed_through_untouched():
    """A rearrangement, not a rebuild: the objects out are the objects in."""
    a1 = _entry("a/1")
    b1 = _entry("b/1")

    result = status.interleave([[a1], [b1]])

    assert result[0] is a1 and result[1] is b1


# --- 2.3 git_status over a container of checkouts ---------------------------

def test_a_container_of_two_checkouts_reports_both_of_them(tmp_path: Path):
    """The feature: a workspace root answers for every checkout below it.

    Today `find_checkout_root` comes back empty for the container and the whole
    function returns `None` before forking anything.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    repo_a = _repo(container / "a", **{"x.txt": "old\n"})
    repo_b = _repo(container / "b", **{"y.txt": "old\n"})
    (repo_a / "x.txt").write_text("changed\n", encoding="utf-8")
    (repo_b / "y.txt").write_text("changed\n", encoding="utf-8")

    assert _states(_run(git_status(str(container)))) == {
        "a/x.txt": "modified",
        "b/y.txt": "modified",
    }


def test_a_path_deep_inside_a_sub_checkout_stays_relative_to_the_container(
    tmp_path: Path,
):
    """Prefixed paths are still paths: `statusList.ts` sorts and draws them.

    That is why phase 1 needs no frontend change -- but only if the path is the
    one the graph would draw, from the observed root down.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    repo_a = _repo(container / "a", **{"src/app.ts": "old\n"})
    (repo_a / "src" / "app.ts").write_text("changed\n", encoding="utf-8")

    assert _states(_run(git_status(str(container)))) == {"a/src/app.ts": "modified"}


def test_every_state_survives_the_trip_out_of_a_sub_checkout(tmp_path: Path):
    """The four states are the panel's colours, and they cross unchanged."""
    container = tmp_path / "workspace"
    container.mkdir()
    repo_a = _repo(container / "a", **{"m.txt": "old\n", "d.txt": "gone\n"})
    (repo_a / "m.txt").write_text("changed\n", encoding="utf-8")
    (repo_a / "d.txt").unlink()
    (repo_a / "new.py").write_text("new\n", encoding="utf-8")
    _git(repo_a, "add", "new.py")
    (repo_a / "loose.txt").write_text("loose\n", encoding="utf-8")

    assert _states(_run(git_status(str(container)))) == {
        "a/m.txt": "modified",
        "a/d.txt": "deleted",
        "a/new.py": "added",
        "a/loose.txt": "untracked",
    }


def test_each_reported_entry_names_the_checkout_that_produced_it(tmp_path: Path):
    """Evidence that the fallback prefixes through `prefix_entries`.

    Which repository a row belongs to cannot be recovered from `a/b/c.ts` later:
    it may be checkout `a` or checkout `a/b`. If the daemon does not record it
    here, nothing downstream can.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    repo_a = _repo(container / "a", **{"x.txt": "old\n"})
    repo_b = _repo(container / "b", **{"y.txt": "old\n"})
    (repo_a / "x.txt").write_text("changed\n", encoding="utf-8")
    (repo_b / "y.txt").write_text("changed\n", encoding="utf-8")

    assert _repositories(_run(git_status(str(container)))) == {
        "a/x.txt": "a",
        "b/y.txt": "b",
    }


def test_a_container_whose_checkouts_are_all_clean_is_clean_not_absent(
    tmp_path: Path,
):
    """`[]` and `None` are different answers and the page renders them apart.

    Redefining `repo: true` as "at least one checkout is described by this frame"
    is what keeps that distinction meaningful over a container.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    _repo(container / "a", **{"x.txt": "old\n"})
    _repo(container / "b", **{"y.txt": "old\n"})

    assert _run(git_status(str(container))) == []


def test_a_checkout_two_levels_below_the_container_is_reported_with_both_segments(
    tmp_path: Path,
):
    """The `org/repo` layout, end to end through the real thing."""
    container = tmp_path / "workspace"
    container.mkdir()
    nested = _repo(container / "org" / "repo", **{"x.txt": "old\n"})
    (nested / "x.txt").write_text("changed\n", encoding="utf-8")

    assert _states(_run(git_status(str(container)))) == {"org/repo/x.txt": "modified"}


# --- 2.4 the guard: discovery gates the fallback, it is not run blind -------
#
# Re-asserted verbatim from `tests/test_status.py`, deliberately and with its
# name unchanged so the two can be grepped together. It must pass *before* the
# fallback is written and *after*: it is what pins that the new branch is gated
# on discovery finding something, rather than forking `git` at whatever the root
# happens to be. The original stays where it is; nothing there moves.

def test_a_directory_outside_any_repository_does_not_even_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # This runs on a timer. Forking `git` every few seconds to be told "not a
    # repository" is pure waste, and `find_checkout_root` answers it from disk.
    plain = tmp_path / "plain"
    plain.mkdir()
    forked: list[str] = []

    def boom(*args, **kwargs):
        forked.append("yes")
        raise AssertionError("git must not be spawned outside a repository")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    assert _run(git_status(str(plain))) is None
    assert forked == []


def test_a_container_holding_only_plain_directories_still_forks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Discovery answering `[]` must end the round, not start a blind fork.

    The difference from the test above is that here the walk has real work to do
    and still finds nothing -- which is the ordinary state of most directories a
    viewer will ever point `ctrl+L` at.
    """
    container = tmp_path / "workspace"
    (container / "docs" / "notes").mkdir(parents=True)
    (container / "build").mkdir()
    forked: list[str] = []

    def boom(*args, **kwargs):
        forked.append("yes")
        raise AssertionError("git must not be spawned when no checkout was found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    assert _run(git_status(str(container))) is None
    assert forked == []


# --- 2.5 upward wins: the downward walk is never even attempted -------------

def test_a_root_that_is_a_checkout_never_asks_what_is_below_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Backwards compatibility as a shape, not as a list of regression tests.

    A repository that *contains* vendored checkouts keeps its single-repo panel,
    because the downward walk does not happen -- not because its results are
    discarded afterwards. The status assertion is here so that a `git_status`
    which returned early for some unrelated reason could not pass this by
    accident.
    """
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    asked: list[str] = []

    real_find_checkouts = status.find_checkouts

    def spy(*args, **kwargs):
        asked.append("yes")
        return real_find_checkouts(*args, **kwargs)

    monkeypatch.setattr(status, "find_checkouts", spy)

    entries = _run(git_status(str(root)))

    assert _states(entries) == {"a.txt": "modified"}
    assert asked == []


def test_a_subdirectory_of_a_checkout_never_asks_what_is_below_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`ctrl+L` onto `<repo>/sub` is still the single-repo path, whole.

    The upward walk answers from above the observed root, so there is nothing to
    discover below it and nothing may be discovered.
    """
    root = _repo(tmp_path / "proj", **{"sub/a.txt": "old\n"})
    (root / "sub" / "a.txt").write_text("changed\n", encoding="utf-8")
    asked: list[str] = []

    real_find_checkouts = status.find_checkouts

    def spy(*args, **kwargs):
        asked.append("yes")
        return real_find_checkouts(*args, **kwargs)

    monkeypatch.setattr(status, "find_checkouts", spy)

    entries = _run(git_status(str(root / "sub")))

    assert _states(entries) == {"a.txt": "modified"}
    assert asked == []


# --- 2.6 concurrency: bounded, and tolerant of one repository failing -------

def _stub_run_git(
    monkeypatch: pytest.MonkeyPatch,
    stdout_for: dict[str, str | None],
    *,
    delay: float = 0.0,
    in_flight: list[int] | None = None,
) -> list[str]:
    """Replace `run_git` with a recorder keyed on the last segment of `cwd`.

    Returns the list the calls are recorded into. `in_flight` collects a sample
    of how many calls were running at once, taken on entry to each one.
    """
    calls: list[str] = []
    live = 0

    async def fake_run_git(argv, cwd, timeout=None):
        nonlocal live
        name = os.path.basename(str(cwd).rstrip(os.sep))
        calls.append(name)
        live += 1
        if in_flight is not None:
            in_flight.append(live)
        try:
            if delay:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)
            return stdout_for.get(name, "")
        finally:
            live -= 1

    monkeypatch.setattr(status, "run_git", fake_run_git)
    return calls


def test_no_more_than_four_repositories_are_asked_at_the_same_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A fork's timeout is 5 s, so the bound is on the worst case, not the mean.

    Sixteen checkouts at 5 s unbounded is a poll round nobody can wait out; four
    waves of four is the 20 s ceiling `_status_busy` is sized against.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    names = [f"r{index}" for index in range(8)]
    for name in names:
        _make_checkout(container / name)
    in_flight: list[int] = []
    calls = _stub_run_git(
        monkeypatch,
        {name: _z(f"?? {name}.txt") for name in names},
        delay=0.01,
        in_flight=in_flight,
    )

    _run(git_status(str(container)))

    assert sorted(calls) == names
    assert max(in_flight) <= status.MAX_CONCURRENT_STATUS


def test_the_concurrency_budget_is_four():
    """Pinned by name and by value: it is the poll round's worst case."""
    assert status.MAX_CONCURRENT_STATUS == 4


def test_one_repository_whose_git_fails_does_not_lose_the_other_seven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A repository mid-rebase with `index.lock` held must not blank the panel.

    `run_git` already collapses every failure to `None`; the fan-out has to read
    that as "this one has nothing to say" rather than as the round's answer.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    names = [f"r{index}" for index in range(8)]
    for name in names:
        _make_checkout(container / name)
    stdout_for: dict[str, str | None] = {
        name: _z(f"?? {name}.txt") for name in names
    }
    stdout_for["r3"] = None
    _stub_run_git(monkeypatch, stdout_for)

    entries = _run(git_status(str(container)))

    assert _paths(entries) == [
        f"{name}/{name}.txt" for name in names if name != "r3"
    ]


def test_every_repository_failing_is_reported_as_nothing_to_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No `git` on the machine at all is `None`, not "everything is clean"."""
    container = tmp_path / "workspace"
    container.mkdir()
    for name in ("a", "b"):
        _make_checkout(container / name)
    _stub_run_git(monkeypatch, {"a": None, "b": None})

    assert _run(git_status(str(container))) is None


def test_two_successive_event_loops_each_get_the_same_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The semaphore must be created inside the call, never at module level.

    An `asyncio.Semaphore` built at import time binds to the first loop that has
    to *wait* on it and raises `RuntimeError` on every loop after that. It passes
    every single-loop functional test in this file and fails the second time a
    daemon's loop exists -- which, in this suite, is every test. Eight checkouts
    against a budget of four is what forces the wait, so the binding really
    happens on the first run.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    names = [f"r{index}" for index in range(8)]
    for name in names:
        _make_checkout(container / name)
    calls = _stub_run_git(
        monkeypatch, {name: _z(f"?? {name}.txt") for name in names}, delay=0.01
    )

    first = _run(git_status(str(container)))
    second = _run(git_status(str(container)))

    assert len(calls) == 16
    assert _pairs(second) == _pairs(first) != []


# --- 2.7 discovery is filesystem work, and it runs off the loop -------------

def test_the_downward_walk_does_not_run_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A root like `~` would otherwise freeze every connected browser.

    The walk opens up to 4000 directories, and one of them may be a network
    mount. `scan_tree` is already handed to `asyncio.to_thread` for exactly this
    reason; discovery runs on every poll, which is worse.

    The probe: discovery blocks until another task on the loop releases it. On
    the loop's own thread that task can never run, so the wait times out and the
    flag recorded is `False`.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    resume = threading.Event()
    the_loop_kept_running: list[bool] = []

    def blocking_find_checkouts(root, *args, **kwargs):
        the_loop_kept_running.append(resume.wait(timeout=5.0))
        return []

    monkeypatch.setattr(status, "find_checkouts", blocking_find_checkouts)

    async def scenario():
        async def another_client_being_served():
            await asyncio.sleep(0)
            resume.set()

        task = asyncio.create_task(another_client_being_served())
        await git_status(str(container))
        await task

    _run(scenario())

    assert the_loop_kept_running == [True]


# --- 2.8 fairness, pinned against the frame the browser actually receives ---

def test_every_repository_is_represented_in_the_two_hundred_rows_sent(
    tmp_path: Path,
):
    """The fairness pin, and it belongs in the suite permanently.

    Three repositories of 150 pending entries is 450, and the frame carries 200.
    Ordered repo-by-repo, the first two would fill the cut and the third would
    be invisible -- a panel that silently answers for some of the workspace is
    worse than the one that answers for none of it, because it looks correct.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    for name in ("a", "b", "c"):
        checkout = _repo(container / name, **{"committed.txt": "old\n"})
        for index in range(150):
            (checkout / f"f{index:04d}.txt").write_text("loose\n", encoding="utf-8")

    frame = status_frame(_run(git_status(str(container))), max_entries=200)
    counts = _by_repository(entry["path"] for entry in frame["entries"])

    assert len(frame["entries"]) == 200
    # 200 rows over three repositories is ~66 each. Anything far below that means
    # the list was not interleaved before it was cut.
    assert sorted(counts) == ["a", "b", "c"] and min(counts.values()) >= 50


# --- The per-repo cap: a memory bound that must not silence `truncated` -----
#
# The cap is `DEFAULT_MAX_ENTRIES + 1`, and the `+ 1` is the whole point.
#
# The cap exists only as a memory bound: sixteen repositories times five
# thousand entries, parsed every three seconds to keep two hundred, is garbage
# the loop does not need. It is `DEFAULT_MAX_ENTRIES` per repository rather than
# a share of it because a share would be `200 // N`, a constant that depends on
# N and that nobody can choose.
#
# But a bound of *exactly* `DEFAULT_MAX_ENTRIES` makes the frame lie. Truncation
# is derived in `status_frame` as `len(entries) > len(shown)`, so a lone
# sub-repository with 300 pending changes, cut to exactly 200 on the way in,
# produces `200 > 200` -- `False`. The panel would state the list is complete
# over a list that was cut. The same repository observed directly, with no cap
# in front of it, reports `truncated: True`: same repository, same 300 changes,
# two different claims about whether the viewer is seeing everything, decided by
# nothing but whether the observed root happens to sit one directory higher.
# `truncated` exists because "a silently cut list reads as the whole truth", in
# `status_frame`'s own words, and the memory bound must not be what silences it.
#
# One entry more than the frame can show is exactly enough to keep the signal
# true: anything above the global cut still exceeds it after the per-repo cut,
# and anything at or below it is untouched. The bound is unharmed -- 16 x 201 is
# the same nothing as 16 x 200.

def test_each_repository_is_cut_to_the_cap_before_the_interleave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The memory bound applies per repository, before anything is merged.

    Sixteen repositories times five thousand entries, parsed to keep two
    hundred, is work the daemon's loop does every three seconds for nothing.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    for name in ("a", "b"):
        _make_checkout(container / name)
    _stub_run_git(
        monkeypatch,
        {"a": _untracked(250), "b": _untracked(250)},
    )

    entries = _run(git_status(str(container)))

    assert _by_repository(_paths(entries)) == {
        "a": DEFAULT_MAX_ENTRIES + 1,
        "b": DEFAULT_MAX_ENTRIES + 1,
    }


def test_a_lone_repository_can_still_fill_every_row_of_the_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The per-repo cap can never bind before the global cut matters.

    One checkout with 300 pending entries under a container still hands the
    browser a full frame of 200 -- the same number the single-repo path would
    have sent. A cap that cost rows here would be a fairness rule punishing a
    workspace for having one repository in it.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    _make_checkout(container / "solo")
    _stub_run_git(monkeypatch, {"solo": _untracked(300)})

    frame = status_frame(_run(git_status(str(container))))

    assert len(frame["entries"]) == DEFAULT_MAX_ENTRIES
    assert all(_leading(entry["path"]) == "solo" for entry in frame["entries"])


def test_a_lone_repository_cut_by_the_cap_still_admits_the_list_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A multi-repo frame claims what a single-repo frame would, for one repo.

    300 pending changes seen through a container and 300 seen directly are the
    same 300, and the viewer must be told the same thing about them. A cap of
    exactly `DEFAULT_MAX_ENTRIES` would answer `truncated: False` here and
    `truncated: True` there, and the difference would be which directory the
    root happens to be pointed at.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    _make_checkout(container / "solo")
    _stub_run_git(monkeypatch, {"solo": _untracked(300)})

    frame = status_frame(_run(git_status(str(container))))

    assert (len(frame["entries"]), frame["truncated"]) == (DEFAULT_MAX_ENTRIES, True)


def test_the_per_repository_cap_keeps_one_entry_more_than_the_frame_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The cap is doing its job, and it is doing it one entry too high on purpose.

    This is the assertion that fails in both directions: at
    `DEFAULT_MAX_ENTRIES` the frame loses the evidence it needs to say the list
    was cut, and with no cap at all all 300 entries ride the loop every three
    seconds to be thrown away by `status_frame`.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    _make_checkout(container / "solo")
    _stub_run_git(monkeypatch, {"solo": _untracked(300)})

    entries = _run(git_status(str(container)))

    assert len(_reported(entries)) == DEFAULT_MAX_ENTRIES + 1


def test_a_repository_with_exactly_a_frame_of_entries_is_not_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The lower boundary: the cap must not invent a truncation either.

    Exactly `DEFAULT_MAX_ENTRIES` pending changes fit, and a viewer told the
    list is partial would go looking for rows that do not exist.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    _make_checkout(container / "solo")
    _stub_run_git(monkeypatch, {"solo": _untracked(DEFAULT_MAX_ENTRIES)})

    frame = status_frame(_run(git_status(str(container))))

    assert (len(frame["entries"]), frame["truncated"]) == (DEFAULT_MAX_ENTRIES, False)


def test_a_repository_one_entry_over_a_frame_is_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The upper boundary: one entry over, and the frame says so.

    The narrowest case there is, and the one the `+ 1` is sized for -- a cap of
    `DEFAULT_MAX_ENTRIES` would drop precisely the entry that is the evidence.
    """
    container = tmp_path / "workspace"
    container.mkdir()
    _make_checkout(container / "solo")
    _stub_run_git(monkeypatch, {"solo": _untracked(DEFAULT_MAX_ENTRIES + 1)})

    frame = status_frame(_run(git_status(str(container))))

    assert (len(frame["entries"]), frame["truncated"]) == (DEFAULT_MAX_ENTRIES, True)
