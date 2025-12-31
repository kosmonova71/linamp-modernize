import datetime
import json
import logging
import os
import platform
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
import random
import requests
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from requests.adapters import HTTPAdapter
from stem.control import Controller
from urllib3.util.retry import Retry
import gi

try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
except ImportError:
    dbus = None
    DBusGMainLoop = None

GST_VAAPI_AVAILABLE = False
try:
    gi.require_version('GstVa', '1.0')
    from gi.repository import GstVa
    GstVaapi = GstVa
    GST_VAAPI_AVAILABLE = True
except (ImportError, ValueError):
    GstVaapi = None
    GST_VAAPI_AVAILABLE = False

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gtk, Gst, WebKit, Gdk, GLib

    Gst.init(None)
    Gst.debug_set_active(True)
    Gst.debug_set_default_threshold(Gst.DebugLevel.WARNING)
    GST_AVAILABLE = True

except (ImportError, ValueError) as e:
    print(f"Error initializing required libraries: {e}", file=sys.stderr)
    GST_AVAILABLE = False
    GST_VAAPI_AVAILABLE = False

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
    extract_onclick_url = extract_onclick_url
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
        """Detect supported codecs and capabilities"""
        codecs = {
            'h264': False,
            'h265': False,
            'vp8': False,
            'vp9': False,
            'av1': False
        }
        if not self._detected_driver or not self._detected_device:
            return codecs
        try:
            env = os.environ.copy()
            env['LIBVA_DRIVER_NAME'] = self._detected_driver
            env['GST_VAAPI_DRM_DEVICE'] = self._detected_device
            result = subprocess.run(
                ['vainfo'],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                codecs['h264'] = 'h264' in output or 'avc' in output
                codecs['h265'] = 'h265' in output or 'hevc' in output
                codecs['vp8'] = 'vp8' in output
                codecs['vp9'] = 'vp9' in output
                codecs['av1'] = 'av1' in output
        except Exception as e:
            self._log(f"Failed to detect codec support: {e}", 'warning')
        self._log(f"Detected codec support: {', '.join(k for k, v in codecs.items() if v) or 'None'}")
        return codecs

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
        config = self.get_optimal_vaapi_config()
        env = {
            'LIBVA_DRIVER_NAME': config['driver'],
            'GST_VAAPI_ALL_DRIVERS': '1',
            'GST_VAAPI_DRM_DEVICE': config['device'],
            'GST_VAAPI_DRM_DISPLAY': 'drm',
            'GST_VAAPI_DRM_BUFFER_SIZE': '67108864',
            'GST_VAAPI_DRM_MAX_BUFFERS': '64',
            'GST_VAAPI_DRM_MEMORY_TYPE': 'drm',
            'GST_VAAPI_DRM_ZERO_COPY': '1',
            'GST_VAAPI_DRM_DIRECT_RENDERING': '1',
            'GST_VAAPI_DRM_THREADED_DECODE': '1',
            'GST_VAAPI_DRM_DISABLE_IMPLICIT_SYNC': '1',
            'GST_VAAPI_DRM_IGNORE_DRM_MODE': '1',
            'GST_VAAPI_DRM_DISABLE_DROP_FRAME': '1',
            'GST_VAAPI_DRM_FORCE_COMPOSITION': '0',
            'GST_VAAPI_DRM_USE_OVERLAY': '1',
            'GST_VAAPI_DRM_USE_OVERLAY_SCALING': '1',
            'GST_VAAPI_THREAD_POOL_SIZE': str(config['threading']['pool_size']),
            'GST_VAAPI_THREAD_POOL_MAX_THREADS': str(config['threading']['max_threads']),
            'GST_VAAPI_THREAD_POOL_MIN_THREADS': '2',
            'GST_VAAPI_THREAD_POOL_STACK_SIZE': '2097152',
            'GST_VAAPI_THREAD_POOL_MAX_QUEUE_SIZE': '1024',
            'GST_VAAPI_THREAD_POOL_IDLE_TIMEOUT': '10000',
            'GST_VAAPI_DISABLE_INTERLACE': '1',
            'GST_VAAPI_DISABLE_PROTECTED': '1',
            'GST_VAAPI_DISABLE_DEINTERLACE': '0',
            'GST_VAAPI_DISABLE_SCALING': '0',
            'GST_VAAPI_DISABLE_COLORSPACE_CONVERSION': '0',
            'GST_VAAPI_FORCE_COLORSPACE': '0:0:0:0',
            'GST_VAAPI_FORCE_PROFILE': 'none',
            'GST_VAAPI_FORCE_ENTRYPOINT': 'none',
            'GST_VAAPI_MEMORY_TYPE': 'drm',
            'GST_VAAPI_MEMORY_CACHE_SIZE': '64',
            'GST_VAAPI_MEMORY_CACHE_MAX_SIZE': '128',
            'GST_VAAPI_MEMORY_CACHE_LOW_WATERMARK': '32',
            'GST_VAAPI_MEMORY_CACHE_HIGH_WATERMARK': '96',
            'GST_DEBUG': '2',
            'GST_DEBUG_DUMP_DOT_DIR': '/tmp/gst-debug',
            'GST_DEBUG_NO_COLOR': '1',
            'GST_DEBUG_FILE': '/tmp/gstreamer-debug.log',
            'GST_PLUGIN_SYSTEM_PATH': ';'.join([
                '/usr/lib64/gstreamer-1.0',
                '/usr/local/lib/gstreamer-1.0',
                '/usr/lib/x86_64-linux-gnu/gstreamer-1.0',
                '/usr/lib/aarch64-linux-gnu/gstreamer-1.0',
                '/usr/lib/arm-linux-gnueabihf/gstreamer-1.0'
            ]),
            'GST_PLUGIN_PATH': ';'.join([
                '/usr/lib64/gstreamer-1.0',
                '/usr/local/lib/gstreamer-1.0',
                '/usr/lib/x86_64-linux-gnu/gstreamer-1.0',
                '/usr/lib/aarch64-linux-gnu/gstreamer-1.0',
                '/usr/lib/arm-linux-gnueabihf/gstreamer-1.0'
            ]),
            'WEBKIT_DISABLE_COMPOSITING_MODE': '0',
            'WEBKIT_DISABLE_ACCELERATED_2D_CANVAS': '0',
            'WEBKIT_ENABLE_ACCELERATED_2D_CANVAS': '1',
            'WEBKIT_DISABLE_WEBGL': '0',
            'WEBKIT_WEBGL_ENABLED': '1',
            'WEBKIT_WEBGL2_ENABLED': '1',
            'WEBKIT_WEBGPU_ENABLED': '1',

           # WebKit settings
            'WEBKIT_SETTINGS_ENABLE_JAVASCRIPT': '1',
            'WEBKIT_SETTINGS_ENABLE_DEVELOPER_EXTRAS': '1',
            'WEBKIT_SETTINGS_ENABLE_WRITE_CONSOLE_MESSAGES_TO_STDOUT': '1',
            'WEBKIT_DEBUG': 'all',
            'G_MESSAGES_DEBUG': 'all',
            'WEBKIT_INSPECTOR_SERVER': '127.0.0.1:9222',

            # Audio settings
            'GST_PULSE_LATENCY_MSEC': '50',
            'PULSE_PROP_OVERRIDE_DEVICE_NAME': 'auto_null',
            'GSTREAMER_PLAYER_AUDIO_SINK': 'pulsesink',

            # OpenGL/EGL settings
            'GST_GL_PLATFORM': 'egl',
            'GST_GL_API': 'opengl',
            'GST_GL_WINDOW': 'egl',
            'GST_GL_DISPLAY': 'egl',
            'GST_GL_CONTEXT': 'egl',
            'GST_GL_USE_EGL': '1',
            'GST_GL_USE_GLX': '0',
            'GST_GL_USE_WAYLAND': '0',
            'GST_GL_USE_X11': '0',

            # Performance tuning
            'GST_VIDEO_DISABLE_COLORBALANCE': '1',
            'GST_VIDEO_DISABLE_GAMMA': '1',
            'GST_VIDEO_DISABLE_CONTOUR_CORRECTION': '1',
            'GST_VIDEO_SINK_XID': '0',
            'GST_VIDEO_OVERLAY_COMPOSITION': '1',
            'GST_VIDEO_VSYNC': 'enabled',
            'GST_VIDEO_FORCE_FPS': '0',

            # Network and buffering
            'GST_HTTP_BUFFER_SIZE': '10485760',  # 10MB
            'GST_HTTP_BUFFER_MAX_SIZE': '20971520',  # 20MB
            'GST_HTTP_RETRY_ATTEMPTS': '5',
            'GST_HTTP_RETRY_DELAY': '500000000',  # 0.5s
            'GST_HTTP_TIMEOUT': '30000000000',  # 30s

            # HLS/DASH streaming
            'GST_HLS_PLAYLIST_UPDATE_INTERVAL': '10000000',  # 10s
            'GST_HLS_LIVE_DELAY': '3000000000',  # 3s
            'GST_HLS_BUFFER_SIZE': '10485760',  # 10MB
            'GST_HLS_MAX_BUFFER_SIZE': '20971520',  # 20MB
        }
        codec_env = {
            'h264': 'GST_VAAPI_ENABLE_H264',
            'h265': 'GST_VAAPI_ENABLE_H265',
            'vp8': 'GST_VAAPI_ENABLE_VP8',
            'vp9': 'GST_VAAPI_ENABLE_VP9',
            'av1': 'GST_VAAPI_ENABLE_AV1'
        }
        for codec, var in codec_env.items():
            env[var] = '1' if config['features'].get(codec, False) else '0'
        for key, value in env.items():
            if key not in os.environ:
                os.environ[key] = value
        plugins = [
            'vaapih264dec:MAX', 'vaapih265dec:MAX',
            'vaapivp8dec:MAX' if config['features']['vp8'] else 'vaapivp8dec:0',
            'vaapivp9dec:MAX' if config['features']['vp9'] else 'vaapivp9dec:0',
            'vaapimpeg2dec:MAX', 'vaapijpegdec:MAX',
            'msdkh264dec:MAX', 'msdkh265dec:MAX',
            'vaapisink:MAX', 'glimagesink:MAX',
            'glvideomixer:MAX', 'glvideomixerelement:MAX',
            'avdec_h264:SECONDARY', 'avdec_h265:SECONDARY', 'avdec_aac:SECONDARY'
        ]
        if 'GST_PLUGIN_FEATURE_RANK' not in os.environ:
            os.environ['GST_PLUGIN_FEATURE_RANK'] = ','.join(plugin for plugin in plugins if not plugin.endswith(':0'))
        return env

    def configure_webkit_settings(self, webview):
        """Configure WebKit settings for optimal media playback and performance"""
        try:
            settings = webview.get_settings()

            # Media and playback settings
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
                'media-cache-size': 512 * 1024 * 1024,  # 512MB cache
                'media-disk-cache-disk-cache-directory': os.path.expanduser('~/.cache/shadow-browser/media'),
                'media-disk-cache-enabled': True,
                'media-disk-cache-size': 256 * 1024 * 1024,  # 256MB disk cache
                'media-source-enabled': True,
                'media-stream-enabled': True
            }

            # Hardware acceleration
            hw_settings = {
                'hardware-acceleration-policy': WebKit.HardwareAccelerationPolicy.ALWAYS,
                'enable-accelerated-2d-canvas': True,
                'enable-accelerated-video-decode': True,
                'enable-accelerated-video-encode': True,
                'enable-gpu': True,
                'enable-gpu-compositing': True,
                'enable-webgl': True,
                'enable-webgl2': True,
                'enable-webgpu': True,
                'enable-webxr': True
            }

            # Performance optimizations
            perf_settings = {
                'enable-accelerated-2d-canvas': True,
                'enable-accelerated-video': True,
                'enable-accelerated-video-decode': True,
                'enable-accelerated-video-encode': True,
                'enable-cache': True,
                'enable-javascript-markup': True,
                'enable-media-stream': True,
                # 'enable-offline-web-application-cache' is deprecated in newer WebKit versions
                # 'enable-offline-web-application-cache': True,
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

            # Security settings
            security_settings = {
                'allow-file-access-from-file-urls': False,
                'allow-universal-access-from-file-urls': False,
                'enable-caret-browsing': False,
                # 'enable-dns-prefetching' is deprecated in newer WebKit versions
                # 'enable-dns-prefetching': True,
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

            # Apply all settings with error handling
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
            self._videosink = None
            self._audiosink = None
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

                    # Initialize GStreamer if not already initialized
                    if not Gst.is_initialized():
                        Gst.init(None)

                    # Set up the main loop and context
                    self._loop = GLib.MainLoop()
                    self._context = GLib.MainContext.default()

                    # Configure debug settings
                    Gst.debug_set_active(True)
                    Gst.debug_set_default_threshold(Gst.DebugLevel.WARNING)

                    # Remove default log handler and add our custom one
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
        # Filter out common non-critical warnings
        ignore_messages = [
            "Got data flow before stream-start event",
            "Got data flow before segment event"
        ]

        if any(msg in message for msg in ignore_messages):
            return

        # Forward other messages to the default handler
        if hasattr(Gst.DebugLevel, 'NONE'):
            Gst.debug_log_default(category, level, file, function, line, obj, message, user_data)

    def _log(self, message, level='info'):
        """Log a message with the specified log level"""
        log_entry = f"[{level.upper()}] {message}"
        self._log_messages.append((level, message))

        # Also print to stderr for immediate feedback
        if level == 'error':
            print(f"\033[91m{log_entry}\033[0m", file=sys.stderr)
        elif level == 'warning':
            print(f"\033[93m{log_entry}\033[0m", file=sys.stderr)
        elif level == 'debug':
            if os.environ.get('GST_DEBUG_LEVEL', '0') != '0':
                print(f"\033[90m{log_entry}\033[0m", file=sys.stderr)
        else:
            print(log_entry, file=sys.stderr)

    def _check_vaapi_environment(self):
        """
        Check if VA-API environment is properly configured.
        This method verifies if the system has a working VA-API setup by:
        1. Checking if GStreamer VA-API bindings are available
        2. Looking for a suitable VA-API driver
        3. Configuring the DRM device
        4. Setting up environment variables for optimal VA-API operation
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
        except Exception:
            return False

    def _check_gst_plugins(self):
        """Check for required GStreamer plugins and return True if all are available.
        Returns:
            bool: True if all required plugins are available, False otherwise.
        """
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
                factory = Gst.ElementFactory.find(plugin)
                if not factory:
                    missing_plugins[category].append(plugin)
                    all_available = False
        return all_available

    def _setup_gst_elements(self, pipeline=None, use_vaapi=True):
        """Set up GStreamer elements with hardware acceleration if available."""

        if not GST_AVAILABLE:
            self._log("GStreamer not available, cannot set up elements", level='error')
            return False

        # Initialize elements dictionary
        elements = {}

        try:
            # Check for required features
            if not self._check_gst_features():
                self._log("Some required GStreamer features are missing", level='warning')

            # Try to create appsink with fallback to fakesink
            try:
                appsink = Gst.ElementFactory.make('appsink', 'appsink')
                if not appsink:
                    raise Exception("Failed to create appsink")
                self._log("Created appsink successfully")
            except Exception as e:
                self._log(f"Failed to create appsink: {e}, falling back to fakesink", level='warning')
                appsink = Gst.ElementFactory.make('fakesink', 'fakesink')
                if not appsink:
                    self._log("Failed to create fakesink fallback", level='error')
                    return False

            # Ensure required plugins are available
            required_plugins = ['playback', 'video', 'audio', 'pulse', 'gl', 'vaapi']
            missing_plugins = [p for p in required_plugins if not Gst.Registry.get().find_plugin(p)]

            if missing_plugins:
                self._log(f"Missing required GStreamer plugins: {', '.join(missing_plugins)}", level='error')
                return False

            # Create a new pipeline if none was provided
            if pipeline is None:
                self._pipeline = Gst.Pipeline.new('media-pipeline')
                if not self._pipeline:
                    self._log("Failed to create GStreamer pipeline", level='error')

                # Add all elements to the pipeline
                for element in elements.values():
                    if element and not element.get_parent():
                        pipeline.add(element)

                # Set up video bin
                video_bin = Gst.Bin.new("video_bin")
                video_elements = [
                    elements['queue_video'],
                    elements.get('h264parse') if 'h264parse' in elements and elements['h264parse'] else None,
                    elements.get('h265parse') if 'h265parse' in elements and elements['h265parse'] else None,
                    elements.get('h264dec') if 'h264dec' in elements and elements['h264dec'] else None,
                    elements.get('h265dec') if 'h265dec' in elements and elements['h265dec'] else None,
                    elements['videoconvert'],
                    elements['videosink']
                ]

                # Filter out None elements and add to bin
                video_elements = [e for e in video_elements if e is not None]
                for element in video_elements:
                    if not video_bin.add(element):
                        self._log(f"Failed to add {element.get_name()} to video bin", level='error')
                        return False

                # Link video elements
                if not Gst.Element.link_many(*video_elements):
                    self._log("Failed to link video elements", level='error')
                    return False

                # Set up audio bin
                audio_bin = Gst.Bin.new("audio_bin")
                audio_elements = [
                    elements['queue_audio'],
                    elements['audioconvert'],
                    elements['audioresample'],
                    elements['autoaudiosink']
                ]

                # Add audio elements to the bin
                for element in audio_elements:
                    if element and not audio_bin.add(element):
                        self._log(f"Failed to add {element.get_name()} to audio bin", level='error')
                        return False

                # Link audio elements
                if not Gst.Element.link_many(*audio_elements):
                    self._log("Failed to link audio elements", level='error')
                    return False

                # Add bins to the pipeline
                if not pipeline.add(video_bin):
                    self._log("Failed to add video bin to pipeline", level='error')
                    return False

                if not pipeline.add(audio_bin):
                    self._log("Failed to add audio bin to pipeline", level='error')
                    return False

                # Create ghost pads for dynamic linking
                video_sink_pad = elements['queue_video'].get_static_pad('sink')
                audio_sink_pad = elements['queue_audio'].get_static_pad('sink')

                if not video_sink_pad or not audio_sink_pad:
                    self._log("Failed to get sink pads for dynamic linking", level='error')
                    return False

                ghost_video = Gst.GhostPad.new('sink', video_sink_pad)
                ghost_audio = Gst.GhostPad.new('sink', audio_sink_pad)

                if not video_bin.add_pad(ghost_video):
                    self._log("Failed to add video ghost pad", level='error')
                    return False

                if not audio_bin.add_pad(ghost_audio):
                    self._log("Failed to add audio ghost pad", level='error')
                    return False

                # Connect pad-added signal for dynamic linking
                elements['source'].connect('pad-added', self._on_pad_added, {
                    'video': ghost_video,
                    'audio': ghost_audio
                })

                # Store element references
                self._source = elements['source']
                self._video_convert = elements['videoconvert']
                self._videosink = elements['videosink']
                self._audiosink = elements['autoaudiosink']

                self._log("Successfully set up GStreamer pipeline")
                if GST_VAAPI_AVAILABLE:
                    self._log("Using hardware-accelerated video decoding")
                else:
                    self._log("Using software video decoding")

                return True

        except Exception as e:
            self._log(f"Error in _setup_gst_elements: {str(e)}", level='error')
            import traceback
            self._log(f"Traceback: {traceback.format_exc()}", level='debug')
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
                self._log(f"Warning: GStreamer feature not available: {name}", level='warning')
                if name == 'appsink':
                    self._log("Install missing plugins with: sudo dnf install gstreamer1-plugins-bad-free", level='warning')
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
        except Exception:
            return False

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
        try:
            sink_name = sink.get_factory().get_name()
            self._log(f"Configuring video sink: {sink_name}")

            # Common sink properties
            if hasattr(sink.props, 'sync'):
                sink.set_property('sync', False)
            if hasattr(sink.props, 'max-lateness'):
                sink.set_property('max-lateness', 20000000)  # 20ms
            if hasattr(sink.props, 'qos'):
                sink.set_property('qos', True)
            if hasattr(sink.props, 'async'):
                sink.set_property('async', False)

            if sink_name == 'vaapisink':
                # VA-API specific settings
                sink.set_property('fullscreen-toggle-mode', 0)
                sink.set_property('show-preroll-frame', False)
                sink.set_property('max-buffers', 5)  # Increased from 3
                sink.set_property('vsync', True)
                sink.set_property('async', False)
                sink.set_property('drop', True)  # Drop frames if falling behind

                # Try to set display if not already set
                if hasattr(sink.props, 'display') and not sink.get_property('display'):
                    try:
                        display = GstVaapi.Display()
                        if display:
                            sink.set_property('display', display)
                            self._log("Successfully set VA-API display")
                    except Exception as e:
                        self._log(f"Error setting VA-API display: {str(e)}", level='warning')

            elif sink_name in ['xvimagesink', 'glimagesink', 'autovideosink']:
                # Fallback sink settings
                if hasattr(sink.props, 'force-aspect-ratio'):
                    sink.set_property('force-aspect-ratio', True)
                if hasattr(sink.props, 'handle-events'):
                    sink.set_property('handle-events', False)
                if hasattr(sink.props, 'handle-expose'):
                    sink.set_property('handle-expose', True)
                if hasattr(sink.props, 'texture-target'):
                    try:
                        sink.set_property('texture-target', 1)
                    except (GLib.GError, AttributeError, TypeError) as e:
                        self._log(f"Error setting texture target: {str(e)}", level='debug')

            self._log(f"Successfully configured video sink: {sink_name}")
            return True

        except Exception as e:
            self._log(f"Error configuring video sink: {str(e)}", level='error')
            return False

    def _setup_gst_elements(self, pipeline=None, use_vaapi=True):
        """Set up GStreamer elements with hardware acceleration if available.

        This method creates and configures a GStreamer pipeline with hardware-accelerated
        video decoding and rendering when available. It includes automatic fallbacks to
        software rendering if hardware acceleration is not available.

        Args:
            pipeline: Optional GStreamer pipeline to add elements to
            use_vaapi: Whether to try using VA-API hardware acceleration

        Returns:
            bool: True if setup was successful, False otherwise
        """
        try:
            if not GST_AVAILABLE:
                self._log("GStreamer not available, cannot set up elements", level='error')
                return False
            # Ensure required plugins are available
            required_plugins = ['playback', 'video', 'audio', 'pulse', 'gl']
            if use_vaapi and GST_VAAPI_AVAILABLE:
                required_plugins.append('vaapi')

            missing_plugins = [p for p in required_plugins if not Gst.Registry.get().find_plugin(p)]

            if missing_plugins:
                self._log(f"Missing required GStreamer plugins: {', '.join(missing_plugins)}", level='error')
                return False

            # Create a new pipeline if none was provided
            if pipeline is None:
                pipeline = Gst.Pipeline.new('media-pipeline')
                if not pipeline:
                    self._log("Failed to create GStreamer pipeline", level='error')
                    return False

                # Create elements dictionary
                elements = {}

                try:
                    # Create source element
                    elements['source'] = Gst.ElementFactory.make('uridecodebin', 'source')
                    elements['queue_video'] = Gst.ElementFactory.make('queue', 'video_queue')
                    elements['queue_audio'] = Gst.ElementFactory.make('queue', 'audio_queue')
                    # Tune queue properties to tolerate upstream jitter for live playback.
                    # Use downstream leaky mode so the queue can drop old buffers instead
                    # of stalling the pipeline when downstream lags.
                    try:
                        if elements['queue_video']:
                            # 5 MB max, ~2s max time, downstream leaky (2)
                            elements['queue_video'].set_property('max-size-bytes', 5 * 1024 * 1024)
                            elements['queue_video'].set_property('max-size-time', int(2 * 1e9))
                            elements['queue_video'].set_property('max-size-buffers', 0)
                            elements['queue_video'].set_property('leaky', 2)
                        if elements['queue_audio']:
                            # smaller audio buffer, 256KB
                            elements['queue_audio'].set_property('max-size-bytes', 256 * 1024)
                            elements['queue_audio'].set_property('max-size-time', int(1 * 1e9))
                            elements['queue_audio'].set_property('max-size-buffers', 0)
                            elements['queue_audio'].set_property('leaky', 2)
                    except Exception:
                        # If properties aren't supported by the specific queue element
                        # ignore errors and continue with defaults.
                        pass
                    elements['videoconvert'] = Gst.ElementFactory.make('videoconvert', 'video_convert')
                    elements['audioconvert'] = Gst.ElementFactory.make('audioconvert', 'audio_convert')
                    elements['audioresample'] = Gst.ElementFactory.make('audioresample', 'audio_resample')
                    elements['autoaudiosink'] = Gst.ElementFactory.make('autoaudiosink', 'audio_sink')

                    # Create video sink based on VA-API availability
                    if use_vaapi and GST_VAAPI_AVAILABLE:
                        elements['videosink'] = Gst.ElementFactory.make('vaapisink', 'vaapi_sink')
                        if elements['videosink']:
                            elements['videosink'].set_property('fullscreen-toggle-mode', 0)
                            elements['videosink'].set_property('show-preroll-frame', False)

                    # Fallback to autovideosink if VA-API not available or failed
                    if 'videosink' not in elements or not elements['videosink']:
                        elements['videosink'] = Gst.ElementFactory.make('autovideosink', 'video_sink')

                    # Add all elements to the pipeline
                    for name, element in elements.items():
                        if element and not element.get_parent():
                            if not pipeline.add(element):
                                self._log(f"Failed to add {name} to pipeline", level='error')
                                return False
                except Exception as e:
                    self._log(f"Error creating GStreamer elements: {str(e)}", level='error')
                    return False

                # Set up video bin
                video_bin = Gst.Bin.new("video_bin")
                video_elements = [
                    elements['queue_video'],
                    elements.get('h264parse') if 'h264parse' in elements and elements['h264parse'] else None,
                    elements.get('h265parse') if 'h265parse' in elements and elements['h265parse'] else None,
                    elements.get('h264dec') if 'h264dec' in elements and elements['h264dec'] else None,
                    elements.get('h265dec') if 'h265dec' in elements and elements['h265dec'] else None,
                    elements['videoconvert'],
                    elements['videosink']
                ]

                # Filter out None elements and add to bin
                video_elements = [e for e in video_elements if e is not None]
                for element in video_elements:
                    if not video_bin.add(element):
                        self._log(f"Failed to add {element.get_name()} to video bin", level='error')
                        return False

                # Link video elements
                if not Gst.Element.link_many(*video_elements):
                    self._log("Failed to link video elements", level='error')
                    return False

                # Set up audio bin
                audio_bin = Gst.Bin.new("audio_bin")
                audio_elements = [
                    elements['queue_audio'],
                    elements['audioconvert'],
                    elements['audioresample'],
                    elements['autoaudiosink']
                ]

                # Add audio elements to the bin
                for element in audio_elements:
                    if element and not audio_bin.add(element):
                        self._log(f"Failed to add {element.get_name()} to audio bin", level='error')
                        return False

                # Link audio elements
                if not Gst.Element.link_many(*audio_elements):
                    self._log("Failed to link audio elements", level='error')
                    return False

                # Add bins to the pipeline
                if not pipeline.add(video_bin):
                    self._log("Failed to add video bin to pipeline", level='error')
                    return False

                if not pipeline.add(audio_bin):
                    self._log("Failed to add audio bin to pipeline", level='error')
                    return False

                # Create ghost pads for dynamic linking
                video_sink_pad = elements['queue_video'].get_static_pad('sink')
                audio_sink_pad = elements['queue_audio'].get_static_pad('sink')

                if not video_sink_pad or not audio_sink_pad:
                    self._log("Failed to get sink pads for dynamic linking", level='error')
                    return False

                ghost_video = Gst.GhostPad.new('sink', video_sink_pad)
                ghost_audio = Gst.GhostPad.new('sink', audio_sink_pad)

                if not video_bin.add_pad(ghost_video):
                    self._log("Failed to add video ghost pad", level='error')
                    return False

                if not audio_bin.add_pad(ghost_audio):
                    self._log("Failed to add audio ghost pad", level='error')
                    return False

                # Connect pad-added signal for dynamic linking
                elements['source'].connect('pad-added', self._on_pad_added, {
                    'video': ghost_video,
                    'audio': ghost_audio
                })

                # Store element references
                self._source = elements['source']
                self._video_convert = elements['videoconvert']
                self._videosink = elements['videosink']
                self._audiosink = elements['autoaudiosink']

                self._log("Successfully set up GStreamer pipeline")
                if GST_VAAPI_AVAILABLE:
                    self._log("Using hardware-accelerated video decoding")
                else:
                    self._log("Using software video decoding")

                return True

        except Exception as e:
            self._log(f"Error in _setup_gst_elements: {str(e)}", level='error')
            import traceback
            self._log(f"Traceback: {traceback.format_exc()}", level='debug')
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
            print(f"Error in _on_pad_added: {e}")
            return

    def _on_pad_link_probe(self, pad, info, user_data):
        """Probe to ensure proper event ordering before linking pads.
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
            if info.type & Gst.PadProbeType.BLOCK:
                if hasattr(self, '_pads_with_stream_start') and pad in self._pads_with_stream_start:
                    target = user_data.get('target')
                    media_kind = user_data.get('media_kind', 'unknown')
                    caps = user_data.get('caps')
                    if not target or pad.is_linked():
                        return Gst.PadProbeReturn.REMOVE
                    if hasattr(self, '_pending_caps_events') and pad in self._pending_caps_events:
                        pad.push_event(self._pending_caps_events.pop(pad))
                    link_ret = pad.link(target)
                    if link_ret == Gst.PadLinkReturn.OK:
                        return Gst.PadProbeReturn.REMOVE
                    elif link_ret == Gst.PadLinkReturn.WRONG_HIERARCHY and caps:
                        caps_filter = Gst.ElementFactory.make('capsfilter', f'filter_{media_kind}')
                        if not caps_filter:
                            print(f"Failed to create caps filter for {media_kind}")
                            return Gst.PadProbeReturn.REMOVE
                        caps_filter.set_property('caps', caps)
                        if not self._pipeline.add(caps_filter):
                            print("Failed to add caps filter to pipeline")
                            return Gst.PadProbeReturn.REMOVE
                        if pad.link(caps_filter.get_static_pad('sink')) != Gst.PadLinkReturn.OK:
                            print("Failed to link pad to caps filter")
                            return Gst.PadProbeReturn.REMOVE
                        if not Gst.Element.link(caps_filter, target.get_parent()):
                            print("Failed to link caps filter to target")
                            return Gst.PadProbeReturn.REMOVE
                        caps_filter.sync_state_with_parent()
        except Exception as e:
            print(f"Error in _on_pad_link_probe: {e}", file=sys.stderr)
            return Gst.PadProbeReturn.REMOVE

    def _setup_vaapi_environment(self):
        """Set up VA-API environment for hardware acceleration."""
        if not self.debug_mode:
            return

        print("DEBUG: Setting up VA-API environment in browser")

        # VA-API driver configuration
        vaapi_env = {
            'LIBVA_DRIVER_NAME': 'iHD',
            'LIBVA_DRIVERS_PATH': '/usr/lib64/dri',
            'GST_VAAPI_DRM_DEVICE': '/dev/dri/renderD128',
            'GST_VAAPI_ALL_DRIVERS': '1'
        }

        # Set environment variables if not already set
        for key, value in vaapi_env.items():
            if key not in os.environ:
                os.environ[key] = value
                if self.debug_mode:
                    print(f"DEBUG: Set VA-API environment {key}={value}")

        # GStreamer plugin ranking for hardware acceleration
        gst_rank = 'vaapih264dec:256,vaapih265dec:256,avdec_h264:128,avdec_aac_fixed:128'
        if 'GST_PLUGIN_FEATURE_RANK' not in os.environ:
            os.environ['GST_PLUGIN_FEATURE_RANK'] = gst_rank
            if self.debug_mode:
                print(f"DEBUG: Set GST_PLUGIN_FEATURE_RANK={gst_rank}")
    def initialize(self):
        """Initialize the video playback system with hardware acceleration if available."""
        if self._initialized:
            return True

        try:
            # Try different VA-API drivers
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

            # Initialize GStreamer
            Gst.debug_set_active(True)
            Gst.debug_set_default_threshold(Gst.DebugLevel.WARNING)

            # Check for required plugins
            if not self._check_gst_plugins():
                print("Warning: Missing some GStreamer plugins, some features may be limited")

            # Set up GStreamer elements
            if not self._setup_gst_elements():
                print("Warning: Failed to set up some GStreamer elements, using fallbacks")

            # Build the pipeline
            if not self._build_pipeline():
                print("Error: Failed to build GStreamer pipeline")
                return False

            # Print pipeline configuration
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
        self.download_area.set_child(self.box)
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
        """Check if a Tor process is already running using the data directory or standard ports."""
        # First try to check by connecting to the Tor ports
        try:
            import socket
            for port in [9050, 9051]:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        sock.settimeout(1)
                        if sock.connect_ex(('127.0.0.1', port)) == 0:
                            return True
                except (socket.error, OSError):
                    continue
        except Exception as e:
            print(f"Error checking Tor ports: {e}")

        # If port check fails, try process inspection as fallback
        try:
            import psutil
            for proc in psutil.process_iter(['name', 'cmdline', 'pid']):
                try:
                    if not proc.info['name'] or 'tor' not in proc.info['name'].lower():
                        continue

                    # Check command line arguments
                    cmdline = proc.info['cmdline'] or []
                    if any(self.tor_data_dir in arg for arg in cmdline):
                        return True
                    if any(str(port) in arg for port in [9050, 9051] for arg in cmdline):
                        return True

                except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                    continue

        except ImportError:
            print("psutil not available, using basic port check only")
        except Exception as e:
            print(f"Error checking Tor processes: {e}")

        # If we get here, Tor doesn't seem to be running
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

class SystemWakeLock:
    def __init__(self, app_id="shadow-browser", reason="Browser is running"):
        self._inhibit_cookie = None
        self._dbus_inhibit = None
        self._inhibit_method = None
        self._app_id = app_id
        self._reason = reason
        self._setup_inhibit()

    def _setup_inhibit(self):
        """Set up the appropriate inhibition method for Linux."""
        if platform.system() != 'Linux' or not dbus:
            return
        try:
            DBusGMainLoop(set_as_default=True)
            bus = dbus.SessionBus()
            try:
                portal = bus.get_object(
                    "org.freedesktop.portal.Desktop",
                    "/org/freedesktop/portal/desktop"
                )
                self._dbus_inhibit = dbus.Interface(
                    portal, dbus_interface="org.freedesktop.portal.Inhibit"
                )
                self._inhibit_method = "portal"
                return
            except dbus.exceptions.DBusException as e:
                if "org.freedesktop.DBus.Error.ServiceUnknown" not in str(e):
                    print(f"Warning: DBus portal error: {e}")
            try:
                screensaver = bus.get_object(
                    "org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver"
                )
                self._dbus_inhibit = dbus.Interface(
                    screensaver, dbus_interface="org.freedesktop.ScreenSaver"
                )
                self._inhibit_method = "screensaver"
                return
            except dbus.exceptions.DBusException as e:
                print(f"Warning: DBus screensaver error: {e}")
        except Exception as e:
            print(f"Warning: Failed to set up DBus: {e}")

    def inhibit(self):
        """Prevent system sleep/screensaver on Linux."""
        if platform.system() != 'Linux':
            print("Warning: Sleep inhibition is only supported on Linux")
            return False
        if not self._dbus_inhibit or not self._inhibit_method:
            print("Warning: No inhibition method available")
            return False
        try:
            if self._inhibit_method == "portal":
                flags = 0x1
                self._inhibit_cookie = self._dbus_inhibit.Inhibit(
                    self._app_id,
                    flags,
                    dbus.Dictionary({
                        'reason': dbus.String(self._reason),
                        'application-name': dbus.String(self._app_id)
                    }, signature='sv')
                )
                return True
            elif self._inhibit_method == "screensaver":
                self._inhibit_cookie = self._dbus_inhibit.Inhibit(self._app_id, self._reason)
                return True
        except Exception as e:
            print(f"Warning: Could not inhibit sleep via DBus: {e}")
            return False

    def uninhibit(self):
        """Allow system sleep/screensaver again."""
        if platform.system() != 'Linux' or not self._dbus_inhibit or self._inhibit_cookie is None:
            return False
        try:
            if self._inhibit_method == "portal":
                request_handle = str(self._inhibit_cookie)
                try:
                    bus = dbus.SessionBus()
                    request = bus.get_object("org.freedesktop.portal.Desktop", request_handle)
                    request.Close(dbus_interface="org.freedesktop.portal.Request")
                    self._inhibit_cookie = None
                    return True
                except Exception as e:
                    print(f"Warning: Failed to close portal request: {e}")
                    return False
            elif self._inhibit_method == "screensaver":
                self._dbus_inhibit.UnInhibit(self._inhibit_cookie)
                return True
        except Exception as e:
            print(f"Warning: Could not release DBus inhibition: {e}")
            return False
        finally:
            self._inhibit_cookie = None

class ShadowBrowser(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.shadowyfigure.shadowbrowser")
        self.debug_mode = False
        self.wake_lock = SystemWakeLock()
        self.wake_lock_active = False
        self.webview = WebKit.WebView()
        self.content_manager = WebKit.UserContentManager()
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
        self.setup_security_policies()
        self.download_manager.on_download_start_callback = self.on_download_start
        self.download_manager.on_download_finish_callback = self.on_download_finish
        try:
            self.adblocker.inject_to_webview(self.content_manager)
            self.inject_nonce_respecting_script()
            self.inject_remove_malicious_links()
            self.inject_adware_cleaner()
            # Initialize the main webview's biometrics protection
            if hasattr(self, 'webview'):
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
        except Exception:
            pass

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
            WebKit.WebView: A configured WebView instance or None if creation fails
        """
        try:
            content_manager = WebKit.UserContentManager()
            webview = WebKit.WebView(user_content_manager=content_manager)
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
                webview = WebKit.WebView()
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
        if load_event == WebKit.LoadEvent.STARTED:
            try:
                uri = webview.get_uri()
                if not uri:
                    return False
                if not (uri.startswith(('http:', 'https:', 'blob:'))):
                    return False
                if any(blocked_url in uri.lower() for blocked_url in self.blocked_urls):
                    return True

                settings = webview.get_settings()

                # Set user agent to a modern browser to ensure compatibility with media sites
                settings.set_user_agent(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )

                # Core settings for media playback
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
                    'enable-webgl': True,
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
                    'enable-accelerated-2d-canvas': True,
                    'enable-accelerated-video-decode': True,
                    'enable-webgl2': True,
                    'enable-gpu': True,
                    'enable-gpu-compositing': True
                }
                for prop, value in core_settings.items():
                    try:
                        settings.set_property(prop, value)
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Warning: Could not set {prop}: {str(e)}")
                try:
                    if hasattr(settings, 'set_hardware_acceleration_policy'):
                        try:
                            if hasattr(WebKit, 'HardwareAccelerationPolicy'):
                                settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ALWAYS)
                            else:
                                settings.set_hardware_acceleration_policy(1)
                        except (AttributeError, TypeError):
                            try:
                                settings.set_hardware_acceleration_policy(1)
                            except (AttributeError, TypeError, GLib.Error) as accel_error:
                                if self.debug_mode:
                                    print(f"Warning: Could not set hardware acceleration policy (fallback): {str(accel_error)}")
                    for setting in [
                        'hardware-acceleration-policy',
                        'enable-hardware-acceleration',
                        'enable-accelerated-compositing',
                        'enable-accelerated-2d-canvas',
                        'enable-webgl'
                    ]:
                        try:
                            settings.set_property(setting, True)
                        except (AttributeError, TypeError, GLib.Error) as prop_error:
                            if self.debug_mode:
                                print(f"Warning: Could not set {setting}: {str(prop_error)}")
                            continue
                except Exception as e:
                    if self.debug_mode:
                        print(f"Warning: Could not set hardware acceleration policy: {str(e)}")
                if hasattr(settings, 'set_auto_play_policy'):
                    try:
                        settings.set_auto_play_policy(WebKit.AutoPlayPolicy.ALLOW)
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Warning: Could not set autoplay policy: {str(e)}")
                if hasattr(settings, 'set_webrtc_ip_handling_policy'):
                    try:
                        settings.set_webrtc_ip_handling_policy(
                            WebKit.WebRTCIPHandlingPolicy.DEFAULT_PUBLIC_AND_PRIVATE_INTERFACES)
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Warning: Could not set WebRTC policy: {str(e)}")
                webview.set_settings(settings)
                if hasattr(settings, 'set_enable_media_cache'):
                    settings.set_enable_media_cache(True)
                if hasattr(settings, 'set_enable_mediasource'):
                    settings.set_enable_mediasource(True)
                settings.set_enable_smooth_scrolling(True)
                if hasattr(WebKit, 'HardwareAccelerationPolicy') and hasattr(WebKit.HardwareAccelerationPolicy, 'ALWAYS'):
                    settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ALWAYS)
                elif hasattr(WebKit, 'HardwareAccelerationPolicy') and hasattr(WebKit.HardwareAccelerationPolicy, 'ON'):
                    settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ON)
                else:
                    try:
                        settings.set_hardware_acceleration_policy(1)
                    except (AttributeError, TypeError, GLib.Error):
                        pass
                if hasattr(settings, 'set_enable_hardware_accelerated_video_decode'):
                    settings.set_enable_hardware_accelerated_video_decode(True)
                settings.set_enable_webaudio(True)
                settings.set_enable_webgl(True)
                if hasattr(settings, 'set_enable_webgl2'):
                    settings.set_enable_webgl2(True)
                if hasattr(settings, 'set_enable_webaudio'):
                    settings.set_enable_webaudio(True)
                return False
            except Exception as e:
                print(f"Error in inject_security_headers: {str(e)}")
                return False

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
                if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.windowOpenHandler) {
                    window.webkit.messageHandlers.windowOpenHandler.postMessage(urlToSend);
                    return null;
                }
                return originalOpen.apply(this, arguments);
            };
        })();
        '''
        print('[ShadowBrowser] Injecting window.open handler JS')
        content_manager.add_script(
            WebKit.UserScript.new(
                js_code,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
            )
        )

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
        script = WebKit.UserScript.new(
            js_code,
            WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.END,
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
            user_content_manager: The WebKit.UserContentManager that received the message
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

    def setup_webview_settings(self, webview):
        """Configure WebView settings for security, compatibility, and performance."""
        settings = webview.get_settings()

        # Enable essential media settings
        media_settings = {
            'enable_media': True,
            'enable_media_capabilities': True,
            'enable_media_stream': True,
            'enable_mediasource': True,
            'enable_encrypted_media': True,
            'enable_webrtc': True,
            'enable_webaudio': True,
            'enable_webgl': True,
            'enable_javascript': True,
            'enable_page_cache': True,
            'enable_smooth_scrolling': True,
            'enable_fullscreen': True,
            'media_playback_requires_user_gesture': False,
            'media_playback_allows_inline': True,
            'auto_load_images': True
        }

        # Apply all media settings
        for setting, value in media_settings.items():
            try:
                if hasattr(settings, f'set_{setting}'):
                    getattr(settings, f'set_{setting}')(value)
            except Exception as e:
                if self.debug_mode:
                    print(f"Warning: Could not set {setting}: {e}")

        # Enable hardware acceleration
        if hasattr(settings, 'set_hardware_acceleration_policy'):
            try:
                if hasattr(WebKit.HardwareAccelerationPolicy, 'ALWAYS'):
                    settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ALWAYS)
                elif hasattr(WebKit.HardwareAccelerationPolicy, 'ON'):
                    settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ON)
            except Exception as e:
                if self.debug_mode:
                    print(f"Warning: Could not set hardware acceleration: {e}")

        # Enable autoplay
        if hasattr(settings, 'set_auto_play_policy'):
            settings.set_auto_play_policy(WebKit.AutoPlayPolicy.ALLOW)

        # Set up GStreamer plugins path
        gst_plugin_paths = [
            '/usr/lib64/gstreamer-1.0',
            '/usr/lib/gstreamer-1.0',
            '/usr/local/lib/gstreamer-1.0',
            '/usr/lib/x86_64-linux-gnu/gstreamer-1.0',
            '/usr/lib/gstreamer1.0',
            '/usr/local/lib/gstreamer1.0',
            '/usr/lib/x86_64-linux-gnu/gstreamer1.0'
        ]

        gst_plugin_path = ':'.join(p for p in gst_plugin_paths if os.path.exists(p))
        os.environ['GST_PLUGIN_PATH'] = gst_plugin_path
        os.environ['GST_PLUGIN_SYSTEM_PATH'] = gst_plugin_path
        os.environ['GST_PLUGIN_SYSTEM_PATH_1_0'] = gst_plugin_path

        # Enable debug logging for GStreamer
        os.environ['GST_DEBUG'] = '3,webkit*:5,enc*:5,EME:5,adaptive*:5,dash*:5,hls*:5'

        # Set up DRM backends (Widevine, PlayReady, etc.)
        os.environ['WEBKIT_DISABLE_COMPOSITING_MODE'] = '1'
        os.environ['WEBKIT_ENABLE_ENCRYPTED_MEDIA'] = '1'
        os.environ['WEBKIT_ENABLE_ENCRYPTED_MEDIA_V2'] = '1'

        # Enable experimental features for better EME support
        if hasattr(settings, 'set_enable_experimental_web_platform_features'):
            settings.set_enable_experimental_web_platform_features(True)

        # Set user agent to a modern browser to ensure EME support
        if hasattr(settings, 'set_user_agent'):
            settings.set_user_agent(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

        # Apply settings to webview
        webview.set_settings(settings)

        # Add console filtering to suppress source map errors
        source_map_script = """
        // Disable source maps
        window.sourceMapsEnabled = false;

        // Store original console methods
        const originalConsole = {
            log: console.log,
            warn: console.warn,
            error: console.error
        };

        // Override console methods to filter out source map errors
        ['log', 'warn', 'error'].forEach(method => {
            console[method] = function() {
                // Skip messages about source maps
                for (let arg of arguments) {
                    if (typeof arg === 'string' && (arg.includes('source map') ||
                                                 arg.includes('.map') ||
                                                 arg.includes('SourceMap'))) {
                        return;
                    }
                }
                // Call original console method with all arguments
                originalConsole[method].apply(console, arguments);
            };
        });
        """

        # Create and add the script to suppress source map errors
        source_map_script = WebKit.UserScript.new(
            source_map_script,
            WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.START
        )
        webview.get_user_content_manager().add_script(source_map_script)

        # Enable developer extras for debugging
        if self.debug_mode:
            settings.set_enable_developer_extras(True)
            settings.set_enable_write_console_messages_to_stdout(True)
            settings.set_enable_site_specific_quirks(True)

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
                const observer = new MutationObserver(function(mutations) {
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
        """Create a toolbar button with an icon.
        Args:
            icon_name: Name of the icon to display
            callback: Function to call when button is clicked
            tooltip_text: Optional tooltip text
        """
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
        """Increase the zoom level of the current webview."""
        webview = self.get_current_webview()
        if webview:
            current_zoom = webview.get_zoom_level()
            webview.set_zoom_level(min(current_zoom + 0.1, 5.0))  # Cap zoom at 500%

    def zoom_out(self):
        """Decrease the zoom level of the current webview."""
        webview = self.get_current_webview()
        if webview:
            current_zoom = webview.get_zoom_level()
            webview.set_zoom_level(max(current_zoom - 0.1, 0.25))  # Minimum 25% zoom

    def zoom_reset(self):
        """Reset the zoom level to 100%."""
        webview = self.get_current_webview()
        if webview:
            webview.set_zoom_level(1.0)  # 100%

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
        if hasattr(self, "toolbar") and self.toolbar is not None:
            if self.toolbar.get_parent() is not None:
                return self.toolbar
            else:
                self.toolbar = None
        self.toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.toolbar.set_margin_start(6)
        self.toolbar.set_margin_end(6)
        self.toolbar.set_margin_top(6)
        self.toolbar.set_margin_bottom(6)

        def icon_button(icon_name, callback):
            image = Gtk.Image.new_from_icon_name(icon_name)
            button = Gtk.Button()
            button.set_child(image)
            button.connect("clicked", callback)
            return button
        self.toolbar.append(icon_button("go-previous-symbolic", self.on_back_clicked))
        self.toolbar.append(icon_button("go-next-symbolic", self.on_forward_clicked))
        self.toolbar.append(icon_button("view-refresh-symbolic", self.on_refresh_clicked))
        self.toolbar.append(icon_button("go-home-symbolic", lambda b: self.load_url(self.home_url)))
        self.url_entry = Gtk.Entry(placeholder_text="Enter URL")
        self.url_entry.connect("activate", self.on_go_clicked)
        self.toolbar.append(self.url_entry)
        self.toolbar.append(icon_button("go-jump-symbolic", self.on_go_clicked))
        self.toolbar.append(icon_button("bookmark-new-symbolic", self.on_add_bookmark_clicked))
        self.toolbar.append(icon_button("tab-new-symbolic", self.on_new_tab_clicked))
        self.toolbar.append(icon_button("zoom-out-symbolic", self.on_zoom_out_clicked))
        self.toolbar.append(icon_button("zoom-fit-best-symbolic", self.on_zoom_reset_clicked))
        self.toolbar.append(icon_button("zoom-in-symbolic", self.on_zoom_in_clicked))
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
        if hasattr(self, 'download_spinner') and self.download_spinner:
            self.download_spinner.set_halign(Gtk.Align.END)
            self.download_spinner.set_valign(Gtk.Align.CENTER)
            self.download_spinner.set_margin_start(10)
            self.download_spinner.set_margin_end(10)
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
                        settings.set_property('enable-developer-extras', True)
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
        """Rebuild the bookmarks menu with favicons (GTK4, Pixbuf-free)."""
        if not menu_container:
            if getattr(self, "debug_mode", False):
                print("[Bookmarks] menu_container is None")
            return

        # Clear existing children
        while (child := menu_container.get_first_child()):
            menu_container.remove(child)

        # Create scrollable container if this is the main bookmarks menu
        if menu_container == getattr(self, "bookmark_menu", None):
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scrolled.set_property("height-request", 300)
            scrolled.set_property("width-request", 300)

            bookmarks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            bookmarks_box.set_margin_top(6)
            bookmarks_box.set_margin_bottom(6)
            bookmarks_box.set_margin_start(6)
            bookmarks_box.set_margin_end(6)

            scrolled.set_child(bookmarks_box)
            menu_container.append(scrolled)
            menu_container = bookmarks_box

        # Handle empty bookmarks
        if not getattr(self, "bookmarks", []):
            empty_label = Gtk.Label(label="No bookmarks yet")
            empty_label.set_margin_top(12)
            empty_label.set_margin_bottom(12)
            menu_container.append(empty_label)
            menu_container.show()
            return

        # Build bookmark list
        for bookmark in self.bookmarks:
            if isinstance(bookmark, str):
                bookmark = {"url": bookmark, "title": bookmark, "favicon": None}

            url = bookmark.get("url")
            if not url:
                continue

            title = bookmark.get("title") or url
            display_text = (title[:30] + "...") if len(title) > 30 else title

            # Container for this bookmark row
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row_box.set_hexpand(True)
            row_box.set_halign(Gtk.Align.FILL)
            row_box.set_margin_start(6)
            row_box.set_margin_end(6)

            # Main button (favicon + title)
            bookmark_btn = Gtk.Button()
            bookmark_btn.set_hexpand(True)
            bookmark_btn.set_halign(Gtk.Align.FILL)
            bookmark_btn.set_has_frame(False)
            bookmark_btn.set_can_focus(False)

            content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            content_box.set_hexpand(True)

            # Load favicon (Pixbuf-free)
            favicon = bookmark.get("favicon")
            favicon_img = Gtk.Image.new_from_icon_name("bookmark-new-symbolic")

            if favicon:
                try:
                    import base64
                    from gi.repository import Gdk, GLib

                    # Handle either raw base64 or data URI
                    encoded = favicon.split(",", 1)[1] if favicon.startswith("data:image/") else favicon
                    padding = len(encoded) % 4
                    if padding:
                        encoded += "=" * (4 - padding)
                    image_data = base64.b64decode(encoded)

                    gbytes = GLib.Bytes.new(image_data)
                    texture = Gdk.Texture.new_from_bytes(gbytes)

                    # Scale small if needed
                    w, h = texture.get_width(), texture.get_height()
                    if w > 16 or h > 16:
                        scale = min(16 / w, 16 / h)
                        new_w, new_h = int(w * scale), int(h * scale)
                        if hasattr(texture, "scale"):
                            texture = texture.scale(new_w, new_h)

                    picture = Gtk.Picture.new_for_paintable(texture)
                    picture.set_size_request(16, 16)
                    favicon_img = picture
                except Exception as e:
                    if getattr(self, "debug_mode", False):
                        print(f"[Bookmarks] Favicon decode failed: {e}")

            favicon_img.set_margin_end(6)
            content_box.append(favicon_img)

            label = Gtk.Label(label=display_text)
            label.set_halign(Gtk.Align.START)
            label.set_ellipsize(3)  # Pango.EllipsizeMode.END
            content_box.append(label)

            bookmark_btn.set_child(content_box)
            bookmark_btn.set_tooltip_text(url)
            bookmark_btn.connect("clicked", lambda _, u=url: self.load_url(u))

            # Delete button
            delete_btn = Gtk.Button()
            delete_btn.set_icon_name("edit-delete-symbolic")
            delete_btn.add_css_class("flat")
            delete_btn.set_tooltip_text("Delete bookmark")
            delete_btn.connect("clicked", self._on_delete_bookmark_clicked, url)

            # Assemble row
            row_box.append(bookmark_btn)
            row_box.append(delete_btn)
            menu_container.append(row_box)

        # Add clear all section
        if getattr(self, "bookmarks", []):
            menu_container.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            btn_box.set_halign(Gtk.Align.CENTER)

            clear_btn = Gtk.Button(label="Clear All Bookmarks")
            clear_btn.set_halign(Gtk.Align.CENTER)
            clear_btn.connect("clicked", self._clear_all_bookmarks)
            btn_box.append(clear_btn)
            menu_container.append(btn_box)

    def do_startup(self):
        Gtk.Application.do_startup(self)

    def do_activate(self):
        """Create and show the main window."""
        try:
            if not self.wake_lock_active:
                self.wake_lock_active = self.wake_lock.inhibit()
            if hasattr(self, "window") and self.window:
                try:
                    self.window.present()
                    return
                except Exception:
                    self.window = None
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("Shadow Browser")
            self.window.set_default_size(1280, 800)
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            menubar = self.create_menubar()
            self.safe_append(vbox, menubar)
            toolbar = self.create_toolbar()
            self.safe_append(vbox, toolbar)
            self.safe_append(vbox, self.notebook)
            self.download_manager.parent_window = self.window
            self.download_manager.show()
            self.safe_append(vbox, self.download_manager.box)
            if not self.window.get_child():
                self.window.set_child(vbox)
            if not hasattr(self, '_window_signals_connected'):
                self.window.connect("close-request", self.on_window_destroy)
                self._window_signals_connected = True
            if len(self.tabs) == 0:
                self.add_new_tab(self.home_url)
            self.window.present()
        except Exception:
            pass

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
        if not hasattr(self, 'bookmarks') or not self.bookmarks:
            return

        # Find and remove the bookmark by URL
        for i, bookmark in enumerate(self.bookmarks[:]):
            if (isinstance(bookmark, str) and bookmark == url) or \
               (isinstance(bookmark, dict) and bookmark.get('url') == url):
                self.bookmarks.pop(i)
                break

        # Save changes to disk
        self.save_json(BOOKMARKS_FILE, self.bookmarks)

        # Update the UI
        if hasattr(self, 'bookmark_menu') and self.bookmark_menu:
            self.update_bookmarks_menu(self.bookmark_menu)

        # Close the popover
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
            self.bookmark_popover.set_size_request(300, -1)  # Fixed width, auto height
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
        self.adblock_toggle.set_active(getattr(self.adblocker, "enabled", True))
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
        save_button.connect("clicked", self.on_settings_save)
        cancel_button.connect("clicked", lambda btn: self.settings_dialog.close())
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
        try:
            self.adblocker.enabled = self.adblock_toggle.get_active()
            self.incognito_mode = self.incognito_toggle.get_active()
            self.anti_fingerprinting_enabled = self.anti_fp_toggle.get_active()
            self.search_engine = self.search_engine_entry.get_text().strip()
            self.home_url = self.home_page_entry.get_text().strip()
            with self.tabs_lock:
                for tab in self.tabs:
                    if hasattr(tab, 'webview') and tab.webview:
                        GLib.idle_add(tab.webview.reload)
        except Exception:
            pass
        finally:
            pass

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
                tor_port = getattr(self.tor_manager, 'tor_port', 9050)
                tor_proxy = f"socks5h://127.0.0.1:{tor_port}"
                os.environ['http_proxy'] = tor_proxy
                os.environ['https_proxy'] = tor_proxy
                os.environ['all_proxy'] = tor_proxy
                self.tor_enabled = True
                self.tor_status = "running"
                return True
            else:
                self.tor_enabled = False
                self.tor_status = "failed"
                return False
        else:
            self.tor_enabled = False
            self.tor_status = "disabled"
            if hasattr(self, 'tor_manager') and self.tor_manager:
                self.tor_manager.stop()
            os.environ.pop('http_proxy', None)
            os.environ.pop('https_proxy', None)
            os.environ.pop('all_proxy', None)
            self.home_url = "https://duckduckgo.com/"
            return True

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

    def update_webview_tor_proxy(self, webview):
        """
        Update the Tor proxy configuration for an existing webview.
        Args:
            webview: The WebKit.WebView to update
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
            web_context: The WebKit.WebContext to clear proxy from
        """

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
        try:
            context = WebKit.WebContext.get_default()
            cookie_manager = context.get_cookie_manager()
            if cookie_manager:
                cookie_manager.delete_all_cookies()
        except AttributeError:
            try:
                cookie_manager = WebKit.CookieManager.get_default()
                if cookie_manager:
                    cookie_manager.delete_all_cookies()
            except AttributeError:
                pass
        except Exception:
            pass

    def clear_cache(self):
        try:
            context = WebKit.WebContext.get_default()
            if context:
                if hasattr(context, 'clear_cache'):
                    context.clear_cache()
                elif hasattr(context, 'clear_cache_storage'):
                    context.clear_cache_storage()
        except Exception:
            pass

    def clear_passwords(self):
        try:
            context = WebKit.WebContext.get_default()
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
        if GST_AVAILABLE:
            Gst.debug_remove_log_function(None)
            Gst.debug_add_log_function(self._gst_log_handler, None)

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
        try:
            webview = self.create_secure_webview()
            if webview is None:
                return
            webview.load_uri(url)
            scrolled_window = Gtk.ScrolledWindow()
            scrolled_window.set_vexpand(True)
            scrolled_window.set_child(webview)
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
                pass
                return
            index = self.notebook.append_page(scrolled_window, box)
            self.notebook.set_current_page(index)
            self.tabs.append(tab)
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
                    page = self.notebook.get_nth_page(notebook_page_num)
                    if page:
                        if isinstance(page, Gtk.ScrolledWindow):
                            child = page.get_child()
                            if isinstance(child, Gtk.Viewport):
                                webview = child.get_child()
                            else:
                                webview = child
                        try:
                            if webview:
                                try:
                                    if hasattr(webview, 'disconnect_by_func'):
                                        webview.disconnect_by_func(self.on_load_changed)
                                except Exception:
                                    pass
                                try:
                                    if hasattr(webview, 'disconnect_by_func'):
                                        webview.disconnect_by_func(self.on_title_changed)
                                except Exception:
                                    pass
                                try:
                                    if hasattr(webview, 'disconnect_by_func'):
                                        webview.disconnect_by_func(self.on_decide_policy)
                                except Exception:
                                    pass
                                try:
                                    if hasattr(webview, 'disconnect_by_func'):
                                        webview.disconnect_by_func(self.on_webview_create)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        if notebook_page_num < self.notebook.get_n_pages():
                            self.notebook.remove_page(notebook_page_num)
                        if webview and hasattr(webview, 'get_parent'):
                            try:
                                parent = webview.get_parent()
                                if parent and webview in parent:
                                    parent.remove(webview)
                            except Exception:
                                pass
                        if page and hasattr(page, 'get_parent') and page.get_parent() == self.notebook:
                            try:
                                self.notebook.remove(page)
                            except Exception:
                                pass
                                pass
                        try:
                            parent = page.get_parent()
                            if parent and hasattr(parent, "remove") and page.get_parent() == parent:
                                parent.remove(page)
                        except Exception:
                            pass
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
        from gi.repository import WebKit, GLib
        try:
            if not hasattr(self, 'download_spinner') or not self.download_spinner:
                return
            if load_event == WebKit.LoadEvent.COMMITTED:
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
            elif load_event == WebKit.LoadEvent.FINISHED:
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

                # Get the favicon URL from the webview
                favicon_uri = webview.get_favicon()
                if not favicon_uri and url:
                    # Fallback to favicon.ico in the root of the domain
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
            # --- Convert favicon to PNG bytes ---
            if hasattr(favicon, 'save_to_png_bytes'):
                # GTK 4: Gdk.Texture (new API)
                try:
                    png_data = favicon.save_to_png_bytes()
                    if png_data:
                        favicon_data = base64.b64encode(png_data.get_data()).decode('utf-8')
                except Exception as e:
                    if self.debug_mode:
                        print(f"[DEBUG] Error saving favicon to PNG: {e}")

            elif hasattr(favicon, 'save_to_stream'):
                # Gdk.Paintable fallback
                bytes_stream = GLib.Bytes.new()
                success = favicon.save_to_stream(bytes_stream, 'png')
                if success:
                    favicon_data = base64.b64encode(bytes_stream.get_data()).decode('utf-8')

            elif isinstance(favicon, GdkPixbuf.Pixbuf):
                # Fallback for legacy or WebKit favicon fetch
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

        # --- Update existing bookmark entry ---
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

            # --- Save bookmarks safely ---
            try:
                self.save_json(BOOKMARKS_FILE, self.bookmarks)
            except Exception as e:
                print(f"[ERROR] Could not save bookmarks: {e}")

            # --- Refresh bookmarks menu if active ---
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
            new_webview = WebKit.WebView(
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
        except Exception:
            pass
            decision.ignore()
            return True

    def on_decide_policy(self, webview, decision, decision_type):
        """Handle navigation and new window actions, manage downloads, enforce policies, and apply adblock rules."""
        try:
            from gi.repository import WebKit
            if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                return self._handle_navigation_action(
                    webview, decision, decision.get_navigation_action()
                )
            elif decision_type == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
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
            toolbar.append(self.download_spinner)
            self.download_spinner.set_halign(Gtk.Align.END)
            self.download_spinner.set_valign(Gtk.Align.END)
            self.download_spinner.set_margin_start(10)
            self.download_spinner.set_margin_end(10)
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
            # Get all children of the window
            children = window.get_children() if hasattr(window, 'get_children') else []

            for child in children:
                try:
                    # Only try to remove if the child has a parent and the parent is not None
                    if hasattr(child, 'get_parent'):
                        parent = child.get_parent()
                        if parent is not None:
                            if hasattr(parent, 'remove'):
                                parent.remove(child)
                            elif hasattr(parent, 'destroy'):
                                parent.destroy()
                except Exception as e:
                    print(f"Error cleaning up child {child}: {e}", file=sys.stderr)

            # Make sure to destroy the window itself
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


            webview.connect("load-changed", self.on_load_changed)
            webview.connect("notify::title", self.on_title_changed)
            webview.connect("decide-policy", self.on_decide_policy)
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
            close_button = Gtk.Button.new_from_icon_name("window-close")
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
            window.set_child(vbox)
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
            style = WebKit.UserStyleSheet.new(
                css,
                WebKit.UserContentInjectedFrames.TOP_FRAME,
                WebKit.UserStyleSheetLevel.USER,
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
            script = WebKit.UserScript.new(
                script_source,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.END,
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
            script = WebKit.UserScript.new(
                script_source,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.END,
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
            script = WebKit.UserScript.new(
                script_source,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
            )
            self.content_manager.add_script(script)
        except Exception:
            pass

    def disable_biometrics_in_webview(self, webview):
        """
        Injects JavaScript into the WebKitGTK WebView to block WebAuthn biometric prompts.
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
            user_script = WebKit.UserScript.new(
                script,
                WebKit.UserContentInjectedFrames.TOP_FRAME,
                WebKit.UserScriptInjectionTime.START,
                [], []
            )
            webview.get_user_content_manager().add_script(user_script)
        except Exception:
            pass

    def block_biometric_apis(self, webview: WebKit.WebView):
        """
        Blocks WebAuthn biometric APIs and navigator.sendBeacon() in WebKitGTK browser.
        This method injects JavaScript to prevent fingerprinting through WebAuthn and
        blocks the sendBeacon API which can be used for tracking. It provides a clean
        rejection message without cluttering the console with warnings.
        Args:
            webview: The WebKit.WebView instance to apply the blocking to
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
            except Exception:
                pass
        except Exception:
            pass

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
        try:
            user_script = WebKit.UserScript.new(
                script,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
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
            user_script = WebKit.UserScript.new(
                script,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
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
                    # First try a HEAD request to check if the resource exists
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

                    # If HEAD was successful, try to get the actual content
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
                        time.sleep(0.5 * (attempt + 1))  # Exponential backoff
                        continue
                except Exception:
                    # Catch any other unexpected errors
                    if attempt < retries:
                        time.sleep(0.5 * (attempt + 1))
                        continue

            return None

        def on_favicon_loaded(texture):
            """Trigger callback on GTK main thread."""
            if callback:
                GLib.idle_add(callback, texture)

        try:
            # Determine candidate URLs if not provided
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

            # Fetch the favicon
            response = load_favicon_with_retry(favicon_uri)
            if not response:
                on_favicon_loaded(None)
                return

            # Read the data
            image_data = response.content
            if not image_data:
                raise ValueError("Empty favicon data")

            try:
                # Create Gdk.Texture from encoded bytes (auto-decoded)
                gbytes = GLib.Bytes.new(image_data)
                texture = Gdk.Texture.new_from_bytes(gbytes)
                on_favicon_loaded(texture)
            except Exception as e:
                print(f"Failed to decode favicon: {e}")
                on_favicon_loaded(None)

        except Exception as e:
            print(f"Unexpected error in favicon loading: {e}")
            on_favicon_loaded(None)

def main():
    """Main entry point for the Shadow Browser."""
    try:
        app = ShadowBrowser()
        return app.run(None)
    except Exception as e:
        print(f"Fatal error: {e}")
        return 1

if __name__ == "__main__":
    import sys
    sys.exit(main())
