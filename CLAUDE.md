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
   implement on its own. All work goes through the specialists: `developer-tester`
   (tests/RED), `developer-backend` (Python) and/or `developer-frontend` (TypeScript),
   collaborating according to the layer involved.
3. **NEVER commit or push without the user asking for it.** Not at the end of a task, not
   because the work is finished, not because the tree is dirty and tests are green. Leave
   changes in the working tree and say what is uncommitted; the user decides what becomes
   history and what gets published. This covers `git commit`, `git push`, `git merge`,
   branch deletion, tags, and opening PRs. "Implement X" is not permission to commit X, and
   permission given once does not carry to the next change.
4. **English is the only language in this repository.** Identifiers, function and file names,
   comments, docstrings, commit messages, agent definitions, and — this is the one that keeps
   getting missed — every string a human ends up reading: HUD text, `start.sh` log lines and
   `--help` output, error messages. Half-Portuguese was the actual state of this repo (the
   git-status panel counted changes in Portuguese under an English keys legend; `start.sh`
   explained itself entirely in Portuguese), and mixing is worse than either language alone:
   the reader switches mid-sentence, and grepping for a message seen on screen finds nothing.
   `tests/test_language_policy.py` enforces this over the authored sources — it fails on any
   accented Latin letter or a short list of unaccented Portuguese words, so do not quote the
   forbidden text in a file it scans, describe it. The `tests/` trees are exempt only because
   encoding tests need real non-ASCII fixtures, never as licence for prose in another
   language. Talking to the user in Portuguese is fine — writing it into a file is not.

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
  opaque per-subagent id) and `agent_type` (the readable name: `developer-backend`,
  `developer-tester`, ...). Subagent tool calls **do** fire the hook; this was the open
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
graphagents/paths.py      # pure: resolve a typed root, and complete a directory like a shell
graphagents/hexdump.py    # pure: the xxd format, byte for byte, + is-this-binary
graphagents/gitcmd.py     # the ONE place that forks `git` (kill + close + reap on timeout)
graphagents/diff.py       # the uncommitted diff of one file (see the note in Status)
graphagents/status.py     # pure parse of `git status --porcelain -z` + the poll's frame
graphagents/file_view.py  # what a clicked file shows: diff, else text, else hex
hooks/emit_event.py       # hook entrypoint: JSON in → daemon socket
daemon/server.py          # EventHub: seed, attribution, dedupe, meta, WebSocket + HTTP
daemon/watcher.py         # inotify watcher (watchdog)
config/settings.json      # hooks to install into a target project's .claude/
web/src/avatar.ts         # the agent figure, painted on a canvas
web/src/eventLog.ts       # pure: the recent-changes list model (drops seed, folds repeats)
web/src/attribution.ts    # pure: has any attributed event arrived? (latch, never unlatches)
web/src/search.ts         # pure: match, the walk over matches, and the camera frame for them
web/src/rootPrompt.ts     # pure: the ctrl+L bar's state (text, completion, discard on Esc)
web/src/pick.ts           # pure: which file a click (or the resting pointer) landed on
web/src/labels.ts         # pure: label size/placement, and which files are named this frame
web/src/fileView.ts       # pure: the content panel's state (request, adopt, discard, tokens)
web/src/language.ts       # pure: path -> the grammar id, or null (no generic fallback)
web/src/diffModel.ts      # pure: the unified diff, parsed into numbered rows
web/src/fileDoc.ts        # pure: what the panel draws — rows, gutter, tokenize requests
web/src/highlight.ts      # the ONE place that names shiki (lazy wasm + 22 literal imports)
web/src/statusList.ts     # pure: the uncommitted-changes panel (order, cap, is it visible)
web/src/searchKeys.ts     # pure: what ctrl+F / F3 / Esc mean
web/src/*Hud.ts           # thin DOM painters: context caption, event list, attribution, search
                          # box, git status panel
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

- **`developer-backend`** — implements Python (adapter, hook scripts, aggregator daemon, CLI).
- **`developer-frontend`** — implements TypeScript under `web/` (three.js renderer, the
  pure model/layout/view/label modules, WebSocket client, UI).
- **`developer-tester`** — writes tests only, **never** production code; drives development via TDD.

This project follows **Test-Driven Development**. The intended loop:

1. **RED** — `developer-tester` writes the smallest failing test that specifies a behavior
   (pytest for backend, vitest for frontend) and confirms it fails for the right reason.
2. **GREEN** — a `developer-*` agent writes the minimal implementation to make it pass.
3. **REFACTOR** — with the suite green, refactor safely; tests must stay green.

Subagents don't call each other directly — the main session orchestrates the hand-off
(tester produces the failing tests → developer implements to green). Start a new feature by
asking the tester for the RED tests, not by asking a developer to implement blind.

## Status

Web MVP implemented and verified end-to-end (TDD).

- **Backend** (`graphagents/`, `hooks/`, `daemon/`): 503/503 pytest green. Hook is stdlib-only
  and exits 0 on garbage input. Daemon seeds the project tree at boot, ingests hook events on
  a Unix socket, watches the filesystem, and serves `web/dist` over HTTP **and** broadcasts
  events over WebSocket (`/ws`) on a single port (`:8080`) — one forwarded port is enough for
  remote/SSH use, and the browser derives the socket URL from its own origin.
- **The observed root is no longer a boot constant.** `ctrl+L` in the page opens a bar that
  swaps it: the WebSocket, once broadcast-only, now also accepts `{"kind":"complete"}` (the
  browser cannot read the disk, so the daemon answers tab-completion) and
  `{"kind":"setRoot"}`. `Session.switch_root` stops the watcher, calls `EventHub.reset` —
  which clears `known_paths`, the seed and the replay, and broadcasts a `reset` frame the
  clients wipe their graph on — re-seeds, and restarts the watcher on the new root. Two
  details are load-bearing: `reset` sits FIRST in `replay_messages()`, so a client connecting
  mid-switch clears before it is handed the new tree; and `scan_tree` runs through
  `asyncio.to_thread`, because a root like `~` would otherwise block the event loop for
  seconds and freeze every viewer. The branch poll reads the session's current root each turn
  — capturing it would caption the new project with the old project's branch forever.
- **Clicking a file opens what is inside it.** A click (not a drag: under 4 px and 400 ms, and
  never the second half of a double-click, which belongs to auto-fit) picks the nearest file
  node within ~14 device px and asks the daemon for `{"kind":"file"}`. The answer is, in this
  order, the `git diff HEAD --` of that path, else its text, else an `xxd` dump — and `xxd`
  means the real format: the tests compare against the installed binary rather than trusting
  a hand-written spec. Two defences apply because the path arrives from the network:
  `resolve_inside` refuses anything resolving outside the observed root (symlinks included),
  and the content is capped at 256 KiB, flagged `truncated`, because it crosses the WebSocket
  whole. **Caveat, unfixed:** that cap is on the text/hex path only — `mode: "diff"` returns
  `git diff` output with no cap at all, so a regenerated dump crosses whole. `MAX_ROWS` bounds
  what the browser draws; the frame itself is still unbounded.
- **The panel colours what it shows, with VS Code's own palette.** Not an imitation: `shiki`
  carries the real TextMate grammars and the Dark+ theme, so `#569CD6` on a keyword is the
  colour VS Code would paint. Five things are load-bearing:
  - **The whole engine is lazy.** `highlight.ts` is reached by `await import("./highlight")` on
    the first file opened, so the entry chunk is unchanged (measured: +5 KB, and `grep -c
    shikijs dist/assets/index-*.js` is 0). Each grammar is its own chunk.
  - **The 22 language imports must be written as literal arrows.** `` import(`@shikijs/langs/${id}`) ``
    does not work — Vite's dynamic-import-vars plugin cannot glob a bare specifier into
    `node_modules`, and it either fails the build or drags all 346 grammars (~15 MB) into
    `dist`. Typing the table as `Record<LanguageId, Loader>` makes tsc prove it stays in step
    with `language.ts`.
  - **The engine is oniguruma (WASM), and that was measured, not assumed.** The JavaScript
    RegExp engine is ~10× smaller and its docs claim full coverage, but on these 22 grammars it
    diverges on two, and is wrong on both: in C++ a trailing `// c` never becomes a comment, and
    in HTML the embedded `<script>`/`<style>` handling collapses. `forgiving: true` swallows the
    pattern silently. Re-measure before switching.
  - **A diff is tokenized one fragment per hunk per side**, never as two concatenated documents:
    hunks are not contiguous, so joining them invents adjacency, and one unterminated string in
    an early hunk would poison every later one. A context line is present in the *old* fragment
    so the grammar sees coherent code, but its row index there is `-1` — it is painted from the
    *new* side. `code.split("\n").length === rows.length` is the invariant that holds it
    together. The residual cost is inherent to diffs: a hunk that opens inside a block comment
    tokenizes its first lines out of context, exactly as it does on GitHub.
  - **No shiki outside `highlight.ts`, not even `import type`.** That is what keeps the suite
    mock-free, jsdom-free and fast; `CodeToken` is ours, and `highlight.ts` renames shiki's
    `.content` to `.text` and resolves the optional colour on the way through.
  Budget: 4 000 lines / 128 KiB to colour, 20 000 rows before the panel falls back to today's
  single text node. Over budget the diff keeps its rows, stripes and gutter and loses only the
  colour, and the header says so. Unknown extension → plain, deliberately: no generic lexer.
- **The diff reads like the CLI now.** Old/new line-number gutter, a full-width stripe on the
  row (`rgba(63,185,80,.16)` / `rgba(248,81,73,.16)` — translucent so the tokens stay legible on
  top), and the syntax coloured over it. Three details: the stripe covers sign + code but *not*
  the gutter, because banded line numbers are harder to scan; `user-select: none` on the gutter,
  or copying a snippet takes the numbers with it; and `--- a/x` / `+++ b/x` are `meta`,
  classified before the `+`/`-` rules — the old `diffLineClass` coloured them as del/add, which
  with a gutter would have handed them line numbers.
- **Two callers fork `git`, through one runner.** `graphagents/gitcmd.py` owns the fork itself
  and `diff.py`/`status.py` own the argv and the parsing. The "files, never `subprocess`" rule
  in `repo.py` is about the branch poll, and it still holds there: the branch is a dozen bytes
  in `.git/HEAD`. Neither of these has a small file to read — a diff means the index, zlib
  objects and a diff algorithm, and the working-tree status means the same plus the untracked
  walk — so reimplementing them to honour a rule written about one line of one file would be
  the wrong trade. Nothing here raises: no repo, no `git`, a non-zero exit or a timeout all
  mean `None`, which each caller reads as its own "nothing to show". On timeout the child is
  killed *and* its transport closed before waiting — a wrapper script leaves a grandchild
  holding the inherited pipe, and `wait()` alone hangs until it dies.
- **The bottom-right panel lists what is uncommitted,** and only then: over a clean tree, or
  outside a repository, it is not on screen at all (`visible` derives from the entry count,
  never from the `repo` flag — a permanent empty strip would report nothing). A row is a
  `modified` / `added` / `deleted` / `untracked` path, and clicking it opens the same viewer a
  click in the graph opens, through the same `openFile` in `main.ts`. Four things are
  load-bearing:
  - **It is a poll, in a task of its own** (`STATUS_POLL_INTERVAL_SECONDS`, 3 s, or
    `GRAPHAGENTS_STATUS_INTERVAL`; ≤ 0 disables it and creates no task). It cannot ride the
    branch poll: that one is fork-free by doctrine. It cannot be event-driven either — a
    `git add` or a `git commit` typed in a terminal touches only `.git/`, which the watcher
    drops through `tree.is_ignored`, so the list would never notice the commit that emptied it.
    A round is skipped while one is still in flight, and outside a repository nothing forks at
    all (`find_checkout_root` answers first).
  - **The frame is deduped** in a replaceable slot like `_meta`, so a status that has not
    changed costs nothing on the wire, and it sits in `replay_messages()` before the seed —
    the panel is right on the first paint, not three seconds later.
  - **`git status` reports paths relative to the REPOSITORY root** even when run from a
    subdirectory (measured, not assumed), while everything else here — the graph, the click,
    `resolve_inside` — speaks in paths relative to the OBSERVED root, which `ctrl+L` may have
    pointed at a subdirectory. `relativize` converts them and drops what falls outside.
  - **A deleted file had to become clickable**, so `file_view` now tries the diff *before*
    concluding "no such file": the row the user most wants to open is the one whose content is
    gone. The directory check stays ahead of the diff.
- **Control commands are loopback-only** (`GRAPHAGENTS_ALLOW_REMOTE_CONTROL=1` opens them up).
  The listener binds every interface, so without the gate anyone who can reach `:8080` could
  list the host's directories and repoint the graph. SSH and VS Code forwarding arrive as
  loopback, so the ordinary remote setup is unaffected.
- **Frontend** (`web/`): 901/901 vitest green, `tsc` + `vite build` clean. `shiki` (pinned to
  3.23.0 — 4.x needs Node ≥ 20 and this machine has 18) is the first runtime dependency added
  since `d3-force`; note that `npm install` under npm 10 strips the `libc` fields from
  `package-lock.json`, so check `git diff` on the lock after touching dependencies. Gource-style WebGL
  renderer (three.js force layout + `UnrealBloomPass` + per-agent figure and beams), pure
  `simulation.ts` model, typed `parseEvent`, auto-reconnecting `wsClient.ts`. Label placement
  lives in pure `labels.ts` (like `view.ts`) because `renderer.ts` needs a GL context and
  cannot be unit-tested: sizes are constant in **pixels** (the camera spans halfHeight
  2..4000, so a world-sized label is either sub-pixel or screen-filling), and file names go
  only to touched files plus — past a zoom threshold — the idle ones still on screen, capped
  at a 48-sprite pool whose slots stay bound to a path so a new event does not repaint every
  canvas. `updateLabels` runs **every frame**: positioning labels only on topology change
  left them stranded while the force layout kept moving the nodes.
- **Pointing at a dot names it.** With the tree framed whole every rule above conspires to
  keep the node under the pointer anonymous — it is cold (that is *why* the user is asking)
  and the camera is past the zoom threshold — so the only way to ask "what is that one?" was
  to click it and open a viewer over the graph. `hoverTarget` (pick.ts) is a thin guard
  around `pickFile`: it answers `null` while the pointer is off the canvas or a drag is in
  progress (a pan moves the tree *under* the pointer instead of inspecting it), and otherwise
  returns the click's own answer — same `PICK_RADIUS_PIXELS`, because what you see named must
  be what a click would open. `selectFileLabels` and `fileLabelOpacity` take the hovered path
  and exempt it from the cold-plus-far cut, ahead of the search matches (a hover is the
  question being asked right now; a query is a standing one) but still inside the cull and the
  48-slot cap. The renderer records the position on every `pointermove` and resolves the hover
  **every frame**, from the label candidate list it has just refilled: the force layout never
  settles, so a node slides under a pointer that has not moved, and the camera changes what is
  under it too. Only `pointerType === "mouse"` counts — a touchscreen has no hover, and a
  finger would leave a name stuck where it last landed. The cursor follows (`pointer` over a
  file, `grab` otherwise, `grabbing` untouched during a drag).
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
  its `agent_type` as a label. For the status panel, against a scratch repository holding one
  file of each state: the frame reaches a fresh client ahead of the seed with the four states
  correct and clean files absent; clicking the *deleted* file answers `mode: "diff"` with the
  removal; committing everything empties the list (`repo: true`, no entries); switching the
  root to a subdirectory relativizes the paths to it and drops what is outside; a root outside
  any repository answers `repo: false`.
  For the highlighter, the boundary that no unit test reaches was driven end to end outside the
  browser instead: the real `buildDoc → highlightChunks → applyTokens → buildDoc` path, over a
  real file and a real `git diff` from this repo, rendered both as ANSI and as HTML against the
  shipped stylesheet. That confirmed the Dark+ colours, the stripes, the numbering across
  hunks, italic/bold, and that an unknown extension still gets a gutter.
  **Not yet verified:** the actual in-browser visual (this host has no Chrome — no chromium, no
  playwright, no selenium — and a headless screenshot of an animated force layout proves
  nothing). Outstanding: whether the bottom-right status panel clears `#context` and `#hud` at
  narrow window widths, and, for the viewer, the gutter's alignment on *wrapped* lines, the new
  `#file-view-lang` span at narrow widths, and how the stripes read on a real monitor.

Run: `GRAPHAGENTS_PROJECT_ROOT=/path/to/observed ./start.sh`. Point the root at the project
you want to *watch*, not at `graph-agents` — or start anywhere and switch with `ctrl+L` in the
page (the switch is global: one daemon watches one root, so every viewer follows). Install attribution by copying the `hooks` block
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
