"""Running `git` safely from the event loop -- the one place that forks it.

Two callers need an answer only `git` can give: :mod:`rhizome_graph.diff` (the
pending delta of one file, once per click) and :mod:`rhizome_graph.status` (the
pending changes of the whole tree, on a timer). Everything *else* in this project
reads files instead -- see the doctrine in :mod:`rhizome_graph.repo` -- and both
exceptions are documented where they live.

What they share is not the argv but the discipline around it, and every line of
it was paid for:

  * **Never ``shell=True``.** The arguments carry paths that arrive over a
    WebSocket; a shell would read ``;`` in a filename as a command separator.
  * **Never raises.** A missing binary, a cwd that was deleted while a browser
    still had it on screen, a NUL byte straight off the network: all of it is one
    answer, ``None``. An exception would kill the task serving that client, or
    the poll for the rest of the session.
  * **Never hangs.** A repository mid-rebase with `index.lock` held blocks for as
    long as the other process pleases, and this runs on the loop that serves
    every connected browser.
  * **On timeout, kill *and* close the transport before waiting.** See
    :func:`_abandon`: ``wait()`` alone would hang on exactly the case being
    escaped from.

A non-zero exit is not an error to propagate either. Outside a repository `git`
exits 128 with a sentence on stderr; here that is an ordinary "nothing to
report", so it collapses into ``None`` like the rest.
"""

from __future__ import annotations

import asyncio
import contextlib

#: How long the killed process is waited on before giving up on reaping it too.
_REAP_TIMEOUT_SECONDS = 2.0


async def run_git(argv: list[str], cwd: str, timeout: float) -> str | None:
    """`argv` run in `cwd`, as decoded stdout, or ``None`` if it did not succeed.

    ``""`` and ``None`` are different answers and callers depend on it: an empty
    stdout means `git` ran and had nothing to say (a clean tree, an unchanged
    file), while ``None`` means there was no usable answer at all.

    stderr goes to ``/dev/null``: everything `git` writes there is already
    covered by the exit status, and a pipe nobody drains is a deadlock waiting
    for a verbose enough repository.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        # No binary, a cwd that does not exist, a NUL in an argument: `git`
        # never started, so there is nothing to report.
        return None

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        await _abandon(process)
        return None
    except Exception:
        return None

    if process.returncode != 0:
        return None

    return stdout.decode("utf-8", errors="replace")


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
    and the wait is bounded regardless -- nothing here may block the loop serving
    every connected browser.
    """
    with contextlib.suppress(Exception):
        process.kill()
    with contextlib.suppress(Exception):
        process._transport.close()  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=_REAP_TIMEOUT_SECONDS)
