# Webkit Gtk4 Gdk.Texture only remove all GdkPixbuf
import asyncio
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import base64
import urllib.request
from datetime import datetime, timezone
from urllib import parse as urlparse
from urllib.parse import parse_qs, unquote, urlunparse
import gi
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from functools import partial
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from stem.control import Controller

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gdk", "4.0")
    import cairo  # noqa: F401
    from gi.repository import Gtk, Gdk, GLib, Gio, WebKit, Pango, GdkPixbuf  # noqa: F401
    GST_AVAILABLE = False
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    GST_AVAILABLE = True
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
        except Exception:
            pass
if not isinstance(sys.stderr, _GIWarningFilter):
    try:
        sys.stderr = _GIWarningFilter(sys.stderr)
    except Exception:
        pass

class DBusErrorFilter(Exception):
    """Dummy DBusErrorFilter class for when dbus-python is not properly installed."""
    pass
try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
except ImportError:
    dbus = None
    DBusGMainLoop = None
    GST_VAAPI_AVAILABLE = False
    GstVa = None
    GstVaapi = None
    import importlib.util
    GST_VAAPI_AVAILABLE = importlib.util.find_spec('gi.repository.GstVa') is not None and \
                         importlib.util.find_spec('gi.repository.GstVaapi') is not None
    if not GST_VAAPI_AVAILABLE:
        os.environ['GST_MSDK_DISABLE'] = '1'
    Gst.init(None)
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
            widget.unparent()
        if hasattr(container, 'append'):
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
except ImportError:
    try:
        from js_obfuscation import extract_url_from_javascript as js_extract_url
        extract_onclick_url = None
    except ImportError:
        js_extract_url = None
        extract_onclick_url = None

class VAAPIManager:
    """
    Manages VA-API context and hardware acceleration settings for video playback.
    Provides a singleton instance to handle VA-API initialization and configuration.
    """
    _instance = None
    _log_messages = []
    _va_drivers = ['iHD', 'i965', 'radeonsi', 'nouveau', 'r600', 'nvidia']
    _drm_devices = ['/dev/dri/renderD128', '/dev/dri/card0', '/dev/dri/renderD129', '/dev/dri/card1']

    def __new__(cls):
        if cls.__dict__.get('_instance', None) is None:
            inst = object.__new__(cls)
            inst._initialized = False
            inst._va_display = None
            inst._va_config = None
            inst._va_context = None
            inst._gst_plugins = {}
            inst._gst_elements = {}
            inst._pipeline = None
            inst._source = None
            inst._video_convert = None
            inst._audio_convert = None
            inst._detected_driver = None
            inst._detected_device = None
            inst._available_codecs = {}
            inst._capabilities = {}
            inst.debug_mode = False
            inst._gst_initialized = False
            cls._instance = inst
        return cls._instance

    def _detect_hardware(self):
        """Detect available hardware and capabilities"""
        cpu_flags = set()
        try:
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.startswith('flags'):
                        cpu_flags = set(line.split(':')[1].strip().lower().split())
                        break
        except Exception as e:
            self._log(f"Failed to detect CPU flags: {e}", 'warning')
        available_devices = []
        for device in self._drm_devices:
            if os.path.exists(device):
                available_devices.append(device)
        if not available_devices:
            try:
                for entry in os.listdir('/dev/dri'):
                    if entry.startswith(('renderD', 'card')):
                        available_devices.append(f'/dev/dri/{entry}')
            except Exception as e:
                self._log(f"Failed to scan /dev/dri: {e}", 'warning')
        available_drivers = []
        for driver in self._va_drivers:
            try:
                result = subprocess.run(
                    ['vainfo', '--display', f'vaapi_drm:{driver}'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2
                )
                if result.returncode == 0:
                    available_drivers.append(driver)
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
        self._detected_driver = available_drivers[0] if available_drivers else 'iHD'
        self._detected_device = available_devices[0] if available_devices else None
        self._log(f"Detected CPU flags: {', '.join(sorted(cpu_flags))}")
        self._log(f"Available VA-API drivers: {', '.join(available_drivers) or 'None'}")
        self._log(f"Available DRM devices: {', '.join(available_devices) or 'None'}")
        return {
            'cpu_flags': cpu_flags,
            'available_drivers': available_drivers,
            'available_devices': available_devices
        }

    def _detect_codecs(self):
        """Detect supported codecs and capabilities with detailed logging"""
        self._log("Starting codec detection...", 'debug')
        codecs = {
            'h264': {'supported': False, 'profiles': []},
            'h265': {'supported': False, 'profiles': []},
            'vp8': {'supported': False, 'profiles': []},
            'vp9': {'supported': False, 'profiles': []},
            'av1': {'supported': False, 'profiles': []}
        }
        if not self._detected_driver:
            self._log("No VA-API driver detected, skipping codec detection", 'warning')
            return {k: v['supported'] for k, v in codecs.items()}
        if not self._detected_device:
            self._log("No DRM device detected, skipping codec detection", 'warning')
            return {k: v['supported'] for k, v in codecs.items()}
        self._log(f"Using VA-API driver: {self._detected_driver}", 'debug')
        self._log(f"Using DRM device: {self._detected_device}", 'debug')
        try:
            env = os.environ.copy()
            env['LIBVA_DRIVER_NAME'] = self._detected_driver
            env['GST_VAAPI_DRM_DEVICE'] = self._detected_device
            self._log("Running vainfo to detect codec support...", 'debug')
            result = subprocess.run(
                ['vainfo'],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                output = result.stdout
                self._log("Successfully ran vainfo", 'debug')
                codec_checks = {
                    'h264': [
                        (r'VAProfileH264(High|Main|ConstrainedBaseline|Extended|High10|High422|High444)', 'H.264/AVC'),
                        (r'AVC|H\.?264', 'H.264/AVC (alternate)')
                    ],
                    'h265': [
                        (r'VAProfile(HEVC|H265)(Main|Main10|Main12|Main422_10|Main444_10|Main422_12|Main444_12)', 'H.265/HEVC'),
                        (r'HEVC|H\.?265', 'H.265/HEVC (alternate)')
                    ],
                    'vp8': [(r'VP8', 'VP8')],
                    'vp9': [(r'VP9(Profile0|Profile1|Profile2|Profile3)?', 'VP9')],
                    'av1': [(r'AV1(Profile0|Profile1|Profile2)?', 'AV1')]
                }
                for codec, patterns in codec_checks.items():
                    for pattern, name in patterns:
                        matches = re.finditer(pattern, output, re.IGNORECASE)
                        for match in matches:
                            profile = match.group(0)
                            if profile not in codecs[codec]['profiles']:
                                codecs[codec]['profiles'].append(profile)
                            if not codecs[codec]['supported']:
                                codecs[codec]['supported'] = True
                                self._log(f"✅ Detected {name} support via profile: {profile}", 'info')
            else:
                error_msg = result.stderr or "Unknown error"
                self._log(f"vainfo failed with error: {error_msg}", 'error')
        except subprocess.TimeoutExpired:
            self._log("vainfo timed out while detecting codecs", 'warning')
        except Exception as e:
            self._log(f"Error running vainfo: {str(e)}", 'error')
        if not any(codec['supported'] for codec in codecs.values()):
            self._log("No codecs detected via VA-API, trying GStreamer detection...", 'debug')
            try:
                from gi.repository import Gst
                if not Gst.is_initialized():
                    Gst.init(None)
                gst_codecs = {
                    'h264': ['vaapih264dec', 'avdec_h264', 'h264parse'],
                    'h265': ['vaapih265dec', 'avdec_h265', 'h265parse'],
                    'vp8': ['vaapivp8dec', 'avdec_vp8', 'vp8parse'],
                    'vp9': ['vaapivp9dec', 'avdec_vp9', 'vp9parse'],
                    'av1': ['vaapiav1dec', 'av1parse']
                }
                for codec, elements in gst_codecs.items():
                    for element in elements:
                        factory = Gst.ElementFactory.find(element)
                        if factory:
                            codecs[codec]['supported'] = True
                            codecs[codec]['profiles'].append(f"GStreamer:{element}")
                            self._log(f"✅ Detected {codec.upper()} support via GStreamer element: {element}", 'info')
                            break
            except Exception as e:
                self._log(f"GStreamer detection failed: {str(e)}", 'debug')
        supported = []
        for codec, data in codecs.items():
            if data['supported']:
                profiles = ", ".join(data['profiles']) if data['profiles'] else "(no profiles)"
                supported.append(f"{codec.upper()}: [{profiles}]")
        if supported:
            self._log(f"Detected codec support: {'; '.join(supported)}", 'info')
        else:
            self._log("No hardware-accelerated codecs detected", 'warning')
        return {k: v['supported'] for k, v in codecs.items()}

    def get_optimal_vaapi_config(self):
        """Determine optimal VA-API configuration based on hardware detection"""
        hw_info = self._detect_hardware()
        codec_support = self._detect_codecs()
        try:
            cpu_count = os.cpu_count() or 4
        except Exception:
            cpu_count = 4
        threading_config = {
            'enabled': True,
            'pool_size': min(8, cpu_count),
            'max_threads': min(16, cpu_count * 2)
        }
        if 'avx2' in hw_info.get('cpu_flags', set()) and codec_support.get('av1', False):
            codec_support['av1'] = True
            threading_config['pool_size'] = min(12, cpu_count)
        config = {
            'driver': self._detected_driver,
            'device': self._detected_device or '/dev/dri/renderD128',
            'features': codec_support,
            'threading': threading_config,
            'debug': {
                'enabled': True,
                'level': '2,GST_REFCOUNTING:5,GST_BUFFER:5,GST_VAAPI:4'
            }
        }
        self._log(f"VA-API Configuration: {json.dumps(config, indent=2)}")
        return config

    def setup_vaapi_environment(self):
        """Set up VA-API environment with optimized settings for hardware acceleration"""
        if not GST_AVAILABLE or not hasattr(Gst, 'vaapi'):
            self._log("GStreamer VA-API not available, falling back to software decoding", 'warning')
            return False
        self._detect_hardware()
        self._detect_codecs()
        env_vars = {
            'GST_VAAPI_ALL_DRIVERS': '1',
            'GST_VAAPI_DRM_DEVICE': self._detected_device or '/dev/dri/renderD128',
            'LIBVA_DRIVER_NAME': self._detected_driver or 'iHD',
            'GST_VAAPI_DISABLE_INTERLACE': '1',
            'GST_VAAPI_DISABLE_PROTECTED': '1',
            'GST_VAAPI_DISABLE_OVERLAY': '1',
            'GST_VAAPI_DRM_ZERO_COPY': '1',
            'GST_VAAPI_DRM_DISABLE_VP9': '0' if 'vp9' in self._available_codecs and self._available_codecs['vp9'] else '1',
            'GST_VAAPI_DRM_DISABLE_VP9_10': '0' if 'vp9' in self._available_codecs and self._available_codecs['vp9'] else '1',
            'GST_VAAPI_NUM_THREADS': '1',
            'GST_VAAPI_DRM_THREADED': '0',
            'GST_VAAPI_DRM_ASYNC': '1',
            'GST_VAAPI_DRM_THROTTLE': '0',
            'GST_VAAPI_DRM_SKIP_FRAME': '0',
            'GST_VAAPI_DISABLE_VIDEO_MEMORY_CACHE': '0',
            'GST_VAAPI_DRM_DISABLE_VIDEO_SURFACE_CACHE': '0',
            'GST_VAAPI_DRM_DISABLE_VIDEO_SURFACE_SHARING': '0',
            'GST_VAAPI_DRM_DISPLAY_FORCE_COMPOSITING': '0',
            'GST_VAAPI_DRM_DISPLAY_FORCE_OVERLAY': '0',
            'GST_VAAPI_DRM_MAX_SURFACES': '4',
            'GST_VAAPI_DRM_DISPLAY_BUFFERS': '2',
            'GST_VAAPI_DRM_DISPLAY_SYNC': '0',
            'GST_VAAPI_DRM_THREADED_DISPLAY': '0',
            'GST_VAAPI_DRM_DISPLAY_ASYNC': '0',
            'GST_VAAPI_DRM_DISPLAY_THROTTLE': '0',
            'GST_VAAPI_DRM_DISPLAY_MAX_LATENCY': '20000000',
            'GST_VAAPI_DRM_DISPLAY_FORCE_FRAME_RATE': '0',
            'GST_VAAPI_DRM_DISPLAY_MAX_FPS': '0',
            'GST_VAAPI_DRM_DISPLAY_MIN_FPS': '0',
            'GST_VAAPI_DRM_DISABLE_VSYNC': '1',
            'GST_VAAPI_DRM_DISPLAY_QUEUE_SIZE': '2',
            'GST_VAAPI_DRM_DISPLAY_DRIVER': self._detected_driver or 'iHD',
            'GST_DEBUG': '2,GST_BUFFER:1,GST_MEMORY:1',
            'GST_DEBUG_NO_COLOR': '1',
            'GST_VAAPI_DRM_DEBUG': '1',
            'GST_ALLOC_TRACE': '1',
            'GST_ALLOC_TRACE_STACK_TRACE': '1',
        }
        for key, value in env_vars.items():
            if value is not None:
                os.environ[key] = str(value)
        self._log(f"VA-API environment configured with driver: {self._detected_driver or 'default'}")
        return True

    def configure_webkit_settings(self, webview):
        """Configure WebKit settings for optimal media playback and performance"""
        try:
            settings = webview.get_settings()
            media_settings = {
                'enable-media': True,
                'enable-encrypted-media': True,
                'enable-media-capabilities': True,
                'enable-media-stream': True,
                'enable-webrtc': True,
                'enable-webrtc-hardware-acceleration': True,
                'enable-webrtc-multiple-routes': True,
                'enable-webrtc-stun-ipv6': True,
                'media-playback-allows-inline': True,
                'media-playback-requires-user-gesture': False,
                'media-cache-size': 512 * 1024 * 1024,
                'media-disk-cache-disk-cache-directory': os.path.expanduser('~/.cache/shadow-browser/media'),
                'media-disk-cache-enabled': True,
                'media-disk-cache-size': 256 * 1024 * 1024,
                'media-source-enabled': True,
                'media-stream-enabled': True
            }
            hw_settings = {
                'hardware-acceleration-policy': WebKit.HardwareAccelerationPolicy.ALWAYS,
                'enable-accelerated-2d-canvas': True,
                'enable-accelerated-video': True,
                'enable-accelerated-video-decode': True,
                'enable-accelerated-video-encode': True,
                'enable-gpu': True,
                'enable-gpu-compositing': True,
                'enable-webgl': True,
                'enable-webgl2': True,
                'enable-webgpu': True,
                'enable-webxr': True
            }
            perf_settings = {
                'enable-accelerated-2d-canvas': True,
                'enable-accelerated-video': True,
                'enable-accelerated-video-decode': True,
                'enable-accelerated-video-encode': True,
                'enable-cache': True,
                'enable-javascript-markup': True,
                'enable-media-stream': True,
                'enable-page-cache': True,
                'enable-smooth-scrolling': True,
                'enable-spatial-navigation': True,
                'enable-true-smooth-scrolling': True,
                'enable-webgl': True,
                'enable-webgl2-compute-context': True,
                'enable-xss-auditor': True,
                'auto-load-images': True,
                'auto-shrink-images': True,
                'enable-caret-browsing': False,
                'enable-javascript': True,
                'javascript-can-open-windows-automatically': False,
                'enable-developer-extras': True
            }
            security_settings = {
                'allow-file-access-from-file-urls': False,
                'allow-universal-access-from-file-urls': False,
                'enable-caret-browsing': False,
                'enable-fullscreen': True,
                'enable-html5-database': False,
                'enable-html5-local-storage': True,
                'enable-java': False,
                'enable-javascript-can-open-windows-automatically': False,
                'enable-media-capabilities': True,
                'enable-mock-capture-devices': False,
                'enable-plugins': False,
                'enable-private-browsing': True,
                'enable-site-specific-quirks': True,
                'enable-spell-checking': True,
                'enable-webaudio': True,
                'enable-websql': False,
                'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
            }
            settings_map = {
                'media': media_settings,
                'hardware': hw_settings,
                'performance': perf_settings,
                'security': security_settings
            }
            for group_name, settings_group in settings_map.items():
                for prop, value in settings_group.items():
                    try:
                        setter_name = f'set_{prop.replace("-", "_")}'
                        if hasattr(settings, setter_name):
                            getattr(settings, setter_name)(value)
                        elif hasattr(settings, prop):
                            setattr(settings, prop, value)
                        else:
                            self._log(f"Setting not found: {prop} in {group_name}", 'debug')
                    except Exception as e:
                        self._log(f"Failed to set {prop}: {e}", 'warning')
            return settings
        except Exception as e:
            if self.debug_mode:
                print(f"Error configuring WebKit settings: {e}")
            return None

    def setup_webview_settings(self, webview):
        """Set up WebView with secure settings."""
        settings = webview.get_settings()
        settings.set_property("enable-javascript", True)
        settings.set_property("enable-plugins", False)
        settings.set_property("enable-java", False)
        settings.set_property("enable-webgl", True)
        settings.set_property("enable-media-stream", False)
        settings.set_property("enable-webaudio", True)
        settings.set_property("enable-web-security", True)
        settings.set_property("allow-modal-dialogs", False)
        settings.set_property("enable-developer-extras", self.debug_mode)
        settings.set_property("enable-csp", True)
        settings.set_property("enable-frame-flattening", True)
        settings.set_property("enable-dns-prefetching", False)
        settings.set_property("enable-html5-local-storage", True)
        settings.set_property("enable-html5-database", False)
        settings.set_property("enable-offline-web-application-cache", False)
        settings.set_property("enable-page-cache", False)
        settings.set_property("enable-site-specific-quirks", False)
        settings.set_property("enable-smooth-scrolling", True)
        settings.set_property("enable-spatial-navigation", False)
        settings.set_property("enable-tabs-to-links", False)
        settings.set_property("enable-webrtc", False)
        settings.set_property("enable-write-console-messages-to-stdout", self.debug_mode)
        settings.set_property("default-charset", "utf-8")
        if hasattr(self, '_global_csp'):
            csp_script = f"""
            (function() {{
                try {{
                    var meta = document.createElement('meta');
                    meta.httpEquiv = 'Content-Security-Policy';
                    meta.content = '{self._global_csp}';
                    var head = document.head || document.getElementsByTagName('head')[0];
                    head.appendChild(meta);
                }} catch (e) {{
                    console.error('Failed to apply CSP:', e);
                }}
            }})();
            """
            script = WebKit.UserScript.new(
                csp_script,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
                None,
                None
            )
            webview.get_user_content_manager().add_script(script)
        self._setup_security_violation_handling(webview)
        webview.connect("resource-load-started", self._on_resource_load_started)
        webview.connect("load-changed", self._on_load_changed)
        self._enforce_frame_ancestors(webview)
        return webview

    def _configure_video_sink(self, sink, name="videosink"):
        """Configure video sink for optimal performance and hardware acceleration.
        This method configures a video sink element with settings that optimize
        video playback performance, including hardware acceleration when available.
        Args:
            sink: The GStreamer video sink element to configure (e.g., vaapisink, xvimagesink, etc.)
            name: Optional name for the element
        Returns:
            bool: True if configuration was successful, False otherwise
        """
        if not sink:
            return False
        # For live playback prefer syncing to the clock and enable QoS so
        # the pipeline can drop frames or adjust buffering instead of stalling.
        sink.set_property("sync", True)
        sink.set_property("async", False)
        sink.set_property("qos", True)
        sink.set_property("max-lateness", 20000000)
        sink.set_property("processing-deadline", 20000000)
        sink.set_property("qos-dither", 0)
        # QoS interval in nanoseconds (0.1s). Allows periodic QoS updates.
        sink.set_property("qos-interval", 100000000)
        if hasattr(sink.props, 'show_preroll_frame'):
            sink.set_property("show-preroll-frame", True)
        if hasattr(sink.props, 'max_buffers'):
            # Raise max buffers to allow some jitter tolerance
            sink.set_property("max-buffers", 6)
        if hasattr(sink.props, 'max_errors'):
            sink.set_property("max-errors", 3)
        if hasattr(sink.props, 'enable_hardware'):
            sink.set_property("enable-hardware", True)
        if hasattr(sink.props, 'drm_mode'):
            sink.set_property("drm-mode", 1)
        if hasattr(sink.props, 'presentation_mode'):
            try:
                sink.set_property("presentation-mode", 1)
            except Exception as e:
                if hasattr(self, 'debug_mode') and self.debug_mode:
                    print(f"Error setting presentation mode: {e}", file=sys.stderr)
        if name == 'vaapisink':
            sink.set_property("max-render-delay", 20000000)
            sink.set_property("frame-buffer-count", 3)
            sink.set_property("max-parallel-triple-buffering", False)
            sink.set_property("drm-mode", 'fifo')
            sink.set_property("desired-framerate", '60/1')
            sink.set_property("max-framerate", '60/1')
            # Allow the sink to drop frames when necessary to avoid visible stutters
            sink.set_property("drop-frames", True)
            sink.set_property("max-frame-latency", 4)
            sink.set_property("max-frame-time", 33333333)
            # Increase framebuffer count for smoother playback under jitter
            try:
                sink.set_property("frame-buffer-count", 6)
            except Exception:
                pass
            sink.set_property("enable-last-sample", True)
        return True

    def __init__(self):
        """Initialize the VAAPI manager with optimized settings for video playback."""
        if not hasattr(self, '_initialized'):
            self._initialized = False
            self._gst_plugins = {}
            self._gst_elements = {}
            self._pipeline = None
            self._source = None
            self._video_convert = None
            self._audio_convert = None
            self._loop = None
            self._context = None
            if hasattr(self, '_log_messages'):
                self._log_messages.clear()
            self.setup_vaapi_environment()
            global GST_AVAILABLE
            if not GST_AVAILABLE:
                try:
                    gi.require_version('Gst', '1.0')
                    from gi.repository import Gst, GLib, GObject
                    if not Gst.is_initialized():
                        Gst.init(None)
                    self._loop = GLib.MainLoop()
                    self._context = GLib.MainContext.default()
                    Gst.debug_set_active(True)
                    Gst.debug_set_default_threshold(Gst.DebugLevel.WARNING)
                    Gst.debug_remove_log_function(None)
                    Gst.debug_add_log_function(self._gst_debug_func, None)
                    GObject.threads_init()
                    GST_AVAILABLE = True
                    self._log("GStreamer initialized with hardware acceleration")
                    self._log(f"GStreamer version: {Gst.version_string()}")
                except Exception as e:
                    self._log(f"Failed to initialize GStreamer: {str(e)}", level='error')
                    GST_AVAILABLE = False
            self.initialize()

    def _gst_debug_func(self, category, level, file, function, line, obj, message, user_data):
        """
        Custom GStreamer debug handler to filter out common non-critical warnings.
        """
        ignore_messages = [
            "Got data flow before stream-start event",
            "Got data flow before segment event"
        ]
        if any(msg in message for msg in ignore_messages):
            return
        if hasattr(Gst.DebugLevel, 'NONE'):
            Gst.debug_log_default(category, level, file, function, line, obj, message, user_data)

    def _log(self, message, level='info'):
        """Log a message with the specified log level"""
        log_entry = f"[{level.upper()}] {message}"
        self._log_messages.append((level, message))
        if level == 'error':
            print(f"\033[91m{log_entry}\033[0m", file=sys.stderr)
        elif level == 'warning':
            print(f"\033[93m{log_entry}\033[0m", file=sys.stderr)
        elif level == 'debug':
            if os.environ.get('GST_DEBUG_LEVEL', '0') != '0':
                print(f"\033[90m{log_entry}\033[0m", file=sys.stderr)
        else:
            print(log_entry, file=sys.stderr)

    def _check_vaapi_support(self):
        """Check if VA-API is available and properly configured.
        This method performs several checks:
        1. Verifies GStreamer VA-API support is available
        2. Tries to detect and set the correct VA-API driver
        3. Attempts to find a suitable DRM device
        4. Sets up environment variables for optimal VA-API operation
        Returns:
            bool: True if VA-API is properly configured, False otherwise
        """
        if GstVaapi is None:
            self._log("GStreamer VA-API support not available - falling back to software rendering",
                     level='warning')
            return False
        try:
            driver_found = False
            if not os.environ.get('LIBVA_DRIVER_NAME'):
                drivers_to_try = [
                    ('iHD', 'Intel HD Graphics'),
                    ('i965', 'Intel GEN Graphics (legacy)'),
                    ('radeonsi', 'AMD Radeon'),
                    ('nouveau', 'Nouveau (NVIDIA)'),
                    ('r600', 'AMD Radeon (legacy)')
                ]
                for driver, description in drivers_to_try:
                    driver_paths = [
                        f"/usr/lib64/dri/{driver}_drv_video.so",
                        f"/usr/lib/x86_64-linux-gnu/dri/{driver}_drv_video.so",
                        f"/usr/lib/dri/{driver}_drv_video.so",
                        f"/usr/lib64/dri-nonfree/{driver}_drv_video.so"
                    ]
                    if any(os.path.exists(path) for path in driver_paths):
                        os.environ['LIBVA_DRIVER_NAME'] = driver
                        self._log(f"Found VA-API driver: {driver} ({description})",
                                level='info')
                        driver_found = True
                        break
                if not driver_found:
                    self._log("No suitable VA-API driver found. "
                            "Hardware acceleration will be disabled.",
                            level='warning')
                    return False
                return True
        except Exception as e:
            self._log(f"Error initializing VA-API driver: {e}", level='error')
            return False
        if not os.environ.get('GST_VAAPI_DRM_DEVICE'):
            drm_devices = [
                        ('/dev/dri/renderD128', 'Primary render node'),
                        ('/dev/dri/card0', 'Primary GPU'),
                        ('/dev/dri/renderD129', 'Secondary render node'),
                        ('/dev/dri/card1', 'Secondary GPU'),
                        ('/dev/dri/renderD130', 'Tertiary render node'),
                        ('/dev/dri/card2', 'Tertiary GPU')
                    ]
            for dev, desc in drm_devices:
                if os.path.exists(dev):
                    os.environ['GST_VAAPI_DRM_DEVICE'] = dev
                    os.environ['LIBVA_DRIVER_DEVICE'] = dev
                    self._log(f"Using DRM device: {dev} ({desc})",
                            level='info')
                    break
            else:
                self._log("No suitable DRM device found. "
                        "Hardware acceleration will be disabled.",
                        level='warning')
                return False
        try:
            display = GstVaapi.Display()
            if not display:
                return False
            return True
        except Exception:
            return False

    def _check_gst_plugins(self):
        """Check for required GStreamer plugins and return True if all are available.
        Returns:
            bool: True if all required plugins are available, False otherwise.
        """
        if not hasattr(Gst, 'ElementFactory'):
            self._log("GStreamer not properly initialized", level='error')
            return False
        try:
            self._log("Checking for required GStreamer plugins...")
            essential_plugins = [
                'playbin', 'playbin3', 'uridecodebin', 'decodebin', 'gl', 'glx', 'egl',
                'videoconvert', 'videoscale', 'videorate', 'audioconvert', 'audioresample',
                'autovideosink', 'autoaudiosink', 'queue', 'capsfilter', 'typefind'
            ]
            video_plugins = [
                'h264parse', 'h265parse', 'mpeg2dec', 'theora', 'vp8dec', 'vp9dec',
                'avdec_h264', 'avdec_h265', 'avdec_mpeg2video', 'avdec_mpeg4',
                'vaapi', 'vaapih264dec', 'vaapih265dec', 'vaapivp8dec', 'vaapivp9dec',
                'vaapipostproc', 'vaapisink', 'v4l2h264dec', 'v4l2h265dec'
            ]
            audio_plugins = [
                'mpg123audiodec', 'vorbisdec', 'aacparse', 'mp3parse', 'flacparse',
                'avdec_aac', 'avdec_mp3', 'avdec_ac3', 'avdec_vorbis', 'opusdec',
                'audiomixer', 'audioresample', 'audiorate'
            ]
            container_plugins = [
                'matroska', 'webm', 'mp4', 'mpegts', 'ogg', 'flv', 'avi', 'mov',
                'mpegps', 'asf', '3gpp', '3gpp2', 'amr', 'wav', 'aiff',
                'hlsdemux', 'dashdemux', 'm3u8playlist', 'isoff', 'fragmented'
            ]
            plugin_categories = {
                'Essential': essential_plugins,
                'Video': video_plugins,
                'Audio': audio_plugins,
                'Containers': container_plugins
            }
            missing_plugins = {category: [] for category in plugin_categories}
            all_available = True
            for category, plugins in plugin_categories.items():
                for plugin in plugins:
                    try:
                        factory = Gst.ElementFactory.find(plugin)
                        if not factory:
                            missing_plugins[category].append(plugin)
                            all_available = False
                    except Exception as e:
                        self._log(f"Error checking plugin {plugin}: {e}", level='warning')
                        missing_plugins[category].append(plugin)
                        all_available = False
            if not all_available:
                for category, plugins in missing_plugins.items():
                    if plugins:
                        self._log(f"Missing {category} plugins: {', '.join(plugins)}", level='warning')
            return all_available
        except Exception as e:
            self._log(f"Error checking GStreamer plugins: {e}", level='error')
            return False

    def _check_gst_plugins(self):
        """Check for essential GStreamer plugins and attempt auto-installation if missing."""
        essential_elements = [
            'uridecodebin',
            'hlsdemux',
            'dashdemux',
            'vaapih264dec',
            'vaapih265dec',
            'avdec_h264',
            'vaapisink',
            'videoconvert',
            'audioresample',
        ]
        missing_elements = []
        for element in essential_elements:
            factory = Gst.ElementFactory.find(element)
            if not factory:
                missing_elements.append(element)
                if self.debug_mode:
                    self.show_info_message(f"[GStreamer] Missing element: {element}")
        if missing_elements:
            self.show_info_message(f"[GStreamer] Missing {len(missing_elements)} essential elements: {', '.join(missing_elements)}")
            packages = {
                'hlsdemux': 'gstreamer1.0-plugins-bad',
                'dashdemux': 'gstreamer1.0-plugins-bad',
                'vaapih264dec': 'gstreamer1.0-vaapi',
                'vaapih265dec': 'gstreamer1.0-vaapi',
                'avdec_h264': 'gstreamer1.0-libav',
                'vaapisink': 'gstreamer1.0-vaapi',
                'videoconvert': 'gstreamer1.0-plugins-base',
                'audioresample': 'gstreamer1.0-plugins-base',
                'uridecodebin': 'gstreamer1.0-plugins-base',
            }
            required_packages = set()
            for elem in missing_elements:
                if elem in packages:
                    required_packages.add(packages[elem])
            if required_packages:
                try:
                    subprocess.run(['sudo', 'apt', 'update'], check=True, capture_output=True)
                    install_cmd = ['sudo', 'apt', 'install', '-y'] + list(required_packages)
                    result = subprocess.run(install_cmd, check=True, capture_output=True)
                    if result.returncode == 0:
                        self.show_info_message(f"[GStreamer] Successfully installed packages: {', '.join(required_packages)}")
                        Gst.deinit()
                        Gst.init(None)
                        if self.debug_mode:
                            self.show_info_message("[GStreamer] Re-initialized after plugin installation")
                    else:
                        print(f"[GStreamer] Failed to install packages. Output: {result.stderr.decode()}")
                        self.show_error_message(
                            f"Missing GStreamer plugins detected. Please install manually:\n"
                            f"sudo apt update && sudo apt install -y {' '.join(required_packages)}\n"
                            f"Elements missing: {', '.join(missing_elements)}"
                        )
                except subprocess.CalledProcessError as e:
                    print(f"[GStreamer] Installation error: {e}")
                    self.show_error_message(
                        f"GStreamer plugin installation failed. Please run:\n"
                        f"sudo apt update && sudo apt install -y {' '.join(required_packages)}"
                    )
                except Exception as e:
                    print(f"[GStreamer] Unexpected error during installation: {e}")
            else:
                print("[GStreamer] No known packages to install for missing elements")
        else:
            if self.debug_mode:
                print("[GStreamer] All essential plugins available")

    def _init_gstreamer(self):
        """Initialize GStreamer with optimal settings for live streams and warning suppression."""
        global GST_AVAILABLE
        if not GST_AVAILABLE:
            return
        try:
            gst_plugin_paths = [
                '/usr/lib64/gstreamer-1.0/',
                '/usr/lib/x86_64-linux-gnu/gstreamer-1.0/',
                '/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/'
            ]
            for path in gst_plugin_paths:
                if os.path.exists(path):
                    os.environ['GST_PLUGIN_PATH'] = path
                    break
            if os.path.exists('/dev/dri'):
                os.environ['GST_VAAPI_ALL_DRIVERS'] = '1'
                for driver in ['iHD', 'i965', 'radeonsi', 'nouveau']:
                    os.environ['LIBVA_DRIVER_NAME'] = driver
                    try:
                        try:
                            import gi
                            gi.require_version('GstVaapi', '1.0')
                            from gi.repository import GstVaapi
                            display = GstVaapi.Display()
                            if display:
                                break
                        except (ImportError, ValueError):
                            continue
                    except Exception:
                        continue
            gst_env = {
                'GST_BUFFER_SIZE': '2097152',
                'GST_ADAPTIVE_DEMUX_LIVE_REF': '1',
                'GST_ADAPTIVE_DEMUX_MAX_BUFFERING_TIME': '30000000000',
                'GST_ADAPTIVE_DEMUX_MIN_BUFFERING_TIME': '2000000000',
                'GST_ADAPTIVE_DEMUX_BUFFER_SIZE': '10485760',
                'GST_ADAPTIVE_DEMUX_USE_BUFRING': '1',
                'GST_GL_API': 'opengl',
                'GST_GL_PLATFORM': 'glx',
                'GST_REGISTRY_UPDATE': 'no',
                'GST_DEBUG_DUMP_DOT_DIR': '/tmp/gst-debug',
                'GST_DEBUG_NO_COLOR': '1',
                'GST_DEBUG': '3,GST_CAPS:1,GST_ELEMENT_*:3,GST_PADS:1,GST_EVENT:1',
                'GST_PLUGIN_LOADING_WHITELIST': 'gstreamer',
                'GST_PLUGIN_FEATURE_RANK': 'vaapidecode:MAX',
                'GST_VAAPI_DISABLE_AV1': '1',
                'GST_VAAPI_DISABLE_MPEG2': '1',
                'GST_VAAPI_DISABLE_VC1': '1',
                'GST_VAAPI_DISABLE_JPEG': '1',
                'GST_VAAPI_DISABLE_VP8': '1',
                'GST_VAAPI_DISABLE_VP9': '1',
                'GST_VAAPI_DISABLE_MPEG4': '1',
                'GST_VAAPI_DISABLE_H263': '1',
                'GST_VAAPI_DISABLE_WMV3': '1',
                'GST_VAAPI_DISABLE_H264': '0',
                'GST_VAAPI_DISABLE_H265': '0',
                'GST_VAAPI_DISABLE_POST_PROC': '1',
                'GST_VAAPI_DISABLE_SCALING': '1',
                'GST_VAAPI_DISABLE_DEINTERLACE': '1',
                'GST_VAAPI_DISABLE_DENOISE': '1',
                'GST_VAAPI_DISABLE_SHARPEN': '1',
                'GST_VAAPI_DISABLE_COLOR_BALANCE': '1',
                'GST_VAAPI_DISABLE_CSC': '1',
                'GST_VAAPI_DISABLE_ROTATION': '1',
                'GST_VAAPI_DISABLE_MIRRORING': '1',
                'GST_VAAPI_DISABLE_CROPPING': '1',
                'GST_VAAPI_DISABLE_BLENDING': '1',
                'GST_VAAPI_DISABLE_COMPOSITION': '1',
                'GST_VAAPI_DISABLE_ENCODING': '1',
                'GST_VAAPI_DISABLE_DECODER_FALLBACK': '1',
                'GST_VAAPI_DISABLE_ENCODER_FALLBACK': '1',
                'GST_VAAPI_DISABLE_POST_PROC_FALLBACK': '1',
                'GST_VAAPI_DISABLE_SCALING_FALLBACK': '1',
                'GST_VAAPI_DISABLE_DEINTERLACE_FALLBACK': '1',
                'GST_VAAPI_DISABLE_DENOISE_FALLBACK': '1',
                'GST_VAAPI_DISABLE_SHARPEN_FALLBACK': '1',
                'GST_VAAPI_DISABLE_COLOR_BALANCE_FALLBACK': '1',
                'GST_VAAPI_DISABLE_CSC_FALLBACK': '1',
                'GST_VAAPI_DISABLE_ROTATION_FALLBACK': '1',
                'GST_VAAPI_DISABLE_MIRRORING_FALLBACK': '1',
                'GST_VAAPI_DISABLE_CROPPING_FALLBACK': '1',
                'GST_VAAPI_DISABLE_BLENDING_FALLBACK': '1',
                'GST_VAAPI_DISABLE_COMPOSITION_FALLBACK': '1',
                'GST_VAAPI_DISABLE_ENCODING_FALLBACK': '1'
            }
            os.environ.update(gst_env)
            if not Gst.init_check(None)[0]:
                print("[GStreamer] Initialization failed")
                return
            self._check_gst_plugins()
            if self.debug_mode:
                os.makedirs('/tmp/gst-debug', exist_ok=True)
                print("[GStreamer] Debug logging enabled. Check /tmp/gst-debug for logs.")
                print("[GStreamer] Initialized with optimizations")
        except Exception as e:
            GST_AVAILABLE = False
            if self.debug_mode:
                print(f"[GStreamer] Initialization error: {e}")
                import traceback
                traceback.print_exc()

    def _setup_gst_elements(self, pipeline=None, use_vaapi=True):
        """
        Set up GStreamer elements with hardware acceleration if available.
        Optimized for smooth video playback with minimal shuttering.
        Args:
            pipeline: Existing GStreamer pipeline to use (optional)
            use_vaapi: Whether to try using VA-API hardware acceleration
        Returns:
            bool: True if setup was successful, False otherwise
        """
        if not GST_AVAILABLE:
            self._log("GStreamer not available, cannot set up elements", level='error')
            return False
        elements = {}
        try:
            if not self._check_gst_features():
                self._log("Some required GStreamer features are missing", level='warning')
            if pipeline is None:
                pipeline = Gst.Pipeline.new('media-pipeline')
                if not pipeline:
                    self._log("Failed to create GStreamer pipeline", level='error')
                    return False
            elements['queue_video'] = Gst.ElementFactory.make('queue', 'queue_video')
            if elements['queue_video']:
                elements['queue_video'].set_property('max-size-buffers', 5)
                elements['queue_video'].set_property('max-size-bytes', 1048576)
                elements['queue_video'].set_property('max-size-time', 25 * Gst.MSECOND)
                elements['queue_video'].set_property('leaky', 2)
                elements['queue_video'].set_property('silent', True)
                elements['queue_video'].set_property('flush-on-eos', True)
            elements['queue_audio'] = Gst.ElementFactory.make('queue', 'queue_audio')
            if elements['queue_audio']:
                elements['queue_audio'].set_property('max-size-buffers', 8)
                elements['queue_audio'].set_property('max-size-bytes', 1048576)
                elements['queue_audio'].set_property('max-size-time', 50 * Gst.MSECOND)
                elements['queue_audio'].set_property('leaky', 2)
                elements['queue_audio'].set_property('silent', True)
                elements['queue_audio'].set_property('flush-on-eos', True)
            elements['videoconvert'] = Gst.ElementFactory.make('videoconvert', 'videoconvert')
            if elements['videoconvert']:
                elements['videoconvert'].set_property('n-threads', 1)
                elements['videoconvert'].set_property('dither', 0)
                elements['videoconvert'].set_property('chroma-mode', 0)
                elements['videoconvert'].set_property('matrix-mode', 0)
                elements['videoconvert'].set_property('alpha-mode', 0)
                elements['videoconvert'].set_property('n-threshold', 0)
                elements['videoconvert'].set_property('qos', False)
                elements['videoconvert'].set_property('max-lateness', 10000000)
                elements['videoconvert'].set_property('processing-deadline', 10000000)
                elements['videoconvert'].set_property('qos-dither', 0)
                elements['videoconvert'].set_property('qos-interval', 0)
            vaapi_available = False
            if use_vaapi and self._detected_driver:
                elements['vaapi'] = Gst.ElementFactory.make('vaapi', 'vaapi')
                elements['vaapipostproc'] = Gst.ElementFactory.make('vaapipostproc', 'vaapipostproc')
                elements['vaapisink'] = Gst.ElementFactory.make('vaapisink', 'vaapisink')
                if all(elements.values()):
                    vaapi_available = True
                    self._log("VA-API elements created successfully", level='info')
                    elements['vaapi'].set_property('display', 'drm')
                    elements['vaapi'].set_property('low-latency', True)
                    elements['vaapi'].set_property('threads', 2)
                    elements['vaapipostproc'].set_property('scale-method', 1)
                    elements['vaapipostproc'].set_property('deinterlace-method', 0)
                    elements['vaapipostproc'].set_property('saturation', 1.1)
                    elements['vaapipostproc'].set_property('hue', 1.0)
                    sink = elements['vaapisink']
                    sink.set_property('sync', False)
                    sink.set_property('async', True)
                    sink.set_property('max-buffers', 32)
                    sink.set_property('threads', 4)
                    sink.set_property('ts-offset', 0)
                    sink.set_property('render-delay', 0)
                    sink.set_property('max-lateness', 10000000)
                    sink.set_property('qos', True)
                    sink.set_property('qos-dither', 0)
                    sink.set_property('qos-interval', 10000000)
                    sink.set_property('max-errors', 3)
                    sink.set_property('show-preroll-frame', True)
                    sink.set_property('vsync', 1)
                    sink.set_property('drm-vsync', True)
                    sink.set_property('max-render-delay', 15000000)
                    sink.set_property('frame-buffer-count', 2)
                    sink.set_property('max-parallel-triple-buffering', False)
                    sink.set_property('drm-mode', 'fifo')
                    sink.set_property('desired-framerate', '60/1')
                    sink.set_property('max-framerate', '60/1')
                    sink.set_property('presentation-mode', 'fifo' if self._is_fullscreen else 'auto')
                    sink.set_property('max-bitrate', 50000000)
                    sink.set_property('drop-frames', False)
                    sink.set_property('max-frame-latency', 2)
                    sink.set_property('max-frame-time', 50000000)
                    sink.set_property('enable-last-sample', True)
                    sink.set_property('drm-atomic', True)
                    sink.set_property('direct-rendering', True)
                    sink.set_property('max-process-time', 16666666)
                    sink.set_property('processing-deadline', 16666666)
                    os.environ['GST_VAAPI_DRM_DISABLE_VSYNC'] = '1'
                    os.environ['GST_VAAPI_DRM_THREADED_DISPLAY'] = '1'
            if not vaapi_available:
                self._log("VA-API not available, falling back to software rendering", level='warning')
                elements['videosink'] = Gst.ElementFactory.make('xvimagesink', 'videosink')
                if elements['videosink']:
                    elements['videosink'].set_property('sync', False)
                    elements['videosink'].set_property('async', True)
                    elements['videosink'].set_property('max-lateness', 10 * Gst.MSECOND)
                    elements['videosink'].set_property('qos', True)
                    elements['videosink'].set_property('show-preroll-frame', False)
            elements['audioconvert'] = Gst.ElementFactory.make('audioconvert', 'audioconvert')
            elements['audioresample'] = Gst.ElementFactory.make('audioresample', 'audioresample')
            elements['autoaudiosink'] = Gst.ElementFactory.make('autoaudiosink', 'autoaudiosink')
            for name, element in elements.items():
                if element and not element.get_parent():
                    if not pipeline.add(element):
                        self._log(f"Failed to add {name} to pipeline", level='error')
                        return False
            if vaapi_available:
                video_elements = [
                    elements['queue_video'],
                    elements['videoconvert'],
                    elements['vaapi'],
                    elements['vaapipostproc'],
                    elements['vaapisink']
                ]
            else:
                video_elements = [
                    elements['queue_video'],
                    elements['videoconvert'],
                    elements['videosink']
                ]
            audio_elements = [
                elements['queue_audio'],
                elements['audioconvert'],
                elements['audioresample'],
                elements['autoaudiosink']
            ]
            video_sink_pad = elements['queue_video'].get_static_pad('sink')
            audio_sink_pad = elements['queue_audio'].get_static_pad('sink')
            if not video_sink_pad or not audio_sink_pad:
                self._log("Failed to get sink pads for video/audio queues", level='error')
                return False
            if not Gst.Element.link_many(*video_elements):
                self._log("Failed to link video elements", level='error')
                return False
            if not Gst.Element.link_many(*audio_elements):
                self._log("Failed to link audio elements", level='error')
                return False
            ghost_video = Gst.GhostPad.new('sink', video_sink_pad)
            ghost_audio = Gst.GhostPad.new('sink', audio_sink_pad)
            if not ghost_video or not ghost_audio:
                self._log("Failed to create ghost pads", level='error')
                return False
            if not pipeline.add_pad(ghost_video):
                self._log("Failed to add video ghost pad to pipeline", level='error')
                return False
            if not pipeline.add_pad(ghost_audio):
                self._log("Failed to add audio ghost pad to pipeline", level='error')
                return False
            self._pipeline = pipeline
            self._videosink = elements.get('vaapisink') or elements.get('videosink')
            self._audiosink = elements['autoaudiosink']
            if self._videosink:
                self._configure_video_sink(self._videosink)
            return True
        except Exception as e:
            self._log(f"Error setting up GStreamer elements: {e}", level='error')
            if self.debug_mode:
                import traceback
                traceback.print_exc()
            return False

    def _check_gst_features(self):
        """Check for required GStreamer features and log any issues."""
        features = {
            'vaapi': Gst.ElementFactory.find('vaapidecode'),
            'gl': Gst.ElementFactory.find('glupload'),
            'pulse': Gst.ElementFactory.find('pulsesink'),
            'appsink': Gst.ElementFactory.find('appsink')
        }
        all_available = True
        for name, feature in features.items():
            if not feature:
                self._log(
                    f"Warning: GStreamer feature not available: {name}",
                    level='warning'
                )
                if name == 'appsink':
                    self._log(
                        "Install missing plugins with: "
                        "sudo dnf install gstreamer1-plugins-bad-free",
                        level='warning'
                    )
                all_available = False
            else:
                self._log(f"GStreamer feature available: {name}")
        return all_available

    def _on_pad_probe(self, pad, info, user_data):
        """Handle dynamic pad linking during caps negotiation.
        This method is called when a pad receives a probe during the CAPS negotiation
        phase. It attempts to link the source pad to the target sink pad if they are
        compatible.
        Args:
            pad: The source pad that received the probe
            info: The probe info object
            user_data: Dictionary containing the target 'sink_pad'
        Returns:
            Gst.PadProbeReturn: The action to take after this probe
        """
        try:
            if not user_data or 'sink_pad' not in user_data:
                return Gst.PadProbeReturn.REMOVE
            sink_pad = user_data['sink_pad']
            if not isinstance(sink_pad, Gst.Pad):
                return Gst.PadProbeReturn.REMOVE
            if pad.is_linked():
                return Gst.PadProbeReturn.REMOVE
            caps = pad.get_current_caps()
            if not caps:
                caps = pad.query_caps(None)
            if not caps:
                return Gst.PadProbeReturn.REMOVE
            link_ret = pad.link(sink_pad)
            if link_ret == Gst.PadLinkReturn.OK:
                return Gst.PadProbeReturn.REMOVE
            ghost_pad = Gst.GhostPad.new(f"ghost-{pad.get_name()}", pad)
            if ghost_pad:
                parent = pad.get_parent_element()
                if parent and ghost_pad.set_active(True):
                    parent.add_pad(ghost_pad)
                    link_ret = ghost_pad.link(sink_pad)
                    if link_ret == Gst.PadLinkReturn.OK:
                        return Gst.PadProbeReturn.REMOVE
                    else:
                        ghost_pad.set_active(False)
                        parent.remove_pad(ghost_pad)
            return Gst.PadProbeReturn.REMOVE
        except Exception:
            return Gst.PadProbeReturn.REMOVE

    def _configure_h264parse(self, h264parse, name="h264parse"):
        """Configure h264parse element with optimal settings for H.264 video streams.
        This method configures the h264parse element with settings that optimize
        H.264 stream parsing and improve compatibility with various decoders.
        Args:
            h264parse: The GStreamer h264parse element to configure
            name: Optional name for the element
        Returns:
            bool: True if configuration was successful, False otherwise
        """
        try:
            h264parse.set_property('max-framerate', 240)
            h264parse.set_property('config-interval', -1)
            h264parse.set_property('disable-passthrough', False)
            h264parse.set_property('output-corrupt', False)
            h264parse.set_property('output-reorder', True)
            h264parse.set_property('b-frames', 16)
            h264parse.set_property('alignment', 'au')
            h264parse.set_property('interval', 1)
            h264parse.set_property('nal-length-size', 4)
            h264parse.set_property('sync', True)
            h264parse.set_property('min-force-key-unit-interval', 5000000000)
            h264parse.set_property('max-reorder-buffers', 16)
            h264parse.set_property('disable-passthrough', False)
            h264parse.set_property('tolerance', 40000000)
            return True
        except Exception as e:
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"Error configuring h264parse: {e}", file=sys.stderr)
            return False

    def _configure_video_sink(self, sink, name="videosink"):
        """Configure video sink for optimal performance and hardware acceleration.
        Args:
            sink: The GStreamer video sink element to configure
            name: Optional name for the element
        Returns:
            bool: True if configuration was successful, False otherwise
        """
        if not sink:
            return False
        try:
            if hasattr(sink.props, 'handle-navigation-events'):
                sink.set_property('handle-navigation-events', False)
            if hasattr(sink.props, 'double-buffer'):
                sink.set_property('double-buffer', True)
            if hasattr(sink.props, 'autopaint-colorkey'):
                sink.set_property('autopaint-colorkey', False)
            if hasattr(sink.props, 'draw-borders'):
                sink.set_property('draw-borders', False)
            if hasattr(sink.props, 'draw-text-overlay'):
                sink.set_property('draw-text-overlay', False)
            if hasattr(sink.props, 'synchronous'):
                sink.set_property('synchronous', False)
            if hasattr(sink.props, 'show-preroll-frame'):
                sink.set_property('show-preroll-frame', False)
            if hasattr(sink.props, 'enable-last-buffer'):
                sink.set_property('enable-last-buffer', False)
            if hasattr(sink.props, 'max-lateness'):
                sink.set_property('max-lateness', 10000000)
            if hasattr(sink.props, 'qos'):
                sink.set_property('qos', True)
            if hasattr(sink.props, 'max-buffers'):
                sink.set_property('max-buffers', 5)
            if hasattr(sink.props, 'async'):
                sink.set_property('async', True)
            if hasattr(sink.props, 'ts-offset'):
                sink.set_property('ts-offset', 0)
            if hasattr(sink.props, 'render-delay'):
                sink.set_property('render-delay', 0)
            if hasattr(sink.props, 'processing-deadline'):
                sink.set_property('processing-deadline', 10000000)
            if hasattr(sink.props, 'max-bitrate'):
                sink.set_property('max-bitrate', 0)
            if hasattr(sink.props, 'max-errors'):
                sink.set_property('max-errors', -1)
            return True
        except Exception as e:
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"Error configuring video sink: {e}", file=sys.stderr)
            return False

    def _build_pipeline(self):
        """Build and return a GStreamer pipeline for video playback."""
        try:
            pipeline = Gst.Pipeline()
            if not pipeline:
                return None
            if not self._setup_gst_elements(pipeline):
                return None
            source = self._source
            videosink = self._videosink
            audiosink = self._audiosink
            if not all([source, videosink, audiosink]):
                return None
            ret = pipeline.set_state(Gst.State.READY)
            if ret == Gst.StateChangeReturn.FAILURE:
                return None
            elif ret == Gst.StateChangeReturn.ASYNC:
                ret, state, pending = pipeline.get_state(timeout=Gst.SECOND * 5)
                if ret == Gst.StateChangeReturn.FAILURE:
                    return None
            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                return None
            ret, state, pending = pipeline.get_state(timeout=Gst.SECOND * 5)
            if ret == Gst.StateChangeReturn.FAILURE:
                return None
            if state != Gst.State.PLAYING:
                return None
            return pipeline
        except Exception:
            return None

    def _on_pad_added(self, element, pad, targets):
        """Handle the pad-added signal from uridecodebin.
        Args:
            element: The element that emitted the signal
            pad: The source pad that was added
            targets: Dictionary containing 'video' and 'audio' target pads
        """
        try:
            pad_name = pad.get_name()
            if not pad_name:
                return
            caps = pad.get_current_caps()
            if not caps:
                caps = pad.query_caps(None)
            if not caps or caps.is_empty():
                return
            structure = caps.get_structure(0)
            if not structure:
                return
            media_type = structure.get_name()
            target = None
            media_kind = None
            if media_type.startswith('video/'):
                target = targets.get('video')
                media_kind = 'video'
            elif media_type.startswith('audio/'):
                target = targets.get('audio')
                media_kind = 'audio'
            else:
                return
            if pad.is_linked():
                return
            probe_id = pad.add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM | Gst.PadProbeType.BLOCK,
                self._on_pad_link_probe,
                {'target': target, 'media_kind': media_kind, 'caps': caps}
            )
            if not hasattr(self, '_pad_probes'):
                self._pad_probes = {}
            self._pad_probes[pad] = probe_id
        except Exception as e:
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"Error in _on_pad_added: {e}", file=sys.stderr)

    def _on_pad_link_probe(self, pad, info, user_data):
        """
        Handle pad probe to ensure proper event ordering before linking pads.
        This probe ensures that stream-start events are sent before caps events,
        preventing GStreamer warnings about sticky event misordering.
        """
        try:
            event = info.get_event()
            if event is None:
                return Gst.PadProbeReturn.PASS
            if event.type == Gst.EventType.STREAM_START:
                if not hasattr(self, '_pads_with_stream_start'):
                    self._pads_with_stream_start = {}
                self._pads_with_stream_start[pad] = event
                return Gst.PadProbeReturn.PASS
            elif event.type == Gst.EventType.CAPS:
                if not hasattr(self, '_pads_with_stream_start') or pad not in self._pads_with_stream_start:
                    if not hasattr(self, '_pending_caps_events'):
                        self._pending_caps_events = {}
                    self._pending_caps_events[pad] = event
                    return Gst.PadProbeReturn.DROP
            if 'GST_PLUGIN_FEATURE_RANK' not in os.environ and hasattr(self, 'gst_rank'):
                os.environ['GST_PLUGIN_FEATURE_RANK'] = self.gst_rank
                if hasattr(self, 'debug_mode') and self.debug_mode:
                    print(f"DEBUG: Set GST_PLUGIN_FEATURE_RANK={self.gst_rank}")
        except Exception as e:
            print(f"Error in _on_pad_link_probe: {e}", file=sys.stderr)
            return Gst.PadProbeReturn.REMOVE
        if 'GST_PLUGIN_FEATURE_RANK' not in os.environ and hasattr(self, 'gst_rank'):
            os.environ['GST_PLUGIN_FEATURE_RANK'] = self.gst_rank
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"DEBUG: Set GST_PLUGIN_FEATURE_RANK={self.gst_rank}")
        return Gst.PadProbeReturn.OK

    def initialize(self):
        """Initialize the video playback system with hardware acceleration if available."""
        if self._initialized:
            return True
        try:
            va_drivers = ['iHD', 'i965', 'radeonsi', 'nouveau']
            va_working = False
            for driver in va_drivers:
                try:
                    os.environ['LIBVA_DRIVER_NAME'] = driver
                    if self._check_vaapi_environment():
                        print(f"Using VA-API driver: {driver}")
                        va_working = True
                        break
                    print(f"VA-API driver {driver} not available")
                except Exception as e:
                    print(f"Error initializing VA-API driver {driver}: {e}")
            if not va_working:
                print("Warning: No working VA-API driver found, using software rendering")
            Gst.debug_set_active(True)
            Gst.debug_set_default_threshold(Gst.DebugLevel.WARNING)
            if not self._check_gst_plugins():
                print("Warning: Missing some GStreamer plugins, some features may be limited")
            if not self._setup_gst_elements():
                print("Warning: Failed to set up some GStreamer elements, using fallbacks")
            if not self._build_pipeline():
                print("Error: Failed to build GStreamer pipeline")
                return False
            print("Video pipeline configuration:")
            if hasattr(self, '_gst_elements') and 'videosink' in self._gst_elements:
                print(f"- Using hardware acceleration: {self._gst_elements.get('using_hw_accel', False)}")
                print(f"- Video sink: {self._gst_elements['videosink'].get_factory().get_name()}")
                print(f"- Audio sink: {self._gst_elements['audiosink'].get_factory().get_name()}")
            else:
                print("- Using software rendering (no hardware acceleration available)")
            self._initialized = True
            return True
        except Exception as e:
            print(f"Error initializing video pipeline: {e}")
            import traceback
            traceback.print_exc()
            return False

    def is_initialized(self):
        """Check if VA-API and GStreamer are properly initialized."""
        return all([
            GST_AVAILABLE,
            bool(self._gst_plugins),
            bool(self._gst_elements.get('videosink')),
            bool(self._gst_elements.get('audiosink'))
        ])
vaapi_manager = VAAPIManager()

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
        try:
            if hasattr(webview, 'download-started'):
                webview.connect("download-started", self.on_download_started)
            elif hasattr(webview, 'download-begin'):
                webview.connect("download-begin", self.on_download_started)
        except Exception as e:
            if hasattr(self, 'parent_window') and hasattr(self.parent_window, 'debug_mode') and self.parent_window.debug_mode:
                print(f"Error connecting download signal: {e}")

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
                if status == WebKit.DownloadStatus.FINISHED:
                    info["status"] = "Finished"
                    info["progress"].set_fraction(1.0)
                    info["progress"].set_text("100%")
                    info["label"].set_text(f"Download finished: {os.path.basename(info['filepath'])}")
                    GLib.timeout_add_seconds(5, lambda: self.cleanup_download(download))
                elif status == WebKit.DownloadStatus.FAILED:
                    info["status"] = "Failed"
                    info["label"].set_text(f"Download failed: {os.path.basename(info['filepath'])}")
                    info["progress"].set_text("Failed")
                    GLib.timeout_add_seconds(5, lambda: self.cleanup_download(download))
                elif status == WebKit.DownloadStatus.CANCELLED:
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

    def cancel_all_downloads(self):
        """Cancel all active downloads and clean up their UI elements."""
        with self.lock:
            downloads_to_cancel = list(self.downloads.items())
            for download_key, info in downloads_to_cancel:
                try:
                    if hasattr(download_key, 'cancel'):
                        download_key.cancel()
                    if info:
                        info["status"] = "Cancelled"
                        info["label"].set_text(f"Download cancelled: {os.path.basename(info.get('filepath', 'unknown'))}")
                        if "progress" in info and info["progress"]:
                            info["progress"].set_text("Cancelled")
                            info["progress"].set_fraction(0.0)
                        GLib.timeout_add_seconds(3, lambda k=download_key: self.cleanup_download(k))
                except Exception as e:
                    if hasattr(self, 'parent_window') and hasattr(self.parent_window, 'debug_mode') and self.parent_window.debug_mode:
                        print(f"Error cancelling download: {e}")
            self.downloads.clear()

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
            if self.download_area.get_parent() is not None:
                return
        self.download_area = Gtk.ScrolledWindow()
        self.download_area.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.download_area.set_max_content_height(200)
        self.download_area.set_min_content_height(0)
        self.download_area.set_child(self.box)
        self.download_area.set_vexpand(False)
        self.download_area.set_margin_top(5)
        self.download_area.set_margin_bottom(5)
        parent_window = self.parent_window
        if parent_window is None:
            return
        parent_child = parent_window.get_child()
        if parent_child is not None and hasattr(parent_child, "append"):
            if hasattr(self.download_area, 'get_parent') and self.download_area.get_parent() is not None:
                parent = self.download_area.get_parent()
                if parent and hasattr(parent, "remove"):
                    try:
                        if self.download_area.get_parent() == parent:
                            parent.remove(self.download_area)
                    except Exception:
                        pass
            try:
                parent_child.append(self.download_area)
            except Exception:
                pass

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
            csp_policy = "default-src 'self'; script-src 'self' https://trusted.com https://cdn.jsdelivr.net;"
        import re
        directives_to_remove = [
            r"\bmanifest-src[^;]*;?",
            r"require-trusted-types-for[^;]*;?",
            r"trusted-types[^;]*;?"
        ]
        sanitized_csp = csp_policy
        for directive in directives_to_remove:
            sanitized_csp = re.sub(directive, "", sanitized_csp, flags=re.IGNORECASE).strip()
        sanitized_csp = re.sub(r';\s*;', ';', sanitized_csp)
        if sanitized_csp.endswith(";"):
            sanitized_csp = sanitized_csp[:-1].strip()
        csp_script = f"""
        (function() {{
            try {{
                var meta = document.createElement('meta');
                meta.httpEquiv = 'Content-Security-Policy';
                meta.content = '{sanitized_csp}';
                var existingMeta = document.querySelectorAll('meta[http-equiv="Content-Security-Policy"]');
                existingMeta.forEach(function(meta) {{
                    meta.parentNode.removeChild(meta);
                }});
                if (document.head) {{
                    document.head.insertBefore(meta, document.head.firstChild);
                }} else {{
                    var head = document.createElement('head');
                    head.appendChild(meta);
                    document.documentElement.insertBefore(head, document.documentElement.firstChild);
                }}
                console.log('[CSP] Content Security Policy applied');
            }} catch (e) {{
                console.error('[CSP] Error applying policy:', e);
            }}
        }})();
        """
        content_manager = webview.get_user_content_manager()
        content_manager.remove_all_scripts()
        script = WebKit.UserScript.new(
            csp_script,
            WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.START,
            None
        )
        content_manager.add_script(script)

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
        page_origin = urlparse(self.get_current_webview().get_uri()).netloc
        return current_origin != page_origin

    def enable_mixed_content_blocking(self, webview):
        settings = webview.get_settings()
        settings.set_property("allow-running-insecure-content", False)
        webview.set_settings(settings)

    def secure_cookies(self):
        """Disable all cookies by setting accept policy to NEVER."""
        webview = self.get_current_webview()
        if webview:
            cookie_manager = webview.get_context().get_cookie_manager()
            cookie_manager.set_accept_policy(WebKit.CookiePolicyAccept.NEVER)

    def set_samesite_cookie(self, cookie_manager, cookie):
        cookie.set_same_site_policy(WebKit.CookieSameSitePolicy.STRICT)
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
        request.finish_error(WebKit.NetworkError.FAILED,
                           WebKit.PolicyDecisionType.IGNORE,
                           "Blob URI media playback not supported")

    def handle_data_uri(self, request, user_data=None):
        """Handle data: URIs for embedded content"""
        request.finish_error(WebKit.NetworkError.FAILED,
                           WebKit.PolicyDecisionType.IGNORE,
                           "Data URI handling not implemented")

    def handle_media_request(self, request, user_data=None):
        """Handle media requests for better streaming support"""
        uri = request.get_uri()
        if any(substring in uri for substring in self.blocklist):
            request.finish_error(WebKit.NetworkError.CANCELLED, "Media request blocked")
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
            import psutil
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
        proxy_settings = WebKit.NetworkProxySettings()
        proxy_settings.add_proxy_for_scheme("http", "socks5://127.0.0.1:{}".format(proxy_port))
        proxy_settings.add_proxy_for_scheme("https", "socks5://127.0.0.1:{}".format(proxy_port))
        proxy_settings.add_proxy_for_scheme("ftp", "socks5://127.0.0.1:{}".format(proxy_port))
        web_context.set_network_proxy_settings(proxy_settings)
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

class Tab:
    """Represents a single browser tab and its associated data."""

    def __init__(self, url, webview, scrolled_window=None):
        self.url = url or "about:blank"
        self.webview = webview
        self.scrolled_window = scrolled_window
        self._init_ui()
        self.last_activity = time.time()

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

class SystemWakeLock:
    def __init__(self, app_id="shadow-browser", reason="Browser is running"):
        self._inhibit_cookie = None
        self._dbus_inhibit = None
        self._inhibit_method = None
        self._app_id = app_id
        self._reason = reason
        self._setup_inhibit()
        os.environ["WEBKIT_DISABLE_DBUS_INHIT"] = "1"

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
                    flags = 4  # 4 = Inhibit idle (prevent screensaver)
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
                    except Exception as e:
                        print(f"Warning: Failed to close portal request: {e}")
                        return False
                self._inhibit_cookie = None
                return True
            except Exception as e:
                print(f"Warning: Could not release DBus inhibition: {e}")
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

class CORSProxy:
    def __init__(self, session=None):
        self.session = session or WebKit.WebContext.get_default().get_session()
        self.retry_delay = 1
        self.max_retries = 3
        self.allowed_domains = [
            "myfreecams.com",
            "edgevideo.myfreecams.com",
            "www.myfreecams.com"
        ]
    async def handle_request_async(self, message):
        """Handle CORS and rate limiting for requests asynchronously."""
        uri = message.get_uri()
        if not uri:
            return False
        if uri.endswith('.map'):
            message.set_status(404)
            return True
        request_headers = message.get_request_headers()
        response_headers = message.get_response_headers()
        if not any(domain in uri for domain in self.allowed_domains):
            return False
        response_headers.append("Access-Control-Allow-Origin", "*")
        response_headers.append("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE")
        response_headers.append("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        response_headers.append("Access-Control-Allow-Credentials", "true")
        response_headers.append("Access-Control-Max-Age", "86400")
        if message.get_http_method() == "OPTIONS":
            message.set_status(204)
            return True
        if "myfreecams.com" in uri:
            request_headers.replace("Origin", "https://www.myfreecams.com")
            request_headers.replace("Referer", "https://www.myfreecams.com/")
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                self.session.send_message(message)
                status = message.get_status()
                if status == 429:
                    retry_after = int(response_headers.get_one("Retry-After") or self.retry_delay)
                    await asyncio.sleep(retry_after * (2 ** retry_count))  # Exponential backoff
                    retry_count += 1
                    continue
                return True
            except Exception as e:
                print(f"Request error: {e}")
                if retry_count < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (2 ** retry_count))
                    retry_count += 1
                else:
                    return False
        return False

class ShadowBrowser(Gtk.Application):
    def _create_secure_web_context(self):
        """Create and configure a secure WebKit WebContext with persistent storage."""
        import os
        from pathlib import Path
        base_dir = os.path.expanduser("~/.shadowbrowser")
        data_dir = os.path.join(base_dir, "data")
        for directory in [base_dir, data_dir]:
            Path(directory).mkdir(parents=True, exist_ok=True)
        context = WebKit.WebContext()
        if hasattr(context, 'set_tls_errors_policy'):
            context.set_tls_errors_policy(WebKit.TLSErrorsPolicy.FAIL)
        if hasattr(context, 'set_web_security_enabled'):
            context.set_web_security_enabled(True)
        if hasattr(context, 'set_allow_universal_access_from_file_urls'):
            context.set_allow_universal_access_from_file_urls(False)
        if hasattr(context, 'set_allow_file_access_from_file_urls'):
            context.set_allow_file_access_from_file_urls(False)
        self._global_csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https: wss:; "
            "media-src 'self' https:; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "upgrade-insecure-requests; "
            "block-all-mixed-content;"
        )
        return context

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
            logging.error(f"Error handling script dialog: {e}")
        return False

    def _on_insecure_content(self, webview, event):
        """Handle insecure content detection."""
        uri = webview.get_uri()
        logging.warning(f"Insecure content detected on {uri}: {event.value_name}")
        return True

    def _on_permission_request(self, webview, request):
        """Handle permission requests with secure defaults."""
        permission = request.get_permission()
        if permission in [
            WebKit.PermissionRequestType.GEOLOCATION,
            WebKit.PermissionRequestType.MEDIA_KEY_SYSTEM_ACCESS,
            WebKit.PermissionRequestType.MIDI_SYSEX,
            WebKit.PermissionRequestType.NOTIFICATIONS,
            WebKit.PermissionRequestType.USER_MEDIA
        ]:
            logging.info(f"Denying permission request: {permission.value_name}")
            request.deny()
            return True
        return False

    def _on_resource_load_started(self, webview, resource, request, response=None):
        """Intercept and secure resource loading."""
        uri = request.get_uri()
        if any(uri.startswith(proto) for proto in ['javascript:', 'vbscript:', 'data:']):
            request.set_uri('about:blank')
            return
        if self._should_upgrade_to_https(uri):
            secure_uri = self._upgrade_to_https(uri)
            request.set_uri(secure_uri)
            logging.info(f"Upgraded to HTTPS: {uri} -> {secure_uri}")
            return
        if response:
            headers = response.get_http_headers()
            if headers:
                headers = self._sanitize_headers(dict(headers))
                response.set_http_headers(headers)

    def __init__(self):
        os.environ["WEBKIT_DISABLE_DBUS_INHIBIT"] = "1"
        os.environ["WEBKIT_DISABLE_MPRIS"] = "1"
        super().__init__(application_id="com.shadowyfigure.shadowbrowser")
        self.debug_mode = True
        self.wake_lock = SystemWakeLock()
        self.wake_lock_active = False
        self.webview = WebKit.WebView()
        self.content_manager = self.webview.get_user_content_manager()
        self.social_tracker_blocker = SocialTrackerBlocker()
        self.adblocker = AdBlocker()
        self.adblocker.enable()
        self.setup_webview_settings(self.webview)
        self._create_secure_web_context()
        self.webview.connect("create", self.on_webview_create)
        self.bookmarks = self.load_json(BOOKMARKS_FILE)
        self.history = self.load_json(HISTORY_FILE)
        self.tabs = []
        self.tabs_lock = threading.Lock()
        self.blocked_urls = []
        self.window = None
        self.notebook = Gtk.Notebook()
        self.url_entry = Gtk.Entry()
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
        self.favicon_cache = {}
        self.default_favicon = Gdk.Texture.new_for_pixbuf(
            GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 16, 16)
        )
        self.download_manager.on_download_start_callback = self.on_download_start
        self.download_manager.on_download_finish_callback = self.on_download_finish
        try:
            self.inject_nonce_respecting_script()
            self.inject_remove_malicious_links()
            self.inject_adware_cleaner()
            if not hasattr(self, 'webview'):
                self.webview = WebKit.WebView()
            self.disable_biometrics_in_webview(self.webview)
            self.content_manager.register_script_message_handler("voidLinkClicked")
            self.content_manager.connect(
                "script-message-received::voidLinkClicked", self.on_void_link_clicked
            )
            test_script = WebKit.UserScript.new(
                "console.log('Test script injected into shared content manager');",
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
            )
            self.content_manager.add_script(test_script)
        except Exception as e:
            print(f"Error setting up content manager: {e}")

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
        return 'https://' + url[7:]  # Replace 'http://' with 'https://'

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
            'X-Content-Type-Options': 'nosniff',
            'X-XSS-Protection': '1; mode=block',
            'Referrer-Policy': 'strict-origin-when-cross-origin',
            'Permissions-Policy': 'geolocation=(), microphone=(), camera=()',
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
        directives_to_remove = [
            r"\bmanifest-src[^;]*;?",
            r"require-trusted-types-for[^;]*;?",
            r"trusted-types[^;]*;?"
        ]
        sanitized = csp_policy
        for directive in directives_to_remove:
            sanitized = re.sub(directive, "", sanitized, flags=re.IGNORECASE).strip()
        sanitized = re.sub(r';\s*;', ';', sanitized)
        sanitized = re.sub(r';\s*$', '', sanitized)
        return sanitized

    def _enforce_frame_ancestors(self, webview, frame_ancestors=None):
        """Enforce frame-ancestors policy using X-Frame-Options as fallback."""
        if frame_ancestors is None:
            frame_ancestors = "'none'"
        csp = f"frame-ancestors {frame_ancestors};"
        settings = webview.get_settings()
        settings.set_property("enable-csp", True)
        if "'none'" in frame_ancestors.lower():
            settings.set_property("enable-frame-flattening", True)
        elif "'self'" in frame_ancestors.lower():
            settings.set_property("enable-frame-flattening", False)
        csp_script = f"""
        (function() {{
            try {{
                var meta = document.createElement('meta');
                meta.httpEquiv = 'Content-Security-Policy';
                meta.content = '{csp}';
                var head = document.head || document.getElementsByTagName('head')[0];
                head.appendChild(meta);
            }} catch (e) {{
                console.error('Failed to apply CSP:', e);
            }}
        }})();
        """
        script = WebKit.UserScript.new(
            csp_script,
            WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.START,
            None,
            None
        )
        webview.get_user_content_manager().add_script(script)

    def on_void_link_clicked(self, _user_content_manager, js_message):
        """Handle clicks on void links (e.g., javascript:void(0) links)."""
        if not js_message:
            return
        try:
            js_value = js_message.get_js_value()
            if js_value and hasattr(js_value, 'to_string'):
                url = js_value.to_string()
                if url and url.startswith('http'):
                    self.open_url_in_new_tab(url)
        except Exception as e:
            logging.error(f"Error handling void link click: {e}")

    def initialize_tor(self, retry_count=0, max_retries=2):
        """Initialize Tor with proper error handling and fallback mechanisms."""
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
                return False
            if self.tor_manager.start():
                self.tor_status = "running"
                tor_port = getattr(self.tor_manager, 'tor_port', 9050)
                try:
                    session = requests.Session()
                    session.proxies = {
                        'http': f'socks5://127.0.0.1:{tor_port}',
                        'https': f'socks5://127.0.0.1:{tor_port}'
                    }
                    session.get('https://check.torproject.org', timeout=5)
                    self.tor_status = "running"
                    return True
                except Exception as e:
                    logging.error(f"Error initializing Tor: {e}")
                    self.tor_status = "failed"
                    return False
        except Exception as e:
            logging.error(f"Error initializing Tor: {e}")
            self.tor_status = "failed"
            return False
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
                        self.tor_status = "running"
                        return True
                    else:
                        self.tor_status = "misconfigured"
                        return False
                except requests.exceptions.RequestException as e:
                    if hasattr(e, 'response') and e.response is not None:
                        print(f"Response status: {e.response.status_code}")
                        print(f"Response text: {e.response.text[:500]}")
                    return self.initialize_tor(retry_count + 1, max_retries)
            else:
                if retry_count < max_retries:
                    if self.tor_manager:
                        self.tor_manager.stop()
                        self.tor_manager = None
                    return self.initialize_tor(retry_count + 1, max_retries)
                return False
        except Exception as e:
            logging.error(f"Error initializing Tor: {e}")
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
            WebKit.WebView: A configured WebView instance or None if creation fails
        """
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
            except Exception:
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
                                except Exception:
                                    pass
                        del webview._handler_ids
                    del webview._content_manager
                except Exception:
                    pass
        webview.load_uri('about:blank')
        if hasattr(webview, 'stop_loading'):
            webview.stop_loading()
        if hasattr(webview, 'load_html_string'):
            webview.load_html_string('', 'about:blank')
        parent = webview.get_parent()
        if parent:
            parent.remove(webview)

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

    def on_download_start(self):
        if not self.download_spinner:
            return
        self.active_downloads += 1
        if self.active_downloads == 1:
            GLib.idle_add(self.download_spinner.start)
            GLib.idle_add(lambda: self.download_spinner.set_visible(True))

    def on_download_finish(self):
        if not self.download_spinner:
            return
        if self.active_downloads > 0:
            self.active_downloads -= 1
        if self.active_downloads == 0:
            GLib.idle_add(self.download_spinner.stop)
            GLib.idle_add(lambda: self.download_spinner.set_visible(False))

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

    def uuid_to_token(self, uuid_str: str):
        import uuid
        try:
            u = uuid.UUID(uuid_str)
            b = u.bytes
            token = base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
            return token
        except Exception:
            return uuid_str

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

    def on_request_started(self, session, message, user_data=None):
        """Handle all HTTP requests through the CORS proxy."""
        uri = message.get_uri()
        if not uri:
            return False
        try:
            return self.loop.run_until_complete(self.cors_proxy.handle_request_async(message))
        except Exception as e:
            print(f"Error in request handler: {e}")
            return False

    def get_tab_for_webview(self, webview):
        """Find tab containing this webview."""
        for tab in self.tabs:
            if tab.webview == webview:
                return tab
        return None

    def setup_webview_settings(self, webview):
        settings = webview.get_settings()
        settings_map = {
            'media-playback-requires-user-gesture': True,
            'enable-media-stream': False,
            'enable-webaudio': False,
            'enable-webrtc': False,
            'enable-javascript': True,
            'enable-smooth-scrolling': True,
            'enable-page-cache': True,
            'enable-offline-web-application-cache': False,
            'enable-html5-database': False,
            'enable-html5-local-storage': False,
            'enable-developer-extras': True,
            'enable-write-console-messages-to-stdout': True,
            'enable-site-specific-quirks': True,
            'enable-caret-browsing': False,
            'enable-encrypted-media': False,
            'enable-media-capabilities': False,
        }
        click_gesture = Gtk.GestureClick(button=3)
        click_gesture.connect('pressed', self.on_right_click)
        webview.add_controller(click_gesture)
        for prop, value in settings_map.items():
            if hasattr(settings.props, prop):
                setattr(settings.props, prop, value)
            else:
                print(f"[DEBUG] WebKitSettings does not have property: {prop}")
        webview.set_settings(settings)

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
                        logDebug(`Link details - href: ${href}, hasOnClick: ${hasOnClick}, isVoidLink: ${isVoidLink}`);
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
                                logDebug('No URL found for clickable element');
                            }
                        }
                    } else {
                        logDebug('No clickable element found for:', target);
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
            if self.window and self.window.get_window():
                window_state = self.window.get_window().get_state()
                if window_state & Gdk.WindowState.FULLSCREEN:
                    self.window.unfullscreen()
                    return True
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

    def on_tor_status_clicked(self, button):
        """Handle Tor status button click."""
        self.toggle_tor(not self.tor_enabled)
        self.update_tor_status_indicator()

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
        except Exception as e:
            if self.debug_mode:
                print(f"DEBUG: Failed to create dev tools: {e}")
        if hasattr(self, 'download_spinner') and self.download_spinner:
            self.download_spinner.set_halign(Gtk.Align.END)
            self.download_spinner.set_valign(Gtk.Align.CENTER)
            self.download_spinner.set_margin_start(6)
            self.download_spinner.set_margin_end(6)
            self.download_spinner.set_visible(False)
            self.toolbar.append(self.download_spinner)
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
            except Exception:
                try:
                    dev_enabled = bool(settings.get_property('enable-developer-extras'))
                except Exception:
                    pass
            if not dev_enabled:
                try:
                    if hasattr(settings, 'set_enable_developer_extras'):
                        settings.set_enable_developer_extras(True)
                    else:
                      settings.set_property("enable-accelerated-2d-canvas", True)
                except Exception:
                    pass
        inspector = getattr(webview, 'get_inspector', lambda: None)()
        if inspector and hasattr(inspector, 'show'):
            inspector.show()
        elif hasattr(webview, 'run_javascript'):
            js = "console.log('[Inspector] Requested via toolbar'); debugger;"
            try:
                webview.run_javascript(js, None, None, None)
            except Exception:
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
        except Exception as e:
            if self.debug_mode:
                print(f"Error clearing bookmarks menu: {e}")
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
            button.set_child(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6))
            button.get_child().append(favicon_img)
            button.get_child().append(label)
            button.set_hexpand(True)
            button.set_halign(Gtk.Align.FILL)
            button.connect("clicked", partial(self.load_url, url))
            delete_button = Gtk.Button()
            delete_icon = Gtk.Image.new_from_icon_name("edit-delete-symbolic")
            delete_button.set_child(delete_icon)
            delete_button.set_has_frame(False)
            delete_button.set_tooltip_text("Remove bookmark")
            delete_button.add_css_class("delete-button")
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
            """
            css_provider.load_from_data(css.encode())
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception as e:
            print(f"Error loading CSS: {e}")

    def do_activate(self):
        """Create and show the main window."""
        if not self.wake_lock_active:
            self.wake_lock_active = self.wake_lock.inhibit()
        if hasattr(self, "window") and self.window:
            try:
                self.window.present()
                return
            except Exception:
                self.window = None
        self.window = Gtk.ApplicationWindow(application=self, title="Shadow Browser")
        btn_small = Gtk.Button()
        btn_small.set_child(Gtk.Image.new_from_icon_name("zoom-out-symbolic"))
        btn_small.connect("clicked", lambda w: self.window.set_default_size(640, 360))
        btn_medium = Gtk.Button()
        btn_medium.set_child(Gtk.Image.new_from_icon_name("view-fullscreen-symbolic"))
        btn_medium.connect("clicked", lambda w: self.window.set_default_size(880, 540))
        btn_large = Gtk.Button()
        btn_large.set_child(Gtk.Image.new_from_icon_name("zoom-in-symbolic"))
        btn_large.connect("clicked", lambda w: self.window.set_default_size(1200, 800))
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
        self.safe_append(vbox, self.notebook)
        self.download_manager.parent_window = self.window
        self.download_manager.show()
        self.safe_append(vbox, self.download_manager.box)
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
            if hasattr(self, 'download_manager') and self.download_manager:
                try:
                    if hasattr(self.download_manager, 'box') and self.download_manager.box:
                        self.download_manager.clear_all()
                        try:
                            if hasattr(self.download_manager.box, 'get_parent') and self.download_manager.box.get_parent() is not None:
                                parent = self.download_manager.box.get_parent()
                                if parent and hasattr(parent, "remove") and self.download_manager.box.get_parent() == parent:
                                    parent.remove(self.download_manager.box)
                        except Exception:
                            pass
                        self.download_manager.box = None
                    if hasattr(self.download_manager, 'download_area') and self.download_manager.download_area:
                        try:
                            if hasattr(self.download_manager.download_area, 'get_parent') and self.download_manager.download_area.get_parent() is not None:
                                parent = self.download_manager.download_area.get_parent()
                                if parent and hasattr(parent, "remove") and self.download_manager.download_area.get_parent() == parent:
                                    parent.remove(self.download_manager.download_area)
                        except Exception:
                            pass
                        self.download_manager.download_area = None
                    if hasattr(self.download_manager, 'download_spinner') and self.download_manager.download_spinner:
                        try:
                            self.download_manager.download_spinner.stop()
                            self.download_manager.download_spinner.set_visible(False)
                        except Exception:
                            pass
                        self.download_manager.download_spinner = None
                    self.download_manager = None
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
            self.bookmark_popover.set_size_request(300, -1)
            if not hasattr(self, 'bookmark_menu') or self.bookmark_menu is None:
                self.bookmark_menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
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
            self.update_bookmarks_menu(self.bookmark_menu)
            self.bookmark_popover.set_child(self.bookmark_menu)
            self.bookmark_menu_button.set_popover(self.bookmark_popover)
            self.bookmark_popover.connect("closed", lambda popover: popover.set_visible(False))
            self.safe_append(menubar, self.bookmark_menu_button)
        except Exception:
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
            self.tor_button = Gtk.Button()
            self.tor_button.set_tooltip_text("Toggle Tor connection")
            self.update_tor_button()
            self.tor_button.connect("clicked", self.on_tor_button_clicked)
            self.safe_append(menubar, self.tor_button)
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
        return menubar

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
        self.tor_toggle = Gtk.CheckButton(label="Enable Tor (Requires Tor to be installed)")
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

    def toggle_tor(self, enabled):
        """Toggle Tor on or off.
        Args:
            enabled (bool): Whether to enable or disable Tor
        Returns:
            bool: True if the operation was successful, False otherwise
        """
        if enabled:
            if not hasattr(self, 'tor_manager') or not self.tor_manager:
                self.tor_manager = TorManager()
            if self.tor_manager.start():
                tor_proxy = f"socks5h://127.0.0.1:{self.tor_manager.tor_port}"
                os.environ['http_proxy'] = tor_proxy
                os.environ['https_proxy'] = tor_proxy
                os.environ['all_proxy'] = tor_proxy
                self.tor_enabled = True
                GLib.idle_add(self.update_tor_status_indicator)
                return True
            else:
                GLib.idle_add(self.update_tor_status_indicator)
                return False
        else:
            self.tor_enabled = False
            if hasattr(self, 'tor_manager') and self.tor_manager:
                self.tor_manager.stop()
            os.environ.pop('http_proxy', None)
            os.environ.pop('https_proxy', None)
            os.environ.pop('all_proxy', None)
            self.home_url = "https://duckduckgo.com/"
            GLib.idle_add(self.update_tor_status_indicator)
            return True
        GLib.idle_add(self.update_tor_status_indicator)
        return False

    def on_tor_toggled(self, toggle_button):
        enabled = toggle_button.get_active()
        if self.toggle_tor(enabled):
            with self.tabs_lock:
                for tab in self.tabs:
                    if hasattr(tab, 'webview'):
                        GLib.idle_add(tab.webview.reload)
            if enabled:
                GLib.timeout_add(1000, self.update_tor_status_indicator)
        else:
            toggle_button.set_active(not enabled)
            self.show_error_message("Failed to toggle Tor. Please check the logs for more details.")
            self.update_tor_status_indicator()

    def update_tor_status_indicator(self):
        if not hasattr(self, 'tor_status_icon'):
            return
        if self.tor_enabled and hasattr(self, 'tor_manager') and self.tor_manager and self.tor_manager.is_running():
            icon_name = "network-transmit-receive-symbolic"
            tooltip = "Tor is enabled (click to disable)"
            opacity = 1.0
        else:
            icon_name = "network-transmit-receive-symbolic"
            tooltip = "Tor is disabled (click to enable)"
            opacity = 0.5
        self.tor_status_icon.set_from_icon_name(icon_name)
        new_icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        if hasattr(self.tor_status_button, 'get_child'):
            self.tor_status_button.remove(self.tor_status_button.get_child())
            self.tor_status_button.add(new_icon)
            self.tor_status_icon = new_icon
        self.tor_status_icon.set_tooltip_text(tooltip)
        if hasattr(self.tor_status_icon.props, 'opacity'):
            self.tor_status_icon.props.opacity = opacity
        self.tor_status_button.show_all()

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
                self.show_info_message("Tor enabled - All traffic now routed through Tor")
            else:
                self.show_info_message("Tor disabled - Using direct connection")
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
            return False
        context = webview.get_context()
        if hasattr(context, 'get_cookie_manager'):
            cookie_manager = context.get_cookie_manager()
            if cookie_manager:
                cookie_manager.delete_all_cookies()
                return True
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
            dialog = Gtk.MessageDialog(
                transient_for=self.window,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Browsing history has been cleared"
                )
            dialog.connect("response", lambda d, r: d.destroy())
            dialog.present()

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

    def on_downloads_clicked(self, button):
        downloads_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if not downloads_dir:
            downloads_dir = os.path.expanduser("~/Downloads")
        import subprocess
        subprocess.Popen(["xdg-open", downloads_dir])

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
        if self.debug_mode:
            msg = f"DEBUG: Added new bookmark for {url}"
            self.show_error_message(msg)
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

    def _gst_log_handler(self, category, level, file, function, line, obj, message, user_data):
        """Handle GStreamer log messages."""
        if level >= Gst.DebugLevel.WARNING:
            if self.debug_mode:
                print(f"[GStreamer {level}] {message}")

    def _init_gstreamer(self):
        """Initialize GStreamer with optimal settings."""
        if not GST_AVAILABLE:
            return
        try:
            Gst.init_check(None)
            os.environ['GST_PLUGIN_PATH'] = '/usr/lib/x86_64-linux-gnu/gstreamer-1.0/plugins'
            if os.path.exists('/dev/dri'):
                os.environ['GST_VAAPI_ALL_DRIVERS'] = '1'
                os.environ['LIBVA_DRIVER_NAME'] = 'iHD'
            if self.debug_mode:
                os.environ['GST_DEBUG'] = '3'
                os.environ['GST_DEBUG_DUMP_DOT_DIR'] = '/tmp/gst-debug'
                os.path.exists('/tmp/gst-debug') or os.makedirs('/tmp/gst-debug')
        except Exception as e:
            if self.debug_mode:
                print(f"Error initializing GStreamer: {e}")
        about_dialog = Gtk.AboutDialog(transient_for=self.window)
        about_dialog.connect("response", lambda d, r: d.destroy())
        about_dialog.present()

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
        about_dialog.set_version("1.0")
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

    def add_new_tab(self, url):
        """Add a new tab with a webview loading the specified URL."""
        webview = self.create_secure_webview()
        if webview is None:
            return
        webview.load_uri(url)
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_child(webview)
        tab = Tab(url, webview, scrolled_window)
        if url and url != 'about:blank':
            def set_initial_favicon():
                favicon = self.get_favicon(url)
                if favicon:
                    tab.favicon.set_paintable(favicon)
            GLib.idle_add(set_initial_favicon)
        tab_index = self.notebook.append_page(scrolled_window, tab.header_box)
        self.notebook.set_current_page(tab_index)
        self.tabs.append(tab)

        def on_close_clicked(button, tab=tab):
            if tab in self.tabs:
                tab_index = self.tabs.index(tab)
                self.on_tab_close_clicked(button, tab_index)
        tab.close_button.connect("clicked", on_close_clicked)
        signal_map = {
            "load-changed": self.on_load_changed,
            "notify::title": self.on_title_changed,
            "decide-policy": self.on_decide_policy,
        }
        for sig, handler in signal_map.items():
            try:
                webview.connect(sig, handler)
            except Exception as e:
                if self.debug_mode:
                    print(f"[WARN] signal '{sig}' connect error: {e}")

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
                    try:
                        if hasattr(webview, 'disconnect_by_func'):
                            handler = getattr(self, f'on_{signal.replace("-", "_").replace("::", "__")}', None)
                            if handler:
                                webview.disconnect_by_func(handler)
                    except Exception as e:
                        if hasattr(self, 'debug_mode') and self.debug_mode:
                            print(f"[DEBUG] Error disconnecting {signal}: {e}")
            if page is not None:
                self.notebook.remove_page(tab_index)
            removed_tab = self.tabs.pop(tab_index)
            try:
                if hasattr(removed_tab, 'webview'):
                    removed_tab.webview = None
                if hasattr(removed_tab, 'destroy'):
                    removed_tab.destroy()
            except Exception as e:
                if hasattr(self, 'debug_mode') and self.debug_mode:
                    print(f"[DEBUG] Error cleaning up tab: {e}")
            if not self.tabs:
                self.add_new_tab("about:blank")

    def on_load_changed(self, webview, load_event):
        """Handle page load events to update UI."""
        if load_event == WebKit.LoadEvent.FINISHED:
            url = webview.get_uri()
            if self.debug_mode:
                print(f"[LOAD] Page finished loading: {url}")

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
                decision.use()
                return True
        parsed = urlparse.urlparse(requested_url)
        if parsed.scheme not in ("http", "https"):
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
        if url.lower() == "javascript:void(0)":
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
        self.add_webview_to_tab(new_webview)
        new_webview.load_uri(url)
        decision.ignore()
        return True

    def on_decide_policy(self, webview, decision, decision_type):
        """Handle navigation and new window actions, manage downloads, enforce policies, and apply adblock rules."""
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            return self._handle_navigation_action(
                webview, decision, decision.get_navigation_action()
            )
        elif decision_type == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            return self._handle_new_window_action(webview, decision)
        else:
            decision.use()
            return True

    def add_download_spinner(self, toolbar):
        """Add download spinner to toolbar."""
        if toolbar:
            toolbar.append(self.download_spinner)
            self.download_spinner.set_halign(Gtk.Align.END)
            self.download_spinner.set_valign(Gtk.Align.END)
            self.download_spinner.set_margin_start(10)
            self.download_spinner.set_margin_end(10)
            self.download_spinner.set_visible(True)

    def start_manual_download(self, url):
        """Manually download a file from the given URL."""
        import requests

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
                if self.debug_mode:
                    print(f"[DEBUG] Parsing URL: {url}")
                parsed_url = urlparse(url)
                GLib.idle_add(
                    lambda: self.show_error_message("Invalid URL format"),
                    priority=GLib.PRIORITY_DEFAULT,
                )
                return
                headers = {
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML like Gecko) Chrome/91.0.4472.124 Safari/537.36'
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

    def extract_tab_title(self, url):
        """Extract a display title from a URL, limited to 30 characters."""
        max_length = 30
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
        session_data = [
            {
                "url": tab.url,
                "title": tab.title_label.get_text() if hasattr(tab, 'title_label') and tab.title_label else "",
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

    def safe_window_cleanup(self):
        """Ensure proper window cleanup to prevent GTK warnings."""
        if not hasattr(self, 'window') or not self.window:
            return
        try:
            if hasattr(self.window, 'disconnect_by_func'):
                try:
                    self.window.disconnect_by_func(self.on_window_destroy)
                except Exception as e:
                    if self.debug_mode:
                        print(f"Error disconnecting window signal: {e}")
            if hasattr(self.window, 'get_child'):
                child = self.window.get_child()
                if child:
                    try:
                        if hasattr(self.window, 'remove') and callable(self.window.remove):
                            self.window.remove(child)
                        elif hasattr(self.window, 'set_child') and callable(self.window.set_child):
                            self.window.set_child(None)
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Error removing child widget: {e}")
            try:
                self.window.destroy()
            except Exception as e:
                if self.debug_mode:
                    print(f"Error destroying window: {e}")
            self.window = None
        except Exception as e:
            if self.debug_mode:
                print(f"Error during window cleanup: {e}")

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
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Error removing notebook page: {e}")
                        break
            except Exception as e:
                if self.debug_mode:
                    print(f"Error cleaning up notebook: {e}")
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
            scrolled_window.set_child(new_webview)
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
            close_button.connect("clicked", on_close_clicked)
            new_webview.connect("load-changed", self.on_load_changed)
            new_webview.connect("notify::title", self.on_title_changed)
            new_webview.connect("decide-policy", self.on_decide_policy)
            new_webview.connect("create", self.on_webview_create)
        except Exception:
            pass

    def add_webview_to_tab(self, webview, url=None, title=None, switch_to=True):
        """
        Add a webview to a new tab.
        Args:
            webview: The WebKit.WebView to add
            url: The URL to load (optional)
            title: The tab title (optional)
            switch_to: Whether to switch to the new tab (default: True)
        Returns:
            The created Tab object
        """
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_child(webview)
        tab = Tab(url, webview, scrolled_window)
        tab_index = self.notebook.append_page(scrolled_window, tab.header_box)
        signal_map = {
            "load-changed": self.on_load_changed,
            "notify::title": self.on_title_changed,
            "decide-policy": self.on_decide_policy,
        }
        for sig, handler in signal_map.items():
            try:
                webview.connect(sig, handler)
            except Exception as e:
                if self.debug_mode:
                    print(f"[WARN] signal '{sig}' connect error: {e}", file=sys.stderr)
            else:
                pass
        if switch_to:
            self.notebook.set_current_page(tab_index)
        self.tabs.append(tab)
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
        close_button = Gtk.Button.new_from_icon_name("window-close")
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
            div[class*="ad"]:not(.player-container, #player, .controls) {
                display: none !important;
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
                    meta.content = `script-src 'nonce-${nonce}' 'strict-dynamic' 'unsafe-inline' 'self'`;
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
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: function() { return 4; } });
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
        if self.tor_enabled and hasattr(self, 'tor_manager') and self.tor_manager.is_running():
            proxy_url = f'socks5h://127.0.0.1:{self.tor_manager.tor_port}'
            session.proxies = {
                'http': proxy_url,
                'https': proxy_url
            }
            try:
                test_url = 'https://check.torproject.org/api/ip'
                response = session.get(test_url, timeout=10)
                data = response.json()
                if not data.get('IsTor', False):
                    self.tor_enabled = False
            except Exception:
                pass
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

    def on_favicon_changed(self, webview, param):
        """Stub for favicon change handler (functionality removed)."""
        pass

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
        """Asynchronously fetch favicon from a URL."""
        try:
            import aiohttp
            import asyncio
            import ssl
            from urllib.parse import urljoin
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            async def _download_favicon(session, favicon_url):
                try:
                    async with session.get(favicon_url, ssl=ssl_context, timeout=5) as response:
                        if response.status == 200:
                            data = await response.read()
                            loader = GdkPixbuf.PixbufLoader()
                            loader.write(data)
                            loader.close()
                            return loader.get_pixbuf()
                except Exception as e:
                    if self.debug_mode:
                        print(f"Error downloading favicon from {favicon_url}: {e}")
                    return None
            async def _get_favicon():
                domain = self._extract_domain(url)
                if not domain:
                    return None
                base_url = f"https://{domain}"
                favicon_urls = [
                    f"{base_url}/favicon.ico",
                    f"{base_url}/favicon.png",
                    f"{base_url}/favicon.jpg"
                ]
                try:
                    import requests
                    from bs4 import BeautifulSoup
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    response = requests.get(url, timeout=5, verify=False,
                                         headers={'User-Agent': 'Mozilla/5.0'})
                    soup = BeautifulSoup(response.text, 'html.parser')
                    for link in soup.find_all('link', rel=['icon', 'shortcut icon']):
                        href = link.get('href')
                        if href:
                            favicon_urls.insert(0, urljoin(url, href))
                except Exception as e:
                    if self.debug_mode:
                        print(f"Error parsing HTML for favicon: {e}")
                async with aiohttp.ClientSession() as session:
                    for favicon_url in favicon_urls:
                        pixbuf = await _download_favicon(session, favicon_url)
                        if pixbuf:
                            width = pixbuf.get_width()
                            height = pixbuf.get_height()
                            size = min(16, width, height)
                            pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
                            return Gdk.Texture.new_for_pixbuf(pixbuf)
                return None
            return asyncio.new_event_loop().run_until_complete(_get_favicon())
        except Exception as e:
            if self.debug_mode:
                print(f"Error in favicon fetch: {e}")
            return None

    def get_favicon(self, url):
        """Get favicon for a URL, using cache if available.
        Args:
            url (str): The URL of the website to fetch favicon from
        Returns:
            Gdk.Texture: The favicon as a texture, or default favicon if not found
        """
        if not url or not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            return self.default_favicon
        domain = self._extract_domain(url)
        if not domain:
            return self.default_favicon
        if domain in self.favicon_cache:
            return self.favicon_cache[domain]
        if hasattr(self, '_favicon_fetch_in_progress'):
            if len(self._favicon_fetch_in_progress) > 5:
                return self.default_favicon
        else:
            self._favicon_fetch_in_progress = set()
        if domain in self._favicon_fetch_in_progress:
            return self.default_favicon

        def _update_favicon():
            try:
                self._favicon_fetch_in_progress.add(domain)
                favicon = self._fetch_favicon(url)
                if favicon and domain:
                    self.favicon_cache[domain] = favicon
                    if hasattr(self, 'bookmark_menu') and self.bookmark_menu:
                        GLib.idle_add(self.update_bookmarks_menu, self.bookmark_menu)
            except Exception as e:
                if self.debug_mode:
                    print(f"Error in favicon update: {e}")
            finally:
                if domain in self._favicon_fetch_in_progress:
                    self._favicon_fetch_in_progress.remove(domain)
        try:
            import threading
            thread = threading.Thread(target=_update_favicon, daemon=True)
            thread.start()
        except Exception as e:
            if self.debug_mode:
                print(f"Failed to start favicon fetch thread: {e}")
        return self.default_favicon

def main() -> None:
    """Main entry point for the Shadow Browser."""
    app = ShadowBrowser()
    return app.run(None)

if __name__ == "__main__":
    import sys
    sys.exit(main())
