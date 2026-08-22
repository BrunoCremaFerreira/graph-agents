"""Contract tests (RED) for the window ending the run, and for it failing well.

Motivation: two user requirements meet here and pull in opposite directions.

  * **Closing the window ends the whole execution, daemon included.** An
    application whose window is gone but whose process still holds a port, an
    ingest socket and an inotify watch is a leak the user cannot see and did not
    ask for; the next `rhi` then finds its own default socket occupied and its
    port taken, by a ghost.
  * **`http://localhost:8080` stays reachable in an ordinary browser.** The
    window is one viewer, not the only one, and the daemon is what the page and
    the hooks talk to.

**One shutdown path, three triggers.** Everything real about stopping -- the
signal handlers, cancelling the polls, `session.stop()`, unlinking the socket --
already lives inside `run()` and resolves a single `stop` future; SIGTERM and an
embedded caller's cancellation already converge on it. A window closing is the
third trigger and it must resolve **that same future**, never grow a second
teardown in `cli.py`. This is the precedent `closeView` set in `main.ts`, where
Escape and the close button both run through one function: "both paths run
through the same `closeView`, never two."

So the convergence itself is asserted, not merely its outcome. A test that
watched the port stop accepting would pass just as happily against a second
teardown bolted onto the launcher, and would leave the two free to drift until
one of them forgets to unlink the socket. `test_the_window_resolves_the_same_
future_a_signal_resolves` reaches for the future handed to `_install_stop_
signals` and asserts the window resolved *it*; `test_no_teardown_lives_outside_
run` asserts the second path was never written.

**A failed window degrades to headless, loudly.** Requirement 4 -- reachable in
an ordinary browser -- does not depend on the window, so an `rhi` that dies
because WebKitGTK is missing has failed a requirement it could have met, and it
fails on exactly the machines least able to diagnose it: headless servers, SSH
sessions, containers, and this very host. But quiet degradation is its own
defect with a precedent in this repository: `start.sh` silently serving a stale
`dist` when node is missing, which CLAUDE.md flags as too quiet to be right. So
the URL goes to stdout and the reason goes to stderr, and the exit status
follows the rule the port and the ingest socket already follow -- **a default may
be adjusted, an explicit request may not.** No flag means the window was a
default and its absence is a degradation; `--window` means somebody asked, and
an unmet explicit request is a refusal.

**The strategy is injected, and that is what makes any of this testable.** No
GUI is opened anywhere in this file: `rhizome_graph.window.strategy_for` is
replaced with a fake that closes immediately, or never, or raises. The seam is
therefore part of the specified surface -- `cli` must resolve it through the
module at call time, not bind it with `from ... import strategy_for` at import,
or a fake installed before `main()` runs would never be seen.

**What is NOT specified here, and must not be faked:** that a window appears,
renders the graph, and disappears. No test can assert a GL surface on somebody's
desktop; the spike harness re-run against the shipped code on a real Linux
desktop session and on macOS is the only evidence for that, and for the reverse
half of "the daemon does not outlive the window" -- that killing `rhi` takes the
app-mode browser child down with it.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import ast
import asyncio
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest

import daemon.server as server
import rhizome_graph.cli as cli
import rhizome_graph.launch as launch
from daemon.server import run
from daemon_probe import (
    SHUTDOWN_TIMEOUT_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
    accepts,
    drive,
    scrub,
    settings_for,
)
from rhi_process import (
    NO_FRONT_END_REASON,
    REPO_ROOT,
    URL,
    clean_environment,
    entry_argv,
    front_end_is_built,
    get,
    require_front_end,
    start,
)

#: How long `rhi` is given to import websockets and watchdog, seed a nearly
#: empty directory, bind and print. Generous; the imports dominate.
STARTUP_TIMEOUT_SECONDS_PROCESS = 90.0

#: How long a process is given to exit once it has been asked to, or once its
#: window has closed. A process that has been told to stop and has not is the
#: defect, so this is short enough to be a failure rather than a wait.
EXIT_TIMEOUT_SECONDS = 30.0

#: What a strategy that cannot open a window says, and what must therefore reach
#: stderr verbatim. A reason nobody can act on is the same as no reason.
FAILURE_REASON = "no display: WebKitGTK could not be initialised"


# --- the injected strategies, spelled once ----------------------------------
#
# These run inside the `rhi` subprocess, after `rhizome_graph.cli` is imported
# and before `main()` is called. `choose_window_backend` is forced too: this
# host has neither pywebview nor a browser, so an honest answer would be `none`
# and no strategy would ever be consulted.

_FORCE_BACKEND = """\
import rhizome_graph.window as window
window.choose_window_backend = lambda *a, **k: {backend!r}
"""

#: `*rest` on every fake below, deliberately. A strategy is handed a third
#: argument -- `close_requested`, the channel a stopping daemon asks its window
#: to go away through (`tests/test_window_close_request.py`) -- and not one
#: assertion in this file is about it. Swallowing it keeps these specifications
#: about the lifecycle they were written for instead of about an arity.
CLOSES_IMMEDIATELY = _FORCE_BACKEND.format(backend="webview") + """\
def _strategy(url, on_closed, *rest):
    on_closed()

window.strategy_for = lambda backend: _strategy
"""

#: A window that opens and stays open -- and, crucially, one whose strategy call
#: **blocks**, because that is what a real one does: `webview.start()` does not
#: return until the window closes. A blocking window on the event loop's thread
#: would freeze the daemon, so the page would stop answering and the interrupt
#: would never be handled. That is what the two tests using this catch.
NEVER_CLOSES = _FORCE_BACKEND.format(backend="webview") + """\
def _strategy(url, on_closed, *rest):
    import threading

    threading.Event().wait()
"""  + "\nwindow.strategy_for = lambda backend: _strategy\n"

CANNOT_OPEN = _FORCE_BACKEND.format(backend="webview") + f"""\
def _strategy(url, on_closed, *rest):
    raise RuntimeError({FAILURE_REASON!r})

window.strategy_for = lambda backend: _strategy
"""

NOTHING_AVAILABLE = _FORCE_BACKEND.format(backend="none")


@dataclass
class Run:
    """Everything one `rhi` process had to say, start to finish."""

    url: str
    returncode: int | None
    stdout: str
    stderr: str
    fetched: int


def _fetch(url: str, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _body = get(url)
        except Exception:  # noqa: BLE001 - not accepting yet is not an answer
            time.sleep(0.2)
            continue
        return status
    return 0


def _wait_for_exit(running, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = running.process.poll()
        if code is not None:
            return int(code)
        time.sleep(0.1)
    return None


# ===========================================================================
# D4 -- closing the window ends everything
# ===========================================================================


# --- 1. in process: the seam itself -----------------------------------------


def test_closing_the_window_ends_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole requirement, at the one place it is decided.

    `run()` must *return*, not be cancelled: a caller that reached for
    `task.cancel()` instead of the stop the daemon handed it would raise
    `CancelledError` out of here, which is what distinguishes the two.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path)

    async def scenario():
        await asyncio.wait_for(
            run(settings, ready=lambda ready: ready.stop()),
            timeout=STARTUP_TIMEOUT_SECONDS + SHUTDOWN_TIMEOUT_SECONDS,
        )

    drive(scenario())


def test_closing_the_window_stops_the_daemon_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is left holding the port the next `rhi` will want."""
    scrub(monkeypatch)
    settings = settings_for(tmp_path)

    async def scenario():
        await run(settings, ready=lambda ready: ready.stop())
        assert not accepts(settings.host, settings.port)

    drive(scenario())


def test_closing_the_window_removes_the_ingest_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof the real teardown ran, not merely that the coroutine returned.

    A corpse at the socket path is exactly what the next start has to reason
    about, and it is the part a second, hand-rolled shutdown would forget.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path)

    async def scenario():
        await run(settings, ready=lambda ready: ready.stop())
        assert not Path(settings.socket_path).exists()

    drive(scenario())


def test_the_window_resolves_the_same_future_a_signal_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One shutdown path, three triggers -- asserted, not assumed.

    `_install_stop_signals` is handed the future SIGINT and SIGTERM resolve.
    Intercepting it is the only way to say "the window resolved *that one*"
    rather than "the daemon stopped somehow", and the difference is a second
    teardown path nobody notices until it drifts.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path)
    futures: list[asyncio.Future] = []
    monkeypatch.setattr(server, "_install_stop_signals", futures.append)

    async def scenario():
        await run(settings, ready=lambda ready: ready.stop())
        assert len(futures) == 1, "run() no longer offers one future to stop on"
        assert futures[0].done(), (
            "the window closed and the daemon stopped, but the future SIGTERM "
            "resolves is still pending: shutdown has grown a second path"
        )

    drive(scenario())


def test_stopping_twice_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C on a window that is also closing is an ordinary Tuesday.

    Two triggers racing must not surface as `InvalidStateError` from inside the
    event loop, which is a traceback the user can neither read nor prevent.
    """
    scrub(monkeypatch)
    settings = settings_for(tmp_path)

    def close_twice(ready) -> None:
        ready.stop()
        ready.stop()

    async def scenario():
        await run(settings, ready=close_twice)

    drive(scenario())


def test_the_daemon_can_be_stopped_from_the_thread_the_window_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GUI toolkit owns the main thread and calls back from it, not from the
    loop -- so `stop` is called from a foreign thread or it is not usable."""
    scrub(monkeypatch)
    settings = settings_for(tmp_path)

    def close_from_elsewhere(ready) -> None:
        threading.Thread(target=ready.stop, daemon=True).start()

    async def scenario():
        await asyncio.wait_for(
            run(settings, ready=close_from_elsewhere),
            timeout=STARTUP_TIMEOUT_SECONDS + SHUTDOWN_TIMEOUT_SECONDS,
        )

    drive(scenario())


# --- 2. structural: the second path was never written -----------------------


def _attributes_and_names(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Name):
            found.add(node.id)
    return found


#: Vocabulary that belongs to `run()`'s teardown and to nothing else. `stop` is
#: deliberately absent: `cli` legitimately hands `ready.stop` to a strategy as
#: the close callback, which is the convergence rather than a breach of it.
TEARDOWN_VOCABULARY = {"unlink", "cancel", "Session", "add_signal_handler", "FsWatcher"}


@pytest.mark.parametrize("module", [cli, launch], ids=["cli", "launch"])
def test_no_teardown_lives_outside_run(module) -> None:
    """Shutdown is `run()`'s, entire. The launcher and the CLI only ask for it.

    `launch.py` cancels a herald task today, which is the readiness poll stage D
    replaces; once the daemon announces its own readiness there is nothing left
    out here with a lifecycle of its own.
    """
    source = Path(module.__file__).read_text(encoding="utf-8")

    offenders = sorted(_attributes_and_names(source) & TEARDOWN_VOCABULARY)

    assert offenders == [], (
        f"{module.__name__} performs teardown of its own: {offenders}. Signals, "
        "cancellation and the window all resolve one future inside run(); a "
        "second path is one that forgets to unlink the socket."
    )


# ===========================================================================
# D4 -- the same thing, through the command the user actually types
# ===========================================================================


@pytest.fixture(scope="module")
def closed_immediately(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """One `rhi <dir>` whose window closes the instant it opens."""
    root = tmp_path_factory.mktemp("observed")
    ingest = tmp_path_factory.mktemp("run") / "ingest.sock"
    running = start(
        (str(root), "--socket", str(ingest)),
        prelude=CLOSES_IMMEDIATELY,
    )
    try:
        url = running.wait_for_line(
            URL, STARTUP_TIMEOUT_SECONDS_PROCESS
        ).group(0).rstrip(".,")
        returncode = _wait_for_exit(running, EXIT_TIMEOUT_SECONDS)
    finally:
        if running.is_alive():
            running.stop()
    return Run(
        url=url,
        returncode=returncode,
        stdout=running.out,
        stderr=running.err,
        fetched=0,
    )


def test_closing_the_window_ends_the_whole_execution(closed_immediately: Run) -> None:
    """User requirement 5, end to end: nobody had to press Ctrl-C."""
    assert closed_immediately.returncode is not None, (
        "rhi was still running after its window closed\n"
        f"--- stdout ---\n{closed_immediately.stdout}"
        f"--- stderr ---\n{closed_immediately.stderr}"
    )


def test_a_closed_window_is_a_clean_exit(closed_immediately: Run) -> None:
    """Quitting is not a failure; a launcher and a shell both read this."""
    assert closed_immediately.returncode == 0, closed_immediately.stderr


def test_a_closed_window_leaves_the_port_free(closed_immediately: Run) -> None:
    """The leak the requirement exists to prevent, observed from outside."""
    parsed = urlparse(closed_immediately.url)

    assert not accepts(parsed.hostname or "", parsed.port or 0)


def test_closing_the_window_prints_no_traceback(closed_immediately: Run) -> None:
    """An ordinary quit says nothing a person has to decode."""
    assert "Traceback" not in closed_immediately.stderr, closed_immediately.stderr


@pytest.fixture(scope="module")
def window_open(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """One `rhi <dir>` with a window that opens and never closes, then Ctrl-C."""
    root = tmp_path_factory.mktemp("observed")
    ingest = tmp_path_factory.mktemp("run") / "ingest.sock"
    running = start((str(root), "--socket", str(ingest)), prelude=NEVER_CLOSES)
    try:
        url = running.wait_for_line(
            URL, STARTUP_TIMEOUT_SECONDS_PROCESS
        ).group(0).rstrip(".,")
        fetched = _fetch(url)
        running.process.send_signal(signal.SIGINT)
        returncode = _wait_for_exit(running, EXIT_TIMEOUT_SECONDS)
    finally:
        if running.is_alive():
            running.stop()
    return Run(
        url=url,
        returncode=returncode,
        stdout=running.out,
        stderr=running.err,
        fetched=fetched,
    )


def test_the_page_stays_reachable_in_an_ordinary_browser(window_open: Run) -> None:
    """User requirement 4: the window is one viewer, not the only one.

    Stands down where nobody has built the front end: with no `web/dist` the
    daemon answers 503 for the page by design -- that is the `--dev` path, with
    Vite hosting it -- so a red here would blame this code for an unbuilt
    checkout. `NO_FRONT_END_REASON` in `tests/rhi_process.py` says what to run.
    The fixture is untouched, so the two assertions beside this one -- that Ctrl-C
    still quits with a window open, and prints no traceback -- keep running.
    """
    require_front_end()

    assert window_open.fetched == 200, (
        f"{window_open.url} answered {window_open.fetched} while a window was "
        f"open\n--- stderr ---\n{window_open.stderr}"
    )


def test_ctrl_c_still_quits_with_a_window_open(window_open: Run) -> None:
    """The terminal keeps its say. A window must not capture the interrupt."""
    assert window_open.returncode == 0, (
        f"rhi answered SIGINT with {window_open.returncode}\n"
        f"--- stderr ---\n{window_open.stderr}"
    )


def test_ctrl_c_with_a_window_open_prints_no_traceback(window_open: Run) -> None:
    """`KeyboardInterrupt` unwinding through a GUI callback is the risk here."""
    assert "Traceback" not in window_open.stderr, window_open.stderr


# ===========================================================================
# D5 -- a window that cannot open degrades to headless, loudly
# ===========================================================================


@pytest.fixture(scope="module")
def window_failed(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """One `rhi <dir>` -- no flag -- whose window strategy raises on start."""
    root = tmp_path_factory.mktemp("observed")
    ingest = tmp_path_factory.mktemp("run") / "ingest.sock"
    running = start((str(root), "--socket", str(ingest)), prelude=CANNOT_OPEN)
    try:
        url = running.wait_for_line(
            URL, STARTUP_TIMEOUT_SECONDS_PROCESS
        ).group(0).rstrip(".,")
        fetched = _fetch(url)
        returncode = running.stop()
    finally:
        if running.is_alive():
            running.stop()
    return Run(
        url=url,
        returncode=returncode,
        stdout=running.out,
        stderr=running.err,
        fetched=fetched,
    )


def test_a_window_that_cannot_open_still_serves_the_page(window_failed: Run) -> None:
    """Requirement 4 does not depend on the window, so it must not die with it.

    Unless there is no page to serve at all, which is not the window's doing and
    not a defect: see `NO_FRONT_END_REASON` in `tests/rhi_process.py`. The rest
    of the degradation -- the URL, the reason on stderr, no traceback, exit 0 --
    is asserted by the four tests below from the same run, and none of them
    stands down.
    """
    require_front_end()

    assert window_failed.fetched == 200, (
        f"{window_failed.url} answered {window_failed.fetched}\n"
        f"--- stderr ---\n{window_failed.stderr}"
    )


def test_a_window_that_cannot_open_still_prints_the_url(window_failed: Run) -> None:
    """The URL comes first, so a window that blows up cannot swallow it."""
    assert URL.search(window_failed.stdout) is not None, window_failed.stdout


def test_a_window_that_cannot_open_says_why_on_stderr(window_failed: Run) -> None:
    """Loud, because the quiet version is `start.sh` serving a stale `dist`."""
    assert FAILURE_REASON in window_failed.stderr, window_failed.stderr


def test_the_reason_is_not_a_traceback(window_failed: Run) -> None:
    """A reason somebody can act on, not twenty frames of GUI internals."""
    assert "Traceback" not in window_failed.stderr, window_failed.stderr


def test_a_window_nobody_asked_for_failing_is_not_a_failed_run(
    window_failed: Run,
) -> None:
    """A default may be adjusted: no flag was given, so none was disappointed."""
    assert window_failed.returncode == 0, window_failed.stderr


def _refusal(tmp_path: Path, prelude: str, argv: tuple[str, ...]):
    """One `rhi` run to completion, with a guard against a false green.

    A refusal is a non-zero exit, and so is a crash -- including the crash of the
    injected prelude itself while `rhizome_graph.window` does not exist. So the
    interpreter is required to have got past its imports before any assertion
    about the exit status is allowed to mean anything.
    """
    completed = subprocess.run(
        entry_argv((str(tmp_path), "--socket", str(tmp_path / "ingest.sock"), *argv), prelude),
        cwd=str(REPO_ROOT),
        env=clean_environment(),
        capture_output=True,
        text=True,
        timeout=EXIT_TIMEOUT_SECONDS + STARTUP_TIMEOUT_SECONDS_PROCESS,
    )
    if "Traceback" in completed.stderr:
        pytest.fail(
            "rhi did not refuse -- it crashed before it could:\n" + completed.stderr
        )
    return completed


def test_an_explicitly_requested_window_that_cannot_open_refuses(
    tmp_path: Path,
) -> None:
    """An explicit request may not be adjusted -- the port's rule, verbatim.

    `--window` means somebody said it. Serving on quietly would report success
    for something that did not happen.
    """
    completed = _refusal(tmp_path, CANNOT_OPEN, ("--window",))

    assert 0 < completed.returncode < 128, (
        f"`rhi --window` with an unopenable window exited "
        f"{completed.returncode}\n--- stderr ---\n{completed.stderr}"
    )


def test_an_explicit_refusal_names_the_reason(tmp_path: Path) -> None:
    """The actionable part, exactly as the busy-port refusal names its port."""
    completed = _refusal(tmp_path, CANNOT_OPEN, ("--window",))

    assert FAILURE_REASON in completed.stderr, completed.stderr


def test_an_explicit_refusal_prints_no_traceback(tmp_path: Path) -> None:
    completed = _refusal(tmp_path, CANNOT_OPEN, ("--window",))

    assert "Traceback" not in completed.stderr, completed.stderr


def test_an_explicit_window_with_no_backend_at_all_refuses(tmp_path: Path) -> None:
    """Nothing raised: there was simply nothing to open, which is the same no.

    This is the headless server, the SSH session and this very host, and it is
    the case a caller reaches only because `choose_window_backend` answers what
    *can* open rather than what *should* be done about it.
    """
    completed = _refusal(tmp_path, NOTHING_AVAILABLE, ("--window",))

    assert 0 < completed.returncode < 128, (
        f"`rhi --window` on a machine that can open no window exited "
        f"{completed.returncode}\n--- stderr ---\n{completed.stderr}"
    )


def test_no_backend_at_all_without_the_flag_is_not_a_refusal(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The mirror: on this very host, plain `rhi` must still serve headless.

    Nothing was requested, so nothing was denied -- the daemon runs until it is
    asked to stop, exactly as `--no-window` would have.

    Only the served page needs a built front end. That it kept running rather
    than refusing -- which is what this test is named for -- is asserted with or
    without one, so the skip is declared at the very end and takes nothing with
    it. Without `web/dist` the 503 is deliberate (`--dev` has Vite host the
    page), so it is an incomplete environment, not a regression; see
    `NO_FRONT_END_REASON` in `tests/rhi_process.py`.
    """
    served = front_end_is_built()
    ingest = tmp_path_factory.mktemp("run") / "ingest.sock"
    running = start((str(tmp_path), "--socket", str(ingest)), prelude=NOTHING_AVAILABLE)
    try:
        url = running.wait_for_line(
            URL, STARTUP_TIMEOUT_SECONDS_PROCESS
        ).group(0).rstrip(".,")
        status = _fetch(url)
        if served:
            assert status == 200, f"{url} answered {status}"
        assert running.is_alive(), "rhi gave up because it could open no window"
    finally:
        returncode = running.stop()

    assert returncode == 0, running.err

    if not served:
        pytest.skip(NO_FRONT_END_REASON)
