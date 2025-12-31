import base64
import datetime
import hashlib
import json
import logging
import os
import platform
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
import urllib.request
from typing import Union

from urllib.parse import urlparse, urlunparse
import gi
import psutil
import requests
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from requests.adapters import HTTPAdapter
from stem.control import Controller
from urllib3.util.retry import Retry

def pixbuf_to_base64(pixbuf):
    """Convert a GdkPixbuf to a base64-encoded string.
    Args:
        pixbuf: A GdkPixbuf object to convert
    Returns:
        str: Base64-encoded string of the pixbuf data, or None if conversion fails
    """
    try:
        if not pixbuf.get_width() or not pixbuf.get_height():
            return None
        pixels = pixbuf.get_pixels()
        return base64.b64encode(pixels).decode('utf-8')
    except Exception as e:
        print(f"Error converting pixbuf to base64: {e}")
        return None
try:
    gi.require_version('Gtk', '4.0')
    gi.require_version('WebKit', '6.0')
    gi.require_version('Gst', '1.0')
    gi.require_version('GstVideo', '1.0')
    gi.require_version('Gio', '2.0')
    from gi.repository import Gtk, Gio, Gdk, WebKit, GLib, Gst, Pango
except (ValueError, ImportError) as e:
    print(f"Failed to import required GTK modules: {e}")
    exit(1)

_GI_WARNING_PATTERNS = [
    "cannot register existing type 'GtkWidget'",
    "cannot add class private field to invalid type",
    "cannot add private field to invalid (non-instantiatable) type",
    "g_type_add_interface_static: assertion 'G_TYPE_IS_INSTANTIATABLE (instance_type)' failed",
    "cannot register existing type 'GtkBuildable'",
    "g_type_interface_add_prerequisite: assertion 'G_TYPE_IS_INTERFACE (interface_type)' failed",
    "g_once_init_leave_pointer: assertion 'result != 0' failed",
    "g_param_spec_object: assertion 'g_type_is_a (object_type, G_TYPE_OBJECT)' failed",
    "validate_pspec_to_install: assertion 'G_IS_PARAM_SPEC (pspec)' failed",
    "g_param_spec_ref_sink: assertion 'G_IS_PARAM_SPEC (pspec)' failed",
    "g_param_spec_unref: assertion 'G_IS_PARAM_SPEC (pspec)' failed",
]

class _GIWarningFilter:
    def __init__(self, original):
        self._orig = original
    def write(self, message):
        try:
            if not message:
                return
            lower = message.lower()
            for pat in _GI_WARNING_PATTERNS:
                if pat.lower() in lower:
                    return
            self._orig.write(message)
        except Exception:
            try:
                self._orig.write(message)
            except Exception:
                pass

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
        logging.warning("VA-API not available. Hardware acceleration will be disabled.")   
    os.environ['GST_MSDK_DISABLE'] = '1'
    Gst.init(None)
    from gi.repository import WebKit
except (ValueError, ImportError):
    exit(1)

try:
    Gst.init(None)
    GST_AVAILABLE = True
except Exception:
    GST_AVAILABLE = False

def safe_widget_append(container, widget):
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
SETTINGS_FILE = os.path.expanduser("~/.config/shadowbrowser/settings.json")
HISTORY_LIMIT = 100

try:
    from js_obfuscation_improved import extract_url_from_javascript as js_extract_url
    from js_obfuscation_improved import extract_onclick_url
    print("Imported extract_onclick_url from js_obfuscation_improved")
except ImportError as e:
    print("ImportError for js_obfuscation_improved:", e)
    try:
        from js_obfuscation import extract_url_from_javascript as js_extract_url
        extract_onclick_url = None
        print("Imported from js_obfuscation")
    except ImportError as e2:
        print("ImportError for js_obfuscation:", e2)
        js_extract_url = None
        extract_onclick_url = None
        print("No obfuscation modules available")

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
            'WEBKIT_SETTINGS_ENABLE_JAVASCRIPT': '1',
            'WEBKIT_SETTINGS_ENABLE_DEVELOPER_EXTRAS': '1',
            'WEBKIT_SETTINGS_ENABLE_WRITE_CONSOLE_MESSAGES_TO_STDOUT': '1',
            'WEBKIT_DEBUG': 'all',
            'G_MESSAGES_DEBUG': 'all',
            'WEBKIT_INSPECTOR_SERVER': '127.0.0.1:9222', 
            'GST_PULSE_LATENCY_MSEC': '50',
            'PULSE_PROP_OVERRIDE_DEVICE_NAME': 'auto_null',
            'GSTREAMER_PLAYER_AUDIO_SINK': 'pulsesink',
            'GST_GL_PLATFORM': 'egl',
            'GST_GL_API': 'opengl',
            'GST_GL_WINDOW': 'egl',
            'GST_GL_DISPLAY': 'egl',
            'GST_GL_CONTEXT': 'egl',
            'GST_GL_USE_EGL': '1',
            'GST_GL_USE_GLX': '0',
            'GST_GL_USE_WAYLAND': '0',
            'GST_GL_USE_X11': '0',
            'GST_VIDEO_DISABLE_COLORBALANCE': '1',
            'GST_VIDEO_DISABLE_GAMMA': '1',
            'GST_VIDEO_DISABLE_CONTOUR_CORRECTION': '1',
            'GST_VIDEO_SINK_XID': '0',
            'GST_VIDEO_OVERLAY_COMPOSITION': '1',
            'GST_VIDEO_VSYNC': 'enabled',
            'GST_VIDEO_FORCE_FPS': '0',
            'GST_HTTP_BUFFER_SIZE': '10485760',
            'GST_HTTP_BUFFER_MAX_SIZE': '20971520',
            'GST_HTTP_RETRY_ATTEMPTS': '5',
            'GST_HTTP_RETRY_DELAY': '500000000',
            'GST_HTTP_TIMEOUT': '30000000000',
            'GST_HLS_PLAYLIST_UPDATE_INTERVAL': '10000000',
            'GST_HLS_LIVE_DELAY': '3000000000',
            'GST_HLS_BUFFER_SIZE': '10485760',
            'GST_HLS_MAX_BUFFER_SIZE': '20971520',
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

    def __init__(self):
        """Initialize the VAAPI manager with optimized settings for video playback."""
        if not hasattr(self, '_initialized'):
            self._initialized = False
            self._gst_plugins = {}
            self._gst_elements = {}
            self._pipeline = None
            self._source = None
            self._video_convert = None
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

    def _setup_gst_elements(self, pipeline=None, use_vaapi=True):
        """Set up GStreamer elements with hardware acceleration if available."""
        if not GST_AVAILABLE:
            self._log("GStreamer not available, cannot set up elements", level='error')
            return False
        elements = {}
        try:
            if not self._check_gst_features():
                self._log("Some required GStreamer features are missing", level='warning')
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
            required_plugins = ['playback', 'video', 'audio', 'pulse', 'gl', 'vaapi']
            missing_plugins = [p for p in required_plugins if not Gst.Registry.get().find_plugin(p)]
            
            if missing_plugins:
                self._log(f"Missing required GStreamer plugins: {', '.join(missing_plugins)}", level='error')
                return False
            if pipeline is None:
                self._pipeline = Gst.Pipeline.new('media-pipeline')
                if not self._pipeline:
                    self._log("Failed to create GStreamer pipeline", level='error')
                for element in elements.values():
                    if element and not element.get_parent():
                        if not pipeline.add(element):
                            self._log(f"Failed to add {element.get_name()} to pipeline", level='error')
                            return False
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
                video_elements = [e for e in video_elements if e is not None]
                for element in video_elements:
                    if not video_bin.add(element):
                        self._log(f"Failed to add {element.get_name()} to video bin", level='error')
                        return False
                if not Gst.Element.link_many(*video_elements):
                    self._log("Failed to link video elements", level='error')
                    return False
                audio_bin = Gst.Bin.new("audio_bin")
                audio_elements = [
                    elements['queue_audio'],
                    elements['audioconvert'],
                    elements['audioresample'],
                    elements['autoaudiosink']
                ]
                for element in audio_elements:
                    if element and not audio_bin.add(element):
                        self._log(f"Failed to add {element.get_name()} to audio bin", level='error')
                        return False
                if not Gst.Element.link_many(*audio_elements):
                    self._log("Failed to link audio elements", level='error')
                    return False
                if not pipeline.add(video_bin):
                    self._log("Failed to add video bin to pipeline", level='error')
                    return False                  
                if not pipeline.add(audio_bin):
                    self._log("Failed to add audio bin to pipeline", level='error')
                    return False
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
                elements['source'].connect('pad-added', self._on_pad_added, {
                    'video': ghost_video,
                    'audio': ghost_audio
                })
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
            if not caps or caps.is_empty():
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
            if hasattr(sink.props, 'sync'):
                sink.set_property('sync', False)
            if hasattr(sink.props, 'max-lateness'):
                sink.set_property('max-lateness', 20000000)
            if hasattr(sink.props, 'qos'):
                sink.set_property('qos', True)
            if hasattr(sink.props, 'async'):
                sink.set_property('async', False)
            if sink_name == 'vaapisink':
                sink.set_property('fullscreen-toggle-mode', 0)
                sink.set_property('show-preroll-frame', False)
                sink.set_property('max-buffers', 5)
                sink.set_property('vsync', True)
                sink.set_property('async', False)
                sink.set_property('drop', True)
                if hasattr(sink.props, 'display') and not sink.get_property('display'):
                    try:
                        display = GstVaapi.Display()
                        if display:
                            sink.set_property('display', display)
                            self._log("Successfully set VA-API display")
                    except Exception as e:
                        self._log(f"Error setting VA-API display: {str(e)}", level='warning')
            elif sink_name in ['xvimagesink', 'glimagesink', 'autovideosink']:
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
            required_plugins = ['playback', 'video', 'audio', 'pulse', 'gl']
            if use_vaapi and GST_VAAPI_AVAILABLE:
                required_plugins.append('vaapi')               
            missing_plugins = [p for p in required_plugins if not Gst.Registry.get().find_plugin(p)]            
            if missing_plugins:
                self._log(f"Missing required GStreamer plugins: {', '.join(missing_plugins)}", level='error')
                return False
            if pipeline is None:
                pipeline = Gst.Pipeline.new('media-pipeline')
                if not pipeline:
                    self._log("Failed to create GStreamer pipeline", level='error')
                    return False
                elements = {}              
                try:
                    elements['source'] = Gst.ElementFactory.make('uridecodebin', 'source')
                    elements['queue_video'] = Gst.ElementFactory.make('queue', 'video_queue')
                    elements['queue_audio'] = Gst.ElementFactory.make('queue', 'audio_queue')
                    elements['videoconvert'] = Gst.ElementFactory.make('videoconvert', 'video_convert')
                    elements['audioconvert'] = Gst.ElementFactory.make('audioconvert', 'audio_convert')
                    elements['audioresample'] = Gst.ElementFactory.make('audioresample', 'audio_resample')
                    elements['autoaudiosink'] = Gst.ElementFactory.make('autoaudiosink', 'audio_sink')
                    if use_vaapi and GST_VAAPI_AVAILABLE:
                        elements['videosink'] = Gst.ElementFactory.make('vaapisink', 'vaapi_sink')
                        if elements['videosink']:
                            elements['videosink'].set_property('fullscreen-toggle-mode', 0)
                            elements['videosink'].set_property('show-preroll-frame', False)
                    if 'videosink' not in elements or not elements['videosink']:
                        elements['videosink'] = Gst.ElementFactory.make('autovideosink', 'video_sink')
                    for name, element in elements.items():
                        if element and not element.get_parent():
                            if not pipeline.add(element):
                                self._log(f"Failed to add {name} to pipeline", level='error')
                                return False
                except Exception as e:
                    self._log(f"Error creating GStreamer elements: {str(e)}", level='error')
                    return False
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
                video_elements = [e for e in video_elements if e is not None]
                for element in video_elements:
                    if not video_bin.add(element):
                        self._log(f"Failed to add {element.get_name()} to video bin", level='error')
                        return False
                if not Gst.Element.link_many(*video_elements):
                    self._log("Failed to link video elements", level='error')
                    return False
                audio_bin = Gst.Bin.new("audio_bin")
                audio_elements = [
                    elements['queue_audio'],
                    elements['audioconvert'],
                    elements['audioresample'],
                    elements['autoaudiosink']
                ]
                for element in audio_elements:
                    if element and not audio_bin.add(element):
                        self._log(f"Failed to add {element.get_name()} to audio bin", level='error')
                        return False
                if not Gst.Element.link_many(*audio_elements):
                    self._log("Failed to link audio elements", level='error')
                    return False
                if not pipeline.add(video_bin):
                    self._log("Failed to add video bin to pipeline", level='error')
                    return False                  
                if not pipeline.add(audio_bin):
                    self._log("Failed to add audio bin to pipeline", level='error')
                    return False
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
                elements['source'].connect('pad-added', self._on_pad_added, {
                    'video': ghost_video,
                    'audio': ghost_audio
                })
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
        if 'GST_PLUGIN_FEATURE_RANK' not in os.environ and hasattr(self, 'gst_rank'):
            os.environ['GST_PLUGIN_FEATURE_RANK'] = self.gst_rank
            if self.debug_mode:
                print(f"DEBUG: Set GST_PLUGIN_FEATURE_RANK={self.gst_rank}")
                
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
        return cert.not_valid_after < datetime.datetime.now(datetime.timezone.utc)

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
        webview.connect("download-started", self.on_download_started)

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
        parsed = urlparse(url)
        full_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        for pattern in self.adult_patterns:
            if pattern in full_url.lower():
                return True
        for pattern in self.blocked_patterns:
            if pattern.search(full_url):
                return True
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
            csp_policy = """
                default-src https: http: data: blob:;
                script-src 'unsafe-inline' 'unsafe-eval' https: http: data: blob: *;
                style-src 'unsafe-inline' https: http: data: *;
                img-src data: https: http: blob: *;
                media-src blob: https: http: data: *;
                connect-src https: http: wss: ws: * theanimecommunity.com *.theanimecommunity.com justanime.vercel.app *.justanime.vercel.app animixplay.st *.animixplay.st;
                frame-src https: http: *;
                child-src blob: https: http: *;
                worker-src blob: https: http: *;
                font-src https: http: data: *;
                object-src 'none';
                base-uri 'self';
                form-action https: http: *;
                frame-ancestors 'self';
                upgrade-insecure-requests;
            """
        import re
        sanitized_csp = re.sub(
            r"\b(manifest-src|prefetch-src|navigate-to|require-trusted-types-for|sandbox|trusted-types)[^;]*;?", 
            "", 
            csp_policy, 
            flags=re.IGNORECASE
        ).strip()
        if "media-src" not in sanitized_csp:
            sanitized_csp += "; media-src 'self' blob: https: *;"
        if "connect-src" not in sanitized_csp:
            sanitized_csp += "; connect-src 'self' https: wss:;"
        if "child-src" not in sanitized_csp:
            sanitized_csp += "; child-src 'self' blob: https:;"
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
            WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.START,
        )
        webview.get_user_content_manager().add_script(script)
        webview.get_settings().set_allow_modal_dialogs(True)
        webview.get_settings().set_allow_file_access_from_file_urls(True)
        webview.get_settings().set_allow_universal_access_from_file_urls(True)
        settings = webview.get_settings()
        settings.set_property('enable-media', True)
        settings.set_property('enable-media-capabilities', True)
        settings.set_property('enable-media-stream', True)
        settings.set_property('enable-mediasource', True)
        settings.set_property('enable-encrypted-media', True)
        settings.set_property('media-playback-requires-user-gesture', False)
        settings.set_property('auto-load-images', True)
        settings.set_property('allow-modal-dialogs', True)
        settings.set_property('allow-file-access-from-file-urls', True)
        settings.set_property('allow-universal-access-from-file-urls', True)
        webview.set_settings(settings)           

    def report_csp_violation(self, report):
        report_url = "http://127.0.0.1:9000/"
        data = json.dumps({"csp-report": report}).encode("utf-8")
        req = urllib.request.Request(
            report_url,
            data=data,
            headers={"Content-Type": "application/csp-report"}
        )
        with urllib.request.urlopen(req) as _:
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
            cookie_manager.set_accept_policy(WebKit.CookieAcceptPolicy.NEVER)

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
        self.password = None
        self.use_bridges = False
        self.proxy_settings = None
        self.use_system_tor = False

    def _check_system_tor_running(self):
        """Check if system Tor is already running on standard ports."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", self.tor_port)) == 0:
                return True
        return False

    def _is_tor_already_running(self):
        """Check if a Tor process is already running using the data directory or standard ports."""
        for proc in psutil.process_iter(['name', 'cmdline', 'pid']):
            try:
                if proc.info['name'] and 'tor' in proc.info['name'].lower():
                    cmdline = proc.info['cmdline'] or []
                    if any(self.tor_data_dir in arg for arg in cmdline):
                        return True
                    if any('9050' in arg or '9051' in arg for arg in cmdline):
                        return True
                    try:
                        for conn in proc.net_connections(kind='inet'):
                            if conn.laddr.port in [9050, 9051]:
                                return True
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        continue
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for port in [9050, 9051]:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    return True
            except (socket.timeout, ConnectionRefusedError, OSError):
                continue
        return False

    def start(self):
        """Start the Tor process with proper error handling and port fallback."""
        if not shutil.which("tor"):
            return False
        if hasattr(self, 'process') and self.process:
            self.process.terminate()
            self.process = None
        if self._check_system_tor_running():
            self.use_system_tor = True
            self.is_running_flag = True
            return True
        if self._is_tor_already_running():
            for control_port in [9051, 9151, 9152, 9153]:
                controller = Controller.from_port(port=control_port)
                controller.authenticate(password="shadow-browser")
                self.controller = controller
                socks_ports = controller.get_conf('SocksPort', multiple=True)
                if socks_ports:
                    try:
                        self.tor_port = int(socks_ports[0].split(':')[0])
                    except (ValueError, IndexError):
                        self.tor_port = 9050
                control_ports = controller.get_conf('ControlPort', multiple=True)
                if control_ports:
                    try:
                        self.control_port = int(control_ports[0])
                    except (ValueError, IndexError):
                        self.control_port = control_port
                    controller.get_info('version')
                    self.is_running_flag = True
                    return True   
            if not self.is_running_flag:
                return self._start_new_tor_instance()
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
            except Exception:
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
                return True
        except Exception:
            return False

    def setup_proxy(self, web_context):
        """Configure the WebKit web context to use the Tor SOCKS proxy."""
        if not self.is_running() and not self.start():
            return False   
        proxy_uri = f"socks5://127.0.0.1:{self.tor_port}"
        for var in ("all_proxy", "http_proxy", "https_proxy", "ftp_proxy", "socks_proxy"):
            os.environ[var] = proxy_uri
        if hasattr(web_context, 'set_network_proxy_settings'):
            proxy_settings = WebKit.NetworkProxySettings()
            proxy_settings.add_proxy_for_scheme('http', proxy_uri)
            proxy_settings.add_proxy_for_scheme('https', proxy_uri)
            proxy_settings.add_proxy_for_scheme('ftp', proxy_uri)
            proxy_settings.add_proxy_for_scheme('socks', proxy_uri)
            web_context.set_network_proxy_settings(WebKit.NetworkProxyMode.CUSTOM, proxy_settings)
            return True            
        session = WebKit.NetworkSession.get_default()
        if session and hasattr(session, 'set_network_proxy_settings'):
            proxy_settings = WebKit.NetworkProxySettings()
            proxy_settings.add_proxy_for_scheme('http', proxy_uri)
            proxy_settings.add_proxy_for_scheme('https', proxy_uri)
            proxy_settings.add_proxy_for_scheme('ftp', proxy_uri)
            proxy_settings.add_proxy_for_scheme('socks', proxy_uri)
            session.set_network_proxy_settings(WebKit.NetworkProxyMode.CUSTOM, proxy_settings)
            return True
        return False

    def _start_new_tor_instance(self):
        """Check if system Tor service is running and connect to it."""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", "tor"],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                return False
            try:
                self.controller = Controller.from_port(port=9051)
                try:
                    self.controller.authenticate()
                except Exception:
                    self.controller.authenticate(password="shadow-browser")
                self.is_running_flag = True
                try:
                    socks_ports = self.controller.get_conf("SocksPort", multiple=True)
                    if socks_ports:
                        self.tor_port = int(socks_ports[0].split(":")[0])
                    else:
                        self.tor_port = 9050
                except Exception:
                    self.tor_port = 9050
                return True
            except Exception:
                return False
        except FileNotFoundError:
            if self._check_system_tor_running():
                self.tor_port = 9050
                self.control_port = 9051
                return True
            return False

    def _print_bootstrap_lines(self, line=None):
        """
        Handle and display Tor bootstrap progress messages.
        This method processes Tor bootstrap progress messages and updates the UI
        with the current status. It's called by the Tor controller during the
        bootstrap process.
        Args:
            line (str, optional): The bootstrap progress line from Tor. If None,
                                the current bootstrap status will be displayed.
        """
        if not hasattr(self, 'bootstrap_status'):
            self.bootstrap_status = {
                'progress': 0,
                'tag': None,
                'summary': 'Starting Tor...',
                'warning': None,
                'last_update': 0
            }
        current_time = time.time()
        if line:
            line = line.strip()
            if not line:
                return
            if 'Bootstrapped' in line and '%' in line:
                try:
                    progress = int(line.split('%')[0].split()[-1])
                    self.bootstrap_status['progress'] = min(100, max(0, progress))
                    if '[' in line and ']' in line:
                        tag_start = line.find('[') + 1
                        tag_end = line.find(']')
                        if tag_start < tag_end:
                            self.bootstrap_status['tag'] = line[tag_start:tag_end].lower()
                    if ':' in line:
                        summary = line.split(':', 1)[1].strip()
                        self.bootstrap_status['summary'] = summary
                except (ValueError, IndexError):
                    self.bootstrap_status['summary'] = line
            elif any(w in line.lower() for w in ['warn', 'error', 'failed']):
                self.bootstrap_status['warning'] = line
        if current_time - self.bootstrap_status['last_update'] < 1.0 and line is not None:
            return
        self.bootstrap_status['last_update'] = current_time

        def update_ui():
            if not hasattr(self, 'status_bar'):
                return
            status_text = f"Tor: {self.bootstrap_status['summary']}"
            if self.bootstrap_status['progress'] > 0:
                status_text = f"[{self.bootstrap_status['progress']}%] {status_text}"
            self.status_bar.push(0, status_text)
            self.status_bar.push(0, status_text)
            if self.bootstrap_status.get('warning'):
                self.show_error_message(
                    self.bootstrap_status['warning'],
                    title="Tor Warning"
                )
                self.bootstrap_status['warning'] = None
        if Gtk.main_level() > 0:
            GLib.idle_add(update_ui)
        else:
            update_ui()

class Tab:
    """Represents a single browser tab and its associated data."""
    def __init__(self, url, webview, scrolled_window=None, favicon=None):
        self.url = url or "about:blank"
        self.webview = webview
        self.scrolled_window = scrolled_window
        self.favicon = favicon
        self.label_box = None
        self.favicon_widget = None
        self.title_label = None
        self.close_button = None
        self.header_box = None
        self.last_activity = time.time()
        self.pinned = False
        self.muted = False

    def update_favicon(self, favicon):
        """Update the favicon in the tab's label."""
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
        else:
            from gi.repository import Gdk, GdkPixbuf
            if isinstance(favicon, GdkPixbuf.Pixbuf):
                texture = Gdk.Texture.new_for_pixbuf(favicon)
                self.favicon_widget.set_from_paintable(texture)
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
        """Refresh the last activity timestamp."""
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
        self._portal_request = None
        self._setup_inhibit()

    def _setup_inhibit(self):
        """Set up the appropriate inhibition method for Linux."""
        if platform.system() != 'Linux':
            return           
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
        except ImportError:
            return            
        try:
            DBusGMainLoop(set_as_default=True)
            bus = dbus.SessionBus(private=True)
            try:
                portal = bus.get_object(
                    "org.freedesktop.portal.Desktop",
                    "/org/freedesktop/portal/desktop",
                    follow_name_owner_changes=True
                )
                self._dbus_inhibit = dbus.Interface(
                    portal, dbus_interface="org.freedesktop.portal.Inhibit"
                )
                self._inhibit_method = "portal"
                return
            except dbus.exceptions.DBusException:
                pass              
            try:
                screensaver = bus.get_object(
                    "org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver"
                )
                self._dbus_inhibit = dbus.Interface(
                    screensaver,
                    "org.freedesktop.ScreenSaver"
                )
                self._inhibit_method = "screensaver"
                return
            except dbus.exceptions.DBusException:
                pass               
        except Exception:
            pass

    def inhibit(self):
        """Prevent system sleep/screensaver on Linux."""
        if platform.system() != 'Linux':
            return False      
        if not self._dbus_inhibit or not self._inhibit_method:
            return False            
        try:
            if self._inhibit_method == "portal":
                try:
                    self._portal_request = self._dbus_inhibit.Inhibit(
                        self._app_id,
                        dbus.UInt32(0),  # No flags
                        dbus.Dictionary({
                            'reason': dbus.String(self._reason, variant_level=1),
                            'app_id': dbus.String(self._app_id, variant_level=1)
                        }, signature='sv')
                    )
                    self._inhibit_cookie = self._portal_request
                    return True
                except dbus.exceptions.DBusException:
                    return False                    
            elif self._inhibit_method == "screensaver":
                try:
                    self._inhibit_cookie = self._dbus_inhibit.Inhibit(
                        self._app_id,
                        self._reason
                    )
                    return True
                except dbus.exceptions.DBusException:
                    return False                   
        except Exception:
            return False

    def uninhibit(self):
        """Allow system sleep/screensaver again."""
        if not self._inhibit_cookie or platform.system() != 'Linux':
            return False
        try:
            if self._inhibit_method == "portal" and self._portal_request:
                try:
                    if hasattr(self._portal_request, 'Close'):
                        self._portal_request.Close()
                    return True
                except (dbus.exceptions.DBusException, Exception):
                    return True                   
            elif self._inhibit_method == "screensaver":
                try:
                    self._dbus_inhibit.UnInhibit(self._inhibit_cookie)
                    return True
                except dbus.exceptions.DBusException:
                    return True
            return False                   
        except Exception:
            return False
        finally:
            self._inhibit_cookie = None
            self._portal_request = None
            if hasattr(self, '_bus'):
                try:
                    self._bus.close()
                except Exception:
                    pass
                delattr(self, '_bus')

class ShadowBrowser(Gtk.Application):
    def __init__(self, **kwargs):
        # Initialize the application
        super().__init__(**kwargs)
        
        # Basic initialization
        self.favicon_lock = threading.Lock()
        self.favicon_cache = {}
        self.settings_dialog = None
        self.cookies_check = None
        self.debug_mode = True
        
        # Set up logging
        import logging
        self.logger = logging.getLogger('ShadowBrowser')
        
        if self.debug_mode:
            self.logger.info("Debug mode enabled")
            self.logger.debug(f"Python version: {sys.version}")
            self.logger.debug(f"GTK version: {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
            self.logger.debug(f"WebKit version: {WebKit.get_major_version()}.{WebKit.get_minor_version()}.{WebKit.get_micro_version()}")
        
        # Connect signals
        self.connect('startup', self.on_startup)
        self.connect('activate', self.on_activate)
        self.connect('shutdown', self.on_shutdown)
        
        # Initialize instance variables
        self.window = None
        self.webview = None
        self.content_manager = None
        self.adblocker = None
        self.social_tracker_blocker = None
        self.download_manager = None
        self.tor_manager = None
        self.wake_lock = None
        self.tabs = []
        self.tabs_lock = threading.Lock()
        self.blocked_urls = []
        self.active_downloads = 0
        self.error_handlers = {}
        self.tor_enabled = False
        
    def on_startup(self, app):
        """Handle application startup - initialize non-UI components."""
        if self.debug_mode:
            self.logger.info("Application starting up...")
        
        # Set up VA-API environment
        self._setup_vaapi_environment()
        
        # Initialize non-UI components
        self.content_manager = WebKit.UserContentManager()
        self.adblocker = AdBlocker()
        self.social_tracker_blocker = SocialTrackerBlocker()
        self.download_manager = DownloadManager(None)
        self.wake_lock = SystemWakeLock()
        self.wake_lock_active = False
        
        # Load settings and data
        bookmarks_data = self.load_json(BOOKMARKS_FILE, default=[])
        self.bookmarks = bookmarks_data if isinstance(bookmarks_data, list) else []
        self.history = self.load_json(HISTORY_FILE, default=[])
        
        # Initialize Tor if enabled
        self.tor_enabled = self.load_json(SETTINGS_FILE, {}).get('tor_enabled', False)
        if self.tor_enabled:
            self.initialize_tor()
            if self.tor_manager and self.tor_manager.is_running():
                tor_port = getattr(self.tor_manager, 'tor_port', 9050)
                tor_proxy = f"socks5h://127.0.0.1:{tor_port}"
                os.environ['http_proxy'] = tor_proxy
                os.environ['https_proxy'] = tor_proxy
                os.environ['all_proxy'] = tor_proxy
                self.tor_status = "running"
        
        # Set up error handlers
        self.context = ssl.create_default_context()
        self._setup_error_handlers()
        
        # Set up security policies and content manager
        self.setup_security_policies()
        self._setup_content_manager()
        
        # Set up download manager callbacks
        self.download_manager.on_download_start_callback = self.on_download_start
        self.download_manager.on_download_finish_callback = self.on_download_finish

    def on_activate(self, app):
        """Handle application activation - create and show the main window."""
        if self.debug_mode:
            self.logger.info("Application activated")
        
        if not self.window:
            self.create_main_window()
        
        if self.window:
            self.window.present()
    
    def on_shutdown(self, app):
        """Handle application shutdown - clean up resources."""
        if self.debug_mode:
            self.logger.info("Application shutting down")
        
        if hasattr(self, 'wake_lock') and self.wake_lock:
            self.wake_lock.uninhibit()
        
        if hasattr(self, 'tor_manager') and self.tor_manager:
            self.tor_manager.stop()
    
    def _setup_vaapi_environment(self):
        """Set up VA-API environment for hardware acceleration."""
        if not self.debug_mode:
            return
            
        if self.debug_mode:
            self.logger.debug("Setting up VA-API environment")
            
        vaapi_env = {
            'LIBVA_DRIVER_NAME': 'iHD',
            'LIBVA_DRIVERS_PATH': '/usr/lib64/dri',
            'GST_VAAPI_DRM_DEVICE': '/dev/dri/renderD128',
            'GST_VAAPI_ALL_DRIVERS': '1'
        }
        
        for key, value in vaapi_env.items():
            if key not in os.environ:
                os.environ[key] = value
                if self.debug_mode:
                    self.logger.debug(f"Set VA-API environment {key}={value}")
        
        self.gst_rank = 'vaapih264dec:256,vaapih265dec:256,avdec_h264:128,avdec_aac_fixed:128'
        if 'GST_PLUGIN_FEATURE_RANK' not in os.environ:
            os.environ['GST_PLUGIN_FEATURE_RANK'] = self.gst_rank
            if self.debug_mode:
                self.logger.debug(f"Set GST_PLUGIN_FEATURE_RANK={self.gst_rank}")
    
    def _setup_error_handlers(self):
        """Set up error handlers for the application."""
        def _handle_ssl_error(error, url):
            if self.debug_mode:
                self.logger.warning(f"SSL Error loading {url}: {error}")
            return False
            
        def _handle_network_error(error, url):
            if self.debug_mode:
                self.logger.warning(f"Network Error loading {url}: {error}")
            return False
            
        def _handle_http_error(error, url):
            if self.debug_mode:
                self.logger.warning(f"HTTP Error loading {url}: {error}")
            return False
        
        self.error_handlers = {
            'ssl': _handle_ssl_error,
            'network': _handle_network_error,
            'http': _handle_http_error
        }
    
    def _setup_content_manager(self):
        """Set up the WebKit content manager with scripts and handlers."""
        if not self.content_manager:
            return
            
        try:
            # Set up content manager with adblocker and other scripts
            if self.adblocker:
                self.adblocker.inject_to_webview(self.content_manager)
            
            # Register additional scripts and message handlers
            self.content_manager.register_script_message_handler("voidLinkClicked")
            self.content_manager.connect(
                "script-message-received::voidLinkClicked", 
                self.on_void_link_clicked
            )
            
            # Add custom scripts
            scripts = [
                ("console.log('ShadowBrowser content manager initialized');",
                 WebKit.UserContentInjectedFrames.ALL_FRAMES,
                 WebKit.UserScriptInjectionTime.START),
                
                # Add other scripts as needed
                # self._create_security_script(),
                # self._create_performance_script(),
            ]
            
            for script, frames, injection_time in scripts:
                user_script = WebKit.UserScript.new(script, frames, injection_time)
                self.content_manager.add_script(user_script)
                
        except Exception as e:
            if self.debug_mode:
                self.logger.error(f"Error setting up content manager: {e}")
    
    def create_main_window(self):
        """Create and set up the main application window."""
        if self.debug_mode:
            self.logger.info("Creating main window")
        
        # Create the main window
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_default_size(1024, 768)
        self.window.set_title("Shadow Browser")
        
        # Create main container
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.set_child(self.main_box)
        
        # Create URL bar
        self.url_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.main_box.append(self.url_bar)
        
        # Add back/forward buttons
        self.back_button = Gtk.Button(label="←")
        self.forward_button = Gtk.Button(label="→")
        self.url_bar.append(self.back_button)
        self.url_bar.append(self.forward_button)
        
        # Add URL entry
        self.url_entry = Gtk.Entry()
        self.url_entry.set_hexpand(True)
        self.url_bar.append(self.url_entry)
        
        # Create notebook for tabs
        self.notebook = Gtk.Notebook()
        self.main_box.append(self.notebook)
        
        # Create initial tab
        self.new_tab()
        
        # Connect signals
        self._connect_signals()
        
        # Show the window
        self.window.present()
    
    def _connect_signals(self):
        """Connect UI signals."""
        if not self.window:
            return
            
        # Connect window close event
        self.window.connect("close-request", self.on_window_close)
        
        # Connect URL entry signals
        if hasattr(self, 'url_entry'):
            self.url_entry.connect("activate", self.on_url_activate)
        
        # Connect navigation buttons
        if hasattr(self, 'back_button'):
            self.back_button.connect("clicked", self.on_back_clicked)
        if hasattr(self, 'forward_button'):
            self.forward_button.connect("clicked", self.on_forward_clicked)
    
    def new_tab(self, url=None):
        """Create a new browser tab."""
        if not self.window or not self.notebook:
            return
            
        # Create a new WebView
        webview = WebKit.WebView()
        
        # Configure WebView settings
        settings = webview.get_settings()
        self._configure_common_webview_settings(settings, webview)
        
        # Set up WebView signals
        webview.connect('context-menu', self.on_webview_context_menu)
        webview.connect("create", self.on_webview_create)
        
        # Add key controller for keyboard shortcuts
        key_controller = Gtk.EventControllerKey()
        key_controller.connect('key-pressed', self.on_webview_key_press)
        webview.add_controller(key_controller)
        
        # Create a scrolled window for the WebView
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(webview)
        
        # Create tab label with close button
        label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tab_label = Gtk.Label(label="New Tab")
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.set_relief(Gtk.ReliefStyle.NONE)
        close_button.set_focus_on_click(False)
        
        label_box.append(tab_label)
        label_box.append(close_button)
        label_box.show_all()
        
        # Add the tab to the notebook
        tab_index = self.notebook.append_page(scrolled, label_box)
        self.notebook.set_tab_reorderable(scrolled, True)
        self.notebook.set_current_page(tab_index)
        
        # Store tab information
        tab = Tab(url or "about:blank", webview, scrolled)
        self.tabs.append(tab)
        
        # Connect close button signal
        close_button.connect("clicked", self.on_tab_close_clicked, tab)
        
        # Load the URL if provided
        if url:
            webview.load_uri(url)
        
        return tab
    
    def on_tab_close_clicked(self, button, tab):
        """Handle tab close button click."""
        if not self.notebook or not tab or not tab.scrolled_window:
            return
            
        page_num = self.notebook.page_num(tab.scrolled_window)
        if page_num >= 0:
            self.notebook.remove_page(page_num)
            
        # Remove tab from tabs list
        if tab in self.tabs:
            self.tabs.remove(tab)
            
        # Close window if no tabs left
        if len(self.tabs) == 0:
            self.window.close()
    
    def on_window_close(self, window):
        """Handle window close event."""
        # Save session state
        self.save_session()
        
        # Clean up resources
        if hasattr(self, 'wake_lock') and self.wake_lock:
            self.wake_lock.uninhibit()
        
        # Quit the application
        self.quit()
        return False

    def initialize_tor(self, retry_count=0, max_retries=2):
        """Initialize Tor with proper error handling and fallback mechanisms.
        Args:
            retry_count: Current retry attempt
            max_retries: Maximum number of retry attempts
        Returns:
            bool: True if Tor was successfully initialized, False otherwise
        """
        if not self.tor_enabled:
            self.tor_manager = None
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
                proxies = {
                    'http': f'socks5h://127.0.0.1:{tor_port}',
                    'https': f'socks5h://127.0.0.1:{tor_port}'
                }
                session = requests.Session()
                session.proxies = proxies
                try:
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
                        return self.initialize_tor(retry_count + 1, max_retries)
                    return self.initialize_tor(retry_count + 1, max_retries)
            else:
                if retry_count < max_retries:
                    if self.tor_manager:
                        self.tor_manager.stop()
                        self.tor_manager = None
                    return self.initialize_tor(retry_count + 1, max_retries)
                return False
        except Exception:
            self.tor_status = "error"
            if retry_count < max_retries:
                if hasattr(self, 'tor_manager') and self.tor_manager:
                    self.tor_manager.stop()
                    self.tor_manager = None
                return self.initialize_tor(retry_count + 1, max_retries)
            return False

    def _get_default_html(self):
        """
        Get the default HTML content for new tabs.       
        Returns:
            str: HTML content for the new tab page
        """
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>New Tab</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen-Sans, Ubuntu, Cantarell, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background-color: #f5f5f5;
                    color: #333;
                }
                .container {
                    text-align: center;
                    padding: 2rem;
                    max-width: 600px;
                }
                h1 {
                    font-size: 2.5rem;
                    margin-bottom: 1rem;
                }
                p {
                    font-size: 1.1rem;
                    line-height: 1.6;
                    color: #666;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Shadow Browser</h1>
                <p>Enter a URL in the address bar to begin browsing.</p>
            </div>
        </body>
        </html>
        """

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
            webview.connect('load-changed', self._on_webview_load_changed)
            webview.load_html(self._get_default_html(), 'about:blank')
            self.setup_webview_settings(webview)
            context = webview.get_context()
            if hasattr(context, 'get_soup_session'):
                soup_session = context.get_soup_session()
                if hasattr(soup_session, 'trust_env'):
                    soup_session.trust_env = True
        except Exception:
            pass
        if self.tor_enabled and self.tor_manager:
            if not self.tor_manager.is_running():
                self.initialize_tor()
            if self.tor_manager.is_running():
                web_context = webview.get_context()
                self.tor_manager.setup_proxy(web_context)
        self._register_webview_message_handlers(webview)
        self.adblocker.inject_to_webview(content_manager)
        self.adblocker.enable_csp(webview)
        try:
            self._setup_webview_handlers(webview)
        except Exception:
            pass
        webview.connect("create", self.on_webview_create)
        return webview

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
            try:
                webview.load_uri('about:blank')
                if hasattr(webview, 'stop_loading'):
                    webview.stop_loading()
                if hasattr(webview, 'load_html_string'):
                    webview.load_html_string('', 'about:blank')
            except Exception:
                pass
            parent = webview.get_parent()
            if parent:
                parent.remove(webview)
        except Exception:
            pass
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
        Configure WebView for secure and smooth media playback.
        """
        if load_event != WebKit.LoadEvent.STARTED:
            return False
        uri = webview.get_uri()
        if not uri:
                    return False
        if not (uri.startswith(('http:', 'https:', 'blob:'))):
            return False
        if any(blocked_url in uri.lower() for blocked_url in self.blocked_urls):
            return True
        settings = webview.get_settings()
        try:
            default_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            user_agent = settings.get_property('user-agent') or default_ua
            if 'SecurityBrowser' not in user_agent:
                settings.set_property("user-agent", f"{user_agent} SecurityBrowser/1.0")
        except Exception:
            pass
        core_settings = {
            "enable-javascript": True,
            "enable-page-cache": True,
            "enable-smooth-scrolling": True,
            "enable-fullscreen": True,
            "auto-load-images": True,
            "enable-media": True,
            "enable-media-capabilities": True,
            "enable-media-stream": True,
            "enable-mediasource": True,
            "enable-encrypted-media": True,
            "media-playback-requires-user-gesture": False,
            "media-playback-allows-inline": True
        }
        for k, v in core_settings.items():
            settings.set_property(k, v)
        if hasattr(settings, "set_hardware_acceleration_policy"):
            if hasattr(WebKit, "HardwareAccelerationPolicy"):
                settings.set_hardware_acceleration_policy(
                    WebKit.HardwareAccelerationPolicy.ALWAYS
                )
        accel_settings = [
            "enable-accelerated-compositing",
            "enable-accelerated-video",
            "enable-accelerated-video-decode",
            "enable-accelerated-webgl",
            "enable-webrtc-hw-decoding",
            "enable-webrtc-hw-encoding"
        ]
        for accel_flag in accel_settings:
            try:
                settings.set_property(accel_flag, True)
            except (TypeError, ValueError):
                pass
        if hasattr(settings, "set_auto_play_policy"):
            settings.set_auto_play_policy(WebKit.AutoPlayPolicy.ALLOW)
        if hasattr(settings, "set_webrtc_ip_handling_policy"):
            settings.set_webrtc_ip_handling_policy(
                WebKit.WebRTCIceTransportPolicy.ALL
            )
        if hasattr(settings, "set_media_playback_requires_user_gesture"):
            settings.set_media_playback_requires_user_gesture(False)
        if hasattr(settings, "set_media_playback_allows_inline"):
            settings.set_media_playback_allows_inline(True)
        if hasattr(settings, "set_enable_media"):
            settings.set_enable_media(True)
        if hasattr(settings, "set_enable_mediasource"):
            settings.set_enable_mediasource(True)
        if hasattr(settings, "set_enable_media_capabilities"):
            settings.set_enable_media_capabilities(True)
        if hasattr(settings, "set_enable_encrypted_media"):
            settings.set_enable_encrypted_media(True)
        if hasattr(settings, "set_media_content_types_requiring_hardware_support"):
            settings.set_media_content_types_requiring_hardware_support("video/.*")
        webview.set_settings(settings)
        return False

    def _inject_videojs_support(self, webview):
        """Inject Video.js support script into the WebView"""
        videojs_init_script = """
        function initVideoJS() {
            const videos = document.querySelectorAll('video.video-js:not(.vjs-has-started)');      
            videos.forEach(video => {
                try {
                    if (!video.classList.contains('vjs-has-started')) {
                        const player = videojs(video, {
                            controls: true,
                            autoplay: 'muted',
                            preload: 'auto'
                        });                       
                        video.classList.add('vjs-has-started');
                    }
                } catch (e) {
                    console.error('Video.js initialization error:', e);
                }
            });
        }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initVideoJS);
        } else {
            initVideoJS();
        }
        const observer = new MutationObserver((mutations) => {
            initVideoJS();
        });
        observer.observe(document.body, {
            childList: true,
            subtree: True
        });
        """
        webview.evaluate_javascript(videojs_init_script, -1, None, None, None)

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
        """
        import re
        def replace_uuid(match):
            original = match.group(0)
            uuid_str = match.group(1)
            token = self.uuid_to_token(uuid_str)
            replaced = original.replace(uuid_str, token)
            return replaced
        pattern = r"onclick=\"window\.open\(dbneg\('([0-9a-fA-F\-]+)'\)"
        transformed_html = re.sub(pattern, replace_uuid, html_content)
        return transformed_html
        
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
                if (typeof isUrlBlocked === 'function' && isUrlBlocked(url)) {
                    console.log('[ShadowBrowser] window.open blocked by adblocker:', url);
                    return null;
                }
                var urlToSend = (typeof url === 'string' && url) ? url : '';
                if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.windowOpenHandler) {
                    window.webkit.messageHandlers.windowOpenHandler.postMessage(urlToSend);
                    return null;
                }
                return originalOpen.apply(this, arguments);
            };
        })();
        '''
        content_manager.add_script(
            self._create_user_script(js_code)
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
        """Handle console messages from JavaScript."""
        try:
            level_map = {
                0: "DEBUG",
                1: "INFO",
                2: "WARNING",
                3: "ERROR"
            }
            log_level = level_map.get(level, "INFO")
            source = source_id or "JavaScript"
            if hasattr(self, "debug_mode") and self.debug_mode:
                print(f"[{log_level}] {source}:{line}: {message}")
        except Exception as e:
            if hasattr(self, "debug_mode") and self.debug_mode:
                print(f"Error handling console message: {e}")
    
    def _create_user_script(self, js_code, injection_time=WebKit.UserScriptInjectionTime.START, 
                           frames=WebKit.UserContentInjectedFrames.ALL_FRAMES):
        """
        Create a WebKit UserScript with common parameters.
        Args:
            js_code: JavaScript code to inject
            injection_time: When to inject the script
            frames: Which frames to inject into
        Returns:
            WebKit.UserScript: Configured user script
        """
        return WebKit.UserScript.new(
            js_code,
            frames,
            injection_time,
        )
    
    def on_webview_create(self, webview, navigation_action):
        """Handle creation of new WebViews for popups and new windows."""
        try:
            new_window = Gtk.Window(application=self)
            new_window.set_default_size(1024, 768)
            new_webview = self.create_secure_webview()
            scrolled_window = Gtk.ScrolledWindow()
            scrolled_window.set_child(new_webview)
            new_window.set_child(scrolled_window)
            try:
                if hasattr(self, 'window') and self.window:
                    new_window.set_transient_for(self.window)
            except Exception:
                pass
            new_window.set_destroy_with_parent(True)
            new_window.present()
            return new_webview
        except Exception:
            try:
                return self.create_secure_webview()
            except Exception:
                return None
        
    def _setup_webview_handlers(self, webview):
        """
        Set up common WebView event handlers to avoid duplication.
        Args:
            webview: The WebView to configure
        """
        if hasattr(webview, 'connect'):
            webview.connect('context-menu', self.on_webview_context_menu)
            key_controller = Gtk.EventControllerKey()
            key_controller.connect('key-pressed', self.on_webview_key_press)
            webview.add_controller(key_controller)
        webview.connect('notify::favicon', self._on_favicon_changed)
        if not hasattr(self, '_original_load_changed'):
            self._original_load_changed = webview.connect('load-changed', self._on_webview_load_changed)       
        webview.connect("create", self.on_webview_create)
        webview.connect('resource-load-started', self.on_resource_load_started)
        
    def _configure_common_webview_settings(self, settings, webview):
        """
        Configure common WebView settings to avoid duplication.
        Args:
            settings: WebKit Settings object to configure
            webview: WebView instance (for context access if needed)
        """
        settings.set_enable_javascript(True)
        if hasattr(settings, 'set_enable_html5_local_storage'):
            settings.set_enable_html5_local_storage(True)
        if hasattr(settings, 'set_enable_html5_database'):
            settings.set_enable_html5_database(True)
        if hasattr(settings, 'set_auto_load_images'):
            settings.set_auto_load_images(True)
        if hasattr(settings, 'set_enable_smooth_scrolling'):
            settings.set_enable_smooth_scrolling(True)
        if hasattr(settings, 'set_enable_fullscreen'):
            settings.set_enable_fullscreen(True)
        if hasattr(settings, 'set_enable_developer_extras'):
            settings.set_enable_developer_extras(getattr(self, 'debug_mode', False))
        if hasattr(settings, 'set_enable_private_browsing'):
            settings.set_enable_private_browsing(False)
        if hasattr(settings, 'set_enable_write_console_messages_to_stdout'):
            settings.set_enable_write_console_messages_to_stdout(False)
        if hasattr(settings, 'set_enable_hardware_acceleration'):
            settings.set_enable_hardware_acceleration(False)
        if hasattr(settings, 'set_enable_webgl'):
            settings.set_enable_webgl(True)
        if hasattr(settings, 'set_enable_webaudio'):
            settings.set_enable_webaudio(True)
        if hasattr(settings, 'set_enable_accelerated_2d_canvas'):
            settings.set_enable_accelerated_2d_canvas(True)
        if hasattr(settings, 'set_preferred_video_decoder'):
            settings.set_preferred_video_decoder('software')
        if hasattr(settings, 'set_hardware_acceleration_policy'):
            settings.set_hardware_acceleration_policy(1)

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
            observer.observe(document.body, { childList: true, subtree: True });
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

    def _configure_gstreamer_environment(self):
        """
        Configure GStreamer environment for optimal video playback with hardware acceleration.
        This method sets up the GStreamer pipeline and environment variables for the best
        possible video playback performance, with fallbacks for different hardware configurations.
        """
        try:
            if not Gst.is_initialized():
                Gst.init(None)
            if os.getenv('GST_DEBUG'):
                os.environ['GST_DEBUG_NO_COLOR'] = '1'
            if hasattr(self, 'vaapi_manager') and hasattr(self.vaapi_manager, 'vaapi_available') and self.vaapi_manager.vaapi_available:
                os.environ['GST_VAAPI_ALL_DRIVERS'] = '1'
                os.environ['GST_VAAPI_DRM_THREADED_DECODE'] = '1'
                os.environ['GST_VAAPI_DRM_DISABLE_VP9'] = '0'
                os.environ['GST_VAAPI_DRM_DEVICE'] = '/dev/dri/renderD128'
                os.environ['GST_VAAPI_DRM_DISPLAY'] = 'drm'
                os.environ['GST_VAAPI_VIDEO_SINK'] = 'vaapisink'
                os.environ['GST_VAAPI_DISABLE_VPP'] = '1'
                os.environ['GST_VAAPI_DISABLE_FILTER'] = '1'
                os.environ['GST_VAAPI_DISABLE_POSTPROC'] = '1'
                os.environ['GST_VAAPI_DISABLE_VIDEOPROCESS'] = '1'
                os.environ['GST_VAAPI_DISABLE_ALL'] = '1'
                os.environ['GST_VAAPI_DISABLE_DECODER'] = '1'
                os.environ['GST_VAAPI_DISABLE_ENCODER'] = '1'
                os.environ['GST_PLUGIN_FEATURE_RANK'] = (
                    'vafilter:0,vavpp:0,vaapipostproc:0,vaapih264dec:0,vaapih265dec:0,'
                    'vaapivp9dec:0,vaapivp8dec:0,vaapimpeg2dec:0,vaapisink:0,'
                    'avdec_h264:MAX,avdec_h265:MAX,avdec_vp9:MAX,avdec_vp8:MAX,'
                    'glimagesink:MAX,autovideosink:MIN'
                )
                os.environ['GST_PLUGIN_FEATURE_BLACKLIST'] = 'vafilter,vavpp,vaapipostproc'
                os.environ['VAAPI_MPEG4_ENABLED'] = '1'
                os.environ['VAAPI_MPEG2_ENABLED'] = '1'
                os.environ['VAAPI_VP8_ENABLED'] = '1'
                os.environ['VAAPI_VP9_ENABLED'] = '1'
                os.environ['GST_VIDEO_DECODER_MAX_ERRORS'] = '100'
                os.environ['GST_VIDEO_DECODER_DROP_FRAME_INTERVAL'] = '1000'
                os.environ['GST_BASE_SINK_SYNC_METHOD'] = 'latest'
                os.environ['GST_VIDEO_SINK_SYNC_METHOD'] = 'latest'
                os.environ['GST_VIDEO_DECODER_SKIP_CORRUPTED'] = '1'
                if getattr(self, 'debug_mode', False):
                    print("VA-API hardware acceleration enabled")
            else:
                os.environ['GST_PLUGIN_FEATURE_RANK'] = (
                    'vafilter:0,vavpp:0,vaapipostproc:0,vaapih264dec:0,vaapih265dec:0,'
                    'vaapivp9dec:0,vaapivp8dec:0,vaapimpeg2dec:0,vaapisink:0,'
                    'avdec_h264:MAX,avdec_h265:MAX,avdec_vp9:MAX,avdec_vp8:MAX,'
                    'glimagesink:MAX,autovideosink:MIN'
                )
                os.environ['GST_PLUGIN_FEATURE_BLACKLIST'] = 'vafilter,vavpp,vaapipostproc'
                os.environ['GST_VAAPI_DISABLE_VPP'] = '1'
                os.environ['GST_VAAPI_DISABLE_FILTER'] = '1'
                os.environ['GST_VAAPI_DISABLE_POSTPROC'] = '1'
                os.environ['GST_VAAPI_DISABLE_VIDEOPROCESS'] = '1'
                os.environ['GST_VAAPI_DISABLE_ALL'] = '1'
                os.environ['GST_VAAPI_DISABLE_DECODER'] = '1'
                os.environ['GST_VAAPI_DISABLE_ENCODER'] = '1'
                os.environ['GST_VAAPI_DISABLE_DMABUF'] = '1'
                os.environ['GST_VAAPI_DISABLE_SURFACE'] = '1'
                os.environ['GST_VAAPI_DISABLE_BUFFER'] = '1'
                if getattr(self, 'debug_mode', False):
                    print("Using software decoding (VA-API not available)")
            os.environ['WEBKIT_GST_ENABLE_VIDEO'] = '1'
            os.environ['WEBKIT_GST_ENABLE_AUDIO'] = '1'
            os.environ['GST_BASE_SINK_SYNC_METHOD'] = 'latest'
            os.environ['GST_VIDEO_DECODER_USE_THREADS'] = '1'
            os.environ['GST_VIDEO_SINK_SYNC_METHOD'] = 'latest'
            os.environ['GST_VIDEO_DECODER_SKIP_CORRUPTED'] = '1'
            os.environ['GST_ELEMENT_FACTORY_CACHE'] = '1'
            os.environ['GST_PLUGIN_LOADING_WHITELIST'] = 'gstreamer'
            os.environ['GST_REGISTRY_DISABLE_PLUGIN_CACHE'] = '1'
            os.environ['GST_PLUGIN_PATH'] = '/usr/lib64/gstreamer-1.0:/usr/lib/gstreamer-1.0'
            os.environ['WEBKIT_GST_ENABLE_MP4'] = '1'
            os.environ['WEBKIT_GST_ENABLE_WEBM'] = '1'
            os.environ['WEBKIT_GST_USE_PLAYBIN3'] = '1'
            os.environ['WEBKIT_DISABLE_HW_ACCELERATED_VIDEO_DECODER_BLACKLIST'] = '1'
            os.environ['GST_VIDEO_DECODER_USE_THREADS'] = '1'
            os.environ['GST_VIDEO_SINK'] = 'glimagesink'
            os.environ['GST_GL_WINDOW'] = 'gdk'
            os.environ['GST_GL_API'] = 'opengl'
            os.environ['GST_GL_WINDOW_X11_AUTOSELECT_ENV'] = '1'
            os.environ['GST_GL_XINITTHREADS'] = '1'
            os.environ['LIBVA_DRIVER_NAME'] = os.environ.get('LIBVA_DRIVER_NAME', 'i965')            
            if getattr(self, 'debug_mode', False):
                print("GStreamer environment configured")
                print(f"  GST_PLUGIN_FEATURE_RANK: {os.environ.get('GST_PLUGIN_FEATURE_RANK')}")
                print(f"  GST_VAAPI_DRM_DEVICE: {os.environ.get('GST_VAAPI_DRM_DEVICE')}")
                print(f"  LIBVA_DRIVER_NAME: {os.environ.get('LIBVA_DRIVER_NAME')}")                
        except Exception as e:
            if getattr(self, 'debug_mode', False):
                print(f"Error configuring GStreamer environment: {e}")
            Gst.init(None)
        if not hasattr(self, 'settings'):
            self.settings = WebKit.Settings()
        settings = self.settings       
        settings_dict = {
            'enable-javascript': True,
            'enable-plugins': True,
            'enable-html5-local-storage': True,
            'enable-html5-database': True,
            'enable-html5-offline-application-cache': True,
            'enable-page-cache': True,
            'enable-java': False,
            'enable-media-stream': True,
            'enable-media': True,
            'media-playback-requires-user-gesture': False,
            'media-playback-allows-inline': True
        }       
        for k, v in settings_dict.items():
            try:
                if hasattr(settings, f"set_{k.replace('-', '_')}"):
                    getattr(settings, f"set_{k.replace('-', '_')}")(v)
                else:
                    settings.set_property(k, v)
            except Exception as e:
                if getattr(self, 'debug_mode', False):
                    print(f"Error setting {k}: {e}")
        try:
            if hasattr(settings, 'set_hardware_acceleration_policy'):
                policy = getattr(WebKit, 'HardwareAccelerationPolicy', None)
                if policy:
                    settings.set_hardware_acceleration_policy(policy.ALWAYS)
        except Exception:
            pass
        if hasattr(self, 'get_current_webview'):
            webview = self.get_current_webview()
            if webview:
                try:
                    context = webview.get_context()
                    if hasattr(context, 'get_website_data_manager'):
                        manager = context.get_website_data_manager()
                        if hasattr(manager, 'set_cache_model'):
                            manager.set_cache_model(WebKit.CacheModel.DOCUMENT_BROWSER)
                except Exception as _: 
                    if getattr(self, 'debug_mode', False):
                        print("Warning: Could not configure webview context")
        self._configure_gstreamer_environment()
        try:
            if hasattr(settings, 'set_enable_webrtc_hardware_acceleration'):
                settings.set_enable_webrtc_hardware_acceleration(True)
        except Exception:
            pass
        try:
            settings.set_enable_mediasource(True)
            settings.set_enable_media_capabilities(True)
            settings.set_enable_media_stream(True)
        except Exception:
            pass
        try:
            context = webview.get_context()
            if context:
                if hasattr(context, "set_process_model"):
                    context.set_process_model(WebKit.ProcessModel.MULTIPLE_SECONDARY_PROCESSES)
                if hasattr(context, "set_media_playback_requires_user_gesture"):
                    context.set_media_playback_requires_user_gesture(False)
                if hasattr(settings, "set_auto_play_policy"):
                    settings.set_auto_play_policy(WebKit.AutoPlayPolicy.ALLOW)
                if hasattr(context, "set_webrtc_ip_handling_policy"):
                    context.set_webrtc_ip_handling_policy(WebKit.WebRTCIceTransportPolicy.ALL)
                if hasattr(context, "set_cache_model"):
                    context.set_cache_model(WebKit.CacheModel.DOCUMENT_BROWSER)
        except Exception:
            pass
        return webview

    def _on_webview_create(self, webview, navigation_action):
        """Handle creation of new WebView instances (for new windows/tabs)."""
        new_webview = self.create_secure_webview()
        if not new_webview:
            return None            
        self.setup_webview_settings(new_webview)
        self._setup_cross_origin_handling(new_webview)
        self.add_tab_with_webview(new_webview, "New Tab")       
        if navigation_action and navigation_action.get_request():
            request = navigation_action.get_request()
            if request and request.get_uri():
                new_webview.load_uri(request.get_uri())
        return new_webview
        
    def _setup_cross_origin_handling(self, webview):
        """Set up CORS and cross-origin handling for the webview."""
        webview.get_context().set_web_process_extensions_directory("/tmp")   
    cors_script = """
    (function() {
        const originalOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url, async, user, password) {
            this._url = url;
            return originalOpen.apply(this, arguments);
        };
        const originalSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.send = function(body) {
            this.addEventListener('readystatechange', function() {
                if (this.readyState === 4) {
                    try {
                        if (this._url.startsWith('http://') || this._url.startsWith('https://')) {
                            this.responseHeaders = this.responseHeaders.replace(/Access-Control-Allow-Origin: .*/g, 'Access-Control-Allow-Origin: *');
                        }
                    } catch (_) {
                    }
                }
            });
            return originalSend.apply(this, arguments);
        };
    });    
    Object.defineProperty(document, 'domain', {
        get: function() { 
            return window.location.hostname; 
        },
        set: function(domain) {
            if (domain.includes(window.location.hostname)) {
                Object.defineProperty(document, 'domain', {
                    value: domain,
                    writable: false
                });
            }
        },
        configurable: true
    });
    """
    
    def clear_cache(self, webview):
        """Clear browser cache and reload the current page."""
        js_code = """
        window.addEventListener('message', function(event) {
            if (window.parent !== window) {
                window.parent.postMessage(event.data, '*');
            }
            const iframes = document.getElementsByTagName('iframe');
            for (let i = 0; i < iframes.length; i++) {
                try {
                    iframes[i].contentWindow.postMessage(event.data, '*');
                } catch (_) {
                }
            }
        });
        Object.defineProperty(document, 'domain', {
            get: function() { 
                return window.location.hostname; 
            },
            set: function(domain) {
                if (domain.includes(window.location.hostname)) {
                    Object.defineProperty(document, 'domain', {
                        value: domain,
                        writable: false
                    });
                }
            },
            configurable: true
        });
        """
        try:
            webview.evaluate_javascript(js_code, -1, None, None, None, None, None, None)
            context = webview.get_website_data_manager().get_context()
            data_types = 0
            for attr in dir(WebKit.WebsiteDataTypes):
                if not attr.startswith('_'):
                    data_types |= getattr(WebKit.WebsiteDataTypes, attr)            
            if data_types > 0:
                context.clear_website_data(
                    data_types,
                    0, 
                    None, None, None
                )
            current_uri = webview.get_uri()
            if current_uri:
                webview.load_uri(current_uri)
            self.show_info_message("✅ Cache cleared successfully")
            return True                    
        except Exception:
            try:
                current_uri = webview.get_uri()
                if current_uri:
                    webview.load_uri(current_uri)
                self.show_info_message("✅ Page reloaded (some cache may remain)")
                return True
            except Exception as e2:
                self.show_error_message(f"❌ Error clearing cache: {e2}")
                return False

    def on_clear_data_confirm(self, dialog):
        """Handle the confirmation of data clearing."""
        if hasattr(self, 'cookies_check') and self.cookies_check.get_active():
            self.clear_cookies()
        if hasattr(self, 'cache_check') and self.cache_check.get_active():
            self.clear_cache()
        if hasattr(self, 'history_check') and self.history_check.get_active():
            self.clear_history()
        if hasattr(self, 'local_storage_check') and self.local_storage_check.get_active():
            self.clear_all_data()
        if dialog and dialog.is_visible():
            dialog.destroy()

    def on_clear_data_response(self, dialog, response_id):
        if response_id == Gtk.ResponseType.OK:
            self.on_clear_data_confirm(dialog)
        dialog.destroy()

    def clear_all_data(self):
        """Clear all browsing data using WebKit's data manager."""
        data_types = 0
        if hasattr(self, 'cookies_check') and self.cookies_check.get_active():
            data_types |= WebKit.WebsiteDataTypes.COOKIES
            data_types |= WebKit.WebsiteDataTypes.WEBSQL_DATABASES
            data_types |= WebKit.WebsiteDataTypes.INDEXEDDB_DATABASE
        if hasattr(self, 'cache_check') and self.cache_check.get_active():
            data_types |= WebKit.WebsiteDataTypes.DISK_CACHE
            data_types |= WebKit.WebsiteDataTypes.MEMORY_CACHE
        if hasattr(self, 'history_check') and self.history_check.get_active():
            self.history = []
            self.save_json(HISTORY_FILE, self.history)
        if hasattr(self, 'local_storage_check') and self.local_storage_check.get_active():
            data_types |= WebKit.WebsiteDataTypes.LOCAL_STORAGE
            data_types |= WebKit.WebsiteDataTypes.SESSION_STORAGE
            data_types |= WebKit.WebsiteDataTypes.WEBSQL_DATABASES
            data_types |= WebKit.WebsiteDataTypes.INDEXEDDB_DATABASE
        if data_types == 0:
            data_types = WebKit.WebsiteDataTypes.ALL

    def on_data_cleared(self, manager, result, user_data=None):
        """Callback when data clearing is complete."""
        manager.clear_finish(result)
        self.show_info_message("✅ Browsing data cleared successfully")
        notification = Gtk.InfoBar()
        notification.set_message_type(Gtk.MessageType.INFO)
        notification.add_button("_OK", Gtk.ResponseType.OK)
        content = notification.get_content_area()
        content.append(Gtk.Label(label="Browsing data has been cleared"))
        if hasattr(self, 'window') and self.window:
            overlay = Gtk.Overlay()
            overlay.set_child(self.window.get_child())
            overlay.add_overlay(notification)
            self.window.set_child(overlay)
            GLib.timeout_add_seconds(3, self._remove_notification, notification)

    def _remove_notification(self, notification):
        """Remove the notification from the window."""
        if hasattr(self, 'window') and self.window:
            overlay = notification.get_parent()
            if overlay and isinstance(overlay, Gtk.Overlay):
                child = overlay.get_child()
                overlay.unparent()
                self.window.set_child(child)
        return False
 
    def on_downloads_clicked(self, button):
        downloads_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if not downloads_dir:
            downloads_dir = os.path.expanduser("~/Downloads")
        import subprocess
        subprocess.Popen(["xdg-open", downloads_dir])

    def is_valid_url(self, url):
        result = urlparse(url)
        return all([result.scheme, result.netloc])

    def load_url(self, url):
        """Load a URL in the current active webview."""
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            if url.startswith("www."):
                url = "https://" + url
            else:
                url = f"https://{url}" if '://' not in url else url
        webview = self.get_current_webview()
        if webview is None and hasattr(self, 'webview') and self.webview is not None:
            webview = self.webview
        if webview:
            try:
                webview.load_uri(url)
                if hasattr(self, 'url_entry') and self.url_entry:
                    self.url_entry.set_text(url)
                if hasattr(self, 'update_history'):
                    self.update_history(url)
            except Exception as e:
                print(f"Error loading URL {url}: {e}")

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
            favicon_img = Gtk.Box()
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
            button.connect("clicked", lambda btn, u=url: self.load_url(u))
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

    def _on_delete_bookmark_clicked(self, button, url):
        """Handle click on the delete bookmark button.
        
        Args:
            button: The button that was clicked
            url: The URL of the bookmark to delete
        """
        # Find and remove the bookmark with the matching URL
        self.bookmarks = [b for b in self.bookmarks if not (isinstance(b, dict) and b.get('url') == url)]
        self.save_json(BOOKMARKS_FILE, self.bookmarks)
        self.update_bookmarks_menu(self.bookmark_menu)
        self._close_bookmark_popover()

    def _clear_all_bookmarks(self, button=None):
        """Clear all bookmarks and update the UI."""
        self.bookmarks = []
        self.save_json(BOOKMARKS_FILE, self.bookmarks)
        self.update_bookmarks_menu(self.bookmark_menu)
        self._close_bookmark_popover()

    def _close_bookmark_popover(self):
        """Helper to close the bookmarks popover."""
        if hasattr(self, 'bookmark_popover') and self.bookmark_popover:
            self.bookmark_popover.popdown()
    
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
        save_button.connect("clicked", lambda btn: self.settings_dialog.emit("response", Gtk.ResponseType.OK))
        cancel_button.connect("clicked", lambda btn: self.settings_dialog.emit("response", Gtk.ResponseType.CANCEL))
        self.settings_dialog.connect("response", self.on_settings_dialog_response)       
        self.settings_dialog.present()

    def on_add_bookmark_clicked(self, button):
        """Handle Add Bookmark button click."""
        current_webview = self.get_current_webview()
        if current_webview:
            url = current_webview.get_uri()
            if url:
                favicon = None
                with self.favicon_lock:
                    if url in self.favicon_cache:
                        favicon = self.favicon_cache[url]
                if favicon is not None:
                    self.add_bookmark(url, favicon=favicon)
                else:
                    self.add_bookmark(url)
                    if self.debug_mode:
                        self.show_info_message(f"DEBUG: Bookmark added, fetching favicon asynchronously for {url}")

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
        self.adblocker.enabled = self.adblock_toggle.get_active()
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
    
    def _process_favicon(self, icon):
        """Process a favicon into a base64-encoded string.      
        Args:
            icon: The favicon to process (can be None, str, GdkPixbuf, or bytes)           
        Returns:
            str or None: Base64-encoded string of the favicon, or None if processing fails
        """
        if icon is None:
            return None
        if isinstance(icon, str):
            return icon
        if hasattr(icon, 'save_to_bufferv'):
            return pixbuf_to_base64(icon)
        if isinstance(icon, bytes):
            try:
                return base64.b64encode(icon).decode('utf-8')
            except Exception as e:
                if hasattr(self, 'debug_mode') and self.debug_mode:
                    self.show_info_message(f"DEBUG: Error encoding bytes to base64: {e}")
                return None
        return None
        
    def add_bookmark(self, url, title=None, favicon=None):
        """Add or update a bookmark.    
        Args:
            url: The URL to bookmark
            title: Optional title for the bookmark (will use page title if None)
            favicon: Optional favicon for the bookmark           
        Returns:
            bool: True if bookmark was added/updated, False otherwise
        """
        if not hasattr(self, 'bookmarks'):
            self.bookmarks = []
        
        # Ensure bookmarks is a list
        if not isinstance(self.bookmarks, list):
            self.bookmarks = []
        
        # Clean up the bookmarks list to ensure all entries are dictionaries
        cleaned_bookmarks = []
        for item in self.bookmarks:
            if isinstance(item, dict) and 'url' in item:
                cleaned_bookmarks.append(item)
            elif isinstance(item, str):
                cleaned_bookmarks.append({'url': item, 'title': item, 'favicon': None})
        self.bookmarks = cleaned_bookmarks
        
        # Process favicon if provided
        favicon_data = self._process_favicon(favicon) if favicon else None
        
        # Check if bookmark already exists and update it
        for i, bookmark in enumerate(self.bookmarks):
            if isinstance(bookmark, dict) and bookmark.get('url') == url:
                if title is not None:
                    self.bookmarks[i]['title'] = title
                if favicon_data is not None:
                    self.bookmarks[i]['favicon'] = favicon_data
                    if hasattr(self, 'debug_mode') and self.debug_mode:
                        self.show_info_message(f"DEBUG: Updated favicon for bookmark: {url}")
                self.save_json(BOOKMARKS_FILE, self.bookmarks)
                GLib.idle_add(self.update_bookmarks_menu, self.bookmark_menu)
                return True
        
        # If we get here, it's a new bookmark
        if title is None:
            webview = self.get_current_webview()
            title = webview.get_title() if webview else url
        
        # Add the new bookmark
        self.bookmarks.append({
            'url': url,
            'title': title,
            'favicon': favicon_data
        })       
        
        if hasattr(self, 'debug_mode') and self.debug_mode:
            self.show_info_message(f"DEBUG: Added new bookmark for {url}")
        
        self.save_json(BOOKMARKS_FILE, self.bookmarks)
        GLib.idle_add(self.update_bookmarks_menu, self.bookmark_menu)    
        
        # If favicon wasn't provided, try to get it asynchronously
        if favicon is None and hasattr(self, 'get_favicon'):
            def update_favicon():
                try:
                    favicon = self.get_favicon(url)
                    if favicon:
                        self.add_bookmark(url, title, favicon)
                except Exception as e:
                    if hasattr(self, 'debug_mode') and self.debug_mode:
                        self.show_info_message(f"DEBUG: Error updating favicon: {e}")
            
            # Run favicon update in a separate thread to avoid blocking
            threading.Thread(target=update_favicon, daemon=True).start()
        
        return True

    def update_history(self, url):
        """Add URL to browser history."""
        if url and url.startswith(("http://", "https://")):
            self.history.append({"url": url, "timestamp": time.time()})
            self.history = self.history[-HISTORY_LIMIT:]
            self.save_json(HISTORY_FILE, self.history)

    def load_json(self, filename: str, default=None) -> Union[dict, list]:
        """Load and parse JSON data from a file.   
        Args:
            filename: Path to the JSON file to load.
            default: Default value to return if file doesn't exist or is invalid.
                   Defaults to an empty dict if not specified.
        Returns:
            Parsed JSON data (dict or list), or default value if file doesn't exist or is invalid.
        """
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {} if default is None else default

    def save_json(self, filename, data):
        """Save JSON data to file."""
        with open(filename, "w") as f:
            json.dump(data, f)

    def show_error_message(self, message):
        """Display an error message dialog."""
        logging.basicConfig(
            level=logging.DEBUG if self.debug_mode else logging.ERROR,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            filename='shadow_browser.log',
            filemode='w'
        )

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

    def _load_texture_from_file(self, filepath):
        """Load an image file into a Gdk.Texture.
        Args:
        filepath (str): Path to the image file   
        Returns:
            Gdk.Texture: The loaded texture, or None if loading failed
        """
        try:
            return Gdk.Texture.new_from_filename(filepath)
        except Exception as e:
            print(f"Error loading texture from {filepath}: {e}")
            return None
            
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
        if os.path.exists(image_path):
            texture = self._load_texture_from_file(image_path)
            if texture:
                about.set_logo(texture)
            else:
                about.set_logo_icon_name("web-browser")
        else:
            about.set_logo_icon_name("web-browser")
        about.present()

    def on_back_clicked(self, button):
        """Handle back button click."""
        webview = self.get_current_webview()
        if webview and webview.can_go_back():
            webview.go_back()

    def on_screenshot_clicked(self, button):
        """Handle screenshot button click."""
        self.show_info_message("Screenshot functionality is currently disabled.")

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
        label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title_label = Gtk.Label(label=self.extract_tab_title(url))
        label_box.append(title_label)
        close_button = Gtk.Button.new_from_icon_name("window-close")
        close_button.set_size_request(24, 24)
        close_button.set_tooltip_text("Close tab")
        close_button.add_css_class("flat")
        title_label.add_css_class("tab-label")
        tab = Tab(url, webview, scrolled_window)
        tab.label_widget = title_label
        tab.label_box = label_box
        tab.close_button = close_button

        def on_close_clicked(button, tab=tab):
            if tab in self.tabs:
                tab_index = self.tabs.index(tab)
                self.on_tab_close_clicked(button, tab_index)
        label_box.append(close_button)
        close_button.connect("clicked", on_close_clicked)
        webview.connect("notify::favicon", self._on_favicon_changed)
        if not self.notebook:
            return
        index = self.notebook.append_page(scrolled_window, label_box)
        self.notebook.set_current_page(index)
        self.tabs.append(tab)
        webview.connect("load-changed", self.on_load_changed)
        webview.connect("notify::title", self.on_title_changed)
        webview.connect("decide-policy", self.on_decide_policy)

    def _page_contains_webview(self, page, webview):
        """Check if the given page contains the specified webview.       
        Args:
            page: The notebook page to check
            webview: The webview to look for
        Returns:
            bool: True if the page contains the webview, False otherwise
        """
        if not page or not webview:
            return False
        scrolled_window = page.get_child()
        if not scrolled_window:
            return False
        viewport = scrolled_window.get_child()
        if not viewport:
            return False
        child = viewport.get_first_child()
        return child == webview

    def on_tab_close_clicked(self, button, tab_index):
        """Close the tab at the given index."""
        if not (0 <= tab_index < len(self.tabs)):
            return
        tab = self.tabs.pop(tab_index)
        webview = getattr(tab, "webview", None)
        if not webview:
            return
        page_index = next(
            (i for i in range(self.notebook.get_n_pages())
            if self._page_contains_webview(self.notebook.get_nth_page(i), webview)),
            None
        )
        for handler in (
            self.on_load_changed,
            self.on_title_changed,
            self.on_decide_policy,
            self.on_webview_create,
        ):
            try:
                webview.disconnect_by_func(handler)
            except Exception:
                pass
        if page_index is not None:
            self.notebook.remove_page(page_index)
        if hasattr(tab, "webview"):
            tab.webview = None
        if hasattr(tab, "label_widget"):
            tab.label_widget = None

    def on_load_changed(self, webview, load_event):
        """Handle load state changes."""
        from gi.repository import WebKit, GLib        
        if not hasattr(self, 'download_spinner') or not self.download_spinner:
            return            
        current_webview = self.get_current_webview()
        current_url = webview.get_uri() or ""        
        if any(ext in current_url.lower() for ext in ['.mp4', '.webm', '.m3u8', '.mpd', '.m3u', '.mp3', '.ogg', '.m4a', '.m4v']):
            return           
        if load_event == WebKit.LoadEvent.COMMITTED:
            if webview == current_webview:
                if hasattr(self, 'url_entry') and self.url_entry:
                    self.url_entry.set_text(current_url)
                    for tab in self.tabs:
                        if tab.webview == webview:
                            tab.url = current_url
                            if tab.label_widget and not webview.get_title():
                                tab.label_widget.set_text(self.extract_tab_title(current_url))
                            break
                if not any(ext in current_url.lower() for ext in ['.mp4', '.webm', '.m3u8', '.mpd', '.m3u', '.mp3', '.ogg', '.m4a', '.m4v']):
                    GLib.idle_add(self.download_spinner.start)
                    GLib.idle_add(lambda: self.download_spinner.set_visible(True))                   
        elif load_event == WebKit.LoadEvent.FINISHED:
            if hasattr(self, 'url_entry') and self.url_entry and webview == current_webview:
                self.url_entry.set_text(current_url)               
            for tab in self.tabs:
                if tab.webview == webview:
                    tab.url = current_url
                    if tab.label_widget and not webview.get_title():
                        tab.label_widget.set_text(self.extract_tab_title(current_url))
                    break                   
            if not any(ext in current_url.lower() for ext in ['.mp4', '.webm', '.m3u8', '.mpd', '.m3u', '.mp3', '.ogg', '.m4a', '.m4v']):
                GLib.idle_add(self.download_spinner.stop)
                GLib.idle_add(lambda: self.download_spinner.set_visible(False))              
            if current_url and not current_url.startswith(('about:', 'data:')):
                self.update_history(current_url)

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
                favicon = self.get_favicon(url)
                if favicon:
                    GLib.idle_add(self._update_tab_favicon, tab, favicon)
                    self._update_bookmark_favicon(url, favicon)
            except Exception:
                pass
        tab._favicon_thread = threading.Thread(target=update_favicon, daemon=True)
        tab._favicon_thread.start()
        
    def _on_favicon_changed(self, webview, param):
        """Handle favicon changes from WebView.       
        Args:
            webview: The WebView that triggered the favicon change
            param: The GParamSpec of the changed property (unused)
        """
        if not webview or not hasattr(webview, 'get_uri'):
            return
        url = webview.get_uri()
        if not url:
            return
        favicon = webview.get_favicon()
        if not favicon:
            return
        with self.favicon_lock:
            self.favicon_cache[url] = favicon
        current_tab = next((t for t in getattr(self, 'tabs', []) if t.webview == webview), None)
        if current_tab:
            GLib.idle_add(self._update_tab_favicon, current_tab, favicon)
        if hasattr(self, 'bookmarks'):
            self._update_bookmark_favicon(url, favicon)

    def _update_bookmark_favicon(self, url, favicon):
        """Update the favicon for a bookmarked URL.
        
        Args:
            url: The URL of the bookmark
            favicon: The favicon to save (can be Gdk.Texture, Gdk.Paintable, bytes, or GLib.Bytes)
        """
        if not url or not favicon or not hasattr(self, 'bookmarks'):
            return
        try:
            if hasattr(favicon, 'save_to_png_bytes'):
                bytes_data = favicon.save_to_png_bytes()
            elif hasattr(favicon, 'get_paintable'):
                paintable = favicon.get_paintable()
                if paintable and hasattr(paintable, 'save_to_png_bytes'):
                    bytes_data = paintable.save_to_png_bytes()
                else:
                    return
            elif isinstance(favicon, GLib.Bytes):
                bytes_data = favicon.get_data()
            else:
                bytes_data = favicon
            if not bytes_data:
                return
            if hasattr(bytes_data, 'get_data'):
                bytes_data = bytes(bytes_data.get_data())
            elif hasattr(bytes_data, 'tobytes'):
                bytes_data = bytes_data.tobytes()
            elif not isinstance(bytes_data, (bytes, bytearray)):
                try:
                    bytes_data = bytes(bytes_data)
                except (TypeError, ValueError):
                    if hasattr(self, 'debug_mode') and self.debug_mode:
                        print(f"Warning: Could not convert favicon data to bytes for {url}")
                    return
            base64_data = base64.b64encode(bytes_data).decode('utf-8')
            if not base64_data:
                return
            updated = False
            for i, bookmark in enumerate(self.bookmarks):
                if isinstance(bookmark, dict) and bookmark.get('url') == url:
                    self.bookmarks[i]['favicon'] = base64_data
                    updated = True
                    break
            if updated:
                self.save_json(BOOKMARKS_FILE, self.bookmarks)
                if hasattr(self, 'bookmark_menu'):
                    GLib.idle_add(self.update_bookmarks_menu, self.bookmark_menu)
        except Exception as e:
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"Error updating bookmark favicon: {e}")
                import traceback
                traceback.print_exc()

    def _create_texture_from_bytes(self, data, width, height, has_alpha=True):
        """
        Create a Gdk.Texture from raw pixel data (GTK 4), handling stride correctly.
        """
        from gi.repository import Gdk, GLib
        try:
            if hasattr(data, "get_data"):  # GLib.Bytes
                data = data.get_data()
            elif not isinstance(data, (bytes, bytearray)):
                data = bytes(data)
            bpp = 4 if has_alpha else 3
            expected_stride = width * bpp
            expected_total = expected_stride * height
            if len(data) != expected_total:
                row_len = len(data) // height if height > 0 else 0
                if row_len != expected_stride and row_len > 0:
                    fixed = bytearray()
                    for y in range(height):
                        start = y * row_len
                        row = data[start : start + row_len]
                        row = row.ljust(expected_stride, b"\x00")
                        fixed.extend(row)
                    data = bytes(fixed)
                else:
                    data = data.ljust(expected_total, b"\x00")
            gbytes = GLib.Bytes.new(data)
            fmt = (
                Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED
                if has_alpha
                else Gdk.MemoryFormat.R8G8B8
            )
            texture = Gdk.MemoryTexture.new(width, height, fmt, gbytes, expected_stride)
            return texture
        except Exception as e:
            if getattr(self, "debug_mode", False):
                print(f"Error in _create_texture_from_bytes: {e}")
                import traceback
                traceback.print_exc()
            return None

    def _update_tab_favicon(self, tab, favicon):
        """Update the favicon in the tab header (GTK 4, uses Gdk.Texture only)."""
        from gi.repository import Gtk, Gdk, GLib
        if not favicon or not hasattr(tab, "label_box"):
            return
        try:
            if not hasattr(tab, "favicon_widget") or not tab.favicon_widget:
                tab.favicon_widget = Gtk.Image()
                tab.favicon_widget.set_size_request(16, 16)
                tab.label_box.prepend(tab.favicon_widget)
                tab.favicon_widget.set_visible(True)
            texture = None
            if isinstance(favicon, Gdk.Texture):
                texture = favicon
            elif isinstance(favicon, Gdk.Paintable):
                tab.favicon_widget.set_from_paintable(favicon)
                tab.favicon = favicon
                tab.favicon_widget.set_visible(True)
                return
            elif hasattr(favicon, "get_pixels") and hasattr(favicon, "get_width"):
                try:
                    width, height = favicon.get_width(), favicon.get_height()
                    stride = favicon.get_rowstride()
                    pixel_data = favicon.get_pixels()
                    gbytes = GLib.Bytes.new(pixel_data)
                    fmt = Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED
                    texture = Gdk.MemoryTexture.new(width, height, fmt, gbytes, stride)
                except Exception as e:
                    if getattr(self, "debug_mode", False):
                        print(f"Failed to convert pixbuf to texture: {e}")
            elif isinstance(favicon, (bytes, bytearray, GLib.Bytes)):
                data = favicon.get_data() if hasattr(favicon, "get_data") else favicon
                try:
                    gbytes = GLib.Bytes.new(data)
                    texture = Gdk.Texture.new_from_bytes(gbytes)
                except Exception:
                    try:
                        size = int((len(data) // 4) ** 0.5) or 16
                        texture = self._create_texture_from_bytes(data, size, size, has_alpha=True)
                    except Exception as e:
                        if getattr(self, "debug_mode", False):
                            print(f"Error creating texture from raw bytes: {e}")
            if not texture:
                if getattr(self, "debug_mode", False):
                    print("Favicon texture creation failed — using fallback icon")
                try:
                    icon_theme = Gtk.IconTheme.get_for_display(self.get_display())
                    paintable = icon_theme.lookup_icon(
                        "web-browser-symbolic",
                        None,
                        16,
                        self.get_scale_factor(),
                        Gtk.TextDirection.NONE,
                        Gtk.IconLookupFlags.FORCE_SYMBOLIC,
                    )
                    if paintable:
                        tab.favicon_widget.set_from_paintable(paintable)
                        tab.favicon = paintable
                    return
                except Exception:
                    return
            if texture:
                w, h = texture.get_width(), texture.get_height()
                if w > 16 or h > 16:
                    scale = min(16 / w, 16 / h)
                    w, h = int(w * scale), int(h * scale)
                    texture = texture.scale_simple(w, h)
                tab.favicon_widget.set_from_paintable(texture)
                tab.favicon = texture
                tab.favicon_widget.set_visible(True)
        except Exception as e:
            if getattr(self, "debug_mode", False):
                print(f"Error in _update_tab_favicon: {e}")
                import traceback
                traceback.print_exc()

    def on_webview_key_press(self, controller, keyval, keycode, state):
        """Keyboard shortcuts to open the Web Inspector: F12 or Ctrl+Shift+I.      
        GTK4 EventControllerKey signal handler.
        Args:
            controller: The EventControllerKey instance
            keyval: The key value (Gdk.KEY_*)
            keycode: The hardware keycode
            state: Modifier state (Gdk.ModifierType)
        """
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        webview = controller.get_widget()
        if keyval == Gdk.KEY_F12:
            insp = webview.get_inspector() if hasattr(webview, 'get_inspector') else None
            if insp and hasattr(insp, 'show'):
                insp.show()
                return True
        if ctrl and shift and keyval in (Gdk.KEY_i, Gdk.KEY_I):
            insp = webview.get_inspector() if hasattr(webview, 'get_inspector') else None
            if insp and hasattr(insp, 'show'):
                insp.show()
                return True

    def on_webview_context_menu(self, webview, context_menu, hit_test_result):
        """
        Add 'Inspect Element' to the WebKit context menu if developer extras are enabled.
        Args:
            webview: The WebKit.WebView that received the signal.
            context_menu: The WebKit.ContextMenu to be displayed.
            hit_test_result: WebKit.HitTestResult with context about the clicked location.
        """
        settings = getattr(webview, "get_settings", lambda: None)()
        dev_enabled = False
        if settings:
            get_extras = getattr(settings, "get_enable_developer_extras", None)
            if callable(get_extras):
                dev_enabled = bool(get_extras())
            elif hasattr(settings, "get_property"):
                dev_enabled = bool(settings.get_property("enable-developer-extras"))
        if not dev_enabled:
            return False
        if hasattr(WebKit, "ContextMenuItem") and hasattr(WebKit, "ContextMenuAction"):
            item = WebKit.ContextMenuItem.new_from_stock_action(
                WebKit.ContextMenuAction.INSPECT_ELEMENT
            )
            if item and hasattr(context_menu, "append"):
                context_menu.append(item)
                return False

            def _activate_inspect(_item):
                inspector = getattr(webview, "get_inspector", lambda: None)()
                if inspector and hasattr(inspector, "show"):
                    inspector.show()
                    return
                try:
                    js = (
                        "(function(){"
                        "if (window.webkit && window.webkit.messageHandlers) {"
                        "    console.log('WebKit message handlers available');"
                        "}"
                        "})();"
                    )
                    webview.run_javascript(js, None, None, None)
                except Exception as e:
                    print(f"[WARNING] Failed to check WebKit message handlers: {e}")
            if hasattr(WebKit, "ContextMenuItem"):
                item = WebKit.ContextMenuItem.new_from_stock_action_with_label(
                    WebKit.ContextMenuAction.NO_ACTION, "Inspect Element"
                )
                if hasattr(item, "connect"):
                    item.connect("activate", _activate_inspect)
                if hasattr(context_menu, "append"):
                    context_menu.append(item)
        return False
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
        """
        Handle navigation policy decisions for new page loads or link clicks.
        Filters unsafe or unwanted URLs, runs optional cleanup JS, and decides whether to allow navigation.
        """
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
        lower_url = requested_url.lower()
        if lower_url.startswith("javascript:"):
            decision.ignore()
            return True
        is_main_frame = True
        try:
            frame = getattr(navigation_action, "get_frame", lambda: None)()
            if frame and hasattr(frame, "is_main_frame"):
                is_main_frame = frame.is_main_frame()
        except Exception:
            pass
        if self.is_internal_url_blocked(requested_url, is_main_frame):
            decision.ignore()
            return True
        if lower_url.startswith(("about:", "data:", "blob:", "_data:", "_blank", "_parent", "_self", "_top", "_window")):
            if not is_main_frame:
                decision.ignore()
                return True
            decision.use()
            return True
        parsed = urlparse(requested_url)
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            decision.ignore()
            return True
        if not is_main_frame:
            top_url = webview.get_uri()
            if top_url:
                top_host = urlparse(top_url).hostname
                req_host = parsed.hostname
                if top_host and req_host and top_host != req_host:
                    decision.ignore()
                    return True
        if self.adblocker.is_blocked(requested_url):
            decision.ignore()
            return True
        if lower_url.endswith(tuple(DOWNLOAD_EXTENSIONS)):
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
            webview.evaluate_javascript(cleanup_js, -1, None, None, None, None, None, None)
        except Exception:
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
        if url.lower() in ["about:blank", "javascript:void(0)"]:
            decision.ignore()
            return True
        if url.lower().endswith(tuple(DOWNLOAD_EXTENSIONS)):
            self.start_manual_download(url)
            decision.ignore()
            return True
        new_webview = self.create_secure_webview()
        if new_webview is None:
            decision.ignore()
            return True
        self.add_webview_to_tab(new_webview)
        new_webview.load_uri(url)           
        decision.ignore()
        return True            

    def on_decide_policy(self, webview, decision, decision_type):
        """Handle navigation decisions, including JavaScript links and new window actions."""
        if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            navigation_action = decision.get_navigation_action()
            if not navigation_action:
                decision.ignore()
                return True                   
            request = navigation_action.get_request()
            if not request:
                decision.ignore()
                return True                   
            uri = request.get_uri()
            if uri and uri.strip().lower().startswith('javascript:'):
                js_uri = 'javascript:' + uri.split(':', 1)[1].lstrip()
                self.open_url_in_new_tab(js_uri)
                decision.ignore()
                return True
            if uri in ["about:blank#blocked", "about:blank"]:
                decision.use()
                return True
            return self._handle_navigation_action(webview, decision, navigation_action)                
        elif decision_type == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            return self._handle_new_window_action(webview, decision)                
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
            progress_info = {}
            try:
                parsed_url = urlparse(url)
                if not parsed_url.scheme or not parsed_url.netloc:
                    raise ValueError("Invalid URL format")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
                with requests.get(url, stream=True, timeout=30, headers=headers) as response:
                    response.raise_for_status()
                    content_disposition = response.headers.get("content-disposition", "")
                    filename = None
                    if content_disposition:
                        filename_match = re.search(r'filename[^;=]*=([^;\n]*)', content_disposition, flags=re.IGNORECASE)
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
                    downloads_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~/Downloads")
                    os.makedirs(downloads_dir, exist_ok=True)
                    base_name, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(os.path.join(downloads_dir, filename)):
                        filename = f"{base_name}_{counter}{ext}"
                        counter += 1
                    filepath = os.path.join(downloads_dir, filename)
                    total_size = int(response.headers.get("content-length", 0))
                    progress_info = {
                        "filename": filename,
                        "total_size": total_size,
                        "cancelled": False,
                    }
                    self.download_manager.add_progress_bar(progress_info)
                    with open(filepath, "wb") as f:
                        downloaded = 0
                        for chunk in response.iter_content(chunk_size=8192):
                            if progress_info["cancelled"]:
                                break
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                progress = downloaded / total_size if total_size > 0 else 0
                                GLib.idle_add(self.download_manager.update_progress, progress_info, progress)
                    if not progress_info["cancelled"]:
                        GLib.idle_add(self.download_manager.download_finished, progress_info)
            except requests.exceptions.RequestException as e:
                GLib.idle_add(self.download_manager.download_failed, progress_info, f"Download request failed: {e}")
            except Exception as e:
                GLib.idle_add(self.download_manager.download_failed, progress_info, f"Unexpected download error: {e}")
            finally:
                if progress_info:
                    GLib.idle_add(self.download_manager.cleanup_download, progress_info["filename"])
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

    def safe_window_cleanup(self):
        """Ensure proper window cleanup to prevent GTK warnings."""
        if not hasattr(self, 'window') or not self.window:
            return  
        if hasattr(self.window, 'disconnect_by_func'):
            self.window.disconnect_by_func(self.on_window_destroy)
        if hasattr(self.window, 'get_child'):
            child = self.window.get_child()
            if child and hasattr(child, 'destroy'):
                    if hasattr(child, 'remove'):
                        self.window.remove(child)
                    child.destroy()
        if hasattr(self.window, 'destroy'):
            self.window.destroy()
        self.window = None

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
        self.quit()

    def simulate_left_click_on_void_link(self, data_url):
        try:
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
                webview.run_javascript(js_code, None, None, None)
        except Exception as e:
            print(f"Error simulating click: {e}")
            
        def js_callback(self, webview, result):
            try:
                if result is None:
                    return
                webview.run_javascript_finish(result)
            except Exception:
                pass

    def test_js_execution(self):
        webview = self.get_current_webview()
        if webview:
            js_code = "console.log('Test JS execution in webview'); 'JS executed';"
            webview.run_javascript(js_code, None, self.js_callback, None)

    def _on_js_executed(self, webview, result, user_data):
        """Callback after JavaScript execution completes."""
        webview.run_javascript_finish(result)

    def _inject_jquery(self, webview):
        """Inject jQuery if not already loaded."""
        jquery_check = """
        (function() {
            if (typeof jQuery == 'undefined') {
                var script = document.createElement('script');
                script.src = 'https://code.jquery.com/jquery-3.6.0.min.js';
                script.integrity = 'sha256-/xUj+3OJU5yExlq6GSYGSHk7tPXikynS7ogEvDej/m4=';
                script.crossOrigin = 'anonymous';
                                var onclick = $this.attr('onclick');
                                if (onclick) {
                                    $this.off('click').on('click', function(e) {
                                        try {
                                            return eval(onclick);
                                        } catch (e) {
                                            console.error('Error in click handler:', e);
                                            return true;
                                        }
                                    });
                                } catch (err) {
                                    console.log('Error executing click handler:', err);
                                }
                            }
                        });
                    });
                };
                document.head.appendChild(script);
                return true;
            }
            return false;
        })();
        """
        webview.evaluate_javascript(jquery_check, -1, None, None, None, None, None, None)

    def _handle_javascript_errors(self, webview):
        """Handle JavaScript errors in the web view."""
        try:
            error_handler = """
                window.onerror = function(msg, url, line, col, error) {
                    console.error("Error: " + msg + " at " + url + ":" + line + ":" + col);
                    return false;
                };
                true;
            """
            webview.evaluate_javascript(
                error_handler,
                -1,
                None,
                None,
                None,
                None,
                None,
                None
            )
        except Exception as e:
            if self.debug_mode:
                print(f"Error setting up JavaScript error handler: {e}")

    def _on_webview_load_changed(self, webview, load_event):
        """Handle web view load changes."""
        try:
            if load_event == WebKit.LoadEvent.FINISHED:
                self._update_loading_state(webview, False)
                # Skip error handler and jQuery injection for now to prevent recursion
                # self._handle_javascript_errors(webview)
                # self._inject_jquery(webview)
                pass
                
                def check_again():
                    check_script = """
                    (function() {
                        var issues = [];
                        if (typeof $ === 'undefined' && !document.querySelector('script[src*="jquery"]')) {
                            issues.push('jQuery not loaded');
                        }                       
                        var brokenHandlers = [];
                        document.querySelectorAll('a[onclick*="javascript:"]').forEach(function(el) {
                            try {
                                var onclick = el.getAttribute('onclick');
                                if (onclick && onclick.includes('javascript:')) {
                                    var testFn = new Function('event', onclick.replace('javascript:', ''));
                                    testFn.call(el, {preventDefault: function(){}});
                                }
                            } catch (e) {
                                brokenHandlers.push(e.message);
                            }
                        });
                        
                        if (brokenHandlers.length > 0) {
                            issues.push('Found ' + brokenHandlers.length + ' broken click handlers');
                        }
                        
                        return {
                            hasIssues: issues.length > 0,
                            issues: issues,
                            brokenHandlers: brokenHandlers
                        };
                    })();
                    """
                    webview.evaluate_javascript(
                        check_script,
                        -1,
                        None,
                        None,
                        None,
                        None,
                        lambda w, r: self._handle_js_issues(webview, r, None),
                        None
                    )               
                GLib.timeout_add(1000, check_again)                
        except Exception as e:
            if self.debug_mode:
                print(f"Error in load changed handler: {e}")

    def _handle_js_issues(self, webview, result, user_data=None):
        """Handle JavaScript issues found during page load."""
        try:
            if result and hasattr(result, 'get_js_value'):
                js_value = result.get_js_value()
                if js_value and hasattr(js_value, 'to_json'):
                    issues = js_value.to_json(-1)
                    if issues and isinstance(issues, dict) and issues.get('hasIssues'):
                        if self.debug_mode:
                            print(f"Detected JavaScript issues: {issues.get('issues', [])}")
                            if issues.get('brokenHandlers'):
                                print(f"Broken click handlers: {issues['brokenHandlers']}")
                                print('Attempting to fix broken click handlers...')
                                self._fix_broken_handlers(webview)
        except Exception as e:
            if self.debug_mode:
                print('Error handling JS issues:', str(e))

    def _fix_broken_handlers(self, webview):
        """Attempt to fix broken JavaScript click handlers."""
        fix_script = """
        (function() {
            var fixed = 0;
            document.querySelectorAll('a[onclick]').forEach(function(el) {
                try {
                    var onclick = el.getAttribute('onclick');
                    if (onclick) {
                        el.removeAttribute('onclick');
                        el.addEventListener('click', function(e) {
                            try {
                                return eval(onclick);
                            } catch (e) {
                                console.error('Error in fixed click handler:', e);
                                return true;
                            }
                        });
                        fixed++;
                    }
                } catch (e) {
                    console.error('Error fixing click handler:', e);
                }
            });
            return fixed;
        })();
        """
        webview.run_javascript(
            fix_script,
            -1,
            None,
            None,
            None,
            lambda w, r: print(f"Fixed {r.get_js_value().to_int32()} click handlers") if self.debug_mode else None,
            None
        )
        fix_script = """
        (function() {
            document.querySelectorAll('a[onclick*="javascript:"]').forEach(function(el) {
                var onclick = el.getAttribute('onclick');
                if (onclick && onclick.includes('javascript:')) {
                    el.setAttribute('data-original-onclick', onclick);
                    el.removeAttribute('onclick');
                    
                    el.addEventListener('click', function(e) {
                        e.preventDefault();
                        try {
                            var code = this.getAttribute('data-original-onclick')
                                .replace('javascript:', '');
                            (new Function(code)).call(this, e);
                        } catch (err) {
                            console.log('Error in fixed click handler:', err);
                        }
                        return false;
                    });
                }
            });
            return 'Fixed ' + document.querySelectorAll('[data-original-onclick]').length + ' click handlers';
        })();
        """
        webview.run_javascript(fix_script, None, 
            lambda w, r, d: print('Fixed click handlers') if self.debug_mode else None, 
            None
        )
            
    def _update_loading_state(self, webview, loading):
        """Update the UI to reflect the loading state.       
        Args:
            webview: The WebView that changed its loading state
            loading: Boolean indicating if the view is loading
        """
        if hasattr(self, 'statusbar') and self.statusbar:
            self.statusbar.set_visible(loading)
            if loading:
                self.statusbar.push(0, "Loading...")       
        if hasattr(self, 'refresh_button') and self.refresh_button:
            self.refresh_button.set_sensitive(not loading)
            
    def _on_webview_loaded(self, webview, load_event, js_code=None):
        """Handler for webview load events to execute JavaScript after page load."""
        if load_event == WebKit.LoadEvent.FINISHED and js_code:
            webview.run_javascript(
                """
                (function() {
                    return document.readyState === 'complete' || document.readyState === 'interactive';
                })();
                """,
                -1,
                None,
                None,
                None,
                lambda w, r: self._on_dom_ready(webview, r, js_code) if js_code else None
            )
        return False

    def detect_environment(self):
        """Detect if we're running in a sandboxed environment."""
        try:
            if os.path.exists('/.flatpak-info'):
                return 'flatpak'
            if 'SNAP' in os.environ:
                return 'snap'
            if os.path.exists('/proc/self/exe') and 'appimage' in os.readlink('/proc/self/exe'):
                return 'appimage'
            return 'native'
        except (OSError, FileNotFoundError, PermissionError):
            return 'unknown'

    def check_file_access(self, file_path):
        """Check if file is accessible in the current environment."""
        try:
            with open(file_path, 'rb') as f:
                f.read(1)
            return True
        except (IOError, OSError, PermissionError):
            return False

    def _execute_js_after_ready(self, webview, result, js_code):
        """Execute JavaScript after ensuring the page is fully ready."""
        try:
            js_result = webview.run_javascript_finish(result)
            if js_result and hasattr(js_result, 'get_js_value'):
                webview.run_javascript(
                js_code,
                -1,
                None,
                None,
                None,
                self._on_js_executed,
                None
            )
        except Exception as e:
            if self.debug_mode:
                print(f"Error executing JavaScript: {e}")

    def open_url_in_new_tab(self, url, execute_js=True):
        """Open a URL in a new tab.       
        Args:
            url: The URL to open
            execute_js: If True and URL is JavaScript, execute it in the new tab
        """
        if not url or not isinstance(url, str):
            return       
        if url.startswith('file://'):
            from urllib.parse import unquote, urlparse, quote
            from pathlib import Path
            import base64
            parsed = urlparse(url)
            file_path = unquote(parsed.path)
            abs_path = Path(file_path).resolve()
            if not abs_path.exists():
                return
            if not abs_path.is_file():
                return
            file_accessible = self.check_file_access(str(abs_path))
            if not file_accessible:
                with open(abs_path, 'rb') as f:
                    content = f.read()
                import mimetypes
                mime_type, _ = mimetypes.guess_type(str(abs_path))
                if mime_type is None:
                    mime_type = 'text/html' if abs_path.suffix.lower() in ['.html', '.htm'] else 'text/plain'                        
                encoded_content = base64.b64encode(content).decode('utf-8')
                url = f"data:{mime_type};base64,{encoded_content}"
            else:
                url = f"file://{quote(str(abs_path))}"                   
        elif os.path.exists(url):
            from urllib.parse import quote
            from pathlib import Path
            import base64
            file_path = url
            abs_path = Path(file_path).resolve()
            if not abs_path.exists():
                return
            if not abs_path.is_file():
                return
            file_accessible = self.check_file_access(str(abs_path))
            if not file_accessible:
                import mimetypes
                mime_type, _ = mimetypes.guess_type(str(abs_path))
                if mime_type is None:
                    mime_type = 'text/html' if abs_path.suffix.lower() in ['.html', '.htm'] else 'text/plain'
            try:
                with open(abs_path, 'rb') as f:
                    content = f.read()
                import mimetypes
                mime_type, _ = mimetypes.guess_type(str(abs_path))
                if mime_type is None:
                    mime_type = 'text/html' if abs_path.suffix.lower() in ['.html', '.htm'] else 'text/plain'                       
                encoded_content = base64.b64encode(content).decode('utf-8')
                url = f"data:{mime_type};base64,{encoded_content}"
            except Exception:
                return
            else:
                url = f"file://{quote(str(abs_path))}"           
        new_webview = self.create_secure_webview()
        if new_webview is None:
            return            
        new_webview.set_vexpand(True)
        new_webview.set_hexpand(True)           
        new_webview.connect('load-changed', self.on_load_changed)
        new_webview.connect('notify::title', self.on_title_changed)
        new_webview.connect('decide-policy', self.on_decide_policy)
        new_webview.connect('create', self.on_webview_create)           
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_child(new_webview)
        label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title_label = Gtk.Label(label=self.extract_tab_title(url))
        label_box.append(title_label)
        close_button = Gtk.Button.new_from_icon_name("window-close")
        close_button.set_size_request(24, 24)
        close_button.set_tooltip_text("Close tab")
        tab = Tab(url, new_webview, scrolled_window)
        tab.label_widget = title_label
        tab.label_box = label_box
        tab.close_button = close_button
        self.tabs.append(tab)

        def on_close_clicked(button, tab=tab):
            try:
                tab_index = self.tabs.index(tab)
                self.on_tab_close_clicked(button, tab_index)
            except ValueError:
                pass
        label_box.append(close_button)
        close_button.connect("clicked", on_close_clicked)
        new_webview.connect("notify::favicon", self._on_favicon_changed)
        index = self.notebook.append_page(scrolled_window, label_box)
        self.notebook.set_current_page(index)
        self.tabs.append(tab)
        if url.lower().startswith("javascript:"):
            if not execute_js:
                return
            js_code = url[11:].strip()
            if not js_code:
                return
            
            def on_js_webview_loaded(webview, load_event, js_code):
                if load_event == WebKit.LoadEvent.FINISHED:
                    webview.run_javascript(
                        "document.readyState === 'complete' || document.readyState === 'interactive' ? true : false",
                        -1, None, None, None, None, None, None
                    )
                    webview.disconnect_by_func(on_js_webview_loaded)               
            new_webview.connect('load-changed', on_js_webview_loaded, js_code)
            new_webview.load_uri("about:blank")
        else:
            new_webview.load_uri(url)
        self.notebook.set_visible(True)           

    def add_webview_to_tab(self, webview, is_terminal=False):
        """Add a webview to a new tab, initialize favicon and tab UI.     
        Args:
            webview: The WebKit.WebView to add to the tab
            is_terminal: Whether this is a terminal tab (default: False)
        """
        if is_terminal and not hasattr(webview, 'is_terminal'):
            webview.is_terminal = True
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_vexpand(True)
        scrolled_window.set_child(webview)
        label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title_text = "Terminal" if is_terminal else self.extract_tab_title(webview.get_uri() or "New Tab")
        title_label = Gtk.Label(label=title_text)
        label_box.append(title_label)
        close_button = Gtk.Button.new_from_icon_name("window-close")
        close_button.set_size_request(24, 24)
        close_button.add_css_class("flat")
        close_button.set_tooltip_text("Close tab")
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header_box.append(label_box)
        header_box.append(close_button)
        tab = Tab(webview.get_uri() or ("terminal" if is_terminal else ""), webview, scrolled_window)
        tab.title_label = title_label
        tab.close_button = close_button
        tab.header_box = header_box
        tab.is_terminal = is_terminal
        close_button.connect("clicked", self.on_tab_close_clicked, len(self.tabs))
        index = self.notebook.append_page(scrolled_window, header_box)
        self.tabs.append(tab)
        self.notebook.set_current_page(index)
        from gtk_compat import widget_show_all
        widget_show_all(header_box)
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
        vbox = Gtk.Box.new(Gtk.Orientation.VERTICAL, 0)
        vbox.set_ox(orientation=Gtk.Orientation.VERTICAL)
        if hasattr(webview, 'get_parent') and webview.get_parent() is not None:
            parent = webview.get_parent()
            if parent and hasattr(parent, "remove") and webview.get_parent() == parent:
                parent.remove(webview)
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
                    parent.remove(close_button)
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
                    meta.content = `script-src 'nonce-${nonce}' 'strict-dynamic' 'unsafe-inline'`;
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
        """
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
        # User script is not being used, so we'll remove it
        # If you need to use it later, uncomment and add to webview
        # user_script = WebKit.UserScript.new(
        #     script,
        #     WebKit.UserContentInjectedFrames.TOP_FRAME,
        #     WebKit.UserScriptInjectionTime.START,
        #     [], []
        # )
        # webview.add_script(user_script)

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
        session = None
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
                pool_maxsize=10,
                pool_block=False
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            session.timeout = 30
            if self.tor_enabled and hasattr(self, 'tor_manager'):
                pass  # ...optional tor proxy logic...
            return session            
        except Exception:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            raise
        session = None
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
                pool_maxsize=10,
                pool_block=False
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            session.timeout = 30
            if self.tor_enabled and hasattr(self, 'tor_manager'):
                try:
                    if not self.tor_manager.is_running():
                        self.tor_manager.start()                    
                    if self.tor_manager.is_running():
                        proxy_url = f'socks5h://127.0.0.1:{self.tor_manager.tor_port}'
                        session.proxies = {
                            'http': proxy_url,
                            'https': proxy_url
                        }
                        test_url = 'https://check.torproject.org/api/ip'
                        try:
                            response = session.get(test_url, timeout=15)
                            response.raise_for_status()
                            response_data = response.json()                           
                            if not response_data.get('IsTor', False):
                                
                                self.tor_enabled = False
                                session.proxies = {}                           
                        except Exception:
                            self.tor_enabled = False
                            session.proxies = {}
                    else:
                        self.tor_enabled = False
                        self.tor_enabled = False                       
                except Exception:
                    self.tor_enabled = False
                    session.proxies = {}           
            return session            
        except Exception:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            raise

    def initialize(self):
        import logging
        import traceback       
        logger = logging.getLogger('VAAPIManager')       
        if self._gst_initialized:
            logger.debug("GStreamer is already initialized")
            return True            
        try:
            logger.info("Initializing GStreamer...")
            
            if not self.Gst.init_check(None):
                error = self.Gst.init_get_error()
                if error:
                    logger.error(f"GStreamer initialization failed: {error[1]}")
                else:
                    logger.error("GStreamer initialization failed with unknown error")
                return False               
            logger.debug("GStreamer initialized successfully")           
            try:
                import gi
                gi.require_version('GstVa', '1.0')
                from gi.repository import GstVa as GstVaapi
                self.GstVaapi = GstVaapi
                logger.debug("GStreamer VA-API imported successfully")
            except (ImportError, ValueError) as e:
                logger.error(f"Failed to import GStreamer VA-API: {str(e)}")
                if self.debug_mode:
                    logger.debug(traceback.format_exc())
                return False           
            try:
                gi.require_version('GstVideo', '1.0')
                from gi.repository import GstVideo
                self.GstVideo = GstVideo
                logger.debug("GStreamer Video imported successfully")
            except (ImportError, ValueError) as e:
                logger.error(f"Failed to import GStreamer Video: {str(e)}")
                if self.debug_mode:
                    logger.debug(traceback.format_exc())
            required_plugins = [
                'playbin', 'h264parse', 'h265parse', 'videoconvert', 'audioconvert'
            ]
            vaapi_plugins = [
                'vaapih264dec', 'vaapih265dec', 'vaapisink'
            ]
            missing_plugins = []
            vaapi_missing = []

            registry = self.Gst.Registry.get()

            for plugin in required_plugins:
                try:
                    feature = registry.lookup_feature(plugin)
                    if not feature:
                        missing_plugins.append(plugin)
                except (TypeError, AttributeError):
                    try:
                        feature = registry.lookup_feature(plugin.encode('utf-8'))
                        if not feature:
                            missing_plugins.append(plugin)
                    except Exception as e:
                        logger.warning(f"Failed to check plugin {plugin}: {str(e)}")
                        missing_plugins.append(plugin)
            for plugin in vaapi_plugins:
                try:
                    feature = registry.lookup_feature(plugin)
                    if not feature:
                        vaapi_missing.append(plugin)
                except (TypeError, AttributeError):
                    try:
                        feature = registry.lookup_feature(plugin.encode('utf-8'))
                        if not feature:
                            vaapi_missing.append(plugin)
                    except Exception as e:
                        logger.debug(f"VA-API plugin {plugin} not found: {str(e)}")
                        vaapi_missing.append(plugin)
            if missing_plugins:
                logger.warning(f"Missing required GStreamer plugins: {', '.join(missing_plugins)}")
            if vaapi_missing:
                logger.info(f"VA-API plugins not found in registry: {', '.join(vaapi_missing)}")
                logger.info("This is normal - VA-API plugins are loaded dynamically based on hardware availability")
            else:
                logger.info("VA-API plugins found in registry")            
            if self.debug_mode:
                self.Gst.debug_set_active(True)
                self.Gst.debug_set_default_threshold(self.Gst.DebugLevel.WARNING)
                logger.info("GStreamer debug logging enabled")
            
            self._gst_initialized = True
            logger.info("GStreamer and VA-API initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Critical error initializing GStreamer: {str(e)}")
            if self.debug_mode:
                logger.debug(traceback.format_exc())
            return False

    def load_page(self):
        self.webview.load_uri(self.url)
        time.sleep(random.uniform(2, 5))

    def navigate_to(self, path):
        new_url = f"{self.url.rstrip('/')}/{path.lstrip('/')}"
        self.webview.load_uri(new_url)
        time.sleep(random.uniform(2, 5))

    def get_favicon_(self, url):
        """Get favicon for a given URL.        
        Args:
            url: The URL to get the favicon for            
        Returns:
            Gdk.Texture: The favicon texture, or None if not found
        """
        if not url:
            return None
        with self.favicon_lock:
            if url in self.favicon_cache:
                return self.favicon_cache[url]
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None
        favicon_url = f"{parsed.scheme}://{parsed.netloc.rstrip('/')}/favicon.ico"
        try:
            response = requests.get(favicon_url, timeout=5)
            if response.status_code == 200 and response.content:
                texture = self._texture_from_bytes(response.content)
                if texture:
                    with self.favicon_lock:
                        self.favicon_cache[url] = texture
                    return texture
        except Exception as e:
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"Error fetching favicon: {e}")
        return None

    def _get_favicon_cache_path(self, url):
        """Get the filesystem path for a cached favicon.       
        Args:
            url: The URL to get the cache path for           
        Returns:
            str: The filesystem path for the cached favicon
        """
        cache_dir = os.path.join(os.path.expanduser("~"), ".shadowbrowser", "favicons")
        os.makedirs(cache_dir, exist_ok=True)
        filename = hashlib.sha1(url.encode("utf-8")).hexdigest() + ".png"
        return os.path.join(cache_dir, filename)

    def _load_cached_favicon(self, url):
        """Load a favicon from the cache.        
        Args:
            url: The URL to load the favicon for            
        Returns:
            Gdk.Texture: The cached favicon texture, or None if not found
        """
        cache_path = self._get_favicon_cache_path(url)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    return self._texture_from_bytes(f.read())
            except Exception as e:
                if hasattr(self, 'debug_mode') and self.debug_mode:
                    print(f"Error loading cached favicon: {e}")
        return None

    def _texture_from_bytes(self, data):
        """Create a Gdk.Texture from raw or encoded image bytes (GTK4-native).        
        Args:
            data (bytes | bytearray | GLib.Bytes): Image data
        Returns:
            Gdk.Texture | None
        """
        from gi.repository import Gdk, GLib
        if not data:
            return None
        try:
            if not isinstance(data, GLib.Bytes):
                data = GLib.Bytes.new(data)
            texture = Gdk.Texture.new_from_bytes(data)
            return texture
        except Exception as e:
            if getattr(self, "debug_mode", False):
                print(f"Gdk.Texture.new_from_bytes failed: {e}")
        try:
            raw = data.get_data() if hasattr(data, "get_data") else bytes(data)
            size = int((len(raw) // 4) ** 0.5)
            if size <= 0:
                return None
            stride = size * 4
            gbytes = GLib.Bytes.new(raw)
            texture = Gdk.MemoryTexture.new(
                size, size, Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED, gbytes, stride
            )
            return texture
        except Exception as e:
            if getattr(self, "debug_mode", False):
                print(f"Raw texture creation failed: {e}")
            return None

    def _save_favicon_to_cache(self, url, texture_or_data):
        """Save favicon image bytes to cache."""
        cache_path = self._get_favicon_cache_path(url)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        try:
            if isinstance(texture_or_data, (bytes, bytearray)):
                with open(cache_path, "wb") as f:
                    f.write(texture_or_data)
                return True
            elif hasattr(texture_or_data, "get_bytes"):
                gbytes = texture_or_data.get_bytes()
                if gbytes:
                    with open(cache_path, "wb") as f:
                        f.write(gbytes.get_data())
                    return True
        except Exception as e:
            if getattr(self, "debug_mode", False):
                print(f"Error saving favicon cache: {e}")
        return False

    def get_favicon(self, url, callback=None):
        """Get a favicon for the given URL.        
        Args:
            url: The URL to get the favicon for
            callback: Optional callback function that receives the texture            
        Returns:
            Gdk.Texture: The favicon texture if available, None otherwise
        """
        if not url or not isinstance(url, str):
            if callback:
                GLib.idle_add(callback, None)
            return None           
        cache_key = url.lower().strip()       
        with self.favicon_lock:
            if cache_key in self.favicon_cache:
                cached = self.favicon_cache[cache_key]
                if cached is not None and callback:
                    GLib.idle_add(callback, cached)
                return cached
        disk_cached = self._load_cached_favicon(cache_key)
        if disk_cached:
            with self.favicon_lock:
                self.favicon_cache[cache_key] = disk_cached
            if callback:
                GLib.idle_add(callback, disk_cached)
            return disk_cached 
                   
        def load_favicon_async():
            texture = self._load_favicon_async(url)
            if texture:
                with self.favicon_lock:
                    self.favicon_cache[cache_key] = texture
                if callback:
                    GLib.idle_add(callback, texture)
                return texture
            return None           
        threading.Thread(target=load_favicon_async, daemon=True).start()
        return None

    def _on_favicon_loaded(self, cache_key, texture):
        """Handle a favicon that was loaded asynchronously.        
        Args:
            cache_key: The cache key for the favicon
            texture: The loaded Gdk.Texture or None if loading failed
        """
        with self.favicon_lock:
            if texture:
                self.favicon_cache[cache_key] = texture
                self._save_favicon_to_cache(cache_key, texture)       
        if hasattr(self, '_pending_favicon_callbacks'):
            callbacks = self._pending_favicon_callbacks.pop(cache_key, [])
            for callback in callbacks:
                GLib.idle_add(callback, texture)

    def _load_favicon_async(self, url, callback=None):
        """Asynchronously load a favicon from the given URL.        
        Args:
            url: The URL to load the favicon from
            callback: Optional callback function that will be called with the result            
        Returns:
            Gdk.Texture: The loaded texture, or None if loading failed
        """
        if not url:
            if callback:
                GLib.idle_add(callback, None)
            return None           
        parsed = urllib.parse.urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            if callback:
                GLib.idle_add(callback, None)
            return None           
        domain = parsed.netloc[4:] if parsed.netloc.startswith("www.") else parsed.netloc
        favicon_urls = [
            f"https://www.google.com/s2/favicons?domain={domain}&sz=32",
            f"{parsed.scheme}://{domain}/favicon.ico",
            f"https://{domain}/favicon.ico",
            f"{parsed.scheme}://{domain}/favicon.png",
        ]        
        if parsed.scheme == "https":
            favicon_urls.extend([
                f"http://{domain}/favicon.ico",
                f"http://{domain}/favicon.png",
            ])           
        session = self._create_http_session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        })       
        valid_content_types = {
            "image/svg+xml", "image/svg", "image/webp", "image/png",
            "image/x-icon", "image/vnd.microsoft.icon", "image/icon",
            "image/ico", "image/jpeg", "image/jpg", "image/gif",
            "application/ico", "application/x-ico", "application/octet-stream"
        }
        
        def try_next_favicon(index=0):
            if index >= len(favicon_urls):
                if callback:
                    GLib.idle_add(callback, None)
                return               
            favicon_url = favicon_urls[index]
            
            def on_response(session, response):
                try:
                    if response.status_code != 200:
                        raise Exception(f"HTTP {response.status_code}")                       
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(x in content_type for x in valid_content_types):
                        if not response.content.startswith((b"\x89PNG", b"GIF", b"\xff\xd8", b"<svg", b"<?xml", b"\x00\x00")):
                            raise Exception("Invalid content type")                            
                    data = response.content
                    if not data:
                        raise Exception("Empty response")                        
                    texture = self._texture_from_bytes(data)
                    if texture:
                        try:
                            cache_key = f"{parsed.scheme}://{parsed.netloc}"
                            cache_path = self._get_favicon_cache_path(cache_key)
                            with open(cache_path, "wb") as f:
                                f.write(data)
                        except Exception as e:
                            if hasattr(self, 'debug_mode') and self.debug_mode:
                                print(f"Error saving favicon to cache: {e}")                                
                        if callback:
                            GLib.idle_add(callback, texture)
                        return                        
                except Exception as e:
                    if hasattr(self, 'debug_mode') and self.debug_mode:
                        print(f"Error loading favicon from {favicon_url}: {e}")               
                try_next_favicon(index + 1)
                
            def make_request():
                try:
                    response = session.get(favicon_url, timeout=5, stream=False)
                    GLib.idle_add(lambda: on_response(session, response))
                except Exception as e:
                    if hasattr(self, 'debug_mode') and self.debug_mode:
                        print(f"Error making request to {favicon_url}: {e}")
                    GLib.idle_add(lambda: try_next_favicon(index + 1))            
            threading.Thread(target=make_request, daemon=True).start()           
        try_next_favicon()

    def _create_http_session(self):
        if not hasattr(self, "_favicon_session"):
            self._favicon_session = requests.Session()
            self._favicon_session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/117.0 Safari/537.36"
                )
            })
        return self._favicon_session
    
    def _update_tab_favicon(self, tab, favicon):
        """Update the favicon in the tab header using only Gdk.Texture / Gdk.Paintable."""
        from gi.repository import Gtk, Gdk, GLib
        if not favicon or not hasattr(tab, "label_box"):
            return
        try:
            if not getattr(tab, "favicon_widget", None):
                tab.favicon_widget = Gtk.Image()
                tab.favicon_widget.set_size_request(16, 16)
                tab.label_box.prepend(tab.favicon_widget)
                tab.favicon_widget.set_visible(True)
            texture = None
            max_size = 16
            if isinstance(favicon, Gdk.Texture):
                texture = favicon
            elif isinstance(favicon, Gdk.Paintable):
                tab.favicon_widget.set_from_paintable(favicon)
                tab.favicon = favicon
                return
            elif isinstance(favicon, (bytes, bytearray, GLib.Bytes)):
                try:
                    data = favicon.get_data() if hasattr(favicon, "get_data") else favicon
                    gbytes = GLib.Bytes.new(data)
                    texture = Gdk.Texture.new_from_bytes(gbytes)
                except Exception as e:
                    if getattr(self, "debug_mode", False):
                        print(f"[favicon] Error creating texture from bytes: {e}")
            if not texture:
                self._set_fallback_favicon(tab)
                return
            width, height = texture.get_width(), texture.get_height()
            if width > max_size or height > max_size:
                try:
                    if hasattr(texture, "downscale"):
                        texture = texture.downscale(max_size, max_size)
                except Exception as e:
                    if getattr(self, "debug_mode", False):
                        print(f"[favicon] Downscale failed: {e}")
            tab.favicon_widget.set_from_paintable(texture)
            tab.favicon = texture
            tab.favicon_widget.set_visible(True)
        except Exception as e:
            if getattr(self, "debug_mode", False):
                print(f"Error in _update_tab_favicon: {e}")
            self._set_fallback_favicon(tab)
            
    def _set_fallback_favicon(self, tab):
        """Set a fallback favicon when loading fails."""
        from gi.repository import Gtk
        try:
            icon_theme = Gtk.IconTheme.get_for_display(self.get_display())
            paintable = icon_theme.lookup_icon(
                "web-browser-symbolic",
                None,
                16,
                self.get_scale_factor(),
                Gtk.TextDirection.NONE,
                Gtk.IconLookupFlags.FORCE_SYMBOLIC
            )
            if paintable:
                tab.favicon_widget.set_from_paintable(paintable)
                tab.favicon = paintable
        except Exception:
            pass

    def get_favicon_from_cache(self, url):
        """
        Synchronous fallback: return a Gdk.Texture for the given URL if available
        in memory/disk cache, or attempt a short synchronous fetch from common
        favicon endpoints. Returns Gdk.Texture or None.
        """
        import urllib.parse
        if not url or not isinstance(url, str):
            self.debug_print("Invalid URL provided")
            return None
        parsed = urllib.parse.urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            self.debug_print(f"Invalid URL format: {url}")
            return None
        cache_key = f"{parsed.scheme}://{parsed.netloc}"
        with self.favicon_lock:
            tex = self.favicon_cache.get(cache_key)
            if tex:
                return tex
        try:
            disk_tex = self._load_cached_favicon(cache_key)
            if disk_tex:
                with self.favicon_lock:
                    self.favicon_cache[cache_key] = disk_tex
                return disk_tex
        except Exception as e:
            if getattr(self, "debug_mode", False):
                print(f"Error reading disk cache for {cache_key}: {e}")
        session = self._create_http_session()
        favicon_urls = [
            f"https://www.google.com/s2/favicons?domain={parsed.netloc}&sz=32",
            f"{parsed.scheme}://{parsed.netloc}/favicon.ico",
            f"{parsed.scheme}://{parsed.netloc}/favicon.png",
            f"{parsed.scheme}://{parsed.netloc}/favicon.jpg",
        ]
        valid_sig_prefixes = (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",   # JPEG
            b"GIF8",           # GIF
            b"<?xml",          # SVG (text)
            b"<svg",           # SVG alternative
            b"\x00\x00\x01\x00"  # ICO header
        )
        for fav_url in favicon_urls:
            try:
                r = session.get(fav_url, timeout=5)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            raw_bytes = r.content
            if not raw_bytes:
                continue
            content_type = (r.headers.get("content-type") or "").lower()
            if content_type and not any(x in content_type for x in ("image/", "application/octet-stream", "image/vnd.microsoft.icon")):
                if not any(raw_bytes.startswith(sig) for sig in valid_sig_prefixes):
                    continue
            try:
                tex = self._texture_from_bytes(raw_bytes)
            except Exception:
                tex = None
            if tex:
                with self.favicon_lock:
                    self.favicon_cache[cache_key] = tex
                try:
                    cache_path = self._get_favicon_cache_path(cache_key)
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    with open(cache_path, "wb") as f:
                        f.write(raw_bytes)
                except Exception as e:
                    if getattr(self, "debug_mode", False):
                        print(f"Error saving favicon to disk cache: {e}")
                return tex
        return None

    def _process_favicon_texture(self, favicon_data, url):
        """
        Convert favicon_data (bytes) into a Gdk.Texture and cache it.
        If the cache already contains bytes, attempt to convert them.
        Returns Gdk.Texture or None.
        """
        from gi.repository import Gdk
        try:
            cache_key = url
            with self.favicon_lock:
                cached = self.favicon_cache.get(cache_key)
            if isinstance(cached, Gdk.Texture):
                return cached
            if isinstance(cached, (bytes, bytearray)):
                tex = self._texture_from_bytes(cached)
                if tex:
                    with self.favicon_lock:
                        self.favicon_cache[cache_key] = tex
                    return tex
            if favicon_data:
                tex = self._texture_from_bytes(favicon_data)
                if tex:
                    with self.favicon_lock:
                        self.favicon_cache[cache_key] = tex
                    return tex
            return None
        except Exception as e:
            if getattr(self, "debug_mode", False):
                print(f"Error processing favicon texture for {url}: {e}")
            return None

    def zoom_in(self):
        """Increase the zoom level of the current webview."""
        current_webview = self.get_current_webview()
        if current_webview:
            current_zoom = current_webview.get_zoom_level()
            current_webview.set_zoom_level(round(min(current_zoom + 0.1, 5.0), 1))

    def zoom_out(self):
        """Decrease the zoom level of the current webview."""
        current_webview = self.get_current_webview()
        if current_webview:
            current_zoom = current_webview.get_zoom_level()
            current_webview.set_zoom_level(round(max(current_zoom - 0.1, 0.25), 1))

    def zoom_reset(self):
        """Reset the zoom level of the current webview to 100%."""
        current_webview = self.get_current_webview()
        if current_webview:
            current_webview.set_zoom_level(1.0)

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
        self.webview.evaluate_javascript(script, -1, None, None, None, None, self.turnstile_callback, None)

    def turnstile_callback(self, webview, result, user_data):
        js_result = webview.evaluate_javascript_finish(result)
        if js_result:
            value = js_result.get_js_value()
            if not value.is_null():
                pass
            else:
                pass

    def debug_print(self, msg):
        """Helper function for debug output when debug mode is enabled."""
        if getattr(self, "debug", False):
            print(msg)

    @staticmethod
    def setup_dbus_error_handling():
        """Set up D-Bus error handling to suppress common non-critical errors."""
        import sys
        if hasattr(sys, '_dbus_error_handler_setup'):
            return   
        original_stderr = sys.stderr
        
        class DBusErrorFilter:
            def __init__(self, original):
                self.original = original
                
            def write(self, message):
                if 'Error writing credentials to socket' in message and 'Broken pipe' in message:
                    return
                self.original.write(message)
                
            def flush(self):
                self.original.flush()
                
        if not getattr(sys, 'debug_mode', False):
            sys.stderr = DBusErrorFilter(original_stderr)
            sys._dbus_error_handler_setup = True

    @staticmethod
    def cleanup_dbus():
        """Clean up DBus connections for the application."""
        try:
            import dbus
            session_bus = dbus.SessionBus()
            for bus_name in list(session_bus.list_names()):
                if 'org.mpris.MediaPlayer2.shadow-browser' in bus_name:
                    proxy = session_bus.get_object('org.freedesktop.DBus', 
                                                '/org/freedesktop/DBus')
                    proxy.ReleaseName('org.mpris.MediaPlayer2.shadow-browser',
                                   dbus_interface='org.freedesktop.DBus')
            return True
        except ImportError:
            return True
        except Exception as e:
            if hasattr(sys, 'stderr') and hasattr(sys.stderr, 'write'):
                sys.stderr.write(f"Error cleaning up DBus: {e}\n")
            return False

    class DBusErrorFilter:
        def __init__(self, original):
            self.original = original
            
        def write(self, message):
            if 'Error writing credentials to socket' in message and 'Broken pipe' in message:
                return
            self.original.write(message)
            
        def flush(self):
            self.original.flush()

    def run(self, argv=None):
        """Run the GTK application."""
        self.app = Gtk.Application(
            application_id='org.shadow.browser',
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        self.app.connect('startup', self.do_startup)
        self.app.connect('activate', self.do_activate)
        
        # Setup DBus error filter if not in debug mode
        if not getattr(sys, 'debug_mode', False) and not hasattr(sys, '_dbus_error_handler_setup'):
            original_stderr = sys.stderr
            sys.stderr = self.DBusErrorFilter(original_stderr)
            sys._dbus_error_handler_setup = True
            
        return self.app.run(argv if argv is not None else sys.argv)
            
    def do_startup(self, app=None):
        """Initialize application resources."""
        Gtk.Application.do_startup(self)
        
        # Initialize wake lock
        if not hasattr(self, 'wake_lock_active') or not self.wake_lock_active:
            self.wake_lock_active = self.wake_lock.inhibit()
            
        # Create the main window but don't show it yet
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("Shadow Browser")
        self.window.set_default_size(1200, 800)
        
        # Initialize UI components
        if not self.initialize_ui():
            raise RuntimeError("Failed to initialize UI components")
        
    def do_activate(self, app=None):
        """Show the main window."""
        try:
            if hasattr(self, 'window') and self.window:
                self.window.present()
            else:
                # If window doesn't exist, try to create it
                self.do_startup()
                if hasattr(self, 'window') and self.window:
                    self.window.present()
                else:
                    raise RuntimeError("Failed to create main window")
        except Exception as e:
            if self.debug_mode:
                print(f"Error in do_activate: {e}")
            try:
                dialog = Gtk.MessageDialog(
                    transient_for=self.window if hasattr(self, 'window') and self.window else None,
                    message_type=Gtk.MessageType.ERROR,
                    buttons=Gtk.ButtonsType.CLOSE,
                    text=f"Failed to start application: {str(e)}"
                )
                dialog.connect("response", lambda d, r: d.destroy())
                dialog.show()
            except Exception as dialog_error:
                print(f"Error showing error dialog: {dialog_error}")
            if hasattr(self, 'app') and Gtk.Application.get_default() == self.app:
                sys.exit(1)

    def on_new_window(self, widget):
        """Handle new window menu item."""
        try:
            new_browser = ShadowBrowser()
            new_browser.run()
        except Exception as e:
            if self.debug_mode:
                print(f"Error creating new window: {e}")

    def on_copy(self, widget):
        """Handle copy action."""
        try:
            if hasattr(self, 'webview') and self.webview:
                self.webview.execute_editing_command(WebKit.EDITING_COMMAND_COPY)
        except Exception as e:
            if self.debug_mode:
                print(f"Error copying: {e}")

    def on_paste(self, widget):
        """Handle paste action."""
        try:
            if hasattr(self, 'webview') and self.webview:
                self.webview.execute_editing_command(WebKit.EDITING_COMMAND_PASTE)
        except Exception as e:
            if self.debug_mode:
                print(f"Error pasting: {e}")
                
    def on_zoom_in_clicked(self, widget, *args):
        """Handle zoom in action."""
        try:
            if hasattr(self, 'webview') and self.webview:
                self.webview.set_zoom_level(self.webview.get_zoom_level() + 0.1)
        except Exception as e:
            if self.debug_mode:
                print(f"Error zooming in: {e}")
                
    def on_zoom_out_clicked(self, widget, *args):
        """Handle zoom out action."""
        try:
            if hasattr(self, 'webview') and self.webview:
                self.webview.set_zoom_level(max(0.1, self.webview.get_zoom_level() - 0.1))
        except Exception as e:
            if self.debug_mode:
                print(f"Error zooming out: {e}")
                
    def on_zoom_reset_clicked(self, widget, *args):
        """Reset zoom level to default."""
        try:
            if hasattr(self, 'webview') and self.webview:
                self.webview.set_zoom_level(1.0)
        except Exception as e:
            if self.debug_mode:
                print(f"Error resetting zoom: {e}")
                
    def on_inspect_clicked(self, widget, *args):
        """Open Web Inspector for the current web view."""
        try:
            if hasattr(self, 'webview') and self.webview:
                self.webview.get_inspector().show()
        except Exception as e:
            if self.debug_mode:
                print(f"Error opening inspector: {e}")
                
    def on_void_link_clicked(self, widget, uri, *args):
        """Handle void: links which are commonly used in bookmarks and placeholders."""
        if uri == 'about:blank':
            self.load_url(self.home_url)
        return True
                
    def initialize_ui(self):
        """Initialize the main UI components."""
        if not hasattr(self, 'window') or not self.window:
            return False
        try:
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)   
            if not hasattr(self, 'menubar'):
                self.menubar = self.create_menubar()
            if self.menubar and self.menubar.get_parent() is None:
                vbox.append(self.menubar)
            if not hasattr(self, 'toolbar'):
                self.toolbar = self.create_toolbar()
            if self.toolbar and self.toolbar.get_parent() is None:
                vbox.append(self.toolbar)
            if not hasattr(self, 'notebook'):
                self.notebook = Gtk.Notebook()
                self.notebook.set_show_tabs(True)
                self.notebook.set_scrollable(True)
            if self.notebook.get_parent() is None:
                vbox.append(self.notebook)
            self.download_manager.parent_window = self.window
            if not hasattr(self, 'download_box') or self.download_box is None:
                self.download_box = Gtk.Box()
                self.download_box.append(self.download_manager.box)
                vbox.append(self.download_box)               
            if self.window.get_child() is None:
                self.window.set_child(vbox)
            if not hasattr(self, '_window_signals_connected'):
                self.window.connect("close-request", self.on_window_destroy)
                self._window_signals_connected = True
            if len(self.tabs) == 0:
                self.add_new_tab(self.home_url)
            return True
        except Exception as e:
            if self.debug_mode:
                print(f"Error initializing UI: {e}")
                import traceback
                traceback.print_exc()
            return False
    
    def _create_icon_button(self, icon_name, callback, tooltip_text=None):
        """Create a styled icon button with a tooltip.     
        Args:
            icon_name: The name of the icon to use (e.g., "go-previous-symbolic")
            callback: The function to call when the button is clicked
            tooltip_text: Optional tooltip text to display on hover            
        Returns:
            Gtk.Button: The created button
        """
        try:
            button = Gtk.Button()
            button.set_has_frame(False)
            button.set_can_focus(False)
            button.add_css_class("flat")
            icon = Gtk.Image.new_from_icon_name(icon_name)
            button.set_child(icon)
            if button.get_parent():
                button.unparent()
            if callback:
                button.connect("clicked", callback)
            if tooltip_text:
                button.set_tooltip_text(tooltip_text)               
            return button
        except Exception as e:
            if self.debug_mode:
                print(f"Error creating icon button: {e}")
            button = Gtk.Button(label=icon_name)
            if callback:
                button.connect("clicked", callback)
            return button
            
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
    
    def on_navigation_changed(self, webview, uri):
        """Update the URL entry when navigation occurs."""
        uri = webview.get_uri()
        if uri != self.url_entry.get_text():
            self.url_entry.set_text(uri or "")

def signal_handler(sig, frame):
    """Handle termination signals to ensure clean shutdown."""
    ShadowBrowser.cleanup_dbus()
    sys.exit(128 + sig)

def main():
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create and run the application
    app = ShadowBanker(application_id='com.example.ShadowBrowser')
    return app.run(None)

if __name__ == "__main__":
    import sys
    exit_code = main()
    sys.exit(exit_code)
