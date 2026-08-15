"""Contract tests (RED) for start.sh resolving its own `npm` dependency.

The defect these pin down, measured on this machine: Debian's `nodejs` package
installs /usr/bin/node (v18.19.1) and NO npm. With no `web/dist` yet, start.sh
dies with "Sem web/dist e sem npm no PATH" and the user is left to discover, by
hand, that the fix is to fetch the npm tarball from the registry and run
`node <dir>/package/bin/npm-cli.js`. That workaround belongs in the script.

The contract specified here
---------------------------
* New flag `--print-npm`: resolve the npm command, print it -- and NOTHING else
  -- on stdout, exit 0, and DO NOT start the daemon. Every log/progress line
  goes to stderr, so the flag is machine-readable (this is what makes the
  bootstrap testable at all without a network). On failure: non-zero exit and a
  message on stderr that names the real cause.
* Resolution order: `$NPM` from the environment > `npm` on `$PATH` > a
  repo-local bootstrap.
* The bootstrap downloads the npm tarball with `curl` or `wget` (the tests stub
  both and do not care which), unpacks it under `$REPO_ROOT/.npm-bootstrap/`,
  and yields a single executable file usable as `"$NPM" install` /
  `"$NPM" run build` -- i.e. a wrapper, not the bare `npm-cli.js`, since the
  script invokes `$NPM` as one command.
* The bootstrapped version is pinned to npm 10.9.4, and the cache is keyed by
  it. The only reason for *this* number: it is the newest npm that runs on the
  node that makes the bootstrap necessary here. npm 10.9.4 declares
  `engines.node: "^18.17.0 || >=20.5.0"` and this machine has node 18.19.1;
  npm 11.6.2 declares `^20.17.0 || >=22.9.0` and refuses. An unpinned `npm@latest`
  would resolve to something that cannot run.
* It is cached: with the cache warm, no download is attempted -- but "warm"
  includes the version, so bumping the pin invalidates an old cache by itself
  instead of serving the wrong npm forever.
* The front-end install uses `npm ci` whenever `web/package-lock.json` exists,
  and falls back to `npm install` if the `ci` fails.
* No node at all, or no network, must produce a clear message; and an existing
  `web/dist` must still be served instead of aborting.
* `.npm-bootstrap/` is a build artifact and belongs in `.gitignore`.

The second defect, measured after the pin was in place
-----------------------------------------------------
The pin does NOT keep `web/package-lock.json` clean, and the comment that said
so was wrong. Running `npm install` in the real `web/` with npm 10.9.4 still
dropped the same 42 `libc` lines: the lock is `lockfileVersion: 3` and its
`libc` fields are written only by npm 11, which cannot run on node 18 at all.
No npm available on this machine preserves them. So the fix is not a version:
it is the verb. `npm ci` installs from the lock and never rewrites it.

* With `web/package-lock.json` present, the front-end install is `npm ci`.
* If that `ci` exits non-zero -- which is what happens when the lock has drifted
  out of sync with `package.json`, the one case where `ci` legitimately aborts
  -- the script falls back to `npm install` and carries on, instead of dying and
  leaving the user with no build.
* With no lockfile, it is `npm install`, and no `ci` is attempted (a `ci`
  without a lock only produces a confusing error).
* `npm run build` follows the install in every path.
* The install is NOT skipped because `web/node_modules` already exists. That
  guard is what made `--rebuild` a half-measure: it rebuilt, but reused
  whatever tree happened to be on disk. `--rebuild` means reinstall *and*
  rebuild.

The third defect: the fix above stopped at the prod branch
----------------------------------------------------------
`front_install` is only reachable from `build_front`, and `build_front` is only
called by the prod/`--rebuild` paths. The `--dev` branch kept its own private,
pre-fix install line -- `[[ -d web/node_modules ]] || npm install` -- so on a
machine with no `web/node_modules` (a fresh clone, a wiped tree, CI), a plain
`./start.sh --dev` still runs `npm install` and still rewrites
`web/package-lock.json`. Same defect, second door.

* `--dev` installs through the *same* path as prod: `npm ci` over a lockfile,
  fallback to `npm install` when that `ci` exits non-zero, plain `npm install`
  with no lockfile.
* `--dev` does NOT run `npm run build`. Vite serves from source; the dev branch
  ends in `npm run dev`. Asserting a build here would assert a lie.
* The `node_modules` guard: KEPT in `--dev`, unlike prod, and `--rebuild`
  overrides it. The prod guard was removed because the only paths that reach it
  are `--rebuild` ("reinstall AND rebuild") and the first-ever build, where
  there is nothing to skip. `--dev` is the opposite: it is the loop you restart
  a dozen times an hour, and `npm ci` deletes `node_modules` and refetches the
  whole tree every single time -- tens of seconds added to every hot-reload
  restart, for a tree that was already correct. So a warm tree is reused, and
  the way to force a reinstall stays the one word it already is everywhere
  else: `./start.sh --dev --rebuild`.

Isolation: start.sh is copied into a tmpdir (it derives REPO_ROOT from
BASH_SOURCE, so the copy is the repo as far as it knows), PATH is rebuilt from
symlinks to the real tools MINUS node/npm/npx, and node/npm/curl/wget/python are
shell stubs. Nothing here touches the network, the real repo, or $HOME.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
START_SH = REPO_ROOT / "start.sh"

BOOTSTRAP_DIRNAME = ".npm-bootstrap"
DAEMON_MARKER = "DAEMON-STARTED"

# The pinned npm line (see the module docstring for why it is 10, not 9).
NPM_VERSION = "10.9.4"
# Any other plausible version, used only to forge "a cache from another pin".
OTHER_NPM_VERSION = "9.9.4"

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Executables the sandbox hides from PATH so a machine that *does* have npm
# still exercises the "npm is missing" branch.
HIDDEN = {"node", "nodejs", "npm", "npx", "corepack", "yarn", "pnpm"}


def _clean(text: str) -> str:
    return ANSI.sub("", text)


def _stdout_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [ln.strip() for ln in _clean(result.stdout).splitlines() if ln.strip()]


def _mentions(text: str, *words: str) -> bool:
    low = _clean(text).lower()
    return any(word in low for word in words)


def _rewrite_version(root: Path, old: str, new: str) -> None:
    """Turn a warm cache into the cache of a *different* npm version.

    Deliberately layout-agnostic: whatever the bootstrap chose to name its
    directories or write into its wrapper, every occurrence of the version is
    rewritten (contents first, then path names, deepest first). If nothing
    changes, the cache carries no version at all -- which is exactly the bug
    the caller is testing for.
    """
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if old in text:
                path.write_text(text.replace(old, new))
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if old in path.name:
            path.rename(path.with_name(path.name.replace(old, new)))


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture(scope="session")
def clean_bin(tmp_path_factory) -> Path:
    """A PATH holding every real tool except node/npm/npx."""
    d = tmp_path_factory.mktemp("clean-bin")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isdir(entry):
            continue
        try:
            names = os.listdir(entry)
        except OSError:
            continue
        for name in names:
            if name in HIDDEN:
                continue
            link = d / name
            if link.exists() or link.is_symlink():
                continue
            src = os.path.join(entry, name)
            if os.path.isdir(src) or not os.access(src, os.X_OK):
                continue
            try:
                link.symlink_to(src)
            except OSError:
                pass
    assert shutil.which("tar", path=str(d)), "sandbox PATH must still have tar"
    assert not shutil.which("npm", path=str(d)), "sandbox PATH must hide npm"
    return d


@pytest.fixture(scope="session")
def npm_tarball(tmp_path_factory) -> Path:
    """A stand-in for registry.npmjs.org's npm-<v>.tgz, shaped like the real one."""
    d = tmp_path_factory.mktemp("registry")
    tgz = d / f"npm-{NPM_VERSION}.tgz"

    def _add(tar: tarfile.TarFile, name: str, text: str) -> None:
        data = text.encode()
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(tgz, "w:gz") as tar:
        _add(
            tar,
            "package/package.json",
            '{"name":"npm","version":"%s","bin":{"npm":"bin/npm-cli.js"}}' % NPM_VERSION,
        )
        _add(tar, "package/bin/npm-cli.js", "// fake npm cli\n")
    return tgz


class Sandbox:
    """A throwaway copy of the repo with a fabricated PATH."""

    def __init__(self, root: Path, clean_bin: Path, tarball: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.clean_bin = clean_bin
        self.tarball = tarball
        self.bin = root / "stub-bin"
        self.bin.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.node_log = root / "node.log"
        self.npm_log = root / "npm.log"
        self.download_log = root / "download.log"
        self.python_log = root / "python.log"

        shutil.copy2(START_SH, root / "start.sh")
        (root / "web").mkdir()
        (root / "web" / "package.json").write_text('{"name":"web","version":"0.0.0"}\n')

        self.python = _write_exec(
            self.bin / "python-stub",
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.python_log}"\n'
            f'case "$*" in *daemon.server*) echo "{DAEMON_MARKER}"; exit 0;; esac\n'
            "exit 0\n",
        )
        _write_exec(self.bin / "python3", f'#!/bin/sh\nexec "{self.python}" "$@"\n')

    # -- fixture pieces ----------------------------------------------------
    def with_node(self) -> None:
        _write_exec(
            self.bin / "node",
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.node_log}"\n'
            'case "$1" in -v|--version) echo "v18.19.1"; exit 0;; esac\n'
            'case "$*" in *--version*) echo "10.8.2"; exit 0;; esac\n'
            "exit 0\n",
        )

    def with_npm_on_path(self, *, ci_exit: int = 0) -> Path:
        """Stub npm. Records every invocation; `ci` can be made to fail."""
        return _write_exec(
            self.bin / "npm",
            f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.npm_log}"\n'
            'case "$*" in *--version*) echo "10.9.0"; exit 0;; esac\n'
            f'case "$1" in ci) [ {ci_exit} -eq 0 ] || {{ echo "npm error code EUSAGE:'
            f' lock out of sync" >&2; exit {ci_exit}; }};; esac\n'
            "exit 0\n",
        )

    def with_lockfile(self) -> Path:
        """The real web/package-lock.json is lockfileVersion 3; shape matters none."""
        lock = self.root / "web" / "package-lock.json"
        lock.write_text('{"name":"web","lockfileVersion":3,"packages":{}}\n')
        return lock

    def with_node_modules(self) -> Path:
        mods = self.root / "web" / "node_modules"
        (mods / ".package-lock.json").parent.mkdir(parents=True, exist_ok=True)
        (mods / ".package-lock.json").write_text("{}\n")
        return mods

    # -- observations ------------------------------------------------------
    def npm_calls(self) -> list[str]:
        """Every argv start.sh handed to npm, in order."""
        if not self.npm_log.exists():
            return []
        return [ln.strip() for ln in self.npm_log.read_text().splitlines() if ln.strip()]

    def npm_verbs(self) -> list[str]:
        """First word of each npm invocation: 'ci', 'install', 'run', ..."""
        return [call.split()[0] for call in self.npm_calls() if call.split()]

    def with_downloader(self, *, working: bool) -> None:
        """Stub curl and wget. Both honour -o/-O <file> and otherwise use stdout."""
        fail = (
            'echo "stub downloader: network unreachable" >&2\nexit 1\n'
            if not working
            else ""
        )
        body = (
            f'#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "{self.download_log}"\n'
            f"{fail}"
            'out=""\nprev=""\nfor a in "$@"; do\n'
            '  case "$prev" in -o|--output|-O|--output-document) out="$a";; esac\n'
            '  case "$a" in -O-|-o-) out="-";; esac\n'
            '  prev="$a"\ndone\n'
            'if [ -n "$out" ] && [ "$out" != "-" ]; then\n'
            f'  cp "{self.tarball}" "$out"\n'
            "else\n"
            f'  cat "{self.tarball}"\n'
            "fi\n"
        )
        _write_exec(self.bin / "curl", body)
        _write_exec(self.bin / "wget", body)

    def with_dist(self) -> Path:
        dist = self.root / "web" / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<!doctype html><title>stale</title>")
        return dist

    # -- running -----------------------------------------------------------
    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{self.clean_bin}",
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

    @property
    def bootstrap_dir(self) -> Path:
        return self.root / BOOTSTRAP_DIRNAME


@pytest.fixture
def sandbox(tmp_path: Path, clean_bin: Path, npm_tarball: Path) -> Sandbox:
    return Sandbox(tmp_path / "repo", clean_bin, npm_tarball)


# ---------------------------------------------------------------------------
# 1. npm already on PATH: nothing changes.
# ---------------------------------------------------------------------------


def test_print_npm_reports_the_npm_already_on_path(sandbox: Sandbox):
    real = sandbox.with_npm_on_path()
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    result = sandbox.run("--print-npm")

    assert result.returncode == 0, result.stderr
    assert _stdout_lines(result) == [str(real)]


def test_an_npm_on_path_is_never_replaced_by_a_download(sandbox: Sandbox):
    sandbox.with_npm_on_path()
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    sandbox.run("--print-npm")

    assert not sandbox.download_log.exists(), sandbox.download_log.read_text()
    assert not sandbox.bootstrap_dir.exists()


def test_the_npm_environment_override_wins_over_the_one_on_path(sandbox: Sandbox):
    sandbox.with_npm_on_path()
    sandbox.with_node()
    chosen = _write_exec(sandbox.root / "my-npm", "#!/bin/sh\nexit 0\n")

    result = sandbox.run("--print-npm", NPM=str(chosen))

    assert result.returncode == 0, result.stderr
    assert _stdout_lines(result) == [str(chosen)]


def test_the_npm_environment_override_suppresses_the_bootstrap(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)
    chosen = _write_exec(sandbox.root / "my-npm", "#!/bin/sh\nexit 0\n")

    result = sandbox.run("--print-npm", NPM=str(chosen))

    assert _stdout_lines(result) == [str(chosen)]
    assert not sandbox.bootstrap_dir.exists()


# ---------------------------------------------------------------------------
# 2. npm missing, node present: bootstrap a repo-local npm.
# ---------------------------------------------------------------------------


def test_a_missing_npm_is_bootstrapped_into_a_repo_local_cache(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    result = sandbox.run("--print-npm")

    assert result.returncode == 0, result.stderr + result.stdout
    printed = Path(_stdout_lines(result)[-1])
    assert printed.is_relative_to(sandbox.bootstrap_dir), printed
    assert os.access(printed, os.X_OK), f"{printed} must be runnable as $NPM"


def test_the_bootstrap_downloads_the_npm_tarball(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    sandbox.run("--print-npm")

    assert sandbox.download_log.exists(), "expected curl/wget to be invoked"


def test_the_bootstrapped_npm_is_a_single_command_that_runs(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    printed = _stdout_lines(sandbox.run("--print-npm"))[-1]
    probe = subprocess.run(
        [printed, "--version"],
        capture_output=True,
        text=True,
        env=sandbox.env(),
        timeout=30,
    )

    assert probe.returncode == 0, probe.stderr


def test_print_npm_does_not_start_the_daemon(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    result = sandbox.run("--print-npm")

    assert DAEMON_MARKER not in result.stdout


def test_the_bootstrapped_npm_runs_the_front_build(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    result = sandbox.run(timeout=90)

    assert result.returncode == 0, result.stderr
    log = sandbox.node_log.read_text() if sandbox.node_log.exists() else ""
    assert "install" in log, log
    assert "run build" in log, log


def test_the_daemon_still_comes_up_after_a_bootstrapped_build(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    result = sandbox.run(timeout=90)

    assert DAEMON_MARKER in result.stdout, result.stderr


# ---------------------------------------------------------------------------
# 3. The bootstrap is cached.
# ---------------------------------------------------------------------------


def test_the_bootstrap_fetches_the_pinned_npm_version(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)

    sandbox.run("--print-npm")

    asked = re.findall(r"npm-(\d+\.\d+\.\d+)\.tgz", sandbox.download_log.read_text())
    assert asked, sandbox.download_log.read_text()
    assert set(asked) == {NPM_VERSION}, (
        f"the bootstrap must fetch npm {NPM_VERSION}, the newest npm that runs "
        f"on node 18 (npm 11 requires ^20.17.0 || >=22.9.0); an unpinned "
        f"npm@latest resolves to an npm this node refuses to execute"
    )


def test_a_warm_bootstrap_cache_is_reused_without_downloading_again(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)
    first = _stdout_lines(sandbox.run("--print-npm"))[-1]
    sandbox.with_downloader(working=False)  # network is gone now

    result = sandbox.run("--print-npm")

    assert result.returncode == 0, result.stderr
    assert _stdout_lines(result)[-1] == first


def test_a_cache_left_by_another_npm_version_is_not_taken_as_warm(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=True)
    sandbox.run("--print-npm")  # warm cache for the pinned version
    _rewrite_version(sandbox.bootstrap_dir, NPM_VERSION, OTHER_NPM_VERSION)
    sandbox.download_log.unlink()

    result = sandbox.run("--print-npm")

    assert result.returncode == 0, result.stderr
    assert sandbox.download_log.exists(), (
        "a cache built for another npm version was served as if it were the "
        f"pinned {NPM_VERSION}; the version must be part of what makes the "
        "cache warm, so bumping the pin invalidates the cache by itself"
    )


# ---------------------------------------------------------------------------
# 4. Failure modes stay loud, clear, and non-fatal when a dist exists.
# ---------------------------------------------------------------------------


def test_a_missing_node_is_named_as_the_reason_the_bootstrap_cannot_run(
    sandbox: Sandbox,
):
    sandbox.with_downloader(working=True)  # no node stub at all

    result = sandbox.run("--print-npm")

    assert result.returncode != 0
    assert _mentions(result.stderr, "node"), result.stderr


def test_a_failed_download_is_reported_as_a_download_failure(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=False)

    result = sandbox.run("--print-npm")

    assert result.returncode != 0
    assert _mentions(
        result.stderr, "baix", "download", "rede", "network"
    ), result.stderr


def test_a_failed_download_leaves_no_broken_npm_behind(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=False)
    sandbox.run("--print-npm")
    sandbox.with_downloader(working=True)  # network is back

    result = sandbox.run("--print-npm")

    assert result.returncode == 0, result.stderr


def test_an_existing_dist_is_still_served_when_the_bootstrap_fails(sandbox: Sandbox):
    sandbox.with_node()
    sandbox.with_downloader(working=False)
    sandbox.with_dist()

    result = sandbox.run(timeout=90)

    assert result.returncode == 0, result.stderr
    assert DAEMON_MARKER in result.stdout


def test_a_bootstrap_failure_is_warned_about_even_when_a_dist_is_served(
    sandbox: Sandbox,
):
    sandbox.with_node()
    sandbox.with_downloader(working=False)
    sandbox.with_dist()

    result = sandbox.run(timeout=90)

    assert _mentions(result.stderr, "npm"), (
        "a silent fallback to a stale dist is how a front-end change looks like "
        "it did nothing; say npm could not be bootstrapped"
    )


def test_a_missing_npm_without_any_dist_fails_fast_instead_of_hanging(
    sandbox: Sandbox,
):
    sandbox.with_node()
    sandbox.with_downloader(working=False)

    result = sandbox.run(timeout=90)

    assert result.returncode != 0
    assert DAEMON_MARKER not in result.stdout


# ---------------------------------------------------------------------------
# 5. The front-end install verb: `npm ci` over a lockfile, `npm install` else.
#
# `npm install` rewrites web/package-lock.json on every machine here (no
# available npm writes the lock's npm-11 `libc` fields), so a plain `./start.sh`
# dirties a tracked file. `npm ci` installs *from* the lock and never writes it.
# ---------------------------------------------------------------------------


def test_a_lockfile_makes_the_front_install_use_npm_ci(sandbox: Sandbox):
    sandbox.with_npm_on_path()
    sandbox.with_lockfile()

    result = sandbox.run(timeout=90)

    assert result.returncode == 0, result.stderr
    assert "ci" in sandbox.npm_verbs(), (
        "with a lockfile present the install must be `npm ci`: `npm install` "
        f"rewrites web/package-lock.json. npm saw {sandbox.npm_calls()}"
    )


def test_a_lockfile_install_never_falls_through_to_npm_install(sandbox: Sandbox):
    sandbox.with_npm_on_path()
    sandbox.with_lockfile()

    sandbox.run(timeout=90)

    assert "install" not in sandbox.npm_verbs(), (
        "a successful `npm ci` must be the whole install; a following "
        f"`npm install` would dirty the lock anyway. npm saw {sandbox.npm_calls()}"
    )


def test_a_failed_npm_ci_falls_back_to_npm_install(sandbox: Sandbox):
    sandbox.with_npm_on_path(ci_exit=1)
    sandbox.with_lockfile()

    sandbox.run(timeout=90)

    assert sandbox.npm_verbs()[:2] == ["ci", "install"], (
        "a lock out of sync with package.json makes `npm ci` abort; that is "
        f"exactly when `npm install` is right. npm saw {sandbox.npm_calls()}"
    )


def test_a_failed_npm_ci_does_not_abort_the_build(sandbox: Sandbox):
    sandbox.with_npm_on_path(ci_exit=1)
    sandbox.with_lockfile()

    result = sandbox.run(timeout=90)

    assert "ci" in sandbox.npm_verbs(), sandbox.npm_calls()  # the ci did happen
    assert result.returncode == 0, result.stderr
    assert sandbox.npm_verbs()[-1] == "run", sandbox.npm_calls()
    assert DAEMON_MARKER in result.stdout, result.stderr


def test_without_a_lockfile_the_front_installs_with_npm_install(sandbox: Sandbox):
    sandbox.with_npm_on_path()  # no web/package-lock.json

    result = sandbox.run(timeout=90)

    assert result.returncode == 0, result.stderr
    assert "install" in sandbox.npm_verbs(), sandbox.npm_calls()


def test_without_a_lockfile_no_npm_ci_is_attempted(sandbox: Sandbox):
    sandbox.with_npm_on_path()

    sandbox.run(timeout=90)

    assert "ci" not in sandbox.npm_verbs(), (
        "`npm ci` without a lockfile only produces a confusing error; the "
        f"lockfile is what selects it. npm saw {sandbox.npm_calls()}"
    )


@pytest.mark.parametrize("locked", [True, False], ids=["with-lock", "without-lock"])
def test_the_build_runs_after_the_install_in_either_path(sandbox: Sandbox, locked: bool):
    sandbox.with_npm_on_path()
    if locked:
        sandbox.with_lockfile()

    sandbox.run(timeout=90)

    verbs = sandbox.npm_verbs()
    assert verbs, "npm was never invoked"
    assert verbs[-1] == "run", f"the build must be last: {sandbox.npm_calls()}"
    assert sandbox.npm_calls()[-1].split()[:2] == ["run", "build"], sandbox.npm_calls()


def test_an_existing_node_modules_does_not_skip_the_install_on_rebuild(
    sandbox: Sandbox,
):
    sandbox.with_npm_on_path()
    sandbox.with_lockfile()
    sandbox.with_node_modules()

    sandbox.run("--rebuild", timeout=90)

    assert "ci" in sandbox.npm_verbs(), (
        "--rebuild must mean reinstall AND rebuild; skipping the install "
        "because node_modules exists rebuilds against whatever tree happens "
        f"to be on disk. npm saw {sandbox.npm_calls()}"
    )


def test_an_existing_node_modules_does_not_skip_the_install_without_a_lock(
    sandbox: Sandbox,
):
    sandbox.with_npm_on_path()
    sandbox.with_node_modules()

    sandbox.run("--rebuild", timeout=90)

    assert "install" in sandbox.npm_verbs(), sandbox.npm_calls()


# ---------------------------------------------------------------------------
# 6. --dev installs through the same door as prod.
#
# The `ci`-over-lock rule above lives in front_install(), which only build_front()
# calls -- and the --dev branch does not call build_front(). It has its own
# `[[ -d web/node_modules ]] || npm install`, which is the pre-fix line verbatim:
# on a tree with no node_modules, `./start.sh --dev` rewrites the lockfile.
#
# --dev ends in `npm run dev`, never `npm run build`: Vite serves from source.
# ---------------------------------------------------------------------------


def test_dev_installs_the_front_with_npm_ci_when_a_lockfile_exists(sandbox: Sandbox):
    sandbox.with_npm_on_path()
    sandbox.with_lockfile()

    result = sandbox.run("--dev", timeout=90)

    assert result.returncode == 0, result.stderr
    assert "ci" in sandbox.npm_verbs(), (
        "--dev must install through the same front_install() as prod: with a "
        "lockfile that is `npm ci`, because `npm install` rewrites "
        f"web/package-lock.json. npm saw {sandbox.npm_calls()}"
    )


def test_dev_does_not_follow_a_successful_ci_with_an_npm_install(sandbox: Sandbox):
    sandbox.with_npm_on_path()
    sandbox.with_lockfile()

    sandbox.run("--dev", timeout=90)

    assert "install" not in sandbox.npm_verbs(), (
        "a successful `npm ci` is the whole install; a trailing `npm install` "
        f"dirties the lock anyway. npm saw {sandbox.npm_calls()}"
    )


def test_dev_falls_back_to_npm_install_when_the_ci_fails(sandbox: Sandbox):
    sandbox.with_npm_on_path(ci_exit=1)
    sandbox.with_lockfile()

    sandbox.run("--dev", timeout=90)

    assert sandbox.npm_verbs()[:2] == ["ci", "install"], (
        "a lock out of sync with package.json aborts `npm ci`; --dev must "
        "recover exactly like prod does instead of leaving the developer with "
        f"no node_modules. npm saw {sandbox.npm_calls()}"
    )


def test_a_failed_ci_in_dev_still_reaches_the_vite_dev_server(sandbox: Sandbox):
    sandbox.with_npm_on_path(ci_exit=1)
    sandbox.with_lockfile()

    result = sandbox.run("--dev", timeout=90)

    assert "ci" in sandbox.npm_verbs(), sandbox.npm_calls()  # the ci did happen
    assert result.returncode == 0, result.stderr
    assert sandbox.npm_calls()[-1].split()[:2] == ["run", "dev"], sandbox.npm_calls()
    assert DAEMON_MARKER in result.stdout, result.stderr


def test_dev_without_a_lockfile_installs_with_npm_install(sandbox: Sandbox):
    sandbox.with_npm_on_path()  # no web/package-lock.json

    result = sandbox.run("--dev", timeout=90)

    assert result.returncode == 0, result.stderr
    assert "install" in sandbox.npm_verbs(), sandbox.npm_calls()


def test_dev_without_a_lockfile_attempts_no_npm_ci(sandbox: Sandbox):
    sandbox.with_npm_on_path()

    sandbox.run("--dev", timeout=90)

    assert "ci" not in sandbox.npm_verbs(), (
        "`npm ci` without a lock only produces a confusing error; the lockfile "
        f"is what selects the verb. npm saw {sandbox.npm_calls()}"
    )


def test_dev_reuses_an_existing_node_modules_instead_of_reinstalling(sandbox: Sandbox):
    """--dev keeps the guard prod dropped: `npm ci` wipes and refetches the whole
    tree, and this is the command you restart a dozen times an hour."""
    sandbox.with_npm_on_path()
    sandbox.with_lockfile()
    sandbox.with_node_modules()

    sandbox.run("--dev", timeout=90)

    assert sandbox.npm_verbs() == ["run"], (
        "a warm web/node_modules must be reused in --dev: reinstalling from "
        "the lock on every hot-reload restart costs tens of seconds for a tree "
        f"that was already correct. npm saw {sandbox.npm_calls()}"
    )


def test_dev_with_rebuild_reinstalls_even_over_a_warm_node_modules(sandbox: Sandbox):
    """`--rebuild` keeps one meaning in both modes: reinstall, don't trust the tree."""
    sandbox.with_npm_on_path()
    sandbox.with_lockfile()
    sandbox.with_node_modules()

    sandbox.run("--dev", "--rebuild", timeout=90)

    assert "ci" in sandbox.npm_verbs(), (
        "--rebuild is the documented way to force a reinstall; if --dev ignores "
        "it there is no way to repair a broken node_modules short of `rm -rf`. "
        f"npm saw {sandbox.npm_calls()}"
    )


def test_dev_ends_in_the_vite_dev_server_and_never_runs_the_build(sandbox: Sandbox):
    sandbox.with_npm_on_path()
    sandbox.with_lockfile()

    result = sandbox.run("--dev", timeout=90)

    assert result.returncode == 0, result.stderr
    assert sandbox.npm_calls()[-1].split()[:2] == ["run", "dev"], sandbox.npm_calls()
    assert not any(
        call.split()[:2] == ["run", "build"] for call in sandbox.npm_calls()
    ), f"--dev serves from source; a build here is dead work. {sandbox.npm_calls()}"


# ---------------------------------------------------------------------------
# 7. The cache is a build artifact, not source.
# ---------------------------------------------------------------------------


def test_the_bootstrap_cache_directory_is_gitignored():
    ignored = (REPO_ROOT / ".gitignore").read_text().split()

    assert f"{BOOTSTRAP_DIRNAME}/" in ignored or BOOTSTRAP_DIRNAME in ignored
