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
pushed to a browser. The cap applies to the bytes *read*, in both modes.

The read itself goes to a thread. Blocking the loop freezes every connected
viewer, for the same reason :func:`rhizome_graph.tree.scan_tree` is off it. Nothing
here raises.
"""

from __future__ import annotations

import asyncio
import os

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
        return _frame(relative_path, mode="diff", content=diff)

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


def _read_capped(target: str, max_bytes: int) -> tuple[bytes, bool]:
    """The first `max_bytes` of `target`, and whether there was more.

    One byte past the cap is read so "exactly at the cap" is not reported as
    truncated, and nothing beyond that ever enters the daemon's memory.
    """
    with open(target, "rb") as handle:
        data = handle.read(max_bytes + 1)
    return data[:max_bytes], len(data) > max_bytes


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
