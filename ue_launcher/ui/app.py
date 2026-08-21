"""GTK4 + libadwaita UI for Unreal Launcher."""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .. import branding, icons
from ..config import APP_ID, APP_NAME, Config
from ..engines import EngineInstall, discover_engines, pick_engine
from ..epic import (
    EpicAuthError,
    clear_tokens,
    ensure_fresh_tokens,
    exchange_authorization_code,
    load_tokens,
    open_login_page,
)
from ..epic.cosmos import EngineBlob, list_linux_engine_blobs, open_linux_downloads
from ..epic.engine_install import install_engine
from ..epic.fab import FabAsset, clear_library_cache, list_library_cached, load_library_cache
from ..launch import launch_editor, open_in_file_manager
from ..plugins import (
    asset_kind,
    build_plugin_linux,
    filter_plugin_assets,
    install_fab_content,
    install_fab_plugin,
    install_fab_project,
    is_content_asset,
    is_project_asset,
    plugin_has_linux_binaries,
    plugin_installed_for_asset,
    search_plugins,
)
from ..projects import (
    UProject,
    create_project_from_template,
    discover_projects,
    guess_repo_folder_name,
    import_project_from_git,
    list_templates,
    project_thumbnail_path,
)
from ..thumbnails import cached_thumbnail, fetch_thumbnail


class PluginListItem(GObject.Object):
    """GObject wrapper so Fab assets can live in a Gio.ListStore / Gtk.ListView."""

    __gtype_name__ = "MatesPluginListItem"

    def __init__(self, asset: FabAsset) -> None:
        super().__init__()
        self.asset = asset


class MatesUnrealLauncherApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=f"dev.mates.{APP_ID}",
            # NON_UNIQUE: avoids silent no-op when a stuck instance owns the bus name
            # (common after a failed AppImage launch on Steam Deck).
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.config = Config.load()
        self.engines: list[EngineInstall] = []
        self.projects: list[UProject] = []
        self.engine_blobs: list[EngineBlob] = []
        self.fab_plugins: list[FabAsset] = []
        self.window: Adw.ApplicationWindow | None = None
        self.status: Gtk.Label | None = None
        self.engine_list: Gtk.ListBox | None = None
        self.engine_download_list: Gtk.ListBox | None = None
        self.project_list: Gtk.ListBox | None = None
        self.plugin_list: Gtk.ListView | None = None
        self.plugin_store: Gio.ListStore | None = None
        self.plugin_selection: Gtk.SingleSelection | None = None
        self.plugin_engine_dropdown: Gtk.DropDown | None = None
        self.settings_group: Adw.PreferencesGroup | None = None
        self.preferred_engine_row: Adw.PreferencesRow | None = None
        self._syncing_preferred_engine = False
        self.plugin_search: Gtk.Entry | None = None
        self.account_btn: Gtk.MenuButton | None = None
        self.view_stack: Adw.ViewStack | None = None
        self.toast_overlay: Adw.ToastOverlay | None = None
        self._installing_engine = False
        self._removing_engine = False
        self._installing_plugin = False
        self._plugin_search_query = ""
        self._plugin_textures: dict[str, Gdk.Texture] = {}
        self._project_textures: dict[str, Gdk.Texture] = {}
        self._plugin_search_timeout_id: int = 0

    def do_startup(self) -> None:  # noqa: N802
        Adw.Application.do_startup(self)
        self._apply_branding()
        self._install_actions()

    def _install_actions(self) -> None:
        actions = [
            ("sign-in", lambda *_: self._login_dialog()),
            ("sign-out", lambda *_: self._logout()),
            ("refresh-catalog", lambda *_: self._load_engine_blobs_async()),
            ("refresh-plugins", lambda *_: self._load_plugins_async(force_refresh=True)),
            ("open-downloads", lambda *_: self._open_epic_linux_downloads()),
            ("refresh", lambda *_: self.refresh_all()),
        ]
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    def _apply_branding(self) -> None:
        style = Adw.StyleManager.get_default()
        style.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        provider = Gtk.CssProvider()
        provider.load_from_data(branding.css().encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            self._setup_icon_theme(display)

    def _setup_icon_theme(self, display: Gdk.Display) -> None:
        """Prefer bundled Lucide + Adwaita so Steam Deck / KDE doesn't substitute random icons."""
        theme = Gtk.IconTheme.get_for_display(display)
        search: list[str] = []
        # Lucide icons shipped with the app (first so mates-* names resolve reliably).
        if icons.ICONS_DIR.is_dir():
            search.append(str(icons.ICONS_DIR))
        appdir = os.environ.get("APPDIR")
        if not appdir:
            # site-packages/ue_launcher → usr/lib/python3 → usr → AppDir
            guessed = Path(__file__).resolve().parents[4]
            if (guessed / "usr" / "share" / "icons").is_dir():
                appdir = str(guessed)
        if appdir:
            bundled = Path(appdir) / "usr" / "share" / "icons"
            if bundled.is_dir():
                search.append(str(bundled))
        if branding.ICONS_HICOLOR.parent.is_dir():
            search.append(str(branding.ICONS_HICOLOR.parent))
        host = Path("/usr/share/icons")
        if host.is_dir():
            search.append(str(host))
        for path in search:
            theme.add_search_path(path)

    def _icon_paintable(self, name: str, size: int = 16) -> Gdk.Paintable | None:
        display = Gdk.Display.get_default()
        if display is None:
            return None
        theme = Gtk.IconTheme.get_for_display(display)
        if not theme.has_icon(name):
            return None
        try:
            return theme.lookup_icon(
                name,
                None,
                size,
                1,
                Gtk.TextDirection.NONE,
                Gtk.IconLookupFlags(0),
            )
        except GLib.Error:
            return None

    def _set_icon_on_image(self, image: Gtk.Image, name: str, size: int) -> None:
        png = icons.png_path(name, size)
        if png is not None:
            try:
                gicon = Gio.FileIcon.new(Gio.File.new_for_path(str(png)))
                image.set_from_gicon(gicon)
                image.set_pixel_size(size)
                image.add_css_class(f"mates-icon-{size}")
                return
            except (GLib.Error, TypeError, AttributeError):
                try:
                    texture = Gdk.Texture.new_from_filename(str(png))
                    image.set_from_paintable(texture)
                    image.set_pixel_size(size)
                    image.add_css_class(f"mates-icon-{size}")
                    return
                except GLib.Error:
                    pass
        paintable = self._icon_paintable(name, size)
        if paintable is not None:
            image.set_from_paintable(paintable)
            image.set_pixel_size(size)
            image.add_css_class(f"mates-icon-{size}")

    def _icon_image(self, name: str, size: int = 16, tooltip: str = "") -> Gtk.Image:
        image = Gtk.Image()
        image.set_valign(Gtk.Align.CENTER)
        self._set_icon_on_image(image, name, size)
        if tooltip:
            image.set_tooltip_text(tooltip)
        return image

    def _brand_mark_widget(self, size: int = 22) -> Gtk.Widget | None:
        path = branding.mark_path()
        if path is None:
            return None
        try:
            texture = Gdk.Texture.new_from_filename(str(path))
        except GLib.Error:
            return None
        image = Gtk.Image.new_from_paintable(texture)
        image.set_pixel_size(size)
        image.set_hexpand(False)
        image.set_vexpand(False)
        image.add_css_class("mates-mark")
        image.set_tooltip_text(APP_NAME)
        return image

    def _add_row_actions(self, row: Adw.ActionRow, *buttons: Gtk.Widget) -> None:
        """Push row action buttons to the trailing edge of the row."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        box.set_hexpand(True)
        box.set_halign(Gtk.Align.END)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        box.append(spacer)
        for btn in buttons:
            box.append(btn)
        row.add_suffix(box)

    def _section_header(self, title: str, *trailing: Gtk.Widget) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("mates-section-title")
        label.set_hexpand(True)
        row.append(label)
        for widget in trailing:
            row.append(widget)
        return row

    def _flat_btn(self, icon: str, tooltip: str = "") -> Gtk.Button:
        """Icon-only flat button; tooltip carries the accessible name."""
        btn = Gtk.Button()
        btn.set_child(self._icon_image(icon, 16))
        btn.add_css_class("flat")
        btn.add_css_class("mates-row-btn")
        if tooltip:
            btn.set_tooltip_text(tooltip)
        return btn

    def _header_action_btn(self, icon: str, tooltip: str) -> Gtk.Button:
        """Compact icon-only header control (Deck-friendly hit target + tooltip)."""
        btn = self._flat_btn(icon=icon, tooltip=tooltip)
        btn.add_css_class("mates-header-action")
        return btn

    def _view_tab_button(
        self,
        group: Gtk.ToggleButton | None,
        name: str,
        title: str,
        icon: str,
    ) -> Gtk.ToggleButton:
        """Tab toggle with Lucide PNG icon (ViewSwitcher icon theme lookup is unreliable)."""
        btn = Gtk.ToggleButton()
        btn.add_css_class("flat")
        btn.add_css_class("mates-view-tab")
        if group is not None:
            btn.set_group(group)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        inner.append(self._icon_image(icon, 16))
        inner.append(Gtk.Label(label=title))
        btn.set_child(inner)
        btn.set_tooltip_text(title)

        def _on_toggled(toggle: Gtk.ToggleButton) -> None:
            if not toggle.get_active() or self.view_stack is None:
                return
            self.view_stack.set_visible_child_name(name)

        btn.connect("toggled", _on_toggled)
        return btn

    def _add_view_page(
        self,
        stack: Adw.ViewStack,
        tab_bar: Gtk.Box,
        tab_group: Gtk.ToggleButton | None,
        name: str,
        title: str,
        icon: str,
        page: Gtk.Widget,
    ) -> Gtk.ToggleButton:
        stack.add_titled(page, name, title)
        btn = self._view_tab_button(tab_group, name, title, icon)
        tab_bar.append(btn)
        return btn

    def _menu_item(self, label: str, action: str, icon: str) -> Gio.MenuItem:
        item = Gio.MenuItem.new(label, action)
        png = icons.png_path(icon, 16)
        if png is not None:
            item.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(str(png))))
        else:
            item.set_icon(Gio.ThemedIcon.new(icon))
        return item

    def _status_icon(self, icon: str, tooltip: str, size: int = 16) -> Gtk.Image:
        return self._icon_image(icon, size, tooltip=tooltip)

    def do_activate(self) -> None:  # noqa: N802
        if self.window:
            self.window.present()
            return

        self.window = Adw.ApplicationWindow(application=self)
        self.window.set_title(APP_NAME)
        self.window.set_default_size(920, 600)
        self.window.set_icon_name(branding.ICON_NAME)

        self.toast_overlay = Adw.ToastOverlay()
        self.window.set_content(self.toast_overlay)

        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        header.add_css_class("flat")

        refresh_btn = self._flat_btn(icons.REFRESH, "Rescan engines & projects")
        refresh_btn.connect("clicked", lambda *_: self.refresh_all())

        start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        start_box.add_css_class("mates-header-start")
        start_box.set_margin_start(16)
        start_box.set_margin_end(4)
        mark = self._brand_mark_widget(20)
        if mark is not None:
            mark_wrap = Gtk.Box()
            mark_wrap.add_css_class("mates-header-mark")
            mark_wrap.append(mark)
            start_box.append(mark_wrap)
        start_box.append(refresh_btn)
        header.pack_start(start_box)

        account_menu = Gio.Menu()
        account_menu.append_item(self._menu_item("Sign in with Epic", "app.sign-in", icons.LOGIN))
        account_menu.append_item(self._menu_item("Sign out", "app.sign-out", icons.LOGOUT))
        account_menu.append_item(
            self._menu_item("Refresh catalog", "app.refresh-catalog", icons.REFRESH)
        )
        account_menu.append_item(
            self._menu_item("Refresh plugins", "app.refresh-plugins", icons.PLUGIN)
        )
        account_menu.append_item(
            self._menu_item("Open in browser", "app.open-downloads", icons.EXTERNAL)
        )

        self.account_btn = Gtk.MenuButton()
        self.account_btn.set_child(self._icon_image(icons.ACCOUNT, 16, tooltip="Account"))
        self.account_btn.set_menu_model(account_menu)
        self.account_btn.set_always_show_arrow(False)
        self.account_btn.add_css_class("flat")
        header.pack_end(self.account_btn)

        toolbar.add_top_bar(header)

        self.view_stack = Adw.ViewStack()
        tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tab_bar.add_css_class("linked")

        tab_group: Gtk.ToggleButton | None = None
        tab_group = self._add_view_page(
            self.view_stack,
            tab_bar,
            tab_group,
            "engines",
            "Engines",
            icons.ENGINES,
            self._build_engines_page(),
        )
        tab_group.set_active(True)
        self._add_view_page(
            self.view_stack,
            tab_bar,
            tab_group,
            "projects",
            "Projects",
            icons.FOLDER,
            self._build_projects_page(),
        )
        self._add_view_page(
            self.view_stack,
            tab_bar,
            tab_group,
            "plugins",
            "Library",
            icons.LIBRARY,
            self._build_plugins_page(),
        )
        self._add_view_page(
            self.view_stack,
            tab_bar,
            tab_group,
            "settings",
            "Settings",
            icons.SETTINGS,
            self._build_settings_page(),
        )

        header.set_title_widget(tab_bar)
        toolbar.set_content(self.view_stack)

        self.status = Gtk.Label(label="Ready", xalign=0)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("mates-status")
        toolbar.add_bottom_bar(self.status)

        self.refresh_all()
        self.window.present()

    # --- pages -----------------------------------------------------------------

    def _build_engines_page(self) -> Gtk.Widget:
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(14)
        root.set_margin_end(14)

        # Installed
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.add_css_class("mates-panel")
        left.set_hexpand(True)
        left.set_size_request(280, -1)

        left.append(self._section_header("Installed"))

        scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.engine_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.engine_list.add_css_class("boxed-list")
        self.engine_list.set_activate_on_single_click(True)
        self.engine_list.connect(
            "row-activated",
            lambda *_: self._launch_selected_engine(),
        )
        scrolled.set_child(self.engine_list)
        left.append(scrolled)
        root.append(left)

        # Available
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.add_css_class("mates-panel")
        right.set_hexpand(True)
        right.set_size_request(320, -1)

        refresh_avail = self._flat_btn(
            icon=icons.REFRESH, tooltip="Refresh available builds"
        )
        refresh_avail.connect("clicked", lambda *_: self._load_engine_blobs_async())
        right.append(self._section_header("Available", refresh_avail))

        dl_scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        dl_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.engine_download_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.engine_download_list.add_css_class("boxed-list")
        self.engine_download_list.set_activate_on_single_click(True)
        self.engine_download_list.connect(
            "row-activated",
            lambda *_: self._confirm_install_selected_blob(),
        )
        dl_scrolled.set_child(self.engine_download_list)
        right.append(dl_scrolled)
        root.append(right)

        return root

    def _build_projects_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.add_css_class("mates-panel")

        new_btn = self._header_action_btn(
            icons.PLUS, "Create a project from a template"
        )
        new_btn.connect("clicked", lambda *_: self._new_project_dialog())
        git_btn = self._header_action_btn(
            icons.GIT, "Clone a project from git"
        )
        git_btn.connect("clicked", lambda *_: self._import_git_project_dialog())
        browse_btn = self._header_action_btn(
            icons.FOLDER_OPEN, "Open a .uproject on disk"
        )
        browse_btn.connect("clicked", lambda *_: self._browse_project())
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        actions.add_css_class("linked")
        actions.append(new_btn)
        actions.append(git_btn)
        actions.append(browse_btn)
        box.append(self._section_header("Projects", actions))

        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.project_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.project_list.add_css_class("boxed-list")
        self.project_list.set_activate_on_single_click(True)
        self.project_list.connect("row-activated", lambda *_: self._open_selected_project())
        scrolled.set_child(self.project_list)
        box.append(scrolled)
        return box

    def _build_plugins_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.add_css_class("mates-panel")

        engine_labels = Gtk.StringList.new([])
        self.plugin_engine_dropdown = Gtk.DropDown(model=engine_labels)
        self.plugin_engine_dropdown.set_tooltip_text("Install into this engine")
        self.plugin_engine_dropdown.set_size_request(160, -1)
        self.plugin_engine_dropdown.connect(
            "notify::selected",
            lambda *_: self._populate_plugins(),
        )

        refresh_btn = self._flat_btn(
            icon=icons.REFRESH,
            tooltip="Refresh Fab library from Epic",
        )
        refresh_btn.connect("clicked", lambda *_: self._load_plugins_async(force_refresh=True))
        box.append(self._section_header("Library", self.plugin_engine_dropdown, refresh_btn))

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_row.append(self._icon_image(icons.SEARCH, 16, tooltip="Search"))
        self.plugin_search = Gtk.Entry()
        self.plugin_search.set_placeholder_text("Search plugins & projects…")
        self.plugin_search.set_hexpand(True)
        self.plugin_search.connect("changed", self._on_plugin_search_changed)
        search_row.append(self.plugin_search)
        box.append(search_row)

        scrolled = Gtk.ScrolledWindow(vexpand=True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.plugin_store = Gio.ListStore.new(PluginListItem)
        self.plugin_selection = Gtk.SingleSelection.new(self.plugin_store)
        self.plugin_selection.set_autoselect(False)
        self.plugin_selection.set_can_unselect(True)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_plugin_list_setup)
        factory.connect("bind", self._on_plugin_list_bind)
        factory.connect("unbind", self._on_plugin_list_unbind)

        self.plugin_list = Gtk.ListView(model=self.plugin_selection, factory=factory)
        self.plugin_list.add_css_class("boxed-list")
        self.plugin_list.set_single_click_activate(True)
        self.plugin_list.connect("activate", self._on_plugin_list_activate)
        scrolled.set_child(self.plugin_list)
        box.append(scrolled)
        return box

    def _build_settings_page(self) -> Gtk.Widget:
        clamp = Adw.Clamp(maximum_size=640)
        clamp.set_margin_top(12)
        clamp.set_margin_bottom(16)
        clamp.set_margin_start(14)
        clamp.set_margin_end(14)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(box)

        group = Adw.PreferencesGroup()
        group.add_css_class("mates-settings")
        self.settings_group = group

        engine_row = Adw.EntryRow(title="Engine roots")
        engine_row.set_text(", ".join(str(p) for p in self.config.engine_roots))
        group.add(engine_row)

        project_row = Adw.EntryRow(title="Project scan roots")
        project_row.set_text(", ".join(str(p) for p in self.config.project_scan_roots))
        group.add(project_row)

        engine_install_row = Adw.EntryRow(title="Install directory")
        engine_install_row.set_text(str(self.config.engine_install_dir))
        group.add(engine_install_row)

        engine_cache_row = Adw.EntryRow(title="Zip cache")
        engine_cache_row.set_text(str(self.config.engine_cache_dir))
        group.add(engine_cache_row)

        self._sync_preferred_engine_row()

        box.append(group)

        def _persist_settings(*_args: object) -> None:
            self.config.set(
                "engine_roots",
                [p.strip() for p in engine_row.get_text().split(",") if p.strip()],
            )
            self.config.set(
                "project_scan_roots",
                [p.strip() for p in project_row.get_text().split(",") if p.strip()],
            )
            self.config.set("engine_install_dir", engine_install_row.get_text().strip())
            self.config.set("engine_cache_dir", engine_cache_row.get_text().strip())
            self.config.save()
            self.refresh_all()

        for row in (
            engine_row,
            project_row,
            engine_install_row,
            engine_cache_row,
        ):
            row.connect("apply", _persist_settings)

        about = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        about.add_css_class("mates-about")
        about.set_margin_top(8)
        about_mark = self._brand_mark_widget(28)
        if about_mark is not None:
            about.append(about_mark)
        about_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=APP_NAME, xalign=0)
        title.add_css_class("mates-brand-title")
        studio = Gtk.LinkButton(uri=branding.STUDIO_URL, label=branding.STUDIO_DOMAIN)
        studio.set_halign(Gtk.Align.START)
        about_text.append(title)
        about_text.append(studio)
        about.append(about_text)
        box.append(about)
        return clamp

    # --- data refresh ----------------------------------------------------------

    def refresh_all(self) -> None:
        self._set_status("Scanning…")
        self.engines = discover_engines(self.config)
        self.projects = discover_projects(self.config)
        self._populate_engines()
        self._populate_projects()
        self._populate_plugin_engine_dropdown()
        self._sync_preferred_engine_row()
        self._update_account_label()
        self._set_status(f"{len(self.engines)} engines · {len(self.projects)} projects")
        if load_tokens():
            self._load_engine_blobs_async()
            self._load_plugins_async()

    def _populate_engines(self) -> None:
        assert self.engine_list is not None
        while child := self.engine_list.get_first_child():
            self.engine_list.remove(child)
        for eng in self.engines:
            row = Adw.ActionRow()
            row.set_title(eng.version_label.replace("UE_", "UE "))
            row.set_subtitle(str(eng.path))
            row.set_activatable(True)
            row.engine = eng  # type: ignore[attr-defined]

            # Icon actions — Launch stays visible; folder is secondary.
            launch = self._flat_btn(
                icon=icons.PLAY,
                tooltip=f"Launch {eng.version_label}",
            )

            def _launch(_btn: Gtk.Button, engine: EngineInstall = eng) -> None:
                launch_editor(engine, self.config)
                self.toast(f"Launching {engine.version_label}")

            launch.connect("clicked", _launch)

            folder = self._flat_btn(icon=icons.FOLDER, tooltip="Open folder")
            folder.connect(
                "clicked",
                lambda _b, path=eng.path: open_in_file_manager(path),
            )

            remove = self._flat_btn(
                icon=icons.TRASH,
                tooltip=f"Remove {eng.version_label}",
            )
            remove.connect("clicked", lambda _b, engine=eng: self._confirm_remove_engine(engine))

            self._add_row_actions(row, folder, launch, remove)
            self.engine_list.append(row)
        if self.engines:
            self.engine_list.select_row(self.engine_list.get_row_at_index(0))

    def _populate_engine_blobs(self) -> None:
        assert self.engine_download_list is not None
        while child := self.engine_download_list.get_first_child():
            self.engine_download_list.remove(child)

        installed_dirs = {eng.path.name for eng in self.engines}
        available = [b for b in self.engine_blobs if b.install_dirname not in installed_dirs]

        for blob in available:
            size = f"{blob.size_gib:.1f} GiB" if blob.size else "—"
            row = Adw.ActionRow()
            row.set_title(blob.install_dirname.replace("UE_", "UE "))
            row.set_subtitle(size)
            row.set_activatable(True)
            row.blob = blob  # type: ignore[attr-defined]

            install = self._flat_btn(
                icon=icons.DOWNLOAD,
                tooltip=f"Install {blob.name}",
            )
            install.add_css_class("mates-install-btn")

            def _install(_btn: Gtk.Button, target: EngineBlob = blob) -> None:
                self._confirm_install_blob(target)

            install.connect("clicked", _install)
            self._add_row_actions(row, install)
            self.engine_download_list.append(row)

        if available:
            self.engine_download_list.select_row(
                self.engine_download_list.get_row_at_index(0)
            )

    def _populate_projects(self) -> None:
        assert self.project_list is not None
        while child := self.project_list.get_first_child():
            self.project_list.remove(child)
        for proj in self.projects:
            kind = "C++" if proj.has_code else "Blueprint"
            assoc = proj.engine_association or "—"
            row = Adw.ActionRow()
            row.set_title(proj.name)
            row.set_subtitle(f"{kind} · UE {assoc}")
            row.set_activatable(True)
            row.project = proj  # type: ignore[attr-defined]

            open_btn = self._flat_btn(
                icon=icons.PLAY,
                tooltip=f"Open {proj.name}",
            )
            folder_btn = self._flat_btn(
                icon=icons.FOLDER,
                tooltip="Show project folder",
            )

            def _open(_btn: Gtk.Button, project: UProject = proj) -> None:
                assert self.project_list is not None
                for i, p in enumerate(self.projects):
                    if p.path == project.path:
                        self.project_list.select_row(self.project_list.get_row_at_index(i))
                        break
                self._open_selected_project()

            def _folder(_btn: Gtk.Button, project: UProject = proj) -> None:
                open_in_file_manager(project.directory)

            open_btn.connect("clicked", _open)
            folder_btn.connect("clicked", _folder)
            self._add_row_actions(row, folder_btn, open_btn)

            thumb = self._icon_image(icons.FOLDER, 32)
            thumb.add_css_class("mates-project-icon")
            row.add_prefix(thumb)
            self._bind_project_icon(thumb, proj)
            self.project_list.append(row)

    def _bind_project_icon(self, image: Gtk.Image, project: UProject) -> None:
        key = str(project.path)
        cached = self._project_textures.get(key)
        if cached is not None:
            image.set_from_paintable(cached)
            return
        path = project_thumbnail_path(project)
        if path is None:
            mark = branding.mark_path()
            path = mark if mark is not None else None
        if path is None:
            return
        try:
            texture = Gdk.Texture.new_from_filename(str(path))
        except GLib.Error:
            return
        self._project_textures[key] = texture
        image.set_from_paintable(texture)

    def _populate_plugin_engine_dropdown(self) -> None:
        if not self.plugin_engine_dropdown:
            return
        labels = [eng.path.name for eng in self.engines] or ["No engines"]
        model = Gtk.StringList.new(labels)
        self.plugin_engine_dropdown.set_model(model)
        preferred = self.config.preferred_engine
        for i, eng in enumerate(self.engines):
            if eng.version_label == preferred or preferred in eng.path.name:
                self.plugin_engine_dropdown.set_selected(i)
                break

    def _sync_preferred_engine_row(self) -> None:
        group = self.settings_group
        if group is None:
            return

        if self.preferred_engine_row is not None:
            group.remove(self.preferred_engine_row)
            self.preferred_engine_row = None

        if not self.engines:
            row = Adw.ActionRow(title="Preferred engine")
            row.set_subtitle("Not installed")
            row.set_activatable(False)
            self.preferred_engine_row = row
            group.add(row)
            return

        combo = Adw.ComboRow(title="Preferred engine")
        labels = [eng.version_label.replace("UE_", "UE ") for eng in self.engines]
        combo.set_model(Gtk.StringList.new(labels))

        preferred = self.config.preferred_engine
        selected = 0
        for i, eng in enumerate(self.engines):
            if eng.version_label == preferred or preferred in eng.path.name:
                selected = i
                break
        self._syncing_preferred_engine = True
        try:
            combo.set_selected(selected)
        finally:
            self._syncing_preferred_engine = False

        combo.connect("notify::selected", self._on_preferred_engine_changed)
        self.preferred_engine_row = combo
        group.add(combo)

    def _on_preferred_engine_changed(self, row: Adw.ComboRow, _pspec: GObject.ParamSpec) -> None:
        if self._syncing_preferred_engine or not self.engines:
            return
        idx = int(row.get_selected())
        if idx < 0 or idx >= len(self.engines):
            return
        label = self.engines[idx].version_label
        if self.config.preferred_engine == label:
            return
        self.config.set("preferred_engine", label)
        self.config.save()
        self._populate_plugin_engine_dropdown()

    def _selected_plugin_engine(self) -> EngineInstall | None:
        if not self.plugin_engine_dropdown or not self.engines:
            return None
        idx = int(self.plugin_engine_dropdown.get_selected())
        if idx < 0 or idx >= len(self.engines):
            return pick_engine(self.engines, self.config.preferred_engine)
        return self.engines[idx]

    def _on_plugin_search_changed(self, entry: Gtk.Entry) -> None:
        self._plugin_search_query = entry.get_text()
        if self._plugin_search_timeout_id:
            GLib.source_remove(self._plugin_search_timeout_id)
            self._plugin_search_timeout_id = 0

        def _apply() -> bool:
            self._plugin_search_timeout_id = 0
            self._populate_plugins()
            return False

        self._plugin_search_timeout_id = GLib.timeout_add(120, _apply)

    def _on_plugin_list_activate(self, _list: Gtk.ListView, position: int) -> None:
        if self.plugin_store is None:
            return
        item = self.plugin_store.get_item(position)
        if not isinstance(item, PluginListItem):
            return
        asset = item.asset
        kind = asset_kind(asset)
        if kind == "plugin":
            engine = self._selected_plugin_engine()
            installed = (
                plugin_installed_for_asset(asset, engine=engine) if engine else None
            )
            if installed and not plugin_has_linux_binaries(installed):
                self._build_installed_plugin(installed)
                return
            if installed:
                self.toast(f"{asset.title} is already installed")
                return
        self._plugin_install_dialog(asset)

    def _on_plugin_list_setup(
        self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        list_item.set_activatable(True)
        row = Adw.ActionRow()
        row.set_activatable(True)

        image = self._icon_image(icons.PLUGIN, 32)
        image.add_css_class("mates-plugin-icon")
        row.add_prefix(image)

        badge = self._status_icon(icons.CHECK, "Installed")
        badge.set_visible(False)
        row.add_suffix(badge)

        action_btn = self._flat_btn(icon=icons.DOWNLOAD, tooltip="Install")
        action_btn.add_css_class("mates-install-btn")
        action_btn.set_visible(False)
        row.add_suffix(action_btn)

        row._plugin_image = image  # type: ignore[attr-defined]
        row._plugin_badge = badge  # type: ignore[attr-defined]
        row._plugin_action = action_btn  # type: ignore[attr-defined]
        row._plugin_action_handler = 0  # type: ignore[attr-defined]
        list_item.set_child(row)

    def _on_plugin_list_unbind(
        self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        row = list_item.get_child()
        if row is None:
            return
        action_btn: Gtk.Button = row._plugin_action  # type: ignore[attr-defined]
        handler = int(getattr(row, "_plugin_action_handler", 0) or 0)
        if handler:
            action_btn.disconnect(handler)
            row._plugin_action_handler = 0  # type: ignore[attr-defined]
        image: Gtk.Image = row._plugin_image  # type: ignore[attr-defined]
        image.plugin_asset_id = None  # type: ignore[attr-defined]
        self._set_icon_on_image(image, icons.PLUGIN, 32)

    def _on_plugin_list_bind(
        self, _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        item = list_item.get_item()
        row = list_item.get_child()
        if not isinstance(item, PluginListItem) or row is None:
            return

        asset = item.asset
        kind = asset_kind(asset)
        kind_label = {
            "plugin": "Plugin",
            "project": "Project",
            "content": "Content",
        }.get(kind, kind.title())
        engines = ", ".join(asset.engine_versions[-3:]) or "—"
        row.set_title(GLib.markup_escape_text(asset.title))
        row.set_subtitle(f"{kind_label} · {engines}")

        image: Gtk.Image = row._plugin_image  # type: ignore[attr-defined]
        badge: Gtk.Image = row._plugin_badge  # type: ignore[attr-defined]
        action_btn: Gtk.Button = row._plugin_action  # type: ignore[attr-defined]

        # Disconnect previous bind's handler before wiring a new one.
        handler = int(getattr(row, "_plugin_action_handler", 0) or 0)
        if handler:
            action_btn.disconnect(handler)
            row._plugin_action_handler = 0  # type: ignore[attr-defined]

        self._bind_plugin_icon(image, asset)

        engine = self._selected_plugin_engine()
        installed = (
            plugin_installed_for_asset(asset, engine=engine)
            if kind == "plugin" and engine
            else None
        )
        if installed and plugin_has_linux_binaries(installed):
            badge.set_visible(True)
            action_btn.set_visible(False)
        elif installed:
            badge.set_visible(False)
            action_btn.set_visible(True)
            action_btn.set_child(self._icon_image(icons.BUILD, 16))
            action_btn.set_tooltip_text(f"Build {asset.title} for Linux")

            def _build(_btn: Gtk.Button, path: Path = installed) -> None:
                self._build_installed_plugin(path)

            row._plugin_action_handler = action_btn.connect("clicked", _build)  # type: ignore[attr-defined]
        else:
            badge.set_visible(False)
            action_btn.set_visible(True)
            action_btn.set_child(self._icon_image(icons.DOWNLOAD, 16))
            action_btn.set_tooltip_text(f"Install {asset.title}")

            def _install(_btn: Gtk.Button, target: FabAsset = asset) -> None:
                self._plugin_install_dialog(target)

            row._plugin_action_handler = action_btn.connect("clicked", _install)  # type: ignore[attr-defined]

    def _bind_plugin_icon(self, image: Gtk.Image, asset: FabAsset) -> None:
        image.plugin_asset_id = asset.asset_id  # type: ignore[attr-defined]
        self._set_icon_on_image(image, icons.PLUGIN, 32)

        texture = self._plugin_textures.get(asset.asset_id)
        if texture is not None:
            image.set_from_paintable(texture)
            return

        path = cached_thumbnail(asset.asset_id, asset.thumbnail_url)
        if path is not None:
            try:
                texture = Gdk.Texture.new_from_filename(str(path))
                self._plugin_textures[asset.asset_id] = texture
                image.set_from_paintable(texture)
                return
            except GLib.Error:
                pass

        if asset.thumbnail_url:
            self._load_plugin_thumbnail_async(asset, image)

    def _load_plugin_thumbnail_async(self, asset: FabAsset, image: Gtk.Image) -> None:
        asset_id = asset.asset_id
        url = asset.thumbnail_url

        def worker() -> None:
            path = fetch_thumbnail(asset_id, url)

            def done() -> bool:
                if path is None or not path.is_file():
                    return False
                try:
                    texture = Gdk.Texture.new_from_filename(str(path))
                except GLib.Error:
                    return False
                self._plugin_textures[asset_id] = texture
                if getattr(image, "plugin_asset_id", None) == asset_id:
                    image.set_from_paintable(texture)
                return False

            GLib.idle_add(done)

        image.plugin_asset_id = asset_id  # type: ignore[attr-defined]
        threading.Thread(target=worker, daemon=True).start()

    def _populate_plugins(self) -> None:
        if self.plugin_store is None:
            return

        visible = search_plugins(self.fab_plugins, self._plugin_search_query)
        items = [PluginListItem(asset) for asset in visible]
        self.plugin_store.splice(0, self.plugin_store.get_n_items(), items)

        if self.fab_plugins:
            shown = len(visible)
            total = len(self.fab_plugins)
            if self._plugin_search_query.strip():
                self._set_status(f"{shown} of {total} match(es)")
            else:
                plugins_n = sum(1 for a in self.fab_plugins if asset_kind(a) == "plugin")
                projects_n = sum(1 for a in self.fab_plugins if asset_kind(a) == "project")
                content_n = sum(1 for a in self.fab_plugins if asset_kind(a) == "content")
                parts = [f"{plugins_n} plugin(s)", f"{projects_n} project(s)"]
                if content_n:
                    parts.append(f"{content_n} content")
                self._set_status(" · ".join(parts))

    def _update_account_label(self) -> None:
        if not self.account_btn:
            return
        tokens = load_tokens()
        if not tokens:
            self.account_btn.set_child(
                self._icon_image(icons.ACCOUNT, 16, tooltip="Account — sign in with Epic")
            )
            return
        try:
            tokens = ensure_fresh_tokens(tokens)
            name = tokens.display_name or tokens.account_id[:8]
            self.account_btn.set_child(
                self._icon_image(icons.ACCOUNT, 16, tooltip=f"Signed in as {name}")
            )
        except EpicAuthError:
            self.account_btn.set_child(
                self._icon_image(
                    icons.ACCOUNT, 16, tooltip="Session expired — sign in again"
                )
            )

    # --- actions ---------------------------------------------------------------

    def _selected_engine(self) -> EngineInstall | None:
        assert self.engine_list is not None
        row = self.engine_list.get_selected_row()
        return getattr(row, "engine", None) if row else None

    def _selected_blob(self) -> EngineBlob | None:
        assert self.engine_download_list is not None
        row = self.engine_download_list.get_selected_row()
        return getattr(row, "blob", None) if row else None

    def _selected_project(self) -> UProject | None:
        assert self.project_list is not None
        row = self.project_list.get_selected_row()
        return getattr(row, "project", None) if row else None

    def _launch_selected_engine(self) -> None:
        eng = self._selected_engine() or pick_engine(self.engines, self.config.preferred_engine)
        if not eng:
            self.toast("No engine found")
            return
        launch_editor(eng, self.config)
        self.toast(f"Launching {eng.version_label}")

    def _open_selected_engine_folder(self) -> None:
        eng = self._selected_engine()
        if eng:
            open_in_file_manager(eng.path)

    def _load_engine_blobs_async(self) -> None:
        if not load_tokens():
            self.toast("Sign in with Epic first")
            return
        self._set_status("Fetching available Linux engines…")

        def worker() -> None:
            try:
                blobs = list_linux_engine_blobs()
                err: str | None = None
            except Exception as exc:  # noqa: BLE001
                blobs = []
                err = str(exc)

            def done() -> bool:
                if err:
                    self.toast(err)
                    short = err if len(err) < 80 else err[:77] + "…"
                    self._set_status(f"Engine catalog failed — {short}")
                else:
                    self.engine_blobs = blobs
                    self._populate_engine_blobs()
                    self._set_status(f"{len(blobs)} Linux engine build(s) available")
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _load_plugins_async(self, *, force_refresh: bool = False) -> None:
        tokens = load_tokens()
        if not tokens:
            self.toast("Sign in with Epic first")
            return

        if not force_refresh:
            cached = load_library_cache(tokens.account_id)
            if cached is not None:
                assets, age = cached
                self.fab_plugins = filter_plugin_assets(assets)
                self._populate_plugins()
                if age < 60:
                    age_label = "just now"
                else:
                    age_label = f"{int(age // 60)}m ago"
                self._set_status(f"{len(self.fab_plugins)} library item(s) · cached {age_label}")
                return

        self._set_status("Loading Fab library…")

        def worker() -> None:
            try:
                assets, _from_cache = list_library_cached(
                    tokens,
                    force_refresh=True,
                )
                plugins = filter_plugin_assets(assets)
                err: str | None = None
            except Exception as exc:  # noqa: BLE001
                plugins = []
                err = str(exc)

            def done() -> bool:
                if err:
                    self.toast(err)
                    self._set_status("Fab library failed")
                else:
                    self.fab_plugins = plugins
                    self._populate_plugins()
                    self._set_status(f"{len(plugins)} library item(s) · updated")
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _plugin_install_dialog(self, asset: FabAsset) -> None:
        if self._installing_plugin:
            self.toast("A plugin install is already running")
            return
        if not load_tokens():
            self.toast("Sign in with Epic first")
            return
        if not self.engines:
            self.toast("No engine installed")
            return

        kind = asset_kind(asset)
        as_project_pack = is_project_asset(asset)
        as_content = is_content_asset(asset)

        if as_content and not self.projects:
            self.toast("Add a project first — content installs into a project")
            return

        dialog = Adw.AlertDialog()
        dialog.set_heading(asset.title)
        if as_project_pack:
            dialog.set_body("Install this Fab project to your projects folder.")
        elif as_content:
            dialog.set_body(
                "Install this Fab content into the project's Content folder "
                "(World Partition maps need assets at Content root)."
            )
        else:
            dialog.set_body(
                "Fab plugins ship Win/Mac binaries only. "
                "Engine installs are compiled for Linux after download "
                "(needs the engine toolchain — a few minutes)."
            )

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_top(8)

        # Target mode for code plugins: engine vs project
        target_dropdown: Gtk.DropDown | None = None
        project_dropdown: Gtk.DropDown | None = None

        engine_labels = [eng.path.name for eng in self.engines]
        engine_dd = Gtk.DropDown(model=Gtk.StringList.new(engine_labels))
        pref = self.config.preferred_engine
        for i, eng in enumerate(self.engines):
            if eng.version_label == pref or pref in eng.path.name:
                engine_dd.set_selected(i)
                break
        # Prefer the plugins-page engine selection when present
        current = self._selected_plugin_engine()
        if current is not None:
            for i, eng in enumerate(self.engines):
                if eng.path == current.path:
                    engine_dd.set_selected(i)
                    break

        engine_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        engine_lbl = Gtk.Label(label="Engine", xalign=0)
        engine_lbl.set_size_request(72, -1)
        engine_dd.set_hexpand(True)
        engine_row.append(engine_lbl)
        engine_row.append(engine_dd)
        body.append(engine_row)

        if as_content:
            project_names = [p.name for p in self.projects]
            project_dropdown = Gtk.DropDown(model=Gtk.StringList.new(project_names))
            project_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            project_lbl = Gtk.Label(label="Project", xalign=0)
            project_lbl.set_size_request(72, -1)
            project_dropdown.set_hexpand(True)
            project_row.append(project_lbl)
            project_row.append(project_dropdown)
            body.append(project_row)
        elif not as_project_pack:
            target_dropdown = Gtk.DropDown(
                model=Gtk.StringList.new(["Engine (Marketplace)", "Project (Plugins/)"])
            )
            target_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            target_lbl = Gtk.Label(label="Target", xalign=0)
            target_lbl.set_size_request(72, -1)
            target_dropdown.set_hexpand(True)
            target_row.append(target_lbl)
            target_row.append(target_dropdown)
            body.append(target_row)

            project_names = [p.name for p in self.projects] or ["No projects found"]
            project_dropdown = Gtk.DropDown(model=Gtk.StringList.new(project_names))
            project_dropdown.set_sensitive(False)
            project_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            project_lbl = Gtk.Label(label="Project", xalign=0)
            project_lbl.set_size_request(72, -1)
            project_dropdown.set_hexpand(True)
            project_row.append(project_lbl)
            project_row.append(project_dropdown)
            body.append(project_row)

            def _on_target(*_a: object) -> None:
                assert target_dropdown is not None and project_dropdown is not None
                project_dropdown.set_sensitive(int(target_dropdown.get_selected()) == 1)

            target_dropdown.connect("notify::selected", _on_target)
        else:
            dest = Gtk.Label(
                label=f"Folder: {Path.home() / 'UnrealProjects'}",
                xalign=0,
                wrap=True,
            )
            dest.add_css_class("dim-label")
            body.append(dest)

        kind_label = {
            "plugin": "Plugin",
            "project": "Project",
            "content": "Content",
        }.get(kind, kind.title())
        versions = ", ".join(asset.engine_versions[-6:]) or "—"
        meta = Gtk.Label(label=f"{kind_label} · {versions}", xalign=0, wrap=True)
        meta.add_css_class("dim-label")
        body.append(meta)

        dialog.set_extra_child(body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("install")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response != "install":
                return
            eng_idx = int(engine_dd.get_selected())
            if eng_idx < 0 or eng_idx >= len(self.engines):
                self.toast("Select an engine")
                return
            engine = self.engines[eng_idx]
            project: UProject | None = None
            if as_content:
                if not self.projects or project_dropdown is None:
                    self.toast("No project available")
                    return
                p_idx = int(project_dropdown.get_selected())
                if p_idx < 0 or p_idx >= len(self.projects):
                    self.toast("Select a project")
                    return
                project = self.projects[p_idx]
            elif (
                not as_project_pack
                and target_dropdown is not None
                and int(target_dropdown.get_selected()) == 1
            ):
                if not self.projects or project_dropdown is None:
                    self.toast("No project available")
                    return
                p_idx = int(project_dropdown.get_selected())
                if p_idx < 0 or p_idx >= len(self.projects):
                    self.toast("Select a project")
                    return
                project = self.projects[p_idx]
            self._start_fab_install(
                asset,
                engine,
                project=project,
                as_project_pack=as_project_pack,
                as_content=as_content,
            )

        dialog.connect("response", _on_response)
        assert self.window is not None
        dialog.present(self.window)

    def _start_fab_install(
        self,
        asset: FabAsset,
        engine: EngineInstall,
        *,
        project: UProject | None = None,
        as_project_pack: bool = False,
        as_content: bool = False,
    ) -> None:
        if self._installing_plugin:
            self.toast("A plugin install is already running")
            return
        if as_content and project is None:
            self.toast("Select a project for content")
            return

        where = (
            f"project {project.name}"
            if project is not None
            else ("projects folder" if as_project_pack else engine.path.name)
        )
        self._installing_plugin = True
        self._set_status(f"Installing {asset.title} → {where}…")

        projects_root = Path.home() / "UnrealProjects"

        def worker() -> None:
            try:

                def prog(msg: str, done: int, total: int) -> None:
                    if total:
                        pct = min(100, int(done * 100 / total))
                        GLib.idle_add(self._set_status, f"{asset.title}: {msg} — {pct}%")
                    else:
                        GLib.idle_add(self._set_status, f"{asset.title}: {msg}")

                if as_project_pack:
                    dest = install_fab_project(
                        asset,
                        engine,
                        projects_root,
                        self.config.plugin_cache_dir,
                        progress=prog,
                    )
                elif as_content:
                    assert project is not None
                    dest = install_fab_content(
                        asset,
                        engine,
                        project,
                        self.config.plugin_cache_dir,
                        progress=prog,
                    )
                else:
                    dest = install_fab_plugin(
                        asset,
                        engine,
                        self.config.plugin_cache_dir,
                        project=project,
                        progress=prog,
                    )
                err = None
            except Exception as exc:  # noqa: BLE001
                dest = None
                err = str(exc)

            def done() -> bool:
                self._installing_plugin = False
                if err:
                    self.toast(err)
                    self._set_status("Install failed")
                else:
                    assert dest is not None
                    if as_project_pack:
                        self.refresh_all()
                    else:
                        self._populate_plugins()
                    self.toast(f"Installed {asset.title}")
                    self._set_status(f"Installed → {dest}")
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _build_installed_plugin(self, plugin_dir: Path) -> None:
        engine = self._selected_plugin_engine()
        if not engine:
            self.toast("Select an engine")
            return
        if self._installing_plugin:
            self.toast("An install/build is already running")
            return

        self._installing_plugin = True
        self._set_status(f"Building {plugin_dir.name} for Linux…")

        def worker() -> None:
            try:

                def prog(msg: str, done: int, total: int) -> None:
                    GLib.idle_add(self._set_status, msg)

                build_plugin_linux(engine, plugin_dir, progress=prog)
                err = None
            except Exception as exc:  # noqa: BLE001
                err = str(exc)

            def done() -> bool:
                self._installing_plugin = False
                if err:
                    self.toast(err)
                    self._set_status("Plugin build failed")
                else:
                    self._populate_plugins()
                    self.toast(f"Built {plugin_dir.name} for Linux")
                    self._set_status(f"Ready — restart the editor to load {plugin_dir.name}")
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_install_selected_blob(self) -> None:
        self._confirm_install_blob(self._selected_blob())

    def _confirm_install_blob(self, blob: EngineBlob | None) -> None:
        if not blob:
            self.toast("Select an engine to download")
            return
        if self._installing_engine:
            self.toast("An engine install is already running")
            return
        if not load_tokens():
            self.toast("Sign in with Epic first")
            return

        install_dir = self.config.engine_install_dir
        target = install_dir / blob.install_dirname
        if target.exists():
            self.toast(f"Already installed at {target}")
            self.refresh_all()
            return

        size_label = f"{blob.size_gib:.1f} GiB" if blob.size else "unknown size"
        needed = blob.size * 2 if blob.size else 0
        space_note = ""
        if needed:
            check_path = install_dir if install_dir.exists() else Path.home()
            free = shutil.disk_usage(check_path).free
            space_note = (
                f"\n\nNeed ~{needed / (1024**3):.0f} GiB free "
                f"(have ~{free / (1024**3):.0f} GiB)."
            )

        dialog = Adw.AlertDialog(
            heading=f"Install {blob.install_dirname.replace('UE_', 'UE ')}?",
            body=(
                f"Download and extract ~{size_label} to:\n{target}"
                f"{space_note}"
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("install", "Install")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("install")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response == "install":
                self._start_engine_install(blob)

        dialog.connect("response", _on_response)
        assert self.window is not None
        dialog.present(self.window)

    def _confirm_remove_engine(self, engine: EngineInstall) -> None:
        if self._installing_engine:
            self.toast("An engine install is already running")
            return
        if self._removing_engine:
            self.toast("An engine removal is already running")
            return

        dialog = Adw.AlertDialog(
            heading=f"Remove {engine.version_label.replace('UE_', 'UE ')}?",
            body=(
                f"Delete this engine from disk:\n{engine.path}\n\n"
                "This cannot be undone."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response == "remove":
                self._remove_engine(engine)

        dialog.connect("response", _on_response)
        assert self.window is not None
        dialog.present(self.window)

    def _remove_engine(self, engine: EngineInstall) -> None:
        if self._removing_engine or self._installing_engine:
            return
        path = engine.path.resolve()
        self._removing_engine = True
        self._set_status(f"Removing {engine.version_label}…")

        def worker() -> None:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                err: str | None = None
            except Exception as exc:  # noqa: BLE001
                err = str(exc)

            def done() -> bool:
                self._removing_engine = False
                if err:
                    self.toast(err)
                    self._set_status("Engine removal failed")
                else:
                    if str(self.config.get("default_engine_path", "")) == str(path):
                        self.config.set("default_engine_path", "")
                    preferred = self.config.preferred_engine
                    if preferred and preferred in path.name:
                        self.config.set("preferred_engine", "UE_5.7")
                    self.config.save()
                    self.refresh_all()
                    self.toast(f"Removed {engine.version_label}")
                    self._set_status(f"Removed {path}")
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _start_engine_install(self, blob: EngineBlob) -> None:
        if self._installing_engine:
            self.toast("An engine install is already running")
            return
        if not load_tokens():
            self.toast("Sign in with Epic first")
            return

        install_dir = self.config.engine_install_dir
        target = install_dir / blob.install_dirname
        if target.exists():
            self.toast(f"Already installed at {target}")
            return

        check_path = install_dir if install_dir.exists() else Path.home()
        free = shutil.disk_usage(check_path).free
        needed = blob.size * 2 if blob.size else 0
        if needed and free < needed:
            self.toast(
                f"Not enough free space (need ~{needed / (1024**3):.0f} GiB, "
                f"have {free / (1024**3):.0f} GiB)"
            )
            return

        self._installing_engine = True
        self._set_status(f"Installing {blob.name}…")
        keep_zip = bool(self.config.get("keep_engine_zips", False))

        def worker() -> None:
            try:
                fresh = {b.name: b for b in list_linux_engine_blobs()}
                current = fresh.get(blob.name, blob)

                def prog(msg: str, done: int, total: int) -> None:
                    if total:
                        pct = min(100, int(done * 100 / total))
                        GLib.idle_add(self._set_status, f"{msg} — {pct}%")
                    else:
                        GLib.idle_add(self._set_status, msg)

                path = install_engine(
                    current,
                    install_root=install_dir,
                    cache_dir=self.config.engine_cache_dir,
                    keep_zip=keep_zip,
                    progress=prog,
                )
                err = None
            except Exception as exc:  # noqa: BLE001
                path = None
                err = str(exc)

            def done() -> bool:
                self._installing_engine = False
                if err:
                    self.toast(err)
                    self._set_status("Engine install failed")
                else:
                    assert path is not None
                    self.config.set("default_engine_path", str(path))
                    self.config.set("preferred_engine", blob.version_label)
                    roots = list(self.config.get("engine_roots") or [])
                    root = str(install_dir)
                    if root not in roots:
                        roots.append(root)
                        self.config.set("engine_roots", roots)
                    self.config.save()
                    self.refresh_all()
                    self.toast(f"Installed {blob.install_dirname}")
                    self._set_status(f"Installed {path}")
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _install_selected_blob(self) -> None:
        self._confirm_install_selected_blob()

    def _open_epic_linux_downloads(self) -> None:
        if not load_tokens():
            self.toast("Sign in with Epic first")
            return
        try:
            open_linux_downloads()
        except Exception as exc:  # noqa: BLE001
            self.toast(str(exc))
            return
        self.toast("Opened unrealengine.com/linux in your browser")

    def _open_selected_project(self) -> None:
        proj = self._selected_project()
        if not proj:
            self.toast("Select a project")
            return
        eng = pick_engine(self.engines, self.config.preferred_engine)
        if not eng:
            self.toast("No engine available")
            return
        self.config.push_recent_project(str(proj.path))
        self.config.save()
        launch_editor(eng, self.config, project=proj.path)
        self.toast(f"Opening {proj.name}")

    def _browse_project(self) -> None:
        dialog = Gtk.FileDialog(title="Open .uproject")
        filt = Gtk.FileFilter()
        filt.set_name("Unreal Project")
        filt.add_pattern("*.uproject")
        dialog.set_default_filter(filt)

        def _done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            if not file:
                return
            path = Path(file.get_path())
            eng = pick_engine(self.engines, self.config.preferred_engine)
            if not eng:
                self.toast("No engine available")
                return
            self.config.push_recent_project(str(path))
            self.config.save()
            launch_editor(eng, self.config, project=path)
            self.refresh_all()

        assert self.window is not None
        dialog.open(self.window, None, _done)

    def _new_project_dialog(self) -> None:
        eng = pick_engine(self.engines, self.config.preferred_engine)
        if not eng:
            self.toast("No engine available")
            return
        templates = list_templates(eng)
        if not templates:
            self.toast("No templates found")
            return

        dialog = Adw.AlertDialog(
            heading="New project",
            body="MinUEmal is the default custom starter. Engine templates are listed below it.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("MyProject")
        content.append(name_entry)

        template_model = Gtk.StringList.new([t.label for t in templates])
        dropdown = Gtk.DropDown(model=template_model)
        # Prefer MinUEmal (first custom entry)
        for i, tmpl in enumerate(templates):
            if tmpl.template_id == "minuemal":
                dropdown.set_selected(i)
                break
        content.append(dropdown)

        dest_entry = Gtk.Entry()
        dest_entry.set_text(str(Path.home() / "UnrealProjects"))
        dest_entry.set_placeholder_text("Destination folder")
        content.append(dest_entry)

        dialog.set_extra_child(content)

        def _on_response(dlg: Adw.AlertDialog, response: str) -> None:
            if response != "create":
                return
            name = name_entry.get_text().strip()
            idx = int(dropdown.get_selected())
            if idx < 0 or idx >= len(templates):
                self.toast("Select a template")
                return
            template = templates[idx]
            dest = Path(dest_entry.get_text().strip() or str(Path.home() / "UnrealProjects"))
            self._set_status(f"Creating {name} from {template.name}…")

            def worker() -> None:
                try:
                    proj = create_project_from_template(eng, template, dest, name)
                    err = None
                except Exception as exc:  # noqa: BLE001
                    proj = None
                    err = str(exc)

                def done() -> bool:
                    if err:
                        self.toast(err)
                        self._set_status("Project create failed")
                    else:
                        assert proj is not None
                        self.config.push_recent_project(str(proj.path))
                        self.config.save()
                        self.refresh_all()
                        self.toast(f"Created {proj.name}")
                        self._set_status(f"Created {proj.path}")
                    return False

                GLib.idle_add(done)

            threading.Thread(target=worker, daemon=True).start()

        dialog.connect("response", _on_response)
        assert self.window is not None
        dialog.present(self.window)

    def _import_git_project_dialog(self) -> None:
        eng = pick_engine(self.engines, self.config.preferred_engine)

        dialog = Adw.AlertDialog(
            heading="Import from Git",
            body="Clone a repo that contains a .uproject (GitHub, GitLab, etc.).",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("import", "Import")
        dialog.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        url_entry = Gtk.Entry()
        url_entry.set_placeholder_text("https://github.com/org/repo.git")
        url_entry.set_hexpand(True)
        content.append(url_entry)

        branch_entry = Gtk.Entry()
        branch_entry.set_placeholder_text("Branch (optional)")
        content.append(branch_entry)

        folder_entry = Gtk.Entry()
        folder_entry.set_placeholder_text("Folder name (optional)")
        content.append(folder_entry)

        dest_entry = Gtk.Entry()
        dest_entry.set_text(str(Path.home() / "UnrealProjects"))
        dest_entry.set_placeholder_text("Destination parent folder")
        content.append(dest_entry)

        def _on_url_changed(entry: Gtk.Entry) -> None:
            if folder_entry.get_text().strip():
                return
            folder_entry.set_placeholder_text(
                guess_repo_folder_name(entry.get_text()) or "Folder name"
            )

        url_entry.connect("changed", _on_url_changed)
        dialog.set_extra_child(content)

        def _on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if response != "import":
                return
            url = url_entry.get_text().strip()
            branch = branch_entry.get_text().strip()
            folder = folder_entry.get_text().strip() or None
            dest = Path(dest_entry.get_text().strip() or str(Path.home() / "UnrealProjects"))
            self._set_status(f"Cloning {url}…")

            def worker() -> None:
                try:
                    proj = import_project_from_git(
                        url,
                        dest,
                        folder_name=folder,
                        branch=branch,
                        engine=eng,
                    )
                    err = None
                except Exception as exc:  # noqa: BLE001
                    proj = None
                    err = str(exc)

                def done() -> bool:
                    if err:
                        self.toast(err)
                        self._set_status("Git import failed")
                    else:
                        assert proj is not None
                        self.config.push_recent_project(str(proj.path))
                        # Ensure scan root includes dest
                        roots = [str(p) for p in self.config.project_scan_roots]
                        parent = str(proj.directory.parent)
                        if parent not in roots:
                            roots.append(parent)
                            self.config.set("project_scan_roots", roots)
                        self.config.save()
                        self.refresh_all()
                        self.toast(f"Imported {proj.name}")
                        self._set_status(f"Imported {proj.path}")
                    return False

                GLib.idle_add(done)

            threading.Thread(target=worker, daemon=True).start()

        dialog.connect("response", _on_response)
        assert self.window is not None
        dialog.present(self.window)

    def _login_dialog(self) -> None:
        url = open_login_page()
        dialog = Adw.AlertDialog(
            heading="Sign in with Epic Games",
            body=(
                "A browser window opened. Sign in, then copy the authorizationCode "
                "value from the JSON page and paste it below.\n\n"
                f"{url}"
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "Exchange code")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Paste authorizationCode here")
        dialog.set_extra_child(entry)

        def _on_response(dlg: Adw.AlertDialog, response: str) -> None:
            if response != "ok":
                return
            code = entry.get_text().strip()
            # Allow pasting full JSON
            if "authorizationCode" in code:
                import json as _json
                import re as _re

                try:
                    data = _json.loads(code)
                    code = data.get("authorizationCode") or code
                except Exception:  # noqa: BLE001
                    match = _re.search(r'"authorizationCode"\s*:\s*"([^"]+)"', code)
                    if match:
                        code = match.group(1)
            try:
                tokens = exchange_authorization_code(code)
            except EpicAuthError as exc:
                self.toast(str(exc))
                return
            self._update_account_label()
            self.toast(f"Signed in as {tokens.display_name}")
            self._load_engine_blobs_async()
            self._load_plugins_async()

        dialog.connect("response", _on_response)
        assert self.window is not None
        dialog.present(self.window)

    def _logout(self) -> None:
        clear_tokens()
        clear_library_cache()
        self.fab_plugins = []
        self._populate_plugins()
        self._update_account_label()
        self.toast("Signed out")

    # --- helpers ---------------------------------------------------------------

    def toast(self, message: str) -> None:
        if self.toast_overlay:
            self.toast_overlay.add_toast(Adw.Toast(title=message))

    def _set_status(self, text: str) -> None:
        if self.status:
            self.status.set_label(text)


def run() -> int:
    app = MatesUnrealLauncherApp()
    return app.run(None)
