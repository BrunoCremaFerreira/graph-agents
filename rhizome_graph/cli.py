"""The daemon's configuration, as a value instead of as the surrounding air.

`daemon/server.py` used to learn what it was by reading `os.environ` in four
places, two of them deep inside the machinery. That works for exactly one caller
shape -- a shell script that exports variables and execs a module -- and breaks
the moment there is a second front door: a command on `$PATH` started in
whatever directory the user happens to be in, with a positional directory and
flags. Making the environment an internal protocol between two halves of one
program would leave the configuration invisible in a stack trace, untestable
without a subprocess, and impossible to build twice in one process.

So the configuration is a frozen :class:`Settings`, and :func:`settings_from` is
the one function that builds it. Three rules shape it:

  * **Flag beats environment beats default**, for every field. One rule, so a
    second front door can rely on it.
  * **Pure and total.** No filesystem, no `sys.exit`, no exception: every value
    here is one somebody can export by hand, and a typo in an optional knob must
    not cost a daemon its boot. `cwd` is a parameter for the same reason `home`
    is one in :mod:`rhizome_graph.paths` -- a function that reads the process's
    own working directory cannot be tested from another one.
  * **Nothing from the daemon side.** No `asyncio`, no `websockets`, no
    `watchdog`, no `daemon`: printing `--help` or refusing a bad flag must not
    first import an event loop, a WebSocket stack and an inotify backend.

That last rule is why the daemon's defaults are spelled here and imported by
`daemon/server.py` rather than the other way round. They are one value each, in
one place; two spellings of the same default drift, and then one of them is
wrong.

:func:`main` is the installed `rhi` command, and it is the one scope here that
reads the process environment, mirroring `daemon/server.py`'s rule. It decides --
purely, through :func:`choose_port` and :func:`ingest_socket_path` -- which port
and which ingest socket this instance may have, and only then hands a finished
:class:`Settings` to :mod:`rhizome_graph.launch`. The launcher exists because
running a daemon means `asyncio` and `daemon.server`, and importing either of
those to print `--help` is what the last rule above forbids.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import queue
import shlex
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from rhizome_graph import hookinstall
from rhizome_graph.assets import hook_command
from rhizome_graph.ipc import port_is_free, socket_is_live
from rhizome_graph.paths import expand_user
from rhizome_graph.token import token_from_env

#: Where hooks connect to hand their events over.
DEFAULT_SOCKET_PATH = "/tmp/rhizome-graph.sock"

#: The one port that serves the page and the WebSocket alike.
DEFAULT_HTTP_PORT = 8080

#: How often the working tree is re-read for the status panel. Zero or negative
#: means the poll is not started at all.
DEFAULT_STATUS_INTERVAL_SECONDS = 3.0

#: What an installed command binds when nobody says otherwise. Loopback, because
#: a command started casually should not open a perimeter nobody asked for; SSH
#: and VS Code forwarding both arrive here and are unaffected.
DEFAULT_HOST = "127.0.0.1"

DEFAULT_LOG_LEVEL = "INFO"

#: The distribution `importlib.metadata` is asked about for ``--version``. The
#: number itself is never spelled here: a literal and `pyproject.toml` drift, and
#: the version quoted in a bug report is then not the one that shipped.
DISTRIBUTION_NAME = "rhizome-graph"

#: What ``--version`` says when there is no installed distribution to ask -- a
#: checkout run straight off `sys.path`. Deliberately not a version number: "I
#: could not find out" and "0.0.0" are different facts.
UNKNOWN_VERSION = "unknown"

#: How many ports the search may try, counting the preferred one. More attempts
#: than a desk ever needs, fewer than a person will wait for: twenty refused
#: binds on loopback are microseconds, and if the whole block is taken then the
#: machine is saying something a twenty-first attempt will not change.
PORT_SEARCH_LIMIT = 20

#: The highest port number there is. A walk that runs past it asks the operating
#: system about an address that cannot exist.
MAX_PORT = 65535

#: What an ingest socket is called, and how a person reading a settings file
#: recognises one.
SOCKET_SUFFIX = ".sock"

#: How much of the root's digest goes into a derived socket name. An AF_UNIX
#: address is limited to about 108 bytes, so the root is hashed rather than
#: spelled; ten hex characters distinguish every checkout on a machine.
INSTANCE_DIGEST_LENGTH = 10

#: Addresses that mean "every interface", which is not something a browser can
#: be pointed at.
WILDCARD_HOSTS = ("", "0.0.0.0", "::", "*")

#: What to open instead, when the bind address is a wildcard.
LOOPBACK_HOST = "127.0.0.1"

#: What the command line asked for about the window. `auto` is plain `rhi`,
#: `none` is `--no-window`, `window` is an explicit `--window` -- and the
#: difference between the last two and `auto` is the one the port and the ingest
#: socket already draw: a default may be adjusted, an explicit request may not.
WINDOW_UNSPECIFIED = "auto"
WINDOW_DECLINED = "none"
WINDOW_REQUESTED = "window"

#: Refusing an explicit `--window` on a machine that can open none. Named
#: causes, because the reader is on a headless server, in an SSH session or in a
#: container and has to be able to act on it.
NO_BACKEND_REFUSAL = (
    "no window can be opened here: there is no pywebview, no Chromium-family "
    "browser and no display. A window was asked for by name rather than "
    "chosen, so it will not be quietly skipped -- run without --window to serve "
    "the page in the terminal instead."
)

#: The same fact when nobody asked either way: not a refusal, but not a silence
#: either. `--no-window` says nothing at all, having got exactly what it asked
#: for.
NO_BACKEND_NOTE = (
    "no window can be opened here (no pywebview, no Chromium-family browser or "
    "no display), so the page is served in the terminal instead."
)

#: What a window that raised on its way up reports, before the run degrades to
#: headless. Loud, because the quiet version of this is `start.sh` serving a
#: stale `dist` when node is missing.
WINDOW_FAILED = (
    "the window could not be opened, so the page is served without one: {reason}"
)

#: The same failure when somebody asked for the window by name, which is a
#: refusal rather than a degradation.
EXPLICIT_WINDOW_REFUSAL = (
    "--window was asked for by name and the window could not be opened: {reason}"
)

#: Where a settings file lives, under an observed project and under the user's
#: home alike. One spelling, which is what makes "the project's file" and "the
#: user's file" one mechanism rather than two.
SETTINGS_RELATIVE = (".claude", "settings.json")

#: What a settings file with no hook of ours in it is told about itself. One
#: line each, and every line carries the file it is about: with two settings
#: files and possibly two checkouts, a report that lists the files in one place
#: and the commands in another leaves the reader to guess the pairing, and
#: guessing wrong is how the wrong file gets edited.
STATE_CAPTIONS = {
    hookinstall.ABSENT: "no capture hook here",
    hookinstall.FOREIGN: "other PostToolUse hooks here, and none of them ours",
    hookinstall.MALFORMED: "not readable as JSON, so nothing in it can be trusted",
}

#: The healthy verdict, and the only one `rhi --doctor` exits zero on.
HOOKS_WORKING = "attribution is wired up: what an agent does will be shown as its own."

#: The loud failure, and the one this whole command exists for. A command that
#: cannot be executed fails before the hook's own "exit 0 and stay silent" rule
#: can run, so Claude Code reports a blocking hook error on every tool call --
#: however healthy the hook in the other settings file is.
HOOKS_ROTTED = (
    "a hook command above is not there any more, and Claude Code answers every "
    "tool call in this project with a blocking hook error while that is true."
)

#: The quiet failure: the graph fills up with changes that nobody is credited
#: with, which looks exactly like a healthy setup with nobody working.
HOOKS_MISSING = (
    "no working capture hook was found in either file, so the graph will show "
    "changes with nobody on camera."
)

#: How to fix either of them, spelled as the command to type.
HOOKS_REMEDY = "install the hooks with: rhi {root} --install-hooks"

#: Why an unparseable settings file is refused rather than replaced: nobody here
#: can reconstruct what a rewrite would lose.
SETTINGS_UNREADABLE = (
    "{path} is not readable as JSON, so it was left exactly as it was. Repair "
    "it by hand and run this again."
)

#: Said after a successful install, because the change does not take effect in
#: the session reading it: Claude Code reads its settings at startup.
HOOKS_INSTALLED_NOTE = (
    "installed. Claude Code reads its settings when a session starts, so a "
    "session that is already open keeps running without them."
)

#: What the thread running the daemon calls itself, for a stack trace to name.
DAEMON_THREAD_NAME = "rhizome-daemon"

#: What the thread waiting for a signal calls itself. It exists because a GUI
#: toolkit's main loop may never let a Python signal handler run.
SIGNAL_THREAD_NAME = "rhizome-signal"

#: How long the daemon is given to finish once it has been asked to stop, and
#: how often it is looked in on while waiting. Bounded so a daemon that will not
#: stop cannot leave a user with a program that will not quit.
SHUTDOWN_GRACE_SECONDS = 30.0
JOIN_POLL_SECONDS = 0.1

ROOT_ENV = "RHIZOME_PROJECT_ROOT"
HOST_ENV = "RHIZOME_HOST"
PORT_ENV = "RHIZOME_HTTP_PORT"
SOCKET_ENV = "RHIZOME_SOCKET"
LOG_LEVEL_ENV = "RHIZOME_LOG_LEVEL"
STATUS_INTERVAL_ENV = "RHIZOME_STATUS_INTERVAL"
ALLOW_REMOTE_CONTROL_ENV = "RHIZOME_ALLOW_REMOTE_CONTROL"
WEB_DIST_ENV = "RHIZOME_WEB_DIST"
HOME_ENV = "HOME"


@dataclass(frozen=True)
class Settings:
    """Everything one daemon needs to know about itself.

    Frozen: configuration read twice must not be able to differ between the
    reads.

    ``port_is_explicit`` records whether anybody actually asked for this port --
    a flag or an exported variable both count as asking. A port nobody named may
    be moved when it is busy; a port somebody named must not be, because a viewer
    told to open it by hand, or an SSH forward already set up, breaks silently
    when the daemon quietly lands somewhere else.

    ``web_dist`` is empty when there is no override, and stays a string rather
    than becoming a resolved path: deciding which candidate exists is a
    filesystem question, and this value is built without a filesystem (see
    :mod:`rhizome_graph.assets`).
    """

    root: str
    host: str
    port: int
    port_is_explicit: bool
    socket_path: str
    token: str
    status_interval: float
    log_level: str
    allow_remote_control: bool
    web_dist: str


def build_parser() -> argparse.ArgumentParser:
    """The command line this program accepts, built fresh on every call.

    A new parser each time, with no shared global: a caller must not be able to
    change what the next one parses.
    """
    # No `prog`: argparse takes it from how the program was actually invoked, so
    # an installed command names itself and `python -m daemon.server` names the
    # module, instead of one of them advertising the other.
    parser = argparse.ArgumentParser(
        description="Watch a project and draw what each agent does to it.",
    )
    parser.add_argument(
        "root",
        metavar="DIR",
        nargs="?",
        default=None,
        help="the project to observe (default: the current directory)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help=f"address to bind (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"port for the page and the WebSocket (default: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help=f"ingest socket the hooks connect to (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help=f"logging level (default: {DEFAULT_LOG_LEVEL})",
    )
    # Mutually exclusive, and refused as a conflict rather than resolved by a
    # precedence rule nobody would remember: a command line that asks for a
    # window and for no window is a typo, not a preference.
    windows = parser.add_mutually_exclusive_group()
    windows.add_argument(
        "--window",
        action="store_true",
        help="open a window, and refuse to run if none can be opened",
    )
    windows.add_argument(
        "--no-window",
        action="store_true",
        help="run in the terminal and open no window",
    )
    # Flags rather than subcommands, and the reason is argparse rather than
    # taste: `root` is an optional positional, and subparsers eat the first one
    # -- `rhi doctor` and `rhi ./doctor` would become the same string, and `rhi
    # mydir` would be "invalid choice".
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="report whether the capture hooks are installed, and start nothing",
    )
    parser.add_argument(
        "--install-hooks",
        action="store_true",
        help="write the capture hooks into the project's .claude/settings.json",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {installed_version()}",
        help="print the installed version and exit",
    )
    return parser


def installed_version() -> str:
    """What this distribution says it is, or that it could not be found.

    Read from the install, never written down here: `pyproject.toml` is the one
    place the number lives, and a copy of it in the source is a copy that goes
    stale without anybody noticing.
    """
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except Exception:  # noqa: BLE001 - not installed is an answer, not a crash
        return UNKNOWN_VERSION


def settings_from(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    cwd: str,
) -> Settings:
    """One command line plus one environment plus one directory: one Settings.

    Total by contract. Nothing here raises and nothing exits: `rhi` owns the exit
    codes, and a value that cannot be used simply was not chosen, so the next
    source down answers instead.
    """
    port, port_is_explicit = _port(_flag(args, "port"), environ)
    return Settings(
        root=_root(_flag(args, "root"), environ, cwd),
        host=_text(_flag(args, "host"), environ.get(HOST_ENV), DEFAULT_HOST),
        port=port,
        port_is_explicit=port_is_explicit,
        socket_path=_text(
            _flag(args, "socket"), environ.get(SOCKET_ENV), DEFAULT_SOCKET_PATH
        ),
        token=token_from_env(environ),
        status_interval=_status_interval(environ),
        log_level=_text(
            _flag(args, "log_level"), environ.get(LOG_LEVEL_ENV), DEFAULT_LOG_LEVEL
        ),
        allow_remote_control=_allow_remote_control(environ),
        web_dist=_text(None, environ.get(WEB_DIST_ENV), ""),
    )


def _flag(args: argparse.Namespace, name: str) -> object:
    """What the command line said about `name`, or ``None`` if it said nothing.

    Read defensively: a caller may hand over a namespace built by hand, and a
    missing attribute means "not given" rather than an error nobody can act on.
    """
    return getattr(args, name, None)


def _text(flag: object, from_environ: object, default: str) -> str:
    """Flag, then environment, then default -- with empty reading as unset.

    An exported variable left blank (``export RHIZOME_SOCKET=``) is a wrapper
    script saying "I did not choose", never a choice of the empty string.
    """
    if isinstance(flag, str) and flag:
        return flag
    if isinstance(from_environ, str) and from_environ:
        return from_environ
    return default


def _root(flag: object, environ: Mapping[str, str], cwd: str) -> str:
    """The directory to observe, resolved the way the ``ctrl+L`` bar resolves it.

    Same rule for ``~``, ``..`` and trailing slashes as
    :func:`rhizome_graph.paths.resolve_root`, and deliberately not the same
    function: that one asks the disk whether the path is a directory and answers
    ``None`` when it is not. Refusing a project directory that has not been
    created yet is a decision for a caller that can print a reason; a pure
    function that has never touched the disk has nothing to refuse it on.
    """
    text = _text(flag, environ.get(ROOT_ENV), cwd)
    home = environ.get(HOME_ENV, "")
    try:
        expanded = expand_user(text.strip(), home if isinstance(home, str) else "")
        return os.path.normpath(os.path.join(cwd, expanded) if expanded else cwd)
    except Exception:  # noqa: BLE001 - total: an unusable value is not a crash
        return cwd


def _port(flag: object, environ: Mapping[str, str]) -> tuple[int, bool]:
    """The port, and whether anybody asked for it.

    A port that will not parse was not usable, so it was not chosen: the default
    stands, and it stands as a default rather than as somebody's decision. The
    daemon boots once, and `int(os.environ[...])` on a typo used to cost it that
    boot.
    """
    if isinstance(flag, int) and not isinstance(flag, bool):
        return flag, True
    raw = environ.get(PORT_ENV, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if raw:
        try:
            return int(raw), True
        except ValueError:
            pass
    return DEFAULT_HTTP_PORT, False


def _status_interval(environ: Mapping[str, str]) -> float:
    """How often to re-read the working tree, from the environment.

    An escape hatch rather than a tuning knob: on a huge repository on a slow
    disk `git status` is expensive enough that somebody watching may want it
    rarer, or off. Zero and negative mean off and are carried through rather than
    clamped; garbage falls back to the default.
    """
    raw = environ.get(STATUS_INTERVAL_ENV, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return DEFAULT_STATUS_INTERVAL_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_STATUS_INTERVAL_SECONDS


def _allow_remote_control(environ: Mapping[str, str]) -> bool:
    """Is the address gate opened to the rest of the network?

    Unset, empty and ``0`` keep it shut; anything else opens it. Exactly the
    meaning the variable has always had, moved rather than rewritten -- and still
    only an opt-out of the address check, never of the token.
    """
    raw = environ.get(ALLOW_REMOTE_CONTROL_ENV, "")
    return isinstance(raw, str) and raw not in ("", "0")


class PortUnavailableError(RuntimeError):
    """No port this instance is allowed to take was free.

    An ordinary exception rather than a `SystemExit`: what to do about it -- a
    message, a different port, a window that reports it -- belongs to the caller,
    and a pure decision function that exits the process cannot be tested.
    """


def choose_port(
    preferred: int, explicit: bool, is_free: Callable[[int], bool]
) -> int:
    """Which port to serve on, given who asked for the preferred one.

    **A default may be adjusted; an explicit request may not.** Moving off a busy
    `:8080` is a kindness -- nobody chose it, they only failed to say otherwise.
    Moving off a port somebody typed is a lie that reports success: an SSH
    forward already set up, a bookmark and a colleague told which port to open
    all break silently.

    The walk is bounded by :data:`PORT_SEARCH_LIMIT` and never steps past
    :data:`MAX_PORT`, because a hang at startup is indistinguishable from a
    program that does not work.
    """
    if explicit:
        if is_free(preferred):
            return preferred
        raise PortUnavailableError(
            f"port {preferred} is already in use, and it was asked for by name "
            "rather than chosen, so it will not be moved"
        )
    last = min(preferred + PORT_SEARCH_LIMIT - 1, MAX_PORT)
    for candidate in range(preferred, last + 1):
        if is_free(candidate):
            return candidate
    raise PortUnavailableError(
        f"no free port between {preferred} and {last}"
    )


def ingest_socket_path(
    root: str, default: str, is_live: Callable[[str], bool]
) -> str:
    """Where this instance listens for hook events.

    With nothing answering at `default` the answer is `default`, unchanged and
    exactly: every hook block already installed anywhere names it by omission,
    so the ordinary case may not move for any reason.

    With another daemon there, this instance takes a path of its own, derived
    from the observed root -- deterministic, because whoever pastes it into a
    hook block needs the same answer tomorrow, and beside the default rather than
    inside the observed tree, which the watcher would otherwise draw into the
    graph.
    """
    if not is_live(default):
        return default
    return _derived_socket_path(root, default)


def _derived_socket_path(root: str, default: str) -> str:
    """The default's name, marked with a digest of the root.

    Hashed rather than spelled: an AF_UNIX address is limited to about 108 bytes,
    so a scheme that embeds the root works on `~/w/x` and fails on a real
    checkout path.
    """
    directory = os.path.dirname(default)
    stem = os.path.basename(default)
    if stem.endswith(SOCKET_SUFFIX):
        stem = stem[: -len(SOCKET_SUFFIX)]
    if not stem:
        stem = DISTRIBUTION_NAME
    digest = hashlib.sha256(
        os.path.normpath(root).encode("utf-8", "surrogateescape")
    ).hexdigest()[:INSTANCE_DIGEST_LENGTH]
    return os.path.join(directory, f"{stem}-{digest}{SOCKET_SUFFIX}")


def reachable_host(host: str) -> str:
    """An address something can actually connect to, given one that was bound.

    A wildcard bind is not somewhere to point a browser or a readiness probe;
    loopback is reachable whenever the wildcard is.
    """
    return LOOPBACK_HOST if host in WILDCARD_HOSTS else host


def page_url(host: str, port: int) -> str:
    """The address to open, spelled the way a browser accepts it."""
    reachable = reachable_host(host)
    if ":" in reachable:
        reachable = f"[{reachable}]"
    return f"http://{reachable}:{port}/"


def socket_was_named(args: argparse.Namespace, environ: Mapping[str, str]) -> bool:
    """Did anybody actually ask for this ingest socket path?

    A flag or an exported variable both count as asking, exactly as they do for
    the port -- and for the same reason: a path somebody named is the path they
    have already written into a hook block, so it is honoured or refused, never
    quietly moved off.
    """
    named = _flag(args, "socket")
    if isinstance(named, str) and named:
        return True
    from_environ = environ.get(SOCKET_ENV, "")
    return isinstance(from_environ, str) and bool(from_environ)


def main(argv: Sequence[str] | None = None) -> None:
    """The installed `rhi` command: a command line in, a served project out.

    The one scope in this module that reads the process environment, for the
    reason `daemon/server.py`'s `main()` is: this is a front door, where air
    becomes a value. Everything below is handed that value.

    `--version` and `--help` are answered by argparse before any of this runs, so
    neither binds a port, opens an ingest socket nor imports an event loop.
    """
    args = build_parser().parse_args(argv)
    settings = settings_from(args, os.environ, os.getcwd())
    home = os.environ.get(HOME_ENV, "") or os.path.expanduser("~")

    # Both of these only read and write files, so they run before a port is
    # chosen and before an ingest socket is named: the precedent is
    # `./start.sh --print-token`, and the reason is the same -- a diagnostic
    # that takes over the shared socket cannot be run while the thing it is
    # diagnosing is running, which is exactly when somebody wants to run it.
    if _flag(args, "doctor"):
        raise SystemExit(_report_hooks(settings.root, home))
    if _flag(args, "install_hooks"):
        raise SystemExit(_write_hooks(settings.root))

    try:
        port = choose_port(
            settings.port,
            explicit=settings.port_is_explicit,
            is_free=lambda candidate: port_is_free(settings.host, candidate),
        )
    except PortUnavailableError as exc:
        raise SystemExit(str(exc)) from None

    # A socket somebody named is used as given; a live one under that name is
    # refused by the daemon's own ingest guard, one layer down. Only the default
    # may be stepped off, and only onto this root's own derived path -- which is
    # why a second window on the SAME root is refused rather than moved again.
    socket_path = (
        settings.socket_path
        if socket_was_named(args, os.environ)
        else ingest_socket_path(settings.root, settings.socket_path, socket_is_live)
    )

    settings = replace(settings, port=port, socket_path=socket_path)
    announcement = _announcement(settings)
    requested = _window_request(args)

    # Imported here, not at the top, like the launcher below: nothing about
    # `--help` should pay for a decision about windows.
    from rhizome_graph import window

    # Both names are resolved through the module at call time, never bound here:
    # what opens a window is exactly the seam a test replaces.
    backend = window.choose_window_backend(
        platform=sys.platform,
        available=window.available_backends(),
        requested=requested,
    )
    strategy = window.strategy_for(backend)

    if strategy is None:
        if requested == WINDOW_REQUESTED:
            # Nothing raised: there was simply nothing to open. An explicit
            # request may not be adjusted, so this is a refusal -- and it costs
            # nothing to make it before a port is bound.
            raise SystemExit(NO_BACKEND_REFUSAL)
        if requested == WINDOW_UNSPECIFIED:
            # A window was the default here, and it is not happening. Said out
            # loud for the same reason a window that raises is: a person who
            # expected an application and got a URL should not have to guess
            # which half of the machine is missing.
            _warn(NO_BACKEND_NOTE)
        _serve_headless(settings, announcement)
        return

    _serve_with_window(
        settings,
        announcement,
        strategy,
        explicit=requested == WINDOW_REQUESTED,
    )


def _report_hooks(root: str, home: str) -> int:
    """Say whether attribution is set up here, and start nothing at all.

    **Both settings files, and a hook in either one is a pass.** Claude Code
    merges the user-level `~/.claude/settings.json` with the project's own, so a
    hook installed globally really does fire for sessions in this project.
    Reporting a failure to somebody whose global hook works would be a false
    alarm, and a diagnostic that cries wolf is worse than none: it teaches the
    reader to ignore the next one, and the failure this command looks for costs
    hours to spot.

    Reading the user's file is not touching it. Nothing here writes anywhere.
    """
    expected = hook_command()
    files = (_settings_file(root), _settings_file(home))
    diagnoses = [
        hookinstall.diagnose(_file_text(path), expected, _command_exists)
        for path in files
    ]

    lines = [f"capture hooks for {root}"]
    for path, diagnosis in zip(files, diagnoses):
        lines.extend(_hook_lines(path, diagnosis))
    verdict = hookinstall.overall_state(diagnosis.state for diagnosis in diagnoses)
    lines.extend(_verdict_lines(verdict, root))
    _say(lines)

    return 0 if verdict == hookinstall.INSTALLED else 1


def _hook_lines(path: str, diagnosis: hookinstall.Diagnosis) -> list[str]:
    """One settings file's answer, with each command beside the file it is in."""
    if diagnosis.commands:
        return [
            f"  {path}: {_command_caption(command)}: {command}"
            for command in diagnosis.commands
        ]
    return [f"  {path}: {STATE_CAPTIONS.get(diagnosis.state, diagnosis.state)}"]


def _command_caption(command: str) -> str:
    """Whether this particular command is one that can actually be run."""
    if _command_exists(command):
        return "capture hook installed, and it runs"
    return "capture hook installed, and its command is missing"


def _verdict_lines(verdict: str, root: str) -> list[str]:
    """What the two files add up to, and what to do about it."""
    if verdict == hookinstall.INSTALLED:
        return [HOOKS_WORKING]
    if verdict == hookinstall.STALE:
        return [HOOKS_ROTTED, HOOKS_REMEDY.format(root=root)]
    return [HOOKS_MISSING, HOOKS_REMEDY.format(root=root)]


def _write_hooks(root: str) -> int:
    """Install the capture hooks into one project, and say so before doing it.

    The offer half of "diagnose always, offer explicitly, never write silently".
    `.claude/settings.json` is a committed file in many repositories -- it is
    committed in this one -- so this is the only path in the program that writes
    into the observed project, and it runs only when it was typed.

    A file nobody can parse is refused rather than replaced, and a stranger's
    `PostToolUse` entry survives: both belong to :func:`hookinstall.merge_hook_block`,
    which is pure and idempotent, so running this twice is running it once.
    """
    expected = hook_command()
    path = _settings_file(root)
    text = _file_text(path)
    settings = {} if not text.strip() else hookinstall.parse_settings(text)
    if settings is None:
        _warn(SETTINGS_UNREADABLE.format(path=path))
        return 1

    merged = hookinstall.merge_hook_block(settings, hookinstall.hook_block(expected))
    # Printed before the write, and naming both the file and the command: "it
    # wrote something somewhere" is how a user loses track of which of their
    # projects is instrumented.
    _say([f"writing the capture hooks into {path}", f"  command: {expected}"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(merged, indent=2) + "\n")
    except OSError as error:
        _warn(f"{path} could not be written: {error}")
        return 1
    _say([HOOKS_INSTALLED_NOTE])
    return 0


def _settings_file(directory: str) -> str:
    """Where `directory` keeps its Claude Code settings."""
    return os.path.join(directory, *SETTINGS_RELATIVE)


def _file_text(path: str) -> str:
    """The contents of `path`, or the empty string when there is no such file.

    A file that is not there is not a sixth state: nothing was written, so
    nothing is claimed. Unreadable for any other reason reads the same way,
    because this command is pointed at broken setups by definition.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _command_exists(command: str) -> bool:
    """Can this hook command actually be executed?

    The disk half of the diagnosis, which :mod:`rhizome_graph.hookinstall` has
    injected precisely so that it stays out of a pure module. The whole command
    string arrives, so `shlex`, `$PATH` and the filesystem are answered here: the
    script is what rots (`python3 /somewhere/emit_event.py`), so a `.py`
    argument is what gets asked about, and a bare program name is looked for on
    `$PATH` the way a shell would.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    scripts = [token for token in tokens if token.endswith(".py")]
    program = scripts[-1] if scripts else tokens[-1]
    if os.sep in program:
        return os.path.isfile(program)
    return shutil.which(program) is not None


def _window_request(args: argparse.Namespace) -> str:
    """What the command line asked for: a window, no window, or neither.

    The distinction the port and the ingest socket already draw: a default may
    be adjusted, an explicit request may not.
    """
    if _flag(args, "no_window"):
        return WINDOW_DECLINED
    if _flag(args, "window"):
        return WINDOW_REQUESTED
    return WINDOW_UNSPECIFIED


def _serve_headless(settings: Settings, announcement: Sequence[str]) -> None:
    """Run the daemon on this thread until it is asked to stop.

    With no window there is nothing that wants the main thread, so the daemon
    keeps it -- and with it the signal handlers `run()` installs, which is how
    Ctrl-C and a terminal's SIGTERM reach the one teardown there is.
    """
    # Imported here, not at the top: the launcher pulls in asyncio and the
    # daemon, and printing `--help` must not pay for an event loop, a WebSocket
    # stack and an inotify backend.
    from rhizome_graph import launch

    try:
        launch.serve(settings, lambda ready: _say(announcement))
    except launch.StartupRefused as exc:
        raise SystemExit(str(exc)) from None


def _serve_with_window(
    settings: Settings,
    announcement: Sequence[str],
    strategy: Callable[[str, Callable[[], None]], None],
    explicit: bool,
) -> None:
    """Open a window over a daemon, and end both when either of them ends.

    **The window owns this thread and the daemon runs on another one.** That is
    the way round a GUI toolkit demands: pywebview refuses to start anywhere but
    the main thread, and every toolkit under it assumes the same. `run()` already
    knows how to be embedded -- it installs no signal handlers off the main
    thread and stops when its future resolves -- so what is left here is the
    triggers that reach that future from *this* side: the window closing, which
    calls it directly, and a signal, which both asks the window to close and
    unblocks whatever this thread is inside so the same call can be made on the
    way out. One teardown, several triggers; never a second shutdown.

    A window that cannot open degrades to headless and says why. Whether that
    ends the run depends on who asked for it.
    """
    from rhizome_graph import launch

    handoff: queue.Queue = queue.Queue()
    failed: list[BaseException] = []
    # The direction that used to be missing. A signal cannot be delivered to a
    # window, and a strategy blocks until its window is gone, so it can hand
    # back no handle either; what a stopping daemon can do is set this, which
    # the strategy is watching. Nothing is torn down here -- the window closes
    # itself and calls the same `stop` a user closing it would.
    close_requested = threading.Event()

    def announce(ready) -> None:
        # On the daemon's thread: the URL is printed first, so a window that
        # blows up a moment later cannot swallow it.
        _say(announcement)
        handoff.put(ready)

    def background() -> None:
        try:
            launch.serve(settings, announce)
        except BaseException as error:  # noqa: BLE001 - reported on the main thread
            failed.append(error)
        finally:
            # Whatever happened, this thread has nothing more to hand over.
            handoff.put(None)

    daemon = threading.Thread(target=background, name=DAEMON_THREAD_NAME, daemon=True)
    _request_close_on_signal(close_requested)
    daemon.start()

    ready = None
    reason = None
    try:
        ready = handoff.get()
        if ready is not None:
            strategy(ready.url, ready.stop, close_requested)
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - a window is not worth a crash
        reason = str(error) or type(error).__name__
        if not explicit:
            # Requirement 4 -- reachable in an ordinary browser -- never depended
            # on the window, so it does not die with it. Said out loud, though:
            # a degradation nobody is told about is the one that wastes an hour.
            _warn(WINDOW_FAILED.format(reason=reason))
            # Waited on the request rather than on the daemon's grace period:
            # with no window left there is nothing to close, and the run lasts
            # until somebody says stop -- where a bounded wait would end a
            # perfectly good headless run on a timer.
            _await_close_request(close_requested, daemon)

    if ready is not None:
        ready.stop()
    _await_daemon(daemon)

    if failed:
        raise _reportable(failed[0], launch.StartupRefused)
    if reason is not None and explicit:
        raise SystemExit(EXPLICIT_WINDOW_REFUSAL.format(reason=reason))


def _reportable(error: BaseException, refusal: type[BaseException]) -> BaseException:
    """A daemon that died on another thread, said the way this one would say it.

    An anticipated refusal becomes an exit message, exactly as it does when the
    daemon has this thread to itself; anything else is somebody's bug and keeps
    its traceback.
    """
    if isinstance(error, refusal):
        return SystemExit(str(error))
    return error


def _request_close_on_signal(close_requested: threading.Event) -> None:
    """Turn a terminal's signal into a request the open window can act on.

    With the daemon on another thread it installs no handlers of its own --
    signals are a main-thread facility by definition -- so this thread is the
    only place a terminal can be answered. It resolves nothing itself: the
    window is asked to go away, it closes, and the single `stop` the daemon
    handed over runs the one teardown there is.

    **Two mechanisms, because one of them is not enough.** A Python signal
    handler runs between bytecodes on the main thread, and this thread is inside
    a GUI toolkit's main loop -- C code that may never return to the interpreter
    between events, which is why raising `KeyboardInterrupt` there was only ever
    best-effort. The C handler CPython installs also writes the signal number to
    a wakeup file descriptor, and that write happens whatever the interpreter is
    doing; a thread of our own is blocked reading it. Either path sets the same
    event, and setting it twice is setting it once.

    The interrupt is still raised as well, after the event: a strategy that
    ignores the request -- anything written before this channel existed -- is
    still unblocked out of the call the way it always was.
    """
    with contextlib.suppress(OSError, ValueError, RuntimeError):
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, _closer(close_requested))
        reader, writer = os.pipe()
        # Required to be non-blocking, and harmless here: the payload is one
        # byte per signal and nothing else ever writes to this pipe.
        os.set_blocking(writer, False)
        signal.set_wakeup_fd(writer)
        threading.Thread(
            target=_close_on_wakeup,
            args=(reader, close_requested),
            name=SIGNAL_THREAD_NAME,
            daemon=True,
        ).start()


def _closer(close_requested: threading.Event) -> Callable[[int, object], None]:
    """The signal handler: ask the window to close, then unblock this thread."""

    def close(signum: int, frame: object) -> None:
        close_requested.set()
        raise KeyboardInterrupt

    return close


def _close_on_wakeup(reader: int, close_requested: threading.Event) -> None:
    """Ask for the window to close as soon as any signal has been delivered.

    One byte is enough: what arrives is not read for its content, only for the
    fact that something arrived. A run nobody signals leaves this thread blocked
    here for the life of the process, which is what a daemon thread is for.
    """
    with contextlib.suppress(OSError, ValueError):
        os.read(reader, 1)
    close_requested.set()


def _await_close_request(
    close_requested: threading.Event, daemon: threading.Thread
) -> None:
    """Wait until this run is asked to end -- or until the daemon has ended it.

    Both, because either can happen first: a signal sets the event, and a daemon
    that dies on its own afterwards would otherwise leave this thread waiting for
    a request nobody is left to make. Polled rather than joined so an interrupt
    lands here promptly.
    """
    with contextlib.suppress(KeyboardInterrupt):
        while daemon.is_alive() and not close_requested.wait(JOIN_POLL_SECONDS):
            pass


def _await_daemon(daemon: threading.Thread) -> None:
    """Wait for the daemon thread, interruptibly and not forever.

    Polled rather than joined outright so an interrupt lands on this thread
    promptly, and bounded so a daemon that refuses to stop cannot leave the user
    with a program that will not quit.
    """
    deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
    with contextlib.suppress(KeyboardInterrupt):
        while daemon.is_alive() and time.monotonic() < deadline:
            daemon.join(JOIN_POLL_SECONDS)


def _warn(message: str) -> None:
    """Say something the user has to know but did not ask for.

    On stderr, and never quietly: the precedent for a quiet degradation is
    `start.sh` serving a stale `dist` when node is missing, which cost real
    hours.
    """
    print(message, file=sys.stderr, flush=True)


def _announcement(settings: Settings) -> list[str]:
    """What to tell the user once the daemon is actually serving.

    The URL, always -- it is the whole output that matters. And, whenever the
    ingest socket is not the default, the variable to set: hooks reach a daemon
    by a path compiled into a settings file, and an instance listening somewhere
    else silently has no attribution at all, which looks exactly like a healthy
    setup with nobody working. That line is printed even when the path was chosen
    by hand, because it says what the hook block in the *observed project* must
    carry, not what was typed here.
    """
    lines = [page_url(settings.host, settings.port)]
    if settings.socket_path != DEFAULT_SOCKET_PATH:
        lines.append(
            "hooks in the observed project must carry "
            f"RHIZOME_SOCKET={settings.socket_path}"
        )
    return lines


def _say(lines: Sequence[str]) -> None:
    """Print, flushed.

    Block-buffered by default when stdout is a pipe, and this process lives until
    the user quits it -- so a URL that is not flushed is a URL nobody ever reads.
    """
    for line in lines:
        print(line, flush=True)
