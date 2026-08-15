/**
 * Contract tests for the read marker: the ring drawn around a file an agent is
 * READING.
 *
 * Written after the implementation rather than before it -- coverage closing a
 * gap, not a RED specification -- because `readMarker.ts` shipped with no tests
 * of its own.
 *
 * The defect it guards against is a visual one, and it is the reason the module
 * exists separately from `searchMarker.ts` at all. Both markers are rings drawn
 * around a node, both go through the bloom, and the bloom washes hue out of
 * small bright shapes: cyan and violet stop being reliably distinguishable at
 * the size these are actually drawn, on a screenshot, and to a colour-blind eye.
 * So the two must differ in SHAPE -- the search draws one thick ring, the read
 * draws two thin concentric ones -- or "an agent is reading this" and "this is
 * what you searched for" become the same signal. Collapsing them back into one
 * shape is the regression worth catching, so it is pinned by comparing the two
 * painters against each other.
 *
 * The other properties are the ones a ring can silently lose: a stroke that
 * reaches past the box is clipped by the quad and reads as broken, a fill closes
 * the middle where the file dot has to stay visible, and rings that touch read
 * as one smear rather than two.
 *
 * Everything is asserted on the CALLS, never on pixel values: the painting is
 * expressed against the small slice of `CanvasRenderingContext2D` it uses, so it
 * is verified without a DOM, a canvas or a GL context (`renderer.ts` needs all
 * three and cannot be tested at all). Radii and widths are deliberately NOT
 * pinned to their constants -- they are tuning, and a test that fails when
 * someone nudges a ring by a pixel is noise. Only the relationships between them
 * are specified.
 */

import { describe, it, expect } from "vitest";
import { READ_MARKER_SIZE, paintReadRings, type ReadMarkerContext } from "../src/readMarker";
import { paintSearchRing, type SearchMarkerContext } from "../src/searchMarker";

/** The violet the renderer paints reads in; any colour would do here. */
const READ_COLOR = 0xaa66ff;

interface Call {
  op: string;
  args: readonly unknown[];
}

/** Records every 2D-context call instead of rasterizing anything. */
function recordingContext(): { ctx: ReadMarkerContext; calls: Call[] } {
  const calls: Call[] = [];
  const record =
    (op: string) =>
    (...args: unknown[]): void => {
      calls.push({ op, args });
    };
  const ctx = {
    beginPath: record("beginPath"),
    closePath: record("closePath"),
    moveTo: record("moveTo"),
    lineTo: record("lineTo"),
    arc: record("arc"),
    fill: record("fill"),
    stroke: record("stroke"),
    clearRect: record("clearRect"),
    set fillStyle(value: string) {
      calls.push({ op: "fillStyle", args: [value] });
    },
    set strokeStyle(value: string) {
      calls.push({ op: "strokeStyle", args: [value] });
    },
    set lineWidth(value: number) {
      calls.push({ op: "lineWidth", args: [value] });
    },
    set lineCap(value: string) {
      calls.push({ op: "lineCap", args: [value] });
    },
  } as unknown as ReadMarkerContext;
  return { ctx, calls };
}

function opsOf(calls: Call[]): string[] {
  return calls.map((call) => call.op);
}

/** The value of a settable property in force when `index` was recorded. */
function propertyAt(calls: Call[], op: string, index: number): unknown {
  let value: unknown;
  for (let i = 0; i < index; i += 1) {
    if (calls[i].op === op) value = calls[i].args[0];
  }
  return value;
}

/** Every arc, with the stroke width that was in force when it was declared. */
function ringsOf(calls: Call[]): { cx: number; cy: number; r: number; width: number }[] {
  return calls
    .map((call, index) => ({ call, index }))
    .filter(({ call }) => call.op === "arc")
    .map(({ call, index }) => {
      const [cx, cy, r] = call.args as [number, number, number];
      return { cx, cy, r, width: Number(propertyAt(calls, "lineWidth", index) ?? 0) };
    });
}

/** Paint the search marker through the same recorder, for shape comparison. */
function recordSearchRing(color: number): Call[] {
  const { ctx, calls } = recordingContext();
  paintSearchRing(ctx as unknown as SearchMarkerContext, color);
  return calls;
}

describe("paintReadRings: the shape", () => {
  it("clears the whole box first, so a repaint never stacks on what was there", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    expect(calls[0].op).toBe("clearRect");
    expect(calls[0].args).toEqual([0, 0, READ_MARKER_SIZE, READ_MARKER_SIZE]);
  });

  it("draws two rings, which is what tells a read apart from a search hit", () => {
    // The count, not the radii: the radii are tuning, the number of rings is
    // the identity of the marker.
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const ops = opsOf(calls);
    expect(ops.filter((op) => op === "arc")).toHaveLength(2);
    expect(ops.filter((op) => op === "stroke")).toHaveLength(2);
  });

  it("gives the two rings different radii instead of overdrawing one circle twice", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const [outer, inner] = ringsOf(calls);
    expect(outer.r).not.toBe(inner.r);
  });

  it("centres both rings on the same point, so they read as concentric", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const [outer, inner] = ringsOf(calls);
    expect(inner.cx).toBe(outer.cx);
    expect(inner.cy).toBe(outer.cy);
  });

  it("centres them in the box, not off toward a corner", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    for (const ring of ringsOf(calls)) {
      expect(ring.cx).toBeCloseTo(READ_MARKER_SIZE / 2, 5);
      expect(ring.cy).toBeCloseTo(READ_MARKER_SIZE / 2, 5);
    }
  });

  it("leaves a gap between the rings, so they are two circles and not one smear", () => {
    // Strokes are centred on their radius: the rings touch as soon as half of
    // each width closes the distance between them, and at that point the marker
    // is just a thick ring again -- the search marker's shape.
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const rings = ringsOf(calls);
    const outer = rings.reduce((a, b) => (a.r >= b.r ? a : b));
    const inner = rings.reduce((a, b) => (a.r <= b.r ? a : b));

    expect(inner.r + inner.width / 2).toBeLessThan(outer.r - outer.width / 2);
  });

  it("keeps every stroke inside the declared marker box, clipping nothing away", () => {
    // The sprite is mapped 1:1 onto a quad, so a ring drawn to the very edge
    // loses the outer half of its stroke and reads as a broken circle. Half the
    // line width has to fit too, on all four sides.
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const rings = ringsOf(calls);
    expect(rings.length).toBeGreaterThanOrEqual(1);
    for (const { cx, cy, r, width } of rings) {
      const reach = r + width / 2;

      expect(cx - reach).toBeGreaterThanOrEqual(0);
      expect(cy - reach).toBeGreaterThanOrEqual(0);
      expect(cx + reach).toBeLessThanOrEqual(READ_MARKER_SIZE);
      expect(cy + reach).toBeLessThanOrEqual(READ_MARKER_SIZE);
    }
  });

  it("leaves the middle hollow, so the file dot it points at stays visible", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    expect(opsOf(calls)).not.toContain("fill");
  });

  it("draws rings big enough to read as rings, not hairline dots", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    for (const ring of ringsOf(calls)) {
      expect(ring.r).toBeGreaterThan(READ_MARKER_SIZE * 0.1);
      expect(ring.width).toBeGreaterThan(0);
    }
  });
});

describe("paintReadRings: the colour", () => {
  it("strokes in the colour it is given, rather than a violet of its own", () => {
    // The renderer owns READ_COLOR; a hard-coded literal here would drift from
    // the colour the dot itself is tinted with.
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const styles = calls
      .filter((call) => call.op === "strokeStyle")
      .map((call) => String(call.args[0]).toLowerCase());
    expect(styles.some((style) => style.includes("aa66ff"))).toBe(true);
  });

  it("tints whatever colour it is handed, not one it recognises", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, 0x33ff33);

    const styles = calls
      .filter((call) => call.op === "strokeStyle")
      .map((call) => String(call.args[0]).toLowerCase());
    expect(styles.some((style) => style.includes("33ff33"))).toBe(true);
  });

  it("has a colour in force before each of its strokes", () => {
    // A stroke issued before any strokeStyle inherits the context's default
    // black, which through the bloom is simply an invisible ring.
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const strokes = calls
      .map((call, index) => ({ call, index }))
      .filter(({ call }) => call.op === "stroke");
    for (const { index } of strokes) {
      expect(String(propertyAt(calls, "strokeStyle", index) ?? "")).toContain("aa66ff");
    }
  });

  it("pads a short hex so the colour is never a broken CSS string", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, 0x0000ff);

    const styles = calls
      .filter((call) => call.op === "strokeStyle")
      .map((call) => String(call.args[0]).toLowerCase());
    expect(styles.some((style) => style.includes("#0000ff"))).toBe(true);
  });

  it("emits a syntactically valid style even for a colour that is not a number", () => {
    // Nothing on the wire reaches this -- the caller passes a module constant --
    // so the only requirement is that a bad value cannot produce "#NaN", which
    // the canvas ignores, leaving the marker painted in whatever colour was set
    // last.
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, Number.NaN);

    const styles = calls
      .filter((call) => call.op === "strokeStyle")
      .map((call) => String(call.args[0]).toLowerCase());
    expect(styles.length).toBeGreaterThan(0);
    for (const style of styles) {
      expect(style).toMatch(/^#[0-9a-f]+$/);
    }
  });
});

/**
 * The cross-module property, and the reason `readMarker.ts` is not a parameter
 * of `searchMarker.ts`. Asserted as a RELATION between the two painters so that
 * either one can be retuned freely: what may not change is that they stay
 * different shapes.
 */
describe("the read marker against the search marker", () => {
  it("draws a different number of rings, so the bloom cannot merge the two meanings", () => {
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const readRings = opsOf(calls).filter((op) => op === "arc").length;
    const searchRings = opsOf(recordSearchRing(0x00ffff)).filter((op) => op === "arc").length;
    expect(readRings).not.toBe(searchRings);
  });

  it("strokes thinner than the search ring, because a read is a quiet state", () => {
    // A search hit answers a question the user just typed and should shout; a
    // read is ambient and must not compete with it.
    const { ctx, calls } = recordingContext();

    paintReadRings(ctx, READ_COLOR);

    const searchWidths = ringsOf(recordSearchRing(0x00ffff)).map((ring) => ring.width);
    const thinnestSearch = Math.min(...searchWidths);
    for (const ring of ringsOf(calls)) {
      expect(ring.width).toBeLessThan(thinnestSearch);
    }
  });
});
