import json
import os
from pathlib import Path

from udfarama.memory import SQLiteBuilder, SQLiteIndex, is_within
from udfarama.search import UdfDocument


class JSONReader:
    def __init__(self, source):
        self.source = source
        self.buffer = ""
        self.position = 0
        self.decoder = json.JSONDecoder()

    def _refill(self):
        if self.position:
            self.buffer = self.buffer[self.position:]
            self.position = 0
        block = self.source.read(65536)
        if not block:
            return False
        self.buffer += block
        return True

    def _skip(self):
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position].isspace():
                self.position += 1
            if self.position < len(self.buffer) or not self._refill():
                return

    def expect(self, char):
        self._skip()
        if self.position >= len(self.buffer) or self.buffer[self.position] != char:
            raise ValueError("JSON dizilimi geçersiz")
        self.position += 1

    def peek(self):
        self._skip()
        return self.buffer[self.position] if self.position < len(self.buffer) else ""

    def value(self):
        self._skip()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
                self.position = end
                return value
            except json.JSONDecodeError as error:
                if not self._refill():
                    raise ValueError("Eski JSON indeksi okunamadı") from error

    def entries(self):
        self.expect("{")
        while self.peek() != "}":
            name = self.value()
            if not isinstance(name, str):
                raise ValueError("Geçersiz JSON alanı")
            self.expect(":")
            if name == "documents":
                self.expect("[")
                while self.peek() != "]":
                    yield "document", self.value()
                    if self.peek() != "]":
                        self.expect(",")
                self.expect("]")
            else:
                yield name, self.value()
            if self.peek() != "}":
                self.expect(",")
        self.expect("}")
        if self.peek():
            raise ValueError("JSON sonrasında beklenmeyen içerik")


def _legacy_document(item, folder):
    if not isinstance(item, dict):
        raise ValueError("JSON içinde geçersiz belge kaydı")
    path = item.get("path")
    text = item.get("text")
    if not isinstance(path, str) or not isinstance(text, str):
        raise ValueError("JSON belge yolu veya metni geçersiz")
    path = os.path.abspath(path)
    if not is_within(folder, path):
        raise ValueError("JSON belgesi seçilen klasörün dışında")
    numeric = (
        "missing_content", "bad_archive", "permission_error", "parse_error",
        "size", "mtime_ns", "ctime_ns", "inode"
    )
    values = []
    for key in numeric:
        default = -1 if key in ("size", "mtime_ns", "ctime_ns", "inode") else 0
        value = item.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("JSON belge üstverisi geçersiz")
        values.append(value)
    return UdfDocument(path, text, *values)


def inspect_legacy(path):
    with Path(path).open("r", encoding="utf-8") as source:
        reader = JSONReader(source)
        found = {}
        for name, value in reader.entries():
            if name == "document":
                break
            if name in ("version", "folder"):
                found[name] = value
            if len(found) == 2:
                break
        return found


def migrate_legacy(path, folder, cancel_event=None, on_progress=None, on_stage=None):
    folder = os.path.abspath(folder)
    header = inspect_legacy(path)
    if header.get("version") not in (1, 2) or not isinstance(header.get("folder"), str):
        raise ValueError("Eski indeks sürümü veya klasörü geçersiz")
    if os.path.abspath(header["folder"]) != folder:
        raise ValueError("Eski indeks başka bir klasöre ait")
    builder = SQLiteBuilder(folder)
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            for name, value in JSONReader(source).entries():
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Aktarım durduruldu")
                if name == "directories":
                    if not isinstance(value, list):
                        raise ValueError("Eski indeks klasör listesi geçersiz")
                    for directory in value:
                        if isinstance(directory, str):
                            builder.add_directory(directory)
                elif name == "document":
                    doc = _legacy_document(value, folder)
                    if not builder.already_indexed(doc.path):
                        builder.add(doc)
                    directory = os.path.dirname(doc.path)
                    while is_within(folder, directory):
                        builder.add_directory(directory)
                        if directory == folder:
                            break
                        directory = os.path.dirname(directory)
                    if on_progress and builder.count % 300 == 0:
                        on_progress(builder.count)
        result = builder.finish(cancel_event, on_stage)
        if on_progress:
            on_progress(builder.count)
        return result
    except BaseException:
        builder.abort()
        raise
