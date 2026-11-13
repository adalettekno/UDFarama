from gi.repository import Adw, Gtk

from udfarama.autostart import set_start_at_login


def build_settings(window, enabled, on_change):
    dialog = Adw.Window(title="UDFarama Ayarları")
    dialog.set_default_size(440, 190)
    dialog.set_resizable(False)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    content.set_margin_top(22)
    content.set_margin_bottom(22)
    content.set_margin_start(20)
    content.set_margin_end(20)

    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    caption = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    label = Gtk.Label(label="Bilgisayar açılışında başlat", xalign=0)
    caption.append(label)
    hint = Gtk.Label(label="Pencereyi açmadan sistem tepsisinde başlat", xalign=0)
    hint.add_css_class("dim-label")
    hint.add_css_class("caption")
    caption.append(hint)
    caption.set_hexpand(True)
    row.append(caption)
    switch = Gtk.Switch()
    switch.set_valign(Gtk.Align.CENTER)
    switch.set_active(enabled)
    def changed(widget, _property):
        active = bool(widget.get_active())
        try:
            set_start_at_login(active)
        except OSError as error:
            window.show_error("Başlangıç ayarı kaydedilemedi: " + str(error))
            widget.set_active(not active)
            return
        on_change(active)
    switch.connect("notify::active", changed)
    row.append(switch)
    content.append(row)

    note = Gtk.Label(
        label="Program kapalıyken dosya değişiklikleri izlenmez; açılışta yeniden tarama yapılmaz.",
        xalign=0,
    )
    note.set_wrap(True)
    note.add_css_class("dim-label")
    content.append(note)
    toolbar.set_content(content)
    dialog.set_content(toolbar)
    dialog.set_transient_for(window)
    return dialog
