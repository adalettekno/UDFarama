import ctypes
import time
import warnings

DEPART_MS = 780
ICON_LEAD = 0.2
ICON_PX = 96


def flight_progress(elapsed_ms, duration_ms=DEPART_MS):
    if duration_ms <= 0:
        return 1.0
    return min(1.0, max(0.0, elapsed_ms / duration_ms))


def flight_motion(progress):
    progress = min(1.0, max(0.0, progress))
    return 1 - (1 - progress) ** 2


def mix_rect(start, end, progress):
    eased = flight_motion(progress)
    return tuple(int(round(a + (b - a) * eased)) for a, b in zip(start, end))


def window_fade(progress, lead=ICON_LEAD):
    progress = min(1.0, max(0.0, progress))
    if progress <= 0:
        return 1.0
    if lead <= 0 or progress >= lead:
        return 0.0
    return 1.0 - (progress / lead)


def icon_motion(progress, lead=ICON_LEAD):
    progress = min(1.0, max(0.0, progress))
    if lead >= 1:
        return 1.0 if progress >= 1 else 0.0
    if progress <= lead:
        return 0.0
    return min(1.0, (progress - lead) / (1.0 - lead))


def icon_opacity(motion):
    motion = min(1.0, max(0.0, motion))
    if motion <= 0.82:
        return 1.0
    fade = (motion - 0.82) / 0.18
    return max(0.0, 1.0 - fade * fade)


def icon_start(window_rect, size):
    x, y, width, height = window_rect
    return (
        int(round(x + width / 2 - size / 2)),
        int(round(y + height / 2 - size / 2)),
        int(size),
        int(size),
    )


def icon_corner(workarea, size, scale=1):
    scale = max(1, int(scale))
    return (
        int(workarea[0] + workarea[2] - size - 16 * scale),
        int(workarea[1] + workarea[3] - size - 12 * scale),
        int(size),
        int(size),
    )


def tray_slot(widget):
    x, y, width, height = (int(value) for value in widget)
    horizontal = width >= height
    thickness = height if horizontal else width
    side = max(16, thickness - 8)
    inset = max(4, (thickness - side) // 2 + 2)
    cross = (thickness - side) // 2
    if horizontal:
        return (x + inset, y + cross, side, side)
    return (x + cross, y + inset, side, side)


def _match_score(pixels, origin_x, origin_y, points):
    error = 0
    for point_x, point_y, red, green, blue in points:
        sample_red, sample_green, sample_blue = pixels[origin_x + point_x, origin_y + point_y]
        error += abs(sample_red - red) + abs(sample_green - green) + abs(sample_blue - blue)
    return error / (len(points) * 3)


def _best_match(pixels, width, height, size, points, step, bounds=None):
    if bounds is None:
        x0, y0, x1, y1 = 0, 0, width - size, height - size
    else:
        x0, y0, x1, y1 = bounds
    best = None
    y = y0
    while y <= y1:
        x = x0
        while x <= x1:
            score = _match_score(pixels, x, y, points)
            if best is None or score < best[2]:
                best = (x, y, score)
            x += step
        y += 1 if step > 1 else 1
    return best


def locate_icon(haystack, icon):
    hay = haystack.convert("RGB")
    needle = icon.convert("RGBA")
    width, height = hay.size
    pixels = hay.load()
    best = None
    for size in (16, 18, 20, 22, 24, 28):
        if size > width or size > height:
            continue
        sample = needle.resize((size, size))
        sample_pixels = sample.load()
        points = []
        for point_y in range(size):
            for point_x in range(size):
                pixel = sample_pixels[point_x, point_y]
                if pixel[3] < 160:
                    continue
                points.append((point_x, point_y, pixel[0], pixel[1], pixel[2]))
        if len(points) < 12:
            continue
        points = points[::max(1, len(points) // 64)]
        coarse = _best_match(pixels, width, height, size, points, 3)
        if coarse is None:
            continue
        refined = _best_match(
            pixels, width, height, size, points, 1,
            (
                max(0, coarse[0] - 3),
                max(0, coarse[1] - 2),
                min(width - size, coarse[0] + 3),
                min(height - size, coarse[1] + 2),
            ),
        )
        choice = refined or coarse
        if best is None or choice[2] < best[3]:
            best = (choice[0], choice[1], size, choice[2])
    if best is None or best[3] > 55:
        return None
    return best[0], best[1], best[2]


def icon_slot(tray_rect, haystack, icon):
    found = locate_icon(haystack, icon)
    if found is None:
        return None
    offset_x, offset_y, size = found
    return (
        int(tray_rect[0] + offset_x),
        int(tray_rect[1] + offset_y),
        int(size),
        int(size),
    )


def _gtk():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    return Gtk


def _clear_flight_chrome(window):
    gtk = _gtk()
    provider = gtk.CssProvider()
    provider.load_from_data(
        b"window.tray-flight, window.tray-flight * { background-color: transparent; box-shadow: none; }"
    )
    gtk.StyleContext.add_provider_for_display(
        window.get_display(),
        provider,
        gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


class _ClientMessage(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


class _Event(ctypes.Union):
    _fields_ = [("xclient", _ClientMessage), ("pad", ctypes.c_byte * 256)]


class _Attributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", ctypes.c_ulong),
        ("background_pixel", ctypes.c_ulong),
        ("border_pixmap", ctypes.c_ulong),
        ("border_pixel", ctypes.c_ulong),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("cursor", ctypes.c_ulong),
    ]


def _library():
    lib = ctypes.CDLL("libX11.so.6")
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XSendEvent.restype = ctypes.c_int
    lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
    lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XTranslateCoordinates.restype = ctypes.c_int
    lib.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
    ]
    lib.XChangeWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    lib.XMoveResizeWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
    ]
    lib.XMapRaised.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XUnmapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XQueryTree.restype = ctypes.c_int
    lib.XQueryTree.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)), ctypes.POINTER(ctypes.c_uint),
    ]
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XSetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
    lib.XChangeProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
    ]
    lib.XGetGeometry.restype = ctypes.c_int
    lib.XGetGeometry.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
    ]
    lib.XDefaultScreen.restype = ctypes.c_int
    lib.XDefaultScreen.argtypes = [ctypes.c_void_p]
    lib.XMatchVisualInfo.restype = ctypes.c_int
    lib.XMatchVisualInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    lib.XCreateColormap.restype = ctypes.c_ulong
    lib.XCreateColormap.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_int]
    lib.XCreateWindow.restype = ctypes.c_ulong
    lib.XCreateWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
    ]
    lib.XCreateGC.restype = ctypes.c_void_p
    lib.XCreateGC.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    lib.XCreateImage.restype = ctypes.c_void_p
    lib.XCreateImage.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
    ]
    lib.XPutImage.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
    ]
    lib.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
    lib.XReparentWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
    lib.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XFreeGC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.XGetWindowProperty.restype = ctypes.c_int
    lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
        ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    return lib


def _xid(surface):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return int(surface.get_xid())


def _client_origin(lib, display, xid):
    root = lib.XDefaultRootWindow(display)
    x = ctypes.c_int()
    y = ctypes.c_int()
    child = ctypes.c_ulong()
    ok = lib.XTranslateCoordinates(
        display, xid, root, 0, 0, ctypes.byref(x), ctypes.byref(y), ctypes.byref(child))
    if not ok:
        return None
    return x.value, y.value


def _window_size(lib, display, xid):
    root = ctypes.c_ulong()
    x = ctypes.c_int()
    y = ctypes.c_int()
    width = ctypes.c_uint()
    height = ctypes.c_uint()
    border = ctypes.c_uint()
    depth = ctypes.c_uint()
    ok = lib.XGetGeometry(
        display, xid, ctypes.byref(root), ctypes.byref(x), ctypes.byref(y),
        ctypes.byref(width), ctypes.byref(height), ctypes.byref(border), ctypes.byref(depth))
    if not ok or width.value < 2 or height.value < 2:
        return None
    return int(width.value), int(height.value)


def _workarea(surface, scale):
    display = surface.get_display()
    monitor = display.get_monitor_at_surface(surface)
    if monitor is None:
        monitors = display.get_monitors()
        monitor = monitors.get_item(0) if monitors is not None and monitors.get_n_items() else None
    if monitor is None:
        return (0, 0, 1920 * scale, 1080 * scale)
    area = monitor.get_workarea()
    return (
        int(area.x) * scale,
        int(area.y) * scale,
        int(area.width) * scale,
        int(area.height) * scale,
    )


_TRAY_SCRIPT = """
var out = [];
for (var i = 0; i < panelIds.length; i++) {
  var panel = panelById(panelIds[i]);
  if (!panel) continue;
  var widgets = panel.widgets("org.kde.plasma.systemtray");
  for (var j = 0; j < widgets.length; j++) {
    var g = widgets[j].geometry;
    if (!g || g.width < 8 || g.height < 8) continue;
    out.push(Math.round(g.x) + "," + Math.round(g.y) + "," + Math.round(g.width) + "," + Math.round(g.height));
  }
}
print(out.join(";"));
"""


def _property_longs(lib, display, window, atom, atom_type):
    actual = ctypes.c_ulong()
    fmt = ctypes.c_int()
    count = ctypes.c_ulong()
    after = ctypes.c_ulong()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    status = lib.XGetWindowProperty(
        display, window, atom, 0, 1024, False, atom_type,
        ctypes.byref(actual), ctypes.byref(fmt), ctypes.byref(count),
        ctypes.byref(after), ctypes.byref(data))
    if status != 0 or not data or count.value == 0:
        if data:
            lib.XFree(ctypes.cast(data, ctypes.c_void_p))
        return []
    values = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
    result = [int(values[index]) for index in range(count.value)]
    lib.XFree(ctypes.cast(data, ctypes.c_void_p))
    return result


def _dock_windows(lib, display):
    root = lib.XDefaultRootWindow(display)
    clients = lib.XInternAtom(display, b"_NET_CLIENT_LIST", False)
    kind = lib.XInternAtom(display, b"_NET_WM_WINDOW_TYPE", False)
    dock = lib.XInternAtom(display, b"_NET_WM_WINDOW_TYPE_DOCK", False)
    windows = []
    for xid in _property_longs(lib, display, root, clients, 33):
        if dock not in _property_longs(lib, display, xid, kind, 4):
            continue
        origin = _client_origin(lib, display, xid)
        size = _window_size(lib, display, xid)
        if origin is None or size is None:
            continue
        windows.append({"xid": xid, "origin": origin, "size": size})
    return windows


def _tray_widgets():
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    result = bus.call_sync(
        "org.kde.plasmashell", "/PlasmaShell", "org.kde.PlasmaShell", "evaluateScript",
        GLib.Variant("(s)", (_TRAY_SCRIPT,)), GLib.VariantType.new("(s)"),
        Gio.DBusCallFlags.NONE, 800, None)
    text = result.unpack()[0] if result is not None else ""
    widgets = []
    for part in str(text).split(";"):
        numbers = part.split(",")
        if len(numbers) != 4:
            continue
        try:
            widgets.append(tuple(int(number) for number in numbers))
        except ValueError:
            continue
    return widgets


def _capture_tray(screen):
    from PIL import ImageGrab
    x, y, width, height = (int(value) for value in screen)
    if width < 8 or height < 8:
        return None
    return ImageGrab.grab(bbox=(x, y, x + width, y + height))


def _matched_slot(screen, icon_file):
    if not icon_file or not icon_file.is_file():
        return None
    try:
        from PIL import Image
        image = _capture_tray(screen)
        if image is None:
            return None
        with Image.open(icon_file) as icon:
            return icon_slot(screen, image, icon)
    except Exception:
        return None


def _tray_view(lib, display, icon_file=None):
    docks = _dock_windows(lib, display)
    for widget in _tray_widgets():
        for dock in docks:
            if widget[0] < -2 or widget[1] < -2:
                continue
            if widget[0] + widget[2] > dock["size"][0] + 4:
                continue
            if widget[1] + widget[3] > dock["size"][1] + 4:
                continue
            if dock["size"][1] > 240:
                continue
            screen = (
                dock["origin"][0] + widget[0],
                dock["origin"][1] + widget[1],
                widget[2],
                widget[3],
            )
            slot = _matched_slot(screen, icon_file)
            if slot is None:
                slot = tray_slot(screen)
            return {"slot": slot}
    return None


class IconFlight:
    def __init__(self):
        self.lib = None
        self.display = None
        self.window = None
        self.xid = 0
        self.origin = None
        self.target = None
        self._opacity_atom = 0
        self._move_atom = 0
        self._tray_check = 0.0
        self._closed = False
        self.icon_file = None

    @classmethod
    def try_open(cls, widget, icon_file):
        flight = cls()
        try:
            if not flight.open(widget, icon_file):
                flight.close()
                return None
        except Exception:
            flight.close()
            return None
        return flight

    def open(self, widget, icon_file):
        surface = widget.get_surface() if hasattr(widget, "get_surface") else None
        if surface is None or not hasattr(surface, "get_xid") or not icon_file or not icon_file.is_file():
            return False
        self.icon_file = icon_file
        scale = max(1, int(surface.get_scale_factor()))
        self.lib = _library()
        self.display = self.lib.XOpenDisplay(None)
        if not self.display:
            return False
        source = _xid(surface)
        point = _client_origin(self.lib, self.display, source)
        size = _window_size(self.lib, self.display, source)
        if not source or point is None or size is None:
            return False
        icon = ICON_PX * scale
        self.origin = icon_start((point[0], point[1], size[0], size[1]), icon)
        self.target = icon_corner(_workarea(surface, scale), icon, scale)
        self._aim_at_tray()
        gtk = _gtk()
        window = gtk.Window()
        window.set_decorated(False)
        window.set_resizable(True)
        window.set_size_request(1, 1)
        window.set_can_focus(False)
        window.set_focus_on_click(False)
        window.add_css_class("tray-flight")
        _clear_flight_chrome(window)
        picture = gtk.Picture.new_for_filename(str(icon_file))
        picture.set_can_shrink(True)
        picture.set_content_fit(gtk.ContentFit.FILL)
        window.set_child(picture)
        window.set_default_size(ICON_PX, ICON_PX)
        window.present()
        self.window = window
        surface = window.get_surface()
        if surface is None:
            return False
        self.xid = _xid(surface)
        if not self.xid:
            return False
        self._move_atom = self.lib.XInternAtom(self.display, b"_NET_MOVERESIZE_WINDOW", False)
        self._opacity_atom = self.lib.XInternAtom(self.display, b"_NET_WM_WINDOW_OPACITY", False)
        self.step(self.origin, 1)
        self._lift()
        return True

    def _aim_at_tray(self):
        self.place_on_tray()

    def place_on_tray(self):
        if self._closed or self.lib is None or self.display is None:
            return False
        now = time.monotonic()
        if now - self._tray_check < 0.12:
            return False
        self._tray_check = now
        try:
            view = _tray_view(self.lib, self.display, self.icon_file)
        except Exception:
            return False
        if view is None:
            return False
        self.target = view["slot"]
        return True

    def step(self, rect, opacity):
        x, y, width, height = (int(value) for value in rect)
        event = _Event()
        event.xclient.type = 33
        event.xclient.send_event = 1
        event.xclient.display = self.display
        event.xclient.window = self.xid
        event.xclient.message_type = self._move_atom
        event.xclient.format = 32
        event.xclient.data[0] = 1 | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11)
        event.xclient.data[1] = x
        event.xclient.data[2] = y
        event.xclient.data[3] = max(1, width)
        event.xclient.data[4] = max(1, height)
        root = self.lib.XDefaultRootWindow(self.display)
        self.lib.XSendEvent(self.display, root, False, (1 << 20) | (1 << 19), ctypes.byref(event))
        self._lift()
        amount = max(0.0, min(1.0, float(opacity)))
        if self.window is not None:
            self.window.set_opacity(amount)
        if self._opacity_atom:
            value = ctypes.c_ulong(int(round(amount * 0xFFFFFFFF)))
            self.lib.XChangeProperty(
                self.display, self.xid, self._opacity_atom, 6, 32, 0, ctypes.byref(value), 1)
        self.lib.XFlush(self.display)

    def _lift(self):
        if self._closed or not self.xid or self.lib is None or self.display is None:
            return
        state = self.lib.XInternAtom(self.display, b"_NET_WM_STATE", False)
        above = self.lib.XInternAtom(self.display, b"_NET_WM_STATE_ABOVE", False)
        skip_taskbar = self.lib.XInternAtom(self.display, b"_NET_WM_STATE_SKIP_TASKBAR", False)
        values = (ctypes.c_ulong * 2)(above, skip_taskbar)
        self.lib.XChangeProperty(
            self.display, self.xid, state, 4, 32, 0,
            ctypes.cast(values, ctypes.c_void_p), 2)
        self.lib.XRaiseWindow(self.display, self.xid)

    def close(self):
        if self._closed:
            return False
        self._closed = True
        display = self.display
        lib = self.lib
        window = self.window
        self.display = None
        self.window = None
        self.xid = 0
        if window is not None:
            window.destroy()
        if display and lib:
            lib.XCloseDisplay(display)
        return False
