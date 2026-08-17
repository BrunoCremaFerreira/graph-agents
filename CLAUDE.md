# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

`rhizome-graph` is a **real-time visualizer of what each Claude Code agent is doing**
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

1. **Seed** — `rhizome_graph/tree.py` walks the project root once at daemon boot and publishes
   the existing tree as `origin: "seed"` events. Without it the graph opens blank and only
   ever holds the handful of files an agent happened to touch — nothing like Gource.
2. **Capture** — two sources, deliberately (see "Conventions & gotchas"):
   - `.claude/settings.json` hooks fire `hooks/emit_event.py` and carry the **agent id**.
     `PostToolUse` on `Write` → `A`/`M`, on `Edit`/`MultiEdit` → `M`, on `Bash` → parse the
     command for `rm`/`rmdir`/`mv`/`mkdir`/`touch`/`cp`, on `Read` → `R`.
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
- `type` is `A`, `M`, `D`, or `R`
- `color` (optional, hex, no `#`) — we set it by op type: `A`→`33FF33`, `M`→`FFAA00`,
  `D`→`FF3333`, `R`→`AA66FF`
- Example: `1754870400|agent-worker|M|src/api/users.ts|FFAA00`
- `R` (read) is ours, not Gource's: the file was *opened*, nothing about the tree changed.
  It rides the same wire and is drawn as a violet pulsing ring, but it mutates no state —
  see "A read is not a change".

### Agent attribution (the hard part)
"Show what *each* agent is doing" means mapping every op to an actor. What the hook JSON
actually carries was settled by capture, not by reasoning — measured against Claude Code
2.1.229 with `RHIZOME_TRACE_LOG` (below). Re-measure before trusting it on a new version:

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
rhizome_graph/normalize.py  # pure: hook JSON → Event; also actor_of / seed_event / fs_event
rhizome_graph/tree.py       # boot snapshot of the observed project
rhizome_graph/repo.py       # pure: reads .git/HEAD for the branch (never shells out to git)
rhizome_graph/paths.py      # pure: resolve a typed root, and complete a directory like a shell
rhizome_graph/token.py      # pure: the control token — mint, compare, and inject into the page
rhizome_graph/hexdump.py    # pure: the xxd format, byte for byte, + is-this-binary
rhizome_graph/gitcmd.py     # the ONE place that forks `git` (kill + close + reap on timeout)
rhizome_graph/diff.py       # the uncommitted diff of one file (see the note in Status)
rhizome_graph/status.py     # pure parse of `git status --porcelain -z` + the poll's frame
rhizome_graph/file_view.py  # what a clicked file shows: diff, else text, else hex
rhizome_graph/cli.py        # pure: argv + environ + cwd → frozen Settings; `rhi` itself
rhizome_graph/assets.py     # pure: where THIS installation keeps web/dist, and the hook command
rhizome_graph/ipc.py        # is that socket live? is that port free? (answers, never raises)
rhizome_graph/launch.py     # the one place `rhi` names the daemon side
rhizome_graph/window.py     # pure choice of backend + the two strategies (never sees a token)
rhizome_graph/hookinstall.py # pure: diagnose a settings file, merge a hook block (idempotent)
rhizome_graph/hook.py       # the hook's ONE implementation, stdlib-only, installed as rhi-hook
hooks/emit_event.py         # shim over rhizome_graph/hook.py, for a checkout with no install
daemon/server.py            # EventHub: seed, attribution, dedupe, meta, WebSocket + HTTP
daemon/watcher.py           # inotify watcher (watchdog)
config/settings.json        # hooks to install into a target project's .claude/
debian/ packaging/ Formula/ # the .deb's control + build script + shims, and the Homebrew formula
tools/webview_spike.py      # the WebGL measurement a human runs on a real desktop session
web/src/avatar.ts           # the agent figure, painted on a canvas
web/src/eventLog.ts         # pure: the recent-changes list model (drops seed, folds repeats)
web/src/attribution.ts      # pure: has any attributed event arrived? (latch, never unlatches)
web/src/search.ts           # pure: match, the walk over matches, and the camera frame for them
web/src/rootPrompt.ts       # pure: the ctrl+L bar's state (text, completion, discard on Esc)
web/src/pick.ts             # pure: which file a click (or the resting pointer) landed on
web/src/labels.ts           # pure: label size/placement, and which files are named this frame
web/src/fileView.ts         # pure: the content panel's state (request, adopt, discard, tokens)
web/src/fileViewClicks.ts   # pure: which click closes the panel (never the backdrop)
web/src/language.ts         # pure: path -> the grammar id, or null (no generic fallback)
web/src/diffModel.ts        # pure: the unified diff, parsed into numbered rows
web/src/fileDoc.ts          # pure: what the panel draws — rows, gutter, tokenize requests
web/src/highlight.ts        # the ONE place that names shiki (lazy wasm + 22 literal imports)
web/src/readMarker.ts       # the violet ring a file wears while an agent is reading it
web/src/statusList.ts       # pure: the uncommitted-changes panel (order, cap, is it visible)
web/src/searchKeys.ts       # pure: what ctrl+F / F3 / Esc mean
web/src/branding.ts         # pure: APP_NAME, so the untestable renderer never spells it
web/src/token.ts            # pure: read the control token, stamp it on a command frame
web/src/*Hud.ts             # thin DOM painters: context caption, event list, attribution, search
                            # box, git status panel
run.sh / start.sh           # minimal launcher / full bootstrap
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
- **A read is not a change.** `R` travels the same wire as `A`/`M`/`D` and must touch none of
  the state they touch. In the daemon it goes through `_broadcast_transient`, never
  `_publish`: not into `known_paths` (Read-then-Edit is the commonest thing an agent does, so
  a read that marks the path as seen turns the very next `Write` into a modification of a node
  no browser was ever shown), not into `_recent` (a reconnecting client would replay stale
  read flashes — a lie about "right now" — and twenty reads would push the real changes out of
  a finite ring), and not into `_hook_paths` (a read has no watcher echo to suppress, and
  stamping it would swallow the genuine write that follows). In the browser, `reading` is a
  channel of its own on `SimNode`, so a read never repaints the amber flash of a write half a
  second old. What a read *does* still do is refresh the active agent: it is evidence of who
  is at work, so the watcher's next change is credited to whoever was reading.
- **Reads are hook-only, by nature.** The watcher cannot see a file being opened — `atime` is
  unreliable under `relatime` and it does not subscribe to access events — so the "two capture
  sources" rule above has no second half here. With no hooks installed there is no read glow
  at all, only the silence that looks like a healthy setup with nobody reading.
- **A read path is held to a stricter rule than a write path.** `_read_path` refuses anything
  that does not stay under the observed root, `..` segments included, where `_make_relative`
  hands an out-of-root absolute path straight back. The asymmetry is deliberate: a stray write
  names a real change and the watcher corrects the picture moments later, while a read has no
  such correction — nothing happened on disk — and agents read `/etc`, `~/.claude`,
  `node_modules` and other checkouts all day. The check is lexical (`normpath`, then a
  boundary test) because `normalize_event` is pure and runs on the hot path; symlinks
  therefore still pass, and resolving them belongs to `resolve_inside` in the daemon.
- If you fork/embed Gource's C++ source later, note it is **GPLv3** — that affects distribution.

## Agents & TDD workflow

Custom agents live in `.claude/agents/`:

- **`developer-backend`** — implements Python (adapter, hook scripts, aggregator daemon, CLI).
- **`developer-frontend`** — implements TypeScript under `web/` (three.js renderer, the
  pure model/layout/view/label modules, WebSocket client, UI).
- **`developer-tester`** — writes tests only, **never** production code; drives development via TDD.
- **`security-auditor`** — audits the network surface, the path defences, the `git` runner and
  the hook; writes a report and a per-finding remediation plan. Writes **no** code, no tests,
  no fixes — its plan names the module to change and the RED test that must come first.
- **`software-architect`** — assesses structure for maintainability, performance and security:
  module boundaries, where a decision belongs, what a coupling will cost the next change.
  Writes **no** code, no tests, no fixes — it hands back an assessment plus a staged plan whose
  every step is one RED test and one GREEN implementation, with an owner per step. Use it
  before a feature that crosses layers, not for a change that fits inside one module.

This project follows **Test-Driven Development**. The intended loop:

1. **RED** — `developer-tester` writes the smallest failing test that specifies a behavior
   (pytest for backend, vitest for frontend) and confirms it fails for the right reason.
2. **GREEN** — a `developer-*` agent writes the minimal implementation to make it pass.
3. **REFACTOR** — with the suite green, refactor safely; tests must stay green.

Subagents don't call each other directly — the main session orchestrates the hand-off
(tester produces the failing tests → developer implements to green). Start a new feature by
asking the tester for the RED tests, not by asking a developer to implement blind. A security
finding enters the same loop rather than bypassing it: `security-auditor` reports, the tester
turns each fix plan into a failing test, and a developer takes it green. `software-architect`
enters it at the other end: it shapes the plan *before* the tester is asked for the first RED
test, and it is the agent to consult when the question is where code belongs rather than what
it should do.

## Status

Web MVP implemented and verified end-to-end (TDD).

- **Backend** (`rhizome_graph/`, `hooks/`, `daemon/`): 1081/1081 pytest green (1100 with
  `RHIZOME_PACKAGE_TESTS=1`, which opts into the slow builds — one wheel, one real `.deb`).
  Hook is stdlib-only and exits 0 on garbage input. Daemon seeds the project tree at boot,
  ingests hook events on a Unix socket, watches the filesystem, and serves `web/dist` over
  HTTP **and** broadcasts events over WebSocket (`/ws`) on a single port (`:8080`) — one
  forwarded port is enough for remote/SSH use, and the browser derives the socket URL from
  its own origin.
- **It is an installed application now: `rhi <dir>`.** The five stages above are untouched;
  what changed is how the daemon is started and where its assets live. Six things carry it,
  and each is a rule rather than a detail:
  - **Configuration is passed, not read from the air.** `rhizome_graph/cli.py` is a pure
    `argv + environ + cwd → Settings` (frozen), and `run(settings, ready=None)` takes it.
    `main()` is the **only** place in `daemon/server.py` that touches `os.environ`, and
    `tests/test_daemon_environment_boundary.py` pins that with **no exemptions** — its
    definition of "reads the environment" is deliberately wide enough to catch
    `default_web_dist(os.environ)` passed as an *argument*. Before this, `rhi` would have had
    to talk to the daemon through `os.environ`, which is a protocol between two halves of one
    program. `start.sh` keeps exporting variables and keeps working; it is the from-source
    developer bootstrap, `rhi` is the installed launcher, and all 50 of its pinned assertions
    are untouched by this work.
  - **`web/dist` is found by a search order, not by `__file__`.** `assets.py` walks
    `$RHIZOME_WEB_DIST` → the packaged `rhizome_graph/web` → `/usr/lib/rhizome-graph/web` →
    the checkout's `web/dist`. `daemon/server.py` keeps no path constant derived from
    `__file__` at all. The old constant served nothing the moment it was installed anywhere
    but a checkout, and the failure was a blank page with an `INFO` line, not an error.
  - **A default may be adjusted; an explicit request may not.** `choose_port` walks off a busy
    `:8080` and prints the port it actually got, but `--port 9000` that is taken **refuses**
    (rc 1, one line, no traceback) — a user who typed 9000 and got 9001 has been lied to. The
    ingest socket follows the same rule, and so does the window: `--window` with no backend is
    a refusal, while the *default* degrades to headless.
  - **A second daemon no longer steals the first's ingest socket.** That unlink was
    unconditional; now a live socket raises `IngestSocketInUseError` and a stale one is cleared
    as before. `ingest_socket_path` derives a root-hashed path for a second instance and `rhi`
    **prints the `RHIZOME_SOCKET` its hook block needs** — attribution for a second instance is
    opt-in and explicit rather than silently broken. The *same* root started twice refuses
    rather than walking again, and that refusal is inherited from the guard, not re-implemented.
  - **One teardown, four triggers.** Everything real — signal handlers, cancelling the polls,
    `session.stop()`, unlinking the socket — lives inside `run()` and resolves a single `stop`
    future. SIGTERM, SIGINT, an embedded caller's cancellation and **the window closing** all
    converge there; none of them adds teardown to `cli.py`. Same discipline as `closeView` in
    `main.ts`. `run()` also no longer *requires* signal handlers, which is what lets it live on
    a worker thread while the GUI keeps the main one — pywebview refuses to start anywhere else.
  - **`rhi` diagnoses hooks, offers to install them, and never writes silently.**
    `.claude/settings.json` is a committed file in many repositories, so editing it as a side
    effect of "show me a graph" writes into a git working tree unasked, and merging JSON hook
    arrays silently is how someone loses a hook. `--doctor` reads **both** `~/.claude` and the
    project's file, because Claude Code merges them — a globally installed hook really does
    fire, and a doctor that ignored it would report a failure that does not exist. One line of
    its report carries a settings path and the command it found *together*, so it answers
    "which file?". **The failure mode it exists for is rot, not absence:** a stale absolute path
    fails *louder and worse* than a missing hook — it is a blocking error on every tool call,
    degrading the agent session rather than the graph, which is exactly what this repository's
    own three settings files did until they were fixed during this work.
- **The window is a strategy, and the choice is pure.** `window.choose_window_backend(platform,
  available, requested)` answers `webview` / `app_browser` / `none` with availability injected,
  so no test needs a display. The framing "pywebview vs Tauri vs Go-webview" is **wrong**: on
  Linux all three bind the *same* WebKitGTK, so changing the shell's language retires no
  rendering risk at all. Only a Chromium engine is a different answer, which is why app-mode
  browser is a first-class fallback rather than an improvisation. `PREFERENCE_BY_PLATFORM` is
  the one table a spike result may edit; nothing else changes. The strategy takes
  `(url, on_closed, close_requested)` and **nothing else** — no token, by parameter or by
  import, asserted over the module's AST the way "no shiki outside `highlight.ts`" is. It needs
  none: the daemon injects the token into the `index.html` it serves, so a webview issuing
  `GET /` from loopback inherits it exactly as a browser does, and a window that *accepted* one
  would be a second place a credential lives. `close_requested` is a `threading.Event` rather
  than a returned handle because a strategy blocks until its window is gone — it would only
  return after the thing you wanted a handle to was dead.
- **Packaging: a `.deb` built here and inspected, and a Homebrew formula that no one has run.**
  `packaging/build-deb.sh` produces the package and writes nothing into the checkout. The
  vendored venv holds **`websockets` and nothing else** (1.4 MB, `--system-site-packages`), so
  `python3-watchdog` and `python3-gi` come from the distribution and keep its security updates.
  `websockets` must be vendored and `watchdog` must not: `websockets/asyncio/` has **0 files in
  12.0 and 8 in 13.0** (measured by unpacking the wheels) while noble ships 10.4, whereas the
  whole suite is green on `watchdog==3.0.0` and the watcher imports only three names that 3.0
  has. `git` is **Recommends, not Depends** — `gitcmd.py` answers `None` without it and only the
  diff and status panels go quiet. **The two commands split along the doctrine's own line:**
  `/usr/bin/rhi-hook` is `#!/usr/bin/python3` and reaches the sources at
  `/usr/lib/rhizome-graph/` with one `sys.path` insert, so the hot path never pays a venv's
  import cost and keeps working if the venv is rebuilt or deleted; `/usr/bin/rhi` names the
  vendored interpreter, because the daemon imports `websockets.asyncio.server`. The Python
  sources are installed as plain files *outside* the venv precisely so that split is possible.
  **They ship byte-compiled, in `unchecked-hash` mode, and both halves of that matter.**
  `/usr/lib` is unwritable to the user whose agent fires the hook, so without a shipped `.pyc`
  the interpreter recompiles on *every tool call* and can never cache the result: measured at
  41.1 ms against 37.2 ms per call, which is 17% of our own import cost above a 18.4 ms bare
  interpreter start. And the default timestamp mode would have been worse than useless here —
  `dpkg` stamps mtimes from the archive while source and bytecode are stamped by different
  build steps, so the `.pyc` is silently discarded and the cache rewrite into root-owned
  `/usr/lib` silently fails, leaving a package that recompiles every time *while still
  containing bytecode*. Checked-hash re-hashes the whole source on every import to detect edits
  to a dpkg-managed file, which is `dpkg -V`'s job and not the agent loop's. The `compileall`
  sits after the build script strips `*.py[co]` and before `du` computes `Installed-Size`;
  at that moment the staging tree holds only the two source trees, so the venv is never
  compiled — deliberately, since the daemon starts once per session and the hook does not.
  The formula's `sha256` is deliberately all zeros with a comment: there is no release, so no
  true digest exists, and a plausible-looking wrong one reads as done and never gets revisited.
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
  what the browser draws; the frame itself is still unbounded. **The panel closes with Escape
  or with the close button** in its header — a modal over the whole graph with no visible way
  out reads as a page that has hung, and the header carries the button alone: the `esc` caption
  that used to sit beside it was a second thing to read in a row already full of path, mode,
  language and truncation, and the `×` is the affordance nobody has to read. Which click closes is
  decided in the pure `fileViewClicks.ts`, the way Escape is decided in `fileViewKeys.ts`: both
  answer nothing while the panel is closed (a live close handler would swallow clicks meant for
  the file dots underneath), and the backdrop is deliberately **not** a dismiss target, because
  a stray click outside would throw away a long read the panel cannot restore. `fileViewHud.ts`
  binds one delegated listener on the container and stays a painter; both paths run through the
  same `closeView` in `main.ts`, never two.
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
- **Two callers fork `git`, through one runner.** `rhizome_graph/gitcmd.py` owns the fork itself
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
    `RHIZOME_STATUS_INTERVAL`; ≤ 0 disables it and creates no task). It cannot ride the
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
- **A control command must pass two gates: the peer's address AND a token.** The first is the
  older one — loopback-only, `RHIZOME_ALLOW_REMOTE_CONTROL=1` opens it up — and it is not
  enough on its own, because the peer's address lies in two measured ways. A WebSocket
  handshake is exempt from the same-origin policy and needs no preflight, so any page in any
  browser on the host can open `ws://127.0.0.1:8080/ws` and send `setRoot` + `file`; and any
  loopback-side proxy launders a remote connection into a local one, which `vite.config.ts`
  does by default (`host: true` plus a `/ws` proxy), so `./start.sh --dev` handed the whole
  LAN a gate that says `127.0.0.1`. Both were reproduced against a live daemon: arbitrary
  file read as the daemon's user, no credential anywhere in the protocol.
  So the daemon mints a token at boot (`rhizome_graph/token.py`), **injects it into the
  `index.html` it serves**, and refuses any command frame that does not carry it back. A
  cross-site page cannot read it — same-origin is what stops it fetching the page — and a
  proxy carries none. It was chosen over an `Origin` allow-list for one reason: an allow-list
  has to know the port, and `ssh -L 9000:localhost:8080` or a VS Code forward means the
  browser's origin is a port the daemon never hears about. A token is indifferent to how you
  reached the page. Five things are load-bearing:
  - **Two conditions, never one.** `token_matches` is checked *after* `control_allowed`, and
    neither replaces the other: the tests pin that a right token does not let a remote peer
    through, and that a wrong token is refused even with remote control opened up.
  - **The empty token is always refused.** `token_matches` returns `False` for an empty
    `expected` before it reaches `hmac.compare_digest` — a daemon that failed to mint one must
    not silently start accepting tokenless commands. Everything fails closed.
  - **`parse_command` always returns `kind`, `path` and `token`**, with `token: ""` when the
    frame named none (as it does for a `null`, a number or an object). Absence and emptiness
    must stay distinguishable, since the whole gate turns on the difference between the empty
    token and one that matches — so do not "tidy away" the empty key from an assertion.
  - **`inject_token` escapes for the script element, not just for JSON.** `json.dumps` then
    `<`, `>` and `&` folded to `\uXXXX`, so a token spelled as a closing script tag round-trips
    intact and leaves exactly one closing-script sequence in the page. An injection bug here
    would turn a security fix into an XSS.
  - **The browser stamps the token in `WsClient.send`**, the single chokepoint, so no call site
    can forget it; `main.ts` is unchanged. With no token available the frame carries **no
    `token` key at all**, not an empty one.
  `--dev` is the one case where the daemon never touches the HTML (Vite serves it and proxies
  `/ws`), so `start.sh` mints one token there and exports it twice: `RHIZOME_TOKEN` for the
  daemon and `VITE_RHIZOME_TOKEN` for Vite, which is the only prefix reaching
  `import.meta.env`. The prod branch exports **neither** — a `VITE_`-prefixed variable alive
  during `npm run build` is substituted into `dist/`, shipping a stale token inside the bundle.
  An existing `RHIZOME_TOKEN` is honoured verbatim, which is how a probe reaches the same
  daemon; `./start.sh --print-token` prints it and starts nothing. A mint that fails is
  tolerated rather than fatal (the `python` stub in the older `start.sh` tests answers every
  call with silence, and prod does not need the variable at all) — it warns on stderr in
  `--dev`, where the cost is that every command is refused.
- **Frontend** (`web/`): 990/990 vitest green, `tsc` + `vite build` clean. `shiki` (pinned to
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
- **A file an agent is reading wears a violet ring.** The fourth event type, `R`, exists
  because the graph used to stay dark through the half of an agent's work that explains the
  other half: it read six files, then wrote one, and only the write was on camera. A write is
  a *flash that decays*; a read is a *ring that pulses* — a different shape, not a different
  shade, so the two never blur together through the bloom. Five things carry it:
  - **`reading` is a channel of its own** on `SimNode`, decayed at `READING_DECAY_PER_SEC`
    (0.5/s against the highlight's 0.9/s — reads arrive in bursts and need to stay legible).
    `applyEvent` routes `R` to `readFile` *before* `touchFile` is ever reached, so a read
    raises `reading` and `opacity` and never touches `highlight` or `color`.
  - **A file first seen by a read enters cold** — no highlight, the neutral colour
    directories get. A read must never masquerade as a write.
  - **The ring is sized in pixels** via `labelMetrics.worldPerPixel`, like every label: the
    camera spans halfHeight 2..4000, so a world-sized ring is either sub-pixel or
    screen-filling. It lives in the main scene, not `overlayScene` — unlike text, a glow
    through the bloom is exactly what is wanted — with 24 pooled sprites whose slots stay
    bound to a path, and `updateReadMarkers` runs after `updateLabels` because it needs that
    frame's metrics.
  - **The read beam is short** (0.6 s against the write beam's 1.2). `MAX_BEAMS` is a fixed
    512 and reads come in bursts, so a long-lived read beam would crowd the write beams out
    of the buffer.
  - **Reads stay out of the recent-changes list.** That panel is a list of *changes*, and an
    agent reads roughly ten times more than it writes, so reads would push every real edit off
    the top within seconds. The drop sits before the fold, so a read cannot inflate the top
    entry's count either. The label candidates carry `max(highlight, reading)`, which is what
    names a file while it is being read with `labels.ts` unchanged.
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
- **Integration** (verified against a live daemon): for reads, driven through the real
  `hooks/emit_event.py` over a scratch project — the `R` frame reaches a client watching at
  that moment, violet, relative to the root, carrying the subagent's id *and* its
  `agent_type`; a read of a file outside the root produces no event at all; a `Write` after a
  `Read` of the same path is still an `A`; and no `R` survives in the replay handed to a
  client that connects afterwards, while the real write does. Also: tree seeded on connect; a Write flashes
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
  `#file-view-lang` span at narrow widths, how the stripes read on a real monitor, and whether
  the close button's padded hit area is comfortable to aim at and stays on the row once a long
  path has squeezed the header at narrow window widths. For the
  read ring: whether violet reads clearly against the write flash at real zoom levels, whether
  24 rings at once is calm or noisy during a read burst, whether the 0.75 tint leaves enough
  amber on a file read right after it was written, and how the ring's pulse sits next to the
  search ring when both land on the same node. One of those is a known risk, not just an
  unknown: the inner ring is a 2.24 px stroke on a 64 px texture rasterised with
  `generateMipmaps = false` and `LinearFilter` (the label doctrine), so drawn much smaller
  than 64 px it is sampled sparsely and can fade out — degrading the read marker into a single
  thin ring, which is precisely the shape it exists not to be. If that is what a real screen
  shows, thicken the inner stroke or rasterise the texture larger; the tests pin only
  relations between the radii, never their values, so retuning is free.
- **Not yet verified, for the installed application.** Everything below is green in the suite
  and unproven on a real machine. It is grouped by what would settle it, because the first
  group settles most of the rest.
  - **The spike, which gates the window's default.** `tools/webview_spike.py` on a real Linux
    desktop session (X11 *and* Wayland) and on macOS, against the running application rather
    than a synthetic demo: WebGL2 present, hardware vs software renderer, a float/half-float
    buffer for `UnrealBloomPass`, p95 frame time at ~1500 nodes, whether
    `WEBKIT_DISABLE_DMABUF_RENDERER=1` is needed, and `devicePixelRatio` against the same page
    in a browser. Its result edits `PREFERENCE_BY_PLATFORM` and the two packages' dependency
    lists, and nothing else. **This host is a tty** — no `DISPLAY`, no `WebKit2` namespace — so
    no agent here can run it.
  - **That a real window opens and a real window dies.** Not one line of `open_webview` has
    ever executed. Specifically: that `webview.start()` returning *is* the window closing; that
    `destroy()` from a worker thread tears down a WebKitGTK or WKWebView window; that
    `close_requested` is honoured at all under a native toolkit loop (the wakeup-fd path exists
    precisely because a Python signal handler may never run there, and neither path is proven);
    and that a Chromium in app mode exits on `terminate()` leaving no orphan and no profile
    directory. Snap confinement is a known suspect: `/snap/bin/chromium` with a
    `--user-data-dir` under `/tmp` may be refused, in which case the profile moves under `$HOME`.
  - **That anything is ever installed.** `dpkg -i` needs root, so no maintainer script has run,
    no dependency has been resolved by apt, `/usr/bin/rhi` has never executed from the prefix it
    was built for, and upgrade/removal/purge is untested. `debhelper` and `lintian` are missing
    here, so `debian/rules` is never exercised as a build entry point and Debian policy
    conformance is entirely unchecked. Nothing Homebrew has been executed — not `brew audit`,
    not `brew install`, not `brew test`; the formula is text that passes text assertions and has
    not even been parsed as Ruby. And `pip install` of the wheel into a clean environment is
    untested, so the console-script branch of `assets.hook_command()` is dead code as measured.
  - **That the hooks it writes actually work.** No Claude Code session has been started against
    a file `--install-hooks` produced, so nothing has put an attributed agent on the graph
    through the packaged `rhi-hook`. The doctor's central rule — that a hook in `~/.claude`
    alone fires for a session in another project — rests on Claude Code's merge behaviour and is
    asserted from documentation, not measured. A near-JSON settings file (a trailing comma, a
    `//` comment an editor tolerates) is treated as malformed and refused; correct by doctrine,
    but whether that is the common real-world shape is unmeasured.
  - **Smaller, and each with a named suspicion.** `web/dist` staleness is invisible to the build
    — it copies whatever is in the checkout, the same hazard `start.sh` has when node is missing.
    The doctor's report puts an absolute settings path and a command on one line deliberately,
    which will wrap on a narrow terminal (only ever seen at 100+ columns). `rhi --doctor` is
    pinned to bind nothing, but that a *live* daemon is undisturbed by it is not. And the
    `NO_BACKEND_NOTE` on a headless machine is the one behaviour here no test demanded — whether
    it reads as helpful or as noise is a judgement nobody has made on a real terminal.

Run, installed: `rhi /path/to/observed` — opens a window, serves the same page at
`http://localhost:8080`, and ends everything when the window closes. `rhi --no-window` for a
headless host, `--doctor` to check the hooks without starting anything, `--install-hooks` to
write them. Run, from a checkout: `RHIZOME_PROJECT_ROOT=/path/to/observed ./start.sh`. Point
the root at the project you want to *watch*, not at `rhizome-graph` — or start anywhere and
switch with `ctrl+L` in the page (the switch is global: one daemon watches one root, so every
viewer follows). Install attribution with `rhi --install-hooks`, which never writes without
being asked, or by copying the `hooks` block from `config/settings.json` into the observed
project's `.claude/settings.json` by hand — either way, hook changes only apply to sessions
started afterwards. Deps: `pip install -e '.[daemon]'`; the hook needs nothing. Rebuilding `web/dist` (or running vitest/tsc) needs Node 18+ — `start.sh` silently
serves a stale `dist` when node is missing, so a front-end change can look like it did
nothing. Debug an empty screen with `RHIZOME_DEBUG_LOG` (records hook *failures*) or
`RHIZOME_TRACE_LOG` (records every raw payload, which is how the shape of the hook JSON
gets settled on a new Claude Code version) on the hook command. A viewer that draws the graph
but refuses every `ctrl+L`, completion and file click is the control token failing, not the
page: `./start.sh --print-token` shows what the daemon expects, and `RHIZOME_TOKEN=<value>`
pins it when something outside the page has to send a command.

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
