"""Contract tests (RED) for graphagents.status.

Motivation: the HUD says which project is on screen and which branch it is on,
and nothing else. "Is anything uncommitted right now, and what?" -- the single
question anyone watching agents edit a checkout actually has -- is invisible: the
graph flashes a node orange when a file is written and forgets it a second later,
so a viewer arriving thirty seconds after the fact sees a clean-looking tree over
a dirty working directory. This module is the model behind a git-status panel:
the pending changes, as data, ready to be pushed down the WebSocket.

Split so that everything except one function is pure, for the same reason
`graphagents.diff` is split that way:

  * ``status_command()`` -- the argv, pinned. Two flags are load-bearing and both
    are invisible in a screenshot: ``-z`` (NUL-separated records) because the
    default output *quotes and escapes* any name with a space or a quote in it,
    and ``core.quotepath=off`` because the default mangles every non-ASCII byte
    into ``\\303\\247`` -- a path that then matches no node in the graph. The
    repository is never an argument: `git` is run with ``cwd`` set, and a path
    argument would be resolved against the daemon's own cwd instead.
  * ``parse_status(stdout)`` -- the ``-z`` grammar, including the one trap in it:
    a rename or copy record is followed by *one extra field* holding the original
    path, which has to be consumed or every record after it is read as a path.
  * ``relativize(entries, checkout_root, observed_root)`` -- `git` reports paths
    relative to the **repository root** even when run from a subdirectory
    (measured), while the graph, `resolve_inside` and every node on screen use
    paths relative to the **observed root**, which ``ctrl+L`` allows to be a
    subdirectory of the repository. Without this the panel offers paths that
    resolve to nothing.
  * ``status_frame(entries, max_entries)`` -- the frame itself. ``None`` (no
    repository, no `git`) and ``[]`` (a clean tree) are different answers and the
    page renders them differently, so they must not collapse into each other.
  * ``git_status(root, timeout)`` -- the only impure piece. It runs on the loop
    that serves every browser, on a timer, against a binary that may be absent
    and a repository that may be mid-rebase with a lock held: it must never raise
    and never hang, and every failure collapses to ``None``.

The parse cases below were measured against git 2.43 rather than reasoned from
the manual -- ``RM`` with its trailing original-path field, ``AD``, ``UU``, a
``??`` whose name contains a space.

These tests build a real repository under ``tmp_path`` instead of mocking
`subprocess`: what is being specified is the behaviour against real `git`, and a
mock would agree just as happily with a wrong argv.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from graphagents.status import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_TIMEOUT_SECONDS,
    STATES,
    StatusEntry,
    git_status,
    parse_status,
    relativize,
    status_command,
    status_frame,
)


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


def _z(*records: str) -> str:
    """`records` as `git status -z` writes them: each terminated by a NUL."""
    return "".join(record + "\0" for record in records)


def _pairs(entries) -> list[tuple[str, str]]:
    return [(e.path, e.state) for e in entries]


def _states(entries) -> dict[str, str]:
    return {e.path: e.state for e in entries}


# --- 1. status_command: the argv, pinned ------------------------------------

def test_the_status_command_is_pinned_exactly():
    assert status_command() == [
        "git",
        "-c",
        "core.quotepath=off",
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=normal",
    ]


def test_the_output_is_asked_for_in_the_stable_porcelain_format():
    # `git status` without it is localized prose that changes between versions.
    assert "--porcelain" in status_command()


def test_records_are_asked_for_nul_separated():
    # Without `-z`, `a b.txt` comes back quoted as `"a b.txt"` and a newline in a
    # file name splits one record into two.
    assert "-z" in status_command()


def test_non_ascii_paths_are_asked_for_unescaped():
    # The default turns `ação.py` into `\303\247`, which matches no node.
    argv = status_command()

    assert argv[argv.index("-c") + 1] == "core.quotepath=off"


def test_the_repository_is_never_passed_as_an_argument():
    # `git` is run with `cwd=root`; a path argument would be resolved against the
    # daemon's own working directory, which is some other project entirely.
    assert not [part for part in status_command() if part.startswith("/")]


# --- 2. parse_status: the -z grammar ----------------------------------------

def test_a_worktree_modification_is_modified():
    assert _pairs(parse_status(_z(" M y.py"))) == [("y.py", "modified")]


def test_a_worktree_deletion_is_deleted():
    assert _pairs(parse_status(_z(" D b.txt"))) == [("b.txt", "deleted")]


def test_a_staged_addition_is_added():
    assert _pairs(parse_status(_z("A  x.py"))) == [("x.py", "added")]


def test_an_untracked_file_is_untracked():
    assert _pairs(parse_status(_z("?? sub/new.txt"))) == [("sub/new.txt", "untracked")]


def test_a_file_added_and_then_edited_is_still_added():
    # `AM`: added to the index, modified since. It is new to HEAD, which is what
    # the panel is colouring.
    assert _pairs(parse_status(_z("AM x.py"))) == [("x.py", "added")]


def test_a_file_added_and_then_deleted_reads_as_deleted():
    # `AD`: staged as new, then removed from the disk. Deletion wins -- the file
    # is not there, and offering it as "added" points at nothing.
    assert _pairs(parse_status(_z("AD z.py"))) == [("z.py", "deleted")]


def test_a_file_modified_in_both_the_index_and_the_tree_is_modified():
    assert _pairs(parse_status(_z("MM y.py"))) == [("y.py", "modified")]


def test_an_unmerged_file_is_modified():
    # `UU` is a conflict. It is not a state of its own here; what matters to the
    # viewer is that the file differs from HEAD.
    assert _pairs(parse_status(_z("UU merge.txt"))) == [("merge.txt", "modified")]


def test_a_rename_is_reported_at_its_new_path():
    # Measured: `RM <new>\0<old>\0`. The node on screen is the new path.
    parsed = parse_status(_z("RM sub/deep/renamed.txt", "sub/deep/a.txt"))

    assert _pairs(parsed) == [("sub/deep/renamed.txt", "modified")]


def test_the_original_path_of_a_rename_is_not_an_entry_of_its_own():
    # The trap in the `-z` format: the extra field is a bare path with no XY
    # prefix. Not consuming it invents a second, garbled entry.
    parsed = parse_status(_z("R  new.txt", "old.txt"))

    assert len(parsed) == 1


def test_the_record_after_a_rename_is_still_read_correctly():
    # The real damage of not consuming the extra field: every record after it is
    # off by one, so the whole rest of the status is garbage.
    parsed = parse_status(_z("R  new.txt", "old.txt", " M y.py", "?? z.txt"))

    assert _pairs(parsed) == [
        ("new.txt", "modified"),
        ("y.py", "modified"),
        ("z.txt", "untracked"),
    ]


def test_a_copy_also_carries_an_original_path_field():
    parsed = parse_status(_z("C  copy.txt", "orig.txt", " M y.py"))

    assert _pairs(parsed) == [("copy.txt", "modified"), ("y.py", "modified")]


def test_an_untracked_name_containing_a_space_survives_intact():
    # The whole reason for `-z`: this is one record, not two.
    parsed = parse_status(_z("?? sub/new file.txt"))

    assert _pairs(parsed) == [("sub/new file.txt", "untracked")]


def test_an_ignored_file_is_not_reported():
    # `!!` only appears with --ignored, but a stray one must not become an entry:
    # the panel would list build output as a pending change.
    assert parse_status(_z("!! node_modules/x.js")) == []


def test_the_order_git_printed_is_the_order_returned():
    parsed = parse_status(_z(" M a.txt", " D b.txt", "?? c.txt", "A  d.txt"))

    assert [e.path for e in parsed] == ["a.txt", "b.txt", "c.txt", "d.txt"]


def test_a_clean_tree_prints_nothing_and_parses_to_nothing():
    assert parse_status("") == []


@pytest.mark.parametrize(
    "stdout",
    [
        "\0",
        "\0\0\0",
        _z("", "  ", "?"),
        _z("XY"),
        _z("\x00\x01\x02\x03"),
        "no separator at all",
        _z("ZZ what.txt"),
    ],
    ids=["one-nul", "many-nuls", "short-records", "no-path", "binary", "unterminated", "unknown-xy"],
)
def test_garbage_is_dropped_without_raising(stdout: str):
    # This is parsed on the loop serving every browser, every few seconds. An
    # exception here would kill the poll for the rest of the session.
    assert isinstance(parse_status(stdout), list)


def test_a_record_too_short_to_have_a_path_is_dropped():
    assert parse_status(_z("XY")) == []


def test_a_state_git_never_emits_is_dropped_rather_than_guessed():
    # Same rule as `_parse_bash`: when it would have to guess, it stays silent.
    assert parse_status(_z("ZZ what.txt")) == []


def test_every_reported_state_is_one_of_the_declared_ones():
    parsed = parse_status(
        _z(" M a.txt", " D b.txt", "A  c.py", "?? d.txt", "RM e.txt", "e-old.txt")
    )

    assert {e.state for e in parsed} <= set(STATES)


def test_the_declared_states_are_the_four_the_panel_paints():
    assert STATES == ("modified", "added", "deleted", "untracked")


def test_a_status_entry_is_a_frozen_pair_of_path_and_state():
    # Frozen because these are handed around and cached; a mutated entry would
    # repaint a node that never changed.
    entry = StatusEntry(path="a.txt", state="modified")

    with pytest.raises(Exception):
        entry.path = "b.txt"  # type: ignore[misc]


# --- 3. relativize: git speaks repo-relative, the graph does not ------------

def test_an_observed_root_that_is_the_repository_leaves_paths_alone():
    entries = [StatusEntry("src/app.py", "modified"), StatusEntry("a.txt", "added")]

    assert _pairs(relativize(entries, "/repo", "/repo")) == [
        ("src/app.py", "modified"),
        ("a.txt", "added"),
    ]


def test_a_path_inside_the_observed_subdirectory_loses_that_prefix():
    # Observing `<repo>/sub` (perfectly normal after ctrl+L), git still says
    # `sub/a.txt`, while the node on screen is `a.txt`.
    entries = [StatusEntry("sub/a.txt", "modified")]

    assert _pairs(relativize(entries, "/repo", "/repo/sub")) == [("a.txt", "modified")]


def test_a_path_outside_the_observed_subdirectory_is_dropped():
    # It is a real pending change, but there is no node for it and clicking it
    # would be refused by `resolve_inside` anyway.
    entries = [StatusEntry("outro/b.txt", "modified")]

    assert relativize(entries, "/repo", "/repo/sub") == []


def test_a_sibling_sharing_the_prefix_of_the_observed_directory_is_dropped():
    # Textual prefix stripping turns `subterfuge/x.txt` into `terfuge/x.txt` --
    # the same defect `display_root` had with `~`. The boundary is a segment.
    entries = [StatusEntry("subterfuge/x.txt", "modified")]

    assert relativize(entries, "/repo", "/repo/sub") == []


def test_the_state_survives_the_move_to_observed_paths():
    entries = [StatusEntry("sub/gone.txt", "deleted")]

    assert relativize(entries, "/repo", "/repo/sub")[0].state == "deleted"


def test_a_deep_observed_root_strips_every_segment_it_owns():
    entries = [StatusEntry("sub/deep/a.txt", "untracked")]

    assert _pairs(relativize(entries, "/repo", "/repo/sub/deep")) == [
        ("a.txt", "untracked")
    ]


def test_the_separator_stays_a_forward_slash():
    entries = [StatusEntry("sub/deep/nested/a.txt", "modified")]

    assert relativize(entries, "/repo", "/repo/sub")[0].path == "deep/nested/a.txt"


def test_a_trailing_slash_on_the_observed_root_changes_nothing():
    entries = [StatusEntry("sub/a.txt", "modified")]

    assert _pairs(relativize(entries, "/repo/", "/repo/sub/")) == [("a.txt", "modified")]


def test_the_entries_handed_in_are_not_mutated():
    # The caller keeps them; rewriting in place would corrupt a second read.
    entries = [StatusEntry("sub/a.txt", "modified")]

    relativize(entries, "/repo", "/repo/sub")

    assert entries[0].path == "sub/a.txt"


def test_an_empty_list_relativizes_to_an_empty_list():
    assert relativize([], "/repo", "/repo/sub") == []


@pytest.mark.parametrize(
    "checkout, observed",
    [("", ""), ("/repo", ""), ("", "/repo/sub"), ("/repo", "/elsewhere")],
    ids=["both-empty", "no-observed", "no-checkout", "unrelated-roots"],
)
def test_roots_that_make_no_sense_yield_a_list_instead_of_an_exception(
    checkout: str, observed: str
):
    assert isinstance(relativize([StatusEntry("a.txt", "modified")], checkout, observed), list)


# --- 4. status_frame: what actually goes on the wire ------------------------

def test_no_repository_is_reported_as_no_repository():
    assert status_frame(None) == {
        "kind": "status",
        "repo": False,
        "truncated": False,
        "entries": [],
    }


def test_a_clean_tree_is_a_repository_with_nothing_pending():
    # The distinction that matters: "no repo" and "clean" look identical if both
    # collapse to an empty list, and the panel says different things about them.
    assert status_frame([]) == {
        "kind": "status",
        "repo": True,
        "truncated": False,
        "entries": [],
    }


def test_the_frame_carries_each_entry_as_a_path_and_a_state():
    frame = status_frame([StatusEntry("a.txt", "modified"), StatusEntry("b.txt", "added")])

    assert frame["entries"] == [
        {"path": "a.txt", "state": "modified"},
        {"path": "b.txt", "state": "added"},
    ]


def test_the_frame_is_serializable_as_it_stands():
    # It crosses a WebSocket; a raw `StatusEntry` smuggled in would raise inside
    # the send, on the daemon's loop, killing that client's task.
    import json

    frame = status_frame([StatusEntry("a.txt", "modified")])

    assert json.loads(json.dumps(frame))["entries"][0]["state"] == "modified"


def test_a_huge_status_is_cut_at_the_cap():
    entries = [StatusEntry(f"f{i}.txt", "modified") for i in range(500)]

    assert len(status_frame(entries, max_entries=10)["entries"]) == 10


def test_a_cut_status_says_so():
    # `git checkout` of a big branch, or a first commit: thousands of entries the
    # page would have to lay out. The viewer must know the list is partial.
    entries = [StatusEntry(f"f{i}.txt", "modified") for i in range(500)]

    assert status_frame(entries, max_entries=10)["truncated"] is True


def test_a_status_exactly_at_the_cap_is_not_truncated():
    entries = [StatusEntry(f"f{i}.txt", "modified") for i in range(10)]

    assert status_frame(entries, max_entries=10)["truncated"] is False


def test_the_cut_keeps_the_first_entries_in_order():
    entries = [StatusEntry(f"f{i}.txt", "modified") for i in range(500)]

    frame = status_frame(entries, max_entries=3)

    assert [e["path"] for e in frame["entries"]] == ["f0.txt", "f1.txt", "f2.txt"]


def test_the_default_cap_is_two_hundred_entries():
    assert DEFAULT_MAX_ENTRIES == 200


def test_the_default_timeout_gives_git_five_seconds():
    assert DEFAULT_TIMEOUT_SECONDS == 5.0


# --- 5. git_status: the real thing, and every way it can fail ---------------

def test_a_clean_repository_reports_an_empty_list_not_none(tmp_path: Path):
    # `[]` means "clean"; `None` means "there is nothing to report on". Collapsing
    # them makes a clean checkout indistinguishable from a plain directory.
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})

    assert _run(git_status(str(root))) == []


def test_a_modified_file_is_reported_as_modified(tmp_path: Path):
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")

    assert _states(_run(git_status(str(root)))) == {"a.txt": "modified"}


def test_the_four_states_are_told_apart_in_one_working_tree(tmp_path: Path):
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n", "b.txt": "b\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    (root / "b.txt").unlink()
    (root / "added.py").write_text("new\n", encoding="utf-8")
    _git(root, "add", "added.py")
    (root / "untracked.txt").write_text("loose\n", encoding="utf-8")

    assert _states(_run(git_status(str(root)))) == {
        "a.txt": "modified",
        "b.txt": "deleted",
        "added.py": "added",
        "untracked.txt": "untracked",
    }


def test_a_file_in_a_subdirectory_keeps_its_path_from_the_root(tmp_path: Path):
    root = _repo(tmp_path / "proj", **{"src/app.ts": "old\n"})
    (root / "src" / "app.ts").write_text("changed\n", encoding="utf-8")

    assert _states(_run(git_status(str(root)))) == {"src/app.ts": "modified"}


def test_a_name_with_a_space_survives_the_round_trip(tmp_path: Path):
    # End-to-end evidence for `-z`: without it git quotes this one.
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})
    (root / "my notes.txt").write_text("loose\n", encoding="utf-8")

    assert _states(_run(git_status(str(root)))) == {"my notes.txt": "untracked"}


def test_a_renamed_file_is_reported_at_its_new_path(tmp_path: Path):
    root = _repo(tmp_path / "proj", **{"old.txt": "content\n"})
    _git(root, "mv", "old.txt", "new.txt")

    assert list(_states(_run(git_status(str(root))))) == ["new.txt"]


def test_an_observed_subdirectory_reports_only_what_is_inside_it(tmp_path: Path):
    # `ctrl+L` can point the graph at `<repo>/sub`. A change in `outro/` has no
    # node on screen, and its path would resolve to nothing.
    root = _repo(tmp_path / "proj", **{"sub/a.txt": "old\n", "outro/b.txt": "old\n"})
    (root / "sub" / "a.txt").write_text("changed\n", encoding="utf-8")
    (root / "outro" / "b.txt").write_text("changed\n", encoding="utf-8")

    assert _states(_run(git_status(str(root / "sub")))) == {"a.txt": "modified"}


def test_an_observed_subdirectory_reports_paths_relative_to_itself(tmp_path: Path):
    # git says `sub/deep/a.txt` even when run from `sub/`; the node is `deep/a.txt`.
    root = _repo(tmp_path / "proj", **{"sub/deep/a.txt": "old\n"})
    (root / "sub" / "deep" / "a.txt").write_text("changed\n", encoding="utf-8")

    assert list(_states(_run(git_status(str(root / "sub"))))) == ["deep/a.txt"]


def test_a_directory_outside_any_repository_reports_nothing(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("hello\n", encoding="utf-8")

    assert _run(git_status(str(plain))) is None


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


def test_a_root_that_does_not_exist_reports_nothing(tmp_path: Path):
    # The observed root can be removed while a browser still has it on screen.
    assert _run(git_status(str(tmp_path / "gone"))) is None


def test_no_git_binary_means_no_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # A machine without git still gets a graph; it just never gets a panel.
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    assert _run(git_status(str(root))) is None


def test_a_git_that_hangs_is_given_up_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # A repository mid-rebase with `index.lock` held blocks for as long as the
    # other process pleases, and this call sits on the loop that serves every
    # connected browser.
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    started = time.monotonic()
    result = _run(git_status(str(root), timeout=0.3))
    elapsed = time.monotonic() - started

    assert result is None and elapsed < 5.0


def test_a_repository_with_no_commits_yet_does_not_raise(tmp_path: Path):
    # `git status` before the first commit works, but HEAD does not resolve --
    # the kind of edge that turns into a traceback on the daemon's loop.
    _require_git()
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("new\n", encoding="utf-8")

    assert _states(_run(git_status(str(root)))) == {"a.txt": "untracked"}
