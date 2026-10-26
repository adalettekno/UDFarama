import json
import logging
import os
import threading
from pathlib import Path
from typing import NamedTuple

from udfarama.search import UdfDocument, read_udf_document


_INDEX_NAME = "index.json"
_INDEX_VERSION = 1


class ScanResult(NamedTuple):
    completed: bool
    cancelled: bool
    failed: bool
    directory_errors: int
    file_count: int


class FolderMemory:
    def __init__(self):
        self._lock = threading.Lock()
        self._records = {}
        self._generation = 0
        self._directory_errors = 0
        self._directory_error_at = {}
        self.folder = None

    def begin(self, folder):
        folder = os.path.abspath(folder)
        with self._lock:
            self._generation += 1
            self.folder = folder
            self._records = {}
            self._directory_errors = 0
            self._directory_error_at = {}
            return self._generation

    def is_current(self, generation):
        with self._lock:
            return generation == self._generation

    def publish(self, generation, documents, directory_errors):
        with self._lock:
            if generation != self._generation:
                return False
            self._records = {document.path: document for document in documents}
            self._directory_error_at = {}
            if directory_errors and self.folder:
                self._directory_error_at[self.folder] = directory_errors
            self._directory_errors = directory_errors
            return True

    def store(self, generation, document):
        with self._lock:
            if generation != self._generation or document is None:
                return False
            if self._records.get(document.path) == document:
                return False
            self._records[document.path] = document
            return True

    def remove(self, generation, path):
        path = os.path.abspath(path)
        prefix = path.rstrip(os.sep) + os.sep
        with self._lock:
            if generation != self._generation:
                return False
            before = len(self._records)
            self._records.pop(path, None)
            for key in [item for item in self._records if item.startswith(prefix)]:
                del self._records[key]
            return len(self._records) != before

    def prune_absent(self, generation, directory, seen_paths):
        directory = os.path.abspath(directory)
        prefix = directory.rstrip(os.sep) + os.sep
        removed = False
        with self._lock:
            if generation != self._generation:
                return False
            for key in list(self._records):
                if key.startswith(prefix) and key not in seen_paths and not os.path.exists(key):
                    del self._records[key]
                    removed = True
        return removed

    def note_directory_errors(self, generation, directory, count):
        directory = os.path.abspath(directory)
        prefix = directory.rstrip(os.sep) + os.sep
        with self._lock:
            if generation != self._generation:
                return
            for key in [
                item for item in self._directory_error_at
                if item == directory or item.startswith(prefix)
            ]:
                del self._directory_error_at[key]
            if count:
                self._directory_error_at[directory] = count
            self._directory_errors = sum(self._directory_error_at.values())

    def snapshot(self):
        with self._lock:
            return list(self._records.values()), self._directory_errors, self._generation

    def export(self):
        with self._lock:
            return (
                self.folder,
                list(self._records.values()),
                dict(self._directory_error_at),
                self._generation,
            )

    def restore(self, folder, documents, directory_errors):
        folder = os.path.abspath(folder)
        with self._lock:
            self._generation += 1
            self.folder = folder
            self._records = {}
            for document in documents:
                path = os.path.abspath(document.path)
                if is_within(folder, path):
                    self._records[path] = document._replace(path=path)
            self._directory_error_at = {}
            for directory, count in directory_errors.items():
                directory = os.path.abspath(directory)
                if count and is_within(folder, directory):
                    self._directory_error_at[directory] = int(count)
            self._directory_errors = sum(self._directory_error_at.values())
            return self._generation

    def count(self):
        with self._lock:
            return len(self._records)


def save_index(directory, memory, generation):
    folder, documents, errors, current = memory.export()
    if current != generation or not folder:
        return False
    payload = {
        "version": _INDEX_VERSION,
        "folder": folder,
        "directory_errors": errors,
        "documents": [
            {
                "path": document.path,
                "text": document.text,
                "missing_content": document.missing_content,
                "bad_archive": document.bad_archive,
                "permission_error": document.permission_error,
                "parse_error": document.parse_error,
            }
            for document in documents
        ],
    }
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / _INDEX_NAME
    temporary = directory / f"{_INDEX_NAME}.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, target)
    return True


def load_index(directory, folder):
    folder = os.path.abspath(folder)
    path = Path(directory) / _INDEX_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _INDEX_VERSION:
        return None
    stored_folder = payload.get("folder")
    if not isinstance(stored_folder, str) or os.path.abspath(stored_folder) != folder:
        return None
    documents = []
    raw_documents = payload.get("documents")
    if not isinstance(raw_documents, list):
        return None
    for item in raw_documents:
        document = _document_from_index(item, folder)
        if document is not None:
            documents.append(document)
    errors = {}
    raw_errors = payload.get("directory_errors")
    if isinstance(raw_errors, dict):
        for directory, count in raw_errors.items():
            if not isinstance(directory, str) or isinstance(count, bool) or not isinstance(count, int):
                continue
            directory = os.path.abspath(directory)
            if count > 0 and is_within(folder, directory):
                errors[directory] = count
    return documents, errors


def _document_from_index(item, folder):
    if not isinstance(item, dict):
        return None
    path = item.get("path")
    text = item.get("text")
    if not isinstance(path, str) or not isinstance(text, str):
        return None
    path = os.path.abspath(path)
    if not is_within(folder, path):
        return None
    counts = []
    for name in ("missing_content", "bad_archive", "permission_error", "parse_error"):
        value = item.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        counts.append(value)
    return UdfDocument(path, text, *counts)


def iter_directories(folder):
    folder = os.path.abspath(folder)
    for base, dirs, _files in os.walk(folder, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
        yield base


def is_within(folder, path):
    folder = os.path.abspath(folder)
    path = os.path.abspath(path)
    return path == folder or path.startswith(folder + os.sep)


def scan_folder(folder, generation, memory, cancel_event, on_progress=None, on_directory=None):
    documents = []
    directory_errors = 0
    seen = 0

    def on_walk_error(_error):
        nonlocal directory_errors
        directory_errors += 1

    try:
        for base, dirs, files in os.walk(folder, followlinks=False, onerror=on_walk_error):
            if cancel_event.is_set() or not memory.is_current(generation):
                return ScanResult(False, True, False, directory_errors, seen)
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
            if on_directory is not None:
                on_directory(base)
            for name in files:
                if cancel_event.is_set() or not memory.is_current(generation):
                    return ScanResult(False, True, False, directory_errors, seen)
                if not name.lower().endswith(".udf"):
                    continue
                document = read_udf_document(os.path.join(base, name))
                if document is None:
                    continue
                documents.append(document)
                seen += 1
                if on_progress is not None and (seen == 1 or seen % 25 == 0):
                    on_progress(seen)
        if not memory.publish(generation, documents, directory_errors):
            return ScanResult(False, True, False, directory_errors, seen)
        return ScanResult(True, False, False, directory_errors, seen)
    except Exception:
        logging.exception("Klasör hafızaya alınamadı")
        return ScanResult(False, False, True, directory_errors, seen)


def refresh_paths(memory, generation, folder, changed, removed, cancel_event=None, on_directory=None):
    mutated = False
    for path in removed:
        if not memory.is_current(generation):
            return False
        mutated = memory.remove(generation, path) or mutated
    for path in changed:
        if cancel_event is not None and cancel_event.is_set():
            return False
        if not memory.is_current(generation):
            return False
        path = os.path.abspath(path)
        if not is_within(folder, path):
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            mutated = _scan_into(memory, generation, path, cancel_event, on_directory) or mutated
        elif os.path.isfile(path) and path.lower().endswith(".udf"):
            document = read_udf_document(path)
            if document is None:
                mutated = memory.remove(generation, path) or mutated
            else:
                mutated = memory.store(generation, document) or mutated
        elif not os.path.lexists(path):
            mutated = memory.remove(generation, path) or mutated
    if not memory.is_current(generation):
        return False
    return mutated


def _scan_into(memory, generation, folder, cancel_event, on_directory):
    seen = set()
    directory_errors = 0
    mutated = False

    def on_walk_error(_error):
        nonlocal directory_errors
        directory_errors += 1

    for base, dirs, files in os.walk(folder, followlinks=False, onerror=on_walk_error):
        if cancel_event is not None and cancel_event.is_set():
            return mutated
        if not memory.is_current(generation):
            return False
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
        if on_directory is not None:
            on_directory(base)
        for name in files:
            if not name.lower().endswith(".udf"):
                continue
            path = os.path.abspath(os.path.join(base, name))
            seen.add(path)
            document = read_udf_document(path)
            if document is not None:
                mutated = memory.store(generation, document) or mutated
    mutated = memory.prune_absent(generation, folder, seen) or mutated
    memory.note_directory_errors(generation, folder, directory_errors)
    return mutated
