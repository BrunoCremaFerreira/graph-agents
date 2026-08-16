"""What the panel shows when a node in the graph is clicked.

The graph says *that* a file changed and nothing about *what* is in it. This
module builds the one frame that answers the click, deciding in a fixed order:

  1. the path is refused (it escapes the observed root) -> ``error``;
  2. it is a directory -> ``error``;
  3. it has an uncommitted change -> ``mode: "diff"``;
  4. it is not on disk and git had nothing either -> ``error: no such file``;
  5. it is text -> ``mode: "text"``;
  6. it is binary -> ``mode: "hex"``.

The order is the point, twice over.

A binary that was just modified is shown as its diff ("Binary files ... differ",
which is all git has to say about it) rather than as a hex dump of the new bytes:
the question the viewer clicked to ask was "what did the agent just do to this
file".

And existence is asked about *after* git, not before. A **deleted** file -- the
single entry the status panel most wants to offer for a click -- is not on disk
by definition, and the old order answered "no such file" while ``git diff HEAD``
had the whole removed content ready to show. The directory check stays ahead of
git regardless: ``git diff HEAD -- src`` happily produces the combined diff of
everything under a folder, and clicking a folder must not open that.

:func:`resolve_inside` is the security half. The path arrives over a WebSocket,
as text, and unlike the completion commands it is used to **read file contents**.
``../../etc/passwd``, ``/etc/passwd`` and a symlink planted inside the project
pointing at ``/etc`` are the same attack, and the defense is the one
``_resolve_static_file`` already uses on the HTTP side: resolve first, then
require the result to sit under the root -- so a path that wanders through ``..``
and comes back is fine, while banning ``..`` textually would refuse it and still
miss the symlink.

``max_bytes`` exists because this frame goes down a WebSocket: a 400 MB core dump
would be read into the daemon's memory, hex-expanded to four times its size and
pushed to a browser. On the text and hex routes the cap applies to the bytes
*read*; on the diff route the text arrives already decoded from ``git``, so
:func:`cap_text` applies the same ceiling to what is *sent*. The two are kept
apart on purpose -- a binary has no lines, and the read must not become
line-aware.

The read itself goes to a thread. Blocking the loop freezes every connected
viewer, for the same reason :func:`rhizome_graph.tree.scan_tree` is off it -- and
that is exactly why only a regular file may be opened at all
(:func:`is_readable_regular`): a thread parked in ``open(2)`` on a named pipe is
lost for good. Nothing here raises.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import stat

from rhizome_graph.diff import git_diff
from rhizome_graph.hexdump import looks_binary, xxd_dump

#: Ceiling on the bytes read for one panel.
DEFAULT_MAX_BYTES = 256 * 1024


def resolve_inside(root: str, relative_path: str) -> str | None:
    """`relative_path` as an absolute path under `root`, or ``None`` if it escapes.

    Resolution comes first and containment second, so symlinks are followed
    before the question is asked -- the textual path of a link planted inside the
    project looks perfectly innocent. Returns ``None`` for anything that lands
    outside, for an absolute path (``os.path.join(root, "/etc/passwd")`` *is*
    ``/etc/passwd``, so the join alone is no check), and for anything the OS
    refuses to look at at all, such as a path carrying a NUL byte.
    """
    try:
        if not relative_path or "\x00" in relative_path:
            return None
        if os.path.isabs(relative_path):
            return None
        base = os.path.realpath(root)
        candidate = os.path.realpath(os.path.join(base, relative_path))
        if candidate != base and not candidate.startswith(base + os.sep):
            return None
        return candidate
    except Exception:
        return None


async def file_view(
    root: str,
    relative_path: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict:
    """The ``fileView`` frame for `relative_path`, ready to be sent as JSON.

    Only plain JSON types leave here: a ``Path`` or raw ``bytes`` smuggled in
    would raise inside the send, on the daemon's loop. ``path`` is echoed intact
    because the page keys the panel by it -- a second click while the first answer
    is still travelling must not paint the wrong file's content.
    """
    target = resolve_inside(root, relative_path)
    if target is None:
        return _frame(relative_path, error="refused: outside the observed project")
    if os.path.isdir(target):
        return _frame(relative_path, error="that is a directory")

    diff = await git_diff(root, relative_path)
    if diff is not None:
        content, truncated = cap_text(diff, max_bytes)
        return _frame(
            relative_path, mode="diff", content=content, truncated=truncated
        )

    if not os.path.exists(target):
        # Reached only once git has said it knows nothing about the path either,
        # so a deletion opens its diff instead of an error.
        return _frame(relative_path, error="no such file")

    try:
        data, truncated = await asyncio.to_thread(_read_capped, target, max_bytes)
    except Exception:
        return _frame(relative_path, error="unreadable")

    if looks_binary(data):
        return _frame(
            relative_path, mode="hex", content=xxd_dump(data), truncated=truncated
        )
    return _frame(
        relative_path,
        mode="text",
        content=data.decode("utf-8", errors="replace"),
        truncated=truncated,
    )


def is_readable_regular(st_mode: int) -> bool:
    """May a file of this type be opened for the panel?

    Only a regular file. A named pipe is the one that matters: ``open(2)`` on a
    FIFO with no writer blocks forever, and since the read runs in a worker
    thread -- which cannot be cancelled -- one click on a build system's pipe
    costs the daemon a worker permanently, eight clicks take the whole file
    layer (and ``switch_root``, which shares the executor) down, and the process
    can no longer even exit, because shutdown joins those threads. Sockets and
    devices are refused by the same rule: the errno that happens to refuse a
    socket today is not a rule.

    The mode must come from a **followed** stat -- ``os.stat`` or ``fstat``,
    never ``lstat`` -- so what is judged is the type at the end of a symlink.
    ``S_IFLNK`` therefore answers ``False``: it should never arrive here, and if
    it does the caller is asking about the link rather than about the file.

    This answers "what kind of file is it", not "may this process read it": the
    permission bits are ignored, because permission is not knowable from the
    mode alone (root reads a ``0o000`` file) and a refusal is already reported
    through the ``unreadable`` path.
    """
    return stat.S_ISREG(st_mode)


def _read_capped(target: str, max_bytes: int) -> tuple[bytes, bool]:
    """The first `max_bytes` of `target`, and whether there was more.

    One byte past the cap is read so "exactly at the cap" is not reported as
    truncated, and nothing beyond that ever enters the daemon's memory.

    The descriptor is opened first and its type asked of the descriptor itself,
    rather than stat'ing the path and then opening it: the two calls name the
    same path but not necessarily the same file, and the whole point is to never
    be holding a FIFO. ``O_NONBLOCK`` is what makes the open survivable -- it is
    what stops ``open(2)`` from blocking on a writerless pipe -- and it is
    cleared again before a byte is read, so a regular file keeps ordinary
    blocking read semantics and never returns a short read with ``EAGAIN``.
    """
    descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not is_readable_regular(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", target)
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        with open(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    return data[:max_bytes], len(data) > max_bytes


def cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """The first `max_bytes` of UTF-8 in `text`, and whether there was more.

    The unit is bytes because the cap exists for what crosses the socket, not
    for how many characters a diff happens to spell.

    The cut lands on a line boundary whenever there is one to land on: the panel
    parses this text back into hunks and rows, and half a hunk header is a worse
    failure than the size being avoided. When a single line is longer than the
    whole cap -- a minified bundle is one line and megabytes long, precisely the
    file most likely to hit this -- trimming back to the last newline would leave
    nothing at all, so that case is cut mid-line rather than emptied.

    What comes back is always a prefix of `text`: no ellipsis and no "... N more
    lines" marker, because the browser reads this as a diff and would parse the
    marker as content.
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    head = raw[:max_bytes] if max_bytes > 0 else b""
    boundary = head.rfind(b"\n")
    if boundary >= 0:
        head = head[: boundary + 1]
    # A slice at an arbitrary byte can split a character; dropping the orphan
    # keeps the answer a true prefix instead of ending it in U+FFFD.
    return head.decode("utf-8", errors="ignore"), True


def _frame(
    relative_path: str,
    *,
    mode: str = "error",
    content: str = "",
    truncated: bool = False,
    error: str = "",
) -> dict:
    return {
        "kind": "fileView",
        "path": relative_path,
        "mode": mode,
        "content": content,
        "truncated": truncated,
        "error": error,
    }
