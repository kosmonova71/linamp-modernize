#!/usr/bin/env python3
import os
import json
import threading
import urllib.request
import urllib.parse
import re
import socket
import sys
import traceback
import gi

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gtk, GLib, Gdk, WebKit, Pango
except ImportError:
    print("Please install the 'gi' module first.")
    sys.exit(1)

APP_ID = "org.example.HybridBrowser"
DEFAULT_HOME = "https://www.startpage.com/"
BOOKMARKS_FILE = os.path.expanduser("~/.hybrid_browser_bookmarks.json")

def detect_tor_port():
    for port in (9050, 9150):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return port
        except OSError:
            pass
    return None

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

def download_data(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HybridBrowser/1.0"})
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

class BrowserTab(Gtk.Box):
    def __init__(self, window, notebook, tor_port=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.notebook = notebook
        self.tor_port = tor_port
        self.current_favicon = None
        self.network_session = None
        tb = Gtk.Box(spacing=6, margin_top=4, margin_bottom=4, margin_start=8, margin_end=8)
        tb.add_css_class("linked")
        self.back_btn     = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        self.forward_btn  = Gtk.Button(icon_name="go-next-symbolic",     tooltip_text="Forward")
        self.reload_btn   = Gtk.Button(icon_name="view-refresh-symbolic",tooltip_text="Reload")
        self.home_btn = Gtk.Button(icon_name="go-home-symbolic", tooltip_text="Home")
        self.url_entry    = Gtk.Entry(hexpand=True, placeholder_text="Search or enter address")
        self.go_btn = Gtk.Button(icon_name="go-jump-symbolic", tooltip_text="Go")
        self.bookmark_btn = Gtk.Button(icon_name="bookmark-new-symbolic", tooltip_text="Add bookmark")
        self.new_tab_btn = Gtk.Button(icon_name="tab-new-symbolic", tooltip_text="New tab")
        self.zoom_out_btn = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text="Zoom out")
        self.zoom_reset_btn = Gtk.Button(icon_name="zoom-fit-best-symbolic", tooltip_text="Reset zoom")
        self.zoom_in_btn = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text="Zoom in")
        for b in (self.back_btn, self.forward_btn, self.reload_btn):
            tb.append(b)
        tb.append(self.home_btn)
        tb.append(self.url_entry)
        tb.append(self.go_btn)
        tb.append(self.bookmark_btn)
        tb.append(self.new_tab_btn)
        tb.append(self.zoom_out_btn)
        tb.append(self.zoom_reset_btn)
        tb.append(self.zoom_in_btn)
        self.append(tb)
        self.webview = self._create_webview()
        self.append(self.webview)
        self.webview.set_vexpand(True)
        self.favicon_img = Gtk.Image()
        self.favicon_img.set_size_request(16, 16)
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
        self.back_btn.connect("clicked", lambda _: self.webview.go_back())
        self.forward_btn.connect("clicked", lambda _: self.webview.go_forward())
        self.reload_btn.connect("clicked", lambda _: self.webview.reload())
        self.home_btn.connect("clicked", lambda _: self.webview.load_uri(DEFAULT_HOME))
        self.go_btn.connect("clicked", self.on_go)
        self.zoom_in_btn.connect("clicked", self.window.on_zoom_in_clicked)
        self.zoom_out_btn.connect("clicked", self.window.on_zoom_out_clicked)
        self.zoom_reset_btn.connect("clicked", self.window.on_zoom_reset_clicked)
        self.url_entry.connect("activate", self.on_go)
        self.bookmark_btn.connect("clicked", self.toggle_bookmark)
        self.webview.connect("notify::uri", self.update_url)
        self.webview.connect("notify::title", self.update_title)
        self.webview.connect("notify::estimated-load-progress", self.update_progress)
        self.webview.connect("load-changed", self.on_load_changed)
        self.webview.load_uri(DEFAULT_HOME)
        self.update_buttons()

    def _create_webview(self):
        view = WebKit.WebView()
        settings = WebKit.Settings()
        settings.set_property("enable-javascript", True)
        settings.set_property("enable-developer-extras", True)
        settings.set_property("enable-smooth-scrolling", True)
        view.set_settings(settings)
        view.set_background_color(Gdk.RGBA(0.0, 0.0, 0.0, 1.0))
        view.connect("load-changed", self.on_load_changed)
        view.connect("notify::uri", self.update_url)
        view.set_hexpand(True)
        view.set_vexpand(True)
        return view

    def _apply_proxy_settings(self, enable_tor=True):
        if self.network_session is None and self.webview:
            try:
                self.network_session = self.webview.get_network_session()
            except AttributeError:
                self.network_session = WebKit.NetworkSession.get_default()
        session = self.network_session or WebKit.NetworkSession.get_default()
        if session is None:
            raise RuntimeError("Unable to access WebKit network session")
        if enable_tor and self.tor_port:
            base_uri = f"socks5h://127.0.0.1:{self.tor_port}"
            candidate_uris = [
                base_uri.replace("socks5h", "socks5"),
                base_uri.replace("socks5h", "socks"),
                base_uri,
            ]
            schemes = ("http", "https", "ftp", "ws", "wss")
            last_error = None
            for proxy_uri in candidate_uris:
                try:
                    proxy_settings = WebKit.NetworkProxySettings.new(proxy_uri, [])
                except (TypeError, GLib.Error) as exc:
                    last_error = exc
                    continue
                for scheme in schemes:
                    proxy_settings.add_proxy_for_scheme(scheme, proxy_uri)
                try:
                    session.set_proxy_settings(WebKit.NetworkProxyMode.CUSTOM, proxy_settings)
                    break
                except (AttributeError, GLib.Error) as exc:
                    last_error = exc
            else:
                if isinstance(last_error, AttributeError):
                    raise RuntimeError("WebKit build lacks proxy configuration support") from last_error
                raise RuntimeError(f"Failed to configure Tor proxy: {last_error}") from last_error
        else:
            session.set_proxy_settings(WebKit.NetworkProxyMode.NO_PROXY, None)

    def enable_tor_proxy(self):
        if not self.tor_port:
            self.window.show_error("Tor not detected on ports 9050 or 9150")
            self.window.tor_switch.set_active(False)
            return
        try:
            current_uri = self.webview.get_uri() or DEFAULT_HOME
            self._apply_proxy_settings(enable_tor=True)
            self.webview.load_uri(current_uri)
            print("[Tor] Enabled for current tab")
        except Exception as e:
            print(f"Error enabling Tor proxy: {e}")
            traceback.print_exc()
            self.window.show_error(f"Failed to enable Tor: {str(e)}")
            self.window.tor_switch.set_active(False)

    def disable_tor_proxy(self):
        try:
            current_uri = self.webview.get_uri() or DEFAULT_HOME
            self._apply_proxy_settings(enable_tor=False)
            self.webview.load_uri(current_uri)
            print("[Tor] Disabled for current tab")
        except Exception as e:
            print(f"Error disabling Tor proxy: {e}")
            traceback.print_exc()
            self.window.show_error(f"Failed to disable Tor: {str(e)}")
            self.window.tor_switch.set_active(False)

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
        self.go_btn.set_sensitive(self.url_entry.get_text().strip() != "")
        self.home_btn.set_sensitive(True)
        self.reload_btn.set_sensitive(True)
        
        # Zoom buttons should be sensitive based on zoom limits, not load progress
        current_zoom = self.webview.get_zoom_level()
        self.zoom_in_btn.set_sensitive(current_zoom < 5.0)
        self.zoom_out_btn.set_sensitive(current_zoom > 0.25)
        self.zoom_reset_btn.set_sensitive(current_zoom != 1.0)

    def set_favicon(self, paintable):
        self.current_favicon = paintable
        if paintable:
            self.favicon_img.set_from_paintable(paintable)
        else:
            self.favicon_img.clear()
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
        self.set_favicon(None)

    def close(self):
        page_num = self.notebook.page_num(self)
        if page_num != -1:
            self.notebook.remove_page(page_num)

class BrowserWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Hybrid Browser")
        self.set_default_size(1200, 800)
        self.tor_port = detect_tor_port()
        hb = Gtk.HeaderBar()
        self.set_titlebar(hb)
        self.tor_switch = Gtk.Switch()
        self.tor_switch.set_active(False)
        self.tor_switch.connect("state-set", self.on_tor_toggled)
        hb.pack_end(self.tor_switch)
        self.tor_status = Gtk.Label(label=f"Tor: {'ON' if self.tor_port else 'OFF'}")
        hb.pack_end(self.tor_status)
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
        if self.tor_port:
            self.tor_status.set_label(f"Tor: Ready ({self.tor_port})")
            self.tor_status.add_css_class("success")
        else:
            self.tor_status.set_label("Tor: Not detected")
            self.tor_status.add_css_class("error")

    def new_tab(self):
        tab = BrowserTab(self, self.nb, self.tor_port)
        self.nb.set_current_page(self.nb.page_num(tab))

    def on_tor_toggled(self, switch, state):
        page = self.nb.get_current_page()
        if page >= 0:
            tab = self.nb.get_nth_page(page)
            if state:
                tab.enable_tor_proxy()
                self.tor_status.set_label(f"Tor: ON ({self.tor_port})")
                self.tor_status.add_css_class("success")
                self.tor_status.remove_css_class("error")
            else:
                tab.disable_tor_proxy()
                self.tor_status.set_label("Tor: OFF")
                self.tor_status.add_css_class("error")
                self.tor_status.remove_css_class("success")
        GLib.timeout_add(200, lambda: tab.webview.reload() or False)
        return True

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
            favicon_img = Gtk.Image()
            favicon_img.set_size_request(16, 16)
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
            btn = Gtk.Button(halign=Gtk.Align.START)
            btn.add_css_class("flat")
            btn_content = Gtk.Box(spacing=6)
            btn_content.append(favicon_img)
            btn_content.append(Gtk.Label(label=e.get("title", e["url"])))
            btn.set_child(btn_content)
            btn.connect("clicked", lambda _, u=e["url"]: self.open_url(u))
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
                self._save_bookmark_favicon(url, paintable)
        fetch_favicon(url, set_favicon_callback)

    def _save_bookmark_favicon(self, url, paintable):
        """Save favicon data to bookmark entry"""
        try:
            if hasattr(paintable, 'save_to_png_bytes'):
                png_bytes = paintable.save_to_png_bytes()
                import base64
                favicon_data = base64.b64encode(png_bytes.get_data()).decode('utf-8')
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

    def get_current_webview(self):
        """Get the webview of the current tab."""
        page = self.nb.get_current_page()
        if page >= 0:
            tab = self.nb.get_nth_page(page)
            return tab.webview
        return None

    def show_error(self, message):
        try:
            alert = Gtk.AlertDialog(
                message=message,
                detail="Please check your Tor installation and try again.",
                buttons=["OK"]
            )
            alert.show()
            return
        except Exception:
            pass
        dlg = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=message
        )
        dlg.format_secondary_text("Please check your Tor installation and try again.")
        dlg.connect("response", lambda d, r: d.destroy())
        dlg.show()

    def zoom_in(self):
        """Increase the zoom level of the current webview."""
        webview = self.get_current_webview()
        if webview:
            current_zoom = webview.get_zoom_level()
            webview.set_zoom_level(min(current_zoom + 0.1, 5.0))

    def zoom_out(self):
        """Decrease the zoom level of the current webview."""
        webview = self.get_current_webview()
        if webview:
            current_zoom = webview.get_zoom_level()
            webview.set_zoom_level(max(current_zoom - 0.1, 0.25))

    def zoom_reset(self):
        """Reset the zoom level to 100%."""
        webview = self.get_current_webview()
        if webview:
            webview.set_zoom_level(1.0)

    def on_zoom_in_clicked(self, button):
        """Handle zoom in button click."""
        self.zoom_in()

    def on_zoom_out_clicked(self, button):
        """Handle zoom out button click."""
        self.zoom_out()

    def on_zoom_reset_clicked(self, button):
        """Handle zoom reset button click."""
        self.zoom_reset()

def add_css():
    css = """
    .error   { color: #ff5555; }
    .success { color: #50fa7b; font-weight: bold; }
    .bold    { font-weight: bold; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

class HybridBrowser(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = BrowserWindow(self)
        win.present()

def main():
    add_css()
    app = HybridBrowser()
    app.run(sys.argv)

if __name__ == "__main__":
    main()
