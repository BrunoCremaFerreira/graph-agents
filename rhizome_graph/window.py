"""Which window `rhi` opens, and how it is opened.

`rhi <dir>` should feel like an application of the operating system rather than
like a program that happens to be written in web technology, so it opens a
window instead of a browser tab. **Which** window is not settled: on Linux
pywebview, Tauri and Go-webview all bind the same WebKitGTK, so the choice of
shell language retires no rendering risk at all, and only a Chromium engine is a
different answer. That is why an app-mode browser is the designated fallback
rather than an improvisation, and why `tools/webview_spike.py` exists -- it is
run by a human on a real desktop session, because no headless suite can measure
a GL surface.

So this module is shaped so that the spike's outcome changes exactly two things:
which backend :func:`choose_window_backend` prefers, and which dependency ships.

Three rules hold it together.

  * **The decision is pure, with availability injected.** No test needs a
    display, a browser or pywebview installed to say what would be chosen.
  * **Nothing here may name the control token.** The daemon injects it into the
    `index.html` it serves, keyed on the request path alone, so a window issuing
    `GET /` from loopback inherits it exactly as a browser does. A window helper
    that could *accept* a token would be a second place the credential lives, and
    a chokepoint with a second entrance is not a chokepoint. The URL a strategy
    is given is a bare origin for the same reason, and the app-mode command line
    carries no credential either -- on Linux, `/proc/<pid>/cmdline` is readable
    by anybody.
  * **A GUI library is imported inside the strategy that needs it, never at
    module level.** `rhi --no-window` on a machine with no pywebview must simply
    work, and this module is imported on every single run.

A strategy is a plain function of `(url, on_closed, close_requested)` that
**blocks until its window is gone**, and calls `on_closed` when it is. It
therefore owns the thread it is called on -- the main one, because that is where
a GUI toolkit insists on living -- and the daemon runs elsewhere.

`close_requested` is the other direction, and it is a parameter rather than a
returned handle for a structural reason: a strategy returns only once its window
is dead, so it cannot hand back anything for closing it. A daemon stopping for a
reason of its own -- a `kill`, a supervisor, a logout -- sets the event, and the
strategy tears its window down and calls `on_closed` exactly as it does when the
user closes it. It is a `threading.Event` because the request is raised on
another thread, and because an Event is **inert**: it carries no capability into
this module, which is what keeps the parameter list an allow-list.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable

#: A window drawn by a native webview widget: WebKitGTK on Linux, WKWebView on
#: macOS, through pywebview.
WEBVIEW = "webview"

#: A window drawn by an installed Chromium-family browser started in app mode.
APP_BROWSER = "app_browser"

#: No window at all -- what `--no-window` asks for, and what a headless machine
#: truthfully reports. An answer, not a failure.
NO_WINDOW = "none"

#: Every answer :func:`choose_window_backend` may give.
BACKENDS = (WEBVIEW, APP_BROWSER, NO_WINDOW)

# ---------------------------------------------------------------------------
# THE PREFERENCE, per platform, most-wanted first.
#
# This is the one table the spike may edit. Today it is the webview everywhere,
# because the requirement is a window and not a browser being launched; if
# WebKitGTK turns out to be unusable on Linux -- a blank GL surface, a bloom pass
# that will not composite, a crash on a real desktop session -- reverse that one
# row and nothing else here changes.
# ---------------------------------------------------------------------------
PREFERENCE_BY_PLATFORM = {
    "linux": (WEBVIEW, APP_BROWSER),
    "darwin": (WEBVIEW, APP_BROWSER),
}

#: What an unrecognised platform gets. The same order: a preference nobody has
#: measured is still better than an arbitrary one.
DEFAULT_PREFERENCE = (WEBVIEW, APP_BROWSER)

#: The browsers an app-mode window may be borrowed from, in the order they are
#: looked for. Chromium-family only: `--app` and `--user-data-dir` are theirs,
#: and Firefox has no equivalent that yields a dedicated process.
APP_BROWSER_COMMANDS = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "microsoft-edge",
    "vivaldi",
)

#: Where the same browsers live on macOS, which puts nothing on `$PATH`.
APP_BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

#: What the window calls itself. Only ever seen by a person.
WINDOW_TITLE = "rhizome-graph"

WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 900

#: The prefix of the throwaway profile directory an app-mode browser is given.
PROFILE_PREFIX = "rhizome-window-"

#: How often a borrowed browser is looked in on while waiting for it to exit.
#: Short enough that a `kill` feels immediate, long enough to cost nothing.
CLOSE_POLL_SECONDS = 0.2

#: How long a browser asked to quit is given before it is killed. A window that
#: refuses to go away is worse than one that vanishes rudely.
CLOSE_GRACE_SECONDS = 5.0

#: What the thread watching for a close request calls itself.
CLOSE_WATCH_THREAD_NAME = "rhizome-window-close"


class WindowUnavailable(RuntimeError):
    """A window was attempted and could not be opened, for a stated reason.

    An ordinary exception rather than an exit: whether an unopenable window ends
    the run depends on who asked for it, and that is the caller's knowledge.
    """


def choose_window_backend(
    *, platform: str, available: Iterable[str], requested: str
) -> str:
    """Which window to open on `platform`, given what is installed and what was
    asked for.

    Pure, and total over every combination: the answer is always one of
    :data:`BACKENDS`, and never a backend that was not available.

    It answers what *can* be opened, never what should be done about it. An
    explicit `--window` that cannot be honoured has to end the run loudly, and
    that decision belongs to the caller holding the request -- folding it in here
    would leave a pure function with an exit code in it.
    """
    if requested == NO_WINDOW:
        return NO_WINDOW
    installed = frozenset(available)
    for backend in PREFERENCE_BY_PLATFORM.get(platform, DEFAULT_PREFERENCE):
        if backend in installed:
            return backend
    return NO_WINDOW


def available_backends() -> frozenset[str]:
    """What this machine could actually open a window with, right now.

    Not a configuration read: a capability probe, of the same kind as asking
    `$PATH` for a program. On Linux it also asks whether there is a display at
    all -- a browser installed on a headless server is still no window -- and on
    macOS the window server is always there.
    """
    found: set[str] = set()
    if not _has_display():
        return frozenset()
    if importlib.util.find_spec("webview") is not None:
        found.add(WEBVIEW)
    if find_app_browser() is not None:
        found.add(APP_BROWSER)
    return frozenset(found)


def _has_display() -> bool:
    """Is there a graphical session to open a window into?"""
    if sys.platform == "darwin":
        return True
    if sys.platform.startswith("win"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def find_app_browser() -> str | None:
    """The first Chromium-family browser this machine has, or ``None``."""
    for command in APP_BROWSER_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    for path in APP_BROWSER_PATHS:
        if os.access(path, os.X_OK):
            return path
    return None


def strategy_for(
    backend: str,
) -> Callable[[str, Callable[[], None], threading.Event], None] | None:
    """How to open `backend`, or ``None`` when there is no window to open.

    Resolved through this function rather than bound at import, so a caller can
    be handed a different strategy without a GUI existing anywhere.
    """
    if backend == WEBVIEW:
        return open_webview
    if backend == APP_BROWSER:
        return open_app_browser
    return None


def open_webview(
    url: str, on_closed: Callable[[], None], close_requested: threading.Event
) -> None:
    """Show `url` in a native webview, and block until the window is gone.

    Must be called on the main thread: every toolkit underneath insists on it,
    and pywebview refuses outright anywhere else. Which is exactly why the close
    request is watched from a thread of its own -- this one is inside the
    toolkit's loop until the window goes away.
    """
    # Imported here, not at the top: `rhi --no-window` on a machine that has
    # never heard of pywebview must simply work, and this module is imported on
    # every run.
    try:
        import webview  # noqa: PLC0415 - lazy on purpose, see above
    except Exception as exc:  # noqa: BLE001 - a reason, not a traceback
        raise WindowUnavailable(f"pywebview is not usable here: {exc}") from None

    try:
        opened = webview.create_window(
            WINDOW_TITLE, url, width=WINDOW_WIDTH, height=WINDOW_HEIGHT
        )
        _destroy_when_asked(opened, close_requested)
        webview.start()
    except Exception as exc:  # noqa: BLE001 - a missing WebKit is a reason too
        raise WindowUnavailable(f"the webview could not be opened: {exc}") from None
    on_closed()


def _destroy_when_asked(opened: object, close_requested: threading.Event) -> None:
    """Tear a webview window down when the daemon asks, from another thread.

    It has to be another thread: the request arrives while this process is
    somewhere inside WebKitGTK's or Cocoa's main loop, which is C code that owes
    the interpreter nothing. Whether the toolkit really honours a destroy from
    off its own thread is the one thing here no headless suite can answer -- it
    is measured by a person on a real desktop session -- so the call is made
    defensively and never becomes an exception of its own.
    """

    def watch() -> None:
        close_requested.wait()
        with contextlib.suppress(Exception):
            opened.destroy()

    threading.Thread(target=watch, name=CLOSE_WATCH_THREAD_NAME, daemon=True).start()


def open_app_browser(
    url: str, on_closed: Callable[[], None], close_requested: threading.Event
) -> None:
    """Show `url` in an installed browser's app mode, and block until it exits."""
    browser = find_app_browser()
    if browser is None:
        raise WindowUnavailable(
            "no Chromium-family browser was found to open a window with"
        )
    profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
    try:
        borrowed = subprocess.Popen(app_browser_argv(browser, url, profile))
    except OSError as exc:
        shutil.rmtree(profile, ignore_errors=True)
        raise WindowUnavailable(f"{browser} could not be started: {exc}") from None
    try:
        _await_browser(borrowed, close_requested)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    on_closed()


def _await_browser(borrowed: subprocess.Popen, close_requested: threading.Event) -> None:
    """Wait for the borrowed browser, or end it when the daemon asks.

    Polled rather than waited on outright, because there are two things to watch
    at once: the user closing the window, and the daemon stopping for a reason of
    its own. `Popen` rather than `subprocess.run` for the same reason -- there has
    to be a handle left to terminate.
    """
    while borrowed.poll() is None:
        if close_requested.wait(CLOSE_POLL_SECONDS):
            _end_browser(borrowed)
            return


def _end_browser(borrowed: subprocess.Popen) -> None:
    """Ask the browser to quit, and insist if it does not."""
    with contextlib.suppress(Exception):
        borrowed.terminate()
    try:
        borrowed.wait(timeout=CLOSE_GRACE_SECONDS)
    except Exception:  # noqa: BLE001 - a window that will not go is killed
        with contextlib.suppress(Exception):
            borrowed.kill()
        with contextlib.suppress(Exception):
            borrowed.wait(timeout=CLOSE_GRACE_SECONDS)


def app_browser_argv(browser: str, url: str, profile: str) -> list[str]:
    """The command line that opens `url` as a window of its own.

    Two arguments are load-bearing. `--app=` is what removes the tab strip, the
    omnibox and the bookmarks, without which this is a browser tab -- the one
    thing the requirement rules out. A private `--user-data-dir` is what makes
    the process *dedicated*: without it, a browser already running hands the URL
    to itself and the command returns immediately, so `rhi` would think a window
    is open while owning no process at all, and closing that window would end
    nothing.

    No credential appears anywhere in it, deliberately: a command line is world
    readable on Linux.
    """
    return [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
