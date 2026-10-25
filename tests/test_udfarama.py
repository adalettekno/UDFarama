import ast
import logging
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from udfarama.logging_setup import configure_logging
from udfarama.search import (
    MatchItem,
    SearchOutcome,
    extract_snippet,
    format_search_status,
    format_search_status_parts,
    make_match_item,
    normalize_search_text,
    search_udf_folder,
    should_apply_search_generation,
)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def document(self, name, text):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("content.xml", text)
        return path

    def search(self, term, cancel_event=None, on_progress=None):
        return search_udf_folder(
            str(self.folder), term, cancel_event=cancel_event, on_progress=on_progress)

    def test_nested_xml_and_uppercase_extension(self):
        path = self.document("nested/test.UDF", "<root><content>karar</content></root>")
        outcome = self.search("karar")
        self.assertEqual(len(outcome.matches), 1)
        match = outcome.matches[0]
        self.assertEqual(match.path, str(path))
        self.assertEqual(match.basename, "test.UDF")
        self.assertEqual(match.relative_path, os.path.join("nested", "test.UDF"))
        self.assertEqual(match.snippet, "karar")
        self.assertEqual(outcome[1:], (0, 0, 0, 0, 0, False, False))

    def test_turkish_case_and_unicode(self):
        path = self.document("test.udf", "<root>İSTANBUL HAKİM IŞIK</root>")
        for term in ("istanbul", "hakim", "ışık", "I\u0307STANBUL"):
            with self.subTest(term=term):
                outcome = self.search(term)
                self.assertEqual([m.path for m in outcome.matches], [str(path)])
                self.assertTrue(outcome.matches[0].snippet)
        self.assertEqual(normalize_search_text("I"), "ı")
        self.assertEqual(self.search("isik").matches, [])

    def test_broken_deflate_counts_as_bad_archive(self):
        broken = self.document("broken.udf", "<root>karar</root>")
        data = bytearray(broken.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", data, 26)
        data[30 + name_len + extra_len] = 7
        broken.write_bytes(data)
        good = self.document("good.udf", "<root>karar</root>")
        outcome = self.search("karar")
        self.assertEqual([m.path for m in outcome.matches], [str(good)])
        self.assertEqual(outcome.bad_archive, 1)
        self.assertEqual(outcome.missing_content, 0)
        self.assertIn("1 dosya bozuk veya açılamadı", format_search_status(outcome))

    def test_invalid_zip_and_missing_content_are_separate(self):
        (self.folder / "invalid.udf").write_text("not a zip")
        with zipfile.ZipFile(self.folder / "missing.udf", "w") as archive:
            archive.writestr("other.xml", "<root/>")
        outcome = self.search("karar")
        self.assertEqual(outcome.matches, [])
        self.assertEqual(outcome.bad_archive, 1)
        self.assertEqual(outcome.missing_content, 1)
        message = format_search_status(outcome)
        self.assertIn("content.xml yok", message)
        self.assertIn("bozuk veya açılamadı", message)

    def test_malformed_xml_fallback_counts_parse(self):
        path = self.document("old.udf", "<root>karar")
        outcome = self.search("karar")
        self.assertEqual([m.path for m in outcome.matches], [str(path)])
        self.assertEqual(outcome.parse_errors, 1)
        self.assertIn("XML çözümlenemedi", format_search_status(outcome))
        self.assertIn("tamamlandı", format_search_status(outcome))

    def test_permission_error_on_file(self):
        self.document("secret.udf", "<root>karar</root>")

        def deny_open(path, *args, **kwargs):
            raise PermissionError("permission denied")

        with patch("udfarama.search.zipfile.ZipFile", side_effect=deny_open):
            outcome = self.search("karar")
        self.assertEqual(outcome.permission_errors, 1)
        self.assertEqual(outcome.bad_archive, 0)
        self.assertIn("izin yok", format_search_status(outcome))

    def test_unreadable_directory_is_reported(self):
        original_walk = os.walk
        def denied_walk(folder, **kwargs):
            kwargs["onerror"](PermissionError("permission denied"))
            yield from original_walk(folder, **kwargs)
        with patch.object(os, "walk", denied_walk):
            outcome = self.search("karar")
        self.assertEqual(outcome, SearchOutcome([], 0, 0, 0, 0, 1, False, False))
        message = format_search_status(outcome)
        self.assertIn("kısmen", message)
        self.assertIn("1 klasör taranamadı", message)

    def test_unexpected_error_sets_failed(self):
        with patch.object(os, "walk", side_effect=ValueError("unexpected")):
            with self.assertLogs(level="ERROR"):
                outcome = self.search("karar")
        self.assertEqual(outcome, SearchOutcome([], 0, 0, 0, 0, 0, True, False))
        self.assertIn("tarama durdu", format_search_status(outcome))

    def test_progress_reports_scanned_and_matches(self):
        self.document("a.udf", "<root>karar</root>")
        progress = []
        outcome = self.search("karar", on_progress=lambda s, m: progress.append((s, m)))
        self.assertEqual(progress, [(1, 1)])
        self.assertEqual(outcome.matches[0].basename, "a.udf")

    def test_cancel_stops_and_keeps_partial_matches(self):
        self.document("one.udf", "<root>karar</root>")
        self.document("two.udf", "<root>karar</root>")
        self.document("three.udf", "<root>karar</root>")
        cancel_event = threading.Event()
        original_read = zipfile.ZipFile.read
        seen = {"n": 0}

        def read_and_cancel(archive, name):
            data = original_read(archive, name)
            seen["n"] += 1
            if seen["n"] >= 1:
                cancel_event.set()
            return data

        with patch.object(zipfile.ZipFile, "read", read_and_cancel):
            outcome = self.search("karar", cancel_event=cancel_event)
        self.assertTrue(outcome.cancelled)
        self.assertLess(len(outcome.matches), 3)
        self.assertIn("durduruldu", format_search_status(outcome))

    def test_make_match_item_fields(self):
        folder = "/docs"
        path = "/docs/alt/emsal.udf"
        item = make_match_item(folder, path, "önce karar sonra", "karar")
        self.assertEqual(
            item,
            MatchItem(path, os.path.join("alt", "emsal.udf"), "emsal.udf", "önce karar sonra"),
        )

    def test_extract_snippet_truncates(self):
        long_line = "x" * 20 + "karar" + "y" * 80
        snippet = extract_snippet(long_line, "karar", max_len=30)
        self.assertEqual(len(snippet), 30)
        self.assertTrue(snippet.endswith("…"))

    def test_cancelled_status_text(self):
        outcome = SearchOutcome([make_match_item("/f", "/f/a.udf")], 0, 0, 0, 0, 0, False, True)
        self.assertEqual(format_search_status(outcome), "Arama durduruldu: 1 sonuç.")

    def test_skips_xml_parse_when_raw_has_no_term(self):
        self.document("a.udf", "<root><content>dilekce</content></root>")
        with patch("udfarama.search.ET.fromstring", wraps=ET.fromstring) as parse:
            outcome = self.search("karar")
            parse.assert_not_called()
        self.assertEqual(outcome.matches, [])

    def test_status_parts_separate_primary_and_detail(self):
        outcome = SearchOutcome([], 2, 1, 0, 0, 0, False, False)
        primary, detail = format_search_status_parts(outcome)
        self.assertEqual(primary, "Arama kısmen tamamlandı: 0 sonuç.")
        self.assertIn("content.xml yok", detail)
        self.assertIn("bozuk veya açılamadı", detail)
        self.assertNotIn("content.xml", primary)

    def test_generation_gate(self):
        self.assertTrue(should_apply_search_generation(3, 3))
        self.assertFalse(should_apply_search_generation(3, 2))


class LoggingSetupTests(unittest.TestCase):
    def test_configure_logging_sets_warning_level(self):
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        try:
            root.handlers.clear()
            configure_logging()
            self.assertEqual(root.level, logging.WARNING)
            self.assertTrue(root.handlers)
            configure_logging()
            self.assertEqual(len(root.handlers), 1)
        finally:
            root.handlers.clear()
            root.handlers.extend(saved_handlers)
            root.setLevel(saved_level)


class MainGuardTests(unittest.TestCase):
    root = ROOT

    def test_app_module_has_no_toplevel_run(self):
        tree = ast.parse((self.root / "src/udfarama/app.py").read_text())
        for node in tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            name = ""
            if isinstance(call.func, ast.Name):
                name = call.func.id
            elif isinstance(call.func, ast.Attribute):
                name = call.func.attr
            if name in ("run", "run_app"):
                self.fail("app.py must not start the UI at module level")

    def test_main_module_guards_entry(self):
        tree = ast.parse((self.root / "src/udfarama/__main__.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                self.fail("__main__.py must not invoke calls outside the name guard")
        guarded = False
        for node in tree.body:
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"):
                continue
            guarded = True
            self.assertTrue(any(isinstance(child, ast.Raise) for child in node.body))
        self.assertTrue(guarded)

    def test_search_result_from_match_smoke(self):
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
        except (ImportError, ValueError):
            self.skipTest("GTK/Adw unavailable")
        from udfarama.app import SearchResult
        from udfarama.search import make_match_item
        item = make_match_item("/f", "/f/a.udf", "karar metni", "karar")
        result = SearchResult.from_match(item)
        self.assertEqual(result.path, item.path)
        self.assertEqual(result.basename, item.basename)
        self.assertEqual(result.snippet, item.snippet)


if __name__ == "__main__":
    unittest.main()
