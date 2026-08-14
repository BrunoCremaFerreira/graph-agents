/**
 * Contract tests (RED) for hit-testing a click against the graph's file nodes.
 *
 * The defect: the graph shows what changed but never what the change WAS. A file
 * lights up, and the only way to see the diff is to leave the page. Clicking a
 * file is going to open a viewer -- so the first decision the feature makes is
 * "which node, if any, did that click land on?".
 *
 * That decision lives in a pure module, like `view.ts` and `labels.ts`, because
 * `renderer.ts` needs a GL context and cannot be unit-tested: the renderer's job
 * stops at unprojecting the pointer into WORLD coordinates, and everything after
 * that is arithmetic this file pins down. Distances here are therefore in world
 * units, never in pixels -- the camera spans halfHeight 2..4000, so a radius in
 * pixels would select nothing when zoomed out and half the tree when zoomed in.
 *
 * Three properties carry the weight:
 *
 *  - **Nearest wins, and only within the radius.** A click on empty space must
 *    open nothing; picking "whatever was closest" turns every stray click on the
 *    background into a modal over a file the user never aimed at.
 *  - **An exact tie is broken by PATH.** The force layout regularly parks a file
 *    on top of another, and the candidate list is built in arrival order, which
 *    changes on every event. Without a deterministic tie-break the same click
 *    opens different files on different frames.
 *  - **A degenerate radius selects nothing.** The renderer derives it from the
 *    viewport, and a zero-height canvas on the first layout pass yields 0,
 *    Infinity or NaN; each of those must mean "no hit", not "everything hits".
 *
 * Expected to FAIL until src/pick.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { pickFile, isClickGesture } from "../src/pick";

/** A node of the graph, as far as hit-testing is concerned. */
interface Candidate {
  path: string;
  x: number;
  y: number;
}

/** Three files spread far enough apart that only one can ever be near. */
const SPREAD: Candidate[] = [
  { path: "web/src/renderer.ts", x: 0, y: 0 },
  { path: "daemon/server.py", x: 100, y: 0 },
  { path: "README.md", x: 0, y: 100 },
];

describe("pickFile: what a click lands on", () => {
  it("returns the file under the click", () => {
    expect(pickFile(SPREAD, { x: 100, y: 0 }, 10)).toBe("daemon/server.py");
  });

  it("returns the nearest file when several are inside the radius", () => {
    // Two files one world unit apart, the click sitting closer to the second.
    const crowded: Candidate[] = [
      { path: "a.ts", x: 0, y: 0 },
      { path: "b.ts", x: 1, y: 0 },
    ];

    expect(pickFile(crowded, { x: 0.9, y: 0 }, 5)).toBe("b.ts");
  });

  it("measures the distance in both axes, not along one of them", () => {
    // A node offset diagonally is 5 away from the click (3-4-5), so a radius of
    // 4 must miss it -- an implementation comparing dx and dy separately, or
    // forgetting to square, would report a hit.
    const diagonal: Candidate[] = [{ path: "a.ts", x: 3, y: 4 }];

    expect(pickFile(diagonal, { x: 0, y: 0 }, 4)).toBeNull();
  });

  it("returns null when the click falls outside every radius, so empty space opens nothing", () => {
    expect(pickFile(SPREAD, { x: 50, y: 50 }, 10)).toBeNull();
  });

  it("counts a click exactly on the radius as a hit, since the boundary is where a deliberate click lands", () => {
    const one: Candidate[] = [{ path: "a.ts", x: 10, y: 0 }];

    expect(pickFile(one, { x: 0, y: 0 }, 10)).toBe("a.ts");
  });

  it("returns null for an empty candidate list, which is the graph before the seed arrives", () => {
    expect(pickFile([], { x: 0, y: 0 }, 10)).toBeNull();
  });
});

describe("pickFile: ties are broken by path, never by arrival order", () => {
  it("picks the lexicographically first path when two nodes sit at the same spot", () => {
    // The force layout parks nodes on top of each other all the time; two files
    // at one position must not open different viewers on different clicks.
    const stacked: Candidate[] = [
      { path: "web/src/zoo.ts", x: 5, y: 5 },
      { path: "web/src/apple.ts", x: 5, y: 5 },
    ];

    expect(pickFile(stacked, { x: 5, y: 5 }, 10)).toBe("web/src/apple.ts");
  });

  it("gives the same answer when the same tied nodes arrive in the opposite order", () => {
    // The candidate list is rebuilt from a live map, so its order changes on
    // every event. The pick must not.
    const stacked: Candidate[] = [
      { path: "web/src/apple.ts", x: 5, y: 5 },
      { path: "web/src/zoo.ts", x: 5, y: 5 },
    ];

    expect(pickFile(stacked, { x: 5, y: 5 }, 10)).toBe("web/src/apple.ts");
  });
});

describe("pickFile: a degenerate radius selects nothing", () => {
  it("returns null for a radius of zero, even with a node exactly under the click", () => {
    expect(pickFile([{ path: "a.ts", x: 0, y: 0 }], { x: 0, y: 0 }, 0)).toBeNull();
  });

  it("returns null for a negative radius", () => {
    expect(pickFile(SPREAD, { x: 0, y: 0 }, -10)).toBeNull();
  });

  it.each([
    ["NaN", NaN],
    ["Infinity", Infinity],
  ])("returns null for a non-finite radius (%s), which is what a zero-height viewport produces", (
    _label,
    radius,
  ) => {
    expect(pickFile(SPREAD, { x: 0, y: 0 }, radius as number)).toBeNull();
  });

  it("returns null when the click position is not a real point", () => {
    // The renderer unprojects the pointer through the camera; a canvas that has
    // not been measured yet hands back NaN, and a NaN click must not open the
    // first file in the list.
    expect(pickFile(SPREAD, { x: NaN, y: NaN }, 10)).toBeNull();
  });

  it("never picks a node whose position is not a real point", () => {
    // A node inserted before the layout has placed it carries NaN coordinates
    // for a frame or two; it is nowhere, so it is never under the pointer.
    const unplaced: Candidate[] = [{ path: "ghost.ts", x: NaN, y: NaN }];

    expect(pickFile(unplaced, { x: 0, y: 0 }, 1000)).toBeNull();
  });
});

/**
 * Contract tests (RED) for the OTHER half of the same feature: does a pointer
 * gesture even count as a click?
 *
 * The defect: `pickFile` answers "which node", but the decision of "was that a
 * click at all" is still buried in `renderer.ts#handleClick`, where no test can
 * reach it -- the class needs a GL context. That is the last decision left in the
 * renderer, and by the same rule as `view.ts` and `labels.ts` it belongs in a
 * pure module. It lives here because this file is already where a gesture turns
 * into a file.
 *
 * One canvas serves three gestures, so the thresholds are the only thing telling
 * them apart:
 *
 *  - **`detail > 1` is never a click.** The double-click already has an owner --
 *    the camera's auto-fit -- and opening a file in the middle of it runs a
 *    second command nobody asked for. `detail` is the browser's own click count,
 *    so deferring to it agrees with `dblclick` exactly and needs no timer.
 *  - **Travel is measured RADIALLY, not per axis**, and the limit is INCLUSIVE
 *    at 4 CSS pixels: a drag is a pan, but nobody holds a mouse perfectly still.
 *    A diagonal gesture where neither axis alone passes the limit still fails if
 *    the distance does -- an implementation comparing dx and dy separately would
 *    open a file at the end of a pan.
 *  - **Duration is INCLUSIVE at 400 ms**: longer is a press-and-hold, not a tap.
 *  - **Non-finite numbers are never a click.** The first layout pass, and a
 *    clock read before the canvas is measured, produce NaN and Infinity; each
 *    must mean "not a click" rather than sliding through a `>` comparison.
 *
 * Expected to FAIL until src/pick.ts exports isClickGesture. One failure reason
 * per test.
 */

/** A pointer gesture: press to release, in CSS pixels and milliseconds. */
interface Gesture {
  detail: number;
  dx: number;
  dy: number;
  elapsedMs: number;
}

/** A still, quick, single press -- the click every other case departs from. */
const TAP: Gesture = { detail: 1, dx: 0, dy: 0, elapsedMs: 50 };

describe("isClickGesture: a still, quick, single press opens a file", () => {
  it("counts a motionless quick tap as a click", () => {
    expect(isClickGesture(TAP)).toBe(true);
  });

  it("counts a small wobble under the slop as a click, since nobody holds a mouse perfectly still", () => {
    expect(isClickGesture({ ...TAP, dx: 1, dy: -1 })).toBe(true);
  });
});

describe("isClickGesture: the double-click belongs to the camera", () => {
  it("rejects the second press of a double-click, whose gesture already means auto-fit", () => {
    expect(isClickGesture({ ...TAP, detail: 2 })).toBe(false);
  });

  it("rejects a triple click too, so a fast repeated tap never opens a viewer behind the auto-fit", () => {
    expect(isClickGesture({ ...TAP, detail: 3 })).toBe(false);
  });
});

describe("isClickGesture: travel beyond the slop is a pan, measured radially", () => {
  it("rejects a gesture that travels past the slop on one axis alone", () => {
    expect(isClickGesture({ ...TAP, dx: 40, dy: 0 })).toBe(false);
  });

  it("rejects a gesture that travels past the slop on the other axis alone", () => {
    expect(isClickGesture({ ...TAP, dx: 0, dy: -40 })).toBe(false);
  });

  it("rejects a diagonal gesture whose DISTANCE passes the slop even though neither axis does", () => {
    // 3-4-5: the distance is 5, past the slop of 4, while dx and dy are each
    // under it. The rule fixed here is the radial one -- an implementation
    // comparing the axes separately would call this a click and open a file at
    // the end of a short diagonal pan.
    expect(isClickGesture({ ...TAP, dx: 3, dy: 4 })).toBe(false);
  });

  it("counts a gesture exactly on the 4px slop as a click, since the boundary is inclusive", () => {
    expect(isClickGesture({ ...TAP, dx: 4, dy: 0 })).toBe(true);
  });

  it("rejects a gesture one hair past the 4px slop, which is what makes the boundary a boundary", () => {
    expect(isClickGesture({ ...TAP, dx: 4.001, dy: 0 })).toBe(false);
  });

  it("measures travel by magnitude, so a backwards drag is no more of a click than a forwards one", () => {
    expect(isClickGesture({ ...TAP, dx: -10, dy: 0 })).toBe(false);
  });
});

describe("isClickGesture: a long press is not a click", () => {
  it("rejects a gesture held past 400ms, which is a press-and-hold rather than a tap", () => {
    expect(isClickGesture({ ...TAP, elapsedMs: 1200 })).toBe(false);
  });

  it("counts a gesture lasting exactly 400ms as a click, since the boundary is inclusive", () => {
    expect(isClickGesture({ ...TAP, elapsedMs: 400 })).toBe(true);
  });

  it("rejects a gesture one millisecond past 400ms", () => {
    expect(isClickGesture({ ...TAP, elapsedMs: 401 })).toBe(false);
  });
});

describe("isClickGesture: non-finite numbers are never a click", () => {
  it.each([
    ["dx NaN", { dx: NaN }],
    ["dy NaN", { dy: NaN }],
    ["dx Infinity", { dx: Infinity }],
    ["dy -Infinity", { dy: -Infinity }],
    ["elapsedMs NaN", { elapsedMs: NaN }],
    ["elapsedMs Infinity", { elapsedMs: Infinity }],
    ["detail NaN", { detail: NaN }],
  ])("rejects a gesture with %s, which is what the first layout pass produces", (
    _label,
    patch,
  ) => {
    expect(isClickGesture({ ...TAP, ...(patch as Partial<Gesture>) })).toBe(false);
  });
});
