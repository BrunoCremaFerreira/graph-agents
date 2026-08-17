"""Contract tests (RED) for rhizome_graph.assets: where the built page lives.

Motivation: `daemon/server.py` finds the front-end with two module constants::

    REPO_ROOT = Path(__file__).resolve().parent.parent
    WEB_DIST = REPO_ROOT / "web" / "dist"

That is the *checkout's* layout, expressed as "two directories above this
source file". It is right exactly once -- when the daemon is run from a clone --
and wrong for every installed form: a wheel in `site-packages`, a distribution
package under `/usr/lib`, a virtualenv anywhere. Nothing raises when it is
wrong. `run()` reads `WEB_DIST.is_dir()`, finds nothing, logs one INFO line
about letting the Vite dev server host the front, and serves the WebSocket
alone. The user gets a blank page from a daemon that reports itself healthy,
which is the same class of silent failure as a graph updating with nobody on
camera.

So the search becomes data. `web_dist_candidates` says *where to look and in
which order*; `find_web_dist` says *which of those is actually there*. They are
split because they fail differently and are tested differently: the order is a
policy decision with no filesystem in it at all, and the existence check is a
filesystem question with no policy in it. Fused into one function, neither can
be examined without faking the other.

The order is itself the specification, so it is pinned literally:

  1. `$RHIZOME_WEB_DIST` -- the escape hatch. A packager whose layout nobody
     anticipated, or a developer serving a `dist` from elsewhere, must be able
     to say so without patching Python. First, because an override that loses to
     anything is not an override.
  2. `package_root / "web"` -- the installed wheel, where the built assets ship
     beside the Python package.
  3. `/usr/lib/rhizome-graph/web` -- the system package: absolute, and belonging
     to neither root, which is why it is asserted independently of both.
  4. `repo_root / "web" / "dist"` -- today's behaviour, kept and demoted. A
     checkout is where the assets are freshest but it is the least likely to be
     what an installed `rhi` is looking at, so it sorts last.

Purity is part of the contract, not decoration: this module is imported to
decide a path before any server exists, and `asyncio`, `websockets` or
`watchdog` reaching into it would make the answer untestable without the daemon
they drag in. It is asserted over the source text rather than by importing,
because an import-time check cannot tell a dependency of *this* module from one
another test already loaded.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

import ast
from pathlib import Path

from rhizome_graph.assets import find_web_dist, web_dist_candidates

#: The distribution's own directory. Not derived from anything the caller
#: passes, so it is spelled here as the specification spells it.
SYSTEM_WEB_DIR = Path("/usr/lib/rhizome-graph/web")

#: Modules that must never appear in an import statement of `assets.py`.
FORBIDDEN_IMPORTS = ("daemon", "websockets", "watchdog", "asyncio")


# --- 1. web_dist_candidates: the order, with no disk involved ---------------


def test_the_search_order_is_override_then_package_then_system_then_checkout(
    tmp_path: Path,
) -> None:
    """The whole policy, in one assertion, because the order IS the feature."""
    package_root = tmp_path / "site-packages" / "rhizome_graph"
    repo_root = tmp_path / "checkout"

    candidates = web_dist_candidates(
        {"RHIZOME_WEB_DIST": str(tmp_path / "elsewhere")},
        package_root,
        repo_root,
    )

    assert list(candidates) == [
        tmp_path / "elsewhere",
        package_root / "web",
        SYSTEM_WEB_DIR,
        repo_root / "web" / "dist",
    ]


def test_an_unset_override_contributes_no_candidate(tmp_path: Path) -> None:
    """The ordinary case: three places, and none of them is the empty path."""
    package_root = tmp_path / "pkg"
    repo_root = tmp_path / "checkout"

    candidates = web_dist_candidates({}, package_root, repo_root)

    assert list(candidates) == [
        package_root / "web",
        SYSTEM_WEB_DIR,
        repo_root / "web" / "dist",
    ]


def test_an_empty_override_contributes_no_candidate(tmp_path: Path) -> None:
    """`RHIZOME_WEB_DIST=` exports the empty string, and `Path("")` is `.`.

    Kept as its own test because the mistake is specific: an unset variable and
    one set to nothing arrive as different values and must produce the same
    answer. Passed through, the empty string becomes the current working
    directory, so the daemon would serve whatever the user happened to `cd` into.
    """
    package_root = tmp_path / "pkg"
    repo_root = tmp_path / "checkout"

    candidates = web_dist_candidates({"RHIZOME_WEB_DIST": ""}, package_root, repo_root)

    assert list(candidates) == [
        package_root / "web",
        SYSTEM_WEB_DIR,
        repo_root / "web" / "dist",
    ]


def test_the_system_directory_belongs_to_neither_root(tmp_path: Path) -> None:
    """It is the distribution's path, so no argument may be able to move it."""
    candidates = web_dist_candidates({}, tmp_path / "pkg", tmp_path / "checkout")

    assert SYSTEM_WEB_DIR in candidates


# --- 2. find_web_dist: which of them is actually there ----------------------


def test_the_first_existing_directory_wins(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    assert find_web_dist([first, second]) == first


def test_a_candidate_that_does_not_exist_is_passed_over(tmp_path: Path) -> None:
    """Being listed first is not being installed first."""
    present = tmp_path / "present"
    present.mkdir()

    assert find_web_dist([tmp_path / "absent", present]) == present


def test_a_candidate_that_is_a_file_is_not_a_web_dist(tmp_path: Path) -> None:
    """`is_dir`, not `exists`: a leftover file there is not a site to serve."""
    decoy = tmp_path / "decoy"
    decoy.write_text("not a directory\n", encoding="utf-8")
    present = tmp_path / "present"
    present.mkdir()

    assert find_web_dist([decoy, present]) == present


def test_nothing_installed_answers_none(tmp_path: Path) -> None:
    """`None` is the documented "serve the WebSocket alone" state, not an error."""
    assert find_web_dist([tmp_path / "absent", tmp_path / "also-absent"]) is None


def test_no_candidates_at_all_answers_none() -> None:
    assert find_web_dist([]) is None


# --- 3. purity --------------------------------------------------------------


def test_the_module_pulls_in_nothing_from_the_daemon_side() -> None:
    """A path decision must be answerable without a server or an event loop."""
    import rhizome_graph.assets as assets

    source = Path(assets.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    offences = sorted(imported & set(FORBIDDEN_IMPORTS))

    assert offences == [], f"rhizome_graph/assets.py must stay pure; it imports {offences}"
