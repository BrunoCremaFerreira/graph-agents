#!/bin/sh
# The launcher as the Debian package installs it: /usr/bin/rhi.
#
# The opposite case from packaging/rhi-hook.py, and the contrast is the whole
# design. The daemon imports websockets.asyncio.server, which first ships in the
# websockets 13 series while Debian carries 10.4, so this command must run the
# vendored interpreter. The hook must not, because it has no dependency to
# satisfy and runs on every tool call.
#
# The virtualenv is created with --system-site-packages and holds websockets
# alone: python3-watchdog and python3-gi come from the distribution, and keep
# getting its security updates. The Python modules are not inside it either --
# they sit at $PREFIX, which is what lets the hook import them under the system
# interpreter with no virtualenv on its path.
set -eu

# Both paths are spelled in full rather than composed, so that a grep for
# either one finds this file: the whole point of this script is which
# interpreter runs, and an answer assembled from fragments is one a reader has
# to reconstruct.
PREFIX=/usr/lib/rhizome-graph
PYTHON=/usr/lib/rhizome-graph/venv/bin/python

if [ ! -x "$PYTHON" ]; then
    echo "rhi: $PYTHON is missing; reinstall the rhizome-graph package" >&2
    exit 1
fi

# Prepended, so this installation's modules win, and the caller's own
# PYTHONPATH is kept rather than discarded.
PYTHONPATH="$PREFIX${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

# The console script's target, called the way pip would call it. argv[0] is set
# so that argparse names the program as the user typed it instead of `-c`.
exec "$PYTHON" -c 'import sys; sys.argv[0] = "rhi"; from rhizome_graph.cli import main; sys.exit(main())' "$@"
