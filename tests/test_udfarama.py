import ast
import logging
import lzma
import os
from pathlib import Path
import struct
import tempfile
import threading
import types
import unicodedata
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile
import zlib

tree = ast.parse((Path(__file__).resolve().parents[1] / "src/udfarama/app.py").read_text())
window = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "UdfSearchWindow")
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "normalize_search_text"]
nodes += [
    n for n in window.body
    if isinstance(n, ast.FunctionDef)
    and n.name in ("_search_worker", "_search_finished", "_search_progress")
]
namespace = dict(os=os, logging=logging, lzma=lzma, unicodedata=unicodedata, ET=ET, zipfile=zipfile, zlib=zlib)
exec(compile(ast.Module(body=nodes, type_ignores=[]), "src/udfarama/app.py", "exec"), namespace)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.callbacks = []
        namespace["GLib"] = types.SimpleNamespace(
            idle_add=lambda callback, *args: self.callbacks.append(args), SOURCE_REMOVE=False)
        self.ui = types.SimpleNamespace(
            _search_finished=Mock(),
            _search_progress=Mock(),
            model=Mock(),
            status=Mock(),
            search_button=Mock(),
            searching=True,
            _search_generation=1,
            _cancel_event=threading.Event(),
        )

    def document(self, name, text):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("content.xml", text)
        return path

    def finish_args(self):
        finished = [args for args in self.callbacks if len(args) >= 2 and isinstance(args[1], list)]
        self.assertEqual(len(finished), 1)
        return finished[0]

    def search(self, term):
        self.callbacks.clear()
        namespace["_search_worker"](self.ui, str(self.folder), term)
        result = self.finish_args()
        namespace["_search_finished"](self.ui, *result)
        self.assertFalse(self.ui.searching)
        self.ui.search_button.set_label.assert_called_with("Ara")
        self.ui.search_button.set_sensitive.assert_called_with(True)
        return result

    def test_nested_xml_and_uppercase_extension(self):
        path = self.document("nested/test.UDF", "<root><content>karar</content></root>")
        self.assertEqual(self.search("karar"), (1, [str(path)], 0, 0, False, False))

    def test_turkish_case_and_unicode(self):
        path = self.document("test.udf", "<root>İSTANBUL HAKİM IŞIK</root>")
        for term in ("istanbul", "hakim", "ışık", "I\u0307STANBUL"):
            with self.subTest(term=term):
                self.assertEqual(self.search(term), (1, [str(path)], 0, 0, False, False))
        self.assertEqual(self.search("isik"), (1, [], 0, 0, False, False))

    def test_broken_deflate_does_not_stop_other_files(self):
        broken = self.document("broken.udf", "<root>karar</root>")
        data = bytearray(broken.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", data, 26)
        data[30 + name_len + extra_len] = 7
        broken.write_bytes(data)
        good = self.document("good.udf", "<root>karar</root>")
        self.assertEqual(self.search("karar"), (1, [str(good)], 1, 0, False, False))
        self.assertIn("1 dosya okunamadı", self.ui.status.set_text.call_args.args[0])

    def test_invalid_zip_and_missing_content(self):
        (self.folder / "invalid.udf").write_text("not a zip")
        with zipfile.ZipFile(self.folder / "missing.udf", "w") as archive:
            archive.writestr("other.xml", "<root/>")
        self.assertEqual(self.search("karar"), (1, [], 2, 0, False, False))

    def test_malformed_xml_fallback(self):
        path = self.document("old.udf", "<root>karar")
        self.assertEqual(self.search("karar"), (1, [str(path)], 0, 0, False, False))

    def test_unreadable_directory_is_reported(self):
        original_walk = os.walk
        def denied_walk(folder, **kwargs):
            kwargs["onerror"](PermissionError("permission denied"))
            yield from original_walk(folder, **kwargs)
        with patch.object(os, "walk", denied_walk):
            self.assertEqual(self.search("karar"), (1, [], 0, 1, False, False))
        message = self.ui.status.set_text.call_args.args[0]
        self.assertIn("kısmen", message)
        self.assertIn("1 klasör taranamadı", message)

    def test_unexpected_error_restores_button(self):
        with patch.object(os, "walk", side_effect=ValueError("unexpected")):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.search("karar"), (1, [], 0, 0, True, False))
        self.assertIn("tarama durdu", self.ui.status.set_text.call_args.args[0])

    def test_completion_error_restores_button(self):
        self.ui.model.append.side_effect = ValueError("model failure")
        with self.assertRaises(ValueError):
            namespace["_search_finished"](self.ui, 1, ["file.udf"], 0)
        self.assertFalse(self.ui.searching)
        self.ui.search_button.set_label.assert_called_with("Ara")
        self.ui.search_button.set_sensitive.assert_called_with(True)

    def test_progress_reports_scanned_and_matches(self):
        self.document("a.udf", "<root>karar</root>")
        self.callbacks.clear()
        namespace["_search_worker"](self.ui, str(self.folder), "karar")
        progress = [args for args in self.callbacks if len(args) == 3]
        self.assertEqual(progress, [(1, 1, 1)])
        namespace["_search_progress"](self.ui, 1, 1, 1)
        self.assertEqual(
            self.ui.status.set_text.call_args.args[0],
            "1 dosya tarandı, 1 eşleşme",
        )

    def test_cancel_stops_and_keeps_partial_matches(self):
        self.document("one.udf", "<root>karar</root>")
        self.document("two.udf", "<root>karar</root>")
        self.document("three.udf", "<root>karar</root>")
        original_read = zipfile.ZipFile.read
        seen = {"n": 0}

        def read_and_cancel(archive, name):
            data = original_read(archive, name)
            seen["n"] += 1
            if seen["n"] >= 1:
                self.ui._cancel_event.set()
            return data

        self.callbacks.clear()
        with patch.object(zipfile.ZipFile, "read", read_and_cancel):
            namespace["_search_worker"](self.ui, str(self.folder), "karar")
        result = self.finish_args()
        self.assertTrue(result[-1])
        self.assertLess(len(result[1]), 3)
        namespace["_search_finished"](self.ui, *result)
        self.assertIn("durduruldu", self.ui.status.set_text.call_args.args[0])

    def test_stale_generation_does_not_apply_results(self):
        self.ui._search_generation = 2
        namespace["_search_finished"](self.ui, 1, ["stale.udf"], 0, 0, False, False)
        self.ui.model.append.assert_not_called()
        self.assertTrue(self.ui.searching)


class MainGuardTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

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


if __name__ == "__main__":
    unittest.main()
