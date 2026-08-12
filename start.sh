#!/usr/bin/env bash
#
# start.sh — bootstrap + run do projeto graph-agents (visualizador web estilo Gource).
#
# Faz tudo a partir do zero:
#   1. cria/prepara a venv Python e instala as deps do daemon;
#   2. (se possível) instala deps do front e gera web/dist;
#   3. sobe o daemon: WebSocket (broadcast) + HTTP (serve o front) + ingest dos hooks.
#
# Uso:
#   ./start.sh                # prod: garante o build e serve web/dist em http://localhost:8080
#   ./start.sh --rebuild      # força reinstalar/rebuildar o front antes de subir
#   ./start.sh --no-build     # pula o front, serve o web/dist já existente
#   ./start.sh --dev          # daemon + Vite dev server (hot reload) em http://localhost:5173
#   ./start.sh --help
#
# Variáveis de ambiente (com defaults):
#   GRAPHAGENTS_SOCKET     /tmp/graph-agents.sock   socket Unix de ingest dos hooks
#   GRAPHAGENTS_WS_PORT    8765                     porta do WebSocket
#   GRAPHAGENTS_HTTP_PORT  8080                     porta HTTP que serve web/dist
#   GRAPHAGENTS_PROJECT_ROOT  (cwd)                 raiz cujos paths viram relativos no grafo
#   PYTHON  NODE  NPM      overrides dos executáveis
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ---- flags -----------------------------------------------------------------
MODE="prod"      # prod | dev
BUILD="auto"     # auto | force | skip
for arg in "$@"; do
  case "$arg" in
    --dev)      MODE="dev" ;;
    --rebuild)  BUILD="force" ;;
    --no-build) BUILD="skip" ;;
    -h|--help)
      sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Argumento desconhecido: $arg (use --help)" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

# ---- 1. Python / daemon ----------------------------------------------------
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  log "Criando virtualenv em .venv"
  python3 -m venv "$REPO_ROOT/.venv"
  PYTHON="$REPO_ROOT/.venv/bin/python"
fi
if ! "$PYTHON" -c "import websockets" >/dev/null 2>&1; then
  log "Instalando deps do daemon (pip install -e '.[daemon]')"
  "$PYTHON" -m pip install --quiet --upgrade pip
  "$PYTHON" -m pip install --quiet -e "$REPO_ROOT[daemon]"
fi

# ---- 2. Frontend -----------------------------------------------------------
NPM="${NPM:-$(command -v npm || true)}"
DIST="$REPO_ROOT/web/dist"

build_front() {
  ( cd "$REPO_ROOT/web"
    [[ -d node_modules ]] || { log "npm install (front)"; "$NPM" install; }
    log "npm run build (gera web/dist)"; "$NPM" run build )
}

if [[ "$MODE" == "dev" ]]; then
  if [[ -z "$NPM" ]]; then
    err "Modo --dev exige npm/node no PATH (ou defina NPM=/caminho/para/npm)."
    exit 1
  fi
  [[ -d "$REPO_ROOT/web/node_modules" ]] || ( cd "$REPO_ROOT/web" && log "npm install (front)" && "$NPM" install )
elif [[ "$BUILD" == "skip" ]]; then
  [[ -d "$DIST" ]] || { err "web/dist não existe e --no-build foi usado. Rode sem --no-build."; exit 1; }
  log "Pulando build; servindo web/dist existente"
elif [[ "$BUILD" == "force" ]]; then
  [[ -n "$NPM" ]] || { err "--rebuild exige npm no PATH."; exit 1; }
  build_front
else # auto
  if [[ -d "$DIST" ]]; then
    log "web/dist já existe (use --rebuild para reconstruir)"
  elif [[ -n "$NPM" ]]; then
    build_front
  else
    err "Sem web/dist e sem npm no PATH — não há front para servir."
    err "Instale Node.js e rode ./start.sh --rebuild, ou copie um web/dist pronto."
    exit 1
  fi
fi

# ---- 3. env do daemon ------------------------------------------------------
export GRAPHAGENTS_SOCKET="${GRAPHAGENTS_SOCKET:-/tmp/graph-agents.sock}"
export GRAPHAGENTS_WS_PORT="${GRAPHAGENTS_WS_PORT:-8765}"
export GRAPHAGENTS_HTTP_PORT="${GRAPHAGENTS_HTTP_PORT:-8080}"
export GRAPHAGENTS_PROJECT_ROOT="${GRAPHAGENTS_PROJECT_ROOT:-$PWD}"

echo
log "graph-agents"
echo "  ingest socket : $GRAPHAGENTS_SOCKET"
echo "  websocket     : ws://localhost:$GRAPHAGENTS_WS_PORT"
echo "  project root  : $GRAPHAGENTS_PROJECT_ROOT"
echo "  hooks         : copie o bloco \"hooks\" de config/settings.json para o"
echo "                  .claude/settings.json do projeto que você quer observar."
echo

# ---- 4. subir --------------------------------------------------------------
if [[ "$MODE" == "dev" ]]; then
  log "Subindo daemon (background) + Vite dev server (http://localhost:5173)"
  "$PYTHON" -m daemon.server &
  DAEMON_PID=$!
  # derruba o daemon quando o vite (foreground) encerrar
  trap 'kill "$DAEMON_PID" 2>/dev/null || true' EXIT INT TERM
  ( cd "$REPO_ROOT/web" && exec "$NPM" run dev )
else
  echo "  http (front)  : http://localhost:$GRAPHAGENTS_HTTP_PORT"
  echo
  log "Subindo daemon (Ctrl-C para parar)"
  exec "$PYTHON" -m daemon.server
fi
