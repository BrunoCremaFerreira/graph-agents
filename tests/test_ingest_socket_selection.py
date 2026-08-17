"""Contract tests (RED) for a second instance needing its own ingest socket.

Motivation: the port collision has a sibling that is quieter and worse. Two
daemons cannot share `/tmp/rhizome-graph.sock`, and the daemon already refuses to
take the name from a live one (`tests/test_ingest_socket_guard.py`,
`tests/test_daemon_start_refusal.py`) -- correctly, because the alternative is the
first daemon serving a browser while every hook event goes to the second. But
"refuse" is the whole answer today, which means a person watching one project
cannot watch a second at the same time, and the desktop application this stage is
building is one people will run twice.

So the second instance gets a socket of its own. Two rules shape it, and the
first is a constraint from outside this file:

  * **With nothing in the way, the answer is the default, unchanged, exactly.**
    `tests/test_project_naming.py` asserts `/tmp/rhizome-graph.sock` as a
    *literal*, in the hook and in the daemon separately -- deliberately, because
    comparing one constant to the other would pass while both were wrong, and
    because a hook writing to one path while a daemon listens on another produces
    the specific failure this project has already paid for: a tree that updates
    with nobody on camera, indistinguishable from "no agent is working". Every
    hook block already installed in somebody's `.claude/settings.json` names the
    default by omission. So the ordinary case may not move, ever, for any reason.

  * **The moved path is a pure function of the root.** Deterministic, because the
    person who has to put `RHIZOME_SOCKET` into a hook block needs the same
    answer tomorrow; and derived from the root because that is what distinguishes
    two instances that are otherwise identical.

Two properties of the derived path are less obvious and are pinned:

  * **It lives beside the default, not under the observed project.** A socket
    inside the watched tree would be seen by the watcher, drawn in the graph and
    listed by `git status` -- the program would be watching itself.

  * **It stays short.** An AF_UNIX address is limited to about 108 bytes, so a
    scheme that embeds the root in the name works on `~/w/x` and fails on a real
    checkout path. Anything derived has to be hashed down, and the test uses a
    deliberately long root to force it.

Finally, and this is the part that decides whether the feature is worth having:
**a moved socket must be announced.** Hooks reach a daemon by a path compiled
into a settings file; an instance quietly listening somewhere else has no
attribution at all, and no attribution looks exactly like a healthy setup with
nobody working. The value is printed in the shape it has to be pasted in --
`RHIZOME_SOCKET=<path>` -- and it is printed whenever the socket is not the
default, **including when the user chose it themselves with `--socket`**. That
last case is not the program repeating back what was typed: it is telling the
user what the hook block *in the observed project* has to say in order to match.
A user who passed `--socket` by hand still has hooks pointing at the default, and
that is the silent-attribution failure this project has already paid for once.
Attribution for a second instance is opt-in and explicit; silently broken
attribution is not an option.

**Two collisions are refusals, not walks**, and both follow the rule the port
already follows -- a default may be adjusted, an explicit request may not:

  * **A `--socket` that is live is refused.** Somebody typed that path; landing
    somewhere else is the same lie as `--port 9000` answering on 9001, and worse
    here, because the path they typed is the one they have already written into a
    hook block. A reader who knows the port rule should be able to predict this
    one, and that symmetry is the property.

  * **The same root started twice is refused.** The derived path is a pure
    function of the root, so a second instance watching the same project derives
    the path the first is already listening on, and stops there. That is right: a
    second window on the *same* project is a genuine collision, not a new
    instance to make room for. **The refusal is inherited, not re-implemented** --
    it is the ingest guard of `tests/test_ingest_socket_guard.py` and
    `tests/test_daemon_start_refusal.py` doing its job one layer down. Nobody
    should later add a second walk here to "handle" it, and the test below exists
    so that behaviour cannot quietly disappear in a refactor that has no other
    reason to notice it.

The composition tests patch `DEFAULT_SOCKET_PATH` for the reason spelled out
in `tests/test_port_selection.py`: a test may not make the real
`/tmp/rhizome-graph.sock` live, because on this machine it may already belong to
a daemon somebody is using.

`ingest_socket_path` is reached through the module (`cli.ingest_socket_path`)
rather than imported by name, deliberately: while it does not exist, a
`from ... import` fails at *collection*, and a collection error takes the entire
suite down with it -- leaving whoever is implementing this unable to run any
other test. Through the module, each test fails on its own line, naming the
attribute that is missing.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

import pytest

from rhi_process import (
    REPO_ROOT,
    URL,
    clean_environment,
    entry_argv,
    free_port,
    start,
)
from rhizome_graph import cli
from rhizome_graph.cli import DEFAULT_SOCKET_PATH

#: The literal, duplicated on purpose. See the module docstring, and the header
#: of `tests/test_project_naming.py` which explains why these are not compared
#: to one another's constants.
DEFAULT_SOCKET = "/tmp/rhizome-graph.sock"

#: A safe ceiling under the AF_UNIX address limit (108 bytes on Linux, 104 on
#: BSD), leaving room for nothing at all -- the path is used as given.
MAX_SOCKET_PATH_BYTES = 100

#: How the moved path is announced, in the shape it has to be pasted into a hook
#: block. Pinned as a shape rather than as a sentence: the surrounding words are
#: free, the assignment is not.
SOCKET_ADVICE = re.compile(r"RHIZOME_SOCKET=(\S+)")

#: One observed project, and a second one that is not it.
ROOT = "/home/alice/project"
OTHER_ROOT = "/home/alice/other"

STARTUP_TIMEOUT_SECONDS = 90.0

#: How long a start that must fail is given to seed a nearly empty directory,
#: discover the live socket and refuse. It refuses before it listens, so this is
#: a ceiling on the pathology rather than a measurement.
REFUSAL_TIMEOUT_SECONDS = 90.0


def _never_live(path: str) -> bool:
    return False


def _always_live(path: str) -> bool:
    return True


def _recording(answer: bool):
    asked: list[str] = []

    def is_live(path: str) -> bool:
        asked.append(path)
        return answer

    return asked, is_live


def _patched_default(path: Path) -> str:
    """A prelude that gives the subprocess a throwaway default socket path.

    `cli` is the name `tests/rhi_process.py` binds the module to inside the child.
    This works only while `settings_from` resolves its defaults from the module
    globals at call time; see the module docstring for why the real default
    cannot be used.
    """
    return f"cli.DEFAULT_SOCKET_PATH = {str(path)!r}\n"


def _holding(path: Path) -> socket.socket:
    """A live AF_UNIX listener -- the other instance."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    return listener


# --- 1. nothing in the way: the default, untouched --------------------------


def test_a_free_default_socket_is_used_exactly_as_it_is() -> None:
    """Every hook block already installed points here by omission."""
    chosen = cli.ingest_socket_path(ROOT, DEFAULT_SOCKET_PATH, _never_live)

    assert chosen == DEFAULT_SOCKET_PATH


def test_the_untouched_default_is_the_literal_path_the_hook_writes_to() -> None:
    """Spelled out rather than compared, for the reason in the docstring."""
    chosen = cli.ingest_socket_path(ROOT, DEFAULT_SOCKET_PATH, _never_live)

    assert chosen == DEFAULT_SOCKET


def test_the_default_is_the_path_that_is_probed() -> None:
    """The question asked is "is the ordinary socket taken", not anything else."""
    asked, is_live = _recording(answer=False)

    cli.ingest_socket_path(ROOT, DEFAULT_SOCKET_PATH, is_live)

    assert asked[:1] == [DEFAULT_SOCKET_PATH]


# --- 2. something is in the way: a path of this instance's own --------------


def test_a_live_default_socket_sends_this_instance_elsewhere() -> None:
    """Refusing to start is the behaviour this replaces for a second instance."""
    chosen = cli.ingest_socket_path(ROOT, DEFAULT_SOCKET_PATH, _always_live)

    assert chosen != DEFAULT_SOCKET_PATH


def test_the_same_root_always_yields_the_same_socket() -> None:
    """Whoever pastes `RHIZOME_SOCKET` into a hook block needs it to keep."""
    first = cli.ingest_socket_path(ROOT, DEFAULT_SOCKET_PATH, _always_live)
    second = cli.ingest_socket_path(ROOT, DEFAULT_SOCKET_PATH, _always_live)

    assert first == second


def test_two_roots_do_not_collide_on_one_socket() -> None:
    """Otherwise the second instance simply moves the collision one step."""
    one = cli.ingest_socket_path(ROOT, DEFAULT_SOCKET_PATH, _always_live)
    other = cli.ingest_socket_path(OTHER_ROOT, DEFAULT_SOCKET_PATH, _always_live)

    assert one != other


def test_the_moved_socket_sits_beside_the_default(tmp_path: Path) -> None:
    """Never inside the observed tree: the watcher would draw it in the graph."""
    chosen = cli.ingest_socket_path(str(tmp_path), DEFAULT_SOCKET_PATH, _always_live)

    assert os.path.dirname(chosen) == os.path.dirname(DEFAULT_SOCKET_PATH)


def test_the_moved_socket_is_recognisable_as_one() -> None:
    """It ends up in a settings file a person reads; it should look like a socket."""
    chosen = cli.ingest_socket_path(ROOT, DEFAULT_SOCKET_PATH, _always_live)

    assert chosen.endswith(".sock")


def test_a_long_root_still_yields_a_bindable_address() -> None:
    """AF_UNIX addresses are ~108 bytes, so the root has to be hashed, not spelled."""
    root = "/home/alice/" + "/".join(f"deeply-nested-directory-{n}" for n in range(12))

    chosen = cli.ingest_socket_path(root, DEFAULT_SOCKET_PATH, _always_live)

    assert len(chosen.encode("utf-8")) < MAX_SOCKET_PATH_BYTES, chosen


# --- 3. composition: the instance says where it is listening ----------------


def test_an_instance_on_the_default_socket_says_nothing_about_it(
    tmp_path: Path,
) -> None:
    """Silence is right here: the hook block everybody has already points here.

    The default is patched to a throwaway path (see the docstring); what makes
    this the "default" case is that `rhi` was given no `--socket` and nothing was
    listening there.
    """
    default = tmp_path / "default.sock"
    running = start(
        (str(tmp_path), "--no-window", "--port", str(free_port())),
        prelude=_patched_default(default),
    )
    try:
        running.wait_for_line(URL, STARTUP_TIMEOUT_SECONDS)
        assert default.exists(), "the daemon did not listen on its default socket"
    finally:
        running.stop()

    assert "RHIZOME_SOCKET" not in running.out, (
        "an instance on the ordinary socket advertised a variable nobody has to "
        f"set:\n{running.out}"
    )


def test_an_instance_pushed_off_the_default_prints_the_variable_to_set(
    tmp_path: Path,
) -> None:
    """The failure this avoids is silent: hooks reaching the other daemon."""
    default = tmp_path / "default.sock"
    listener = _holding(default)
    running = start(
        (str(tmp_path), "--no-window", "--port", str(free_port())),
        prelude=_patched_default(default),
    )
    try:
        advice = running.wait_for_line(SOCKET_ADVICE, STARTUP_TIMEOUT_SECONDS)
        announced = Path(advice.group(1))

        assert announced != default, "it announced the socket it could not have"
        assert announced.exists(), f"nothing is listening at the announced {announced}"
    finally:
        running.stop()
        listener.close()


def test_a_socket_chosen_by_hand_is_announced_too(tmp_path: Path) -> None:
    """`--socket` is explicit, so it is honoured -- and hooks still have to follow."""
    chosen = tmp_path / "chosen.sock"
    running = start(
        (
            str(tmp_path),
            "--no-window",
            "--port",
            str(free_port()),
            "--socket",
            str(chosen),
        )
    )
    try:
        advice = running.wait_for_line(SOCKET_ADVICE, STARTUP_TIMEOUT_SECONDS)

        assert advice.group(1) == str(chosen)
    finally:
        running.stop()


# --- 4. the two collisions that are refusals, not walks ---------------------


def _refused(argv: tuple[str, ...], prelude: str = "") -> subprocess.CompletedProcess:
    """One `rhi` that is expected to give up, run to completion.

    A start that neither serves nor refuses is the failure this whole section is
    about -- an instance that quietly moved -- so a timeout is reported as that
    rather than as a slow machine.
    """
    try:
        return subprocess.run(
            entry_argv(argv, prelude),
            cwd=str(REPO_ROOT),
            env=clean_environment(),
            capture_output=True,
            text=True,
            timeout=REFUSAL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "rhi neither started nor refused against a live ingest socket: a "
            "socket that is taken must be refused, never silently moved off"
        )


def test_a_socket_named_on_the_command_line_is_refused_when_it_is_live(
    tmp_path: Path,
) -> None:
    """Symmetry with `--port`: an explicit request is honoured or refused.

    Moving off it would be the same lie as `--port 9000` answering on 9001, and
    worse, because the path the user typed is the one already written into a hook
    block. Deliberately not a wording assertion -- the exit status is what a
    launcher reads, and the path is the actionable part.
    """
    chosen = tmp_path / "chosen.sock"
    listener = _holding(chosen)
    try:
        completed = _refused(
            (
                str(tmp_path),
                "--no-window",
                "--port",
                str(free_port()),
                "--socket",
                str(chosen),
            )
        )
    finally:
        listener.close()

    assert 0 < completed.returncode < 128, completed.stderr
    assert "Traceback" not in completed.stderr, completed.stderr
    assert str(chosen) in completed.stderr, completed.stderr


def test_a_second_instance_on_the_same_root_is_refused_rather_than_walking_again(
    tmp_path: Path,
) -> None:
    """One root, one derived socket -- so the second window on it stops.

    The refusal is **inherited**, not written again: the derived path is a pure
    function of the root, the first instance is listening on it, and the ingest
    guard already refuses to take a live socket's name. Nothing here should ever
    grow a second walk to "handle" this; a second window on the same project is a
    collision, not a new instance to make room for.

    The ordinary socket is held live by the test so that BOTH instances are
    pushed off it onto the derived path, which is the only way two instances of
    one root can meet: with the default free, the first would take the default
    and the second the derived one, and both would legitimately run -- that is the
    feature, not the collision.
    """
    root = tmp_path / "observed"
    root.mkdir()
    default = tmp_path / "default.sock"
    holder = _holding(default)
    first = start(
        (str(root), "--no-window", "--port", str(free_port())),
        prelude=_patched_default(default),
    )
    try:
        advice = first.wait_for_line(SOCKET_ADVICE, STARTUP_TIMEOUT_SECONDS)
        derived = advice.group(1)

        completed = _refused(
            (str(root), "--no-window", "--port", str(free_port())),
            prelude=_patched_default(default),
        )
    finally:
        first.stop()
        holder.close()

    assert 0 < completed.returncode < 128, completed.stderr
    assert "Traceback" not in completed.stderr, completed.stderr
    assert derived in completed.stderr, completed.stderr
