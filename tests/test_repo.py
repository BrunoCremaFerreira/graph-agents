"""Contract tests (RED) for rhizome_graph.repo.

Motivation: the HUD at the bottom of the screen must say *which* directory is
being watched and *which branch* it is on. Without it a viewer looking at a
forwarded port has no idea whether the graph is showing the project they think
it is -- and a branch switch mid-session silently changes the meaning of every
node on screen.

Design constraints these tests pin down:

  * **Files, never subprocess.** The daemon polls this a few times per minute;
    forking `git` each time is waste, and it would break outright on a machine
    where the binary is absent while `.git/HEAD` is right there to read.
  * **Never raises.** The poll runs in a background task; one exception on an
    unreadable `.git` would kill the task and freeze the HUD forever.
  * **Every real `.git` shape.** A plain repository, a worktree/submodule whose
    `.git` is a *file* pointing elsewhere (absolute or relative), and an
    observed directory that is a *subfolder* of the repository.

`find_checkout_root` (section 5) answers a different question from
`resolve_git_dir`: not "where is the git *directory*" but "where is the top of
the *checkout*". The git-status panel needs the second one -- `git status`
reports paths relative to the top of the working tree, so turning them into the
paths the graph draws requires knowing where that top is. The two answers differ
for every worktree and submodule, where the `.git` directory lives somewhere else
entirely.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

# Section 5 reaches `find_checkout_root` through the module rather than importing
# the name: while it does not exist yet, a missing name in this import would
# collapse the twenty tests above into a collection error instead of leaving
# them green.
from rhizome_graph import repo
from rhizome_graph.repo import display_root, parse_head, read_branch, resolve_git_dir


def _plain_repo(root: Path, head: str = "ref: refs/heads/main\n") -> Path:
    """Create a normal repository at `root` and return its `.git` directory."""
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(head, encoding="utf-8")
    return git_dir


def _same_dir(a: str | None, b: Path) -> bool:
    return a is not None and Path(a).resolve() == b.resolve()


# --- 1. resolve_git_dir: finding the real git directory --------------------

def test_finds_a_plain_dot_git_directory(tmp_path: Path):
    git_dir = _plain_repo(tmp_path)

    found = resolve_git_dir(str(tmp_path))

    assert _same_dir(found, git_dir)


def test_follows_a_dot_git_file_pointing_at_an_absolute_gitdir(tmp_path: Path):
    # This is what a worktree (and a submodule) looks like on disk.
    real_git = tmp_path / "main-repo" / ".git" / "worktrees" / "wt"
    real_git.mkdir(parents=True)
    (real_git / "HEAD").write_text("ref: refs/heads/wt-branch\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    found = resolve_git_dir(str(worktree))

    assert _same_dir(found, real_git)


def test_resolves_a_relative_gitdir_against_the_directory_holding_dot_git(tmp_path: Path):
    # `gitdir: ../modules/lib` is relative to the *checkout*, not to the cwd of
    # whoever happens to be calling us.
    real_git = tmp_path / "modules" / "lib"
    real_git.mkdir(parents=True)
    (real_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    submodule = tmp_path / "lib"
    submodule.mkdir()
    (submodule / ".git").write_text("gitdir: ../modules/lib\n", encoding="utf-8")

    found = resolve_git_dir(str(submodule))

    assert _same_dir(found, real_git)


def test_searches_upward_when_the_observed_root_is_a_subfolder(tmp_path: Path):
    # The watched directory is often `repo/web` or `repo/src`, not the top.
    git_dir = _plain_repo(tmp_path)
    nested = tmp_path / "web" / "src"
    nested.mkdir(parents=True)

    found = resolve_git_dir(str(nested))

    assert _same_dir(found, git_dir)


def test_returns_none_when_no_repository_exists_anywhere_above(tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert resolve_git_dir(str(plain)) is None


def test_upward_search_stops_at_the_filesystem_root(tmp_path: Path):
    # A naive `while True: path = dirname(path)` never terminates at "/".
    # Run it off-thread so a regression fails the test instead of hanging CI.
    result: list[str | None] = []
    worker = threading.Thread(target=lambda: result.append(resolve_git_dir("/")), daemon=True)

    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "resolve_git_dir looped forever walking up from /"
    assert result == [None]


# --- 2. parse_head: HEAD contents -> a branch name -------------------------

def test_parses_a_simple_branch_ref():
    assert parse_head("ref: refs/heads/main") == "main"


def test_keeps_the_slashes_inside_a_namespaced_branch_name():
    assert parse_head("ref: refs/heads/feature/hud-contexto") == "feature/hud-contexto"


def test_detached_head_becomes_a_short_seven_character_sha():
    assert parse_head("9f2a1c4d5e6f708192a3b4c5d6e7f8091a2b3c4d") == "9f2a1c4"


def test_tolerates_surrounding_whitespace_and_a_trailing_newline():
    assert parse_head("  ref: refs/heads/main\n") == "main"


@pytest.mark.parametrize("content", ["", "   \n", "not a ref at all", "ref:", "ref: refs/heads/"])
def test_garbage_head_yields_no_branch(content: str):
    assert parse_head(content) is None


# --- 3. read_branch: the composed call, which must never raise -------------

def test_reads_the_branch_of_a_plain_repository(tmp_path: Path):
    _plain_repo(tmp_path, head="ref: refs/heads/development\n")

    assert read_branch(str(tmp_path)) == "development"


def test_reads_the_branch_of_a_worktree_through_its_gitdir_file(tmp_path: Path):
    real_git = tmp_path / "store" / "wt"
    real_git.mkdir(parents=True)
    (real_git / "HEAD").write_text("ref: refs/heads/feature/hud-contexto\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    assert read_branch(str(worktree)) == "feature/hud-contexto"


def test_a_directory_with_no_repository_has_no_branch(tmp_path: Path):
    assert read_branch(str(tmp_path)) is None


def test_a_missing_head_file_yields_none_instead_of_raising(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()  # repository present, HEAD deleted underneath us

    assert read_branch(str(tmp_path)) is None


def test_a_gitdir_pointing_nowhere_yields_none_instead_of_raising(tmp_path: Path):
    checkout = tmp_path / "wt"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {tmp_path / 'vanished'}\n", encoding="utf-8")

    assert read_branch(str(checkout)) is None


def test_an_unreadable_head_yields_none_instead_of_raising(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root reads through any permission bits")
    git_dir = _plain_repo(tmp_path)
    head = git_dir / "HEAD"
    head.chmod(0o000)
    try:
        assert read_branch(str(tmp_path)) is None
    finally:
        head.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_a_head_that_is_a_directory_yields_none_instead_of_raising(tmp_path: Path):
    git_dir = tmp_path / ".git"
    (git_dir / "HEAD").mkdir(parents=True)

    assert read_branch(str(tmp_path)) is None


# --- 4. display_root: the path as a human wants to read it -----------------

def test_collapses_the_home_directory_to_a_tilde():
    assert display_root("/home/brn/projects/x", "/home/brn") == "~/projects/x"


def test_the_home_directory_itself_is_just_a_tilde():
    assert display_root("/home/brn", "/home/brn") == "~"


def test_a_sibling_sharing_the_home_prefix_is_left_alone():
    # Textual prefix matching would mangle this into "~x/y".
    assert display_root("/home/brnx/y", "/home/brn") == "/home/brnx/y"


def test_a_path_outside_home_is_returned_unchanged():
    assert display_root("/srv/code/app", "/home/brn") == "/srv/code/app"


def test_an_empty_home_leaves_the_path_intact():
    assert display_root("/home/brn/projects/x", "") == "/home/brn/projects/x"


# --- 5. find_checkout_root: the top of the working tree --------------------

def test_finds_the_checkout_root_of_a_plain_repository(tmp_path: Path):
    # The directory that *contains* `.git`, never `.git` itself: `git status`
    # reports `src/app.py`, and joining that onto `.git/` points at nothing.
    _plain_repo(tmp_path)

    assert _same_dir(repo.find_checkout_root(str(tmp_path)), tmp_path)


def test_the_checkout_root_is_not_the_git_directory(tmp_path: Path):
    # The distinction the whole function exists for.
    _plain_repo(tmp_path)

    found = repo.find_checkout_root(str(tmp_path))

    assert found is not None and not found.rstrip("/").endswith(".git")


def test_a_deep_subdirectory_still_reports_the_top_of_the_checkout(tmp_path: Path):
    # The observed root is often `repo/web/src`; git's paths are relative to
    # `repo`, so the panel cannot rebase them without this answer.
    _plain_repo(tmp_path)
    nested = tmp_path / "web" / "src" / "deep"
    nested.mkdir(parents=True)

    assert _same_dir(repo.find_checkout_root(str(nested)), tmp_path)


def test_a_dot_git_file_makes_its_own_directory_the_checkout_root(tmp_path: Path):
    # A worktree or submodule: the git directory is elsewhere, but the working
    # tree -- the thing paths are relative to -- is right here.
    real_git = tmp_path / "store" / "wt"
    real_git.mkdir(parents=True)
    (real_git / "HEAD").write_text("ref: refs/heads/wt\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    assert _same_dir(repo.find_checkout_root(str(worktree)), worktree)


def test_a_subdirectory_of_a_worktree_reports_the_worktree(tmp_path: Path):
    real_git = tmp_path / "store" / "wt"
    real_git.mkdir(parents=True)
    (real_git / "HEAD").write_text("ref: refs/heads/wt\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    assert _same_dir(repo.find_checkout_root(str(worktree / "src")), worktree)


def test_a_directory_outside_any_repository_has_no_checkout_root(tmp_path: Path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert repo.find_checkout_root(str(plain)) is None


def test_a_path_that_does_not_exist_yields_none_instead_of_raising(tmp_path: Path):
    # The observed root can be deleted while the poll is still asking about it.
    assert repo.find_checkout_root(str(tmp_path / "vanished" / "deeper")) is None


def test_the_checkout_root_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # It is handed to `relativize` alongside the observed root; a relative answer
    # would never match and would silently drop every entry.
    _plain_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    found = repo.find_checkout_root(".")

    assert found is not None and os.path.isabs(found)


def test_the_upward_search_for_a_checkout_stops_at_the_filesystem_root():
    # A naive `while True: path = dirname(path)` never terminates at "/".
    result: list[str | None] = []
    worker = threading.Thread(
        target=lambda: result.append(repo.find_checkout_root("/")), daemon=True
    )

    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "find_checkout_root looped forever walking up from /"
    assert result == [None]
