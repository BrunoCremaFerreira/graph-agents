/**
 * Contract tests (RED) for label placement and selection.
 *
 * Two defects motivate this module. Directory names drifted away from their
 * nodes because the renderer only repositioned label sprites when the tree's
 * topology changed, while the force layout keeps moving nodes every frame; and
 * file names were never drawn at all. Both fixes need decisions -- where a label
 * sits, how big it is, and which files deserve one -- that must be testable
 * without a WebGL context, so they live here rather than in renderer.ts.
 *
 * The property that matters for readability is that a label's size is constant
 * in PIXELS, not in world units: the camera spans halfHeight 2..4000, so a
 * world-sized label is either sub-pixel or screen-filling. Expected to FAIL
 * until src/labels.ts exists.
 */

import { describe, it, expect } from "vitest";
import {
  labelWorldHeight,
  labelOffset,
  fileLabelOpacity,
  selectFileLabels,
  FILE_LABEL_ZOOM_THRESHOLD,
  LABEL_PIXEL_HEIGHT,
  MAX_FILE_LABELS,
  type LabelCandidate,
} from "../src/labels";

const VIEWPORT_H = 1000;

/** On-screen height, in pixels, of something `worldHeight` tall. */
function pixelsOnScreen(worldHeight: number, halfHeight: number): number {
  return (worldHeight / (2 * halfHeight)) * VIEWPORT_H;
}

function candidate(path: string, highlight: number, x = 0, y = 0): LabelCandidate {
  return { path, highlight, x, y };
}

/** A viewport centred on the origin; `halfHeight` decides the zoom. */
function viewport(halfHeight: number) {
  return { centerX: 0, centerY: 0, halfHeight, aspect: 16 / 9 };
}

describe("labelWorldHeight", () => {
  it("renders a label at the requested pixel height", () => {
    const world = labelWorldHeight(100, VIEWPORT_H, 13);

    expect(pixelsOnScreen(world, 100)).toBeCloseTo(13);
  });

  it("keeps the on-screen size constant across the whole zoom range", () => {
    const near = labelWorldHeight(2, VIEWPORT_H, LABEL_PIXEL_HEIGHT);
    const far = labelWorldHeight(4000, VIEWPORT_H, LABEL_PIXEL_HEIGHT);

    expect(pixelsOnScreen(near, 2)).toBeCloseTo(pixelsOnScreen(far, 4000));
  });

  it("grows the world height as the camera pulls back", () => {
    // A world-fixed label would return the same number here -- that is the bug.
    expect(labelWorldHeight(200, VIEWPORT_H)).toBeCloseTo(2 * labelWorldHeight(100, VIEWPORT_H));
  });

  it("survives a zero-height viewport during the first layout pass", () => {
    expect(Number.isFinite(labelWorldHeight(100, 0))).toBe(true);
  });
});

describe("labelOffset", () => {
  it("scales with the text so the label hugs its node at any zoom", () => {
    expect(labelOffset(10)).toBeCloseTo(2 * labelOffset(5));
  });

  it("clears the node instead of sitting on top of it", () => {
    expect(labelOffset(10)).toBeGreaterThan(0);
  });
});

describe("fileLabelOpacity", () => {
  const FAR = FILE_LABEL_ZOOM_THRESHOLD * 4;
  const NEAR = FILE_LABEL_ZOOM_THRESHOLD * 0.25;

  it("hides an idle file while the whole tree is framed", () => {
    expect(fileLabelOpacity(0, FAR)).toBe(0);
  });

  it("shows a file that was just touched, however far out the camera is", () => {
    expect(fileLabelOpacity(1, FAR)).toBeGreaterThan(0.5);
  });

  it("fades the name out with the highlight", () => {
    expect(fileLabelOpacity(0.2, FAR)).toBeLessThan(fileLabelOpacity(0.8, FAR));
  });

  it("reveals idle files once the camera is close enough", () => {
    expect(fileLabelOpacity(0, NEAR)).toBeGreaterThan(0);
  });

  it("ramps the zoom reveal in smoothly rather than popping", () => {
    const atThreshold = fileLabelOpacity(0, FILE_LABEL_ZOOM_THRESHOLD);
    const justInside = fileLabelOpacity(0, FILE_LABEL_ZOOM_THRESHOLD * 0.95);

    expect(atThreshold).toBeCloseTo(0);
    expect(justInside).toBeGreaterThan(0);
    expect(justInside).toBeLessThan(0.2);
  });

  it("never exceeds full opacity", () => {
    expect(fileLabelOpacity(1, NEAR)).toBeLessThanOrEqual(1);
  });
});

describe("selectFileLabels", () => {
  it("names nothing when a big idle tree is framed whole", () => {
    const cold = Array.from({ length: 404 }, (_, i) => candidate(`src/f${i}.ts`, 0));

    expect(selectFileLabels(cold, viewport(FILE_LABEL_ZOOM_THRESHOLD * 5))).toEqual([]);
  });

  it("names a touched file even with the camera far out", () => {
    const nodes = [candidate("src/cold.ts", 0), candidate("src/hot.ts", 1)];

    const chosen = selectFileLabels(nodes, viewport(FILE_LABEL_ZOOM_THRESHOLD * 5));

    expect(chosen.map((c) => c.path)).toEqual(["src/hot.ts"]);
  });

  it("names idle files once zoomed in past the threshold", () => {
    const nodes = [candidate("a.ts", 0), candidate("b.ts", 0)];

    const chosen = selectFileLabels(nodes, viewport(FILE_LABEL_ZOOM_THRESHOLD * 0.5));

    expect(chosen).toHaveLength(2);
  });

  it("skips files outside the visible rectangle", () => {
    const view = viewport(50);
    const offscreen = candidate("far.ts", 1, 0, 5000);

    expect(selectFileLabels([offscreen], view)).toEqual([]);
  });

  it("keeps a file that is off the top but inside the wider horizontal span", () => {
    // halfWidth = halfHeight * aspect, so x has more room than y.
    const view = viewport(50);
    const wide = candidate("wide.ts", 1, 70, 0);

    expect(selectFileLabels([wide], view)).toHaveLength(1);
  });

  it("never returns more labels than the pool can draw", () => {
    const many = Array.from({ length: 500 }, (_, i) => candidate(`f${i}.ts`, 0));

    const chosen = selectFileLabels(many, viewport(FILE_LABEL_ZOOM_THRESHOLD * 0.5));

    expect(chosen.length).toBeLessThanOrEqual(MAX_FILE_LABELS);
  });

  it("gives the hottest files the slots when it has to choose", () => {
    const many = Array.from({ length: 500 }, (_, i) => candidate(`f${i}.ts`, 0));
    many.push(candidate("touched.ts", 1));

    const chosen = selectFileLabels(many, viewport(FILE_LABEL_ZOOM_THRESHOLD * 0.5));

    expect(chosen[0].path).toBe("touched.ts");
  });

  it("orders equally cold files deterministically so labels do not flicker", () => {
    const nodes = [candidate("b.ts", 0), candidate("a.ts", 0), candidate("c.ts", 0)];
    const view = viewport(FILE_LABEL_ZOOM_THRESHOLD * 0.5);

    const first = selectFileLabels(nodes, view).map((c) => c.path);
    const second = selectFileLabels([...nodes].reverse(), view).map((c) => c.path);

    expect(first).toEqual(second);
  });

  it("honours an explicit cap so a caller can shrink the pool", () => {
    const nodes = Array.from({ length: 10 }, (_, i) => candidate(`f${i}.ts`, 1));

    expect(selectFileLabels(nodes, viewport(20), 3)).toHaveLength(3);
  });
});
