/**
 * Label placement and selection: where a name sits, how big it is, and which
 * files earn one this frame.
 *
 * Pure data and pure transforms, free of three.js and of the DOM -- same
 * contract as {@link ./view}, and for the same reason: the renderer needs a GL
 * context, so any decision worth testing has to live outside it.
 *
 * Two rules drive everything here:
 *
 *  - **Size is in pixels, not world units.** The camera spans halfHeight 2..4000
 *    (see MIN/MAX_HALF_HEIGHT in view.ts), so a label fixed in world units is
 *    sub-pixel with the tree framed and screen-filling up close. Every size and
 *    offset is therefore derived from the current zoom.
 *  - **Only a bounded handful of files are named.** A project has hundreds of
 *    files; naming them all is both unreadable and a sprite per node. Names go
 *    to the files that were just touched, plus -- once the camera is close
 *    enough to have room -- the idle ones still on screen.
 */

/** On-screen height of label text, in CSS pixels. */
export const LABEL_PIXEL_HEIGHT = 13;

/**
 * Half-height at or below which idle files start showing their names. Above it
 * the camera is framing enough of the tree that the names would collide.
 */
export const FILE_LABEL_ZOOM_THRESHOLD = 90;

/** Zoom at which the reveal is fully faded in, as a fraction of the threshold. */
const ZOOM_REVEAL_FULL = 0.6;

/** Opacity an idle file's name reaches once revealed by zoom. */
const COLD_LABEL_OPACITY = 0.55;

/**
 * How many file names may be on screen at once. This is also the size of the
 * renderer's sprite pool: a cap here is what keeps a burst of writes from
 * turning into hundreds of draw calls.
 */
export const MAX_FILE_LABELS = 48;

/** Below this highlight a file counts as idle -- its name is no longer news. */
const HOT_HIGHLIGHT = 0.02;

/** Fraction of the viewport kept as margin, so labels do not pop at the edge. */
const CULL_MARGIN = 1.05;

/** A file that could be given a name, with its current position and heat. */
export interface LabelCandidate {
  path: string;
  /** Highlight in [0, 1], as decayed by the simulation. */
  highlight: number;
  x: number;
  y: number;
}

/** The world rectangle currently on screen. */
export interface LabelViewport {
  readonly centerX: number;
  readonly centerY: number;
  readonly halfHeight: number;
  readonly aspect: number;
}

function clamp01(value: number): number {
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

/** Hermite ramp from 0 at `edge0` to 1 at `edge1`; edges may run either way. */
function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = clamp01((x - edge0) / (edge1 - edge0));
  return t * t * (3 - 2 * t);
}

/**
 * World height that renders `pixels` tall at the current zoom.
 *
 * The renderer calls this every frame and rescales each label sprite, which is
 * what keeps a name readable from the widest fit down to a single file.
 */
export function labelWorldHeight(
  halfHeight: number,
  viewportHeightPx: number,
  pixels = LABEL_PIXEL_HEIGHT,
): number {
  // Guard the first layout pass, where the canvas can still measure 0.
  const worldPerPixel = (2 * halfHeight) / Math.max(1, viewportHeightPx);
  return worldPerPixel * pixels;
}

/**
 * Vertical gap between a node and its label, in world units.
 *
 * Proportional to the text rather than constant: a fixed offset is what let
 * directory names drift visibly away from their nodes at low zoom.
 */
export function labelOffset(worldHeight: number): number {
  return worldHeight * 0.9;
}

/**
 * How visible a file's name should be.
 *
 * A touched file is named wherever the camera is -- that is the event the user
 * is watching for. An idle file is named only once the camera is close enough
 * to have room, faded in over the approach so names do not pop into existence.
 */
export function fileLabelOpacity(highlight: number, halfHeight: number): number {
  const hot = clamp01(highlight);
  const revealed = smoothstep(
    FILE_LABEL_ZOOM_THRESHOLD,
    FILE_LABEL_ZOOM_THRESHOLD * ZOOM_REVEAL_FULL,
    halfHeight,
  );
  return clamp01(Math.max(hot, revealed * COLD_LABEL_OPACITY));
}

/** Whether a candidate falls inside the visible rectangle, plus margin. */
function onScreen(candidate: LabelCandidate, viewport: LabelViewport): boolean {
  const halfH = viewport.halfHeight * CULL_MARGIN;
  const halfW = viewport.halfHeight * viewport.aspect * CULL_MARGIN;
  return (
    Math.abs(candidate.x - viewport.centerX) <= halfW &&
    Math.abs(candidate.y - viewport.centerY) <= halfH
  );
}

/**
 * Choose which files get a name this frame, hottest first.
 *
 * Off-screen candidates are dropped first -- a label nobody can see still costs
 * a slot. What survives is the touched files (always) plus, when the camera is
 * past {@link FILE_LABEL_ZOOM_THRESHOLD}, the idle ones. Ties break on path so
 * the assignment is stable frame to frame; an unstable order would make labels
 * swap sprites and flicker.
 */
export function selectFileLabels(
  candidates: readonly LabelCandidate[],
  viewport: LabelViewport,
  max = MAX_FILE_LABELS,
): LabelCandidate[] {
  const zoomedIn = viewport.halfHeight <= FILE_LABEL_ZOOM_THRESHOLD;
  const eligible: LabelCandidate[] = [];

  for (const candidate of candidates) {
    if (candidate.highlight <= HOT_HIGHLIGHT && !zoomedIn) continue;
    if (!onScreen(candidate, viewport)) continue;
    eligible.push(candidate);
  }

  eligible.sort((a, b) => b.highlight - a.highlight || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return eligible.length > max ? eligible.slice(0, max) : eligible;
}
