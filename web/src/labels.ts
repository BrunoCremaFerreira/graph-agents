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
 * Takes the height of the text's EM BOX, not of the sprite around it -- the
 * sprite is taller (it carries the texture's padding), and a sprite is centred
 * on its position, so the gap has to clear half of that. Proportional to the
 * text rather than constant: a fixed offset is what let directory names drift
 * visibly away from their nodes at low zoom.
 */
export function labelOffset(emWorldHeight: number): number {
  return emWorldHeight * 0.9;
}

/**
 * World height to scale a label sprite to, so its text lands at
 * `emWorldHeight`.
 *
 * A label texture is taller than its em box: it carries padding above and
 * below. Scaling the whole sprite to the height the text should have shrinks
 * the text by exactly that padding -- with the renderer's 48px font in a 72px
 * canvas the glyphs came out at two thirds of the requested size, which is what
 * made every name look small and soft.
 */
export function spriteHeightForEm(emWorldHeight: number, emFraction: number): number {
  // A texture that failed to measure must not scale a sprite to Infinity: one
  // bad canvas would cover the screen with a single giant quad.
  const fraction = Number.isFinite(emFraction) && emFraction > 0 ? emFraction : 1;
  return emWorldHeight / fraction;
}

/** Smallest raster size that keeps glyph stems from breaking up. */
const MIN_FONT_PIXELS = 12;

/** Largest raster size worth storing; beyond it the texture is never sampled. */
const MAX_FONT_PIXELS = 64;

/**
 * Font size, in device pixels, to rasterise a label texture at.
 *
 * Deliberately independent of the zoom: the sprite is rescaled every frame to a
 * fixed on-screen pixel height, so the only thing that changes how many real
 * pixels the text covers is the device pixel ratio. Making this depend on the
 * camera would rebuild every texture on every wheel tick.
 */
export function labelFontPixels(devicePixelRatio: number, pixels = LABEL_PIXEL_HEIGHT): number {
  const dpr = Number.isFinite(devicePixelRatio) && devicePixelRatio > 0 ? devicePixelRatio : 1;
  const raw = Math.round(pixels * dpr);
  return Math.min(MAX_FONT_PIXELS, Math.max(MIN_FONT_PIXELS, raw));
}

/**
 * Snap a world coordinate to whole device pixels, measured from `origin`.
 *
 * Text sampled with a linear filter is only crisp when texel and pixel centres
 * line up; a sprite landing on a fractional device pixel smears every glyph.
 * The grid is anchored on the camera centre rather than on the world origin so
 * panning shifts the whole grid with the view instead of re-blurring the labels
 * at every intermediate position.
 */
export function snapToPixelGrid(value: number, origin: number, worldPerPixel: number): number {
  // First layout pass: a zero-height viewport makes worldPerPixel 0, and
  // dividing by it would put every label at NaN and blank the graph.
  if (!Number.isFinite(worldPerPixel) || worldPerPixel <= 0) return value;
  return origin + Math.round((value - origin) / worldPerPixel) * worldPerPixel;
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

/**
 * Longest caption allowed over an agent's figure.
 *
 * "desenvolvedor-frontend" is 22 characters, the longest agent type this
 * project defines; the cap sits just above it so an everyday name is shown
 * whole while an invented one cannot run across the screen.
 */
export const MAX_ACTOR_LABEL_CHARS = 24;

/** Character standing in for the part of a caption that did not fit. */
const ELLIPSIS = "…";

/** Longest tail of an opaque id worth printing: enough to tell two apart. */
const SHORT_AGENT_CHARS = 8;

/**
 * A readable short name for an agent id.
 *
 * Session ids are UUID-length; printed in full they overlap each other and the
 * tree. The last segment is enough to tell two sessions apart. An opaque
 * subagent id has no segments, so it is simply cut short -- which is why an
 * agent that has a readable {@link actorDisplayName} should never come here.
 * An agent of `""` names nobody: by project rule it never becomes an actor.
 */
export function shortAgentName(agent: string): string {
  if (typeof agent !== "string") return "";
  const tail = agent.slice(agent.lastIndexOf("-") + 1);
  return tail.length > SHORT_AGENT_CHARS ? tail.slice(0, SHORT_AGENT_CHARS) : tail || agent;
}

/**
 * The caption to draw over an agent's figure.
 *
 * `label` is the subagent's agent type -- display text, and the only thing here
 * a human can read; `agent` is the IDENTITY (actor key and colour seed) and is
 * used only as a fallback, shortened. An orchestrator carries no type, so it
 * still gets its shortened session id; an event with neither is an unattributed
 * or seeded change, and no name is invented for it.
 *
 * Never throws: a stale client or a future daemon may send anything, and a bad
 * caption must not cost us the frame.
 */
export function actorDisplayName(label: string, agent: string): string {
  const text = typeof label === "string" ? label.trim() : "";
  if (!text) return shortAgentName(typeof agent === "string" ? agent.trim() : "");
  if (text.length <= MAX_ACTOR_LABEL_CHARS) return text;
  // Keep the head: it is the part that identifies which agent this is.
  return text.slice(0, MAX_ACTOR_LABEL_CHARS - 1) + ELLIPSIS;
}
