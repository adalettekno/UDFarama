import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from udfarama.search import UdfDocument, extract_snippet, normalize_search_text, prepare_query


APP_ID = "com.adalettekno.UDFarama"
PAGE_SIZE = 200
BATCH_SIZE = 300
BATCH_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class MatchPage:
    matches: tuple
    has_more: bool
    cursor: int
    cancelled: bool = False


@dataclass(frozen=True)
class IndexedMatch:
    path: str
    relative_path: str
    basename: str
    snippet: str


def is_within(folder, path):
    folder = os.path.abspath(folder)
    path = os.path.abspath(path)
    return path == folder or path.startswith(folder + os.sep)


def data_directory():
    root = os.environ.get("UDFARAMA_DATA_DIR")
    if root:
        return Path(root)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local/share"
    return base / APP_ID


def database_path(folder):
    name = hashlib.sha256(os.path.abspath(folder).encode("utf-8")).hexdigest()
    return data_directory() / "indexes" / f"{name}.sqlite3"


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _connection(path, create=False, wal=True):
    if create:
        _private_directory(path.parent)
    connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        if create or not wal:
            connection.execute("PRAGMA journal_mode=" + ("WAL" if wal else "DELETE"))
        connection.execute("PRAGMA synchronous=NORMAL")
        if create:
            path.chmod(0o600)
        return connection
    except BaseException:
        connection.close()
        raise


def _create_schema(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS directories (
            path TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            basename TEXT NOT NULL,
            text TEXT NOT NULL,
            norm_text TEXT NOT NULL,
            norm_name TEXT NOT NULL,
            missing_content INTEGER NOT NULL DEFAULT 0,
            bad_archive INTEGER NOT NULL DEFAULT 0,
            permission_error INTEGER NOT NULL DEFAULT 0,
            parse_error INTEGER NOT NULL DEFAULT 0,
            size INTEGER NOT NULL DEFAULT -1,
            mtime_ns INTEGER NOT NULL DEFAULT -1,
            ctime_ns INTEGER NOT NULL DEFAULT -1,
            inode INTEGER NOT NULL DEFAULT -1
        );
    """)


def _enable_fts(connection):
    connection.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            norm_text, norm_name,
            content='documents', content_rowid='id', tokenize='trigram'
        )
    """)
    connection.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
    connection.executescript("""
        CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, norm_text, norm_name)
            VALUES (new.id, new.norm_text, new.norm_name);
        END;
        CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, norm_text, norm_name)
            VALUES ('delete', old.id, old.norm_text, old.norm_name);
        END;
        CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
            INSERT INTO documents_fts(documents_fts, rowid, norm_text, norm_name)
            VALUES ('delete', old.id, old.norm_text, old.norm_name);
            INSERT INTO documents_fts(rowid, norm_text, norm_name)
            VALUES (new.id, new.norm_text, new.norm_name);
        END;
    """)


def _row(document):
    name = os.path.basename(document.path)
    return (
        document.path, name, document.text,
        normalize_search_text(document.text), normalize_search_text(name),
        document.missing_content, document.bad_archive,
        document.permission_error, document.parse_error,
        document.size, document.mtime_ns, document.ctime_ns, document.inode,
    )


_INSERT = """
    INSERT INTO documents (
        path, basename, text, norm_text, norm_name,
        missing_content, bad_archive, permission_error, parse_error,
        size, mtime_ns, ctime_ns, inode
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        basename=excluded.basename,
        text=excluded.text,
        norm_text=excluded.norm_text,
        norm_name=excluded.norm_name,
        missing_content=excluded.missing_content,
        bad_archive=excluded.bad_archive,
        permission_error=excluded.permission_error,
        parse_error=excluded.parse_error,
        size=excluded.size,
        mtime_ns=excluded.mtime_ns,
        ctime_ns=excluded.ctime_ns,
        inode=excluded.inode
"""


class SQLiteBuilder:
    def __init__(self, folder, fresh=False):
        self.folder = os.path.abspath(folder)
        self.final_path = database_path(self.folder)
        self.fresh = fresh
        suffix = ".building." + uuid.uuid4().hex if fresh else ".building"
        self.temp_path = self.final_path.with_name(self.final_path.name + suffix)
        _private_directory(self.final_path.parent)
        existing = self.temp_path.exists()
        self.connection = _connection(self.temp_path, create=not existing, wal=False)
        _create_schema(self.connection)
        if existing:
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key='folder'"
            ).fetchone()
            if not row or row[0] != self.folder:
                self.connection.close()
                raise ValueError("Yarım kalmış indeks farklı bir klasöre ait")
        else:
            self.connection.execute(
                "INSERT INTO metadata(key,value) VALUES('folder', ?)", (self.folder,)
            )
        self.pending = []
        self.pending_directories = []
        self._recent_directories = set()
        self._pending_bytes = 0
        self.count = self.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        self.closed = False

    def already_indexed(self, path):
        row = self.connection.execute(
            "SELECT 1 FROM documents WHERE path=?", (os.path.abspath(path),)
        ).fetchone()
        return row is not None

    def add(self, document):
        record = _row(document)
        self.pending.append(record)
        self._pending_bytes += len(document.text.encode("utf-8")) * 2 + len(document.path)
        self.count += 1
        if len(self.pending) >= BATCH_SIZE or self._pending_bytes >= BATCH_BYTES:
            self.flush()

    def add_directory(self, path):
        path = os.path.abspath(path)
        if is_within(self.folder, path) and path not in self._recent_directories:
            self._recent_directories.add(path)
            if len(self._recent_directories) > 65000:
                self._recent_directories = {self.folder, path}
            self.pending_directories.append((path,))
            if len(self.pending_directories) >= BATCH_SIZE:
                self.flush()

    def flush(self):
        if not self.pending and not self.pending_directories:
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self.pending:
                self.connection.executemany(_INSERT, self.pending)
            if self.pending_directories:
                self.connection.executemany(
                    "INSERT OR IGNORE INTO directories(path) VALUES(?)",
                    self.pending_directories,
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        self.pending.clear()
        self.pending_directories.clear()
        self._pending_bytes = 0

    def finish(self, cancel_event=None, on_stage=None):
        self.add_directory(self.folder)
        self.flush()
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError()
        if on_stage:
            on_stage("Arama indeksi oluşturuluyor…")
        if cancel_event is not None:
            self.connection.set_progress_handler(
                lambda: 1 if cancel_event.is_set() else 0, 10000
            )
        try:
            _enable_fts(self.connection)
        finally:
            self.connection.set_progress_handler(None, 0)
        self.connection.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('state', 'ready')"
        )
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError()
        self.connection.close()
        self.closed = True
        if self.fresh and self.final_path.is_file():
            source = _connection(self.temp_path, wal=False)
            try:
                destination = _connection(self.final_path)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
            self.temp_path.unlink()
        else:
            os.replace(self.temp_path, self.final_path)
        self.final_path.chmod(0o600)
        connection = _connection(self.final_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        finally:
            connection.close()
        return SQLiteIndex(self.folder)

    def abort(self):
        if not self.closed:
            self.connection.close()
            self.closed = True
        if self.fresh:
            self.temp_path.unlink(missing_ok=True)


class SQLiteIndex:
    def __init__(self, folder):
        self.folder = os.path.abspath(folder)
        self.path = database_path(self.folder)
        self._write_lock = threading.Lock()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key='folder'").fetchone()
            state = conn.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
            if not row or row[0] != self.folder or not state or state[0] != "ready":
                raise ValueError("SQLite indeksi geçerli veya tamamlanmış değil")

    @classmethod
    def available(cls, folder):
        path = database_path(folder)
        if not path.is_file():
            return False
        return True

    @contextmanager
    def connection(self):
        connection = _connection(self.path, wal=True)
        try:
            yield connection
        finally:
            connection.close()

    def count(self):
        with self.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def directories(self, chunk=250):
        with self.connection() as conn:
            cursor = conn.execute("SELECT path FROM directories ORDER BY path")
            while True:
                rows = cursor.fetchmany(chunk)
                if not rows:
                    break
                yield [row[0] for row in rows]

    def _known_directory(self, conn, path):
        return conn.execute("SELECT 1 FROM directories WHERE path=?", (path,)).fetchone() is not None

    def _remove(self, conn, path):
        prefix = path.rstrip(os.sep) + os.sep
        if self._known_directory(conn, path):
            conn.execute("DELETE FROM documents WHERE path >= ? AND path < ?", (prefix, prefix + "\uffff"))
            conn.execute("DELETE FROM directories WHERE path >= ? AND path < ?", (prefix, prefix + "\uffff"))
            conn.execute("DELETE FROM directories WHERE path=?", (path,))
        cur = conn.execute("DELETE FROM documents WHERE path=?", (path,))
        return bool(cur.rowcount)

    def _is_unchanged(self, conn, path, stat):
        row = conn.execute(
            "SELECT size,mtime_ns,ctime_ns,inode FROM documents WHERE path=?", (path,)
        ).fetchone()
        return row == (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)

    def refresh(self, changed, removed, cancel_event=None, on_directory=None):
        changed_count = 0
        with self._write_lock:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for path in sorted(removed):
                        if cancel_event is not None and cancel_event.is_set():
                            raise InterruptedError()
                        path = os.path.abspath(path)
                        if not is_within(self.folder, path):
                            continue
                        self._remove(conn, path)
                        changed_count += 1
                    for path in sorted(changed):
                        if cancel_event is not None and cancel_event.is_set():
                            raise InterruptedError()
                        path = os.path.abspath(path)
                        if not is_within(self.folder, path):
                            continue
                        if os.path.isdir(path) and not os.path.islink(path):
                            if self._known_directory(conn, path):
                                continue
                            for base, dirs, files in os.walk(path, followlinks=False):
                                if cancel_event is not None and cancel_event.is_set():
                                    raise InterruptedError()
                                dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
                                conn.execute("INSERT OR IGNORE INTO directories(path) VALUES(?)", (base,))
                                if on_directory is not None:
                                    on_directory(base)
                                for name in files:
                                    if cancel_event is not None and cancel_event.is_set():
                                        raise InterruptedError()
                                    if name.lower().endswith(".udf"):
                                        candidate = os.path.join(base, name)
                                        changed_count += self._refresh_file(conn, candidate)
                        elif os.path.isfile(path) and path.lower().endswith(".udf"):
                            changed_count += self._refresh_file(conn, path)
                        elif not os.path.lexists(path):
                            self._remove(conn, path)
                            changed_count += 1
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        return changed_count

    def _refresh_file(self, conn, path):
        try:
            state = os.stat(path)
        except OSError:
            return 0
        if self._is_unchanged(conn, path, state):
            return 0
        from udfarama.search import read_udf_document
        document = read_udf_document(path)
        if document is None:
            return 0
        conn.execute(_INSERT, _row(document))
        return 1

    def search(self, term, after_id=0, limit=PAGE_SIZE, cancel_event=None):
        query = prepare_query(term)
        parts = [normalize_search_text(part) for part in term.split("*")]
        seed = max(parts, key=len) if parts else ""
        matched = []
        cursor_id = after_id
        with self.connection() as conn:
            if cancel_event is not None:
                conn.set_progress_handler(lambda: 1 if cancel_event.is_set() else 0, 5000)
            if len(seed) >= 3:
                search_token = '"' + seed.replace('"', '""') + '"'
                statement = """
                    SELECT d.id, d.path, d.text, d.norm_text, d.norm_name
                    FROM documents_fts
                    JOIN documents d ON d.id=documents_fts.rowid
                    WHERE documents_fts MATCH ? AND d.id > ?
                    ORDER BY d.id
                """
                params = (search_token, after_id)
            else:
                statement = """
                    SELECT id,path,text,norm_text,norm_name FROM documents
                    WHERE id > ? ORDER BY id
                """
                params = (after_id,)
            try:
                results = conn.execute(statement, params)
                for row_id, path, text, norm_text, norm_name in results:
                    if cancel_event is not None and cancel_event.is_set():
                        return MatchPage(tuple(), False, after_id, True)
                    kind, value = query
                    if kind == "literal":
                        is_match = value in norm_text or value in norm_name
                        content_match = value in norm_text
                    else:
                        is_match = bool(value.search(norm_text) or value.search(norm_name))
                        content_match = bool(value.search(norm_text))
                    if not is_match:
                        continue
                    if len(matched) >= limit:
                        return MatchPage(tuple(matched), True, cursor_id)
                    matched.append(IndexedMatch(
                        path,
                        os.path.relpath(path, self.folder),
                        os.path.basename(path),
                        extract_snippet(text, term) if content_match else ""
                    ))
                    cursor_id = row_id
            except sqlite3.OperationalError as error:
                if cancel_event is not None and cancel_event.is_set() and "interrupted" in str(error).lower():
                    return MatchPage(tuple(), False, after_id, True)
                raise
            finally:
                conn.set_progress_handler(None, 0)
        return MatchPage(tuple(matched), False, cursor_id)
