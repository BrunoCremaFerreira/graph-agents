"""What a wheel and an sdist actually contain, which today is no front end at all.

Motivation, and it is the failure `rhizome_graph/assets.py` exists to diagnose
rather than a new one. `web/dist/` is in `.gitignore` -- it is 9.4 MB of build
output and it belongs there -- and setuptools builds an sdist from the tree it is
handed, so nothing that has ever been produced from this repository carries the
built page. `pip install rhizome-graph` therefore installs a daemon that starts,
binds its port, reports itself healthy, serves the WebSocket, and shows a blank
page: `default_web_dist()` walks its candidate list, matches none of them, and
`None` is the documented "serve the WebSocket alone" state. Nothing errors. The
same hole empties the `.deb` and the Homebrew formula, since both are built from
the same source.

**The path is not ours to choose.** `assets.py` already fixed it, and this test
asserts against that resolver rather than against a path invented here:

    web_dist_candidates() -> [$RHIZOME_WEB_DIST]        (an override, absent here)
                             package_root / "web"       <-- an installed wheel
                             /usr/lib/rhizome-graph/web <-- the .deb
                             repo_root / "web" / "dist" <-- a checkout

`package_root` is `rhizome_graph/`, so a wheel has exactly one place to put the
page: `rhizome_graph/web/`. Shipping it anywhere else -- `share/rhizome-graph/`,
a top-level `web/`, `data_files` -- installs bytes the daemon never looks at, and
looks identical from the outside to not shipping them.

**How the build is run, and why from a copy.** `python -m build` with no target
selected builds the sdist first and then builds the wheel *from the extracted
sdist*, which is the property worth having: a wheel that carries the page proves
the sdist carried enough to produce it. It runs over a copy of the checkout in a
temporary directory, so that no build artefact -- `build/`, `*.egg-info`, `dist/`
-- can land in the repository, which is a standing rule for this suite.

**Slow, so opt-in.** This repository has no marker convention for slow tests (no
`pytest.ini` markers are registered and `addopts` is `-ra`), and registering one
means editing `pyproject.toml`, which is not a test file. The convention chosen
here, and reused by `tests/test_deb_package.py`, is an environment variable:
export `RHIZOME_PACKAGE_TESTS=1` to run them. Measured cost on this host: about
5 s for the build itself, plus the copy of `web/dist`.

**What this does not verify, and cannot from here.**

  * That `pip install` of the produced wheel into a clean environment yields a
    working `rhi` on `$PATH` serving that page. The suite runs from a checkout
    (`pythonpath = ["."]`), and `tests/rhi_process.py` documents the same gap for
    the console script: everything past the shim -- its shebang, the environment
    `pip` bakes into it -- is out of reach.
  * That the built page *works* in a browser. It is compared here as a set of
    file names, never rendered; this host has no Chrome.
  * Anything about PyPI: no upload, no name availability, no metadata rendering.
  * Whether `web/dist` in the checkout is current. The build copies whatever is
    there, so a stale front end ships silently -- which is the same hazard
    `start.sh` has when `node` is missing.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The opt-in switch for every test in this file and in `test_deb_package.py`.
PACKAGE_TESTS_ENV = "RHIZOME_PACKAGE_TESTS"

#: The built front end as it sits in a checkout, and the only input there is.
CHECKOUT_WEB_DIST = REPO_ROOT / "web" / "dist"

#: Where the resolver looks inside an installed wheel: `package_root / "web"`,
#: with `package_root` being the `rhizome_graph` package directory.
WHEEL_WEB_PREFIX = "rhizome_graph/web/"

#: Trees that must not be copied into the scratch build: too large, irrelevant,
#: or themselves build output. `dist` is deliberately NOT here -- `web/dist` is
#: the very thing under test.
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
)

#: `assets/index-B7Kg6nIH.js` and friends, as written in the built page.
ASSET_REFERENCE = re.compile(r"""["'(/]?(assets/[A-Za-z0-9._-]+)""")


def _require_opt_in() -> None:
    if os.environ.get(PACKAGE_TESTS_ENV, "") != "1":
        pytest.skip(
            f"packaging tests are opt-in and slow; run with {PACKAGE_TESTS_ENV}=1"
        )


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """`(wheel, sdist)` built once, from a copy of the checkout, in a temp tree."""
    _require_opt_in()
    if not CHECKOUT_WEB_DIST.is_dir():
        pytest.skip("web/dist is not built here; there is no front end to package")
    try:
        import build  # noqa: F401
    except ImportError:  # pragma: no cover - depends on the machine
        pytest.skip("`build` is not installed; cannot produce a wheel or an sdist")

    base = tmp_path_factory.mktemp("distribution")
    source = base / "source"
    outdir = base / "dist"
    shutil.copytree(REPO_ROOT, source, ignore=COPY_IGNORE, symlinks=True)

    completed = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir), str(source)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, (
        "`python -m build` failed:\n" + completed.stdout[-4000:] + completed.stderr[-4000:]
    )

    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    assert len(sdists) == 1, f"expected one sdist, got {sdists}"
    return wheels[0], sdists[0]


def _wheel_names(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def _wheel_text(wheel: Path, name: str) -> str:
    with zipfile.ZipFile(wheel) as archive:
        return archive.read(name).decode("utf-8")


def _sdist_names(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as archive:
        return archive.getnames()


def _checkout_front_end_files() -> list[str]:
    """Every built file in the checkout, relative to `web/dist`, maps excluded.

    Source maps are dropped from the comparison on purpose: they are a debugging
    convenience, they are a large share of those 9.4 MB, and a packager who
    leaves them out has made a defensible choice. Everything else is load-bearing
    at runtime -- the 22 grammar chunks are fetched lazily by `highlight.ts` when
    a file is first opened, so a package missing them serves a page that works
    until somebody clicks a file and then fails with a 404 nobody sees.
    """
    return sorted(
        str(path.relative_to(CHECKOUT_WEB_DIST))
        for path in CHECKOUT_WEB_DIST.rglob("*")
        if path.is_file() and path.suffix != ".map"
    )


def test_the_checkout_holds_a_built_front_end_to_package() -> None:
    """A guard, so that the packaging assertions below cannot be vacuous.

    Green on arrival. It exists because every other test in this file compares a
    distributable against `web/dist`: if that directory were empty, they could
    all pass while shipping nothing. It skips rather than fails when the front
    end has never been built, since `node` is optional for the Python suite.
    """
    if not CHECKOUT_WEB_DIST.is_dir():
        pytest.skip("web/dist is not built here")
    files = _checkout_front_end_files()

    assert "index.html" in files, f"no page in {CHECKOUT_WEB_DIST}"
    assert any(
        name.startswith("assets/") and name.endswith(".js") for name in files
    ), "no hashed script in the built front end"


def test_the_wheel_carries_the_page_where_the_asset_resolver_looks(
    built: tuple[Path, Path],
) -> None:
    """`rhizome_graph/web/index.html`, because that is the candidate a wheel hits.

    Not `web/dist/`, not `share/`: `web_dist_candidates()` offers an installed
    package exactly one path, `package_root / "web"`. A wheel that carries the
    page anywhere else installs bytes the daemon never opens.
    """
    wheel, _ = built
    names = _wheel_names(wheel)

    assert f"{WHEEL_WEB_PREFIX}index.html" in names, (
        "the wheel carries no front end at the path `assets.py` resolves; "
        f"entries under rhizome_graph/: "
        f"{sorted(n for n in names if n.startswith('rhizome_graph/'))[:20]}"
    )


def test_the_wheel_carries_every_asset_its_own_page_asks_for(
    built: tuple[Path, Path],
) -> None:
    """The page and its hashed entry chunks travel together or not at all.

    Read out of the wheel's own `index.html` rather than out of the checkout's,
    so that the test still means something after a rebuild changes every hash: it
    asks whether *this* package is internally complete, which is the property a
    user experiences as "the page loaded".
    """
    wheel, _ = built
    names = set(_wheel_names(wheel))
    page_name = f"{WHEEL_WEB_PREFIX}index.html"

    assert page_name in names, f"the wheel carries no {page_name} to read"
    page = _wheel_text(wheel, page_name)
    referenced = sorted(set(ASSET_REFERENCE.findall(page)))

    assert referenced, "the packaged page references no hashed asset at all"
    missing = [ref for ref in referenced if f"{WHEEL_WEB_PREFIX}{ref}" not in names]
    assert missing == [], (
        f"the packaged page loads assets the package does not contain: {missing}"
    )


def test_the_wheel_carries_the_lazily_loaded_chunks_too(
    built: tuple[Path, Path],
) -> None:
    """Not just the entry point: the grammar chunks are fetched on first click.

    `highlight.ts` is reached by `await import("./highlight")` and each of the 22
    grammars is its own chunk, so none of them is named in `index.html`. A
    package rule that copied only what the page references would pass the test
    above and still 404 the moment a user opens a file.
    """
    wheel, _ = built
    names = set(_wheel_names(wheel))
    expected = _checkout_front_end_files()

    missing = [name for name in expected if f"{WHEEL_WEB_PREFIX}{name}" not in names]
    assert missing == [], (
        f"{len(missing)} of {len(expected)} built files are absent from the "
        f"wheel, e.g. {missing[:10]}"
    )


def test_the_sdist_carries_the_front_end(built: tuple[Path, Path]) -> None:
    """A source release must be enough to build a package that serves something.

    Deliberately looser than the wheel assertions about *where*: an sdist may
    legitimately keep the build output under `web/dist/` and have the wheel step
    move it. What may not vary is that it is in there, because `python -m build`
    builds the wheel from this very archive -- so a wheel that is complete and an
    sdist that is empty cannot both be true.
    """
    _, sdist = built
    names = _sdist_names(sdist)

    pages = [name for name in names if name.endswith("/index.html")]
    hashed = [
        name for name in names if "/assets/" in name and name.endswith((".js", ".css"))
    ]

    assert pages, f"no index.html anywhere in the sdist; it holds {len(names)} entries"
    assert hashed, "the sdist carries a page but none of its hashed assets"


def test_the_wheel_declares_both_console_scripts(built: tuple[Path, Path]) -> None:
    """`rhi` and `rhi-hook`, in the metadata that actually creates them.

    Green on arrival -- `pyproject.toml` already declares both, and
    `tests/test_cli_entry_point.py` pins the strings there. What is new is the
    level: this reads `entry_points.txt` out of the built artefact, which is the
    file `pip` consumes. A packaging change that stops shipping the metadata (a
    hand-rolled backend, a `packages` list that drops `rhizome_graph`) leaves the
    declaration in `pyproject.toml` correct and the installed command absent.
    """
    wheel, _ = built
    names = _wheel_names(wheel)

    entry_points = [name for name in names if name.endswith("/entry_points.txt")]
    assert len(entry_points) == 1, f"expected one entry_points.txt, got {entry_points}"
    text = _wheel_text(wheel, entry_points[0])

    assert "rhi = rhizome_graph.cli:main" in text, text
    assert "rhi-hook = rhizome_graph.hook:main" in text, text
