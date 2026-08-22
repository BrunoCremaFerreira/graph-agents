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

The repository is never an argument: `git` runs with ``cwd`` set on the directory
being asked about -- the observed root, or one of the checkouts found under it. A
path argument would be resolved against the *daemon's* own working directory,
which is some other project entirely.

**And there may be several checkouts.** A workspace root of the
``~/projects/{a,b,c}`` shape has no `.git` at or above it, so the upward walk
comes back empty and the panel is simply not on screen -- which reads exactly
like a clean tree, the one thing it must never be confused with. So when nothing
is found above the observed root, this looks below it and answers for every
checkout there, each row carrying the prefix that puts it back where the graph
draws it.

Two decisions hold that fan-out together. The upward answer still wins outright:
a root that is a checkout, or sits inside one, never asks what is below it, so a
repository with vendored checkouts in it keeps the panel it has always had. And
the downward walk *gates* the forks rather than following them -- discovery is
50-100x cheaper than the `git` calls it decides on, which is the same trade the
early return has always made: on a timer for the life of the daemon, forking
`git` to be told "not a repository" is pure waste, and forking it sixteen times
would be sixteen times the waste.

The rows of several checkouts are interleaved rather than concatenated, because
the frame keeps only the first two hundred: laid out repository by repository,
one repository with three hundred untracked files would fill the whole cut and
hide every other one -- this module's own failure mode, moved one level up.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from rhizome_graph.checkouts import find_checkouts
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

#: How many entries one checkout may contribute to a round. Purely a memory
#: bound: sixteen repositories times five thousand entries, parsed every three
#: seconds to keep two hundred, is garbage the loop does not need. It is a whole
#: `DEFAULT_MAX_ENTRIES` per checkout rather than a share of it because a share
#: would be ``200 // N`` -- a constant that depends on N and that nobody can
#: choose.
#:
#: The ``+ 1`` is the whole point of the number. `status_frame` derives
#: truncation as ``len(entries) > len(shown)``, so a lone sub-repository with 300
#: pending changes cut to exactly 200 on the way in yields ``200 > 200`` --
#: ``False``, and the panel would claim completeness over a list it cut, while
#: the same repository observed directly reports it cut. One entry more than a
#: frame can show is exactly enough to keep that signal true, and it costs
#: nothing: 16 x 201 is the same nothing as 16 x 200.
MAX_ENTRIES_PER_CHECKOUT = DEFAULT_MAX_ENTRIES + 1

#: How many checkouts are asked at once. A fork's timeout is 5 s, so what has to
#: be bounded is the worst case and not the measured one: sixteen checkouts
#: unbounded is a poll round nobody can wait out, four waves of four is the 20 s
#: ceiling `_status_busy` is sized against.
MAX_CONCURRENT_STATUS = 4

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
    #: Which checkout produced it, as a prefix relative to the observed root, or
    #: ``""`` when the observed root is itself the checkout. Defaulted, so every
    #: existing construction stands: the single-repo path has one repository and
    #: nothing to say about it. It cannot be recovered from the path afterwards
    #: -- ``a/b/c.ts`` may belong to checkout ``a`` or to checkout ``a/b``, and
    #: only the daemon knows which, at the moment it prefixes the entry.
    repo: str = ""


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


def prefix_entries(entries: list[StatusEntry], prefix: str) -> list[StatusEntry]:
    """`entries` re-expressed relative to a root `prefix` segments above them.

    The inverse of :func:`relativize`, and deliberately not folded into it. `git`
    reports `src/x` because that is where the file sits in *its own* checkout;
    the graph, `resolve_inside` and every node on screen speak in paths relative
    to the observed root, where the same file is `a/src/x`. One function strips a
    prefix and drops what falls outside, the other prepends one and drops
    nothing: a filter and a map, and one function with two moods is a function
    whose tests you have to re-read to know which mood each one pins.

    The empty prefix is the identity -- discovery answers ``[""]`` for a root
    that is itself a checkout, and joining that naively would produce `/src/x`,
    an absolute path matching no node at all.

    New entries, never a mutation: the caller keeps its originals, the same rule
    :func:`relativize` follows.
    """
    if not prefix:
        return [
            StatusEntry(path=entry.path, state=entry.state, repo=prefix)
            for entry in entries
        ]
    return [
        StatusEntry(path=f"{prefix}/{entry.path}", state=entry.state, repo=prefix)
        for entry in entries
    ]


def interleave(groups: list[list[StatusEntry]]) -> list[StatusEntry]:
    """One list drawn round-robin from `groups`, in their own order.

    :func:`status_frame` keeps only the first `DEFAULT_MAX_ENTRIES`. Over a list
    laid out repository by repository, one repository with three hundred
    untracked files fills the entire cut and hides every other one -- this
    feature's own failure mode, moved one level up. A per-repository quota would
    have to be ``200 // N``, a constant that depends on N; round-robin makes the
    *existing* cut fair with no new constant and no signature change.

    A group that runs out simply stops contributing -- no padding, no hole, no
    repetition -- so an empty group costs nothing and skews nothing. With one
    group this is the identity, which is the single-repo invariant: `git` orders
    its own output and one repository must not be reordered on its way through a
    function that exists for the case where there are several.
    """
    merged: list[StatusEntry] = []
    index = 0
    remaining = True
    while remaining:
        remaining = False
        for group in groups:
            if index < len(group):
                merged.append(group[index])
                remaining = True
        index += 1
    return merged


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

    ``None`` means "no repository anywhere, no `git`, or every call failed";
    ``[]`` means "at least one checkout, and nothing is pending in any of them".
    The panel renders those differently.

    The upward walk is asked first and its answer wins outright: when `root` is a
    checkout, or sits inside one, this is byte-for-byte what it has always been
    and nothing below `root` is even looked at. That is what makes backwards
    compatibility a shape rather than a list of regression tests, and it is why a
    repository holding vendored checkouts keeps its single panel.

    Only when that comes back empty does this look downward, for the workspace
    root that holds several checkouts side by side. Discovery *gates* the forks:
    it is 50-100x cheaper than the `git` calls it decides on, and a directory
    with no checkout under it still forks nothing at all -- this runs on a timer
    for the life of the daemon, and forking `git` every three seconds to be told
    "not a git repository" is pure waste.

    One checkout whose `git` fails is one checkout with nothing to say, not the
    round's answer; only every one of them failing is ``None``.

    Never raises and never hangs; see :mod:`rhizome_graph.gitcmd`.
    """
    try:
        checkout_root = find_checkout_root(root)
        if checkout_root is None:
            return await _multi_checkout_status(root, timeout)
        stdout = await run_git(status_command(), cwd=root, timeout=timeout)
        if stdout is None:
            return None
        return relativize(parse_status(stdout), checkout_root, os.path.abspath(root))
    except Exception:
        return None


async def _multi_checkout_status(
    root: str, timeout: float
) -> list[StatusEntry] | None:
    """Every checkout under `root`, merged. See :func:`git_status`.

    The walk itself goes to a worker thread: it opens up to `MAX_SCANNED_DIRS`
    directories, one of which may be a network mount, and on the loop's own
    thread that is every connected browser frozen for as long as the mount feels
    like taking. `scan_tree` is handed off for exactly this reason; this runs on
    every poll, which is worse.
    """
    prefixes = await asyncio.to_thread(find_checkouts, root)
    if not prefixes:
        return None

    # Inside the call, never at module level: a Semaphore built at import time
    # binds to the first loop that has to wait on it and raises on every loop
    # after that -- swallowed by the blanket ``except`` above into a silent
    # ``None``, so it would pass every single-loop test and fail the second time
    # a daemon's loop exists.
    limit = asyncio.Semaphore(MAX_CONCURRENT_STATUS)

    async def group_for(prefix: str) -> list[StatusEntry] | None:
        async with limit:
            stdout = await run_git(
                status_command(), cwd=os.path.join(root, prefix), timeout=timeout
            )
        if stdout is None:
            return None
        entries = parse_status(stdout)[:MAX_ENTRIES_PER_CHECKOUT]
        return prefix_entries(entries, prefix)

    results = await asyncio.gather(
        *(group_for(prefix) for prefix in prefixes), return_exceptions=True
    )
    groups = [
        result
        for result in results
        if isinstance(result, list)  # a failure is ``None``, a crash an exception
    ]
    if not groups:
        return None
    return interleave(groups)
