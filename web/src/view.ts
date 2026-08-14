/**
 * Interactive camera state: what the orthographic camera is looking at.
 *
 * Pure data and pure transforms, deliberately free of three.js and of the DOM,
 * so the awkward parts (zoom-under-cursor, pixel-to-world pan) are testable
 * without a GL context. The renderer owns one of these and copies it onto the
 * camera each frame.
 *
 * `manual` records that the user has taken over. While it is set, the automatic
 * fit stops moving the camera -- otherwise every frame would drag the view back
 * and the user could never hold a position long enough to read a label.
 */

/** Closest the camera may get; below this the bloom swallows everything. */
export const MIN_HALF_HEIGHT = 2;
/** Farthest out; beyond this the graph is a speck. */
export const MAX_HALF_HEIGHT = 4000;

export interface ViewState {
  /** World-space point at the centre of the screen. */
  readonly centerX: number;
  readonly centerY: number;
  /** Half of the visible world height; smaller means more zoomed in. */
  readonly halfHeight: number;
  /** True once the user has zoomed or panned, which suspends auto-fit. */
  readonly manual: boolean;
}

/** A point in normalized device coordinates: -1..1 on both axes, y up. */
export interface NdcPoint {
  readonly x: number;
  readonly y: number;
}

export interface ViewportSize {
  readonly width: number;
  readonly height: number;
}

export function createView(halfHeight = 60): ViewState {
  return { centerX: 0, centerY: 0, halfHeight, manual: false };
}

function clampHalfHeight(value: number): number {
  return Math.min(MAX_HALF_HEIGHT, Math.max(MIN_HALF_HEIGHT, value));
}

/**
 * Scale the view by `factor` (below 1 zooms in) about the pointer.
 *
 * The world point under the cursor is held still: it is recovered before the
 * zoom and the centre is re-derived from it afterwards, so the thing being
 * inspected stays put instead of sliding away.
 */
export function zoomAt(
  view: ViewState,
  factor: number,
  pointer: NdcPoint,
  aspect: number,
): ViewState {
  const halfHeight = clampHalfHeight(view.halfHeight * factor);
  const worldX = view.centerX + pointer.x * view.halfHeight * aspect;
  const worldY = view.centerY + pointer.y * view.halfHeight;
  return {
    centerX: worldX - pointer.x * halfHeight * aspect,
    centerY: worldY - pointer.y * halfHeight,
    halfHeight,
    manual: true,
  };
}

/**
 * Translate the view by a drag measured in screen pixels.
 *
 * The camera moves against the drag so the graph tracks the cursor, and the
 * world-per-pixel scale follows the zoom level, so a drag covers the same
 * on-screen distance however far in you are.
 */
export function panByPixels(
  view: ViewState,
  dxPixels: number,
  dyPixels: number,
  viewport: ViewportSize,
): ViewState {
  const worldPerPixel = (2 * view.halfHeight) / Math.max(1, viewport.height);
  return {
    centerX: view.centerX - dxPixels * worldPerPixel,
    // Screen y grows downward, world y grows upward.
    centerY: view.centerY + dyPixels * worldPerPixel,
    halfHeight: view.halfHeight,
    manual: true,
  };
}

export interface ViewTarget {
  readonly centerX: number;
  readonly centerY: number;
  readonly halfHeight: number;
}

/** Ease towards the auto-fit target, unless the user has taken control. */
export function follow(view: ViewState, target: ViewTarget, ease: number): ViewState {
  if (view.manual) return view;
  return {
    centerX: view.centerX + (target.centerX - view.centerX) * ease,
    centerY: view.centerY + (target.centerY - view.centerY) * ease,
    halfHeight: view.halfHeight + (target.halfHeight - view.halfHeight) * ease,
    manual: false,
  };
}

/**
 * Ease towards a target the user asked for, whatever `manual` says.
 *
 * The opposite of {@link follow} on both counts, and deliberately so. Focusing
 * is a direct order -- a search hit, say -- and the camera is usually already
 * `manual` by then, because the user had been driving it around looking for the
 * very thing they gave up and searched for; obeying `manual` here would mean the
 * match never arrives. Setting `manual` on the way out is the other half: left
 * false, the next frame's auto-fit would drag the view straight back off it.
 */
export function focusOn(view: ViewState, target: ViewTarget, ease: number): ViewState {
  return {
    centerX: view.centerX + (target.centerX - view.centerX) * ease,
    centerY: view.centerY + (target.centerY - view.centerY) * ease,
    // A single matched file gives a target of nearly zero size; unclamped, the
    // camera closes in until the bloom whites the screen out.
    halfHeight: clampHalfHeight(view.halfHeight + (target.halfHeight - view.halfHeight) * ease),
    manual: true,
  };
}

/** Resume auto-fit from wherever the camera currently sits. */
export function releaseToAuto(view: ViewState): ViewState {
  return { ...view, manual: false };
}
