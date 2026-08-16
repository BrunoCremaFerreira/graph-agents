/**
 * Contract tests (RED) for `src/token.ts`.
 *
 * The defect this exists for is on the daemon's side, and the page is the other
 * half of the fix. `control_allowed` decides who may send `setRoot` / `file`
 * from the peer's IP alone, and the peer's IP lies twice over: a WebSocket
 * handshake is not subject to the same-origin policy, so ANY page loaded in a
 * browser on this host can open `ws://127.0.0.1:8080/ws` and read files through
 * it; and any loopback-side proxy (this repo's own `vite.config.ts` binds
 * `host: true` and proxies `/ws`) makes a LAN connection arrive as 127.0.0.1.
 *
 * So the daemon mints a token at boot and injects it into the `index.html` it
 * serves, as `window.__RHIZOME_TOKEN__`. A cross-site page cannot read it --
 * same-origin is exactly what stops it fetching the page to scrape it -- and a
 * proxy has none to forward.
 *
 * Two pure functions, because the alternative is this logic living in
 * `wsClient.ts` next to a real WebSocket, where nothing can reach it:
 *
 *   readToken  -- where the page finds its token, including the dev-server case
 *                 where Vite serves the HTML and the daemon never touched it.
 *   withToken  -- how a payload carries it.
 *
 * `readToken` takes `win` and `env` as arguments rather than reading globals, so
 * every shape below is a value, not a stubbed global. It must never throw: it
 * runs at module scope, and an exception there is a page that never boots --
 * a black screen, over a token that is merely absent.
 *
 * Expected to FAIL until `src/token.ts` exists. One property per test.
 */

import { describe, it, expect } from "vitest";
import { readToken, withToken } from "../src/token";

describe("readToken", () => {
  it("reads the token the daemon injected into the page", () => {
    expect(readToken({ __RHIZOME_TOKEN__: "s3cret-token" }, {})).toBe("s3cret-token");
  });

  it("falls back to the build-time variable when the page carries none", () => {
    // The dev-server path: Vite serves index.html, so nothing ever injected the
    // global, and the daemon behind the proxy still demands a token.
    expect(readToken({}, { VITE_RHIZOME_TOKEN: "dev-token" })).toBe("dev-token");
  });

  it("prefers the injected token over the build-time one", () => {
    // The injected one came from the daemon actually running; a stale value
    // baked into the bundle would be refused by it.
    expect(
      readToken({ __RHIZOME_TOKEN__: "s3cret-token" }, { VITE_RHIZOME_TOKEN: "dev-token" }),
    ).toBe("s3cret-token");
  });

  it("treats an empty injected token as no token at all", () => {
    expect(readToken({ __RHIZOME_TOKEN__: "" }, { VITE_RHIZOME_TOKEN: "dev-token" })).toBe(
      "dev-token",
    );
  });

  it("ignores an injected value that is not a string", () => {
    // A minifier, a stray global, another script on the page: anything but a
    // string is not the daemon's token.
    expect(readToken({ __RHIZOME_TOKEN__: 42 }, { VITE_RHIZOME_TOKEN: "dev-token" })).toBe(
      "dev-token",
    );
  });

  it("ignores a build-time value that is not a string", () => {
    expect(readToken({}, { VITE_RHIZOME_TOKEN: ["dev-token"] })).toBe("");
  });

  it("answers with no token when neither source has one", () => {
    // An old daemon, or a page opened straight off the filesystem. The frames
    // then go out untokenized and are refused with a reason, which is a far
    // better failure than a page that throws before it paints.
    expect(readToken({}, {})).toBe("");
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 42],
    ["a string", "window"],
    ["an array", []],
    ["a boolean", false],
  ])("survives %s where a window was expected", (_label, win) => {
    expect(() => readToken(win, {})).not.toThrow();
    expect(readToken(win, {})).toBe("");
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["a number", 42],
    ["a string", "env"],
  ])("survives %s where an env was expected", (_label, env) => {
    expect(() => readToken({}, env)).not.toThrow();
    expect(readToken({}, env)).toBe("");
  });

  it("still finds the injected token when the env is missing entirely", () => {
    expect(readToken({ __RHIZOME_TOKEN__: "s3cret-token" }, undefined)).toBe("s3cret-token");
  });
});

describe("withToken", () => {
  it("adds the token to the payload", () => {
    expect(withToken({ kind: "file", path: "src/app.ts" }, "s3cret-token")).toEqual({
      kind: "file",
      path: "src/app.ts",
      token: "s3cret-token",
    });
  });

  it("leaves the payload alone when there is no token to add", () => {
    // A `token: ""` on the wire is not the same thing as no token, and an old
    // daemon would see a field it does not know.
    expect(withToken({ kind: "complete", path: "~/pro" }, "")).toEqual({
      kind: "complete",
      path: "~/pro",
    });
  });

  it("never mutates the payload it was given", () => {
    // The caller in `wsClient.send` is handed an object literal built by
    // `main.ts`; writing into it would leak the token into whatever else holds
    // a reference to it.
    const payload = { kind: "setRoot", path: "/srv/other" };

    withToken(payload, "s3cret-token");

    expect(payload).toEqual({ kind: "setRoot", path: "/srv/other" });
  });

  it("keeps every field the payload already had", () => {
    expect(withToken({ kind: "setRoot", path: "/srv/other" }, "s3cret-token")).toMatchObject({
      kind: "setRoot",
      path: "/srv/other",
    });
  });

  it("overrides a token the payload was already carrying", () => {
    // The real one wins over anything a call site invented, so there is exactly
    // one place that decides what authenticates a frame.
    expect(withToken({ kind: "file", path: "a.txt", token: "stale" }, "s3cret-token")).toEqual({
      kind: "file",
      path: "a.txt",
      token: "s3cret-token",
    });
  });
});
