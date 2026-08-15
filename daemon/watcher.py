"""Filesystem watcher: completeness where the hooks only give authorship.

Hooks see exactly what Claude's file tools report. They miss everything else:
a glob (``cp src/*.md docs/``) reports one destination directory instead of the
files actually copied, a compound command (``cd x && rm y``) parses to nothing,
and a change made outside the agent is invisible. That gap is why a busy session
could produce two dots on screen.

This watcher reports what really happened on disk. It carries no attribution --
:class:`~daemon.server.EventHub` pairs each change with the agent whose hook
fired moments earlier, so hooks stay the source of *who* and the watcher becomes
the source of *what*.

The mapping from a filesystem event to our A/M/D vocabulary is a pure function
(:func:`classify`) so it can be tested without an observer running.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from rhizome_graph.tree import is_ignored

LOGGER = logging.getLogger("rhizome_graph.watcher")

#: (relative_path, op_type) -> None
ChangeSink = Callable[[str, str], None]


def classify(event_type: str, is_directory: bool) -> str | None:
    """Map a watchdog event to ``"A"``/``"M"``/``"D"``, or ``None`` to ignore.

    Directory creations and modifications carry nothing the graph can use: the
    frontend derives directory nodes from their children's paths. A directory
    *deletion* is kept, because the subtree under it has to be pruned.
    """
    if event_type == "deleted":
        return "D"
    if is_directory:
        return None
    if event_type == "created":
        return "A"
    if event_type == "modified":
        return "M"
    return None


def relative_to_root(path: str, root: str) -> str | None:
    """Return `path` relative to `root`, or ``None`` if it must not be shown.

    Rejects anything outside the root, the root itself, and paths inside the
    build/VCS directories that :mod:`rhizome_graph.tree` already filters out of the
    initial snapshot -- otherwise a single ``npm install`` would bury the graph.
    """
    normalized = os.path.normpath(path)
    base = os.path.normpath(root)
    if normalized == base:
        return None
    prefix = base.rstrip("/") + "/"
    if not normalized.startswith(prefix):
        return None
    relative = normalized[len(prefix):]
    if not relative or is_ignored(relative):
        return None
    return relative


class _Handler(FileSystemEventHandler):
    """Translates watchdog callbacks into ``(relative_path, op)`` pairs."""

    def __init__(self, root: str, on_change: ChangeSink) -> None:
        self._root = root
        self._on_change = on_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        try:
            if event.event_type == "moved":
                # A rename is a deletion at the source and an addition at the
                # destination; reporting only one would leave a ghost node.
                self._report(getattr(event, "src_path", ""), "D", event.is_directory)
                self._report(getattr(event, "dest_path", ""), "A", event.is_directory)
                return
            op = classify(event.event_type, event.is_directory)
            if op is not None:
                self._report(event.src_path, op, event.is_directory)
        except Exception as exc:  # a bad event must never kill the observer
            LOGGER.debug("watcher event error: %s", exc)

    def _report(self, path: str, op: str, is_directory: bool) -> None:
        if not path:
            return
        if is_directory and op == "A":
            return
        relative = relative_to_root(_decode(path), self._root)
        if relative is None:
            return
        self._on_change(relative, op)


def _decode(path: str | bytes) -> str:
    return path.decode("utf-8", errors="replace") if isinstance(path, bytes) else path


class FsWatcher:
    """Recursive observer over the project root, reporting relative changes.

    Robustness rule, same as the rest of the daemon: an unwatchable root or a
    failing observer degrades to "no filesystem events", never to a crash. The
    hooks keep working on their own in that case.
    """

    def __init__(self, root: str, on_change: ChangeSink) -> None:
        self._root = os.path.normpath(root)
        self._on_change = on_change
        self._observer: Observer | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._observer is not None:
                return
            if not os.path.isdir(self._root):
                LOGGER.warning("watcher root %s does not exist; not watching", self._root)
                return
            try:
                observer = Observer()
                observer.schedule(_Handler(self._root, self._on_change), self._root, recursive=True)
                observer.start()
                self._observer = observer
            except Exception as exc:
                LOGGER.warning("could not start the filesystem watcher: %s", exc)
                self._observer = None

    def stop(self) -> None:
        with self._lock:
            observer, self._observer = self._observer, None
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=2.0)
        except Exception as exc:
            LOGGER.debug("watcher stop error: %s", exc)

    @staticmethod
    def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        """Poll `predicate` until true or `timeout` elapses (test helper).

        Filesystem notifications are inherently asynchronous, so tests need a
        bounded wait rather than a fixed sleep.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()
