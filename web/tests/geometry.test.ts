/**
 * Contract tests (RED) for the node/edge geometry factories.
 *
 * Regression: the renderer allocated its buffer attributes only inside
 * `rebuildNodeBuffers`, which runs only when the topology *changes*. On the
 * first frame the graph is still empty (0 nodes vs 0 known), so no rebuild
 * happened, and the attribute writer dereferenced a missing attribute:
 *
 *     TypeError: can't access property "array", t is undefined
 *
 * Thrown inside the requestAnimationFrame callback, before the call that
 * schedules the next frame, this killed the render loop permanently -- the
 * page stayed black even once events arrived.
 *
 * The invariant: geometry always carries its attributes, including at size 0.
 * Expected to FAIL until src/geometry.ts exists. One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { BufferAttribute } from "three";
import { createNodeGeometry, createEdgeGeometry } from "../src/geometry";

function attr(geometry: { getAttribute(name: string): unknown }, name: string): BufferAttribute {
  return geometry.getAttribute(name) as BufferAttribute;
}

describe("createNodeGeometry", () => {
  it("allocates every attribute for an empty graph", () => {
    const geometry = createNodeGeometry(0);

    expect(attr(geometry, "position")).toBeDefined();
    expect(attr(geometry, "aColor")).toBeDefined();
    expect(attr(geometry, "aSize")).toBeDefined();
  });

  it("exposes a usable array on an empty graph rather than undefined", () => {
    // This is the exact dereference that used to throw.
    const geometry = createNodeGeometry(0);

    expect(attr(geometry, "position").array).toBeInstanceOf(Float32Array);
    expect(attr(geometry, "position").array).toHaveLength(0);
  });

  it("sizes position and colour as 3 floats per node, size as 1", () => {
    const geometry = createNodeGeometry(4);

    expect(attr(geometry, "position").array).toHaveLength(12);
    expect(attr(geometry, "aColor").array).toHaveLength(12);
    expect(attr(geometry, "aSize").array).toHaveLength(4);
  });

  it("declares the right item size per attribute", () => {
    const geometry = createNodeGeometry(2);

    expect(attr(geometry, "position").itemSize).toBe(3);
    expect(attr(geometry, "aColor").itemSize).toBe(3);
    expect(attr(geometry, "aSize").itemSize).toBe(1);
  });
});

describe("createEdgeGeometry", () => {
  it("allocates a position attribute for an empty edge set", () => {
    const geometry = createEdgeGeometry(0);

    expect(attr(geometry, "position")).toBeDefined();
    expect(attr(geometry, "position").array).toHaveLength(0);
  });

  it("allocates two endpoints of 3 floats per edge", () => {
    const geometry = createEdgeGeometry(5);

    expect(attr(geometry, "position").array).toHaveLength(30);
  });
});
