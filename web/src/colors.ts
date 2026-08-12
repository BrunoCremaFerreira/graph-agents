/**
 * Color helpers. Gource colors file dots by extension; directories and actor
 * beams get stable per-key colors from a hash. Everything here returns a
 * `THREE.Color`-friendly 24-bit RGB integer (0xRRGGBB).
 */

/** Curated extension palette, close to Gource's defaults for common languages. */
const EXTENSION_COLORS: Readonly<Record<string, number>> = {
  ts: 0x3178c6,
  tsx: 0x3178c6,
  js: 0xf0db4f,
  jsx: 0xf0db4f,
  mjs: 0xf0db4f,
  json: 0x8bc34a,
  py: 0x4b8bbe,
  rb: 0xcc342d,
  go: 0x00add8,
  rs: 0xdea584,
  c: 0x555555,
  h: 0x777777,
  cpp: 0x00599c,
  cc: 0x00599c,
  hpp: 0x00599c,
  java: 0xe76f00,
  cs: 0x9b4f96,
  php: 0x777bb4,
  html: 0xe34c26,
  css: 0x264de4,
  scss: 0xc76494,
  md: 0x083fa1,
  yml: 0xcb171e,
  yaml: 0xcb171e,
  toml: 0x9c4221,
  sh: 0x89e051,
  sql: 0xe38c00,
  png: 0xad4fd6,
  jpg: 0xad4fd6,
  svg: 0xffb13b,
  lock: 0x999999,
};

/** Parse `#RRGGBB`/`RRGGBB` hex (no `#`) into 0xRRGGBB, or `null` if malformed. */
export function hexToInt(hex: string): number | null {
  const clean = hex.startsWith("#") ? hex.slice(1) : hex;
  if (!/^[0-9a-fA-F]{6}$/.test(clean)) return null;
  return parseInt(clean, 16);
}

/** Lowercased extension of a path, or `""` when there is none. */
function extensionOf(path: string): string {
  const base = path.slice(path.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  return dot <= 0 ? "" : base.slice(dot + 1).toLowerCase();
}

/** Deterministic bright color derived from a string (FNV-1a hash → HSL). */
export function hashColor(key: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < key.length; i += 1) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  const hue = (hash >>> 0) % 360;
  return hslToInt(hue, 0.7, 0.6);
}

/** Color for a file dot: extension palette first, hashed fallback otherwise. */
export function fileColor(path: string): number {
  const ext = extensionOf(path);
  const known = EXTENSION_COLORS[ext];
  return known ?? hashColor(ext || path);
}

function hslToInt(h: number, s: number, l: number): number {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const hp = h / 60;
  const x = c * (1 - Math.abs((hp % 2) - 1));
  let r = 0;
  let g = 0;
  let b = 0;
  if (hp < 1) [r, g, b] = [c, x, 0];
  else if (hp < 2) [r, g, b] = [x, c, 0];
  else if (hp < 3) [r, g, b] = [0, c, x];
  else if (hp < 4) [r, g, b] = [0, x, c];
  else if (hp < 5) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  const m = l - c / 2;
  const to255 = (v: number): number => Math.round((v + m) * 255) & 0xff;
  return (to255(r) << 16) | (to255(g) << 8) | to255(b);
}
