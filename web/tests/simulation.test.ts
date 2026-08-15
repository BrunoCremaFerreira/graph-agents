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
  const color = type === "A" ? "33FF33" : type === "M" ? "FFAA00" : "FF3333";
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
});
