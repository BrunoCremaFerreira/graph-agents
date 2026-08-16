/**
 * Pure: where the page finds its control token, and how a request carries it.
 *
 * The daemon authorizes commands (`complete`, `setRoot`, `file`) and cannot do
 * it from the peer's address: a WebSocket handshake is not subject to the
 * same-origin policy, so any page open on this host can reach the socket, and a
 * loopback-side proxy makes a remote connection arrive as loopback. So the
 * daemon mints a token at boot and injects it into the `index.html` it serves,
 * as `window.__RHIZOME_TOKEN__`; a cross-site page cannot read that page, and a
 * proxy has no token to forward.
 *
 * Both functions take values rather than reading globals, so they stay testable
 * without stubbing a browser — `wsClient.ts` is the one place that hands them
 * the real `window` and `import.meta.env`.
 */

/** The global the daemon writes into the HTML it serves. */
const INJECTED_KEY = "__RHIZOME_TOKEN__";

/** The build-time variable, for a page served by the Vite dev server. */
const ENV_KEY = "VITE_RHIZOME_TOKEN";

/** A string property of an object-like value, or "" for anything else. */
function stringField(source: unknown, key: string): string {
  if (typeof source !== "object" || source === null) return "";
  const value = (source as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

/**
 * The token this page should send, or "" when it has none.
 *
 * The injected value wins: it comes from the daemon actually running, while a
 * value baked into the bundle can be stale and would only be refused. Never
 * throws — this runs while the page is booting, and an exception there is a
 * black screen over a token that is merely absent.
 */
export function readToken(win: unknown, env: unknown): string {
  const injected = stringField(win, INJECTED_KEY);
  if (injected.length > 0) return injected;
  return stringField(env, ENV_KEY);
}

/**
 * The payload as it goes on the wire.
 *
 * An empty token adds no field at all: `token: ""` is not the same thing as no
 * token, and a daemon built before the token existed would see a key it does
 * not know. Never mutates the payload it was given — the caller in
 * `WsClient.send` is handed an object literal that something else may hold.
 */
export function withToken(payload: object, token: string): object {
  if (token.length === 0) return payload;
  return { ...payload, token };
}
