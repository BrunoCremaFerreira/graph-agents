/**
 * The model behind the HUD's git status panel: what is uncommitted right now.
 *
 * Pure — no DOM, no three.js — for the same reason as `eventLog.ts` and
 * `labels.ts`: `statusHud.ts` must stay a dumb painter, and the test environment
 * is `node`. Four decisions carry the weight:
 *
 *  - **`visible` derives from the ENTRY COUNT, never from `repo`.** The panel
 *    exists to show uncommitted work; a clean tree has nothing to say, and a
 *    panel that appears empty over a clean checkout is a permanent strip of
 *    chrome reporting nothing. Keying it on `repo` would do exactly that.
 *  - **The order is total and computed here.** Grouped by state in
 *    {@link STATE_ORDER}, then by path compared as plain strings. NOT
 *    `localeCompare`: its answer depends on the runtime's locale data, so the
 *    same dirty tree would list differently on two machines and rows would swap
 *    under the reader's eye when the daemon repolls.
 *  - **The cut respects the order.** `rows` is the SORTED list truncated at
 *    `max`, so what survives a 5000-file `git status` is the first rows of the
 *    order and not of whatever order the daemon happened to walk in.
 *  - **Glyph and CSS class travel WITH the row,** so the painter chooses
 *    nothing and a fifth state added later cannot land in the DOM unstyled.
 *
 * Nothing received is mutated: the entries array is the parsed frame, which the
 * caller keeps, and a shuffled copy of it would leak into anything else reading
 * the status.
 */

import type { GitStatus, GitStatusState } from "./protocol";

/** Deep enough to scroll through, small enough for a repo-wide reformat. */
export const DEFAULT_MAX_ROWS = 200;

/** Display order of the groups: what the user changed, then what is untracked. */
export const STATE_ORDER: readonly GitStatusState[] = [
  "modified",
  "added",
  "deleted",
  "untracked",
];

/** Operation glyph per state, mirroring the activity list's vocabulary. */
export const STATE_GLYPH: Record<GitStatusState, string> = {
  modified: "~",
  added: "+",
  deleted: "−",
  untracked: "?",
};

/** Class suffix per state, so CSS owns the actual colours. */
export const STATE_CLASS: Record<GitStatusState, string> = {
  modified: "m",
  added: "a",
  deleted: "d",
  untracked: "u",
};

/** One line of the panel: a path, plus everything needed to paint it. */
export interface StatusRow {
  /** Path relative to the observed root, exactly as received. */
  path: string;
  /** What git says about it. */
  state: GitStatusState;
  /** The glyph to show. */
  glyph: string;
  /** The class suffix to style it with. */
  cssClass: string;
}

export interface StatusListModel {
  /** Whether the panel belongs on screen at all. */
  visible: boolean;
  /** The rows to paint, ordered and capped. */
  rows: StatusRow[];
  /** How many entries the frame carried, cut or not. */
  total: number;
  /** How many the cap left out. */
  hidden: number;
}

/** A degenerate cap (0, negative, NaN, Infinity) falls back to the default. */
function resolveMax(max: number | undefined): number {
  if (max === undefined || !Number.isFinite(max)) return DEFAULT_MAX_ROWS;
  const limit = Math.floor(max);
  return limit >= 1 ? limit : DEFAULT_MAX_ROWS;
}

/** Rank of a state in {@link STATE_ORDER}; unknown states sort last. */
function rankOf(state: GitStatusState): number {
  const rank = STATE_ORDER.indexOf(state);
  return rank < 0 ? STATE_ORDER.length : rank;
}

/**
 * Decide what the git status panel shows for `status`.
 *
 * @param status The last status frame, or `null` before one has arrived.
 * @param max Row cap; defaults to {@link DEFAULT_MAX_ROWS}.
 */
export function buildStatusList(status: GitStatus | null, max?: number): StatusListModel {
  const entries = status && Array.isArray(status.entries) ? status.entries : [];
  const total = entries.length;
  if (total === 0) return { visible: false, rows: [], total: 0, hidden: 0 };

  // A copy: the caller still holds the parsed frame's array.
  const sorted = entries.slice().sort((a, b) => {
    const byGroup = rankOf(a.state) - rankOf(b.state);
    if (byGroup !== 0) return byGroup;
    if (a.path < b.path) return -1;
    if (a.path > b.path) return 1;
    return 0;
  });

  const limit = resolveMax(max);
  const rows: StatusRow[] = sorted.slice(0, limit).map((entry) => ({
    path: entry.path,
    state: entry.state,
    glyph: STATE_GLYPH[entry.state] ?? "?",
    cssClass: STATE_CLASS[entry.state] ?? "",
  }));

  return { visible: true, rows, total, hidden: total - rows.length };
}
