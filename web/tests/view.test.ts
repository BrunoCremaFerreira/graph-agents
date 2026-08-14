/**
 * Contract tests (RED) for the interactive view state (zoom + pan).
 *
 * The camera auto-fits the whole graph every frame, which makes directory and
 * file labels unreadable once the tree grows. These specify a view state the
 * user can drive: wheel to zoom, drag to pan, and a release back to auto-fit.
 *
 * The property that matters for usability is zoom-under-cursor: the world point
 * beneath the pointer must not drift while zooming, otherwise the thing you are
 * trying to read slides off screen. Expected to FAIL until src/view.ts exists.
 *
 * A second defect motivates the `focusOn` group at the bottom. Search has to be
 * able to fly the camera to a match, and `follow` cannot do it: `follow` is
 * auto-fit, so it deliberately does nothing once `manual` is set -- which is
 * exactly the state the camera is in after the user zoomed in to look for the
 * file they are now searching for. Worse, easing there while leaving `manual`
 * false would let the next frame's auto-fit drag the view straight back off the
 * match. Focusing is a direct order from the user: it moves regardless, and it
 * takes manual control on the way out.
 */

import { describe, it, expect } from "vitest";
import {
  createView,
  zoomAt,
  panByPixels,
  follow,
  focusOn,
  releaseToAuto,
  MIN_HALF_HEIGHT,
  MAX_HALF_HEIGHT,
  type ViewState,
} from "../src/view";

const ASPECT = 16 / 9;

/** World point currently under a pointer given in NDC (y up). */
function worldUnderPointer(view: ViewState, ndc: { x: number; y: number }, aspect: number) {
  return {
    x: view.centerX + ndc.x * view.halfHeight * aspect,
    y: view.centerY + ndc.y * view.halfHeight,
  };
}

describe("zoomAt", () => {
  it("zooms in by shrinking the visible half-height", () => {
    const zoomed = zoomAt(createView(100), 0.5, { x: 0, y: 0 }, ASPECT);

    expect(zoomed.halfHeight).toBeCloseTo(50);
  });

  it("zooms out by growing the visible half-height", () => {
    const zoomed = zoomAt(createView(100), 2, { x: 0, y: 0 }, ASPECT);

    expect(zoomed.halfHeight).toBeCloseTo(200);
  });

  it("leaves the centre alone when zooming at the middle of the screen", () => {
    const view = { centerX: 7, centerY: -3, halfHeight: 100, manual: true };

    const zoomed = zoomAt(view, 0.5, { x: 0, y: 0 }, ASPECT);

    expect(zoomed.centerX).toBeCloseTo(7);
    expect(zoomed.centerY).toBeCloseTo(-3);
  });

  it("keeps the world point under the cursor fixed", () => {
    const view = createView(100);
    const pointer = { x: 0.6, y: -0.4 };
    const before = worldUnderPointer(view, pointer, ASPECT);

    const after = worldUnderPointer(zoomAt(view, 0.25, pointer, ASPECT), pointer, ASPECT);

    expect(after.x).toBeCloseTo(before.x);
    expect(after.y).toBeCloseTo(before.y);
  });

  it("refuses to zoom in past the minimum half-height", () => {
    const zoomed = zoomAt(createView(MIN_HALF_HEIGHT), 0.01, { x: 0, y: 0 }, ASPECT);

    expect(zoomed.halfHeight).toBeGreaterThanOrEqual(MIN_HALF_HEIGHT);
  });

  it("refuses to zoom out past the maximum half-height", () => {
    const zoomed = zoomAt(createView(MAX_HALF_HEIGHT), 100, { x: 0, y: 0 }, ASPECT);

    expect(zoomed.halfHeight).toBeLessThanOrEqual(MAX_HALF_HEIGHT);
  });

  it("takes the camera off auto-fit so it stops fighting the user", () => {
    expect(zoomAt(createView(100), 0.5, { x: 0, y: 0 }, ASPECT).manual).toBe(true);
  });
});

describe("panByPixels", () => {
  const VIEWPORT = { width: 1920, height: 1000 };

  it("moves the camera opposite the drag so content follows the cursor", () => {
    const panned = panByPixels(createView(100), 100, 0, VIEWPORT);

    // 100px of a 1000px-tall viewport showing 200 world units => 20 units.
    expect(panned.centerX).toBeCloseTo(-20);
  });

  it("maps a downward drag to an upward camera move", () => {
    const panned = panByPixels(createView(100), 0, 50, VIEWPORT);

    expect(panned.centerY).toBeCloseTo(10);
  });

  it("scales the pan with the zoom level", () => {
    const zoomedIn = panByPixels(createView(10), 100, 0, VIEWPORT);

    expect(zoomedIn.centerX).toBeCloseTo(-2);
  });

  it("takes the camera off auto-fit", () => {
    expect(panByPixels(createView(100), 10, 10, VIEWPORT).manual).toBe(true);
  });
});

describe("follow", () => {
  const TARGET = { centerX: 100, centerY: 100, halfHeight: 200 };

  it("eases towards the target while on auto-fit", () => {
    const followed = follow(createView(100), TARGET, 0.5);

    expect(followed.centerX).toBeCloseTo(50);
    expect(followed.halfHeight).toBeCloseTo(150);
  });

  it("does not move a manually positioned camera", () => {
    const manual = zoomAt(createView(100), 0.5, { x: 0, y: 0 }, ASPECT);

    const followed = follow(manual, TARGET, 0.5);

    expect(followed).toEqual(manual);
  });
});

describe("focusOn", () => {
  const TARGET = { centerX: 100, centerY: 100, halfHeight: 200 };

  it("eases towards the target instead of cutting to it", () => {
    const focused = focusOn(createView(100), TARGET, 0.5);

    expect(focused.centerX).toBeCloseTo(50);
    expect(focused.centerY).toBeCloseTo(50);
    expect(focused.halfHeight).toBeCloseTo(150);
  });

  it("moves a manually positioned camera, because the user asked for this one", () => {
    // The whole difference from `follow`: a search hit must reach the screen
    // even though the user had already taken the camera over by hand.
    const manual = zoomAt(createView(100), 0.5, { x: 0, y: 0 }, ASPECT);

    const focused = focusOn(manual, TARGET, 0.5);

    expect(focused.centerX).toBeCloseTo(50);
  });

  it("keeps manual control so auto-fit does not pull the camera back next frame", () => {
    const auto = createView(100);

    expect(focusOn(auto, TARGET, 0.5).manual).toBe(true);
  });

  it("arrives exactly on the target at full ease", () => {
    const focused = focusOn(createView(100), TARGET, 1);

    expect(focused.centerX).toBeCloseTo(TARGET.centerX);
    expect(focused.centerY).toBeCloseTo(TARGET.centerY);
    expect(focused.halfHeight).toBeCloseTo(TARGET.halfHeight);
  });

  it("refuses to close in past the minimum half-height", () => {
    // A single matched file gives a target of nearly zero size; unclamped, the
    // camera dives inside the bloom and the screen goes white.
    const focused = focusOn(createView(100), { centerX: 0, centerY: 0, halfHeight: 0.01 }, 1);

    expect(focused.halfHeight).toBeGreaterThanOrEqual(MIN_HALF_HEIGHT);
  });

  it("refuses to pull back past the maximum half-height", () => {
    const focused = focusOn(
      createView(100),
      { centerX: 0, centerY: 0, halfHeight: MAX_HALF_HEIGHT * 10 },
      1,
    );

    expect(focused.halfHeight).toBeLessThanOrEqual(MAX_HALF_HEIGHT);
  });
});

describe("releaseToAuto", () => {
  it("hands control back to auto-fit", () => {
    const manual = panByPixels(createView(100), 50, 50, { width: 800, height: 600 });

    expect(releaseToAuto(manual).manual).toBe(false);
  });

  it("keeps the current framing so the release is not a jump cut", () => {
    const manual = panByPixels(createView(100), 50, 50, { width: 800, height: 600 });

    const released = releaseToAuto(manual);

    expect(released.centerX).toBeCloseTo(manual.centerX);
    expect(released.halfHeight).toBeCloseTo(manual.halfHeight);
  });
});
