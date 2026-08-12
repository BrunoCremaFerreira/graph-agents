/**
 * Composition root. Wires the three layers together and starts them:
 *   network (WsClient) -> model (Simulation) -> drawing (GourceRenderer)
 * Each event validated on the wire is applied to the pure model and announced
 * to the renderer for its beam/flash effect. Nothing here holds domain logic.
 */

import "./style.css";
import { createSimulation } from "./simulation";
import { createRenderer } from "./renderer";
import { createWsClient, resolveWsUrl } from "./wsClient";

function boot(): void {
  const canvas = document.getElementById("stage") as HTMLCanvasElement | null;
  if (!canvas) throw new Error("missing #stage canvas");

  const sim = createSimulation();
  const renderer = createRenderer(canvas, sim);

  const client = createWsClient((event) => {
    sim.applyEvent(event);
    renderer.onEvent(event);
  }, resolveWsUrl());

  window.addEventListener("resize", () => renderer.resize());
  renderer.resize();
  renderer.start();
  client.connect();
}

boot();
