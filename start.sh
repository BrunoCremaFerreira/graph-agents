#!/usr/bin/env bash
#
# start.sh — bootstrap + run graph-agents (the Gource-style web visualizer).
#
# Does everything from scratch:
#   1. creates/prepares the Python venv and installs the daemon deps;
#   2. secures a usable npm (see below) and builds web/dist;
#   3. starts the daemon: HTTP (serving the front end) + WebSocket (/ws) on the
#      SAME port + the ingest socket.
#
# Usage:
#   ./start.sh                # prod: ensure the build, serve web/dist on http://localhost:8080
#   ./start.sh --rebuild      # force a reinstall/rebuild of the front end before starting
#   ./start.sh --no-build     # skip the front end, serve the existing web/dist
#   ./start.sh --dev          # daemon + Vite dev server (hot reload) on http://localhost:5173
#   ./start.sh --print-npm    # only resolve npm, print the path on stdout and exit (starts nothing)
#   ./start.sh --help
#
# How npm is resolved (in this order):
#   1. $NPM from the environment;
#   2. npm on $PATH;
#   3. local bootstrap: download the npm tarball (curl or wget) from the registry, unpack it
#      into .npm-bootstrap/ and write a wrapper .npm-bootstrap/bin/npm that runs
#      `node .../npm-cli.js "$@"`. Needs node installed; it is cached (with a warm cache
#      nothing is downloaded). Distros packaging only `node` (Debian/Ubuntu) land here.
#
# How the front end is installed (the SAME path in prod and in --dev):
#   - with web/package-lock.json: `npm ci`, which installs FROM the lock and does not rewrite
#     it (an `npm install` here strips the lock's libc fields). If `ci` exits non-zero (lock
#     out of sync with package.json), it falls back to `npm install` and carries on.
#   - without a lockfile: `npm install` directly (a `ci` with no lock only yields a confusing
#     error).
#   - in prod, `npm run build` at the end; in --dev there is no build at all — Vite serves
#     from source and the mode ends in `npm run dev`.
#   - in prod the install always runs, even with web/node_modules already present — that is
#     what makes --rebuild mean "reinstall AND rebuild". In --dev an existing
#     web/node_modules is reused (`npm ci` wipes and rebuilds the whole tree, and --dev is
#     the command restarted dozens of times an hour); use `./start.sh --dev --rebuild` to
#     force the reinstall there too.
#
# Environment variables (with defaults):
#   GRAPHAGENTS_SOCKET     /tmp/graph-agents.sock   Unix ingest socket for the hooks
#   GRAPHAGENTS_HTTP_PORT  8080                     single port: serves web/dist AND the
#                                                   WebSocket at /ws (one forwarded port
#                                                   is enough over SSH)
#   GRAPHAGENTS_PROJECT_ROOT  (cwd)                 root the graph's paths are relative to
#   PYTHON  NODE  NPM      executable overrides
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ---- flags -----------------------------------------------------------------
MODE="prod"      # prod | dev
BUILD="auto"     # auto | force | skip
PRINT_NPM=0
LOG_FD=1         # --print-npm sends every log line to stderr: stdout is the answer
for arg in "$@"; do
  case "$arg" in
    --dev)       MODE="dev" ;;
    --rebuild)   BUILD="force" ;;
    --no-build)  BUILD="skip" ;;
    --print-npm) PRINT_NPM=1; LOG_FD=2 ;;
    -h|--help)
      sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown argument: $arg (use --help)" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;36m▶ %s\033[0m\n' "$*" >&"$LOG_FD"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

# ---- 0. npm ----------------------------------------------------------------
NPM="${NPM:-}"
NPM_ERROR=""
# The pin exists for one reason only: 10.9.4 is the newest npm that runs on this machine's
# Node 18 (it declares engines.node "^18.17.0 || >=20.5.0"; npm 11 demands ^20.17.0 || >=22.9.0).
# It is not about the lockfile: measured, 10.9.4 also strips the 42 `libc` lines on an
# `npm install`, because only npm 11 writes them. What preserves the lock is `npm ci`.
NPM_BOOTSTRAP_VERSION="10.9.4"
BOOT_DIR="$REPO_ROOT/.npm-bootstrap"
BOOT_NPM="$BOOT_DIR/bin/npm"
BOOT_CLI="$BOOT_DIR/npm-$NPM_BOOTSTRAP_VERSION/package/bin/npm-cli.js"

download() {  # download <url> <destination-file>
  local url="$1" out="$2"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSL --connect-timeout 15 -o "$out" "$url"; then return 0; fi
  fi
  if command -v wget >/dev/null 2>&1; then
    if wget -q -O "$out" "$url"; then return 0; fi
  fi
  return 1
}

bootstrap_npm() {
  if [[ -x "$BOOT_NPM" && -f "$BOOT_CLI" ]]; then
    return 0   # warm cache: nothing is downloaded
  fi

  local node
  node="${NODE:-}"
  if [[ -z "$node" ]]; then
    node="$(command -v node 2>/dev/null || command -v nodejs 2>/dev/null || true)"
  fi
  if [[ -z "$node" ]]; then
    NPM_ERROR="node was not found on PATH; without node there is no way to prepare a local npm (install Node.js or set NODE=/path/to/node)."
    return 1
  fi

  local url="https://registry.npmjs.org/npm/-/npm-$NPM_BOOTSTRAP_VERSION.tgz"
  local tmp
  mkdir -p "$BOOT_DIR"
  tmp="$(mktemp -d "$BOOT_DIR/.tmp.XXXXXX")" || {
    NPM_ERROR="could not create a temporary directory under $BOOT_DIR"
    return 1
  }

  log "Preparing a local npm in .npm-bootstrap (npm $NPM_BOOTSTRAP_VERSION)"
  if ! download "$url" "$tmp/npm.tgz"; then
    rm -rf "$tmp"
    NPM_ERROR="failed to download npm $NPM_BOOTSTRAP_VERSION from $url (the download did not complete — network unavailable? curl/wget missing?)"
    return 1
  fi
  if ! tar xzf "$tmp/npm.tgz" -C "$tmp"; then
    rm -rf "$tmp"
    NPM_ERROR="the downloaded npm tarball could not be unpacked (corrupted download?)"
    return 1
  fi
  if [[ ! -f "$tmp/package/bin/npm-cli.js" ]]; then
    rm -rf "$tmp"
    NPM_ERROR="the downloaded npm package does not contain bin/npm-cli.js"
    return 1
  fi

  # only now does the cache come into existence: a failed download leaves no usable litter
  rm -rf "$BOOT_DIR/npm-$NPM_BOOTSTRAP_VERSION"
  mkdir -p "$BOOT_DIR/npm-$NPM_BOOTSTRAP_VERSION" "$BOOT_DIR/bin"
  mv "$tmp/package" "$BOOT_DIR/npm-$NPM_BOOTSTRAP_VERSION/package"
  rm -rf "$tmp"

  cat > "$BOOT_NPM.new" <<EOF
#!/bin/sh
# generated by start.sh — local npm $NPM_BOOTSTRAP_VERSION, no global install
NODE_BIN="\${NODE:-$node}"
[ -x "\$NODE_BIN" ] || NODE_BIN="\$(command -v node || command -v nodejs)"
exec "\$NODE_BIN" "$BOOT_CLI" "\$@"
EOF
  chmod +x "$BOOT_NPM.new"
  mv "$BOOT_NPM.new" "$BOOT_NPM"
  return 0
}

resolve_npm() {
  if [[ -n "$NPM" ]]; then return 0; fi
  local found
  found="$(command -v npm 2>/dev/null || true)"
  if [[ -n "$found" ]]; then NPM="$found"; return 0; fi
  if bootstrap_npm; then NPM="$BOOT_NPM"; return 0; fi
  return 1
}

if [[ "$PRINT_NPM" == "1" ]]; then
  if ! resolve_npm; then
    err "Could not obtain an npm: $NPM_ERROR"
    exit 1
  fi
  printf '%s\n' "$NPM"
  exit 0
fi

# ---- 1. Python / daemon ----------------------------------------------------
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  log "Creating the virtualenv in .venv"
  python3 -m venv "$REPO_ROOT/.venv"
  PYTHON="$REPO_ROOT/.venv/bin/python"
fi
if ! "$PYTHON" -c "import websockets" >/dev/null 2>&1; then
  log "Installing the daemon deps (pip install -e '.[daemon]')"
  "$PYTHON" -m pip install --quiet --upgrade pip
  "$PYTHON" -m pip install --quiet -e "$REPO_ROOT[daemon]"
fi

# ---- 2. Frontend -----------------------------------------------------------
DIST="$REPO_ROOT/web/dist"

front_install() {  # runs with cwd inside web/
  if [[ -f package-lock.json ]]; then
    log "npm ci (front end)"
    if ! "$NPM" ci; then
      warn "npm ci failed (lock out of sync with package.json?); trying npm install"
      "$NPM" install
    fi
  else
    log "npm install (front end)"
    "$NPM" install
  fi
}

build_front() {
  ( cd "$REPO_ROOT/web"
    front_install
    log "npm run build (produces web/dist)"; "$NPM" run build )
}

if [[ "$MODE" == "dev" ]]; then
  if ! resolve_npm; then
    err "--dev mode requires npm: $NPM_ERROR"
    exit 1
  fi
  # The same entry point as prod (front_install): `ci` over the lock, falling back to
  # `install`. The node_modules guard survives only here — `npm ci` wipes and rebuilds the
  # whole tree, and --dev is the command restarted dozens of times an hour — but --rebuild
  # still means "reinstall" in both modes.
  if [[ "$BUILD" == "force" || ! -d "$REPO_ROOT/web/node_modules" ]]; then
    ( cd "$REPO_ROOT/web" && front_install )
  else
    log "web/node_modules already exists (use --rebuild to reinstall)"
  fi
elif [[ "$BUILD" == "skip" ]]; then
  [[ -d "$DIST" ]] || { err "web/dist does not exist and --no-build was used. Run without --no-build."; exit 1; }
  log "Skipping the build; serving the existing web/dist"
elif [[ "$BUILD" == "force" ]]; then
  if ! resolve_npm; then
    err "--rebuild requires npm: $NPM_ERROR"
    exit 1
  fi
  build_front
else # auto
  if resolve_npm; then
    if [[ -d "$DIST" ]]; then
      log "web/dist already exists (use --rebuild to rebuild it)"
    else
      build_front
    fi
  elif [[ -d "$DIST" ]]; then
    warn "npm could not be prepared ($NPM_ERROR)"
    warn "serving the existing web/dist WITHOUT rebuilding — front-end changes will not show up."
  else
    err "No web/dist and no usable npm: $NPM_ERROR"
    err "Install Node.js (+ network access for the npm bootstrap) and run ./start.sh --rebuild,"
    err "or copy in a prebuilt web/dist."
    exit 1
  fi
fi

# ---- 3. daemon env ---------------------------------------------------------
export GRAPHAGENTS_SOCKET="${GRAPHAGENTS_SOCKET:-/tmp/graph-agents.sock}"
export GRAPHAGENTS_HTTP_PORT="${GRAPHAGENTS_HTTP_PORT:-8080}"
export GRAPHAGENTS_PROJECT_ROOT="${GRAPHAGENTS_PROJECT_ROOT:-$PWD}"

echo
log "graph-agents"
echo "  ingest socket : $GRAPHAGENTS_SOCKET"
echo "  page + socket : http://localhost:$GRAPHAGENTS_HTTP_PORT (ws at /ws)"
echo "  project root  : $GRAPHAGENTS_PROJECT_ROOT"
echo "  hooks         : copy the \"hooks\" block from config/settings.json into the"
echo "                  .claude/settings.json of the project you want to observe."
echo

# ---- 4. start --------------------------------------------------------------
if [[ "$MODE" == "dev" ]]; then
  log "Starting the daemon (background) + Vite dev server (http://localhost:5173)"
  "$PYTHON" -m daemon.server &
  DAEMON_PID=$!
  # bring the daemon down when vite (in the foreground) exits
  trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT INT TERM
  ( cd "$REPO_ROOT/web" && exec "$NPM" run dev )
else
  echo "  http (front)  : http://localhost:$GRAPHAGENTS_HTTP_PORT"
  echo
  log "Starting the daemon (Ctrl-C to stop)"
  exec "$PYTHON" -m daemon.server
fi
