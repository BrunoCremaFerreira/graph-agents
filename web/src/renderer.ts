/**
 * WebGL drawing layer (three.js). Reads the pure {@link Simulation} model and
 * the {@link ForceLayout} positions each frame and paints the Gource look:
 * a black field, thin directory edges, glowing colored file dots, directory
 * labels, and per-agent actors that fire animated beams at the files they touch.
 *
 * This layer owns NO domain state -- it renders what the model says and plays
 * transient visual effects (beams, flashes) on top. The hot path allocates
 * nothing in steady state; buffers are rebuilt only when the tree's topology
 * changes.
 */

import {
  AdditiveBlending,
  BufferAttribute,
  BufferGeometry,
  CanvasTexture,
  Color,
  LineBasicMaterial,
  LineSegments,
  OrthographicCamera,
  Points,
  Scene,
  ShaderMaterial,
  Sprite,
  SpriteMaterial,
  Vector2,
  WebGLRenderer,
} from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import type { AgentEvent } from "./protocol";
import type { SimNode, Simulation } from "./simulation";
import { ForceLayout } from "./layout";
import { createAvatarCanvas } from "./avatar";
import { fileColor, hashColor, hexToInt } from "./colors";
import {
  allocateEdgeAttributes,
  allocateNodeAttributes,
  createEdgeGeometry,
  createNodeGeometry,
} from "./geometry";
import {
  createView,
  follow,
  panByPixels,
  releaseToAuto,
  zoomAt,
  type ViewState,
} from "./view";

/** A transient animated line from an actor to a file it just touched. */
interface Beam {
  actor: string;
  target: string;
  color: number;
  age: number;
  life: number;
}

/** Eased on-screen position, figure and label for an agent. */
interface ActorView {
  agent: string;
  color: number;
  x: number;
  y: number;
  hasPos: boolean;
  /** The Gource-style figure that walks the tree. */
  figure: Sprite;
  label: Sprite;
}

const MAX_BEAMS = 512;
const BEAM_LIFE_SECONDS = 1.2;
const DIR_COLOR = 0x9aa0a6;
/** Height of the agent figure in world units (a file dot is a few px wide). */
const AVATAR_WORLD_HEIGHT = 7;

/** Per-point shader: per-vertex size (px) + color, soft circular alpha. */
const POINT_VERTEX = /* glsl */ `
  attribute float aSize;
  attribute vec3 aColor;
  varying vec3 vColor;
  void main() {
    vColor = aColor;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = aSize;
  }
`;
const POINT_FRAGMENT = /* glsl */ `
  varying vec3 vColor;
  void main() {
    vec2 d = gl_PointCoord - vec2(0.5);
    float r = length(d) * 2.0;
    float alpha = smoothstep(1.0, 0.15, r);
    if (alpha <= 0.01) discard;
    gl_FragColor = vec4(vColor, alpha);
  }
`;

export class GourceRenderer {
  private readonly renderer: WebGLRenderer;
  private readonly scene = new Scene();
  private readonly camera: OrthographicCamera;
  private readonly composer: EffectComposer;
  private readonly bloom: UnrealBloomPass;
  private readonly layout = new ForceLayout();

  private readonly nodePoints: Points;
  // Allocated empty (not bare) so the first frame -- which runs before any
  // event arrives, and therefore triggers no rebuild -- still finds attributes.
  private readonly nodeGeom = createNodeGeometry(0);
  private readonly edges: LineSegments;
  private readonly edgeGeom = createEdgeGeometry(0);
  private readonly beamLines: LineSegments;
  private readonly beamGeom = new BufferGeometry();
  private dragPointer: number | null = null;
  private dragX = 0;
  private dragY = 0;
  private readonly reportedFrameErrors = new Set<string>();
  private readonly beamPos = new Float32Array(MAX_BEAMS * 2 * 3);
  private readonly beamColor = new Float32Array(MAX_BEAMS * 2 * 3);

  private readonly nodeIndex = new Map<string, number>();
  private nodeIds: string[] = [];
  private edgeChild: string[] = [];
  private edgeParent: string[] = [];

  private readonly actors = new Map<string, ActorView>();
  private readonly dirLabels = new Map<string, Sprite>();
  private readonly beams: Beam[] = [];

  private view: ViewState = createView(60);
  private lastTime = 0;
  private running = false;
  private readonly scratchColor = new Color();

  constructor(private readonly canvas: HTMLCanvasElement, private readonly sim: Simulation) {
    this.renderer = new WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setClearColor(0x000000, 1);
    this.scene.background = new Color(0x000000);

    const aspect = canvas.clientWidth / Math.max(1, canvas.clientHeight);
    const half = this.view.halfHeight;
    this.camera = new OrthographicCamera(-half * aspect, half * aspect, half, -half, 0.1, 1000);
    this.camera.position.set(0, 0, 100);

    const pointMaterial = new ShaderMaterial({
      vertexShader: POINT_VERTEX,
      fragmentShader: POINT_FRAGMENT,
      transparent: true,
      depthTest: false,
      depthWrite: false,
    });
    this.nodePoints = new Points(this.nodeGeom, pointMaterial);
    this.nodePoints.frustumCulled = false;
    this.scene.add(this.nodePoints);

    this.edges = new LineSegments(
      this.edgeGeom,
      new LineBasicMaterial({ color: DIR_COLOR, transparent: true, opacity: 0.25, depthTest: false }),
    );
    this.edges.frustumCulled = false;
    this.scene.add(this.edges);

    this.beamGeom.setAttribute("position", new BufferAttribute(this.beamPos, 3));
    // LineBasicMaterial reads per-vertex color from the "color" attribute.
    this.beamGeom.setAttribute("color", new BufferAttribute(this.beamColor, 3));
    this.beamLines = new LineSegments(
      this.beamGeom,
      new LineBasicMaterial({ vertexColors: true, transparent: true, blending: AdditiveBlending, depthTest: false, opacity: 0.9 }),
    );
    this.beamLines.frustumCulled = false;
    this.scene.add(this.beamLines);

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new Vector2(canvas.clientWidth, canvas.clientHeight), 1.1, 0.6, 0.05);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());

    this.bindInput();
    this.resize();
  }

  /**
   * Wheel to zoom under the cursor, drag to pan, double-click to resume
   * auto-fit. Without this the camera reframes the whole graph every frame and
   * labels are unreadable as soon as the tree grows.
   */
  private bindInput(): void {
    this.canvas.addEventListener(
      "wheel",
      (event: WheelEvent) => {
        event.preventDefault();
        // One notch ~= 10%; trackpads send many small deltas, so scale by size.
        const factor = Math.exp(event.deltaY * 0.0015);
        this.view = zoomAt(this.view, factor, this.pointerNdc(event), this.aspect());
        this.syncCamera();
      },
      { passive: false },
    );

    this.canvas.addEventListener("pointerdown", (event: PointerEvent) => {
      this.dragPointer = event.pointerId;
      this.dragX = event.clientX;
      this.dragY = event.clientY;
      this.canvas.setPointerCapture(event.pointerId);
      this.canvas.style.cursor = "grabbing";
    });

    this.canvas.addEventListener("pointermove", (event: PointerEvent) => {
      if (this.dragPointer !== event.pointerId) return;
      const dx = event.clientX - this.dragX;
      const dy = event.clientY - this.dragY;
      this.dragX = event.clientX;
      this.dragY = event.clientY;
      this.view = panByPixels(this.view, dx, dy, {
        width: this.canvas.clientWidth || window.innerWidth,
        height: this.canvas.clientHeight || window.innerHeight,
      });
      this.syncCamera();
    });

    const endDrag = (event: PointerEvent): void => {
      if (this.dragPointer !== event.pointerId) return;
      this.dragPointer = null;
      this.canvas.style.cursor = "grab";
      if (this.canvas.hasPointerCapture(event.pointerId)) {
        this.canvas.releasePointerCapture(event.pointerId);
      }
    };
    this.canvas.addEventListener("pointerup", endDrag);
    this.canvas.addEventListener("pointercancel", endDrag);

    this.canvas.addEventListener("dblclick", () => {
      this.view = releaseToAuto(this.view);
    });

    this.canvas.style.cursor = "grab";
  }

  /** Pointer position in normalized device coordinates (y up). */
  private pointerNdc(event: MouseEvent): { x: number; y: number } {
    const rect = this.canvas.getBoundingClientRect();
    return {
      x: ((event.clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1,
      y: -(((event.clientY - rect.top) / Math.max(1, rect.height)) * 2 - 1),
    };
  }

  /** Register a discrete event for its visual effect (actor beam + flash). */
  onEvent(event: AgentEvent): void {
    // Seeded tree entries and unattributed filesystem changes have no actor, so
    // there is no figure to place and no beam to fire. The model still flashes
    // the file itself for the watcher case.
    if (event.origin === "seed" || !event.agent) return;

    const actor = this.ensureActor(event.agent);
    // Put the figure straight onto its first target instead of letting it slide
    // in from the origin, which reads as an unrelated object crossing the tree.
    if (!actor.hasPos) {
      const target = this.layout.position(event.path);
      if (target) {
        actor.x = target.x;
        actor.y = target.y;
        actor.hasPos = true;
      }
    }
    // The model already flashed the file's color/highlight; we add the beam.
    if (this.beams.length < MAX_BEAMS) {
      this.beams.push({ actor: event.agent, target: event.path, color: actor.color, age: 0, life: BEAM_LIFE_SECONDS });
    }
  }

  /** Start the render loop. */
  start(): void {
    if (this.running) return;
    this.running = true;
    this.lastTime = performance.now();
    const loop = (now: number): void => {
      if (!this.running) return;
      const dt = Math.min(0.05, (now - this.lastTime) / 1000);
      this.lastTime = now;
      try {
        this.frame(dt);
      } catch (error) {
        // One bad frame must not end the animation: scheduling the next frame
        // from `finally` keeps a transient fault transient instead of leaving
        // a permanently black canvas.
        this.reportFrameError(error);
      } finally {
        requestAnimationFrame(loop);
      }
    };
    requestAnimationFrame(loop);
  }

  stop(): void {
    this.running = false;
  }

  /** Resize buffers and camera to the current canvas size. */
  resize(): void {
    const w = this.canvas.clientWidth || window.innerWidth;
    const h = this.canvas.clientHeight || window.innerHeight;
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    this.bloom.setSize(w, h);
    this.applyCameraFrustum(w / Math.max(1, h));
  }

  /** Log a failing frame once per distinct message, so it never floods. */
  private reportFrameError(error: unknown): void {
    const message = error instanceof Error ? error.message : String(error);
    if (this.reportedFrameErrors.has(message)) return;
    this.reportedFrameErrors.add(message);
    console.error("graph-agents: frame failed:", error);
  }

  private frame(dt: number): void {
    this.sim.tick(dt);

    const model = this.sim.listNodes();
    this.layout.sync(model);
    this.layout.tick();

    if (this.topologyChanged(model)) this.rebuildNodeBuffers(model);
    this.updateNodeAttributes(model);
    this.updateEdges();
    this.updateActors(dt);
    this.updateBeams(dt);
    this.updateCamera(model);

    this.composer.render();
  }

  private topologyChanged(model: readonly SimNode[]): boolean {
    if (model.length !== this.nodeIds.length) return true;
    for (const node of model) {
      if (!this.nodeIndex.has(node.path)) return true;
    }
    return false;
  }

  private rebuildNodeBuffers(model: readonly SimNode[]): void {
    const n = model.length;
    this.nodeIndex.clear();
    this.nodeIds = new Array<string>(n);
    this.edgeChild = [];
    this.edgeParent = [];

    for (let i = 0; i < n; i += 1) {
      const node = model[i];
      this.nodeIndex.set(node.path, i);
      this.nodeIds[i] = node.path;
      if (this.nodeIndex.has(node.parent) || node.parent === "") {
        this.edgeChild.push(node.path);
        this.edgeParent.push(node.parent);
      }
      if (node.kind === "dir") this.ensureDirLabel(node.path);
    }

    allocateNodeAttributes(this.nodeGeom, n);
    allocateEdgeAttributes(this.edgeGeom, this.edgeChild.length);

    this.pruneDirLabels();
  }

  private updateNodeAttributes(model: readonly SimNode[]): void {
    const pos = this.nodeGeom.getAttribute("position") as BufferAttribute;
    const col = this.nodeGeom.getAttribute("aColor") as BufferAttribute;
    const size = this.nodeGeom.getAttribute("aSize") as BufferAttribute;
    const posArr = pos.array as Float32Array;
    const colArr = col.array as Float32Array;
    const sizeArr = size.array as Float32Array;
    const dpr = this.renderer.getPixelRatio();

    for (const node of model) {
      const idx = this.nodeIndex.get(node.path);
      if (idx === undefined) continue;
      const p = this.layout.position(node.path);
      const x = p?.x ?? 0;
      const y = p?.y ?? 0;
      posArr[idx * 3] = x;
      posArr[idx * 3 + 1] = y;
      posArr[idx * 3 + 2] = 0;

      if (node.kind === "dir") {
        this.scratchColor.setHex(DIR_COLOR).multiplyScalar(0.5);
        sizeArr[idx] = 3.5 * dpr;
      } else {
        const base = fileColor(node.path);
        const flash = hexToInt(node.color) ?? base;
        this.scratchColor.setHex(base).lerp(tmpColor.setHex(flash), node.highlight);
        this.scratchColor.multiplyScalar(0.35 + 0.65 * node.opacity);
        sizeArr[idx] = (6 + node.highlight * 8) * dpr;
      }
      colArr[idx * 3] = this.scratchColor.r;
      colArr[idx * 3 + 1] = this.scratchColor.g;
      colArr[idx * 3 + 2] = this.scratchColor.b;
    }
    pos.needsUpdate = true;
    col.needsUpdate = true;
    size.needsUpdate = true;
    this.nodeGeom.setDrawRange(0, model.length);
  }

  private updateEdges(): void {
    const attr = this.edgeGeom.getAttribute("position") as BufferAttribute | undefined;
    if (!attr) return;
    const arr = attr.array as Float32Array;
    let w = 0;
    for (let i = 0; i < this.edgeChild.length; i += 1) {
      const c = this.layout.position(this.edgeChild[i]);
      const p = this.layout.position(this.edgeParent[i]) ?? { x: 0, y: 0 };
      if (!c) continue;
      arr[w++] = c.x; arr[w++] = c.y; arr[w++] = 0;
      arr[w++] = p.x; arr[w++] = p.y; arr[w++] = 0;
    }
    attr.needsUpdate = true;
    this.edgeGeom.setDrawRange(0, (w / 3) | 0);
  }

  private updateActors(dt: number): void {
    for (const actor of this.actors.values()) {
      const intensity = this.sim.getActor(actor.agent)?.intensity ?? 0;
      // The figure never fades out entirely: an idle agent is still present and
      // must stay findable, it just stops drawing attention.
      const alpha = 0.4 + 0.6 * intensity;
      if (actor.hasPos) {
        actor.figure.position.set(actor.x, actor.y + AVATAR_WORLD_HEIGHT * 0.5, 2);
        actor.figure.visible = true;
        (actor.figure.material as SpriteMaterial).opacity = alpha;

        actor.label.position.set(actor.x, actor.y + AVATAR_WORLD_HEIGHT + 1.5, 2);
        actor.label.visible = true;
        (actor.label.material as SpriteMaterial).opacity = alpha;
      } else {
        actor.figure.visible = false;
        actor.label.visible = false;
      }
      // ease actor toward its most recent beam target
      const beam = this.latestBeamFor(actor.agent);
      if (beam) {
        const t = this.layout.position(beam.target);
        if (t) {
          const k = 1 - Math.pow(0.001, dt);
          actor.x += (t.x - actor.x) * k;
          actor.y += (t.y - actor.y) * k;
          actor.hasPos = true;
        }
      }
    }
  }

  private updateBeams(dt: number): void {
    let seg = 0;
    for (let i = this.beams.length - 1; i >= 0; i -= 1) {
      const beam = this.beams[i];
      beam.age += dt;
      if (beam.age >= beam.life) {
        this.beams.splice(i, 1);
        continue;
      }
      const actor = this.actors.get(beam.actor);
      const target = this.layout.position(beam.target);
      if (!actor || !actor.hasPos || !target || seg >= MAX_BEAMS) continue;
      const fade = 1 - beam.age / beam.life;
      this.scratchColor.setHex(beam.color).multiplyScalar(fade);
      const o = seg * 6;
      this.beamPos[o] = actor.x; this.beamPos[o + 1] = actor.y; this.beamPos[o + 2] = 0;
      this.beamPos[o + 3] = target.x; this.beamPos[o + 4] = target.y; this.beamPos[o + 5] = 0;
      this.beamColor[o] = this.scratchColor.r; this.beamColor[o + 1] = this.scratchColor.g; this.beamColor[o + 2] = this.scratchColor.b;
      this.beamColor[o + 3] = this.scratchColor.r; this.beamColor[o + 4] = this.scratchColor.g; this.beamColor[o + 5] = this.scratchColor.b;
      seg += 1;
    }
    (this.beamGeom.getAttribute("position") as BufferAttribute).needsUpdate = true;
    (this.beamGeom.getAttribute("color") as BufferAttribute).needsUpdate = true;
    this.beamGeom.setDrawRange(0, seg * 2);
  }

  private updateCamera(model: readonly SimNode[]): void {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of model) {
      const p = this.layout.position(node.path);
      if (!p) continue;
      if (p.x < minX) minX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.x > maxX) maxX = p.x;
      if (p.y > maxY) maxY = p.y;
    }
    if (!Number.isFinite(minX)) return;

    const targetCX = (minX + maxX) / 2;
    const targetCY = (minY + maxY) / 2;
    const spanY = Math.max(maxY - minY, (maxX - minX) / Math.max(0.0001, this.aspect())) * 0.5 + 20;

    // `follow` is a no-op once the user has zoomed or panned.
    this.view = follow(
      this.view,
      { centerX: targetCX, centerY: targetCY, halfHeight: spanY },
      0.05,
    );
    this.syncCamera();
  }

  /** Copy the view state onto the camera. */
  private syncCamera(): void {
    this.camera.position.set(this.view.centerX, this.view.centerY, 100);
    this.applyCameraFrustum(this.aspect());
  }

  private applyCameraFrustum(aspect: number): void {
    const halfH = this.view.halfHeight;
    const halfW = halfH * aspect;
    this.camera.left = -halfW;
    this.camera.right = halfW;
    this.camera.top = halfH;
    this.camera.bottom = -halfH;
    this.camera.updateProjectionMatrix();
  }

  private aspect(): number {
    const w = this.canvas.clientWidth || window.innerWidth;
    const h = this.canvas.clientHeight || window.innerHeight;
    return w / Math.max(1, h);
  }

  private ensureActor(agent: string): ActorView {
    const existing = this.actors.get(agent);
    if (existing) return existing;
    const color = hashColor(`actor:${agent}`);

    const figure = makeAvatar(color);
    figure.visible = false;
    this.scene.add(figure);

    // Session ids are long; the tail is what distinguishes two agents.
    const label = makeLabel(shortAgentName(agent), color, 0.6);
    label.visible = false;
    this.scene.add(label);

    const view: ActorView = { agent, color, x: 0, y: 0, hasPos: false, figure, label };
    this.actors.set(agent, view);
    return view;
  }

  private latestBeamFor(agent: string): Beam | undefined {
    for (let i = this.beams.length - 1; i >= 0; i -= 1) {
      if (this.beams[i].actor === agent) return this.beams[i];
    }
    return undefined;
  }

  private ensureDirLabel(path: string): void {
    if (this.dirLabels.has(path)) return;
    const name = path.slice(path.lastIndexOf("/") + 1);
    const sprite = makeLabel(name, DIR_COLOR, 0.7);
    this.scene.add(sprite);
    this.dirLabels.set(path, sprite);
  }

  private pruneDirLabels(): void {
    for (const [path, sprite] of this.dirLabels) {
      const p = this.layout.position(path);
      if (!this.nodeIndex.has(path)) {
        this.scene.remove(sprite);
        (sprite.material as SpriteMaterial).map?.dispose();
        (sprite.material as SpriteMaterial).dispose();
        this.dirLabels.delete(path);
      } else if (p) {
        sprite.position.set(p.x, p.y + 3, 1);
      }
    }
  }
}

/** Shared scratch color to avoid per-frame allocation in the lerp path. */
const tmpColor = new Color();

/** Sprite carrying the agent's figure, sized in world units. */
function makeAvatar(color: number): Sprite {
  const texture = new CanvasTexture(createAvatarCanvas(color));
  const material = new SpriteMaterial({
    map: texture,
    transparent: true,
    // Drawn on top of the tree: the figure is the subject, not part of the
    // structure it moves over.
    depthTest: false,
  });
  const sprite = new Sprite(material);
  sprite.scale.set(AVATAR_WORLD_HEIGHT, AVATAR_WORLD_HEIGHT, 1);
  return sprite;
}

/**
 * A readable short name for an agent.
 *
 * Session ids are UUID-length; printed in full they overlap each other and the
 * tree. The last segment is enough to tell two sessions apart.
 */
function shortAgentName(agent: string): string {
  const tail = agent.slice(agent.lastIndexOf("-") + 1);
  return tail.length > 8 ? tail.slice(0, 8) : tail || agent;
}

/** Build a text label sprite (white-ish text tinted by `color`). */
function makeLabel(text: string, color: number, scale = 1): Sprite {
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d")!;
  const font = 48;
  ctx.font = `${font}px system-ui, sans-serif`;
  const metrics = ctx.measureText(text);
  const pad = 12;
  canvas.width = Math.ceil(metrics.width) + pad * 2;
  canvas.height = font + pad * 2;
  ctx.font = `${font}px system-ui, sans-serif`;
  ctx.textBaseline = "middle";
  ctx.fillStyle = `#${color.toString(16).padStart(6, "0")}`;
  ctx.fillText(text, pad, canvas.height / 2);

  const texture = new CanvasTexture(canvas);
  const material = new SpriteMaterial({ map: texture, transparent: true, depthTest: false, opacity: 0.9 });
  const sprite = new Sprite(material);
  const worldH = 4 * scale;
  sprite.scale.set((canvas.width / canvas.height) * worldH, worldH, 1);
  return sprite;
}

/** Factory that keeps construction details out of `main.ts`. */
export function createRenderer(canvas: HTMLCanvasElement, sim: Simulation): GourceRenderer {
  return new GourceRenderer(canvas, sim);
}
