/**
 * Wire protocol for events pushed from the backend over the WebSocket.
 *
 * STUB written by the tester agent for the RED phase of TDD. The `AgentEvent`
 * type fixes the shared contract; `parseEvent` is left unimplemented so the
 * specifying tests fail for the right reason. Implementation belongs to
 * `developer-frontend`.
 */

/** Operation kind: Added, Modified, Deleted. */
export type EventType = "A" | "M" | "D";

/**
 * What produced the event.
 *
 * `hook`  — a Claude Code tool call: live activity with a known agent.
 * `seed`  — part of the project tree snapshot the daemon sends on connect.
 *           Backdrop: it must not flash, and it belongs to no agent.
 * `watch` — a change the filesystem watcher saw. Real activity, but the agent
 *           may be empty when it could not be attributed to one.
 */
export type EventOrigin = "hook" | "seed" | "watch";

/**
 * A single activity event as received from the backend.
 *
 * Matches the JSON broadcast contract:
 * `{ ts, agent, type, path, color }` where `color` is a hex string without `#`.
 */
export interface AgentEvent {
  /** Unix time in seconds (float). */
  ts: number;
  /** Actor id (the backend's session-derived agent); `""` when unattributed. */
  agent: string;
  /** Operation kind. */
  type: EventType;
  /** Path relative to the observed project root. */
  path: string;
  /** Hex color without a leading `#` (A->33FF33, M->FFAA00, D->FF3333). */
  color: string;
  /** Where the event came from. Absent on the wire means `"hook"`. */
  origin: EventOrigin;
  /**
   * Human-readable name of the actor (the hook's `agent_type`, e.g.
   * `"developer-backend"`), for DISPLAY only. Never an identity: `agent`
   * remains the actor key and the seed of its color, so two subagents of the
   * same type stay two figures. Absent on the wire means `""`, which is also
   * the legitimate orchestrator case (its payload carries no `agent_type`).
   */
  label: string;
}

/** The three valid operation kinds, used for runtime validation. */
const EVENT_TYPES: ReadonlySet<string> = new Set<EventType>(["A", "M", "D"]);

/** Valid origins. Anything else on the wire degrades to `"hook"`. */
const EVENT_ORIGINS: ReadonlySet<string> = new Set<EventOrigin>(["hook", "seed", "watch"]);

/** Type guard narrowing an unknown value to a plain (non-array) object. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Whether `value` is a real finite number (rejects NaN and strings). */
function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * Validate and parse a raw WebSocket message into an {@link AgentEvent}.
 *
 * Contract (see tests/protocol.test.ts):
 *   - Returns a fully-typed `AgentEvent` for a well-formed message.
 *   - Returns `null` for a non-object, a missing/mistyped field, or an invalid
 *     `type` value.
 *   - An absent or unrecognized `origin` degrades to `"hook"` rather than
 *     rejecting the event, so a page served from a newer or older daemon than
 *     the one broadcasting still draws everything it receives.
 *   - An absent or mistyped `label` degrades to `""` for the same reason: it is
 *     display text, never a reason to drop a frame. A page built against the
 *     daemon that broadcasts it still draws every event from one that does not
 *     (and vice versa) — just without a readable name for the actor.
 *   - NEVER throws: bad input from the network must be handled gracefully.
 *
 * @param raw The value received from the socket (already JSON-parsed or not).
 */
export function parseEvent(raw: unknown): AgentEvent | null {
  if (!isRecord(raw)) return null;

  const { ts, agent, type, path, color, origin, label } = raw;

  if (!isFiniteNumber(ts)) return null;
  if (typeof agent !== "string") return null;
  if (typeof path !== "string") return null;
  if (typeof color !== "string") return null;
  if (typeof type !== "string" || !EVENT_TYPES.has(type)) return null;

  const resolvedOrigin =
    typeof origin === "string" && EVENT_ORIGINS.has(origin)
      ? (origin as EventOrigin)
      : "hook";

  return {
    ts,
    agent,
    type: type as EventType,
    path,
    color,
    origin: resolvedOrigin,
    label: typeof label === "string" ? label : "",
  };
}

/**
 * What the daemon is observing, announced on the same socket as the events.
 *
 * Discriminated from {@link AgentEvent} by a `kind: "meta"` field the events do
 * not carry, so neither parser can accept the other's frame.
 */
export interface DaemonMeta {
  /** The observed project root, as the daemon wants it displayed. */
  root: string;
  /** Current git branch, or `null` when the root is not a git repository. */
  branch: string | null;
}

/**
 * Validate and parse a raw WebSocket message into a {@link DaemonMeta}.
 *
 * Contract (see tests/meta.test.ts):
 *   - Returns `null` for a non-object, for a missing/mistyped `root`, and for
 *     anything whose `kind` is not exactly `"meta"` (which is what keeps an
 *     activity event out).
 *   - A missing or mistyped `branch` degrades to `null` instead of rejecting
 *     the frame: `null` is the legitimate not-a-git-repo case anyway, and a
 *     page served by an older daemon must still show the path.
 *   - NEVER throws.
 *
 * @param raw The value received from the socket (already JSON-parsed or not).
 */
export function parseMeta(raw: unknown): DaemonMeta | null {
  if (!isRecord(raw)) return null;

  const { kind, root, branch } = raw;

  if (kind !== "meta") return null;
  if (typeof root !== "string") return null;

  return { root, branch: typeof branch === "string" ? branch : null };
}

/**
 * The daemon's answer to a Tab in the root bar.
 *
 * The browser cannot read the disk, so completion is a round trip: the reply
 * lands milliseconds later, while the user keeps typing. `path` is the text the
 * reply ANSWERS, and it is what lets the client recognise a stale one instead of
 * overwriting the characters typed in between.
 */
export interface RootCompletion {
  /** The text that was sent to be completed. */
  path: string;
  /** What it expands to (unchanged when nothing more is unambiguous). */
  completed: string;
  /** Directories the prefix still allows. A hint: may legitimately be empty. */
  matches: string[];
}

/** The daemon switched roots: empty the graph, the new tree is coming. */
export interface RootReset {
  /** The root now being observed; `""` when the frame did not name one. */
  root: string;
}

/** The daemon refused a typed root, with a reason to show the user. */
export interface RootError {
  /** The path that was refused. */
  path: string;
  /** Why, as the daemon put it; `""` when it did not say. */
  reason: string;
}

/**
 * Validate and parse a raw WebSocket message into a {@link RootCompletion}.
 *
 * Contract (see tests/rootProtocol.test.ts):
 *   - `kind` must be exactly `"completion"`, which is what keeps every other
 *     frame on this socket out.
 *   - `path` and `completed` are required: without `completed` there is nothing
 *     to adopt, and without `path` the reply cannot be matched to the field.
 *   - `matches` is a HINT, so it degrades to `[]` when absent or not an array,
 *     and non-string items are dropped one by one — a candidate that is not a
 *     string reaches the DOM as "[object Object]", but the rest of the list is
 *     still worth showing, and the `completed` path is worth adopting either way.
 *   - NEVER throws.
 */
export function parseCompletion(raw: unknown): RootCompletion | null {
  if (!isRecord(raw)) return null;

  const { kind, path, completed, matches } = raw;

  if (kind !== "completion") return null;
  if (typeof path !== "string") return null;
  if (typeof completed !== "string") return null;

  const candidates = Array.isArray(matches)
    ? matches.filter((item): item is string => typeof item === "string")
    : [];

  return { path, completed, matches: candidates };
}

/**
 * Validate and parse a raw WebSocket message into a {@link RootReset}.
 *
 * Contract (see tests/rootProtocol.test.ts):
 *   - Only `kind: "reset"` is accepted; anything else returns null, so an
 *     activity event never wipes the graph and a `meta` frame (one word away,
 *     and it also carries a `root`) never does either.
 *   - A missing or mistyped `root` degrades to `""` instead of rejecting the
 *     frame. This is stronger than `parseMeta`'s degradation and deliberate:
 *     dropping a reset leaves the old project's nodes on screen while the new
 *     project's tree streams in on top of them — two trees in one graph, with no
 *     event that will ever delete the first. Clearing under a nameless root is
 *     recoverable (the next `meta` names it); not clearing is not.
 *   - NEVER throws.
 */
export function parseReset(raw: unknown): RootReset | null {
  if (!isRecord(raw)) return null;
  if (raw.kind !== "reset") return null;

  return { root: typeof raw.root === "string" ? raw.root : "" };
}

/**
 * Validate and parse a raw WebSocket message into a {@link RootError}.
 *
 * Contract (see tests/rootProtocol.test.ts):
 *   - `kind` must be exactly `"rootError"` and `path` must be there: a refusal
 *     that names no attempt cannot be matched to what the user typed.
 *   - A missing or mistyped `reason` degrades to `""` rather than swallowing the
 *     refusal — dropping it would close the bar as if the root had been
 *     accepted, leaving the graph on the old project pretending to be the new.
 *   - NEVER throws.
 */
export function parseRootError(raw: unknown): RootError | null {
  if (!isRecord(raw)) return null;

  const { kind, path, reason } = raw;

  if (kind !== "rootError") return null;
  if (typeof path !== "string") return null;

  return { path, reason: typeof reason === "string" ? reason : "" };
}

/**
 * How the daemon chose to render the file it was asked for.
 *
 * The fallback chain: the `git diff` of the file, else its text, else a hex dump
 * when it is binary.
 */
export type FileViewMode = "diff" | "text" | "hex";

/** The three modes the panel knows how to draw. Anything else degrades to text. */
const FILE_VIEW_MODES: ReadonlySet<string> = new Set<FileViewMode>(["diff", "text", "hex"]);

/**
 * The daemon's answer to a click on a file.
 *
 * The browser cannot read the disk, so the content is a round trip on the same
 * socket the events arrive on. `path` is the file the frame ANSWERS, and it is
 * what lets the client recognise a reply for a file it has stopped showing.
 */
export interface FileView {
  /** The file this answer is about, relative to the observed root. */
  path: string;
  /** How to render {@link content}. */
  mode: FileViewMode;
  /** The diff, the text or the hex dump; `""` when the read failed. */
  content: string;
  /** Whether the daemon cut the output short. */
  truncated: boolean;
  /** Why the daemon could not show the file, or `""` when it could. */
  error: string;
}

/**
 * Validate and parse a raw WebSocket message into a {@link FileView}.
 *
 * Contract (see tests/fileViewProtocol.test.ts):
 *   - `kind` must be exactly `"fileView"` and `path` must be a string: an answer
 *     naming no file cannot be matched to the click that asked for it, and
 *     `applyView` would paint one file's diff under another file's name.
 *   - everything else DEGRADES rather than costing the frame, as `parseMeta`'s
 *     `branch` does. An unusable `mode` from a newer daemon falls back to
 *     `"text"` — the one rendering that is never actively wrong — a missing
 *     `content` or `error` to `""`, and a non-boolean `truncated` to `false`, so
 *     an "output cut" notice never lands on content that is whole. Dropping the
 *     frame instead would leave the panel spinning on `loading` forever, because
 *     no second reply is coming for that click.
 *   - NEVER throws.
 */
export function parseFileView(raw: unknown): FileView | null {
  if (!isRecord(raw)) return null;

  const { kind, path, mode, content, truncated, error } = raw;

  if (kind !== "fileView") return null;
  if (typeof path !== "string") return null;

  return {
    path,
    mode: typeof mode === "string" && FILE_VIEW_MODES.has(mode) ? (mode as FileViewMode) : "text",
    content: typeof content === "string" ? content : "",
    truncated: truncated === true,
    error: typeof error === "string" ? error : "",
  };
}

/**
 * How git sees a path that is not committed yet.
 *
 * The four words this page knows how to draw. A newer daemon reporting a fifth
 * (a rename, say) is not an error — the row is simply dropped, see
 * {@link parseStatus}.
 */
export type GitStatusState = "untracked" | "modified" | "added" | "deleted";

/** The four states, for runtime validation. Anything else drops its row. */
const GIT_STATUS_STATES: ReadonlySet<string> = new Set<GitStatusState>([
  "untracked",
  "modified",
  "added",
  "deleted",
]);

/** One uncommitted path, exactly as the daemon reported it. */
export interface GitStatusEntry {
  /** Path relative to the observed root; not normalized here. */
  path: string;
  /** What git says about it. */
  state: GitStatusState;
}

/**
 * The working tree's uncommitted changes, pushed on the same socket as events.
 *
 * The browser cannot read the disk (nor run `git`), so "what is dirty right
 * now?" is answered by the daemon and repolled. Discriminated from every other
 * frame on this socket by `kind: "status"`.
 */
export interface GitStatus {
  /** Whether the observed root is a git repository at all. */
  repo: boolean;
  /** Whether the daemon cut the list short. */
  truncated: boolean;
  /** The dirty paths, in the order the daemon walked them. */
  entries: GitStatusEntry[];
}

/**
 * Validate and parse a raw WebSocket message into a {@link GitStatus}.
 *
 * Contract (see tests/statusProtocol.test.ts):
 *   - `kind` must be exactly `"status"`. The gate matters in both directions: a
 *     status frame routed as activity would grow a node called "status" in the
 *     graph, and an activity event mistaken for a status frame would repaint the
 *     whole panel from one file save.
 *   - `entries` DEGRADES, as `parseCompletion`'s `matches` does: absent or
 *     mistyped it becomes `[]`, and a junk item is dropped ONE AT A TIME rather
 *     than costing the frame. This is the load-bearing choice here. A newer
 *     daemon that adds a fifth state would otherwise blank the panel for the
 *     four files this page does understand — and an empty panel does not read as
 *     "I could not parse one row", it reads as "the tree is clean", which is a
 *     bigger lie than a partial list.
 *   - `repo` and `truncated` are booleans or they are `false`: a truthy
 *     non-boolean would claim the output was cut when it was whole, or claim a
 *     git repository where there is none.
 *   - Order is preserved; sorting and capping belong to `statusList.ts`.
 *   - NEVER throws: this comes off the network.
 */
export function parseStatus(raw: unknown): GitStatus | null {
  if (!isRecord(raw)) return null;
  if (raw.kind !== "status") return null;

  const entries: GitStatusEntry[] = [];
  if (Array.isArray(raw.entries)) {
    for (const item of raw.entries) {
      if (!isRecord(item)) continue;
      const { path, state } = item;
      if (typeof path !== "string") continue;
      if (typeof state !== "string" || !GIT_STATUS_STATES.has(state)) continue;
      entries.push({ path, state: state as GitStatusState });
    }
  }

  return { repo: raw.repo === true, truncated: raw.truncated === true, entries };
}

/** Marker standing in for the elided middle of a truncated string. */
const ELISION = "…";

/**
 * Shorten `text` to at most `max` characters by eliding its MIDDLE.
 *
 * Clipping the tail (what CSS ellipsis does) would throw away the segment that
 * names the project — every checkout under `~/projects` renders identically.
 * Head and tail both survive here; the cut lands between them.
 *
 * When it has to cut, the result is exactly `max` characters long. `max <= 0`,
 * `NaN`, and empty text all yield `""` rather than throwing.
 */
export function truncateMiddle(text: string, max: number): string {
  if (text.length === 0) return "";
  if (!Number.isFinite(max) || max <= 0) return "";

  const limit = Math.floor(max);
  if (text.length <= limit) return text;
  if (limit <= ELISION.length) return text.slice(0, limit);

  const keep = limit - ELISION.length;
  const head = Math.ceil(keep / 2);
  const tail = keep - head;

  return text.slice(0, head) + ELISION + (tail > 0 ? text.slice(text.length - tail) : "");
}
