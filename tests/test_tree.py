"""Contract tests (RED) for graphagents.tree.

`scan_tree` is what lets the graph start as a *tree* instead of a blank field:
the daemon walks the observed project once at boot and seeds every existing file
before a single agent event arrives. Gource shows the whole repository from
frame 1; without this the page shows only the two or three files an agent
happened to touch.

Style: Arrange-Act-Assert, one failure reason per test.
"""

from __future__ import annotations

import os
from pathlib import Path

from graphagents.tree import is_ignored, scan_tree


def _touch(root: Path, rel: str) -> None:
    """Create an empty file at `rel` under `root`, with its parent dirs."""
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")


# --- 1. Basic walk ----------------------------------------------------------

def test_returns_paths_relative_to_root(tmp_path: Path):
    _touch(tmp_path, "src/app.py")
    _touch(tmp_path, "README.md")

    paths = scan_tree(str(tmp_path))

    assert set(paths) == {"src/app.py", "README.md"}


def test_result_is_sorted_for_a_stable_seed_order(tmp_path: Path):
    _touch(tmp_path, "z.py")
    _touch(tmp_path, "a.py")
    _touch(tmp_path, "m/inner.py")

    paths = scan_tree(str(tmp_path))

    assert paths == sorted(paths)


def test_empty_project_yields_no_paths(tmp_path: Path):
    assert scan_tree(str(tmp_path)) == []


# --- 2. Noise the graph must never show ------------------------------------

def test_skips_vcs_and_build_directories(tmp_path: Path):
    _touch(tmp_path, "src/app.py")
    for noisy in (
        ".git/config",
        "node_modules/three/index.js",
        "__pycache__/app.cpython-312.pyc",
        ".venv/bin/python",
        "dist/bundle.js",
        ".pytest_cache/CACHEDIR.TAG",
    ):
        _touch(tmp_path, noisy)

    paths = scan_tree(str(tmp_path))

    assert paths == ["src/app.py"]


def test_skips_packaging_metadata_directories(tmp_path: Path):
    _touch(tmp_path, "graphagents/__init__.py")
    _touch(tmp_path, "graphagents.egg-info/PKG-INFO")

    assert scan_tree(str(tmp_path)) == ["graphagents/__init__.py"]


def test_is_ignored_matches_any_segment_of_the_path():
    assert is_ignored("node_modules/three/build/three.js")
    assert is_ignored(".git/HEAD")
    assert is_ignored("web/node_modules/vite/bin.js")
    assert not is_ignored("src/app.py")
    assert not is_ignored("web/src/renderer.ts")


# --- 3. Guard rails: never raise, never flood ------------------------------

def test_missing_root_returns_empty_instead_of_raising(tmp_path: Path):
    assert scan_tree(str(tmp_path / "does-not-exist")) == []


def test_respects_max_files_cap(tmp_path: Path):
    for i in range(20):
        _touch(tmp_path, f"f{i:02d}.txt")

    paths = scan_tree(str(tmp_path), max_files=5)

    assert len(paths) == 5


def test_symlinked_directories_are_not_followed(tmp_path: Path):
    _touch(tmp_path, "real/app.py")
    os.symlink(tmp_path / "real", tmp_path / "link", target_is_directory=True)

    paths = scan_tree(str(tmp_path))

    assert paths == ["real/app.py"]
