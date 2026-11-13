import logging
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from udfarama import __version__
from udfarama.autostart import config_directory, sync_start_at_login
from udfarama.index_controller import IndexController
from udfarama.search import format_snippet_markup
from udfarama.search_controller import SearchController
from udfarama.settings import build_settings
from udfarama.tray import register_app_icon
from udfarama.ui.layout import SearchResult, build_main_layout


APP_ID = "com.adalettekno.UDFarama"
APP_NAME = "UDFarama"
APP_WEBSITE = "https://www.adalettekno.com/"


class UdfSearchWindow(SearchController, Adw.ApplicationWindow):
    result_type = SearchResult

    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(624, 560)
        self.set_size_request(620, 430)
        self.selected_folder = None
        self._pending_error = None
        self._settings_window = None
        self._about_window = None
        self._menu_result = None
        self._result_popover = None
        self._autostart_enabled = False
        self.initialize_search()
        self._build_actions()
        build_main_layout(self)
        self._index = IndexController(
            self._index_ready,
            self._index_updated,
            self._set_status_primary,
            self.show_error,
        )
        self.connect("close-request", self._on_close_request)
        shortcuts = Gtk.ShortcutController()
        shortcuts.set_scope(Gtk.ShortcutScope.MANAGED)
        for key, action in (
            ("Escape", "win.cancel-search"),
            ("<Control>o", "win.choose-folder"),
            ("<Control>f", "win.focus-search"),
            ("<Control>q", "app.exit"),
        ):
            shortcuts.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(key),
                Gtk.NamedAction.new(action),
            ))
        self.add_controller(shortcuts)
        try:
            self._autostart_enabled = sync_start_at_login()
        except OSError:
            logging.exception("Otomatik başlatma ayarı kaydedilemedi")
        self.load_saved_folder()
        self._update_empty_state()

    def _build_actions(self):
        actions = (
            ("choose-folder", self.choose_folder),
            ("cancel-search", self.cancel_search),
            ("copy-result-path", self._copy_menu_path),
            ("open-result-folder", self._open_menu_folder),
            ("open-result-file", self._open_menu_file),
            ("about", self.show_about),
            ("settings", self.show_settings),
            ("focus-search", self._focus_search),
        )
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_args, fn=callback: fn())
            self.add_action(action)

    def _focus_search(self):
        self.search_entry.grab_focus()

    def _install_minimize_button(self, header):
        header.set_decoration_layout("icon:maximize,close")
        button = Gtk.Button(icon_name="window-minimize-symbolic")
        button.add_css_class("titlebutton")
        button.set_tooltip_text("Sistem tepsisine küçült")
        button.connect("clicked", lambda *_args: self._minimize_window())
        header.pack_end(button)
        self._minimize_button = button

    def _minimize_window(self):
        app = self.get_application()
        if app is not None and app.tray_active and not app.quitting:
            self.set_visible(False)
        else:
            self.minimize()

    def _on_close_request(self, *_args):
        app = self.get_application()
        if app is not None and app.tray_active and not app.quitting:
            self.set_visible(False)
            return True
        return False

    def present_window(self):
        self.set_visible(True)
        self.unminimize()
        self.present()
        if self._pending_error:
            message = self._pending_error
            self._pending_error = None
            self.show_error(message)

    def open_settings(self):
        self.show_settings()

    def show_settings(self):
        if self._settings_window is not None:
            self._settings_window.present()
            return
        dialog = build_settings(
            self, self._autostart_enabled,
            lambda active: setattr(self, "_autostart_enabled", active),
        )
        self._settings_window = dialog
        dialog.connect("close-request", self._settings_closed)
        dialog.present()

    def _settings_closed(self, window):
        if window is self._settings_window:
            self._settings_window = None
        return False

    def show_about(self):
        if self._about_window is not None:
            self._about_window.present()
            return
        register_app_icon()
        dialog = Adw.AboutWindow(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Adalet Tekno",
            version=__version__,
            website=APP_WEBSITE,
        )
        dialog.set_transient_for(self)
        self._about_window = dialog
        dialog.connect("close-request", self._about_closed)
        dialog.present()

    def _about_closed(self, window):
        if window is self._about_window:
            self._about_window = None
        return False

    def show_error(self, message):
        if not self.get_visible():
            self._pending_error = message
            logging.error("UDFarama: %s", message)
            return
        dialog = Gtk.AlertDialog(message=message)
        dialog.show(self)

    def _set_status_primary(self, message):
        self.status.set_text(message)
        if self._index.ready:
            self.state_icon.set_from_icon_name("emblem-ok-symbolic")
        else:
            self.state_icon.set_from_icon_name("view-refresh-symbolic")

    def _restore_idle_status(self):
        if self._index.ready:
            try:
                count = self._index.store.count()
                self._set_status_primary(f"İndeks hazır · {count:,} UDF belgesi")
            except Exception:
                logging.exception("Belge sayısı okunamadı")
                self._set_status_primary("İndeks hazır")
        elif self.selected_folder:
            self._set_status_primary("İndeks hazırlanıyor…")
        else:
            self._set_status_primary("Önce bir klasör seçin.")

    def _update_results_label(self, count):
        self.results_label.set_text(
            f"{count:,}+ sonuç" if self._has_more else (f"{count:,} sonuç" if self._has_searched else "")
        )

    def _set_folder_display(self, path):
        if not path:
            self.folder_label.set_text("Arama klasörü seçilmedi")
            self.folder_label.set_tooltip_text("")
            return
        self.folder_label.set_text(os.path.basename(path.rstrip(os.sep)) or path)
        self.folder_label.set_tooltip_text(path)

    def _update_empty_state(self):
        if self.model.get_n_items():
            self.stack.set_visible_child_name("results")
            return
        self.stack.set_visible_child_name("empty")
        if self._has_searched:
            self.empty_page.set_title("Sonuç bulunamadı")
            self.empty_page.set_description("Başka bir ifade deneyin.")
        else:
            self.empty_page.set_title("Belge aramaya hazır")
            self.empty_page.set_description(
                "Klasör seçip belge adı veya metin içinde arama yapın."
            )

    def load_saved_folder(self):
        path = config_directory() / "folder"
        try:
            folder = path.read_text(encoding="utf-8").strip()
        except OSError:
            self._restore_idle_status()
            return
        if os.path.isdir(folder):
            self._set_selected_folder(folder, save=False)
        elif folder:
            self._set_status_primary("Kayıtlı arama klasörüne erişilemiyor.")
        else:
            self._restore_idle_status()

    def choose_folder(self):
        dialog = Gtk.FileDialog(title="Aranacak üst klasörü seçin")
        dialog.select_folder(self, None, self._folder_selected)

    def _folder_selected(self, dialog, result):
        try:
            chosen = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if chosen is None or not chosen.get_path():
            return
        path = chosen.get_path()
        if not os.path.isdir(path):
            self.show_error("Klasör artık mevcut değil.")
            return
        self._set_selected_folder(path)

    def _set_selected_folder(self, folder, save=True):
        folder = os.path.abspath(folder)
        if self.selected_folder == folder and self._index.ready:
            return
        self.selected_folder = folder
        self.cancel_search()
        self.model.remove_all()
        self._has_searched = False
        self._has_more = False
        self.more_button.set_visible(False)
        self._update_empty_state()
        self._update_results_label(0)
        self._set_folder_display(folder)
        if save:
            directory = config_directory()
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "folder").write_text(folder + "\n", encoding="utf-8")
        self._set_status_primary("Kalıcı indeks açılıyor…")
        self._index.open_folder(folder)

    def _index_ready(self):
        self.on_index_ready()

    def _index_updated(self):
        self.on_index_updated()

    def _setup_item(self, _factory, item):
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        root.add_css_class("udf-result")
        image = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        image.set_valign(Gtk.Align.START)
        image.set_margin_top(3)
        root.append(image)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        details.set_hexpand(True)
        title = Gtk.Label(xalign=0)
        title.add_css_class("udf-result-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        details.append(title)
        location = Gtk.Label(xalign=0)
        location.add_css_class("udf-result-location")
        location.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        details.append(location)
        snippet = Gtk.Label(xalign=0)
        snippet.add_css_class("udf-result-snippet")
        snippet.set_ellipsize(Pango.EllipsizeMode.END)
        details.append(snippet)
        root.append(details)
        menu_click = Gtk.GestureClick()
        menu_click.set_button(Gdk.BUTTON_SECONDARY)
        menu_click.connect("pressed", self._item_secondary, item)
        root.add_controller(menu_click)
        item.set_child(root)

    def _bind_item(self, _factory, item):
        result = item.get_item()
        root = item.get_child()
        details = root.get_last_child()
        title = details.get_first_child()
        location = title.get_next_sibling()
        snippet = location.get_next_sibling()
        title.set_text(result.basename)
        location.set_text(os.path.dirname(result.relative_path) or "Ana klasör")
        if result.snippet:
            snippet.set_markup(format_snippet_markup(result.snippet, self._last_search_term))
            snippet.set_visible(True)
        else:
            snippet.set_visible(False)
        root.set_tooltip_text(result.path)

    def _item_secondary(self, _gesture, _presses, _x, _y, item):
        result = item.get_item()
        if result is None:
            return
        self._menu_result = result
        if self._result_popover is not None:
            self._result_popover.popdown()
            self._result_popover.unparent()
        menu = Gio.Menu()
        menu.append("Belgeyi aç", "win.open-result-file")
        menu.append("İçeren klasörü aç", "win.open-result-folder")
        menu.append("Dosya yolunu kopyala", "win.copy-result-path")
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(item.get_child())
        popover.popup()
        self._result_popover = popover

    def _copy_menu_path(self):
        if self._menu_result is not None:
            self.get_clipboard().set(self._menu_result.path)
            self.toast_overlay.add_toast(Adw.Toast.new("Dosya yolu kopyalandı"))

    def _launch(self, path):
        if not path or not os.path.exists(path):
            self.show_error("Belge artık bu konumda bulunmuyor. Kayıt kapalıyken değiştirilmiş olabilir.")
            return
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        except GLib.Error as error:
            self.show_error("Konum açılamadı: " + str(error))

    def _open_menu_file(self):
        if self._menu_result is not None:
            self._launch(self._menu_result.path)

    def _open_menu_folder(self):
        result = self._menu_result
        if result is None or not result.path:
            return
        from pathlib import Path
        from gi.repository import Gio, GLib
        path = Path(result.path).expanduser().absolute()
        if not path.is_file():
            self.show_error("Belge bulunamadı veya taşınmış.")
            return
        uri = Gio.File.new_for_path(str(path)).get_uri()
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call(
                "org.freedesktop.FileManager1",
                "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1",
                "ShowItems",
                GLib.Variant("(ass)", ([uri], "")),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
                self._on_show_item_done,
                str(path),
            )
        except GLib.Error:
            self._select_file_with_dolphin(str(path))

    def _on_show_item_done(self, connection, result, path):
        from gi.repository import GLib
        try:
            connection.call_finish(result)
        except GLib.Error:
            self._select_file_with_dolphin(path)

    def _select_file_with_dolphin(self, path):
        from gi.repository import Gio, GLib
        try:
            Gio.Subprocess.new(
                ["dolphin", "--select", path],
                Gio.SubprocessFlags.STDOUT_SILENCE
                | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except GLib.Error as exc:
            self.show_error(f"Dolphin açılamadı:\n{exc}")

    def open_selected(self, _list_view, position):
        result = self.model.get_item(position)
        if result is not None:
            self._launch(result.path)

    def shutdown_background(self, on_complete=None):
        self.shutdown_search()
        if self._settings_window is not None:
            self._settings_window.close()
        if self._about_window is not None:
            self._about_window.close()
        if on_complete is not None:
            self._index.finish(on_complete)
        else:
            self._index.close()
