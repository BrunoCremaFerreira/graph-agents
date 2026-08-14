/**
 * Contract tests (RED) for label placement and selection.
 *
 * Two defects motivate this module. Directory names drifted away from their
 * nodes because the renderer only repositioned label sprites when the tree's
 * topology changed, while the force layout keeps moving nodes every frame; and
 * file names were never drawn at all. Both fixes need decisions -- where a label
 * sits, how big it is, and which files deserve one -- that must be testable
 * without a WebGL context, so they live here rather than in renderer.ts.
 *
 * The property that matters for readability is that a label's size is constant
 * in PIXELS, not in world units: the camera spans halfHeight 2..4000, so a
 * world-sized label is either sub-pixel or screen-filling. Expected to FAIL
 * until src/labels.ts exists.
 *
 * A third defect motivates the sharpness group below: names render blurry and
 * too small. `labelWorldHeight` returns the height the TEXT should occupy, but
 * the renderer scales the whole sprite to it, and the label texture is
 * font(48) + pad(12)*2 = 72px tall for a 48px em box -- so the text lands at
 * 67% of 13px, about 8.7px. On top of that the texture is rasterised at a fixed
 * 48px regardless of device pixel ratio and the sprites land on fractional
 * device pixels, so the linear filter smears every glyph. The three fixes --
 * sizing the sprite from the em box, choosing the raster size from the DPR, and
 * snapping positions to the pixel grid -- are arithmetic, not GL, so they
 * belong here where they can be tested without a canvas.
 *
 * A fourth defect motivates the actor-name group at the bottom: the name over an
 * agent's figure is unreadable. It comes from `shortAgentName` in renderer.ts,
 * which cuts an id at its last `-` and truncates to 8 chars -- fine for a session
 * UUID, but a subagent's id is opaque (`a747fec535c143044`), so the caption reads
 * as hex garbage. The daemon now sends the subagent's `agent_type` as a separate
 * `label` field, and the choice between the two -- readable name when there is
 * one, shortened id otherwise, and nothing at all for an empty agent, which by
 * project rule never becomes an actor -- is pure string logic. It belongs here,
 * not behind a GL context.
 *
 * A fifth defect motivates the "pinned" argument in the two search groups at the
 * very bottom: a search match must show its name, and every rule in this module
 * is against it. A matched file is usually cold -- nobody just touched it, that
 * is why it had to be searched for -- and the camera framing several matches at
 * once sits far out, so the cold-plus-far cut in `selectFileLabels` and the fade
 * in `fileLabelOpacity` erase exactly the names the user asked to see. Pinning
 * overrides both, and only for the caller that asks for it: the argument is
 * optional and every existing call must keep behaving as it does today.
 */

import { describe, it, expect } from "vitest";
import {
  labelWorldHeight,
  labelOffset,
  fileLabelOpacity,
  selectFileLabels,
  spriteHeightForEm,
  labelFontPixels,
  snapToPixelGrid,
  actorDisplayName,
  shortAgentName,
  FILE_LABEL_ZOOM_THRESHOLD,
  LABEL_PIXEL_HEIGHT,
  MAX_FILE_LABELS,
  MAX_ACTOR_LABEL_CHARS,
  type LabelCandidate,
} from "../src/labels";

const VIEWPORT_H = 1000;

/**
 * Fraction of the label texture's height taken by the em box, as the renderer
 * builds it today: a 48px font inside a 48 + 12*2 = 72px canvas.
 */
const EM_FRACTION = 48 / 72;

/** On-screen height, in pixels, of something `worldHeight` tall. */
function pixelsOnScreen(worldHeight: number, halfHeight: number): number {
  return (worldHeight / (2 * halfHeight)) * VIEWPORT_H;
}

function candidate(path: string, highlight: number, x = 0, y = 0): LabelCandidate {
  return { path, highlight, x, y };
}

/** A viewport centred on the origin; `halfHeight` decides the zoom. */
function viewport(halfHeight: number) {
  return { centerX: 0, centerY: 0, halfHeight, aspect: 16 / 9 };
}

describe("labelWorldHeight", () => {
  it("renders a label at the requested pixel height", () => {
    const world = labelWorldHeight(100, VIEWPORT_H, 13);

    expect(pixelsOnScreen(world, 100)).toBeCloseTo(13);
  });

  it("keeps the on-screen size constant across the whole zoom range", () => {
    const near = labelWorldHeight(2, VIEWPORT_H, LABEL_PIXEL_HEIGHT);
    const far = labelWorldHeight(4000, VIEWPORT_H, LABEL_PIXEL_HEIGHT);

    expect(pixelsOnScreen(near, 2)).toBeCloseTo(pixelsOnScreen(far, 4000));
  });

  it("grows the world height as the camera pulls back", () => {
    // A world-fixed label would return the same number here -- that is the bug.
    expect(labelWorldHeight(200, VIEWPORT_H)).toBeCloseTo(2 * labelWorldHeight(100, VIEWPORT_H));
  });

  it("survives a zero-height viewport during the first layout pass", () => {
    expect(Number.isFinite(labelWorldHeight(100, 0))).toBe(true);
  });
});

describe("labelOffset", () => {
  it("scales with the text so the label hugs its node at any zoom", () => {
    expect(labelOffset(10)).toBeCloseTo(2 * labelOffset(5));
  });

  it("clears the node instead of sitting on top of it", () => {
    expect(labelOffset(10)).toBeGreaterThan(0);
  });

  it("lifts the label clear of its own sprite, now that it is fed the em height", () => {
    // The argument becomes the em-box height, not the full texture height, so
    // the sprite around it is taller than what is passed in. A sprite is
    // centred on its position, so anything less than half its height leaves the
    // name overlapping the node it is supposed to caption.
    const em = LABEL_PIXEL_HEIGHT;

    expect(labelOffset(em)).toBeGreaterThanOrEqual(spriteHeightForEm(em, EM_FRACTION) / 2);
  });
});

describe("spriteHeightForEm", () => {
  it("makes the text render at the requested height, not at the texture's", () => {
    // The bug: scaling the sprite to 13 world units puts the em box at
    // 13 * 48/72 = 8.7 -- two thirds of the size the caller asked for.
    const sprite = spriteHeightForEm(13, EM_FRACTION);

    expect(sprite * EM_FRACTION).toBeCloseTo(13);
  });

  it("scales in proportion to the height the text must occupy", () => {
    expect(spriteHeightForEm(20, EM_FRACTION)).toBeCloseTo(2 * spriteHeightForEm(10, EM_FRACTION));
  });

  it("leaves the height alone when the em box fills the whole texture", () => {
    expect(spriteHeightForEm(13, 1)).toBeCloseTo(13);
  });

  it("grows the sprite as the texture's padding takes more of it", () => {
    expect(spriteHeightForEm(13, 0.5)).toBeGreaterThan(spriteHeightForEm(13, 0.9));
  });

  it("keeps the em box at the requested size at any zoom", () => {
    const em = labelWorldHeight(4000, VIEWPORT_H, LABEL_PIXEL_HEIGHT);
    const sprite = spriteHeightForEm(em, EM_FRACTION);

    expect(pixelsOnScreen(sprite * EM_FRACTION, 4000)).toBeCloseTo(LABEL_PIXEL_HEIGHT);
  });

  it("falls back to a finite height when the em fraction is degenerate", () => {
    // A texture that failed to measure must not scale a sprite to Infinity:
    // one bad canvas would blank the screen with a single giant quad.
    for (const bad of [0, -0.5, NaN]) {
      const height = spriteHeightForEm(13, bad);

      expect(Number.isFinite(height)).toBe(true);
      expect(height).toBeGreaterThan(0);
    }
  });
});

describe("labelFontPixels", () => {
  it("rasterises at the physical pixel size the screen will show", () => {
    expect(labelFontPixels(2, 20)).toBe(40);
  });

  it("returns a whole number of pixels for the font string", () => {
    expect(Number.isInteger(labelFontPixels(1.5, 13))).toBe(true);
  });

  it("asks a retina screen for twice the texture of a plain one", () => {
    expect(labelFontPixels(2, 20)).toBe(2 * labelFontPixels(1, 20));
  });

  it("ignores the zoom, because the sprite is rescaled to a fixed pixel height", () => {
    // A zoom-dependent raster size would rebuild every texture on every wheel
    // tick. The label is always LABEL_PIXEL_HEIGHT px on screen, so the only
    // thing that changes how many real pixels that is, is the device ratio.
    expect(labelFontPixels.length).toBeLessThanOrEqual(2);
  });

  it("never rasterises so small that the glyphs fall apart", () => {
    expect(labelFontPixels(1, 3)).toBeGreaterThanOrEqual(12);
  });

  it("never wastes texture beyond what the screen can resolve", () => {
    expect(labelFontPixels(4, 200)).toBeLessThanOrEqual(64);
  });

  it("treats a missing or nonsensical device pixel ratio as 1", () => {
    for (const bad of [0, NaN, -2]) {
      expect(labelFontPixels(bad, 20)).toBe(labelFontPixels(1, 20));
    }
  });

  it("defaults to the on-screen label height when no pixel size is given", () => {
    expect(labelFontPixels(1)).toBe(labelFontPixels(1, LABEL_PIXEL_HEIGHT));
  });
});

describe("snapToPixelGrid", () => {
  const WORLD_PER_PIXEL = 0.37;
  const ORIGIN = -12.5;

  it("leaves a coordinate sitting on the camera centre where it is", () => {
    expect(snapToPixelGrid(ORIGIN, ORIGIN, WORLD_PER_PIXEL)).toBe(ORIGIN);
  });

  it("lands on a whole number of pixels away from the camera centre", () => {
    // The grid follows the camera: anchoring it at the world origin would make
    // every label re-blur the moment the user pans.
    for (const value of [-40.3, -12.4, 0, 7.77, 133.1]) {
      const steps = (snapToPixelGrid(value, ORIGIN, WORLD_PER_PIXEL) - ORIGIN) / WORLD_PER_PIXEL;

      expect(Math.abs(steps - Math.round(steps))).toBeLessThan(1e-9);
    }
  });

  it("never moves a label by more than half a pixel", () => {
    for (const value of [-40.3, -12.4, 0, 7.77, 133.1]) {
      const moved = Math.abs(snapToPixelGrid(value, ORIGIN, WORLD_PER_PIXEL) - value);

      expect(moved).toBeLessThanOrEqual(WORLD_PER_PIXEL / 2 + 1e-9);
    }
  });

  it("is idempotent, so a snapped label does not creep frame after frame", () => {
    const once = snapToPixelGrid(7.77, ORIGIN, WORLD_PER_PIXEL);

    expect(snapToPixelGrid(once, ORIGIN, WORLD_PER_PIXEL)).toBeCloseTo(once, 10);
  });

  it("passes the value through when the pixel size is unusable", () => {
    // First layout pass: a zero-height viewport makes worldPerPixel 0. Dividing
    // by it would put every label at NaN and blank the graph.
    for (const bad of [0, -1, NaN]) {
      expect(snapToPixelGrid(7.77, ORIGIN, bad)).toBe(7.77);
    }
  });
});

describe("fileLabelOpacity", () => {
  const FAR = FILE_LABEL_ZOOM_THRESHOLD * 4;
  const NEAR = FILE_LABEL_ZOOM_THRESHOLD * 0.25;

  it("hides an idle file while the whole tree is framed", () => {
    expect(fileLabelOpacity(0, FAR)).toBe(0);
  });

  it("shows a file that was just touched, however far out the camera is", () => {
    expect(fileLabelOpacity(1, FAR)).toBeGreaterThan(0.5);
  });

  it("fades the name out with the highlight", () => {
    expect(fileLabelOpacity(0.2, FAR)).toBeLessThan(fileLabelOpacity(0.8, FAR));
  });

  it("reveals idle files once the camera is close enough", () => {
    expect(fileLabelOpacity(0, NEAR)).toBeGreaterThan(0);
  });

  it("ramps the zoom reveal in smoothly rather than popping", () => {
    const atThreshold = fileLabelOpacity(0, FILE_LABEL_ZOOM_THRESHOLD);
    const justInside = fileLabelOpacity(0, FILE_LABEL_ZOOM_THRESHOLD * 0.95);

    expect(atThreshold).toBeCloseTo(0);
    expect(justInside).toBeGreaterThan(0);
    expect(justInside).toBeLessThan(0.2);
  });

  it("never exceeds full opacity", () => {
    expect(fileLabelOpacity(1, NEAR)).toBeLessThanOrEqual(1);
  });
});

describe("selectFileLabels", () => {
  it("names nothing when a big idle tree is framed whole", () => {
    const cold = Array.from({ length: 404 }, (_, i) => candidate(`src/f${i}.ts`, 0));

    expect(selectFileLabels(cold, viewport(FILE_LABEL_ZOOM_THRESHOLD * 5))).toEqual([]);
  });

  it("names a touched file even with the camera far out", () => {
    const nodes = [candidate("src/cold.ts", 0), candidate("src/hot.ts", 1)];

    const chosen = selectFileLabels(nodes, viewport(FILE_LABEL_ZOOM_THRESHOLD * 5));

    expect(chosen.map((c) => c.path)).toEqual(["src/hot.ts"]);
  });

  it("names idle files once zoomed in past the threshold", () => {
    const nodes = [candidate("a.ts", 0), candidate("b.ts", 0)];

    const chosen = selectFileLabels(nodes, viewport(FILE_LABEL_ZOOM_THRESHOLD * 0.5));

    expect(chosen).toHaveLength(2);
  });

  it("skips files outside the visible rectangle", () => {
    const view = viewport(50);
    const offscreen = candidate("far.ts", 1, 0, 5000);

    expect(selectFileLabels([offscreen], view)).toEqual([]);
  });

  it("keeps a file that is off the top but inside the wider horizontal span", () => {
    // halfWidth = halfHeight * aspect, so x has more room than y.
    const view = viewport(50);
    const wide = candidate("wide.ts", 1, 70, 0);

    expect(selectFileLabels([wide], view)).toHaveLength(1);
  });

  it("never returns more labels than the pool can draw", () => {
    const many = Array.from({ length: 500 }, (_, i) => candidate(`f${i}.ts`, 0));

    const chosen = selectFileLabels(many, viewport(FILE_LABEL_ZOOM_THRESHOLD * 0.5));

    expect(chosen.length).toBeLessThanOrEqual(MAX_FILE_LABELS);
  });

  it("gives the hottest files the slots when it has to choose", () => {
    const many = Array.from({ length: 500 }, (_, i) => candidate(`f${i}.ts`, 0));
    many.push(candidate("touched.ts", 1));

    const chosen = selectFileLabels(many, viewport(FILE_LABEL_ZOOM_THRESHOLD * 0.5));

    expect(chosen[0].path).toBe("touched.ts");
  });

  it("orders equally cold files deterministically so labels do not flicker", () => {
    const nodes = [candidate("b.ts", 0), candidate("a.ts", 0), candidate("c.ts", 0)];
    const view = viewport(FILE_LABEL_ZOOM_THRESHOLD * 0.5);

    const first = selectFileLabels(nodes, view).map((c) => c.path);
    const second = selectFileLabels([...nodes].reverse(), view).map((c) => c.path);

    expect(first).toEqual(second);
  });

  it("honours an explicit cap so a caller can shrink the pool", () => {
    const nodes = Array.from({ length: 10 }, (_, i) => candidate(`f${i}.ts`, 1));

    expect(selectFileLabels(nodes, viewport(20), 3)).toHaveLength(3);
  });
});

/** A subagent id as the daemon reports it: opaque, no structure to cut on. */
const SUBAGENT_ID = "a747fec535c143044";

/** An orchestrator's identity: the session UUID, which has no agent_type. */
const SESSION_ID = "3f7a1c9e-2b4d-4f6a-8c1e-9b7d4c2a5e01";

/** Every agent_type this project actually defines; all must survive whole. */
const REAL_AGENT_TYPES = ["desenvolvedor-backend", "desenvolvedor-frontend", "desenvolvedor-tester"];

describe("shortAgentName", () => {
  it("keeps only the tail of a session UUID, which is what tells two apart", () => {
    expect(shortAgentName(SESSION_ID)).toBe("9b7d4c2a");
  });

  it("truncates an opaque id to eight characters", () => {
    expect(shortAgentName(SUBAGENT_ID)).toBe("a747fec5");
  });

  it("leaves an already short name untouched", () => {
    expect(shortAgentName("worker")).toBe("worker");
  });

  it("names nobody when there is no agent", () => {
    // An event with agent "" must never create an actor; the shortener must not
    // hand the renderer a caption for one either.
    expect(shortAgentName("")).toBe("");
  });
});

describe("actorDisplayName", () => {
  it("captions a subagent with its agent type instead of its opaque id", () => {
    // The defect: without the label the figure reads "a747fec5".
    expect(actorDisplayName("desenvolvedor-backend", SUBAGENT_ID)).toBe("desenvolvedor-backend");
  });

  it("falls back to the shortened session id when the agent has no type", () => {
    // The orchestrator: no agent_type, so the old behaviour is still the best
    // available.
    expect(actorDisplayName("", SESSION_ID)).toBe(shortAgentName(SESSION_ID));
  });

  it("treats a blank label as no label at all", () => {
    expect(actorDisplayName("   \t\n ", SUBAGENT_ID)).toBe(shortAgentName(SUBAGENT_ID));
  });

  it("trims the padding off a label before showing it", () => {
    expect(actorDisplayName("  desenvolvedor-tester\n", SUBAGENT_ID)).toBe("desenvolvedor-tester");
  });

  it("captions nobody when there is neither a type nor an agent", () => {
    // Seeded and unattributed events carry agent "". They are real changes, but
    // nobody did them on camera, so there is no name to invent.
    expect(actorDisplayName("", "")).toBe("");
    expect(actorDisplayName("  ", "  ")).toBe("");
  });

  it("shows every agent type this project defines in full", () => {
    for (const type of REAL_AGENT_TYPES) {
      expect(actorDisplayName(type, SUBAGENT_ID)).toBe(type);
    }
  });

  it("caps the caption so a long type cannot run across the screen", () => {
    const long = "desenvolvedor-especialista-em-integracao-continua-e-deploy";

    expect(actorDisplayName(long, SUBAGENT_ID).length).toBeLessThanOrEqual(MAX_ACTOR_LABEL_CHARS);
  });

  it("keeps the head of a truncated caption, which is the part that identifies it", () => {
    const long = "desenvolvedor-especialista-em-integracao-continua-e-deploy";

    expect(actorDisplayName(long, SUBAGENT_ID).startsWith("desenvolvedor")).toBe(true);
  });

  it("leaves room for the longest agent type plus a little headroom", () => {
    // 22 chars ("desenvolvedor-frontend") is the longest name in use; a cap at
    // or below it would truncate a name this project shows every day.
    expect(MAX_ACTOR_LABEL_CHARS).toBe(24);
  });

  it("never throws, whatever the daemon or a stale client sends", () => {
    const junk = [undefined, null, 42, {}, [], true];

    for (const bad of junk) {
      for (const other of junk) {
        expect(typeof actorDisplayName(bad as unknown as string, other as unknown as string)).toBe(
          "string",
        );
      }
    }
  });
});

/** The camera framing the whole tree: too far out to name an idle file. */
const FAR_VIEW = FILE_LABEL_ZOOM_THRESHOLD * 5;

/** Close enough that idle files are named anyway. */
const NEAR_VIEW = FILE_LABEL_ZOOM_THRESHOLD * 0.5;

describe("selectFileLabels with pinned matches", () => {
  it("names a pinned file that is cold with the camera far out", () => {
    // The search defect in one line: without this, the match the user just
    // asked for is the one node on screen guaranteed to stay anonymous.
    const nodes = [candidate("src/wanted.ts", 0), candidate("src/other.ts", 0)];

    const chosen = selectFileLabels(nodes, viewport(FAR_VIEW), MAX_FILE_LABELS, new Set(["src/wanted.ts"]));

    expect(chosen.map((c) => c.path)).toEqual(["src/wanted.ts"]);
  });

  it("puts a pinned file ahead of a hotter unpinned one when slots run short", () => {
    const nodes = [candidate("touched.ts", 1), candidate("match.ts", 0)];

    const chosen = selectFileLabels(nodes, viewport(NEAR_VIEW), MAX_FILE_LABELS, new Set(["match.ts"]));

    expect(chosen[0].path).toBe("match.ts");
  });

  it("still drops a pinned file that is off screen", () => {
    // An invisible label costs a slot and buys nothing; the camera move is what
    // brings an off-screen match into view, not the label.
    const offscreen = candidate("far.ts", 0, 0, 5000);

    expect(selectFileLabels([offscreen], viewport(50), MAX_FILE_LABELS, new Set(["far.ts"]))).toEqual(
      [],
    );
  });

  it("honours the cap even when everything on screen is pinned", () => {
    // A one-letter query matches most of the project; the sprite pool does not
    // grow to meet it.
    const nodes = Array.from({ length: 10 }, (_, i) => candidate(`f${i}.ts`, 0));

    const chosen = selectFileLabels(nodes, viewport(FAR_VIEW), 3, new Set(nodes.map((c) => c.path)));

    expect(chosen).toHaveLength(3);
  });

  it("orders pinned files deterministically so search labels do not flicker", () => {
    const nodes = [candidate("b.ts", 0), candidate("a.ts", 0), candidate("c.ts", 0)];
    const pinned = new Set(["a.ts", "b.ts", "c.ts"]);

    const first = selectFileLabels(nodes, viewport(FAR_VIEW), MAX_FILE_LABELS, pinned).map((c) => c.path);
    const second = selectFileLabels([...nodes].reverse(), viewport(FAR_VIEW), MAX_FILE_LABELS, pinned).map(
      (c) => c.path,
    );

    expect(first).toEqual(second);
  });

  it("changes nothing when no file is pinned", () => {
    const nodes = [candidate("hot.ts", 1), candidate("cold.ts", 0), candidate("b.ts", 0.5)];
    const view = viewport(NEAR_VIEW);

    expect(selectFileLabels(nodes, view, MAX_FILE_LABELS, new Set())).toEqual(
      selectFileLabels(nodes, view),
    );
  });
});

describe("fileLabelOpacity with a pinned match", () => {
  const FAR = FILE_LABEL_ZOOM_THRESHOLD * 4;

  it("shows a pinned name at full strength however cold and far it is", () => {
    expect(fileLabelOpacity(0, FAR, true)).toBe(1);
  });

  it("shows a pinned name at full strength up close too, without exceeding 1", () => {
    expect(fileLabelOpacity(1, FILE_LABEL_ZOOM_THRESHOLD * 0.25, true)).toBe(1);
  });

  it("leaves an unpinned file faded exactly as before", () => {
    expect(fileLabelOpacity(0, FAR, false)).toBe(fileLabelOpacity(0, FAR));
  });
});
