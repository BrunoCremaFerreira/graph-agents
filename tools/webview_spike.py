#!/usr/bin/env python3
"""Measure whether a WebKit webview can render this project's graph.

Run this on a REAL desktop session before committing to a window backend. It
cannot run on a tty: it needs a display, and the whole point is what the GPU
does. Nothing else in this repository can answer these questions -- the suite
runs headless by design, and a headless screenshot of an animated force layout
proves nothing.

The question is NOT "which shell language". On Linux, pywebview's GTK backend,
Tauri's `wry` and Go's `webview` all bind the SAME WebKitGTK, so swapping the
shell retires no rendering risk at all. Only a Chromium engine is a different
answer, which is why `--app` mode is the designated fallback rather than an
improvisation. What this script decides is whether WebKit is good enough here.

Usage:

    # 1. start the daemon on the project you want to look at
    rhi ~/some/project --no-window --port 8080

    # 2. in a graphical session, with pywebview installed:
    python3 tools/webview_spike.py http://127.0.0.1:8080/

    # 3. drive the graph for a minute (let the layout settle, open a file,
    #    let an agent write something), then close the window.

It prints one report block. Paste that block back verbatim -- the numbers are
the deliverable, not the impressions.

Dependencies, Linux:  apt install python3-gi gir1.2-webkit2-4.1
                      pip install pywebview
Dependencies, macOS:  pip install pywebview   (pulls pyobjc)
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import textwrap

#: Renderer substrings that mean the GPU is not involved. Any of these is a
#: hard fail: three.js will start, the bloom pass will composite, and the whole
#: thing will crawl -- which is the failure that looks like success in a
#: screenshot and only shows up as frame time.
SOFTWARE_RENDERERS = ("llvmpipe", "softpipe", "swiftshader", "software rasterizer")

#: The frame-time budget, in milliseconds, at the 5th percentile. `updateLabels`,
#: `pickFile` and `updateReadMarkers` all run every frame by design, so this is
#: the budget they live in. Worse than this and the window is not worth having.
FRAME_TIME_BUDGET_MS = 33.0

#: How long to sample. Long enough that the force layout stops being a
#: transient and starts being the steady state.
SAMPLE_SECONDS = 60

PROBE_JS = r"""
(function () {
  const out = {};

  // --- 1. is there a WebGL2 context at all? -------------------------------
  const canvas = document.createElement("canvas");
  const gl = canvas.getContext("webgl2");
  out.webgl2 = gl !== null;
  if (!gl) { return JSON.stringify(out); }

  // --- 2. hardware or software? -------------------------------------------
  const dbg = gl.getExtension("WEBGL_debug_renderer_info");
  out.renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : "(masked)";
  out.vendor = dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : "(masked)";

  // --- 3. can the bloom pass have its render targets? ----------------------
  // UnrealBloomPass needs a float or half-float colour buffer. Without one it
  // does not throw -- it composites black, which is why this is asked
  // separately from "did the page load".
  out.halfFloat = gl.getExtension("EXT_color_buffer_half_float") !== null;
  out.float = gl.getExtension("EXT_color_buffer_float") !== null;
  out.maxTexture = gl.getParameter(gl.MAX_TEXTURE_SIZE);

  // --- 7. DPI ---------------------------------------------------------------
  out.devicePixelRatio = window.devicePixelRatio;
  out.innerWidth = window.innerWidth;

  // --- did the real renderer come up? --------------------------------------
  // The page's own canvas, not ours. A WebGL context that exists in isolation
  // but fails inside three.js is a real outcome and this is what catches it.
  const live = document.querySelector("canvas");
  out.pageCanvas = live !== null;
  out.pageCanvasSize = live ? [live.width, live.height] : null;

  return JSON.stringify(out);
})();
"""

FRAME_JS = r"""
(function () {
  if (window.__spike) { return "already running"; }
  window.__spike = { frames: [], last: performance.now() };
  function tick(now) {
    const w = window.__spike;
    w.frames.push(now - w.last);
    w.last = now;
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  return "sampling";
})();
"""

REPORT_JS = r"""
(function () {
  const w = window.__spike;
  if (!w || w.frames.length < 10) { return JSON.stringify({frames: 0}); }
  // Drop the first few: they include the page still settling.
  const f = w.frames.slice(5).sort((a, b) => a - b);
  const at = (q) => f[Math.min(f.length - 1, Math.floor(f.length * q))];
  return JSON.stringify({
    frames: f.length,
    mean: f.reduce((a, b) => a + b, 0) / f.length,
    median: at(0.5),
    p95: at(0.95),   // the 5th-percentile WORST frame time
  });
})();
"""


def _verdict(probe: dict, frames: dict) -> list[str]:
    """Turn the numbers into pass/fail lines. The numbers still get printed."""
    lines = []

    if not probe.get("webgl2"):
        lines.append("FAIL  1. no WebGL2 context -- three.js will not start at all")
    else:
        lines.append("pass  1. WebGL2 context present")

    renderer = str(probe.get("renderer", "")).lower()
    if any(soft in renderer for soft in SOFTWARE_RENDERERS):
        lines.append(f"FAIL  2. software renderer: {probe.get('renderer')!r}")
    elif renderer in ("(masked)", ""):
        lines.append("????  2. renderer is masked -- cannot tell hardware from software")
    else:
        lines.append(f"pass  2. hardware renderer: {probe.get('renderer')!r}")

    if not (probe.get("halfFloat") or probe.get("float")):
        lines.append("FAIL  3. no float/half-float colour buffer -- bloom composites black")
    else:
        lines.append("pass  3. bloom pass has a render target")

    if not probe.get("pageCanvas"):
        lines.append("FAIL  --  the page rendered no canvas: the app itself did not come up")

    if not frames.get("frames"):
        lines.append("????  4. no frames sampled -- was the window driven at all?")
    elif frames["p95"] > FRAME_TIME_BUDGET_MS:
        lines.append(
            f"FAIL  4. 5th-percentile frame time {frames['p95']:.1f} ms "
            f"exceeds the {FRAME_TIME_BUDGET_MS:.0f} ms budget"
        )
    else:
        lines.append(f"pass  4. 5th-percentile frame time {frames['p95']:.1f} ms")

    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure WebGL under a webview, against the real application."
    )
    parser.add_argument("url", help="the running daemon's page, e.g. http://127.0.0.1:8080/")
    parser.add_argument(
        "--seconds",
        type=int,
        default=SAMPLE_SECONDS,
        help=f"how long to sample frame times (default {SAMPLE_SECONDS})",
    )
    args = parser.parse_args()

    try:
        import webview  # noqa: PLC0415 - the whole point is whether this imports
    except Exception as exc:  # pragma: no cover - environment probe
        print(f"pywebview did not import: {exc}", file=sys.stderr)
        print(
            "Linux: apt install python3-gi gir1.2-webkit2-4.1 && pip install pywebview",
            file=sys.stderr,
        )
        return 2

    results: dict = {}

    def measure(window) -> None:
        import time

        window.evaluate_js(FRAME_JS)
        results["probe"] = json.loads(window.evaluate_js(PROBE_JS))
        print(
            f"\nSampling for {args.seconds}s. Drive the graph now: let the layout\n"
            "settle, pan and zoom, open a file, and if you can, let an agent write\n"
            "something so beams and read rings are on screen.\n"
        )
        time.sleep(args.seconds)
        results["frames"] = json.loads(window.evaluate_js(REPORT_JS))
        window.destroy()

    window = webview.create_window("rhizome-graph spike", args.url, width=1400, height=900)
    webview.start(measure, window)

    probe = results.get("probe", {})
    frames = results.get("frames", {})

    print("\n" + "=" * 68)
    print("WEBVIEW SPIKE REPORT -- paste this block back verbatim")
    print("=" * 68)
    print(f"platform         : {platform.system()} {platform.release()}")
    print(f"python           : {sys.version.split()[0]}")
    print(f"session type     : {_session_type()}")
    print(f"url              : {args.url}")
    print("-" * 68)
    for key in ("webgl2", "vendor", "renderer", "halfFloat", "float", "maxTexture"):
        print(f"{key:17}: {probe.get(key)!r}")
    print(f"{'devicePixelRatio':17}: {probe.get('devicePixelRatio')!r}")
    print(f"{'pageCanvas':17}: {probe.get('pageCanvas')!r} {probe.get('pageCanvasSize')}")
    print("-" * 68)
    if frames.get("frames"):
        print(
            f"frames {frames['frames']}  mean {frames['mean']:.1f} ms  "
            f"median {frames['median']:.1f} ms  p95 {frames['p95']:.1f} ms"
        )
    else:
        print("frames: none sampled")
    print("-" * 68)
    for line in _verdict(probe, frames):
        print(line)
    print("=" * 68)
    print(
        textwrap.dedent(
            """
            Still to report by eye, because no number captures them:
              5. Did it need WEBKIT_DISABLE_DMABUF_RENDERER=1? Run once without it
                 and once with. Black or corrupt output without it means the
                 launcher must set it -- that is a design output, not a support note.
              6. On macOS: the same four checks under WKWebView.
              7. Does devicePixelRatio match what the same page reports in a
                 browser on the same monitor? Label textures are rasterised once
                 at construction DPI, so a mismatch makes every name soft.
              8. Do the violet read rings read clearly against the amber write
                 flash at real zoom, and does the ring survive being drawn much
                 smaller than its 64 px texture?
            """
        ).strip()
    )
    return 0


def _session_type() -> str:
    import os

    return (
        os.environ.get("XDG_SESSION_TYPE")
        or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else "")
        or ("x11" if os.environ.get("DISPLAY") else "")
        or "unknown"
    )


if __name__ == "__main__":
    sys.exit(main())
