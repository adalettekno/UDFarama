import logging
import os
import sys
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gio, GLib, GObject, Pango, Gdk, Adw

from udfarama import __version__
from udfarama.autostart import config_directory, set_start_at_login, sync_start_at_login
from udfarama.flight import (
    IconFlight,
    flight_progress,
    icon_motion,
    icon_opacity,
    mix_rect,
    window_fade,
)
from udfarama.logging_setup import configure_logging
from udfarama.memory import FolderMemory, refresh_paths, scan_folder
from udfarama.search import (
    SearchOutcome,
    format_search_status,
    format_search_status_parts,
    format_snippet_markup,
    search_records,
    should_apply_search_generation,
    should_live_search,
)
from udfarama.tray import StatusTray, icon_file, register_app_icon
from udfarama.watch import TreeWatch


LIVE_SEARCH_DELAY_MS = 300

APP_ID = "com.adalettekno.UDFarama"
APP_NAME = "UDFarama"
APP_WEBSITE = "https://www.adalettekno.com/"


def decoration_without(layout, names):
    left, separator, right = (layout or "").partition(":")
    if not separator:
        right = left
        left = ""

    def strip(part):
        return ",".join(item for item in part.split(",") if item and item not in names)

    return f"{strip(left)}:{strip(right)}"


def decoration_without_minimize(layout):
    return decoration_without(layout, {"minimize"})


def current_decoration_layout(header):
    layout = header.get_decoration_layout()
    if not layout:
        settings = Gtk.Settings.get_default()
        if settings is not None:
            layout = settings.get_property("gtk-decoration-layout")
    return layout or "icon:minimize,maximize,close"


def minimize_is_on_end(layout):
    left, separator, right = (layout or "").partition(":")
    side = right if separator else left
    return "minimize" in [item for item in side.split(",") if item]


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
        self.set_size_request(560, 420)
        self.selected_folder = None
        self.searching = False
        self._has_searched = False
        self._last_search_term = ""
        self._cancel_event = threading.Event()
        self._search_generation = 0
        self._menu_result = None
        self._result_popover = None
        self._progress_lock = threading.Lock()
        self._progress_pending = None
        self._progress_scheduled = False
        self._pulse_source = None
        self._memory = FolderMemory()
        self._memory_ready = False
        self._memory_generation = 0
        self._index_cancel = None
        self._watch = None
        self._search_when_ready = False
        self._live_search_source = None
        self._memory_dirty = False
        self._refresh_running = False
        self._refresh_waiting = None
        self._queued_fs = []
        self._pending_error = None
        self._settings_window = None
        self._about_window = None
        self._index_failed = False
        self._autostart_enabled = False
        self._flying = False
        self._flight = None
        self._flight_tick = None
        self._flight_origin_time = None
        self._in_flight_frame = False
        self._flight_finishing = False
        self._returning = False
        self._minimize_surface = None

        self._build_actions()

        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_label = Gtk.Label(label=APP_NAME)
        self.title_label.add_css_class("title")
        header.set_title_widget(self.title_label)
        self._install_minimize_button(header)

        self.folder_label = Gtk.Label(label="Arama klasörü seçilmedi", xalign=0)
        self.folder_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.folder_label.set_hexpand(True)
        choose_btn = Gtk.Button(label="Klasör Seç")
        choose_btn.set_action_name("win.choose-folder")
        choose_btn.set_tooltip_text("Arama klasörü seç (Ctrl+O)")
        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_box.append(self.folder_label)
        folder_box.append(choose_btn)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_btn.set_tooltip_text("Menü")
        menu = Gio.Menu()
        menu.append("Ayarlar", "win.settings")
        menu.append("Hakkında", "win.about")
        menu.append("Çıkış", "app.exit")
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.append(folder_box)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("Aranacak ifade")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("activate", self.start_search)
        self.search_entry.connect("changed", self._on_search_changed)
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
        self.connect("close-request", self._on_close_request)
        self.connect("realize", self._hook_minimize)

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
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>f"),
            Gtk.NamedAction.new("win.focus-search"),
        ))
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>q"),
            Gtk.NamedAction.new("app.exit"),
        ))
        self.add_controller(shortcuts)

        try:
            self._autostart_enabled = sync_start_at_login()
        except OSError:
            logging.exception("Otomatik başlatma ayarı yazılamadı")
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

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", lambda *_: self.show_about())
        self.add_action(about)

        settings = Gio.SimpleAction.new("settings", None)
        settings.connect("activate", lambda *_: self.show_settings())
        self.add_action(settings)

        focus_search = Gio.SimpleAction.new("focus-search", None)
        focus_search.connect("activate", lambda *_: self.search_entry.grab_focus())
        self.add_action(focus_search)

    def show_about(self, *_):
        register_app_icon()
        if self._about_window is not None:
            self._about_window.present()
            return
        window = Adw.AboutWindow(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Adalet Tekno",
            version=__version__,
            website=APP_WEBSITE,
        )
        window.connect("close-request", self._on_about_closed)
        self._about_window = window
        window.present()

    def _on_about_closed(self, window):
        if self._about_window is window:
            self._about_window = None
        return False

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
            snippet.set_markup(format_snippet_markup(result.snippet, self._last_search_term))
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
        if not self.is_visible():
            self._pending_error = message
            return
        dialog = Gtk.AlertDialog(message=message)
        dialog.show(self)

    def present_window(self):
        self._cancel_flight()
        self.set_opacity(1)
        self.set_visible(True)
        self.unminimize()
        self.present()
        if self._pending_error:
            message = self._pending_error
            self._pending_error = None
            dialog = Gtk.AlertDialog(message=message)
            dialog.show(self)

    def open_settings(self):
        self.show_settings()

    def _on_close_request(self, *_args):
        app = self.get_application()
        if app is not None and not app.quitting and app.tray_active:
            self.depart_to_tray()
            return True
        return False

    def _install_minimize_button(self, header):
        layout = current_decoration_layout(header)
        on_end = minimize_is_on_end(layout)
        self._title_layout = decoration_without_minimize(layout)
        header.set_decoration_layout(self._title_layout)
        button = Gtk.Button(
            icon_name="window-minimize-symbolic",
            tooltip_text="Simge durumuna küçült",
        )
        button.add_css_class("titlebutton")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._title_minimize)
        self._minimize_button = button
        if on_end:
            header.pack_end(button)
        else:
            header.pack_start(button)

    def _title_minimize(self, *_args):
        app = self.get_application()
        if app is not None and not app.quitting and app.tray_active:
            self.depart_to_tray()
            return
        self.minimize()

    def _hook_minimize(self, *_args):
        surface = self.get_surface()
        if surface is None or surface is self._minimize_surface:
            return
        self._minimize_surface = surface
        surface.connect("notify::state", self._on_surface_state)

    def _on_surface_state(self, surface, _param):
        if self._flying or self._returning:
            return
        try:
            minimized = bool(surface.get_state() & Gdk.ToplevelState.MINIMIZED)
        except Exception:
            return
        app = self.get_application()
        if not minimized or app is None or app.quitting or not app.tray_active:
            return
        self._returning = True
        self.set_opacity(0)
        self.set_visible(False)
        self.unminimize()
        self._pulse_and_hide()
        GLib.idle_add(self._clear_returning)

    def _clear_returning(self):
        self._returning = False
        return GLib.SOURCE_REMOVE

    def depart_to_tray(self):
        if self._flying or not self.get_visible():
            return
        app = self.get_application()
        if app is None or not app.tray_active or app.quitting:
            self._conceal_for_tray()
            return
        settings = Gtk.Settings.get_default()
        animate = True if settings is None else bool(settings.get_property("gtk-enable-animations"))
        icon = icon_file()
        flight = None
        if animate and icon is not None and icon.is_file():
            flight = IconFlight.try_open(self, icon)
        if flight is None:
            self._pulse_and_hide()
            return
        self._flying = True
        self._flight = flight
        self._flight_origin_time = None
        self._in_flight_frame = False
        self._flight_tick = self.add_tick_callback(self._flight_frame)

    def _flight_frame(self, _widget, clock):
        self._in_flight_frame = True
        try:
            if not self._flying or self._flight is None:
                return GLib.SOURCE_REMOVE
            now = clock.get_frame_time()
            if self._flight_origin_time is None:
                self._flight_origin_time = now
            progress = flight_progress((now - self._flight_origin_time) / 1000)
            self.set_opacity(window_fade(progress))
            flight = self._flight
            motion = icon_motion(progress)
            rect = mix_rect(flight.origin, flight.target, motion)
            try:
                flight.step(rect, icon_opacity(motion))
            except Exception:
                progress = 1
            app = self.get_application()
            tray_active = app is not None and app.tray_active and not app.quitting
            if progress >= 1 or not tray_active:
                self._finish_flight(hide=tray_active)
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE
        finally:
            self._in_flight_frame = False

    def _finish_flight(self, hide):
        if self._flight_finishing:
            return
        self._flight_finishing = True
        try:
            self._flying = False
            flight = self._flight
            tick = self._flight_tick
            self._flight = None
            self._flight_tick = None
            self._flight_origin_time = None
            if tick is not None and not self._in_flight_frame:
                self.remove_tick_callback(tick)
            if flight is not None:
                GLib.idle_add(flight.close)
            if hide:
                self._pulse_and_hide()
            else:
                self.set_opacity(1)
        finally:
            self._flight_finishing = False

    def _cancel_flight(self):
        if self._flying or self._flight is not None:
            self._finish_flight(hide=False)

    def _pulse_and_hide(self):
        app = self.get_application()
        tray = getattr(app, "_tray", None) if app is not None else None
        if tray is not None and getattr(tray, "active", False):
            tray.pulse()
        self._conceal_for_tray()

    def _conceal_for_tray(self):
        self.set_opacity(0)
        self.set_visible(False)
        GLib.idle_add(self._restore_hidden_opacity)

    def _restore_hidden_opacity(self):
        if not self.get_visible():
            self.set_opacity(1)
        return GLib.SOURCE_REMOVE

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
        self.folder_label.set_tooltip_text(path + "\nAlt klasörler dahil hafızaya alınır.")

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
        path_file = config_directory() / "folder"
        try:
            path = path_file.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not path:
            return
        if os.path.isdir(path):
            self.selected_folder = path
            self._set_folder_display(path)
            self._start_memory(path)
            return
        self.selected_folder = None
        self._set_folder_display(None)
        self._set_status_primary("Kayıtlı klasör bulunamadı. Lütfen Klasör Seç ile yeniden seçin.")
        self.show_error(
            "Kayıtlı arama klasörü artık yok veya erişilemiyor. "
            "Lütfen Klasör Seç ile yeniden seçin."
        )

    def save_folder(self, path):
        config_dir = config_directory()
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
        self._start_memory(path)

    def on_search_button(self, *_):
        if self.search_button.get_label() == "Durdur":
            self.stop_search()
            return
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

    def _on_search_changed(self, _entry):
        if self._live_search_source is not None:
            GLib.source_remove(self._live_search_source)
            self._live_search_source = None
        term = self.search_entry.get_text().strip()
        if not should_live_search(term):
            if self.searching or self._has_searched or self.model.get_n_items() or self._search_when_ready:
                self._clear_short_query()
            return
        self._live_search_source = GLib.timeout_add(LIVE_SEARCH_DELAY_MS, self._run_live_search)

    def _run_live_search(self):
        self._live_search_source = None
        if should_live_search(self.search_entry.get_text()):
            self.start_search(live=True)
        elif self.searching or self._has_searched or self.model.get_n_items():
            self._clear_short_query()
        return GLib.SOURCE_REMOVE

    def _clear_short_query(self):
        self._search_when_ready = False
        self._cancel_event.set()
        self._search_generation += 1
        self._cancel_event = threading.Event()
        self.searching = False
        self._set_search_button_searching(False)
        self.model.splice(0, self.model.get_n_items(), [])
        self._has_searched = False
        self._update_empty_state()
        self._update_title_count(0)
        self._restore_idle_status()

    def _restore_idle_status(self):
        if self._index_failed:
            self._set_status_primary("Klasör hafızaya alınamadı. Yeniden Klasör Seç ile deneyin.")
            return
        if self.selected_folder and not self._memory_ready:
            return
        if self.selected_folder and self._memory_ready:
            count = self._memory.count()
            self._set_status_primary(f"Hafızada {count} UDF belgesi var. Arama yapabilirsiniz.")
            return
        self._set_status_primary("Önce bir klasör seçin.")

    def start_search(self, *_args, quiet=False, live=False):
        if self.searching:
            self._cancel_event.set()
            self._search_generation += 1
            self.searching = False
            self._set_search_button_searching(False)
        if self._index_failed:
            if not quiet and not live:
                self.show_error("Klasör hafızaya alınamadı. Yeniden Klasör Seç ile deneyin.")
            return
        if not self.selected_folder or not os.path.isdir(self.selected_folder):
            self.selected_folder = None
            self._set_folder_display(None)
            if not quiet and not live:
                self.show_error("Önce geçerli bir arama klasörü seçin.")
                self.choose_folder()
            return
        term = self.search_entry.get_text().strip()
        if live and not should_live_search(term):
            self._clear_short_query()
            return
        if not term:
            if not quiet and not live:
                self.show_error("Aranacak ifade boş olamaz.")
            return
        if not self._memory_ready:
            self._search_when_ready = "live" if live else "explicit"
            self._set_status_primary("Klasör hafızaya alınıyor. Arama hazır olunca başlayacak.")
            return

        self._cancel_event = threading.Event()
        self.searching = True
        self._last_search_term = term
        self._search_generation += 1
        generation = self._search_generation
        cancel_event = self._cancel_event
        with self._progress_lock:
            self._progress_pending = None
            self._progress_scheduled = False
        if not live:
            self._set_search_button_searching(True)
        if quiet:
            self._set_status_primary("Değişiklikler hafızaya alındı, sonuçlar güncelleniyor…")
        elif not live:
            self.model.splice(0, self.model.get_n_items(), [])
            self._update_empty_state()
            self._update_title_count(0)
            self._set_status_primary("Hafızadaki belgeler aranıyor…")
        threading.Thread(
            target=self._search_worker,
            args=(self.selected_folder, term, generation, cancel_event, live),
            name="udfarama-search",
            daemon=True,
        ).start()

    def _search_worker(self, folder, term, generation, cancel_event, live):
        records, directory_errors, memory_generation = self._memory.snapshot()
        if memory_generation != self._memory_generation:
            GLib.idle_add(
                self._search_finished,
                generation,
                SearchOutcome([], 0, 0, 0, 0, 0, False, True),
                live,
            )
            return

        def on_progress(scanned, match_count):
            with self._progress_lock:
                self._progress_pending = (generation, scanned, match_count)
                if self._progress_scheduled:
                    return
                self._progress_scheduled = True
            GLib.idle_add(self._flush_progress)

        outcome = search_records(
            records,
            folder,
            term,
            cancel_event=cancel_event,
            on_progress=on_progress,
            directory_errors=directory_errors,
        )
        GLib.idle_add(self._search_finished, generation, outcome, live)

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

    def _search_finished(self, generation, outcome, live=False):
        if not should_apply_search_generation(self._search_generation, generation):
            return GLib.SOURCE_REMOVE
        try:
            self._has_searched = True
            self.model.splice(0, self.model.get_n_items(), [])
            for match in outcome.matches:
                self.model.append(SearchResult.from_match(match))
            if live:
                self._set_status_primary(format_search_status(outcome))
            else:
                primary, detail = format_search_status_parts(outcome)
                self._set_status_primary(primary)
                self._show_detail_toast(detail)
            self._update_title_count(len(outcome.matches))
            self._update_empty_state()
        finally:
            self.searching = False
            self._set_search_button_searching(False)
        if self._memory_dirty:
            self._memory_dirty = False
            self._after_memory_update()
        return GLib.SOURCE_REMOVE

    def open_selected(self, list_view, position):
        result = self.model.get_item(position)
        if result is None:
            return
        self._open_path(result.path)

    def show_settings(self):
        if self._settings_window is not None:
            self._settings_window.present()
            return
        window = Adw.Window(title="Ayarlar")
        window.set_default_size(420, 120)
        window.set_resizable(False)
        header = Adw.HeaderBar()
        window._title_layout = decoration_without(
            current_decoration_layout(header), {"minimize", "maximize"})
        header.set_decoration_layout(window._title_layout)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(18)
        page.set_margin_bottom(18)
        page.set_margin_start(18)
        page.set_margin_end(18)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title = Gtk.Label(label="Bilgisayar açılışında başlat", xalign=0)
        title.set_hexpand(True)
        switch = Gtk.Switch()
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_active(self._autostart_enabled)
        switch.connect("notify::active", self._on_autostart_notify)
        row.append(title)
        row.append(switch)
        page.append(row)
        toolbar.set_content(page)
        window.set_content(toolbar)
        window.connect("close-request", self._on_settings_closed)
        self._settings_window = window
        window.present()

    def _on_settings_closed(self, window):
        if self._settings_window is window:
            self._settings_window = None
        return False

    def _on_autostart_notify(self, row, _pspec):
        enabled = bool(row.props.active)
        if enabled == self._autostart_enabled:
            return
        try:
            set_start_at_login(enabled)
        except OSError as exc:
            self.show_error(f"Başlangıç ayarı kaydedilemedi.\n{exc}")
            row.set_active(self._autostart_enabled)
            return
        self._autostart_enabled = enabled

    def shutdown_background(self):
        self._cancel_flight()
        if self._settings_window is not None:
            self._settings_window.close()
        if self._about_window is not None:
            about = self._about_window
            self._about_window = None
            about.close()
        if self._live_search_source is not None:
            GLib.source_remove(self._live_search_source)
            self._live_search_source = None
        if self._index_cancel is not None:
            self._index_cancel.set()
        if self._watch is not None:
            self._watch.clear()
            self._watch = None

    def _start_memory(self, path):
        self._search_generation += 1
        self._cancel_event.set()
        self._cancel_event = threading.Event()
        self.searching = False
        self._set_search_button_searching(False)
        self._search_when_ready = False
        self._index_failed = False
        if self._live_search_source is not None:
            GLib.source_remove(self._live_search_source)
            self._live_search_source = None
        self._memory_dirty = False
        self._refresh_waiting = None
        self._queued_fs = []
        if self._index_cancel is not None:
            self._index_cancel.set()
        self._index_cancel = threading.Event()
        if self._watch is not None:
            self._watch.clear()
        self._watch = TreeWatch(self._on_fs_batch)
        self._memory_ready = False
        generation = self._memory.begin(path)
        self._memory_generation = generation
        self._watch.watch(path)
        self._set_status_primary("Klasör ve alt klasörler hafızaya alınıyor…")
        threading.Thread(
            target=self._index_worker,
            args=(path, generation, self._index_cancel),
            name="udfarama-index",
            daemon=True,
        ).start()

    def _index_worker(self, folder, generation, cancel_event):
        def on_progress(count):
            GLib.idle_add(self._index_progress, generation, count)

        def on_directory(directory):
            GLib.idle_add(self._watch_directory, generation, directory)

        result = scan_folder(
            folder,
            generation,
            self._memory,
            cancel_event,
            on_progress=on_progress,
            on_directory=on_directory,
        )
        GLib.idle_add(self._index_finished, generation, result)

    def _index_progress(self, generation, count):
        if generation != self._memory_generation or self._memory_ready or self.searching:
            return GLib.SOURCE_REMOVE
        self._set_status_primary(f"Klasör ve alt klasörler hafızaya alınıyor… {count} dosya")
        return GLib.SOURCE_REMOVE

    def _watch_directory(self, generation, directory):
        if generation != self._memory_generation or self._watch is None:
            return GLib.SOURCE_REMOVE
        self._watch.watch(directory)
        return GLib.SOURCE_REMOVE

    def _index_finished(self, generation, result):
        if generation != self._memory_generation:
            return GLib.SOURCE_REMOVE
        if result.cancelled:
            return GLib.SOURCE_REMOVE
        self._index_failed = bool(result.failed)
        self._memory_ready = True
        self._flush_queued_fs()
        count = self._memory.count()
        if result.failed:
            self._set_status_primary("Klasör hafızaya alınamadı. Yeniden Klasör Seç ile deneyin.")
        elif count == 0:
            self._set_status_primary(
                "Seçilen klasörde UDF belgesi yok. Eklenen dosyalar otomatik hafızaya alınır."
            )
        else:
            self._set_status_primary(f"Hafızada {count} UDF belgesi var. Arama yapabilirsiniz.")
        pending_search = self._search_when_ready
        self._search_when_ready = False
        if pending_search and not result.failed:
            term = self.search_entry.get_text().strip()
            if pending_search == "live" and should_live_search(term):
                self.start_search(live=True)
            elif pending_search != "live" and term:
                self.start_search()
        return GLib.SOURCE_REMOVE

    def _on_fs_batch(self, changed, removed):
        if not self._memory_ready:
            self._queued_fs.append((set(changed), set(removed)))
            return
        self._run_refresh(changed, removed, notify=True)

    def _flush_queued_fs(self):
        queued = self._queued_fs
        self._queued_fs = []
        changed = set()
        removed = set()
        for item_changed, item_removed in queued:
            changed.update(item_changed)
            removed.update(item_removed)
        if changed or removed:
            self._run_refresh(changed, removed, notify=False)

    def _run_refresh(self, changed, removed, notify):
        if self._refresh_running:
            if self._refresh_waiting is None:
                self._refresh_waiting = [set(changed), set(removed), notify]
            else:
                self._refresh_waiting[0].update(changed)
                self._refresh_waiting[1].update(removed)
                self._refresh_waiting[2] = self._refresh_waiting[2] or notify
            return
        self._refresh_running = True
        generation = self._memory_generation
        folder = self.selected_folder
        threading.Thread(
            target=self._refresh_worker,
            args=(generation, folder, set(changed), set(removed), self._index_cancel, notify),
            name="udfarama-refresh",
            daemon=True,
        ).start()

    def _refresh_worker(self, generation, folder, changed, removed, cancel_event, notify):
        def on_directory(directory):
            GLib.idle_add(self._watch_directory, generation, directory)

        changed_memory = False
        if folder:
            changed_memory = refresh_paths(
                self._memory,
                generation,
                folder,
                changed,
                removed,
                cancel_event=cancel_event,
                on_directory=on_directory,
            )
        GLib.idle_add(self._refresh_done, generation, notify, changed_memory)

    def _refresh_done(self, generation, notify, changed_memory):
        self._refresh_running = False
        waiting = self._refresh_waiting
        self._refresh_waiting = None
        if generation == self._memory_generation and notify and changed_memory:
            self._after_memory_update()
        if waiting and generation == self._memory_generation:
            self._run_refresh(waiting[0], waiting[1], waiting[2])
        return GLib.SOURCE_REMOVE

    def _after_memory_update(self):
        if self.searching:
            self._memory_dirty = True
            return
        self._show_detail_toast("Klasör hafızası güncellendi.")
        term = self.search_entry.get_text().strip()
        if self._has_searched and term and term == self._last_search_term:
            self.start_search(quiet=True)
            return
        count = self._memory.count()
        self._set_status_primary(f"Hafızada {count} UDF belgesi var. Arama yapabilirsiniz.")


class UDFaramaApp(Adw.Application):
    def __init__(self, background=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.background = background
        self.quitting = False
        self.tray_active = False
        self._tray = None
        self._background_wait = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        register_app_icon()
        exit_action = Gio.SimpleAction.new("exit", None)
        exit_action.connect("activate", lambda *_args: self.request_quit())
        self.add_action(exit_action)

    def do_activate(self):
        win = self.props.active_window
        first = win is None
        if first:
            win = UdfSearchWindow(self)
            self._setup_tray(win)
        if first and self.background:
            self._background_wait = True
            GLib.timeout_add(2000, self._background_fallback)
            return
        if win._flying or win._returning:
            return
        self.background = False
        self._background_wait = False
        win.present_window()

    def _setup_tray(self, window):
        try:
            self._tray = StatusTray(
                window.present_window, window.open_settings, window.show_about, self.request_quit)
            self._tray.start(self._tray_ready, self._tray_failed)
        except Exception:
            logging.exception("Sistem tepsisi başlatılamadı")
            self._tray_failed()

    def _tray_ready(self):
        self.tray_active = True
        self._background_wait = False

    def _tray_failed(self):
        self.tray_active = False
        if self._background_wait:
            self._background_fallback()

    def _background_fallback(self):
        if self.quitting or self.tray_active:
            self._background_wait = False
            return GLib.SOURCE_REMOVE
        win = self.props.active_window
        self.background = False
        self._background_wait = False
        if win is not None and not win.is_visible():
            win.present_window()
        return GLib.SOURCE_REMOVE

    def request_quit(self):
        self.quitting = True
        self.background = False
        self._background_wait = False
        win = self.props.active_window
        if win is not None:
            win.shutdown_background()
        if self._tray is not None:
            self._tray.close()
            self._tray = None
        self.quit()

    def do_shutdown(self):
        self.quitting = True
        win = self.props.active_window
        if win is not None:
            win.shutdown_background()
        if self._tray is not None:
            self._tray.close()
            self._tray = None
        Adw.Application.do_shutdown(self)


def run_app(argv=None, background=False):
    configure_logging()
    application = UDFaramaApp(background=background)
    if argv is None:
        argv = [sys.argv[0] or "udfarama"]
    return application.run(argv)
