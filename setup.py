"""The one build-time step `pyproject.toml` cannot express: shipping the page.

Everything declarative lives in `pyproject.toml` and stays there. What is here is
a single `build_py` subclass, because the built front end is a directory of data
that has to arrive inside the import package while *not* existing in the source
tree most of the time:

  * It must land at `rhizome_graph/web/` in the installed distribution. That is
    the only path `rhizome_graph.assets.web_dist_candidates()` offers a wheel or
    a `.deb`, so a page installed anywhere else is bytes the daemon never opens
    -- indistinguishable from shipping no page at all, and silent.
  * It is `web/dist`, gitignored build output produced by `npm run build`, which
    on a fresh clone has never been run. Declaring it as a package with a
    `package-dir` -- the first attempt -- made setuptools resolve that directory
    while producing *metadata*, so every build-backend entry point died with
    `package directory 'web/dist' does not exist` on any clean tree. That took
    down the documented `pip install -e '.[daemon]'`, `python -m build`, the
    Homebrew formula and the `.deb`, and it took down `start.sh`, which installs
    the daemon before it builds the front end and so could never bootstrap out of
    it.

Copying at build time inverts that: an absent `web/dist` is nothing to copy, and
a present one is packaged whole. Nothing is written into the source tree, and no
placeholder directory is created anywhere -- an empty `rhizome_graph/web` would be
elected by the asset search and served as a blank page, which is precisely the
failure `assets.py` exists to prevent (`assets.holds_page` now refuses one, but a
packaging step should not need saving from itself).

`_get_data_files` is setuptools' own private hook for exactly this list, and
using it rather than a bare copy in `run()` keeps the files in
`get_output_mapping()` too, so the build's idea of its outputs stays honest. If a
future setuptools renames it, this fails loudly at build time and
`tests/test_distribution_front_end.py` says which files went missing.

The sdist gets `web/dist` from `MANIFEST.in`, which is a separate path through
setuptools: `python -m build` builds the wheel from the extracted sdist, so the
archive has to carry the raw build output for the copy above to have an input.
"""

from __future__ import annotations

import os
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

HERE = Path(__file__).resolve().parent

#: Where `npm run build` (and `start.sh`, and `packaging/build-deb.sh`) leave the
#: front end. Unchanged by this file, deliberately: moving it would spread into
#: `vite.config.ts`, `.gitignore`, `start.sh`, the `.deb` and the tests.
FRONT_END_BUILD = HERE / "web" / "dist"

#: The package the page travels inside, and its subdirectory there. Together
#: these spell `rhizome_graph/web/`, which is `assets.PACKAGE_ROOT / "web"`.
FRONT_END_PACKAGE = "rhizome_graph"
FRONT_END_SUBDIR = "web"

#: What makes a `web/dist` worth packaging. The same file `assets.holds_page`
#: looks for: a directory with no page in it is not a built front end, and
#: packaging one would ship the blank page rather than nothing.
PAGE_NAME = "index.html"


def front_end_files() -> list[str]:
    """Every built file, relative to `web/dist`, or nothing at all.

    Source maps included: they are what today's package carries, and dropping
    them is a size decision nobody has asked for. The 22 lazily loaded grammar
    chunks are in here too and are not optional -- none of them is named in
    `index.html`, so a rule that copied only what the page references would 404
    the first time somebody opens a file.
    """
    if not (FRONT_END_BUILD / PAGE_NAME).is_file():
        return []
    return sorted(
        str(path.relative_to(FRONT_END_BUILD))
        for path in FRONT_END_BUILD.rglob("*")
        if path.is_file()
    )


class build_py(_build_py):
    """`build_py`, plus the front end as package data of `rhizome_graph`."""

    def _get_data_files(self):
        data_files = super()._get_data_files()
        filenames = front_end_files()
        if not filenames:
            return data_files
        build_dir = os.path.join(self.build_lib, FRONT_END_PACKAGE, FRONT_END_SUBDIR)
        data_files.append(
            (FRONT_END_PACKAGE, str(FRONT_END_BUILD), build_dir, filenames)
        )
        return data_files


setup(cmdclass={"build_py": build_py})
