"""Contract tests (RED) for `rhi --install-hooks`: write, but only when asked.

Motivation: `rhi --doctor` (`tests/test_hook_doctor.py`) tells a user that
attribution is not wired up. This is the other half -- the offer -- and the
design is deliberately narrow, because the file being written is not ours:

  * **`.claude/settings.json` is a committed file in many repositories.** It is
    committed in this one. A tool that edited it as a side effect of "show me a
    graph" would put a change in the user's working tree that they discover from
    `git status`, in a file that decides what an agent is allowed to do. So the
    first test in this file is not about installing at all: it is that an
    ordinary `rhi <dir>` writes **nothing**.
  * **A merge can clobber.** The observed project may already hold `PostToolUse`
    hooks -- a formatter, a linter -- and losing one to a silent merge is a
    defect the user will attribute to anything but this program. The stranger's
    entry survives byte for byte, and a settings file nobody can parse is
    refused outright rather than replaced.
  * **Running it twice must be running it once.** Somebody unsure whether they
    already installed will run it again; that is what unsure means. Two
    identical blocks means every tool call fires the hook twice and every change
    flashes twice on the graph -- a bug with no error message anywhere.

**The command that gets written is absolute, and belongs to the package.** Not
`python3 hooks/emit_event.py`: a relative path resolves against whatever
directory Claude Code happens to run the hook from, and even spelled absolutely
into a checkout it is a path the user may not keep -- which is exactly the rot
`--doctor` exists to catch. Where it comes from on this machine is
`rhizome_graph.assets.hook_command()`, specified in
`tests/test_hook_dependencies.py`; what is pinned here is that whatever it
answers, the file that lands names something that runs.

**A flag, not a subcommand** -- see `tests/test_hook_doctor.py` for the argparse
reason, which is that `root` is an optional positional and subparsers would eat
it.

**What is NOT specified here:** the wording of the preview. What is pinned is
that the path being changed and the command being written both appear on stdout
before the process ends, because "it wrote something somewhere" is how a user
loses track of which of their projects is instrumented.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from rhi_process import (
    REPO_ROOT,
    URL,
    clean_environment,
    entry_argv,
    free_port,
    port_is_busy,
    start,
)
from rhizome_graph.cli import build_parser

#: How long a command that writes one small file is given to do it and return.
#: A `--install-hooks` that has not returned by now has started a daemon.
WRITE_TIMEOUT_SECONDS = 30.0

#: How long `rhi` is given to bind and print, in the one test here that really
#: runs it. Generous; the imports dominate.
STARTUP_TIMEOUT_SECONDS = 90.0

FOREIGN_COMMAND = "prettier --write $CLAUDE_FILE_PATHS"

FOREIGN_ENTRY = {
    "matcher": "Write|Edit",
    "hooks": [{"type": "command", "command": FOREIGN_COMMAND}],
}


def _install_hooks(project: Path, home: Path, port: int) -> subprocess.CompletedProcess:
    """Run `rhi <project> --install-hooks` in isolation, refusing a false green.

    As in `tests/test_hook_doctor.py`: a timeout means a daemon was started, and
    an argparse "unrecognized arguments" is a non-zero exit that would satisfy
    any status assertion today for entirely the wrong reason. `HOME` is a
    throwaway directory, so nothing here can reach the real `~/.claude`.
    """
    environ = clean_environment(
        HOME=str(home),
        RHIZOME_HTTP_PORT=str(port),
        RHIZOME_SOCKET=str(home / "ingest.sock"),
    )
    try:
        completed = subprocess.run(
            entry_argv((str(project), "--install-hooks")),
            cwd=str(REPO_ROOT),
            env=environ,
            capture_output=True,
            text=True,
            timeout=WRITE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"`rhi --install-hooks` had not returned after "
            f"{WRITE_TIMEOUT_SECONDS:.0f}s: a command that writes one file "
            "started a daemon"
        )
    if "unrecognized argument" in completed.stderr:
        pytest.fail("rhi has no --install-hooks flag:\n" + completed.stderr)
    return completed


def _doctor(project: Path, home: Path) -> subprocess.CompletedProcess:
    """`rhi <project> --doctor`, for the round trip at the bottom of this file."""
    return subprocess.run(
        entry_argv((str(project), "--doctor")),
        cwd=str(REPO_ROOT),
        env=clean_environment(HOME=str(home), RHIZOME_SOCKET=str(home / "ingest.sock")),
        capture_output=True,
        text=True,
        timeout=WRITE_TIMEOUT_SECONDS,
    )


def _written(project: Path) -> dict:
    return json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _our_entries(settings: dict) -> list[dict]:
    """Every `PostToolUse` entry that runs this project's hook."""
    return [
        entry
        for entry in settings.get("hooks", {}).get("PostToolUse", [])
        if any(
            "emit_event.py" in str(hook.get("command", ""))
            or "rhi-hook" in str(hook.get("command", ""))
            for hook in entry.get("hooks", [])
        )
    ]


def _our_commands(settings: dict) -> list[str]:
    return [
        str(hook.get("command", ""))
        for entry in _our_entries(settings)
        for hook in entry.get("hooks", [])
    ]


# ===========================================================================
# 1. the doctrine: nothing is written unless it was asked for
# ===========================================================================


def test_serving_a_project_writes_nothing_into_it(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The load-bearing half of "diagnose always, offer explicitly".

    An ordinary run over a project with no hooks is the case where the
    temptation to be helpful is strongest -- the graph is about to show the
    signature failure -- and it is exactly where a write would be least
    expected. Green today; it exists so it stays green once the installer does.
    """
    project = tmp_path_factory.mktemp("served")
    ingest = tmp_path_factory.mktemp("run") / "ingest.sock"

    running = start(
        (str(project), "--no-window", "--port", str(free_port()), "--socket", str(ingest))
    )
    try:
        running.wait_for_line(URL, STARTUP_TIMEOUT_SECONDS)
        time.sleep(0.5)
    finally:
        running.stop()

    assert not (project / ".claude").exists(), (
        "serving a project created .claude in it; .claude/settings.json is a "
        "committed file in many repositories, and a graph is not consent to "
        "edit one"
    )


# ===========================================================================
# 2. the flag
# ===========================================================================


def test_the_parser_accepts_an_install_hooks_flag() -> None:
    args = build_parser().parse_args(["--install-hooks"])

    assert args.install_hooks is True


def test_installing_is_not_the_default() -> None:
    args = build_parser().parse_args([])

    assert args.install_hooks is False


def test_the_install_flag_composes_with_a_project_directory() -> None:
    """The argparse reason for a flag rather than a subcommand, again."""
    args = build_parser().parse_args(["/home/someone/work/thing", "--install-hooks"])

    assert args.root == "/home/someone/work/thing"
    assert args.install_hooks is True


# ===========================================================================
# 3. a project with nothing in it
# ===========================================================================


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory):
    """One `rhi <dir> --install-hooks` over a project with no `.claude`."""
    project = tmp_path_factory.mktemp("fresh")
    home = tmp_path_factory.mktemp("home")
    port = free_port()
    return _install_hooks(project, home, port), project, home, port


def test_installing_hooks_succeeds(installed) -> None:
    completed, _project, _home, _port = installed

    assert completed.returncode == 0, (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


def test_installing_hooks_writes_the_settings_file(installed) -> None:
    """The one thing this command is for."""
    _completed, project, _home, _port = installed

    assert (project / ".claude" / "settings.json").is_file()


def test_the_written_file_carries_our_post_tool_use_hook(installed) -> None:
    _completed, project, _home, _port = installed

    assert len(_our_entries(_written(project))) == 1


def test_the_written_command_is_absolute(installed) -> None:
    """A relative command resolves against whatever directory Claude Code runs
    the hook from, which is not a directory anybody here chose."""
    _completed, project, _home, _port = installed
    command = _our_commands(_written(project))[0]

    program = shlex.split(command)[-1]

    assert Path(program).is_absolute(), command


def test_the_written_command_names_something_that_exists(installed) -> None:
    """The `stale` state, prevented at the moment of writing.

    `tests/test_capture_settings.py` pins the same property on this
    repository's own installed settings, after a rename broke it there. A
    freshly written one may not start out broken.
    """
    _completed, project, _home, _port = installed
    command = _our_commands(_written(project))[0]

    program = shlex.split(command)[-1]

    assert Path(program).is_file(), f"{command!r} names {program}, which is not there"


def test_the_preview_names_the_file_it_will_change(installed) -> None:
    """A user with several projects has to be able to see which one this was."""
    completed, project, _home, _port = installed

    assert str(project / ".claude" / "settings.json") in completed.stdout, (
        completed.stdout
    )


def test_the_preview_names_the_command_it_will_write(installed) -> None:
    """The command is the thing that rots, so it is the thing to show."""
    completed, project, _home, _port = installed
    command = _our_commands(_written(project))[0]

    assert command in completed.stdout, completed.stdout


def test_installing_hooks_starts_no_daemon(installed) -> None:
    """Same rule as `--doctor`: it must be usable while a daemon is running."""
    completed, _project, _home, _port = installed

    assert "rhizome_graph.daemon" not in completed.stderr, completed.stderr


def test_installing_hooks_leaves_the_port_free(installed) -> None:
    _completed, _project, _home, port = installed

    assert not port_is_busy(port)


def test_installing_hooks_opens_no_ingest_socket(installed) -> None:
    _completed, _project, home, _port = installed

    assert not (home / "ingest.sock").exists()


def test_installing_hooks_touches_no_settings_outside_the_project(installed) -> None:
    """The user-level `~/.claude` is a different scope and a different decision.

    A hook installed there attributes every project on the machine, which is not
    what "instrument this project" means.
    """
    _completed, _project, home, _port = installed

    assert not (home / ".claude").exists(), f"{home}/.claude was created"


# ===========================================================================
# 4. running it twice
# ===========================================================================


def test_installing_twice_leaves_one_hook_block(tmp_path: Path) -> None:
    """Idempotence end to end, which is the property a re-run depends on.

    Two identical blocks fire the hook twice per tool call: every change flashes
    twice on the graph, and nothing anywhere reports an error.
    """
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    _install_hooks(project, home, free_port())
    _install_hooks(project, home, free_port())

    assert len(_our_entries(_written(project))) == 1


def test_installing_twice_leaves_one_hook_command(tmp_path: Path) -> None:
    """The same property one level down: not two commands inside one entry."""
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    _install_hooks(project, home, free_port())
    _install_hooks(project, home, free_port())

    assert len(_our_commands(_written(project))) == 1


# ===========================================================================
# 5. a project that already has other hooks and other settings
# ===========================================================================


def _seed(project: Path, settings: dict) -> None:
    claude = project / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")


def test_an_unrelated_post_tool_use_hook_survives_the_install(tmp_path: Path) -> None:
    """The clobber this design exists to prevent, on real bytes on a real disk."""
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _seed(project, {"hooks": {"PostToolUse": [FOREIGN_ENTRY]}})

    _install_hooks(project, home, free_port())

    assert FOREIGN_ENTRY in _written(project)["hooks"]["PostToolUse"]


def test_settings_that_are_not_hooks_survive_the_install(tmp_path: Path) -> None:
    """`permissions` decides what an agent may do. Losing it is not a cosmetic
    regression."""
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _seed(project, {"permissions": {"allow": ["Bash(ls:*)"]}, "model": "opus"})

    _install_hooks(project, home, free_port())

    written = _written(project)
    assert written["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert written["model"] == "opus"


# ===========================================================================
# 6. a settings file nobody can parse is refused, not replaced
# ===========================================================================


def test_a_settings_file_that_is_not_json_is_left_exactly_as_it_was(
    tmp_path: Path,
) -> None:
    """Never clobber. Whatever is in there, nobody here can reconstruct it."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    settings = project / ".claude" / "settings.json"
    original = '{"hooks": {,,, "permissions": "precious"'
    settings.write_text(original, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    _install_hooks(project, home, free_port())

    assert settings.read_text(encoding="utf-8") == original


def test_refusing_an_unparseable_settings_file_is_reported(tmp_path: Path) -> None:
    """Silently declining to install is the failure this whole stage is about:
    the user believes attribution is on, and the graph shows nobody working."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text('{"hooks": {,,,', encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    completed = _install_hooks(project, home, free_port())

    assert 0 < completed.returncode < 128, (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )
    assert "Traceback" not in completed.stderr, completed.stderr


# ===========================================================================
# 7. the round trip
# ===========================================================================


def test_a_project_that_was_just_installed_doctors_clean(tmp_path: Path) -> None:
    """What is written must be what is recognised, through the real commands.

    The unit version of this lives in `tests/test_hook_install_model.py`; this
    is the one that would catch a `hook_command` spelled one way by the writer
    and looked for another way by the reader, with two real processes and a real
    file between them.
    """
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _install_hooks(project, home, free_port())

    completed = _doctor(project, home)

    assert completed.returncode == 0, (
        "`rhi --doctor` does not recognise the hook `rhi --install-hooks` just "
        f"wrote\n--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )
