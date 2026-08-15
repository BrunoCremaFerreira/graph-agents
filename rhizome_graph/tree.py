"""Initial snapshot of the observed project tree.

The graph used to open on an empty field and only ever grew nodes an agent had
touched -- two or three lonely dots, nothing like Gource, which shows the whole
repository from the first frame. This module produces that first frame: one walk
of the project root at daemon boot, turned into seed events.

Design notes:
  * **Never raises.** An unreadable directory or a vanished root yields fewer
    paths, never an exception -- the daemon must still come up.
  * **Noise is structural, not configurable.** Build output and VCS internals
    would swamp the picture, so they are skipped by name (plus every dotted
    directory). This is deliberately not a `.gitignore` parser: the daemon also
    watches projects that are not git repositories.
  * Directories are not listed. The frontend materializes them from the paths of
    their children, so emitting them separately would only duplicate nodes.
"""

from __future__ import annotations

import os

#: Directory names that never carry anything worth visualizing. Any directory
#: whose name starts with "." is skipped as well (``.git``, ``.venv``, ...).
IGNORED_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        "coverage",
        "htmlcov",
        "vendor",
        "venv",
    }
)

#: Directory name suffixes that mark generated packaging metadata.
_IGNORED_SUFFIXES = (".egg-info",)

#: Safety valve: stop walking a pathological tree rather than hang the boot.
_MAX_WALK_ENTRIES = 200_000

DEFAULT_MAX_FILES = 20_000


def is_ignored(relative_path: str) -> bool:
    """Whether `relative_path` sits inside a directory the graph should skip.

    Only *directory* segments are considered, so a dotted file at the top level
    (``.gitignore``) is kept while anything under ``.git/`` is dropped.
    """
    return any(_is_ignored_dir(seg) for seg in relative_path.split("/")[:-1] if seg)


def _is_ignored_dir(name: str) -> bool:
    return (
        name in IGNORED_DIRS
        or name.startswith(".")
        or name.endswith(_IGNORED_SUFFIXES)
    )


def scan_tree(root: str, max_files: int = DEFAULT_MAX_FILES) -> list[str]:
    """Return the project's file paths, relative to `root` and sorted.

    Sorted so the seed order is stable across runs (the frontend lays the tree
    out in arrival order, and a shuffled tree would look different every boot).
    Symlinked directories are not followed, so a link back into the tree cannot
    duplicate the graph or loop forever.
    """
    try:
        return _scan(root, max_files)
    except Exception:
        # Seeding is a nicety; failing it must never stop the daemon booting.
        return []


def _scan(root: str, max_files: int) -> list[str]:
    if max_files <= 0 or not os.path.isdir(root):
        return []

    paths: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Pruning in place is what keeps os.walk out of node_modules entirely,
        # instead of walking it and discarding the results afterwards.
        dirnames[:] = [name for name in dirnames if not _is_ignored_dir(name)]
        for name in filenames:
            seen += 1
            if seen > _MAX_WALK_ENTRIES:
                break
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            paths.append(os.path.relpath(full, root))
        if seen > _MAX_WALK_ENTRIES:
            break

    paths.sort()
    return paths[:max_files]
