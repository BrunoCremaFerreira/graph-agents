#!/usr/bin/python3
"""The capture hook as the Debian package installs it: `/usr/bin/rhi-hook`.

This file is not imported by anything in the checkout. It is copied to
`/usr/bin/rhi-hook` by `packaging/build-deb.sh`, and it exists to resolve a
conflict between two rules that both hold:

  * **The system interpreter, named absolutely.** The shebang is
    `#!/usr/bin/python3` and not the vendored virtualenv's interpreter, and not
    `/usr/bin/env python3` either. This runs on *every* tool call in somebody's
    Claude Code session, it needs no third-party module, and it must survive the
    virtualenv being rebuilt, upgraded or removed. `env` would be worse than the
    virtualenv: a hook inherits the agent's environment, so a `$PATH` pointing
    at a project virtualenv would silently pick that interpreter instead.
  * **One implementation of the hot path.** The adapter lives in
    `rhizome_graph.hook`, and `hooks/emit_event.py` is already a shim over it,
    so a second copy of the forwarding logic here would be the one that drifts.

The two are reconciled by where the package puts the code: the Python modules
are installed as plain sources at `/usr/lib/rhizome-graph`, *beside* the
virtualenv rather than inside it, and the virtualenv holds third-party wheels
only. So this shim needs one entry on `sys.path` and no virtualenv at all --
`/usr/bin/rhi` is the command that reaches for the vendored interpreter, because
the daemon is the half with a dependency. Nothing here is coupled to a Python
minor version, which is what globbing a `venv/lib/python3.*/site-packages`
directory would have been.

Silence is the contract, as in `rhizome_graph.hook`: a hook that writes to
stderr or exits non-zero is reported by Claude Code as a blocking error on the
tool call it was watching, so an installation broken badly enough that the
import fails drops the event instead of the session.
"""

import sys

#: Where the package installs the Python modules. Prepended rather than
#: appended: an unrelated `rhizome_graph` on the path is not the one this
#: installation owns.
PACKAGE_PREFIX = "/usr/lib/rhizome-graph"

if PACKAGE_PREFIX not in sys.path:
    sys.path.insert(0, PACKAGE_PREFIX)

try:
    from rhizome_graph.hook import main
except Exception:  # noqa: BLE001 - a broken install may not break the session
    raise SystemExit(0)

raise SystemExit(main())
