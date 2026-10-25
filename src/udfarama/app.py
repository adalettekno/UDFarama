import os
import threading
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GLib, GObject, Pango

from udfarama.search import format_search_status, search_udf_folder


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


class UdfSearchWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(980, 680)
        self.selected_folder = None
        self.searching = False
        self._cancel_event = threading.Event()
        self._search_generation = 0

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(18)
        root.set_margin_bottom(18)
        root.set_margin_start(18)
        root.set_margin_end(18)
        self.set_child(root)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        root.append(header)

        self.folder_label = Gtk.Label(label="Arama klasörü seçilmedi", xalign=0)
        self.folder_label.set_hexpand(True)
        self.folder_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        header.append(self.folder_label)

        choose_btn = Gtk.Button(label="Klasör Seç")
        choose_btn.connect("clicked", self.choose_folder)
        header.append(choose_btn)

        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Aranacak kelime veya ifade")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("activate", self.start_search)
        root.append(self.search_entry)

        self.search_button = Gtk.Button(label="Ara")
        self.search_button.connect("clicked", self.on_search_button)
        root.append(self.search_button)

        self.status = Gtk.Label(label="Önce bir klasör seçin.", xalign=0)
        root.append(self.status)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        root.append(scrolled)

        self.model = Gio.ListStore.new(SearchResult)
        self.list_view = Gtk.ListView.new(
            Gtk.SingleSelection.new(self.model),
            Gtk.SignalListItemFactory()
        )
        factory = self.list_view.get_factory()
        factory.connect("setup", self._setup_item)
        factory.connect("bind", self._bind_item)
        self.list_view.connect("activate", self.open_selected)
        scrolled.set_child(self.list_view)

        self.load_saved_folder()

    def _setup_item(self, factory, item):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle = Gtk.Label(xalign=0)
        subtitle.add_css_class("dim-label")
        subtitle.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        box.append(title)
        box.append(subtitle)
        item.set_child(box)

    def _bind_item(self, factory, item):
        result = item.get_item()
        box = item.get_child()
        title = box.get_first_child()
        subtitle = title.get_next_sibling()
        title.set_text(result.basename)
        subtitle.set_text(result.relative_path)

    def show_error(self, message):
        dialog = Gtk.AlertDialog(message=message)
        dialog.show(self)

    def load_saved_folder(self):
        config_dir = Path(GLib.get_user_config_dir()) / APP_ID
        path_file = config_dir / "folder"
        try:
            path = path_file.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if path and os.path.isdir(path):
            self.selected_folder = path
            self.folder_label.set_text(path)
            self.status.set_text("Kayıtlı klasör hazır. Arama yapabilirsiniz.")

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
        self.selected_folder = path
        self.folder_label.set_text(path)
        self.save_folder(path)
        self.status.set_text("Klasör seçildi. Aramak istediğiniz ifadeyi girin.")

    def on_search_button(self, *_):
        if self.searching:
            self.stop_search()
        else:
            self.start_search()

    def stop_search(self, *_):
        if not self.searching:
            return
        self._cancel_event.set()

    def start_search(self, *_):
        if self.searching:
            return
        if not self.selected_folder or not os.path.isdir(self.selected_folder):
            self.show_error("Önce geçerli bir arama klasörü seçin.")
            return
        term = self.search_entry.get_text().strip()
        if not term:
            self.show_error("Aranacak ifade boş olamaz.")
            return

        self.searching = True
        self._cancel_event.clear()
        self._search_generation += 1
        self.search_button.set_label("Durdur")
        self.search_button.set_sensitive(True)
        self.model.splice(0, self.model.get_n_items(), [])
        self.status.set_text("UDF dosyaları taranıyor…")
        threading.Thread(target=self._search_worker, args=(self.selected_folder, term), daemon=True).start()

    def _search_worker(self, folder, term):
        generation = self._search_generation
        cancel_event = self._cancel_event

        def on_progress(scanned, match_count):
            GLib.idle_add(self._search_progress, generation, scanned, match_count)

        outcome = search_udf_folder(
            folder, term, cancel_event=cancel_event, on_progress=on_progress)
        GLib.idle_add(self._search_finished, generation, outcome)

    def _search_progress(self, generation, scanned, match_count):
        if generation != self._search_generation or not self.searching:
            return GLib.SOURCE_REMOVE
        self.status.set_text(f"{scanned} dosya tarandı, {match_count} eşleşme")
        return GLib.SOURCE_REMOVE

    def _search_finished(self, generation, outcome):
        if generation != self._search_generation:
            return GLib.SOURCE_REMOVE
        try:
            for match in outcome.matches:
                self.model.append(SearchResult.from_match(match))
            self.status.set_text(format_search_status(outcome))
        finally:
            self.searching = False
            self.search_button.set_label("Ara")
            self.search_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def open_selected(self, list_view, position):
        result = self.model.get_item(position)
        if result is None or not result.path or not os.path.isfile(result.path):
            return
        uri = Gio.File.new_for_path(result.path).get_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            self.show_error(f"Dosya varsayılan uygulamayla açılamadı:\n{exc}")


class UDFaramaApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = UdfSearchWindow(self)
        win.present()


def run_app(argv=None):
    return UDFaramaApp().run(argv)
