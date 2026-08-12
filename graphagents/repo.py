"""Which checkout is on screen: observed directory and current git branch.

Feeds the HUD line at the bottom of the page. A viewer looking at a forwarded
port cannot otherwise tell *which* project the graph belongs to, and a branch
switch mid-session changes the meaning of every node without a pixel saying so.

Design notes:
  * **Files, never `subprocess`.** The daemon re-reads this every couple of
    seconds; forking `git` that often is pure waste, and it would report nothing
    at all on a machine without the binary installed -- while `.git/HEAD` is a
    dozen bytes sitting right there. Reading it is also exactly what git itself
    does to answer the question.
  * **Never raises.** The poll lives in a background asyncio task; a single
    exception on an unreadable `.git` would kill that task and freeze the HUD on
    a stale branch for the rest of the session. Every failure mode collapses to
    ``None``, which the page renders as "no branch".
"""

from __future__ import annotations

import os

_REF_PREFIX = "ref: "
_HEADS_PREFIX = "refs/heads/"
_SHORT_SHA_LENGTH = 7
_SHA_LENGTH = 40


def resolve_git_dir(root: str) -> str | None:
    """Find the git directory governing `root`, walking upward, or ``None``.

    Two shapes exist on disk. A normal repository has a `.git` *directory*. A
    worktree or a submodule has a `.git` *file* holding ``gitdir: <path>``, where
    a relative path is relative to the checkout that contains the file -- not to
    the caller's cwd, which is why it is joined against `current` here.

    The search stops when `dirname` stops moving ("/" is its own parent), so the
    daemon cannot be hung by a root that is outside any repository.
    """
    current = os.path.abspath(root)
    while True:
        candidate = os.path.join(current, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            return _read_gitdir_file(candidate, current)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _read_gitdir_file(candidate: str, holder: str) -> str | None:
    try:
        with open(candidate, encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("gitdir:"):
            target = line[len("gitdir:") :].strip()
            if target:
                return os.path.normpath(os.path.join(holder, target))
    return None


def parse_head(content: str) -> str | None:
    """Turn the contents of a `HEAD` file into a name to show, or ``None``.

    ``ref: refs/heads/feature/x`` is a branch (inner slashes are part of the
    name); a bare 40-character sha is a detached HEAD, shortened the way git
    displays it. Anything else -- empty, truncated, unrecognized -- yields
    ``None`` rather than a guess: a wrong branch on screen is worse than none.
    """
    text = content.strip()
    if not text:
        return None
    if text.startswith(_REF_PREFIX):
        ref = text[len(_REF_PREFIX) :].strip()
        if ref.startswith(_HEADS_PREFIX):
            return ref[len(_HEADS_PREFIX) :] or None
        return None
    if len(text) == _SHA_LENGTH and all(c in "0123456789abcdefABCDEF" for c in text):
        return text[:_SHORT_SHA_LENGTH]
    return None


def read_branch(root: str) -> str | None:
    """The branch (or short sha) of the repository containing `root`.

    Swallows everything: no repository, a `gitdir:` pointing at a directory that
    was removed, a `HEAD` deleted underneath us, unreadable permissions, even a
    `HEAD` that is somehow a directory. See the module docstring for why the
    caller must never see an exception.
    """
    try:
        git_dir = resolve_git_dir(root)
        if git_dir is None:
            return None
        with open(os.path.join(git_dir, "HEAD"), encoding="utf-8", errors="replace") as handle:
            return parse_head(handle.read())
    except Exception:
        return None


def display_root(path: str, home: str) -> str:
    """`path` as a human reads it, with the home directory collapsed to ``~``.

    The comparison is per path *segment*, not textual: `startswith` would turn
    ``/home/brnx/y`` into ``~x/y`` for a user whose home is ``/home/brn``.
    """
    if not home:
        return path
    trimmed = home.rstrip("/")
    if not trimmed:
        return path
    if path == trimmed:
        return "~"
    if path.startswith(trimmed + "/"):
        return "~" + path[len(trimmed) :]
    return path
