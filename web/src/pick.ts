/**
 * What a click lands on: hit-testing a pointer against the graph's file nodes.
 *
 * Clicking a file opens its viewer, so the first decision the feature makes is
 * "which node, if any, was that click aimed at?". It lives in a pure module, the
 * way {@link ./view} and {@link ./labels} do, because `renderer.ts` needs a GL
 * context and cannot be unit-tested: the renderer's job stops at unprojecting the
 * pointer into WORLD coordinates, and everything after that is the arithmetic
 * here.
 *
 * Distances are therefore in world units, never in pixels. The camera spans
 * halfHeight 2..4000, so a radius fixed in pixels would select nothing with the
 * tree framed and half the project up close; the caller derives the radius from
 * the current zoom, exactly as the labels derive their size.
 *
 * Two rules carry the weight beyond "nearest wins":
 *
 *  - an exact tie is broken by PATH, because the force layout parks nodes on top
 *    of each other and the candidate list is rebuilt in arrival order on every
 *    event -- without it, one click opens different files on different frames;
 *  - a degenerate radius or a degenerate position selects NOTHING. A canvas that
 *    has not been measured yet yields 0, Infinity or NaN, and each of those has
 *    to mean "no hit" rather than "everything hits".
 */

/**
 * How far the pointer may travel between press and release and still count as a
 * click, in CSS pixels.
 *
 * The same gesture opens a file and pans the graph, so the two have to be told
 * apart from the movement alone. Nobody holds a mouse perfectly still while
 * clicking, and a slop this small never swallows a deliberate drag. Travel is
 * measured RADIALLY: a short diagonal pan passes neither axis alone and still
 * has to be a pan.
 */
export const CLICK_SLOP_PIXELS = 4;
/** And how long it may be held: past this it is a press-and-hold, not a tap. */
export const CLICK_MAX_MS = 400;

/** A pointer gesture: press to release, in CSS pixels and milliseconds. */
export interface ClickGesture {
  /** The browser's own click count for this press (`PointerEvent.detail`). */
  readonly detail: number;
  readonly dx: number;
  readonly dy: number;
  readonly elapsedMs: number;
}

/**
 * Whether a pointer gesture counts as a click that opens a file.
 *
 * Both boundaries are inclusive -- a gesture exactly on the slop or exactly at
 * 400ms is still a click -- and every field has to be a real number first, since
 * the first layout pass and a clock read before the canvas is measured produce
 * NaN and Infinity, which would otherwise slide straight through a `>`.
 */
export function isClickGesture(gesture: ClickGesture): boolean {
  const { detail, dx, dy, elapsedMs } = gesture;
  if (!Number.isFinite(detail)) return false;
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return false;
  if (!Number.isFinite(elapsedMs)) return false;
  // The double-click already has an owner -- the camera's auto-fit -- and opening
  // a file in the middle of it runs a second command nobody asked for. `detail`
  // is what the browser counts to decide when to emit `dblclick`, so deferring to
  // it agrees with that exactly and needs no timer of our own.
  if (detail > 1) return false;
  if (Math.hypot(dx, dy) > CLICK_SLOP_PIXELS) return false;
  return elapsedMs <= CLICK_MAX_MS;
}

/** A node of the graph, as far as hit-testing is concerned. */
export interface PickCandidate {
  readonly path: string;
  readonly x: number;
  readonly y: number;
}

/** Where the click landed, in world units. */
export interface PickPoint {
  readonly x: number;
  readonly y: number;
}

/**
 * The path of the candidate nearest to `world` within `radius`, or `null`.
 *
 * The radius is inclusive: a click exactly on the boundary is where a deliberate
 * click on a small node lands.
 */
export function pickFile(
  candidates: readonly PickCandidate[],
  world: PickPoint,
  radius: number,
): string | null {
  if (!Number.isFinite(radius) || radius <= 0) return null;
  if (!Number.isFinite(world.x) || !Number.isFinite(world.y)) return null;

  // Compared squared, so no candidate pays for a square root.
  const limit = radius * radius;
  let best: string | null = null;
  let bestDistance = Infinity;

  for (const candidate of candidates) {
    // A node inserted before the layout has placed it is nowhere for a frame or
    // two, so it is never under the pointer.
    if (!Number.isFinite(candidate.x) || !Number.isFinite(candidate.y)) continue;

    const dx = candidate.x - world.x;
    const dy = candidate.y - world.y;
    const distance = dx * dx + dy * dy;
    if (distance > limit) continue;

    const closer = distance < bestDistance;
    const tied = distance === bestDistance && best !== null && candidate.path < best;
    if (closer || tied) {
      best = candidate.path;
      bestDistance = distance;
    }
  }

  return best;
}

/**
 * The file the pointer is currently RESTING on, or `null`.
 *
 * Hovering answers "what is that dot?" without opening anything, so the answer
 * has to be {@link pickFile}'s, unchanged: the caller passes the same radius a
 * click would use, because what you see named is what clicking would open, and a
 * hover radius wider (or narrower) than the click's turns every label into a
 * false promise about where the click will land.
 *
 * Only two states are decided here, and both mean the pointer is not ASKING:
 *
 *  - the pointer is off the canvas (`null`), which must drop the name rather
 *    than freeze it on the last node the pointer happened to cross;
 *  - the graph is being dragged. A drag moves the tree UNDER the pointer instead
 *    of inspecting it, so nodes sweep past by the dozen and naming each one in
 *    turn is noise over a camera gesture.
 */
export function hoverTarget(
  candidates: readonly PickCandidate[],
  pointer: PickPoint | null,
  radius: number,
  dragging: boolean,
): string | null {
  if (pointer === null || dragging) return null;
  return pickFile(candidates, pointer, radius);
}
