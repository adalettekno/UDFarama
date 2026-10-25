import os

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


_CHANGE_EVENTS = (
    Gio.FileMonitorEvent.CREATED,
    Gio.FileMonitorEvent.CHANGED,
    Gio.FileMonitorEvent.CHANGES_DONE_HINT,
    Gio.FileMonitorEvent.MOVED_IN,
    Gio.FileMonitorEvent.ATTRIBUTE_CHANGED,
)
_REMOVE_EVENTS = (
    Gio.FileMonitorEvent.DELETED,
    Gio.FileMonitorEvent.MOVED_OUT,
)
_RENAME_EVENTS = (
    Gio.FileMonitorEvent.RENAMED,
    Gio.FileMonitorEvent.MOVED,
)


def classify_monitor_event(event, path, other_path=None):
    changed = set()
    removed = set()
    if not path:
        return changed, removed
    if event in _REMOVE_EVENTS:
        removed.add(path)
    elif event in _RENAME_EVENTS:
        removed.add(path)
        if other_path:
            changed.add(other_path)
    elif event in _CHANGE_EVENTS:
        changed.add(path)
    return changed, removed


class TreeWatch:
    def __init__(self, on_batch, delay_ms=400):
        self._on_batch = on_batch
        self._delay = delay_ms
        self._monitors = {}
        self._changed = set()
        self._removed = set()
        self._source = None

    def watch(self, directory):
        directory = os.path.abspath(directory)
        if directory in self._monitors or os.path.islink(directory) or not os.path.isdir(directory):
            return
        monitor = Gio.File.new_for_path(directory).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES,
            None,
        )
        monitor.connect("changed", self._on_changed)
        self._monitors[directory] = monitor

    def unwatch_under(self, path):
        path = os.path.abspath(path)
        prefix = path.rstrip(os.sep) + os.sep
        for directory in list(self._monitors):
            if directory == path or directory.startswith(prefix):
                self._monitors.pop(directory).cancel()

    def clear(self):
        for monitor in self._monitors.values():
            monitor.cancel()
        self._monitors.clear()
        self._changed.clear()
        self._removed.clear()
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None

    def _on_changed(self, _monitor, file, other, event):
        path = file.get_path() if file is not None else None
        other_path = other.get_path() if other is not None else None
        changed, removed = classify_monitor_event(event, path, other_path)
        if not changed and not removed:
            return
        for item in removed:
            self.unwatch_under(item)
        self._removed.update(removed)
        self._changed.update(changed)
        self._changed.difference_update(self._removed)
        if self._source is None:
            self._source = GLib.timeout_add(self._delay, self._fire)

    def _fire(self):
        changed = set(self._changed)
        removed = set(self._removed)
        self._changed.clear()
        self._removed.clear()
        self._source = None
        if changed or removed:
            self._on_batch(changed, removed)
        return GLib.SOURCE_REMOVE
