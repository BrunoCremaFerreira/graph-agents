/**
 * Contract tests (RED) for the pure simulation model.
 *
 * They specify BEHAVIOR only -- the directory tree, actor registry, and fade.
 * No WebGL/three.js drawing is exercised. Expected to FAIL until
 * `developer-frontend` implements createSimulation (currently a
 * NotImplementedError stub). One failure reason per test.
 */

import { describe, it, expect } from "vitest";
import { createSimulation } from "../src/simulation";
import { AgentEvent, EventType } from "../src/protocol";

function event(
  type: EventType,
  path: string,
  agent = "sess-1",
  ts = 1000,
): AgentEvent {
  const color =
    type === "A"
      ? "33FF33"
      : type === "M"
        ? "FFAA00"
        : type === "R"
          ? "AA66FF"
          : "FF3333";
  // `origin` distinguishes live agent activity from the seeded project tree;
  // these tests are all about live activity, hence "hook".
  // `label` is display text only; the model keys actors off `agent`.
  return { ts, agent, type, path, color, origin: "hook", label: "" };
}

describe("simulation model", () => {
  it("materializes ancestor directories when a file is added", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "src/api/users.ts"));

    expect(sim.getNode("src")?.kind).toBe("dir");
    expect(sim.getNode("src/api")?.kind).toBe("dir");
    expect(sim.getNode("src/api/users.ts")?.kind).toBe("file");
  });

  it("removes the node on a delete event", () => {
    const sim = createSimulation();
    sim.applyEvent(event("A", "src/gone.ts"));

    sim.applyEvent(event("D", "src/gone.ts"));

    expect(sim.hasNode("src/gone.ts")).toBe(false);
  });

  it("registers a new actor for a never-seen agent", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "a.ts", "worker-7"));

    expect(sim.getActor("worker-7")).toBeDefined();
    expect(sim.getActor("worker-7")?.agent).toBe("worker-7");
  });

  it("brings an actor to full intensity right after its event", () => {
    const sim = createSimulation();

    sim.applyEvent(event("M", "a.ts", "worker-7"));

    expect(sim.getActor("worker-7")?.intensity).toBeCloseTo(1, 5);
  });

  it("fades an idle actor's intensity as time advances", () => {
    const sim = createSimulation();
    sim.applyEvent(event("M", "a.ts", "worker-7"));
    const before = sim.getActor("worker-7")!.intensity;

    sim.tick(5);

    const after = sim.getActor("worker-7")!.intensity;
    expect(after).toBeLessThan(before);
  });
});

/**
 * The observed root is about to become switchable from the page (ctrl+L). When
 * it changes the daemon sends a `reset` frame and then re-seeds the whole new
 * tree -- so the model has to be emptied first. Without this the two projects
 * are drawn as one graph: the old files never disappear (nothing deletes them),
 * they hang off directories the new root does not have, and the figures of
 * agents that worked in the old checkout keep standing there. Reset is also the
 * only way to forget a path, which matters because a path already in the tree is
 * refreshed rather than created, and a file with the same relative path in the
 * new project must enter as a new node.
 */
describe("simulation reset", () => {
  it("empties the tree, so nothing from the old project is left on screen", () => {
    const sim = createSimulation();
    sim.applyEvent(event("A", "src/api/users.ts"));
    sim.applyEvent(event("A", "README.md"));

    sim.reset();

    expect(sim.listNodes()).toEqual([]);
  });

  it("forgets every actor, so a figure from the old project stops posing", () => {
    const sim = createSimulation();
    sim.applyEvent(event("M", "a.ts", "worker-7"));

    sim.reset();

    expect(sim.listActors()).toEqual([]);
  });

  it("rebuilds a path it had already seen instead of treating it as still known", () => {
    // Same relative path, different project: it has to be created from scratch,
    // ancestors included, not refreshed in place from the old tree.
    const sim = createSimulation();
    sim.applyEvent(event("A", "src/api/users.ts"));
    sim.reset();

    sim.applyEvent(event("A", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.kind).toBe("file");
    expect(sim.getNode("src/api")?.kind).toBe("dir");
  });

  it("is harmless on a simulation that has seen nothing yet", () => {
    // The page may reset before the first frame arrives (a switch requested
    // while the socket was reconnecting).
    const sim = createSimulation();

    expect(() => sim.reset()).not.toThrow();
  });

  it("forgets a file that was only ever read, like any other node", () => {
    const sim = createSimulation();
    sim.applyEvent(event("R", "src/api/users.ts"));

    sim.reset();

    expect(sim.listNodes()).toEqual([]);
    expect(sim.hasNode("src/api/users.ts")).toBe(false);
  });
});

/**
 * Reading is the fourth operation (`R`, violet `AA66FF`), and it needs a decay
 * channel of its OWN -- `reading`, alongside `highlight`.
 *
 * The defect that forces the separation: an agent reads roughly ten times more
 * often than it writes, and it very often reads a file it has just edited. If a
 * read reused `highlight` and `color`, the amber flash of a write half a second
 * old would be repainted violet by the agent re-reading its own output, and the
 * one thing the graph exists to show -- who changed what -- would be erased by
 * the noisiest event on the wire. A read is not a change: it lights its own
 * channel, it never touches the write's, and it fades slower because reading is
 * a slower, more sustained act than a save.
 *
 * The other half is that a read must not MASQUERADE as a write. A file the tree
 * has never seen still has to appear when someone reads it (it is real, and it
 * is being looked at), but cold: no highlight, no author's flash.
 */
describe("simulation: reading is a channel of its own", () => {
  it("gives a written file a reading level of 0, because a write is not a read", () => {
    const sim = createSimulation();

    sim.applyEvent(event("M", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.reading).toBe(0);
  });

  it("gives a directory a reading level of 0, since only files are read", () => {
    const sim = createSimulation();

    sim.applyEvent(event("A", "src/api/users.ts"));

    expect(sim.getNode("src/api")?.reading).toBe(0);
  });

  it("gives a seeded file a reading level of 0, so the tree does not open violet", () => {
    const sim = createSimulation();

    sim.applyEvent({ ...event("A", "src/api/users.ts", ""), origin: "seed" });

    expect(sim.getNode("src/api/users.ts")?.reading).toBe(0);
  });

  it("raises reading to 1 on a file the tree already knows", () => {
    const sim = createSimulation();
    sim.applyEvent(event("A", "src/api/users.ts"));

    sim.applyEvent(event("R", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.reading).toBeCloseTo(1, 5);
  });

  it("brings a file faded out by idle decay back to full opacity when it is read", () => {
    const sim = createSimulation();
    sim.applyEvent(event("A", "src/api/users.ts"));
    sim.tick(200);
    expect(sim.getNode("src/api/users.ts")?.opacity).toBe(0);

    sim.applyEvent(event("R", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.opacity).toBeCloseTo(1, 5);
  });

  it("leaves the highlight of a write untouched when the agent reads back what it wrote", () => {
    const sim = createSimulation();
    sim.applyEvent(event("M", "src/api/users.ts"));

    sim.applyEvent(event("R", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.highlight).toBeCloseTo(1, 5);
  });

  it("leaves the colour of a write untouched, so the amber of an edit is not repainted violet", () => {
    const sim = createSimulation();
    sim.applyEvent(event("M", "src/api/users.ts"));

    sim.applyEvent(event("R", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.color).toBe("FFAA00");
  });

  it("leaves reading alone when the file is written afterwards", () => {
    // The mirror of the rule above: the two channels are independent in both
    // directions, so a save does not extinguish the violet of an open file.
    const sim = createSimulation();
    sim.applyEvent(event("R", "src/api/users.ts"));

    sim.applyEvent(event("M", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.reading).toBeCloseTo(1, 5);
  });
});

describe("simulation: reading a file the tree has never seen", () => {
  it("materializes the ancestor directories, exactly as a write does", () => {
    const sim = createSimulation();

    sim.applyEvent(event("R", "src/api/users.ts"));

    expect(sim.getNode("src")?.kind).toBe("dir");
    expect(sim.getNode("src/api")?.kind).toBe("dir");
  });

  it("creates the target as a file node", () => {
    const sim = createSimulation();

    sim.applyEvent(event("R", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.kind).toBe("file");
  });

  it("enters it cold, with no highlight, because nobody changed it", () => {
    const sim = createSimulation();

    sim.applyEvent(event("R", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.highlight).toBe(0);
  });

  it("enters it visible and being read: full opacity, reading at 1", () => {
    const sim = createSimulation();

    sim.applyEvent(event("R", "src/api/users.ts"));

    expect(sim.getNode("src/api/users.ts")?.opacity).toBeCloseTo(1, 5);
    expect(sim.getNode("src/api/users.ts")?.reading).toBeCloseTo(1, 5);
  });
});

describe("simulation: the reading fade", () => {
  it("dims reading as the file goes unread", () => {
    const sim = createSimulation();
    sim.applyEvent(event("R", "src/api/users.ts"));

    sim.tick(0.2);

    const reading = sim.getNode("src/api/users.ts")!.reading;
    expect(reading).toBeGreaterThan(0);
    expect(reading).toBeLessThan(1);
  });

  it("clamps reading at 0 instead of letting it go negative", () => {
    const sim = createSimulation();
    sim.applyEvent(event("R", "src/api/users.ts"));

    sim.tick(60);

    expect(sim.getNode("src/api/users.ts")?.reading).toBe(0);
  });

  it("fades reading more slowly than the highlight of a write", () => {
    // Reading is a sustained act, not an instant one: the violet lingers while
    // the agent works through the file, where a write's flash is a blink. The
    // ordering is specified behaviourally so the rates stay tunable.
    const sim = createSimulation();
    sim.applyEvent(event("M", "src/api/users.ts"));
    sim.applyEvent(event("R", "src/api/users.ts"));

    sim.tick(1);

    const node = sim.getNode("src/api/users.ts")!;
    expect(node.reading).toBeGreaterThan(node.highlight);
  });
});

describe("simulation: who gets credited for a read", () => {
  it("creates no actor for a read nobody could be credited for", () => {
    // Same rule as every other event: an empty agent must never put a figure on
    // screen. The watcher cannot see a read at all, but a hook payload that lost
    // its ids still arrives with `agent: ""`.
    const sim = createSimulation();

    sim.applyEvent(event("R", "src/api/users.ts", ""));

    expect(sim.listActors()).toEqual([]);
  });

  it("brings the reader's actor to full intensity, like any other event", () => {
    const sim = createSimulation();

    sim.applyEvent(event("R", "src/api/users.ts", "worker-7"));

    expect(sim.getActor("worker-7")?.intensity).toBeCloseTo(1, 5);
  });
});
