import ast
import logging
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from udfarama.autostart import read_start_at_login, set_start_at_login, sync_start_at_login
from udfarama.logging_setup import configure_logging
from udfarama.search import (
    extract_snippet,
    find_normalized_span,
    format_snippet_markup,
    normalize_search_text,
    read_udf_document,
    search_udf_folder,
    should_live_search,
    text_matches,
)


ROOT = Path(__file__).resolve().parents[1]


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def udf(self, name, content):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("content.xml", content)
        return path

    def test_turkish_case(self):
        self.udf("a.UDF", "<root>İSTANBUL HAKİM IŞIK</root>")
        for term in ("istanbul", "hakim", "ışık"):
            self.assertEqual(len(search_udf_folder(str(self.folder), term).matches), 1)
        self.assertEqual(normalize_search_text("I"), "ı")
        self.assertEqual(search_udf_folder(str(self.folder), "isik").matches, [])

    def test_wildcard_and_literal(self):
        self.udf("barolar.udf", "<root>Türkiye Barolar Birliği</root>")
        self.udf("bankalar.udf", "<root>Türkiye Bankalar Birliği</root>")
        self.assertEqual(len(search_udf_folder(str(self.folder), "tür* birliği").matches), 2)
        self.assertTrue(text_matches("Türkiye Barolar Birliği", "tür* birliği"))
        self.assertFalse(text_matches("Türkiye Büyük Millet Meclisi", "tür* birliği"))

    def test_highlight_escapes_markup(self):
        text = "a <b> İSTANBUL karar"
        self.assertIn("&lt;b&gt;", format_snippet_markup(text, "karar"))
        self.assertIn("<b>İSTANBUL</b>", format_snippet_markup(text, "istanbul"))
        self.assertEqual(find_normalized_span("İSTANBUL", "istanbul"), (0, 8))

    def test_long_snippet(self):
        text = "x" * 1000 + "karar" + "y" * 1000
        snippet = extract_snippet(text, "karar", max_len=80)
        self.assertIn("karar", snippet)
        self.assertEqual(len(snippet), 80)

    def test_missing_xml_and_malformed_zip(self):
        with zipfile.ZipFile(self.folder / "missing.udf", "w") as archive:
            archive.writestr("other.xml", "<root/>")
        (self.folder / "invalid.udf").write_text("invalid", encoding="utf-8")
        self.assertEqual(read_udf_document(str(self.folder / "missing.udf")).missing_content, 1)
        self.assertEqual(read_udf_document(str(self.folder / "invalid.udf")).bad_archive, 1)

    def test_unfinished_xml_fallback(self):
        path = self.udf("badxml.udf", "<root>karar")
        document = read_udf_document(str(path))
        self.assertEqual(document.parse_error, 1)
        self.assertIn("karar", document.text)

    def test_live_search_minimum(self):
        self.assertFalse(should_live_search("kar"))
        self.assertTrue(should_live_search("kara"))


class SourceTests(unittest.TestCase):
    def test_app_does_not_use_flight_animation(self):
        app = (ROOT / "src/udfarama/app.py").read_text(encoding="utf-8")
        window = (ROOT / "src/udfarama/window.py").read_text(encoding="utf-8")
        self.assertNotIn("IconFlight", app + window)
        self.assertNotIn("flight_progress", app + window)
        self.assertFalse((ROOT / "src/udfarama/flight.py").exists())

    def test_source_files_have_no_comments_or_docstrings(self):
        import io
        import tokenize
        for file in (ROOT / "src/udfarama").rglob("*.py"):
            with self.subTest(file=file):
                text = file.read_text(encoding="utf-8")
                comments = [t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT]
                self.assertEqual(comments, [])
                tree = ast.parse(text)
                self.assertIsNone(ast.get_docstring(tree))

    def test_app_module_only_starts_under_entrypoint(self):
        text = (ROOT / "src/udfarama/__main__.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        self.assertTrue(any(isinstance(node, ast.If) for node in tree.body))


class AutostartTests(unittest.TestCase):
    def test_background_autostart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            start = Path(tmp) / "startup"
            self.assertTrue(sync_start_at_login(config, start, "udfarama --background"))
            self.assertTrue((start / "com.adalettekno.UDFarama.desktop").exists())
            set_start_at_login(False, config, start, "udfarama --background")
            self.assertFalse(read_start_at_login(config))
            self.assertFalse((start / "com.adalettekno.UDFarama.desktop").exists())


class UITests(unittest.TestCase):
    def test_window_smoke(self):
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
            from gi.repository import GLib
        except (ImportError, ValueError):
            self.skipTest("GTK4/Adw unavailable")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            self.skipTest("No graphical display")
        from udfarama.app import UDFaramaApp
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "UDFARAMA_CONFIG_DIR": tmp,
                "UDFARAMA_AUTOSTART_DIR": str(Path(tmp) / "autostart"),
            }):
                app = UDFaramaApp()
                observed = {}
                def inspect():
                    window = app.props.active_window
                    observed["ready"] = bool(
                        window and window.search_entry and window.more_button
                        and window.state_icon and window.list_view
                    )
                    app.quit()
                    return GLib.SOURCE_REMOVE
                GLib.timeout_add(550, inspect)
                self.assertEqual(app.run([]), 0)
                self.assertTrue(observed.get("ready"))


if __name__ == "__main__":
    unittest.main()
