"""Metadata this project cannot produce on a tree where nobody built the page.

The defect, shipped in `8a166bd`. Stage F taught the wheel to carry the front end
at the one path `assets.web_dist_candidates()` looks at inside an installed
distribution, and it did so by mapping a package onto the build output:

    [tool.setuptools]
    packages = ["rhizome_graph", "daemon", "rhizome_graph.web"]

    [tool.setuptools.package-dir]
    "rhizome_graph.web" = "web/dist"

`package-dir` names a directory that setuptools resolves while it is producing
*metadata* -- inside `egg_info`, before any file is copied anywhere -- and
`web/dist` is gitignored build output, 9.4 MB of it. So on a clean checkout every
build-backend entry point dies the same way:

    error: package directory 'web/dist' does not exist

which takes down `pip install -e '.[daemon]'` (the install command CLAUDE.md
documents), `python -m build`, the Homebrew formula's
`virtualenv_install_with_resources`, and the `.deb`. `start.sh` cannot bootstrap
its way out of it either: it installs the daemon deps first and only builds the
front end afterwards, so the step that fails is always the earlier one.

This is the class of failure the packaging work kept turning up -- something that
reports success in the state the author happens to be in, and fails in the state
a new user is actually in. Here the sign flipped: before stage F an unbuilt tree
produced a package that installed and served a blank page; now it produces no
package at all.

**The property, and it is not a mechanism.** The distribution's metadata must be
obtainable from a tree where the front end has never been built. Whether that is
reached by dropping `package-dir`, by a `MANIFEST.in`, by a directory that always
exists, or by a backend shim is `developer-backend`'s call; nothing here names
`pyproject.toml` in an assertion, and every test drives a real build-backend
invocation over a real copy of the checkout.

**The other jaw of the vice is `tests/test_distribution_front_end.py`**, whose
`test_the_wheel_carries_the_page_where_the_asset_resolver_looks` and
`test_the_wheel_carries_the_lazily_loaded_chunks_too` pin that a *built* tree
still ships `rhizome_graph/web/index.html` and every lazy grammar chunk. That is
what stops the cheapest fix -- deleting the front end from the package -- from
being a fix, so it is not repeated here. What is added here instead is the
symmetric half those tests cannot see, because they skip when `web/dist` is
absent: the built tree must keep yielding metadata too, so that the repair cannot
be a special case that only holds while the page is missing.

**How the backend is invoked, and why this one.** `pip install --editable
. --no-deps --dry-run`, because `pip install -e` is the command that actually
broke and this is that command with the network and the filesystem taken out of
it: pip still prepares an isolated build environment and still calls
`get_requires_for_build_editable` and `prepare_metadata_for_build_editable`, which
is where the error comes from. `--no-deps` keeps `websockets`/`watchdog` out of
it, and `--dry-run` stops before anything is installed. Measured on this host:
2.2 s.

What it therefore does *not* cover: that the produced editable install imports,
that `rhi` lands on `$PATH`, or that a wheel built from an unbuilt tree is
coherent. Calling `setuptools.build_meta` in-process would have been cheaper
still and was rejected -- this repository's `.venv` has no `setuptools` at all
(pip 26 does not vendor one), so such a test would report `ModuleNotFoundError`
and look like a defect in the distribution.

**A failure that is the machine's, not the distribution's.** If pip cannot build
*anything* here -- no cached `setuptools>=61`, no network to fetch one -- the run
fails with a message about the build environment and nothing has been learnt. So
on failure the same command is first run against a synthetic minimal project; if
that fails too, the test skips instead of accusing this repository. The control
runs only on the failing path, so a green run pays for one pip invocation.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rhizome_graph.assets import WEB_DIST_ENV, default_web_dist

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The built front end as it sits in a checkout: present only if somebody ran
#: `npm run build`, and the whole point of this file is that it usually has not
#: been run.
CHECKOUT_WEB_DIST = REPO_ROOT / "web" / "dist"

#: The opt-in switch this suite already uses for slow packaging tests, spelled
#: the same way `tests/test_distribution_front_end.py` spells it.
PACKAGE_TESTS_ENV = "RHIZOME_PACKAGE_TESTS"

#: What is never copied into a scratch build: too big, machine-local, or itself
#: build output. `dist` covers the top-level artefact directory AND `web/dist`,
#: so a copy starts with no front end and each test then arranges the state it
#: means -- reading the test tells you which of the two it is about, instead of
#: inheriting whatever this machine happens to have built.
COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "*.egg-info",
    ".pytest_cache",
    ".npm-bootstrap",
    "build",
    "dist",
)

#: Generous: an isolated build environment may have to be created from scratch.
BUILD_ENVIRONMENT_TIMEOUT_SECONDS = 600


def _copy_checkout(destination: Path) -> Path:
    """The working tree as a fresh clone sees it, in a directory we may dirty.

    A copy, never `REPO_ROOT` itself: a metadata run writes `*.egg-info` beside
    the source, and no test in this suite may leave build output in the
    repository. The working tree rather than `git archive HEAD`, because the fix
    has to be testable before it is committed.
    """
    shutil.copytree(REPO_ROOT, destination, ignore=COPY_IGNORE, symlinks=True)
    return destination


def _unbuilt_checkout(destination: Path) -> Path:
    """A copy in which the front end has never been built."""
    project = _copy_checkout(destination)
    shutil.rmtree(project / "web" / "dist", ignore_errors=True)
    assert not (project / "web" / "dist").exists(), (
        "arrangement failed: this copy still holds a built front end"
    )
    return project


def _built_checkout(destination: Path) -> Path:
    """A copy carrying the checkout's own `web/dist`, page and chunks included."""
    project = _copy_checkout(destination)
    shutil.copytree(CHECKOUT_WEB_DIST, project / "web" / "dist", symlinks=True)
    assert (project / "web" / "dist" / "index.html").is_file(), (
        "arrangement failed: this copy holds no page"
    )
    return project


def _editable_metadata(project: Path) -> subprocess.CompletedProcess[str]:
    """Ask pip for the editable metadata of `project`, installing nothing."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--editable",
            str(project),
            "--no-deps",
            "--dry-run",
        ],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=BUILD_ENVIRONMENT_TIMEOUT_SECONDS,
    )


def _tail(completed: subprocess.CompletedProcess[str], limit: int = 3000) -> str:
    return (completed.stdout + completed.stderr)[-limit:]


def _skip_unless_the_machine_can_build_anything(scratch: Path) -> None:
    """Skip when even a trivial project cannot be prepared here.

    Without this, a host with no cached `setuptools` and no network reports the
    same red as a broken `pyproject.toml`, and the red would be a lie.
    """
    control = scratch / "control"
    (control / "control_package").mkdir(parents=True)
    (control / "control_package" / "__init__.py").write_text("", encoding="utf-8")
    (control / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=61.0"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        'name = "control-package"\n'
        'version = "0.0.0"\n'
        "\n"
        "[tool.setuptools]\n"
        'packages = ["control_package"]\n',
        encoding="utf-8",
    )

    completed = _editable_metadata(control)
    if completed.returncode != 0:
        pytest.skip(
            "pip cannot prepare a build environment on this host, so nothing can "
            f"be concluded about this distribution:\n{_tail(completed)}"
        )


def _require_opt_in() -> None:
    if os.environ.get(PACKAGE_TESTS_ENV, "") != "1":
        pytest.skip(
            f"a full build is slow and opt-in; run with {PACKAGE_TESTS_ENV}=1"
        )


# --- 1. the failing case, driven for real -----------------------------------


def test_editable_metadata_comes_out_of_a_tree_whose_front_end_was_never_built(
    tmp_path: Path,
) -> None:
    """`pip install -e .` on a clean clone, which is how everybody arrives.

    The documented bootstrap. `start.sh` runs this before it runs `npm`, so a
    distribution whose metadata needs the build output can never be installed by
    the script that produces that output.
    """
    project = _unbuilt_checkout(tmp_path / "clone")

    completed = _editable_metadata(project)

    if completed.returncode != 0:
        _skip_unless_the_machine_can_build_anything(tmp_path)
    assert completed.returncode == 0, (
        "the editable metadata of an unbuilt checkout cannot be produced:\n"
        + _tail(completed)
    )


def test_a_source_distribution_builds_from_a_tree_whose_front_end_was_never_built(
    tmp_path: Path,
) -> None:
    """The other entry point, since a release tarball is made the same way.

    `python -m build --sdist` calls `get_requires_for_build_sdist` rather than
    the editable hook above, and the `.deb` and the Homebrew formula both go
    through it. Opt-in: it is a second isolated environment and a real archive.
    """
    _require_opt_in()
    try:
        import build  # noqa: F401
    except ImportError:  # pragma: no cover - depends on the machine
        pytest.skip("`build` is not installed here")
    project = _unbuilt_checkout(tmp_path / "clone")
    outdir = tmp_path / "out"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--outdir",
            str(outdir),
            str(project),
        ],
        capture_output=True,
        text=True,
        timeout=BUILD_ENVIRONMENT_TIMEOUT_SECONDS,
    )

    if completed.returncode != 0:
        _skip_unless_the_machine_can_build_anything(tmp_path)
    assert completed.returncode == 0, (
        "no source distribution can be built from an unbuilt checkout:\n"
        + _tail(completed)
    )
    assert sorted(outdir.glob("*.tar.gz")), f"no archive in {outdir}"


# --- 2. the half that must not regress --------------------------------------


def test_editable_metadata_still_comes_out_of_a_tree_whose_front_end_was_built(
    tmp_path: Path,
) -> None:
    """Green on arrival, and it is the fix's ceiling rather than its floor.

    The repair must not be a special case that holds only while `web/dist` is
    missing -- a `package-dir` swapped for a build-time hook, say, that then
    trips over a directory that does exist. What this does NOT check is that the
    page ends up inside the package: that is
    `tests/test_distribution_front_end.py`, which reads
    `rhizome_graph/web/index.html` out of a real wheel and is the other jaw of
    this vice. Skips when nobody has built the front end here, since `node` is
    optional for the Python suite.
    """
    if not CHECKOUT_WEB_DIST.is_dir():
        pytest.skip("web/dist is not built here; there is no built tree to arrange")
    project = _built_checkout(tmp_path / "clone")

    completed = _editable_metadata(project)

    if completed.returncode != 0:
        _skip_unless_the_machine_can_build_anything(tmp_path)
    assert completed.returncode == 0, (
        "the editable metadata of a built checkout cannot be produced:\n"
        + _tail(completed)
    )


# --- 3. an unbuilt tree must not look built ---------------------------------


def test_a_web_directory_with_no_page_in_it_is_not_a_built_front_end(
    tmp_path: Path,
) -> None:
    """An empty directory is a blank page reporting success, which is the enemy.

    The likely shapes of the repair -- a committed `web/dist/.gitkeep`, an empty
    `rhizome_graph/web` shipped so `package-dir` always resolves -- all end in a
    directory that exists and holds nothing. `find_web_dist` tests candidates
    with `is_dir()` and nothing else, so it would elect that directory, and the
    daemon would serve a static root with no `index.html`: exactly the silent
    blank page `assets.py` exists to prevent, now reachable from a tree that
    merely was not built.

    Asked of `default_web_dist`, which is what `daemon/server.py` calls, and
    through the override because that is the one candidate a test can plant.
    Where the check lives -- `find_web_dist`, a new predicate beside it, or a
    packaging rule that never creates such a directory -- is not pinned here.
    """
    empty = tmp_path / "web"
    empty.mkdir()

    assert default_web_dist({WEB_DIST_ENV: str(empty)}) is None, (
        f"{empty} holds no index.html and was still accepted as a built front end"
    )


def test_a_web_directory_holding_a_page_is_the_built_front_end(tmp_path: Path) -> None:
    """The control, so the test above cannot be satisfied by refusing everything.

    Green on arrival, and it stays the definition of "built": the page the HTTP
    handler appends `index.html` to find.
    """
    built = tmp_path / "web"
    built.mkdir()
    (built / "index.html").write_text("<!doctype html>\n", encoding="utf-8")

    assert default_web_dist({WEB_DIST_ENV: str(built)}) == built
