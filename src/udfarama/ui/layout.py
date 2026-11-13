import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GObject, Gtk, Pango


class SearchResult(GObject.Object):
    __gtype_name__ = "UDFaramaSearchResult"

    path = GObject.Property(type=str, default="")
    relative_path = GObject.Property(type=str, default="")
    basename = GObject.Property(type=str, default="")
    snippet = GObject.Property(type=str, default="")

    @classmethod
    def from_match(cls, match):
        return cls(
            path=match.path,
            relative_path=match.relative_path,
            basename=match.basename,
            snippet=match.snippet,
        )


def install_styles():
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(b"""
        .udf-result { padding: 11px 15px; }
        .udf-result-title { font-weight: 650; }
        .udf-result-location { opacity: 0.72; font-size: 0.91em; }
        .udf-result-snippet { opacity: 0.84; }
        .udf-section { margin-top: 6px; }
        .udf-status { padding: 7px 1px; }
        .udf-count { font-weight: 600; }
    """)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def build_main_layout(window):
    install_styles()
    window.toast_overlay = Adw.ToastOverlay()
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    window.title_label = Gtk.Label(label="UDFarama")
    window.title_label.add_css_class("title")
    header.set_title_widget(window.title_label)
    window._install_minimize_button(header)
    menu = Gio.Menu()
    menu.append("Ayarlar", "win.settings")
    menu.append("Hakkında", "win.about")
    menu.append("Çıkış", "app.exit")
    menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
    menu_button.set_tooltip_text("Uygulama menüsü")
    menu_button.set_menu_model(menu)
    header.pack_end(menu_button)
    toolbar.add_top_bar(header)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    content.set_margin_top(18)
    content.set_margin_bottom(18)
    content.set_margin_start(20)
    content.set_margin_end(20)

    group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
    group.add_css_class("udf-section")
    heading = Gtk.Label(label="ARAMA KLASÖRÜ", xalign=0)
    heading.add_css_class("caption")
    heading.add_css_class("dim-label")
    group.append(heading)
    folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
    folder_row.append(Gtk.Image.new_from_icon_name("folder-symbolic"))
    window.folder_label = Gtk.Label(label="Arama klasörü seçilmedi", xalign=0)
    window.folder_label.set_hexpand(True)
    window.folder_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    folder_row.append(window.folder_label)
    choose_button = Gtk.Button(label="Klasör Seç")
    choose_button.set_action_name("win.choose-folder")
    choose_button.set_tooltip_text("Klasör seç (Ctrl+O)")
    folder_row.append(choose_button)
    group.append(folder_row)
    content.append(group)

    search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    window.search_entry = Gtk.SearchEntry()
    window.search_entry.set_placeholder_text("Belge adı veya metin içinde ara")
    window.search_entry.set_hexpand(True)
    window.search_entry.connect("activate", window.start_search)
    window.search_entry.connect("changed", window._on_search_changed)
    search_row.append(window.search_entry)
    window.spinner = Gtk.Spinner()
    window.spinner.set_visible(False)
    search_row.append(window.spinner)
    window.search_button = Gtk.Button(label="Ara")
    window.search_button.add_css_class("suggested-action")
    window.search_button.connect("clicked", window.on_search_button)
    search_row.append(window.search_button)
    content.append(search_row)

    summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
    summary.add_css_class("udf-status")
    window.state_icon = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
    summary.append(window.state_icon)
    window.status = Gtk.Label(label="Klasör seçin", xalign=0)
    window.status.set_hexpand(True)
    window.status.set_ellipsize(Pango.EllipsizeMode.END)
    summary.append(window.status)
    window.results_label = Gtk.Label(label="", xalign=1)
    window.results_label.add_css_class("udf-count")
    summary.append(window.results_label)
    content.append(summary)

    window.model = Gio.ListStore.new(SearchResult)
    selection = Gtk.SingleSelection.new(window.model)
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", window._setup_item)
    factory.connect("bind", window._bind_item)
    window.list_view = Gtk.ListView.new(selection, factory)
    window.list_view.set_tab_behavior(Gtk.ListTabBehavior.ITEM)
    window.list_view.connect("activate", window.open_selected)
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_vexpand(True)
    scrolled.set_child(window.list_view)

    window.empty_page = Adw.StatusPage(
        icon_name="system-search-symbolic",
        title="Belge aramaya hazır",
        description="Klasör seçip belge adı veya metin içinde arama yapın.",
    )
    window.stack = Gtk.Stack()
    window.stack.set_vexpand(True)
    window.stack.add_named(window.empty_page, "empty")
    window.stack.add_named(scrolled, "results")
    window.stack.set_visible_child_name("empty")
    content.append(window.stack)

    window.more_button = Gtk.Button(label="Daha fazla sonuç yükle")
    window.more_button.set_halign(Gtk.Align.CENTER)
    window.more_button.set_visible(False)
    window.more_button.connect("clicked", window.load_more)
    content.append(window.more_button)

    toolbar.set_content(content)
    window.toast_overlay.set_child(toolbar)
    window.set_content(window.toast_overlay)
