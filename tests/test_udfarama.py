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
from udfarama.autostart import (
    background_command,
    read_start_at_login,
    set_start_at_login,
    sync_start_at_login,
)
from udfarama.memory import (
    FolderMemory,
    iter_directories,
    load_index,
    refresh_paths,
    save_index,
    scan_folder,
)
from udfarama.search import (
    MatchItem,
    SearchOutcome,
    extract_snippet,
    find_normalized_span,
    format_search_status,
    format_search_status_parts,
    format_snippet_markup,
    make_match_item,
    normalize_search_text,
    search_records,
    search_udf_folder,
    should_apply_search_generation,
    should_live_search,
    text_matches,
    UdfDocument,
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

    def test_extract_snippet_centers_late_match(self):
        line = "önce " + ("x" * 100) + " karar " + ("y" * 100)
        snippet = extract_snippet(line, "karar", max_len=80)
        self.assertEqual(len(snippet), 80)
        self.assertIn("karar", snippet)
        self.assertTrue(snippet.startswith("…") or snippet.endswith("…"))

    def test_format_snippet_markup_highlights_term(self):
        markup = format_snippet_markup("önce karar sonra", "karar")
        self.assertEqual(markup, "önce <b>karar</b> sonra")
        escaped = format_snippet_markup("a <b> karar", "karar")
        self.assertIn("&lt;b&gt;", escaped)
        self.assertIn("<b>karar</b>", escaped)
        self.assertIsNotNone(find_normalized_span("İSTANBUL", normalize_search_text("istanbul")))
        self.assertIn("<b>", format_snippet_markup("İSTANBUL", "istanbul"))

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
        self.assertEqual(primary, "Arama tamamlandı: 0 sonuç.")
        self.assertIn("content.xml yok", detail)
        self.assertIn("bozuk veya açılamadı", detail)
        self.assertNotIn("content.xml", primary)

    def test_generation_gate(self):
        self.assertTrue(should_apply_search_generation(3, 3))
        self.assertFalse(should_apply_search_generation(3, 2))

    def test_wildcard_lists_matching_phrases(self):
        barolar = self.document("barolar.udf", "<root>Türkiye Barolar Birliği kararı</root>")
        bankalar = self.document("alt/bankalar.udf", "<root>Türkiye Bankalar Birliği duyurusu</root>")
        self.document("meclis.udf", "<root>Türkiye Büyük Millet Meclisi</root>")
        self.document("ters.udf", "<root>Birliği Türkiye</root>")
        outcome = self.search("tür* birliği")
        self.assertEqual(
            sorted(match.path for match in outcome.matches),
            sorted([str(barolar), str(bankalar)]),
        )
        self.assertIn("Türkiye Barolar Birliği", outcome.matches[0].snippet + outcome.matches[1].snippet)

    def test_wildcard_ignores_case_and_regex_chars(self):
        path = self.document("madde.udf", "<root>madde (1) fıkra metni</root>")
        self.assertEqual([match.path for match in self.search("TÜR* BİRLİĞİ").matches], [])
        self.document("birlik.udf", "<root>TÜRKİYE BAROLAR BİRLİĞİ</root>")
        folded = self.search("tür* birliği")
        self.assertEqual([match.basename for match in folded.matches], ["birlik.udf"])
        literal = self.search("madde (1)")
        self.assertEqual([match.path for match in literal.matches], [str(path)])
        wildcard = self.search("madde (1)*fıkra")
        self.assertEqual([match.path for match in wildcard.matches], [str(path)])
        self.assertEqual(self.search("madde (2)*fıkra").matches, [])

    def test_wildcard_markup_highlights_span(self):
        markup = format_snippet_markup("önce Türkiye Bankalar Birliği sonra", "tür* birliği")
        self.assertEqual(markup, "önce <b>Türkiye Bankalar Birliği</b> sonra")
        self.assertTrue(text_matches("Türkiye Barolar Birliği", "tür* birliği"))
        self.assertFalse(text_matches("Türkiye Büyük Millet Meclisi", "tür* birliği"))

    def test_memory_tracks_nested_add_and_remove(self):
        self.document("alt/bir.udf", "<root>Türkiye Barolar Birliği</root>")
        memory = FolderMemory()
        generation = memory.begin(str(self.folder))
        result = scan_folder(str(self.folder), generation, memory, threading.Event())
        self.assertTrue(result.completed)
        self.assertEqual(result.file_count, 1)
        records, directory_errors, snap_generation = memory.snapshot()
        found = search_records(records, str(self.folder), "tür* birliği", directory_errors=directory_errors)
        self.assertEqual(len(found.matches), 1)
        added = self.document("iki.udf", "<root>Türkiye Bankalar Birliği</root>")
        refresh_paths(memory, generation, str(self.folder), [str(added)], [])
        records, directory_errors, snap_generation = memory.snapshot()
        self.assertEqual(snap_generation, generation)
        found = search_records(records, str(self.folder), "tür* birliği", directory_errors=directory_errors)
        self.assertEqual(sorted(match.basename for match in found.matches), ["bir.udf", "iki.udf"])
        added.unlink()
        refresh_paths(memory, generation, str(self.folder), [], [str(added)])
        records, _, _ = memory.snapshot()
        found = search_records(records, str(self.folder), "tür* birliği")
        self.assertEqual([match.basename for match in found.matches], ["bir.udf"])
        memory.begin(str(self.folder))
        self.assertFalse(memory.is_current(generation))

    def test_saved_index_reloads_without_reading_the_folder_again(self):
        path = self.document("alt/bir.udf", "<root>Türkiye Barolar Birliği</root>")
        memory = FolderMemory()
        generation = memory.begin(str(self.folder))
        result = scan_folder(str(self.folder), generation, memory, threading.Event())
        self.assertTrue(result.completed)
        store = self.folder / "store"
        self.assertTrue(save_index(store, memory, generation))
        path.unlink()
        loaded = load_index(store, str(self.folder))
        self.assertIsNotNone(loaded)
        documents, errors = loaded
        restored = FolderMemory()
        restored.restore(str(self.folder), documents, errors)
        records, directory_errors, _generation = restored.snapshot()
        found = search_records(records, str(self.folder), "barolar", directory_errors=directory_errors)
        self.assertEqual([match.basename for match in found.matches], ["bir.udf"])
        self.assertIsNone(load_index(store, str(self.folder / "baska")))
        (store / "index.json").write_text("{", encoding="utf-8")
        self.assertIsNone(load_index(store, str(self.folder)))

    def test_saved_index_keeps_a_change_made_after_the_first_scan(self):
        self.document("bir.udf", "<root>Türkiye Barolar Birliği</root>")
        memory = FolderMemory()
        generation = memory.begin(str(self.folder))
        scan_folder(str(self.folder), generation, memory, threading.Event())
        added = self.document("iki.udf", "<root>Türkiye Bankalar Birliği</root>")
        self.assertTrue(refresh_paths(memory, generation, str(self.folder), [str(added)], []))
        store = self.folder / "store"
        self.assertTrue(save_index(store, memory, generation))
        documents, _errors = load_index(store, str(self.folder))
        self.assertEqual(sorted(Path(item.path).name for item in documents), ["bir.udf", "iki.udf"])

    def test_directory_walk_lists_nested_folders_and_skips_links(self):
        (self.folder / "alt" / "derin").mkdir(parents=True)
        with tempfile.TemporaryDirectory() as outside_name:
            outside = Path(outside_name)
            link = self.folder / "bag"
            link.symlink_to(outside, target_is_directory=True)
            listed = set(iter_directories(str(self.folder)))
        self.assertIn(str(self.folder), listed)
        self.assertIn(str(self.folder / "alt" / "derin"), listed)
        self.assertNotIn(str(outside), listed)
        self.assertNotIn(str(link), listed)

    def test_live_search_starts_at_four_characters(self):
        self.assertFalse(should_live_search("kar"))
        self.assertFalse(should_live_search("  kar "))
        self.assertTrue(should_live_search("kara"))
        self.assertTrue(should_live_search(" tür*"))

    def test_search_records_lists_name_and_content_once(self):
        folder = "/docs"
        records = [
            UdfDocument("/docs/türkiye-bankalar.udf", "başka metin", 0, 0, 0, 0),
            UdfDocument("/docs/dilekce.udf", "Türkiye Bankalar Birliği", 0, 0, 0, 0),
            UdfDocument("/docs/bankalar.udf", "Türkiye Bankalar Birliği", 0, 0, 0, 0),
            UdfDocument("/docs/gizli.udf", "Türkiye Bankalar Birliği", 0, 0, 1, 0),
        ]
        found = search_records(records, folder, "banka")
        by_name = {item.basename: item for item in found.matches}
        self.assertEqual(
            set(by_name),
            {"türkiye-bankalar.udf", "dilekce.udf", "bankalar.udf"},
        )
        self.assertEqual(by_name["türkiye-bankalar.udf"].snippet, "")
        self.assertIn("Bankalar", by_name["dilekce.udf"].snippet)
        self.assertIn("Bankalar", by_name["bankalar.udf"].snippet)
        self.assertEqual(found.permission_errors, 1)
        named = search_records(records[:1], folder, "tür*banka")
        self.assertEqual(named.matches[0].basename, "türkiye-bankalar.udf")
        self.assertEqual(named.matches[0].snippet, "")

    def test_rescanning_a_folder_does_not_inflate_directory_errors(self):
        memory = FolderMemory()
        generation = memory.begin(str(self.folder))
        memory.publish(generation, [], 1)
        memory.note_directory_errors(generation, str(self.folder), 1)
        memory.note_directory_errors(generation, str(self.folder), 1)
        _records, errors, _generation = memory.snapshot()
        self.assertEqual(errors, 1)


class WatchTests(unittest.TestCase):
    def test_classify_create_and_delete(self):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
        except (ImportError, ValueError):
            self.skipTest("Gio unavailable")
        from udfarama.watch import classify_monitor_event
        changed, removed = classify_monitor_event(Gio.FileMonitorEvent.CREATED, "/belgeler/yeni.udf")
        self.assertEqual(changed, {"/belgeler/yeni.udf"})
        self.assertEqual(removed, set())
        changed, removed = classify_monitor_event(Gio.FileMonitorEvent.DELETED, "/belgeler/yeni.udf")
        self.assertEqual(changed, set())
        self.assertEqual(removed, {"/belgeler/yeni.udf"})
        changed, removed = classify_monitor_event(
            Gio.FileMonitorEvent.RENAMED, "/belgeler/eski.udf", "/belgeler/yeni.udf"
        )
        self.assertEqual(removed, {"/belgeler/eski.udf"})
        self.assertEqual(changed, {"/belgeler/yeni.udf"})

    def test_tree_watch_reports_new_file(self):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import GLib
        except (ImportError, ValueError):
            self.skipTest("Gio unavailable")
        from udfarama.watch import TreeWatch
        with tempfile.TemporaryDirectory() as tmp:
            seen = {}
            loop = GLib.MainLoop()

            def on_batch(changed, removed):
                seen["changed"] = changed
                seen["removed"] = removed
                loop.quit()

            watch = TreeWatch(on_batch, delay_ms=40)
            watch.watch(tmp)

            def create_file():
                (Path(tmp) / "yeni.udf").write_text("x")
                return GLib.SOURCE_REMOVE

            def give_up():
                seen["timeout"] = True
                loop.quit()
                return GLib.SOURCE_REMOVE

            GLib.timeout_add(30, create_file)
            GLib.timeout_add(1500, give_up)
            loop.run()
            watch.clear()
            self.assertFalse(seen.get("timeout"), "klasör izleyicisi dosya oluşturmayı görmedi")
            self.assertTrue(any(path.endswith("yeni.udf") for path in seen.get("changed", set())))


class FlightAndTrayTests(unittest.TestCase):
    def test_locate_icon_uses_the_badge_instead_of_the_left_edge(self):
        from PIL import Image
        from udfarama.flight import icon_slot, locate_icon
        icon = Image.open(ROOT / "data/app-icons/hicolor/512x512/apps/udfarama.png")
        small = icon.resize((22, 22))
        canvas = Image.new("RGB", (160, 30), (24, 28, 36))
        canvas.paste(small, (96, 4), small)
        found = locate_icon(canvas, icon)
        self.assertIsNotNone(found)
        self.assertGreater(found[0], 80)
        self.assertLess(abs(found[0] - 96), 4)
        slot = icon_slot((1381, 8, 160, 30), canvas, icon)
        self.assertGreater(slot[0], 1381 + 80)

    def test_flight_moves_from_the_window_toward_the_corner(self):
        from udfarama.flight import ICON_LEAD, icon_corner, icon_motion, icon_opacity, icon_start, mix_rect, window_fade
        self.assertEqual(window_fade(0), 1)
        self.assertEqual(window_fade(ICON_LEAD), 0)
        self.assertEqual(window_fade(1), 0)
        self.assertEqual(icon_motion(ICON_LEAD), 0)
        self.assertGreater(icon_motion(ICON_LEAD + 0.2), 0)
        start = icon_start((100, 80, 528, 349), 64)
        corner = icon_corner((0, 0, 1920, 1040), 64, 1)
        self.assertEqual(mix_rect(start, corner, icon_motion(ICON_LEAD)), start)
        self.assertEqual(mix_rect(start, corner, 1), corner)
        self.assertEqual(icon_opacity(0.5), 1)
        self.assertEqual(icon_opacity(1), 0)

    def test_native_minimize_is_taken_out_of_the_titlebar(self):
        from udfarama.app import decoration_without_minimize, minimize_is_on_end
        layout = "icon:minimize,maximize,close"
        self.assertTrue(minimize_is_on_end(layout))
        self.assertEqual(decoration_without_minimize(layout), "icon:maximize,close")
        left_sided = "close,minimize,maximize:"
        self.assertFalse(minimize_is_on_end(left_sided))
        self.assertEqual(decoration_without_minimize(left_sided), "close,maximize:")

    def test_tray_menu_says_open(self):
        try:
            import gi
            gi.require_version("Gio", "2.0")
        except (ImportError, ValueError):
            self.skipTest("Gio unavailable")
        from udfarama.tray import menu_layout
        text = str(menu_layout())
        self.assertIn("Aç", text)
        self.assertNotIn("Pencereyi aç", text)
        self.assertLess(text.index("Ayarlar"), text.index("Hakkında"))
        self.assertLess(text.index("Hakkında"), text.index("Çıkış"))


class AutostartTests(unittest.TestCase):
    def test_setting_writes_and_removes_desktop_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            autostart = root / "autostart"
            enabled = sync_start_at_login(config, autostart, "udfarama --background")
            desktop = autostart / "com.adalettekno.UDFarama.desktop"
            self.assertTrue(enabled)
            self.assertIn("Exec=udfarama --background", desktop.read_text(encoding="utf-8"))
            self.assertTrue(read_start_at_login(config))
            set_start_at_login(False, config, autostart, "udfarama --background")
            self.assertFalse(desktop.exists())
            self.assertFalse(read_start_at_login(config))
            set_start_at_login(True, config, autostart, "udfarama --background")
            self.assertTrue(desktop.is_file())

    def test_background_command_uses_tray_flag(self):
        command = background_command()
        self.assertIn("--background", command)
        self.assertIn("udfarama", command)


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


class VersionSyncTests(unittest.TestCase):
    def test_metainfo_release_matches_package_version(self):
        import re
        from udfarama import __version__
        text = (ROOT / "packaging/com.adalettekno.UDFarama.metainfo.xml").read_text()
        match = re.search(r'<release\s+version="([^"]+)"', text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), __version__)


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

    def test_adw_window_smoke(self):
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            gi.require_version("Adw", "1")
            from gi.repository import GLib
        except (ImportError, ValueError):
            self.skipTest("GTK/Adw unavailable")
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            self.skipTest("No display")
        from udfarama.app import APP_NAME, UDFaramaApp
        config = tempfile.TemporaryDirectory()
        self.addCleanup(config.cleanup)
        os.environ["UDFARAMA_CONFIG_DIR"] = config.name
        os.environ["UDFARAMA_AUTOSTART_DIR"] = str(Path(config.name) / "autostart")
        seen = {}
        app = UDFaramaApp()

        def quit_check():
            win = app.props.active_window
            seen["ok"] = win is not None and APP_NAME in (win.get_title() or "")
            seen["focus"] = hasattr(win, "search_entry") if win else False
            seen["minimize"] = (
                win is not None
                and "minimize" not in (win._title_layout or "")
                and win._minimize_button.get_tooltip_text() == "Simge durumuna küçült"
            )
            if win is not None:
                win.show_settings()
                settings = win._settings_window
                seen["settings"] = (
                    settings is not None
                    and "minimize" not in (settings._title_layout or "")
                    and "maximize" not in (settings._title_layout or "")
                )
                win.show_about()
                seen["about"] = win._about_window is not None and win._about_window.get_visible()
                from gi.repository import Gdk, Gtk
                theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
                looked_up = theme.lookup_icon(
                    "com.adalettekno.UDFarama", None, 128, 1, Gtk.TextDirection.NONE, 0)
                icon_path = ""
                if looked_up is not None and looked_up.get_file() is not None:
                    icon_path = looked_up.get_file().get_path() or ""
                seen["about_icon"] = icon_path.endswith("com.adalettekno.UDFarama.png")
            desktop = Path(config.name) / "autostart" / "com.adalettekno.UDFarama.desktop"
            seen["autostart"] = desktop.is_file() and "--background" in desktop.read_text(encoding="utf-8")
            app.quit()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(400, quit_check)
        self.assertEqual(app.run([]), 0)
        self.assertTrue(seen.get("ok"))
        self.assertTrue(seen.get("focus"))
        self.assertTrue(seen.get("minimize"))
        self.assertTrue(seen.get("settings"))
        self.assertTrue(seen.get("about"))
        self.assertTrue(seen.get("about_icon"))
        self.assertTrue(seen.get("autostart"))


if __name__ == "__main__":
    unittest.main()
