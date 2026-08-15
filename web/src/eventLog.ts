/**
 * The model behind the HUD's recent-activity list: what happened, newest first.
 *
 * Pure — no DOM, no three.js — because every decision here is worth testing and
 * the list element itself (`eventHud.ts`) must stay a dumb painter of this
 * state. Three behaviours carry the weight:
 *
 *  - **Seed is dropped, and only seed.** The daemon replays the whole project
 *    tree on connect as `origin: "seed"`; accepting it opens the list full of
 *    backdrop and pushes the first real edit off the top. A `watch` event with
 *    `agent: ""` is a real change nobody could be credited for, and stays.
 *  - **Repeats collapse against the TOP entry only.** One save fires the hook
 *    and the watcher, and agents re-edit the same file in bursts. Folding into
 *    an older entry further down would reorder the list under the reader's eye.
 *  - **The list is bounded.** A long session cannot grow an array forever.
 *
 * Order is arrival order, never `ts`: the daemon's clock is not the order the
 * reader watched things happen in.
 */

import type { AgentEvent, EventType } from "./protocol";

/** Deep enough to scroll through, small enough for a day-long session. */
export const DEFAULT_MAX_ENTRIES = 200;

/** One line of the list: an operation on a path, plus how often it repeated. */
export interface LogEntry {
  /** Path relative to the observed project root, exactly as received. */
  path: string;
  /** Operation kind. */
  type: EventType;
  /** Actor id; `""` when the change could not be attributed. */
  agent: string;
  /** Timestamp of the MOST RECENT occurrence folded into this entry. */
  ts: number;
  /** Occurrences folded into this entry; `1` unless repeats collapsed. */
  count: number;
}

export interface EventLog {
  /**
   * Record an event.
   *
   * @returns `true` when the list changed (a new entry, or a repeat folded into
   * the top one — folding is not ignoring), `false` when the event was dropped.
   */
  push(event: AgentEvent): boolean;
  /** Current entries, newest first. A snapshot: mutating it is harmless. */
  entries(): readonly LogEntry[];
  /**
   * Drop every line, because the observed root changed.
   *
   * Each one names a file of the PREVIOUS project — paths that no longer exist,
   * credited to agents no longer on screen — so keeping them makes the HUD read
   * as activity in a project nobody is watching. It is not cosmetic either:
   * repeats collapse against the TOP entry, so a stale top line would fold the
   * new project's first edit into a count of 2 under the old project's.
   */
  reset(): void;
}

/** A degenerate cap (0, negative, NaN, Infinity) falls back to the default. */
function resolveMax(max: number | undefined): number {
  if (max === undefined || !Number.isFinite(max)) return DEFAULT_MAX_ENTRIES;
  const limit = Math.floor(max);
  return limit >= 1 ? limit : DEFAULT_MAX_ENTRIES;
}

/**
 * Create a bounded, newest-first log of activity events.
 *
 * @param max Maximum entries kept; defaults to {@link DEFAULT_MAX_ENTRIES}.
 */
export function createEventLog(max?: number): EventLog {
  const limit = resolveMax(max);
  const list: LogEntry[] = [];

  return {
    push(event: AgentEvent): boolean {
      if (event === null || typeof event !== "object") return false;
      if (typeof event.path !== "string") return false;
      if (event.origin === "seed") return false;
      // A read is not a change, and this list is a list of CHANGES. An agent
      // reads roughly ten times more often than it writes, so accepting `R`
      // would push every real edit off the top within seconds — and, since
      // repeats fold against the TOP entry only, a read of the file just saved
      // would either inflate that entry's count with work that changed nothing
      // or open a line of its own above it. Dropped before the fold, so the list
      // is left byte for byte as the read found it.
      if (event.type === "R") return false;

      const top = list[0];
      if (top && top.path === event.path && top.type === event.type) {
        top.count += 1;
        top.ts = event.ts;
        top.agent = event.agent;
        return true;
      }

      list.unshift({
        path: event.path,
        type: event.type,
        agent: event.agent,
        ts: event.ts,
        count: 1,
      });
      if (list.length > limit) list.length = limit;
      return true;
    },

    entries(): readonly LogEntry[] {
      return list.slice();
    },

    reset(): void {
      list.length = 0;
    },
  };
}

/**
 * Split `path` at its LAST slash, keeping the slash on the directory side.
 *
 * `dir + name` reassembles the original exactly: nothing is normalized, so a
 * repeated or leading slash survives into the rendered line instead of the HUD
 * quietly showing a path that is not the one on disk. The name never contains a
 * slash, which is what lets the two halves be painted in different greys.
 */
export function splitPath(path: string): { dir: string; name: string } {
  const cut = path.lastIndexOf("/");
  if (cut < 0) return { dir: "", name: path };
  return { dir: path.slice(0, cut + 1), name: path.slice(cut + 1) };
}
