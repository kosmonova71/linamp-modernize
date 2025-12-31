import datetime
import hashlib
import json
import logging
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse, urlunparse

import gi
import psutil
import requests
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from requests.adapters import HTTPAdapter
from stem.control import Controller
from urllib3.util.retry import Retry

# Constants
CONFIG_DIR = os.path.expanduser('~/.config/shadowbrowser')
BOOKMARKS_FILE = os.path.join(CONFIG_DIR, 'bookmarks.json')
HISTORY_FILE = os.path.join(CONFIG_DIR, 'history.json')

# Create config directory if it doesn't exist
os.makedirs(CONFIG_DIR, exist_ok=True)

try:
    gi.require_version('Gtk', '3.0')
    gi.require_version('WebKit2', '4.1')
    from gi.repository import Gtk, Gdk, GLib, WebKit2
except (ValueError, ImportError):
    exit(1)

def safe_widget_append(container, widget):
    """
    Safely append a widget to a container, handling any necessary unparenting.
    Args:
        container: The GTK container to append to.
        widget: The widget to append.
    Returns:
        bool: True if append was successful, False otherwise.
    """
    if not container or not widget:
        return False
    try:
        current_parent = widget.get_parent()
        if current_parent is not None and current_parent != container:
            widget.unparent()
        if isinstance(container, Gtk.Box):
            container.pack_start(widget, False, False, 0)
        elif hasattr(container, 'append'):
            container.append(widget)
        else:
            container.add(widget)
        return True
    except (AttributeError, TypeError):
        return False

DOWNLOAD_EXTENSIONS = [
    ".3gp", ".7z", ".aac", ".apk", ".appimage", ".avi", ".bat", ".bin", ".bmp",
    ".bz2", ".c", ".cmd", ".cpp", ".cs", ".deb", ".dmg", ".dll", ".doc", ".docx",
    ".eot", ".exe", ".flac", ".flv", ".gif", ".gz", ".h", ".ico", ".img", ".iso",
    ".jar", ".java", ".jpeg", ".jpg", ".js", ".lua", ".lz", ".lzma", ".m4a", ".mkv",
    ".mov", ".mp3", ".mp4", ".mpg", ".mpeg", ".msi", ".odp", ".ods", ".odt", ".ogg",
    ".otf", ".pdf", ".pkg", ".pl", ".png", ".pps", ".ppt", ".pptx", ".ps1",
    ".py", ".rar", ".rb", ".rpm", ".rtf", ".run", ".sh", ".so", ".svg", ".tar",
    ".tar.bz2", ".tar.gz", ".tbz2", ".tgz", ".tiff", ".ttf", ".txt", ".vhd", ".vmdk",
    ".wav", ".webm", ".webp", ".wma", ".woff", ".woff2", ".wmv", ".xls", ".xlsx", ".zip"
]

BOOKMARKS_FILE = "bookmarks.json"
HISTORY_FILE = "history.json"
SESSION_FILE = "session.json"
TABS_FILE = "tabs.json"
HISTORY_LIMIT = 100

try:
    from js_obfuscation_improved import extract_url_from_javascript as js_extract_url
    from js_obfuscation_improved import extract_onclick_url
    extract_onclick_url = extract_onclick_url
except ImportError:
    js_extract_url = None
    extract_onclick_url = None

class SSLUtils:
    def __init__(self):
        self.context = ssl.create_default_context()

    def fetch_certificate(self, url):
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 443
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                with self.context.wrap_socket(sock, server_hostname=host) as ssock:
                    cert_bin = ssock.getpeercert(binary_form=True)
                    cert = x509.load_der_x509_certificate(cert_bin, default_backend())
                    return cert
        except Exception:
            return None

    def get_ocsp_url(self, cert):
        try:
            aia = cert.extensions.get_extension_for_oid(
                x509.ExtensionOID.AUTHORITY_INFORMATION_ACCESS
            ).value
            for access in aia:
                if access.access_method == x509.AuthorityInformationAccessOID.OCSP:
                    return access.access_location.value
        except Exception:
            return None

    def is_certificate_expired(self, cert: x509.Certificate) -> bool:
        """
        Check if the certificate is expired.
        Args:
            cert (x509.Certificate): The X.509 certificate object.
        Returns:
            bool: True if the certificate is expired, False otherwise.
        """
        try:
            return cert.not_valid_after < datetime.datetime.utcnow()
        except Exception:
            return True

class DownloadManager:
    def __init__(self, parent_window):
        self.parent_window = parent_window
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.downloads = {}
        self.lock = threading.Lock()
        self.ensure_download_directory()
        self.on_download_start_callback = None
        self.on_download_finish_callback = None

    def safe_append(self, container, widget):
        """
        Safely append a widget to a container using the shared utility function.
        Args:
            container: The GTK container to append to
            widget: The widget to append
        Returns:
            bool: True if append was successful, False otherwise
        """
        return safe_widget_append(container, widget)

    def add_webview(self, webview):
        """Connect download signals to the download manager."""
        webview.get_context().connect("download-started", self.on_download_started)

    def on_download_started(self, context, download):
        """Handle download started event."""
        if self.on_download_start_callback:
            self.on_download_start_callback()
        uri = download.get_request().get_uri()
        if not uri:
            return False
        downloads_dir = GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_DOWNLOAD
        ) or os.path.expanduser("~/Downloads")
        os.makedirs(downloads_dir, exist_ok=True)
        filename = os.path.basename(uri)
        counter = 1
        base_name, ext = os.path.splitext(filename)
        while os.path.exists(os.path.join(downloads_dir, filename)):
            filename = f"{base_name}_{counter}{ext}"
            counter += 1
        filepath = os.path.join(downloads_dir, filename)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = Gtk.Label(label=f"Downloading {filename}")
        progress = Gtk.ProgressBar()
        with self.lock:
            self.downloads[download] = {
                "hbox": hbox,
                "label": label,
                "progress": progress,
                "filepath": filepath,
                "status": "Downloading",
                "cancelled": False,
            }
        self.safe_append(hbox, label)
        self.safe_append(hbox, progress)
        self.safe_append(self.box, hbox)
        download.connect("notify::estimated-progress", self.on_progress_changed)
        download.connect("notify::status", self.on_download_status_changed)
        return True

    def on_progress_changed(self, download, param):
        """Update progress bar for a download."""
        with self.lock:
            info = self.downloads.get(download)
            if info:
                progress = download.get_estimated_progress()
                info["progress"].set_fraction(progress)
                info["progress"].set_text(f"{progress * 100:.1f}%")
                info["label"].set_text(f"Downloading {os.path.basename(info['filepath'])}")

    def on_download_status_changed(self, download, param):
        """Handle download status changes."""
        with self.lock:
            info = self.downloads.get(download)
            if info:
                status = download.get_status()
                if status == WebKit2.DownloadStatus.FINISHED:
                    info["status"] = "Finished"
                    info["progress"].set_fraction(1.0)
                    info["progress"].set_text("100%")
                    info["label"].set_text(f"Download finished: {os.path.basename(info['filepath'])}")
                    GLib.timeout_add_seconds(5, lambda: self.cleanup_download(download))
                elif status == WebKit2.DownloadStatus.FAILED:
                    info["status"] = "Failed"
                    info["label"].set_text(f"Download failed: {os.path.basename(info['filepath'])}")
                    info["progress"].set_text("Failed")
                    GLib.timeout_add_seconds(5, lambda: self.cleanup_download(download))
                elif status == WebKit2.DownloadStatus.CANCELLED:
                    info["status"] = "Cancelled"
                    info["label"].set_text(f"Download cancelled: {os.path.basename(info['filepath'])}")
                    info["progress"].set_text("Cancelled")
                    GLib.timeout_add_seconds(5, lambda: self.cleanup_download(download))

    def add_progress_bar(self, progress_info):
        """Add progress bar for manual downloads."""
        with self.lock:
            if self.on_download_start_callback:
                self.on_download_start_callback()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            label = Gtk.Label(label=f"Downloading {progress_info['filename']}")
            progress = Gtk.ProgressBar()
            self.downloads[progress_info["filename"]] = {
                "hbox": hbox,
                "label": label,
                "progress": progress,
                "filepath": os.path.join(
                    GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                    or os.path.expanduser("~/Downloads"),
                    progress_info["filename"],
                ),
                "status": "Downloading",
                "cancelled": False,
            }
            self.safe_append(hbox, label)
            self.safe_append(hbox, progress)
            self.safe_append(self.box, hbox)

    def update_progress(self, progress_info, progress):
        """Update progress for manual downloads."""
        with self.lock:
            info = self.downloads.get(progress_info["filename"])
            if info:
                info["progress"].set_fraction(progress)
                info["progress"].set_text(f"{progress * 100:.1f}%")
                info["label"].set_text(f"Downloading {progress_info['filename']}")

    def download_finished(self, progress_info):
        """Handle manual download finished."""
        with self.lock:
            if self.on_download_finish_callback:
                self.on_download_finish_callback()
            info = self.downloads.get(progress_info["filename"])
            if info:
                info["status"] = "Finished"
                info["progress"].set_fraction(1.0)
                info["progress"].set_text("100%")
                info["label"].set_text(f"Download finished: {progress_info['filename']}")
                GLib.timeout_add_seconds(
                    5, lambda: self.cleanup_download(progress_info["filename"])
                )

    def download_failed(self, progress_info, error_message):
        """Handle manual download failure."""
        with self.lock:
            if self.on_download_finish_callback:
                self.on_download_finish_callback()
            if progress_info is None:
                return
            info = self.downloads.get(progress_info["filename"])
            if info:
                info["status"] = "Failed"
                info["label"].set_text(f"Download failed: {error_message}")
                info["progress"].set_text("Failed")
                GLib.timeout_add_seconds(
                    5, lambda: self.cleanup_download(progress_info["filename"])
                )

    def cleanup_download(self, download_key):
        """Clean up download UI elements."""
        with self.lock:
            info = self.downloads.pop(download_key, None)
            if info:
                try:
                    parent = info["hbox"].get_parent()
                    if parent and hasattr(parent, "remove"):
                        if info["hbox"].get_parent() == parent:
                            parent.remove(info["hbox"])
                except Exception:
                    pass

    def ensure_download_directory(self):
        """Ensure the downloads directory exists."""
        downloads_dir = GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_DOWNLOAD
        ) or os.path.expanduser("~/Downloads")
        try:
            os.makedirs(downloads_dir, exist_ok=True)
        except OSError:
            raise

    def show(self):
        """Show the downloads area."""
        if hasattr(self, "download_area") and self.download_area:
            parent = self.download_area.get_parent()
            if parent:
                return
        self.download_area = Gtk.ScrolledWindow()
        self.download_area.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.download_area.set_max_content_height(200)
        self.download_area.set_min_content_height(0)
        self.download_area.add(self.box)
        self.download_area.set_vexpand(False)
        self.download_area.set_margin_top(5)
        self.download_area.set_margin_bottom(5)
        parent_window = self.parent_window
        if parent_window is None:
            return
        parent_child = parent_window.get_child()
        if parent_child and hasattr(parent_child, "append"):
            current_parent = self.download_area.get_parent()
            if current_parent:
                current_parent.remove(self.download_area)
            parent_child.append(self.download_area)

    def clear_all(self):
        """Clear all completed downloads from the UI."""
        for download, info in list(self.downloads.items()):
            if info["status"] in ["Finished", "Failed", "Cancelled"]:
                self.cleanup_download(download)

class AdBlocker:
    def __init__(self):
        self.blocked_patterns = []
        self.enabled = True
        self.block_list_url = "https://easylist.to/easylist/easylist.txt"
        self.cache_file = "easylist_cache.txt"
        self.cache_max_age = 86400
        self.adult_patterns = []
        self.load_block_lists()

    def inject_to_webview(self, user_content_manager):
        self.inject_adblock_script_to_ucm(user_content_manager)

    def inject_adblock_script_to_ucm(self, user_content_manager):
        """
        Injects JavaScript into UserContentManager to block ads and handle void links.
        """
        adblock_script = r"""
        (function() {
            const selectorsToHide = [
                '.ad', '.ads', '.advert', '.advertisement', '.banner', '.promo', '.sponsored',
                '[id*="ad-"]', '[id*="ads-"]', '[id*="advert-"]', '[id*="banner"]',
                '[class*="-ad"]', '[class*="-ads"]', '[class*="-advert"]', '[class*="-banner"]',
                '[class*="adbox"]', '[class*="adframe"]', '[class*="adwrapper"]', '[class*="bannerwrapper"]',
                '[class*="__wrap"]','[class*="__content"]','[class*="__btn-block"]',
                '[src*="cdn.creative-sb1.com"]','[src*="cdn.storageimagedisplay.com"]',
                'iframe[src*="ad"], iframe[src*="ads"]',
                'div[id^="google_ads_"]',
                'div[class^="adsbygoogle"]',
                'ins.adsbygoogle'
            ];
            function hideElements() {
                selectorsToHide.forEach(selector => {
                    try {
                        document.querySelectorAll(selector).forEach(el => {
                            if (el.style.display !== 'none' || el.style.visibility !== 'hidden') {
                                el.style.setProperty('display', 'none', 'important');
                                el.style.setProperty('visibility', 'hidden', 'important');
                            }
                        });
                    } catch (e) {
                        console.error('AdBlock: Error querying selector', selector, e);
                    }
                });
            }
            function isUrlBlocked(url) {
                if (!url) return false;
                const patterns = [
                    /doubleclick\.net/,
                    /googlesyndication\.com/,
                    /\/ads\//,
                    /adframe\./,
                    /bannerads\./
                ];
                // Whitelist Java video player domains
                const whitelist = %s;
                for (let i = 0; i < whitelist.length; i++) {
                    if (url.includes(whitelist[i])) {
                        return false;
                    }
                }
                return patterns.some(p => p.test(url));
            }
            const OriginalXHR = window.XMLHttpRequest;
            window.XMLHttpRequest = function() {
                const xhr = new OriginalXHR();
                const originalOpen = xhr.open;
                xhr.open = function(method, url) {
                    if (isUrlBlocked(url)) {
                        arguments[1] = 'about:blank#blocked';
                    }
                    return originalOpen.apply(this, arguments);
                };
                return xhr;
            };
            if (window.fetch) {
                const originalFetch = window.fetch;
                window.fetch = function(input, init) {
                    const url = typeof input === 'string' ? input : (input && input.url);
                    if (isUrlBlocked(url)) {
                        return Promise.reject(new Error('AdBlock: Fetch blocked'));
                    }
                    return originalFetch.apply(this, arguments);
                };
            }
            const originalOpen = window.open;
            window.open = function(url, name, features) {
                if (isUrlBlocked(url)) return null;
                return originalOpen.apply(this, arguments);
            };
            hideElements();
            const observer = new MutationObserver(() => {
                hideElements();
            });
            if (document.body instanceof Node) {
                observer.observe(document.body, { childList: true, subtree: true });
            }
        })();
        """ % json.dumps(["java.com", "oracle.com", "javaplugin.com", "javaplayer.com"])
        custom_script = r"""
        (function() {
            window.addEventListener('click', function(event) {
                let target = event.target;
                while (target && target.tagName !== 'A') {
                    target = target.parentElement;
                }
                if (target && target.tagName === 'A') {
                    const href = target.getAttribute('href');
                    if (href && href.trim().toLowerCase() === 'javascript:void(0)') {
                        // Remove preventDefault and stopPropagation to allow click event
                        // event.preventDefault();
                        // event.stopPropagation();
                        const onclick = target.getAttribute('onclick');
                        if (onclick) {
                            const match = onclick.match(/dbneg\(['"]([^'"]+)['"]\)/);
                            if (match) {
                                const id = match[1];
                                const url = window.dbneg(id);
                                if (url && url !== 'about:blank' && url !== window.location.href) {
                                    if (window.WebKit2 && window.WebKit2.messageHandlers && window.WebKit2.messageHandlers.voidLinkClicked) {
                                        window.WebKit2.messageHandlers.voidLinkClicked.postMessage(url);
                                    }
                                }
                            }
                        }
                    }
                }
            }, true);
        })();
        """
        user_content_manager.add_script(
            WebKit2.UserScript.new(
                adblock_script,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
            )
        )
        user_content_manager.add_script(
            WebKit2.UserScript.new(
                custom_script,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.END,
            )
        )

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def load_block_lists(self):
        """Loads and caches ad blocking patterns from EasyList."""
        if (
            os.path.exists(self.cache_file)
            and (time.time() - os.path.getmtime(self.cache_file)) < self.cache_max_age
        ):
            with open(self.cache_file, "r", encoding="utf-8") as f:
                lines = [
                    line.strip() for line in f if line and not line.startswith("!")
                ]
        else:
            lines = self.fetch_block_list(self.block_list_url)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        self.blocked_patterns = self._parse_block_patterns(lines)

    def fetch_block_list(self, url):
        """Fetches the block list content from a URL."""
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return [
                line.strip()
                for line in response.text.splitlines()
                if line and not line.startswith("!")
            ]
        except requests.exceptions.RequestException:
            return []

    def _parse_block_patterns(self, lines):
        """Parses block list rules into regex patterns."""
        compiled_patterns = []
        for line in lines:
            if any(s in line for s in ("##", "#@#", "@@")):
                continue
            try:
                pattern = line
                if pattern.startswith("||"):
                    pattern = r"^https?://([a-z0-9-]+\.)?" + re.escape(pattern[2:])
                elif pattern.startswith("|"):
                    pattern = r"^" + re.escape(pattern[1:])
                elif pattern.endswith("|"):
                    pattern = re.escape(pattern[:-1]) + r"$"
                pattern = re.escape(pattern)
                pattern = pattern.replace(r"\*", ".*")
                pattern = pattern.replace(r"\^", r"[^a-zA-Z0-9_\-%\.]")
                pattern = pattern.replace(r"\|", "")
                regex = re.compile(pattern, re.IGNORECASE)
                compiled_patterns.append(regex)
            except re.error:
                pass
        return compiled_patterns

    def is_blocked(self, url):
        """Checks if the given URL matches any blocked pattern."""
        if not self.enabled or not url:
            return False
        try:
            parsed = urlparse(url)
            full_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            for pattern in self.adult_patterns:
                if pattern in full_url.lower():
                    return True
            for pattern in self.blocked_patterns:
                if pattern.search(full_url):
                    return True
        except Exception:
            pass
        return False

    def connect_webview_signals(self, webview):
        webview.connect("load-changed", self.on_load_changed)
        webview.connect("notify::title", self.on_title_changed)
        webview.connect("decide-policy", self.on_decide_policy)

    def is_mime_type_displayable(self, mime_type):
        displayable_types = [
            "text/html",
            "text/plain",
            "image/png",
            "image/jpeg",
            "image/gif",
            "application/xhtml+xml",
        ]
        return mime_type in displayable_types if mime_type else False

    def validate_and_clean_url(self, url):
        cleaned_url = url.strip()
        if not re.match(r"^(http|https)://", cleaned_url):
            cleaned_url = "https://" + cleaned_url
        parsed_url = urlparse(cleaned_url)
        if not parsed_url.netloc:
            raise ValueError(f"Invalid URL: {cleaned_url}")
        return urlunparse(parsed_url)

    def enable_csp(self, webview, csp_policy=None):
        """
        Enable Content Security Policy on the webview with optional CSP string.
        Sanitizes the CSP string to remove unsupported directives like 'manifest-src'.
        """
        if csp_policy is None:
            csp_policy = "default-src 'self'; script-src 'self' https://trusted.com;"
        sanitized_csp = re.sub(
            r"\bmanifest-src[^;]*;?", "", csp_policy, flags=re.IGNORECASE
        ).strip()
        if sanitized_csp.endswith(";"):
            sanitized_csp = sanitized_csp[:-1].strip()
        csp_script = f"""
        (function() {{
            var meta = document.createElement('meta');
            meta.httpEquiv = 'Content-Security-Policy';
            meta.content = '{sanitized_csp}';
            document.getElementsByTagName('head')[0].appendChild(meta);
        }})();
        """
        script = WebKit2.UserScript.new(
            csp_script,
            WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.START,
        )
        webview.get_user_content_manager().add_script(script)

    def report_csp_violation(self, report):
        report_url = "http://127.0.0.1:9000/"
        data = json.dumps({"csp-report": report}).encode("utf-8")
        req = urllib.request.Request(
            report_url,
            data=data,
            headers={"Content-Type": "application/csp-report"}
        )
        try:
            with urllib.request.urlopen(req) as _:
                pass
        except Exception:
            pass

    def on_csp_violation(self, report):
        """Handles CSP violation and passes it to report_csp_violation."""
        self.report_csp_violation(report)

    def is_third_party_request(self, url, current_origin, webview=None):
        try:
            if webview is None:
                return False
            page_origin = urlparse(webview.get_uri()).netloc
            return current_origin != page_origin
        except Exception:
            return False

    def enable_mixed_content_blocking(self, webview):
        settings = webview.get_settings()
        settings.set_property("allow-running-insecure-content", False)
        webview.set_settings(settings)

    def secure_cookies(self, webview=None):
        """Disable all cookies by setting accept policy to NEVER."""
        try:
            if webview is None:
                return
            cookie_manager = webview.get_context().get_cookie_manager()
            cookie_manager.set_accept_policy(WebKit2.CookieAcceptPolicy.NEVER)
        except Exception:
            pass

    def set_samesite_cookie(self, cookie_manager, cookie):
        cookie.set_same_site(WebKit2.CookieSameSitePolicy.STRICT)
        cookie_manager.set_cookie(cookie)

    def attach_csp_listener(self, webview):
        manager = webview.get_user_content_manager()
        manager.connect("console-message-received", self.on_console_message)

    def on_console_message(self, manager, message):
        msg_text = message.get_text()
        if "Refused to load" in msg_text or "CSP" in msg_text:
            report = {"message": msg_text, "source": "console"}
            self.on_csp_violation(report)

class SocialTrackerBlocker:
    """Minimal social tracker blocker providing a domain substring blocklist."""
    def __init__(self):
        self.blocklist = [
            "facebook.com",
            "facebook.net",
            "fbcdn.net",
            "instagram.com",
            "t.co",
            "twitter.com",
            "x.com",
            "linkedin.com",
            "doubleclick.net",
            "google-analytics.com",
            "googletagmanager.com",
            "snapchat.com",
            "pixel.wp.com",
        ]

    def handle_blob_uri(self, request, user_data=None):
        """Handle blob: URIs for media streaming"""
        request.finish_error(WebKit2.NetworkError.CANCELLED, "Blob URI media playback not supported")

    def handle_data_uri(self, request, user_data=None):
        """Handle data: URIs for embedded content"""
        request.finish_error(WebKit2.NetworkError.CANCELLED, "Data URI handling not implemented")

    def handle_media_request(self, request, user_data=None):
        """Handle media requests for better streaming support"""
        uri = request.get_uri()
        if any(substring in uri for substring in self.blocklist):
            request.finish_error(WebKit2.NetworkError.CANCELLED, "Media request blocked")
            return
        request.finish()

class TorManager:
    def __init__(self, tor_port=9050, control_port=9051):
        """Initialize Tor manager to use system Tor instance.
        Args:
            tor_port: Port for SOCKS proxy (default: 9050)
            control_port: Port for Tor control (default: 9051)
        """
        self.tor_port = tor_port
        self.control_port = control_port
        self.controller = None
        self.is_running_flag = False
        self.tor_data_dir = os.path.join(os.path.expanduser('~'), '.tor', 'shadow-browser')
        self.torrc_path = os.path.join(self.tor_data_dir, 'torrc')
        self.tor_log_file = os.path.join(self.tor_data_dir, 'tor.log')
        self.password = None
        self.use_bridges = False
        self.proxy_settings = None
        self.use_system_tor = False

    def _check_system_tor_running(self):
        """Check if system Tor is already running on standard ports."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(("127.0.0.1", self.tor_port)) == 0:
                    return True
            return False
        except Exception as e:
            print(f"Error checking system Tor status: {e}")
            return False

    def _is_tor_already_running(self):
        """Check if a Tor process is already running using the data directory or standard ports.
        This method uses a multi-layered approach to detect Tor:
        1. First checks if Tor is listening on standard ports
        2. Then looks for Tor processes by name and command line arguments
        """
        try:
            import socket
            for port in [9050, 9051]:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    if sock.connect_ex(('127.0.0.1', port)) == 0:
                        return True
        except Exception as e:
            print(f"Socket check failed: {e}")
        try:
            if psutil is not None:
                for proc in psutil.process_iter(['name', 'cmdline']):
                    try:
                        if not proc.info['name'] or 'tor' not in proc.info['name'].lower():
                            continue
                        cmdline = proc.info['cmdline'] or []
                        if any(isinstance(arg, str) and
                              (self.tor_data_dir in arg or
                               any(port in arg for port in ['9050', '9051']))
                             for arg in cmdline):
                            return True
                    except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError):
                        continue
        except ImportError:
            pass
        return False

    def _create_torrc(self):
        """Create a torrc configuration file with enhanced security settings."""
        try:
            with open(self.torrc_path, 'w') as f:
                f.write(f"SOCKSPort {self.tor_port if self.tor_port else 'auto'}\n")
                f.write(f"ControlPort {self.control_port if self.control_port else 'auto'}\n")
                f.write(f"DataDirectory {self.tor_data_dir}\n")
                f.write(f"Log notice file {self.tor_log_file}\n")
                f.write("ClientOnly 1\n")
                f.write("SafeLogging 1\n")
                f.write("SafeSocks 1\n")
                f.write("WarnUnsafeSocks 1\n")
                f.write("StrictNodes 1\n")
                f.write("EnforceDistinctSubnets 1\n")
                f.write("NewCircuitPeriod 30\n")
                f.write("MaxCircuitDirtiness 10 minutes\n")
                f.write("MaxClientCircuitsPending 48\n")
                f.write("AvoidDiskWrites 1\n")
                f.write("DisableDebuggerAttachment 0\n")
                if self.password:
                    f.write(f"HashedControlPassword {self._hash_password()}\n")
                else:
                    f.write("CookieAuthentication 1\n")
                f.write("UseEntryGuards 1\n")
                f.write("NumEntryGuards 3\n")
                f.write("UseGuardFraction 1\n")
                f.write("UseMicrodescriptors 1\n")
                f.write("UseMicrodescriptors 1\n")
                f.write("ExitPolicy reject *:*\n")
                if self.use_bridges:
                    f.write("UseBridges 1\n")
                    f.write("Bridge obfs4 193.11.166.194:27015 1E2F3F6C31013377B838710AF02C77BEA4780F55 cert=FK8a9Aqghj9FwpbMp5Aog6UC5uvLQfk24UqBLidRsW0udof8OWaSpH6pdAKJreYwZVDpoGgA iat-mode=0\n")
                    f.write("ClientTransportPlugin obfs4 exec /usr/bin/obfs4proxy\n")
                if self.proxy_settings:
                    proxy_type = self.proxy_settings.get('type', 'socks5')
                    proxy_host = self.proxy_settings.get('host', '')
                    proxy_port = self.proxy_settings.get('port', '')
                    if proxy_host and proxy_port:
                        f.write(f"{proxy_type.upper()}Proxy {proxy_host}:{proxy_port}\n")
                        if 'username' in self.proxy_settings and 'password' in self.proxy_settings:
                            f.write(f"{proxy_type.upper()}ProxyAuthenticator {self.proxy_settings['username']}:{self.proxy_settings['password']}\n")
            return True
        except Exception as e:
            print(f"Error creating torrc: {e}")
            return False

    def _hash_password(self):
        """Hash the password for Tor control port authentication."""
        if not self.password:
            return ""
        try:
            salt = os.urandom(8)
            key = hashlib.pbkdf2_hmac('sha1', self.password.encode(), salt, 1000, 32)
            return '16:' + salt.hex() + key.hex()
        except Exception as e:
            print(f"Error hashing password: {e}")
            return ""

    def start(self):
        """Start the Tor process with proper error handling and port fallback."""
        try:
            if not shutil.which("tor"):
                print("Tor executable not found. Please install Tor.")
                return False
            if hasattr(self, 'process') and self.process:
                self.process.terminate()
                self.process = None
            if self._check_system_tor_running():
                print("System Tor service is running")
                self.use_system_tor = True
                self.is_running_flag = True
                return True
            if self._is_tor_already_running():
                print("Found existing Tor process. Attempting to connect...")
                for control_port in [9051, 9151, 9152, 9153]:
                    try:
                        controller = Controller.from_port(port=control_port)
                        controller.authenticate()
                        self.controller = controller
                        socks_ports = controller.get_conf('SocksPort', multiple=True)
                        if socks_ports:
                            try:
                                self.tor_port = int(socks_ports[0].split(':')[0])
                                print(f"Found Tor SOCKS port: {self.tor_port}")
                            except (ValueError, IndexError) as e:
                                print(f"Warning: Could not parse SOCKS port: {e}")
                                self.tor_port = 9050
                        control_ports = controller.get_conf('ControlPort', multiple=True)
                        if control_ports:
                            try:
                                self.control_port = int(control_ports[0])
                                print(f"Found Tor control port: {self.control_port}")
                            except (ValueError, IndexError) as e:
                                print(f"Warning: Could not parse control port: {e}")
                                self.control_port = control_port
                        try:
                            controller.get_info('version')
                            self.is_running_flag = True
                            print(f"Successfully connected to existing Tor instance: SOCKS={self.tor_port}, Control={self.control_port}")
                            return True
                        except Exception as e:
                            print(f"Warning: Could not verify Tor control connection: {e}")
                            continue
                    except Exception as e:
                        print(f"Failed to connect to Tor control port {control_port}: {str(e)}")
                        continue
                if not self.is_running_flag:
                    print("Warning: Could not connect to any running Tor instance, will try to start a new one")
            return self._start_new_tor_instance()
        except Exception as e:
            print(f"Error in TorManager.start(): {e}")
            import traceback
            traceback.print_exc()
            return False

    def stop(self):
        """Stop the Tor process and clean up resources."""
        success = True
        if hasattr(self, 'controller') and self.controller:
            try:
                if self.controller.is_alive():
                    self.controller.close()
            except Exception:
                success = False
            finally:
                self.controller = None
        if hasattr(self, 'process') and self.process:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            self.process.kill()
                            self.process.wait()
                        except Exception:
                            success = False
            except Exception:
                success = False
            finally:
                self.process = None
        self.is_running_flag = False
        return success

    def is_running(self):
        """Check if Tor is running and connected."""
        if self.use_system_tor:
            return self._check_system_tor_running()
        if self.controller:
            try:
                self.controller.get_info("version")
                return True
            except Exception as e:
                print(f"Error checking Tor controller status: {e}")
                return False
        return self.is_running_flag

    def new_identity(self):
        """Request a new Tor circuit using system Tor."""
        if not self.is_running():
            if not self.start():
                return False
        try:
            if self.controller and self.controller.is_alive():
                self.controller.signal("NEWNYM")
                print("Requested new Tor circuit")
                return True
        except Exception as e:
            print(f"Error requesting new Tor circuit: {e}")
        return False

    def setup_proxy(self, web_context):
        """Configure web context to use Tor proxy."""
        if not self.is_running():
            if not self.start():
                print("Failed to start Tor")
                return False
        proxy_port = self.tor_port
        print(f"Configuring SOCKS5 proxy on 127.0.0.1:{proxy_port}")
        try:
            proxy_settings = WebKit2.NetworkProxySettings()
            proxy_settings.add_proxy_for_scheme("http", "socks5://127.0.0.1:{}".format(proxy_port))
            proxy_settings.add_proxy_for_scheme("https", "socks5://127.0.0.1:{}".format(proxy_port))
            proxy_settings.add_proxy_for_scheme("ftp", "socks5://127.0.0.1:{}".format(proxy_port))
            web_context.set_network_proxy_settings(proxy_settings)
            return True
        except Exception as e:
            print(f"Error setting up proxy: {e}")
            return False

    def _start_new_tor_instance(self):
        """Check if system Tor service is running and connect to it."""
        print("Checking for system Tor service...")
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", "tor"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                print("System Tor service is running")
                try:
                    self.controller = Controller.from_port(port=9051)
                    self.controller.authenticate()
                    self.is_running_flag = True
                    try:
                        socks_ports = self.controller.get_conf('SocksPort', multiple=True)
                        if socks_ports:
                            actual_socks_port = socks_ports[0].split(':')[0]
                            self.tor_port = int(actual_socks_port)
                            print(f"Connected to system Tor on SOCKS port {self.tor_port}")
                    except Exception as e:
                        print(f"Warning: Could not determine SOCKS port: {e}")
                        print("Using default SOCKS port 9050")
                        self.tor_port = 9050
                    return True
                except Exception as e:
                    print(f"Error connecting to Tor control port: {e}")
                    print("Make sure the system Tor service has ControlPort 9051 enabled")
            else:
                print("System Tor service is not running")
                print("Please start the Tor service with: sudo systemctl start tor")
        except FileNotFoundError:
            print("systemctl not found, checking Tor process directly...")
            if self._check_system_tor_running():
                print("Found Tor running on standard port 9050")
                self.tor_port = 9050
                self.control_port = 9051
                return True
            else:
                print("Could not find a running Tor instance")
                print("Please install and start the Tor service")
        return False

    def _print_bootstrap_lines(self, line=None):
        """Print Tor bootstrap progress (stub for compatibility)."""
        pass

class Tab:
    def __init__(self, url, webview, scrolled_window=None):
        self.url = url
        self.webview = webview
        self.label_widget = None
        self.close_button = None
        self.scrolled_window = scrolled_window
        self.header_box = None
        self.last_activity = time.time()
        self.pinned = False
        self.muted = False
        self.favicon = None

    def update_favicon(self, favicon):
        if not favicon:
            return
        self.favicon = favicon
        if not self.favicon_widget:
            self.favicon_widget = Gtk.Image()
            self.favicon_widget.set_size_request(16, 16)
            if self.label_box:
                self.label_box.prepend(self.favicon_widget)
                self.favicon_widget.set_visible(True)
        if hasattr(favicon, 'get_type') and 'Gdk' in str(favicon.get_type()):
            self.favicon_widget.set_from_paintable(favicon)
        elif isinstance(favicon, str):
            if os.path.exists(favicon) or favicon.startswith(('http://', 'https://', 'file://')):
                self.favicon_widget.set_from_file(favicon)
            else:
                if hasattr(favicon, 'get_paintable'):
                    self.favicon_widget.set_from_paintable(favicon.get_paintable())
                else:
                    self.favicon_widget.set_from_paintable(favicon)
        self.favicon_widget.set_visible(True)

    def update_activity(self):
        self.last_activity = time.time()

    def __repr__(self):
        return f"<Tab url='{self.url}' pinned={self.pinned} muted={self.muted}>"

class ShadowBrowser(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.shadowyfigure.shadowbrowser")
        self.debug_mode = False
        if WebKit2 is None:
            print("Error: WebKit2 is not available. Please install WebKit2GTK or similar package.")
            print("On Ubuntu/Debian: sudo apt install gir1.2-WebKit22-4.0")
            print("On Fedora: sudo dnf install webkit2gtk3-devel")
            print("On Arch: sudo pacman -S webkit2gtk")
            sys.exit(1)
        self.connect('activate', self.on_activate)
        self.webview = WebKit2.WebView()
        self.content_manager = WebKit2.UserContentManager()
        self.adblocker = AdBlocker()
        self.social_tracker_blocker = SocialTrackerBlocker()
        self.setup_webview_settings(self.webview)
        self.webview.connect("create", self.on_webview_create)
        self.bookmarks = self.load_json(BOOKMARKS_FILE)
        self.history = self.load_json(HISTORY_FILE)
        self.tabs = []
        self.tabs_lock = threading.Lock()
        self.blocked_urls = []
        self.window = None
        self.notebook = None
        self.url_entry = None
        self.home_url = "https://duckduckgo.com/"
        self.theme = "dark"
        self.tor_enabled = True
        self.tor_manager = None
        self.tor_status = "disabled"
        self.initialize_tor()
        self.download_manager = DownloadManager(None)
        self.active_downloads = 0
        self.context = ssl.create_default_context()
        self.error_handlers = {}
        self.register_error_handlers()
        self.download_spinner = Gtk.Spinner()
        self.download_spinner.set_visible(False)
        self.bookmark_menu = None
        self.setup_security_policies()
        self.download_manager.on_download_start_callback = self.on_download_start
        self.download_manager.on_download_finish_callback = self.on_download_finish

    def create_toolbar(self):
        """Create the main toolbar with navigation controls (GTK3 version)."""
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)
        toolbar.set_property("margin-left", 6)
        toolbar.set_property("margin-right", 6)
        back_button = Gtk.Button.new_from_icon_name("go-previous", Gtk.IconSize.BUTTON)
        back_button.set_tooltip_text("Go back")
        back_button.connect("clicked", self.on_back_clicked)
        toolbar.pack_start(back_button, False, False, 0)
        forward_button = Gtk.Button.new_from_icon_name("go-next", Gtk.IconSize.BUTTON)
        forward_button.set_tooltip_text("Go forward")
        forward_button.connect("clicked", self.on_forward_clicked)
        toolbar.pack_start(forward_button, False, False, 0)
        refresh_button = Gtk.Button.new_from_icon_name("view-refresh", Gtk.IconSize.BUTTON)
        refresh_button.set_tooltip_text("Refresh")
        refresh_button.connect("clicked", self.on_refresh_clicked)
        toolbar.pack_start(refresh_button, False, False, 0)
        self.url_entry = Gtk.Entry()
        self.url_entry.set_hexpand(True)
        self.url_entry.set_placeholder_text("Enter URL or search term")
        self.url_entry.connect("activate", self.on_go_clicked)
        toolbar.pack_start(self.url_entry, True, True, 0)
        go_button = Gtk.Button.new_from_icon_name("go-jump", Gtk.IconSize.BUTTON)
        go_button.set_tooltip_text("Go")
        go_button.connect("clicked", self.on_go_clicked)
        toolbar.pack_start(go_button, False, False, 0)
        new_tab_button = Gtk.Button.new_from_icon_name("tab-new", Gtk.IconSize.BUTTON)
        new_tab_button.set_tooltip_text("New tab")
        new_tab_button.connect("clicked", self.on_new_tab_clicked)
        toolbar.pack_start(new_tab_button, False, False, 0)
        self.zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.zoom_box.get_style_context().add_class("linked")
        self.zoom_box.pack_start(
            self._create_icon_button("zoom-out", self.on_zoom_out_clicked, "Zoom Out"),
            False, False, 0
        )
        self.zoom_box.pack_start(
            self._create_icon_button("zoom-fit-best", self.on_zoom_reset_clicked, "Reset Zoom"),
            False, False, 0
        )
        self.zoom_box.pack_start(
            self._create_icon_button("zoom-in", self.on_zoom_in_clicked, "Zoom In"),
            False, False, 0
        )
        toolbar.pack_start(self.zoom_box, False, False, 0)
        dev_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        dev_box.get_style_context().add_class("linked")
        inspect_button = Gtk.Button(label="Inspect")
        inspect_button.set_tooltip_text("Open Web Inspector")
        inspect_button.connect("clicked", self.on_inspect_clicked)
        dev_box.pack_start(inspect_button, False, False, 0)
        toolbar.pack_start(dev_box, False, False, 0)
        self.add_download_spinner(toolbar)
        return toolbar

    def create_menubar(self):
        menubar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        try:
            if hasattr(self, 'bookmark_menu_button') and self.bookmark_menu_button:
                parent = self.bookmark_menu_button.get_parent()
                if parent:
                    parent.remove(self.bookmark_menu_button)
            self.bookmark_menu_button = Gtk.MenuButton(label="Bookmarks")
            self.bookmark_menu_button.set_tooltip_text("Show bookmarks")
            self.bookmark_menu = Gtk.Menu()
            self.update_bookmarks_menu(self.bookmark_menu)
            self.bookmark_menu_button.set_popup(self.bookmark_menu)
            menubar.pack_start(self.bookmark_menu_button, False, False, 0)
        except Exception:
            pass
        try:
            if hasattr(self, 'window') and self.window:
                self.window.connect("key-press-event", self._on_key_pressed)
        except Exception:
            pass
        try:
            download_button = Gtk.Button(label="Downloads")
            download_button.set_tooltip_text("Open Downloads Folder")
            download_button.connect("clicked", self.on_downloads_clicked)
            menubar.pack_start(download_button, False, False, 0)
        except Exception:
            pass
        try:
            settings_button = Gtk.Button(label="Settings")
            settings_button.set_tooltip_text("Open settings dialog")
            settings_button.connect("clicked", lambda x: self.on_settings_clicked(x))
            menubar.pack_start(settings_button, False, False, 0)
        except Exception:
            pass
        try:
            self.tor_button = Gtk.Button(label="Tor")
            self.tor_button.set_tooltip_text("Toggle Tor connection")
            self.tor_button.get_style_context().add_class("tor-button")
            self.update_tor_button()
            self.tor_button.connect("clicked", self.on_tor_button_clicked)
            menubar.pack_start(self.tor_button, False, False, 0)
        except Exception:
            pass
        try:
            clear_data_button = Gtk.Button(label="Clear Data")
            clear_data_button.set_tooltip_text("Clear browsing data")
            clear_data_button.connect("clicked", lambda x: self.create_clear_data_dialog().show_all())
            menubar.pack_start(clear_data_button, False, False, 0)
        except Exception:
            pass
        try:
            about_button = Gtk.Button(label="About")
            about_button.connect("clicked", self.on_about)
            menubar.pack_start(about_button, False, False, 0)
        except Exception:
            pass
        return menubar

    def update_bookmarks_menu(self, menu):
        """Update the bookmarks menu with current bookmarks."""
        if not menu:
            return
        items = menu.get_children()
        for item in items[3:]:
            menu.remove(item)
        if self.bookmarks:
            for bookmark in self.bookmarks:
                if isinstance(bookmark, dict) and 'url' in bookmark and 'title' in bookmark:
                    bookmark_item = Gtk.MenuItem(label=bookmark['title'])
                    bookmark_item.connect("activate", lambda _, url=bookmark['url']: self.load_url(url))
                    menu.append(bookmark_item)
        menu.show_all()

    def on_activate(self, app):
        """Handle application activation - create and show the main window."""
        try:
            self.window = Gtk.ApplicationWindow(application=app)
            self.window.set_title("Shadow Browser")
            self.window.set_default_size(1200, 800)
            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            menubar = self.create_menubar()
            main_box.pack_start(menubar, False, False, 0)
            toolbar = self.create_toolbar()
            main_box.pack_start(toolbar, False, False, 0)
            self.notebook = Gtk.Notebook()
            self.notebook.set_scrollable(True)
            self.notebook.set_show_border(False)
            self.notebook.set_show_tabs(True)
            self.notebook.show()
            main_box.pack_start(self.notebook, True, True, 0)
            self.window.add(main_box)
            main_box.show_all()
            self.download_manager.parent_window = self.window
            self.add_new_tab(self.home_url)
            self.window.connect("destroy", self.on_window_destroy)
            self.window.present()
            self.update_bookmarks_menu(self.bookmark_menu)
        except Exception as e:
            print(f"Error in on_activate: {e}")
            import traceback
            traceback.print_exc()
        try:
            self.adblocker.inject_to_webview(self.content_manager)
            self.inject_nonce_respecting_script()
            self.inject_remove_malicious_links()
            self.inject_adware_cleaner()
            if hasattr(self, 'webview'):
                self.disable_biometrics_in_webview(self.webview)
            self.content_manager.register_script_message_handler("voidLinkClicked")
            self.content_manager.connect(
                "script-message-received::voidLinkClicked", self.on_void_link_clicked
            )
            test_script = WebKit2.UserScript.new(
                "console.log('Test script injected into shared content manager');",
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
            )
            self.content_manager.add_script(test_script)
        except Exception:
            pass

    def register_error_handlers(self):
        """Register error handlers for various browser components."""
        self.error_handlers = {
            'network': self.handle_network_error,
            'ssl': self.handle_ssl_error,
            'download': self.handle_download_error,
            'javascript': self.handle_javascript_error,
        }

    def handle_network_error(self, error):
        """Handle network-related errors."""
        logging.error(f"Network error: {error}")

    def handle_ssl_error(self, error):
        """Handle SSL/TLS related errors."""
        logging.error(f"SSL error: {error}")

    def handle_download_error(self, error):
        """Handle download-related errors."""
        logging.error(f"Download error: {error}")

    def handle_javascript_error(self, error):
        """Handle JavaScript execution errors."""
        logging.error(f"JavaScript error: {error}")

    def on_webview_load_changed(self, webview, load_event):
        """Handle webview load state changes - alias for on_load_changed."""
        self.on_load_changed(webview, load_event)

    def on_webview_load_failed(self, webview, load_event, error):
        """Handle webview load failures."""
        logging.error(f"Load failed for {webview.get_uri()}: {error}")

    def on_webview_tls_error(self, webview, certificate, error):
        """Handle TLS certificate errors."""
        logging.error(f"TLS error for {webview.get_uri()}: {error}")

    def on_webview_title_changed(self, webview, param):
        """Handle webview title changes - alias for on_title_changed."""
        self.on_title_changed(webview, param)

    def on_webview_decide_policy(self, webview, decision, decision_type):
        """Handle webview policy decisions - alias for on_decide_policy."""
        self.on_decide_policy(webview, decision, decision_type)

    def on_webview_context_menu(self, webview, context_menu, hit_test_result, user_data=None):
        """Handle webview context menu."""
        context_menu.remove_all()
        pass

    def on_download_started(self, context, download):
        """Handle download started events - delegate to download manager."""
        self.download_manager.on_download_started(context, download)

    def initialize_tor(self, retry_count=0, max_retries=2):
        """Initialize Tor with proper error handling and fallback mechanisms.
        Args:
            retry_count: Current retry attempt
            max_retries: Maximum number of retry attempts
        Returns:
            bool: True if Tor was successfully initialized, False otherwise
        """
        if not self.tor_enabled:
            self.tor_status = "disabled"
            return False
        try:
            if not self.tor_manager:
                self.tor_manager = TorManager()
            if self.tor_manager.is_running():
                self.tor_status = "running"
                return True
            if retry_count >= max_retries:
                self.tor_status = "failed"
                print("Error: Failed to start Tor after multiple attempts")
                return False
            if self.tor_manager.start():
                self.tor_status = "running"
                tor_port = getattr(self.tor_manager, 'tor_port', 9050)
                try:
                    session = requests.Session()
                    session.proxies = {
                        'http': f'socks5h://127.0.0.1:{tor_port}',
                        'https': f'socks5h://127.0.0.1:{tor_port}'
                    }
                    response = session.get('https://check.torproject.org/api/ip', timeout=30)
                    response.raise_for_status()
                    result = response.json()
                    if result.get('IsTor', False):
                        print(f"Successfully connected to Tor on port {tor_port}")
                        self.tor_status = "running"
                        return True
                    else:
                        print("Warning: Tor is running but not properly configured")
                        print(f"Response: {result}")
                        self.tor_status = "misconfigured"
                        return False
                except requests.exceptions.RequestException as e:
                    print(f"Tor connection test failed: {str(e)}")
                    if hasattr(e, 'response') and e.response is not None:
                        print(f"Response status: {e.response.status_code}")
                        print(f"Response text: {e.response.text[:500]}")
                    return self.initialize_tor(retry_count + 1, max_retries)
            else:
                print("Failed to start Tor process")
                if retry_count < max_retries:
                    print(f"Retrying Tor startup... (attempt {retry_count + 1}/{max_retries})")
                    if self.tor_manager:
                        self.tor_manager.stop()
                        self.tor_manager = None
                    return self.initialize_tor(retry_count + 1, max_retries)
                return False
        except Exception as e:
            import traceback
            print(f"Unexpected error initializing Tor: {str(e)}")
            print("Traceback:")
            traceback.print_exc()
            self.tor_status = "error"
            if retry_count < max_retries:
                if hasattr(self, 'tor_manager') and self.tor_manager:
                    self.tor_manager.stop()
                    self.tor_manager = None
                return self.initialize_tor(retry_count + 1, max_retries)
            return False

    def create_secure_webview(self):
        """
        Create a new secure WebView with all necessary scripts and handlers.
        Returns:
            WebKit2.WebView: A configured WebView instance or None if creation fails
        """
        try:
            content_manager = WebKit2.UserContentManager()
            webview = WebKit2.WebView(user_content_manager=content_manager)
            webview.set_hexpand(True)
            webview.set_vexpand(True)
            webview._content_manager = content_manager
            self._register_webview_message_handlers(webview)
            self.adblocker.inject_to_webview(content_manager)
            self.inject_nonce_respecting_script()
            self.inject_remove_malicious_links()
            self.inject_adware_cleaner()
            self.disable_biometrics_in_webview(webview)
            self.inject_mouse_event_script()
            self.adblocker.enable_csp(webview)
            webview.connect("create", self.on_webview_create)
            return webview
        except Exception:
            pass
            try:
                webview = WebKit2.WebView()
                if not webview:
                    print("Error: Failed to create WebView instance")
                    return None
                webview.set_hexpand(True)
                webview.set_vexpand(True)
                if not hasattr(webview, '_signal_handlers'):
                    webview._signal_handlers = []
                def on_destroy(webview, *args):
                    self.cleanup_webview(webview)
                handler_id = webview.connect('destroy', on_destroy)
                webview._signal_handlers.append(handler_id)
                return webview
            except Exception as e:
                print(f"Error creating WebView: {e}")
                return None

    def cleanup_webview(self, webview):
        """
        Clean up resources used by a WebView.
        Args:
            webview: The WebView to clean up
        """
        if not webview:
            return
        try:
            for handler_id in getattr(webview, '_signal_handlers', []):
                try:
                    webview.handler_disconnect(handler_id)
                except Exception as e:
                    print(f"Error disconnecting signal: {e}")
            if hasattr(webview, '_content_manager'):
                try:
                    content_manager = webview._content_manager
                    if hasattr(content_manager, 'remove_all_scripts'):
                        content_manager.remove_all_scripts()
                    if hasattr(webview, '_handler_ids'):
                        for content_mgr, handler_id in webview._handler_ids:
                            if handler_id > 0 and content_mgr:
                                try:
                                    content_mgr.disconnect(handler_id)
                                except Exception as e:
                                    print(f"Error disconnecting handler: {e}")
                        del webview._handler_ids
                    del webview._content_manager
                except Exception as e:
                    print(f"Error cleaning up content manager: {e}")
            try:
                webview.load_uri('about:blank')
                if hasattr(webview, 'stop_loading'):
                    webview.stop_loading()
                if hasattr(webview, 'load_html_string'):
                    webview.load_html_string('', 'about:blank')
            except Exception as e:
                print(f"Error clearing WebView content: {e}")
            parent = webview.get_parent()
            if parent:
                parent.remove(webview)
        except Exception as e:
            print(f"Error during WebView cleanup: {e}")
        finally:
            import gc
            gc.collect()

    def _register_webview_message_handlers(self, webview):
        content_manager = webview._content_manager
        content_manager.register_script_message_handler("voidLinkClicked")
        handler_id = content_manager.connect(
            "script-message-received::voidLinkClicked",
            self.on_void_link_clicked
        )
        content_manager.register_script_message_handler("windowOpenHandler")
        handler_id2 = content_manager.connect(
            "script-message-received::windowOpenHandler",
            self.on_window_open_handler
        )
        if not hasattr(webview, '_handler_ids'):
            webview._handler_ids = []
        webview._handler_ids.append((content_manager, handler_id))
        webview._handler_ids.append((content_manager, handler_id2))

    def inject_wau_tracker_removal_script(self):
        try:
            wau_removal_script = WebKit2.UserScript.new(
                """
                (function() {
                    var wauScript = document.getElementById('_wau3wa');
                    if (wauScript) {
                        var parentDiv = wauScript.parentElement;
                        if (parentDiv && parentDiv.style && parentDiv.style.display === 'none') {
                            parentDiv.remove();
                        } else {
                            wauScript.remove();
                        }
                    }
                    var scripts = document.getElementsByTagName('script');
                    for (var i = scripts.length - 1; i >= 0; i--) {
                        var src = scripts[i].getAttribute('src');
                        if (src && src.indexOf('waust.at') !== -1) {
                            scripts[i].remove();
                        }
                    }
                })();
                """,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
            )
            self.content_manager.add_script(wau_removal_script)
        except Exception:
            pass

    def on_download_start(self):
        try:
            if not self.download_spinner:
                return
            self.active_downloads += 1
            if self.active_downloads == 1:
                GLib.idle_add(self.download_spinner.start)
                GLib.idle_add(lambda: self.download_spinner.set_visible(True))
        except Exception:
            pass

    def on_download_finish(self):
        try:
            if not self.download_spinner:
                return
            if self.active_downloads > 0:
                self.active_downloads -= 1
            if self.active_downloads == 0:
                GLib.idle_add(self.download_spinner.stop)
                GLib.idle_add(lambda: self.download_spinner.set_visible(False))
        except Exception:
            pass

    def setup_security_policies(self):
        """Setup comprehensive security policies for the browser."""
        self.blocked_urls.extend(
            [
                "accounts.google.com/gsi/client",
                "facebook.com/connect",
                "twitter.com/widgets",
                "youtube.com/player_api",
                "doubleclick.net",
                "googletagmanager.com",
            ]
        )

    def inject_security_headers(self, webview, load_event):
        """
        Configure WebView for optimal media playback and inject security headers.
        This method ensures all necessary media-related settings are enabled and properly
        configured for smooth video and audio playback while maintaining security.
        """
        if load_event == WebKit2.LoadEvent.STARTED:
            try:
                uri = webview.get_uri()
                if not uri:
                    return False
                if not (uri.startswith(('http:', 'https:', 'blob:'))):
                    return False
                if any(blocked_url in uri.lower() for blocked_url in self.blocked_urls):
                    return True
                settings = webview.get_settings()
                settings.set_user_agent(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit2/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                core_settings = {
                    'enable-javascript': True,
                    'enable-page-cache': True,
                    'enable-smooth-scrolling': True,
                    'enable-fullscreen': True,
                    'enable-media': True,
                    'enable-media-stream': True,
                    'enable-mediasource': True,
                    'enable-encrypted-media': True,
                    'enable-webrtc': True,
                    'enable-webaudio': True,
                    'media-playback-requires-user-gesture': False,
                    'media-playback-allows-inline': True,
                    'auto-load-images': True,
                    'enable-java': False,
                    'enable-plugins': False,
                    'enable-html5-database': False,
                    'enable-html5-local-storage': True,
                    'enable-site-specific-quirks': True,
                    'enable-universal-access-from-file-uris': False,
                    'enable-xss-auditor': True,
                    'enable-web-security': True,
                    'allow-file-access-from-file-urls': False,
                    'allow-universal-access-from-file-urls': False,
                    'enable-developer-extras': self.debug_mode,
                    'enable-write-console-messages-to-stdout': self.debug_mode,
                    'enable-javascript-markup': True,
                    'enable-media-capabilities': True,
                    'enable-media-source': True,
                                    }
                for prop, value in core_settings.items():
                    try:
                        settings.set_property(prop, value)
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Warning: Could not set {prop}: {str(e)}")
                if hasattr(settings, 'set_auto_play_policy'):
                    try:
                        settings.set_auto_play_policy(WebKit2.AutoPlayPolicy.ALLOW)
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Warning: Could not set autoplay policy: {str(e)}")
                if hasattr(settings, 'set_webrtc_ip_handling_policy'):
                    try:
                        settings.set_webrtc_ip_handling_policy(
                            WebKit2.WebRTCIPHandlingPolicy.DEFAULT_PUBLIC_AND_PRIVATE_INTERFACES)
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Warning: Could not set WebRTC policy: {str(e)}")
                webview.set_settings(settings)
                if hasattr(settings, 'set_enable_media_cache'):
                    settings.set_enable_media_cache(True)
                if hasattr(settings, 'set_enable_mediasource'):
                    settings.set_enable_mediasource(True)
                settings.set_enable_smooth_scrolling(True)
                if hasattr(settings, 'set_enable_webaudio'):
                    settings.set_enable_webaudio(True)
                return False
            except Exception as e:
                print(f"Error in inject_security_headers: {str(e)}")
                return False

    def block_social_trackers(self, webview, decision, decision_type):
        """Block social media trackers."""
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            nav_action = decision.get_navigation_action()
            uri = nav_action.get_request().get_uri()
            if any(
                tracker in uri.lower()
                for tracker in self.social_tracker_blocker.blocklist
            ):
                decision.ignore()
                return True
        return False

    def uuid_to_token(self, uuid_str: str) -> str:
        """
        Convert a UUID string to a short base64url token.
        """
        import base64
        import uuid
        try:
            u = uuid.UUID(uuid_str)
            b = u.bytes
            token = base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
            return token
        except Exception:
            return uuid_str

    def transform_embed_selector_links(self, html_content: str) -> str:
        """
        Transform UUIDs in <a> tags with class 'embed-selector asg-hover even' and onclick handlers
        by replacing UUIDs with short tokens in the onclick attribute.
        Returns:
            str: The transformed HTML content with UUIDs replaced by tokens
        """
        import re
        uuid_pattern = r'onclick="[^"]*([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})[^"]*"'
        def replace_uuid(match):
            uuid_str = match.group(1)
            try:
                return f'onclick="{self.uuid_to_token(uuid_str)}"'
            except Exception:
                return match.group(0)
        return re.sub(uuid_pattern, replace_uuid, html_content, flags=re.IGNORECASE)

    def dbneg(self, id_string: str) -> str:
        """
        Python equivalent of the JavaScript dbneg function.
        Constructs a URL with the given IDs as a query parameter.
        """
        base_url = "https://example.com/dbneg?ids="
        import urllib.parse
        encoded_ids = urllib.parse.quote(id_string)
        return base_url + encoded_ids

    def inject_window_open_handler(self, content_manager):
        """Inject JS to override window.open and send URLs to Python for new tab opening."""
        js_code = '''
        (function() {
            console.log('[ShadowBrowser] Injecting window.open override');
            const originalOpen = window.open;
            window.open = function(url, name, features) {
                console.log('[ShadowBrowser] window.open called with:', url, name, features);
                // Check if URL is blocked by adblocker first
                if (typeof isUrlBlocked === 'function' && isUrlBlocked(url)) {
                    console.log('[ShadowBrowser] window.open blocked by adblocker:', url);
                    return null;
                }
                // Always send a string to Python, even if url is undefined/null
                var urlToSend = (typeof url === 'string' && url) ? url : '';
                if (window.WebKit2 && window.WebKit2.messageHandlers && window.WebKit2.messageHandlers.windowOpenHandler) {
                    window.WebKit2.messageHandlers.windowOpenHandler.postMessage(urlToSend);
                    return null;
                }
                return originalOpen.apply(this, arguments);
            };
        })();
        '''
        print('[ShadowBrowser] Injecting window.open handler JS')
        content_manager.add_script(
            WebKit2.UserScript.new(
                js_code,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
            )
        )

    def on_window_open_handler(self, user_content_manager, js_message):
        """Handle window.open JS calls and open the URL in a new tab."""
        print('[ShadowBrowser] Python window.open handler triggered')
        try:
            data = js_message.get_js_value() if hasattr(js_message, 'get_js_value') else js_message
            print(f'[ShadowBrowser] JS message data: {data!r}')
            url = None
            if isinstance(data, dict):
                url = data.get('url')
            elif isinstance(data, str):
                url = data
            if url is None:
                print('[ShadowBrowser] No URL received from JS (None)')
            elif not isinstance(url, str):
                print(f'[ShadowBrowser] URL received is not a string: {url!r}')
                url = str(url)
            url = url.strip() if isinstance(url, str) else ''
            print(f'[ShadowBrowser] window.open URL to open (after strip): {url!r}')
            if url:
                self.open_url_in_new_tab(url)
            else:
                print('[ShadowBrowser] No valid URL provided to open_url_in_new_tab (empty or blank)')
        except Exception as e:
            print(f'[ShadowBrowser] Exception in on_window_open_handler: {e}')

    def setup_webview_settings(self, webview):
        """Configure settings for a WebKit2.WebView.
        Args:
            webview: The WebKit2.WebView to configure
        """
        try:
            settings = webview.get_settings()
            settings.set_enable_developer_extras(True)
            settings.set_enable_smooth_scrolling(True)
            settings.set_enable_javascript(True)
            settings.set_enable_webgl(True)
            settings.set_enable_media(True)
            settings.set_enable_media_stream(True)
            settings.set_enable_webrtc(True)
            settings.set_enable_html5_database(True)
            settings.set_enable_html5_local_storage(True)
            settings.set_allow_modal_dialogs(False)
            settings.set_javascript_can_open_windows_automatically(False)
            settings.set_enable_fullscreen(True)
            settings.set_enable_site_specific_quirks(True)
            settings.set_default_font_family('Sans')
            settings.set_monospace_font_family('Monospace')
            settings.set_serif_font_family('Serif')
            settings.set_sans_serif_font_family('Sans')
            webview.set_settings(settings)
            self._connect_webview_signals(webview)
        except Exception as e:
            logging.error(f"Failed to set up webview settings: {e}")

    def _connect_webview_signals(self, webview):
        """Connect common webview signals."""
        try:
            webview.connect('load-changed', self.on_webview_load_changed)
            webview.connect('load-failed', self.on_webview_load_failed)
            webview.connect('load-failed-with-tls-errors', self.on_webview_tls_error)
            webview.connect('notify::title', self.on_webview_title_changed)
            webview.connect('create', self.on_webview_create)
            webview.connect('decide-policy', self.on_webview_decide_policy)
            webview.connect('context-menu', self.on_webview_context_menu)
            # Handle download signal with compatibility check like shadowbrowser.py
            try:
                if hasattr(webview, 'download-started'):
                    webview.connect("download-started", self.on_download_started)
                elif hasattr(webview, 'download-begin'):
                    webview.connect("download-begin", self.on_download_started)
            except Exception as e:
                if self.debug_mode:
                    print(f"Error connecting download signal: {e}")
        except Exception as e:
            logging.error(f"Failed to connect webview signals: {e}")

    def get_current_webview(self):
        """Return the webview of the currently active tab."""
        current_page = self.notebook.get_current_page()
        if current_page == -1:
            return None
        child = self.notebook.get_nth_page(current_page)
        if child is None:
            return None
        if isinstance(child, Gtk.ScrolledWindow):
            inner_child = child.get_child()
            if isinstance(inner_child, Gtk.Viewport):
                webview = inner_child.get_child()
                return webview
            return inner_child

    def replace_uuid(self, match):
        original = match.group(0)
        uuid_str = match.group(1)
        token = self.uuid_to_token(uuid_str)
        replaced = original.replace(uuid_str, token)
        return replaced

    def on_webview_console_message(self, webview, level, message, line, source_id):
        pass

    def on_console_message_received(self, user_content_manager, js_message):
        pass

    def inject_video_ad_skipper(self, webview):
        js_code = """
        (function() {
            console.log('Video Ad Skipper script injected');
            function clickSkip() {
                const skipBtn = document.querySelector('.ytp-ad-skip-button');
                if (skipBtn) {
                    skipBtn.click();
                    console.log('Ad skipped');
                }
            }
            // Observe DOM changes to detect skip button dynamically
            const observer = new MutationObserver(() => {
                clickSkip();
            });
            observer.observe(document.body, { childList: true, subtree: true });
            // Also run periodically as fallback
            setInterval(clickSkip, 1000);
        })();
        """
        script = WebKit2.UserScript.new(
            js_code,
            WebKit2.UserContentInjectedFrames.ALL_FRAMES,
            WebKit2.UserScriptInjectionTime.END,
        )
        webview.get_user_content_manager().add_script(script)

    def _process_clicked_url(self, url, metadata):
        """
        Process a clicked URL with the given metadata.
        Args:
            url: The URL that was clicked
            metadata: Additional metadata about the click event
        """
        try:
            if url.startswith('/'):
                current_uri = self.webview.get_uri()
                if current_uri:
                    from urllib.parse import urljoin
                    abs_url = urljoin(current_uri, url)
                    url = abs_url
            if url.startswith('javascript:'):
                return
            self.open_url_in_new_tab(url)
        except Exception:
            pass

    def on_javascript_finished(self, webview, result, user_data):
        """Handle the result of JavaScript execution."""
        try:
            js_result = webview.run_javascript_finish(result)
            if js_result:
                value = js_result.get_js_value()
                if value and value.is_string():
                    print(f"JavaScript result: {value.to_string()}")
                else:
                    print("JavaScript executed, no string return value")
        except Exception as e:
            print(f"Error executing JavaScript: {e}")

    def on_void_link_clicked(self, user_content_manager, js_message):
        """
        Handle clicks on void links and other clickable elements that don't have direct hrefs.
        Args:
            user_content_manager: The WebKit2.UserContentManager that received the message
            js_message: The message containing click data from JavaScript
        """
        try:
            print("[VOID_LINK_CLICKED] Triggered")
            if hasattr(js_message, 'get_js_value'):
                message_data = js_message.get_js_value()
                print(f"[VOID_LINK_CLICKED] js_message.get_js_value(): {message_data}")
                if hasattr(message_data, 'to_dict') and callable(getattr(message_data, 'to_dict')):
                    message_data = message_data.to_dict()
                elif isinstance(message_data, str):
                    try:
                        message_data = json.loads(message_data)
                    except Exception as e:
                        print(f"[VOID_LINK_CLICKED] JSON decode error: {e}")
                url = None
                metadata = {}
                if isinstance(message_data, dict):
                    url = message_data.get('url', '')
                    metadata = message_data
                    if not url and 'message' in message_data:
                        url = message_data['message']
                elif isinstance(message_data, str):
                    url = message_data
                    metadata = {'url': url}
                print(f"[VOID_LINK_CLICKED] url: {url}, metadata: {metadata}")
                if url and url != "about:blank":
                    print(f"[VOID_LINK_CLICKED] Calling _process_clicked_url with url: {url}")
                    GLib.idle_add(self._process_clicked_url, str(url), metadata)
                    return
            else:
                message_data = js_message
                print(f"[VOID_LINK_CLICKED] js_message (no get_js_value): {message_data}")
            url = None
            metadata = {}
            if isinstance(message_data, dict):
                url = message_data.get('url', '')
                metadata = message_data
                if not url and 'message' in message_data:
                    url = message_data['message']
            elif isinstance(message_data, str):
                url = message_data
                metadata = {'url': url}
            elif hasattr(message_data, 'is_string') and message_data.is_string():
                url = message_data.to_string()
                metadata = {'url': url}
            print(f"[VOID_LINK_CLICKED] url: {url}, metadata: {metadata}")
            if url and url != "about:blank":
                print(f"[VOID_LINK_CLICKED] Calling _process_clicked_url with url: {url}")
                GLib.idle_add(self._process_clicked_url, str(url), metadata)
                return
            if not url or url == "about:blank":
                print("[VOID_LINK_CLICKED] No valid URL, opening about:blank as fallback.")
                GLib.idle_add(self._process_clicked_url, "about:blank", metadata)
                return
        except Exception as e:
            print(f"Error in on_void_link_clicked: {e}")
            import traceback
            traceback.print_exc()
        try:
            print(f"[PROCESS_CLICKED_URL] url: {url}, metadata: {metadata}")
            if url is not None:
                if url.startswith('/'):
                    current_uri = self.webview.get_uri()
                    print(f"[PROCESS_CLICKED_URL] current_uri: {current_uri}")
                    if current_uri:
                        from urllib.parse import urljoin
                        abs_url = urljoin(current_uri, url)
                        print(f"[PROCESS_CLICKED_URL] abs_url: {abs_url}")
                        url = abs_url
                if url.startswith('javascript:'):
                    print("[PROCESS_CLICKED_URL] Ignoring javascript: url")
                    return
                print(f"[PROCESS_CLICKED_URL] Opening in new tab: {url}")
                self.open_url_in_new_tab(url)
            else:
                print("[PROCESS_CLICKED_URL] url is None, skipping.")
        except Exception as e:
            print(f"[PROCESS_CLICKED_URL] Exception: {e}")
            import traceback
            traceback.print_exc()

    def check_tor_connection(self, timeout=30, max_retries=2):
        """Check if Tor connection is working by making a request to check.torproject.org.
        Args:
            timeout: Timeout in seconds for the connection attempt
            max_retries: Maximum number of retry attempts
        Returns:
            tuple: (bool: connection status, str: status message)
        """
        if not self.is_tor_running():
            return False, "Tor service is not running"
        session = requests.Session()
        session.proxies = {
            'http': 'socks5h://127.0.0.1:9050',
            'https': 'socks5h://127.0.0.1:9050'
        }
        endpoints = [
            'https://check.torproject.org/api/ip',
            'https://check.torproject.org/',
            'http://wtfismyip.com/json'
        ]
        for attempt in range(max_retries):
            for url in endpoints:
                try:
                    response = session.get(url, timeout=timeout)
                    response.raise_for_status()
                    if 'check.torproject.org' in url:
                        if url.endswith('api/ip'):
                            data = response.json()
                            is_tor = data.get('IsTor', False)
                        else:
                            is_tor = 'Congratulations. This browser is configured to use Tor.' in response.text
                    else:
                        is_tor = True
                    if is_tor:
                        logging.info("Successfully verified Tor connection")
                        return True, "Tor connection is active"
                except requests.exceptions.RequestException as e:
                    logging.warning(f"Tor check failed for {url} (attempt {attempt + 1}): {e}")
                    continue
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logging.info(f"Retrying Tor connection in {wait_time} seconds...")
                time.sleep(wait_time)
        return False, "Could not establish Tor connection"

    def toggle_tor(self, enable=None, show_status=True):
        """Toggle Tor connection on or off.
        Args:
            enable: Boolean to force enable/disable, or None to toggle
            show_status: Whether to show status messages to the user
        Returns:
            bool: New Tor enabled state
        """
        if enable is None:
            enable = not self.tor_enabled
        try:
            if enable:
                if not self.is_tor_running():
                    if show_status:
                        self.show_status_message("Starting Tor service...")
                    if not self.start_tor():
                        if show_status:
                            self.show_error_message("Failed to start Tor service")
                        return False
                if show_status:
                    self.show_status_message("Verifying Tor connection...")

                def check_connection():
                    success, message = self.check_tor_connection()
                    GLib.idle_add(lambda: self._handle_tor_connection_result(success, message, show_status))
                import threading
                threading.Thread(target=check_connection, daemon=True).start()
                return True
            else:
                self.tor_enabled = False
                self.update_tor_status_indicator()
                if show_status:
                    self.show_status_message("Tor disabled")
                logging.info("Tor disabled")
                return True
        except Exception as e:
            error_msg = f"Failed to toggle Tor: {str(e)}"
            logging.error(error_msg)
            if show_status:
                self.show_error_message(error_msg)
            return False

    def _handle_tor_connection_result(self, success, message, show_status=True):
        """Handle the result of a Tor connection check.

        Args:
            success: Whether the connection was successful
            message: Status message to display
            show_status: Whether to show the status to the user
        """
        self.tor_enabled = success
        self.update_tor_status_indicator()

        if show_status:
            if success:
                self.show_status_message("Tor connection is active")
            else:
                self.show_error_message(f"Tor connection failed: {message}")

        logging.info(f"Tor connection check: {message}")

    def update_tor_status_indicator(self):
        """Update the Tor status indicator based on current state."""
        if not hasattr(self, 'tor_status_indicator'):
            return

        if self.tor_enabled:
            self.tor_status_indicator.set_from_icon_name("network-vpn-symbolic")
            self.tor_status_indicator.set_tooltip_text("Tor is enabled")
            self.tor_status_indicator.add_css_class("tor-enabled")
            self.tor_status_indicator.remove_css_class("tor-disabled")
        else:
            self.tor_status_indicator.set_from_icon_name("network-offline-symbolic")
            self.tor_status_indicator.set_tooltip_text("Tor is disabled")
            self.tor_status_indicator.add_css_class("tor-disabled")
            self.tor_status_indicator.remove_css_class("tor-enabled")

    def update_webview_tor_proxy(self, webview):
        """
        Update the Tor proxy configuration for an existing webview.
        Args:
            webview: The WebKit2.WebView to update
        """
        if not webview:
            return
        web_context = webview.get_context()
        if self.tor_enabled and self.tor_manager and self.tor_manager.is_running():
            if not self.tor_manager.setup_proxy(web_context):
                pass
        else:
            self.clear_webview_proxy(web_context)

    def clear_webview_proxy(self, web_context):
        """
        Clear proxy configuration from a web context.
        Args:
            web_context: The WebKit2.WebContext to clear proxy from
        """
        try:
            if web_context:
                # Reset to no proxy
                proxy_settings = WebKit2.ProxySettings.new()
                web_context.set_proxy_settings(proxy_settings)
        except Exception:
            pass

    def update_tor_button(self):
        """Update the Tor button appearance based on current Tor status."""
        if not hasattr(self, 'tor_button') or not self.tor_button:
            return
        is_tor_running = (self.tor_enabled and
                        hasattr(self, 'tor_manager') and
                        self.tor_manager and
                        self.tor_manager.is_running())
        if is_tor_running:
            self.tor_button.set_label("🔗 Tor ON")
            self.tor_button.set_tooltip_text("Tor is enabled - Click to disable")
            css_provider = Gtk.CssProvider()
            css_provider.load_from_data(b"""
                .tor-button {
                    background: #ADD8E6;
                    color: black;
                    border: 1px solid #45a049;
                }
            """)
            display = self.tor_button.get_display() or Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        else:
            self.tor_button.set_label("⚪ Tor OFF")
            self.tor_button.set_tooltip_text("Tor is disabled - Click to enable")
            css_provider = Gtk.CssProvider()
            css_provider.load_from_data(b"""
                .tor-button {
                    background: #ADD8E6;
                    color: white;
                    border: 1px solid #616161;
                }
            """)
            display = self.tor_button.get_display() or Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def on_tor_button_clicked(self, button):
        """Handle Tor button click - toggle Tor connection."""
        current_state = (self.tor_enabled and
                        hasattr(self, 'tor_manager') and
                        self.tor_manager and
                        self.tor_manager.is_running())
        new_state = not current_state
        if self.toggle_tor(new_state):
            with self.tabs_lock:
                for tab in self.tabs:
                    if hasattr(tab, 'webview'):
                        self.update_webview_tor_proxy(tab.webview)
                        GLib.idle_add(tab.webview.reload)
            self.update_tor_button()
            if new_state:
                self.show_error_message("Tor enabled - All traffic now routed through Tor")
            else:
                self.show_error_message("Tor disabled - Using direct connection")
        else:
            self.update_tor_button()

    def on_anti_fingerprinting_toggled(self, toggle_button):
        self.anti_fingerprinting_enabled = toggle_button.get_active()
        with self.tabs_lock:
            for tab in self.tabs:
                GLib.idle_add(tab.webview.reload)

    def create_clear_data_dialog(self):
        dialog = Gtk.Dialog(
            title="Clear Browsing Data",
            transient_for=self.window,
            modal=True,
            destroy_with_parent=True
        )
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main_box.set_margin_top(12)
        main_box.set_margin_bottom(12)
        main_box.set_property("margin-left", 12)
        main_box.set_property("margin-right", 12)
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.pack_start(Gtk.Label(label="Select the types of data to clear:"), False, False, 0)
        self.cookies_check = Gtk.CheckButton(label="Cookies and other site data")
        self.cookies_check.set_active(True)
        content_box.pack_start(self.cookies_check, False, False, 0)
        self.cache_check = Gtk.CheckButton(label="Cached images and files")
        self.cache_check.set_active(True)
        content_box.pack_start(self.cache_check, False, False, 0)
        self.passwords_check = Gtk.CheckButton(label="Saved passwords")
        content_box.pack_start(self.passwords_check, False, False, 0)
        self.history_check = Gtk.CheckButton(label="Browsing history")
        content_box.pack_start(self.history_check, False, False, 0)
        main_box.pack_start(content_box, True, True, 0)
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_property("halign", Gtk.Align.END)
        cancel_button = Gtk.Button(label="_Cancel", use_underline=True)
        cancel_button.connect("clicked", lambda btn: dialog.close())
        button_box.pack_start(cancel_button, False, False, 0)
        clear_button = Gtk.Button(label="_Clear Data", use_underline=True)
        clear_button.connect("clicked", lambda btn: self.on_clear_data_confirm(dialog))
        button_box.pack_start(clear_button, False, False, 0)
        main_box.pack_start(button_box, False, False, 0)
        dialog.get_content_area().add(main_box)
        return dialog

    def on_clear_data_confirm(self, dialog):
        if self.cookies_check.get_active():
            self.clear_cookies()
        if self.cache_check.get_active():
            self.clear_cache()
        if self.passwords_check.get_active():
            self.clear_passwords()
        if self.history_check.get_active():
            self.clear_history()
        dialog.close()

    def on_clear_data_response(self, dialog, response_id):
        if response_id == Gtk.ResponseType.ACCEPT:
            if self.cookies_check.get_active():
                self.clear_cookies()
            if self.cache_check.get_active():
                self.clear_cache()
            if self.passwords_check.get_active():
                self.clear_passwords()
            if self.history_check.get_active():
                self.clear_history()
            self.show_message("Data Cleared", "The selected browsing data has been cleared.")
        dialog.destroy()

    def clear_cookies(self):
        try:
            context = WebKit2.WebContext.get_default()
            cookie_manager = context.get_cookie_manager()
            if cookie_manager:
                cookie_manager.delete_all_cookies()
        except AttributeError:
            try:
                cookie_manager = WebKit2.CookieManager.get_default()
                if cookie_manager:
                    cookie_manager.delete_all_cookies()
            except AttributeError:
                pass
        except Exception:
            pass

    def clear_cache(self):
        try:
            context = WebKit2.WebContext.get_default()
            if context:
                if hasattr(context, 'clear_cache'):
                    context.clear_cache()
                elif hasattr(context, 'clear_cache_storage'):
                    context.clear_cache_storage()
        except Exception:
            pass

    def clear_passwords(self):
        try:
            context = WebKit2.WebContext.get_default()
            if context and hasattr(context, 'clear_credentials'):
                context.clear_credentials()
        except Exception:
            pass

    def clear_history(self):
        if hasattr(self, 'history'):
            self.history.clear()
            try:
                self.save_json(HISTORY_FILE, [])
                dialog = Gtk.MessageDialog(
                    transient_for=self.window,
                    message_type=Gtk.MessageType.INFO,
                    buttons=Gtk.ButtonsType.OK,
                    text="Browsing history has been cleared"
                )
                dialog.connect("response", lambda d, r: d.destroy())
                dialog.present()
            except Exception:
                pass

    def on_downloads_clicked(self, button):
        downloads_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if not downloads_dir:
            downloads_dir = os.path.expanduser("~/Downloads")
        try:
            import subprocess
            subprocess.Popen(["xdg-open", downloads_dir])
        except Exception:
            pass

    def is_valid_url(self, url):
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except Exception:
            return False

    def load_url(self, url):
        """Load a URL in the current active webview."""
        try:
            if not url.startswith(("http://", "https://")):
                if url.startswith("www."):
                    url = "https://" + url
                else:
                    url = f"https://duckduckgo.com/?q={requests.utils.quote(url)}"
            webview = self.get_current_webview()
            if webview:
                webview.load_uri(url)
                self.url_entry.set_text(url)
                self.update_history(url)
        except Exception:
            pass

    def on_add_bookmark_clicked(self, button):
        """Handle Add Bookmark button click."""
        current_webview = self.get_current_webview()
        if current_webview:
            url = current_webview.get_uri()
            if url:
                self.add_bookmark(url)

    def add_bookmark(self, url):
        """Add URL to bookmarks."""
        if not url or not url.startswith(("http://", "https://")):
            return
        if url not in self.bookmarks:
            self.bookmarks.append(url)
            self.save_json(BOOKMARKS_FILE, self.bookmarks)
            self.update_bookmarks_menu(self.bookmark_menu)
            return True
        return False

    def update_history(self, url):
        """Add URL to browser history."""
        if url and url.startswith(("http://", "https://")):
            self.history.append({"url": url, "timestamp": time.time()})
            self.history = self.history[-HISTORY_LIMIT:]
            self.save_json(HISTORY_FILE, self.history)

    def load_json(self, filename):
        """Load JSON data from file."""
        try:
            if os.path.exists(filename):
                with open(filename, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def save_json(self, filename, data):
        """Save JSON data to file."""
        try:
            with open(filename, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def show_error_message(self, message):
        """Display an error message dialog."""
        logging.basicConfig(
            level=logging.DEBUG if self.debug_mode else logging.ERROR,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            filename='shadow_browser.log',
            filemode='w'
        )
        logging.error(message)
        try:
            dialog = Gtk.MessageDialog(
                transient_for=self.window,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=message
            )
            dialog.connect("response", lambda d, r: d.destroy())
            dialog.present()
        except Exception:
            pass

    def show_status_message(self, message):
        """Display a status message (could be in status bar or notification)."""
        logging.info(message)
        try:
            if hasattr(self, 'status_bar') and self.status_bar:
                self.status_bar.push(0, message)
            else:
                # Fallback to logging if no status bar available
                print(f"Status: {message}")
        except Exception:
            pass

    def show_message(self, title, message):
        """Display an info message dialog."""
        logging.info(f"{title}: {message}")
        try:
            dialog = Gtk.MessageDialog(
                transient_for=self.window,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=title
            )
            dialog.format_secondary_text(message)
            dialog.connect("response", lambda d, r: d.destroy())
            dialog.present()
        except Exception:
            pass

    def on_about(self, button):
        """Show the about dialog."""
        about = Gtk.AboutDialog(transient_for=self.window)
        about.set_program_name("Shadow Browser")
        about.set_version("1.0")
        about.set_copyright(" 2025 ShadowyFigure")
        about.set_comments("A privacy-focused web browser")
        about.set_website("https://github.com/shadowyfigure/shadow-browser-")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        image_path = os.path.join(script_dir, "background.png")
        try:
            if os.path.exists(image_path):
                texture = Gdk.Texture.new_from_filename(image_path)
                about.set_logo(texture)
            else:
                about.set_logo_icon_name("web-browser")
        except Exception:
            pass
        about.present()

    def on_back_clicked(self, button):
        """Handle back button click."""
        webview = self.get_current_webview()
        if webview and webview.can_go_back():
            webview.go_back()

    def on_new_tab_clicked(self, button):
        """Handle New Tab button click."""
        self.add_new_tab(self.home_url)

    def add_new_tab(self, url):
        """Add a new tab with a webview loading the specified URL."""
        try:
            webview = self.create_secure_webview()
            if webview is None:
                return
            webview.load_uri(url)
            webview.show()
            scrolled_window = Gtk.ScrolledWindow()
            scrolled_window.set_vexpand(True)
            scrolled_window.add(webview)
            scrolled_window.show_all()
            label = Gtk.Label(label=self.extract_tab_title(url))
            close_button = Gtk.Button.new_from_icon_name("window-close")
            close_button.set_size_request(24, 24)
            close_button.set_tooltip_text("Close tab")
            tab = Tab(url, webview)
            tab.label_widget = label
            tab.close_button = close_button

            def on_close_clicked(button, tab=tab):
                try:
                    if tab in self.tabs:
                        tab_index = self.tabs.index(tab)
                        self.on_tab_close_clicked(button, tab_index)
                except ValueError:
                    pass
                except Exception:
                    pass
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            self.safe_append(box, label)
            self.safe_append(box, close_button)
            if not self.notebook:
                return
            index = self.notebook.append_page(scrolled_window, box)
            self.notebook.set_current_page(index)
            self.tabs.append(tab)
            box.show_all()
            label.show()
            close_button.show()
            try:
                close_button.connect("clicked", on_close_clicked)
                webview.connect("load-changed", self.on_load_changed)
                webview.connect("notify::title", self.on_title_changed)
                webview.connect("decide-policy", self.on_decide_policy)
            except Exception:
                pass
        except Exception:
            pass

    def on_tab_close_clicked(self, button, tab_index):
        """Close the tab at the given index."""
        try:
            if 0 <= tab_index < len(self.tabs):
                tab = self.tabs[tab_index]
                webview = tab.webview
                notebook_page_num = None
                for page_index in range(self.notebook.get_n_pages()):
                    page = self.notebook.get_nth_page(page_index)
                    if isinstance(page, Gtk.ScrolledWindow):
                        child = page.get_child()
                        if child == webview or (
                            isinstance(child, Gtk.Viewport) and child.get_child() == webview
                        ):
                            notebook_page_num = page_index
                            break
                if notebook_page_num is not None:
                    if notebook_page_num < self.notebook.get_n_pages():
                        self.notebook.remove_page(notebook_page_num)
                removed_tab = self.tabs.pop(tab_index)
                try:
                    if hasattr(removed_tab, 'webview'):
                        removed_tab.webview = None
                    if hasattr(removed_tab, 'label_widget'):
                        removed_tab.label_widget = None
                except Exception:
                    pass
        except Exception:
            pass

    def on_load_changed(self, webview, load_event):
        """Handle load state changes."""
        from gi.repository import WebKit2, GLib
        try:
            if not hasattr(self, 'download_spinner') or not self.download_spinner:
                return
            if load_event == WebKit2.LoadEvent.COMMITTED:
                current_webview = self.get_current_webview()
                if webview == current_webview:
                    if hasattr(self, 'url_entry') and self.url_entry:
                        current_url = webview.get_uri() or ""
                        self.url_entry.set_text(current_url)
                        for tab in self.tabs:
                            if tab.webview == webview:
                                tab.url = current_url
                                if tab.label_widget and not webview.get_title():
                                    tab.label_widget.set_text(self.extract_tab_title(current_url))
                                break
                GLib.idle_add(self.download_spinner.start)
                GLib.idle_add(lambda: self.download_spinner.set_visible(True))
            elif load_event == WebKit2.LoadEvent.FINISHED:
                current_url = webview.get_uri() or ""
                if hasattr(self, 'url_entry') and self.url_entry and webview == self.get_current_webview():
                    self.url_entry.set_text(current_url)
                for tab in self.tabs:
                    if tab.webview == webview:
                        tab.url = current_url
                        if tab.label_widget and not webview.get_title():
                            tab.label_widget.set_text(self.extract_tab_title(current_url))
                        break
                GLib.idle_add(self.download_spinner.stop)
                GLib.idle_add(lambda: self.download_spinner.set_visible(False))
                if current_url and not current_url.startswith(('about:', 'data:')):
                    self.update_history(current_url)
        except Exception:
            pass

    def on_title_changed(self, webview, param):
        """Update tab label and favicon when page title changes."""
        title = webview.get_title() or "Untitled"
        url = webview.get_uri()
        if not url:
            return
        max_length = getattr(self, "tab_title_max_length", 10)
        display_title = title[:max_length - 3] + "..." if len(title) > max_length else title
        tab = next((t for t in self.tabs if t.webview == webview), None)
        if not tab:
            return
        if hasattr(tab.label_widget, "set_text"):
            tab.label_widget.set_text(display_title)
        if hasattr(tab, "_favicon_thread") and tab._favicon_thread.is_alive():
            return

        def update_favicon():
            try:
                def on_favicon_loaded(texture):
                    if texture:
                        GLib.idle_add(self._update_tab_favicon, tab, texture)
                        self._update_bookmark_favicon(url, texture)
                favicon_uri = webview.get_favicon()
                if not favicon_uri and url:
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    favicon_uri = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
                if favicon_uri:
                    self._load_favicon_async(favicon_uri, on_favicon_loaded)
            except Exception as e:
                if self.debug_mode:
                    print(f"Error loading favicon: {e}")
        tab._favicon_thread = threading.Thread(target=update_favicon, daemon=True)
        tab._favicon_thread.start()

    def _on_favicon_changed(self, webview, favicon, *args):
        """Handle favicon changes for the current tab."""
        if not favicon:
            return
        url = webview.get_uri()
        if not url:
            return
        with self.favicon_lock:
            self.favicon_cache[url] = favicon
        current_tab = next((t for t in getattr(self, 'tabs', []) if t.webview == webview), None)
        if current_tab:
            self._update_tab_favicon(current_tab, favicon)
        if hasattr(self, 'bookmarks'):
            self._update_bookmark_favicon(url, favicon)

    def _update_tab_favicon(self, tab, texture):
        """Update the favicon in the tab header.
        Args:
            tab: The tab to update
            texture: A Gdk.Texture containing the favicon
        """
        if not texture:
            return

        def update_ui():
            if hasattr(tab, "favicon_widget") and tab.favicon_widget:
                tab.favicon_widget.set_from_paintable(texture)
                tab.favicon_widget.set_visible(True)
            else:
                if getattr(tab, "label_box", None):
                    image = Gtk.Image()
                    image.set_size_request(16, 16)
                    image.set_from_paintable(texture)
                    tab.label_box.prepend(image)
                    image.set_visible(True)
                    tab.favicon_widget = image
            tab.favicon = texture
        GLib.idle_add(update_ui)

    def _update_bookmark_favicon(self, url, favicon):
        """Update the favicon for a bookmarked URL.
        Args:
            url (str): The URL of the bookmark to update.
            favicon (Gdk.Texture | Gdk.Paintable | GdkPixbuf.Pixbuf): The favicon image.
        """
        import base64
        from gi.repository import GdkPixbuf, GLib
        if not hasattr(self, 'bookmarks') or not self.bookmarks or not url:
            return
        if self.debug_mode:
            print(f"[DEBUG] Updating favicon for bookmark: {url}")
        favicon_data = None
        try:
            if hasattr(favicon, 'save_to_png_bytes'):
                try:
                    png_data = favicon.save_to_png_bytes()
                    if png_data:
                        favicon_data = base64.b64encode(png_data.get_data()).decode('utf-8')
                except Exception as e:
                    if self.debug_mode:
                        print(f"[DEBUG] Error saving favicon to PNG: {e}")
            elif hasattr(favicon, 'save_to_stream'):
                bytes_stream = GLib.Bytes.new()
                success = favicon.save_to_stream(bytes_stream, 'png')
                if success:
                    favicon_data = base64.b64encode(bytes_stream.get_data()).decode('utf-8')
            elif isinstance(favicon, GdkPixbuf.Pixbuf):
                buffer = favicon.save_to_bufferv('png', [], [])
                favicon_data = base64.b64encode(buffer).decode('utf-8')
            else:
                if self.debug_mode:
                    print(f"[DEBUG] Unsupported favicon type: {type(favicon)}")
                    return
        except Exception as e:
            if self.debug_mode:
                print(f"[ERROR] Failed to convert favicon for {url}: {e}")
            return
        if not favicon_data:
            if self.debug_mode:
                print("[WARN] Could not encode favicon to base64.")
            return
        updated = False
        for i, bookmark in enumerate(self.bookmarks):
            if isinstance(bookmark, dict) and bookmark.get('url') == url:
                self.bookmarks[i]['favicon'] = favicon_data
                updated = True
                break
            elif isinstance(bookmark, str) and bookmark == url:
                self.bookmarks[i] = {
                    'url': url,
                    'title': url,
                    'favicon': favicon_data
                }
                updated = True
                break
        if updated:
            if self.debug_mode:
                print(f"[INFO] Bookmark favicon updated for: {url}")
            try:
                self.save_json(BOOKMARKS_FILE, self.bookmarks)
            except Exception as e:
                print(f"[ERROR] Could not save bookmarks: {e}")
            if hasattr(self, 'bookmark_menu') and self.bookmark_menu:
                try:
                    self.update_bookmarks_menu(self.bookmark_menu)
                except Exception as e:
                    if self.debug_mode:
                        print(f"[WARN] Failed to update bookmarks menu: {e}")
        else:
            if self.debug_mode:
                print(f"[DEBUG] No bookmark entry found for {url}.")

    def on_webview_create(self, webview, navigation_action, window_features=None):
        """Handle creation of new webviews."""
        try:
            if window_features is None:
                return None
            new_webview = WebKit2.WebView(
                settings=webview.get_settings(),
                user_content_manager=webview.get_user_content_manager()
            )
            new_webview.set_hexpand(True)
            new_webview.set_vexpand(True)
            if not hasattr(new_webview, '_signals_connected'):
                new_webview.connect("create", self.on_webview_create)
                new_webview.connect("decide-policy", self.on_decide_policy)
                new_webview._signals_connected = True
            is_popup = False
            try:
                if (
                    window_features is not None
                    and hasattr(window_features, "get")
                    and callable(window_features.get)
                ):
                    try:
                        is_popup = window_features.get("popup", False)
                    except Exception:
                        pass
            except Exception:
                pass
            if is_popup:
                self.open_popup_window(new_webview, window_features)
            else:
                self.add_webview_to_tab(new_webview)
            return new_webview
        except Exception:
            return None
    BLOCKED_INTERNAL_URLS = [
        "about:blank",
        "about:srcdoc",
        "blob:",
        "data:",
        "about:debug",
    ]
    allow_about_blank = False

    def is_internal_url_blocked(self, url, is_main_frame):
        """
        Determine if an internal URL should be blocked.
        Args:
            url (str): The URL to check.
            is_main_frame (bool): Whether the request is for the main frame.
        Returns:
            bool: True if the URL should be blocked, False otherwise.
        """
        if not url:
            return False
        if url.startswith("about:blank") and not self.allow_about_blank:
            return True
        if url in self.BLOCKED_INTERNAL_URLS:
            return True
        if not is_main_frame and url.startswith(("about:", "data:", "blob:", "_blank", "_data:")):
            return True
        return False

    def _handle_navigation_action(self, webview, decision, navigation_action):
        """Handle navigation action policy decision."""
        try:
            if not navigation_action:
                decision.ignore()
                return True
            request = navigation_action.get_request()
            if not request:
                decision.ignore()
                return True
            requested_url = request.get_uri()
            if not requested_url:
                decision.ignore()
                return True
            is_main_frame = True
            if hasattr(navigation_action, "get_frame"):
                frame = navigation_action.get_frame()
                if hasattr(frame, "is_main_frame"):
                    try:
                        is_main_frame = frame.is_main_frame()
                    except Exception:
                        pass
            if self.is_internal_url_blocked(requested_url, is_main_frame):
                decision.ignore()
                return True
            if requested_url.startswith(("about:", "data:", "blob:", "_data:", "_blank", "_parent", "_self", "_top", "_window")):
                if not is_main_frame:
                    decision.ignore()
                    return True
                decision.use()
                return True
            parsed = urlparse(requested_url)
            if parsed.scheme not in ("http", "https"):
                decision.ignore()
                return True
            if not is_main_frame:
                top_level_url = webview.get_uri()
                if top_level_url:
                    top_host = urlparse(top_level_url).hostname
                    req_host = parsed.hostname
                    if top_host and req_host and top_host != req_host:
                        decision.ignore()
                        return True
            if self.adblocker.is_blocked(requested_url):
                decision.ignore()
                return True
            if requested_url.lower().endswith(tuple(DOWNLOAD_EXTENSIONS)):
                self.start_manual_download(requested_url)
                decision.ignore()
                return True
            cleanup_js = """
            document.querySelectorAll('a').forEach(a => {
                if (
                    (!a.textContent.trim() && !a.innerHTML.trim()) ||
                    getComputedStyle(a).opacity === '0' ||
                    getComputedStyle(a).visibility === 'hidden'
                ) {
                    a.remove();
                }
            });
            """
            try:
                webview.evaluate_javascript(
                    cleanup_js,
                    -1,
                    None,
                    None,
                    None,
                    None
                )
            except Exception:
                pass
                try:
                    script = WebKit2.UserScript.new(
                        cleanup_js,
                        WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                        WebKit2.UserScriptInjectionTime.END,
                        None,
                        None
                    )
                    webview.get_user_content_manager().add_script(script)
                except Exception:
                    pass
            decision.use()
            return True
        except Exception:
            pass
            decision.ignore()
            return True

    def _handle_new_window_action(self, webview, decision):
        """Handle new window action policy decision."""
        try:
            navigation_action = decision.get_navigation_action()
            if navigation_action is None:
                decision.ignore()
                return True
            request = navigation_action.get_request()
            if request is None:
                decision.ignore()
                return True
            url = request.get_uri()
            if url is None:
                decision.ignore()
                return True
            if url.lower() == "about:blank":
                decision.ignore()
                return True
            if url.lower() == "javascript:void(0)":
                decision.ignore()
                return True
            if url.lower().endswith(tuple(DOWNLOAD_EXTENSIONS)):
                self.start_manual_download(url)
                decision.ignore()
                return True
            user_content_manager = webview.get_user_content_manager()
            new_webview = WebKit2.WebView(user_content_manager=user_content_manager)
            self.setup_webview_settings(new_webview)
            self.download_manager.add_webview(new_webview)
            if not hasattr(new_webview, "_create_signal_connected"):
                new_webview.connect("create", self.on_webview_create)
                new_webview._create_signal_connected = True
            if not hasattr(new_webview, "_decide_policy_connected"):
                new_webview.connect("decide-policy", self.on_decide_policy)
                new_webview._decide_policy_connected = True
            self.add_webview_to_tab(new_webview)
            new_webview.load_uri(url)
            decision.ignore()
            return True
        except Exception:
            pass
            decision.ignore()
            return True

    def on_decide_policy(self, webview, decision, decision_type):
        """Handle navigation and new window actions, manage downloads, enforce policies, and apply adblock rules."""
        try:
            from gi.repository import WebKit2
            if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
                return self._handle_navigation_action(
                    webview, decision, decision.get_navigation_action()
                )
            elif decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                return self._handle_new_window_action(webview, decision)
            else:
                decision.use()
                return True
        except Exception:
            pass
            decision.ignore()
            return True

    def add_download_spinner(self, toolbar):
        """Add download spinner to toolbar."""
        if toolbar:
            toolbar.pack_start(self.download_spinner, False, False, 0)
            self.download_spinner.set_property("halign", Gtk.Align.END)
            self.download_spinner.set_property("valign", Gtk.Align.END)
            self.download_spinner.set_property("margin-left", 10)
            self.download_spinner.set_property("margin-right", 10)
            self.download_spinner.set_visible(True)

    def start_manual_download(self, url):
        """Manually download a file from the given URL."""
        import requests
        from urllib.parse import urlparse, unquote, parse_qs

        def sanitize_filename(filename):
            """Sanitize and clean up the filename."""
            filename = re.sub(r'[?#].*$', '', filename)
            filename = re.sub(r'[?&][^/]+$', '', filename)
            filename = re.sub(r'[^\w\-_. ]', '_', filename).strip()
            return filename or 'download'

        def get_filename_from_url(parsed_url):
            """Extract and clean filename from URL path."""
            path = unquote(parsed_url.path)
            filename = os.path.basename(path)
            if not filename and parsed_url.path.endswith('/'):
                filename = parsed_url.netloc.split('.')[-2] if '.' in parsed_url.netloc else 'file'
            if 'download' in parse_qs(parsed_url.query):
                dl_param = parse_qs(parsed_url.query)['download'][0]
                if dl_param:
                    filename = unquote(dl_param)
            return sanitize_filename(filename)

        def get_extension_from_content_type(content_type):
            """Get appropriate file extension from content type."""
            content_type = (content_type or '').split(';')[0].lower()
            ext_map = {
                'video/mp4': '.mp4',
                'video/webm': '.webm',
                'video/quicktime': '.mov',
                'video/x-msvideo': '.avi',
                'video/x-matroska': '.mkv',
                'video/3gpp': '.3gp',
                'video/mpeg': '.mpeg',
                'video/ogg': '.ogv',
                'video/x-flv': '.flv',
                'application/x-mpegURL': '.m3u8',
                'application/dash+xml': '.mpd',
                'application/octet-stream': '.bin',
                'application/zip': '.zip',
                'application/x-rar-compressed': '.rar',
                'application/x-7z-compressed': '.7z',
                'application/x-tar': '.tar',
                'application/gzip': '.gz',
                'application/x-bzip2': '.bz2',
                'application/pdf': '.pdf',
                'application/msword': '.doc',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
                'application/vnd.ms-excel': '.xls',
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
                'application/vnd.ms-powerpoint': '.ppt',
                'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
                'text/plain': '.txt',
                'text/html': '.html',
                'text/css': '.css',
                'text/csv': '.csv',
                'application/json': '.json',
                'application/javascript': '.js',
                'image/jpeg': '.jpg',
                'image/png': '.png',
                'image/gif': '.gif',
                'image/webp': '.webp',
                'image/svg+xml': '.svg',
                'audio/mpeg': '.mp3',
                'audio/wav': '.wav',
                'audio/ogg': '.ogg',
                'audio/webm': '.weba',
            }
            return ext_map.get(content_type, '')

        def download_thread():
            try:
                parsed_url = urlparse(url)
                if not parsed_url.scheme or not parsed_url.netloc:
                    GLib.idle_add(
                        lambda: self.show_error_message("Invalid URL format"),
                        priority=GLib.PRIORITY_DEFAULT,
                    )
                    return
                headers = {
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit2/537.36 (KHTML like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
                with requests.get(url, stream=True, timeout=30, headers=headers) as response:
                    response.raise_for_status()
                    content_disposition = response.headers.get("content-disposition", "")
                    filename = None
                    if content_disposition:
                        filename_match = re.search(
                            r'filename[^;=]*=([^;\n]*)',
                            content_disposition,
                            flags=re.IGNORECASE
                        )
                        if filename_match:
                            filename = filename_match.group(1).strip('\'" ')
                            filename = unquote(filename)
                            filename = sanitize_filename(filename)
                    if not filename:
                        filename = get_filename_from_url(parsed_url)
                    base_name, ext = os.path.splitext(filename)
                    if not ext:
                        content_type = response.headers.get('content-type', '')
                        ext = get_extension_from_content_type(content_type)
                        if ext:
                            filename = f"{base_name}{ext}"
                    downloads_dir = GLib.get_user_special_dir(
                        GLib.UserDirectory.DIRECTORY_DOWNLOAD
                    ) or os.path.expanduser("~/Downloads")
                    os.makedirs(downloads_dir, exist_ok=True)
                    base_name, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(os.path.join(downloads_dir, filename)):
                        filename = f"{base_name}_{counter}{ext}"
                        counter += 1
                    filepath = os.path.join(downloads_dir, filename)
                    total_size = int(response.headers.get("content-length", 0))
                    block_size = 8192
                    downloaded = 0
                    progress_info = {
                        "filename": filename,
                        "total_size": total_size,
                        "downloaded": downloaded,
                        "cancelled": False,
                        "thread_id": threading.current_thread().ident,
                    }
                    self.download_manager.add_progress_bar(progress_info)
                    try:
                        with open(filepath, "wb") as f:
                            for chunk in response.iter_content(block_size):
                                if progress_info["cancelled"]:
                                    break
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    progress = (
                                        downloaded / total_size if total_size > 0 else 0
                                    )
                                    GLib.idle_add(
                                        self.download_manager.update_progress,
                                        progress_info,
                                        progress,
                                    )
                        if not progress_info["cancelled"]:
                            GLib.idle_add(
                                self.download_manager.download_finished, progress_info
                            )
                    except Exception:
                        GLib.idle_add(
                            self.download_manager.download_failed,
                            progress_info,
                            "Error writing to file",
                        )
                    finally:
                        GLib.idle_add(
                            self.download_manager.cleanup_download,
                            progress_info["filename"],
                        )
            except requests.exceptions.RequestException:
                GLib.idle_add(
                    self.download_manager.download_failed,
                    None,
                    "Download request failed",
                )
            except Exception:
                GLib.idle_add(
                    self.download_manager.download_failed,
                    None,
                    "Unexpected download error",
                )
        thread = threading.Thread(
            target=download_thread, daemon=True, name=f"download_{url}"
        )
        thread.start()
        return thread.ident

    def on_forward_clicked(self, button):
        """Navigate forward in the current tab."""
        webview = self.get_current_webview()
        if webview and webview.can_go_forward():
            webview.go_forward()

    def on_go_clicked(self, button):
        """Load URL from URL entry."""
        url = self.url_entry.get_text().strip()
        if url:
            self.load_url(url)

    def on_refresh_clicked(self, button):
        """Reload the current webview."""
        webview = self.get_current_webview()
        if webview:
            webview.reload()

    def on_zoom_out_clicked(self, button):
        """Zoom out the current webview."""
        webview = self.get_current_webview()
        if webview:
            try:
                current_zoom = webview.get_zoom_level()
                new_zoom = max(0.5, current_zoom - 0.1)
                webview.set_zoom_level(new_zoom)
            except Exception:
                pass

    def on_zoom_in_clicked(self, button):
        """Zoom in the current webview."""
        webview = self.get_current_webview()
        if webview:
            try:
                current_zoom = webview.get_zoom_level()
                new_zoom = min(2.0, current_zoom + 0.1)
                webview.set_zoom_level(new_zoom)
            except Exception:
                pass

    def on_zoom_reset_clicked(self, button):
        """Reset zoom to default level."""
        webview = self.get_current_webview()
        if webview:
            try:
                webview.set_zoom_level(1.0)
            except Exception:
                pass

    def on_inspect_clicked(self, button):
        """Open web inspector for current page."""
        webview = self.get_current_webview()
        if webview:
            try:
                inspector = webview.get_inspector()
                if inspector:
                    inspector.show()
            except Exception:
                pass

    def _create_icon_button(self, icon_name, callback, tooltip_text):
        """Create a button with icon and tooltip."""
        button = Gtk.Button.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        button.set_tooltip_text(tooltip_text)
        button.connect("clicked", callback)
        return button

    def extract_tab_title(self, url):
        """Extract a display title from a URL, limited to 30 characters."""
        max_length = 30
        try:
            parsed = urlparse(url)
            title = parsed.netloc or "New Tab"
            if len(title) > max_length:
                title = title[: max_length - 3] + "..."
            return title
        except Exception:
            return "New Tab"

    def save_session(self):
        """Save current browser session."""
        session_data = [
            {
                "url": tab.url,
                "title": tab.label_widget.get_text() if tab.label_widget else "",
            }
            for tab in self.tabs
        ]
        self.save_json(SESSION_FILE, session_data)

    def save_tabs(self):
        """Save current tabs info."""
        tabs_data = [tab.url for tab in self.tabs if tab.url]
        self.save_json(TABS_FILE, tabs_data)

    def restore_session(self):
        """Restore previous session."""
        if os.path.exists(SESSION_FILE):
            session_data = self.load_json(SESSION_FILE)
            if session_data and isinstance(session_data, list):
                for tab_data in session_data:
                    if isinstance(tab_data, dict) and "url" in tab_data:
                        self.add_new_tab(tab_data["url"])

    def apply_theme(self):
        """Apply the current theme setting."""
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-application-prefer-dark-theme", self.theme == "dark")

    def safe_window_cleanup(self, window):
        """Safely clean up window resources."""
        try:
            children = window.get_children() if hasattr(window, 'get_children') else []
            for child in children:
                try:
                    if hasattr(child, 'get_parent'):
                        parent = child.get_parent()
                        if parent is not None:
                            if hasattr(parent, 'remove'):
                                parent.remove(child)
                            elif hasattr(parent, 'destroy'):
                                parent.destroy()
                except Exception as e:
                    print(f"Error cleaning up child {child}: {e}", file=sys.stderr)
            if hasattr(window, 'destroy'):
                window.destroy()
        except Exception as e:
            print(f"Error during window cleanup: {e}", file=sys.stderr)

    def cleanup_widgets(self):
        """Clean up all widgets to prevent GTK warnings."""
        for tab in self.tabs[:]:
            if hasattr(tab, 'webview') and tab.webview:
                try:
                    tab.webview.disconnect_by_func(self.on_load_changed)
                    tab.webview.disconnect_by_func(self.on_title_changed)
                    tab.webview.disconnect_by_func(self.on_decide_policy)
                    tab.webview.disconnect_by_func(self.on_webview_create)
                except Exception:
                    pass
                tab.webview = None
            if hasattr(tab, 'label_widget'):
                tab.label_widget = None
        self.tabs.clear()
        if hasattr(self, 'notebook') and self.notebook:
            try:
                for i in range(self.notebook.get_n_pages() - 1, -1, -1):
                    page = self.notebook.get_nth_page(i)
                    if page:
                        try:
                            self.notebook.remove_page(i)
                        except Exception:
                            pass
            except Exception:
                pass

    def disconnect_all_signals(self):
        """Disconnect all signals to prevent GTK warnings."""
        for webview in [tab.webview for tab in self.tabs if hasattr(tab, 'webview')]:
            try:
                webview.disconnect_by_func(self.on_load_changed)
                webview.disconnect_by_func(self.on_title_changed)
                webview.disconnect_by_func(self.on_decide_policy)
                webview.disconnect_by_func(self.on_webview_create)
            except Exception:
                pass

    def on_window_destroy(self, window):
        """Handle window closure with proper cleanup."""
        try:
            self.save_session()
            self.save_tabs()
            self.cleanup_widgets()
            self.disconnect_all_signals()
            if hasattr(self, '_popup_windows'):
                try:
                    for popup in self._popup_windows:
                        try:
                            popup.destroy()
                        except Exception:
                            pass
                    self._popup_windows = []
                except Exception:
                    pass
            if hasattr(self, 'download_manager') and self.download_manager:
                try:
                    self.download_manager.clear_all()
                    self.download_manager = None
                except Exception:
                    pass
            self.safe_window_cleanup()
        except Exception:
            pass
        finally:
            self.quit()

    def simulate_left_click_on_void_link(self, data_url):
        js_code = (
            "(function() {"
            "let links = document.querySelectorAll('a[href=\"javascript:void(0)\"]');"
            f"let targetDataUrl = {json.dumps(data_url)};"
            "for (let link of links) {"
            "if (link.getAttribute('data-url') === targetDataUrl) {"
            "['mousedown', 'mouseup', 'click'].forEach(eventType => {"
            "let event = new MouseEvent(eventType, { view: window, bubbles: true, cancelable: true, button: 0 });"
            "link.dispatchEvent(event);"
            "});"
            "return true;"
            "}"
            "}"
            "return false;"
            "})();"
        )
        webview = self.get_current_webview()
        if webview:
            webview.evaluate_javascript(js_code, self.js_callback)

        def js_callback(self, webview, result):
            try:
                if result is None:
                    return
                webview.evaluate_javascript_finish(result)
            except Exception:
                pass

    def test_js_execution(self):
        webview = self.get_current_webview()
        if webview:
            js_code = "console.log('Test JS execution in webview'); 'JS executed';"
            webview.evaluate_javascript(js_code, self.js_callback)

    def open_url_in_new_tab(self, url):
        """Open a URL in a new tab."""
        try:
            if not url or not isinstance(url, str):
                return
            if url.startswith("javascript:") or url == "about:blank":
                return
            new_webview = self.create_secure_webview()
            if new_webview is None:
                return
            new_webview.load_uri(url)
            scrolled_window = Gtk.ScrolledWindow()
            scrolled_window.set_vexpand(True)
            scrolled_window.add(new_webview)
            label = Gtk.Label(label=self.extract_tab_title(url))
            close_button = Gtk.Button.new_from_icon_name("window-close")
            close_button.set_size_request(24, 24)
            close_button.set_tooltip_text("Close tab")
            tab = Tab(url, new_webview)
            tab.label_widget = label

            def on_close_clicked(button, tab=tab):
                try:
                    tab_index = self.tabs.index(tab)
                    self.on_tab_close_clicked(button, tab_index)
                except ValueError:
                    pass
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            self.safe_append(box, label)
            self.safe_append(box, close_button)
            index = self.notebook.append_page(scrolled_window, box)
            self.notebook.set_current_page(index)
            self.tabs.append(tab)
            box.show_all()
            label.show()
            close_button.show()
            close_button.connect("clicked", on_close_clicked)
            new_webview.connect("load-changed", self.on_load_changed)
            new_webview.connect("notify::title", self.on_title_changed)
            new_webview.connect("decide-policy", self.on_decide_policy)
            new_webview.connect("create", self.on_webview_create)
        except Exception:
            pass

    def open_popup_window(self, webview, window_features):
        """Open a popup window with the given webview."""
        try:
            window = Gtk.Window(title="Popup")
            window.set_transient_for(self.window)
            window.set_destroy_with_parent(True)
            window.set_modal(False)
            if window_features:
                default_width = int(window_features.get_width() or 800)
                default_height = int(window_features.get_height() or 600)
                window.set_default_size(default_width, default_height)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            if hasattr(webview, 'get_parent') and webview.get_parent() is not None:
                parent = webview.get_parent()
                if parent and hasattr(parent, "remove") and webview.get_parent() == parent:
                    try:
                        parent.remove(webview)
                    except Exception:
                        pass
            self.safe_append(vbox, webview)
            close_button = Gtk.Button.new_from_icon_name("window-close", Gtk.IconSize.BUTTON)
            close_button.set_size_request(24, 24)
            close_button.set_tooltip_text("Close popup")
            window._webview = webview
            window._close_button = close_button
            window._vbox = vbox

            def on_popup_destroy(widget):
                try:
                    if hasattr(window, '_webview'):
                        window._webview = None
                    if hasattr(window, '_close_button'):
                        window._close_button = None
                    if hasattr(window, '_vbox'):
                        window._vbox = None
                except Exception:
                    pass
            window.connect("destroy", on_popup_destroy)
            close_button.connect("clicked", lambda btn: window.destroy())
            if hasattr(close_button, 'get_parent') and close_button.get_parent() is not None:
                parent = close_button.get_parent()
                if parent and hasattr(parent, "remove") and close_button.get_parent() == parent:
                    try:
                        parent.remove(close_button)
                    except Exception:
                        pass
            self.safe_append(vbox, close_button)
            window.add(vbox)
            if not hasattr(self, '_popup_windows'):
                self._popup_windows = []
            self._popup_windows.append(window)

            def cleanup_window_reference(widget):
                try:
                    if hasattr(self, '_popup_windows'):
                        if window in self._popup_windows:
                            self._popup_windows.remove(window)
                except Exception:
                    pass
            window.connect("destroy", cleanup_window_reference)
            window.present()
        except Exception:
            pass

    def load_html_with_bootstrap(self, html):
        """
        Load HTML content into the current webview with Bootstrap CSS linked in the head.
        If Bootstrap CSS link is not present, it will be injected.
        """
        try:
            webview = self.get_current_webview()
            if not webview:
                return
        except Exception:
            pass

    def inject_css_adblock(self):
        """Inject CSS to hide ad elements."""
        try:
            css = """
                div[class*="ad"]:not(.player-container, #player, .controls) {
                    display: none !important;
                }
            """
            style = WebKit2.UserStyleSheet.new(
                css,
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserStyleSheetLevel.USER,
                [], []
            )
            self.content_manager.add_style_sheet(style)
        except Exception:
            pass

    def inject_adware_cleaner(self):
        """Enhanced ad-blocker that preserves media players while blocking ads."""
        script_source = """
        (function() {
            // Media player selectors to preserve
            const playerSelectors = [
                '[class*="player" i]',
                '[id*="player" i]',
                '[class*="video" i]',
                '[id*="video" i]',
                '[class*="media" i]',
                '[id*="media" i]',
                'video', 'audio', 'object', 'embed',
                // Streaming service specific selectors
                '[class*="jwplayer" i]',
                '[class*="vjs-" i]',
                '[class*="video-js" i]',
                '[class*="mejs-" i]',
                '[class*="flowplayer" i]',
                '[class*="plyr" i]',
                '[class*="shaka-" i]',
                '[class*="dash-" i]',
                '[class*="hls-" i]',
                '[class*="youtube" i]',
                '[class*="vimeo" i]',
                '[class*="netflix" i]',
                '[class*="hulu" i]',
                '[class*="amazon" i]',
                '[class*="disney" i]',
                '[class*="crunchyroll" i]',
                '[class*="funimation" i]',
                '[class*="tubi" i]',
                '[class*="peacock" i]',
                '[class*="paramount" i]',
                '[class*="hbomax" i]',
                '[class*="max" i]',
                '[class*="roku" i]',
                '[class*="twitch" i]',
                '[class*="kick" i]',
                '[class*="tiktok" i]',
                '[class*="instagram" i]',
                '[class*="facebook" i]',
                '[class*="twitter" i]',
                '[class*="x" i]',
                '[class*="snapchat" i]',
                '[class*="linkedin" i]',
                '[class*="pinterest" i]',
                '[class*="reddit" i]',
                '[class*="tumblr" i]',
                '[class*="discord" i]',
                '[class*="mixer" i]',
                '[class*="beam" i]',
                '[class*="hitbox" i]',
                '[class*="smashcast" i]',
                '[class*="azubu" i]',
                '[class*="dailymotion" i]',
                '[class*="vevo" i]',
                '[class*="mtv" i]',
                '[class*="vh1" i]',
                '[class*="bet" i]',
                '[class*="cm" i]'
            ];
            // Whitelist of classes that should never be removed
            const whitelistedClasses = [
                'java', 'javaplayer', 'javaplugin', 'jvplayer', 'jwplayer',
                'video', 'player', 'mediaplayer', 'html5-video-player',
                'vjs-', 'mejs-', 'flowplayer', 'plyr', 'mediaelement',
                'shaka-', 'dash-', 'hls-', 'video-js', 'youtube', 'vimeo',
                'netflix', 'hulu', 'amazon', 'disney', 'crunchyroll', 'funimation',
                'tubi', 'peacock', 'paramount', 'hbomax', 'max', 'roku', 'twitch',
                'kick', 'tiktok', 'instagram', 'facebook', 'twitter', 'x', 'snapchat',
                'linkedin', 'pinterest', 'reddit', 'tumblr', 'discord', 'dailymotion',
                'vevo', 'mtv', 'vh1', 'bet', 'cm', 'logo', 'brand', 'sponsor', 'promo',
                'commercial', 'advert', 'banner', 'popup', 'overlay', 'modal', 'lightbox',
                'interstitial', 'pre-roll', 'mid-roll', 'post-roll', 'skip', 'close',
                'dismiss', 'hide', 'remove', 'block', 'mute', 'pause', 'stop', 'cancel',
                'exit', 'quit', 'end', 'finish', 'complete', 'done', 'finished', 'completed',
                'ended', 'stopped', 'paused', 'muted', 'blocked', 'removed', 'hidden',
                'dismissed', 'closed', 'skipped', 'post-rolled', 'mid-rolled', 'pre-rolled',
                'interstitialed', 'lightboxed', 'modaled', 'overlaid', 'popped', 'bannered',
                'advertised', 'promoted', 'sponsored', 'branded', 'logod', 'cmd'
            ];
            // Ad patterns to block - more conservative approach
            const blockedSelectors = [
                // Ad iframes - only block obvious ad domains
                'iframe[src*="doubleclick.net" i]',
                'iframe[src*="googlesyndication.com" i]',
                'iframe[src*="adsystem.amazon" i]',
                'iframe[src*="adsystem" i]',
                // Ad containers - be more specific to avoid blocking players
                'div[class*="ad-container" i]:not([class*="player" i]):not([class*="video" i]):not([class*="media" i])',
                'div[class*="ad_wrapper" i]:not([class*="player" i]):not([class*="video" i]):not([class*="media" i])',
                'div[class*="ad-wrapper" i]:not([class*="player" i]):not([class*="video" i]):not([class*="media" i])',
                // Popups and overlays - exclude player-related
                'div[class*="popup" i]:not([class*="player" i]):not([class*="video" i]):not([class*="media" i])',
                'div[class*="overlay" i]:not([class*="player" i]):not([class*="video" i]):not([class*="media" i])',
                'div[class*="modal" i]:not([class*="player" i]):not([class*="video" i]):not([class*="media" i])',
                'div[class*="lightbox" i]:not([class*="player" i]):not([class*="video" i]):not([class*="media" i])'
            ];
            function isInPlayer(element) {
                // Check if element is inside a media player
                let parent = element;
                while (parent) {
                    if (playerSelectors.some(selector => parent.matches && parent.matches(selector))) {
                        return true;
                    }
                    parent = parent.parentElement;
                }
                return false;
            }
            function hasPlayerClass(element) {
                // Check if element or its parents have player-related classes
                let parent = element;
                while (parent) {
                    const classList = parent.classList || [];
                    for (const className of classList) {
                        if (whitelistedClasses.some(whitelist => className.toLowerCase().includes(whitelist.toLowerCase()))) {
                            return true;
                        }
                    }
                    parent = parent.parentElement;
                }
                return false;
            }
            function removeAds() {
                blockedSelectors.forEach(selector => {
                    try {
                        document.querySelectorAll(selector).forEach(el => {
                            if (el.offsetParent !== null && !isInPlayer(el) && !hasPlayerClass(el)) {
                                // Additional check: don't remove if element contains video/audio tags
                                if (!el.querySelector('video, audio, object, embed')) {
                                    el.remove();
                                }
                            }
                        });
                    } catch (e) {
                        console.warn('Error in ad blocker:', e);
                    }
                });
            }
            // Run on page load and when DOM changes
            document.addEventListener('DOMContentLoaded', removeAds);
            const observer = new MutationObserver(removeAds);
            observer.observe(document.body, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ['class', 'id', 'src']
            });
        })();
        """
        try:
            script = WebKit2.UserScript.new(
                script_source,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.END,
            )
            self.content_manager.add_script(script)
        except Exception:
            pass

    def inject_remove_malicious_links(self):
        """Inject malicious link remover JavaScript."""
        script_source = """
        // Remove or neutralize potentially malicious links
        function sanitizeLinks() {
            const links = document.querySelectorAll('a[href^="javascript:"]:not([href^="javascript:void(0)"])');
            links.forEach(link => {
                link.removeAttribute('onclick');
                link.removeAttribute('onmousedown');
                link.href = '#';
                link.title = 'Potentially harmful link blocked';
            });
        }
        // Run on page load and when DOM changes
        document.addEventListener('DOMContentLoaded', sanitizeLinks);
        const observer = new MutationObserver(sanitizeLinks);
        observer.observe(document.body, { childList: true, subtree: true });
        """
        try:
            script = WebKit2.UserScript.new(
                script_source,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.END,
            )
            self.content_manager.add_script(script)
        except Exception:
            pass

    def inject_nonce_respecting_script(self):
        """Inject nonce-respecting script for CSP compatibility."""
        script_source = """
        // This script respects CSP nonce if present
        (function() {
            const scripts = document.querySelectorAll('script[nonce]');
            if (scripts.length > 0) {
                const nonce = scripts[0].nonce || scripts[0].getAttribute('nonce');
                if (nonce) {
                    const meta = document.createElement('meta');
                    meta.httpEquiv = "Content-Security-Policy";
                    meta.content = `script-src 'nonce-${nonce}' 'strict-dynamic' 'unsafe-inline' 'self'`;
                    document.head.appendChild(meta);
                }
            }
        })();
        """
        try:
            script = WebKit2.UserScript.new(
                script_source,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
            )
            self.content_manager.add_script(script)
        except Exception:
            pass

    def disable_biometrics_in_webview(self, webview):
        """
        Injects JavaScript into the WebKit2GTK WebView to block WebAuthn biometric prompts.
        This disables navigator.credentials.get/create with publicKey options.
        """
        try:
            script = """
            (function() {
                if (navigator.credentials) {
                    const originalGet = navigator.credentials.get;
                    const originalCreate = navigator.credentials.create;
                    navigator.credentials.get = function(options) {
                        if (options && options.publicKey) {
                            console.warn("[WebAuthn Blocked] navigator.credentials.get intercepted");
                            return Promise.reject(new DOMException("Biometric login blocked by user", "NotAllowedError"));
                        }
                        return originalGet.apply(this, arguments);
                    };
                    navigator.credentials.create = function(options) {
                        if (options && options.publicKey) {
                            console.warn("[WebAuthn Blocked] navigator.credentials.create intercepted");
                            return Promise.reject(new DOMException("Biometric credential creation blocked", "NotAllowedError"));
                        }
                        return originalCreate.apply(this, arguments);
                    };
                }
            })();
            """
            user_script = WebKit2.UserScript.new(
                script,
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START,
                [], []
            )
            webview.get_user_content_manager().add_script(user_script)
        except Exception:
            pass

    def block_biometric_apis(self, webview: WebKit2.WebView):
        """
        Blocks WebAuthn biometric APIs and navigator.sendBeacon() in WebKit2GTK browser.
        This method injects JavaScript to prevent fingerprinting through WebAuthn and
        blocks the sendBeacon API which can be used for tracking. It provides a clean
        rejection message without cluttering the console with warnings.
        Args:
            webview: The WebKit2.WebView instance to apply the blocking to
        """
        try:
            if not webview or not hasattr(webview, 'get_user_content_manager'):
                return
            script = """
            (function() {
                // Block WebAuthn
                if (navigator.credentials) {
                    const originalGet = navigator.credentials.get;
                    const originalCreate = navigator.credentials.create;
                    // Store original console.warn to suppress our own messages
                    const originalWarn = console.warn;
                    const originalError = console.error;
                    // Only show our warning once per page load
                    let warningShown = false;
                    // Function to show warning only once
                    function showWarningOnce(message) {
                        if (!warningShown) {
                            originalWarn.call(console, "[Shadow Browser] " + message);
                            warningShown = true;
                        }
                    }
                    // Override credentials.get
                    navigator.credentials.get = function(options) {
                        if (options && options.publicKey) {
                            showWarningOnce("WebAuthn authentication blocked for security");
                            return Promise.reject(
                                new DOMException(
                                    "Biometric authentication is disabled in this browser for security reasons.",
                                    "NotAllowedError"
                                )
                            );
                        }
                        return originalGet.apply(this, arguments);
                    };
                    // Override credentials.create
                    navigator.credentials.create = function(options) {
                        if (options && options.publicKey) {
                            showWarningOnce("WebAuthn registration blocked for security");
                            return Promise.reject(
                                new DOMException(
                                    "Biometric registration is disabled in this browser for security reasons.",
                                    "NotAllowedError"
                                )
                            );
                        }
                        return originalCreate.apply(this, arguments);
                    };
                    // Restore original console methods
                    Object.defineProperty(console, 'warn', {
                        value: originalWarn,
                        writable: false,
                        configurable: false
                    });
                    Object.defineProperty(console, 'error', {
                        value: originalError,
                        writable: false,
                        configurable: false
                    });
                }
                // Block navigator.sendBeacon silently
                const originalSendBeacon = navigator.sendBeacon;
                navigator.sendBeacon = function() {
                    // Silently block without logging to avoid console spam
                    return false;
                };
                // Make it harder to detect our sendBeacon override
                Object.defineProperty(navigator, 'sendBeacon', {
                    value: navigator.sendBeacon,
                    writable: false,
                    configurable: false
                });
            })();
            """
            try:
                user_script = WebKit2.UserScript.new(
                    script,
                    WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                    WebKit2.UserScriptInjectionTime.START,
                    [],
                    []
                )
                content_manager = webview.get_user_content_manager()
                content_manager.remove_all_scripts()
                content_manager.add_script(user_script)
            except Exception:
                pass
        except Exception:
            pass

    def inject_anti_fingerprinting_script(self, user_content_manager):
        """Inject anti-fingerprinting JavaScript."""
        script = """
        (function() {
            try {
                Object.defineProperty(navigator, 'userAgent', { get: function() { return 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit2/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36'; } });
                Object.defineProperty(navigator, 'platform', { get: function() { return 'Linux x86_64'; } });
                                Object.defineProperty(navigator, 'deviceMemory', { get: function() { return 8; } });
                Object.defineProperty(navigator, 'plugins', { get: function() { return []; } });
                Object.defineProperty(navigator, 'webdriver', { get: function() { return false; } });
                Object.defineProperty(navigator, 'getBattery', { get: function() { return function() { return Promise.resolve({ charging: true, level: 1.0 }); }; } });
                Object.defineProperty(navigator, 'geolocation', { get: function() { return { getCurrentPosition: function() {}, watchPosition: function() {} }; } });
            } catch (e) {
                console.log('Anti-fingerprinting script error:', e);
            }
        })();
        """
        try:
            user_script = WebKit2.UserScript.new(
                script,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
                None, None
            )
            user_content_manager.add_script(user_script)
        except Exception:
            pass

    def inject_js_router_fix(self, user_content_manager):
        """Inject JavaScript to fix flawed JS routers and handle Next Episode links."""
        script = """
        (function() {
            'use strict';
            // Router fix for Next Episode links
            const RouterFix = {
                init: function() {
                    this.fixHistoryAPI();
                    this.fixPushState();
                    this.fixReplaceState();
                    this.fixNextEpisodeLinks();
                    this.setupMutationObserver();
                    this.fixHashChange();
                },
                fixHistoryAPI: function() {
                    const originalPushState = history.pushState;
                    const originalReplaceState = history.replaceState;
                    history.pushState = function(state, title, url) {
                        console.log('[RouterFix] pushState intercepted:', url);
                        const result = originalPushState.apply(this, arguments);
                        RouterFix.handleRouteChange(url);
                        return result;
                    };
                    history.replaceState = function(state, title, url) {
                        console.log('[RouterFix] replaceState intercepted:', url);
                        const result = originalReplaceState.apply(this, arguments);
                        RouterFix.handleRouteChange(url);
                        return result;
                    };
                },
                fixPushState: function() {
                    // Fix for SPAs that use pushState incorrectly
                    window.addEventListener('popstate', function(event) {
                        console.log('[RouterFix] popstate event:', event.state);
                        RouterFix.handleRouteChange(window.location.href);
                    });
                },
                fixReplaceState: function() {
                    // Ensure replaceState updates the URL correctly
                    const originalReplaceState = history.replaceState;
                    history.replaceState = function(state, title, url) {
                        if (url && typeof url === 'string') {
                            // Ensure URL is properly formatted
                            try {
                                new URL(url, window.location.origin);
                            } catch (e) {
                                console.warn('[RouterFix] Invalid URL in replaceState:', url);
                                return;
                            }
                        }
                        return originalReplaceState.apply(this, arguments);
                    };
                },
                fixNextEpisodeLinks: function() {
                    // Fix Next Episode links that use flawed JS routing
                    const fixNextEpisode = function() {
                        const nextEpisodeLinks = document.querySelectorAll('a[href*="next"], a[href*="episode"], .next-episode, .episode-next');
                        nextEpisodeLinks.forEach(link => {
                            // Store original href
                            const originalHref = link.getAttribute('href');
                            link.addEventListener('click', function(e) {
                                console.log('[RouterFix] Next Episode link clicked:', originalHref);
                                if (originalHref === '#' || originalHref.startsWith('javascript:')) {
                                    e.preventDefault();
                                    const actualUrl = link.getAttribute('data-url') ||
                                                     link.getAttribute('data-next') ||
                                                     link.getAttribute('data-href');
                                    if (actualUrl) {
                                        RouterFix.navigateTo(actualUrl);
                                    } else {
                                        const onclick = link.getAttribute('onclick');
                                        if (onclick) {
                                            const urlMatch = onclick.match(/['"]([^'"]+)['"]/);
                                            if (urlMatch) {
                                                RouterFix.navigateTo(urlMatch[1]);
                                            }
                                        }
                                    }
                                }
                            });
                        });
                    };
                    fixNextEpisode();
                },
                navigateTo: function(url) {
                    console.log('[RouterFix] Navigating to:', url);
                    const absoluteUrl = new URL(url, window.location.origin).href;
                    history.pushState({}, '', absoluteUrl);
                    window.dispatchEvent(new CustomEvent('routerfix:navigate', {
                        detail: { url: absoluteUrl }
                    }));
                    window.dispatchEvent(new PopStateEvent('popstate', {
                        state: { url: absoluteUrl }
                    }));
                },
                handleRouteChange: function(url) {
                    console.log('[RouterFix] Route changed to:', url);
                    setTimeout(() => {
                        RouterFix.fixNextEpisodeLinks();
                    }, 100);
                },
                setupMutationObserver: function() {
                    const observer = new MutationObserver(function(mutations) {
                        mutations.forEach(function(mutation) {
                            if (mutation.type === 'childList') {
                                mutation.addedNodes.forEach(function(node) {
                                    if (node.nodeType === 1) {
                                        const elements = [node, ...node.querySelectorAll('*')];
                                        for (const el of elements) {
                                            const text = (el.textContent || '').toLowerCase().trim();
                                            if (text.includes('next') || text.includes('episode')) {
                                                RouterFix.fixNextEpisodeLinks();
                                            }
                                        }
                                    }
                                });
                            }
                        });
                    });
                    observer.observe(document.body, { childList: true, subtree: true });
                },
                fixHashChange: function() {
                    window.addEventListener('hashchange', function() {
                        console.log('[RouterFix] Hash changed:', window.location.hash);
                        RouterFix.handleRouteChange(window.location.href);
                    });
                }
            };
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', RouterFix.init);
            } else {
                RouterFix.init();
            }
            window.RouterFix = RouterFix;
            console.log('[RouterFix] JavaScript router fix loaded');
        })();
        """
        try:
            user_script = WebKit2.UserScript.new(
                script,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
                None, None
            )
            user_content_manager.add_script(user_script)
            print("JavaScript router fix injected successfully")
        except Exception:
            pass

    def DNT(self):
        """Inject Do Not Track header."""
        try:
            dnt_script = """
            Object.defineProperty(navigator, 'doNotTrack', {
                get: function() { return '1'; }
            });
            """
            user_script = WebKit2.UserScript.new(
                dnt_script,
                WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START,
                [], []
            )
            webview = self.get_current_webview()
            if webview:
                content_manager = webview.get_user_content_manager()
                content_manager.add_script(user_script)
        except Exception:
            pass

    def _create_http_session(self):
        """
        Create a configured requests session with retries, timeouts, and optional Tor routing.
        Returns:
            requests.Session: Configured session object
        """
        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; rv:102.0) Gecko/20100101 Firefox/102.0',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1'
            })
            retry_strategy = Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[408, 429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST", "HEAD", "OPTIONS"],
                raise_on_status=False
            )
            adapter = HTTPAdapter(
                max_retries=retry_strategy,
                pool_connections=10,
                pool_maxsize=10
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            session.timeout = 30
            if self.tor_enabled and hasattr(self, 'tor_manager') and self.tor_manager.is_running():
                proxy_url = f'socks5h://127.0.0.1:{self.tor_manager.tor_port}'
                session.proxies = {
                    'http': proxy_url,
                    'https': proxy_url
                }
                try:
                    test_url = 'https://check.torproject.org/api/ip'
                    response = session.get(test_url, timeout=10)
                    if 'IsTor' not in response.json().get('IsTor', ''):
                        self.tor_enabled = False
                except Exception:
                    pass
            return session
        except ImportError:
            pass
        except Exception:
            pass

    def check_turnstile(self):
        script = """
        var turnstile = document.querySelector('.cf-turnstile');
        if (turnstile) {
            console.log('Turnstile detected');
            turnstile;
        } else {
            console.log('No Turnstile found');
            null;
        }
        """
        self.webview.run_javascript(script, None, self.turnstile_callback, None)

    def turnstile_callback(self, webview, result, user_data):
            js_result = webview.run_javascript_finish(result)
            if js_result:
                value = js_result.get_js_value()
                if not value.is_null():
                    pass
                else:
                    pass

    def load_page(self):
        self.webview.load_uri(self.url)
        time.sleep(random.uniform(2, 5))

    def navigate_to(self, path):
        new_url = f"{self.url.rstrip('/')}/{path.lstrip('/')}"
        self.webview.load_uri(new_url)
        time.sleep(random.uniform(2, 5))

    def _load_favicon_async(self, favicon_uri, callback=None):
        """
        Load favicon asynchronously and return a Gdk.Texture.
        """
        def load_favicon_with_retry(uri, retries=2, timeout=5):
            """Try to fetch favicon with retries and better error handling."""
            if not uri or not uri.startswith(('http://', 'https://')):
                return None
            headers = {
                'User-Agent': self.webview.get_settings().get_user_agent(),
                'Accept': 'image/webp,image/*,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            for attempt in range(retries + 1):
                try:
                    try:
                        head_response = requests.head(
                            uri,
                            headers=headers,
                            timeout=timeout,
                            allow_redirects=True
                        )
                        if head_response.status_code != 200:
                            continue
                    except (requests.exceptions.RequestException, Exception):
                        continue
                    response = requests.get(
                        uri,
                        stream=True,
                        headers=headers,
                        timeout=timeout,
                        allow_redirects=True
                    )
                    if response.status_code == 200 and response.content:
                        content_type = response.headers.get('Content-Type', '').lower()
                        if 'image/' in content_type:
                            return response
                except (requests.exceptions.SSLError,
                       requests.exceptions.ConnectTimeout,
                       requests.exceptions.ReadTimeout,
                       requests.exceptions.ConnectionError,
                       requests.exceptions.TooManyRedirects,
                       requests.exceptions.RequestException):
                    if attempt < retries:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                except Exception:
                    if attempt < retries:
                        time.sleep(0.5 * (attempt + 1))
                        continue
            return None

        def on_favicon_loaded(texture):
            if callback:
                GLib.idle_add(callback, texture)
        try:
            if not favicon_uri or not favicon_uri.startswith(('http://', 'https://')):
                base_uri = self.webview.get_uri()
                if not base_uri:
                    on_favicon_loaded(None)
                    return
                base_url = base_uri.rstrip('/')
                domain = urlparse(base_uri).netloc
                favicon_urls = [
                    f"{base_url}/favicon.ico",
                    f"https://{domain}/favicon.ico",
                    f"{base_url}/favicon.png",
                    f"https://{domain}/favicon.png",
                ]
                for url in favicon_urls:
                    response = load_favicon_with_retry(url)
                    if response:
                        favicon_uri = url
                        break
                else:
                    on_favicon_loaded(None)
                    return
            response = load_favicon_with_retry(favicon_uri)
            if not response:
                on_favicon_loaded(None)
                return
            image_data = response.content
            if not image_data:
                raise ValueError("Empty favicon data")
            try:
                gbytes = GLib.Bytes.new(image_data)
                texture = Gdk.Texture.new_from_bytes(gbytes)
                on_favicon_loaded(texture)
            except Exception as e:
                print(f"Failed to decode favicon: {e}")
                on_favicon_loaded(None)
        except Exception as e:
            print(f"Unexpected error in favicon loading: {e}")
            on_favicon_loaded(None)

    # Menu item handlers
    def on_new_window(self, menu_item):
        """Handle File -> New Window menu item."""
        new_app = ShadowBrowser()
        new_app.run(None)

    def on_open_file(self, menu_item):
        """Handle File -> Open File menu item."""
        dialog = Gtk.FileChooserDialog(
            title="Open File",
            parent=self.window,
            action=Gtk.FileChooserAction.OPEN,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        )
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            file_uri = dialog.get_uri()
            self.load_url(file_uri)
        dialog.destroy()

    def on_save_page(self, menu_item):
        """Handle File -> Save Page As menu item."""
        webview = self.get_current_webview()
        if webview:
            dialog = Gtk.FileChooserDialog(
                title="Save Page As",
                parent=self.window,
                action=Gtk.FileChooserAction.SAVE,
                buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
            )
            dialog.set_current_name("webpage.html")
            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                file_path = dialog.get_filename()
                webview.run_javascript("document.documentElement.outerHTML", None,
                                      lambda w, r, d: self._save_page_callback(r, file_path), None)
            dialog.destroy()

    def _save_page_callback(self, result, file_path):
        """Callback for saving page content."""
        try:
            js_result = self.get_current_webview().run_javascript_finish(result)
            if js_result:
                content = js_result.get_js_value().to_string()
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
        except Exception as e:
            print(f"Error saving page: {e}")

    def on_print_page(self, menu_item):
        """Handle File -> Print menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.run_javascript("window.print()", None, None, None)

    def on_quit(self, menu_item):
        """Handle File -> Quit menu item."""
        self.on_window_destroy(None)

    def on_undo(self, menu_item):
        """Handle Edit -> Undo menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.run_javascript("document.execCommand('undo', false, null)", None, None, None)

    def on_redo(self, menu_item):
        """Handle Edit -> Redo menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.run_javascript("document.execCommand('redo', false, null)", None, None, None)

    def on_cut(self, menu_item):
        """Handle Edit -> Cut menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.run_javascript("document.execCommand('cut', false, null)", None, None, None)

    def on_copy(self, menu_item):
        """Handle Edit -> Copy menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.run_javascript("document.execCommand('copy', false, null)", None, None, None)

    def on_paste(self, menu_item):
        """Handle Edit -> Paste menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.run_javascript("document.execCommand('paste', false, null)", None, None, None)

    def on_delete(self, menu_item):
        """Handle Edit -> Delete menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.run_javascript("document.execCommand('delete', false, null)", None, None, None)

    def on_select_all(self, menu_item):
        """Handle Edit -> Select All menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.run_javascript("document.execCommand('selectAll', false, null)", None, None, None)

    def on_find(self, menu_item):
        """Handle Edit -> Find menu item."""
        dialog = Gtk.Dialog(title="Find", parent=self.window, buttons=(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE))
        entry = Gtk.Entry()
        entry.set_placeholder_text("Search text...")
        dialog.get_content_area().pack_start(entry, True, True, 0)
        entry.show()
        dialog.run()
        dialog.destroy()

    def on_zoom_in(self, menu_item):
        """Handle View -> Zoom In menu item."""
        webview = self.get_current_webview()
        if webview:
            zoom_level = webview.get_zoom_level()
            webview.set_zoom_level(min(zoom_level + 0.1, 3.0))

    def on_zoom_out(self, menu_item):
        """Handle View -> Zoom Out menu item."""
        webview = self.get_current_webview()
        if webview:
            zoom_level = webview.get_zoom_level()
            webview.set_zoom_level(max(zoom_level - 0.1, 0.5))

    def on_zoom_normal(self, menu_item):
        """Handle View -> Actual Size menu item."""
        webview = self.get_current_webview()
        if webview:
            webview.set_zoom_level(1.0)

    def on_fullscreen(self, menu_item):
        """Handle View -> Fullscreen menu item."""
        if menu_item.get_active():
            self.window.fullscreen()
        else:
            self.window.unfullscreen()

    def on_downloads(self, menu_item):
        """Handle View -> Downloads menu item."""
        self.download_manager.show()

    def on_home_clicked(self, menu_item):
        """Handle History -> Home menu item."""
        self.load_url(self.home_url)

    def on_show_history(self, menu_item):
        """Handle History -> Show All History menu item."""
        dialog = Gtk.Dialog(title="History", parent=self.window,
                          buttons=(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE))
        dialog.set_default_size(600, 400)

        scrolled = Gtk.ScrolledWindow()
        listbox = Gtk.ListBox()

        for item in reversed(self.history[-50:]):
            if isinstance(item, dict) and 'url' in item:
                url = item['url']
                timestamp = item.get('timestamp', 0)
                time_str = datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')
                label = Gtk.Label(label=f"{time_str} - {url}")
                label.set_halign(Gtk.Align.START)
                row = Gtk.ListBoxRow()
                row.add(label)
                row.connect("button-press-event", lambda w, e, u=url: self.load_url(u))
                listbox.add(row)

        scrolled.add(listbox)
        dialog.get_content_area().pack_start(scrolled, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_clear_history(self, menu_item):
        """Handle History -> Clear History menu item."""
        dialog = Gtk.MessageDialog(parent=self.window, flags=Gtk.DialogFlags.MODAL,
                                  type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                                  message_format="Clear all history?")
        dialog.format_secondary_text("This action cannot be undone.")
        response = dialog.run()
        if response == Gtk.ResponseType.YES:
            self.history = []
            self.save_json(HISTORY_FILE, self.history)
        dialog.destroy()

    def on_bookmark_page(self, menu_item):
        """Handle Bookmarks -> Bookmark This Page menu item."""
        webview = self.get_current_webview()
        if webview:
            url = webview.get_uri()
            title = webview.get_title() or url
            if url and url.startswith(('http://', 'https://')):
                bookmark = {'url': url, 'title': title, 'timestamp': time.time()}
                self.bookmarks.append(bookmark)
                self.save_json(BOOKMARKS_FILE, self.bookmarks)
                self.update_bookmarks_menu(self.bookmark_menu)

    def on_show_bookmarks(self, menu_item):
        """Handle Bookmarks -> Show All Bookmarks menu item."""
        dialog = Gtk.Dialog(title="Bookmarks", parent=self.window,
                          buttons=(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE))
        dialog.set_default_size(600, 400)

        scrolled = Gtk.ScrolledWindow()
        listbox = Gtk.ListBox()

        for bookmark in self.bookmarks:
            if isinstance(bookmark, dict) and 'url' in bookmark and 'title' in bookmark:
                url = bookmark['url']
                title = bookmark['title']
                label = Gtk.Label(label=f"{title}\n{url}")
                label.set_halign(Gtk.Align.START)
                row = Gtk.ListBoxRow()
                row.add(label)
                row.connect("button-press-event", lambda w, e, u=url: self.load_url(u))
                listbox.add(row)

        scrolled.add(listbox)
        dialog.get_content_area().pack_start(scrolled, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_web_inspector(self, menu_item):
        """Handle Tools -> Web Inspector menu item."""
        webview = self.get_current_webview()
        if webview:
            settings = webview.get_settings()
            settings.set_property("enable-developer-extras", True)
            webview.set_settings(settings)
            inspector = webview.get_inspector()
            inspector.show()

    def on_tor_toggle(self, menu_item):
        """Handle Tools -> Tor -> Enable Tor menu item."""
        self.tor_enabled = menu_item.get_active()
        if self.tor_enabled:
            self.initialize_tor()
        else:
            if self.tor_manager:
                self.tor_manager.stop()

    def on_new_identity(self, menu_item):
        """Handle Tools -> Tor -> New Identity menu item."""
        if self.tor_manager and self.tor_manager.is_running():
            self.tor_manager.new_identity()

def main():
    """Main entry point for the Shadow Browser."""
    print("Starting Shadow Browser...")
    try:
        print("Creating ShadowBrowser instance...")
        app = ShadowBrowser()
        print("Running app...")
        return app.run(None)
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
