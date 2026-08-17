"""Running one daemon from a finished :class:`~rhizome_graph.cli.Settings`.

This is the half of `rhi` that cannot live in :mod:`rhizome_graph.cli`. That
module is imported to print `--help` and to refuse a bad flag, so it may not name
`asyncio`, `websockets`, `watchdog` or `daemon` at all; this one names them
freely, and is imported from inside `main()` -- after argparse has already
answered the two flags that start nothing.

It owns exactly one thing beyond the call to `run()`: **turning an anticipated
refusal into one the caller can report.** `IngestSocketInUseError` is the
daemon's way of saying another instance owns the socket, and nobody reads twenty
frames of asyncio internals and concludes that. It becomes a
:class:`StartupRefused` carrying the same path, which `main()` prints as an exit
message.

**When the news is announced is not owned here either, any more.** `run()` says
so itself, once, from inside, at the moment both its listeners accept; this
module only forwards the callback. The poll that used to live here -- open a TCP
connection to the HTTP port until one succeeds -- was wrong twice over: it made
`run()`'s internal ordering a load-bearing contract nobody had written down (it
could only ever observe the HTTP half, and was a valid readiness test purely
because the ingest socket happened to be bound first), and it fabricated a client
on every boot, logging a `connection closed` with no matching open.

**Shutdown is not owned here.** `run()` installs the signal handlers and unwinds
its own listeners, watcher and polls; a terminal's SIGTERM, an embedded caller's
cancellation and a window being closed all resolve the same future inside it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from daemon.server import IngestSocketInUseError, Readiness, run
from rhizome_graph.cli import Settings

LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class StartupRefused(RuntimeError):
    """This instance could not start, for a reason somebody can act on."""


def serve(settings: Settings, on_ready: Callable[[Readiness], None]) -> None:
    """Run one daemon in the foreground until it is asked to stop.

    Returns normally on a clean shutdown -- a signal, the loop being stopped, or
    the window being closed -- so a caller that returns this to `sys.exit` exits
    zero.
    """
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT)
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run(settings, ready=on_ready))
    except IngestSocketInUseError as exc:
        raise StartupRefused(
            f"{exc}. Stop it, or name another socket with --socket."
        ) from None
