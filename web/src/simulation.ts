/**
 * Pure simulation model: the directory tree, the per-agent actors, and the
 * time-based fade. This layer is deliberately independent of the WebGL/three.js
 * drawing layer so it can be unit-tested for BEHAVIOR only.
 *
 * Nothing here imports three.js or touches the DOM: it is a plain in-memory
 * state machine driven by `applyEvent` (discrete events) and `tick` (continuous
 * time). The renderer reads this state each frame; it never mutates it.
 */

import { AgentEvent } from "./protocol";

/** A node in the visualized directory tree. */
export type NodeKind = "file" | "dir";

export interface SimNode {
  /** Path relative to the project root (`""` is reserved for the root). */
  path: string;
  kind: NodeKind;
  /** Immediate parent path (`""` for top-level entries). */
  parent: string;
  /**
   * Highlight level in [0, 1]: ~1 right after the file is touched, decaying to 0
   * as it stays idle. Directories keep this at 0 (they are structural).
   */
  highlight: number;
  /** Visibility in [0, 1]: files fade toward 0 while idle. 1 means fully present. */
  opacity: number;
  /** Hex color (no `#`) carried from the last event that touched the node. */
  color: string;
  /**
   * Reading level in [0, 1]: ~1 right after an agent read the file, decaying to
   * 0 (more slowly than {@link highlight}) as nobody looks at it again.
   *
   * A CHANNEL OF ITS OWN, deliberately. An agent reads roughly ten times more
   * often than it writes, and very often reads back the file it has just
   * edited: were a read to reuse `highlight`/`color`, the amber of that write
   * would be repainted violet half a second later, and the one thing the graph
   * exists to show — who changed what — would be erased by the noisiest event
   * on the wire. Directories keep this at 0: only files are read.
   */
  reading: number;
}

/** On-screen actor derived from an event's `agent`. */
export interface Actor {
  agent: string;
  /**
   * Activity level in [0, 1]: ~1 right after an event, decaying toward 0 as the
   * actor stays idle across {@link Simulation.tick} calls (fade).
   */
  intensity: number;
}

/**
 * The observable, drawing-agnostic simulation state.
 */
export interface Simulation {
  /** Apply one event: mutate the tree and register/refresh the actor. */
  applyEvent(event: AgentEvent): void;
  /** Advance time by `dtSeconds`, decaying idle actors (and node highlights). */
  tick(dtSeconds: number): void;
  /**
   * Forget everything: the whole tree AND every actor.
   *
   * The observed root can be switched from the page, and the daemon answers by
   * re-seeding a different project. Without this the two are drawn as one graph:
   * the old files never disappear (nothing deletes them), they hang off
   * directories the new root does not have, and figures of agents that worked in
   * the old checkout keep standing there. It is also the only way to forget a
   * path, which matters because a known path is REFRESHED rather than created —
   * a file with the same relative name in the new project has to enter as a new
   * node, ancestors included.
   */
  reset(): void;

  /** Whether a node exists at `path`. */
  hasNode(path: string): boolean;
  /** The node at `path`, or `undefined`. */
  getNode(path: string): SimNode | undefined;

  /** The actor for `agent`, or `undefined` if never seen. */
  getActor(agent: string): Actor | undefined;
  /** All known actors. */
  listActors(): Actor[];

  /** All live nodes (files and directories). Read-only view for the renderer. */
  listNodes(): SimNode[];
}

/**
 * Fade rates, in units-per-second. Chosen to mirror Gource's slow idle decay:
 * an untouched actor/file dims gently rather than snapping off.
 */
const ACTOR_DECAY_PER_SEC = 0.08;
const HIGHLIGHT_DECAY_PER_SEC = 0.9;
const FILE_OPACITY_DECAY_PER_SEC = 0.03;
/**
 * Strictly slower than {@link HIGHLIGHT_DECAY_PER_SEC}: reading is a sustained
 * act, not an instant one, and reads arrive in bursts. A write's flash is a
 * blink; the violet has to linger while the agent works through the file, or a
 * burst of reads is a strobe nobody can follow.
 */
const READING_DECAY_PER_SEC = 0.5;

/**
 * Neutral colour for a node that no event has ever flashed — a directory, or a
 * file that entered the tree because somebody READ it. `color` is the flash
 * colour, and it is only ever mixed in proportion to `highlight`, which is 0 for
 * both; a read must not leave an author's colour behind on a file nobody wrote.
 */
const NEUTRAL_COLOR = "888888";

/** Split a path into its ancestor directories, innermost last. */
function ancestorDirs(path: string): string[] {
  const parts = path.split("/").filter((p) => p.length > 0);
  const dirs: string[] = [];
  for (let i = 0; i < parts.length - 1; i += 1) {
    dirs.push(parts.slice(0, i + 1).join("/"));
  }
  return dirs;
}

/** Parent path of a slash-delimited path (`""` for a top-level entry). */
function parentOf(path: string): string {
  const idx = path.lastIndexOf("/");
  return idx === -1 ? "" : path.slice(0, idx);
}

function clamp01(value: number): number {
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

class SimulationImpl implements Simulation {
  private readonly nodes = new Map<string, SimNode>();
  private readonly actors = new Map<string, Actor>();

  applyEvent(event: AgentEvent): void {
    // An empty agent means nobody could be credited: the project tree the
    // daemon seeds at boot, or a change on disk with no hook around it. Making
    // an actor for it would put a figure and a beam on screen for work no agent
    // did.
    if (event.agent) this.touchActor(event.agent);

    if (event.type === "D") {
      this.markDeleted(event.path);
      return;
    }

    // A, M or R: materialize every ancestor directory, then the file itself.
    // A read reaches the same tree — a file being looked at is real and has to
    // appear — but through its own branch below.
    for (const dir of ancestorDirs(event.path)) {
      this.ensureDir(dir);
    }

    if (event.type === "R") {
      this.readFile(event.path);
      return;
    }

    this.touchFile(event.path, event.color, event.origin === "seed");
  }

  tick(dtSeconds: number): void {
    if (dtSeconds <= 0) return;

    for (const actor of this.actors.values()) {
      actor.intensity = clamp01(actor.intensity - ACTOR_DECAY_PER_SEC * dtSeconds);
    }

    for (const node of this.nodes.values()) {
      if (node.kind !== "file") continue;
      node.highlight = clamp01(node.highlight - HIGHLIGHT_DECAY_PER_SEC * dtSeconds);
      node.reading = clamp01(node.reading - READING_DECAY_PER_SEC * dtSeconds);
      node.opacity = clamp01(node.opacity - FILE_OPACITY_DECAY_PER_SEC * dtSeconds);
    }
  }

  reset(): void {
    this.nodes.clear();
    this.actors.clear();
  }

  hasNode(path: string): boolean {
    return this.nodes.has(path);
  }

  getNode(path: string): SimNode | undefined {
    return this.nodes.get(path);
  }

  getActor(agent: string): Actor | undefined {
    return this.actors.get(agent);
  }

  listActors(): Actor[] {
    return Array.from(this.actors.values());
  }

  listNodes(): SimNode[] {
    return Array.from(this.nodes.values());
  }

  private touchActor(agent: string): void {
    const existing = this.actors.get(agent);
    if (existing) {
      existing.intensity = 1;
      return;
    }
    this.actors.set(agent, { agent, intensity: 1 });
  }

  private ensureDir(path: string): void {
    if (this.nodes.has(path)) return;
    this.nodes.set(path, {
      path,
      kind: "dir",
      parent: parentOf(path),
      highlight: 0,
      opacity: 1,
      color: NEUTRAL_COLOR,
      reading: 0,
    });
  }

  /**
   * Add or refresh a file node.
   *
   * A seeded file was already on disk before anyone connected, so it enters the
   * tree cold: no highlight, and at the dim baseline the renderer gives idle
   * files. Otherwise the whole repository would flare up at boot as if every
   * file had just been written.
   */
  private touchFile(path: string, color: string, seeded = false): void {
    const highlight = seeded ? 0 : 1;
    const opacity = seeded ? 0 : 1;
    const existing = this.nodes.get(path);
    if (existing) {
      existing.kind = "file";
      existing.highlight = highlight;
      existing.opacity = opacity;
      existing.color = color;
      // `reading` is deliberately untouched: the two channels are independent in
      // BOTH directions, so saving a file the agent still has open does not
      // extinguish its violet.
      return;
    }
    this.nodes.set(path, {
      path,
      kind: "file",
      parent: parentOf(path),
      highlight,
      opacity,
      color,
      reading: 0,
    });
  }

  /**
   * Mark a file as being read.
   *
   * Everything here is about what it does NOT do. It never goes through
   * {@link touchFile}: `highlight` and `color` are left exactly as they were, so
   * a read cannot repaint the flash of a write that is half a second old. It
   * raises `opacity` because a file dimmed by idle decay has become interesting
   * again — somebody is looking at it — and a file the tree has never seen
   * enters COLD: it is real, but nobody changed it, so it gets no flash and no
   * author's colour.
   */
  private readFile(path: string): void {
    const existing = this.nodes.get(path);
    if (existing) {
      existing.kind = "file";
      existing.reading = 1;
      existing.opacity = 1;
      return;
    }
    this.nodes.set(path, {
      path,
      kind: "file",
      parent: parentOf(path),
      highlight: 0,
      opacity: 1,
      color: NEUTRAL_COLOR,
      reading: 1,
    });
  }

  private markDeleted(path: string): void {
    // Removed immediately from the logical tree (tests assert absence); the
    // renderer plays its own shrink animation off the last known node.
    this.nodes.delete(path);
  }
}

/**
 * Create an empty simulation.
 *
 * Contract (see tests/simulation.test.ts):
 *   - Applying an `A`/`M` event materializes the file node AND every ancestor
 *     directory node.
 *   - Applying a `D` event removes the target node.
 *   - A never-seen `agent` produces a new actor at full intensity.
 *   - `tick` after inactivity strictly reduces an actor's intensity (fade).
 */
export function createSimulation(): Simulation {
  return new SimulationImpl();
}
