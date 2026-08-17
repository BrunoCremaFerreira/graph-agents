#!/usr/bin/env python3
"""Claude Code hook entrypoint: PostToolUse JSON on stdin -> event to the daemon.

The spelling every `.claude/settings.json` installed so far already carries --
`python3 /path/to/rhizome-graph/hooks/emit_event.py` -- kept working. The
adapter itself now lives in :mod:`rhizome_graph.hook`, inside the package that
`pip install` actually installs, so a settings file can name the `rhi-hook`
console script instead of a source tree the user may move or delete. This file
is the same program reached by the older name, and there is one implementation.

The shebang names the *system* interpreter, and must keep doing so: the hook
needs no third-party dependency, so pointing it at a virtualenv would couple the
hot path to something a rebuild deletes -- loudly, on every tool call, in a
project that may have nothing to do with this one.
"""

from __future__ import annotations

import os
import sys

# The package sits beside `hooks/` in a checkout, and a script's own directory
# is all that is on `sys.path` when Claude Code runs it from somewhere else.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from rhizome_graph.hook import DEFAULT_SOCKET_PATH, main
except Exception:  # noqa: BLE001 - a hook that cannot load still exits quietly
    sys.exit(0)

__all__ = ["DEFAULT_SOCKET_PATH", "main"]

if __name__ == "__main__":
    sys.exit(main())
