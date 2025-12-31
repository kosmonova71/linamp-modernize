#!/usr/bin/env python3
"""
Mini Browser – GTK4 + WebKitGTK 6.0
FINAL CLEAN VERSION – Zero warnings, fully modern (Dec 2025)
"""
import os
import json
import threading
import urllib.request
import urllib.parse
import re
import sys
import gi

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gtk, GLib, Gdk, WebKit, Pango
except ImportError:
    print("Please install the 'gi' module first.")
    sys.exit(1)

BOOKMARKS_FILE = os.path.expanduser("~/.mini_browser_bookmarks.json")

# ========================
# Bookmarks
# ========================
def load_bookmarks():
    try:
        return json.load(open(BOOKMARKS_FILE, encoding="utf-8")) if os.path.exists(BOOKMARKS_FILE) else []
    except Exception:
        return []

def save_bookmarks(bm):
    try:
        json.dump(bm, open(BOOKMARKS_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    except Exception:
        pass

# ========================
# Favicon – Modern, no deprecations
# ========================
def download_data(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MiniBrowser/1.0"})
        return urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        return None

def fetch_favicon(page_url, callback):
    if not page_url or not page_url.startswith(("http://", "https://")):
        GLib.idle_add(callback, None)
        return
    parsed = urllib.parse.urlparse(page_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    html = download_data(page_url)
    candidates = []
    if html:
        text = html.decode("utf-8", errors="ignore")
        for rel in ["icon", "shortcut icon", "apple-touch-icon"]:
            for m in re.finditer(rf'<link[^>]+rel=["\']?(?:{rel})[^>]*href=["\']([^"\'>]+)', text, re.I):
                candidates.append(urllib.parse.urljoin(page_url, m.group(1)))
    for p in ["/favicon.ico", "/favicon.png", "/apple-touch-icon.png"]:
        candidates.append(urllib.parse.urljoin(base + "/", p))
    for url in candidates:
        data = download_data(url)
        if not data:
            continue
        try:
            gbytes = GLib.Bytes.new(data)
            texture = Gdk.Texture.new_from_bytes(gbytes)
            GLib.idle_add(callback, texture)
            return
        except Exception:
            continue
    GLib.idle_add(callback, None)

# ========================
# Browser Tab
# ========================
class BrowserTab(Gtk.Box):
    def __init__(self, window, notebook):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.notebook = notebook
        self.current_favicon = None  # Store current favicon for bookmark saving

        # Toolbar
        tb = Gtk.Box(spacing=6, margin_top=4, margin_bottom=4, margin_start=8, margin_end=8)
        tb.add_css_class("linked")

        self.back_btn     = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        self.forward_btn  = Gtk.Button(icon_name="go-next-symbolic",     tooltip_text="Forward")
        self.reload_btn   = Gtk.Button(icon_name="view-refresh-symbolic",tooltip_text="Reload")
        self.bookmark_btn = Gtk.Button(icon_name="non-starred-symbolic")
        self.url_entry    = Gtk.Entry(hexpand=True, placeholder_text="Search or enter address")

        for b in (self.back_btn, self.forward_btn, self.reload_btn):
            tb.append(b)
        tb.append(self.url_entry)
        tb.append(self.bookmark_btn)
        self.append(tb)

        # WebView
        self.webview = WebKit.WebView.new()
        self.append(self.webview)
        self.webview.set_vexpand(True)

        # Tab label with favicon
        self.favicon_img = Gtk.Image()                 # ← modern way
        self.favicon_img.set_size_request(16, 16)      # ← replaces deprecated pixel_size

        self.title_label = Gtk.Label(
            label="New Tab",
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=30,
            xalign=0,
            hexpand=True
        )

        label_box = Gtk.Box(spacing=6)
        label_box.append(self.favicon_img)
        label_box.append(self.title_label)

        close_btn = Gtk.Button(icon_name="window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.connect("clicked", lambda _: self.close())

        tab_widget = Gtk.Box(spacing=8)
        tab_widget.append(label_box)
        tab_widget.append(close_btn)

        self.notebook.append_page(self, tab_widget)
        self.notebook.set_tab_reorderable(self, True)

        # Signals
        self.back_btn.connect("clicked", lambda _: self.webview.go_back())
        self.forward_btn.connect("clicked", lambda _: self.webview.go_forward())
        self.reload_btn.connect("clicked", lambda _: self.webview.reload())
        self.url_entry.connect("activate", self.on_go)
        self.bookmark_btn.connect("clicked", self.toggle_bookmark)
        self.webview.connect("notify::uri", self.update_url)
        self.webview.connect("notify::title", self.update_title)
        self.webview.connect("notify::estimated-load-progress", self.update_progress)
        self.webview.connect("load-changed", self.on_load_changed)

        self.webview.load_uri("https://www.startpage.com")
        self.update_buttons()

    def on_go(self, entry):
        text = entry.get_text().strip()
        if not text:
            return
        if "." not in text and " " not in text and not text.startswith(("http://", "https://")):
            url = "https://www.startpage.com/sp/search?q=" + urllib.parse.quote(text)
        elif not text.startswith(("http://", "https://", "file://", "about:")):
            url = "https://" + text
        else:
            url = text
        self.webview.load_uri(url)

    def update_url(self, *_):
        u = self.webview.get_uri()
        if u:
            self.url_entry.set_text(u)

    def update_title(self, *_):
        t = self.webview.get_title() or "New Tab"
        self.title_label.set_text(t)

    def update_progress(self, *_):
        p = self.webview.get_estimated_load_progress()
        t = self.webview.get_title() or "Loading..."
        self.title_label.set_text(f"({int(p*100)}%) {t}" if p < 1.0 else t)

    def on_load_changed(self, webview, event):
        if event == WebKit.LoadEvent.FINISHED:
            self.update_buttons()
            uri = webview.get_uri()
            if uri and uri.startswith(("http://", "https://")):
                threading.Thread(
                    target=fetch_favicon,
                    args=(uri, self.set_favicon),
                    daemon=True
                ).start()

    def update_buttons(self):
        self.back_btn.set_sensitive(self.webview.can_go_back())
        self.forward_btn.set_sensitive(self.webview.can_go_forward())

    def set_favicon(self, paintable):
        self.current_favicon = paintable  # Store favicon for bookmark saving
        if paintable:
            self.favicon_img.set_from_paintable(paintable)
        else:
            self.favicon_img.clear()
        # clear icon
        self.bookmark_btn.set_icon_name("starred-symbolic" if self.is_bookmarked() else "non-starred-symbolic")

    def is_bookmarked(self):
        u = self.webview.get_uri()
        return u and any(b.get("url") == u for b in load_bookmarks())

    def toggle_bookmark(self, _):
        uri = self.webview.get_uri()
        title = self.webview.get_title() or uri
        bm = load_bookmarks()
        if self.is_bookmarked():
            bm = [b for b in bm if b.get("url") != uri]
        else:
            bookmark_data = {"title": title, "url": uri}
            # Add favicon data if available
            if self.current_favicon:
                try:
                    if hasattr(self.current_favicon, 'save_to_png_bytes'):
                        png_bytes = self.current_favicon.save_to_png_bytes()
                        import base64
                        bookmark_data["favicon_data"] = base64.b64encode(png_bytes.get_data()).decode('utf-8')
                except Exception:
                    pass
            bm.append(bookmark_data)
        save_bookmarks(bm)
        self.window.reload_bookmarks_menu()
        self.set_favicon(None)  # refresh star

    def close(self):
        page_num = self.notebook.page_num(self)
        if page_num != -1:                     # -1 means tab not found
            self.notebook.remove_page(page_num)

# ========================
# Main Window
# ========================
class BrowserWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Mini Browser")
        self.set_default_size(1200, 800)

        hb = Gtk.HeaderBar()
        self.set_titlebar(hb)

        new_tab_btn = Gtk.Button(icon_name="tab-new-symbolic")
        new_tab_btn.set_tooltip_text("New tab")
        new_tab_btn.connect("clicked", lambda _: self.new_tab())
        hb.pack_start(new_tab_btn)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        hb.pack_end(menu_btn)

        self.pop = Gtk.Popover()
        menu_btn.set_popover(self.pop)

        self.bbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                            margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        sw = Gtk.ScrolledWindow()
        sw.set_child(self.bbox)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.pop.set_child(sw)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(main)

        self.nb = Gtk.Notebook(scrollable=True)
        main.append(self.nb)

        self.new_tab()
        self.reload_bookmarks_menu()

    def new_tab(self):
        tab = BrowserTab(self, self.nb)
        self.nb.set_current_page(self.nb.page_num(tab))

    def reload_bookmarks_menu(self):
        child = self.bbox.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.bbox.remove(child)
            child = next_child

        bm = load_bookmarks()
        if not bm:
            self.bbox.append(Gtk.Label(label="No bookmarks yet", margin_top=10))
            return

        for e in bm:
            # Add favicon
            favicon_img = Gtk.Image()
            favicon_img.set_size_request(16, 16)

            # Set favicon if available
            if e.get("favicon_data"):
                try:
                    import base64
                    favicon_bytes = base64.b64decode(e["favicon_data"])
                    gbytes = GLib.Bytes.new(favicon_bytes)
                    texture = Gdk.Texture.new_from_bytes(gbytes)
                    favicon_img.set_from_paintable(texture)
                except Exception:
                    favicon_img.set_from_icon_name("text-html-symbolic")
            else:
                favicon_img.set_from_icon_name("text-html-symbolic")

            # Create button with favicon and title
            btn = Gtk.Button(halign=Gtk.Align.START)
            btn.add_css_class("flat")
            btn_content = Gtk.Box(spacing=6)
            btn_content.append(favicon_img)
            btn_content.append(Gtk.Label(label=e.get("title", e["url"])))
            btn.set_child(btn_content)
            btn.connect("clicked", lambda _, u=e["url"]: self.open_url(u))

            # Fetch favicon in background if not available
            if not e.get("favicon_data") and e["url"].startswith(("http://", "https://")):
                threading.Thread(
                    target=self._fetch_bookmark_favicon,
                    args=(e["url"], favicon_img),
                    daemon=True
                ).start()
            self.bbox.append(btn)
        self.bbox.append(Gtk.Separator(margin_top=10, margin_bottom=10))
        clear = Gtk.Button(label="Clear All Bookmarks")
        clear.connect("clicked", lambda _: (save_bookmarks([]), self.reload_bookmarks_menu()))
        self.bbox.append(clear)

    def _fetch_bookmark_favicon(self, url, image_widget):
        """Fetch favicon for a bookmark and update the image widget"""
        def set_favicon_callback(paintable):
            if paintable:
                GLib.idle_add(image_widget.set_from_paintable, paintable)
                # Save favicon data to bookmarks
                self._save_bookmark_favicon(url, paintable)
        fetch_favicon(url, set_favicon_callback)

    def _save_bookmark_favicon(self, url, paintable):
        """Save favicon data to bookmark entry"""
        try:
            # Convert paintable to bytes
            if hasattr(paintable, 'save_to_png_bytes'):
                png_bytes = paintable.save_to_png_bytes()
                import base64
                favicon_data = base64.b64encode(png_bytes.get_data()).decode('utf-8')

                # Update bookmarks with favicon data
                bm = load_bookmarks()
                for bookmark in bm:
                    if bookmark.get("url") == url:
                        bookmark["favicon_data"] = favicon_data
                        break
                save_bookmarks(bm)
        except Exception:
            pass

    def open_url(self, url):
        page = self.nb.get_current_page()
        if page >= 0:
            tab = self.nb.get_nth_page(page)
            tab.webview.load_uri(url)

# ========================
# Run
# ========================
if __name__ == "__main__":
    app = Gtk.Application(application_id="org.example.MiniBrowser")
    app.connect("activate", lambda app: BrowserWindow(app).present())
    app.run(sys.argv)
