"""Which git checkouts sit *below* a directory -- the downward question.

Every repository question this codebase had walked upward. `repo.find_checkout_root`
climbs from the observed root until it meets a `.git`, and `status.git_status`
gives up -- without forking anything -- the moment that climb comes back empty. So
a workspace root holding five checkouts side by side shows no status panel at
all: not "everything is clean", but silence, which reads exactly like a healthy
repository with nothing pending. The one downward walk that already existed,
`tree.scan_tree`, prunes every dotted directory in place, so it steps over a
`.git` without ever seeing one.

**Why this is its own module.** Not `status.py`: nothing here is about the
porcelain format, and the click router will want the same answer without dragging
the parser in. Not `repo.py`: that module's whole docstring is the upward walk and
its "files, never `subprocess`" doctrine, neither of which says anything about a
walk that goes the other way. This asks one question -- which working trees are
under this path -- and answers it from the filesystem alone.

**This module reaches for `repo` and `tree`, and for nothing else of ours; it
starts no process.** That is a contract, not a preference, and `tests/test_checkouts.py`
asserts it over the parsed source the way the front end asserts "no shiki outside
`highlight.ts`". The status poll calls discovery on every round, uncached, because
it is 50-100x cheaper than the forks it decides on (0.2-0.4 ms against ~20 ms);
route a `git` call through here and that trade quietly inverts, with a caller who
believes the walk is free now paying 5 s timeouts. `gitcmd` stays the one place in
this project where a process is started.

Design notes:
  * **Discovery stops at what it finds.** A root that is itself a checkout answers
    ``[""]`` and looks no further, and a checkout found below the root is not
    descended into. `git status` already reports a nested checkout as a single
    entry, so listing it separately would double-count it and double the forks.
  * **Three budgets, because a walk precedes forks.** What has to be bounded is
    the worst case, not the measured one: a home directory with a network mount
    in it, polled every few seconds.
  * **Never raises.** The caller is a background poll on the daemon's loop. An
    unreadable directory costs results, never an exception.
"""

from __future__ import annotations

import os

from rhizome_graph import repo, tree

#: How many path segments below the root a checkout may sit and still be found.
#: Three covers both layouts people actually use: ``~/projects/a`` and
#: ``~/src/github.com/org/repo``. Depth is counted in segments of the returned
#: prefix, so ``github.com/org/repo`` is three and ``a/b/c/d`` is four.
MAX_DEPTH = 3

#: How many checkouts one answer may name. Each one costs a `git status` fork
#: whose timeout is 5 s, so this is the ceiling on a poll round's worst case.
MAX_CHECKOUTS = 16

#: How many directories the walk may open before it gives up. A guess at a safe
#: ceiling rather than a measured one -- but a bounded walk over a hostile tree
#: returns something, and an unbounded one returns when the mount does.
MAX_SCANNED_DIRS = 4000


def find_checkouts(
    root: str,
    max_depth: int = MAX_DEPTH,
    max_checkouts: int = MAX_CHECKOUTS,
    max_dirs: int = MAX_SCANNED_DIRS,
) -> list[str]:
    """The checkouts under `root`, as prefixes relative to it, sorted.

    ``[""]`` -- the empty prefix, which joins to nothing -- when `root` itself is
    a checkout. ``[]`` when there is none below it: that is an answer, not a
    failure, and the caller reads it as "do not fork anything".

    Sorted, and cut to `max_checkouts` *after* the sort, so which repositories a
    crowded workspace reports does not depend on inode order.
    """
    try:
        return _find(root, max_depth, max_checkouts, max_dirs)
    except Exception:
        # See the module docstring: the caller is a poll, and it must keep going.
        return []


def _find(root: str, max_depth: int, max_checkouts: int, max_dirs: int) -> list[str]:
    if max_checkouts <= 0 or max_dirs <= 0 or not os.path.isdir(root):
        return []

    base = os.path.abspath(root)
    if _holds_dot_git(base):
        # Upward wins: one working tree, one panel, and nothing vendored inside
        # it is a checkout of its own as far as this answer is concerned.
        return [""]

    found: list[str] = []
    opened = 0
    stack: list[tuple[str, str]] = [(base, "")]
    while stack and opened < max_dirs:
        directory, prefix = stack.pop()
        opened += 1
        descend: list[tuple[str, str]] = []
        for name in _child_directories(directory):
            child = os.path.join(directory, name)
            child_prefix = f"{prefix}/{name}" if prefix else name
            if _holds_dot_git(child):
                found.append(child_prefix)
                continue
            if child_prefix.count("/") + 1 < max_depth:
                descend.append((child, child_prefix))
        # Reversed, so popping the stack walks the children in name order: with a
        # budget that can bind, which directories go unopened must be decidable
        # from the tree rather than from the order the disk reported it in.
        stack.extend(reversed(descend))

    found.sort()
    return found[:max_checkouts]


def _child_directories(directory: str) -> list[str]:
    """The subdirectory names of `directory` worth walking into, sorted.

    Symlinked directories are excluded rather than followed: a link back into the
    tree would report the same checkout twice, and a loop would never end. The
    noise rule is `tree`'s own, so a row this feature produces always points at a
    part of the tree the graph actually draws.
    """
    try:
        with os.scandir(directory) as entries:
            names = [
                entry.name
                for entry in entries
                if not tree._is_ignored_dir(entry.name)
                and entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        # One bad permission bit costs the checkouts under it, and nothing else.
        return []
    names.sort()
    return names


def _holds_dot_git(directory: str) -> bool:
    """Whether `directory` is the top of a working tree.

    Both shapes count, as in `repo._find_dot_git`: a plain repository has a `.git`
    directory, while a worktree or a submodule has a `.git` *file* pointing at a
    git directory elsewhere. A worktree has a status like any other.
    """
    candidate = os.path.join(directory, ".git")
    return os.path.isdir(candidate) or os.path.isfile(candidate)


def owning_checkout(observed_root: str, absolute_path: str) -> str | None:
    """The checkout owning `absolute_path`, if it lies at or under `observed_root`.

    The question a click asks: which working tree should `git` be run in. The
    upward walk is `repo`'s, reused whole; what this adds is the containment
    test, because a checkout *above* the observed root is out of scope -- that is
    today's single-repo path, and answering with it here would hand `git` a
    working directory the viewer never agreed to observe.

    Resolved on both sides, and the answer is a `realpath` too. `find_checkout_root`
    returns an `abspath`, so a root reached through a symlinked component (`/tmp`
    is one on more than one platform) would silently never match; and a caller
    that goes on to relativize a resolved target against this answer needs the two
    spelled the same way.
    """
    try:
        checkout = repo.find_checkout_root(absolute_path)
        if checkout is None:
            return None
        resolved = os.path.realpath(checkout)
        if not _is_at_or_under(resolved, os.path.realpath(observed_root)):
            return None
        return resolved
    except Exception:
        return None


def _is_at_or_under(path: str, ancestor: str) -> bool:
    """Containment by whole path segments, so ``/a/bc`` is not under ``/a/b``."""
    trimmed = ancestor.rstrip(os.sep)
    if path == (trimmed or os.sep):
        return True
    return path.startswith(trimmed + os.sep)
