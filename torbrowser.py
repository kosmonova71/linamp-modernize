#!/usr/bin/env python3
"""
GTK4 + WebKitGTK 6 Minimal Tor Browser – Updated for 2025 APIs
"""
import socket
import sys
import traceback

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, WebKit, GLib, Gdk



APP_ID = "org.example.TorBrowserGTK4"
DEFAULT_HOME = "https://check.torproject.org/"


def detect_tor_port():
    for port in (9050, 9150):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return port
        except OSError:
            pass
    return None


class TorBrowser(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None
        self.tor_port = detect_tor_port()
        self.tor_active = False
        self.network_session = None  # Initialize network session

    def do_activate(self):
        if not self.window:
            self.build_ui()
        self.window.present()

    def build_ui(self):
        # Main window
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_default_size(900, 700)
        self.window.set_title("Tor Browser GTK4 (WebKitGTK 6)")

        # Header bar
        header = Gtk.HeaderBar()
        self.window.set_titlebar(header)

        # Back button
        back_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back_btn.connect("clicked", lambda *_: self.webview.go_back())
        header.pack_start(back_btn)

        # Forward button
        fwd_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
        fwd_btn.connect("clicked", lambda *_: self.webview.go_forward())
        header.pack_start(fwd_btn)

        # URL entry
        self.url_entry = Gtk.Entry()
        self.url_entry.set_hexpand(True)
        self.url_entry.connect("activate", self.on_url_activate)
        header.pack_start(self.url_entry)

        # Tor toggle switch
        self.tor_switch = Gtk.Switch()
        self.tor_switch.set_active(False)
        self.tor_switch.connect("state-set", self.on_tor_toggled)
        header.pack_end(self.tor_switch)

        # A small status label (optional)
        self.tor_status = Gtk.Label(label="Tor: Unknown")
        header.pack_end(self.tor_status)

        # Create WebView directly (WebView is scrollable on its own)
        self.webview = self._create_webview()
        # Put the webview into a simple box so we can replace it later
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(self.webview)
        self.window.set_child(main_box)

        # Load a starting page
        self.webview.load_uri("https://browserleaks.com/ip")

        self.window.present()

    def _create_webview(self):
        # Use default WebContext (cannot assign manually in PyGObject WebKitGTK 6)
        view = WebKit.WebView()

        # Configure settings
        settings = WebKit.Settings()
        settings.set_property("enable-javascript", True)
        settings.set_property("enable-developer-extras", True)
        settings.set_property("enable-smooth-scrolling", True)
        view.set_settings(settings)

        # Signals
        view.connect("load-changed", self.on_load_changed)
        view.connect("notify::uri", self.on_uri_changed)

        view.set_hexpand(True)
        view.set_vexpand(True)

        return view

    def _apply_proxy_settings(self, enable_tor=True):
        """Apply proxy settings on the active WebKit network session."""
        if self.network_session is None and self.webview:
            try:
                self.network_session = self.webview.get_network_session()
            except AttributeError:
                # Fallback to the default session if the method is unavailable
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
            self.show_error("Tor not detected on ports 9050 or 9150")
            self.tor_switch.set_active(False)
            return

        try:
            current_uri = self.webview.get_uri() or DEFAULT_HOME
            self._apply_proxy_settings(enable_tor=True)
            self.webview.load_uri(current_uri)

            self.tor_active = True
            self.tor_status.set_label(f"Tor: ON ({self.tor_port})")
            self.tor_status.add_css_class("success")
            self.tor_status.remove_css_class("error")

        except Exception as e:
            print(f"Error enabling Tor proxy: {e}")
            traceback.print_exc()
            self.show_error(f"Failed to enable Tor: {str(e)}")
            self.tor_switch.set_active(False)

    def disable_tor_proxy(self):
        try:
            current_uri = self.webview.get_uri() or DEFAULT_HOME
            self._apply_proxy_settings(enable_tor=False)
            self.webview.load_uri(current_uri)

            self.tor_active = False
            self.tor_status.set_label("Tor: OFF")
            self.tor_status.add_css_class("error")
            self.tor_status.remove_css_class("success")

        except Exception as e:
            print(f"Error disabling Tor proxy: {e}")
            traceback.print_exc()
            self.show_error(f"Failed to disable Tor: {str(e)}")
            self.tor_switch.set_active(False)


    def on_tor_toggled(self, switch, state):
        # state is the *new* state for Gtk.Switch's "state-set" handler
        if state:
            self.enable_tor_proxy()
            print("[Tor] Enabled")
        else:
            self.disable_tor_proxy()
            print("[Tor] Disabled")

        # reload to make sure the page refreshes with the new network settings
        # schedule reload after a short moment to give WebKit time to apply settings
        GLib.timeout_add(200, lambda: self.webview.reload() or False)

        # return True to stop the "state-set" default handler from toggling the state again
        return True

    # —————— UI helpers ——————
    def on_url_activate(self, entry):
        text = entry.get_text().strip()
        if not text:
            return
        if not text.startswith(("http://", "https://", "file://", "about:")):
            text = "https://" + text
        self.webview.load_uri(text)

    def on_uri_changed(self, webview, _param):
        uri = webview.get_uri() or ""
        self.url_entry.set_text(uri)

    def on_load_changed(self, webview, load_event):
        if load_event == WebKit.LoadEvent.FINISHED:
            title = webview.get_title() or "Tor Browser"
            self.window.set_title(f"{title} — GTK4 Tor Browser")

    def show_error(self, message):
        # Use Gtk.AlertDialog where available (GTK 4.10+), otherwise fallback to MessageDialog
        try:
            # AlertDialog API (GTK 4.10+)
            alert = Gtk.AlertDialog(
                message=message,
                detail="Please check your Tor installation and try again.",
                buttons=["OK"]
            )
            # present non-blocking
            alert.show()
            return
        except Exception:
            pass

        # Fallback for older GTK4
        dlg = Gtk.MessageDialog(
            transient_for=self.window,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=message
        )
        dlg.format_secondary_text("Please check your Tor installation and try again.")
        dlg.connect("response", lambda d, r: d.destroy())
        dlg.show()


    def _create_webview(self):
        # Use default WebContext (cannot assign manually in PyGObject WebKitGTK 6)
        view = WebKit.WebView()

        # Configure settings
        settings = WebKit.Settings()
        settings.set_property("enable-javascript", True)
        settings.set_property("enable-developer-extras", True)
        settings.set_property("enable-smooth-scrolling", True)
        view.set_settings(settings)

        # Signals
        view.connect("load-changed", self.on_load_changed)
        view.connect("notify::uri", self.on_uri_changed)

        view.set_hexpand(True)
        view.set_vexpand(True)

        return view
 
 
            
# —————— CSS for nice colors (optional but recommended) ——————
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


def main():
    add_css()  # nice red/green Tor status
    app = TorBrowser()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
