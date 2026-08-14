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
3. **NEVER commit or push without the user asking for it.** Not at the end of a task, not
   because the work is finished, not because the tree is dirty and tests are green. Leave
   changes in the working tree and say what is uncommitted; the user decides what becomes
   history and what gets published. This covers `git commit`, `git push`, `git merge`,
   branch deletion, tags, and opening PRs. "Implement X" is not permission to commit X, and
   permission given once does not carry to the next change.

## Architecture

Data flows through five stages. Keep this separation when adding code.

1. **Seed** — `graphagents/tree.py` walks the project root once at daemon boot and publishes
   the existing tree as `origin: "seed"` events. Without it the graph opens blank and only
   ever holds the handful of files an agent happened to touch — nothing like Gource.
2. **Capture** — two sources, deliberately (see "Conventions & gotchas"):
   - `.claude/settings.json` hooks fire `hooks/emit_event.py` and carry the **agent id**.
     `PostToolUse` on `Write` → `A`/`M`, on `Edit`/`MultiEdit` → `M`, on `Bash` → parse the
     command for `rm`/`rmdir`/`mv`/`mkdir`/`touch`/`cp`.
   - `daemon/watcher.py` (inotify via watchdog) reports **every** change on disk, with no
     idea who caused it.
3. **Normalize + aggregate** — `daemon/server.py` owns the shared state: the set of seen
   paths (drives `A` vs `M`, and lets a directory delete prune its subtree), the seed
   snapshot, the replay buffer, and the last agent to act (which is what attributes a
   filesystem change to an agent).
4. **Transport** — a Unix socket for ingest; WebSocket + static HTTP on one port out.
5. **Render** — `web/` (three.js), not the Gource binary. The `gource --realtime` path
   described below is the original design, kept because the log format is still our
   vocabulary.

### Gource custom log format
Pipe-delimited: `timestamp|user|type|path|color`
- `type` is `A`, `M`, or `D`
- `color` (optional, hex, no `#`) — we set it by op type: `A`→`33FF33`, `M`→`FFAA00`, `D`→`FF3333`
- Example: `1754870400|agent-worker|M|src/api/users.ts|FFAA00`

### Agent attribution (the hard part)
"Show what *each* agent is doing" means mapping every op to an actor. What the hook JSON
actually carries was settled by capture, not by reasoning — measured against Claude Code
2.1.229 with `GRAPHAGENTS_TRACE_LOG` (below). Re-measure before trusting it on a new version:

- A tool call made by the **orchestrator** carries `session_id` and **no** `agent_id` /
  `agent_type` — the keys are absent, not empty.
- A tool call made by a **subagent** carries the same `session_id` **plus** `agent_id` (an
  opaque per-subagent id) and `agent_type` (the readable name: `desenvolvedor-backend`,
  `desenvolvedor-tester`, ...). Subagent tool calls **do** fire the hook; this was the open
  question, and the answer is yes.

So `actor_of` (in `normalize.py`, shared with the daemon) resolves the actor as `agent_id`
when usable, else `session_id`, else `""`. `agent_type` becomes the event's `label`.

**`agent` is identity; `label` is only text.** The actor key and its color hash come from
`agent`, so two subagents of the same type stay two figures with two colors. Never key an
actor on the label.

The `label` had to reach the watcher path too: a filesystem change credited to a subagent
inherits its id *and* its name, or the specialist's figure goes nameless for half the events
it causes.

## Intended layout

```
graphagents/normalize.py  # pure: hook JSON → Event; also actor_of / seed_event / fs_event
graphagents/tree.py       # boot snapshot of the observed project
graphagents/repo.py       # pure: reads .git/HEAD for the branch (never shells out to git)
hooks/emit_event.py       # hook entrypoint: JSON in → daemon socket
daemon/server.py          # EventHub: seed, attribution, dedupe, meta, WebSocket + HTTP
daemon/watcher.py         # inotify watcher (watchdog)
config/settings.json      # hooks to install into a target project's .claude/
web/src/avatar.ts         # the agent figure, painted on a canvas
web/src/eventLog.ts       # pure: the recent-changes list model (drops seed, folds repeats)
web/src/attribution.ts    # pure: has any attributed event arrived? (latch, never unlatches)
web/src/search.ts         # pure: match, the walk over matches, and the camera frame for them
web/src/searchKeys.ts     # pure: what ctrl+F / F3 / Esc mean
web/src/*Hud.ts           # thin DOM painters: context caption, event list, attribution, search box
run.sh / start.sh         # minimal launcher / full bootstrap
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
- **`A` vs `M`** requires knowing prior existence — the daemon's `known_paths` set decides it.
- **Two capture sources, both required.** Hooks give *authorship* but only cover Claude's
  file tools and cannot resolve a glob or a compound command; the watcher gives
  *completeness* but no attribution. They are combined in `EventHub`: a filesystem change
  within `ATTRIBUTION_WINDOW_SECONDS` of a hook inherits that hook's agent, and a path a hook
  just reported is suppressed on the watcher side so one write flashes once. Neither source
  replaces the other.
- **When the parser would have to guess, it stays silent.** `_parse_bash` returns `None` for
  globs and directory destinations rather than inventing a path: a wrong node stays on screen
  forever, a missing one is filled in by the watcher milliseconds later.
- **An event with `agent: ""` must never create an actor** — seeded files and unattributed
  changes are real, but nobody did them on camera.
- If you fork/embed Gource's C++ source later, note it is **GPLv3** — that affects distribution.

## Agents & TDD workflow

Custom agents live in `.claude/agents/`:

- **`desenvolvedor-backend`** — implements Python (adapter, hook scripts, aggregator daemon, CLI).
- **`desenvolvedor-frontend`** — implements TypeScript under `web/` (three.js renderer, the
  pure model/layout/view/label modules, WebSocket client, UI).
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

Web MVP implemented and verified end-to-end (TDD).

- **Backend** (`graphagents/`, `hooks/`, `daemon/`): 182/182 pytest green. Hook is stdlib-only
  and exits 0 on garbage input. Daemon seeds the project tree at boot, ingests hook events on
  a Unix socket, watches the filesystem, and serves `web/dist` over HTTP **and** broadcasts
  events over WebSocket (`/ws`) on a single port (`:8080`) — one forwarded port is enough for
  remote/SSH use, and the browser derives the socket URL from its own origin.
- **Frontend** (`web/`): 368/368 vitest green, `tsc` + `vite build` clean. Gource-style WebGL
  renderer (three.js force layout + `UnrealBloomPass` + per-agent figure and beams), pure
  `simulation.ts` model, typed `parseEvent`, auto-reconnecting `wsClient.ts`. Label placement
  lives in pure `labels.ts` (like `view.ts`) because `renderer.ts` needs a GL context and
  cannot be unit-tested: sizes are constant in **pixels** (the camera spans halfHeight
  2..4000, so a world-sized label is either sub-pixel or screen-filling), and file names go
  only to touched files plus — past a zoom threshold — the idle ones still on screen, capped
  at a 48-sprite pool whose slots stay bound to a path so a new event does not repaint every
  canvas. `updateLabels` runs **every frame**: positioning labels only on topology change
  left them stranded while the force layout kept moving the nodes.
- **Search (`ctrl+F`)** follows the same split: every decision is pure and tested —
  `search.ts` (substring match on the file name, or on the whole path once the query
  contains `/`; the walk `F3` takes over the matches; and `frameMatches`, which returns the
  camera target: one match is approached at `SEARCH_FOCUS_HALF_HEIGHT`, several are framed
  together with a margin) and `searchKeys.ts` (what a keystroke means). The renderer only
  paints: cyan nodes, a ring on the active one, and `focusOn` — the one camera transform
  that ignores `manual`, because a search is a direct order. Two things are load-bearing:
  the camera target is recomputed **every frame** from live positions (the force layout
  never stops moving, so a frame chosen once slides its matches off screen), and a live
  tree needs `refreshMatches`, not `setQuery`, to fold new events into an open search —
  `setQuery` restarts the walk by contract, which would throw an `F3` walk back to the
  overview every time a file was written. Touching the wheel disarms the camera without
  dropping the highlights; the next query or `F3` rearms it.
- **Text is not part of the glow.** Labels live in a separate `overlayScene`, drawn after
  the composer with `autoClear = false`. Every glyph pixel clears the bloom's 0.05
  threshold, so a label left in the main scene gets an additive halo that closes the
  counters of its letters. Four more rules keep names sharp, and all four were once broken
  at the same time: the sprite is scaled by `spriteHeightForEm` so the requested pixel
  height applies to the **em box**, not to the padded texture canvas (that alone cost a
  third of the size); textures are rasterised at `labelFontPixels(dpr)` — constant, because
  a label is always the same CSS height on screen — so sampling is 1:1 and mipmaps are off;
  positions pass through `snapToPixelGrid` anchored on the camera centre, since a sprite
  landing between device pixels is smeared by the linear filter; and every label texture is
  marked `SRGBColorSpace`, or the gamma of each antialiased edge shifts and fattens the
  outline. Do not resize the bloom pass by hand in `resize()` — the composer already sizes
  its passes in drawing-buffer pixels, and re-setting them in CSS pixels halves them on
  HiDPI screens.
- **Integration** (verified against a live daemon): tree seeded on connect; a Write flashes
  once across both channels; `cp *.md docs/` reports each file actually copied, credited to
  the agent; `rm -rf docs/` prunes the subtree; a non-agent edit appears with no actor; the
  meta frame arrives first and a branch switch is pushed without reconnecting; real captured
  hook payloads replayed through the daemon yield two distinct actors, the subagent's carrying
  its `agent_type` as a label.
  **Not yet verified:** the actual in-browser visual (this host has no Chrome, and a headless
  screenshot of an animated force layout proves nothing).

Run: `GRAPHAGENTS_PROJECT_ROOT=/path/to/observed ./start.sh`. Point the root at the project
you want to *watch*, not at `graph-agents`. Install attribution by copying the `hooks` block
from `config/settings.json` into the observed project's `.claude/settings.json` — hook changes
only apply to sessions started afterwards. Deps: `pip install -e '.[daemon]'`; the hook needs
nothing. Rebuilding `web/dist` (or running vitest/tsc) needs Node 18+ — `start.sh` silently
serves a stale `dist` when node is missing, so a front-end change can look like it did
nothing. Debug an empty screen with `GRAPHAGENTS_DEBUG_LOG` (records hook *failures*) or
`GRAPHAGENTS_TRACE_LOG` (records every raw payload, which is how the shape of the hook JSON
gets settled on a new Claude Code version) on the hook command.

**A tree that updates while nobody is on camera means the hooks are not installed.** The
watcher alone gives completeness with no authorship, every event arrives with `agent: ""`,
and an empty agent never creates an actor — so the graph looks alive and unattended, which is
indistinguishable from "no agent is working right now". That ambiguity cost real hours; the
page now says so itself, in the HUD, once activity has arrived with no author. This repo has
the block installed in its own `.claude/settings.json`.

Not yet built: custom avatar *images* per agent, `.gitignore` parsing, recorded-session
replay/export. Attribution of *watcher* events is time-based, so simultaneous agents can be
credited to one of them — hook events themselves are attributed exactly. Label textures are
rasterised once at the pixel ratio the renderer had at construction, so dragging the window
to a monitor of a different DPI leaves the names slightly soft until a reload.
