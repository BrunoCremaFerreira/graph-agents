"""What is uncommitted in the observed checkout, as data for the HUD panel.

The graph answers "what changed just now" and forgets: a node flashes orange for
a second and goes cold again. A viewer arriving thirty seconds after an agent
finished therefore sees a calm tree over a working directory full of pending
work, and cannot tell that from a clean one. This module is the model behind the
panel that says so out loud -- the pending changes, as plain data, ready to cross
the WebSocket.

**Why this is a poll that forks `git`, unlike the branch.** `repo.py` reads
`.git/HEAD` because the answer is a dozen bytes sitting in a file. There is no
such file for the status: the answer is the index compared against `HEAD` *and*
against every file in the working tree, which is precisely what `git status` is.
Nor can the watcher supply it -- it sees writes, not whether a write brought a
file back to what `HEAD` already had. So this forks, on a timer, and every
defence in :mod:`rhizome_graph.gitcmd` applies (that discipline is shared with
:mod:`rhizome_graph.diff` rather than written twice).

Everything except :func:`git_status` is pure, and split that way for the reason
the same split exists in `diff.py`: the format decisions are the part that can be
got wrong silently, and they are testable without a repository.

Two flags in the argv are load-bearing and invisible in a screenshot. ``-z``,
because the default output *quotes and escapes* any name with a space in it and a
newline in a name would split one record into two. ``core.quotepath=off``,
because the default mangles every non-ASCII byte into ``\\303\\247`` -- a path
that then matches no node in the graph.

The repository is never an argument: `git` runs with ``cwd`` set on the observed
root. A path argument would be resolved against the *daemon's* own working
directory, which is some other project entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from rhizome_graph.gitcmd import run_git
from rhizome_graph.repo import find_checkout_root

#: The states the panel paints. Everything git can report collapses into one of
#: these four, or is dropped -- a fifth colour nobody defined is not a feature.
STATES = ("modified", "added", "deleted", "untracked")

#: How long `git status` is given before it is abandoned. Longer than a diff's:
#: this one walks the whole working tree, and a cold cache on a big repository is
#: slow without anything being wrong.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: How many entries one frame may carry. A `git checkout` of a distant branch, or
#: a repository whose first commit has not happened yet, produces thousands; the
#: page would have to lay out every one of them, and the viewer learns nothing
#: from entry nine hundred that entry two hundred did not already say.
DEFAULT_MAX_ENTRIES = 200

#: Rename and copy records are followed by one extra field holding the *original*
#: path. It carries no XY prefix, so a parser that does not consume it reads it as
#: a path and every record after it is off by one.
_CARRIES_ORIGINAL = ("R", "C")

#: Any of these in either half of XY means "differs from HEAD" once addition and
#: deletion have had their say. ``U`` is a conflict: not a state of its own here,
#: because what matters to the viewer is that the file is not what HEAD has.
_MODIFIED_CODES = "MTRCU"


@dataclass(frozen=True)
class StatusEntry:
    """One pending change: a path as the graph draws it, and its state.

    Frozen because these are handed around and cached between polls; a mutated
    entry would repaint a node that never changed.
    """

    path: str
    state: str


def status_command() -> list[str]:
    """The argv for reading the working tree's status. See the module docstring."""
    return [
        "git",
        "-c",
        "core.quotepath=off",
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=normal",
    ]


def parse_status(stdout: str) -> list[StatusEntry]:
    """The ``-z`` output as entries, in the order `git` printed them.

    The grammar is ``XY<space><path>\\0`` per record, with one trap: a rename or
    a copy is followed by a second record holding the *original* path, bare. It
    is consumed and discarded here -- the node on screen is the new path -- and
    consuming it is what keeps every following record from being misread.

    Anything unrecognized is dropped rather than guessed, the same rule
    `_parse_bash` follows: this is parsed on the loop serving every browser,
    every few seconds, and a wrong entry offers the viewer a file that is not
    there. Nothing here raises, for the same reason.
    """
    records = stdout.split("\0")
    entries: list[StatusEntry] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            # Too short to hold "XY path", or not that shape at all: the tail of
            # the split, an empty poll, or garbage.
            continue
        xy, path = record[:2], record[3:]
        if _carries_original(xy):
            index += 1  # the original path, consumed even if the entry is dropped
        state = _state_of(xy)
        if state is not None and path:
            entries.append(StatusEntry(path=path, state=state))
    return entries


def _carries_original(xy: str) -> bool:
    return any(code in xy for code in _CARRIES_ORIGINAL)


def _state_of(xy: str) -> str | None:
    """The one state `XY` means to the panel, or ``None`` to drop the record.

    Precedence, and each step is a decision:

      * ``??`` is untracked -- and ``!!`` (ignored, only with ``--ignored``) is
        dropped, or the panel would list build output as pending work.
      * a ``D`` anywhere wins: ``AD`` is a file staged as new and then removed,
        and offering it as "added" points at nothing on disk.
      * an ``A`` next: ``AM`` is new to HEAD, which is what the colour says.
      * then anything that merely differs.
    """
    if xy == "??":
        return "untracked"
    if xy == "!!":
        return None
    if "D" in xy:
        return "deleted"
    if "A" in xy:
        return "added"
    if any(code in xy for code in _MODIFIED_CODES):
        return "modified"
    return None


def relativize(
    entries: list[StatusEntry], checkout_root: str, observed_root: str
) -> list[StatusEntry]:
    """`entries` re-expressed relative to `observed_root`, dropping what falls outside.

    `git` reports paths relative to the top of the **checkout**, even when run
    from a subdirectory (measured, not assumed). The graph, `resolve_inside` and
    every node on screen use paths relative to the **observed** root, which
    ``ctrl+L`` allows to be a subdirectory of the repository. Without this the
    panel offers paths that resolve to nothing.

    A change outside the observed root is real but undrawable: there is no node
    for it, and clicking it would be refused anyway. It is dropped.

    The prefix always ends in ``/``, so the boundary is a path *segment*: plain
    textual stripping would turn ``subterfuge/x.txt`` into ``terfuge/x.txt`` for
    an observed ``sub``, the same defect `display_root` had with ``~``. Roots
    that make no sense together yield an empty list rather than an exception --
    the caller is a background poll.
    """
    checkout = _normalized(checkout_root)
    observed = _normalized(observed_root)
    if not checkout or not observed:
        return []
    if observed == checkout:
        prefix = ""
    elif observed.startswith(checkout + "/"):
        prefix = observed[len(checkout) + 1 :] + "/"
    else:
        return []

    kept: list[StatusEntry] = []
    for entry in entries:
        if not prefix:
            kept.append(entry)
        elif entry.path.startswith(prefix):
            # A new entry, never a mutation: the caller keeps the originals.
            kept.append(StatusEntry(path=entry.path[len(prefix) :], state=entry.state))
    return kept


def _normalized(root: str) -> str:
    if not root or not root.strip():
        return ""
    return os.path.normpath(root)


def status_frame(
    entries: list[StatusEntry] | None, max_entries: int = DEFAULT_MAX_ENTRIES
) -> dict:
    """The ``status`` frame, ready to be sent as JSON.

    ``None`` and ``[]`` are different answers and must not collapse into each
    other: "there is no repository here" and "there is one and it is clean" look
    identical as an empty list, and the page says different things about them.

    Only plain JSON types leave here. A raw :class:`StatusEntry` smuggled into
    the frame would raise inside the send, on the daemon's loop, killing that
    client's task.
    """
    if entries is None:
        return {"kind": "status", "repo": False, "truncated": False, "entries": []}

    cap = max(0, int(max_entries))
    shown = list(entries)[:cap]
    return {
        "kind": "status",
        "repo": True,
        # The viewer has to know the list is partial; a silently cut list reads
        # as the whole truth.
        "truncated": len(entries) > len(shown),
        "entries": [{"path": entry.path, "state": entry.state} for entry in shown],
    }


async def git_status(
    root: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> list[StatusEntry] | None:
    """The pending changes under `root`, or ``None`` when there is nothing to report.

    ``None`` means "no repository, no `git`, or the call failed"; ``[]`` means
    "a repository, and it is clean". The panel renders those differently.

    Outside a repository this does not fork at all. The question is answered from
    disk by :func:`rhizome_graph.repo.find_checkout_root`, and this runs on a
    timer for the life of the daemon -- forking `git` every three seconds to be
    told "not a git repository" is pure waste.

    Never raises and never hangs; see :mod:`rhizome_graph.gitcmd`.
    """
    try:
        checkout_root = find_checkout_root(root)
        if checkout_root is None:
            return None
        stdout = await run_git(status_command(), cwd=root, timeout=timeout)
        if stdout is None:
            return None
        return relativize(parse_status(stdout), checkout_root, os.path.abspath(root))
    except Exception:
        return None
