/**
 * Contract tests (RED) for the agent avatar.
 *
 * Gource's defining image is a little figure walking the tree and shooting a
 * beam at each file it touches. Our actors were text labels only, so there was
 * nothing on screen to read as "this agent is doing that". These tests specify
 * the drawing itself, against the minimal 2D-context surface it uses, so the
 * shape is verified without a DOM or a GL context.
 *
 * One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { AVATAR_SIZE, paintAvatar, type AvatarContext } from "../src/avatar";

interface Call {
  op: string;
  args: readonly unknown[];
}

/** Records every 2D-context call instead of rasterizing anything. */
function recordingContext(): { ctx: AvatarContext; calls: Call[] } {
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
  } as unknown as AvatarContext;
  return { ctx, calls };
}

function opsOf(calls: Call[]): string[] {
  return calls.map((call) => call.op);
}

describe("paintAvatar", () => {
  it("draws a head as a filled circle", () => {
    const { ctx, calls } = recordingContext();

    paintAvatar(ctx, 0x33ff33);

    const arcs = calls.filter((call) => call.op === "arc");
    expect(arcs.length).toBeGreaterThanOrEqual(1);
  });

  it("draws a body, so the figure is more than a floating dot", () => {
    const { ctx, calls } = recordingContext();

    paintAvatar(ctx, 0x33ff33);

    expect(opsOf(calls)).toContain("lineTo");
  });

  it("tints the figure with the agent color", () => {
    const { ctx, calls } = recordingContext();

    paintAvatar(ctx, 0x33ff33);

    const styles = calls
      .filter((call) => call.op === "fillStyle" || call.op === "strokeStyle")
      .map((call) => String(call.args[0]).toLowerCase());
    expect(styles.some((style) => style.includes("33ff33"))).toBe(true);
  });

  it("pads a short hex so the color is never a broken CSS string", () => {
    const { ctx, calls } = recordingContext();

    paintAvatar(ctx, 0x0000ff);

    const styles = calls
      .filter((call) => call.op === "fillStyle" || call.op === "strokeStyle")
      .map((call) => String(call.args[0]).toLowerCase());
    expect(styles.some((style) => style.includes("#0000ff"))).toBe(true);
  });

  it("clears the canvas first, so a repaint never stacks on the previous frame", () => {
    const { ctx, calls } = recordingContext();

    paintAvatar(ctx, 0x33ff33);

    expect(opsOf(calls)[0]).toBe("clearRect");
  });

  it("stays inside the declared avatar box", () => {
    const { ctx, calls } = recordingContext();

    paintAvatar(ctx, 0x33ff33);

    const coords = calls
      .filter((call) => ["moveTo", "lineTo", "arc"].includes(call.op))
      .flatMap((call) => [call.args[0] as number, call.args[1] as number]);
    expect(coords.length).toBeGreaterThan(0);
    for (const value of coords) {
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThanOrEqual(AVATAR_SIZE);
    }
  });
});
