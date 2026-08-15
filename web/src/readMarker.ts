/**
 * The ring drawn around a file an agent is READING.
 *
 * A write is a flash that decays; a read is a ring that pulses while it lasts.
 * The two markers are deliberately different shapes as well as different
 * colours: `searchMarker.ts` draws ONE thick ring in cyan around what the user
 * asked for, this one draws TWO thin concentric rings in violet around what an
 * agent is looking at, so the marker still says which is which on a screenshot,
 * on a colour-blind eye, and through the bloom (which washes hue differences
 * out of small bright shapes).
 *
 * Like {@link ./searchMarker} and {@link ./avatar}, the painting is expressed
 * against {@link ReadMarkerContext} -- the slice of `CanvasRenderingContext2D`
 * it actually uses -- so the shape can be exercised without a DOM, a canvas or a
 * GL context. `createReadMarkerCanvas` is the only part that needs a browser.
 */

import { cssHex } from "./avatar";

/** The marker is painted into a square of this many pixels, per side. */
export const READ_MARKER_SIZE = 64;

/**
 * The two rings, as fractions of the box: radius and stroke width.
 *
 * The sprite is mapped 1:1 onto a quad, so `radius + width / 2` has to stay
 * under 0.5 or the outer half of the stroke is clipped and the ring reads as
 * broken. Both rings are thinner than the search marker's single one: this is a
 * quiet, sustained state, not the answer to a question the user typed.
 */
const OUTER_RADIUS = 0.44;
const OUTER_WIDTH = 0.05;
const INNER_RADIUS = 0.3;
const INNER_WIDTH = 0.035;

/** The subset of the 2D context this module needs. Keeps the drawing testable. */
export interface ReadMarkerContext {
  strokeStyle: string;
  lineWidth: number;
  lineCap: string;
  clearRect(x: number, y: number, w: number, h: number): void;
  beginPath(): void;
  closePath(): void;
  arc(cx: number, cy: number, r: number, start: number, end: number): void;
  stroke(): void;
}

/** Paint the two rings filling the {@link READ_MARKER_SIZE} box, tinted `color`. */
export function paintReadRings(ctx: ReadMarkerContext, color: number): void {
  const s = READ_MARKER_SIZE;
  // Clear first: both rings are hollow, so anything left underneath shows
  // through the middle -- which is exactly where the file dot has to stay
  // visible.
  ctx.clearRect(0, 0, s, s);

  ctx.strokeStyle = cssHex(color);
  ctx.lineCap = "round";

  ctx.lineWidth = s * OUTER_WIDTH;
  ctx.beginPath();
  ctx.arc(s * 0.5, s * 0.5, s * OUTER_RADIUS, 0, Math.PI * 2);
  ctx.closePath();
  ctx.stroke();

  ctx.lineWidth = s * INNER_WIDTH;
  ctx.beginPath();
  ctx.arc(s * 0.5, s * 0.5, s * INNER_RADIUS, 0, Math.PI * 2);
  ctx.closePath();
  ctx.stroke();
}

/**
 * Build a canvas carrying the rings, ready to become a texture.
 *
 * Browser-only, and called ONCE: every marker in the renderer's pool shares the
 * single texture built from it, because they all show the same shape in the same
 * colour and only their position, scale and opacity differ.
 */
export function createReadMarkerCanvas(color: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = READ_MARKER_SIZE;
  canvas.height = READ_MARKER_SIZE;
  const ctx = canvas.getContext("2d");
  if (ctx) paintReadRings(ctx as unknown as ReadMarkerContext, color);
  return canvas;
}
