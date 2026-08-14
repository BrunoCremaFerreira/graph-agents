"""Contract tests (RED) for graphagents.diff.

Motivation: the file panel opened by clicking a node should show *what changed*,
not just what is there. Inside a checkout, the interesting thing about a file an
agent just wrote is the uncommitted delta -- the two lines it added -- and that is
precisely what the graph cannot express. Outside a checkout, or with nothing
pending, there is no delta and the panel falls back to the content.

So this module answers one question, "is there an uncommitted diff for this
file?", and answers it with ``None`` when there is not. ``None`` is not an error
signal here: it is the instruction "show the content instead", which is why every
failure mode collapses into it.

Three pieces, split so that two of them are pure:

  * ``diff_command(relative_path)`` -- the argv. The path is data: it comes from a
    node in the graph, and a file genuinely named ``-x`` or ``--cached`` must not
    turn into an option, so it goes **after** ``--``. Pinning the argv is what
    makes that reviewable without running anything.
  * ``parse_diff_output(stdout)`` -- empty output means "no change". `git` exits 0
    and prints nothing for an unmodified file, so the exit code alone cannot tell
    the two apart.
  * ``git_diff(root, relative_path, timeout=3.0)`` -- the one impure piece. It runs
    on the daemon's event loop, on a path that arrived over a socket, against a
    binary that may not be installed and a repository that may be mid-rebase with
    a lock held. It must **never raise and never hang**: an exception kills the
    task serving that browser, and a wait on `git` freezes every other client.

The comparison is against ``HEAD``, deliberately: a file the agent wrote and then
staged is still an uncommitted change, and hiding it the moment `git add` runs
would blank the panel for the most interesting file on screen.

These tests build a real repository under ``tmp_path`` rather than mocking
`subprocess` -- what is being specified is the behaviour against real `git`, and a
mock would happily agree with a wrong argv. Commits are made with `-c
user.email`/`-c user.name` so the suite does not depend on the machine's git
config, and with signing off so it does not hang on a gpg prompt.

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

from graphagents.diff import diff_command, git_diff, parse_diff_output


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


# --- 1. diff_command: the argv, pinned --------------------------------------

def test_the_diff_is_taken_against_head():
    assert diff_command("src/app.ts") == ["git", "diff", "HEAD", "--", "src/app.ts"]


def test_the_path_is_passed_after_a_double_dash():
    # A file really named `-x` exists in this world (the graph draws whatever is
    # on disk); without the separator `git` would read it as an option.
    argv = diff_command("-x")

    assert argv[-2:] == ["--", "-x"]


def test_a_path_that_looks_like_an_option_is_not_rewritten():
    # No quoting, no `./` prefix, no escaping: argv is not a shell line, and
    # mangling the path here would make `git` diff a file that does not exist.
    assert diff_command("--cached")[-1] == "--cached"


# --- 2. parse_diff_output: empty means "no change" --------------------------

def test_no_output_means_there_is_no_diff():
    assert parse_diff_output("") is None


def test_output_that_is_only_whitespace_means_there_is_no_diff():
    # `git` exits 0 either way, so the emptiness is the only evidence.
    assert parse_diff_output("   \n\t\n  ") is None


def test_a_real_diff_comes_back_untouched():
    # The panel renders this verbatim; stripping it would drop the leading space
    # that marks a context line and the trailing newline of the last hunk.
    stdout = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"

    assert parse_diff_output(stdout) == stdout


# --- 3. git_diff: the real thing, and every way it can fail -----------------

def test_a_modified_file_reports_the_new_line(tmp_path: Path):
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")

    diff = _run(git_diff(str(root), "a.txt"))

    assert diff is not None and "+changed" in diff


def test_a_staged_change_is_still_a_change(tmp_path: Path):
    # Against `HEAD`, not against the index: `git add` must not blank the panel
    # for the file the agent just finished writing.
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    _git(root, "add", "a.txt")

    diff = _run(git_diff(str(root), "a.txt"))

    assert diff is not None and "+changed" in diff


def test_a_file_in_a_subdirectory_is_addressed_from_the_root(tmp_path: Path):
    # Paths in the graph are relative to the observed root, and `git` is run with
    # `cwd=root`; a path resolved against the daemon's own cwd finds nothing.
    root = _repo(tmp_path / "proj", **{"src/app.ts": "old\n"})
    (root / "src" / "app.ts").write_text("changed\n", encoding="utf-8")

    diff = _run(git_diff(str(root), "src/app.ts"))

    assert diff is not None and "+changed" in diff


def test_an_unmodified_file_has_no_diff(tmp_path: Path):
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})

    assert _run(git_diff(str(root), "a.txt")) is None


def test_an_untracked_file_has_no_diff(tmp_path: Path):
    # `git diff HEAD` says nothing about a file git has never seen; the panel
    # shows its content, which is the whole file anyway.
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})
    (root / "new.txt").write_text("brand new\n", encoding="utf-8")

    assert _run(git_diff(str(root), "new.txt")) is None


def test_a_path_that_does_not_exist_has_no_diff(tmp_path: Path):
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})

    assert _run(git_diff(str(root), "nope/gone.txt")) is None


def test_a_directory_outside_any_repository_has_no_diff(tmp_path: Path):
    # `git` exits 128 with "not a git repository" on stderr; that is an ordinary
    # answer here, not a failure to propagate.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("hello\n", encoding="utf-8")

    assert _run(git_diff(str(plain), "a.txt")) is None


def test_a_root_that_does_not_exist_has_no_diff(tmp_path: Path):
    # The observed root can be removed while a browser still has it on screen;
    # spawning with a missing cwd raises before `git` ever starts.
    assert _run(git_diff(str(tmp_path / "gone"), "a.txt")) is None


def test_a_path_with_a_nul_byte_has_no_diff(tmp_path: Path):
    # Straight off the network. `create_subprocess_exec` rejects embedded NULs
    # with `ValueError`, which must not reach the loop.
    root = _repo(tmp_path / "proj", **{"a.txt": "old\n"})

    assert _run(git_diff(str(root), "a\x00.txt")) is None


def test_no_git_binary_means_no_diff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # A machine without git still gets a file panel; it just never shows a diff.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(empty_bin))

    assert _run(git_diff(str(project), "a.txt")) is None


def test_a_git_that_hangs_is_given_up_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # A repository mid-rebase with `index.lock` held can block for as long as the
    # other process pleases, and this call is on the event loop that serves every
    # connected browser.
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake_git.chmod(0o755)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    started = time.monotonic()
    diff = _run(git_diff(str(project), "a.txt", timeout=0.3))
    elapsed = time.monotonic() - started

    assert diff is None and elapsed < 5.0
