import ast
import logging
import lzma
import os
from pathlib import Path
import struct
import tempfile
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
nodes += [n for n in window.body if isinstance(n, ast.FunctionDef) and n.name in ("_search_worker", "_search_finished")]
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
        self.ui = types.SimpleNamespace(_search_finished=Mock(), model=Mock(),
                                        status=Mock(), search_button=Mock(), searching=True)

    def document(self, name, text):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("content.xml", text)
        return path

    def search(self, term):
        namespace["_search_worker"](self.ui, str(self.folder), term)
        self.assertEqual(len(self.callbacks), 1)
        result = self.callbacks.pop()
        namespace["_search_finished"](self.ui, *result)
        self.assertFalse(self.ui.searching)
        self.ui.search_button.set_sensitive.assert_called_with(True)
        return result

    def test_nested_xml_and_uppercase_extension(self):
        path = self.document("nested/test.UDF", "<root><content>karar</content></root>")
        self.assertEqual(self.search("karar"), ([str(path)], 0, 0, False))

    def test_turkish_case_and_unicode(self):
        path = self.document("test.udf", "<root>İSTANBUL HAKİM IŞIK</root>")
        for term in ("istanbul", "hakim", "ışık", "I\u0307STANBUL"):
            with self.subTest(term=term):
                self.assertEqual(self.search(term), ([str(path)], 0, 0, False))
        self.assertEqual(self.search("isik"), ([], 0, 0, False))

    def test_broken_deflate_does_not_stop_other_files(self):
        broken = self.document("broken.udf", "<root>karar</root>")
        data = bytearray(broken.read_bytes())
        name_len, extra_len = struct.unpack_from("<HH", data, 26)
        data[30 + name_len + extra_len] = 7
        broken.write_bytes(data)
        good = self.document("good.udf", "<root>karar</root>")
        self.assertEqual(self.search("karar"), ([str(good)], 1, 0, False))
        self.assertIn("1 dosya okunamadı", self.ui.status.set_text.call_args.args[0])

    def test_invalid_zip_and_missing_content(self):
        (self.folder / "invalid.udf").write_text("not a zip")
        with zipfile.ZipFile(self.folder / "missing.udf", "w") as archive:
            archive.writestr("other.xml", "<root/>")
        self.assertEqual(self.search("karar"), ([], 2, 0, False))

    def test_malformed_xml_fallback(self):
        path = self.document("old.udf", "<root>karar")
        self.assertEqual(self.search("karar"), ([str(path)], 0, 0, False))

    def test_unreadable_directory_is_reported(self):
        original_walk = os.walk
        def denied_walk(folder, **kwargs):
            kwargs["onerror"](PermissionError("permission denied"))
            yield from original_walk(folder, **kwargs)
        with patch.object(os, "walk", denied_walk):
            self.assertEqual(self.search("karar"), ([], 0, 1, False))
        message = self.ui.status.set_text.call_args.args[0]
        self.assertIn("kısmen", message)
        self.assertIn("1 klasör taranamadı", message)

    def test_unexpected_error_restores_button(self):
        with patch.object(os, "walk", side_effect=ValueError("unexpected")):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.search("karar"), ([], 0, 0, True))
        self.assertIn("tarama durdu", self.ui.status.set_text.call_args.args[0])

    def test_completion_error_restores_button(self):
        self.ui.model.append.side_effect = ValueError("model failure")
        with self.assertRaises(ValueError):
            namespace["_search_finished"](self.ui, ["file.udf"], 0)
        self.assertFalse(self.ui.searching)
        self.ui.search_button.set_sensitive.assert_called_with(True)


if __name__ == "__main__":
    unittest.main()
