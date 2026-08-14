/**
 * Contract tests (RED) for the camera target a search result asks for.
 *
 * Finding a node is only half the feature: the camera has to go there. One match
 * means approaching it close enough that its name is drawn; several mean pulling
 * back until all of them are on screen at once, which depends on the viewport's
 * ASPECT -- the visible world is `halfHeight * aspect` wide, so a row of matches
 * spread horizontally needs a much wider frame than its height alone suggests.
 * Framing on height only is exactly how a wide match set ends up half off screen.
 *
 * This is arithmetic over a bounding box, so it belongs beside {@link ../src/view}
 * rather than inside renderer.ts, which needs a GL context and cannot be tested.
 * The tests below assert the PROPERTY the renderer depends on -- every match
 * lands inside the visible rectangle, with room to spare -- not the formula that
 * produces it, so the fit can be retuned without rewriting the specification.
 *
 * Two floors guard the degenerate cases the graph really produces: matches
 * sitting almost on top of each other (a directory and its single file) must not
 * zoom the camera into the bloom, and matches flung apart by a force layout that
 * has not settled must not push the camera past MAX_HALF_HEIGHT.
 *
 * Expected to FAIL until src/search.ts exports frameMatches.
 */

import { describe, it, expect } from "vitest";
import { frameMatches, SEARCH_FOCUS_HALF_HEIGHT } from "../src/search";
import { MIN_HALF_HEIGHT, MAX_HALF_HEIGHT, type ViewTarget } from "../src/view";
import { FILE_LABEL_ZOOM_THRESHOLD } from "../src/labels";

const ASPECT = 16 / 9;

interface Point {
  x: number;
  y: number;
}

/** Whether every point falls inside the rectangle `target` puts on screen. */
function allVisible(points: readonly Point[], target: ViewTarget, aspect: number): boolean {
  const halfH = target.halfHeight;
  const halfW = target.halfHeight * aspect;
  return points.every(
    (p) => Math.abs(p.x - target.centerX) <= halfW && Math.abs(p.y - target.centerY) <= halfH,
  );
}

/** The largest fraction of the visible half-extent any point reaches. */
function fillFraction(points: readonly Point[], target: ViewTarget, aspect: number): number {
  const halfW = target.halfHeight * aspect;
  return Math.max(
    ...points.map((p) =>
      Math.max(
        Math.abs(p.x - target.centerX) / halfW,
        Math.abs(p.y - target.centerY) / target.halfHeight,
      ),
    ),
  );
}

/** Matches spread mostly vertically: the height is what binds the frame. */
const TALL: Point[] = [
  { x: -8, y: -120 },
  { x: 12, y: 0 },
  { x: 4, y: 140 },
];

/** Matches spread mostly horizontally: only the aspect can fit these. */
const WIDE: Point[] = [
  { x: -400, y: -2 },
  { x: 0, y: 1 },
  { x: 400, y: 2 },
];

describe("frameMatches", () => {
  it("has no camera target when nothing matched", () => {
    expect(frameMatches([], ASPECT)).toBeNull();
  });

  it("centres the camera exactly on a lone match", () => {
    const target = frameMatches([{ x: 37, y: -11 }], ASPECT);

    expect(target?.centerX).toBe(37);
    expect(target?.centerY).toBe(-11);
  });

  it("approaches a lone match at the focus zoom", () => {
    expect(frameMatches([{ x: 37, y: -11 }], ASPECT)?.halfHeight).toBe(SEARCH_FOCUS_HALF_HEIGHT);
  });

  it("focuses close enough for the found file to be named", () => {
    // Past FILE_LABEL_ZOOM_THRESHOLD idle files show no name at all, so a match
    // framed from further out is an unlabelled dot the user cannot identify.
    expect(SEARCH_FOCUS_HALF_HEIGHT).toBeLessThan(FILE_LABEL_ZOOM_THRESHOLD);
  });

  it("stops short of the zoom where the bloom swallows everything", () => {
    expect(SEARCH_FOCUS_HALF_HEIGHT).toBeGreaterThan(MIN_HALF_HEIGHT);
  });

  it("centres the camera on the middle of the matches' bounding box", () => {
    const target = frameMatches(
      [
        { x: -10, y: -4 },
        { x: 30, y: 16 },
      ],
      ASPECT,
    );

    expect(target?.centerX).toBeCloseTo(10);
    expect(target?.centerY).toBeCloseTo(6);
  });

  it("frames every match that is spread out vertically", () => {
    const target = frameMatches(TALL, ASPECT)!;

    expect(allVisible(TALL, target, ASPECT)).toBe(true);
  });

  it("frames every match that is spread out horizontally", () => {
    // The defect this catches: fitting on height alone leaves a wide row of
    // matches running off both sides of the screen.
    const target = frameMatches(WIDE, ASPECT)!;

    expect(allVisible(WIDE, target, ASPECT)).toBe(true);
  });

  it("frames every match on a tall narrow viewport too", () => {
    const narrow = 0.5;
    const target = frameMatches(WIDE, narrow)!;

    expect(allVisible(WIDE, target, narrow)).toBe(true);
  });

  it("leaves margin around the matches instead of touching the edges", () => {
    for (const points of [TALL, WIDE]) {
      const target = frameMatches(points, ASPECT)!;

      expect(fillFraction(points, target, ASPECT)).toBeLessThanOrEqual(0.9);
    }
  });

  it("does not dive into the bloom when the matches sit on top of each other", () => {
    // A directory and its only file land microns apart; fitting their bounding
    // box would put the camera inside them.
    const clustered: Point[] = [
      { x: 100, y: 50 },
      { x: 100.01, y: 50.02 },
    ];

    expect(frameMatches(clustered, ASPECT)!.halfHeight).toBeGreaterThanOrEqual(
      SEARCH_FOCUS_HALF_HEIGHT,
    );
  });

  it("never pulls back further than the camera is allowed to go", () => {
    const scattered: Point[] = [
      { x: -1e9, y: -1e9 },
      { x: 1e9, y: 1e9 },
    ];

    expect(frameMatches(scattered, ASPECT)!.halfHeight).toBeLessThanOrEqual(MAX_HALF_HEIGHT);
  });

  it("returns a usable target on the first layout pass, before the canvas measures", () => {
    // A zero-height canvas makes the aspect 0, Infinity or NaN depending on how
    // it is derived; any of them must not hand the camera a NaN half-height.
    for (const bad of [0, Infinity, NaN]) {
      const target = frameMatches(WIDE, bad)!;

      expect(Number.isFinite(target.halfHeight)).toBe(true);
      expect(target.halfHeight).toBeGreaterThanOrEqual(MIN_HALF_HEIGHT);
      expect(target.halfHeight).toBeLessThanOrEqual(MAX_HALF_HEIGHT);
    }
  });
});
