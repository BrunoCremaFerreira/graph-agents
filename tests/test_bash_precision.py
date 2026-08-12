"""Contract tests (RED) for Bash parsing that stays silent when it would guess.

Observed end-to-end: `cp /proj/*.md /proj/docs/` made the hook emit `A "docs/"`.
That is wrong three times over -- it is a directory, not a file; the trailing
slash makes it a second node distinct from `docs`; and the files actually copied
were never reported. It then poisoned the delete path, because pruning `docs/`
matched the phantom node as if it were its own child.

The watcher now reports what such a command really did, file by file. So the
rule for the Bash parser is: when the target cannot be pinned to one concrete
file, emit nothing and let the watcher speak. Precision beats coverage here --
a wrong node stays on screen forever, a missing one is filled in milliseconds
later.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import pytest

from graphagents.normalize import normalize_event

ROOT = "/proj"


def _bash(command: str) -> dict:
    return {
        "session_id": "sess-abc",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def _event(command: str):
    return normalize_event(_bash(command), known_paths=set(), project_root=ROOT)


# --- 1. Directory destinations are not files -------------------------------

def test_copy_into_a_directory_emits_nothing():
    assert _event(f"cp {ROOT}/a.md {ROOT}/docs/") is None


def test_copy_of_several_sources_emits_nothing():
    # The destination of a multi-source copy is necessarily a directory.
    assert _event(f"cp {ROOT}/a.md {ROOT}/b.md {ROOT}/docs") is None


def test_move_into_a_directory_emits_nothing():
    assert _event(f"mv {ROOT}/a.md {ROOT}/docs/") is None


# --- 2. Globs name no single path ------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        f"cp {ROOT}/*.md {ROOT}/docs",
        f"rm {ROOT}/build/*.o",
        f"touch {ROOT}/src/?.tmp",
        f"rm {ROOT}/src/[ab].py",
    ],
)
def test_glob_operands_emit_nothing(command):
    assert _event(command) is None


# --- 3. A trailing slash must not create a twin node -----------------------

def test_trailing_slash_is_stripped_from_a_removed_directory():
    event = _event(f"rm -rf {ROOT}/build/")

    assert event is not None
    assert event.path == "build"


def test_made_directory_keeps_a_single_canonical_path():
    event = _event(f"mkdir {ROOT}/newdir/")

    assert event is not None
    assert event.path == "newdir"


# --- 4. Unambiguous commands still work ------------------------------------

def test_single_file_copy_is_still_reported():
    event = _event("cp src.txt dst.txt")

    assert event is not None
    assert event.type == "A"
    assert event.path == "dst.txt"


def test_single_file_removal_is_still_reported():
    event = _event("rm notes.txt")

    assert event is not None
    assert event.type == "D"
    assert event.path == "notes.txt"
