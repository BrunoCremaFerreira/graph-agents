/**
 * Finding a node by name: what a typed query matches, and what the search box
 * is doing.
 *
 * A seeded project opens with its whole tree on screen, and past
 * FILE_LABEL_ZOOM_THRESHOLD none of it is named -- so locating one known file
 * means dragging the camera around reading labels until it turns up. This module
 * answers "where is renderer.ts?".
 *
 * Pure data and pure transforms, free of three.js and of the DOM, for the same
 * reason as {@link ./view} and {@link ./labels}: the renderer needs a GL context
 * and cannot be unit-tested, and a keyboard handler wired straight into the DOM
 * would bury this logic where no test can reach it. Every transition returns a
 * NEW state, like `ViewState`; nothing here is mutated in place.
 *
 * Two matching rules carry the weight:
 *
 *  - a query with no `/` matches only the LAST SEGMENT, so typing `src` while
 *    hunting for a file does not drag in everything under `web/src`;
 *  - a query containing `/` matches the whole relative path, which is the only
 *    way to ask for a subtree.
 *
 * And the result list is ordered BY PATH, never by arrival: F3 walks it frame
 * after frame while the force layout keeps moving the nodes, so an order that
 * depended on anything but the paths themselves would make "next match" jump at
 * random.
 */

import { MIN_HALF_HEIGHT, MAX_HALF_HEIGHT, type ViewTarget } from "./view";

/** A node of the graph, as far as searching is concerned. */
export interface SearchNode {
  readonly path: string;
  readonly kind: "file" | "dir";
}

/**
 * Half-height the camera closes in to when it approaches a single match.
 *
 * Below FILE_LABEL_ZOOM_THRESHOLD, or the found node would be an unnamed dot
 * among the others; above MIN_HALF_HEIGHT, or the bloom swallows it.
 */
export const SEARCH_FOCUS_HALF_HEIGHT = 25;

/**
 * Fraction of the visible half-extent the matches are allowed to reach.
 *
 * The rest is margin: matches sitting exactly on the edge of the screen read as
 * cut off, and the force layout keeps nudging them outwards after the frame is
 * chosen.
 */
const FRAME_FILL = 0.85;

/** Aspect to fall back on before the canvas has measured itself. */
const FALLBACK_ASPECT = 1;

/**
 * Whether a lowercased path matches a slashless query.
 *
 * The name itself matches by substring; the path also matches when the query
 * names a directory ANCHORED AT THE ROOT, which is how `daemon` reaches
 * `daemon/server.py` without a trailing slash. A directory buried mid-path is
 * deliberately not anchored: typing `src` to look for a file returns `web/src`
 * itself, not every file that merely lives under it.
 */
function matchesName(lowerPath: string, needle: string): boolean {
  const name = lowerPath.slice(lowerPath.lastIndexOf("/") + 1);
  return name.includes(needle) || lowerPath.startsWith(`${needle}/`);
}

/** Paths matching `query`, ordered by path. Empty for an empty query. */
export function matchPaths(nodes: readonly SearchNode[], query: string): string[] {
  const needle = query.trim().toLowerCase();
  // A held-down space bar must not select the entire project.
  if (!needle) return [];

  // A slash is how the user asks for a subtree; without one they mean a name.
  const wholePath = needle.includes("/");
  const matched: string[] = [];

  for (const node of nodes) {
    const path = node.path;
    const lower = path.toLowerCase();
    if (wholePath ? lower.includes(needle) : matchesName(lower, needle)) matched.push(path);
  }

  return matched.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

/** Whether the camera should frame every match or approach the active one. */
export type SearchFrame = "all" | "active";

export interface SearchState {
  /** True while the box is showing and taking keystrokes. */
  readonly open: boolean;
  readonly query: string;
  /** Matching paths, ordered by path. */
  readonly matches: readonly string[];
  /** Which match F3 has walked to. */
  readonly activeIndex: number;
  readonly frame: SearchFrame;
}

export function createSearchState(): SearchState {
  return { open: false, query: "", matches: [], activeIndex: 0, frame: "all" };
}

/**
 * Show the box, with nothing typed and nothing selected yet.
 *
 * The previous state is discarded on purpose: reopening on the last query would
 * highlight nodes the user has to clear before asking a new question.
 */
export function openSearch(_state: SearchState): SearchState {
  return { ...createSearchState(), open: true };
}

/**
 * Record what was typed and recompute the matches.
 *
 * A new query is a new question, so the walk restarts at the first match and the
 * camera goes back to framing all of them. Typing a prefix that matches nothing
 * keeps the box open -- the user is mid-word, and closing on the first miss
 * would dismiss the box halfway through every longer name -- but DELETING the
 * text closes it, which is how the box is dismissed without reaching for Escape.
 */
export function setQuery(
  state: SearchState,
  query: string,
  nodes: readonly SearchNode[],
): SearchState {
  // Only a deletion closes the box: ctrl+F opens with an empty query and the
  // input reports that emptiness once, which must not shut the box instantly.
  if (query === "" && state.query !== "") return closeSearch(state);

  return {
    open: true,
    query,
    matches: matchPaths(nodes, query),
    activeIndex: 0,
    frame: "all",
  };
}

/**
 * Recompute the matches against the tree as it is now, without disturbing the walk.
 *
 * The graph is LIVE: files enter and leave on every WebSocket event, so a result
 * list computed when the query was typed goes stale within seconds -- a file
 * created afterwards is never found, and a deleted one stays highlighted at a
 * position the camera can still be sent to. The only other recomputation,
 * {@link setQuery}, restarts the walk at index 0 and returns to `frame: "all"`,
 * because typing is a new question; driving it from the event stream would throw
 * anyone stepping through matches with F3 back to the overview every few seconds.
 *
 * Recomputing is not typing, so:
 *  - the walk is preserved BY PATH, not by index -- a new match sorting before
 *    the active one shifts every index, and holding the index would silently move
 *    the camera to a different file;
 *  - when the active path is gone the index falls back to a real match (clamped
 *    at `matches.length - 1`), since the camera is sent to `matches[activeIndex]`
 *    every frame;
 *  - `frame` survives, so an approach stays an approach;
 *  - an empty query does NOT close the box, unlike `setQuery`, where emptiness
 *    means the user deleted the text.
 */
export function refreshMatches(state: SearchState, nodes: readonly SearchNode[]): SearchState {
  if (!state.open) return state;

  const active = state.matches[state.activeIndex] ?? null;
  const matches = matchPaths(nodes, state.query);
  const found = active === null ? -1 : matches.indexOf(active);
  const activeIndex =
    found >= 0 ? found : Math.max(0, Math.min(state.activeIndex, matches.length - 1));

  return { ...state, matches, activeIndex };
}

/** Walk to the following match, wrapping around, and approach it. */
export function nextMatch(state: SearchState): SearchState {
  if (state.matches.length === 0) return state;
  return {
    ...state,
    activeIndex: (state.activeIndex + 1) % state.matches.length,
    frame: "active",
  };
}

/** Dismiss the box, leaving nothing matched and nothing highlighted. */
export function closeSearch(_state: SearchState): SearchState {
  return createSearchState();
}

/** The match the camera should be on, or null when there is none. */
export function activePath(state: SearchState): string | null {
  if (!state.open || state.matches.length === 0) return null;
  return state.matches[state.activeIndex] ?? null;
}

/** A world position the camera has to show. */
export interface FramePoint {
  readonly x: number;
  readonly y: number;
}

/**
 * The camera target that puts every match on screen.
 *
 * One match is approached at {@link SEARCH_FOCUS_HALF_HEIGHT}; several are fit
 * to their bounding box. The fit has to account for the ASPECT -- the visible
 * world is `halfHeight * aspect` wide -- because fitting on height alone leaves
 * a horizontally spread match set running off both sides of the screen.
 *
 * The result is clamped at both ends: matches landing microns apart (a directory
 * and its only file) must not dive into the bloom, and matches flung apart by a
 * force layout that has not settled must not push the camera past the limit it
 * is allowed to reach.
 */
export function frameMatches(points: readonly FramePoint[], aspect: number): ViewTarget | null {
  if (points.length === 0) return null;

  // A zero-height canvas on the first layout pass makes the aspect 0, Infinity
  // or NaN; any of them would hand the camera a NaN half-height.
  const safeAspect = Number.isFinite(aspect) && aspect > 0 ? aspect : FALLBACK_ASPECT;

  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const point of points) {
    if (point.x < minX) minX = point.x;
    if (point.x > maxX) maxX = point.x;
    if (point.y < minY) minY = point.y;
    if (point.y > maxY) maxY = point.y;
  }

  const needed = Math.max(
    (maxY - minY) / 2 / FRAME_FILL,
    (maxX - minX) / 2 / safeAspect / FRAME_FILL,
    SEARCH_FOCUS_HALF_HEIGHT,
  );

  return {
    centerX: (minX + maxX) / 2,
    centerY: (minY + maxY) / 2,
    halfHeight: Math.min(MAX_HALF_HEIGHT, Math.max(MIN_HALF_HEIGHT, needed)),
  };
}
