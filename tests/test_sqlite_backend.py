import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from udfarama.memory import SQLiteBuilder, SQLiteIndex, database_path
from udfarama.migration import inspect_legacy, migrate_legacy
from udfarama.search import UdfDocument, read_udf_document


class SQLiteBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "documents"
        self.root.mkdir()
        self.environ = patch.dict(os.environ, {"UDFARAMA_DATA_DIR": str(Path(self.tmp.name) / "db")})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def udf(self, path, content):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(file, "w") as archive:
            archive.writestr("content.xml", "<root>" + content + "</root>")
        return file

    def build(self, paths):
        builder = SQLiteBuilder(str(self.root))
        builder.add_directory(str(self.root))
        for path in paths:
            builder.add_directory(str(path.parent))
            builder.add(read_udf_document(str(path)))
        return builder.finish()

    def test_index_on_restart_never_reads_udf_files(self):
        file = self.udf("a.udf", "ilk karar")
        self.build([file])
        self.udf("a.udf", "sonradan değiştirildi")
        with patch("udfarama.search.read_udf_document", side_effect=AssertionError("UDF read")):
            with patch("os.walk", side_effect=AssertionError("walk")):
                store = SQLiteIndex(str(self.root))
                self.assertEqual(len(store.search("ilk karar").matches), 1)
                self.assertEqual(len(store.search("sonradan").matches), 0)

    def test_live_changes_are_saved_and_survive_restart(self):
        file = self.udf("a.udf", "önceki metin")
        store = self.build([file])
        self.udf("a.udf", "güncel metin")
        self.assertEqual(store.refresh({str(file)}, set()), 1)
        again = SQLiteIndex(str(self.root))
        self.assertEqual(len(again.search("güncel").matches), 1)
        self.assertFalse(again.search("önceki").matches)

    def test_duplicate_live_event_does_not_reopen(self):
        file = self.udf("a.udf", "ilk")
        store = self.build([file])
        with patch("udfarama.search.read_udf_document", side_effect=AssertionError("repeat read")):
            self.assertEqual(store.refresh({str(file)}, set()), 0)

    def test_new_folder_and_rename_and_delete(self):
        old = self.udf("a.udf", "karar")
        store = self.build([old])
        new_dir = self.root / "new" / "nested"
        new_dir.mkdir(parents=True)
        new = self.udf("new/nested/b.udf", "emsal")
        seen = []
        store.refresh({str(self.root / "new")}, set(), on_directory=seen.append)
        self.assertIn(str(new_dir), seen)
        self.assertEqual(len(store.search("emsal").matches), 1)
        renamed = new.with_name("c.udf")
        new.rename(renamed)
        store.refresh({str(renamed)}, {str(new)})
        self.assertIn("c.udf", [m.basename for m in store.search("emsal").matches])
        store.refresh(set(), {str(self.root / "new")})
        self.assertFalse(store.search("emsal").matches)
        self.assertEqual(store.count(), 1)

    def test_existing_directory_event_does_not_reindex_files(self):
        file = self.udf("nested/a.udf", "muris")
        store = self.build([file])
        with patch("udfarama.search.read_udf_document", side_effect=AssertionError("read")):
            self.assertEqual(store.refresh({str(file.parent)}, set()), 0)

    def test_turkish_case_wildcard_substring_and_filename(self):
        a = self.udf("a.udf", "Türkiye Barolar Birliği İSTANBUL")
        b = self.udf("emsal-karar.udf", "alakasız")
        store = self.build([a, b])
        self.assertEqual(len(store.search("tür* birliği").matches), 1)
        self.assertEqual(len(store.search("istanbul").matches), 1)
        self.assertEqual(len(store.search("stan").matches), 1)
        self.assertEqual(len(store.search("emsal").matches), 1)
        self.assertFalse(store.search("isik").matches)

    def test_page_size_does_not_load_all_results(self):
        files = [self.udf(f"{i:03d}.udf", "çok belgeli karar") for i in range(9)]
        store = self.build(files)
        cursor = 0
        seen = []
        for page_no in range(5):
            page = store.search("karar", after_id=cursor, limit=2)
            seen.extend(m.path for m in page.matches)
            cursor = page.cursor
            if not page.has_more:
                break
        self.assertEqual(len(seen), 9)
        self.assertEqual(len(set(seen)), 9)

    def test_multiple_folders_have_separate_indexes(self):
        original = self.build([self.udf("a.udf", "bir")])
        second = Path(self.tmp.name) / "other"
        second.mkdir()
        builder = SQLiteBuilder(str(second))
        builder.add(UdfDocument(str(second / "b.udf"), "iki", 0, 0, 0, 0))
        other = builder.finish()
        self.assertNotEqual(original.path, other.path)
        self.assertEqual(original.count(), 1)
        self.assertEqual(other.count(), 1)

    def test_cancelled_refresh_rolls_back(self):
        file = self.udf("a.udf", "ilk")
        store = self.build([file])
        self.udf("a.udf", "son")
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(InterruptedError):
            store.refresh({str(file)}, set(), cancelled)
        self.assertTrue(store.search("ilk").matches)
        self.assertFalse(store.search("son").matches)

    def test_old_json_migration_does_not_read_udf(self):
        stored = self.root / "offline.udf"
        path = Path(self.tmp.name) / "index.json"
        path.write_text(json.dumps({
            "version": 2,
            "folder": str(self.root),
            "directories": [str(self.root), str(self.root / "empty")],
            "directory_errors": {},
            "documents": [{
                "path": str(stored), "text": "Türkiye Bankalar Birliği",
                "missing_content": 0, "bad_archive": 0,
                "permission_error": 0, "parse_error": 0,
            }],
        }, ensure_ascii=False), encoding="utf-8")
        with patch("udfarama.search.read_udf_document", side_effect=AssertionError("read")):
            migrated = migrate_legacy(path, str(self.root))
        self.assertEqual(migrated.count(), 1)
        self.assertTrue(migrated.search("bankalar").matches)
        self.assertTrue(path.exists())
        self.assertIn(str(self.root / "empty"), list(migrated.directories())[0])

    def test_large_migration_reads_json_in_chunks(self):
        path = Path(self.tmp.name) / "index.json"
        payload = {
            "version": 2, "folder": str(self.root), "directories": [str(self.root)],
            "directory_errors": {}, "documents": [
                {"path": str(self.root / f"d_{i:05d}.udf"), "text": "sabit metin " + str(i),
                 "missing_content": 0, "bad_archive": 0, "permission_error": 0, "parse_error": 0}
                for i in range(1400)
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(inspect_legacy(path)["version"], 2)
        store = migrate_legacy(path, str(self.root))
        self.assertEqual(store.count(), 1400)

    def test_invalid_index_is_not_silently_rebuilt(self):
        self.build([self.udf("a.udf", "karar")])
        db_path = database_path(self.root)
        db_path.write_bytes(b"invalid sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            SQLiteIndex(str(self.root))

    def test_sqlite_file_is_private(self):
        store = self.build([self.udf("a.udf", "karar")])
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()

class PartialBuildTests(unittest.TestCase):
    def test_resume_partial_without_duplicating_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"UDFARAMA_DATA_DIR": str(Path(tmp) / "data")}):
                folder = Path(tmp) / "documents"
                folder.mkdir()
                builder = SQLiteBuilder(str(folder))
                builder.add(UdfDocument(str(folder / "first.udf"), "ilk", 0, 0, 0, 0))
                builder.flush()
                builder.abort()
                resumed = SQLiteBuilder(str(folder))
                self.assertEqual(resumed.count, 1)
                self.assertTrue(resumed.already_indexed(str(folder / "first.udf")))
                resumed.add(UdfDocument(str(folder / "second.udf"), "ikinci", 0, 0, 0, 0))
                store = resumed.finish()
                self.assertEqual(store.count(), 2)
                self.assertTrue(store.search("ikinci").matches)
