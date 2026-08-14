"""The uncommitted change of one file -- the only place here that runs `git`.

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
other connected client.
"""

from __future__ import annotations

import asyncio
import contextlib

#: How long `git` is given before it is abandoned.
DEFAULT_TIMEOUT_SECONDS = 3.0

#: How long the killed process is waited on before giving up on reaping it too.
_REAP_TIMEOUT_SECONDS = 2.0


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
    one thing to do with all of them: show the file's content instead.

    Never ``shell=True``: the path is untrusted text, and a shell would read
    ``;`` in a filename as a command separator.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *diff_command(relative_path),
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        # No binary, a cwd that does not exist, a NUL in the path: `git` never
        # started, so there is no diff to show.
        return None

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        await _abandon(process)
        return None
    except Exception:
        return None

    if process.returncode != 0:
        # Outside a repository `git` exits 128 with "not a git repository" on
        # stderr. That is an ordinary answer here, not a failure to propagate.
        return None

    return parse_diff_output(stdout.decode("utf-8", errors="replace"))


async def _abandon(process: asyncio.subprocess.Process) -> None:
    """Kill a `git` that overstayed, and reap it before returning.

    Reaping is not tidiness. An unwaited child leaves its transport to be
    garbage-collected whenever, and the transport's ``__del__`` closes pipes on
    the loop it was created on -- which, by then, may be closed, so the process
    exits with ``RuntimeError: Event loop is closed`` printed from nowhere.

    ``wait()`` alone is not enough and would itself hang: the pipes have to be
    closed too. `git` is a wrapper script often enough, and a grandchild that
    inherited stdout keeps the pipe open for as long as *it* runs, which is
    exactly the case being escaped from. Closing the transport drops the pipes,
    and the wait is bounded regardless -- nothing in this module may block the
    loop serving every connected browser.
    """
    with contextlib.suppress(Exception):
        process.kill()
    with contextlib.suppress(Exception):
        process._transport.close()  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=_REAP_TIMEOUT_SECONDS)
