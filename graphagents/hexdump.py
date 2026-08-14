"""A binary file, shown the way every developer already knows how to read one.

Clicking a node opens a panel with what the file contains. Text is easy; a
`.png`, a `.pyc` or a `.so` is not: decoding one as UTF-8 gives either an
exception or a screenful of replacement characters, and the viewer learns
nothing either way. So a binary is dumped in `xxd`'s default format -- not
"something hex-like", but that layout byte for byte, because the reader already
has the columns memorized.

Both functions are pure stdlib and never raise. :func:`looks_binary` reads only
the head of the content: it runs on the daemon's event loop, where a scan of
every byte of a few hundred kilobytes buys nothing the first line does not
already say.
"""

from __future__ import annotations

import codecs

#: Bytes per dumped line, as `xxd` prints them.
_BYTES_PER_LINE = 16

#: Bytes per space-separated group.
_GROUP_SIZE = 2

#: Width of the hex column: 8 groups of 4 hex digits plus the 7 spaces between
#: them. A partial last line is padded to it so the ASCII column of every line
#: starts in the same place -- misaligned, the dump reads as noise.
_HEX_WIDTH = 39

_PRINTABLE_LOW = 0x20
_PRINTABLE_HIGH = 0x7E

#: How much of the content :func:`looks_binary` inspects.
_SNIFF_BYTES = 8192


def xxd_dump(data: bytes, offset: int = 0) -> str:
    """`data` in `xxd`'s default format, addressed from `offset`.

    One line per 16 bytes, each ending in a newline (`xxd` terminates the last
    one too). Empty input dumps to the empty string: an empty file is an
    ordinary thing to click on, not an error and not a line of padding.

    `offset` shifts the addresses so a second chunk of a large file can be
    dumped from where it actually starts, the way ``xxd -o`` does it.
    """
    lines = []
    for start in range(0, len(data), _BYTES_PER_LINE):
        chunk = data[start : start + _BYTES_PER_LINE]
        lines.append(
            f"{offset + start:08x}: {_hex_column(chunk):<{_HEX_WIDTH}}  {_ascii_column(chunk)}\n"
        )
    return "".join(lines)


def _hex_column(chunk: bytes) -> str:
    groups = [
        chunk[i : i + _GROUP_SIZE].hex()
        for i in range(0, len(chunk), _GROUP_SIZE)
    ]
    return " ".join(groups)


def _ascii_column(chunk: bytes) -> str:
    # Anything outside the printable range becomes ".", never itself: a raw 0x07
    # would beep the terminal of anyone who copied the panel out, and a high byte
    # would mean whatever the reader's encoding decided.
    return "".join(
        chr(byte) if _PRINTABLE_LOW <= byte <= _PRINTABLE_HIGH else "."
        for byte in chunk
    )


def looks_binary(data: bytes) -> bool:
    """Whether `data` should be shown as a hex dump rather than as text.

    Two signals, in the order that catches the most with the least work: a NUL
    byte near the start (which is what `.pyc`, `.png` and ELF all have), and
    content that does not decode as UTF-8 at all. Text with accents must survive
    both -- half this project's own paths would otherwise be dumped as hex.

    Empty content is not binary: an empty text panel is the honest answer, an
    empty hex dump is a puzzle.
    """
    head = data[:_SNIFF_BYTES]
    if not head:
        return False
    if b"\x00" in head:
        return True
    # `final=False`, because the head may end in the middle of a multi-byte
    # sequence of otherwise perfectly good UTF-8.
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(head, False)
    except UnicodeDecodeError:
        return True
    return False
