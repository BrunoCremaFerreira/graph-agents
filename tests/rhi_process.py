"""Driving the installed command's entry point from a checkout that has no install.

Not a test file (the name keeps pytest from collecting it): three test modules --
`test_cli_entry_point.py`, `test_rhi_start.py`, `test_port_selection.py` and
`test_ingest_socket_selection.py` -- all need to run `rhi` as a real process, and
a fourth copy of this machinery is how the four would drift apart.

**Why not the console script itself.** `rhi` becomes a `[project.scripts]` entry,
and a `[project.scripts]` entry only becomes a file on `$PATH` when the
distribution is (re)installed. This suite runs from the checkout -- the whole
point of `pythonpath = ["."]` in `pyproject.toml` -- and the virtualenv here holds
an editable install predating the entry point, so `.venv/bin/rhi` does not exist
and will not exist until somebody reinstalls. A test that needs an install is a
test that is skipped on the machine where it matters.

So the subprocess invokes the *target* of the console script rather than the
script: `python -c "import rhizome_graph.cli; sys.exit(cli.main())"`, from the
repository root, exactly as `tests/test_daemon_start_refusal.py` runs
`python -m daemon.server`. `ENTRY_POINT` below is the same string
`tests/test_cli_entry_point.py` pins into `pyproject.toml`, so the two cannot
name different functions without one of them going red.

**What is therefore uncovered**, and named here so nobody assumes otherwise: that
`pip install` actually produces a working `rhi` on `$PATH`. Everything on the far
side of the console-script shim -- its shebang, the environment `pip` bakes into
it, the wheel's `entry_points.txt` -- is out of reach of a suite that runs from a
checkout. What is covered is that the function the shim would call does the right
thing, and that the shim is declared to call that function.

**`sys.argv[0]` is set to `rhi`** so that argparse names the program the way the
user invoked it, rather than `-c`. That is cosmetic for every assertion here and
saves the `--help` output from being unreadable if anybody ever prints it.

**Nothing sets `PYTHONUNBUFFERED`, deliberately.** A URL printed to a pipe sits in
an 8 KiB block buffer until the process flushes, and this process's lifetime is
"until the user quits it" -- so an unflushed URL is a URL nobody ever sees, and a
test that papered over it with an environment variable would be certifying a
launcher that prints nothing. If `wait_for_line` times out, the first suspect is a
missing `flush=True`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The console script's target, `module:function`. `tests/test_cli_entry_point.py`
#: pins that `pyproject.toml` declares exactly this for `rhi`.
ENTRY_POINT = "rhizome_graph.cli:main"

#: The distribution name the console script belongs to, and the name
#: `importlib.metadata` is asked about.
DISTRIBUTION = "rhizome-graph"

#: Any absolute http(s) URL, as printed on a line of its own or inside a sentence.
URL = re.compile(r"https?://[^\s<>\"']+")

_PROGRAM = """\
import importlib
import sys

sys.argv[0] = "rhi"
cli = importlib.import_module({module!r})
{prelude}
sys.exit(getattr(cli, {function!r})())
"""


def entry_argv(argv: tuple[str, ...] = (), prelude: str = "") -> list[str]:
    """The command that runs the console script's target with `argv`.

    `prelude` is executed after `rhizome_graph.cli` is imported and before
    `main()` is called, with the module bound to the name `cli`. It exists for
    one purpose: two of the behaviours specified here -- moving off a busy
    default port, moving off a live default ingest socket -- are only reachable
    when the *default* is the thing in the way, and a test may not make the real
    `:8080` or the real `/tmp/rhizome-graph.sock` busy on the machine it runs on.
    Both would be somebody else's daemon. Patching the default to a throwaway one
    is the only way to observe the walk end to end; every other test leaves the
    prelude empty.
    """
    module, _, function = ENTRY_POINT.partition(":")
    code = _PROGRAM.format(module=module, function=function, prelude=prelude)
    return [sys.executable, "-c", code, *argv]


def clean_environment(**extra: str) -> dict[str, str]:
    """This process's environment with every `RHIZOME_*` variable removed.

    The same scrub `tests/test_run_settings.py` performs, and for the same
    reason: a developer shell that exports `RHIZOME_PROJECT_ROOT` or
    `RHIZOME_SOCKET` must not be able to change what these tests measure.
    """
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("RHIZOME_")
    }
    environment.update(extra)
    return environment


def free_port() -> int:
    """An ephemeral port, released before anything under test binds it."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def port_is_busy(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    """Fetch `url`, returning (status, body). An HTTP error is an answer too."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


@dataclass
class Running:
    """A live `rhi`, with its two streams drained by threads as they arrive."""

    process: subprocess.Popen
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)

    def wait_for_line(self, pattern: re.Pattern[str], timeout: float) -> re.Match[str]:
        """The first stdout line matching `pattern`, or an assertion failure."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in list(self.stdout):
                found = pattern.search(line)
                if found is not None:
                    return found
            if self.process.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError(
            f"nothing on stdout matched {pattern.pattern!r} within {timeout:.0f}s.\n"
            f"exit status: {self.process.poll()}\n"
            f"--- stdout ---\n{''.join(self.stdout)}"
            f"--- stderr ---\n{''.join(self.stderr)}"
        )

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def stop(self, timeout: float = 30.0) -> int:
        """Ask it to quit the way a terminal would, and return its exit status."""
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - a hang is its own bug
            self.process.kill()
            self.process.wait(timeout=timeout)
            raise AssertionError("rhi ignored SIGTERM and had to be killed")
        return int(self.process.returncode)

    @property
    def out(self) -> str:
        return "".join(self.stdout)

    @property
    def err(self) -> str:
        return "".join(self.stderr)


def start(
    argv: tuple[str, ...],
    environ: dict[str, str] | None = None,
    prelude: str = "",
) -> Running:
    """Launch `rhi` and start draining both of its streams."""
    process = subprocess.Popen(
        entry_argv(argv, prelude),
        cwd=str(REPO_ROOT),
        env=clean_environment() if environ is None else environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    running = Running(process=process)
    for stream, sink in ((process.stdout, running.stdout), (process.stderr, running.stderr)):
        thread = threading.Thread(target=_drain, args=(stream, sink), daemon=True)
        thread.start()
    return running


def _drain(stream, sink: list[str]) -> None:
    try:
        for line in stream:
            sink.append(line)
    except Exception:  # pragma: no cover - the pipe dying is the process dying
        pass
