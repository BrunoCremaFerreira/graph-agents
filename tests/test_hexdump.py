"""Contract tests (RED) for graphagents.hexdump.

Motivation: clicking a node in the graph is about to open a panel showing what
that file actually contains. Text is easy; a `.png`, a `.pyc` or a `.so` is not.
Decoding one as UTF-8 produces either an exception or a screenful of replacement
characters, and either way the viewer learns nothing -- so a binary is shown the
way every developer already knows how to read one: `xxd`.

Two functions, both pure stdlib, both callable without a daemon or a browser:

  * ``xxd_dump(data, offset=0)`` -- the dump itself. "Like a hex dump" is not a
    specification; **the format is `xxd`'s, byte for byte**, so the decisive
    tests below run the real binary and compare. Where `xxd` is not installed the
    comparison is skipped rather than faked, and the hand-written expectations
    keep covering the shape.
  * ``looks_binary(data)`` -- the decision of *which* of the two panels to
    render. It reads the head of the content only: the panel is capped at a few
    hundred kilobytes and this runs on the daemon's event loop.

The edges that matter are the partial last line (the ASCII column must stay in
the same screen column or the dump reads as noise), bytes outside 0x20..0x7e
(shown as ``.``, never as themselves -- a raw 0x07 would beep the terminal of
anyone who copied the panel out), and empty input, which is a real file state and
not an error.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from graphagents.hexdump import looks_binary, xxd_dump

#: Where the ASCII column begins in `xxd`'s default output: 8 offset digits, a
#: colon, a space, 39 columns of hex (8 groups of 2 bytes, space-separated), then
#: two spaces. A partial last line is padded up to here.
ASCII_COLUMN = 51


def _real_xxd(data: bytes, tmp_path: Path, *args: str) -> str:
    """What the actual `xxd` binary prints for `data`, or skip the test."""
    if shutil.which("xxd") is None:  # pragma: no cover - depends on the machine
        pytest.skip("xxd is not installed; cannot compare against the real thing")
    target = tmp_path / "blob.bin"
    target.write_bytes(data)
    result = subprocess.run(
        ["xxd", *args, str(target)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# --- 1. xxd_dump: exactly what xxd prints -----------------------------------

def test_an_empty_file_dumps_to_nothing():
    # An empty file is an ordinary thing to click on; the panel shows an empty
    # dump, not a line of padding and not an error.
    assert xxd_dump(b"") == ""


def test_a_single_byte_matches_the_real_xxd(tmp_path: Path):
    data = b"A"

    assert xxd_dump(data) == _real_xxd(data, tmp_path)


def test_a_full_line_of_sixteen_bytes_matches_the_real_xxd(tmp_path: Path):
    data = b"0123456789abcdef"

    assert xxd_dump(data) == _real_xxd(data, tmp_path)


def test_a_partial_last_line_matches_the_real_xxd(tmp_path: Path):
    # 17 bytes: the case that gets the padding wrong.
    data = b"0123456789abcdef!"

    assert xxd_dump(data) == _real_xxd(data, tmp_path)


def test_every_byte_value_matches_the_real_xxd(tmp_path: Path):
    # 256 bytes, 16 full lines, every value from 0x00 to 0xff -- NULs, control
    # codes, high bytes and the printable range in one shot.
    data = bytes(range(256))

    assert xxd_dump(data) == _real_xxd(data, tmp_path)


def test_a_long_file_matches_the_real_xxd(tmp_path: Path):
    # Past 256 bytes the offset column stops being two digits wide; a dump built
    # by string concatenation of `%02x` addresses breaks exactly here.
    data = bytes((i * 7 + 3) % 256 for i in range(300))

    assert xxd_dump(data) == _real_xxd(data, tmp_path)


def test_a_starting_offset_shifts_the_addresses(tmp_path: Path):
    # The panel may dump only the first chunk of a large file, and a second chunk
    # has to be addressed from where it starts, not from zero.
    data = b"A"

    assert xxd_dump(data, offset=4096) == _real_xxd(data, tmp_path, "-o", "4096")


def test_the_ascii_column_of_a_partial_line_stays_aligned():
    # Held independently of the binary above, because this is the property a
    # reader depends on and the one machine without `xxd` still has to keep.
    lines = xxd_dump(b"0123456789abcdef!").splitlines()

    assert lines[1] == "00000010: 21" + " " * (ASCII_COLUMN - len("00000010: 21")) + "!"


def test_bytes_outside_the_printable_range_show_as_dots():
    # A raw 0x07 in the panel would beep the terminal of anyone who copied it
    # out; 0xff would depend on the reader's encoding to mean anything at all.
    line = xxd_dump(b"\x00\x07\x1f\x7f\x80\xff").splitlines()[0]

    assert line[ASCII_COLUMN:] == "......"


def test_the_printable_ascii_range_shows_as_itself():
    line = xxd_dump(b" ~az").splitlines()[0]

    assert line[ASCII_COLUMN:] == " ~az"


def test_each_line_of_the_dump_ends_in_a_newline():
    # Including the last one: the panel appends nothing, and `xxd` terminates
    # every line it prints.
    assert xxd_dump(b"0123456789abcdef!").endswith("!\n")


# --- 2. looks_binary: which panel to draw -----------------------------------

def test_a_nul_byte_near_the_start_means_binary():
    # The classic test, and the one that catches `.pyc`, `.png` and ELF.
    assert looks_binary(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") is True


def test_content_that_is_not_valid_utf8_means_binary():
    # No NUL here at all; the only evidence is that it does not decode.
    assert looks_binary(b"\xff\xfe\xfa\xfb") is True


def test_utf8_text_with_accents_is_not_binary():
    # The high bytes of "ação" must not be read as a binary marker: half this
    # project's own paths would be dumped as hex.
    assert looks_binary("ação — não é binário\n".encode("utf-8")) is False


def test_plain_ascii_text_is_not_binary():
    assert looks_binary(b"hello world\n\tindented\r\n") is False


def test_an_empty_file_is_not_binary():
    # Nothing in it to prove otherwise, and an empty text panel is the honest
    # answer; an empty hex dump is a puzzle.
    assert looks_binary(b"") is False
