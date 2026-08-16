"""Contract tests (RED) for start.sh handing ONE control token to both processes.

The defect
----------
The control token landed on the daemon side only. The daemon mints a token at
boot (`rhizome_graph.token.token_from_env`, which honours `RHIZOME_TOKEN`),
injects it into the `index.html` IT serves, and refuses any command frame that
does not carry it. That closes the hole everywhere except in one mode:

`./start.sh --dev` does not serve the page. The daemon runs in the background
and Vite serves the front end from source in the foreground (`web/vite.config.ts`
proxies `/ws` back to the daemon), so the daemon's HTTP handler -- the only thing
that injects `window.__RHIZOME_TOKEN__` -- never touches that HTML. The daemon
mints a token, the page has none, and every `ctrl+L`, every tab-completion and
every file click is refused. `--dev` is broken today, by the change that fixed
prod.

`web/src/token.ts` already has the second door: it falls back to
`import.meta.env.VITE_RHIZOME_TOKEN`. Nothing puts a value behind it.

The contract specified here
---------------------------
* In `--dev`, start.sh exports the SAME value as `RHIZOME_TOKEN` (which the
  daemon reads) and `VITE_RHIZOME_TOKEN` (only `VITE_`-prefixed variables reach
  `import.meta.env`). One token, two processes; if the two values differ the
  page is locked out exactly as it is today, so the equality is the property,
  not the presence of either variable alone.
* A `RHIZOME_TOKEN` already in the environment is respected, never overwritten.
  That is how a probe, a second viewer or a wrapper script is pointed at the
  same daemon; minting over it would silently disconnect the tool the user just
  configured.
* The token is minted by `rhizome_graph.token.mint_token`, not reinvented in
  shell. One place decides what a token looks like: a `$RANDOM`-flavoured second
  implementation would drift in entropy and in alphabet from the one the tests
  in `tests/test_token.py` actually pin down.
* Prod does NOT export `VITE_RHIZOME_TOKEN`. This is the trap. In prod the
  daemon injects the token into the page itself, so the variable buys nothing --
  and a `VITE_`-prefixed variable present during `npm run build` is *substituted
  into the bundle*, so `web/dist` would ship a hard-coded token from the machine
  that built it: stale (the next daemon mints another one) and a secret
  committed to a build artifact.

How it is observed
------------------
`--print-token`, following the precedent `--print-npm` set in
`tests/test_start_script.py`: resolve the token, print it -- and nothing else --
on stdout, exit 0, start no daemon, and send every log line to stderr, so the
answer is machine-readable.

That flag alone cannot show that BOTH processes received it, so the stubs
standing in for `python` and `npm` dump their own environment to a file. Reading
the daemon's dump and Vite's dump gives the two values side by side, which is
the only way to assert they are equal.

Isolation: start.sh is copied into a tmpdir (it derives REPO_ROOT from
BASH_SOURCE, so the copy is the repo as far as it knows), and node/npm/python are
shell stubs on a PATH that starts with the stub directory. Nothing here touches
the network, the real repo, or $HOME.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
START_SH = REPO_ROOT / "start.sh"

DAEMON_MARKER = "DAEMON-STARTED"

# What the python stub prints when start.sh asks it to mint a token. A value no
# shell would ever invent, so "the token came from rhizome_graph.token" is
# checkable by looking at the token itself.
MINTED_BY_PYTHON = "MINTED-BY-PYTHON-3f9c2ab1d7e4"

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return ANSI.sub("", text)


def _stdout_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [ln.strip() for ln in _clean(result.stdout).splitlines() if ln.strip()]


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Sandbox:
    """A throwaway copy of the repo whose `python` and `npm` dump their env."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.bin = root / "stub-bin"
        self.bin.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.envs = root / "envs"  # one dump per process launched
        self.envs.mkdir()
        self.python_log = root / "python.log"
        self.npm_log = root / "npm.log"

        shutil.copy2(START_SH, root / "start.sh")
        (root / "web").mkdir()
        (root / "web" / "package.json").write_text('{"name":"web","version":"0.0.0"}\n')
        (root / "web" / "package-lock.json").write_text(
            '{"name":"web","lockfileVersion":3,"packages":{}}\n'
        )

        # `python`: mints on demand, and records the environment the daemon got.
        self.python = _write_exec(
            self.bin / "python-stub",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.python_log}"\n'
            "case \"$*\" in\n"
            f'  *daemon.server*) env > "{self.envs}/daemon"; echo "{DAEMON_MARKER}";'
            " exit 0;;\n"
            f'  *mint_token*|*rhizome_graph.token*) env > "{self.envs}/mint";'
            f' printf "%s\\n" "{MINTED_BY_PYTHON}"; exit 0;;\n'
            "esac\n"
            "exit 0\n",
        )
        _write_exec(self.bin / "python3", f'#!/bin/sh\nexec "{self.python}" "$@"\n')

        # `npm`: records the environment of every command start.sh runs, under a
        # name built from the verb ("ci", "run-dev", "run-build").
        _write_exec(
            self.bin / "npm",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.npm_log}"\n'
            'slot="$1"\n'
            'if [ -n "${2:-}" ]; then slot="$1-$2"; fi\n'
            f'env > "{self.envs}/npm-$slot"\n'
            'case "$*" in *--version*) echo "10.9.0"; exit 0;; esac\n'
            # `npm run dev` is the foreground process in --dev; a moment here
            # lets the backgrounded daemon stub write its own dump first.
            'if [ "$1" = "run" ] && [ "$2" = "dev" ]; then sleep 1; fi\n'
            "exit 0\n",
        )
        _write_exec(
            self.bin / "node",
            '#!/bin/sh\ncase "$1" in -v|--version) echo "v18.19.1";; esac\nexit 0\n',
        )

    # -- fixture pieces ----------------------------------------------------
    def with_node_modules(self) -> Path:
        """A warm tree, so --dev reuses it and goes straight to `npm run dev`."""
        mods = self.root / "web" / "node_modules"
        mods.mkdir(parents=True, exist_ok=True)
        (mods / ".package-lock.json").write_text("{}\n")
        return mods

    # -- observations ------------------------------------------------------
    def process_env(self, name: str, *, timeout: float = 5.0) -> dict[str, str]:
        """The environment of one launched process, by dump name.

        Polls: in --dev the daemon is backgrounded and start.sh can return
        before it has written anything.
        """
        dump = self.envs / name
        deadline = time.monotonic() + timeout
        while not dump.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert dump.exists(), (
            f"no environment dump for {name!r}; start.sh launched "
            f"{sorted(p.name for p in self.envs.iterdir())}"
        )
        env: dict[str, str] = {}
        for line in dump.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                env.setdefault(key, value)
        return env

    def npm_calls(self) -> list[str]:
        if not self.npm_log.exists():
            return []
        return [ln.strip() for ln in self.npm_log.read_text().splitlines() if ln.strip()]

    def python_calls(self) -> list[str]:
        if not self.python_log.exists():
            return []
        return [
            ln.strip() for ln in self.python_log.read_text().splitlines() if ln.strip()
        ]

    # -- running -----------------------------------------------------------
    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "LC_ALL": "C",
            "PYTHON": str(self.python),
            "RHIZOME_PROJECT_ROOT": str(self.root),
        }
        env.update(extra)
        return env

    def run(self, *args: str, timeout: int = 60, **extra_env: str):
        return subprocess.run(
            [shutil.which("bash") or "/bin/bash", str(self.root / "start.sh"), *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=self.env(**extra_env),
            timeout=timeout,
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path / "repo")


# ---------------------------------------------------------------------------
# 1. --print-token: the token, made observable.
# ---------------------------------------------------------------------------


def test_print_token_prints_one_token_and_nothing_else(sandbox: Sandbox):
    sandbox.with_node_modules()

    result = sandbox.run("--dev", "--print-token")

    assert result.returncode == 0, result.stderr
    assert len(_stdout_lines(result)) == 1, (
        "stdout is the answer, like --print-npm: every log line belongs on "
        f"stderr, or the token cannot be read by a machine. Got {result.stdout!r}"
    )
    assert _stdout_lines(result)[0], "the printed token must not be empty"


def test_print_token_starts_no_daemon(sandbox: Sandbox):
    sandbox.with_node_modules()

    result = sandbox.run("--dev", "--print-token")

    assert result.returncode == 0, result.stderr  # the flag exists at all
    assert DAEMON_MARKER not in result.stdout, (
        "--print-token answers a question; starting a daemon that then has to "
        "be killed is a side effect nobody asked for"
    )


def test_a_pinned_rhizome_token_is_printed_back_unchanged(sandbox: Sandbox):
    sandbox.with_node_modules()

    result = sandbox.run("--dev", "--print-token", RHIZOME_TOKEN="pinned-by-the-user")

    assert result.returncode == 0, result.stderr
    assert _stdout_lines(result) == ["pinned-by-the-user"], (
        "an existing RHIZOME_TOKEN is how a probe or a second tool is pointed "
        "at this daemon; minting over it disconnects that tool silently"
    )


def test_a_minted_token_is_the_value_the_token_module_produced(sandbox: Sandbox):
    sandbox.with_node_modules()

    result = sandbox.run("--dev", "--print-token")

    assert _stdout_lines(result) == [MINTED_BY_PYTHON], (
        "the token must come from rhizome_graph.token.mint_token, not from a "
        "second implementation in shell: one place decides a token's entropy "
        f"and alphabet. python saw {sandbox.python_calls()}"
    )


def test_the_mint_is_delegated_to_the_token_module(sandbox: Sandbox):
    sandbox.with_node_modules()

    sandbox.run("--dev", "--print-token")

    assert any(
        "rhizome_graph" in call and ("mint_token" in call or "token" in call)
        for call in sandbox.python_calls()
    ), (
        "no python invocation named rhizome_graph's token module; a $RANDOM-"
        f"flavoured token in the script is the finding, not the fix. python saw "
        f"{sandbox.python_calls()}"
    )


# ---------------------------------------------------------------------------
# 2. --dev: one token in front of both processes.
#
# The daemon reads RHIZOME_TOKEN; Vite only exposes VITE_-prefixed variables to
# `import.meta.env`, which is where web/src/token.ts looks when the page was not
# served by the daemon.
# ---------------------------------------------------------------------------


def test_dev_gives_the_daemon_a_token(sandbox: Sandbox):
    sandbox.with_node_modules()

    sandbox.run("--dev")

    assert sandbox.process_env("daemon").get("RHIZOME_TOKEN"), (
        "in --dev the daemon must be told which token to expect, instead of "
        "minting a private one the page can never learn"
    )


def test_dev_gives_vite_a_vite_prefixed_token(sandbox: Sandbox):
    sandbox.with_node_modules()

    sandbox.run("--dev")

    assert sandbox.process_env("npm-run-dev").get("VITE_RHIZOME_TOKEN"), (
        "the Vite-served page has no injected window.__RHIZOME_TOKEN__, so its "
        "only source is import.meta.env.VITE_RHIZOME_TOKEN; without it every "
        "ctrl+L, completion and file click is refused"
    )


def test_dev_gives_the_daemon_and_vite_the_very_same_token(sandbox: Sandbox):
    sandbox.with_node_modules()

    sandbox.run("--dev")

    daemon = sandbox.process_env("daemon").get("RHIZOME_TOKEN", "")
    vite = sandbox.process_env("npm-run-dev").get("VITE_RHIZOME_TOKEN", "")
    assert daemon == vite != "", (
        "two different tokens lock the page out exactly as no token does: the "
        f"daemon got {daemon!r} and Vite got {vite!r}"
    )


def test_dev_hands_both_processes_the_pinned_token_rather_than_a_fresh_one(
    sandbox: Sandbox,
):
    sandbox.with_node_modules()

    sandbox.run("--dev", RHIZOME_TOKEN="pinned-by-the-user")

    assert sandbox.process_env("daemon").get("RHIZOME_TOKEN") == "pinned-by-the-user"
    assert (
        sandbox.process_env("npm-run-dev").get("VITE_RHIZOME_TOKEN")
        == "pinned-by-the-user"
    ), (
        "a pinned token must reach the page too, or --dev with RHIZOME_TOKEN "
        "set is the same lockout with an extra step"
    )


# ---------------------------------------------------------------------------
# 3. Prod: the VITE_ variable must NOT be there.
#
# Vite substitutes every VITE_-prefixed variable into the bundle at build time,
# so exporting one around `npm run build` writes a token into web/dist: stale by
# the next boot, and a secret inside a build artifact.
#
# Nothing exports it today, so these two are guards on the fix rather than RED.
# ---------------------------------------------------------------------------


def test_prod_does_not_bake_a_vite_token_into_the_build(sandbox: Sandbox):
    result = sandbox.run(timeout=90)

    assert result.returncode == 0, result.stderr
    assert ["run", "build"] in [call.split()[:2] for call in sandbox.npm_calls()], (
        f"expected a prod build to run; npm saw {sandbox.npm_calls()}"
    )
    assert "VITE_RHIZOME_TOKEN" not in sandbox.process_env("npm-run-build"), (
        "Vite substitutes VITE_-prefixed variables into the bundle, so this "
        "would ship a hard-coded token inside web/dist; in prod the daemon "
        "injects the token into the page it serves and needs no build-time one"
    )


def test_a_pinned_token_is_still_not_exposed_to_the_prod_build(sandbox: Sandbox):
    sandbox.run(timeout=90, RHIZOME_TOKEN="pinned-by-the-user")

    assert "VITE_RHIZOME_TOKEN" not in sandbox.process_env("npm-run-build"), (
        "pinning a token is not permission to commit it to a build artifact; "
        "the prod page reads the injected global, never import.meta.env"
    )
