#!/usr/bin/env bash
#
# Start the graph-agents aggregator daemon.
#
# The daemon exposes:
#   * a Unix socket ingest   (GRAPHAGENTS_SOCKET,   default /tmp/graph-agents.sock)
#   * a WebSocket broadcast  (GRAPHAGENTS_WS_PORT,  default 8765)
#   * static hosting of web/dist over HTTP (GRAPHAGENTS_HTTP_PORT, default 8080)
#     when web/dist exists; otherwise run the Vite dev server yourself:
#         cd web && npm install && npm run dev
#     (and build for production with:  cd web && npm run build  -> web/dist)
#
# Install the capture hooks into the OBSERVED project by copying the "hooks"
# block from config/settings.json into that project's .claude/settings.json.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "No venv python at $PYTHON. Create one and install deps:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[daemon]'" >&2
  exit 1
fi

export GRAPHAGENTS_SOCKET="${GRAPHAGENTS_SOCKET:-/tmp/graph-agents.sock}"
export GRAPHAGENTS_WS_PORT="${GRAPHAGENTS_WS_PORT:-8765}"
export GRAPHAGENTS_HTTP_PORT="${GRAPHAGENTS_HTTP_PORT:-8080}"
# The project whose paths become relative in the graph (defaults to CWD).
export GRAPHAGENTS_PROJECT_ROOT="${GRAPHAGENTS_PROJECT_ROOT:-$PWD}"

echo "graph-agents daemon"
echo "  ingest socket : $GRAPHAGENTS_SOCKET"
echo "  websocket     : ws://localhost:$GRAPHAGENTS_WS_PORT"
echo "  http (dist)   : http://localhost:$GRAPHAGENTS_HTTP_PORT (if web/dist exists)"
echo "  project root  : $GRAPHAGENTS_PROJECT_ROOT"

exec "$PYTHON" -m daemon.server
