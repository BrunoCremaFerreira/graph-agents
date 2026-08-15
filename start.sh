#!/usr/bin/env bash
#
# start.sh — bootstrap + run do projeto graph-agents (visualizador web estilo Gource).
#
# Faz tudo a partir do zero:
#   1. cria/prepara a venv Python e instala as deps do daemon;
#   2. garante um npm utilizável (veja abaixo) e gera web/dist;
#   3. sobe o daemon: HTTP (serve o front) + WebSocket (/ws) na MESMA porta + ingest.
#
# Uso:
#   ./start.sh                # prod: garante o build e serve web/dist em http://localhost:8080
#   ./start.sh --rebuild      # força reinstalar/rebuildar o front antes de subir
#   ./start.sh --no-build     # pula o front, serve o web/dist já existente
#   ./start.sh --dev          # daemon + Vite dev server (hot reload) em http://localhost:5173
#   ./start.sh --print-npm    # só resolve o npm, imprime o caminho no stdout e sai (não sobe nada)
#   ./start.sh --help
#
# Como o npm é resolvido (nesta ordem):
#   1. $NPM do ambiente;
#   2. npm no $PATH;
#   3. bootstrap local: baixa o tarball do npm (curl ou wget) do registry, descompacta em
#      .npm-bootstrap/ e escreve um wrapper .npm-bootstrap/bin/npm que roda
#      `node .../npm-cli.js "$@"`. Precisa de node instalado; é cacheado (com o cache
#      quente nada é baixado). Distros que empacotam só o `node` (Debian/Ubuntu) caem aqui.
#
# Como o front é instalado (o MESMO caminho em prod e em --dev):
#   - com web/package-lock.json: `npm ci`, que instala A PARTIR do lock e não o reescreve
#     (um `npm install` aqui apaga os campos libc do lock). Se o `ci` sair não-zero (lock
#     fora de sincronia com o package.json), cai para `npm install` e segue.
#   - sem lockfile: `npm install` direto (um `ci` sem lock só dá erro confuso).
#   - em prod, `npm run build` no fim; em --dev não há build nenhum — o Vite serve do
#     fonte e o modo termina em `npm run dev`.
#   - em prod a instalação roda sempre, inclusive com web/node_modules já presente — é
#     isso que faz --rebuild significar "reinstala E reconstrói". Em --dev um
#     web/node_modules já existente é reaproveitado (o `npm ci` apaga e refaz a árvore
#     inteira, e --dev é o comando que se reinicia dezenas de vezes por hora); use
#     `./start.sh --dev --rebuild` para forçar a reinstalação também ali.
#
# Variáveis de ambiente (com defaults):
#   GRAPHAGENTS_SOCKET     /tmp/graph-agents.sock   socket Unix de ingest dos hooks
#   GRAPHAGENTS_HTTP_PORT  8080                     porta única: serve web/dist E o
#                                                   WebSocket em /ws (uma só porta
#                                                   encaminhada basta via SSH)
#   GRAPHAGENTS_PROJECT_ROOT  (cwd)                 raiz cujos paths viram relativos no grafo
#   PYTHON  NODE  NPM      overrides dos executáveis
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ---- flags -----------------------------------------------------------------
MODE="prod"      # prod | dev
BUILD="auto"     # auto | force | skip
PRINT_NPM=0
LOG_FD=1         # --print-npm manda todo log para stderr: o stdout é a resposta
for arg in "$@"; do
  case "$arg" in
    --dev)       MODE="dev" ;;
    --rebuild)   BUILD="force" ;;
    --no-build)  BUILD="skip" ;;
    --print-npm) PRINT_NPM=1; LOG_FD=2 ;;
    -h|--help)
      sed -n '2,46p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Argumento desconhecido: $arg (use --help)" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;36m▶ %s\033[0m\n' "$*" >&"$LOG_FD"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

# ---- 0. npm ----------------------------------------------------------------
NPM="${NPM:-}"
NPM_ERROR=""
# O pin existe por um motivo só: 10.9.4 é o npm mais novo que roda no Node 18 desta máquina
# (declara engines.node "^18.17.0 || >=20.5.0"; o npm 11 exige ^20.17.0 || >=22.9.0).
# Não é sobre o lockfile: medido, o 10.9.4 também remove as 42 linhas de `libc` num
# `npm install`, porque só o npm 11 as escreve. Quem preserva o lock é o `npm ci`.
NPM_BOOTSTRAP_VERSION="10.9.4"
BOOT_DIR="$REPO_ROOT/.npm-bootstrap"
BOOT_NPM="$BOOT_DIR/bin/npm"
BOOT_CLI="$BOOT_DIR/npm-$NPM_BOOTSTRAP_VERSION/package/bin/npm-cli.js"

download() {  # download <url> <arquivo-destino>
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
    return 0   # cache quente: nada é baixado
  fi

  local node
  node="${NODE:-}"
  if [[ -z "$node" ]]; then
    node="$(command -v node 2>/dev/null || command -v nodejs 2>/dev/null || true)"
  fi
  if [[ -z "$node" ]]; then
    NPM_ERROR="node não foi encontrado no PATH; sem node não há como preparar um npm local (instale o Node.js ou defina NODE=/caminho/para/node)."
    return 1
  fi

  local url="https://registry.npmjs.org/npm/-/npm-$NPM_BOOTSTRAP_VERSION.tgz"
  local tmp
  mkdir -p "$BOOT_DIR"
  tmp="$(mktemp -d "$BOOT_DIR/.tmp.XXXXXX")" || {
    NPM_ERROR="não consegui criar diretório temporário em $BOOT_DIR"
    return 1
  }

  log "Preparando um npm local em .npm-bootstrap (npm $NPM_BOOTSTRAP_VERSION)"
  if ! download "$url" "$tmp/npm.tgz"; then
    rm -rf "$tmp"
    NPM_ERROR="falha ao baixar o npm $NPM_BOOTSTRAP_VERSION de $url (download não completou — rede indisponível? curl/wget ausentes?)"
    return 1
  fi
  if ! tar xzf "$tmp/npm.tgz" -C "$tmp"; then
    rm -rf "$tmp"
    NPM_ERROR="o tarball do npm baixado não pôde ser descompactado (download corrompido?)"
    return 1
  fi
  if [[ ! -f "$tmp/package/bin/npm-cli.js" ]]; then
    rm -rf "$tmp"
    NPM_ERROR="o pacote npm baixado não contém bin/npm-cli.js"
    return 1
  fi

  # só agora o cache passa a existir: um download que falhou não deixa lixo utilizável
  rm -rf "$BOOT_DIR/npm-$NPM_BOOTSTRAP_VERSION"
  mkdir -p "$BOOT_DIR/npm-$NPM_BOOTSTRAP_VERSION" "$BOOT_DIR/bin"
  mv "$tmp/package" "$BOOT_DIR/npm-$NPM_BOOTSTRAP_VERSION/package"
  rm -rf "$tmp"

  cat > "$BOOT_NPM.new" <<EOF
#!/bin/sh
# gerado por start.sh — npm $NPM_BOOTSTRAP_VERSION local, sem instalação global
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
    err "Não foi possível obter um npm: $NPM_ERROR"
    exit 1
  fi
  printf '%s\n' "$NPM"
  exit 0
fi

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
DIST="$REPO_ROOT/web/dist"

front_install() {  # roda com cwd em web/
  if [[ -f package-lock.json ]]; then
    log "npm ci (front)"
    if ! "$NPM" ci; then
      warn "npm ci falhou (lock fora de sincronia com o package.json?); tentando npm install"
      "$NPM" install
    fi
  else
    log "npm install (front)"
    "$NPM" install
  fi
}

build_front() {
  ( cd "$REPO_ROOT/web"
    front_install
    log "npm run build (gera web/dist)"; "$NPM" run build )
}

if [[ "$MODE" == "dev" ]]; then
  if ! resolve_npm; then
    err "Modo --dev exige npm: $NPM_ERROR"
    exit 1
  fi
  # Mesma porta de entrada do prod (front_install): `ci` sobre o lock, fallback para
  # `install`. O guard de node_modules sobrevive só aqui — `npm ci` apaga e refaz a
  # árvore inteira, e --dev é o comando que se reinicia dezenas de vezes por hora —
  # mas --rebuild continua significando "reinstala" nos dois modos.
  if [[ "$BUILD" == "force" || ! -d "$REPO_ROOT/web/node_modules" ]]; then
    ( cd "$REPO_ROOT/web" && front_install )
  else
    log "web/node_modules já existe (use --rebuild para reinstalar)"
  fi
elif [[ "$BUILD" == "skip" ]]; then
  [[ -d "$DIST" ]] || { err "web/dist não existe e --no-build foi usado. Rode sem --no-build."; exit 1; }
  log "Pulando build; servindo web/dist existente"
elif [[ "$BUILD" == "force" ]]; then
  if ! resolve_npm; then
    err "--rebuild exige npm: $NPM_ERROR"
    exit 1
  fi
  build_front
else # auto
  if resolve_npm; then
    if [[ -d "$DIST" ]]; then
      log "web/dist já existe (use --rebuild para reconstruir)"
    else
      build_front
    fi
  elif [[ -d "$DIST" ]]; then
    warn "npm não pôde ser preparado ($NPM_ERROR)"
    warn "servindo o web/dist existente SEM rebuildar — mudanças no front não aparecem."
  else
    err "Sem web/dist e sem npm utilizável: $NPM_ERROR"
    err "Instale Node.js (+ rede para o bootstrap do npm) e rode ./start.sh --rebuild,"
    err "ou copie um web/dist pronto."
    exit 1
  fi
fi

# ---- 3. env do daemon ------------------------------------------------------
export GRAPHAGENTS_SOCKET="${GRAPHAGENTS_SOCKET:-/tmp/graph-agents.sock}"
export GRAPHAGENTS_HTTP_PORT="${GRAPHAGENTS_HTTP_PORT:-8080}"
export GRAPHAGENTS_PROJECT_ROOT="${GRAPHAGENTS_PROJECT_ROOT:-$PWD}"

echo
log "graph-agents"
echo "  ingest socket : $GRAPHAGENTS_SOCKET"
echo "  page + socket : http://localhost:$GRAPHAGENTS_HTTP_PORT (ws em /ws)"
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
