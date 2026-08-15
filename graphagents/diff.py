"""The uncommitted change of one file -- one of the two places that run `git`.

The graph shows *that* a file changed and nothing about *what* changed. Inside a
checkout, the interesting thing about a file an agent just wrote is the pending
delta, so the panel opened by a click asks this module for it. ``None`` is not an
error signal: it means "there is no diff, show the content instead", which is why
every failure mode collapses into it.

**Why this does not contradict `graphagents.repo`.** That module states the rule
"files, never `subprocess`", and keeps it: the branch is re-read every couple of
seconds by a background poll, forking `git` at that rate is pure waste, and the
answer is a dozen bytes sitting in `.git/HEAD`. Neither half of that applies
here. This runs once per *user click*, never on a timer; and there is no small
file to read -- a real `git diff HEAD` means resolving the index, inflating blobs
out of the object store (packed and loose, zlib), and then implementing the diff
algorithm itself. Reimplementing that to honour a rule written about reading one
line of one file would be the wrong trade. What `repo.py` is really protecting --
the event loop, and machines with no `git` installed -- is protected here by the
timeout and by returning ``None`` when the binary is missing.

The comparison is against ``HEAD``, deliberately: a file the agent wrote and then
staged is still an uncommitted change, and hiding it the moment `git add` runs
would blank the panel for the most interesting file on screen.

Nothing here raises and nothing here hangs. An exception would kill the task
serving that browser; a wait on `git` -- a repository mid-rebase with `index.lock`
held can block for as long as the other process pleases -- would freeze every
other connected client. That discipline is not written twice: it lives in
:mod:`graphagents.gitcmd`, which :mod:`graphagents.status` -- the other, later
caller -- runs through as well, so a fix to the way a hung `git` is abandoned
applies to both.
"""

from __future__ import annotations

from graphagents.gitcmd import run_git

#: How long `git` is given before it is abandoned.
DEFAULT_TIMEOUT_SECONDS = 3.0


def diff_command(relative_path: str) -> list[str]:
    """The argv for diffing `relative_path` against ``HEAD``.

    The path is data: it names a node in the graph, and a file genuinely called
    ``-x`` or ``--cached`` exists in this world. It therefore goes **last**,
    after ``--``, where `git` can only read it as a path. No quoting, no ``./``
    prefix, no escaping: this is argv, not a shell line, and mangling the path
    would make `git` diff a file that does not exist.
    """
    return ["git", "diff", "HEAD", "--", relative_path]


def parse_diff_output(stdout: str) -> str | None:
    """The diff `git` printed, or ``None`` when it printed nothing.

    `git` exits 0 both for "here is the delta" and for "this file is unchanged",
    so the emptiness of the output is the only evidence. A real diff comes back
    verbatim: stripping it would drop the leading space that marks a context line
    and the trailing newline of the last hunk.
    """
    if not stdout or not stdout.strip():
        return None
    return stdout


async def git_diff(
    root: str,
    relative_path: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """The uncommitted diff of `relative_path` inside `root`, or ``None``.

    ``None`` covers every way this can come up short -- no repository, no `git`
    binary, a root that was removed, a path carrying a NUL byte straight off the
    network, a non-zero exit, a `git` that hangs, an empty diff. The caller has
    one thing to do with all of them: show the file's content instead. The first
    six are :func:`graphagents.gitcmd.run_git`'s ``None``; the last one is this
    module's own, because "`git` ran and printed nothing" is a successful call.
    """
    stdout = await run_git(diff_command(relative_path), cwd=root, timeout=timeout)
    if stdout is None:
        return None
    return parse_diff_output(stdout)
