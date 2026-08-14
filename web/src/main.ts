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
import { createContextHud } from "./contextHud";
import { createEventHud } from "./eventHud";
import { createAttributionMonitor } from "./attribution";
import { createAttributionHud } from "./attributionHud";
import { createSearchHud } from "./searchHud";
import { interpretSearchKey } from "./searchKeys";
import { createRootHud } from "./rootHud";
import { interpretRootKey } from "./rootKeys";
import {
  applyCompletion,
  cancelPrompt,
  createRootPrompt,
  failPrompt,
  openPrompt,
  setText,
  type RootPromptState,
} from "./rootPrompt";
import {
  activePath,
  closeSearch,
  createSearchState,
  nextMatch,
  openSearch,
  refreshMatches,
  setQuery,
  type SearchState,
} from "./search";

function boot(): void {
  const canvas = document.getElementById("stage") as HTMLCanvasElement | null;
  if (!canvas) throw new Error("missing #stage canvas");

  const sim = createSimulation();
  const renderer = createRenderer(canvas, sim);
  const contextEl = document.getElementById("context");
  const contextHud = contextEl ? createContextHud(contextEl) : null;
  const logEl = document.getElementById("log");
  const eventHud = logEl ? createEventHud(logEl) : null;
  const attributionEl = document.getElementById("attribution");
  const attributionHud = attributionEl ? createAttributionHud(attributionEl) : null;
  const attribution = createAttributionMonitor();
  const searchEl = document.getElementById("search");
  const searchHud = searchEl ? createSearchHud(searchEl) : null;
  const rootEl = document.getElementById("root-bar");
  const rootHud = rootEl ? createRootHud(rootEl) : null;

  // The search's whole state machine is in `search.ts`; this is just the one
  // variable holding the state it returns, and the wiring that shows it.
  let search: SearchState = createSearchState();

  function showSearch(next: SearchState): void {
    search = next;
    if (!search.open) {
      searchHud?.close();
      renderer.clearSearch();
      return;
    }
    searchHud?.setStatus(search.matches.length, search.activeIndex);
    renderer.setSearch(search.matches, activePath(search), search.frame);
  }

  // Same shape for the root bar: the state machine is `rootPrompt.ts`, this is
  // the variable holding what it returns plus the wiring that paints it.
  let rootPrompt: RootPromptState = createRootPrompt();
  /** The observed root, as of the last meta frame; what ctrl+L prefills. */
  let observedRoot = "";

  function showRoot(next: RootPromptState): void {
    rootPrompt = next;
    if (!rootPrompt.open) {
      rootHud?.close();
      return;
    }
    rootHud?.setText(rootPrompt.text);
    rootHud?.setMatches(rootPrompt.matches);
    rootHud?.setError(rootPrompt.error);
  }

  const client = createWsClient(
    (event) => {
      sim.applyEvent(event);
      renderer.onEvent(event);
      eventHud?.push(event);
      attribution.observe(event);
      attributionHud?.update(eventHud?.hasEntries() ?? false, attribution.attributed());
      // The tree changed under the query: a new file may answer it and a
      // deleted one no longer exists to be framed. `refreshMatches`, not
      // `setQuery`, so the recount does not throw an F3 walk back to the
      // overview every time an event lands.
      if (search.open && search.query) showSearch(refreshMatches(search, sim.listNodes()));
    },
    resolveWsUrl(),
    {
      onMeta: (meta) => {
        observedRoot = meta.root;
        contextHud?.setMeta(meta);
      },
      // The daemon answered a Tab. `applyCompletion` decides whether the answer
      // is still the one being waited for -- it travelled the network while the
      // user kept typing -- so nothing here inspects it.
      onCompletion: (completion) => showRoot(applyCompletion(rootPrompt, completion)),
      // A refused path keeps the bar open with the text still in it, so the typo
      // can be fixed; that rule is `failPrompt`'s, not this handler's.
      onRootError: (error) => showRoot(failPrompt(rootPrompt, error.reason)),
      onReset: () => {
        // The root changed: everything on screen belongs to the old project and
        // the new tree is already on its way.
        sim.reset();
        renderer.resetScene();
        eventHud?.clear();
        attribution.reset();
        attributionHud?.update(false, false);
        // Only now does the bar close: the switch is confirmed, not merely sent.
        showRoot(cancelPrompt(rootPrompt));
      },
    },
  );

  searchHud?.onQueryChange((query) => showSearch(setQuery(search, query, sim.listNodes())));
  rootHud?.onTextChange((text) => showRoot(setText(rootPrompt, text)));

  window.addEventListener("keydown", (event) => {
    // The root bar goes first, and an answered key never reaches the search: an
    // open bar owns Enter and Escape, which the search box also answers.
    const rootCommand = interpretRootKey(event, rootPrompt.open);
    if (rootCommand) {
      // Without this the browser takes all three back: ctrl+L focuses its own
      // address bar, Tab moves focus out of the field, and Enter submits.
      event.preventDefault();
      if (rootCommand === "open") {
        // Already open: ctrl+L only refocuses and selects, as in the search box.
        // Reopening over a half-typed path would throw it away.
        if (!rootPrompt.open) showRoot(openPrompt(rootPrompt, observedRoot));
        rootHud?.open();
      } else if (rootCommand === "complete") {
        // The browser cannot read the disk, so Tab is a round trip; the reply
        // comes back through `onCompletion`.
        client.send({ kind: "complete", path: rootHud?.text() ?? rootPrompt.text });
      } else if (rootCommand === "submit") {
        // The bar stays open until the daemon confirms with a `reset` frame, or
        // refuses with a `rootError`.
        client.send({ kind: "setRoot", path: rootHud?.text() ?? rootPrompt.text });
      } else {
        showRoot(cancelPrompt(rootPrompt));
      }
      return;
    }

    const command = interpretSearchKey(event, search.open);
    if (!command) return;
    // ctrl+F would otherwise open the browser's own find bar, and F3 its
    // find-again; both would search the page's text instead of the graph.
    event.preventDefault();
    if (command === "open") {
      // Already open: ctrl+F only refocuses and selects the text, leaving the
      // state alone. `openSearch` returns a CLEAN state by contract, so
      // applying it here would wipe a live query's matches and highlights while
      // the field still showed the old text -- and `setStatus`, which reads the
      // field, would then report "nenhum resultado" over a search that had 12.
      // The selection is what lets the next keystroke replace the query, and
      // that fires `input`, which goes through `setQuery`.
      searchHud?.open();
      if (!search.open) showSearch(openSearch(search));
    } else if (command === "next") {
      showSearch(nextMatch(search));
    } else {
      showSearch(closeSearch(search));
    }
  });

  window.addEventListener("resize", () => {
    renderer.resize();
    contextHud?.refresh();
  });
  renderer.resize();
  renderer.start();
  client.connect();
}

boot();
