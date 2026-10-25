import os
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib, GObject, Pango, Gdk, Adw

from udfarama.logging_setup import configure_logging
from udfarama.search import (
    format_search_status_parts,
    search_udf_folder,
    should_apply_search_generation,
)


APP_ID = "com.adalettekno.UDFarama"
APP_NAME = "UDFarama"


class SearchResult(GObject.Object):
    __gtype_name__ = "UDFaramaSearchResult"

    path = GObject.Property(type=str, default="")
    relative_path = GObject.Property(type=str, default="")
    basename = GObject.Property(type=str, default="")
    snippet = GObject.Property(type=str, default="")

    @classmethod
    def from_match(cls, item):
        return cls(
            path=item.path,
            relative_path=item.relative_path,
            basename=item.basename,
            snippet=item.snippet,
        )


class UdfSearchWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(980, 680)
        self.selected_folder = None
        self.searching = False
        self._has_searched = False
        self._cancel_event = threading.Event()
        self._search_generation = 0
        self._menu_result = None
        self._result_popover = None
        self._progress_lock = threading.Lock()
        self._progress_pending = None
        self._progress_scheduled = False
        self._pulse_source = None

        self._build_actions()

        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_label = Gtk.Label(label=APP_NAME)
        self.title_label.add_css_class("title")
        header.set_title_widget(self.title_label)

        self.folder_label = Gtk.Label(label="Arama klasörü seçilmedi", xalign=0)
        self.folder_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.folder_label.set_max_width_chars(28)
        choose_btn = Gtk.Button(label="Klasör Seç")
        choose_btn.set_action_name("win.choose-folder")
        choose_btn.set_tooltip_text("Arama klasörü seç (Ctrl+O)")
        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_box.append(self.folder_label)
        folder_box.append(choose_btn)
        header.pack_end(folder_box)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Aranacak kelime veya ifade")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("activate", self.start_search)
        self.search_entry.set_tooltip_text("Arama ifadesi")
        search_row.append(self.search_entry)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        search_row.append(self.spinner)

        self.search_button = Gtk.Button(label="Ara")
        self.search_button.add_css_class("suggested-action")
        self.search_button.connect("clicked", self.on_search_button)
        self.search_button.set_tooltip_text("Ara veya taramayı durdur")
        search_row.append(self.search_button)
        content.append(search_row)

        self.progress = Gtk.ProgressBar()
        self.progress.set_visible(False)
        self.progress.set_pulse_step(0.1)
        content.append(self.progress)

        self.status = Gtk.Label(label="Önce bir klasör seçin.", xalign=0)
        self.status.add_css_class("dim-label")
        self.status.set_wrap(True)
        content.append(self.status)

        self.model = Gio.ListStore.new(SearchResult)
        self.list_view = Gtk.ListView.new(
            Gtk.SingleSelection.new(self.model),
            Gtk.SignalListItemFactory()
        )
        factory = self.list_view.get_factory()
        factory.connect("setup", self._setup_item)
        factory.connect("bind", self._bind_item)
        self.list_view.connect("activate", self.open_selected)
        self.list_view.set_tab_behavior(Gtk.ListTabBehavior.ITEM)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self.list_view)

        self.empty_page = Adw.StatusPage(
            icon_name="system-search-symbolic",
            title="Henüz arama yok",
            description="Bir klasör seçip aranacak ifadeyi girin, ardından Ara’ya basın.",
        )
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.empty_page, "empty")
        self.stack.add_named(scrolled, "results")
        self.stack.set_visible_child_name("empty")
        content.append(self.stack)

        toolbar.set_content(content)
        self.toast_overlay.set_child(toolbar)
        self.set_content(self.toast_overlay)

        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.MANAGED)
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.NamedAction.new("win.cancel-search"),
        ))
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>o"),
            Gtk.NamedAction.new("win.choose-folder"),
        ))
        self.add_controller(shortcuts)

        self.load_saved_folder()
        self._update_empty_state()
        self._update_title_count(0)

    def _build_actions(self):
        choose = Gio.SimpleAction.new("choose-folder", None)
        choose.connect("activate", lambda *_: self.choose_folder())
        self.add_action(choose)

        cancel = Gio.SimpleAction.new("cancel-search", None)
        cancel.connect("activate", lambda *_: self.stop_search())
        self.add_action(cancel)

        copy_path = Gio.SimpleAction.new("copy-result-path", None)
        copy_path.connect("activate", lambda *_: self._copy_menu_path())
        self.add_action(copy_path)

        open_folder = Gio.SimpleAction.new("open-result-folder", None)
        open_folder.connect("activate", lambda *_: self._open_menu_folder())
        self.add_action(open_folder)

        open_file = Gio.SimpleAction.new("open-result-file", None)
        open_file.connect("activate", lambda *_: self._open_menu_file())
        self.add_action(open_file)

    def _setup_item(self, factory, item):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
        title.set_attributes(attrs)
        relative = Gtk.Label(xalign=0)
        relative.add_css_class("dim-label")
        relative.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        snippet = Gtk.Label(xalign=0)
        snippet.add_css_class("dim-label")
        snippet.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title)
        box.append(relative)
        box.append(snippet)
        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)
        gesture.connect("pressed", self._on_item_secondary, item)
        box.add_controller(gesture)
        item.set_child(box)

    def _bind_item(self, factory, item):
        result = item.get_item()
        box = item.get_child()
        title = box.get_first_child()
        relative = title.get_next_sibling()
        snippet = relative.get_next_sibling()
        title.set_text(result.basename)
        relative.set_text(result.relative_path)
        if result.snippet:
            snippet.set_text(result.snippet)
            snippet.set_visible(True)
        else:
            snippet.set_text("")
            snippet.set_visible(False)
        box.set_tooltip_text(result.path)
        label = result.basename
        if result.relative_path:
            label = f"{result.basename}, {result.relative_path}"
        if result.snippet:
            label = f"{label}, {result.snippet}"
        box.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def _on_item_secondary(self, gesture, n_press, x, y, list_item):
        result = list_item.get_item()
        if result is None:
            return
        self._show_result_menu(result, list_item.get_child())

    def _show_result_menu(self, result, anchor):
        self._menu_result = result
        if self._result_popover is not None:
            self._result_popover.popdown()
            self._result_popover.unparent()
        menu = Gio.Menu()
        menu.append("Yolu kopyala", "win.copy-result-path")
        menu.append("İçeren klasörü aç", "win.open-result-folder")
        menu.append("Varsayılan uygulamada aç", "win.open-result-file")
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(anchor)
        popover.popup()
        self._result_popover = popover

    def _copy_path_text(self, path):
        self.get_clipboard().set(path)
        self._set_status_primary("Yol panoya kopyalandı.")

    def _copy_menu_path(self):
        if self._menu_result is None or not self._menu_result.path:
            return
        self._copy_path_text(self._menu_result.path)

    def _open_menu_folder(self):
        if self._menu_result is None or not self._menu_result.path:
            return
        folder = os.path.dirname(self._menu_result.path)
        if not folder or not os.path.isdir(folder):
            self.show_error("İçeren klasör açılamadı.")
            return
        self._launch_uri(Gio.File.new_for_path(folder).get_uri(), self._menu_result.path)

    def _open_menu_file(self):
        if self._menu_result is None or not self._menu_result.path:
            return
        self._open_path(self._menu_result.path)

    def _launch_uri(self, uri, fallback_path=None):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            if fallback_path:
                self._copy_path_text(fallback_path)
                self.show_error(
                    f"Dosya varsayılan uygulamayla açılamadı; yol panoya kopyalandı.\n{exc}")
            else:
                self.show_error(f"Konum açılamadı:\n{exc}")

    def _open_path(self, path):
        if not path or not os.path.isfile(path):
            return
        self._launch_uri(Gio.File.new_for_path(path).get_uri(), path)

    def show_error(self, message):
        dialog = Gtk.AlertDialog(message=message)
        dialog.show(self)

    def _set_status_primary(self, text):
        self.status.set_text(text)

    def _show_detail_toast(self, detail):
        if not detail:
            return
        toast = Adw.Toast.new(detail)
        toast.set_timeout(6)
        self.toast_overlay.add_toast(toast)

    def _update_title_count(self, count):
        if count > 0:
            self.title_label.set_text(f"{APP_NAME} — {count} sonuç")
            self.set_title(f"{APP_NAME} — {count} sonuç")
        else:
            self.title_label.set_text(APP_NAME)
            self.set_title(APP_NAME)

    def _set_folder_display(self, path):
        if not path:
            self.folder_label.set_text("Arama klasörü seçilmedi")
            self.folder_label.set_tooltip_text("")
            return
        name = os.path.basename(path.rstrip(os.sep)) or path
        self.folder_label.set_text(name)
        self.folder_label.set_tooltip_text(path)

    def _update_empty_state(self):
        if self.model.get_n_items() > 0:
            self.stack.set_visible_child_name("results")
            return
        self.stack.set_visible_child_name("empty")
        if self._has_searched:
            self.empty_page.set_title("Sonuç bulunamadı")
            self.empty_page.set_description(
                "Farklı bir ifade deneyin veya başka bir klasör seçin.")
        else:
            self.empty_page.set_title("Henüz arama yok")
            self.empty_page.set_description(
                "Bir klasör seçip aranacak ifadeyi girin, ardından Ara’ya basın.")

    def load_saved_folder(self):
        config_dir = Path(GLib.get_user_config_dir()) / APP_ID
        path_file = config_dir / "folder"
        try:
            path = path_file.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not path:
            return
        if os.path.isdir(path):
            self.selected_folder = path
            self._set_folder_display(path)
            self._set_status_primary("Kayıtlı klasör hazır. Arama yapabilirsiniz.")
            return
        self.selected_folder = None
        self._set_folder_display(None)
        self._set_status_primary("Kayıtlı klasör bulunamadı. Lütfen yeniden klasör seçin.")
        self.show_error("Kayıtlı arama klasörü artık yok veya erişilemiyor. Lütfen yeniden klasör seçin.")
        self.choose_folder()

    def save_folder(self, path):
        config_dir = Path(GLib.get_user_config_dir()) / APP_ID
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "folder").write_text(path + "\n", encoding="utf-8")

    def choose_folder(self, *_):
        dialog = Gtk.FileDialog(title="Arama yapılacak üst klasörü seçin")
        dialog.select_folder(self, None, self._folder_selected)

    def _folder_selected(self, dialog, result):
        try:
            file = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if file is None:
            return
        path = file.get_path()
        if not path:
            self.show_error("Seçilen konuma erişilebilir bir yerel yol alınamadı.")
            return
        if not os.path.isdir(path):
            self.show_error("Seçilen klasör geçerli değil. Lütfen yeniden seçin.")
            return
        self.selected_folder = path
        self._set_folder_display(path)
        self.save_folder(path)
        self._set_status_primary("Klasör seçildi. Aramak istediğiniz ifadeyi girin.")

    def on_search_button(self, *_):
        if self.searching:
            self.stop_search()
        else:
            self.start_search()

    def stop_search(self, *_):
        if not self.searching:
            return
        self._cancel_event.set()

    def _set_search_button_searching(self, searching):
        if searching:
            self.search_button.set_label("Durdur")
            self.search_button.remove_css_class("suggested-action")
            self.search_button.add_css_class("destructive-action")
            self.spinner.set_visible(True)
            self.spinner.start()
            self.progress.set_visible(True)
            self.progress.pulse()
            if self._pulse_source is None:
                self._pulse_source = GLib.timeout_add(120, self._pulse_progress)
        else:
            self.search_button.set_label("Ara")
            self.search_button.remove_css_class("destructive-action")
            self.search_button.add_css_class("suggested-action")
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.progress.set_visible(False)
            self.progress.set_fraction(0)
            if self._pulse_source is not None:
                GLib.source_remove(self._pulse_source)
                self._pulse_source = None
        self.search_button.set_sensitive(True)

    def _pulse_progress(self):
        if not self.searching:
            self._pulse_source = None
            return GLib.SOURCE_REMOVE
        self.progress.pulse()
        return GLib.SOURCE_CONTINUE

    def start_search(self, *_):
        if self.searching:
            return
        if not self.selected_folder or not os.path.isdir(self.selected_folder):
            self.selected_folder = None
            self._set_folder_display(None)
            self.show_error("Önce geçerli bir arama klasörü seçin.")
            self.choose_folder()
            return
        term = self.search_entry.get_text().strip()
        if not term:
            self.show_error("Aranacak ifade boş olamaz.")
            return

        self.searching = True
        self._cancel_event.clear()
        self._search_generation += 1
        with self._progress_lock:
            self._progress_pending = None
            self._progress_scheduled = False
        self._set_search_button_searching(True)
        self.model.splice(0, self.model.get_n_items(), [])
        self._update_empty_state()
        self._update_title_count(0)
        self._set_status_primary("UDF dosyaları taranıyor…")
        threading.Thread(target=self._search_worker, args=(self.selected_folder, term), daemon=True).start()

    def _search_worker(self, folder, term):
        generation = self._search_generation
        cancel_event = self._cancel_event

        def on_progress(scanned, match_count):
            with self._progress_lock:
                self._progress_pending = (generation, scanned, match_count)
                if self._progress_scheduled:
                    return
                self._progress_scheduled = True
            GLib.idle_add(self._flush_progress)

        outcome = search_udf_folder(
            folder, term, cancel_event=cancel_event, on_progress=on_progress)
        GLib.idle_add(self._search_finished, generation, outcome)

    def _flush_progress(self):
        with self._progress_lock:
            pending = self._progress_pending
            self._progress_pending = None
            self._progress_scheduled = False
        if pending is None:
            return GLib.SOURCE_REMOVE
        generation, scanned, match_count = pending
        if not should_apply_search_generation(self._search_generation, generation):
            return GLib.SOURCE_REMOVE
        if not self.searching:
            return GLib.SOURCE_REMOVE
        self._set_status_primary(f"{scanned} dosya tarandı, {match_count} eşleşme")
        self._update_title_count(match_count)
        return GLib.SOURCE_REMOVE

    def _search_finished(self, generation, outcome):
        if not should_apply_search_generation(self._search_generation, generation):
            return GLib.SOURCE_REMOVE
        try:
            self._has_searched = True
            for match in outcome.matches:
                self.model.append(SearchResult.from_match(match))
            primary, detail = format_search_status_parts(outcome)
            self._set_status_primary(primary)
            self._show_detail_toast(detail)
            self._update_title_count(len(outcome.matches))
            self._update_empty_state()
        finally:
            self.searching = False
            self._set_search_button_searching(False)
        return GLib.SOURCE_REMOVE

    def open_selected(self, list_view, position):
        result = self.model.get_item(position)
        if result is None:
            return
        self._open_path(result.path)


class UDFaramaApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = UdfSearchWindow(self)
        win.present()


def run_app(argv=None):
    configure_logging()
    return UDFaramaApp().run(argv)
