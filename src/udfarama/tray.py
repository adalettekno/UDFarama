import logging
import os
from pathlib import Path

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from udfarama.autostart import APP_ID


ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
    <signal name="NewAttentionIcon"/>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
  </interface>
</node>
"""

_MENU_ITEMS = (
    (1, "Aç"),
    (2, "Ayarlar"),
    (3, "Hakkında"),
    (4, "Çıkış"),
)


def icon_theme_directory():
    root = Path(__file__).resolve().parents[2] / "data/app-icons"
    if (root / "hicolor").is_dir():
        return root
    return None


def register_app_icon():
    root = icon_theme_directory()
    if root is None:
        return False
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk
    display = Gdk.Display.get_default()
    if display is None:
        return False
    theme = Gtk.IconTheme.get_for_display(display)
    folder = str(root)
    if folder not in [str(path) for path in theme.get_search_path()]:
        theme.add_search_path(folder)
    return theme.has_icon(APP_ID)


def icon_file():
    candidates = [
        Path("/usr/share/icons/hicolor/512x512/apps") / f"{APP_ID}.png",
        Path.home() / ".local/share/icons/hicolor/512x512/apps" / f"{APP_ID}.png",
        Path(__file__).resolve().parents[2] / "data/app-icons/hicolor/512x512/apps" / f"{APP_ID}.png",
        Path(__file__).resolve().parents[2] / "data/app-icons/hicolor/512x512/apps/udfarama.png",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def pixbuf_to_argb(pixbuf):
    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
    width = pixbuf.get_width()
    height = pixbuf.get_height()
    channels = pixbuf.get_n_channels()
    rowstride = pixbuf.get_rowstride()
    pixels = pixbuf.get_pixels()
    data = bytearray()
    for y in range(height):
        row = y * rowstride
        for x in range(width):
            offset = row + x * channels
            red = pixels[offset]
            green = pixels[offset + 1]
            blue = pixels[offset + 2]
            alpha = pixels[offset + 3] if channels >= 4 else 255
            data.extend((alpha, red, green, blue))
    return width, height, bytes(data)


def load_icon_pixmaps():
    path = icon_file()
    if path is None:
        return []
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return []
    pixmaps = []
    for size in (22, 32, 48):
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), size, size)
        pixmaps.append(pixbuf_to_argb(pixbuf))
    return pixmaps


def _menu_properties(label):
    return {
        "type": GLib.Variant("s", "standard"),
        "label": GLib.Variant("s", label),
        "enabled": GLib.Variant("b", True),
        "visible": GLib.Variant("b", True),
    }


def menu_layout():
    children = [
        GLib.Variant("(ia{sv}av)", (item_id, _menu_properties(label), []))
        for item_id, label in _MENU_ITEMS
    ]
    root_props = {"children-display": GLib.Variant("s", "submenu")}
    return GLib.Variant("(u(ia{sv}av))", (1, (0, root_props, children)))


class StatusTray:
    def __init__(self, on_activate, on_settings, on_about, on_quit):
        self._on_activate = on_activate
        self._on_settings = on_settings
        self._on_about = on_about
        self._on_quit = on_quit
        self._on_ready = None
        self._on_failed = None
        self._owner = 0
        self._registrations = []
        self._connection = None
        self._closing = False
        self.active = False
        self._status = "Active"
        self._pulse_id = 0
        self._pixmaps = []
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    def start(self, on_ready, on_failed):
        self._on_ready = on_ready
        self._on_failed = on_failed
        self._pixmaps = load_icon_pixmaps()
        self._owner = Gio.bus_own_name(
            Gio.BusType.SESSION,
            self._bus_name,
            Gio.BusNameOwnerFlags.NONE,
            self._bus_acquired,
            self._name_acquired,
            self._name_lost,
        )

    def close(self):
        self._closing = True
        self.active = False
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0
        connection = self._connection
        for registration in self._registrations:
            if connection is not None:
                try:
                    connection.unregister_object(registration)
                except (GLib.Error, TypeError):
                    pass
        self._registrations.clear()
        if self._owner:
            Gio.bus_unown_name(self._owner)
            self._owner = 0

    def _fail(self):
        if self._closing:
            return
        self.active = False
        callback = self._on_failed
        self._on_failed = None
        if callback is not None:
            callback()

    def _bus_acquired(self, connection, _name):
        self._connection = connection
        try:
            item = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
            menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            self._registrations.append(connection.register_object(
                "/StatusNotifierItem",
                item,
                self._item_method,
                self._item_property,
                None,
            ))
            self._registrations.append(connection.register_object(
                "/StatusNotifierItem/Menu",
                menu,
                self._menu_method,
                None,
                None,
            ))
        except GLib.Error:
            logging.exception("Tepsi nesnesi yayınlanamadı")
            self._fail()

    def _name_acquired(self, _connection, name):
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                None,
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                None,
            )
            proxy.call_sync(
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (name,)),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except GLib.Error:
            logging.exception("Sistem tepsisine kaydolunamadı")
            self._fail()
            return
        self.active = True
        callback = self._on_ready
        self._on_ready = None
        if callback is not None:
            callback()

    def _name_lost(self, _connection, _name):
        if self.active or self._on_failed is not None:
            self._fail()

    def pulse(self):
        if self._closing or not self.active:
            return
        self._announce("NeedsAttention")
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
        self._pulse_id = GLib.timeout_add(700, self._settle_pulse)

    def _settle_pulse(self):
        self._pulse_id = 0
        if not self._closing and self.active:
            self._announce("Active")
        return GLib.SOURCE_REMOVE

    def _announce(self, status):
        self._status = status
        connection = self._connection
        if connection is None or self._closing:
            return
        connection.emit_signal(
            None,
            "/StatusNotifierItem",
            "org.kde.StatusNotifierItem",
            "NewStatus",
            GLib.Variant("(s)", (status,)),
        )
        if status == "NeedsAttention":
            connection.emit_signal(
                None,
                "/StatusNotifierItem",
                "org.kde.StatusNotifierItem",
                "NewAttentionIcon",
                None,
            )
        connection.emit_signal(
            None,
            "/StatusNotifierItem",
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant(
                "(sa{sv}as)",
                ("org.kde.StatusNotifierItem", {"Status": GLib.Variant("s", status)}, []),
            ),
        )

    def _item_method(self, _connection, _sender, _path, _interface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self._on_activate()
        invocation.return_value(None)

    def _item_property(self, _connection, _sender, _path, _interface, name):
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", APP_ID)
        if name == "Title":
            return GLib.Variant("s", "UDFarama")
        if name == "Status":
            return GLib.Variant("s", self._status)
        if name == "IconName":
            return GLib.Variant("s", APP_ID)
        if name == "IconPixmap":
            return GLib.Variant("a(iiay)", self._pixmaps)
        if name == "AttentionIconName":
            return GLib.Variant("s", APP_ID)
        if name == "AttentionIconPixmap":
            return GLib.Variant("a(iiay)", self._pixmaps)
        if name == "Menu":
            return GLib.Variant("o", "/StatusNotifierItem/Menu")
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "ToolTip":
            return GLib.Variant(
                "(sa(iiay)ss)",
                (APP_ID, self._pixmaps, "UDFarama", "Tıklayınca pencere açılır"),
            )
        return None

    def _menu_method(self, _connection, _sender, _path, _interface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(menu_layout())
            return
        if method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
            return
        if method == "GetGroupProperties":
            ids, _names = params.unpack()
            items = []
            labels = dict(_MENU_ITEMS)
            for item_id in ids:
                label = labels.get(int(item_id))
                if label is None:
                    continue
                items.append((int(item_id), _menu_properties(label)))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (items,)))
            return
        if method == "GetProperty":
            item_id, name = params.unpack()
            labels = dict(_MENU_ITEMS)
            props = _menu_properties(labels.get(int(item_id), ""))
            value = props.get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
            return
        if method == "Event":
            item_id, event_id, _data, _timestamp = params.unpack()
            if event_id == "clicked":
                if int(item_id) == 1:
                    self._on_activate()
                elif int(item_id) == 2:
                    self._on_settings()
                elif int(item_id) == 3:
                    self._on_about()
                elif int(item_id) == 4:
                    self._on_quit()
            invocation.return_value(None)
            return
        invocation.return_value(None)
