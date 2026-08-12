/**
 * Force-directed tree layout, driven by d3-force. This module owns positions
 * only -- it knows nothing about three.js. The renderer reads positions each
 * frame; the simulation model feeds it structure via {@link ForceLayout.sync}.
 *
 * Topology: every node links to its parent directory; top-level nodes link to a
 * synthetic root pinned at the origin. `forceLink` pulls children toward
 * parents, `forceManyBody` pushes everything apart, `forceCenter` keeps the
 * whole tree framed -- reproducing Gource's radial "tree breathing" look.
 */

import {
  forceCenter,
  forceLink,
  forceManyBody,
  forceSimulation,
  type Simulation as D3Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import type { SimNode } from "./simulation";

/** Synthetic root id: the pinned center all top-level nodes hang from. */
const ROOT_ID = "";

export interface LayoutNode extends SimulationNodeDatum {
  id: string;
  kind: "file" | "dir" | "root";
  parent: string;
}

type LayoutLink = SimulationLinkDatum<LayoutNode>;

export class ForceLayout {
  private readonly nodes = new Map<string, LayoutNode>();
  private readonly sim: D3Simulation<LayoutNode, LayoutLink>;
  private readonly linkForce = forceLink<LayoutNode, LayoutLink>([])
    .id((n) => n.id)
    .distance((l) => (asNode(l.target).kind === "file" ? 18 : 42))
    .strength(0.6);

  constructor() {
    const root: LayoutNode = { id: ROOT_ID, kind: "root", parent: ROOT_ID, x: 0, y: 0, fx: 0, fy: 0 };
    this.nodes.set(ROOT_ID, root);

    this.sim = forceSimulation<LayoutNode, LayoutLink>([root])
      .force("link", this.linkForce)
      .force("charge", forceManyBody<LayoutNode>().strength((n) => (n.kind === "file" ? -30 : -140)))
      .force("center", forceCenter(0, 0).strength(0.05))
      .alphaDecay(0.02)
      .velocityDecay(0.35)
      .stop();
  }

  /**
   * Reconcile the layout with the current model nodes: add newcomers (spawned
   * next to their parent so they don't fly in from the origin), drop nodes that
   * disappeared, and reheat the simulation when the topology changed.
   */
  sync(modelNodes: readonly SimNode[]): void {
    const live = new Set<string>([ROOT_ID]);
    let changed = false;

    for (const node of modelNodes) {
      live.add(node.path);
      if (this.nodes.has(node.path)) continue;
      const parent = this.nodes.get(node.parent) ?? this.nodes.get(ROOT_ID)!;
      this.nodes.set(node.path, {
        id: node.path,
        kind: node.kind,
        parent: node.parent,
        x: (parent.x ?? 0) + (Math.random() - 0.5) * 8,
        y: (parent.y ?? 0) + (Math.random() - 0.5) * 8,
      });
      changed = true;
    }

    for (const id of this.nodes.keys()) {
      if (live.has(id)) continue;
      this.nodes.delete(id);
      changed = true;
    }

    if (!changed) return;

    const nodeList = Array.from(this.nodes.values());
    const links: LayoutLink[] = [];
    for (const node of nodeList) {
      if (node.kind === "root") continue;
      const parentId = this.nodes.has(node.parent) ? node.parent : ROOT_ID;
      links.push({ source: parentId, target: node.id });
    }
    this.sim.nodes(nodeList);
    this.linkForce.links(links);
    this.sim.alpha(Math.max(this.sim.alpha(), 0.6));
  }

  /** Advance the physics one step (call once per frame). */
  tick(): void {
    this.sim.tick();
  }

  /** Current position of a node, or `undefined` if unknown. */
  position(id: string): { x: number; y: number } | undefined {
    const node = this.nodes.get(id);
    if (!node) return undefined;
    return { x: node.x ?? 0, y: node.y ?? 0 };
  }
}

/** d3 replaces link endpoints with node objects after the first tick. */
function asNode(endpoint: LayoutLink["target"]): LayoutNode {
  return endpoint as LayoutNode;
}
