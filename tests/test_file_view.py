"""Contract tests (RED) for graphagents.file_view.

Motivation: the graph shows *that* a file changed and nothing about *what* is in
it. Clicking a node opens a panel, and this module builds what that panel shows --
one frame, one decision, made in a fixed order:

  1. the path is refused, missing, or a directory -> ``error``, no content;
  2. the file has an uncommitted change -> ``mode: "diff"``;
  3. it is text -> ``mode: "text"``;
  4. it is binary -> ``mode: "hex"``.

The order is the point. A binary that was just modified is shown as its diff
("Binary files ... differ", which is what git has to say about it) rather than as
a hex dump of the new bytes, because the question the viewer clicked to ask was
"what did the agent just do to this file".

``resolve_inside`` is the security half. The path arrives over a WebSocket, as
text, and unlike the completion commands it is used to **read file contents**.
``../../etc/passwd``, ``/etc/passwd`` and a symlink planted inside the project
pointing at ``/etc`` are all the same attack, and the defense is the one
`_resolve_static_file` already uses for the HTTP side: resolve, then require the
result to sit under the root. Containment is decided *after* resolution, so a
path that wanders through ``..`` and comes back is fine.

``max_bytes`` exists because the panel goes over a socket to a browser: a 400 MB
core dump would be read into the daemon's memory, hex-expanded to four times its
size, and pushed down a WebSocket. The cap applies to the bytes read, in both
modes, and ``truncated`` tells the page to say so.

Nothing here raises: this runs on the loop that serves every connected browser.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from graphagents.file_view import file_view, resolve_inside

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


def _project(root: Path, files: dict[str, bytes]) -> Path:
    """A plain directory -- no repository, so nothing can ever be a diff."""
    root.mkdir(parents=True, exist_ok=True)
    for name, blob in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return root


# --- 1. resolve_inside: the path came off the network -----------------------

def test_a_file_inside_the_root_resolves_to_its_absolute_path(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    resolved = resolve_inside(str(root), "a.txt")

    assert resolved is not None
    assert os.path.realpath(resolved) == os.path.realpath(str(root / "a.txt"))


def test_a_file_in_a_subdirectory_resolves(tmp_path: Path):
    root = _project(tmp_path / "proj", {"src/app.ts": b"x\n"})

    resolved = resolve_inside(str(root), "src/app.ts")

    assert resolved is not None
    assert os.path.realpath(resolved) == os.path.realpath(str(root / "src" / "app.ts"))


def test_a_path_that_climbs_out_of_the_root_is_refused(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})
    (tmp_path / "secret.txt").write_text("private\n", encoding="utf-8")

    assert resolve_inside(str(root), "../secret.txt") is None


def test_a_path_climbing_several_levels_is_refused(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert resolve_inside(str(root), "../../../../etc/passwd") is None


def test_an_absolute_path_is_refused(tmp_path: Path):
    # `os.path.join(root, "/etc/passwd")` is "/etc/passwd"; the join alone is not
    # a containment check.
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert resolve_inside(str(root), "/etc/passwd") is None


def test_a_symlink_pointing_out_of_the_root_is_refused(tmp_path: Path):
    # The link sits *inside* the project, so the textual path looks innocent; only
    # resolving it shows where it lands.
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})
    outside = tmp_path / "secret.txt"
    outside.write_text("private\n", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    assert resolve_inside(str(root), "link.txt") is None


def test_a_path_that_wanders_and_comes_back_is_allowed(tmp_path: Path):
    # Containment is decided after resolution, not by banning ".." textually.
    root = _project(tmp_path / "proj", {"src/app.ts": b"x\n"})

    resolved = resolve_inside(str(root), "src/../src/app.ts")

    assert resolved is not None
    assert os.path.realpath(resolved) == os.path.realpath(str(root / "src" / "app.ts"))


def test_a_path_with_a_nul_byte_is_refused_and_does_not_raise(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert resolve_inside(str(root), "a\x00.txt") is None


# --- 2. file_view: the frame the panel renders ------------------------------

def test_the_answer_is_a_file_view_frame(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert _run(file_view(str(root), "a.txt"))["kind"] == "fileView"


def test_the_answer_echoes_the_path_it_was_asked_about(tmp_path: Path):
    # The page keeps the panel keyed by path: a second click while the first
    # answer is still travelling must not paint the wrong file's content.
    root = _project(tmp_path / "proj", {"src/app.ts": b"x\n"})

    assert _run(file_view(str(root), "src/app.ts"))["path"] == "src/app.ts"


def test_the_answer_is_serializable_as_it_stands(tmp_path: Path):
    # It goes on the wire as JSON; a `Path` or `bytes` smuggled in whole would
    # raise inside the send, on the daemon's loop.
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    answer = _run(file_view(str(root), "a.txt"))

    assert json.loads(json.dumps(answer))["path"] == "a.txt"


def test_a_refused_path_answers_with_an_error(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})
    (tmp_path / "secret.txt").write_text("private\n", encoding="utf-8")

    assert _run(file_view(str(root), "../secret.txt"))["error"]


def test_a_refused_path_leaks_no_content(tmp_path: Path):
    # The whole point of the refusal: the bytes must not travel anyway.
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})
    (tmp_path / "secret.txt").write_text("private\n", encoding="utf-8")

    answer = _run(file_view(str(root), "../secret.txt"))

    assert "private" not in str(answer.get("content") or "")


def test_a_missing_file_answers_with_an_error(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert _run(file_view(str(root), "gone.txt"))["error"]


def test_a_directory_answers_with_an_error(tmp_path: Path):
    # Nodes in the graph include directories, and one is perfectly clickable.
    root = _project(tmp_path / "proj", {"src/app.ts": b"x\n"})

    assert _run(file_view(str(root), "src"))["error"]


def test_a_readable_file_answers_with_no_error(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert not _run(file_view(str(root), "a.txt"))["error"]


def test_a_text_file_outside_a_repository_is_shown_as_text(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert _run(file_view(str(root), "a.txt"))["mode"] == "text"


def test_a_text_file_is_shown_with_its_content(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": "olá\nmundo\n".encode("utf-8")})

    assert _run(file_view(str(root), "a.txt"))["content"] == "olá\nmundo\n"


def test_an_unchanged_tracked_file_falls_back_to_its_text(tmp_path: Path):
    # In a repository, but with nothing pending: there is no diff to show.
    root = _repo(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert _run(file_view(str(root), "a.txt"))["mode"] == "text"


def test_a_file_with_an_uncommitted_change_is_shown_as_a_diff(tmp_path: Path):
    root = _repo(tmp_path / "proj", {"a.txt": b"old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")

    assert _run(file_view(str(root), "a.txt"))["mode"] == "diff"


def test_the_diff_is_what_the_panel_gets_as_content(tmp_path: Path):
    root = _repo(tmp_path / "proj", {"a.txt": b"old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")

    content = _run(file_view(str(root), "a.txt"))["content"]

    assert "+changed" in content and "-old" in content


def test_a_binary_file_is_shown_as_a_hex_dump(tmp_path: Path):
    root = _project(tmp_path / "proj", {"logo.png": PNG_MAGIC})

    assert _run(file_view(str(root), "logo.png"))["mode"] == "hex"


def test_the_hex_dump_is_what_the_panel_gets_as_content(tmp_path: Path):
    root = _project(tmp_path / "proj", {"logo.png": PNG_MAGIC})

    content = _run(file_view(str(root), "logo.png"))["content"]

    assert content.startswith("00000000: 8950 4e47")


def test_a_deleted_file_is_shown_as_the_diff_of_its_removal(tmp_path: Path):
    # The order used to be "missing -> error" *before* the diff, which made the
    # one file the status panel most wants to offer -- a deletion -- unopenable:
    # clicking it answered "no such file" while `git diff HEAD` had the whole
    # removed content to show. Existence is now only asked about once git has
    # said it knows nothing either.
    root = _repo(tmp_path / "proj", {"a.txt": b"old\n"})
    (root / "a.txt").unlink()

    assert _run(file_view(str(root), "a.txt"))["mode"] == "diff"


def test_the_removed_lines_are_what_the_panel_gets_for_a_deleted_file(tmp_path: Path):
    root = _repo(tmp_path / "proj", {"a.txt": b"old\n"})
    (root / "a.txt").unlink()

    content = _run(file_view(str(root), "a.txt"))["content"]

    assert "-old" in content


def test_a_deleted_file_answers_without_an_error(tmp_path: Path):
    root = _repo(tmp_path / "proj", {"src/app.ts": b"x\n"})
    (root / "src" / "app.ts").unlink()

    assert not _run(file_view(str(root), "src/app.ts"))["error"]


def test_a_path_that_never_existed_in_a_repository_still_errors(tmp_path: Path):
    # git has nothing to say about it either, so the fallback is unchanged.
    root = _repo(tmp_path / "proj", {"a.txt": b"old\n"})

    assert _run(file_view(str(root), "never-there.txt"))["error"]


def test_a_missing_file_outside_a_repository_still_errors(tmp_path: Path):
    # The pre-existing contract, now reached one step later in the order.
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    answer = _run(file_view(str(root), "gone.txt"))

    assert answer["error"] and answer["mode"] == "error" and answer["content"] == ""


def test_a_directory_is_refused_before_git_is_ever_asked(tmp_path: Path):
    # `git diff HEAD -- src` happily produces a diff for a whole directory; the
    # directory check has to keep coming first, or clicking a folder opens the
    # combined diff of everything under it.
    root = _repo(tmp_path / "proj", {"src/app.ts": b"old\n"})
    (root / "src" / "app.ts").write_text("changed\n", encoding="utf-8")

    assert _run(file_view(str(root), "src"))["error"]


def test_a_modified_binary_still_prefers_its_diff(tmp_path: Path):
    # The order is fixed: the viewer clicked to ask what the agent did to this
    # file, and "Binary files differ" answers that better than the new bytes.
    root = _repo(tmp_path / "proj", {"logo.png": PNG_MAGIC})
    (root / "logo.png").write_bytes(PNG_MAGIC + b"\x01\x02\x03")

    assert _run(file_view(str(root), "logo.png"))["mode"] == "diff"


# --- 3. the cap: this frame goes down a WebSocket ---------------------------

def test_a_small_file_is_not_marked_truncated(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert _run(file_view(str(root), "a.txt"))["truncated"] is False


def test_a_file_exactly_at_the_cap_is_not_truncated(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"x" * 64})

    assert _run(file_view(str(root), "a.txt", max_bytes=64))["truncated"] is False


def test_a_text_file_past_the_cap_is_marked_truncated(tmp_path: Path):
    root = _project(tmp_path / "proj", {"big.txt": b"x" * 5000})

    assert _run(file_view(str(root), "big.txt", max_bytes=64))["truncated"] is True


def test_a_text_file_past_the_cap_is_cut_at_the_cap(tmp_path: Path):
    # The whole 5000 bytes must not be read into the daemon and pushed to the
    # browser; only the first `max_bytes` are.
    root = _project(tmp_path / "proj", {"big.txt": b"x" * 5000})

    content = _run(file_view(str(root), "big.txt", max_bytes=64))["content"]

    assert len(content) == 64


def test_a_binary_file_past_the_cap_is_marked_truncated(tmp_path: Path):
    root = _project(tmp_path / "proj", {"big.bin": PNG_MAGIC + bytes(5000)})

    assert _run(file_view(str(root), "big.bin", max_bytes=64))["truncated"] is True


def test_a_binary_file_past_the_cap_dumps_only_the_capped_bytes(tmp_path: Path):
    # 64 bytes at 16 per line is four lines, the last one addressed 0x30.
    root = _project(tmp_path / "proj", {"big.bin": PNG_MAGIC + bytes(5000)})

    content = _run(file_view(str(root), "big.bin", max_bytes=64))["content"]

    assert [line[:8] for line in content.splitlines()] == [
        "00000000",
        "00000010",
        "00000020",
        "00000030",
    ]
