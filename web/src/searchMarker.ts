/**
 * The ring drawn around a node the search matched.
 *
 * Tinting the node itself does not work: it already carries its own colour, and
 * the bloom washes small hue differences out -- so a match would stay one
 * indistinguishable dot among hundreds. The marker is a separate sprite drawn
 * AROUND the node, hollow in the middle so the node it points at stays visible.
 *
 * Like {@link ./avatar}, the painting is expressed against
 * {@link SearchMarkerContext} -- the small slice of `CanvasRenderingContext2D`
 * it actually uses -- so the shape is testable without a DOM, a canvas or a GL
 * context. `createSearchMarkerCanvas` is the only part that needs a browser.
 */

import { cssHex } from "./avatar";

/** The marker is painted into a square of this many pixels, per side. */
export const SEARCH_MARKER_SIZE = 64;

/** Stroke width, as a fraction of the box. */
const RING_WIDTH = 0.08;

/**
 * Ring radius, as a fraction of the box.
 *
 * The sprite is mapped 1:1 onto a quad, so the outer half of the stroke is
 * clipped away if the circle reaches the edge -- and a ring missing its outer
 * half reads as a broken one. `RING_RADIUS + RING_WIDTH / 2` stays under 0.5.
 */
const RING_RADIUS = 0.42;

/** The subset of the 2D context this module needs. Keeps the drawing testable. */
export interface SearchMarkerContext {
  strokeStyle: string;
  lineWidth: number;
  lineCap: string;
  clearRect(x: number, y: number, w: number, h: number): void;
  beginPath(): void;
  closePath(): void;
  arc(cx: number, cy: number, r: number, start: number, end: number): void;
  stroke(): void;
}

/** Paint one hollow ring filling the {@link SEARCH_MARKER_SIZE} box, tinted `color`. */
export function paintSearchRing(ctx: SearchMarkerContext, color: number): void {
  const s = SEARCH_MARKER_SIZE;
  // Clear first: the same canvas is repainted when the marker changes colour,
  // and a leftover ring underneath would show through the hollow middle.
  ctx.clearRect(0, 0, s, s);

  ctx.strokeStyle = cssHex(color);
  ctx.lineWidth = s * RING_WIDTH;
  ctx.lineCap = "round";

  ctx.beginPath();
  ctx.arc(s * 0.5, s * 0.5, s * RING_RADIUS, 0, Math.PI * 2);
  ctx.closePath();
  ctx.stroke();
}

/**
 * Build a canvas carrying the ring, ready to become a texture.
 *
 * Browser-only: the renderer calls this, the tests exercise
 * {@link paintSearchRing}.
 */
export function createSearchMarkerCanvas(color: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = SEARCH_MARKER_SIZE;
  canvas.height = SEARCH_MARKER_SIZE;
  const ctx = canvas.getContext("2d");
  if (ctx) paintSearchRing(ctx as unknown as SearchMarkerContext, color);
  return canvas;
}
