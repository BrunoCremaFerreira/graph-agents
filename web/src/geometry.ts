/**
 * Buffer geometry allocation for the point cloud and the directory edges.
 *
 * Kept apart from the renderer so the sizing rules are testable without a GL
 * context, and so the geometry can never exist in a half-built state: every
 * geometry returned here already carries all of its attributes, including at
 * size 0. The renderer's per-frame writers therefore always find an attribute
 * to write into, even on the first frame when the graph is still empty.
 */

import { BufferAttribute, BufferGeometry } from "three";

/**
 * (Re)allocate the node attributes on `geometry` for `nodeCount` points.
 *
 * Mutates in place and returns the same object: the `Points` mesh holds this
 * reference, so growing the graph must never swap the geometry out.
 */
export function allocateNodeAttributes(
  geometry: BufferGeometry,
  nodeCount: number,
): BufferGeometry {
  geometry.setAttribute("position", new BufferAttribute(new Float32Array(nodeCount * 3), 3));
  geometry.setAttribute("aColor", new BufferAttribute(new Float32Array(nodeCount * 3), 3));
  geometry.setAttribute("aSize", new BufferAttribute(new Float32Array(nodeCount), 1));
  return geometry;
}

/** As {@link allocateNodeAttributes}, for `edgeCount` two-endpoint segments. */
export function allocateEdgeAttributes(
  geometry: BufferGeometry,
  edgeCount: number,
): BufferGeometry {
  geometry.setAttribute("position", new BufferAttribute(new Float32Array(edgeCount * 2 * 3), 3));
  return geometry;
}

/** Geometry for `nodeCount` node points: position + colour + per-point size. */
export function createNodeGeometry(nodeCount: number): BufferGeometry {
  return allocateNodeAttributes(new BufferGeometry(), nodeCount);
}

/** Geometry for `edgeCount` line segments (two endpoints of 3 floats each). */
export function createEdgeGeometry(edgeCount: number): BufferGeometry {
  return allocateEdgeAttributes(new BufferGeometry(), edgeCount);
}
