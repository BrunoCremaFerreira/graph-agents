import { defineConfig } from "vite";

/**
 * Vite config for the renderer front-end.
 * - `npm run dev`   serves index.html with HMR.
 * - `npm run build` type-checks (tsc) then bundles to `dist/`.
 * The daemon URL is injected at build/dev time via `VITE_WS_URL`
 * (defaults to ws://localhost:8765 inside wsClient.ts).
 */
export default defineConfig({
  root: ".",
  build: {
    outDir: "dist",
    target: "es2021",
    sourcemap: true,
  },
  server: {
    port: 5173,
    host: true,
  },
});
