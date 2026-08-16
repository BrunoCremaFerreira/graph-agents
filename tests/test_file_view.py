"""Contract tests (RED) for rhizome_graph.file_view.

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
import contextlib
import json
import os
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest

from rhizome_graph.diff import git_diff
from rhizome_graph.file_view import file_view, resolve_inside

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


# --- 4. the diff route must obey the same containment as the read route -----
#
# The defect these specify, measured against a live daemon on git 2.43.0:
# `resolve_inside` guards the *file* route and the diff route walks straight past
# it. The path is validated and then the **raw, unvalidated string** is handed to
# `git_diff`, which puts it after `--` and lets `git` read it as a **pathspec** --
# a query language, not a file name. `--` ends option parsing only; magic is
# parsed after it.
#
# `resolve_inside` cannot catch this, and it is worth being precise about why:
# `:/secret.txt` is not absolute and contains no `..`, so joining it onto the
# root normalizes to a path *inside* the root -- a nonexistent one, which
# `realpath` returns without complaint -- and containment passes. `git` then reads
# the same characters as "secret.txt at the top of the REPOSITORY", which is
# above the observed root whenever `ctrl+L` has pointed the daemon at a
# subdirectory (the case `status.relativize` exists to support). The measured
# result, root `.../proj/sub`:
#
#     ../secret.txt   -> error, "refused: outside the observed project"
#     :/secret.txt    -> mode "diff", the file's content, old side included
#
# The honest spelling is refused and the magic one is served. `:/` on its own
# bypasses the directory check too and returned the whole repository's diff
# (54 MB, uncapped -- `max_bytes` is on the text/hex route only).
#
# The last two tests are the guard against a fix that is merely a wider refusal:
# an ordinary path must still open, and a file whose name legitimately begins
# with a character `git` treats specially must not become an error.

def _split_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout whose top holds a secret, with `sub/` as the observed root.

    Everything is left modified so that everything has a diff to leak:
    `secret.txt` above the observed root, and `inner.txt` / `other.txt` inside it
    -- `other.txt` being the file no test ever names.
    """
    root = _repo(
        tmp_path / "proj",
        {
            "secret.txt": b"old secret\n",
            "sub/inner.txt": b"old inner\n",
            "sub/other.txt": b"old other\n",
        },
    )
    (root / "secret.txt").write_bytes(b"PRIVATE TOKEN\n")
    (root / "sub" / "inner.txt").write_bytes(b"changed inner\n")
    (root / "sub" / "other.txt").write_bytes(b"changed other\n")
    return root, root / "sub"


def test_repository_relative_magic_is_answered_with_an_error(tmp_path: Path):
    _, sub = _split_repo(tmp_path)

    assert _run(file_view(str(sub), ":/secret.txt"))["error"]


def test_repository_relative_magic_leaks_no_content_from_above_the_root(tmp_path: Path):
    # The point of the refusal, in the terms that matter: not the frame's shape,
    # but that the bytes do not travel.
    _, sub = _split_repo(tmp_path)

    answer = _run(file_view(str(sub), ":/secret.txt"))

    assert "PRIVATE TOKEN" not in str(answer.get("content") or "")


def test_the_long_form_of_the_top_magic_leaks_nothing_either(tmp_path: Path):
    # One of several spellings. `--literal-pathspecs` neutralizes them together;
    # a fix that pattern-matched on `:/` alone would still be open here.
    _, sub = _split_repo(tmp_path)

    answer = _run(file_view(str(sub), ":(top)secret.txt"))

    assert "PRIVATE TOKEN" not in str(answer.get("content") or "")


def test_the_bare_root_pathspec_is_not_answered_with_a_diff(tmp_path: Path):
    # `:/` is not a file name at all, and it slips past the directory check as
    # well: against a real checkout this came back as the whole repository's
    # pending diff, on a route where `max_bytes` does not apply.
    _, sub = _split_repo(tmp_path)

    assert _run(file_view(str(sub), ":/"))["mode"] != "diff"


def test_the_bare_root_pathspec_leaks_no_file_from_above_the_root(tmp_path: Path):
    _, sub = _split_repo(tmp_path)

    answer = _run(file_view(str(sub), ":/"))

    assert "PRIVATE TOKEN" not in str(answer.get("content") or "")


def test_the_honest_spelling_of_the_same_escape_is_still_refused(tmp_path: Path):
    # The existing defence, restated inside this fixture: if this ever passes
    # while the magic ones fail, the wrong thing was fixed.
    _, sub = _split_repo(tmp_path)

    answer = _run(file_view(str(sub), "../secret.txt"))

    assert answer["error"] and "PRIVATE TOKEN" not in str(answer.get("content") or "")


def test_a_glob_does_not_open_a_file_the_click_never_named(tmp_path: Path):
    # A glob does not escape the root; it widens the question. One click on one
    # node must not come back carrying a sibling's content.
    _, sub = _split_repo(tmp_path)

    answer = _run(file_view(str(sub), "*.txt"))

    assert "changed other" not in str(answer.get("content") or "")


def test_a_single_character_wildcard_does_not_stand_in_for_a_real_file(tmp_path: Path):
    _, sub = _split_repo(tmp_path)

    answer = _run(file_view(str(sub), "?nner.txt"))

    assert "changed inner" not in str(answer.get("content") or "")


def test_an_ordinary_path_inside_the_root_still_opens_its_diff(tmp_path: Path):
    # The regression guard: "refuse everything" is not a fix.
    _, sub = _split_repo(tmp_path)

    answer = _run(file_view(str(sub), "inner.txt"))

    assert answer["mode"] == "diff" and "+changed inner" in answer["content"]


def test_a_file_whose_name_begins_with_a_colon_still_opens_its_diff(tmp_path: Path):
    # `:notes.txt` is a legal name on disk and an ordinary node on the graph. It
    # is also the same defect seen from the other side: `git` reads the leading
    # colon as an empty magic prefix and reports nothing, so the panel shows the
    # new text with no diff at all. Neutralizing the magic has to fix this too,
    # rather than turn the file into an error.
    root = _repo(tmp_path / "proj", {":notes.txt": b"old\n"})
    (root / ":notes.txt").write_bytes(b"changed\n")

    assert _run(file_view(str(root), ":notes.txt"))["mode"] == "diff"


# --- 5. the diff route obeys the cap too ------------------------------------
#
# The defect these specify, measured on this tree: `max_bytes` guards the text
# and hex routes and stops at the diff. `_read_capped` reads one byte past the
# cap, hands back at most `max_bytes` and says `truncated: True`; the diff branch
# returns whatever `git diff` printed, verbatim, with `truncated` left at its
# default of False. The same 400 000-line file, in a scratch repository:
#
#     untracked -> mode "text", 262 144 chars (exactly the cap), truncated True
#     tracked   -> mode "diff", 17 777 895 chars (68x the cap), truncated False
#
# One click, half a second, one frame, JSON-encoded on the way out (which roughly
# doubles it again). `MAX_ROWS` bounds what the browser draws; nothing bounds
# what crosses the socket.
#
# Two properties are worth more than the number itself.
#
# **The cut lands on a line boundary.** The panel parses this text back into
# hunks and rows; a frame ending halfway through `@@ -1,7 +1,` hands the parser a
# header it cannot read, which is a worse failure than the size it was avoiding.
# A bare slice at `max_bytes` gets this wrong, so it is stated on its own.
#
# **A line longer than the whole cap must still show something.** Trimming back
# to the last newline is the obvious way to land on a boundary, and on a minified
# bundle -- one line, megabytes long, exactly the file most likely to blow the cap
# -- it trims back to nothing and the panel opens empty. That is the edge these
# pin, because it is invisible in every other test.
#
# The decision belongs in a named, pure function rather than an inline slice, for
# the reason everything else in this project is split that way: `file_view` needs
# a repository and an event loop to exercise, and the cut does not. It is
# specified here as `cap_text(text, max_bytes) -> (text, truncated)`.
#
# **The unit is bytes, and that is a decision, not a transcription.** The
# constant says bytes, the reason for it is what the socket carries, and the text
# route already measures in bytes -- it caps the `read()` and only then decodes,
# so a file of accented text yields fewer *characters* than `max_bytes`. The diff
# arrives already decoded, so the same rule has to be stated rather than
# inherited: no frame may carry more than `max_bytes` of UTF-8. On ASCII, which
# is all the existing cap tests use, the two units agree and nothing changes.


def _cap(text: str, max_bytes: int) -> tuple[str, bool]:
    """`cap_text`, imported where it is used.

    Deliberately not at module scope: the function does not exist yet, and a
    top-level import of a missing name fails at *collection*, turning every test
    in this file red instead of the ones that are actually specifying it.
    """
    from rhizome_graph.file_view import cap_text

    return cap_text(text, max_bytes)


def test_text_within_the_cap_comes_back_whole():
    assert _cap("first\nsecond\n", 1024)[0] == "first\nsecond\n"


def test_text_within_the_cap_is_not_flagged_as_truncated():
    assert _cap("first\nsecond\n", 1024)[1] is False


def test_text_exactly_at_the_cap_is_not_truncated():
    # The same edge `test_a_file_exactly_at_the_cap_is_not_truncated` pins for
    # the read: "as much as allowed" is not "more than allowed".
    text = "abc\n" * 16  # 64 bytes

    assert _cap(text, 64) == (text, False)


def test_text_past_the_cap_is_flagged_as_truncated():
    assert _cap("0123456789\n" * 20, 64)[1] is True


def test_the_capped_text_never_exceeds_the_cap_in_bytes():
    capped, _ = _cap("0123456789\n" * 20, 64)

    assert len(capped.encode("utf-8")) <= 64


def test_the_capped_text_is_a_prefix_of_the_original():
    # No ellipsis, no re-wrapping, no marker appended: what the panel receives
    # has to be the beginning of the real diff, or the rows it draws are not the
    # rows git produced.
    text = "0123456789\n" * 20

    capped, _ = _cap(text, 64)

    assert text.startswith(capped)


def test_the_cut_lands_on_a_line_boundary():
    # 11-byte lines against a 64-byte cap: five fit, six do not, and the sixth
    # must not be handed over half-written.
    capped, _ = _cap("0123456789\n" * 20, 64)

    assert capped.endswith("\n")


def test_multibyte_text_is_capped_by_what_it_weighs_on_the_wire():
    # The cap is named in bytes and exists because of what crosses the socket;
    # counting characters would let this frame out at three times the size.
    capped, _ = _cap("ééééééééé\n" * 20, 64)

    assert len(capped.encode("utf-8")) <= 64


def test_a_line_longer_than_the_cap_still_comes_back_with_content():
    # A minified bundle is one line and megabytes long -- the very file most
    # likely to hit the cap. Trimming back to the last newline leaves nothing at
    # all, and the panel opens blank on the file the user most wanted to see.
    capped, _ = _cap("x" * 5000 + "\n", 64)

    assert capped != ""


def test_a_line_longer_than_the_cap_is_still_cut_to_the_cap():
    capped, _ = _cap("x" * 5000 + "\n", 64)

    assert len(capped.encode("utf-8")) <= 64


def _rewritten_repo(tmp_path: Path) -> Path:
    """A checkout whose one tracked file has been rewritten line for line.

    Two hundred lines replaced is a diff of some kilobytes -- small next to the
    17 MB that was measured, and far past any cap these tests pass in.
    """
    root = _repo(
        tmp_path / "proj",
        {"big.txt": b"".join(b"old line %d\n" % n for n in range(200))},
    )
    (root / "big.txt").write_bytes(b"".join(b"new line %d\n" % n for n in range(200)))
    return root


def test_a_diff_past_the_cap_is_marked_truncated(tmp_path: Path):
    root = _rewritten_repo(tmp_path)

    assert _run(file_view(str(root), "big.txt", max_bytes=512))["truncated"] is True


def test_a_diff_past_the_cap_is_cut_at_the_cap(tmp_path: Path):
    root = _rewritten_repo(tmp_path)

    content = _run(file_view(str(root), "big.txt", max_bytes=512))["content"]

    assert len(content.encode("utf-8")) <= 512


def test_a_truncated_diff_ends_on_a_line_boundary(tmp_path: Path):
    # The panel parses this back into hunks and rows; half a hunk header is not
    # something it can draw.
    root = _rewritten_repo(tmp_path)

    answer = _run(file_view(str(root), "big.txt", max_bytes=512))

    # The flag is the precondition, not a second property: the boundary only
    # means anything on a frame the cap actually cut, and an uncut frame would
    # satisfy the second half of this by accident.
    assert answer["truncated"] and answer["content"].endswith("\n")


def test_a_truncated_diff_is_the_beginning_of_the_real_diff(tmp_path: Path):
    # Against `git` itself, not against a guess about its output: whatever the
    # panel gets must be a prefix of what the diff actually says -- no ellipsis,
    # no "... 400 more lines" appended into the text the parser reads.
    root = _rewritten_repo(tmp_path)

    answer = _run(file_view(str(root), "big.txt", max_bytes=512))
    whole = _run(git_diff(str(root), "big.txt"))

    assert answer["truncated"] and (whole or "").startswith(answer["content"])


def test_a_truncated_diff_is_still_a_diff(tmp_path: Path):
    # The cap shortens the frame; it does not change what the frame is. Refusing
    # an oversized diff with an error would pass every size assertion above and
    # still lose the panel.
    root = _rewritten_repo(tmp_path)

    answer = _run(file_view(str(root), "big.txt", max_bytes=512))

    assert answer["truncated"] and answer["mode"] == "diff" and not answer["error"]


def test_a_small_diff_still_arrives_whole(tmp_path: Path):
    # The regression guard: capping every frame to the same length, or flagging
    # frames that were never cut, is not a fix.
    root = _repo(tmp_path / "proj", {"a.txt": b"old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")

    answer = _run(file_view(str(root), "a.txt"))

    assert "+changed" in answer["content"] and "-old" in answer["content"]


def test_a_small_diff_is_not_marked_truncated(tmp_path: Path):
    root = _repo(tmp_path / "proj", {"a.txt": b"old\n"})
    (root / "a.txt").write_text("changed\n", encoding="utf-8")

    assert _run(file_view(str(root), "a.txt"))["truncated"] is False


# --- 6. only a regular file may be opened -----------------------------------
#
# The defect these specify, measured on this machine (CPython 3.12.3, 4 CPUs):
# `file_view` asks `os.path.exists` and then opens. A **named pipe** passes both
# -- it exists, it is not a directory -- and `open(target, "rb")` blocks inside
# `open(2)` until a writer appears. With no writer, that is forever:
#
#     [pipe] NEVER ANSWERED
#
# The worker never comes back. The loop's default executor here is
# `min(32, cpu + 4)` = 8 threads, so eight clicks on that node take the whole
# file layer down -- and not only the file layer: `Session.switch_root` runs
# `scan_tree` through the same `asyncio.to_thread`, so `ctrl+L` stops working
# too. The daemon keeps accepting connections and broadcasting events the whole
# time: alive on screen, dead on click.
#
# It is worse than a leak. The process cannot exit either: `asyncio.run` calls
# `shutdown_default_executor`, which joins the wedged threads. The measurement
# above had to be taken with an external timeout, and the probe exited 124 with
# every answer already printed.
#
# No bad faith is needed to reach it. A build system's named pipe inside the
# observed project is seeded into the graph like any other node, and one click
# does it.
#
# Two notes on what is *not* broken:
#
#   * A unix socket already answers, promptly: `open(2)` refuses it outright
#     (ENXIO) and the existing `except` turns that into `error: "unreadable"`.
#     It is specified here anyway, because a rule about which file types may be
#     opened that happens to be enforced by an errno is not a rule.
#   * A directory is caught earlier, by its own check, and keeps its own message.
#     That ordering is pinned below so a new type check does not absorb it.
#
# The decision is specified as a pure predicate, `is_readable_regular(st_mode)`,
# for the reason `cap_text` is pure: "which file types may be read" is one
# decision, it needs no filesystem to state, and character and block devices can
# only be honestly tested that way -- `/dev/zero` lives outside any observed root
# and `mknod` needs root.
#
# **What these tests deliberately do not pin: the open-versus-stat race.** The
# implementation most likely to be reached for is `os.open(target,
# O_RDONLY | O_NONBLOCK)` followed by `fstat` on the descriptor, and it is the
# better one, because it checks the type of the thing it actually opened -- a
# `stat`, then `open` sequence can have a regular file replaced by a FIFO between
# the two calls. That window cannot be opened reliably from a test without
# reaching into the implementation, so it is not asserted here: a `stat`-then-open
# fix passes everything below. This paragraph is the record of the reason.


def _open_write_end(fifo: str, deadline_seconds: float = 2.0) -> bool:
    """Free a worker blocked in `open(2)` on `fifo`, by opening the other end.

    This is test *hygiene*, not part of the specification. A thread wedged in
    `open(2)` cannot be cancelled, and the executor is joined when the loop
    closes and again at interpreter exit -- so a single unrescued wedge does not
    merely leak, it hangs the pytest process on the way out. Opening the write
    end is what a real writer would do: the blocked `open` returns, the following
    `read` sees EOF, and the thread finishes.

    `O_NONBLOCK` makes the rescue itself safe: it fails with ENXIO instead of
    blocking when no reader is waiting, which is also how it can be retried.
    """
    ends_at = time.monotonic() + deadline_seconds
    while time.monotonic() < ends_at:
        try:
            handle = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.02)
            continue
        os.close(handle)
        return True
    return False


def _view_within(
    root: str, relative_path: str, timeout: float = 2.0, rescue: str | None = None
) -> dict:
    """`file_view`, with promptness as part of the contract.

    Answering *eventually* is not the property under test -- a click that never
    comes back is the defect -- so the wait is bounded and running out of it is a
    failure, reported as a `TimeoutError` naming the path rather than as a run
    that hangs.
    """

    async def attempt() -> dict:
        task = asyncio.ensure_future(file_view(root, relative_path))
        try:
            # Shielded: the timeout must not cancel the task, because the thread
            # underneath it cannot be cancelled anyway and the rescue below needs
            # something to wait on.
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            for _ in range(3):
                if task.done():
                    break
                if rescue is not None:
                    _open_write_end(rescue)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(task), 2.0)
            raise TimeoutError(
                f"file_view({relative_path!r}) did not answer within {timeout}s"
            ) from None

    return asyncio.run(attempt())


def _predicate(st_mode: int) -> bool:
    """`is_readable_regular`, imported where it is used.

    Not at module scope, for the reason given on `_cap`: a top-level import of a
    name that does not exist yet fails at collection and reddens the whole file.
    """
    from rhizome_graph.file_view import is_readable_regular

    return is_readable_regular(st_mode)


# --- 6a. the predicate, stated without a filesystem -------------------------

def test_a_regular_file_may_be_read():
    assert _predicate(stat.S_IFREG | 0o644) is True


def test_a_named_pipe_may_not_be_read():
    # The whole finding: this is the one that blocks in `open(2)` forever.
    assert _predicate(stat.S_IFIFO | 0o644) is False


def test_a_unix_socket_may_not_be_read():
    assert _predicate(stat.S_IFSOCK | 0o644) is False


def test_a_character_device_may_not_be_read():
    # `/dev/zero` never ends and `/dev/tty` can block; neither is a file the
    # panel has any business reading, and neither can be created in a tmpdir.
    assert _predicate(stat.S_IFCHR | 0o666) is False


def test_a_block_device_may_not_be_read():
    assert _predicate(stat.S_IFBLK | 0o660) is False


def test_a_directory_may_not_be_read():
    # Caught earlier by its own check; the predicate must not disagree with it.
    assert _predicate(stat.S_IFDIR | 0o755) is False


def test_a_symlink_mode_may_not_be_read():
    # `os.stat` follows links, so this bit pattern should never reach the
    # predicate. If it does, the caller used `lstat`, and the answer that keeps
    # `test_a_symlink_to_a_file_inside_the_root_still_opens` honest is "no":
    # what may be read is the type of the thing at the end of the link.
    assert _predicate(stat.S_IFLNK | 0o777) is False


def test_the_permission_bits_do_not_decide():
    # This answers "what kind of file is it", not "may this process read it".
    # Permission is the `unreadable` path, and it is not knowable from the mode
    # alone anyway -- root reads a 0o000 file.
    assert _predicate(stat.S_IFREG | 0o000) is True


def test_a_real_regular_file_on_disk_is_readable(tmp_path: Path):
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})

    assert _predicate(os.stat(root / "a.txt").st_mode) is True


def test_a_real_named_pipe_on_disk_is_not_readable(tmp_path: Path):
    root = _project(tmp_path / "proj", {})
    os.mkfifo(root / "pipe")

    assert _predicate(os.stat(root / "pipe").st_mode) is False


def test_a_real_unix_socket_on_disk_is_not_readable(tmp_path: Path):
    root = _project(tmp_path / "proj", {})
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(root / "sock"))

    try:
        assert _predicate(os.stat(root / "sock").st_mode) is False
    finally:
        server.close()


def test_a_real_character_device_is_not_readable():
    # The one type that cannot be reached from inside an observed root at all,
    # which is why the predicate is where it is specified.
    if not os.path.exists("/dev/null"):  # pragma: no cover - depends on the machine
        pytest.skip("no /dev/null on this machine")

    assert _predicate(os.stat("/dev/null").st_mode) is False


# --- 6b. the frame: a click on a pipe must come back ------------------------

def _project_with_fifo(tmp_path: Path) -> tuple[Path, str]:
    """A plain project holding a named pipe with nobody at the other end."""
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})
    fifo = root / "pipe"
    os.mkfifo(fifo)
    return root, str(fifo)


def test_a_named_pipe_answers_at_all(tmp_path: Path):
    root, fifo = _project_with_fifo(tmp_path)

    answer = _view_within(str(root), "pipe", rescue=fifo)

    assert answer["kind"] == "fileView"


def test_a_named_pipe_is_answered_with_an_error(tmp_path: Path):
    # The existing frame shape, not a new one: the page already knows how to
    # show `error`.
    root, fifo = _project_with_fifo(tmp_path)

    answer = _view_within(str(root), "pipe", rescue=fifo)

    assert answer["error"] and answer["mode"] == "error"


def test_a_named_pipe_carries_no_content(tmp_path: Path):
    root, fifo = _project_with_fifo(tmp_path)

    answer = _view_within(str(root), "pipe", rescue=fifo)

    assert answer["content"] == ""


def test_a_unix_socket_is_answered_with_an_error(tmp_path: Path):
    # Green today, by accident of errno rather than by rule -- see the section
    # header. It must stay true once the rule exists.
    root = _project(tmp_path / "proj", {})
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(root / "sock"))

    try:
        assert _view_within(str(root), "sock")["error"]
    finally:
        server.close()


def test_a_directory_keeps_its_own_error(tmp_path: Path):
    # The directory check comes first and stays first: "that is a directory"
    # tells the viewer what happened, and a type check that swallowed it would
    # answer the commonest click in the graph with something vaguer.
    root = _project(tmp_path / "proj", {"src/app.ts": b"x\n"})

    assert "directory" in _view_within(str(root), "src")["error"]


# --- 6c. what must keep working --------------------------------------------

def test_an_ordinary_file_still_opens(tmp_path: Path):
    root, _ = _project_with_fifo(tmp_path)

    answer = _view_within(str(root), "a.txt")

    assert answer["mode"] == "text" and answer["content"] == "hello\n"


def test_an_empty_file_still_opens(tmp_path: Path):
    # Zero bytes is a regular file with nothing in it, not a file that cannot be
    # read; it must not fall into the same bucket as the pipe.
    root = _project(tmp_path / "proj", {"empty.txt": b""})

    answer = _view_within(str(root), "empty.txt")

    assert answer["mode"] == "text" and not answer["error"]


def test_a_symlink_to_a_file_inside_the_root_still_opens(tmp_path: Path):
    # The type that matters is the target's. A check on the link itself refuses
    # every symlink in the project, and `resolve_inside` has already established
    # that this one lands inside the root.
    root = _project(tmp_path / "proj", {"a.txt": b"hello\n"})
    (root / "link.txt").symlink_to(root / "a.txt")

    answer = _view_within(str(root), "link.txt")

    assert answer["content"] == "hello\n"


def test_a_binary_file_still_opens_as_hex(tmp_path: Path):
    # The hex route goes through the same open; the new rule must not cost it.
    root = _project(tmp_path / "proj", {"logo.png": PNG_MAGIC})

    assert _view_within(str(root), "logo.png")["mode"] == "hex"
