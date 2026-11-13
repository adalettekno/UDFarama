import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from udfarama.logging_setup import configure_logging
from udfarama.tray import StatusTray, register_app_icon
from udfarama.window import UdfSearchWindow


APP_ID = "com.adalettekno.UDFarama"


class UDFaramaApp(Adw.Application):
    def __init__(self, background=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.background = background
        self.quitting = False
        self.tray_active = False
        self._tray = None
        self._main_window = None
        self._background_wait = False

    def do_startup(self):
        Adw.Application.do_startup(self)
        register_app_icon()
        action = Gio.SimpleAction.new("exit", None)
        action.connect("activate", lambda *_args: self.request_quit())
        self.add_action(action)

    def do_activate(self):
        window = self._main_window
        first = window is None
        if first:
            window = UdfSearchWindow(self)
            self._main_window = window
            self._setup_tray(window)
        if first and self.background:
            self._background_wait = True
            GLib.timeout_add(2000, self._background_fallback)
            return
        self.background = False
        self._background_wait = False
        window.present_window()

    def _setup_tray(self, window):
        try:
            self._tray = StatusTray(
                window.present_window,
                window.open_settings,
                window.show_about,
                self.request_quit,
            )
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
        self.background = False
        self._background_wait = False
        window = self._main_window
        if window is not None and not window.get_visible():
            window.present_window()
        return GLib.SOURCE_REMOVE

    def request_quit(self):
        if self.quitting:
            return
        self.quitting = True
        window = self._main_window
        if window is not None:
            window.shutdown_background(self._finish_quit)
        else:
            self._finish_quit()

    def _finish_quit(self):
        if self._tray is not None:
            self._tray.close()
            self._tray = None
        self.quit()

    def do_shutdown(self):
        self.quitting = True
        window = self._main_window
        if window is not None:
            window.shutdown_background()
        if self._tray is not None:
            self._tray.close()
            self._tray = None
        Adw.Application.do_shutdown(self)


def run_app(argv=None, background=False):
    configure_logging()
    application = UDFaramaApp(background=background)
    return application.run(argv or [sys.argv[0] or "udfarama"])
