/**
 * The agent's on-screen figure -- Gource's "bonequinho".
 *
 * An actor used to be a text label and a beam, which reads as a caption rather
 * than as somebody doing the work. This module draws the figure itself: a head,
 * a torso, arms and legs, tinted with the agent's color so several agents stay
 * telling apart at a glance.
 *
 * The painting is expressed against {@link AvatarContext} -- the small slice of
 * `CanvasRenderingContext2D` it actually uses -- so the shape is unit-testable
 * without a DOM, a canvas, or a GL context. `createAvatarCanvas` is the only
 * part that needs a browser.
 */

/** The avatar is painted into a square of this many pixels, per side. */
export const AVATAR_SIZE = 64;

/** The subset of the 2D context this module needs. Keeps the drawing testable. */
export interface AvatarContext {
  fillStyle: string;
  strokeStyle: string;
  lineWidth: number;
  lineCap: string;
  clearRect(x: number, y: number, w: number, h: number): void;
  beginPath(): void;
  closePath(): void;
  moveTo(x: number, y: number): void;
  lineTo(x: number, y: number): void;
  arc(cx: number, cy: number, r: number, start: number, end: number): void;
  fill(): void;
  stroke(): void;
}

/** `0xRRGGBB` as a CSS hex string, zero-padded so short values stay valid. */
export function cssHex(color: number): string {
  return `#${(color >>> 0).toString(16).padStart(6, "0")}`;
}

/**
 * Paint one stick figure filling the {@link AVATAR_SIZE} box, tinted `color`.
 *
 * Every coordinate stays inside the box: the sprite is mapped 1:1 onto a quad in
 * the scene, so anything drawn outside would simply be clipped away.
 */
export function paintAvatar(ctx: AvatarContext, color: number): void {
  const s = AVATAR_SIZE;
  // Clear first: the same canvas is repainted when an agent's color changes,
  // and a leftover figure underneath would show through the transparent gaps.
  ctx.clearRect(0, 0, s, s);

  const hex = cssHex(color);
  ctx.fillStyle = hex;
  ctx.strokeStyle = hex;
  ctx.lineWidth = s * 0.09;
  ctx.lineCap = "round";

  // Head.
  ctx.beginPath();
  ctx.arc(s * 0.5, s * 0.2, s * 0.15, 0, Math.PI * 2);
  ctx.closePath();
  ctx.fill();

  // Torso.
  ctx.beginPath();
  ctx.moveTo(s * 0.5, s * 0.36);
  ctx.lineTo(s * 0.5, s * 0.64);
  ctx.stroke();

  // Arms, drawn slightly raised so the figure reads as active, not standing.
  ctx.beginPath();
  ctx.moveTo(s * 0.24, s * 0.56);
  ctx.lineTo(s * 0.5, s * 0.44);
  ctx.lineTo(s * 0.76, s * 0.56);
  ctx.stroke();

  // Legs.
  ctx.beginPath();
  ctx.moveTo(s * 0.3, s * 0.92);
  ctx.lineTo(s * 0.5, s * 0.64);
  ctx.lineTo(s * 0.7, s * 0.92);
  ctx.stroke();
}

/**
 * Build a canvas carrying the figure, ready to become a texture.
 *
 * Browser-only: the renderer calls this, the tests exercise {@link paintAvatar}.
 */
export function createAvatarCanvas(color: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = AVATAR_SIZE;
  canvas.height = AVATAR_SIZE;
  const ctx = canvas.getContext("2d");
  if (ctx) paintAvatar(ctx as unknown as AvatarContext, color);
  return canvas;
}
