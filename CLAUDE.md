# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

`graph-agents` is a **real-time visualizer of what each Claude Code agent is doing**
(file/directory creation, edition, deletion), rendered with the **Gource** look.

The core insight: we do **not** reimplement Gource. Gource already accepts a live
event stream on STDIN. Our job is an **adapter** that captures Claude Code hook
events and translates them into Gource's log format:

```
Claude Code agent(s)  ──PostToolUse hook (JSON)──►  adapter  ──Gource log line──►  gource --realtime
```

Each Gource "user" (the on-screen actor/avatar) represents **one agent**.

## Mandatory rules (non-negotiable)

1. **ALWAYS use TDD before any development.** No line of production code is written before a
   failing test exists specifying the desired behavior. The Red → Green → Refactor cycle
   (see "Agents & TDD workflow") is mandatory for every feature, fix, or refactor — no
   exceptions.
2. **All planning and implementation are done by the specialist agents together, never by the
   orchestrator alone.** The main agent orchestrates and delegates; it does not plan or
   implement on its own. All work goes through the specialists: `desenvolvedor-tester`
   (tests/RED), `desenvolvedor-backend` (Python) and/or `desenvolvedor-frontend` (TypeScript),
   collaborating according to the layer involved.

## Architecture

Data flows through four stages. Keep this separation when adding code.

1. **Capture** — `.claude/settings.json` hooks fire an adapter script.
   - `PostToolUse` on `Write` → `A` (added) or `M` (modified)
   - `PostToolUse` on `Edit` / `MultiEdit` → `M`
   - `PostToolUse` on `Bash` → parse the command for `rm`/`rmdir`/`mv`/`mkdir`/`touch`/`cp`
     (deletions and directory ops only reach us this way — Claude has no native Delete tool)
   - `PreToolUse` on `Write` → decide `A` vs `M` by checking if the file already exists
2. **Normalize** — `hooks/emit_event.py` reads the hook JSON on stdin, resolves the
   path relative to the project root, picks the op type, and emits one Gource line.
3. **Transport** — a named pipe (MVP) or a local socket daemon (multi-session).
4. **Render** — `gource --realtime --log-format custom -` (external binary for now).

### Gource custom log format
Pipe-delimited: `timestamp|user|type|path|color`
- `type` is `A`, `M`, or `D`
- `color` (optional, hex, no `#`) — we set it by op type: `A`→`33FF33`, `M`→`FFAA00`, `D`→`FF3333`
- Example: `1754870400|agent-worker|M|src/api/users.ts|FFAA00`

### Agent attribution (the hard part)
"Show what *each* agent is doing" means mapping every op to an actor.
- **Default / guaranteed:** use the hook's `session_id` as the Gource user. This already
  distinguishes multiple Claude Code instances running in parallel.
- **Per-subagent granularity** (Task tool) may not be in the hook JSON. Fallbacks, in order:
  read `transcript_path` (session JSONL) to find the active subagent; inject an id via env
  var at subagent creation; else collapse to one actor per session.
- Start with **one actor per session**; add per-subagent later. Do not over-engineer this early.

## Intended layout

```
hooks/emit_event.py   # hook entrypoint: JSON in → Gource line out
daemon/server.py      # (later) aggregates multiple sessions, feeds Gource
config/settings.json  # hooks to install into a target project's .claude/
avatars/              # one image per agent (gource --user-image-dir)
run.sh                # starts the pipe + gource with the right flags
```

## Running (target workflow)

```sh
mkfifo /tmp/claude-gource.pipe
tail -f /tmp/claude-gource.pipe | gource --realtime --log-format custom \
  --file-idle-time 0 --key -
# hooks append normalized lines to the pipe as agents work
```

`gource` is an external dependency: `apt install gource` / `brew install gource`.

## Conventions & gotchas

- **The adapter must be dependency-free and fast.** It runs on *every* tool call and blocks
  the agent loop. Use the Python 3 stdlib only; no heavy imports.
- **Never let the adapter fail loudly.** A crashing hook disrupts the user's Claude Code
  session. Wrap logic defensively; on error, exit 0 and stay silent.
- **Paths are relative to the project root** so Gource's directory tree stays clean.
- **`A` vs `M`** requires knowing prior existence — track seen paths or check the FS in `PreToolUse`.
- **Two capture sources, deliberate trade-off:** hooks give *authorship* but only cover
  Claude's file tools; a filesystem watcher (inotify/fswatch) gives *completeness* but loses
  attribution. Hooks are primary; a watcher is a future gap-filler, not a replacement.
- If you fork/embed Gource's C++ source later, note it is **GPLv3** — that affects distribution.

## Agents & TDD workflow

Custom agents live in `.claude/agents/`:

- **`desenvolvedor-backend`** — implements Python (adapter, hook scripts, aggregator daemon, CLI).
- **`desenvolvedor-frontend`** — implements TypeScript (WebGL renderer, WebSocket client, UI).
  Only relevant if the target is **web** (Route C); a native desktop target (Route B, C++) would
  replace this agent.
- **`desenvolvedor-tester`** — writes tests only, **never** production code; drives development via TDD.

This project follows **Test-Driven Development**. The intended loop:

1. **RED** — `desenvolvedor-tester` writes the smallest failing test that specifies a behavior
   (pytest for backend, vitest for frontend) and confirms it fails for the right reason.
2. **GREEN** — a `desenvolvedor-*` agent writes the minimal implementation to make it pass.
3. **REFACTOR** — with the suite green, refactor safely; tests must stay green.

Subagents don't call each other directly — the main session orchestrates the hand-off
(tester produces the failing tests → developer implements to green). Start a new feature by
asking the tester for the RED tests, not by asking a developer to implement blind.

## Status

Web MVP implemented and verified end-to-end (TDD, all via the specialist agents).

- **Backend** (`graphagents/normalize.py`, `hooks/emit_event.py`, `daemon/server.py`): 36/36
  pytest green. Hook is stdlib-only and exits 0 on garbage input. Daemon ingests hook events
  on a Unix socket, then serves `web/dist` over HTTP **and** broadcasts events over WebSocket
  (`/ws`) on a single port (`:8080`) — one forwarded port is enough for remote/SSH use, and
  the browser derives the socket URL from its own origin. A/M logic lives in the daemon via a
  `known_paths` set.
- **Frontend** (`web/`): 23/23 vitest green, `tsc` + `vite build` clean. Gource-style WebGL
  renderer (three.js force layout + `UnrealBloomPass` + per-actor beams), pure `simulation.ts`
  model, typed `parseEvent`, auto-reconnecting `wsClient.ts`.
- **Integration:** real Write → hook → daemon → WebSocket delivers the correct event shape;
  HTTP serves the built page. **Not yet verified:** the actual in-browser visual (no browser run).

Run: `./run.sh` (starts the daemon). Build the front once with `cd web && npm install && npm run build`.
Install capture by copying the `hooks` block from `config/settings.json` into the observed
project's `.claude/settings.json`. Deps: `pip install -e '.[daemon]'` for the daemon; the hook
needs nothing. See "Mandatory rules" and "Agents & TDD workflow" for how changes are made.

Not yet built: per-subagent attribution (currently one actor per session), Bash `mv` paired
`A`-of-destination, user avatars/images, recorded-session replay/export.
