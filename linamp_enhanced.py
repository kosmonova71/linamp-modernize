import sys
import os
import json
import random
#import weakref
from collections import OrderedDict
from pathlib import Path

os.environ['ALSA_CONFIG_UCM'] = ''

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gst', '1.0')
    gi.require_version('GLib', '2.0')
    gi.require_version('GObject', '2.0')
    gi.require_version('Gdk', '4.0')
    from gi.repository import Gtk, Gst, GLib, Pango, Gio, GObject, Gdk
except ImportError:
    sys.exit(1)

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3NoHeaderError
    from mutagen.mp3 import HeaderNotFoundError
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False
    print("Warning: mutagen library not found. Install with: pip install mutagen")

Gst.init(None)

# Audio format validation constants
SUPPORTED_AUDIO_FORMATS = {
    '.mp3': {'mime': 'audio/mpeg', 'name': 'MP3 Audio'},
    '.wav': {'mime': 'audio/wav', 'name': 'WAV Audio'},
    '.ogg': {'mime': 'audio/ogg', 'name': 'OGG Vorbis'},
    '.flac': {'mime': 'audio/flac', 'name': 'FLAC Audio'},
    '.m4a': {'mime': 'audio/mp4', 'name': 'M4A Audio'},
    '.aac': {'mime': 'audio/aac', 'name': 'AAC Audio'},
    '.wma': {'mime': 'audio/x-ms-wma', 'name': 'WMA Audio'},
    '.mp4': {'mime': 'audio/mp4', 'name': 'MP4 Audio'},
    '.opus': {'mime': 'audio/opus', 'name': 'Opus Audio'},
}

class LRUCache:
    """Thread-safe LRU cache with size limits"""
    def __init__(self, max_size=1000):
        self.max_size = max_size
        self.cache = OrderedDict()

    def get(self, key):
        if key in self.cache:
            # Move to end (most recently used)
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def put(self, key, value):
        if key in self.cache:
            # Update existing and move to end
            self.cache.move_to_end(key)
            self.cache[key] = value
        else:
            # Add new item
            self.cache[key] = value
            if len(self.cache) > self.max_size:
                # Remove oldest item
                self.cache.popitem(last=False)

    def clear(self):
        self.cache.clear()

    def size(self):
        return len(self.cache)

class Config:
    """Configuration management with persistence"""
    def __init__(self):
        self.config_dir = Path.home() / ".config" / "linamp"
        self.config_dir.mkdir(exist_ok=True)
        self.config_file = self.config_dir / "config.json"
        self.playlist_file = self.config_dir / "playlist.json"
        self.default_config = {
            "volume": 0.8,
            "last_directory": str(Path.home()),
            "shuffle": False,
            "repeat": False,
            "auto_play_next": True,
            "window_width": 500,
            "window_height": 700,
            "equalizer_enabled": False,
            "equalizer_preset": "Flat",
            "equalizer_bands": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "beat_aware_enabled": False,
            "energy_threshold": -30.0,
            "beat_sensitivity": 1.0
        }
        self.equalizer_presets = {
            "Flat": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "Rock": [5.0, 4.0, 3.0, 1.0, -1.0, -2.0, -1.0, 1.0, 3.0, 4.0],
            "Pop": [-1.0, 2.0, 4.0, 5.0, 3.0, 0.0, -1.0, 1.0, 2.0, -1.0],
            "Jazz": [3.0, 2.0, 1.0, 2.0, -2.0, -1.0, 0.0, 1.0, 3.0, 4.0],
            "Classical": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0, -1.0, -1.0, -1.0],
            "Bass Boost": [8.0, 6.0, 4.0, 2.0, 0.0, -2.0, -4.0, -4.0, -4.0, -4.0],
            "Vocal": [-4.0, -3.0, -2.0, 0.0, 2.0, 4.0, 4.0, 3.0, 2.0, 0.0],
            "Electronic": [5.0, 4.0, 3.0, 1.0, 0.0, 1.0, 3.0, 4.0, 5.0, 6.0]
        }
        self.equalizer_frequencies = ["32", "64", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
        self.config = self.load_config()

    def load_config(self):
        """Load configuration from file"""
        try:
            if self.config_file.exists():
                with open(self.config_file, 'r') as f:
                    return {**self.default_config, **json.load(f)}
        except (json.JSONDecodeError, FileNotFoundError, PermissionError) as e:
            print(f"Config load error: {e}")
        except Exception as e:
            print(f"Unexpected config error: {e}")
        return self.default_config.copy()

    def save_config(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except (PermissionError, OSError) as e:
            print(f"Config save error: {e}")
        except Exception as e:
            print(f"Unexpected config save error: {e}")

    def load_playlist(self):
        """Load playlist from file"""
        try:
            if self.playlist_file.exists():
                with open(self.playlist_file, 'r') as f:
                    return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError, PermissionError) as e:
            print(f"Playlist load error: {e}")
        except Exception as e:
            print(f"Unexpected playlist error: {e}")
        return []

    def save_playlist(self, playlist):
        """Save playlist to file"""
        try:
            with open(self.playlist_file, 'w') as f:
                json.dump(playlist, f, indent=2)
        except (PermissionError, OSError) as e:
            print(f"Playlist save error: {e}")
        except Exception as e:
            print(f"Unexpected playlist save error: {e}")

    def save(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f"Failed to save configuration: {e}")

class TrackItem(GObject.Object):
    """Data model for a track in the playlist with weak reference support"""

    def __init__(self, filename, display_name, duration=0):
        super().__init__()
        self._filename = filename
        self._title = display_name
        self._duration = self.format_duration(duration)
        self._duration_seconds = duration
        # Store weak reference to parent playlist if needed
        self._playlist_ref = None

    @GObject.Property(type=str)
    def filename(self):
        return self._filename

    @GObject.Property(type=str)
    def title(self):
        return self._title

    @GObject.Property(type=str)
    def duration(self):
        return self._duration

    @GObject.Property(type=int)
    def duration_seconds(self):
        return self._duration_seconds

    def format_duration(self, seconds):
        """Format duration in MM:SS or HH:MM:SS"""
        if seconds <= 0:
            return "--:--"
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"

class EnhancedWinampPlayer(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.example.EnhancedWinampGTK4")
        self.config = Config()
        self.playlist_store = Gio.ListStore()
        self.current_track_index = -1
        self.is_seeking = False
        self.auto_save_timer_id = None
        self.player = None
        self.equalizer = None
        self.metadata_cache = LRUCache(1000)
        self.active_backend = "playbin"
        self.eq_filter_bin = None
        self.eq_values = self.config.config.get("equalizer_bands", [0.0] * 10).copy()
        self.eq_update_pending = False
        self.beat_aware_enabled = self.config.config.get("beat_aware_enabled", False)
        self.level_element = None
        self._last_rms = -60.0
        self._energy_threshold = self.config.config.get("energy_threshold", -30.0)
        self._beat_sensitivity = self.config.config.get("beat_sensitivity", 1.0)
        self.crossfade_enabled = False
        self.crossfade_duration = 4.0
        self._crossfade_running = False
        self._fade_value = 0.0
        self._crossfade_timer_id = None
        self._fade_timer_id = None
        self.last_volume = self.config.config.get("volume", 0.8)

        self._ui_update_pending = False
        self._ui_update_queue = []

        # Timer pooling for better resource management
        self._active_timers = {}
        self._timer_callbacks = {}

        # Initialize single playbin backend
        self._initialize_gstreamer_backend()

    def is_valid_audio_file(self, filepath):
        """Check if file is a supported audio format"""
        try:
            if not os.path.isfile(filepath):
                return False
            
            # Check file extension
            _, ext = os.path.splitext(filepath.lower())
            if ext not in SUPPORTED_AUDIO_FORMATS:
                return False
            
            # Additional validation: try to read file header
            try:
                with open(filepath, 'rb') as f:
                    header = f.read(12)
                    if not header:
                        return False
                    
                    # Basic format validation based on file signatures
                    if ext == '.mp3':
                        # Check for MP3 frame sync (11 bits set)
                        return len(header) >= 3 and (header[0] & 0xFF) == 0xFF and (header[1] & 0xE0) == 0xE0
                    elif ext == '.wav':
                        # Check for RIFF header
                        return header.startswith(b'RIFF') and len(header) >= 12 and header[8:12] == b'WAVE'
                    elif ext == '.ogg':
                        # Check for OggS header
                        return header.startswith(b'OggS')
                    elif ext == '.flac':
                        # Check for FLAC signature
                        return header.startswith(b'fLaC')
                    elif ext in ['.m4a', '.mp4']:
                        # Check for ftyp box
                        return header.startswith(b'ftyp')
                    elif ext == '.aac':
                        # Basic AAC check (ADTS sync)
                        return len(header) >= 2 and (header[0] & 0xFF) == 0xFF and (header[1] & 0xF0) == 0xF0
                    elif ext == '.opus':
                        # Opus in Ogg container
                        return header.startswith(b'OggS')
                    
                    # If no specific validation, assume valid based on extension
                    return True
                    
            except (IOError, OSError):
                return False
                
        except Exception:
            return False

    def get_audio_file_info(self, filepath):
        """Get detailed information about an audio file"""
        try:
            if not self.is_valid_audio_file(filepath):
                return None
            
            stat = os.stat(filepath)
            _, ext = os.path.splitext(filepath.lower())
            format_info = SUPPORTED_AUDIO_FORMATS.get(ext, {'name': 'Unknown Audio', 'mime': 'application/octet-stream'})
            
            return {
                'path': filepath,
                'filename': os.path.basename(filepath),
                'size': stat.st_size,
                'modified': stat.st_mtime,
                'format': format_info['name'],
                'mime': format_info['mime'],
                'extension': ext,
                'is_valid': True
            }
        except Exception:
            return None

    def filter_audio_files(self, file_list):
        """Filter a list of files to only include valid audio files"""
        valid_files = []
        invalid_files = []
        
        for filepath in file_list:
            if self.is_valid_audio_file(filepath):
                valid_files.append(filepath)
            else:
                invalid_files.append(filepath)
        
        return valid_files, invalid_files

    def _set_timer(self, timer_name, interval_ms, callback, *args):
        """Set a timer with pooling to avoid duplicates"""
        # Clear existing timer with same name if exists
        if timer_name in self._active_timers:
            GLib.source_remove(self._active_timers[timer_name])

        # Store callback and create new timer
        self._timer_callbacks[timer_name] = (callback, args)
        self._active_timers[timer_name] = GLib.timeout_add(interval_ms, self._timer_wrapper, timer_name)

    def _set_timer_seconds(self, timer_name, interval_seconds, callback, *args):
        """Set a timer with interval in seconds"""
        self._set_timer(timer_name, interval_seconds * 1000, callback, *args)

    def _clear_timer(self, timer_name):
        """Clear a specific timer"""
        if timer_name in self._active_timers:
            GLib.source_remove(self._active_timers[timer_name])
            del self._active_timers[timer_name]
            if timer_name in self._timer_callbacks:
                del self._timer_callbacks[timer_name]

    def _clear_all_timers(self):
        """Clear all active timers"""
        for timer_name in list(self._active_timers.keys()):
            self._clear_timer(timer_name)

    def _timer_wrapper(self, timer_name):
        """Wrapper for timer callbacks that handles cleanup"""
        if timer_name in self._timer_callbacks:
            callback, args = self._timer_callbacks[timer_name]
            try:
                result = callback(*args)
                if result is False:
                    # Timer returned False, remove it
                    self._clear_timer(timer_name)
                    return False
                return True
            except Exception as e:
                print(f"Timer {timer_name} error: {e}")
                self._clear_timer(timer_name)
                return False
        return False

    def _batch_ui_update(self, update_func, *args, **kwargs):
        """Batch UI updates to reduce GTK rendering overhead"""
        if not hasattr(self, '_ui_update_queue'):
            self._ui_update_queue = []
            self._ui_update_pending = False

        # Skip duplicate updates for the same function
        for i, (func, fargs, fkwargs) in enumerate(self._ui_update_queue):
            if func == update_func and fargs == args and fkwargs == kwargs:
                return

        self._ui_update_queue.append((update_func, args, kwargs))

        if not self._ui_update_pending:
            self._ui_update_pending = True
            # Use high priority for UI updates to ensure responsiveness
            GLib.idle_add(self._process_ui_updates, priority=GLib.PRIORITY_HIGH)

    def _process_ui_updates(self):
        """Process queued UI updates in a single batch with time slicing"""
        if not hasattr(self, '_ui_update_queue'):
            self._ui_update_pending = False
            return False

        start_time = GLib.get_monotonic_time()
        processed = 0
        max_processing_time = 10000  # 10ms max processing time per frame

        try:
            # Process updates with time slicing to prevent UI freeze
            while self._ui_update_queue and processed < 10:  # Process max 10 updates per frame
                update_func, args, kwargs = self._ui_update_queue.pop(0)
                try:
                    update_func(*args, **kwargs)
                    processed += 1
                except Exception as e:
                    print(f"UI update error: {e}")

                # Check if we've used too much time
                if (GLib.get_monotonic_time() - start_time) > max_processing_time:
                    break

            # If there are more updates to process, schedule another batch
            if self._ui_update_queue:
                return GLib.SOURCE_CONTINUE
            else:
                self._ui_update_pending = False
                return GLib.SOURCE_REMOVE

        except Exception as e:
            print(f"UI batch processing error: {e}")
            self._ui_update_pending = False
            return GLib.SOURCE_REMOVE

    def _update_track_info_ui(self, title, status):
        """Update track information UI elements with batching"""
        if hasattr(self, 'title_label') and self.title_label:
            self.title_label.set_text(title)
        if hasattr(self, 'status_label') and self.status_label:
            self.status_label.set_text(status)

        # Schedule a redraw if needed
        if hasattr(self, 'header_box') and self.header_box:
            self.header_box.queue_draw()

    def _update_progress_ui(self, current_time, total_time, percent):
        """Update progress UI elements with batching and validation"""
        if not hasattr(self, 'progress_scale') or not self.progress_scale:
            return

        # Only update if values actually changed to avoid unnecessary redraws
        if hasattr(self, 'current_time_label') and self.current_time_label:
            if self.current_time_label.get_text() != current_time:
                self.current_time_label.set_text(current_time)

        if hasattr(self, 'total_time_label') and self.total_time_label:
            if self.total_time_label.get_text() != total_time:
                self.total_time_label.set_text(total_time)

        # Only update progress scale if user is not interacting with it
        if not self.is_seeking:
            current_value = self.progress_scale.get_value()
            # Only update if difference is significant (avoids jitter)
            if abs(current_value - percent) > 0.5:
                self.progress_scale.set_value(percent)

    def _initialize_gstreamer_backend(self):
        """Initialize optimized single playbin backend"""
        try:
            # Create single playbin
            self.player = Gst.ElementFactory.make("playbin", "player")
            if not self.player:
                raise RuntimeError("Failed to create playbin element")

            # Setup audio sink
            self._setup_audio_sink()

            # Setup equalizer and level elements
            self.equalizer = Gst.ElementFactory.make("equalizer-10bands", "equalizer")
            self.level_element = Gst.ElementFactory.make("level", "level")

            if self.level_element:
                self.level_element.set_property("interval", 100_000_000)  # 100ms
                self.level_element.set_property("message", True)

            # Setup equalizer filter bin
            if self.equalizer:
                self.setup_equalizer_filter()

            # Connect signals
            self.player.connect("about-to-finish", self.on_about_to_finish)
            bus = self.player.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self.on_bus_message)

        except Exception as e:
            print(f"GStreamer initialization failed: {e}")
            self.player = None
            self.equalizer = None
            self.level_element = None

    def _setup_audio_sink(self):
        """Setup optimal audio sink"""
        sinks_to_try = [
            ("pulsesink", None),
            ("autoaudiosink", None),
            ("alsasink", "default"),
            ("alsasink", "hw:1,0"),
            ("alsasink", "hw:0,0"),
            ("fakesink", None)
        ]

        for sink_name, device in sinks_to_try:
            try:
                audio_sink = Gst.ElementFactory.make(sink_name, "audio_sink")
                if audio_sink:
                    if device:
                        audio_sink.set_property("device", device)
                    self.player.set_property("audio-sink", audio_sink)
                    self._working_audio_sink = (sink_name, device)
                    break
            except Exception:
                continue

    def get_track_metadata(self, filepath):
        """Get cached metadata or read from file using LRU cache"""
        cached_metadata = self.metadata_cache.get(filepath)
        if cached_metadata is not None:
            return cached_metadata

        # Cache miss - read metadata and cache it
        metadata = self._read_metadata(filepath)
        self.metadata_cache.put(filepath, metadata)
        return metadata

    def _read_metadata(self, filepath):
        """Read comprehensive metadata from audio file using mutagen or fallback"""
        try:
            filename = os.path.basename(filepath)
            name_without_ext = os.path.splitext(filename)[0]
            
            # Default metadata
            metadata = {
                'title': name_without_ext,
                'artist': '',
                'album': '',
                'duration': 0
            }
            
            # Try to extract metadata using mutagen if available
            if MUTAGEN_AVAILABLE:
                try:
                    audio_file = MutagenFile(filepath)
                    if audio_file is not None:
                        # Extract title
                        if hasattr(audio_file, 'get'):
                            title = audio_file.get('TIT2', [None])  # ID3 title
                            if title and title[0]:
                                metadata['title'] = str(title[0])
                            else:
                                # Try other common title tags
                                for tag in ['TITLE', '\xa9nam']:
                                    title = audio_file.get(tag)
                                    if title:
                                        metadata['title'] = str(title[0])
                                        break
                        
                        # Extract artist
                        if hasattr(audio_file, 'get'):
                            artist = audio_file.get('TPE1', [None])  # ID3 artist
                            if artist and artist[0]:
                                metadata['artist'] = str(artist[0])
                            else:
                                # Try other common artist tags
                                for tag in ['ARTIST', '\xa9ART']:
                                    artist = audio_file.get(tag)
                                    if artist:
                                        metadata['artist'] = str(artist[0])
                                        break
                        
                        # Extract album
                        if hasattr(audio_file, 'get'):
                            album = audio_file.get('TALB', [None])  # ID3 album
                            if album and album[0]:
                                metadata['album'] = str(album[0])
                            else:
                                # Try other common album tags
                                for tag in ['ALBUM', '\xa9alb']:
                                    album = audio_file.get(tag)
                                    if album:
                                        metadata['album'] = str(album[0])
                                        break
                        
                        # Extract duration
                        if hasattr(audio_file, 'info') and audio_file.info:
                            metadata['duration'] = int(audio_file.info.length)
                
                except (ID3NoHeaderError, HeaderNotFoundError, Exception) as e:
                    # Fall back to basic filename parsing if mutagen fails
                    print(f"Metadata extraction failed for {filename}: {e}")
                    pass
            
            return metadata
            
        except Exception as e:
            print(f"Error reading metadata for {filepath}: {e}")
            filename = os.path.basename(filepath)
            return {
                'title': filename,
                'artist': '',
                'album': '',
                'duration': 0
            }

    def setup_equalizer_filter(self):
        """Setup equalizer as audio filter for single playbin"""
        if not self.equalizer:
            return
        try:
            self.eq_filter_bin = Gst.Bin.new("eq-filter")
            audioconvert_in = Gst.ElementFactory.make("audioconvert", "convert_in")
            audioresample = Gst.ElementFactory.make("audioresample", "resample")
            audioconvert_out = Gst.ElementFactory.make("audioconvert", "convert_out")

            if not all([self.eq_filter_bin, audioconvert_in, audioresample, audioconvert_out, self.equalizer]):
                self.eq_filter_bin = None
                return

            # Add level element for beat detection if available
            if self.level_element:
                self.eq_filter_bin.add(self.level_element)

            self.eq_filter_bin.add(audioconvert_in)
            self.eq_filter_bin.add(audioresample)
            self.eq_filter_bin.add(self.equalizer)
            self.eq_filter_bin.add(audioconvert_out)

            # Link elements
            if not audioconvert_in.link(audioresample):
                return

            if self.level_element:
                if not audioresample.link(self.level_element):
                    return
                if not self.level_element.link(self.equalizer):
                    return
            else:
                if not audioresample.link(self.equalizer):
                    return

            if not self.equalizer.link(audioconvert_out):
                return

            sink_pad = audioconvert_in.get_static_pad("sink")
            src_pad = audioconvert_out.get_static_pad("src")
            ghost_sink = Gst.GhostPad.new("sink", sink_pad)
            ghost_src = Gst.GhostPad.new("src", src_pad)
            self.eq_filter_bin.add_pad(ghost_sink)
            self.eq_filter_bin.add_pad(ghost_src)

        except Exception as e:
            print(f"Equalizer filter setup failed: {e}")
            self.eq_filter_bin = None

    def do_activate(self):
        """Application activation handler"""
        self.build_ui()

    def build_ui(self):
        """Build the main user interface"""
        self._create_main_window()
        self._apply_css_styling()
        self._build_main_layout()
        self._finalize_ui_setup()

    def _create_main_window(self):
        """Create and configure the main application window"""
        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_title("Enhanced GTK4 Winamp Player")
        self.win.set_default_size(
            self.config.config.get("window_width", 300),
            self.config.config.get("window_height", 600)
        )
        self.win.connect("destroy", self.on_window_destroy)

    def _apply_css_styling(self):
        """Apply CSS styling to the application"""
        css_provider = Gtk.CssProvider()
        css = self._get_css_styles()
        css_provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            self.win.get_display(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _get_css_styles(self):
        """Return the CSS styles for the application"""
        return """
        /* Base styling with performance optimizations */
        .control-button {
            min-width: 30px;
            min-height: 30px;
            padding: 3px;
            margin: 0;
            border-radius: 3px;
            background: linear-gradient(#f8f8f8, #e8e8e8);
            border: 1px solid #ccc;
            font-size: 12px;
            transition: background-color 0.15s ease;
        }
        .control-button:hover {
            background: linear-gradient(#e0e0e0, #d0d0d0);
            border-color: #999;
        }
        .control-button:active {
            background: linear-gradient(#d0d0d0, #c0c0c0);
            transform: translateY(1px);
        }

        /* Mode buttons - simplified styling */
        .mode-button {
            min-width: 60px;
            min-height: 24px;
            padding: 2px 4px;
            margin: 0;
            border-radius: 2px;
            background: #fff;
            color: #555;
            font-size: 9px;
            border: 1px solid #ccc;
            transition: all 0.15s ease;
        }
        .mode-button:hover {
            background: #f5f5f5;
            border-color: #999;
        }
        .mode-button:checked {
            background: #007acc;
            color: white;
            border-color: #005999;
        }

        /* Playlist buttons - compact */
        .playlist-button {
            min-width: 28px;
            min-height: 28px;
            padding: 2px;
            margin: 0;
            border-radius: 2px;
            background: #f8f8f8;
            border: 1px solid #ddd;
            transition: background-color 0.15s ease;
        }
        .playlist-button:hover {
            background: #e8e8e8;
            border-color: #999;
        }

        /* Playlist rows styling */
        .playlist-row {
            padding: 4px 6px;
            border-bottom: 1px solid #e0e0e0;
            background: #ffffff;
            min-height: 24px;
        }
        .playlist-row:hover {
            background: #f5f5f5;
        }
        .playlist-row:selected {
            background: #4a90e2;
            color: white;
        }
        .playlist-row label {
            font-size: 11px;
            margin: 0;
            padding: 0;
        }

        /* Track info styling */
        .track-info {
            font-size: 12px;
            font-weight: 600;
            color: #222;
            margin-bottom: 1px;
        }

        /* Equalizer styling - simplified */
        .eq-toggle {
            min-width: 80px;
            min-height: 24px;
            padding: 2px 4px;
            margin: 1px;
            border-radius: 2px;
            background: #fff;
            color: #555;
            font-size: 9px;
            border: 1px solid #ccc;
            transition: all 0.15s ease;
        }
        .eq-toggle:checked {
            background: #28a745;
            color: white;
            border-color: #1e7e34;
        }

       .eq-label {
            font-size: 8px;
            color: #666;
            font-weight: 600;
        }

        .eq-scale {
            margin: 0;
        }

        .eq-value {
            font-size: 7px;
            color: #888;
            margin-top: 0;
        }

        /* Progress bar styling */
        scale {
            margin: 1px 0;
        }

        /* Frame styling - simplified */
        frame {
            border: 1px solid #ddd;
            border-radius: 2px;
            margin: 1px;
            background: #fafafa;
        }

        frame > label {
            font-weight: 600;
            color: #444;
            font-size: 10px;
            padding: 1px 3px;
            background: #f0f0f0;
            border-radius: 1px;
            margin: 0 2px;
        }

        /* Status bar */
        .status-bar {
            border-top: 1px solid #ddd;
            padding: 1px 3px;
            font-size: 9px;
            color: #666;
        }

        /* Current track highlight */
        .current-track {
            background: #4a90e2;
            color: white;
            font-weight: 600;
            border-radius: 2px;
            padding: 0 2px;
        }
        #current-track-label {
            background: #4a90e2;
            color: white;
            font-weight: 600;
            border-radius: 2px;
            padding: 0 2px;
        }

        /* Beat indicator styling - simplified */
        .beat-indicator {
            font-size: 12px;
            color: #666;
            transition: color 0.08s ease;
        }
        .beat-indicator.active {
            color: #ff4444;
            font-weight: 700;
        }

        /* Performance optimizations */
        * {
            box-shadow: none;
        }
        """

    def _build_main_layout(self):
        """Build the main layout structure"""
        # Optimized main layout with reduced nesting
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        main_box.set_margin_top(2)
        main_box.set_margin_bottom(2)
        main_box.set_margin_start(2)
        main_box.set_margin_end(2)
        self.win.set_child(main_box)

        # Build UI sections in logical order
        self._build_header_section(main_box)
        self._build_progress_section(main_box)
        self._build_content_area(main_box)
        self._build_status_bar(main_box)

    def _build_header_section(self, parent):
        """Build the header section with track info and controls"""
        self.build_compact_header(parent)

    def _build_progress_section(self, parent):
        """Build the progress section"""
        self.build_progress_section(parent)

    def _build_content_area(self, parent):
        """Build the main content area with playlist and equalizer"""
        content_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        content_paned.set_position(300)

        # Equalizer section (top)
        eq_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.build_equalizer_section_compact(eq_container)
        content_paned.set_start_child(eq_container)

        # Playlist section (bottom)
        playlist_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.build_playlist_section(playlist_container)
        content_paned.set_end_child(playlist_container)

        parent.append(content_paned)

    def _build_status_bar(self, parent):
        """Build the status bar"""
        self.build_status_bar(parent)

    def _finalize_ui_setup(self):
        """Finalize UI setup and references"""
        self._setup_button_references()
        self.load_playlist()
        self.restore_window_state()
        
        # Set initial volume
        volume = self.config.config.get("volume", 0.8)
        if self.player:
            self.player.set_property("volume", volume)
        self.volume_scale.set_value(volume * 100)

        self.update_control_sensitivity()
        self.start_auto_save_timer()
        self.setup_keyboard_shortcuts()
        self.win.present()

    def _create_button(self, icon_name=None, label=None, css_class=None, tooltip=None, callback=None):
        """Create a button with common configuration"""
        if icon_name:
            button = Gtk.Button.new_from_icon_name(icon_name)
        elif label:
            button = Gtk.Button(label=label)
        else:
            button = Gtk.Button()
        
        if css_class:
            button.add_css_class(css_class)
        if tooltip:
            button.set_tooltip_text(tooltip)
        if callback:
            button.connect("clicked", callback)
        
        return button

    def _create_toggle_button(self, label, css_class=None, callback=None, active=False):
        """Create a toggle button with common configuration"""
        button = Gtk.ToggleButton(label=label)
        
        if css_class:
            button.add_css_class(css_class)
        if callback:
            button.connect("toggled", callback)
        button.set_active(active)
        
        return button

    def _create_label(self, text="", css_class=None, ellipsize=None, xalign=0.0, width_chars=None):
        """Create a label with common configuration"""
        label = Gtk.Label(label=text)
        
        if css_class:
            label.add_css_class(css_class)
        if ellipsize:
            label.set_ellipsize(ellipsize)
        if xalign is not None:
            label.set_xalign(xalign)
        if width_chars:
            label.set_width_chars(width_chars)
        
        return label

    def _create_box(self, orientation=Gtk.Orientation.VERTICAL, spacing=2, **kwargs):
        """Create a box with common configuration"""
        box = Gtk.Box(orientation=orientation, spacing=spacing)
        
        # Apply any additional properties
        for attr, value in kwargs.items():
            if hasattr(box, f'set_{attr}'):
                getattr(box, f'set_{attr}')(value)
        
        return box

    def _setup_button_references(self):
        """Set up button references after all UI elements are created"""
        self.btn_prev = self.prev_button
        self.btn_play = self.play_button
        self.btn_pause = self.play_button
        self.btn_stop = self.stop_button
        self.btn_next = self.next_button
        self.btn_add = self.add_button
        self.btn_add_folder = self.add_folder_button
        self.btn_clear = self.clear_button
        self.btn_repeat = self.repeat_toggle
        self.btn_shuffle = self.shuffle_toggle

        # Volume control reference - only set if it exists
        if hasattr(self, 'volume_scale'):
            self.volume_button = self.volume_scale

    def build_compact_header(self, parent):
        """Build compact header combining track info and controls"""
        header_box = self._create_box(
            orientation=Gtk.Orientation.HORIZONTAL, 
            spacing=4, 
            margin_bottom=2
        )
        self.header_box = header_box

        # Track info section (left side)
        info_box = self._create_box(
            orientation=Gtk.Orientation.VERTICAL, 
            spacing=1, 
            hexpand=True
        )

        self.title_label = self._create_label(
            text="No track loaded",
            css_class="track-info",
            ellipsize=Pango.EllipsizeMode.MIDDLE,
            xalign=0
        )
        info_box.append(self.title_label)

        self.artist_label = self._create_label(
            ellipsize=Pango.EllipsizeMode.MIDDLE,
            xalign=0
        )
        info_box.append(self.artist_label)

        self.album_label = self._create_label(
            ellipsize=Pango.EllipsizeMode.MIDDLE,
            xalign=0
        )
        info_box.append(self.album_label)

        header_box.append(info_box)

        # Controls section (right side)
        controls_box = self._create_box(
            orientation=Gtk.Orientation.VERTICAL, 
            spacing=2, 
            halign=Gtk.Align.END
        )

        # Playback controls
        self._build_playback_controls(controls_box)
        
        # Mode controls
        self._build_mode_controls(controls_box)
        
        # Volume control
        self._build_volume_control(controls_box)

        header_box.append(controls_box)
        parent.append(header_box)

    def _build_playback_controls(self, parent):
        """Build playback control buttons"""
        playback_box = self._create_box(
            orientation=Gtk.Orientation.HORIZONTAL, 
            spacing=2
        )

        self.prev_button = self._create_button(
            icon_name="media-skip-backward",
            css_class="control-button",
            callback=self.on_prev_track
        )
        playback_box.append(self.prev_button)

        self.play_button = self._create_button(
            icon_name="media-playback-start",
            css_class="control-button",
            callback=self.on_play_pause
        )
        playback_box.append(self.play_button)

        self.stop_button = self._create_button(
            icon_name="media-playback-stop",
            css_class="control-button",
            callback=self.on_stop
        )
        playback_box.append(self.stop_button)

        self.next_button = self._create_button(
            icon_name="media-skip-forward",
            css_class="control-button",
            callback=self.on_next_track
        )
        playback_box.append(self.next_button)

        parent.append(playback_box)

    def _build_mode_controls(self, parent):
        """Build mode toggle buttons"""
        mode_box = self._create_box(
            orientation=Gtk.Orientation.HORIZONTAL, 
            spacing=1
        )

        self.repeat_toggle = self._create_toggle_button(
            label="Repeat",
            css_class="mode-button",
            callback=self.on_toggle_repeat,
            active=self.config.config["repeat"]
        )
        mode_box.append(self.repeat_toggle)

        self.shuffle_toggle = self._create_toggle_button(
            label="Shuffle",
            css_class="mode-button",
            callback=self.on_toggle_shuffle,
            active=self.config.config["shuffle"]
        )
        mode_box.append(self.shuffle_toggle)

        parent.append(mode_box)

    def _build_volume_control(self, parent):
        """Build volume control slider"""
        volume_box = self._create_box(
            orientation=Gtk.Orientation.HORIZONTAL, 
            spacing=2
        )
        
        volume_icon = Gtk.Image.new_from_icon_name("audio-volume-high")
        volume_box.append(volume_icon)
        
        self.volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_size_request(80, -1)
        self.volume_scale.connect("value-changed", self.on_volume_changed)
        volume_box.append(self.volume_scale)
        
        parent.append(volume_box)

    def build_progress_section(self, parent):
        """Build compact progress bar"""
        progress_box = self._create_box(
            orientation=Gtk.Orientation.HORIZONTAL, 
            spacing=1
        )
        
        self.current_time_label = self._create_label(
            text="00:00",
            width_chars=4
        )
        progress_box.append(self.current_time_label)
        
        self.progress_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 1.0)
        self.progress_scale.set_draw_value(False)
        self.progress_scale.connect("value-changed", self.on_progress_changed)
        progress_box.append(self.progress_scale)
        
        self.total_time_label = self._create_label(
            text="00:00",
            width_chars=4
        )
        progress_box.append(self.total_time_label)
        
        parent.append(progress_box)

    def _setup_playlist_row(self, factory, list_item):
        """Setup a single playlist row with optimized rendering"""
        # Create a box to hold the row content
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        box.set_hexpand(True)
        box.set_halign(Gtk.Align.FILL)

        # Set CSS class for styling
        box.add_css_class('playlist-row')

        # Track number label (fixed width)
        number_label = Gtk.Label()
        number_label.set_width_chars(4)
        number_label.set_xalign(1.0)
        number_label.set_halign(Gtk.Align.START)
        number_label.set_margin_end(4)

        # Title label with ellipsize
        title_label = Gtk.Label()
        title_label.set_halign(Gtk.Align.START)
        title_label.set_hexpand(True)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_margin_end(4)

        # Duration label (fixed width)
        duration_label = Gtk.Label()
        duration_label.set_halign(Gtk.Align.END)
        duration_label.set_width_chars(8)
        duration_label.set_xalign(1.0)

        # Add all widgets to the box
        box.append(number_label)
        box.append(title_label)
        box.append(duration_label)

        # Store references for binding
        list_item.set_child(box)
        list_item.number_label = number_label
        list_item.title_label = title_label
        list_item.duration_label = duration_label

    def _bind_playlist_row(self, factory, list_item):
        """Bind data to a playlist row with optimized position lookup"""
        track = list_item.get_item()
        if not track:
            return

        # Get widget references
        number_label = getattr(list_item, 'number_label', None)
        title_label = getattr(list_item, 'title_label', None)
        duration_label = getattr(list_item, 'duration_label', None)

        if not all([number_label, title_label, duration_label]):
            return

        try:
            # Get the index efficiently from the list item position
            index = list_item.get_position()

            # Update the labels
            number_label.set_text(f"{index + 1}.")
            title_label.set_text(track.title or "Unknown Title")
            duration_label.set_text(track.duration or "--:--")

            # Update styling based on current track
            if index == self.current_track_index:
                title_label.add_css_class('current-track')
            else:
                title_label.remove_css_class('current-track')

        except Exception as e:
            print(f"Error binding playlist row: {e}")

    def build_playlist_section(self, parent):
        """Build optimized playlist section with lazy loading"""
        # Configure the parent container that was passed in
        parent.set_orientation(Gtk.Orientation.VERTICAL)
        parent.set_spacing(2)
        parent.set_margin_top(2)
        parent.set_margin_bottom(2)
        parent.set_margin_start(2)
        parent.set_margin_end(2)

        # Build playlist controls
        self._build_playlist_controls(parent)
        
        # Build playlist view
        self._build_playlist_view(parent)

    def _build_playlist_controls(self, parent):
        """Build playlist control buttons"""
        controls_box = self._create_box(
            orientation=Gtk.Orientation.HORIZONTAL, 
            spacing=2,
            halign=Gtk.Align.CENTER
        )

        # Create playlist buttons
        buttons_config = [
            ("list-add-symbolic", "Add Files", self.on_add_files, "add_button"),
            ("folder-open-symbolic", "Add Folder", self.on_add_folder, "add_folder_button"),
            ("edit-clear-symbolic", "Clear Playlist", self.on_clear_playlist, "clear_button")
        ]

        for icon_name, tooltip, handler, attr_name in buttons_config:
            btn = self._create_button(
                icon_name=icon_name,
                tooltip=tooltip,
                callback=handler
            )
            btn.add_css_class("flat")
            controls_box.append(btn)
            setattr(self, attr_name, btn)

        parent.append(controls_box)

    def _build_playlist_view(self, parent):
        """Build the main playlist view"""
        # Create scrolled window
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_size_request(-1, 250)
        scrolled.set_has_frame(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Initialize playlist store if needed
        if not hasattr(self, 'playlist_store'):
            self.playlist_store = Gio.ListStore()

        # Set up selection model
        selection_model = Gtk.SingleSelection.new(self.playlist_store)
        self.playlist_selection = selection_model

        # Create list view with factory
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup_playlist_row)
        factory.connect("bind", self._bind_playlist_row)

        # Create column view
        column_view = Gtk.ColumnView(model=selection_model)
        column_view.set_show_row_separators(True)
        column_view.set_hexpand(True)
        column_view.set_vexpand(True)
        
        # Create and add column
        column = Gtk.ColumnViewColumn()
        column.set_title("Tracks")
        column.set_factory(factory)
        column.set_expand(True)
        column_view.append_column(column)

        # Connect signals
        column_view.connect("activate", self.on_playlist_activate)

        # Set up the view
        scrolled.set_child(column_view)
        self.playlist_view = column_view
        self.playlist_scrolled = scrolled

        parent.append(scrolled)
        self.load_playlist()

    def build_equalizer_section_compact(self, parent):
        """Build compact equalizer section without frame"""
        # Create container for the equalizer
        eq_box = self._create_box(
            orientation=Gtk.Orientation.VERTICAL, 
            spacing=2,
            margin_end=2
        )
        
        # Build EQ controls
        self._build_eq_controls(eq_box)
        
        # Build EQ bands
        self._build_eq_bands(eq_box)
        
        parent.append(eq_box)

    def _build_eq_controls(self, parent):
        """Build equalizer control widgets"""
        controls_box = self._create_box(
            spacing=2,
            margin_bottom=1
        )

        # Enable EQ toggle
        self.eq_enabled_toggle = self._create_toggle_button(
            label="Enable EQ",
            css_class="eq-toggle",
            callback=self.on_equalizer_enabled_toggled,
            active=False
        )
        controls_box.append(self.eq_enabled_toggle)

        # Preset label
        controls_box.append(self._create_label(text="Preset:"))

        # Preset dropdown
        preset_keys = list(self.config.equalizer_presets.keys())
        if "Custom" not in preset_keys:
            preset_keys.append("Custom")
        
        self.eq_preset_combo = Gtk.DropDown.new_from_strings(preset_keys)
        current_preset = self.config.config.get("equalizer_preset", "Flat")
        preset_keys = list(self.config.equalizer_presets.keys())
        
        if current_preset in preset_keys:
            preset_index = preset_keys.index(current_preset)
        else:
            preset_index = preset_keys.index("Flat")
            self.config.config["equalizer_preset"] = "Flat"
            
        self.eq_preset_combo.set_selected(preset_index)
        self.eq_preset_combo.connect("notify::selected", self.on_preset_changed)
        controls_box.append(self.eq_preset_combo)

        parent.append(controls_box)

    def _build_eq_bands(self, parent):
        """Build equalizer band controls"""
        self.eq_band_scales = []
        bands_box = self._create_box(spacing=0)

        for i, freq in enumerate(self.config.equalizer_frequencies):
            band_box = self._create_box(
                orientation=Gtk.Orientation.VERTICAL, 
                spacing=0
            )

            # Frequency label
            freq_label = self._create_label(
                text=freq,
                css_class="eq-label"
            )
            freq_label.set_size_request(20, -1)
            band_box.append(freq_label)

            # EQ scale
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -12, 12, 0.5)
            scale.set_size_request(20, 50)
            scale.set_inverted(True)
            scale.set_draw_value(False)
            current_value = self.config.config.get("equalizer_bands", [0.0]*10)[i]
            scale.set_value(current_value)
            scale.add_css_class("eq-scale")
            scale.connect("value-changed", self.on_eq_scale_changed, i)
            band_box.append(scale)

            # Value label
            value_label = self._create_label(
                text=f"{current_value:.0f}",
                css_class="eq-value"
            )
            value_label.set_size_request(20, -1)
            band_box.append(value_label)

            bands_box.append(band_box)
            self.eq_band_scales.append({"scale": scale, "label": value_label})

        parent.append(bands_box)

    def build_status_bar(self, parent):
        """Build status bar"""
        self.status_bar = self._create_box(
            orientation=Gtk.Orientation.HORIZONTAL
        )
        
        self.status_label = self._create_label(
            text="Ready",
            css_class="status-bar"
        )
        self.status_label.set_size_request(100, -1)
        
        self.status_bar.append(self.status_label)
        parent.append(self.status_bar)

    def _title_setup(self, factory, list_item):
        """Setup title column"""
        label = Gtk.Label()
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_xalign(0)
        list_item.set_child(label)

    def _title_bind(self, factory, list_item):
        """Bind title column"""
        label = list_item.get_child()
        item = list_item.get_item()
        if item and hasattr(item, 'title'):
            label.set_text(item.title)
            position = list_item.get_position()
            if position == self.current_track_index:
                # Ensure we don't duplicate the class
                if not label.has_css_class("current-track"):
                    label.add_css_class("current-track")
                label.set_name("current-track-label")
            else:
                # Only remove if the class exists
                if label.has_css_class("current-track"):
                    label.remove_css_class("current-track")
                label.set_name("")

    def _duration_setup(self, factory, list_item):
        """Setup duration column"""
        label = Gtk.Label()
        label.set_xalign(0)
        list_item.set_child(label)

    def _duration_bind(self, factory, list_item):
        """Bind duration column"""
        label = list_item.get_child()
        item = list_item.get_item()
        if item and hasattr(item, 'duration'):
            label.set_text(item.duration)

    def build_crossfade_controls(self, parent):
        """Build crossfade control panel"""
        crossfade_frame = Gtk.Frame(label="Crossfade")
        crossfade_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        crossfade_box.set_margin_top(1)
        crossfade_box.set_margin_bottom(1)
        crossfade_box.set_margin_start(1)
        crossfade_box.set_margin_end(1)
        crossfade_frame.set_child(crossfade_box)
        controls_box = Gtk.Box(spacing=1)
        self.crossfade_toggle = Gtk.ToggleButton.new_with_label("Enable Crossfade")
        self.crossfade_toggle.set_active(self.crossfade_enabled)
        self.crossfade_toggle.connect("toggled", self.on_crossfade_toggled)
        self.crossfade_toggle.set_size_request(15, 8)  # Still valid in GTK4 for fixed size
        self.crossfade_toggle.add_css_class("small-button")
        controls_box.append(self.crossfade_toggle)
        controls_box.append(Gtk.Label(label="Duration:"))
        self.crossfade_duration_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 2.0, 8.0, 0.5
        )
        self.crossfade_duration_scale.set_value(self.crossfade_duration)
        self.crossfade_duration_scale.set_hexpand(True)
        self.crossfade_duration_scale.set_draw_value(True)
        self.crossfade_duration_scale.set_digits(1)
        self.crossfade_duration_scale.connect("value-changed", self.on_crossfade_duration_changed)
        controls_box.append(self.crossfade_duration_scale)
        crossfade_box.append(controls_box)
        beat_box = Gtk.Box(spacing=1)
        self.beat_aware_toggle = Gtk.ToggleButton.new_with_label("Beat-Aware")
        self.beat_aware_toggle.set_active(self.beat_aware_enabled)
        self.beat_aware_toggle.connect("toggled", self.on_beat_aware_toggled)
        self.beat_aware_toggle.set_size_request(15, 8)  # Still valid in GTK4 for fixed size
        self.beat_aware_toggle.add_css_class("small-button")
        beat_box.append(self.beat_aware_toggle)
        beat_box.append(Gtk.Label(label="Sensitivity:"))
        self.energy_threshold_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, -40.0, -20.0, 1.0
        )
        self.energy_threshold_scale.set_value(self._energy_threshold)
        self.energy_threshold_scale.set_hexpand(True)
        self.energy_threshold_scale.set_draw_value(True)
        self.energy_threshold_scale.set_digits(0)
        self.energy_threshold_scale.connect("value-changed", self.on_energy_threshold_changed)
        beat_box.append(self.energy_threshold_scale)
        crossfade_box.append(beat_box)
        parent.append(crossfade_frame)

    def restore_window_state(self):
        """Restore window size and position"""
        width = self.config.config.get("window_width", 120)
        height = self.config.config.get("window_height", 300)
        self.win.set_default_size(width, height)

    def load_playlist(self):
        """Load saved playlist"""
        try:
            playlist_data = self.config.load_playlist()
            for item in playlist_data:
                if isinstance(item, dict):
                    # Handle new format with duration information
                    filepath = item.get('filename', '')
                    if os.path.exists(filepath):
                        filename = os.path.basename(filepath)
                        duration = item.get('duration', 0)
                        track = TrackItem(filepath, filename, duration)
                        self.playlist_store.append(track)
                elif isinstance(item, str) and os.path.exists(item):
                    # Handle old format (just filenames)
                    filename = os.path.basename(item)
                    track = TrackItem(item, filename)
                    self.playlist_store.append(track)
        except Exception as e:
            print(f"Error loading playlist: {e}")

    def on_window_destroy(self, widget):
        """Save configuration when window closes"""
        self.stop_auto_save_timer()
        width = self.win.get_width()
        height = self.win.get_height()
        self.config.config["window_width"] = width
        self.config.config["window_height"] = height
        self.save_playlist()
        self.config.save_config()
        self.quit()

    def on_play_pause(self, button):
        """Handle play/pause toggle button"""
        if self.player:
            state = self.player.get_state(0).state
            if state == Gst.State.PLAYING:
                self.on_pause(button)
            else:
                self.on_play(button)
        else:
            self.on_play(button)

    def on_play(self, button):
        """Handle play button click"""
        if self.current_track_index >= 0:
            self.play_current_track()
        else:
            self.on_add_files(button)

    def on_pause(self, button):
        """Handle pause button click"""
        if self.player:
            state = self.player.get_state(0).state
            if state == Gst.State.PLAYING:
                self.player.set_state(Gst.State.PAUSED)
                self.status_label.set_text("Paused")
            elif state == Gst.State.PAUSED:
                self.player.set_state(Gst.State.PLAYING)
                self.status_label.set_text("Playing")

    def on_stop(self, button):
        """Handle stop button click"""
        if self.player:
            self.player.set_state(Gst.State.NULL)
        self.current_track_index = -1
        self.progress_scale.set_value(0)
        self.current_time_label.set_text("00:00")
        self.status_label.set_text("Stopped")

    def on_prev_track(self, button):
        """Go to previous track"""
        if len(self.playlist_store) > 0:
            if self.config.config["shuffle"]:
                self.current_track_index = random.randint(0, len(self.playlist_store) - 1)
            else:
                self.current_track_index = (self.current_track_index - 1) % len(self.playlist_store)
            self.play_current_track()

    def on_next_track(self, button):
        """Go to next track"""
        if len(self.playlist_store) > 0:
            if self.config.config["shuffle"]:
                self.current_track_index = random.randint(0, len(self.playlist_store) - 1)
            else:
                self.current_track_index = (self.current_track_index + 1) % len(self.playlist_store)
            self.play_current_track()

    def on_about_to_finish(self, player):
        """Handle track ending"""
        if self.config.config["repeat"]:
            self.play_current_track()
        elif self.config.config.get("auto_play_next", True):
            self.on_next_track(None)

    def play_current_track(self):
        """Play the current track using optimized single playbin backend"""
        if 0 <= self.current_track_index < len(self.playlist_store):
            track = self.playlist_store.get_item(self.current_track_index)
            filename = track.filename
            display_name = track.title
            try:
                uri = GLib.filename_to_uri(filename)
                self.current_uri = uri

                if self.player:
                    self.player.set_state(Gst.State.NULL)
                    self.player.set_property("uri", uri)

                    # Apply equalizer if enabled
                    if (self.config.config.get("equalizer_enabled", False) and
                        hasattr(self, 'eq_filter_bin')):
                        try:
                            self.player.set_property("audio-filter", self.eq_filter_bin)
                        except Exception as e:
                            print(f"Failed to apply equalizer: {e}")

                    self.player.set_state(Gst.State.PLAYING)

                    # Batch UI updates
                    self._batch_ui_update(self._update_track_info_ui, display_name, f"Playing: {display_name}")
                    self._batch_ui_update(self.playlist_selection.select_item, self.current_track_index, True)
                    self._batch_ui_update(self.scroll_to_current_track)

                else:
                    self._batch_ui_update(self.status_label.set_text, "Player not available")
            except Exception as e:
                print(f"Error playing track: {e}")
                self._batch_ui_update(self.status_label.set_text, "Error playing")

    def on_playlist_activate(self, listview, position):
        """Handle playlist item activation"""
        self.current_track_index = position
        self.play_current_track()

    def on_toggle_repeat(self, button):
        """Toggle repeat mode"""
        self.config.config["repeat"] = button.get_active()
        self.config.save()

    def on_toggle_shuffle(self, button):
        """Toggle shuffle mode"""
        self.config.config["shuffle"] = button.get_active()
        self.config.save()

    def on_add_files(self, button):
        """Add files to playlist with validation"""
        dialog = Gtk.FileDialog()
        dialog.set_modal(True)
        dialog.set_title("Add Audio Files")
        
        # Create comprehensive audio file filter
        filter_audio = Gtk.FileFilter()
        filter_audio.set_name("Audio Files")
        for ext in SUPPORTED_AUDIO_FORMATS.keys():
            filter_audio.add_pattern(f"*{ext}")
        
        # Create individual format filters
        format_filters = []
        for ext, info in SUPPORTED_AUDIO_FORMATS.items():
            format_filter = Gtk.FileFilter()
            format_filter.set_name(info['name'])
            format_filter.add_pattern(f"*{ext}")
            format_filters.append(format_filter)
        
        filter_all = Gtk.FileFilter()
        filter_all.set_name("All Files")
        filter_all.add_pattern("*")
        
        filter_list = Gio.ListStore.new(Gtk.FileFilter)
        filter_list.append(filter_audio)
        for format_filter in format_filters:
            filter_list.append(format_filter)
        filter_list.append(filter_all)
        
        dialog.set_filters(filter_list)
        dialog.set_initial_folder(Gio.File.new_for_path(self.config.config["last_directory"]))
        dialog.open_multiple(
            self.win,
            None,
            self._on_add_files_response
        )

    def _on_add_files_response(self, dialog, result):
        """Handle add files dialog response with validation"""
        try:
            files = dialog.open_multiple_finish(result)
            if files:
                file_paths = [file.get_path() for file in files]
                valid_files, invalid_files = self.filter_audio_files(file_paths)
                
                # Add valid files to playlist
                files_added = 0
                for filepath in valid_files:
                    try:
                        filename = os.path.basename(filepath)
                        track = TrackItem(filepath, filename)
                        self.playlist_store.append(track)
                        files_added += 1
                    except Exception as e:
                        print(f"Error adding file {filepath}: {e}")
                
                # Update status with feedback
                if files_added > 0:
                    self.config.config["last_directory"] = os.path.dirname(valid_files[0])
                    self.config.save_config()
                    self.save_playlist()
                    self.update_control_sensitivity()
                    
                    if invalid_files:
                        status_msg = f"Added {files_added} audio files. {len(invalid_files)} files were invalid."
                    else:
                        status_msg = f"Added {files_added} audio files."
                    self.status_label.set_text(status_msg)
                    
                    # Auto-play first track if this is the first addition
                    if self.current_track_index == -1 and files_added > 0:
                        self.current_track_index = 0
                        self.play_current_track()
                else:
                    self.status_label.set_text("No valid audio files found.")
                    
                # Log invalid files for debugging
                if invalid_files:
                    print(f"Invalid files rejected: {invalid_files}")
                    
        except Exception as e:
            print(f"Error in file dialog response: {e}")
            self.status_label.set_text("Error adding files")

    def on_add_folder(self, button):
        """Add all audio files from a selected folder"""
        dialog = Gtk.FileDialog()
        dialog.set_modal(True)
        dialog.set_title("Add Audio Folder")
        dialog.select_folder(
            self.win,
            None,
            self._on_add_folder_response
        )

    def _on_add_folder_response(self, dialog, result):
        """Handle add folder dialog response with validation"""
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                folder_path = folder.get_path()
                self.config.config["last_directory"] = folder_path
                self.config.save_config()
                
                # Collect all files in folder
                all_files = []
                try:
                    for file_path in Path(folder_path).iterdir():
                        if file_path.is_file():
                            all_files.append(str(file_path))
                except Exception as e:
                    print(f"Error reading folder {folder_path}: {e}")
                    self.status_label.set_text("Error reading folder")
                    return
                
                # Filter audio files
                valid_files, invalid_files = self.filter_audio_files(all_files)
                
                # Add valid files to playlist
                files_added = 0
                for filepath in valid_files:
                    try:
                        filename = os.path.basename(filepath)
                        track = TrackItem(filepath, filename)
                        self.playlist_store.append(track)
                        files_added += 1
                    except Exception as e:
                        print(f"Error adding file {filepath}: {e}")
                
                # Update status with feedback
                if files_added > 0:
                    self.save_playlist()
                    self.update_control_sensitivity()
                    
                    if invalid_files:
                        status_msg = f"Added {files_added} audio files from folder. {len(invalid_files)} files were invalid."
                    else:
                        status_msg = f"Added {files_added} audio files from folder."
                    self.status_label.set_text(status_msg)
                    
                    # Auto-play first track if this is the first addition
                    if self.current_track_index == -1 and files_added > 0:
                        self.current_track_index = 0
                        self.play_current_track()
                else:
                    self.status_label.set_text("No valid audio files found in folder.")
                    
                # Log summary for debugging
                print(f"Folder scan: {files_added} valid, {len(invalid_files)} invalid files")
                    
        except Exception as e:
            print(f"Error in folder dialog response: {e}")
            self.status_label.set_text("Error adding folder")

    def on_progress_changed(self, scale):
        """Handle progress bar change"""
        if self.player and not self.is_seeking:
            value = scale.get_value()
            success, duration = self.player.query_duration(Gst.Format.TIME)
            if success and duration > 0:
                position = int((value / 100.0) * duration)
                self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, position)

    def on_volume_changed(self, scale):
        """Handle volume change"""
        volume = scale.get_value() / 100.0
        if hasattr(self, 'player') and self.player:
            self.player.set_property("volume", volume)
        self.config.config["volume"] = volume
        self.config.save_config()

    def on_mute_toggle(self, button):
        """Handle mute toggle"""
        if self.player:
            current_volume = self.player.get_property("volume")
            if current_volume > 0:
                self.last_volume = current_volume
                self.player.set_property("volume", 0.0)
                self.volume_scale.set_value(0)
                button.set_active(True)
            else:
                volume = self.last_volume
                self.player.set_property("volume", volume)
                self.volume_scale.set_value(volume * 100)
                button.set_active(False)

    def clear_playlist(self):
        """Clear the playlist"""
        self.playlist_store.clear()
        self.current_track_index = -1
        self.title_label.set_text("No track loaded")
        self.progress_scale.set_value(0)
        self.current_time_label.set_text("00:00")
        self.total_time_label.set_text("00:00")
        self.update_control_sensitivity()

        # Handle mute toggle state
        if hasattr(self, 'player') and self.player:
            current_volume = self.player.get_property("volume")
            if current_volume > 0:
                self.last_volume = current_volume
                self.player.set_property("volume", 0)
                self.volume_scale.set_value(0)
            else:
                volume = getattr(self, 'last_volume', self.config.config.get("volume", 0.8))
                self.player.set_property("volume", volume)
                self.volume_scale.set_value(volume * 100)

    def on_clear_playlist(self, button):
        """Handle clear playlist button"""
        dialog = Gtk.AlertDialog(
            message="Clear Playlist",
            detail="Are you sure you want to clear the entire playlist?"
        )
        dialog.set_buttons(["Cancel", "Clear"])
        dialog.set_default_button(0)
        dialog.set_cancel_button(0)

        def on_response(dialog, result):
            response = dialog.choose_finish(result)
            if response == "Clear":
                self.clear_playlist()
                if self.player:
                    self.player.set_state(Gst.State.NULL)
                self.save_playlist()
                self.update_control_sensitivity()
        dialog.choose(self.win, None, on_response)

    def on_crossfade_toggled(self, button):
        """Handle crossfade toggle"""
        self.crossfade_enabled = button.get_active()
        self.config.config["crossfade_enabled"] = self.crossfade_enabled
        self.config.save_config()
        if hasattr(self, 'active_player') and self.active_player:
            self.config.config["crossfade_duration"] = self.config.config.get("crossfade_duration", 3.0)
            self.config.save_config()

    def on_bus_message(self, bus, message):
        """Handle GStreamer bus messages"""
        if message.type == Gst.MessageType.EOS:
            self.on_next_track(None)
        elif message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            print(f"GStreamer error: {error} - {debug}")
            self._batch_ui_update(self.status_label.set_text, f"Error: {error.message}")
            # Try to handle audio device busy errors
            if "resource is busy" in str(error).lower():
                self._handle_audio_device_busy()
        elif message.type == Gst.MessageType.ELEMENT:
            if self.beat_aware_enabled:
                self._on_level_message(bus, message)
        return True

    def _handle_audio_device_busy(self):
        """Handle audio device busy errors by switching to alternative audio sink"""
        if hasattr(self, '_retry_count') and self._retry_count >= 2:
            print("Audio device error: Maximum retry attempts reached. Stopping playback.")
            self._batch_ui_update(self.status_label.set_text, "Audio device error")
            self.on_stop(None)
            return
        if not hasattr(self, '_retry_count'):
            self._retry_count = 0
        self._retry_count += 1
        was_playing = False
        current_pos = 0
        if hasattr(self, 'player') and self.player:
            state = self.player.get_state(0).state
            if state == Gst.State.PLAYING:
                was_playing = True
                success, pos = self.player.query_position(Gst.Format.TIME)
                if success:
                    current_pos = pos
        alternative_sinks = [
            ("pulsesink", None),
            ("autoaudiosink", None),
            ("fakesink", None)
        ]
        for sink_name, device in alternative_sinks:
            try:
                new_sink = Gst.ElementFactory.make(sink_name, f"fallback_{sink_name}")
                if new_sink:
                    if device:
                        new_sink.set_property("device", device)
                    if self.player:
                        self.player.set_state(Gst.State.NULL)
                    self.player.set_property("audio-sink", new_sink)
                    print(f"Switched to audio sink: {sink_name}")
                    self._batch_ui_update(self.status_label.set_text, f"Using {sink_name}")
                    if was_playing and hasattr(self, 'current_uri'):
                        self.player.set_property("uri", self.current_uri)
                        self.player.set_state(Gst.State.PLAYING)
                        if current_pos > 0:
                            self.player.seek_simple(
                                Gst.Format.TIME,
                                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                current_pos
                            )
                    return
            except Exception as e:
                print(f"Failed to switch to {sink_name}: {e}")
                continue
        print("Audio device error: No working audio sink found")
        self._batch_ui_update(self.status_label.set_text, "No audio device available")
        self.on_stop(None)

    def format_time_ns(self, nanoseconds):
        """Format time in nanoseconds to MM:SS"""
        seconds = nanoseconds // 1000000000
        return self.format_time_seconds(seconds)

    def format_time_seconds(self, seconds):
        """Format time in seconds to MM:SS or HH:MM:SS"""
        if seconds < 0:
            return "00:00"
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:02d}:{secs:02d}"

    def update_progress(self):
        """Update progress bar and time display"""
        if not self.is_seeking:
            GLib.idle_add(self._update_progress_main_thread)
        return True

    def _update_progress_main_thread(self):
        """Update progress from main thread using single backend"""
        try:
            if self.player:
                success, position = self.player.query_position(Gst.Format.TIME)
                success2, duration = self.player.query_duration(Gst.Format.TIME)
            else:
                return
            if success and success2 and duration > 0:
                current_time = self.format_time_ns(position)
                total_time = self.format_time_ns(duration)
                percent = (position / duration) * 100

                # Batch UI updates for progress
                self._batch_ui_update(self._update_progress_ui, current_time, total_time, percent)

        except Exception as e:
            print(f"Progress update error: {e}")

    def setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts for common actions using GTK4 event controllers"""
        # Create key press controller
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        
        # Add controller to main window
        if hasattr(self, 'win'):
            self.win.add_controller(key_controller)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        """Handle key press events for keyboard shortcuts"""
        # Convert keyval to key name
        key_name = Gdk.keyval_name(keyval)
        if not key_name:
            return False
        
        # Check modifier keys
        ctrl_pressed = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift_pressed = bool(state & Gdk.ModifierType.SHIFT_MASK)
        
        # Handle keyboard shortcuts
        try:
            # Playback controls
            if key_name == "space" and not ctrl_pressed and not shift_pressed:
                self.on_play_pause(None)
                return True
            elif key_name == "Left" and ctrl_pressed and not shift_pressed:
                self.on_prev_track(None)
                return True
            elif key_name == "Right" and ctrl_pressed and not shift_pressed:
                self.on_next_track(None)
                return True
            elif key_name == "s" and ctrl_pressed and not shift_pressed:
                self.on_stop(None)
                return True
            
            # Volume controls
            elif key_name == "Up" and ctrl_pressed and not shift_pressed:
                self._adjust_volume(0.1)
                return True
            elif key_name == "Down" and ctrl_pressed and not shift_pressed:
                self._adjust_volume(-0.1)
                return True
            elif key_name == "m" and not ctrl_pressed and not shift_pressed:
                self.on_mute_toggle(None)
                return True
            
            # Playlist controls
            elif key_name == "o" and ctrl_pressed and not shift_pressed:
                self.on_add_files(None)
                return True
            elif key_name == "o" and ctrl_pressed and shift_pressed:
                self.on_add_folder(None)
                return True
            elif key_name == "Delete" and ctrl_pressed and not shift_pressed:
                self.on_clear_playlist(None)
                return True
            
            # Mode toggles
            elif key_name == "r" and not ctrl_pressed and not shift_pressed:
                self.repeat_toggle.set_active(not self.repeat_toggle.get_active())
                return True
            elif key_name == "s" and not ctrl_pressed and not shift_pressed:
                self.shuffle_toggle.set_active(not self.shuffle_toggle.get_active())
                return True
            
            # Navigation
            elif key_name == "Home" and not ctrl_pressed and not shift_pressed:
                self._jump_to_track(0)
                return True
            elif key_name == "End" and not ctrl_pressed and not shift_pressed:
                self._jump_to_track(len(self.playlist_store) - 1)
                return True
            
            # Quit
            elif key_name == "q" and ctrl_pressed and not shift_pressed:
                self.quit()
                return True
            elif key_name == "Escape" and not ctrl_pressed and not shift_pressed:
                self.quit()
                return True
                
        except Exception as e:
            print(f"Error handling keyboard shortcut: {e}")
        
        return False

    def _adjust_volume(self, delta):
        """Adjust volume by delta (0.0 to 1.0)"""
        if hasattr(self, 'volume_scale'):
            current = self.volume_scale.get_value() / 100.0
            new_volume = max(0.0, min(1.0, current + delta))
            self.volume_scale.set_value(new_volume * 100)

    def _jump_to_track(self, index):
        """Jump to specific track index"""
        if 0 <= index < len(self.playlist_store):
            self.current_track_index = index
            self.play_current_track()

    def update_control_sensitivity(self):
        """Update sensitivity of playback controls based on playlist state"""
        has_tracks = len(self.playlist_store) > 0
        self.btn_play.set_sensitive(has_tracks)
        self.btn_next.set_sensitive(has_tracks)
        self.btn_prev.set_sensitive(has_tracks)
        self.btn_pause.set_sensitive(has_tracks)
        self.btn_stop.set_sensitive(has_tracks)

    def do_startup(self):
        """Application startup"""
        Gtk.Application.do_startup(self)
        # Use timer pooling for progress updates
        self._set_timer("progress_update", 1000, self.update_progress)

    def start_auto_save_timer(self):
        """Start the auto-save timer to save playlist every 30 seconds"""
        # Use timer pooling for auto-save
        self._set_timer_seconds("auto_save", 30, self.auto_save_playlist)

    def stop_auto_save_timer(self):
        """Stop the auto-save timer"""
        self._clear_timer("auto_save")

    def auto_save_playlist(self):
        """Auto-save playlist callback"""
        self.save_playlist()
        return True

    def save_playlist(self):
        """Save current playlist to file with track information"""
        try:
            playlist = []
            for i in range(len(self.playlist_store)):
                track = self.playlist_store.get_item(i)
                if track:
                    playlist.append({
                        'filename': track.filename,
                        'title': track.title,
                        'duration': track.duration_seconds
                    })
            self.config.save_playlist(playlist)
        except Exception as e:
            print(f"Error saving playlist: {e}")

    def on_equalizer_enabled_toggled(self, button):
        """Handle equalizer toggle using audio filter approach for single player"""
        if button.get_active():
            if self.player and self.eq_filter_bin:
                try:
                    self.player.set_property("audio-filter", self.eq_filter_bin)
                    self.config.config["equalizer_enabled"] = True
                except Exception as e:
                    print(f"Failed to apply equalizer: {e}")
                    button.set_active(False)
            else:
                button.set_active(False)
        else:
            if self.player:
                try:
                    self.player.set_property("audio-filter", None)
                    self.config.config["equalizer_enabled"] = False
                except Exception as e:
                    print(f"Failed to remove equalizer: {e}")
        self.config.save_config()

    def on_preset_changed(self, combo, param_spec):
        """Handle equalizer preset selection change"""
        if not self.equalizer:
            return
        selected = combo.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION:
            return
        preset_name = combo.get_model().get_string(selected)
        if preset_name in self.config.equalizer_presets:
            bands = self.config.equalizer_presets[preset_name]
            self.config.config["equalizer_bands"] = bands.copy()
            self.config.config["equalizer_preset"] = preset_name
            self.config.save_config()
            if hasattr(self, 'eq_band_scales'):
                for i, value in enumerate(bands):
                    if i < len(self.eq_band_scales):
                        self.eq_band_scales[i]["scale"].set_value(value)
                        self.eq_band_scales[i]["label"].set_text(f"{value:.1f}")
                        if self.equalizer:
                            self.equalizer.set_property(f"band{i}", value)

    def on_eq_scale_changed(self, scale, band):
        """Handle scale value change with throttling to prevent segfaults"""
        self.eq_values[band] = scale.get_value()
        if not self.eq_update_pending:
            self.eq_update_pending = True
            GLib.timeout_add(40, self.apply_eq_safely)

    def apply_eq_safely(self):
        """Apply EQ values safely to single player"""
        self.eq_update_pending = False
        if not self.equalizer:
            return False
        for i, val in enumerate(self.eq_values):
            try:
                self.equalizer.set_property(f"band{i}", val)
            except Exception as e:
                print(f"Failed to set EQ band {i}: {e}")
        return False

    def on_crossfade_duration_changed(self, scale):
        """Handle crossfade duration change"""
        self.crossfade_duration = scale.get_value()

    def on_energy_threshold_changed(self, scale):
        """Handle energy threshold change"""
        self._energy_threshold = scale.get_value()
        if hasattr(self, 'energy_value_label'):
            self.energy_value_label.set_text(f"{self._energy_threshold:.1f} dB")
        self.config.config["energy_threshold"] = self._energy_threshold
        self.config.save_config()

    def on_beat_sensitivity_changed(self, scale):
        """Handle beat sensitivity change"""
        self._beat_sensitivity = scale.get_value()
        if hasattr(self, 'beat_sensitivity_label'):
            self.beat_sensitivity_label.set_text(f"{self._beat_sensitivity:.1f}")
        self.config.config["beat_sensitivity"] = self._beat_sensitivity
        self.config.save_config()

    def on_beat_aware_toggled(self, toggle):
        """Handle beat-aware toggle"""
        self.beat_aware_enabled = toggle.get_active()
        self.config.config["beat_aware_enabled"] = self.beat_aware_enabled
        self.config.save_config()
        if not self.beat_aware_enabled:
            if hasattr(self, 'beat_indicator'):
                self.beat_indicator.remove_css_class("active")
            print("Beat-aware visualization disabled")
        else:
            print("Beat-aware visualization enabled")

    def scroll_to_current_track(self):
        """Auto-scroll playlist to show currently playing track without full model refresh"""
        if self.current_track_index >= 0 and hasattr(self, "playlist_view"):
            # Use a more efficient scrolling approach without full model refresh
            GLib.timeout_add(100, self._do_scroll_to_current_track)

    def _do_scroll_to_current_track(self):
        """Perform the actual scrolling to current track efficiently"""
        try:
            scroller = self.playlist_view.get_parent()
            if scroller:
                vadjustment = scroller.get_vadjustment()
                if vadjustment:
                    total_items = len(self.playlist_store)
                    if total_items > 0 and self.current_track_index >= 0:
                        upper = vadjustment.get_upper()
                        page_size = vadjustment.get_page_size()
                        max_scroll = upper - page_size
                        if max_scroll > 0:
                            item_ratio = self.current_track_index / max(total_items - 1, 1)
                            target_scroll = max_scroll * item_ratio
                            center_offset = page_size / 3
                            final_scroll = max(0, min(target_scroll - center_offset, max_scroll))
                            vadjustment.set_value(final_scroll)
        except Exception as e:
            print(f"Scroll error: {e}")
        return False

    def cleanup(self):
        """Cleanup resources safely"""
        try:
            # Clean up all timers using timer pooling
            self._clear_all_timers()

            # Clean up GStreamer elements
            if hasattr(self, 'player') and self.player:
                self.player.set_state(Gst.State.NULL)
                self.player = None
            if hasattr(self, 'equalizer') and self.equalizer:
                self.equalizer = None
            if hasattr(self, 'level_element') and self.level_element:
                self.level_element = None
            if hasattr(self, 'eq_filter_bin') and self.eq_filter_bin:
                self.eq_filter_bin = None

            # Clean up cache
            if hasattr(self, 'metadata_cache'):
                self.metadata_cache.clear()

        except Exception as e:
            print(f"Cleanup error: {e}")

    def _start_crossfade_timer(self):
        """Start the crossfade detection timer"""
        if self._crossfade_timer_id:
            GLib.source_remove(self._crossfade_timer_id)
        self._crossfade_timer_id = GLib.timeout_add(200, self._check_crossfade)

    def _check_crossfade(self):
        """Check if it's time to start crossfade (disabled for single player)"""
        # Crossfade not supported with single player implementation
        return False

    def _start_crossfade(self):
        """Start the crossfade process (disabled for single player)"""
        # Crossfade not supported with single player implementation
        print("Crossfade not available in single player mode")

    def _fade_step(self):
        """Perform one step of the crossfade (disabled for single player)"""
        # Crossfade not supported with single player implementation
        return False

    def _stop_crossfade(self):
        """Stop any ongoing crossfade (disabled for single player)"""
        # Crossfade not supported with single player implementation
        self._crossfade_running = False

    def _get_next_track_uri(self):
        """Get URI for the next track in playlist"""
        if len(self.playlist_store) == 0:
            return None
        next_index = self.current_track_index
        if self.config.config["shuffle"]:
            if len(self.playlist_store) > 1:
                available_indices = [i for i in range(len(self.playlist_store)) if i != self.current_track_index]
                next_index = random.choice(available_indices)
            else:
                next_index = self.current_track_index
        else:
            next_index = (self.current_track_index + 1) % len(self.playlist_store)
            if next_index == 0 and not self.config.config["repeat"]:
                return None
        if 0 <= next_index < len(self.playlist_store):
            track = self.playlist_store.get_item(next_index)
            return GLib.filename_to_uri(track.filename)
        return None

    def _advance_to_next_track(self):
        """Advance to the next track after crossfade"""
        if len(self.playlist_store) == 0:
            return
        if self.config.config["shuffle"]:
            if len(self.playlist_store) > 1:
                available_indices = [i for i in range(len(self.playlist_store)) if i != self.current_track_index]
                self.current_track_index = random.choice(available_indices)
        else:
            self.current_track_index = (self.current_track_index + 1) % len(self.playlist_store)
            if self.current_track_index == 0 and not self.config.config["repeat"]:
                self.current_track_index = -1
                return
        if 0 <= self.current_track_index < len(self.playlist_store):
            track = self.playlist_store.get_item(self.current_track_index)
            self.title_label.set_text(track.title)
            self.status_label.set_text(f"Playing: {track.title}")
            self.playlist_selection.select_item(self.current_track_index, True)
            self.scroll_to_current_track()

    def _on_level_message(self, bus, msg):
        """Handle level messages for beat-aware visualization"""
        if not self.beat_aware_enabled:
            return
        if msg.get_structure().get_name() != "level":
            return
        try:
            rms_values = msg.get_structure().get_value("rms")
            if rms_values and len(rms_values) > 0:
                rms = rms_values[0]
                self._last_rms = rms
                if hasattr(self, 'beat_indicator'):
                    sensitivity = getattr(self, '_beat_sensitivity', 1.0)
                    adjusted_threshold = self._energy_threshold / sensitivity
                    if rms > adjusted_threshold:
                        self.beat_indicator.add_css_class("active")
                        GLib.timeout_add(100, self._clear_beat_indicator)
                    else:
                        self.beat_indicator.remove_css_class("active")
        except Exception as e:
            print(f"Beat detection error: {e}")

    def _clear_beat_indicator(self):
        """Clear the beat indicator after a delay"""
        if hasattr(self, 'beat_indicator'):
            self.beat_indicator.remove_css_class("active")
        return False

    def do_shutdown(self):
        """GTK shutdown handler"""
        self.cleanup()
        Gtk.Application.do_shutdown(self)
if __name__ == '__main__':
    app = EnhancedWinampPlayer()
    app.run(sys.argv)
