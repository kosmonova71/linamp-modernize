import base64
import atexit
import json
import logging
import os
import random
import re
import signal
import socket
import ssl
import psutil
import requests
import subprocess
import sys
import shutil
import threading
import time
import uuid
import traceback
import urllib.request
from urllib.parse import urlparse, urlunparse
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from stem.control import Controller
import gi

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_EXTENSIONS = [
    ".3gp", ".7z", ".aac", ".apk", ".appimage", ".avi", ".bat", ".bin", ".bmp",
    ".bz2", ".c", ".cmd", ".cpp", ".cs", ".deb", ".dmg", ".dll", ".doc", ".docx",
    ".eot", ".exe", ".flac", ".flv", ".gif", ".gz", ".h", ".ico", ".img", ".iso",
    ".jar", ".java", ".jpeg", ".jpg", ".js", ".lua", ".lz", ".lzma", ".m4a", ".mkv",
    ".mov", ".mp3", ".mp4", ".mpg", ".mpeg", ".msi", ".odp", ".ods", ".odt", ".ogg",
    ".otf", ".pdf", ".pkg", ".pl", ".png", ".pps", ".ppt", ".pptx", ".ps1",
    ".py", ".rar", ".rb", ".rpm", ".rtf", ".run", ".sh", ".so", ".svg", ".tar",
    ".tar.bz2", ".tar.gz", ".tbz2", ".tgz", ".tiff", ".ttf", ".txt", ".vhd", ".vmdk",
    ".wav", ".webm", ".webp", ".wma", ".woff", ".woff2", ".wmv", ".xls", ".xlsx", ".zip",
]

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
        except Exception:
            return False

    def get_status(self):
        """Return current Tor availability and connectivity status."""
        status = {
            'connected': False,
            'working': False,
            'system_available': False,
            'port': self.tor_port,
        }
        try:
            status['system_available'] = self._check_system_tor_running() or self._is_tor_already_running()
            if self.use_system_tor or self.is_running_flag or self.controller:
                status['connected'] = self.is_running()
            if status['connected']:
                try:
                    if self.controller and self.controller.is_alive():
                        circuit_status = self.controller.get_info("status/circuit-established")
                        status['working'] = circuit_status.strip() == '1'
                    else:
                        status['working'] = True
                except Exception:
                    status['working'] = False
        except Exception as exc:
            logger.warning("Error retrieving Tor status: %s", exc)
        return status

    def _is_tor_already_running(self):
        """Check if a Tor process is already running using the data directory or standard ports."""
        try:
            for proc in psutil.process_iter(['name', 'cmdline', 'pid']):
                try:
                    if proc.info['name'] and 'tor' in proc.info['name'].lower():
                        cmdline = proc.info['cmdline'] or []
                        if any(self.tor_data_dir in arg for arg in cmdline):
                            return True
                        if any('9050' in arg or '9051' in arg for arg in cmdline):
                            return True
                        try:
                            connections = proc.net_connections()
                            for conn in connections:
                                if hasattr(conn, 'laddr') and conn.laddr.port in [9050, 9051]:
                                    return True
                        except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                            continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    result = sock.connect_ex(('127.0.0.1', 9051))
                    if result == 0:
                        return True
            except Exception:
                pass
        except ImportError:
            try:
                for port in [9050, 9051]:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        sock.settimeout(1)
                        result = sock.connect_ex(('127.0.0.1', port))
                        if result == 0:
                            return True
            except Exception:
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
                f.write("HardwareAccel 1\n")
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
            import hashlib
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

    def enable_tor_proxy(self):
        """Enable Tor proxy for all network requests.
        Returns:
            bool: True if Tor proxy was successfully enabled, False otherwise
        """
        if not self.is_running():
            if not self.start():
                print("Failed to start Tor")
                return False
        try:
            proxy_uri = f"socks5h://127.0.0.1:{self.tor_port}"
            os.environ.update({
                'http_proxy': proxy_uri,
                'https_proxy': proxy_uri,
                'all_proxy': proxy_uri,
                'HTTP_PROXY': proxy_uri,
                'HTTPS_PROXY': proxy_uri,
                'ALL_PROXY': proxy_uri
            })
            if not self.configure_webkit_proxy(enable_tor=True):
                print("Warning: Could not configure WebKit proxy for Tor")
            print(f"Tor proxy enabled on port {self.tor_port}")
            return True
        except Exception as e:
            print(f"Error enabling Tor proxy: {e}")
            return False

    def disable_tor_proxy(self):
        """Disable Tor proxy and restore direct connections."""
        for var in ['http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']:
            os.environ.pop(var, None)
        if not self.configure_webkit_proxy(enable_tor=False):
            print("Warning: Could not reset WebKit proxy to direct mode")
        print("Tor proxy disabled")
        return True

    def _handle_response_policy(self, webview, decision):
        """Handle response policy decision."""
        try:
            decision.use()
        except Exception:
            pass
        return False

    def _get_network_session(self):
        """Return active WebKit network session if available."""
        session = None
        try:
            context = WebKit.WebContext.get_default()
        except AttributeError:
            context = None
        if context:
            get_session = getattr(context, "get_network_session", None)
            if callable(get_session):
                try:
                    session = get_session()
                except Exception as exc:
                    print(f"Warning: Could not obtain WebContext network session: {exc}")
        if session is None:
            try:
                session = WebKit.NetworkSession.get_default()
            except AttributeError:
                session = None
        return session

    def configure_webkit_proxy(self, enable_tor=True):
        """Configure WebKit network session proxy for Tor usage."""
        session = self._get_network_session()
        if session is None:
            print("Warning: No WebKit network session available for proxy configuration")
            return False
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
                    return True
                except (AttributeError, GLib.Error) as exc:
                    last_error = exc
            if last_error:
                print(f"Warning: Failed to configure Tor proxy: {last_error}")
            return False
        try:
            session.set_proxy_settings(WebKit.NetworkProxyMode.NO_PROXY, None)
            return True
        except (AttributeError, GLib.Error) as exc:
            print(f"Warning: Failed to reset WebKit proxy settings: {exc}")
            return False

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
        proxy = WebKit.NetworkProxy()
        proxy.set_protocol(WebKit.NetworkProxyProtocol.SOCKS5)
        proxy.set_hostname("127.0.0.1")
        proxy.set_port(proxy_port)
        web_context.set_network_proxy_settings(
            WebKit.NetworkProxyMode.CUSTOM,
            { "http": proxy, "https": proxy, "ftp": proxy }
        )
        return True

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

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gtk, Gdk, GLib, Gio, WebKit, Pango, GdkPixbuf
except (ValueError, ImportError):
    exit(1)

class _GIWarningFilter:
    def __init__(self, original):
        self._orig = original

    def write(self, message):
        if not message:
            return
        lower = message.lower()
        _gi_warning_patterns = ["typeinfo", "g_object_ref"]
        for pat in _gi_warning_patterns:
            if pat.lower() in lower:
                return
        self._orig.write(message)

    def flush(self):
        try:
            self._orig.flush()
        except (AttributeError, OSError, ValueError):
            pass

if not isinstance(sys.stderr, _GIWarningFilter):
    try:
        sys.stderr = _GIWarningFilter(sys.stderr)
    except (AttributeError, TypeError):
        pass

try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
except ImportError:
    dbus = None
    DBusGMainLoop = None
    from gi.repository import WebKit
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
            if hasattr(current_parent, 'remove'):
                current_parent.remove(widget)
        if hasattr(container, 'append'):
            container.append(widget)
        else:
            container.add(widget)
        return True
    except (AttributeError, TypeError):
        return False

BOOKMARKS_FILE = "bookmarks.json"
HISTORY_FILE = "history.json"
SESSION_FILE = "session.json"
TABS_FILE = "tabs.json"
HISTORY_LIMIT = 100

try:
    import js_obfuscation_improved
    js_extract_url = getattr(js_obfuscation_improved, 'extract_url_from_javascript', None)
    extract_onclick_url = getattr(js_obfuscation_improved, 'extract_onclick_url', None)
    if js_extract_url is None or extract_onclick_url is None:
        print("Warning: js_obfuscation_improved module found but missing required functions")
        js_extract_url = None
        extract_onclick_url = None
except ImportError:
    pass
except Exception as e:
    print(f"Warning: Error importing js_obfuscation_improved: {e}")
    js_extract_url = None
    extract_onclick_url = None

class SSLUtils:
    def __init__(self):
        self.context = ssl.create_default_context()

    def fetch_certificate(self, url):
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 443
        with socket.create_connection((host, port), timeout=5) as sock:
            with self.context.wrap_socket(sock, server_hostname=host) as ssock:
                cert_bin = ssock.getpeercert(binary_form=True)
                cert = x509.load_der_x509_certificate(cert_bin, default_backend())
                return cert

    def get_ocsp_url(self, cert):
        aia = cert.extensions.get_extension_for_oid(
            x509.ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        ).value
        for access in aia:
            if access.access_method == x509.AuthorityInformationAccessOID.OCSP:
                return access.access_location.value

    def is_certificate_expired(self, cert: x509.Certificate) -> bool:
        """
        Check if the certificate is expired.
        Args:
        cert (x509.Certificate): The X.509 certificate object.
        Returns:
        bool: True if the certificate is expired, False otherwise.
        """
        return cert.not_valid_after < datetime.now(timezone.utc)


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
                        // Don't modify arguments - return early instead
                        return;
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
                        return Promise.reject(new Error('AdBlock: Request blocked'));
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
                                    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.voidLinkClicked) {
                                        window.webkit.messageHandlers.voidLinkClicked.postMessage(url);
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
            WebKit.UserScript.new(
                adblock_script,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
            )
        )
        user_content_manager.add_script(
            WebKit.UserScript.new(
                custom_script,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.END,
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
        import re
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
        script = WebKit.UserScript.new(
            csp_script,
            WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START,
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

    def is_third_party_request(self, url, current_origin):
        try:
            page_origin = urlparse(self.get_current_webview().get_uri()).netloc
            return current_origin != page_origin
        except Exception:
            return False

    def enable_mixed_content_blocking(self, webview):
        settings = webview.get_settings()
        settings.set_property("allow-running-insecure-content", False)
        webview.set_settings(settings)

    def secure_cookies(self):
        """Disable all cookies by setting accept policy to NEVER."""
        try:
            webview = self.get_current_webview()
            if webview:
                cookie_manager = webview.get_context().get_cookie_manager()
                cookie_manager.set_accept_policy(WebKit.CookieAcceptPolicy.NEVER)
        except Exception:
            pass

    def set_samesite_cookie(self, cookie_manager, cookie):
        cookie.set_same_site(WebKit.CookieSameSitePolicy.STRICT)
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
        request.finish_error(WebKit.NetworkError.CANCELLED, "Blob URI media playback not supported")

    def handle_data_uri(self, request, user_data=None):
        """Handle data: URIs for embedded content"""
        request.finish_error(WebKit.NetworkError.CANCELLED, "Data URI handling not implemented")

    def handle_media_request(self, request, user_data=None):
        """Handle media requests for better streaming support"""
        uri = request.get_uri()
        if any(substring in uri for substring in self.blocklist):
            request.finish_error(WebKit.NetworkError.CANCELLED, "Media request blocked")
            return
        request.finish()

class Tab:
    """Represents a single browser tab and its associated data."""

    def __init__(self, url, webview, scrolled_window=None, tab_id=None):
        self.url = url or "about:blank"
        self.webview = webview
        self.scrolled_window = scrolled_window
        self.tab_id = tab_id or str(id(self))
        self.is_pinned = False
        self.is_grouped = False
        self.group_name = None
        self.thumbnail = None
        self.last_activity = time.time()
        self.loading = False
        self._signal_handlers = []
        self._init_ui()

    def _init_ui(self):
        """Initialize the tab's UI components."""
        self.label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.favicon = Gtk.Picture()
        self.favicon.set_size_request(16, 16)
        self.favicon.set_halign(Gtk.Align.CENTER)
        self.favicon.set_valign(Gtk.Align.CENTER)
        self.title_label = Gtk.Label(label="New Tab")
        self.title_label.set_hexpand(True)
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label_box.append(self.favicon)
        self.label_box.append(self.title_label)
        self._init_close_button()
        self._init_header_box()

    def _init_close_button(self):
        """Initialize the tab close button."""
        self.close_button = Gtk.Button(
        icon_name="window-close-symbolic",
        has_frame=False,
        width_request=24,
        height_request=24
        )
        self.close_button.add_css_class("flat")
        self.close_button.add_css_class("circular")
        self.close_button.set_tooltip_text("Close tab")

    def _init_header_box(self):
        """Initialize the tab header box."""
        self.header_box = Gtk.Box(
        orientation=Gtk.Orientation.HORIZONTAL,
        spacing=4,
        margin_start=4,
        margin_end=4
        )
        self.header_box.append(self.label_box)
        self.header_box.append(self.close_button)
        self.header_box.set_tooltip_text(self.url)
        self._setup_context_menu()

    def _setup_context_menu(self):
        """Setup right-click context menu for tab."""
        self.context_menu = Gtk.PopoverMenu()
        menu_model = Gio.Menu()
        menu_model.append("Pin Tab", "tab.pin")
        menu_model.append("Duplicate Tab", "tab.duplicate")
        menu_model.append("Close Other Tabs", "tab.close_others")
        menu_model.append("Close Tabs to Right", "tab.close_right")
        menu_model.append(None, None)
        menu_model.append("Reload Tab", "tab.reload")
        menu_model.append("Close Tab", "tab.close")
        self.context_menu.set_menu_model(menu_model)
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", self._on_tab_right_click)
        self.header_box.add_controller(gesture)

    def _on_tab_right_click(self, gesture, n_press, x, y):
        """Handle right-click on tab header."""
        if n_press == 1:
            self.context_menu.set_parent(self.header_box)
            rect = Gdk.Rectangle()
            rect.x = int(x)
            rect.y = int(y)
            rect.width = 1
            rect.height = 1
            self.context_menu.set_pointing_to(rect)
            self.context_menu.popup()

    def set_pinned(self, pinned):
        """Set the pinned state of the tab."""
        self.is_pinned = pinned
        if pinned:
            self.close_button.set_visible(False)
            self.header_box.add_css_class("pinned-tab")
        else:
            self.close_button.set_visible(True)
            self.header_box.remove_css_class("pinned-tab")

    def set_loading(self, loading):
        """Set the loading state of the tab."""
        self.loading = loading
        if loading:
            self.title_label.set_label("Loading...")
            self.header_box.add_css_class("loading-tab")
        else:
            self.header_box.remove_css_class("loading-tab")

    def cleanup(self):
        """Mark widgets as being destroyed and disconnect all signal handlers."""
        if hasattr(self, 'favicon') and self.favicon:
            self.favicon._destroying = True
        if hasattr(self, 'header_box') and self.header_box:
            self.header_box._destroying = True
        if hasattr(self, 'close_button') and self.close_button:
            self.close_button._destroying = True
        if hasattr(self, 'title_label') and self.title_label:
            self.title_label._destroying = True
        if hasattr(self, 'label_box') and self.label_box:
            self.label_box._destroying = True
        for handler_info in self._signal_handlers:
            try:
                obj, handler_id = handler_info
                if obj and hasattr(obj, 'disconnect'):
                    obj.disconnect(handler_id)
            except (AttributeError, TypeError, RuntimeError):
                pass
        self._signal_handlers.clear()
        if hasattr(self, 'webview'):
            self.webview = None
        if hasattr(self, 'scrolled_window'):
            self.scrolled_window = None

    def track_signal_handler(self, obj, handler_id):
        """Track a signal handler for cleanup."""
        self._signal_handlers.append((obj, handler_id))

    def is_widget_valid(self):
        """Check if tab widgets are still valid and not being destroyed."""
        return (hasattr(self, 'favicon') and self.favicon and
        not hasattr(self.favicon, '_destroying') and
        hasattr(self, 'header_box') and self.header_box and
        not hasattr(self.header_box, '_destroying'))

    def update_thumbnail(self):
        """Capture and store a thumbnail of the current tab."""
        if self.webview:
            def on_snapshot_complete(webview, result):
                try:
                    snapshot = webview.get_snapshot_finish(result)
                    if snapshot:
                        self.thumbnail = snapshot
                except Exception:
                    self.thumbnail = None
            try:
                self.webview.get_snapshot(
                    WebKit.SnapshotRegion.VISIBLE,
                    WebKit.SnapshotOptions.NONE,
                    None,
                    on_snapshot_complete
                )
            except Exception:
                self.thumbnail = None

    def set_group(self, group_name):
        """Set the group for this tab."""
        self.group_name = group_name
        self.is_grouped = bool(group_name)
        for child in self.label_box:
            if hasattr(child, 'get_css_classes') and 'group-indicator' in child.get_css_classes():
                self.label_box.remove(child)
        if group_name:
            self.header_box.add_css_class("grouped-tab")
            group_label = Gtk.Label(label=group_name)
            group_label.add_css_class("group-indicator")
            self.label_box.insert_child_after(group_label, self.favicon)
        else:
            self.header_box.remove_css_class("grouped-tab")
        self.label_box.remove(child)

class SystemWakeLock:
    def __init__(self, app_id="shadow-browser", reason="Browser is running"):
        self._inhibit_cookie = None
        self._dbus_inhibit = None
        self._inhibit_method = None
        self._app_id = app_id
        self._reason = reason
        self._setup_inhibit()
        os.environ["WEBKIT_DISABLE_DBUS_INHIBIT"] = "1"

    def _setup_inhibit(self):
        """Set up the appropriate inhibition method for Linux."""
        try:
            import dbus
            bus = dbus.SessionBus()
            try:
                proxy = bus.get_object('org.freedesktop.ScreenSaver',
                                    '/org/freedesktop/ScreenSaver')
                self._inhibit_method = proxy.get_dbus_method('Inhibit', 'org.freedesktop.ScreenSaver')
                self._uninhibit_method = proxy.get_dbus_method('UnInhibit', 'org.freedesktop.ScreenSaver')
                self._dbus_inhibit = True
                return
            except dbus.exceptions.DBusException:
                pass
            try:
                portal = bus.get_object('org.freedesktop.portal.Desktop',
                                     '/org/freedesktop/portal/desktop')
                self._inhibit_method = portal.get_dbus_method('Inhibit', 'org.freedesktop.portal.Inhibit')
                self._dbus_inhibit = True
                return
            except dbus.exceptions.DBusException:
                pass
            self._dbus_inhibit = False
        except Exception:
            self._dbus_inhibit = False

    def inhibit(self):
        """Prevent system sleep/screensaver on Linux."""
        if self._dbus_inhibit and not self._inhibit_cookie:
            try:
                if hasattr(self, '_uninhibit_method'):
                    self._inhibit_cookie = self._inhibit_method(self._app_id, self._reason)
                else:
                    flags = 4
                    options = {
                        'reason': dbus.String(self._reason, variant_level=1),
                        'app_id': dbus.String(self._app_id, variant_level=1)
                    }
                    self._inhibit_cookie = self._inhibit_method(
                        'x11:0',
                        flags,
                        options
                    )
            except Exception:
                self._inhibit_cookie = None

    def uninhibit(self):
        """Allow system sleep/screensaver again."""
        if self._dbus_inhibit and self._inhibit_cookie is not None:
            try:
                if hasattr(self, '_uninhibit_method'):
                    self._uninhibit_method(self._inhibit_cookie)
                else:
                    try:
                        import dbus
                        bus = dbus.SessionBus()
                        request = bus.get_object("org.freedesktop.portal.Desktop", self._inhibit_cookie)
                        request.Close(dbus_interface="org.freedesktop.portal.Request")
                    except Exception:
                        return False
                    self._inhibit_cookie = None
                    return True
            except Exception:
                return False
        self._inhibit_cookie = None

    def handle_debug_signal(signum, frame):
        """Handle debug signals and print stack traces."""
        print(f"\n=== Received signal {signum} ({signal.Signals(signum).name}) ===")
        print("Stack trace:")
        traceback.print_stack(frame)
        print("\nContinuing execution...")
    signal.signal(signal.SIGTRAP, handle_debug_signal)
    signal.signal(signal.SIGUSR1, handle_debug_signal)

class DownloadManager:
    """Manages file downloads in the browser."""

    def __init__(self, parent):
        """Initialize the download manager.

        Args:
            parent: The parent window for showing dialogs
        """
        self.parent = parent
        self.active_downloads = {}
        self.downloads = {}
        self.lock = threading.Lock()
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.set_visible(False)
        self.download_dir = GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_DOWNLOAD
        ) or os.path.expanduser("~/Downloads")
        os.makedirs(self.download_dir, exist_ok=True)
        self.on_download_start_callback = None

    def register_web_context(self, context):
        """Register a WebKit.WebContext with the download manager.
        Args:
            context: The WebKit.WebContext to register
        """
        pass

    def add_webview(self, webview):
        """Connect download signals to a webview.
        Args:
            webview: The WebKit.WebView instance to connect to
        """
        webview.connect('decide-policy', self.on_decide_policy)

    def on_decide_policy(self, webview, decision, decision_type, *args):
        """Handle policy decisions including downloads."""
        if decision_type == WebKit.PolicyDecisionType.RESPONSE:
            response = decision.get_response()
            if response and ('application/octet-stream' in response.get_mime_type() or
                           'application/force-download' in response.get_mime_type() or
                           'attachment' in response.get_http_headers()):
                download = webview.download_uri(response.get_uri())
                if download:
                    return self.on_download_started(webview.get_context() if hasattr(webview, 'get_context') else None, download)
        return False

    def on_download_started(self, context, download):
        """Handle download started event."""
        try:
            if self.on_download_start_callback:
                self.on_download_start_callback()
            request = download.get_request()
            if not request:
                return False
            uri = request.get_uri()
            if not uri:
                return False
            os.makedirs(self.download_dir, exist_ok=True)
            filename = os.path.basename(uri)
            if not filename or filename == '/':
                filename = f"download_{int(time.time())}"
            base_name, ext = os.path.splitext(filename)
            counter = 1
            filepath = os.path.join(self.download_dir, filename)
            while os.path.exists(filepath):
                filename = f"{base_name}_{counter}{ext}"
                filepath = os.path.join(self.download_dir, filename)
                counter += 1
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
            download.connect("notify::estimated-progress", self.on_download_progress)
            download.connect("notify::status", self.on_download_status_changed)
            self.show()
            download.set_destination(f"file://{filepath}")
            return True
        except Exception as e:
            logger.error(f"Error in on_download_started: {e}", exc_info=True)
            return False

    def safe_append(self, container, widget):
        """Safely append a widget to a container."""
        if container and hasattr(container, 'add') and not widget.get_parent():
            container.add(widget)
            widget.show()
            return True
        return False

    def show(self):
        """Show the downloads area."""
        if self.box and not self.box.get_visible():
            self.box.set_visible(True)
            if hasattr(self.parent, 'pack_start'):
                self.parent.pack_start(self.box, False, True, 0)
                self.box.show_all()

    def on_download_progress(self, download, param):
        """Update download progress."""
        with self.lock:
            if download in self.downloads:
                received = download.get_received_data_length()
                total = download.get_total_data_length()
                if total > 0:
                    progress = received / total
                    self.downloads[download]["progress"].set_fraction(progress)

    def on_download_status_changed(self, download, param):
        """Handle download status changes."""
        with self.lock:
            if download in self.downloads:
                download_info = self.downloads[download]
                status = download.get_status()
                if status == WebKit.DownloadStatus.FINISHED:
                    download_info["status"] = "Completed"
                    download_info["label"].set_label(f"Downloaded: {os.path.basename(download_info['filepath'])}")
                    download_info["progress"].set_fraction(1.0)
                elif status in [WebKit.DownloadStatus.FAILED, WebKit.DownloadStatus.CANCELLED]:
                    error = download.get_error()
                    if error:
                        error_msg = error.message
                    else:
                        error_msg = "Unknown error"
                    download_info["status"] = f"Failed: {error_msg}"
                    download_info["label"].set_label(download_info["status"])
                    dialog = Gtk.MessageDialog(
                        transient_for=self.parent,
                        flags=0,
                        message_type=Gtk.MessageType.ERROR,
                        buttons=Gtk.ButtonsType.CLOSE,
                        text="Download Failed"
                    )
                    dialog.format_secondary_text(f"Failed to download {os.path.basename(download_info['filepath'])}: {error_msg}")
                    dialog.run()
                    dialog.destroy()

    def _download_thread(self, download_id, uri, file_path):
        """Background thread for downloading files."""
        try:
            response = requests.get(
                uri,
                stream=True,
                allow_redirects=True,
                timeout=30
            )
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            self.active_downloads[download_id]['total_bytes'] = total_size
            downloaded = 0
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.active_downloads[download_id]['downloaded_bytes'] = downloaded
            self.active_downloads[download_id]['status'] = 'completed'
            if self.parent:
                GLib.idle_add(
                    self._show_download_complete,
                    os.path.basename(file_path),
                    file_path
                )
        except Exception as e:
            self.active_downloads[download_id]['status'] = 'error'
            self.active_downloads[download_id]['error'] = str(e)
            if self.parent:
                GLib.idle_add(
                    self._show_download_error,
                    os.path.basename(file_path),
                    str(e)
                )

    def _show_download_complete(self, filename, file_path):
        """Show a notification when a download completes."""
        dialog = Gtk.MessageDialog(
            transient_for=self.parent,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            message_format="Download Complete"
        )
        dialog.format_secondary_text(f"{filename} has been saved to {file_path}")
        dialog.run()
        dialog.destroy()

    def _show_download_error(self, filename, error):
        """Show an error message when a download fails."""
        dialog = Gtk.MessageDialog(
            transient_for=self.parent,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CANCEL,
            message_format="Download Failed"
        )
        dialog.format_secondary_text(f"Failed to download {filename}: {error}")
        dialog.run()
        dialog.destroy()


class ShadowBrowser(Gtk.Application):
    def _periodic_cleanup(self):
        """Periodic cleanup of resources."""
        with self._lock:
            if self._shutdown:
                return False
            try:
                self._cleanup_favicon_cache()
                if hasattr(self, '_cleanup_thumbnails'):
                    self._cleanup_thumbnails()
                self._save_state()
            except Exception as e:
                logger.warning("Periodic cleanup failed: %s", str(e), exc_info=True)
                return False
            return True

    def _save_state(self):
        """Save browser state in a thread-safe manner."""
        with self._lock:
            try:
                self._save_json_thread_safe(BOOKMARKS_FILE, self._bookmarks)
                self._save_json_thread_safe(HISTORY_FILE, self._history)
                self._save_favicon_cache()
            except Exception as e:
                logger.error("Failed to save browser state: %s", str(e), exc_info=True)

    def _load_json_thread_safe(self, filename, default=None):
        """Thread-safe JSON loading with error handling."""
        if default is None:
            default = {}
        try:
            if os.path.exists(filename):
                with open(filename, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logger.warning("Failed to load %s: %s", filename, str(e))
        return default

    def _save_json_thread_safe(self, filename, data):
        """Thread-safe JSON saving with error handling."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
            temp_filename = f"{filename}.tmp"
            with open(temp_filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_filename, filename)
        except (IOError, OSError) as e:
            logger.warning("Failed to save %s: %s", filename, str(e))

    def _cleanup_favicon_cache(self):
        """Clean up old favicons from cache."""
        with self._favicon_lock:
            if not os.path.exists(self._favicon_cache_file):
                return
            try:
                current_urls = set()
                for bookmark in self._bookmarks:
                    if isinstance(bookmark, dict):
                        url = bookmark.get('url', '')
                    else:
                        url = str(bookmark)
                    if url:
                        current_urls.add(url)
                for h in self._history:
                    if isinstance(h, dict):
                        url = h.get('url', '')
                    else:
                        url = str(h)
                    if url:
                        current_urls.add(url)
                removed = 0
                for url in list(self._favicon_cache.keys()):
                    if url not in current_urls:
                        del self._favicon_cache[url]
                        removed += 1
                if removed > 0:
                    self._save_favicon_cache()
            except Exception as e:
                logger.warning("Failed to clean up favicon cache: %s", str(e))

    def _save_favicon_cache(self):
        """Save favicon cache to disk."""
        with self._favicon_lock:
            try:
                cache_data = {}
                for domain, texture in self._favicon_cache.items():
                    serialized = self._serialize_texture(texture)
                    if serialized:
                        cache_data[domain] = serialized
                os.makedirs(os.path.dirname(self._favicon_cache_file), exist_ok=True)
                with open(self._favicon_cache_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, indent=2)
            except Exception as e:
                logger.error("Failed to save favicon cache: %s", str(e))

    def cleanup(self):
        """Clean up resources before shutdown."""
        if self._shutdown:
            return
        self._shutdown = True
        try:
            self._save_state()
        except Exception as e:
            logger.error("Error during cleanup: %s", str(e))

    def __del__(self):
        """Destructor to ensure cleanup."""
        self.cleanup()

    @property
    def home_url(self):
        with self._lock:
            return self._home_url

    @home_url.setter
    def home_url(self, value):
        with self._lock:
            self._home_url = value

    @property
    def theme(self):
        with self._lock:
            return self._theme

    @theme.setter
    def theme(self, value):
        with self._lock:
            self._theme = value

    @property
    def active_downloads(self):
        with self._lock:
            return self._active_downloads

    @active_downloads.setter
    def active_downloads(self, value):
        with self._lock:
            self._active_downloads = value
        GLib.idle_add(self._update_downloads_ui)

    @property
    def tor_enabled(self):
        with self._lock:
            return self._tor_enabled

    @tor_enabled.setter
    def tor_enabled(self, value):
        with self._lock:
            self._tor_enabled = value

    def _update_downloads_ui(self):
        """Update downloads UI in the main thread."""
        if self._shutdown:
            return
        try:
            if self.active_downloads > 0:
                self.download_count_label.set_text(str(self.active_downloads))
                self.download_count_label.set_visible(True)
                self.download_spinner.set_visible(True)
                self.download_status_label.set_visible(True)
                self.download_box.set_visible(True)
                self.download_spinner.start()
            else:
                self.download_count_label.set_visible(False)
                self.download_spinner.set_visible(False)
                self.download_status_label.set_visible(False)
                self.download_box.set_visible(False)
                self.download_spinner.stop()
        except Exception as e:
            logger.error("Error updating downloads UI: %s", str(e))

    def _on_download_update(self, download_info):
        """Thread-safe callback for download updates."""
        if self._shutdown:
            return
        with self._lock:
            try:
                if download_info.get('status') == 'started':
                    self.active_downloads += 1
                elif download_info.get('status') in ['completed', 'failed', 'cancelled']:
                    self.active_downloads = max(0, self.active_downloads - 1)
            except Exception as e:
                logger.error("Error in download update: %s", str(e))
    def __init__(self):
        os.environ["WEBKIT_DISABLE_DBUS_INHIBIT"] = "1"
        os.environ["WEBKIT_DISABLE_MPRIS"] = "1"
        os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1"
        os.environ["WEBKIT_USE_SINGLE_WEB_PROCESS"] = "1"
        os.environ["GDK_SCALE"] = "1"
        os.environ["WEBKIT_DISABLE_BACKGROUND_THROTTLING"] = "1"
        os.environ["WEBKIT_DISABLE_ACCELERATED_2D_CANVAS"] = "1"
        initial_tor_enabled = False
        self._tor_enabled = False
        self._tor_toggle_updating = False
        self.tor_manager = TorManager()
        self.tor_menu_toggle = None
        try:
            initial_tor_enabled = bool(self.tor_manager.enable_tor_proxy())
        except Exception as exc:
            print(f"Warning: Failed to pre-enable Tor proxy: {exc}")
        self._tor_enabled = initial_tor_enabled
        Gtk.init()
        super().__init__(application_id="com.shadowyfigure.shadowbrowser")
        self.debug_mode = True
        self.wake_lock = SystemWakeLock()
        self.wake_lock_active = False
        self._shutdown = False
        self._lock = threading.RLock()
        self.tor_enabled = initial_tor_enabled
        self._favicon_fetch_in_progress = set()
        self._favicon_lock = threading.Lock()
        self._favicon_cache = {}
        self._favicon_cache_file = os.path.join(os.path.expanduser('~'), '.cache', 'shadowbrowser', 'favicon_cache.json')
        self.tabs_lock = threading.RLock()
        self._bookmarks = self._load_json_thread_safe(BOOKMARKS_FILE)
        self._history = self._load_json_thread_safe(HISTORY_FILE)
        self._tabs = []
        self._tab_groups = {}
        self._pinned_tabs = []
        self.bookmarks = self._bookmarks
        self.history = self._history
        self.tabs = self._tabs
        self.tab_groups = self._tab_groups
        self.pinned_tabs = self._pinned_tabs
        self.tab_search_visible = False
        self.tab_search_entry = None
        self.tab_search_results = []
        self.blocked_urls = []
        self.window = None
        self.notebook = Gtk.Notebook()
        self.url_entry = Gtk.Entry()
        self._home_url = "https://duckduckgo.com/"
        self._theme = "dark"
        self._active_downloads = 0
        self.download_manager = DownloadManager(None)
        self.context = ssl.create_default_context()
        self.context.verify_mode = ssl.CERT_REQUIRED
        self.context.check_hostname = True
        self.error_handlers = {}
        self.register_error_handlers()
        try:
            self.webview = WebKit.WebView()
            self.content_manager = self.webview.get_user_content_manager()
            self.settings = WebKit.Settings()
            self.social_tracker_blocker = SocialTrackerBlocker()
            self.adblocker = AdBlocker()
            self._configure_webview_security()
            self.webview.connect("create", self.on_webview_create)
            self.webview.connect("notify::favicon", self.on_favicon_changed)
            if self.download_manager:
                self.download_manager.add_webview(self.webview)
            context = WebKit.WebContext.get_default()
            if context and self.download_manager:
                self.download_manager.register_web_context(context)
                if hasattr(context, 'set_proxy_settings'):
                    print(f"DEBUG: WebKit.ProxyMode.CUSTOM exists: {hasattr(WebKit, 'ProxyMode') and hasattr(WebKit.ProxyMode, 'CUSTOM')}")
                    print(f"DEBUG: WebKit.ProxySettings exists: {hasattr(WebKit, 'ProxySettings')}")
                    if hasattr(WebKit, 'ProxySettings'):
                        print(f"DEBUG: ProxySettings.new exists: {hasattr(WebKit.ProxySettings, 'new')}")
        except Exception as e:
            logger.critical("Failed to initialize WebKit components: %s", str(e))
            self.cleanup()
            raise RuntimeError(f"Failed to initialize browser: {str(e)}") from e
        self.loading_spinner = Gtk.Spinner()
        self.loading_spinner.set_visible(False)
        self.bookmark_menu = None
        atexit.register(self.cleanup)
        GLib.timeout_add_seconds(300, self._periodic_cleanup)
        self._favicon_cache = {}
        self._favicon_cache_file = os.path.join(os.path.expanduser('~'), '.cache', 'shadowbrowser', 'favicon_cache.json')
        self._favicon_lock = threading.Lock()
        self._load_favicon_cache()
        os.makedirs(os.path.dirname(self._favicon_cache_file), exist_ok=True)
        self.default_favicon = self._create_default_favicon()
        self._favicon_fallbacks = {}
        self._favicon_retry_counts = {}
        self._favicon_max_retries = 3
        self._favicon_cleanup_timer = None
        self.thumbnail_update_interval = 30000
        self.last_thumbnail_update = 0
        GLib.timeout_add(self.thumbnail_update_interval, self.update_all_thumbnails)
        try:
            self.inject_nonce_respecting_script()
            self.inject_remove_malicious_links()
            self.inject_adware_cleaner()
            if not hasattr(self, 'webview'):
                self.webview = WebKit.WebView()
                self.setup_webview_settings(self.webview)
                self.disable_biometrics_in_webview(self.webview)
                self.content_manager.register_script_message_handler("voidLinkClicked")
                self.content_manager.connect(
                    "script-message-received::voidLinkClicked", self.on_void_link_clicked
                )
                self.inject_tcf_api(self.content_manager)
                self.inject_js_execution_fix(self.content_manager)
                test_script = WebKit.UserScript.new(
                    "console.log('Test script injected into shared content manager');",
                    WebKit.UserContentInjectedFrames.ALL_FRAMES,
                    WebKit.UserScriptInjectionTime.START,
                )
                self.content_manager.add_script(test_script)
        except Exception as e:
            logger.error("Error during browser initialization: %s", str(e), exc_info=True)
            self.cleanup()
            raise

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

    def _configure_webview_security(self):
        """Configure WebView security settings."""
        try:
            self.adblocker.enable()
            self.setup_webview_settings(self.webview)
            self._create_secure_web_context()
            self.settings.set_property('enable_javascript', True)
            self.settings.set_property('enable_media_stream', False)
            self.settings.set_property('enable_webaudio', False)
            self.settings.set_property('enable_webgl', False)
        except Exception as e:
            logger.error("Failed to configure WebView security: %s", str(e))
            raise

    def _create_secure_web_context(self):
        """Create and configure a secure WebKit WebContext with persistent storage."""
        try:
            base_dir = os.path.expanduser("~/.shadowbrowser")
            data_dir = os.path.join(base_dir, "data")
            for directory in [base_dir, data_dir]:
                try:
                    Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
                except Exception as e:
                    logger.warning("Failed to create directory %s: %s", directory, str(e))
                    raise
            context = WebKit.WebContext()
            if hasattr(context, 'set_tls_errors_policy'):
                context.set_tls_errors_policy(WebKit.TLSErrorsPolicy.FAIL)
            if hasattr(context, 'set_web_security_enabled'):
                context.set_web_security_enabled(True)
            if hasattr(context, 'set_allow_universal_access_from_file_urls'):
                context.set_allow_universal_access_from_file_urls(False)
            if hasattr(context, 'set_allow_file_access_from_file_urls'):
                context.set_allow_file_access_from_file_urls(False)
            if hasattr(context, 'set_website_data_manager'):
                manager = WebKit.WebsiteDataManager.new(
                    base_data_directory=data_dir,
                    base_cache_directory=os.path.join(data_dir, 'cache')
                )
                context.set_website_data_manager(manager)
            self._global_csp = (
                "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' https: http: data: blob:; "
                "style-src 'self' 'unsafe-inline' https: http: data:; "
                "img-src 'self' data: https: http: blob:; "
                "connect-src 'self' https: http: wss: ws: blob:; "
                "media-src 'self' https: http: blob:; "
                "object-src 'none'; "
            )
            if hasattr(self.content_manager, 'add_content_filter'):
                csp_filter = WebKit.UserContentFilter(
                    self._global_csp,
                    WebKit.UserContentInjectedFrames.ALL_FRAMES,
                    WebKit.UserScriptInjectionTime.START
                )
                self.content_manager.add_content_filter(csp_filter)
            return context
        except Exception as e:
            logger.error("Failed to create secure WebContext: %s", str(e))
            raise

    def _setup_security_violation_handling(self, webview):
        """Set up handlers for security policy violations."""
        webview.connect("script-dialog", self._on_script_dialog)
        webview.connect("insecure-content-detected", self._on_insecure_content)
        webview.connect("permission-request", self._on_permission_request)

    def _on_script_dialog(self, webview, dialog):
        """Handle script dialogs, including CSP violations."""
        try:
            if dialog.get_dialog_type() == WebKit.ScriptDialogType.ALERT:
                message = dialog.get_message()
                if "Content Security Policy" in message or "CSP" in message:
                    logging.warning(f"CSP Violation: {message}")
                    dialog.close()
                    return True
        except Exception as e:
            logger.error("Error handling script dialog: %s", str(e))
            return False

    def _on_insecure_content(self, webview, event):
        """Handle insecure content detection."""
        if event.get_insecure_content_event() == WebKit.InsecureContentEvent.RUN:
            return True
        return True

    def _on_permission_request(self, webview: WebKit.WebView, request: WebKit.PermissionRequest) -> bool:
        """Handle permission requests with media-friendly defaults."""
        permission = request.get_permission()
        if permission in [
            WebKit.PermissionRequestType.USER_MEDIA,
            WebKit.PermissionRequestType.MEDIA_KEY_SYSTEM_ACCESS,
            WebKit.PermissionRequestType.DEVICE_POLICY,
        ]:
            request.allow()
            return True
        if permission in [
            WebKit.PermissionRequestType.GEOLOCATION,
            WebKit.PermissionRequestType.MIDI_SYSEX,
            WebKit.PermissionRequestType.NOTIFICATIONS
        ]:
            request.deny()
            return True
        request.deny()
        return False

    def _on_resource_load_started(self, webview: WebKit.WebView, resource, request, response=None) -> None:
        """Intercept and secure resource loading."""
        uri = request.get_uri()
        if not uri:
            return
        if uri.startswith(('javascript:', 'vbscript:')):
            request.set_uri('about:blank')
            return
        if uri.startswith('data:'):
            if any(safe_type in uri.lower() for safe_type in ['image/', 'font/', 'text/css']):
                return
            request.set_uri('about:blank')
            return
        if self._should_upgrade_to_https(uri):
            secure_uri = self._upgrade_to_https(uri)
            request.set_uri(secure_uri)
            return
        if response:
            headers = response.get_http_headers()
            if headers:
                headers = self._sanitize_headers(dict(headers))
                response.set_http_headers(headers)

    def _load_favicon_cache(self):
        """Load favicon cache from disk."""
        if os.path.exists(self._favicon_cache_file):
            with open(self._favicon_cache_file, 'r', encoding='utf-8') as f:
                try:
                    cache_data = json.load(f)
                except json.JSONDecodeError:
                    logger.warning("Favicon cache file is corrupted; starting fresh")
                    return
                for domain, data in cache_data.items():
                    try:
                        pixels_hex = data.get('pixels')
                        width = int(data.get('width', 0))
                        height = int(data.get('height', 0))
                        if not pixels_hex or not width or not height:
                            continue
                        rowstride = int(data.get('rowstride', width * 4))
                        pixel_bytes = bytes.fromhex(pixels_hex)
                        if len(pixel_bytes) < rowstride * height:
                            continue
                        byte_data = GLib.Bytes.new(pixel_bytes)
                        import warnings
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", DeprecationWarning)
                            pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                                byte_data,
                                GdkPixbuf.Colorspace.RGB,
                                True,
                                8,
                                width,
                                height,
                                rowstride
                            )
                            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                        self._attach_pixbuf_metadata(texture, pixbuf)
                        self._favicon_cache[domain] = texture
                    except Exception:
                        continue

    def _serialize_texture(self, texture):
        """Serialize a Gdk.Texture into JSON-safe data."""
        if not texture:
            return None
        try:
            pixel_bytes = getattr(texture, '_pixel_data', None)
            width = getattr(texture, '_pixel_width', None)
            height = getattr(texture, '_pixel_height', None)
            rowstride = getattr(texture, '_pixel_rowstride', None)
            if not pixel_bytes or width is None or height is None:
                return None
            if rowstride is None:
                rowstride = width * 4
            return {
                'pixels': pixel_bytes.hex(),
                'width': int(width),
                'height': int(height),
                'rowstride': int(rowstride)
            }
        except Exception:
            return None

    def _texture_from_pixbuf(self, pixbuf):
        """Convert a pixbuf to a texture while preserving pixel metadata."""
        if not pixbuf:
            return None
        try:
            if hasattr(pixbuf, 'get_texture'):
                texture = pixbuf.get_texture()
            else:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        except Exception:
            return None
        self._attach_pixbuf_metadata(texture, pixbuf)
        return texture

    def _attach_pixbuf_metadata(self, texture, pixbuf):
        """Attach pixel metadata to a texture for cache persistence."""
        if not texture or not pixbuf:
            return
        try:
            pixel_bytes = bytes(pixbuf.get_pixels())
            texture._pixel_data = pixel_bytes
            texture._pixel_width = pixbuf.get_width()
            texture._pixel_height = pixbuf.get_height()
            texture._pixel_rowstride = pixbuf.get_rowstride()
        except Exception:
            pass

    def _should_upgrade_to_https(self, url):
        """Check if a URL should be upgraded to HTTPS.
        Args:
        url: The URL to check
        Returns:
        bool: True if the URL should be upgraded to HTTPS
        """
        if not url or not isinstance(url, str):
            return False
        return url.lower().startswith('http://') and not url.lower().startswith('https://')

    def _upgrade_to_https(self, url):
        """Upgrade an HTTP URL to HTTPS.
        Args:
        url: The URL to upgrade
        Returns:
        str: The upgraded URL
        """
        if not self._should_upgrade_to_https(url):
            return url
        return 'https://' + url[7:]

    def _sanitize_headers(self, headers):
        """Sanitize HTTP headers before they're sent to WebKit.
        Args:
        headers: Dictionary of headers to sanitize
        Returns:
        dict: Sanitized headers
        """
        if not headers:
            headers = {}
        deprecated_headers = [
            'X-WebKit-CSP',
            'X-Content-Security-Policy',
            'X-WebKit-CSP-Report-Only',
            'Public-Key-Pins',
            'X-Content-Security-Policy-Report-Only'
        ]
        for header in deprecated_headers:
            if header in headers:
                del headers[header]
        if 'Content-Security-Policy' in headers:
            csp = headers['Content-Security-Policy']
            headers['Content-Security-Policy'] = self._sanitize_csp_policy(csp)

        security_headers = {
            'X-XSS-Protection': '1; mode=block',
            'Referrer-Policy': 'strict-origin-when-cross-origin',
            'X-Frame-Options': 'SAMEORIGIN'
        }
        for header, value in security_headers.items():
            if header not in headers:
                headers[header] = value
        return headers

    def _sanitize_csp_policy(self, csp_policy):
        """Sanitize Content Security Policy string.
        Args:
        csp_policy: The CSP policy string to sanitize
        Returns:
        str: Sanitized CSP policy
        """
        if not csp_policy:
            return ""
        import re
        directives = {}
        for directive in csp_policy.split(';'):
            directive = directive.strip()
            if not directive:
                continue
            if ' ' in directive:
                name, values = directive.split(' ', 1)
                directives[name.strip()] = values.strip()
        if 'script-src' in directives:
            script_src = directives['script-src']
            if 'https:' not in script_src:
                script_src += ' https:'
            if 'http:' not in script_src:
                script_src += ' http:'
            if 'data:' not in script_src:
                script_src += ' data:'
            if 'blob:' not in script_src:
                script_src += ' blob:'
            directives['script-src'] = script_src
        else:
            directives['script-src'] = "'self' 'unsafe-inline' 'unsafe-eval' https: http: data: blob:"
        if 'style-src' in directives:
            style_src = directives['style-src']
            if 'https:' not in style_src:
                style_src += ' https:'
            if 'http:' not in style_src:
                style_src += ' http:'
            if 'data:' not in style_src:
                style_src += ' data:'
            directives['style-src'] = style_src
        else:
            directives['style-src'] = "'self' 'unsafe-inline' https: http: data:"
        if 'font-src' in directives:
            font_src = directives['font-src']
            if 'https:' not in font_src:
                font_src += ' https:'
            if 'http:' not in font_src:
                font_src += ' http:'
            if 'data:' not in font_src:
                font_src += ' data:'
            directives['font-src'] = font_src
        else:
            directives['font-src'] = "'self' https: http: data:"
        if 'media-src' not in directives:
            directives['media-src'] = "'self' https: http: data: blob:"
        if 'img-src' not in directives:
            directives['img-src'] = "'self' https: http: data: blob:"
        directives_to_remove = [
            r"\bmanifest-src[^;]*;?",
            r"require-trusted-types-for[^;]*;?",
            r"trusted-types[^;]*;?"
        ]
        sanitized_parts = []
        for name, values in directives.items():
            if not any(re.match(pattern, name) for pattern in directives_to_remove):
                sanitized_parts.append(f"{name} {values}")
        return '; '.join(sanitized_parts)

    def _enforce_frame_ancestors(self, webview, frame_ancestors=None):
        """Enforce frame-ancestors CSP policy to prevent clickjacking."""
        csp_script = """
        (function() {
        try {
        var meta = document.createElement('meta');
        meta.httpEquiv = 'Content-Security-Policy';
        meta.content = "frame-ancestors 'none';";
        var head = document.head || document.getElementsByTagName('head')[0];
        if (head) {
        head.appendChild(meta);
        console.log('Frame-ancestors CSP applied: frame-ancestors \\'none\\';');
        }
        } catch (e) {
        console.error('Failed to apply frame-ancestors CSP:', e);
        }
        })();
        """
        script = WebKit.UserScript.new(
        csp_script,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        None,
        None
        )
        webview.get_user_content_manager().add_script(script)

    def setup_webview_proxy(self, webview):
        """Set up proxy settings for a WebView based on current Tor status."""
        if not hasattr(self, 'tor_enabled') or not self.tor_enabled:
            return

        try:
            context = webview.get_context()
            if context and hasattr(context, 'set_network_proxy_settings'):
                proxy_settings = WebKit.NetworkProxySettings()
                proxy_settings.add_proxy_for_scheme('http', 'socks5h://127.0.0.1:9050')
                proxy_settings.add_proxy_for_scheme('https', 'socks5h://127.0.0.1:9050')
                context.set_network_proxy_settings(
                    WebKit.NetworkProxyMode.CUSTOM,
                    proxy_settings
                )
                print("WebView proxy configured for Tor")
        except Exception as e:
            print(f"Error setting up WebView proxy: {e}")

    def create_secure_webview(self):
        """
        Create a new secure WebView with all necessary scripts and handlers.
        Returns:
        WebKit.WebView: A configured WebView instance or None if creation fails
        """
        try:
            webview = WebKit.WebView()
            if not webview:
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
        except Exception:
            self.show_error_message("Error creating WebView")
            return None

    def cleanup_webview(self, webview):
        """
        Clean up resources used by a WebView.
        Args:
        webview: The WebView to clean up
        """
        if not webview:
            return
        for handler_id in getattr(webview, '_signal_handlers', []):
            try:
                webview.handler_disconnect(handler_id)
            except (AttributeError, TypeError, RuntimeError):
                pass
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
                            except (AttributeError, TypeError, RuntimeError):
                                pass
                    del webview._handler_ids
                    del webview._content_manager
            except (AttributeError, TypeError, RuntimeError):
                pass
        webview.load_uri('about:blank')
        if hasattr(webview, 'stop_loading'):
            webview.stop_loading()
        if hasattr(webview, 'load_html_string'):
            webview.load_html_string('', 'about:blank')
        parent = webview.get_parent()
        if parent:
            parent.remove(webview)

    def inject_wau_tracker_removal_script(self):
        wau_removal_script = WebKit.UserScript.new(
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
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        )
        self.content_manager.add_script(wau_removal_script)

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

    def block_social_trackers(self, webview, decision, decision_type):
        """Block social media trackers."""
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
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
        Constructs a streaming URL with the given UUID.
        """
        if not id_string or not isinstance(id_string, str):
            return "https://streamtape.com"
        base_url = "https://streamtape.com"
        return f"{base_url}/embed/{id_string}/"

    def inject_window_open_handler(self, content_manager):
        """Inject JS to override window.open and send URLs to Python for new tab opening."""
        js_code = '''
        (function() {
        console.log('[ShadowBrowser] Injecting window.open override');
        const originalOpen = window.open;
        window.open = function(url, name, features) {
        console.log('[ShadowBrowser] window.open called with:', url, name, features);
        // Enhanced popup detection
        const isLikelyPopup = (url, name, features) => {
        // Check if URL is blocked by adblocker first
        if (typeof isUrlBlocked === 'function' && isUrlBlocked(url)) {
        console.log('[ShadowBrowser] window.open blocked by adblocker:', url);
        return true;
        }
        // Check for suspicious popup features
        if (features) {
        const suspiciousFeatures = [
        'width=', 'height=', 'resizable=no', 'scrollbars=no',
        'toolbar=no', 'menubar=no', 'location=no', 'status=no',
        'left=', 'top=', 'alwaysRaised=yes', 'dependent=yes'
        ];
        if (suspiciousFeatures.some(f => features.includes(f))) {
        console.log('[ShadowBrowser] Suspicious popup features detected:', features);
        return true;
        }
        }
        // Check for common popup domains
        if (url && typeof url === 'string') {
        const popupDomains = [
        'doubleclick.net', 'googleadservices.com', 'googlesyndication.com',
        'facebook.com/tr', 'amazon-adsystem.com', 'outbrain.com', 'taboola.com',
        'adnxs.com', 'ads.yahoo.com', 'scorecardresearch.com', 'quantserve.com'
        ];
        const urlLower = url.toLowerCase();
        if (popupDomains.some(domain => urlLower.includes(domain))) {
        console.log('[ShadowBrowser] Blocked ad domain popup:', url);
        return true;
        }
        // Check for popup-like URL patterns
        const popupPatterns = [
        '/popup/', '/modal/', '/overlay/', '/interstitial/',
        'popup.', 'modal.', 'overlay.', 'interstitial.',
        '?popup=', '?modal=', '?overlay=', '?interstitial='
        ];
        if (popupPatterns.some(pattern => urlLower.includes(pattern))) {
        console.log('[ShadowBrowser] Blocked popup pattern URL:', url);
        return true;
        }
        }
        // Check for suspicious window names
        if (name && typeof name === 'string') {
        const suspiciousNames = ['popup', 'modal', 'overlay', 'ad', 'banner', 'sponsor'];
        if (suspiciousNames.some(susp => name.toLowerCase().includes(susp))) {
        console.log('[ShadowBrowser] Blocked popup with suspicious name:', name);
        return true;
        }
        }
        return false;
        };
        // Block if it's likely a popup
        if (isLikelyPopup(url, name, features)) {
        console.log('[ShadowBrowser] Popup blocked:', url);
        return null;
        }
        // Always send a string to Python, even if url is undefined/null
        var urlToSend = (typeof url === 'string' && url) ? url : '';
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.windowOpenHandler) {
        window.webkit.messageHandlers.windowOpenHandler.postMessage({
        url: urlToSend,
        name: name || '',
        features: features || ''
        });
        }
        return null;
        };
        })();
        '''
        content_manager.add_script(
        WebKit.UserScript.new(
        js_code,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        )
        )

    def _register_webview_message_handlers(self, webview):
        content_manager = webview.get_user_content_manager()
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

    def on_window_open_handler(self, user_content_manager, js_message):
        """Handle window.open JS calls and open the URL in a new tab."""
        data = js_message.get_js_value() if hasattr(js_message, 'get_js_value') else js_message
        url = None
        if isinstance(data, dict):
            url = data.get('url')
        elif isinstance(data, str):
            url = data
        if url is None:
            pass
        elif not isinstance(url, str):
            url = str(url)
        url = url.strip() if isinstance(url, str) else ''
        if url:
            self.open_url_in_new_tab(url)
        else:
            pass

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

    def _process_clicked_url(self, url, metadata):
        """
        Process a clicked URL with the given metadata.
        Args:
        url: The URL that was clicked
        metadata: Additional metadata about the click event
        """
        try:
            if url.startswith('/'):
                current_webview = self.get_current_webview()
                current_uri = current_webview.get_uri() if current_webview else None
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
        js_result = webview.evaluate_javascript_finish(result)
        if js_result:
            value = js_result.get_js_value()
            if value and value.is_string():
                pass
            else:
                pass

    def _extract_url_from_message(self, message_data):
        """
        Extract URL and metadata from different types of message data.
        Args:
        message_data: The message data to extract URL from (dict, str, or WebKit.JSValue)
        Returns:
        tuple: (url, metadata) where url is the extracted URL (or None) and
        metadata is a dictionary containing additional data
        """
        if message_data is None:
            return None, {}
        if hasattr(message_data, 'is_string') and message_data.is_string():
            return message_data.to_string(), {'url': message_data.to_string()}
        if isinstance(message_data, str):
            try:
                parsed = json.loads(message_data)
                if isinstance(parsed, dict):
                    return self._extract_url_from_dict(parsed)
                return str(parsed), {'url': str(parsed)}
            except (json.JSONDecodeError, TypeError):
                return message_data, {'url': message_data}
        if isinstance(message_data, dict):
            return self._extract_url_from_dict(message_data)
        return None, {}

    def _extract_url_from_dict(self, data):
        """Extract URL and metadata from a dictionary."""
        if not isinstance(data, dict):
            return None, {}
        url = data.get('url', '')
        if not url and 'message' in data:
            url = data.get('message', '')
        return url, data.copy()

    def on_void_link_clicked(self, user_content_manager, js_message):
        """
        Handle clicks on void links and other clickable elements that don't have direct hrefs.
        Args:
        user_content_manager: The WebKit.UserContentManager that received the message
        js_message: The message containing click data from JavaScript
        """
        message_data = js_message
        if hasattr(js_message, 'get_js_value'):
            message_data = js_message.get_js_value()
            if hasattr(message_data, 'to_dict') and callable(getattr(message_data, 'to_dict')):
                message_data = message_data.to_dict()
            else:
                message_data = js_message
        url, metadata = self._extract_url_from_message(message_data)
        if url and url != "about:blank":
            GLib.idle_add(self._process_clicked_url, url, metadata)
        else:
            pass

    def on_request_started(self, session, message, user_data=None):
        """Handle all HTTP requests through the CORS proxy."""
        uri = message.get_uri()
        if not uri:
            return False
        try:
            return self.loop.run_until_complete(self.cors_proxy.handle_request_async(message))
        except Exception:
            return False

    def get_tab_for_webview(self, webview: WebKit.WebView) -> 'Tab':
        """Find tab containing this webview."""
        for tab in self.tabs:
            if tab.webview == webview:
                return tab
        return None

    def setup_webview_settings(self, webview: WebKit.WebView) -> WebKit.WebView:
        """Configure WebView settings.
        Args:
        webview: The WebKit WebView to configure
        Returns:
        The configured WebView instance
        """
        settings = webview.get_settings()
        settings.set_enable_javascript(True)
        settings.set_enable_back_forward_navigation_gestures(True)
        settings.set_enable_smooth_scrolling(True)
        settings.set_enable_developer_extras(self.debug_mode)
        settings.set_enable_mediasource(True)
        settings.set_enable_media_stream(True)
        settings.set_enable_encrypted_media(True)
        settings.set_enable_media_capabilities(True)
        settings.set_enable_webrtc(True)
        settings.set_enable_webaudio(True)
        settings.set_enable_webgl(True)
        settings.set_enable_fullscreen(True)
        settings.set_enable_html5_database(True)
        settings.set_enable_html5_local_storage(True)
        settings.set_enable_page_cache(True)
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_allow_modal_dialogs(False)
        settings.set_allow_universal_access_from_file_urls(False)
        settings.set_allow_top_navigation_to_data_urls(False)
        settings.set_javascript_can_access_clipboard(False)
        settings.set_javascript_can_open_windows_automatically(True)
        settings.set_media_playback_requires_user_gesture(True)
        settings.set_enable_site_specific_quirks(True)
        webview.set_background_color(Gdk.RGBA(0, 0, 0, 1))
        webview.set_settings(settings)
        self._register_webview_message_handlers(webview)
        content_manager = webview.get_user_content_manager()
        self.adblocker.inject_to_webview(content_manager)
        return webview

    def _setup_security_handlers(self, webview: WebKit.WebView) -> None:
        """Set up security-related signal handlers."""
        webview.connect("insecure-content-detected", self._on_insecure_content)
        webview.connect("permission-request", self._on_permission_request)
        webview.connect("web-process-terminated", self._on_web_process_terminated)

    def _on_console_message(self, webview: WebKit.WebView, console_message: WebKit.ConsoleMessage) -> None:
        """Handle console messages from the web page.
        Args:
        webview: The WebView that emitted the console message
        console_message: The console message containing level, text, and source info
        """
        if self.debug_mode:
            message = console_message.get_message()
            level = console_message.get_level()
            source = console_message.get_source_id() or "unknown"
            line = console_message.get_line() or 0
            logger.debug(f"[WEB CONSOLE][{level}] {message} at {source}:{line}")

    def _on_load_failed(self, webview: WebKit.WebView, load_event: WebKit.LoadEvent, failing_uri: str, error: WebKit.LoadError) -> bool:
        """Handle page load failures.
        Args:
        webview: The WebView that failed to load
        load_event: The type of load event that failed
        failing_uri: The URI that failed to load
        error: The load error details
        Returns:
        bool: False to indicate the error was not handled
        """
        if self.debug_mode:
            logger.error(f"Failed to load {failing_uri}: {error.message}")
        return False

    def _on_tls_error(self, webview: WebKit.WebView, failing_uri: str, certificate, errors: WebKit.TLSErrorsPolicy) -> bool:
        """Handle TLS/SSL errors.
        Args:
        webview: The WebView that encountered the TLS error
        failing_uri: The URI that failed TLS validation
        certificate: The certificate that failed validation
        errors: The TLS errors that occurred
        Returns:
        bool: False to indicate the error was not handled
        """
        if self.debug_mode:
            logger.error(f"TLS error loading {failing_uri}: {errors}")
        return False

    def _on_web_process_terminated(self, webview: WebKit.WebView, reason: WebKit.WebProcessTerminationReason) -> None:
        """Handle web process termination.
        Args:
        webview: The WebView whose web process terminated
        reason: The reason for the termination
        """
        if self.debug_mode:
            reasons = {
                WebKit.WebProcessTerminationReason.CRASHED: "crashed",
                WebKit.WebProcessTerminationReason.EXCEEDED_MEMORY_LIMIT: "exceeded memory limit",
                WebKit.WebProcessTerminationReason.TERMINATED_BY_API: "terminated by API",
                WebKit.WebProcessTerminationReason.REQUESTED: "requested",
                WebKit.WebProcessTerminationReason.UNCAUGHT_EXCEPTION: "uncaught exception",
            }
            reason_str = reasons.get(reason, f"unknown ({reason})")
            logger.warning(f"Web process terminated: {reason_str}")
            if reason in [WebKit.WebProcessTerminationReason.CRASHED,
                          WebKit.WebProcessTerminationReason.EXCEEDED_MEMORY_LIMIT]:
                GLib.timeout_add(1000, self._reload_after_crash, webview)

    def _reload_after_crash(self, webview: WebKit.WebView) -> bool:
        """Reload the webview after a crash.
        Args:
        webview: The WebView to reload
        Returns:
        bool: False to indicate this is a one-time callback
        """
        if webview and hasattr(webview, 'reload'):
            webview.reload()
        return False

    def inject_mouse_event_script(self):
        """Injects JavaScript to capture mouse events in webviews."""
        script = WebKit.UserScript.new(
        """
        (function() {
        console.log('[DEBUG] Mouse event handler script loaded');
        function logDebug(message, obj) {
        console.log('[DEBUG] ' + message, obj || '');
        }
        function handleClick(e) {
        // Debug the click event
        console.log('[DEBUG] Click event detected on:', e.target);
        // Check if this is a left mouse button click
        if (e.button !== 0) {
        logDebug('Not a left-click, ignoring');
        return;
        }
        // Handle both link clicks and elements with click handlers
        let target = e.target;
        logDebug('Click target:', target);
        // Try to find the closest anchor or clickable element
        let link = target.closest('a, [onclick], [data-href], [data-link], [data-url], [role="button"]');
        if (!link && target.matches && !target.matches('a')) {
        // If no link found, check if the target itself is clickable
        const clickable = target.closest('[onclick], [data-href], [data-link], [data-url], [role="button"]');
        if (clickable) {
        link = clickable;
        }
        }
        if (link) {
        logDebug('Found clickable element:', link);
        const href = link.getAttribute('href') || '';
        const hasOnClick = link.hasAttribute('onclick') || link.onclick;
        const isVoidLink = href.trim().toLowerCase() === 'javascript:void(0)' ||
        href.trim() === '#' ||
        hasOnClick ||
        window.getComputedStyle(link).cursor === 'pointer';
        logDebug("Link details - href: " + href + ", hasOnClick: " + hasOnClick + ", isVoidLink: " + isVoidLink);
        if (isVoidLink) {
        // Prevent default only if we're handling it
        if (link.getAttribute('data-handled') === 'true') {
        logDebug('Link already handled, preventing default');
        e.preventDefault();
        e.stopPropagation();
        return false;
        }
        // Check for data-url or try to find a URL in the element
        let dataUrl = link.getAttribute('data-url') ||
        link.getAttribute('data-href') ||
        link.getAttribute('data-link') ||
        link.href;
        // If still no URL, check child elements
        if (!dataUrl) {
        const possibleElements = link.querySelectorAll('[href], [data-href], [data-link], [data-src], [data-url]');
        for (const el of possibleElements) {
        const val = el.href || el.getAttribute('href') ||
        el.getAttribute('data-href') || el.getAttribute('data-link') ||
        el.getAttribute('data-src') || el.getAttribute('data-url');
        if (val && (val.startsWith('http') || val.startsWith('/'))) {
        dataUrl = val;
        break;
        }
        }
        }
        logDebug('Extracted URL:', dataUrl);
        if (dataUrl) {
        // Mark as handled to prevent duplicate processing
        link.setAttribute('data-handled', 'true');
        // Prepare the message
        const message = {
        url: dataUrl,
        href: link.href || '',
        text: (link.innerText || link.textContent || '').trim(),
        hasOnClick: hasOnClick,
        tagName: link.tagName,
        className: link.className || '',
        id: link.id || ''
        };
        logDebug('Sending message to Python:', message);
        // Send message to Python side
        try {
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.voidLinkClicked) {
        window.webkit.messageHandlers.voidLinkClicked.postMessage(message);
        logDebug('Message sent successfully');
        // Prevent default if we're handling the click
        e.preventDefault();
        e.stopPropagation();
        return false;
        } else {
        logDebug('Error: Message handler not found');
        }
        } catch (err) {
        logDebug('Error sending message:', err);
        }
        } else {
        logDebug('No URL found for clickable element, checking for onclick handler');
        // If no URL found but there's an onclick handler, try to execute it
        if (hasOnClick && link.onclick) {
        try {
        logDebug('Executing onclick handler:', link.onclick.toString());
        // Execute the onclick handler - the window.open override should catch any new URLs
        const result = link.onclick.call(link, e);
        logDebug('Onclick execution result:', result);
        } catch (err) {
        logDebug('Error executing onclick handler:', err);
        }
        } else if (hasOnClick && link.getAttribute('onclick')) {
        try {
        const onclickCode = link.getAttribute('onclick');
        logDebug('Executing onclick attribute:', onclickCode);
        // Create a function and execute it
        const onclickFunc = new Function(onclickCode);
        const result = onclickFunc.call(link, e);
        logDebug('Onclick attribute execution result:', result);
        } catch (err) {
        logDebug('Error executing onclick attribute:', err);
        }
        } else {
        logDebug('No onclick handler found to execute');
        }
        }
        }
        // Add click event listener with capturing phase
        document.addEventListener('click', handleClick, {capture: true, passive: false});
        // Also handle mousedown for better compatibility
        document.addEventListener('mousedown', function(e) {
        // Only handle left mouse button
        if (e.button === 0) {
        handleClick(e);
        }
        }, {capture: true, passive: false});
        // Handle dynamically added content
        const observer = new MutationObserver((mutations) => {
        logDebug('DOM mutation detected, reinjecting event listeners');
        document.removeEventListener('click', handleClick, {capture: true, passive: false});
        document.addEventListener('click', handleClick, {capture: true, passive: false});
        });
        observer.observe(document.body, {
        childList: true,
        subtree: true
        });
        logDebug('Mouse event handler injected successfully');
        })();
        """,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        [],
        []
        )
        end_script = WebKit.UserScript.new(
        """
        (function() {
        console.log('[DEBUG] End-of-document mouse event handler loaded');
        // The main script will handle the rest
        })();
        """,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.END,
        [],
        []
        )
        self.content_manager.add_script(script)
        self.content_manager.add_script(end_script)

    def _create_icon_button(self, icon_name, callback, tooltip_text=None):
        """Create a new icon button with the specified properties."""
        image = Gtk.Image.new_from_icon_name(icon_name)
        button = Gtk.Button()
        button.set_child(image)
        button.set_has_frame(False)
        button.set_margin_start(2)
        button.set_margin_end(2)
        if tooltip_text:
            button.set_tooltip_text(tooltip_text)
        button.connect("clicked", callback)
        return button

    def zoom_in(self):
        """Zoom in the current webview."""
        webview = self.get_current_webview()
        if webview:
            current_zoom = webview.get_zoom_level()
            webview.set_zoom_level(min(current_zoom * 1.2, 5.0))

    def zoom_out(self):
        """Zoom out the current webview."""
        webview = self.get_current_webview()
        if webview:
            current_zoom = webview.get_zoom_level()
            webview.set_zoom_level(max(current_zoom / 1.2, 0.25))

    def zoom_reset(self):
        """Reset zoom level to 100%."""
        webview = self.get_current_webview()
        if webview:
            webview.set_zoom_level(1.0)

    def on_right_click(self, gesture, n_press, x, y):
        """Handle right-click to exit fullscreen mode.
        Args:
        gesture: The Gtk.GestureClick that triggered the event
        n_press: Number of presses (1 for single-click, 2 for double-click, etc.)
        x: X coordinate of the click
        y: Y coordinate of the click
        """
        if gesture.get_button() == 3:
            if self.window and self.window.get_surface():
                window_state = self.window.get_surface().get_state()
                if window_state & Gdk.SurfaceState.FULLSCREEN:
                    self.window.unfullscreen()
        return False

    def on_zoom_in_clicked(self, button):
        """Handle zoom in button click."""
        self.zoom_in()

    def on_zoom_out_clicked(self, button):
        """Handle zoom out button click."""
        self.zoom_out()

    def on_zoom_reset_clicked(self, button):
        """Handle zoom reset button click."""
        self.zoom_reset()

    def on_restart_toggle_clicked(self, button):
        """Handle restart toggle button click."""
        if self.is_restart_active():
            self.stop_continuous_restart()
            button.set_label("Start Restart")
            button.remove_css_class("destructive-action")
            self.restart_status_label.set_label("")
        else:
            if self.start_continuous_restart():
                button.set_label("Stop Restart")
                button.add_css_class("destructive-action")
                self.restart_status_label.set_label(f"🔄 {self.restart_interval}s")

    def on_restart_interval_clicked(self, button):
        """Handle restart interval button click."""
        dialog = Gtk.MessageDialog(
        transient_for=self.window,
        modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.OK_CANCEL,
        text="Set Restart Interval"
        )
        dialog.format_secondary_text(f"Current interval: {self.restart_interval} seconds\nEnter new interval (minimum 5 seconds):")
        entry = Gtk.Entry()
        entry.set_text(str(self.restart_interval))
        entry.connect("activate", lambda e: dialog.response(Gtk.ResponseType.OK))
        box = dialog.get_content_area()
        box.append(entry)
        dialog.show()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            try:
                new_interval = int(entry.get_text())
                if new_interval >= 5:
                    self.set_restart_interval(new_interval)
                    if self.is_restart_active():
                        self.restart_status_label.set_label(f"🔄 {new_interval}s")
                    confirm_dialog = Gtk.MessageDialog(
                        transient_for=self.window,
                        modal=True,
                        message_type=Gtk.MessageType.INFO,
                        buttons=Gtk.ButtonsType.OK,
                        text="Interval Updated"
                    )
                    confirm_dialog.format_secondary_text(f"Restart interval set to {new_interval} seconds")
                    confirm_dialog.run()
                    confirm_dialog.destroy()
                else:
                    error_dialog = Gtk.MessageDialog(
                        transient_for=self.window,
                        modal=True,
                        message_type=Gtk.MessageType.ERROR,
                        buttons=Gtk.ButtonsType.OK,
                        text="Invalid Interval"
                    )
                    error_dialog.format_secondary_text("Interval must be at least 5 seconds")
                    error_dialog.run()
                    error_dialog.destroy()
            except ValueError:
                error_dialog = Gtk.MessageDialog(
                    transient_for=self.window,
                    modal=True,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.OK,
                    text="Invalid Input"
                )
                error_dialog.format_secondary_text("Please enter a valid number")
                error_dialog.run()
                error_dialog.destroy()
        dialog.destroy()

    def create_toolbar(self):
        """Create and configure the browser toolbar with navigation and action buttons."""
        if hasattr(self, "toolbar") and self.toolbar is not None:
            parent = self.toolbar.get_parent()
            if parent is not None:
                parent.remove(self.toolbar)
            for child in self.toolbar.get_children():
                self.toolbar.remove(child)
                child.destroy()
        else:
            self.toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            self.toolbar.set_margin_start(4)
            self.toolbar.set_margin_end(4)
            self.toolbar.set_margin_top(2)
            self.toolbar.set_margin_bottom(2)
        self.toolbar.add_css_class("toolbar")
        try:
            self.toolbar.set_hexpand(True)
        except Exception:
            pass
        if hasattr(self, 'nav_box'):
            self.nav_box = None
        if hasattr(self, 'url_entry'):
            self.url_entry = None
        if hasattr(self, 'action_box'):
            self.action_box = None
        if hasattr(self, 'zoom_box'):
            self.zoom_box = None
        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.nav_box.add_css_class("linked")
        self.nav_box.append(self._create_icon_button("go-previous-symbolic",
            self.on_back_clicked,
            "Back"))
        self.nav_box.append(self._create_icon_button("go-next-symbolic",
            self.on_forward_clicked,
            "Forward"))
        self.nav_box.append(self._create_icon_button("view-refresh-symbolic",
            self.on_refresh_clicked,
            "Reload"))
        self.nav_box.append(self._create_icon_button("go-home-symbolic",
            lambda b: self.load_url(self.home_url),
            "Home"))
        self.toolbar.append(self.nav_box)
        url_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        url_box.add_css_class("linked")
        self.url_entry = Gtk.Entry(placeholder_text="Enter URL or search terms")
        self.url_entry.set_hexpand(True)
        self.url_entry.connect("activate", self.on_go_clicked)
        url_box.append(self.url_entry)
        go_button = Gtk.Button(label="Go")
        go_button.connect("clicked", self.on_go_clicked)
        url_box.append(go_button)
        self.toolbar.append(url_box)
        self.action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.action_box.add_css_class("linked")
        self.action_box.append(self._create_icon_button("bookmark-new-symbolic",
            self.on_add_bookmark_clicked,
            "Add Bookmark"))
        self.action_box.append(self._create_icon_button("tab-new-symbolic",
            self.on_new_tab_clicked,
            "New Tab"))
        self.toolbar.append(self.action_box)
        self.zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.zoom_box.add_css_class("linked")
        self.zoom_box.append(self._create_icon_button("zoom-out-symbolic",
            self.on_zoom_out_clicked,
        "Zoom Out"))
        self.zoom_box.append(self._create_icon_button("zoom-fit-best-symbolic",
            self.on_zoom_reset_clicked,
            "Reset Zoom"))
        self.zoom_box.append(self._create_icon_button("zoom-in-symbolic",
            self.on_zoom_in_clicked,
            "Zoom In"))
        self.toolbar.append(self.zoom_box)
        try:
            dev_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            dev_box.add_css_class("linked")
            inspect_button = Gtk.Button(label="Inspect")
            inspect_button.set_tooltip_text("Open Web Inspector")
            inspect_button.connect("clicked", self.on_inspect_clicked)
            dev_box.append(inspect_button)
            self.toolbar.append(dev_box)
        except Exception:
            pass
        if hasattr(self, 'download_box') and self.download_box:
            self.download_box.set_halign(Gtk.Align.END)
        self.download_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.download_box.set_valign(Gtk.Align.CENTER)
        self.download_box.set_margin_start(6)
        self.download_box.set_margin_end(6)
        self.download_box.set_visible(False)
        self.download_box.add_css_class("download-status-box")
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data("""
        .download-status-box {
        background-color: rgba(53, 132, 228, 0.1);
        border-radius: 6px;
        padding: 4px 8px;
        margin: 0 4px;
        }
        """)
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self.toolbar.append(self.download_box)
        if hasattr(self, 'loading_spinner') and self.loading_spinner:
            self.loading_spinner.set_halign(Gtk.Align.END)
            self.loading_spinner.set_valign(Gtk.Align.CENTER)
            self.loading_spinner.set_margin_start(6)
            self.loading_spinner.set_margin_end(6)
            self.loading_spinner.set_visible(False)
            self.toolbar.append(self.loading_spinner)
        return self.toolbar

    def on_inspect_clicked(self, button=None):
        """Open the WebKit Web Inspector for the current webview."""
        webview = self.get_current_webview() or getattr(self, 'webview', None)
        if not webview:
            return
        settings = getattr(webview, 'get_settings', lambda: None)()
        dev_enabled = False
        if settings:
            try:
                dev_enabled = bool(
                    getattr(settings, 'get_enable_developer_extras', lambda: None)()
                )
            except (AttributeError, TypeError):
                try:
                    dev_enabled = bool(settings.get_property('enable-developer-extras'))
                except (AttributeError, TypeError):
                    pass
        if not dev_enabled:
            try:
                webview = WebKit.WebView()
                webview.set_editable(False)
                webview.get_settings().set_enable_developer_extras(True)
                self.setup_webview_proxy(webview)
            except (AttributeError, TypeError, RuntimeError):
                pass
        inspector = getattr(webview, 'get_inspector', lambda: None)()
        if inspector and hasattr(inspector, 'show'):
            inspector.show()
        elif hasattr(webview, 'run_javascript'):
            js = "console.log('[Inspector] Requested via toolbar'); debugger;"
            try:
                webview.run_javascript(js, None, None, None)
            except (AttributeError, TypeError, RuntimeError):
                pass

    def safe_show_popover(self, popover):
        """Safely show a Gtk.Popover, avoiding multiple popups or broken state."""
        if not popover:
            return
        try:
            if not popover.get_child():
                return
            if popover.get_visible():
                return
            child = popover.get_child()
            if (
                child
                and child.get_parent() is not None
                and child.get_parent() != popover
            ):
                try:
                    if hasattr(child, 'get_parent') and child.get_parent() is not None:
                        parent = child.get_parent()
                        if parent and hasattr(parent, "remove") and child.get_parent() == parent:
                            parent.remove(child)
                except Exception:
                    pass
            parent = popover.get_parent()
            if parent is None:
                pass
            popover.popup()
        except Exception:
            pass

    def _show_bookmarks_menu(self, button=None):
        """Show the bookmarks menu."""
        if hasattr(self, "toolbar") and self.toolbar is not None:
            child = self.toolbar.get_first_child()
            while child:
                if (
                    isinstance(child, Gtk.MenuButton)
                    and child.get_label() == "Bookmarks"
                ):
                    popover = child.get_popover()
                    if popover:
                        self.safe_show_popover(popover)
                        return
                child = child.get_next_sibling()

    def update_bookmarks_menu(self, menu_container):
        """Rebuild the bookmarks menu with delete options."""
        if not menu_container:
            return
        try:
            child = menu_container.get_first_child()
            while child:
                next_child = child.get_next_sibling()
                parent = child.get_parent()
                if parent and hasattr(parent, "remove"):
                    parent.remove(child)
                child = next_child
        except Exception:
            pass
        for bookmark in self.bookmarks:
            if isinstance(bookmark, str):
                bookmark = {"url": bookmark, "title": None}
            url = bookmark.get("url")
            if not url:
                continue
            title = bookmark.get("title") or url
            display_text = (title[:30] + "...") if len(title) > 30 else title
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hbox.set_margin_start(6)
            hbox.set_margin_end(6)
            hbox.set_margin_top(3)
            hbox.set_margin_bottom(3)
            hbox.set_halign(Gtk.Align.FILL)
            hbox.set_hexpand(True)
            favicon_img = Gtk.Picture()
            favicon_img.set_size_request(16, 16)
            favicon_img.set_halign(Gtk.Align.CENTER)
            favicon_img.set_valign(Gtk.Align.CENTER)
            favicon = self.get_favicon(url)
            if favicon:
                favicon_img.set_paintable(favicon)
            else:
                favicon_img.set_size_request(16, 16)
            label = Gtk.Label(label=display_text)
            label.set_xalign(0)
            label.set_hexpand(True)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            button = Gtk.Button()
            button.set_tooltip_text(url)
            button_child = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            button_child.append(favicon_img)
            button_child.append(label)
            button.set_child(button_child)
            button.set_hexpand(True)
            button.set_halign(Gtk.Align.FILL)
            button.connect("clicked", partial(self.load_url, url))
            delete_button = Gtk.Button()
            delete_icon = Gtk.Image.new_from_icon_name("edit-delete-symbolic")
            delete_button.set_child(delete_icon)
            delete_button.set_has_frame(False)
            delete_button.add_css_class("destructive-action")
            delete_button.set_tooltip_text("Remove bookmark")
            delete_button.connect("clicked", self._on_delete_bookmark_clicked, url)
            hbox.append(button)
            hbox.append(delete_button)
            menu_container.append(hbox)
        if self.bookmarks:
            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            menu_container.append(separator)
            clear_button = Gtk.Button(label="Clear All Bookmarks")
            clear_button.set_halign(Gtk.Align.CENTER)
            clear_button.connect("clicked", self._clear_all_bookmarks)
            menu_container.append(clear_button)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.setup_keyboard_shortcuts()

    def setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts for tab management."""
        tab_search_action = Gio.SimpleAction.new("tab_search", None)
        tab_search_action.connect("activate", lambda a, p: self.toggle_tab_search())
        self.add_action(tab_search_action)
        new_tab_action = Gio.SimpleAction.new("new_tab", None)
        new_tab_action.connect("activate", lambda a, p: self.add_new_tab(self.home_url))
        self.add_action(new_tab_action)
        close_tab_action = Gio.SimpleAction.new("close_tab", None)
        close_tab_action.connect("activate", lambda a, p: self.close_current_tab())
        self.add_action(close_tab_action)
        pin_tab_action = Gio.SimpleAction.new("pin_tab", None)
        pin_tab_action.connect("activate", lambda a, p: self.toggle_pin_current_tab())
        self.add_action(pin_tab_action)
        self.set_accels_for_action("app.tab_search", ["<Control><Shift>T"])
        self.set_accels_for_action("app.new_tab", ["<Control>T"])
        self.set_accels_for_action("app.close_tab", ["<Control>W"])
        self.set_accels_for_action("app.pin_tab", ["<Control><Shift>P"])

    def _load_custom_css(self):
        """Load custom CSS for the application."""
        try:
            css_provider = Gtk.CssProvider()
            css = """
            /* Style for tabs */
            tab {
            padding: 6px 12px;
            background-color: #f5f5f5;
            border: 1px solid #ccc;
            border-bottom: none;
            border-radius: 4px 4px 0 0;
            margin-right: 2px;
            }
            tab:checked {
            background-color: #fff;
            border-bottom: 1px solid #fff;
            margin-bottom: -1px;
            }
            /* Pinned tab styles */
            .pinned-tab {
            background-color: #e8f4fd;
            border-color: #2196f3;
            }
            /* Loading tab styles */
            .loading-tab {
            background-color: #fff3cd;
            border-color: #ffc107;
            }
            /* Grouped tab styles */
            .grouped-tab {
            background-color: #f3e5f5;
            border-color: #9c27b0;
            }
            /* Group indicator */
            .group-indicator {
            background-color: #9c27b0;
            color: white;
            padding: 2px 6px;
            border-radius: 8px;
            font-size: 0.8em;
            margin-left: 4px;
            }
            /* Search overlay styles */
            .search-overlay {
            background-color: rgba(255, 255, 255, 0.95);
            border: 2px solid #ccc;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
            }
            /* Search results styles */
            .search-results {
            max-height: 200px;
            overflow-y: auto;
            }
            .search-results row {
            padding: 8px;
            border-bottom: 1px solid #eee;
            }
            .search-results row:hover {
            background-color: #f0f0f0;
            }
            .search-results row:selected {
            background-color: #007acc;
            color: white;
            }
            .dim-label {
            color: #666;
            font-size: 0.9em;
            }
            .group-tag {
            background-color: #007acc;
            color: white;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.8em;
            }
        """
            css_provider.load_from_data(css.encode())
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception:
            pass

    def do_activate(self):
        """Create and show the main window."""
        if not self.wake_lock_active:
            self.wake_lock_active = self.wake_lock.inhibit()
        if hasattr(self, "window") and self.window:
            try:
                self.window.present()
                return
            except Exception:
                pass
        self.window = None
        self.window = Gtk.ApplicationWindow(application=self, title="Shadow Browser")
        self.css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.window.set_default_size(1200, 800)
        self.window.set_decorated(True)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        menubar = self.create_menubar()
        self.safe_append(vbox, menubar)
        toolbar = self.create_toolbar()
        self.safe_append(vbox, toolbar)
        if not hasattr(self, 'notebook') or self.notebook is None:
            self.notebook = Gtk.Notebook()
        self.safe_append(vbox, self.notebook)
        self.window.set_child(vbox)
        if not hasattr(self, '_window_signals_connected'):
            self.window.connect("close-request", self.on_window_destroy)
            self._window_signals_connected = True
        if len(self.tabs) == 0:
            self.add_new_tab(self.home_url)
        self.window.set_resizable(True)
        self.window.connect("notify::default-width", self.update_icon_sizes)
        self.update_icon_sizes(self.window, None)
        self.window.present()

    def update_icon_sizes(self, window, _):
        """Dynamically adjust icon sizes based on window width."""
        if not window:
            return
        width = window.get_width()
        size = max(16, min(32, width // 50))
        css = f"""
        button image {{
        -gtk-icon-size: {size}px;
        min-width: {size}px;
        min-height: {size}px;
        }}
        """.encode()
        self.css_provider.load_from_data(css)

    def do_shutdown(self):
        """Save session and tabs before shutdown."""
        try:
            if self.wake_lock_active:
                self.wake_lock.uninhibit()
                self.wake_lock_active = False
            self.save_session()
            self.save_tabs()
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
        except Exception:
            pass
        Gtk.Application.do_shutdown(self)

    def register_error_handlers(self):
        self.error_handlers["gtk_warning"] = self.handle_gtk_warning
        self.error_handlers["network_error"] = self.handle_network_error
        self.error_handlers["webview_error"] = self.handle_webview_error
        self.error_handlers["memory_error"] = self.handle_memory_error

    def handle_gtk_warning(self, message):
        return True

    def handle_network_error(self, url, error):
        return True

    def handle_webview_error(self, webview, error):
        return True

    def handle_memory_error(self, error):
        return True

    def toggle_debug_mode(self, action=None, parameter=None):
        self.debug_mode = not self.debug_mode
        self.set_logging_level()

    def set_logging_level(self):
        pass

    def _close_bookmark_popover(self):
        """Helper to close the bookmarks popover."""
        if hasattr(self, 'bookmark_popover') and self.bookmark_popover:
            self.bookmark_popover.popdown()

    def _on_key_pressed(self, controller, keyval, keycode, state):
        """Handle keyboard shortcuts."""
        from gi.repository import Gdk
        ctrl = (state & Gdk.ModifierType.CONTROL_MASK)
        shift = (state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and shift and keyval == Gdk.KEY_b:
            self.test_bookmarks_menu()
            return True
        return False

    def _on_delete_bookmark_clicked(self, button, url):
        """Handle click on the delete bookmark button."""
        self.bookmarks = [b for b in self.bookmarks if not (isinstance(b, dict) and b.get("url") == url)]
        self.save_json(BOOKMARKS_FILE, self.bookmarks)
        self.update_bookmarks_menu(self.bookmark_menu)
        self._close_bookmark_popover()

    def _clear_all_bookmarks(self, button=None):
        """Clear all bookmarks."""
        self.bookmarks.clear()
        self.save_json(BOOKMARKS_FILE, self.bookmarks)
        self.update_bookmarks_menu(self.bookmark_menu)
        self._close_bookmark_popover()

    def create_menubar(self):
        menubar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        try:
            if self.debug_mode:
                print("[DEBUG] Creating menubar...")
            if hasattr(self, 'bookmark_menu_button') and self.bookmark_menu_button:
                if hasattr(self.bookmark_menu_button, 'get_parent') and self.bookmark_menu_button.get_parent() is not None:
                    try:
                        parent = self.bookmark_menu_button.get_parent()
                        if parent and hasattr(parent, "remove") and self.bookmark_menu_button.get_parent() == parent:
                            parent.remove(self.bookmark_menu_button)
                    except Exception:
                        pass
            self.bookmark_menu_button = Gtk.MenuButton(label="Bookmarks")
            self.bookmark_menu_button.set_tooltip_text("Show bookmarks")
            if hasattr(self, 'bookmark_popover') and self.bookmark_popover:
                try:
                    self.bookmark_popover.popdown()
                except Exception:
                    pass
            self.bookmark_popover = Gtk.Popover()
            self.bookmark_popover.set_size_request(400, 700)
            if not hasattr(self, 'bookmark_menu') or self.bookmark_menu is None:
                self.bookmark_menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                if self.debug_mode:
                    print("[DEBUG] Created new bookmark_menu container")
            else:
                try:
                    child = self.bookmark_menu.get_first_child()
                    while child:
                        next_child = child.get_next_sibling()
                        try:
                            if hasattr(child, 'get_parent') and child.get_parent() is not None:
                                parent = child.get_parent()
                                if parent and hasattr(parent, "remove") and child.get_parent() == parent:
                                    parent.remove(child)
                        except Exception:
                            pass
                        child = next_child
                except Exception:
                    pass
            self.bookmark_scrolled_window = Gtk.ScrolledWindow()
            self.bookmark_scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            self.bookmark_scrolled_window.set_vexpand(True)
            self.bookmark_scrolled_window.set_hexpand(True)
            self.bookmark_scrolled_window.set_child(self.bookmark_menu)
            if self.debug_mode:
                print(f"[DEBUG] Calling update_bookmarks_menu from create_menubar with {len(self.bookmarks)} bookmarks")
            self.update_bookmarks_menu(self.bookmark_menu)
            self.bookmark_popover.set_child(self.bookmark_scrolled_window)
            self.bookmark_menu_button.set_popover(self.bookmark_popover)
            self.bookmark_popover.connect("closed", lambda popover: popover.set_visible(False))
            self.safe_append(menubar, self.bookmark_menu_button)
            if self.debug_mode:
                print("[DEBUG] Menubar creation completed")
        except Exception as e:
            if self.debug_mode:
                print(f"[DEBUG] Error in create_menubar: {e}")
            pass
        try:
            if hasattr(self, 'window') and self.window:
                shortcut_controller = Gtk.EventControllerKey()
                shortcut_controller.connect("key-pressed", self._on_key_pressed)
                self.window.add_controller(shortcut_controller)
        except Exception:
            pass
        try:
            download_button = Gtk.Button(label="Downloads")
            download_button.set_tooltip_text("Open Downloads Folder")
            download_button.connect("clicked", self.on_downloads_clicked)
            self.safe_append(menubar, download_button)
        except Exception:
            pass
        try:
            settings_button = Gtk.Button(label="Settings")
            settings_button.set_tooltip_text("Open settings dialog")
            settings_button.connect("clicked", lambda x: self.on_settings_clicked(x))
            self.safe_append(menubar, settings_button)
        except Exception:
            pass
        try:
            clear_data_button = Gtk.Button(label="Clear Data")
            clear_data_button.set_tooltip_text("Clear browsing data")
            clear_data_button.connect("clicked", lambda x: self.create_clear_data_dialog().present())
            self.safe_append(menubar, clear_data_button)
        except Exception:
            pass
        try:
            about_button = Gtk.Button(label="About")
            about_button.connect("clicked", self.on_about)
            self.safe_append(menubar, about_button)
        except Exception:
            pass
        try:
            self.tor_menu_toggle = Gtk.ToggleButton(label="Tor")
            self.tor_menu_toggle.set_tooltip_text("Toggle Tor routing on/off")
            self.tor_menu_toggle.set_active(getattr(self, "tor_enabled", False))
            self.tor_menu_toggle.connect("toggled", self.on_tor_menu_toggled)
            self.safe_append(menubar, self.tor_menu_toggle)
        except Exception:
            pass
        try:
            self.tor_status_button = Gtk.Button()
            self.tor_status_icon = Gtk.Image.new_from_icon_name("network-offline-symbolic")
            self.tor_status_button.set_child(self.tor_status_icon)
            self.tor_status_button.set_tooltip_text("Tor status - Click to check")
            self.tor_status_button.connect("clicked", self.on_tor_status_clicked)
            self.update_tor_status_indicator()
            self.safe_append(menubar, self.tor_status_button)
        except Exception:
            pass
        return menubar

    def update_tor_status_indicator(self):
        """Update the Tor status indicator in the toolbar."""
        if not hasattr(self, 'tor_status_icon'):
            return
        status = self.tor_manager.get_status()
        if status['connected'] and status['working']:
            icon_name = "security-high-symbolic"
            tooltip = "Tor is connected and working"
        elif status['system_available']:
            icon_name = "dialog-warning-symbolic"
            tooltip = "Tor is available but not enabled"
        else:
            icon_name = "network-offline-symbolic"
            tooltip = "Tor is not available - Install system Tor"
        self.tor_status_icon.set_from_icon_name(icon_name)
        self.tor_status_icon.set_tooltip_text(tooltip)

    def on_tor_status_clicked(self, button):
        """Handle Tor status button click - show detailed status."""
        status = self.tor_manager.get_status()
        if status['system_available']:
            if status['connected'] and status['working']:
                self.show_info_message(
                    "Tor Status: Connected\n\n"
                    f"Port: {status['port']}\n"
                    "All traffic is being routed through Tor."
                )
            else:
                self.show_info_message(
                    "Tor Status: Available but Disabled\n\n"
                    f"Port: {status['port']}\n"
                    "Enable Tor in settings to route traffic through Tor."
                )
        else:
            self.show_info_message(
                "Tor Status: Not Available\n\n"
                "Please install and start system Tor:\n"
                "sudo dnf install tor\n"
                "sudo systemctl start tor\n"
                "sudo systemctl enable tor"
            )

    def on_settings_clicked(self, button):
        """Open the settings dialog."""
        if hasattr(self, "settings_dialog") and self.settings_dialog:
            self.settings_dialog.present()
            return
        self.settings_dialog = Gtk.Dialog(
        title="Settings",
        transient_for=self.window,
        modal=True,
        destroy_with_parent=False,
        )
        content_area = self.settings_dialog.get_child()
        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        content_area.append(grid)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        grid.set_margin_start(10)
        grid.set_margin_end(10)
        self.adblock_toggle = Gtk.CheckButton(label="Enable AdBlocker")
        if hasattr(self, 'adblocker') and self.adblocker is not None:
            self.adblock_toggle.set_active(getattr(self.adblocker, "enabled", True))
        else:
            self.adblock_toggle.set_active(False)
        grid.attach(self.adblock_toggle, 0, 0, 1, 1)
        self.incognito_toggle = Gtk.CheckButton(label="Enable Incognito Mode")
        self.incognito_toggle.set_active(getattr(self, "incognito_mode", False))
        grid.attach(self.incognito_toggle, 0, 1, 1, 1)
        self.anti_fp_toggle = Gtk.CheckButton(label="Enable Anti-Fingerprinting")
        self.anti_fp_toggle.set_active(getattr(self, "anti_fingerprinting_enabled", True))
        grid.attach(self.anti_fp_toggle, 0, 2, 1, 1)
        self.tor_toggle = Gtk.CheckButton(label="Enable Tor (Requires system Tor)")
        self.tor_toggle.set_active(getattr(self, "tor_enabled", False))
        self.tor_toggle.connect("toggled", self.on_tor_toggled)
        grid.attach(self.tor_toggle, 0, 3, 2, 1)
        search_label = Gtk.Label(label="Default Search Engine URL:")
        search_label.set_halign(Gtk.Align.START)
        grid.attach(search_label, 0, 4, 1, 1)
        self.search_engine_entry = Gtk.Entry()
        self.search_engine_entry.set_text(getattr(self, "search_engine", "https://duckduckgo.com/?q={}"))
        grid.attach(self.search_engine_entry, 1, 4, 1, 1)
        home_label = Gtk.Label(label="Home Page URL:")
        home_label.set_halign(Gtk.Align.START)
        grid.attach(home_label, 0, 5, 1, 1)
        self.home_page_entry = Gtk.Entry()
        self.home_page_entry.set_text(getattr(self, "home_url", "https://duckduckgo.com/").replace("https://", "").replace("http://", ""))
        grid.attach(self.home_page_entry, 1, 5, 1, 1)
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        save_button = Gtk.Button(label="Save")
        cancel_button = Gtk.Button(label="Cancel")
        button_box.append(save_button)
        button_box.append(cancel_button)
        grid.attach(button_box, 0, 6, 2, 1)
        save_button.connect("clicked", lambda btn: self.settings_dialog.emit("response", Gtk.ResponseType.OK))
        cancel_button.connect("clicked", lambda btn: self.settings_dialog.emit("response", Gtk.ResponseType.CANCEL))
        self.settings_dialog.connect("response", self.on_settings_dialog_response)
        self.settings_dialog.present()

    def on_settings_dialog_response(self, dialog, response_id):
        if response_id == Gtk.ResponseType.ACCEPT or response_id == Gtk.ResponseType.OK:
            self.on_settings_save(None)
        if dialog and dialog.is_visible():
            dialog.set_visible(False)
        if dialog:
            dialog.destroy()
        if hasattr(self, 'settings_dialog') and self.settings_dialog == dialog:
            self.settings_dialog = None

    def on_settings_save(self, button):
        if self.adblock_toggle.get_active():
            self.adblocker.enable()
        else:
            self.adblocker.disable()
        self.incognito_mode = self.incognito_toggle.get_active()
        self.anti_fingerprinting_enabled = self.anti_fp_toggle.get_active()
        self.search_engine = self.search_engine_entry.get_text().strip()
        self.home_url = self.home_page_entry.get_text().strip()
        with self.tabs_lock:
            for tab in self.tabs:
                if hasattr(tab, 'webview') and tab.webview:
                    GLib.idle_add(tab.webview.reload)

    def _handle_tor_toggle(self, enabled, source=None):
        """Shared logic for handling Tor enable/disable operations."""
        if getattr(self, "tor_manager", None) is None:
            return

        def reload_webview(webview):
            if webview:
                current_uri = webview.get_uri()
                if current_uri and not current_uri.startswith('about:'):
                    webview.load_uri(current_uri)
        success = False
        if enabled:
            if self.tor_manager.enable_tor_proxy():
                self.tor_enabled = True
                self.show_info_message("Tor enabled - All traffic now routed through Tor")
                with self.tabs_lock:
                    for tab in self.tabs:
                        if hasattr(tab, 'webview') and tab.webview:
                            GLib.idle_add(reload_webview, tab.webview)
                success = True
            else:
                self.tor_enabled = False
                self.show_error_message("Failed to enable Tor. Please ensure Tor is installed and running.")
        else:
            self.tor_manager.disable_tor_proxy()
            self.tor_enabled = False
            self.show_info_message("Tor disabled - Using direct connection")
            with self.tabs_lock:
                for tab in self.tabs:
                    if hasattr(tab, 'webview') and tab.webview:
                        GLib.idle_add(reload_webview, tab.webview)
            success = True
        if success:
            self._sync_tor_toggle_states(self.tor_enabled, source)
        else:
            self._sync_tor_toggle_states(False, source)
        GLib.idle_add(self.update_tor_status_indicator)

    def _sync_tor_toggle_states(self, active, source=None):
        """Synchronize Tor toggle widgets without recursive events."""
        toggles = []
        if hasattr(self, 'tor_toggle') and self.tor_toggle:
            toggles.append(self.tor_toggle)
        if hasattr(self, 'tor_menu_toggle') and self.tor_menu_toggle:
            toggles.append(self.tor_menu_toggle)
        for toggle in toggles:
            if toggle is source:
                continue
            if toggle.get_active() == active:
                continue
            self._tor_toggle_updating = True
            try:
                toggle.set_active(active)
            finally:
                self._tor_toggle_updating = False

    def on_tor_toggled(self, toggle_button):
        """Handle Tor toggle from settings dialog."""
        if getattr(self, '_tor_toggle_updating', False):
            return
        self._handle_tor_toggle(toggle_button.get_active(), source=toggle_button)

    def on_tor_menu_toggled(self, toggle_button):
        """Handle Tor toggle from the menubar button."""
        if getattr(self, '_tor_toggle_updating', False):
            return
        self._handle_tor_toggle(toggle_button.get_active(), source=toggle_button)

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
        main_box.set_margin_start(12)
        main_box.set_margin_end(12)
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.append(Gtk.Label(label="Select the types of data to clear:"))
        self.cookies_check = Gtk.CheckButton(label="Cookies and other site data")
        self.cookies_check.set_active(True)
        content_box.append(self.cookies_check)
        self.cache_check = Gtk.CheckButton(label="Cached images and files")
        self.cache_check.set_active(True)
        content_box.append(self.cache_check)
        self.passwords_check = Gtk.CheckButton(label="Saved passwords")
        content_box.append(self.passwords_check)
        self.history_check = Gtk.CheckButton(label="Browsing history")
        content_box.append(self.history_check)
        main_box.append(content_box)
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        button_box.set_halign(Gtk.Align.END)
        cancel_button = Gtk.Button(label="_Cancel", use_underline=True)
        cancel_button.connect("clicked", lambda btn: dialog.close())
        button_box.append(cancel_button)
        clear_button = Gtk.Button(label="_Clear Data", use_underline=True)
        clear_button.connect("clicked", lambda btn: self.on_clear_data_confirm(dialog))
        button_box.append(clear_button)
        main_box.append(button_box)
        dialog.set_child(main_box)
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
        """Clear all cookies using the WebKit cookie manager."""
        webview = self.get_current_webview()
        if webview:
            context = webview.get_context()
            if hasattr(context, 'get_cookie_manager'):
                cookie_manager = context.get_cookie_manager()
                if cookie_manager:
                    cookie_manager.delete_all_cookies()
            return True
        return False
        with self.tabs_lock:
            for tab in self.tabs:
                context = tab.webview.get_context()
                if hasattr(context, 'get_cookie_manager'):
                    cookie_manager = context.get_cookie_manager()
                    cookie_manager.delete_all_cookies()
        return False

    def clear_cache(self):
        context = WebKit.WebContext.get_default()
        if context:
            if hasattr(context, 'clear_cache'):
                context.clear_cache()
            elif hasattr(context, 'clear_cache_storage'):
                context.clear_cache_storage()

    def clear_passwords(self):
        context = WebKit.WebContext.get_default()
        if context and hasattr(context, 'clear_credentials'):
            context.clear_credentials()

    def clear_history(self):
        if hasattr(self, 'history'):
            self.history.clear()
            self.save_json(HISTORY_FILE, [])

    def show_error_message(self, message):
        """Display an error message dialog."""
        dialog = Gtk.MessageDialog(
        transient_for=self.window,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text=message
        )
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.show()

    def show_message(self, title, message):
        """Display an informational message dialog."""
        dialog = Gtk.MessageDialog(
        transient_for=self.window,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.OK,
        text=f"{title}\n\n{message}"
        )
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.show()

    def show_info_message(self, message, timeout=3):
        """Display an informational message to the user.
        Args:
        message (str): The message to display
        timeout (int): How long to show the message in seconds (default: 3)
        """
        if not hasattr(self, 'statusbar'):
            if self.debug_mode:
                print(f"INFO: {message}")
            return
        try:
            self.statusbar.remove_all(0)
            self.statusbar.push(0, message)
            if timeout > 0:
                def clear_message():
                    if hasattr(self, 'statusbar'):
                        self.statusbar.remove_all(0)
                    return False
                GLib.timeout_add_seconds(timeout, clear_message)
        except Exception as e:
            if self.debug_mode:
                print(f"Error showing info message: {e}")


    def is_valid_url(self, url):
        result = urlparse(url)
        return all([result.scheme, result.netloc])

    def load_url(self, url, *args, **kwargs):
        """Load a URL in the current active webview.
        Args:
        url (str): The URL to load
        *args: Additional positional arguments (ignored)
        **kwargs: Additional keyword arguments (ignored)
        """
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

    def on_add_bookmark_clicked(self, button):
        """Handle Add Bookmark button click."""
        current_webview = self.get_current_webview()
        if current_webview:
            url = current_webview.get_uri()
            if url:
                self.add_bookmark(url)

    def add_bookmark(self, url, title=None):
        """
        Add a URL to bookmarks.
        Args:
        url (str): The URL to bookmark.
        title (str, optional): Title for the bookmark.
        """
        if not url or not url.startswith(("http://", "https://")):
            return False
        if isinstance(self.bookmarks, dict):
            self.bookmarks = [
                {"url": k, "title": v.get("title", k)}
                for k, v in self.bookmarks.items()
            ]
        elif not isinstance(self.bookmarks, list):
            self.bookmarks = []
        for i, bookmark in enumerate(self.bookmarks):
            if isinstance(bookmark, dict) and bookmark.get("url") == url:
                if title:
                    self.bookmarks[i]["title"] = title
                    self.save_json(BOOKMARKS_FILE, self.bookmarks)
                    GLib.idle_add(self.update_bookmarks_menu, self.bookmark_menu)
                    return True
                elif isinstance(bookmark, str) and bookmark == url:
                    self.bookmarks[i] = {"url": url, "title": title or url}
                    self.save_json(BOOKMARKS_FILE, self.bookmarks)
                    GLib.idle_add(self.update_bookmarks_menu, self.bookmark_menu)
                    return True
        if title is None:
            webview = self.get_current_webview()
            title = webview.get_title() if webview else url
        self.bookmarks.append({"url": url, "title": title})
        self.save_json(BOOKMARKS_FILE, self.bookmarks)
        GLib.idle_add(self.update_bookmarks_menu, self.bookmark_menu)
        return True

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

    def _load_texture_from_file(self, filename):
        """Load a texture from a file using Gdk.Texture.
        Args:
        filename: Path to the image file
        Returns:
        Gdk.Texture or None: The loaded texture, or None on error
        """
        return Gdk.Texture.new_from_filename(filename)
        file = Gio.File.new_for_path(filename)
        stream = file.read()
        return Gdk.Texture.new_from_stream(stream)

    def on_about(self, button):
        """Show the about dialog."""
        about_dialog = Gtk.AboutDialog(transient_for=self.window)
        about_dialog.set_program_name("Shadow Browser")
        about_dialog.set_version("1.9")
        about_dialog.set_copyright(" 2025 ShadowyFigure")
        about_dialog.set_comments("A privacy-focused web browser")
        about_dialog.set_website("https://github.com/shadowyfigure/shadow-browser-")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        image_path = os.path.join(script_dir, "background.png")
        if os.path.exists(image_path):
            texture = self._load_texture_from_file(image_path)
            if texture:
                about_dialog.set_logo(texture)
            else:
                about_dialog.set_logo_icon_name("web-browser")
        else:
            about_dialog.set_logo_icon_name("web-browser")
        about_dialog.present()

    def on_back_clicked(self, button):
        """Handle back button click."""
        webview = self.get_current_webview()
        if webview and webview.can_go_back():
            webview.go_back()

    def on_new_tab_clicked(self, button):
        """Handle New Tab button click."""
        self.add_new_tab(self.home_url)

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

    def add_new_tab(self, url, background=False):
        """Add a new tab with a webview loading the specified URL."""
        webview = self.create_secure_webview()
        if webview is None:
            self.show_error_message("Error creating new tab: Failed to create WebView")
            return
        self.setup_webview_settings(webview)
        webview.load_uri(url)
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.set_vexpand(True)
        scrolled_window.set_hexpand(True)
        scrolled_window.set_min_content_width(800)
        scrolled_window.set_min_content_height(600)
        scrolled_window.set_child(webview)
        tab_id = f"tab_{len(self.tabs)}_{int(time.time())}"
        tab = Tab(url, webview, scrolled_window, tab_id)
        if url and url != 'about:blank':
            domain = self._extract_domain(url)
            if domain:
                fallback_favicon = self._get_favicon_fallback(domain, 'domain_color')
                if tab.favicon:
                    tab.favicon.set_paintable(fallback_favicon)
                    tab.favicon.queue_draw()
        def set_initial_favicon():
            favicon = self.get_favicon(url)
            if favicon and tab.favicon:
                tab.favicon.set_paintable(favicon)
                tab.favicon.queue_draw()
        GLib.idle_add(set_initial_favicon)
        insert_position = len(self.pinned_tabs)
        if not background:
            tab_index = self.notebook.insert_page(scrolled_window, tab.header_box, insert_position)
            self.notebook.set_current_page(tab_index)
        else:
            tab_index = self.notebook.insert_page(scrolled_window, tab.header_box, insert_position)
        self.tabs.insert(insert_position, tab)

        def on_close_clicked(button, tab=tab):
            if tab in self.tabs:
                tab_index = self.tabs.index(tab)
                self.on_tab_close_clicked(button, tab_index)
        tab.close_button.connect("clicked", on_close_clicked)
        signal_map = {
            "load-changed": self.on_load_changed,
            "notify::title": self.on_title_changed,
            "decide-policy": self.on_decide_policy,
            "notify::favicon": self.on_favicon_changed,
        }
        for sig, handler in signal_map.items():
            webview.connect(sig, handler)
        return tab

    def toggle_tab_search(self):
        """Toggle the tab search interface."""
        if self.tab_search_visible:
            self.hide_tab_search()
        else:
            self.show_tab_search()

    def show_tab_search(self):
        """Show the tab search interface."""
        if self.tab_search_visible:
            return
        self.tab_search_overlay = Gtk.Overlay()
        self.tab_search_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.tab_search_box.set_valign(Gtk.Align.CENTER)
        self.tab_search_box.set_halign(Gtk.Align.CENTER)
        self.tab_search_box.set_size_request(400, 300)
        self.tab_search_box.add_css_class("search-overlay")
        self.tab_search_entry = Gtk.Entry()
        self.tab_search_entry.set_placeholder_text("Search tabs...")
        self.tab_search_entry.set_size_request(380, 40)
        self.tab_search_entry.connect("changed", self.on_tab_search_changed)
        self.tab_search_entry.connect("key-press-event", self.on_tab_search_key_press)
        self.tab_search_results_box = Gtk.ListBox()
        self.tab_search_results_box.set_size_request(380, 200)
        self.tab_search_results_box.add_css_class("search-results")
        self.tab_search_box.append(Gtk.Label(label="Search Tabs"))
        self.tab_search_box.append(self.tab_search_entry)
        self.tab_search_box.append(self.tab_search_results_box)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda x: self.hide_tab_search())
        self.tab_search_box.append(close_btn)
        main_content = self.window.get_content()
        if hasattr(main_content, 'get_first_child'):
            first_child = main_content.get_first_child()
            if first_child:
                main_content.remove(first_child)
                self.tab_search_overlay.set_child(first_child)
                self.tab_search_overlay.add_overlay(self.tab_search_box)
                main_content.append(self.tab_search_overlay)
                self.tab_search_visible = True
        self.tab_search_entry.grab_focus()
        self.update_tab_search_results("")

    def hide_tab_search(self):
        """Hide the tab search interface."""
        if not self.tab_search_visible:
            return
        main_content = self.window.get_content()
        if hasattr(main_content, 'get_first_child'):
            first_child = main_content.get_first_child()
        if first_child and hasattr(first_child, 'get_child'):
            child = first_child.get_child()
            if child:
                first_child.remove(child)
                main_content.remove(first_child)
                main_content.append(child)
                self.tab_search_visible = False
                self.tab_search_entry = None
                self.tab_search_results = []

    def on_tab_search_changed(self, entry):
        """Handle tab search text changes."""
        query = entry.get_text()
        self.update_tab_search_results(query)

    def on_tab_search_key_press(self, widget, event):
        """Handle key press in tab search."""
        if event.keyval == Gdk.KEY_Escape:
            self.hide_tab_search()
        elif event.keyval == Gdk.KEY_Return:
            if self.tab_search_results:
                self.switch_to_tab(self.tab_search_results[0])
                self.hide_tab_search()
        elif event.keyval in [Gdk.KEY_Up, Gdk.KEY_Down]:
            current_row = self.tab_search_results_box.get_selected_row()
            if event.keyval == Gdk.KEY_Up and current_row:
                prev_row = current_row.get_previous_sibling()
                if prev_row:
                    self.tab_search_results_box.select_row(prev_row)
        elif event.keyval == Gdk.KEY_Down:
            if current_row:
                next_row = current_row.get_next_sibling()
                if next_row:
                    self.tab_search_results_box.select_row(next_row)
                else:
                    first_row = self.tab_search_results_box.get_row_at_index(0)
                    if first_row:
                        self.tab_search_results_box.select_row(first_row)

    def update_tab_search_results(self, query):
        """Update tab search results based on query."""
        for child in list(self.tab_search_results_box):
            self.tab_search_results_box.remove(child)
        self.tab_search_results = []
        if not query:
            results = self.tabs[:]
        else:
            query_lower = query.lower()
            results = []
        for tab in self.tabs:
            if (query_lower in tab.title_label.get_text().lower() or
                query_lower in tab.url.lower() or
                (tab.group_name and query_lower in tab.group_name.lower())):
                results.append(tab)
        self.tab_search_results = results
        for tab in results:
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            if tab.thumbnail:
                thumbnail_img = Gtk.Picture()
                thumbnail_img.set_paintable(tab.thumbnail)
                thumbnail_img.set_size_request(80, 60)
                thumbnail_img.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                row_box.append(thumbnail_img)
            else:
                if tab.favicon:
                    row_box.append(tab.favicon)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=tab.title_label.get_text())
            title_label.set_halign(Gtk.Align.START)
            url_label = Gtk.Label(label=tab.url)
            url_label.set_halign(Gtk.Align.START)
            url_label.add_css_class("dim-label")
            vbox.append(title_label)
            vbox.append(url_label)
            row_box.append(vbox)
            if tab.group_name:
                group_label = Gtk.Label(label=f"[{tab.group_name}]")
                group_label.add_css_class("group-tag")
                row_box.append(group_label)
            if tab.is_pinned:
                pin_label = Gtk.Label(label="📌")
                row_box.append(pin_label)
            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            row.tab = tab
            row.connect("activate", lambda r: self.switch_to_tab(r.tab))
            self.tab_search_results_box.append(row)
        self.tab_search_results_box.show()

    def switch_to_tab(self, tab):
        """Switch to a specific tab."""
        if tab in self.tabs:
            tab_index = self.tabs.index(tab)
            self.notebook.set_current_page(tab_index)

    def pin_tab(self, tab):
        """Pin a tab."""
        if tab not in self.pinned_tabs:
            self.pinned_tabs.append(tab)
            tab.set_pinned(True)
            self.reorganize_tabs()

    def unpin_tab(self, tab):
        """Unpin a tab."""
        if tab in self.pinned_tabs:
            self.pinned_tabs.remove(tab)
            tab.set_pinned(False)
            self.reorganize_tabs()

    def reorganize_tabs(self):
        """Reorganize tabs to keep pinned tabs at the start."""
        current_tab = self.get_current_tab()
        pinned = [tab for tab in self.tabs if tab.is_pinned]
        unpinned = [tab for tab in self.tabs if not tab.is_pinned]
        self.tabs = pinned + unpinned
        for tab in self.tabs:
            self.notebook.reorder_child(tab.scrolled_window, self.tabs.index(tab))
        if current_tab:
            self.switch_to_tab(current_tab)

    def group_tabs(self, group_name, tabs):
        """Group multiple tabs together."""
        if group_name not in self.tab_groups:
            self.tab_groups[group_name] = []
        for tab in tabs:
            if tab not in self.tab_groups[group_name]:
                self.tab_groups[group_name].append(tab)
        tab.set_group(group_name)

    def ungroup_tab(self, tab):
        """Remove a tab from its group."""
        if tab.group_name and tab.group_name in self.tab_groups:
            self.tab_groups[tab.group_name].remove(tab)
            if not self.tab_groups[tab.group_name]:
                del self.tab_groups[tab.group_name]
            tab.set_group(None)

    def duplicate_tab(self, tab):
        """Duplicate a tab."""
        new_tab = self.add_new_tab(tab.url, background=True)
        if new_tab:
            if tab.is_pinned:
                self.pin_tab(new_tab)
            if tab.group_name:
                self.group_tabs(tab.group_name, [new_tab])

    def close_other_tabs(self, tab_to_keep):
        """Close all tabs except the specified one."""
        tabs_to_close = [tab for tab in self.tabs if tab != tab_to_keep and not tab.is_pinned]
        for tab in tabs_to_close:
            if tab in self.tabs:
                tab_index = self.tabs.index(tab)
                self.on_tab_close_clicked(None, tab_index)

    def close_tabs_to_right(self, tab):
        """Close all tabs to the right of the specified tab."""
        if tab in self.tabs:
            tab_index = self.tabs.index(tab)
            tabs_to_close = self.tabs[tab_index + 1:]
            for close_tab in tabs_to_close:
                if not close_tab.is_pinned:
                    close_tab_index = self.tabs.index(close_tab)
                    self.on_tab_close_clicked(None, close_tab_index)

    def update_all_thumbnails(self):
        """Update thumbnails for all tabs periodically."""
        current_time = time.time()
        if current_time - self.last_thumbnail_update < self.thumbnail_update_interval / 1000:
            return True
        self.last_thumbnail_update = current_time
        current_tab = self.get_current_tab()
        if current_tab and not current_tab.loading:
            current_tab.update_thumbnail()
        for tab in self.tabs:
            if tab != current_tab and not tab.loading:
                if not tab.is_pinned or current_time % 60 == 0:
                    tab.update_thumbnail()
        return True

    def get_current_tab(self):
        """Get the currently selected tab.
        Returns:
        Tab: The currently selected tab object, or None if no tabs exist.
        """
        if not self.tabs:
            return None
        current_page = self.notebook.get_current_page()
        if 0 <= current_page < len(self.tabs):
            return self.tabs[current_page]
        return None

    def close_current_tab(self):
        """Close the current tab."""
        current_tab = self.get_current_tab()
        if current_tab:
            tab_index = self.tabs.index(current_tab)
            self.on_tab_close_clicked(None, tab_index)

    def toggle_pin_current_tab(self):
        """Toggle pin state of current tab."""
        current_tab = self.get_current_tab()
        if current_tab:
            if current_tab.is_pinned:
                self.unpin_tab(current_tab)
            else:
                self.pin_tab(current_tab)

    def on_tab_close_clicked(self, button, tab_index):
        """Handle tab close button click."""
        if 0 <= tab_index < len(self.tabs):
            tab = self.tabs[tab_index]
            if tab == self.get_current_tab():
                if len(self.tabs) > 1:
                    new_index = max(0, tab_index - 1)
                    self.notebook.set_current_page(new_index)
            page = self.notebook.get_nth_page(tab_index)
        if hasattr(tab, 'webview') and tab.webview:
            webview = tab.webview
            for signal in [
                'load-changed',
                'notify::title',
                'decide-policy',
                'create'
            ]:
                if hasattr(webview, 'disconnect_by_func'):
                    handler = getattr(self, f'on_{signal.replace("-", "_").replace("::", "__")}', None)
        if handler:
                webview.disconnect_by_func(handler)
        if page is not None:
            removed_tab = self.tabs.pop(tab_index)
            removed_tab.cleanup()
            self.notebook.remove_page(tab_index)
            if removed_tab in self.pinned_tabs:
                self.pinned_tabs.remove(removed_tab)
            if removed_tab.group_name:
                self.ungroup_tab(removed_tab)
        if hasattr(removed_tab, 'webview'):
                removed_tab.webview = None
        if hasattr(removed_tab, 'destroy'):
            removed_tab.destroy()
        if not self.tabs:
            self.add_new_tab("about:blank")

    def on_load_changed(self, webview, load_event):
        """Handle page load events to update UI."""
        if not hasattr(self, 'loading_spinner') or not self.loading_spinner:
            return
        current_webview = self.get_current_webview()
        if load_event == WebKit.LoadEvent.COMMITTED:
            if webview == current_webview:
                if hasattr(self, 'url_entry') and self.url_entry:
                    current_url = webview.get_uri() or ""
                    self.url_entry.set_text(current_url)
                for tab in self.tabs:
                    if tab.webview == webview:
                        tab.url = current_url
                        if hasattr(tab, 'title_label') and tab.title_label and not webview.get_title():
                            tab.title_label.set_text(self.extract_tab_title(current_url))
                        break
                GLib.idle_add(self.loading_spinner.start)
                GLib.idle_add(lambda: self.loading_spinner.set_visible(True))
                return
        if load_event == WebKit.LoadEvent.FINISHED:
            current_url = webview.get_uri() or ""
            if hasattr(self, 'url_entry') and self.url_entry and webview == current_webview:
                self.url_entry.set_text(current_url)
            for tab in self.tabs:
                if tab.webview == webview:
                    tab.url = current_url
                    if hasattr(tab, 'title_label') and tab.title_label and not webview.get_title():
                        tab.title_label.set_text(self.extract_tab_title(current_url))
                    break
            GLib.idle_add(self.loading_spinner.stop)
            GLib.idle_add(lambda: self.loading_spinner.set_visible(False))
            if current_url and not current_url.startswith(('about:', 'data:')):
                self.update_history(current_url)
            url = webview.get_uri()
            if not url:
                return
            domain = self._extract_domain(url)
            if not domain:
                return
            favicon = webview.get_favicon()
            if favicon:
                self._update_tab_favicon(webview, favicon)
                self._favicon_cache[domain] = favicon
                self._save_favicon_cache()
                self._update_tabs_for_domain(domain, favicon)
            else:
                def fetch_favicon_on_load():
                    favicon = self.get_favicon(url)
                    if favicon:
                        self._update_tab_favicon(webview, favicon)
                        self._favicon_cache[domain] = favicon
                        self._save_favicon_cache()
                        self._update_tabs_for_domain(domain, favicon)
                GLib.idle_add(fetch_favicon_on_load)

    def on_title_changed(self, webview, param):
        """Update tab label when page title changes."""
        title = webview.get_title() or "Untitled"
        max_length = 10
        if len(title) > max_length:
            title = title[:max_length-3] + "..."
        for tab in self.tabs:
            if tab.webview == webview:
                if tab.title_label:
                    tab.title_label.set_text(title)
                break

    def on_webview_create(self, webview, navigation_action, window_features=None):
        """Handle creation of new webviews."""
        if window_features is None:
            return None
        new_webview = WebKit.WebView(
            settings=webview.get_settings(),
            user_content_manager=webview.get_user_content_manager()
        )
        new_webview.set_hexpand(True)
        new_webview.set_vexpand(True)
        new_webview.connect("load-changed", self.on_load_changed)
        new_webview.connect("notify::favicon", self.on_favicon_changed)
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
            if is_popup:
                self.open_popup_window(new_webview, window_features)
            else:
                self.add_webview_to_tab(new_webview)
            return new_webview
        except Exception:
            pass

    def is_internal_url_blocked(self, url, is_main_frame):
        """Check if an internal URL should be blocked.
        Args:
        url (str): The URL to check.
        is_main_frame (bool): Whether the request is for the main frame.
        Returns:
        bool: True if the URL should be blocked, False otherwise.
        """
        blocked_internal_urls = [
            "about:blank",
            "about:srcdoc",
            "blob:",
            "data:",
            "about:debug",
        ]
        if not url:
            return False
        if url == "about:blank" and not getattr(self, 'allow_about_blank', False):
            return True
        for pattern in blocked_internal_urls:
            if url.startswith(pattern):
                return True
        if not is_main_frame and any(url.startswith(prefix) for prefix in ("about:", "data:", "blob:", "_blank", "_data:")):
            return True
        return False

    def _handle_navigation_action(self, webview, decision, navigation_action):
        """Handle navigation action policy decision."""
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
        parsed = urlparse(requested_url)
        if parsed.scheme not in ("http", "https"):
            decision.ignore()
            return True
        if requested_url.lower().endswith(tuple(DOWNLOAD_EXTENSIONS)):
            self.start_manual_download(requested_url)
            decision.ignore()
            return True
        if not is_main_frame:
            top_level_url = webview.get_uri()
            if top_level_url:
                top_host = urlparse.urlparse(top_level_url).hostname
                req_host = parsed.hostname
                if top_host and req_host and top_host != req_host:
                    decision.ignore()
                    return True
        if self.adblocker.is_blocked(requested_url):
            decision.ignore()
            return True
        decision.use()
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
            script = WebKit.UserScript.new(
                cleanup_js,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.END,
                None,
                None
        )
            webview.get_user_content_manager().add_script(script)
        except Exception:
            pass
        decision.use()
        return True

    def _handle_new_window_action(self, webview, decision):
        """Handle new window action policy decision."""
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
        if url.lower().endswith(tuple(DOWNLOAD_EXTENSIONS)):
            self.start_manual_download(url)
            decision.ignore()
            return True
        user_content_manager = webview.get_user_content_manager()
        new_webview = WebKit.WebView(user_content_manager=user_content_manager)
        self.setup_webview_settings(new_webview)
        self.download_manager.add_webview(new_webview)
        if not hasattr(new_webview, "_create_signal_connected"):
            new_webview.connect("create", self.on_webview_create)
            new_webview._create_signal_connected = True
        if not hasattr(new_webview, "_decide_policy_connected"):
            new_webview.connect("decide-policy", self.on_decide_policy)
            new_webview._decide_policy_connected = True
        if not hasattr(new_webview, "_favicon_connected"):
            new_webview.connect("notify::favicon", self.on_favicon_changed)
            new_webview._favicon_connected = True
        self.add_webview_to_tab(new_webview)
        new_webview.load_uri(url)
        decision.ignore()
        return True

    def on_decide_policy(self, webview, decision, decision_type):
        """Handle navigation and new window actions, manage downloads, enforce policies, and apply adblock rules."""
        try:
            from gi.repository import WebKit
            try:
                if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                    return self._handle_navigation_action(
                        webview, decision, decision.get_navigation_action()
                    )
                elif decision_type == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
                    return self._handle_new_window_action(webview, decision)
                elif decision_type == WebKit.PolicyDecisionType.RESPONSE:
                    return self._handle_response_policy(webview, decision)
                else:
                    decision.use()
                    return True
            except Exception:
                decision.use()
                return True
        except Exception:
            decision.use()
            return True

    def add_download_spinner(self, toolbar):
        """Add enhanced download visual to toolbar."""
        if toolbar and hasattr(self, 'download_box'):
            toolbar.append(self.download_box)
            self.download_box.set_halign(Gtk.Align.END)
            self.download_box.set_valign(Gtk.Align.CENTER)
            self.download_box.set_margin_start(10)
            self.download_box.set_margin_end(10)
            self.download_box.set_visible(True)

    def start_manual_download(self, url):
        """Manually download a file from the given URL."""
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
                    'User-Agent': (
                        'Mozilla/5.0 (X11; Linux x86_64) '
                        'AppleWebKit/537.36 (KHTML like Gecko) '
                        'Chrome/91.0.4472.124 Safari/537.36'
                    )
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
                        filename = unquote(filename_match.group(1).strip('\'" '))
                    filename = sanitize_filename(filename)
                    if not filename:
                        filename = get_filename_from_url(parsed_url)
                    base_name, ext = os.path.splitext(filename)
                    if not ext:
                        ext = get_extension_from_content_type(response.headers.get('content-type', ''))
                    if ext:
                        filename = f"{base_name}{ext}"
                    downloads_dir = (
                        GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                        or os.path.expanduser("~/Downloads")
                    )
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
                    "downloaded": 0,
                    "cancelled": False,
                    "thread_id": threading.current_thread().ident,
                }
                self.download_manager.add_progress_bar(progress_info)
                with open(filepath, "wb") as f:
                    for chunk in response.iter_content(block_size):
                        if progress_info["cancelled"]:
                            break
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            progress = downloaded / total_size if total_size else 0
                            progress_info["downloaded"] = downloaded
                            GLib.idle_add(
                                self.download_manager.update_progress,
                                progress_info,
                                progress,
                            )
                if not progress_info["cancelled"]:
                    GLib.idle_add(
                        self.download_manager.download_finished,
                        progress_info
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
            finally:
                GLib.idle_add(
                    self.download_manager.cleanup_download,
                    progress_info["filename"],
                )
        thread = threading.Thread(
            target=download_thread,
            daemon=True,
            name=f"download_{url}"
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

    def extract_tab_title(self, url):
        """Extract a display title from a URL, limited to 15 characters."""
        max_length = 15
        try:
            parsed = urlparse.urlparse(url)
            title = parsed.netloc or "New Tab"
            if len(title) > max_length:
                title = title[: max_length - 3] + "..."
            return title
        except Exception:
            return "New Tab"

    def save_session(self):
        """Save current browser session."""
        try:
            session_data = [
                {
                    "url": tab.url,
                    "title": tab.title_label.get_text() if hasattr(tab, 'title_label') and tab.title_label else "",
        }
                for tab in self.tabs
            ]
            self.save_json(SESSION_FILE, session_data)
        except Exception:
            pass

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

    def safe_window_cleanup(self):
        """Ensure proper window cleanup to prevent GTK warnings."""
        if not hasattr(self, 'window') or not self.window:
            return
        try:
            if hasattr(self.window, 'disconnect_by_func'):
                try:
                    self.window.disconnect_by_func(self.on_window_destroy)
                except Exception:
                    pass
            if hasattr(self.window, 'get_child'):
                child = self.window.get_child()
                if child:
                    try:
                        if hasattr(self.window, 'remove') and callable(self.window.remove):
                            self.window.remove(child)
                        elif hasattr(self.window, 'set_child') and callable(self.window.set_child):
                            self.window.set_child(None)
                    except Exception:
                        pass
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None
        except Exception:
            pass

    def cleanup_widgets(self):
        """Clean up all widgets to prevent GTK warnings."""
        for tab in self.tabs:
            if hasattr(tab, 'webview') and tab.webview:
                tab.webview = None
            if hasattr(tab, 'title_label') and tab.title_label:
                tab.title_label = None
            if hasattr(tab, 'close_button') and tab.close_button:
                tab.close_button = None
            if hasattr(tab, 'header_box') and tab.header_box:
                tab.header_box = None
        self.tabs.clear()
        if hasattr(self, 'notebook') and self.notebook:
            try:
                while self.notebook.get_n_pages() > 0:
                    try:
                        page = self.notebook.get_nth_page(0)
                        if page:
                            self.notebook.remove_page(0)
                            if hasattr(page, 'destroy') and callable(page.destroy):
                                page.destroy()
                    except Exception:
                        pass
            except Exception:
                pass
            if hasattr(self, 'notebook'):
                self.notebook = None

    def disconnect_all_signals(self):
        """Disconnect all signals to prevent GTK warnings."""
        pass

    def on_window_destroy(self, window):
        """Handle window closure with proper cleanup."""
        try:
            self.save_session()
            self.save_tabs()
            if self.is_restart_active():
                self.stop_continuous_restart()
            self.cleanup_widgets()
            self.disconnect_all_signals()
            if hasattr(self, '_popup_windows'):
                try:
                    for popup in self._popup_windows:
                        try:
                            popup.destroy()
                        except Exception:
                            pass
                    self._popup_windows.clear()
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
            scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scrolled_window.set_vexpand(True)
            scrolled_window.set_hexpand(True)
            scrolled_window.set_min_content_width(800)
            scrolled_window.set_min_content_height(600)
            scrolled_window.set_child(new_webview)
            label = Gtk.Label(label=self.extract_tab_title(url))
            close_button = Gtk.Button()
            close_button.set_icon_name("window-close")
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
            close_button.connect("clicked", on_close_clicked)
            new_webview.connect("load-changed", self.on_load_changed)
            new_webview.connect("notify::title", self.on_title_changed)
            new_webview.connect("decide-policy", self.on_decide_policy)
            new_webview.connect("create", self.on_webview_create)
            new_webview.connect("notify::favicon", self.on_favicon_changed)
        except Exception:
            pass

    def add_webview_to_tab(self, webview):
        """Add a webview to a new tab."""
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_child(webview)
        label = Gtk.Label(label=self.extract_tab_title(webview.get_uri()))
        close_button = Gtk.Button.new_from_icon_name("window-close")
        close_button.set_size_request(24, 24)
        tab = Tab(webview.get_uri(), webview)
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
        self.tabs.append(tab)
        close_button.connect("clicked", on_close_clicked)
        self.notebook.set_current_page(index)
        return tab

    def open_popup_window(self, webview, window_features):
        """Open a popup window with the given webview."""
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
        close_button = Gtk.Button()
        close_button.set_icon_name("window-close")
        close_button.set_size_request(24, 24)
        close_button.set_tooltip_text("Close popup")
        window._webview = webview
        window._close_button = close_button
        window._vbox = vbox

        def on_popup_destroy(widget):
            if hasattr(window, '_webview'):
                window._webview = None
            if hasattr(window, '_close_button'):
                window._close_button = None
            if hasattr(window, '_vbox'):
                window._vbox = None
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
        window.set_child(vbox)
        if not hasattr(self, '_popup_windows'):
            self._popup_windows = []
        self._popup_windows.append(window)

        def cleanup_window_reference(widget):
            if hasattr(self, '_popup_windows'):
                if window in self._popup_windows:
                    self._popup_windows.remove(window)
        window.connect("destroy", cleanup_window_reference)
        window.present()

    def load_html_with_bootstrap(self, html):
        """
        Load HTML content into the current webview with Bootstrap CSS linked in the head.
        If Bootstrap CSS link is not present, it will be injected.
        """
        webview = self.get_current_webview()
        if not webview:
            return

    def inject_css_adblock(self):
        """Inject CSS to hide ad elements."""
        css = """
        /* Hide common ad containers */
        div[class*="ad"]:not(.player-container, #player, .controls):not([class*="player"]):not([class*="video"]):not([class*="media"]),
        div[id*="ad"]:not(.player-container, #player, .controls):not([id*="player"]):not([id*="video"]):not([id*="media"]),
        div[class*="ads"]:not(.player-container, #player, .controls):not([class*="player"]):not([class*="video"]):not([class*="media"]),
        div[id*="ads"]:not(.player-container, #player, .controls):not([id*="player"]):not([id*="video"]):not([id*="media"]),
        div[class*="banner"]:not(.player-container, #player, .controls):not([class*="player"]):not([class*="video"]):not([class*="media"]),
        div[id*="banner"]:not(.player-container, #player, .controls):not([id*="player"]):not([id*="video"]):not([id*="media"]),
        div[class*="popup"]:not(.player-container, #player, .controls):not([class*="player"]):not([class*="video"]):not([class*="media"]),
        div[id*="popup"]:not(.player-container, #player, .controls):not([id*="player"]):not([id*="video"]):not([id*="media"]),
        div[class*="modal"]:not(.player-container, #player, .controls):not([class*="player"]):not([class*="video"]):not([class*="media"]),
        div[id*="modal"]:not(.player-container, #player, .controls):not([id*="player"]):not([id*="video"]):not([id*="media"]),
        div[class*="overlay"]:not(.player-container, #player, .controls):not([class*="player"]):not([class*="video"]):not([class*="media"]),
        div[id*="overlay"]:not(.player-container, #player, .controls):not([id*="player"]):not([id*="video"]):not([id*="media"]),
        div[class*="sponsored"]:not(.player-container, #player, .controls):not([class*="player"]):not([class*="video"]):not([class*="media"]),
        div[id*="sponsored"]:not(.player-container, #player, .controls):not([id*="player"]):not([id*="video"]):not([id*="media"]),
        div[class*="commercial"]:not(.player-container, #player, .controls):not([class*="player"]):not([class*="video"]):not([class*="media"]),
        div[id*="commercial"]:not(.player-container, #player, .controls):not([id*="player"]):not([id*="video"]):not([id*="media"]),
        iframe[src*="ad"], iframe[src*="ads"], iframe[src*="banner"], iframe[src*="popup"],
        iframe[name*="ad"], iframe[name*="ads"], iframe[name*="banner"], iframe[name*="popup"],
        img[src*="ad"]:not([src*="download"]):not([src*="player"]):not([src*="video"]),
        img[src*="ads"]:not([src*="download"]):not([src*="player"]):not([src*="video"]),
        img[src*="banner"]:not([src*="download"]):not([src*="player"]):not([src*="video"]),
        .ad, .ads, .advert, .advertisement, .banner, .promo, .sponsored,
        .ad-container, .ad-wrapper, .adbox, .adslot, .adsbox, .ad-section,
        .google-ad, .adsense, .doubleclick, .advertising, .commercial,
        .popup-ad, .modal-ad, .overlay-ad, .banner-ad,
        [data-ad], [data-ads], [data-advertisement], [data-banner],
        [data-adunit], [data-adslot], [data-adzone],
        .adnxs, .adsystem, .adserver, .adnetwork,
        #ad, #ads, #advert, #advertisement, #banner, #promo, #sponsored,
        #ad-container, #ad-wrapper, #adbox, #adslot, #adsbox, #ad-section,
        #google-ad, #adsense, #doubleclick, #advertising, #commercial,
        .ad-container *, .ad-wrapper *, .adbox *, .adslot *, .adsbox *,
        .google-ads, .google-adSense, .google-adwords,
        .amazon-ads, .facebook-ads, .twitter-ads,
        .outbrain, .taboola, .disqus, .addthis,
        .pubmatic, .rubicon, .criteo, .adnxs,
        .ad-placement, .ad-unit, .ad-slot, .ad-zone,
        .sponsored-content, .sponsored-links, .sponsored-posts,
        .promotion, .promotions, .promo-content,
        .marketing, .marketing-banner, .marketing-content,
        .partner, .partners, .partner-content,
        .affiliate, .affiliates, .affiliate-content {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
        position: absolute !important;
        left: -9999px !important;
        top: -9999px !important;
        width: 0 !important;
        height: 0 !important;
        overflow: hidden !important;
        z-index: -9999 !important;
        }
        /* Hide ads by position and size */
        div[style*="position: fixed"][style*="z-index: 9999"],
        div[style*="position: fixed"][style*="z-index: 99999"],
        div[style*="position: absolute"][style*="bottom: 0"][style*="left: 50%"],
        div[style*="position: fixed"][style*="bottom: 0"][style*="right: 0"],
        div[style*="width: 300px"][style*="height: 250px"],
        div[style*="width: 728px"][style*="height: 90px"],
        div[style*="width: 160px"][style*="height: 600px"],
        div[style*="width: 336px"][style*="height: 280px"],
        div[style*="width: 970px"][style*="height: 90px"],
        div[style*="width: 970px"][style*="height: 250px"] {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
        }
        /* Hide empty ad containers */
        div:empty, iframe:empty, script:empty, noscript:empty {
        display: none !important;
        }
        /* Hide elements with ad-related attributes */
        [onclick*="ad"], [onclick*="popup"], [onclick*="modal"],
        [href*="ad"], [href*="popup"], [href*="modal"],
        [src*="ad"], [src*="popup"], [src*="modal"],
        [data-ad-click], [data-ad-view], [data-ad-impression] {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
        }
        """
        style = WebKit.UserStyleSheet.new(
        css,
        WebKit.UserContentInjectedFrames.TOP_FRAME,
        WebKit.UserStyleSheetLevel.USER,
        [], []
        )
        self.content_manager.add_style_sheet(style)

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
        script = WebKit.UserScript.new(
        script_source,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.END,
        )
        self.content_manager.add_script(script)

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
        script = WebKit.UserScript.new(
        script_source,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.END,
        )
        self.content_manager.add_script(script)

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
        meta.content = "script-src 'nonce-" + nonce + "' 'strict-dynamic' 'unsafe-inline' 'self'";
        document.head.appendChild(meta);
        }
        }
        })();
        """
        script = WebKit.UserScript.new(
        script_source,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        )
        self.content_manager.add_script(script)

    def disable_biometrics_in_webview(self, webview):
        """
        Injects JavaScript into the WebKitGTK WebView to block WebAuthn biometric prompts.
        This disables navigator.credentials.get/create with publicKey options.
        """
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
        user_script = WebKit.UserScript.new(
        script,
        WebKit.UserContentInjectedFrames.TOP_FRAME,
        WebKit.UserScriptInjectionTime.START,
        [], []
        )
        webview.get_user_content_manager().add_script(user_script)

    def block_biometric_apis(self, webview: WebKit.WebView):
        """
        Blocks WebAuthn biometric APIs and navigator.sendBeacon() in WebKitGTK browser.
        This method injects JavaScript to prevent fingerprinting through WebAuthn and
        blocks the sendBeacon API which can be used for tracking. It provides a clean
        rejection message without cluttering the console with warnings.
        Args:
        webview: The WebKit.WebView instance to apply the blocking to
        """
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
        user_script = WebKit.UserScript.new(
        script,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        [],
        []
        )
        content_manager = webview.get_user_content_manager()
        content_manager.remove_all_scripts()
        content_manager.add_script(user_script)

    def inject_anti_fingerprinting_script(self, user_content_manager):
        """Inject anti-fingerprinting JavaScript."""
        script = """
        (function() {
        try {
        Object.defineProperty(navigator, 'userAgent', { get: function() { return 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36'; } });
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
        script_obj = WebKit.UserScript.new(
        script,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        None, None
        )
        user_content_manager.add_script(script_obj)

    def inject_tcf_api(self, user_content_manager):
        """Inject dummy TCF (Transparency and Consent Framework) API for CMP compatibility."""
        tcf_script = """
        (function() {
        'use strict';
        // Create dummy TCF API to prevent sites from breaking due to missing CMP
        window.__tcfapi = function(command, version, callback, parameter) {
        try {
        // Simulate a fully consented state for all TCF commands
        if (command === 'getTCData') {
        callback({
        tcString: 'CP0524100524100.AABFGA.ENAAAAAAABIAAAAAAA.YAAAAAAAAAAA',
        gdprApplies: true,
        cmpId: 0,
        cmpVersion: 0,
        tcfPolicyVersion: 2,
        isServiceSpecific: false,
        useNonStandardStacks: false,
        purposeConsents: {
        '1': true, '2': true, '3': true, '4': true, '5': true,
        '6': true, '7': true, '8': true, '9': true, '10': true
        },
        purposeLegitimateInterests: {},
        vendorConsents: {},
        vendorLegitimateInterests: {},
        specialFeatureOptins: {
        '1': true, '2': true
        },
        publisherConsents: {},
        publisherLegitimateInterests: {},
        publisherCustomConsents: {},
        publisherCustomLegitimateInterests: {},
        purposeOneTreatment: false,
        publisherCC: 'US',
        outOfBand: {
        allowedVendors: {},
        disclosedVendors: {}
        },
        eventStatus: 'tcloaded'
        }, true);
        } else if (command === 'ping') {
        callback({
        gdprAppliesGlobally: true,
        cmpLoaded: true
        }, true);
        } else if (command === 'addEventListener') {
        // Immediately call back with tcloaded event
        callback({
        tcString: 'CP0524100524100.AABFGA.ENAAAAAAABIAAAAAAA.YAAAAAAAAAAA',
        gdprApplies: true,
        cmpId: 0,
        cmpVersion: 0,
        tcfPolicyVersion: 2,
        isServiceSpecific: false,
        useNonStandardStacks: false,
        purposeConsents: {
        '1': true, '2': true, '3': true, '4': true, '5': true,
        '6': true, '7': true, '8': true, '9': true, '10': true
        },
        purposeLegitimateInterests: {},
        vendorConsents: {},
        vendorLegitimateInterests: {},
        specialFeatureOptins: {
        '1': true, '2': true
        },
        publisherConsents: {},
        publisherLegitimateInterests: {},
        publisherCustomConsents: {},
        publisherCustomLegitimateInterests: {},
        purposeOneTreatment: false,
        publisherCC: 'US',
        outOfBand: {
        allowedVendors: {},
        disclosedVendors: {}
        },
        eventStatus: 'tcloaded'
        }, true);
        } else if (command === 'removeEventListener') {
        // No-op for removeEventListener
        callback(false, true);
        } else {
        // Default response for unknown commands
        callback({}, true);
        }
        } catch (e) {
        // Fallback in case of errors
        callback({}, true);
        }
        };
        // Also provide a stub for the CMP locator
        if (!window.__cmp) {
        window.__cmp = function(command, parameter, callback) {
        if (command === 'getConsentString') {
        callback('CP0524100524100.AABFGA.ENAAAAAAABIAAAAAAA.YAAAAAAAAAAA', true);
        } else if (command === 'getVendorConsents') {
        callback({}, true);
        } else {
        callback(null, false);
        }
        };
        }
        // Dispatch a custom event to simulate CMP loading
        setTimeout(function() {
        var event = new CustomEvent('tcloaded', {
        detail: {
        tcString: 'CP0524100524100.AABFGA.ENAAAAAAABIAAAAAAA.YAAAAAAAAAAA',
        gdprApplies: true
        }
        });
        window.dispatchEvent(event);
        // Also dispatch cmpapi-loaded event
        var cmpApiEvent = new CustomEvent('cmpapi-loaded');
        window.dispatchEvent(cmpApiEvent);
        }, 100);
        console.log('[ShadowBrowser] Dummy TCF API injected for CMP compatibility');
        })();
        """
        user_script = WebKit.UserScript.new(
        tcf_script,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        None, None
        )
        user_content_manager.add_script(user_script)

    def inject_js_execution_fix(self, user_content_manager):
        """Inject script to fix JavaScript execution issues caused by restrictive CSP."""
        js_fix_script = """
        (function() {
        'use strict';
        // Remove restrictive CSP meta tags
        function removeRestrictiveCSP() {
        const cspMetas = document.querySelectorAll('meta[http-equiv="Content-Security-Policy"]');
        cspMetas.forEach(meta => {
        const content = meta.getAttribute('content') || '';
        // Only remove CSPs that block external scripts
        if (content.includes("script-src 'self'") && !content.includes('https:')) {
        meta.remove();
        console.log('[ShadowBrowser] Removed restrictive CSP meta tag');
        }
        });
        }
        // Fix MIME type issues by removing nosniff headers
        function fixMimeTypes() {
        // Override fetch to handle MIME type issues
        const originalFetch = window.fetch;
        window.fetch = function(...args) {
        return originalFetch.apply(this, args).catch(error => {
        // If it's a MIME type error, try again with different approach
        if (error.message && error.message.includes('MIME')) {
        console.log('[ShadowBrowser] Attempting to bypass MIME type restriction');
        // Create a new request without strict MIME checking
        const [url, options] = args;
        const newOptions = {...options};
        newOptions.headers = {...newOptions.headers};
        delete newOptions.headers['Accept'];
        return originalFetch.call(this, url, newOptions);
        }
        throw error;
        });
        };
        }
        // Fix cross-origin iframe issues
        function fixCrossOriginIssues() {
        // Override postMessage to handle cross-origin communication
        const originalPostMessage = window.postMessage;
        window.postMessage = function(message, targetOrigin, transfer) {
        try {
        return originalPostMessage.call(this, message, targetOrigin, transfer);
        } catch (e) {
        console.log('[ShadowBrowser] Cross-origin postMessage blocked:', e);
        // Silently fail to prevent page breakage
        }
        };
        // Fix iframe access issues - but avoid video players
        const iframes = document.querySelectorAll('iframe');
        iframes.forEach(iframe => {
        try {
        // Skip iframes that might be video players
        const src = iframe.src || '';
        const parent = iframe.parentElement;
        const parentClass = parent ? parent.className || '' : '';
        const parentId = parent ? parent.id || '' : '';
        // Skip video-related iframes to avoid VideoJS issues
        if (src.includes('video') || src.includes('player') ||
        src.includes('youtube') || src.includes('vimeo') ||
        parentClass.includes('video') || parentClass.includes('player') ||
        parentId.includes('video') || parentId.includes('player') ||
        iframe.className.includes('video') || iframe.id.includes('video')) {
        return;
        }
        // Remove sandbox restrictions that break functionality
        if (iframe.hasAttribute('sandbox')) {
        const sandbox = iframe.getAttribute('sandbox');
        // Allow scripts and same-origin access
        if (sandbox.includes('allow-scripts')) {
        iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
        }
        }
        } catch (e) {
        // Ignore iframe access errors
        }
        });
        }
        // Add VideoJS-specific helper to ensure proper DOM attachment
        function fixVideoJSDomIssues() {
        // Suppress VideoJS warnings immediately, before VideoJS loads
        const originalConsoleWarn = console.warn;
        const originalConsoleError = console.error;
        console.warn = function(...args) {
        const message = args.join(' ');
        if (message.includes('VIDEOJS') &&
        (message.includes('element supplied is not included in the DOM') ||
        message.includes('The element supplied is not included in the DOM') ||
        (message.includes('WARN:') && message.includes('element supplied is not included in the DOM')))) {
        // Suppress this specific warning since we're handling it
        return;
        }
        return originalConsoleWarn.apply(console, args);
        };
        console.error = function(...args) {
        const message = args.join(' ');
        if (message.includes('VIDEOJS') &&
        (message.includes('element supplied is not included in the DOM') ||
        (message.includes('WARN:') && message.includes('element supplied is not included in the DOM')))) {
        // Suppress this specific error since we're handling it
        return;
        }
        return originalConsoleError.apply(console, args);
        };
        // Override VideoJS initialization to ensure DOM readiness
        if (window.videojs) {
        const originalVideoJS = window.videojs;
        window.videojs = function(element, options, ready) {
        // Ensure element is in DOM before initialization
        if (element && typeof element === 'string') {
        element = document.querySelector(element);
        }
        if (element && !document.contains(element)) {
        console.log('[ShadowBrowser] VideoJS element not in DOM, waiting for attachment...');
        // Use a more robust approach with timeout fallback
        let attempts = 0;
        const maxAttempts = 50; // 5 seconds max wait
        function checkAndInit() {
        if (document.contains(element)) {
        console.log('[ShadowBrowser] VideoJS element found in DOM, initializing...');
        return originalVideoJS(element, options, ready);
        }
        attempts++;
        if (attempts < maxAttempts) {
        setTimeout(checkAndInit, 100);
        } else {
        console.log('[ShadowBrowser] VideoJS element never appeared in DOM, forcing initialization');
        return originalVideoJS(element, options, ready);
        }
        }
        checkAndInit();
        return;
        }
        return originalVideoJS(element, options, ready);
        };
        } else {
        // VideoJS might not be loaded yet, set up a watcher
        let videojsCheckCount = 0;
        const maxChecks = 100; // Check for 10 seconds
        function checkForVideoJS() {
        if (window.videojs) {
        const originalVideoJS = window.videojs;
        window.videojs = function(element, options, ready) {
        if (element && typeof element === 'string') {
        element = document.querySelector(element);
        }
        if (element && !document.contains(element)) {
        console.log('[ShadowBrowser] VideoJS element not in DOM, waiting for attachment...');
        let attempts = 0;
        const maxAttempts = 50;
        function checkAndInit() {
        if (document.contains(element)) {
        console.log('[ShadowBrowser] VideoJS element found in DOM, initializing...');
        return originalVideoJS(element, options, ready);
        }
        attempts++;
        if (attempts < maxAttempts) {
        setTimeout(checkAndInit, 100);
        } else {
        console.log('[ShadowBrowser] VideoJS element never appeared in DOM, forcing initialization');
        return originalVideoJS(element, options, ready);
        }
        }
        checkAndInit();
        return;
        }
        return originalVideoJS(element, options, ready);
        };
        } else {
        videojsCheckCount++;
        if (videojsCheckCount < maxChecks) {
        setTimeout(checkForVideoJS, 100);
        }
        }
        }
        checkForVideoJS();
        }
        // Monitor DOM for video elements being added
        const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
        mutation.addedNodes.forEach(function(node) {
        if (node.nodeType === Node.ELEMENT_NODE) {
        // Check for video elements or VideoJS containers
        if (node.tagName === 'VIDEO' ||
        (node.className && node.className.includes('video-js')) ||
        (node.id && node.id.includes('video'))) {
        // Ensure the element is properly attached
        if (node.parentNode && document.contains(node)) {
        // Element is good, no action needed
        } else {
        console.warn('[ShadowBrowser] Video element found without proper parent');
        }
        }
        }
        });
        });
        });
        // Start observing the entire document
        observer.observe(document.documentElement || document.body, {
        childList: true,
        subtree: true
        });
        // Also check for existing video elements that might be in limbo
        setTimeout(function() {
        const videos = document.querySelectorAll('video, [class*="video-js"], [id*="video"]');
        videos.forEach(function(video) {
        if (!document.contains(video)) {
        console.warn('[ShadowBrowser] Found orphaned video element, attempting to reattach');
        // Try to find a suitable parent
        const container = video.closest('.video-container') || document.body;
        if (container && container !== video) {
        container.appendChild(video);
        }
        }
        });
        }, 500);
        }
        // Fix font loading issues
        function fixFontLoading() {
        // Create a style to handle font loading gracefully
        const style = document.createElement('style');
        style.textContent = `
        @font-face {
        font-family: 'fallback-font';
        src: local('Arial'), local('Helvetica'), local('sans-serif');
        }
        * {
        font-display: swap !important;
        }
        `;
        document.head.appendChild(style);
        }
        // Run all fixes
        if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
        removeRestrictiveCSP();
        fixMimeTypes();
        fixCrossOriginIssues();
        fixVideoJSDomIssues();
        fixFontLoading();
        });
        } else {
        removeRestrictiveCSP();
        fixMimeTypes();
        fixCrossOriginIssues();
        fixVideoJSDomIssues();
        fixFontLoading();
        }
        // Also run after a delay to catch dynamically added elements
        setTimeout(function() {
        fixVideoJSDomIssues();
        removeRestrictiveCSP();
        fixCrossOriginIssues();
        fixFontLoading();
        }, 1000);
        console.log('[ShadowBrowser] JavaScript execution fixes applied');
        })();
        """
        user_script = WebKit.UserScript.new(
        js_fix_script,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        None, None
        )
        user_content_manager.add_script(user_script)

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
        user_script = WebKit.UserScript.new(
        script,
        WebKit.UserContentInjectedFrames.ALL_FRAMES,
        WebKit.UserScriptInjectionTime.START,
        None, None
        )
        user_content_manager.add_script(user_script)

    def DNT(self):
        """Inject Do Not Track header."""
        dnt_script = """
        Object.defineProperty(navigator, 'doNotTrack', {
        get: function() { return '1'; }
        });
        """
        user_script = WebKit.UserScript.new(
        dnt_script,
        WebKit.UserContentInjectedFrames.TOP_FRAME,
        WebKit.UserScriptInjectionTime.START,
        [], []
        )
        webview = self.get_current_webview()
        if webview:
            content_manager = webview.get_user_content_manager()
            content_manager.add_script(user_script)

    def _create_http_session(self):
        """
        Create a configured requests session with retries, timeouts, and optional Tor routing.
        Returns:
        requests.Session: Configured session object
        """
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

        # Apply Tor proxy settings if enabled
        if self.tor_enabled and hasattr(self, 'tor_manager') and self.tor_manager.is_connected:
            proxy_uri = f"socks5h://127.0.0.1:{self.tor_manager.tor_port}"
            session.proxies = {
                'http': proxy_uri,
                'https': proxy_uri
            }

        return session

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
        """Handle the result of the Turnstile check."""
        js_result = webview.run_javascript_finish(result)
        if js_result:
            value = js_result.get_js_value()
            if not value.is_null():
                self.handle_turnstile(value)

    def handle_turnstile(self, turnstile_element):
        """Handle the Turnstile element."""
        if turnstile_element:
            self.webview.run_javascript("turnstileElement.submit();", None, None, None)

    def load_page(self):
        """Load the current URL in the webview with a random delay."""
        self.webview.load_uri(self.url)
        time.sleep(random.uniform(2, 5))

    def navigate_to(self, path):
        """Navigate to a relative path from the current URL."""
        new_url = f"{self.url.rstrip('/')}/{path.lstrip('/')}"
        self.webview.load_uri(new_url)
        time.sleep(random.uniform(2, 5))

    def _create_default_favicon(self):
        """Create a better default favicon with globe appearance."""
        try:
            pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 16, 16)
            pixbuf.fill(0x00000000)
            for y in range(16):
                for x in range(16):
                    dx = x - 8
                    dy = y - 8
                    if dx*dx + dy*dy <= 36:
                        pixel = 0xFF2E5F88
                        pixels = pixbuf.get_pixels()
                        offset = (y * 16 + x) * 4
                        pixels = pixels[:offset] + pixel.to_bytes(4, 'big') + pixels[offset+4:]
                        pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                            pixels, GdkPixbuf.Colorspace.RGB, True, 8, 16, 16, 64
                        )
            return self._texture_from_pixbuf(pixbuf)
        except Exception:
            fallback_pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 16, 16)
            return self._texture_from_pixbuf(fallback_pixbuf)

    def _get_favicon_fallback(self, domain, fallback_type='default'):
        """Get a fallback favicon for a domain."""
        if domain not in self._favicon_fallbacks:
            self._favicon_fallbacks[domain] = self._create_favicon_fallbacks(domain)
        return self._favicon_fallbacks[domain].get(fallback_type, self.default_favicon)

    def _create_favicon_fallbacks(self, domain):
        """Create multiple fallback favicons for different scenarios."""
        fallbacks = {}
        fallbacks['default'] = self.default_favicon
        try:
            color_hash = abs(hash(domain)) % 0xFFFFFF
            color_pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 16, 16)
            color_pixbuf.fill(0x00000000)
            for y in range(16):
                for x in range(16):
                    dx = x - 8
                    dy = y - 8
                    if dx*dx + dy*dy <= 36:
                        pixel = (0xFF << 24) | color_hash
                        pixels = color_pixbuf.get_pixels()
                        offset = (y * 16 + x) * 4
                        pixels = pixels[:offset] + pixel.to_bytes(4, 'big') + pixels[offset+4:]
                        color_pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                            pixels, GdkPixbuf.Colorspace.RGB, True, 8, 16, 16, 64
                        )
                        texture = self._texture_from_pixbuf(color_pixbuf)
                        if texture:
                            fallbacks['domain_color'] = texture
        except Exception:
            fallbacks['domain_color'] = fallbacks['default']
        try:
            letter = domain[0].upper() if domain else '?'
            letter_pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 16, 16)
            letter_pixbuf.fill(0x00000000)
            letter_value = ord(letter) if letter != '?' else 63
            letter_color = 0xFF400000 + ((letter_value * 7) % 0xFFFFFF)
            for y in range(16):
                for x in range(16):
                    dx = x - 8
                    dy = y - 8
                    if dx*dx + dy*dy <= 36:
                        pixels = letter_pixbuf.get_pixels()
                        offset = (y * 16 + x) * 4
                        pixels = pixels[:offset] + letter_color.to_bytes(4, 'big') + pixels[offset+4:]
                        letter_pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                            pixels, GdkPixbuf.Colorspace.RGB, True, 8, 16, 16, 64
                        )
                texture = self._texture_from_pixbuf(letter_pixbuf)
                if texture:
                    fallbacks['letter'] = texture
        except Exception:
            fallbacks['letter'] = fallbacks['default']
        return fallbacks

    def _should_retry_favicon(self, domain):
        """Check if favicon fetching should be retried."""
        retry_count = self._favicon_retry_counts.get(domain, 0)
        return retry_count < self._favicon_max_retries

    def _increment_favicon_retry(self, domain):
        """Increment favicon retry count for a domain."""
        self._favicon_retry_counts[domain] = self._favicon_retry_counts.get(domain, 0) + 1

    def on_favicon_changed(self, webview, param):
        """Handle favicon changes from WebKit."""
        favicon = webview.get_favicon()
        url = webview.get_uri()
        if favicon and url:
            self._update_tab_favicon(webview, favicon)
            domain = self._extract_domain(url)
            if domain:
                self._favicon_cache[domain] = favicon
                self._save_favicon_cache()
                self._update_tabs_for_domain(domain, favicon)
        elif url and not favicon:
            domain = self._extract_domain(url)
            if domain:
                def fetch_missing_favicon():
                    favicon = self.get_favicon(url)
                    if favicon:
                        self._update_tab_favicon(webview, favicon)
                        self._update_tabs_for_domain(domain, favicon)
                        self._favicon_cache[domain] = favicon
                        self._save_favicon_cache()
                GLib.idle_add(fetch_missing_favicon)

    def _update_tab_favicon(self, webview, favicon):
        """Update the favicon display for a tab."""
        if not favicon or not webview:
            return
        tab = self.get_tab_for_webview(webview)
        if not tab:
            return

        def safe_update_favicon():
            try:
                if not hasattr(tab, 'favicon') or not tab.favicon:
                    return
                if hasattr(tab.favicon, '_destroying') and tab.favicon._destroying:
                    return
                if not tab.favicon.get_parent():
                    return
                if hasattr(tab.favicon, 'set_paintable'):
                    tab.favicon.set_visible(True)
                    tab.favicon.set_paintable(favicon)
                    tab.favicon.set_size_request(16, 16)
                    tab.favicon.set_halign(Gtk.Align.CENTER)
                    tab.favicon.set_valign(Gtk.Align.CENTER)
                    tab.favicon.queue_draw()
                    if (hasattr(tab, 'header_box') and
                        tab.header_box and
                        hasattr(tab.header_box, 'set_tooltip_text') and
                        webview.get_uri()):
                        tab.header_box.set_tooltip_text(webview.get_uri())
            except Exception:
                pass
        GLib.idle_add(safe_update_favicon)

    def _update_tabs_for_domain(self, domain, favicon):
        """Update favicon for all tabs with the same domain."""
        def safe_update_tab_favicon(tab, favicon):
            try:
                if not hasattr(tab, 'favicon') or not tab.favicon:
                    return
                if hasattr(tab.favicon, 'set_paintable'):
                    tab.favicon.set_visible(True)
                    tab.favicon.set_paintable(favicon)
                    tab.favicon.set_size_request(16, 16)
                    tab.favicon.set_halign(Gtk.Align.CENTER)
                    tab.favicon.set_valign(Gtk.Align.CENTER)
                    tab.favicon.queue_draw()
                    if hasattr(tab, 'label_box') and tab.label_box:
                        tab.label_box.queue_draw()
                    if hasattr(tab, 'header_box') and tab.header_box:
                        tab.header_box.queue_draw()
            except Exception:
                pass
        updated_count = 0
        for tab in self.tabs:
            if hasattr(tab, 'url') and tab.url:
                tab_domain = self._extract_domain(tab.url)
                if tab_domain == domain:
                    GLib.idle_add(safe_update_tab_favicon, tab, favicon)
                    updated_count += 1
        if updated_count > 0:
            print(f"Updated favicon for {updated_count} tabs with domain {domain}")

    def _update_bookmarks_for_domain(self, domain, favicon):
        """Update favicon for bookmarks with the same domain."""
        has_matching_bookmark = False
        matching_bookmarks = []

        def get_base_domain(full_domain):
            if not full_domain:
                return None
            parts = full_domain.split('.')
            if len(parts) >= 2:
                return parts[-2]
            return parts[0] if parts else None
        target_base_domain = get_base_domain(domain)
        for bookmark in self.bookmarks:
            if isinstance(bookmark, str):
                bookmark = {"url": bookmark, "title": None}
            url = bookmark.get("url")
            if url:
                bookmark_domain = self._extract_domain(url)
                if bookmark_domain == domain:
                    has_matching_bookmark = True
                    matching_bookmarks.append(url)
                elif target_base_domain:
                    bookmark_base_domain = get_base_domain(bookmark_domain)
                    if bookmark_base_domain == target_base_domain:
                        has_matching_bookmark = True
                        matching_bookmarks.append(url)
        if has_matching_bookmark:
            if domain not in self._favicon_cache and favicon:
                self._favicon_cache[domain] = favicon
        if not hasattr(self, 'bookmark_menu') or self.bookmark_menu is None:
            return
        GLib.idle_add(self.update_bookmarks_menu, self.bookmark_menu)

    def _extract_domain(self, url):
        """Extract domain from URL for favicon caching."""
        try:
            domain = url.split('//')[-1].split('/')[0]
            if ':' in domain:
                domain = domain.split(':')[0]
            return domain
        except Exception:
            return None

    def _fetch_favicon(self, url):
        """Fetch favicon for a URL using async HTTP requests.
        Args:
            url (str): The URL of the website to fetch favicon from
        Returns:
            Gdk.Texture: The favicon as a texture, or None if not found
        """
        from urllib.parse import urljoin
        import ssl
        import asyncio
        import aiohttp
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        # Throttled debug logging for favicon failures to reduce noisy SOCKS/DNS errors
        if not hasattr(self, '_favicon_error_throttle'):
            self._favicon_error_throttle = {}
        def _throttle_log(key, message):
            try:
                now = time.time()
                last = self._favicon_error_throttle.get(key)
                # only log same error for the same key every 5 minutes
                if last and (now - last) < 300:
                    return
                self._favicon_error_throttle[key] = now
            except Exception:
                pass
            if self.debug_mode:
                print(message)

        async def _download_favicon(session, favicon_url):
            headers = {
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                'Accept': 'image/*,*/*;q=0.8'
            }
            try:
                async with session.get(favicon_url, ssl=ssl_context, timeout=5, headers=headers) as response:
                    if response.status == 200:
                        content_type = response.headers.get('content-type', '').lower()
                        if not content_type.startswith('image/'):
                            return None
                        try:
                            data = await response.read()
                            if not data:
                                return None

                            loader = GdkPixbuf.PixbufLoader()
                            try:
                                loader.write(data)
                                loader.close()
                                pixbuf = loader.get_pixbuf()
                                if pixbuf and pixbuf.get_width() > 0 and pixbuf.get_height() > 0:
                                    return pixbuf
                            except GLib.GError as e:
                                logger.debug(f"Error loading favicon image data: {e}")
                                return None
                            except Exception as e:
                                logger.debug(f"Unexpected error processing favicon: {e}")
                                return None
                            finally:
                                if loader:
                                    try:
                                        loader.close()
                                    except (GLib.Error, ValueError, RuntimeError):
                                        # Ignore errors when closing the loader
                                        pass
                        except (GLib.Error, ValueError):
                            try:
                                loader.close()
                            except (GLib.Error, ValueError, RuntimeError):
                                pass
                            return None
                    else:
                        return None
            except Exception as e:
                _throttle_log(favicon_url, f"DEBUG: Exception in _download_favicon for {favicon_url}: {type(e).__name__}: {e}")
                return None
        favicon_urls = []
        try:
            domain = self._extract_domain(url)
            if domain:
                base_url = f"https://{domain}"
                favicon_urls = [
                    f"{base_url}/favicon.ico",
                    f"{base_url}/favicon.png",
                    f"{base_url}/favicon.svg",
                    f"{base_url}/favicon.jpg",
                    f"{base_url}/apple-touch-icon.png",
                    f"{base_url}/apple-touch-icon-precomposed.png",
                    f"{base_url}/android-chrome-192x192.png",
                    f"{base_url}/icon-192x192.png"
                ]
            try:
                from bs4 import BeautifulSoup
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                response = requests.get(url, timeout=5, verify=False,
                    headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'})
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                favicon_rels = ['icon', 'shortcut icon', 'apple-touch-icon', 'apple-touch-icon-precomposed',
                    'fluid-icon', 'mask-icon', 'manifest']
                for link in soup.find_all('link', rel=lambda x: x and x.lower() in favicon_rels):
                    href = link.get('href')
                    if href:
                        full_url = urljoin(url, href)
                        if full_url not in favicon_urls:
                            favicon_urls.insert(0, full_url)
            except Exception as e:
                _throttle_log(url, f"DEBUG: Exception in favicon HTML parsing for {url}: {type(e).__name__}: {e}")
        except Exception as e:
            if self.debug_mode:
                print(f"DEBUG: Exception in favicon URL discovery for {url}: {type(e).__name__}: {e}")
            return None
        async def _download_all_favicons():
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                    'Accept': 'image/*,*/*;q=0.8'
                }

                # Check if proxy environment variables are set
                proxy = None
                http_proxy = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY')
                https_proxy = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY')

                # Use proxy if available
                if https_proxy:
                    proxy = https_proxy
                elif http_proxy:
                    proxy = http_proxy

                proxy_is_socks = bool(proxy and proxy.startswith("socks"))

                # Create session with or without proxy
                if proxy and not proxy_is_socks:
                    connector = aiohttp.TCPConnector()
                    async with aiohttp.ClientSession(connector=connector) as session:
                        for favicon_url in favicon_urls:
                            try:
                                async with session.get(favicon_url, ssl=ssl_context, timeout=5, headers=headers, proxy=proxy) as response:
                                    if response.status == 200:
                                        content_type = response.headers.get('content-type', '').lower()
                                        if content_type.startswith('image/'):
                                            try:
                                                data = await response.read()
                                                if not data:
                                                    continue

                                                loader = GdkPixbuf.PixbufLoader()
                                                try:
                                                    loader.write(data)
                                                    loader.close()
                                                    pixbuf = loader.get_pixbuf()
                                                    if pixbuf and pixbuf.get_width() > 0 and pixbuf.get_height() > 0:
                                                        width = pixbuf.get_width()
                                                        height = pixbuf.get_height()
                                                        size = min(16, width, height)
                                                        pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
                                                        texture = self._texture_from_pixbuf(pixbuf)
                                                        if texture:
                                                            return texture
                                                except GLib.GError as e:
                                                    logger.debug(f"Error loading favicon image data: {e}")
                                                    continue
                                                except Exception as e:
                                                    logger.debug(f"Unexpected error processing favicon: {e}")
                                                    continue
                                                finally:
                                                    if loader:
                                                        try:
                                                            loader.close()
                                                        except (GLib.Error, ValueError, RuntimeError):
                                                            pass
                                            except Exception as e:
                                                logger.debug(f"Error reading favicon data: {e}")
                                                continue
                                            except (GLib.Error, ValueError):
                                                pass
                            except Exception as e:
                                _throttle_log(favicon_url, f"DEBUG: Exception in _download_favicon for {favicon_url}: {type(e).__name__}: {e}")
                elif proxy_is_socks:
                    # aiohttp does not natively support SOCKS proxies; use requests instead
                    proxies = {
                        'http': proxy,
                        'https': proxy,
                    }
                    for favicon_url in favicon_urls:
                        try:
                            resp = requests.get(
                                favicon_url,
                                timeout=8,
                                verify=False,
                                headers=headers,
                                proxies=proxies,
                            )
                            if resp.status_code == 200:
                                content_type = resp.headers.get('content-type', '').lower()
                                if content_type.startswith('image/'):
                                    try:
                                        data = resp.content
                                        if not data:
                                            continue

                                        loader = GdkPixbuf.PixbufLoader()
                                        try:
                                            loader.write(data)
                                            loader.close()
                                            pixbuf = loader.get_pixbuf()
                                            if pixbuf and pixbuf.get_width() > 0 and pixbuf.get_height() > 0:
                                                width = pixbuf.get_width()
                                                height = pixbuf.get_height()
                                                size = min(16, width, height)
                                                pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
                                                texture = self._texture_from_pixbuf(pixbuf)
                                                if texture:
                                                    return texture
                                        except GLib.GError as e:
                                            logger.debug(f"Error loading favicon image data: {e}")
                                            continue
                                        except Exception as e:
                                            logger.debug(f"Unexpected error processing favicon: {e}")
                                            continue
                                        finally:
                                            if loader:
                                                try:
                                                    loader.close()
                                                except (GLib.Error, ValueError, RuntimeError):
                                                    pass
                                    except Exception as e:
                                        logger.debug(f"Error reading favicon data: {e}")
                                        continue
                                    except (GLib.Error, ValueError):
                                        pass
                        except Exception as e:
                            _throttle_log(favicon_url, f"DEBUG: Exception in _download_favicon for {favicon_url}: {type(e).__name__}: {e}")
                else:
                    # No proxy - use direct connection
                    async with aiohttp.ClientSession() as session:
                        for favicon_url in favicon_urls:
                            try:
                                async with session.get(favicon_url, ssl=ssl_context, timeout=5, headers=headers) as response:
                                    if response.status == 200:
                                        content_type = response.headers.get('content-type', '').lower()
                                        if content_type.startswith('image/'):
                                            try:
                                                data = await response.read()
                                                if not data:
                                                    continue

                                                loader = GdkPixbuf.PixbufLoader()
                                                try:
                                                    loader.write(data)
                                                    loader.close()
                                                    pixbuf = loader.get_pixbuf()
                                                    if pixbuf and pixbuf.get_width() > 0 and pixbuf.get_height() > 0:
                                                        width = pixbuf.get_width()
                                                        height = pixbuf.get_height()
                                                        size = min(16, width, height)
                                                        pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
                                                        texture = self._texture_from_pixbuf(pixbuf)
                                                        if texture:
                                                            return texture
                                                except GLib.GError as e:
                                                    logger.debug(f"Error loading favicon image data: {e}")
                                                    continue
                                                except Exception as e:
                                                    logger.debug(f"Unexpected error processing favicon: {e}")
                                                    continue
                                                finally:
                                                    if loader:
                                                        try:
                                                            loader.close()
                                                        except (GLib.Error, ValueError, RuntimeError):
                                                            pass
                                            except Exception as e:
                                                logger.debug(f"Error reading favicon data: {e}")
                                                continue
                                            except (GLib.Error, ValueError):
                                                pass
                            except Exception as e:
                                _throttle_log(favicon_url, f"DEBUG: Exception in _download_favicon for {favicon_url}: {type(e).__name__}: {e}")
                return None
            except Exception as e:
                _throttle_log(url, f"DEBUG: Exception in async favicon download for {url}: {type(e).__name__}: {e}")
                return None
        try:
            return asyncio.new_event_loop().run_until_complete(_download_all_favicons())
        except Exception as e:
            _throttle_log(url, f"DEBUG: Exception in _fetch_favicon for {url}: {type(e).__name__}: {e}")
            return None

    def get_favicon(self, url):
        """Get favicon for a URL, using cache if available.
        Args:
            url (str): The URL of the website to fetch favicon from
        Returns:
            Gdk.Texture: The favicon as a texture, or fallback favicon if not found
        """
        if not url or not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            return self.default_favicon
        domain = self._extract_domain(url)
        if not domain:
            return self.default_favicon
        if domain in self._favicon_cache:
            return self._favicon_cache[domain]
        if not self._should_retry_favicon(domain):
            return self._get_favicon_fallback(domain, 'domain_color')
        if hasattr(self, '_favicon_fetch_in_progress'):
            if len(self._favicon_fetch_in_progress) > 5:
                return self._get_favicon_fallback(domain, 'letter')
        else:
            self._favicon_fetch_in_progress = set()
        if domain in self._favicon_fetch_in_progress:
            return self._get_favicon_fallback(domain, 'default')

        def _update_favicon():
            try:
                self._favicon_fetch_in_progress.add(domain)
                favicon = self._fetch_favicon(url)
                if favicon and domain:
                    def update_on_main_thread():
                        try:
                            self._favicon_cache[domain] = favicon
                            self._save_favicon_cache()
                            self._update_tabs_for_domain(domain, favicon)
                            self._update_bookmarks_for_domain(domain, favicon)
                            if domain in self._favicon_retry_counts:
                                del self._favicon_retry_counts[domain]
                        except Exception:
                            pass
                    GLib.idle_add(update_on_main_thread)
                else:
                    self._increment_favicon_retry(domain)
            except (requests.ConnectionError, requests.Timeout, requests.RequestException, Exception) as e:
                if self.debug_mode:
                    print(f"DEBUG: Thread exception in _update_favicon for {domain}: {type(e).__name__}: {e}")
                self._increment_favicon_retry(domain)
            finally:
                if domain in self._favicon_fetch_in_progress:
                    self._favicon_fetch_in_progress.remove(domain)
        try:
            import threading
            thread = threading.Thread(target=_update_favicon, daemon=True)
            thread.start()
        except Exception:
            pass
        return self._get_favicon_fallback(domain, 'default')

def main() -> None:
    """Main entry point for the Shadow Browser."""
    app = ShadowBrowser()
    return app.run(None)

if __name__ == "__main__":
    import sys
    sys.exit(main())
