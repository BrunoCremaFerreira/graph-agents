"""Contract tests (RED) for the click path over a workspace of repositories.

The defect these specify, plan section R4: `file_view` runs `git_diff` with
`cwd` = the **observed root** (`rhizome_graph/file_view.py:108` ->
`rhizome_graph/diff.py:96`). Over a `~/projects/{a,b,c}` workspace the observed
root is a container that is not itself a checkout, so `git` exits 128, `run_git`
answers ``None``, and the diff route is dead for every file in every sub-repo.

That degrades in two ways, and the quieter one is the worse one:

  * an **existing** file falls through to the text branch, so the panel looks
    like it works while it silently stops answering the question it exists for,
    "what did the agent just do to this file";
  * a **deleted** file reaches the existence check and answers ``no such file``
    -- undoing the diff-before-existence ordering that `file_view`'s own module
    docstring exists to document, on the single row the status panel most wants
    to offer for a click.

The fix decides the diff's working directory from `owning_checkout`, and three
properties hold it together. Each is a test here:

  * **The chokepoint stays single and stays first.** `resolve_inside` remains the
    only containment check, and the checkout is derived from *its output*, never
    from the raw string off the WebSocket. A `cwd` computed before the chokepoint
    has answered is a second place a path is interpreted, which is precisely how
    a chokepoint becomes bypassable.
  * **`relpath(target, checkout)` cannot escape.** The checkout was found by
    walking *up from* the resolved target, so the target is inside it by
    construction and the result can never begin with `..`.
  * **The single-repo case keeps the raw string.** Deliberate asymmetry: the
    resolved target is a `realpath`, so diffing it would diff a symlink's
    destination rather than the link that was clicked. In the sub-repo branch
    that is unavoidable (the checkout is only knowable from the resolved path);
    in the compat branch it is avoidable, so it is avoided. Pinned here so a
    later tidy-up cannot quietly make the two branches "consistent".

New file rather than additions to `tests/test_file_view.py`, so that file's
assertions stay byte-identical and a reviewer can see nothing moved.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rhizome_graph import checkouts as checkouts_module
from rhizome_graph import diff as diff_module
from rhizome_graph import file_view as file_view_module
from rhizome_graph.file_view import file_view

PNG_MAGIC = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


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


def _repo(root: Path, files: dict[str, bytes]) -> Path:
    """A real repository with `files` committed on HEAD."""
    if shutil.which("git") is None:  # pragma: no cover - depends on the machine
        pytest.skip("git is not installed")
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for name, blob in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _container(tmp_path: Path) -> Path:
    """The workspace root: a plain directory that is not a checkout itself."""
    container = tmp_path / "workspace"
    container.mkdir(parents=True, exist_ok=True)
    return container


def _spy_on_git_diff(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every `(cwd, path)` handed to `git_diff`, and fork nothing.

    Both bindings are replaced -- the name `file_view` imported and the attribute
    on `rhizome_graph.diff` -- so the spy sees the call whichever import style the
    implementation uses. Returning ``None`` is what `git_diff` answers when there
    is no diff, so the caller keeps walking its own order.
    """
    calls: list[tuple[str, str]] = []

    async def recording(root: str, relative_path: str, *args, **kwargs):
        calls.append((root, relative_path))
        return None

    monkeypatch.setattr(file_view_module, "git_diff", recording, raising=False)
    monkeypatch.setattr(diff_module, "git_diff", recording, raising=False)
    return calls


def _spy_on_owning_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    """Record every `(observed_root, absolute_path)` the checkout question got.

    The real answer is passed through: this watches the seam, it does not stub
    it. Both bindings are replaced for the same reason as above, and the one on
    `file_view` is created if absent so the spy is live before the branch exists.
    """
    calls: list[tuple[str, str]] = []
    real = checkouts_module.owning_checkout

    def recording(observed_root: str, absolute_path: str):
        calls.append((observed_root, absolute_path))
        return real(observed_root, absolute_path)

    monkeypatch.setattr(checkouts_module, "owning_checkout", recording)
    monkeypatch.setattr(
        file_view_module, "owning_checkout", recording, raising=False
    )
    return calls


# --- 4.1 A deletion inside a sub-repository is still openable ---------------

def test_a_file_deleted_from_a_sub_repository_still_opens_as_its_diff(
    tmp_path: Path,
):
    """The row the status panel most wants to offer is the one that breaks.

    Present in HEAD, gone from disk: the whole removed content is sitting in
    `git diff HEAD`, and today the container's failed fork sends the click on to
    the existence check, which answers "no such file".
    """
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})
    (container / "a" / "x.txt").unlink()

    assert _run(file_view(str(container), "a/x.txt"))["mode"] == "diff"


def test_the_removed_lines_of_a_sub_repository_deletion_reach_the_panel(
    tmp_path: Path,
):
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})
    (container / "a" / "x.txt").unlink()

    content = _run(file_view(str(container), "a/x.txt"))["content"]

    assert "-old" in content


def test_a_deletion_inside_a_sub_repository_answers_without_an_error(
    tmp_path: Path,
):
    container = _container(tmp_path)
    _repo(container / "a", {"src/app.ts": b"x\n"})
    (container / "a" / "src" / "app.ts").unlink()

    assert not _run(file_view(str(container), "a/src/app.ts"))["error"]


# --- 4.2 The single-repo case is byte-for-byte what it was ------------------

def test_a_single_repository_root_is_still_the_working_directory_git_is_run_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The regression jaw: same cwd string, same path string, one call.

    Equality on the whole call rather than on `realpath`s, because "unchanged"
    here means the characters, not merely the directory they name.
    """
    root = _repo(tmp_path / "proj", {"a.txt": b"old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    calls = _spy_on_git_diff(monkeypatch)

    _run(file_view(str(root), "a.txt"))

    assert calls == [(str(root), "a.txt")]


def test_a_symlinked_file_in_a_single_repository_is_still_diffed_as_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The deliberate asymmetry, pinned so a later refactor cannot tidy it away.

    `resolve_inside` answers a `realpath`, so a branch that diffed *that* would
    show the destination of the link the user clicked. The compat branch keeps
    the string it was handed; only the sub-repo branch pays the asymmetry, and
    only because the checkout is unknowable without resolving.
    """
    root = _repo(tmp_path / "proj", {"real.txt": b"old\n"})
    (root / "real.txt").write_text("changed\n", encoding="utf-8")
    (root / "link.txt").symlink_to(root / "real.txt")
    calls = _spy_on_git_diff(monkeypatch)

    _run(file_view(str(root), "link.txt"))

    assert calls == [(str(root), "link.txt")]


# --- 4.3 An observed root below a checkout is untouched ---------------------

def test_an_observed_root_below_a_checkout_still_runs_git_in_the_observed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Upward wins: the checkout is *above* the root, so there is no sub-repo.

    `owning_checkout` answers ``None`` for this shape by contract, which lands the
    click in the compat branch -- the same one `ctrl+L` into a subdirectory has
    always used.
    """
    root = _repo(tmp_path / "proj", {"sub/inner.txt": b"old\n"})
    (root / "sub" / "inner.txt").write_text("changed\n", encoding="utf-8")
    observed = root / "sub"
    calls = _spy_on_git_diff(monkeypatch)

    _run(file_view(str(observed), "inner.txt"))

    assert calls == [(str(observed), "inner.txt")]


# --- 4.4 The chokepoint stays single, and stays first -----------------------

def test_a_path_climbing_out_of_the_root_is_refused_before_a_checkout_is_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})
    calls = _spy_on_owning_checkout(monkeypatch)

    answer = _run(file_view(str(container), "../../etc/passwd"))

    assert answer["error"].startswith("refused:") and calls == []


def test_an_absolute_path_is_refused_before_a_checkout_is_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})
    calls = _spy_on_owning_checkout(monkeypatch)

    answer = _run(file_view(str(container), "/etc/passwd"))

    assert answer["error"].startswith("refused:") and calls == []


def test_a_path_with_a_nul_byte_is_refused_before_a_checkout_is_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})
    calls = _spy_on_owning_checkout(monkeypatch)

    answer = _run(file_view(str(container), "a/x.txt\x00.png"))

    assert answer["error"].startswith("refused:") and calls == []


def test_a_climb_out_through_a_sub_repository_is_refused_before_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The traversal shape this feature invents: a real sub-repo, then `..`.

    A router that split the incoming string on `/` to name the sub-repo would see
    a plausible `a` and run `git` there with the rest of the path; containment has
    to be decided on the resolved result, as it always was.
    """
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})
    (tmp_path / "secret.txt").write_text("PRIVATE TOKEN\n", encoding="utf-8")
    calls = _spy_on_owning_checkout(monkeypatch)

    answer = _run(file_view(str(container), "a/../../secret.txt"))

    assert answer["error"].startswith("refused:") and calls == []
    assert "PRIVATE TOKEN" not in answer["content"]


def test_a_symlink_to_a_checkout_outside_the_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A checkout reached through a link is still outside the observed root.

    The link's textual path is innocent, which is why resolution comes first; and
    that this repository is a perfectly good checkout must not buy it an answer.
    """
    container = _container(tmp_path)
    outside = _repo(tmp_path / "outside", {"x.txt": b"old\n"})
    (outside / "x.txt").write_text("changed\n", encoding="utf-8")
    (container / "link").symlink_to(outside)
    calls = _spy_on_owning_checkout(monkeypatch)

    answer = _run(file_view(str(container), "link/x.txt"))

    assert answer["error"].startswith("refused:") and calls == []
    assert answer["content"] == ""


def test_the_checkout_question_is_asked_about_the_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The positive half of the four tests above, which are otherwise vacuous.

    A "never called" assertion only guarantees an ordering if the call happens at
    all on the accepted path. This also pins *what* is asked: the resolved target,
    not the string the WebSocket delivered.
    """
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})
    (container / "a" / "x.txt").write_text("changed\n", encoding="utf-8")
    calls = _spy_on_owning_checkout(monkeypatch)

    _run(file_view(str(container), "a/x.txt"))

    assert len(calls) == 1
    observed_root, absolute_path = calls[0]
    assert os.path.realpath(observed_root) == os.path.realpath(str(container))
    assert os.path.realpath(absolute_path) == os.path.realpath(
        str(container / "a" / "x.txt")
    )


# --- 4.5 A modified file in a sub-repository, and how git names it ----------

def test_a_modified_file_in_a_sub_repository_opens_as_its_diff(tmp_path: Path):
    container = _container(tmp_path)
    _repo(container / "a", {"src/x.txt": b"old\n"})
    (container / "a" / "src" / "x.txt").write_text("changed\n", encoding="utf-8")

    assert _run(file_view(str(container), "a/src/x.txt"))["mode"] == "diff"


def test_the_diff_of_a_sub_repository_file_is_the_change_the_agent_made(
    tmp_path: Path,
):
    container = _container(tmp_path)
    _repo(container / "a", {"src/x.txt": b"old\n"})
    (container / "a" / "src" / "x.txt").write_text("changed\n", encoding="utf-8")

    content = _run(file_view(str(container), "a/src/x.txt"))["content"]

    assert "+changed" in content and "-old" in content


def test_the_diff_names_the_file_relative_to_the_checkout_that_owns_it(
    tmp_path: Path,
):
    """`git` prints the path it was given, under its own `a/` and `b/` prefixes.

    Those prefixes are git's, not this workspace's -- the sub-repo here is also
    called `a`, so a diff run from the *container* would have printed
    `a/a/src/x.txt` and this assertion is exactly what tells the two apart.
    """
    container = _container(tmp_path)
    _repo(container / "a", {"src/x.txt": b"old\n"})
    (container / "a" / "src" / "x.txt").write_text("changed\n", encoding="utf-8")

    content = _run(file_view(str(container), "a/src/x.txt"))["content"]

    assert "diff --git a/src/x.txt b/src/x.txt" in content


def test_git_runs_inside_the_sub_repository_with_a_path_relative_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    container = _container(tmp_path)
    _repo(container / "a", {"src/x.txt": b"old\n"})
    (container / "a" / "src" / "x.txt").write_text("changed\n", encoding="utf-8")
    calls = _spy_on_git_diff(monkeypatch)

    _run(file_view(str(container), "a/src/x.txt"))

    assert len(calls) == 1
    cwd, path = calls[0]
    assert os.path.realpath(cwd) == os.path.realpath(str(container / "a"))
    assert path == "src/x.txt"


# --- The two remaining properties -------------------------------------------

def test_the_path_handed_to_git_never_climbs_out_of_the_sub_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The checkout was found by walking *up from* the target, so this holds.

    Stated as the property rather than as one expected string: nothing relative
    may begin with `..`, nothing may be absolute, and the two together must still
    name the file that was clicked.
    """
    container = _container(tmp_path)
    _repo(container / "a", {"deep/nested/x.txt": b"old\n"})
    target = container / "a" / "deep" / "nested" / "x.txt"
    target.write_text("changed\n", encoding="utf-8")
    calls = _spy_on_git_diff(monkeypatch)

    _run(file_view(str(container), "a/deep/nested/x.txt"))

    assert len(calls) == 1
    cwd, path = calls[0]
    assert not os.path.isabs(path)
    assert ".." not in path.split("/")
    assert os.path.realpath(os.path.join(cwd, path)) == os.path.realpath(
        str(target)
    )


def test_the_checkout_is_derived_from_the_resolved_path_not_from_the_string(
    tmp_path: Path,
):
    """A path that wanders through one sub-repo and lands in another.

    `resolve_inside` allows it -- it comes back inside the root -- and the file it
    names belongs to `b`. An implementation that read the leading segment of the
    incoming string would run `git` in `a` with `../b/x.txt`, which is both the
    wrong answer and a path leaving the working tree it was given.
    """
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"a old\n"})
    _repo(container / "b", {"x.txt": b"b old\n"})
    (container / "b" / "x.txt").write_text("b changed\n", encoding="utf-8")

    answer = _run(file_view(str(container), "a/../b/x.txt"))

    assert answer["mode"] == "diff"
    assert "+b changed" in answer["content"]


# --- The order the module docstring documents, inside the new branch --------

def test_a_directory_in_a_sub_repository_is_still_refused_before_git_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`git diff HEAD -- src` produces the combined diff of everything under it.

    Now that a container reaches a real working tree, that combined diff is
    finally *reachable*, so the directory check earning its place ahead of git
    matters here more than it did before.
    """
    container = _container(tmp_path)
    _repo(container / "a", {"src/x.txt": b"old\n"})
    (container / "a" / "src" / "x.txt").write_text("changed\n", encoding="utf-8")
    calls = _spy_on_git_diff(monkeypatch)

    answer = _run(file_view(str(container), "a/src"))

    assert answer["error"] and calls == []


def test_a_sub_repository_itself_is_refused_as_a_directory(tmp_path: Path):
    container = _container(tmp_path)
    _repo(container / "a", {"src/x.txt": b"old\n"})
    (container / "a" / "src" / "x.txt").write_text("changed\n", encoding="utf-8")

    answer = _run(file_view(str(container), "a"))

    assert answer["error"] and answer["mode"] == "error"


def test_an_unchanged_file_in_a_sub_repository_still_falls_back_to_its_text(
    tmp_path: Path,
):
    """Nothing pending: there is no diff to show, so the content is the answer."""
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"hello\n"})

    answer = _run(file_view(str(container), "a/x.txt"))

    assert answer["mode"] == "text" and answer["content"] == "hello\n"


def test_a_file_in_a_plain_directory_of_the_workspace_is_still_text(
    tmp_path: Path,
):
    """Not every file under a workspace belongs to a checkout.

    `owning_checkout` answers ``None`` here, which is the compat branch, and the
    compat branch over a container is exactly today's behaviour: no repository,
    no diff, show the text.
    """
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})
    (container / "notes").mkdir()
    (container / "notes" / "todo.txt").write_text("loose\n", encoding="utf-8")

    answer = _run(file_view(str(container), "notes/todo.txt"))

    assert answer["mode"] == "text" and answer["content"] == "loose\n"


def test_a_path_that_never_existed_in_a_sub_repository_still_errors(
    tmp_path: Path,
):
    """git knows nothing about it either, so the last step of the order stands."""
    container = _container(tmp_path)
    _repo(container / "a", {"x.txt": b"old\n"})

    answer = _run(file_view(str(container), "a/never-there.txt"))

    assert answer["error"] == "no such file"


def test_a_modified_binary_in_a_sub_repository_still_prefers_its_diff(
    tmp_path: Path,
):
    """The order is fixed in the new branch too: what changed beats what is there."""
    container = _container(tmp_path)
    _repo(container / "a", {"logo.png": PNG_MAGIC})
    (container / "a" / "logo.png").write_bytes(PNG_MAGIC + b"\x01\x02\x03")

    assert _run(file_view(str(container), "a/logo.png"))["mode"] == "diff"
