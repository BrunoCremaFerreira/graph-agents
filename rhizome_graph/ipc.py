"""Is another daemon already listening on the ingest socket -- or on the port?

The daemon used to clear the way for its listener by unlinking whatever it found
at the socket path. That is right for the file a crashed daemon leaves behind,
and wrong for a second daemon started from a desktop menu: the first keeps its
descriptor and goes on serving its browser, but the *name* now belongs to the
second, so every hook reaches the newcomer and the first window shows a tree
updating with nobody on camera.

Telling the two apart needs a connection. `os.path.exists` cannot -- the stale
file exists too -- and `S_ISSOCK` only says the file was once a socket, which is
equally true of the corpse. So the probe connects and hangs up. The ingest
protocol is newline-delimited JSON, so a connection that sends nothing costs the
other daemon one empty read it already handles.

Stdlib only, and it never raises: the caller is a daemon deciding at boot
whether to start at all, and a traceback there replaces a refusal the user could
have acted on.

:func:`port_is_free` is the same question asked about the HTTP port, and it lives
here for the same reason: both answers need a real socket, so neither belongs in
the pure configuration module that has to decide what to do about them.
"""

from __future__ import annotations

import socket

#: How long the probe waits for the other end. A local AF_UNIX connect either
#: completes or is refused immediately; this is only a ceiling on the pathology.
PROBE_TIMEOUT_SECONDS = 1.0


def socket_is_live(path: str) -> bool:
    """Does something accept connections at `path` right now?

    `False` for a stale socket file, for a path that does not exist, for a
    regular file in the way, and for a path that cannot be a socket address at
    all -- a NUL byte or an address over the AF_UNIX length limit, both of which
    reach here from `RHIZOME_SOCKET`.
    """
    probe = None
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(PROBE_TIMEOUT_SECONDS)
        probe.connect(path)
        return True
    except Exception:  # noqa: BLE001 - house rule: this answers, it never raises
        return False
    finally:
        if probe is not None:
            probe.close()


def port_is_free(host: str, port: int) -> bool:
    """Could a listener bind `(host, port)` at this moment?

    Asked by binding rather than by connecting: a port nobody accepts on may
    still be unbindable, and the question the caller has is whether *this*
    process can serve there. ``SO_REUSEADDR`` is set because `asyncio` sets it on
    the listener this probe is standing in for -- the probe must fail exactly
    where the real bind would.

    Answers, never raises: an address family that does not exist, a port outside
    the range, a privileged port -- all of them mean "not this one" to a caller
    that is about to try the next.
    """
    probe = None
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
        return True
    except Exception:  # noqa: BLE001 - house rule: this answers, it never raises
        return False
    finally:
        if probe is not None:
            probe.close()
