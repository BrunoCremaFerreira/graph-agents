"""Contract tests (RED) for graphagents.paths.

Motivation: the observed root is decided once, at boot, by
``GRAPHAGENTS_PROJECT_ROOT``. Watching a second project means killing the daemon,
exporting a different variable and starting over -- and losing the graph you were
already looking at. The new feature lets the viewer retype the root in the page
(``ctrl+L``, ``Tab`` to complete, ``Enter`` to apply); this module is the piece
underneath it, and it is deliberately pure stdlib so it can be exercised without
a daemon, a socket or a browser.

Two functions, two jobs:

  * ``resolve_root(text, home)`` -- turn what the user typed into an absolute
    directory path, or ``None``. ``None`` is not an error condition to be raised:
    it is exactly what makes the page say "no such directory" instead of
    switching the graph to nowhere.
  * ``complete_dir(text, home)`` -- terminal-style ``Tab``: only *directories*
    are candidates (you cannot observe a file), the common prefix is filled in so
    repeated tabs converge, and the matches come back for the page to list.

Both take ``home`` as an argument rather than calling ``os.path.expanduser``:
these tests must be hermetic, and the daemon may well want to expand ``~``
against something other than its own process environment.

Neither may raise. This runs inside the daemon's event loop, where the house rule
is that nothing raises -- an exception on a path with a NUL byte in it would kill
the task serving every connected browser.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from graphagents.paths import Completion, complete_dir, resolve_root


def _dirs(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True, exist_ok=True)


# --- 1. resolve_root: what the user typed -> a root, or nothing -------------

def test_an_existing_directory_resolves_to_its_absolute_path(tmp_path: Path):
    _dirs(tmp_path, "proj")

    assert resolve_root(str(tmp_path / "proj"), str(tmp_path)) == str(tmp_path / "proj")


def test_a_leading_tilde_is_expanded_with_the_given_home(tmp_path: Path):
    _dirs(tmp_path, "projects/app")

    resolved = resolve_root("~/projects/app", str(tmp_path))

    assert resolved == str(tmp_path / "projects" / "app")


def test_the_process_home_is_never_consulted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Hermetic by construction: expanding against $HOME would find this one and
    # answer a path the caller never asked about.
    real_home = tmp_path / "real-home"
    (real_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(real_home))
    empty_home = tmp_path / "given-home"
    empty_home.mkdir()

    assert resolve_root("~/projects", str(empty_home)) is None


def test_dot_dot_segments_and_a_trailing_slash_are_normalized_away(tmp_path: Path):
    # The field is typed by hand; "~/projects/x/../x/" is a normal thing to end
    # up with, and it must not become a second, different-looking root.
    _dirs(tmp_path, "proj")

    resolved = resolve_root(f"{tmp_path}/proj/../proj/", str(tmp_path))

    assert resolved == str(tmp_path / "proj")


def test_a_directory_that_does_not_exist_is_refused(tmp_path: Path):
    assert resolve_root(str(tmp_path / "typo"), str(tmp_path)) is None


def test_a_file_is_refused_because_a_root_must_be_a_directory(tmp_path: Path):
    target = tmp_path / "README.md"
    target.write_text("", encoding="utf-8")

    assert resolve_root(str(target), str(tmp_path)) is None


def test_an_empty_field_is_refused_rather_than_meaning_the_current_directory(tmp_path: Path):
    # Pressing Enter on an empty box must not silently repoint the graph at the
    # daemon's cwd, which is not a place the viewer asked to look at.
    assert resolve_root("", str(tmp_path)) is None


@pytest.mark.parametrize(
    "text",
    ["   ", "\x00", "nul\x00byte", "~nosuchuser/x", "//", "~"],
)
def test_resolve_root_never_raises_on_junk(text: str, tmp_path: Path):
    result = resolve_root(text, str(tmp_path))

    assert result is None or isinstance(result, str)


# --- 2. complete_dir: Tab, as a terminal does it ---------------------------

def test_a_prefix_matching_one_directory_completes_to_it_with_a_trailing_slash(tmp_path: Path):
    # The slash is what lets the next Tab descend instead of re-completing the
    # same name forever.
    _dirs(tmp_path, "projects")

    result = complete_dir(f"{tmp_path}/pro", str(tmp_path))

    assert result == Completion(f"{tmp_path}/projects/", ["projects"])


def test_a_prefix_matching_several_advances_to_their_longest_common_prefix(tmp_path: Path):
    _dirs(tmp_path, "project-alpha", "project-beta")

    result = complete_dir(f"{tmp_path}/pro", str(tmp_path))

    assert result.completed == f"{tmp_path}/project-"


def test_every_candidate_comes_back_so_the_page_can_list_them(tmp_path: Path):
    _dirs(tmp_path, "project-alpha", "project-beta")

    result = complete_dir(f"{tmp_path}/pro", str(tmp_path))

    assert result.matches == ["project-alpha", "project-beta"]


def test_matches_are_sorted_regardless_of_the_order_the_filesystem_lists_them(tmp_path: Path):
    _dirs(tmp_path, "zeta", "alpha", "mid")

    result = complete_dir(f"{tmp_path}/", str(tmp_path))

    assert result.matches == ["alpha", "mid", "zeta"]


def test_a_text_ending_in_a_slash_lists_the_children_of_that_directory(tmp_path: Path):
    _dirs(tmp_path, "web", "daemon")

    result = complete_dir(f"{tmp_path}/", str(tmp_path))

    assert result.matches == ["daemon", "web"]


def test_a_prefix_matching_nothing_hands_the_text_back_untouched(tmp_path: Path):
    # Tab on a typo must leave the field exactly as typed, not truncate it back
    # to the parent directory.
    _dirs(tmp_path, "projects")

    assert complete_dir(f"{tmp_path}/zzz", str(tmp_path)) == Completion(f"{tmp_path}/zzz", [])


def test_files_are_not_candidates_because_a_root_must_be_a_directory(tmp_path: Path):
    _dirs(tmp_path, "apps")
    (tmp_path / "app.py").write_text("", encoding="utf-8")

    result = complete_dir(f"{tmp_path}/app", str(tmp_path))

    assert result == Completion(f"{tmp_path}/apps/", ["apps"])


def test_hidden_directories_stay_out_of_a_plain_listing(tmp_path: Path):
    _dirs(tmp_path, ".git", ".venv", "src")

    result = complete_dir(f"{tmp_path}/", str(tmp_path))

    assert result.matches == ["src"]


def test_hidden_directories_appear_once_the_dot_is_typed(tmp_path: Path):
    _dirs(tmp_path, ".claude", "src")

    result = complete_dir(f"{tmp_path}/.", str(tmp_path))

    assert result == Completion(f"{tmp_path}/.claude/", [".claude"])


def test_completion_expands_a_leading_tilde_with_the_given_home(tmp_path: Path):
    _dirs(tmp_path, "projects")

    result = complete_dir("~/pro", str(tmp_path))

    assert result == Completion(f"{tmp_path}/projects/", ["projects"])


def test_a_parent_directory_that_does_not_exist_yields_no_candidates(tmp_path: Path):
    result = complete_dir(f"{tmp_path}/nowhere/xyz", str(tmp_path))

    assert result == Completion(f"{tmp_path}/nowhere/xyz", [])


def test_an_unreadable_parent_yields_no_candidates_instead_of_raising(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root reads through any permission bits")
    locked = tmp_path / "locked"
    (locked / "inside").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        assert complete_dir(f"{locked}/in", str(tmp_path)) == Completion(f"{locked}/in", [])
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "/", "//", "~", "\x00", "/nope-zzz/\x00x"],
)
def test_complete_dir_never_raises_on_junk(text: str, tmp_path: Path):
    result = complete_dir(text, str(tmp_path))

    assert isinstance(result, Completion)
    assert isinstance(result.completed, str)
    assert isinstance(result.matches, list)
