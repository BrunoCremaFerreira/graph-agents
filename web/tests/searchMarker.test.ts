/**
 * Contract tests (RED) for the search-result marker.
 *
 * A match has to be visible among hundreds of identical dots, and tinting the
 * node itself does not work: the node already carries its own colour and the
 * bloom washes small hue differences out. The marker is a ring drawn AROUND the
 * matched node, on its own sprite.
 *
 * Like {@link ../src/avatar}, the painting is expressed against the small slice
 * of `CanvasRenderingContext2D` it uses, so the shape is verified without a DOM,
 * a canvas, or a GL context -- `renderer.ts` needs all three and cannot be
 * tested at all.
 *
 * Expected to FAIL until src/searchMarker.ts exists.
 *
 * One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { SEARCH_MARKER_SIZE, paintSearchRing, type SearchMarkerContext } from "../src/searchMarker";

interface Call {
  op: string;
  args: readonly unknown[];
}

/** Records every 2D-context call instead of rasterizing anything. */
function recordingContext(): { ctx: SearchMarkerContext; calls: Call[] } {
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
  } as unknown as SearchMarkerContext;
  return { ctx, calls };
}

function opsOf(calls: Call[]): string[] {
  return calls.map((call) => call.op);
}

/** The stroke width in force when `index` was recorded; 0 if never set. */
function lineWidthAt(calls: Call[], index: number): number {
  let width = 0;
  for (let i = 0; i < index; i += 1) {
    if (calls[i].op === "lineWidth") width = Number(calls[i].args[0]);
  }
  return width;
}

describe("paintSearchRing", () => {
  it("clears the canvas first, so a repaint never stacks on the previous colour", () => {
    // The same canvas is repainted when the marker changes colour; a leftover
    // ring underneath would show through the transparent middle.
    const { ctx, calls } = recordingContext();

    paintSearchRing(ctx, 0x33ff33);

    expect(opsOf(calls)[0]).toBe("clearRect");
  });

  it("draws a ring, not a blob", () => {
    const { ctx, calls } = recordingContext();

    paintSearchRing(ctx, 0x33ff33);

    const ops = opsOf(calls);
    expect(ops).toContain("arc");
    expect(ops).toContain("stroke");
  });

  it("leaves the middle hollow, so the node it marks stays visible", () => {
    const { ctx, calls } = recordingContext();

    paintSearchRing(ctx, 0x33ff33);

    expect(opsOf(calls)).not.toContain("fill");
  });

  it("tints the ring with the colour it is given", () => {
    const { ctx, calls } = recordingContext();

    paintSearchRing(ctx, 0x33ff33);

    const styles = calls
      .filter((call) => call.op === "strokeStyle")
      .map((call) => String(call.args[0]).toLowerCase());
    expect(styles.some((style) => style.includes("33ff33"))).toBe(true);
  });

  it("pads a short hex so the colour is never a broken CSS string", () => {
    const { ctx, calls } = recordingContext();

    paintSearchRing(ctx, 0x0000ff);

    const styles = calls
      .filter((call) => call.op === "strokeStyle")
      .map((call) => String(call.args[0]).toLowerCase());
    expect(styles.some((style) => style.includes("#0000ff"))).toBe(true);
  });

  it("keeps the whole stroke inside the declared marker box", () => {
    // The sprite is mapped 1:1 onto a quad, so a ring drawn to the very edge
    // loses the outer half of its stroke to clipping and reads as a broken
    // circle. Half the line width has to fit too, on all four sides.
    const { ctx, calls } = recordingContext();

    paintSearchRing(ctx, 0x33ff33);

    const arcs = calls
      .map((call, index) => ({ call, index }))
      .filter(({ call }) => call.op === "arc");
    expect(arcs.length).toBeGreaterThanOrEqual(1);
    for (const { call, index } of arcs) {
      const [cx, cy, r] = call.args as [number, number, number];
      const reach = r + lineWidthAt(calls, index) / 2;

      expect(cx - reach).toBeGreaterThanOrEqual(0);
      expect(cy - reach).toBeGreaterThanOrEqual(0);
      expect(cx + reach).toBeLessThanOrEqual(SEARCH_MARKER_SIZE);
      expect(cy + reach).toBeLessThanOrEqual(SEARCH_MARKER_SIZE);
    }
  });

  it("draws a ring big enough to read as one, not a hairline dot", () => {
    const { ctx, calls } = recordingContext();

    paintSearchRing(ctx, 0x33ff33);

    const radii = calls
      .filter((call) => call.op === "arc")
      .map((call) => Number(call.args[2]));
    expect(Math.max(...radii)).toBeGreaterThan(SEARCH_MARKER_SIZE * 0.25);
  });
});
