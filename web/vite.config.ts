import { defineConfig } from "vite";

/**
 * Vite config for the renderer front-end.
 * - `npm run dev`   serves index.html with HMR.
 * - `npm run build` type-checks (tsc) then bundles to `dist/`.
 *
 * The client derives its WebSocket URL from the page origin (see
 * `resolveWsUrl` in wsClient.ts), so in dev we proxy `/ws` to the daemon.
 * That keeps one code path for dev and production; `VITE_WS_URL` still
 * overrides if the daemon lives somewhere else entirely.
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
    proxy: {
      "/ws": {
        target: process.env.GRAPHAGENTS_ORIGIN ?? "http://localhost:8080",
        ws: true,
      },
    },
  },
});
