"""The two policy walks judged "is this generated?" against the ABSOLUTE path.

Motivation, and it is a real defect that was live in this repository rather than
a hypothetical one. `test_language_policy._scanned_files()` and
`test_project_naming._authored_files()` both dropped a file with

    if any(part in GENERATED_DIRS for part in path.parts):

where `path` is absolute and `GENERATED_DIRS` holds `tmp`, `dist`, `files` and
`node_modules`. Those are perfectly ordinary names for a directory *above* a
checkout, so a repository living anywhere under one -- `/tmp/build/checkout`,
which is where a CI clone and every `git worktree` in this project's own test
instructions land -- matched on every single file and discarded the lot. What
survived was the four root-level entries of `SCANNED_FILES`, appended by a
separate loop that carries no such filter. The Python package, the daemon, the
hooks, `web/src`, `config`, `.claude`, `debian`, `Formula` and `packaging` were
all silently unread, and rule 4 of CLAUDE.md became a green test over four
files. Silent, because the walk finding nothing looks exactly like the walk
finding nothing wrong.

Both scans take a `root` parameter, defaulting to `REPO_ROOT`, purely so this
file can point them at a synthetic tree. The alternative shape -- extracting the
judgement into a pure `_is_generated(relative_path)` helper and testing that --
was rejected because the judgement was never the broken part: given a relative
path it was always right, and the bug was entirely in *which* path the walk
handed it. A test over that helper would have passed throughout. The root
parameter tests the walk end to end, which is the only place the defect lived.

The fixture is deliberately built under a directory literally named `tmp`, and
not merely under `tmp_path` (which is usually below `/tmp` and would reproduce
this by accident): the reproduction has to be a decision the test makes, not a
property of where pytest happens to put a temporary directory.

Style: Arrange-Act-Assert, one property per test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

import test_language_policy as english_policy
import test_project_naming as naming_policy

#: The two walks under test, which share the constant and shared the defect.
#: Both answer a list of absolute paths and both take the root to walk.
Scan = Callable[..., list[Path]]

POLICY_SCANS = (
    pytest.param(english_policy._scanned_files, id="language-policy"),
    pytest.param(naming_policy._authored_files, id="naming-policy"),
)

#: Authored files in a tree both policies walk. Only the directories and root
#: files common to *both* scanners appear here, so one fixture serves both: the
#: two keep their own `SCANNED_DIRS` on purpose (`.claude/agents` against
#: `.claude`, `CLAUDE.md` against `pyproject.toml`) and this file is not the
#: place that pins those differences.
AUTHORED_FILES = (
    "start.sh",
    "run.sh",
    "rhizome_graph/status.py",
    "daemon/server.py",
    "hooks/emit_event.py",
    "web/src/labels.ts",
    "config/settings.json",
    "debian/control",
    "Formula/rhizome-graph.rb",
    "packaging/build-deb.sh",
)

#: Build output and vendored bytes, at the paths they really appear at. The
#: `debian/tmp` entry is the nastiest of the two and the reason `tmp` is in
#: `GENERATED_DIRS` at all: a `dpkg-buildpackage` run stages a *copy* of the
#: authored Python package there, so a scan that read it would report every
#: offence twice and fail on files nobody edits.
GENERATED_FILES = (
    "web/src/node_modules/dep/index.js",
    "debian/tmp/usr/lib/rhizome-graph/rhizome_graph/status.py",
)


def _write(root: Path, relative: str) -> Path:
    """Create `relative` under `root`, parents included, with harmless content."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# authored text\n", encoding="utf-8")
    return path


def _fake_checkout(tmp_path: Path) -> Path:
    """A miniature of this repository, nested under a directory named `tmp`."""
    root = tmp_path / "tmp" / "build" / "checkout"
    for relative in AUTHORED_FILES + GENERATED_FILES:
        _write(root, relative)
    return root


def _relative(root: Path, paths: list[Path]) -> set[str]:
    return {str(path.relative_to(root)) for path in paths}


@pytest.mark.parametrize("scan", POLICY_SCANS)
def test_the_scan_reads_the_authored_tree_of_a_checkout_below_a_generated_name(
    scan: Scan, tmp_path: Path
) -> None:
    """Where the repository sits cannot decide what is inside the repository."""
    root = _fake_checkout(tmp_path)

    scanned = _relative(root, scan(root))

    missing = sorted(set(AUTHORED_FILES) - scanned)
    assert not missing, (
        f"the walk dropped authored files because an ancestor of the checkout is "
        f"named tmp/build/dist/node_modules: {missing}. Scanned: {sorted(scanned)}"
    )


@pytest.mark.parametrize("scan", POLICY_SCANS)
def test_the_scan_skips_build_output_inside_the_checkout(scan: Scan, tmp_path: Path) -> None:
    """A vendored dependency and a staged copy are somebody else's bytes."""
    root = _fake_checkout(tmp_path)

    scanned = _relative(root, scan(root))

    leaked = sorted(set(GENERATED_FILES) & scanned)
    assert not leaked, f"generated files reached the policy scan: {leaked}"


@pytest.mark.parametrize("scan", POLICY_SCANS)
@pytest.mark.parametrize("generated_dir", sorted(english_policy.GENERATED_DIRS))
def test_every_generated_directory_name_still_excludes_what_is_under_it(
    scan: Scan, generated_dir: str, tmp_path: Path
) -> None:
    """Each name in the set earns its place, and a new name is covered for free."""
    root = _fake_checkout(tmp_path)
    leaked = f"rhizome_graph/{generated_dir}/leaked.py"
    _write(root, leaked)

    scanned = _relative(root, scan(root))

    assert leaked not in scanned, (
        f"{generated_dir!r} is in GENERATED_DIRS but no longer excludes its contents"
    )


@pytest.mark.parametrize("scan", POLICY_SCANS)
def test_the_root_defaults_to_this_repository(scan: Scan) -> None:
    """The parameter exists for the tests; every real caller passes nothing."""
    assert scan() == scan(english_policy.REPO_ROOT)
