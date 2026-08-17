"""Contract tests (RED) for the direction that is missing: daemon -> window.

Motivation, found and flagged by the implementer rather than hidden. Requirement
5 -- closing the window ends the run -- works, and `tests/test_window_lifecycle.
py` pins it: a strategy is handed `(url, on_closed)`, and `on_closed` resolves
the one stop future inside `run()`. The signal only ever travels **window ->
daemon**.

Nothing travels the other way. When the daemon stops for any *other* reason -- a
terminal's SIGTERM, a `kill`, a supervisor recycling the process -- there is no
handle on the open window at all. What stands in for one today is
`cli._interrupt_on_terminate`, which installs a SIGTERM handler that raises
`KeyboardInterrupt` so the main thread unblocks out of the strategy call. The
implementer's own assessment of it: *best-effort, and only reliable if the
toolkit's loop lets a Python signal handler run.* A GTK or Cocoa main loop may
never let it run -- both spend their time inside C code that does not return to
the interpreter between events -- and in that case a windowed `rhi` can only be
quit by closing its window. `kill` leaves a window on screen over a daemon that
has already gone, or, worse, a process that never exits at all.

So the reverse half becomes a real mechanism instead of a hope.

**The shape: a `close_requested` event, passed in as a third parameter.** Both
of the shapes on the table were considered.

  * *A returned close handle* cannot work, and the reason is structural rather
    than stylistic: a strategy **blocks until its window is gone** (that is its
    whole contract -- `webview.start()` does not return until the window closes,
    and `subprocess.run` does not return until the browser exits). A function
    that returns only after the window is dead cannot return a handle for
    closing it. Making it return one would mean splitting every strategy into a
    non-blocking start plus a wait, which is a second lifecycle to get wrong.
  * *An event passed in* costs one parameter and no restructuring. It is
    `threading.Event` because the request is raised on a different thread from
    the one the strategy owns -- the daemon runs on a worker, the signal lands on
    the main thread inside the toolkit's loop -- and an Event is the standard
    library's cross-thread latch, with a `wait(timeout)` an idle callback can
    poll and an `is_set()` a loop condition can read. It is also **inert**: it
    carries no capability into the window module, which matters because
    `tests/test_window_backend.py` polices that module for exactly that. That
    file's parameter allow-list is updated to three names and stays an
    allow-list; nothing about the token rules is weakened.

It is a third parameter rather than, say, an attribute hung on `on_closed`,
precisely so it stays visible to that allow-list. A callable smuggling state on
its attributes is how a credential would eventually arrive.

**Still one teardown, one more trigger.** The event does not stop anything: it
asks the window to go away, the window then calls `on_closed` exactly as it does
when the user closes it, and `on_closed` resolves the same future SIGINT already
resolves. `tests/test_window_lifecycle.py::test_no_teardown_lives_outside_run`
keeps guarding the other half, and the socket assertion below is the evidence
that the real teardown ran rather than a second one bolted on.

**The fake window is deaf to signals, on purpose.** That is the entire point: a
strategy that ends on `KeyboardInterrupt` would pass these tests against today's
code and prove nothing. The fake below swallows `KeyboardInterrupt` and keeps
waiting, which is the observable behaviour of a toolkit loop that never lets a
Python handler run. Only the new mechanism can end it.

**What remains unverifiable here, and must not be faked:** whether a *real* GTK
or Cocoa loop honours the mechanism -- whether `webview.destroy_window()` from a
worker thread actually tears the window down on WebKitGTK, and whether a
Chromium started in app mode dies on the terminate this implies. No headless
suite can answer that; the spike harness, re-run by a person on a real desktop
session and on macOS, is the only evidence. What is pinned here is that the
request is *made*, that it is made in time, and that the run ends because of it.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import inspect
import signal
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from rhi_process import URL, start

#: How long `rhi` is given to import websockets and watchdog, seed a nearly
#: empty directory, bind and print. Generous; the imports dominate.
STARTUP_TIMEOUT_SECONDS = 90.0

#: How long the process is given to exit once it has been signalled. A process
#: that has been told to stop and has not is the defect this file is about, so
#: this is short enough to be a failure rather than a wait.
EXIT_TIMEOUT_SECONDS = 30.0

#: What the fake window prints the instant it opens, carrying whether it was
#: already being asked to close. A window asked to close before it has opened
#: would flash and vanish, which reads as a crash.
OPENED = "WINDOW-OPENED"

#: What the fake window prints when, and only when, it is asked to close through
#: the new channel. Its absence is the whole defect.
ASKED_TO_CLOSE = "WINDOW-ASKED-TO-CLOSE"

#: A window that opens, reports the state of its close channel, and then behaves
#: like a GUI toolkit whose main loop never lets a Python signal handler run:
#: `KeyboardInterrupt` is caught and discarded, so the workaround in `cli` --
#: raise an interrupt and hope the strategy unwinds -- cannot end it. Only
#: `close_requested` can.
DEAF_TO_SIGNALS = f'''\
import rhizome_graph.window as window

window.choose_window_backend = lambda *a, **k: "webview"


def _strategy(url, on_closed, close_requested):
    print("{OPENED} set=" + str(close_requested.is_set()), flush=True)
    while not close_requested.is_set():
        try:
            close_requested.wait(0.05)
        except KeyboardInterrupt:
            pass
    print("{ASKED_TO_CLOSE}", flush=True)
    on_closed()


window.strategy_for = lambda backend: _strategy
'''


@dataclass
class Signalled:
    """One `rhi` with a deaf window, signalled, watched to the end."""

    signum: int
    url: str
    returncode: int | None
    stdout: str
    stderr: str
    socket_after_exit: bool


def _wait_for_exit(running, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = running.process.poll()
        if code is not None:
            return int(code)
        time.sleep(0.1)
    return None


@pytest.fixture(scope="module", params=[signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def signalled(request, tmp_path_factory: pytest.TempPathFactory) -> Signalled:
    """One `rhi <dir>` with a window that ignores signals, then a signal.

    Both signals, because the two arrive by different routes and only one of
    them has ever been thought about: SIGINT is Ctrl-C in the terminal the
    window was launched from, SIGTERM is `kill`, a supervisor, a logout or a
    package upgrade. Neither may leave a window open over a dead daemon, and
    neither may leave a process that cannot be quit.
    """
    root = tmp_path_factory.mktemp("observed")
    ingest = tmp_path_factory.mktemp("run") / "ingest.sock"
    running = start((str(root), "--socket", str(ingest)), prelude=DEAF_TO_SIGNALS)
    try:
        url = running.wait_for_line(URL, STARTUP_TIMEOUT_SECONDS).group(0).rstrip(".,")
        running.process.send_signal(request.param)
        returncode = _wait_for_exit(running, EXIT_TIMEOUT_SECONDS)
    finally:
        if running.is_alive():
            running.stop()
    return Signalled(
        signum=int(request.param),
        url=url,
        returncode=returncode,
        stdout=running.out,
        stderr=running.err,
        socket_after_exit=Path(ingest).exists(),
    )


# ===========================================================================
# D6 -- the missing direction
# ===========================================================================


def test_a_daemon_that_is_stopping_asks_its_window_to_close(
    signalled: Signalled,
) -> None:
    """THE assertion of this file: the signal reaches the window as a request.

    Today the only thing travelling this way is a `KeyboardInterrupt` the
    toolkit is free to swallow, so a window that swallows it -- as this one
    deliberately does -- is never told anything at all.
    """
    assert ASKED_TO_CLOSE in signalled.stdout, (
        f"rhi was sent {signal.Signals(signalled.signum).name} with a window "
        "open and the window was never asked to close\n"
        f"--- stdout ---\n{signalled.stdout}"
        f"--- stderr ---\n{signalled.stderr}"
    )


def test_a_window_is_not_asked_to_close_before_it_has_opened(
    signalled: Signalled,
) -> None:
    """An event handed over already set is a window that flashes and vanishes.

    Cheap to get wrong -- one event reused across runs, or one set by the
    launcher on the way in -- and it looks exactly like a crash on startup.
    """
    assert f"{OPENED} set=False" in signalled.stdout, (
        "the close channel was already set when the window opened\n"
        f"--- stdout ---\n{signalled.stdout}"
    )


def test_rhi_exits_when_a_window_that_ignores_signals_is_open(
    signalled: Signalled,
) -> None:
    """A program that can only be quit by closing its window is a program that
    a `kill` cannot stop, on precisely the toolkits this one will run on."""
    assert signalled.returncode is not None, (
        f"rhi ignored {signal.Signals(signalled.signum).name} and was still "
        f"running {EXIT_TIMEOUT_SECONDS:.0f}s later\n"
        f"--- stdout ---\n{signalled.stdout}"
        f"--- stderr ---\n{signalled.stderr}"
    )


def test_being_signalled_with_a_window_open_is_a_clean_exit(
    signalled: Signalled,
) -> None:
    """Quitting is not a failure; a launcher and a shell both read this."""
    assert signalled.returncode == 0, signalled.stderr


def test_the_one_teardown_still_runs_when_the_window_is_asked_to_close(
    signalled: Signalled,
) -> None:
    """The proof it is a trigger and not a second shutdown.

    A corpse at the ingest socket is what the next start has to reason about,
    and unlinking it is the step a hand-rolled teardown outside `run()` forgets.
    """
    assert not signalled.socket_after_exit, (
        "the ingest socket outlived the process, so the shutdown that ran was "
        "not the one inside run()"
    )


def test_asking_a_window_to_close_prints_no_traceback(signalled: Signalled) -> None:
    """An ordinary quit says nothing a person has to decode."""
    assert "Traceback" not in signalled.stderr, signalled.stderr


# ===========================================================================
# D6 -- the seam itself
# ===========================================================================


@pytest.mark.parametrize("backend", ["webview", "app_browser"])
def test_every_real_strategy_can_be_asked_to_close(backend: str) -> None:
    """The mechanism is part of the contract, not a favour one backend does.

    A backend written later that takes only `(url, on_closed)` would be
    unstoppable from the daemon side, and nothing would say so until somebody
    tried to `kill` it on a machine nobody here has.
    """
    import rhizome_graph.window as window

    strategy = window.strategy_for(backend)

    assert strategy is not None, f"no strategy for {backend!r}"
    assert "close_requested" in inspect.signature(strategy).parameters, (
        f"the {backend} strategy takes "
        f"{list(inspect.signature(strategy).parameters)} -- there is no way to "
        "ask its window to go away, so a daemon stopped by a signal leaves it "
        "on screen"
    )
