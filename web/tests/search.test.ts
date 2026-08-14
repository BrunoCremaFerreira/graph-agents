/**
 * Contract tests (RED) for graph search: matching paths and the search box's
 * state machine.
 *
 * The defect is navigational. The seed walk publishes the whole observed
 * project, so the graph opens with hundreds of nodes; past
 * FILE_LABEL_ZOOM_THRESHOLD nothing is named at all, and finding one known file
 * means dragging the camera around reading labels until it turns up. There is no
 * way to ask "where is renderer.ts?".
 *
 * Everything the feature decides -- which nodes a typed query means, which one
 * F3 lands on next, and when the box closes -- is pure data. It lives here for
 * the same reason as {@link ../src/view} and {@link ../src/labels}: renderer.ts
 * needs a GL context and cannot be unit-tested, and a keyboard handler wired
 * straight into the DOM would put this logic somewhere no test can reach.
 *
 * Two matching rules carry the weight. A query with no `/` matches only the LAST
 * SEGMENT, because typing `src` while looking for a file should not drag in
 * every file under `web/src`; a query containing `/` matches the whole relative
 * path, because that is the only way to ask for a subtree. And the result list
 * is ORDERED BY PATH, not by arrival: F3 walks that list frame after frame while
 * the force layout keeps moving the nodes, so an order that depends on anything
 * but the paths themselves would make "next match" jump at random.
 *
 * Expected to FAIL until src/search.ts exists.
 *
 * A second defect motivates `refreshMatches`. This graph is LIVE: nodes appear
 * and disappear on every WebSocket event, so a result list computed when the
 * query was typed goes stale within seconds -- a file created after the search
 * opened is never found, and one deleted stays highlighted at a position the
 * camera can still be sent to. The only recomputation available today is
 * `setQuery`, which by contract restarts the walk at index 0 and returns to
 * `frame: "all"`; driving it from the event stream would yank anyone stepping
 * through matches with F3 back to the overview a second later. Recomputing is
 * not typing, so it preserves the walk -- the ACTIVE PATH, not the active index,
 * because a new match sorting before it shifts every index -- and it does not
 * close the box on an empty query.
 */

import { describe, it, expect } from "vitest";
import {
  matchPaths,
  createSearchState,
  openSearch,
  setQuery,
  nextMatch,
  refreshMatches,
  closeSearch,
  activePath,
  type SearchNode,
} from "../src/search";

/** A slice of this repository's own tree: files and directories, mixed. */
const NODES: readonly SearchNode[] = [
  { path: "README.md", kind: "file" },
  { path: "daemon", kind: "dir" },
  { path: "daemon/server.py", kind: "file" },
  { path: "web", kind: "dir" },
  { path: "web/src", kind: "dir" },
  { path: "web/src/renderer.ts", kind: "file" },
  { path: "web/src/view.ts", kind: "file" },
  { path: "web/tests", kind: "dir" },
  { path: "web/tests/view.test.ts", kind: "file" },
];

describe("matchPaths", () => {
  it("matches nothing on an empty query", () => {
    expect(matchPaths(NODES, "")).toEqual([]);
  });

  it("matches nothing on a query of only whitespace", () => {
    // A held-down space bar must not select the entire project.
    expect(matchPaths(NODES, "   \t ")).toEqual([]);
  });

  it("ignores case, so a name half-remembered still finds its file", () => {
    expect(matchPaths(NODES, "RENDER")).toEqual(["web/src/renderer.ts"]);
    expect(matchPaths(NODES, "readme")).toEqual(["README.md"]);
  });

  it("matches a substring of the file name when the query has no slash", () => {
    expect(matchPaths(NODES, "render")).toEqual(["web/src/renderer.ts"]);
  });

  it("does not let a directory name in the middle of a path match its files", () => {
    // The defect this rule prevents: typing `src` to look for a file should not
    // return every file that merely lives under web/src.
    expect(matchPaths(NODES, "src")).toEqual(["web/src"]);
  });

  it("matches the whole relative path once the query contains a slash", () => {
    expect(matchPaths(NODES, "web/src")).toEqual([
      "web/src",
      "web/src/renderer.ts",
      "web/src/view.ts",
    ]);
  });

  it("matches directories as well as files", () => {
    expect(matchPaths(NODES, "daemon")).toEqual(["daemon", "daemon/server.py"]);
  });

  it("returns the matches ordered by path", () => {
    expect(matchPaths(NODES, "view")).toEqual(["web/src/view.ts", "web/tests/view.test.ts"]);
  });

  it("returns the same order however the nodes arrived", () => {
    // F3 walks this list frame after frame; an order that follows insertion
    // would make "next match" jump somewhere else on every rebuild.
    const forwards = matchPaths(NODES, "view");
    const backwards = matchPaths([...NODES].reverse(), "view");

    expect(backwards).toEqual(forwards);
  });

  it("matches nothing when no path contains the query", () => {
    expect(matchPaths(NODES, "gource.cpp")).toEqual([]);
  });
});

describe("createSearchState", () => {
  it("starts closed, with no query and no matches", () => {
    const state = createSearchState();

    expect(state.open).toBe(false);
    expect(state.query).toBe("");
    expect(state.matches).toEqual([]);
  });
});

describe("openSearch", () => {
  it("opens the box with nothing typed and nothing selected yet", () => {
    const state = openSearch(createSearchState());

    expect(state.open).toBe(true);
    expect(state.query).toBe("");
    expect(state.matches).toEqual([]);
    expect(state.activeIndex).toBe(0);
  });
});

describe("setQuery", () => {
  const opened = openSearch(createSearchState());

  it("records what was typed", () => {
    expect(setQuery(opened, "view", NODES).query).toBe("view");
  });

  it("recomputes the matches from the typed query", () => {
    expect(setQuery(opened, "view", NODES).matches).toEqual([
      "web/src/view.ts",
      "web/tests/view.test.ts",
    ]);
  });

  it("frames all the matches, because a new query is a new question", () => {
    expect(setQuery(opened, "view", NODES).frame).toBe("all");
  });

  it("restarts the walk at the first match when the query changes", () => {
    const stepped = nextMatch(setQuery(opened, "view", NODES));

    expect(setQuery(stepped, "vie", NODES).activeIndex).toBe(0);
  });

  it("goes back to framing everything when the query changes mid-walk", () => {
    const stepped = nextMatch(setQuery(opened, "view", NODES));

    expect(setQuery(stepped, "vie", NODES).frame).toBe("all");
  });

  it("keeps the box open when a query matches nothing", () => {
    // The user is still typing; closing on the first unmatched prefix would
    // dismiss the box halfway through every longer name.
    const state = setQuery(opened, "gource.cpp", NODES);

    expect(state.open).toBe(true);
    expect(state.matches).toEqual([]);
  });

  it("closes and clears itself when the typed text is deleted", () => {
    const typed = setQuery(opened, "view", NODES);

    const cleared = setQuery(typed, "", NODES);

    expect(cleared.open).toBe(false);
    expect(cleared.query).toBe("");
    expect(cleared.matches).toEqual([]);
  });

  it("leaves a freshly opened box open when the empty query is set again", () => {
    // ctrl+F opens with an empty query, and the input fires its first change
    // before anything is typed. Treating that as "the text was deleted" would
    // shut the box the instant it appeared.
    expect(setQuery(opened, "", NODES).open).toBe(true);
  });
});

describe("nextMatch", () => {
  const found = setQuery(openSearch(createSearchState()), "view", NODES);

  it("advances to the following match", () => {
    expect(nextMatch(found).activeIndex).toBe(1);
  });

  it("wraps from the last match back to the first", () => {
    expect(nextMatch(nextMatch(found)).activeIndex).toBe(0);
  });

  it("switches the camera from framing all matches to approaching one", () => {
    expect(nextMatch(found).frame).toBe("active");
  });

  it("keeps the matches it is walking", () => {
    expect(nextMatch(found).matches).toEqual(found.matches);
  });

  it("does nothing when there is nothing to step through", () => {
    const empty = setQuery(openSearch(createSearchState()), "gource.cpp", NODES);

    expect(nextMatch(empty)).toEqual(empty);
  });
});

describe("refreshMatches", () => {
  const found = setQuery(openSearch(createSearchState()), "view", NODES);

  /** A file that arrives while the search is open, sorting BEFORE both matches. */
  const withNewFile: readonly SearchNode[] = [...NODES, { path: "daemon/view.py", kind: "file" }];

  /** The same tree after `web/src/view.ts` is deleted. */
  const withoutFirst: readonly SearchNode[] = NODES.filter(
    (node) => node.path !== "web/src/view.ts",
  );

  it("picks up a file that appeared after the query was typed", () => {
    expect(refreshMatches(found, withNewFile).matches).toEqual([
      "daemon/view.py",
      "web/src/view.ts",
      "web/tests/view.test.ts",
    ]);
  });

  it("drops a file that was deleted while the search was open", () => {
    expect(refreshMatches(found, withoutFirst).matches).toEqual(["web/tests/view.test.ts"]);
  });

  it("keeps the query it recomputed from", () => {
    expect(refreshMatches(found, withNewFile).query).toBe("view");
  });

  it("stays on the same node when a new match shifts its index", () => {
    // The walk is on `web/src/view.ts` at index 0; `daemon/view.py` sorts first,
    // so holding the INDEX would silently move the camera to another file.
    expect(activePath(refreshMatches(found, withNewFile))).toBe("web/src/view.ts");
    expect(refreshMatches(found, withNewFile).activeIndex).toBe(1);
  });

  it("stays on the same node when a match before it disappears", () => {
    const walked = nextMatch(found); // on web/tests/view.test.ts, index 1

    expect(activePath(refreshMatches(walked, withoutFirst))).toBe("web/tests/view.test.ts");
  });

  it("keeps approaching the active match instead of framing them all again", () => {
    expect(refreshMatches(nextMatch(found), withNewFile).frame).toBe("active");
  });

  it("keeps framing them all when the walk had not started", () => {
    expect(refreshMatches(found, withNewFile).frame).toBe("all");
  });

  it("falls back to a real match when the active node is deleted", () => {
    const walked = nextMatch(found); // on web/tests/view.test.ts, index 1
    const gone = NODES.filter((node) => node.path !== "web/tests/view.test.ts");

    const refreshed = refreshMatches(walked, gone);

    // The index must never point past the end: the camera is sent to
    // `matches[activeIndex]` every frame.
    expect(refreshed.activeIndex).toBeLessThan(refreshed.matches.length);
    expect(activePath(refreshed)).toBe("web/src/view.ts");
  });

  it("highlights nobody when every match disappeared", () => {
    const gone = NODES.filter((node) => !node.path.includes("view"));

    const refreshed = refreshMatches(found, gone);

    expect(refreshed.matches).toEqual([]);
    expect(activePath(refreshed)).toBeNull();
    expect(refreshed.open).toBe(true);
  });

  it("keeps the box open on an empty query, because recomputing is not typing", () => {
    // setQuery(state, "") means the user deleted the text and closes the box; an
    // event arriving while the box sits empty and freshly opened must not.
    const opened = openSearch(createSearchState());

    const refreshed = refreshMatches(opened, NODES);

    expect(refreshed.open).toBe(true);
    expect(refreshed.query).toBe("");
    expect(refreshed.matches).toEqual([]);
  });

  it("does nothing at all while the box is closed", () => {
    const closed = createSearchState();

    expect(refreshMatches(closed, NODES)).toEqual(closed);
  });
});

describe("closeSearch", () => {
  it("returns to the initial state, leaving nothing highlighted", () => {
    const found = setQuery(openSearch(createSearchState()), "view", NODES);

    expect(closeSearch(found)).toEqual(createSearchState());
  });
});

describe("activePath", () => {
  const found = setQuery(openSearch(createSearchState()), "view", NODES);

  it("names the match the camera should be on", () => {
    expect(activePath(found)).toBe("web/src/view.ts");
  });

  it("follows the walk to the next match", () => {
    expect(activePath(nextMatch(found))).toBe("web/tests/view.test.ts");
  });

  it("names nobody when the query matched nothing", () => {
    const empty = setQuery(openSearch(createSearchState()), "gource.cpp", NODES);

    expect(activePath(empty)).toBeNull();
  });

  it("names nobody in a closed box", () => {
    expect(activePath(createSearchState())).toBeNull();
  });
});
