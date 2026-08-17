"""Contract tests (RED) for rhizome_graph.cli: argv + environ + cwd -> Settings.

Motivation: `daemon/server.py` has no value anywhere that describes "how this
instance is configured". `main()` reads four variables out of `os.environ` and
hands three scalars to `run()`, and four more environment reads happen *inside*
the machinery instead of at the entry: `_status_poll_interval()`,
`_allow_remote_control()`, `Session.__init__`'s token, and the obsolete
`RHIZOME_WS_PORT` warning. `run()` then decides two more things on its own -- the
static root and `host=""`.

That works because there is exactly one caller shape: a shell script that
exports variables and execs a module. An installed `rhi` is a *second* front
door with different inputs -- a positional directory, a port that may have to
move, a window to open -- and building it by having `rhi` set `os.environ` and
call `main()` would make the environment an internal protocol between two parts
of one program: unreadable in a stack trace, untestable without a subprocess,
and impossible to run twice in one process.

So the configuration becomes a value, in the shape the rest of this codebase
already uses for decisions (`paths.py`, `status.py`, `pick.ts`): a frozen
`Settings`, and a pure `settings_from(args, environ, cwd)` that is total, does
no I/O and never calls `sys.exit`. `cwd` is a parameter for exactly the reason
`home` is one in `paths.py` -- a function that reads the process's own working
directory cannot be tested from another one.

Two properties carry the whole module and are pinned as tables rather than as
examples:

  * **Precedence: flag > environment > default, for every field.** One example
    proves one field; the table proves the rule, and the rule is what the second
    front door depends on.
  * **`~` and relative paths mean here exactly what they mean in the `ctrl+L`
    bar.** The bar resolves through `rhizome_graph.paths.resolve_root`, so this
    must agree with it -- a second rule for `~` in the CLI is a bug waiting for
    the first user who types one and gets a different directory in each place.

`settings_from` cannot simply *call* `resolve_root`, and that is not an
oversight: `resolve_root` asks the disk whether the path is a directory and
resolves relative paths against the process's cwd. Purity here means the
agreement is asserted over real directories instead (see part 3), while a path
that does not exist still yields a `Settings` rather than `None` -- refusing an
unwritten project directory is a decision for the caller that can print, not for
a pure function.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
from pathlib import Path

import pytest

import daemon.server as server
from rhizome_graph.cli import Settings, build_parser, settings_from
from rhizome_graph.paths import resolve_root

#: Modules that must never appear in an import statement of `cli.py`. The whole
#: point of the seam is that `rhi` can build a `Settings`, print `--help` and
#: refuse a bad flag without an event loop, a WebSocket stack or an inotify
#: backend being imported first.
FORBIDDEN_IMPORTS = ("asyncio", "websockets", "watchdog", "daemon")

#: The bind address an installed, casually started `rhi` defaults to. Deliberately
#: NOT a statement about `python -m daemon.server`, which binds every interface
#: today: whether that entry point's default moves too is a security judgement
#: reserved for `security-auditor`, and nothing here pins it either way.
CLI_DEFAULT_HOST = "127.0.0.1"


def _args(*argv: str) -> argparse.Namespace:
    """One command line, parsed by the parser the CLI actually ships."""
    return build_parser().parse_args(list(argv))


# --- 1. precedence: flag > environment > default, across the fields ---------
#
# One row per field: what a flag sets it to, what the environment sets it to,
# and what each of the three sources is expected to yield. The three tests below
# read one column each, so a field that honours the environment but ignores its
# flag fails in one place with its own name in the test id.

CWD = "/work/project"

PRECEDENCE = [
    # field, flag argv, environ, flag wins, environ wins, default
    ("port", ["--port", "9100"], {"RHIZOME_HTTP_PORT": "9000"}, 9100, 9000, 8080),
    (
        "host",
        ["--host", "0.0.0.0"],
        {"RHIZOME_HOST": "10.1.2.3"},
        "0.0.0.0",
        "10.1.2.3",
        CLI_DEFAULT_HOST,
    ),
    (
        "socket_path",
        ["--socket", "/tmp/from-flag.sock"],
        {"RHIZOME_SOCKET": "/tmp/from-env.sock"},
        "/tmp/from-flag.sock",
        "/tmp/from-env.sock",
        "/tmp/rhizome-graph.sock",
    ),
    (
        "log_level",
        ["--log-level", "DEBUG"],
        {"RHIZOME_LOG_LEVEL": "WARNING"},
        "DEBUG",
        "WARNING",
        "INFO",
    ),
    (
        "root",
        ["/flag/root"],
        {"RHIZOME_PROJECT_ROOT": "/env/root"},
        "/flag/root",
        "/env/root",
        CWD,
    ),
]

_IDS = [row[0] for row in PRECEDENCE]


@pytest.mark.parametrize(
    "field,argv,environ,from_flag,_from_env,_default", PRECEDENCE, ids=_IDS
)
def test_a_flag_beats_the_environment(
    field: str,
    argv: list[str],
    environ: dict,
    from_flag: object,
    _from_env: object,
    _default: object,
) -> None:
    """What the user typed now beats what a wrapper script exported earlier."""
    settings = settings_from(_args(*argv), environ, CWD)

    assert getattr(settings, field) == from_flag


@pytest.mark.parametrize(
    "field,_argv,environ,_from_flag,from_env,_default", PRECEDENCE, ids=_IDS
)
def test_the_environment_beats_the_default(
    field: str,
    _argv: list[str],
    environ: dict,
    _from_flag: object,
    from_env: object,
    _default: object,
) -> None:
    """`start.sh` keeps configuring the daemon by exporting variables."""
    settings = settings_from(_args(), environ, CWD)

    assert getattr(settings, field) == from_env


@pytest.mark.parametrize(
    "field,_argv,_environ,_from_flag,_from_env,default", PRECEDENCE, ids=_IDS
)
def test_neither_leaves_the_default(
    field: str,
    _argv: list[str],
    _environ: dict,
    _from_flag: object,
    _from_env: object,
    default: object,
) -> None:
    settings = settings_from(_args(), {}, CWD)

    assert getattr(settings, field) == default


def test_the_canonical_case_is_the_port(tmp_path: Path) -> None:
    """The example the plan spells out, kept whole so the rule is readable."""
    environ = {"RHIZOME_HTTP_PORT": "9000"}

    assert settings_from(_args("--port", "9100"), environ, CWD).port == 9100
    assert settings_from(_args(), environ, CWD).port == 9000
    assert settings_from(_args(), {}, CWD).port == 8080


# --- 2. the defaults are the daemon's own, not a second copy of them --------


def test_the_default_socket_path_is_the_daemons_own() -> None:
    """Two spellings of the same default drift; one of them is then wrong."""
    assert settings_from(_args(), {}, CWD).socket_path == server.DEFAULT_SOCKET_PATH


def test_the_default_port_is_the_daemons_own() -> None:
    assert settings_from(_args(), {}, CWD).port == server.DEFAULT_HTTP_PORT


def test_the_default_status_interval_is_the_daemons_own() -> None:
    assert (
        settings_from(_args(), {}, CWD).status_interval
        == server.STATUS_POLL_INTERVAL_SECONDS
    )


# --- 3. the positional DIR, resolved the way the ctrl+L bar resolves --------


def test_no_directory_argument_means_the_current_one() -> None:
    """`rhi` with no argument watches where it was started."""
    assert settings_from(_args(), {}, CWD).root == CWD


def test_a_relative_directory_is_resolved_against_the_cwd_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cwd is a parameter, so the process's own must not leak in."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    settings = settings_from(_args("sub"), {}, "/work/project")

    assert settings.root == "/work/project/sub"


def test_a_tilde_is_expanded_against_the_home_in_the_environment() -> None:
    """`environ` is the only place a pure function may learn a home from."""
    settings = settings_from(_args("~/pro"), {"HOME": "/home/alice"}, CWD)

    assert settings.root == "/home/alice/pro"


def test_the_cli_and_the_ctrl_l_bar_resolve_the_same_directory_the_same_way(
    tmp_path: Path
) -> None:
    """One rule for `~`, `..` and trailing slashes, in both front doors.

    `resolve_root` is what the bar calls, so it is the reference. Real
    directories, because that is the only input on which the reference answers
    at all.
    """
    home = tmp_path / "home" / "alice"
    project = home / "pro"
    project.mkdir(parents=True)
    typed = ["~/pro", "~/pro/", "~/pro/../pro", str(project)]

    for text in typed:
        settings = settings_from(_args(text), {"HOME": str(home)}, str(tmp_path))

        assert settings.root == resolve_root(text, str(home)), f"disagreed on {text!r}"


def test_a_directory_that_does_not_exist_is_still_a_root(tmp_path: Path) -> None:
    """No I/O: `resolve_root` answers `None` here, and this must not.

    Refusing a path is a decision for a caller that can print a reason and exit;
    a pure function that has never touched the disk has nothing to refuse it on.
    """
    absent = tmp_path / "not-created-yet"

    assert settings_from(_args(str(absent)), {}, str(tmp_path)).root == str(absent)


def test_an_empty_project_root_variable_reads_as_unset(tmp_path: Path) -> None:
    """`export RHIZOME_PROJECT_ROOT=` means "I did not choose", not "/"."""
    settings = settings_from(_args(), {"RHIZOME_PROJECT_ROOT": ""}, CWD)

    assert settings.root == CWD


# --- 4. port_is_explicit: did anybody actually ask for this port? -----------
#
# The field exists for the step after this one: a port nobody named may be moved
# when it is busy, and a port somebody named must not be -- a viewer told to open
# :8080 by hand, or an SSH forward already set up, both break silently if the
# daemon quietly lands somewhere else.


def test_a_port_on_the_command_line_is_explicit() -> None:
    assert settings_from(_args("--port", "9100"), {}, CWD).port_is_explicit is True


def test_a_port_in_the_environment_is_explicit() -> None:
    """A wrapper script exporting it chose it just as deliberately as a flag."""
    settings = settings_from(_args(), {"RHIZOME_HTTP_PORT": "9000"}, CWD)

    assert settings.port_is_explicit is True


def test_the_default_port_is_not_explicit() -> None:
    assert settings_from(_args(), {}, CWD).port_is_explicit is False


def test_a_port_that_is_not_a_number_falls_back_instead_of_raising() -> None:
    """`main()` does `int(os.environ[...])` today and dies at boot on a typo.

    A pure, total function has a better answer: the knob was not usable, so it
    was not chosen, and the default stands.
    """
    settings = settings_from(_args(), {"RHIZOME_HTTP_PORT": "eighty-eighty"}, CWD)

    assert settings.port == 8080
    assert settings.port_is_explicit is False


# --- 5. the status interval, with its escape hatch intact ------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12.5", 12.5),
        (" 5 ", 5.0),
        ("0", 0.0),
        ("-1", -1.0),
    ],
)
def test_a_usable_status_interval_is_taken_from_the_environment(
    raw: str, expected: float
) -> None:
    """Zero and negative mean "off", and are carried through, not clamped."""
    settings = settings_from(_args(), {"RHIZOME_STATUS_INTERVAL": raw}, CWD)

    assert settings.status_interval == expected


@pytest.mark.parametrize("raw", ["", "   ", "3 seconds", "NaN-ish"])
def test_an_unusable_status_interval_falls_back_to_the_default(raw: str) -> None:
    """The daemon boots once; a typo in an optional knob must not cost it."""
    settings = settings_from(_args(), {"RHIZOME_STATUS_INTERVAL": raw}, CWD)

    assert settings.status_interval == server.STATUS_POLL_INTERVAL_SECONDS


# --- 6. the control gate's two inputs, carried rather than sniffed ---------
#
# These move onto `Settings` with everything else, and moving them must not
# weaken them. The failing-closed behaviour itself is pinned where it is
# enforced (`tests/test_settings_control_gate.py`, `tests/test_token.py`); what
# is pinned here is only that the value carried is the value that was set.


def test_the_token_may_be_pinned_by_the_environment() -> None:
    """`start.sh --dev` and `--print-token` both depend on this exact behaviour."""
    settings = settings_from(_args(), {"RHIZOME_TOKEN": "chosen-by-hand"}, CWD)

    assert settings.token == "chosen-by-hand"


def test_a_settings_with_no_token_in_the_environment_still_carries_one() -> None:
    """The empty token is refused by the gate, so minting one is the safe half."""
    assert settings_from(_args(), {}, CWD).token != ""


def test_an_empty_token_variable_does_not_disable_the_gate() -> None:
    """`export RHIZOME_TOKEN=` must not lock the page out of its own daemon."""
    assert settings_from(_args(), {"RHIZOME_TOKEN": ""}, CWD).token != ""


def test_two_instances_do_not_share_a_minted_token() -> None:
    assert settings_from(_args(), {}, CWD).token != settings_from(_args(), {}, CWD).token


def test_remote_control_is_off_unless_the_environment_opens_it() -> None:
    assert settings_from(_args(), {}, CWD).allow_remote_control is False


@pytest.mark.parametrize("raw,expected", [("1", True), ("yes", True), ("0", False), ("", False)])
def test_the_remote_control_flag_keeps_the_meaning_it_has_today(
    raw: str, expected: bool
) -> None:
    """Exactly `_allow_remote_control`: anything but unset, empty or `0` opens it."""
    environ = {"RHIZOME_ALLOW_REMOTE_CONTROL": raw}

    assert settings_from(_args(), environ, CWD).allow_remote_control is expected


# --- 7. where the built page comes from ------------------------------------


def test_the_web_dist_override_is_carried_on_the_settings() -> None:
    """`RHIZOME_WEB_DIST` has to survive the trip from `rhi` to what is served."""
    environ = {"RHIZOME_WEB_DIST": "/opt/rhizome/web"}

    assert settings_from(_args(), environ, CWD).web_dist == "/opt/rhizome/web"


def test_no_override_leaves_the_search_to_the_installation(tmp_path: Path) -> None:
    """Empty means "look where you are installed", which is `assets.py`'s job.

    Not resolved here, and not `Path("")`: deciding *which* candidate exists is a
    filesystem question, and this function has no filesystem.
    """
    assert settings_from(_args(), {}, CWD).web_dist == ""


# --- 8. the shape of the value itself --------------------------------------


def test_settings_is_frozen() -> None:
    """Configuration read twice must not be able to differ between the reads."""
    settings = settings_from(_args(), {}, CWD)

    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.port = 1234  # type: ignore[misc]


def test_settings_names_every_field_the_daemon_needs() -> None:
    """Total: `run()` may ask this value for anything it used to sniff."""
    fields = {field.name for field in dataclasses.fields(Settings)}

    assert {
        "root",
        "host",
        "port",
        "port_is_explicit",
        "socket_path",
        "token",
        "status_interval",
        "log_level",
        "allow_remote_control",
        "web_dist",
    } <= fields


def test_the_parser_is_an_argument_parser() -> None:
    assert isinstance(build_parser(), argparse.ArgumentParser)


def test_building_the_parser_twice_yields_two_parsers() -> None:
    """Pure construction: no shared global to be mutated by the first caller."""
    assert build_parser() is not build_parser()


def test_the_parser_understands_the_whole_documented_command_line(
    tmp_path: Path
) -> None:
    """One line exercising every flag, so a renamed one fails here and not later."""
    argv = [
        str(tmp_path),
        "--host",
        "0.0.0.0",
        "--port",
        "9100",
        "--socket",
        "/tmp/x.sock",
        "--log-level",
        "DEBUG",
    ]

    settings = settings_from(_args(*argv), {}, CWD)

    assert (settings.root, settings.host, settings.port) == (
        str(tmp_path),
        "0.0.0.0",
        9100,
    )
    assert (settings.socket_path, settings.log_level) == ("/tmp/x.sock", "DEBUG")


def test_a_hostile_environment_yields_a_settings_rather_than_an_exit() -> None:
    """Total, and no `sys.exit`: `rhi` owns the exit codes, not this function.

    Every value here is one somebody could actually export. None of them may
    raise, and none may take the process down from inside a pure call.
    """
    environ = {
        "RHIZOME_HTTP_PORT": "-",
        "RHIZOME_STATUS_INTERVAL": "soon",
        "RHIZOME_PROJECT_ROOT": "~nobody/nowhere",
        "RHIZOME_SOCKET": "",
        "RHIZOME_LOG_LEVEL": "",
        "RHIZOME_TOKEN": "",
        "RHIZOME_ALLOW_REMOTE_CONTROL": "maybe",
        "RHIZOME_WEB_DIST": "",
        "HOME": "",
    }

    settings = settings_from(_args(), environ, CWD)

    assert isinstance(settings, Settings)


# --- 9. purity --------------------------------------------------------------


def test_the_module_pulls_in_nothing_from_the_daemon_side() -> None:
    """`rhi --help` must not import an event loop, a WebSocket stack or inotify.

    Asserted over the source text rather than by importing, because an
    import-time check cannot tell a dependency of *this* module from one another
    test already loaded.
    """
    import rhizome_graph.cli as cli

    source = Path(cli.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    offences = sorted(imported & set(FORBIDDEN_IMPORTS))

    assert offences == [], f"rhizome_graph/cli.py must stay pure; it imports {offences}"
