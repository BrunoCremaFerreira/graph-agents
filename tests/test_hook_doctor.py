"""Contract tests (RED) for `rhi --doctor`: report the hooks, start nothing.

Motivation: attribution is the point of this program, and attribution exists
only if the `hooks` block reaches the OBSERVED project. Both ways of not having
it are hard to see from the graph:

  * **Absent** -- the tree updates while nobody is on camera. CLAUDE.md calls
    this the project's signature failure, indistinguishable from "no agent is
    working right now", and records that it cost real hours.
  * **Stale** -- worse, and measured rather than imagined. Earlier in this very
    session all three settings files in this repository named a hook under a
    directory that stopped existing when the project was renamed, and every tool
    call came back with a blocking hook error. The graph looks the same as
    absent; the agent session does not.

Neither is visible from inside the page, because the page cannot read the disk
and the daemon cannot tell "no hook installed" from "nobody is working". So
there is a command that says so, and it says so **without starting anything**:
the precedent is `./start.sh --print-token`, which prints and starts nothing,
and the reason is the same -- a diagnostic that binds a port and takes over
`/tmp/rhizome-graph.sock` cannot be run while the thing it is diagnosing is
running, which is exactly when somebody wants to run it.

**It reads BOTH settings files, and a hook in either one is a pass.** Claude
Code merges hooks from the user-level `~/.claude/settings.json` and the
project's `.claude/settings.json`, and a hook installed globally really does
fire for sessions in this project. An earlier draft of this file diagnosed the
project alone, which would have reported broken attribution to the person
running it *right now* -- their block is in the user-level file and it works. A
false alarm from a diagnostic is worse than no diagnostic, because it teaches
the reader to ignore the next one, and the entire argument for having this
command is that the failure it looks for costs hours to spot.

So: a working hook in either file is a pass; the report says **which file
carries it**, on one line with the command, because two settings files and two
checkouts is exactly the situation where "there is a hook somewhere" is not an
answer. A stale command in either file is a failure naming the file it is in --
both files' hooks run, so a rotted one errors on every tool call however healthy
the other is. The precedence is `overall_state` in `rhizome_graph.hookinstall`,
specified as pure logic in `tests/test_hook_install_model.py` rather than
through these subprocesses.

**Reading the user's file is not touching it.** Every run below sets `HOME` to a
throwaway directory, so no test here can read or write the real `~/.claude`; the
fixtures put the global block there themselves. That mechanism is what makes the
two-file rule specifiable at all, and it is also what proves the command never
writes: `--doctor` must leave both directories exactly as it found them.

**A flag, not a subcommand, and the reason is argparse rather than taste.** The
primary form of this program is `rhi <dir>`: `root` is an optional positional.
Subparsers and an optional positional cannot coexist -- argparse consumes the
first positional as the subcommand name, so `rhi doctor` and `rhi ./doctor`
become the same string and a directory called `doctor` becomes unopenable, while
`rhi mydir` becomes "invalid choice". A flag composes with the positional
instead. `--install-hooks` (`tests/test_hook_install_command.py`) is spelled the
same way for the same reason.

**What is NOT specified here:** the wording of the report. What is pinned is
that a single line carries the settings file and the command together, that the
remedy is named when there is nothing, and that the exit status is usable from a
script -- because "is attribution wired up here?" is a question a wrapper will
eventually want to ask without reading prose.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rhi_process import (
    REPO_ROOT,
    clean_environment,
    entry_argv,
    free_port,
    port_is_busy,
)
from rhizome_graph.cli import build_parser

#: How long a command that only reports is given to report and return. Generous
#: by an order of magnitude; a `--doctor` that has not returned by now has
#: started a daemon, which is the thing being ruled out.
REPORT_TIMEOUT_SECONDS = 30.0

#: A hook command that really resolves on this machine: this checkout's own
#: adapter. `tests/test_capture_settings.py` already pins that this path is the
#: one the installed settings must name and that it exists.
WORKING_COMMAND = f"python3 {REPO_ROOT / 'hooks' / 'emit_event.py'}"

#: The command this repository's settings carried after the rename, kept as the
#: `stale` fixture because it is the real one. `graph-agents` is a directory that
#: no longer exists, and the interpreter fails before the hook's own "exit 0 and
#: stay silent" rule can run.
ROTTED_COMMAND = "python3 /home/brn/projects/graph-agents/hooks/emit_event.py"


def _settings(command: str) -> str:
    return json.dumps(
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit|MultiEdit|Bash|Read",
                        "hooks": [{"type": "command", "command": command}],
                    }
                ]
            }
        },
        indent=2,
    )


def settings_path(directory: Path) -> Path:
    """Where a settings file lives under `directory`, project or home alike.

    The same relative spelling in both places, which is what makes "the user's
    file" and "the project's file" one mechanism rather than two.
    """
    return directory / ".claude" / "settings.json"


def _install(directory: Path, command: str) -> Path:
    """Write a `.claude/settings.json` under `directory`, naming `command`."""
    written = settings_path(directory)
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(_settings(command), encoding="utf-8")
    return written


def _doctor(project: Path, home: Path, port: int | None = None):
    """Run `rhi <project> --doctor` in isolation, and refuse a false green.

    Two guards. A timeout is failed here rather than reported as an exit status,
    because a `--doctor` that has not returned has started a daemon. And an
    argparse "unrecognized arguments" is *also* a non-zero exit, so a test
    asserting only the status would pass today, before the flag exists, for
    entirely the wrong reason -- that is caught and named.

    `HOME` is a throwaway directory: the user-level settings file is one this
    fixture wrote, so the two-file rule can be specified without any test coming
    near the real `~/.claude`. `RHIZOME_*` is scrubbed so a developer's exported
    socket or root cannot change what is measured.
    """
    environ = clean_environment(
        HOME=str(home),
        RHIZOME_HTTP_PORT=str(port if port is not None else free_port()),
        RHIZOME_SOCKET=str(home / "ingest.sock"),
    )
    try:
        completed = subprocess.run(
            entry_argv((str(project), "--doctor")),
            cwd=str(REPO_ROOT),
            env=environ,
            capture_output=True,
            text=True,
            timeout=REPORT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"`rhi --doctor` had not returned after {REPORT_TIMEOUT_SECONDS:.0f}s: "
            "a command that only reports started a daemon"
        )
    if "unrecognized argument" in completed.stderr:
        pytest.fail("rhi has no --doctor flag:\n" + completed.stderr)
    return completed


def says_together(completed, *needles: str) -> bool:
    """Is there ONE line of output carrying all of `needles`?

    The report has to associate a command with the file it lives in, and with
    two settings files in play, "both strings appear somewhere" is satisfied by
    a report that lists the files in one place and the commands in another --
    which is precisely the report that does not answer "which one?". Asserted as
    a line rather than as a format, so the wording stays free.
    """
    return any(
        all(needle in line for needle in needles)
        for line in (completed.stdout + completed.stderr).splitlines()
    )


def _blank(factory: pytest.TempPathFactory, name: str) -> Path:
    """A directory with no `.claude` in it at all."""
    return factory.mktemp(name)


# ===========================================================================
# 1. the flag itself
# ===========================================================================


def test_the_parser_accepts_a_doctor_flag() -> None:
    """The front door for the diagnosis, spelled so a script can call it."""
    args = build_parser().parse_args(["--doctor"])

    assert args.doctor is True


def test_doctoring_is_not_the_default() -> None:
    """`rhi <dir>` still serves a graph; nothing about that changes."""
    args = build_parser().parse_args([])

    assert args.doctor is False


def test_the_doctor_flag_composes_with_a_project_directory() -> None:
    """The whole argument for a flag over a subcommand, as a test.

    A subparser would have eaten this positional: `rhi mydir --doctor` would be
    "invalid choice: 'mydir'", and `rhi doctor` would be ambiguous with a
    directory of that name.
    """
    args = build_parser().parse_args(["/home/someone/work/thing", "--doctor"])

    assert args.root == "/home/someone/work/thing"
    assert args.doctor is True


# ===========================================================================
# 2. neither file has anything
# ===========================================================================


@pytest.fixture(scope="module")
def unhooked(tmp_path_factory: pytest.TempPathFactory):
    """`rhi <dir> --doctor` with no `.claude` in the project and none in HOME.

    Module-scoped: one run answers every question below, and booting a
    subprocess per assertion would cost eight starts to say eight things.
    """
    project = _blank(tmp_path_factory, "unhooked")
    home = _blank(tmp_path_factory, "home")
    port = free_port()
    return _doctor(project, home, port), project, home, port


def test_a_project_with_hooks_nowhere_is_reported_as_a_failure(unhooked) -> None:
    """The status a wrapper reads. Attribution is off; that is not a success."""
    completed, _project, _home, _port = unhooked

    assert 0 < completed.returncode < 128, (
        f"`rhi --doctor` with no hook in either settings file exited "
        f"{completed.returncode}\n--- stdout ---\n{completed.stdout}"
        f"--- stderr ---\n{completed.stderr}"
    )


def test_the_report_names_the_project_settings_file_it_looked_at(unhooked) -> None:
    """"No hook found" is unactionable without saying where it looked.

    The commonest confusion this command will meet is a user who installed the
    block in the rhizome-graph checkout instead of in the project being watched.
    """
    completed, project, _home, _port = unhooked
    expected = str(settings_path(project))

    assert expected in completed.stdout + completed.stderr, (
        f"the report never mentions {expected}\n--- stdout ---\n{completed.stdout}"
        f"--- stderr ---\n{completed.stderr}"
    )


def test_the_report_names_the_user_settings_file_it_looked_at(unhooked) -> None:
    """Both files were consulted, so both are accounted for.

    Otherwise a reader who knows they installed globally cannot tell whether the
    command checked there and found nothing, or never looked -- and the second
    is a bug in this command rather than in their setup.
    """
    completed, _project, home, _port = unhooked
    expected = str(settings_path(home))

    assert expected in completed.stdout + completed.stderr, (
        f"the report never mentions {expected}\n--- stdout ---\n{completed.stdout}"
        f"--- stderr ---\n{completed.stderr}"
    )


def test_the_report_names_the_remedy(unhooked) -> None:
    """A diagnosis that does not say what to do next is half a diagnosis."""
    completed, _project, _home, _port = unhooked

    assert "--install-hooks" in completed.stdout + completed.stderr, (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


def test_doctoring_starts_no_daemon(unhooked) -> None:
    """The `--print-token` rule: it must be usable while the daemon is running.

    A cheap and honest proxy -- the daemon logs its seed on the way up, always --
    rather than a claim about the port during a run nobody watched.
    """
    completed, _project, _home, _port = unhooked

    assert "rhizome_graph.daemon" not in completed.stderr, (
        f"`rhi --doctor` produced daemon log output:\n{completed.stderr}"
    )


def test_doctoring_leaves_the_port_free(unhooked) -> None:
    """Nothing is left holding the port the next `rhi` will want."""
    _completed, _project, _home, port = unhooked

    assert not port_is_busy(port)


def test_doctoring_opens_no_ingest_socket(unhooked) -> None:
    """The socket is the shared name: taking it over derails a live daemon."""
    _completed, _project, home, _port = unhooked

    assert not (home / "ingest.sock").exists()


def test_doctoring_writes_nothing_into_the_project(unhooked) -> None:
    """`rhi` diagnoses always and writes only when asked, and this is the asking
    it is not. `.claude/settings.json` is a committed file in many repositories,
    including this one."""
    _completed, project, _home, _port = unhooked

    assert not (project / ".claude").exists(), (
        "`rhi --doctor` created .claude in the observed project"
    )


def test_doctoring_writes_nothing_into_the_users_home(unhooked) -> None:
    """Reading the user-level file is not permission to create one.

    A hook written there would attribute every project on the machine, which is
    a decision nobody made by typing `--doctor`.
    """
    _completed, _project, home, _port = unhooked

    assert not (home / ".claude").exists(), f"`rhi --doctor` created {home}/.claude"


# ===========================================================================
# 3. the hook is in the user's own settings, and only there
# ===========================================================================


@pytest.fixture(scope="module")
def global_only(tmp_path_factory: pytest.TempPathFactory):
    """The setup the person running this actually has: hooks in `~/.claude`."""
    project = _blank(tmp_path_factory, "global-only")
    home = _blank(tmp_path_factory, "home-hooked")
    _install(home, WORKING_COMMAND)
    return _doctor(project, home), project, home


def test_a_hook_installed_for_the_user_is_a_pass(global_only) -> None:
    """THE assertion this file was rewritten for.

    Claude Code merges the user-level hooks into every session, so this project
    *is* instrumented. Reporting a failure here would be a false alarm delivered
    to the one person guaranteed to be running the command, and a diagnostic
    that cries wolf is worse than none.
    """
    completed, _project, home = global_only

    assert completed.returncode == 0, (
        f"`rhi --doctor` called a working user-level hook a failure\n"
        f"user settings: {settings_path(home)}\n"
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


def test_a_user_level_hook_is_reported_against_the_file_that_carries_it(
    global_only,
) -> None:
    """One line, naming both, so "which one?" is answered rather than raised.

    With two settings files and possibly two checkouts, a report that lists the
    files in one place and the commands in another leaves the reader to guess
    the pairing -- and guessing wrong is how the wrong file gets edited.
    """
    completed, _project, home = global_only

    assert says_together(completed, str(settings_path(home)), WORKING_COMMAND), (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


# ===========================================================================
# 4. the hook is in the project, and only there
# ===========================================================================


@pytest.fixture(scope="module")
def project_only(tmp_path_factory: pytest.TempPathFactory):
    """The setup this repository has: hooks committed in the project."""
    project = _blank(tmp_path_factory, "project-only")
    home = _blank(tmp_path_factory, "home-bare")
    _install(project, WORKING_COMMAND)
    return _doctor(project, home), project, home


def test_a_hook_installed_in_the_project_is_a_pass(project_only) -> None:
    """Zero, so `rhi --doctor && rhi .` is a thing somebody can write."""
    completed, _project, _home = project_only

    assert completed.returncode == 0, (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


def test_a_project_hook_is_reported_against_the_file_that_carries_it(
    project_only,
) -> None:
    """The mirror of the user-level case, so neither is the whole rule by
    accident -- and a doctor that named one file for every answer would pass
    exactly one of these two."""
    completed, project, _home = project_only

    assert says_together(completed, str(settings_path(project)), WORKING_COMMAND), (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


# ===========================================================================
# 5. a hook that rotted -- the measured defect, in either file
# ===========================================================================


@pytest.fixture(scope="module")
def rotted_in_project(tmp_path_factory: pytest.TempPathFactory):
    """The exact breakage this repository had, in the project's own settings."""
    project = _blank(tmp_path_factory, "rotted-project")
    home = _blank(tmp_path_factory, "home-bare-2")
    _install(project, ROTTED_COMMAND)
    return _doctor(project, home), project, home


def test_a_hook_that_no_longer_resolves_is_reported_as_a_failure(
    rotted_in_project,
) -> None:
    """The state a `PostToolUse` array alone cannot show: the block is there,
    it looks right, and it errors on every tool call."""
    completed, _project, _home = rotted_in_project

    assert 0 < completed.returncode < 128, (
        f"`rhi --doctor` over a project whose hook script does not exist exited "
        f"{completed.returncode}\n--- stdout ---\n{completed.stdout}"
        f"--- stderr ---\n{completed.stderr}"
    )


def test_a_rotted_project_hook_is_reported_against_its_own_file(
    rotted_in_project,
) -> None:
    """The command and the file it is in, together: that pair is the fix."""
    completed, project, _home = rotted_in_project

    assert says_together(completed, str(settings_path(project)), ROTTED_COMMAND), (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


@pytest.fixture(scope="module")
def rotted_in_home(tmp_path_factory: pytest.TempPathFactory):
    """A rotted hook in the user-level file, over a project with none.

    The likeliest way this happens to a real person: they installed globally
    from a checkout once, and later moved or deleted the checkout.
    """
    project = _blank(tmp_path_factory, "rotted-home-project")
    home = _blank(tmp_path_factory, "home-rotted")
    _install(home, ROTTED_COMMAND)
    return _doctor(project, home), project, home


def test_a_rotted_user_level_hook_is_a_failure_too(rotted_in_home) -> None:
    """Reading the user's file has to mean reading it for real.

    A doctor that consulted `~/.claude` only far enough to say "something is
    installed" would turn the loudest failure this project has into a pass.
    """
    completed, _project, _home = rotted_in_home

    assert 0 < completed.returncode < 128, (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


def test_a_rotted_user_level_hook_is_reported_against_the_home_file(
    rotted_in_home,
) -> None:
    """Which file to edit, when the broken one is not in this project at all."""
    completed, _project, home = rotted_in_home

    assert says_together(completed, str(settings_path(home)), ROTTED_COMMAND), (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


# ===========================================================================
# 6. one file working, the other rotted -- the loud one wins
# ===========================================================================


@pytest.fixture(scope="module")
def working_project_rotted_home(tmp_path_factory: pytest.TempPathFactory):
    """A healthy project hook beside a rotted global one. Both of them run."""
    project = _blank(tmp_path_factory, "healthy-project")
    home = _blank(tmp_path_factory, "home-also-rotted")
    _install(project, WORKING_COMMAND)
    _install(home, ROTTED_COMMAND)
    return _doctor(project, home), project, home


def test_a_rotted_hook_beside_a_working_one_is_still_a_failure(
    working_project_rotted_home,
) -> None:
    """Hooks from both files run, so the broken one still errors every time.

    This is the same rule `tests/test_hook_install_model.py` pins within a
    single file -- stale wins because it is the loud one -- read across the two
    files Claude Code merges. A pass here would send somebody chasing a blocking
    hook error through the project that does not contain it.
    """
    completed, _project, _home = working_project_rotted_home

    assert 0 < completed.returncode < 128, (
        f"a rotted user-level hook was excused by a working project hook\n"
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


def test_both_hooks_are_reported_each_against_its_own_file(
    working_project_rotted_home,
) -> None:
    """The whole point of pairing a command with a file, in the one case where
    a reader has to tell two hooks apart to act."""
    completed, project, home = working_project_rotted_home

    assert says_together(completed, str(settings_path(project)), WORKING_COMMAND), (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )
    assert says_together(completed, str(settings_path(home)), ROTTED_COMMAND), (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )


# ===========================================================================
# 7. a settings file nobody can parse
# ===========================================================================


def test_a_settings_file_that_is_not_json_is_reported_rather_than_crashing(
    tmp_path: Path,
) -> None:
    """This command exists to be pointed at the broken file. Crashing on it is
    the one thing it may not do, and a traceback is not a diagnosis."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    settings_path(project).write_text('{"hooks": {,,,', encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    completed = _doctor(project, home)

    assert "Traceback" not in completed.stderr, completed.stderr
    assert 0 < completed.returncode < 128, completed.stdout + completed.stderr


def test_an_unreadable_user_file_does_not_hide_a_working_project_hook(
    tmp_path: Path,
) -> None:
    """A working hook is a working hook, whatever the other file says.

    Claude Code may well reject an unparseable `~/.claude/settings.json`, and
    that changes nothing about the project's own block, which fires either way.
    Failing here would be the false alarm this file exists to prevent, arriving
    by a different door.
    """
    project = tmp_path / "project"
    project.mkdir()
    _install(project, WORKING_COMMAND)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    settings_path(home).write_text("not json at all", encoding="utf-8")

    completed = _doctor(project, home)

    assert completed.returncode == 0, (
        f"--- stdout ---\n{completed.stdout}--- stderr ---\n{completed.stderr}"
    )
