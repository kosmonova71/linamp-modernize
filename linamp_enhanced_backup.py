import sys
import os
import json
import random
from pathlib import Path

# Set minimal environment variables for safer GStreamer initialization
os.environ['ALSA_CONFIG_UCM'] = ''

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gst', '1.0')
    gi.require_version('GLib', '2.0')
    gi.require_version('GObject', '2.0')
    from gi.repository import Gtk, Gst, GLib, Pango, Gio, GObject
except ImportError:
    sys.exit(1)

Gst.init(None)

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
            "window_width": 300,
            "window_height": 600,
            "equalizer_enabled": False,
            "equalizer_preset": "Flat",
            "equalizer_bands": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
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
        except Exception:
            print("Warning: Could not load config")
        return self.default_config.copy()

    def save_config(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception:
            print("Warning: Could not save config")

    def load_playlist(self):
        """Load playlist from file"""
        try:
            if self.playlist_file.exists():
                with open(self.playlist_file, 'r') as f:
                    return json.load(f)
        except Exception:
            print("Warning: Could not load playlist")
        return []

    def save_playlist(self, playlist):
        """Save playlist to file"""
        try:
            with open(self.playlist_file, 'w') as f:
                json.dump(playlist, f, indent=2)
        except Exception:
            print("Warning: Could not save playlist")

    def save(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")

class TrackItem(GObject.Object):
    """Data model for a track in the playlist"""

    def __init__(self, filename, display_name, duration=0):
        super().__init__()
        self._filename = filename
        self._title = display_name
        self._duration = self.format_duration(duration)
        self._duration_seconds = duration

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

class Player(GObject.Object):

    def __init__(self):
        super().__init__()
        self._uri = None
        self._volume = 0.5
        self._state = 'stopped'
        self._position = 0
        self._duration = "00:00"
        self._duration_seconds = 0
        self._current_stream_info = None
        self._tracks = []
        self._player = Gst.ElementFactory.make("playbin", "player")
        # Connect signals
        self._player.connect("about-to-finish", self.on_about_to_finish)
        bus = self._player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_bus_message)

    @GObject.Property(type=str)
    def uri(self):
        return self._uri

    @uri.setter
    def uri(self, value):
        self._uri = value

    @GObject.Property(type=float, default=0.5)
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value):
        self._volume = value

    @GObject.Property(type=str)
    def state(self):
        return self._state

    @GObject.Property(type=int, default=0)
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value

    @GObject.Property(type=str)
    def duration(self):
        return self._duration

    @GObject.Property(type=int, default=0)
    def duration_seconds(self):
        return self._duration_seconds

    @GObject.Property(type=object)
    def current_stream_info(self):
        return self._current_stream_info

    @GObject.Property(type=object)
    def tracks(self):
        return self._tracks

    def on_about_to_finish(self, player):
        pass

    def on_bus_message(self, bus, message):
        pass

    def on_duration_changed(self, player, new_duration):
        self._duration_seconds = new_duration
        self._duration = self.format_duration(new_duration)
        self.notify('duration')
        self.notify('duration_seconds')

    def on_position_changed(self, player, new_position):
        self._position = new_position
        self.notify('position')

    def on_current_time_changed(self, player, new_current_time):
        self._position = new_current_time
        self.notify('position')

    def format_duration(self, seconds):
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    def add_track(self, track):
        self._tracks.append(track)

    def remove_track(self, track):
        self._tracks.remove(track)

    def notify(self, property_name):
        super().notify(property_name)

class EnhancedWinampPlayer(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.example.EnhancedWinampGTK4")
        self.config = Config()
        self.playlist_store = Gio.ListStore()
        self.current_track_index = -1
        self.is_seeking = False
        self.auto_save_timer_id = None
        # Initialize GStreamer with fallback audio sinks
        self.player = None
        self.equalizer = None
        self.audioconvert = None
        self.audioresample = None

        # Single active backend tracking
        self.active_backend = "playbin"  # "playbin" or "eq"

        # Safe EQ update tracking
        self.eq_values = [0.0] * 10
        self.eq_update_pending = False

        # Initialize EQ values from config
        self.eq_values = self.config.config.get("equalizer_bands", [0.0] * 10).copy()

        try:
            # Try to create playbin with ALSA sink
            self.player = Gst.ElementFactory.make("playbin", "player")
            if not self.player:
                raise RuntimeError("Failed to create playbin element")

            # Set audio sink with fallback options to avoid device conflicts
            audio_sink = None
            sinks_to_try = [
                ("pulsesink", None),      # PulseAudio (preferred for multi-app sharing)
                ("autoaudiosink", None),  # Automatic detection
                ("alsasink", "default"),  # ALSA default device
                ("alsasink", "hw:1,0"),  # Try alternative hardware device
                ("alsasink", "hw:0,0"),  # Direct hardware access (last resort)
                ("fakesink", None)        # Null sink for testing
            ]

            for sink_name, device in sinks_to_try:
                try:
                    audio_sink = Gst.ElementFactory.make(sink_name, sink_name)
                    if audio_sink:
                        # Set device property if specified
                        if device:
                            audio_sink.set_property("device", device)

                        # Test if the sink can actually be used by setting it
                        test_player = Gst.ElementFactory.make("playbin", "test")
                        test_player.set_property("audio-sink", audio_sink)

                        print(f"Using audio sink: {sink_name}" + (f" with device: {device}" if device else ""))
                        self._working_audio_sink = (sink_name, device)  # Store for equalizer
                        audio_sink = audio_sink  # Keep the working sink
                        break
                except Exception as e:
                    print(f"Failed to create {sink_name}: {e}")
                    if audio_sink:
                        audio_sink = None
                    continue

            if audio_sink:
                self.player.set_property("audio-sink", audio_sink)
            else:
                print("Warning: No audio sink available, audio disabled")

            self.player.connect("about-to-finish", self.on_about_to_finish)
            bus = self.player.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self.on_bus_message)

            # Initialize equalizer components (create once, reuse)
            self.eq_element = Gst.ElementFactory.make("equalizer-10bands", "equalizer")
            self.audioconvert = Gst.ElementFactory.make("audioconvert", "audioconvert")
            self.audioresample = Gst.ElementFactory.make("audioresample", "audioresample")

            if not self.eq_element:
                print("Warning: Equalizer element not available, equalizer disabled")
                self.equalizer = None
            else:
                self.equalizer = self.eq_element  # Keep reference for compatibility
                # Setup equalizer pipeline immediately to ensure it's available for GUI
                self.setup_equalizer_pipeline()

            print("GStreamer initialized successfully")

        except Exception as e:
            print(f"Error initializing GStreamer: {e}")
            print("Audio playback will be disabled")
            self.player = None
            self.equalizer = None
            self.audioconvert = None
            self.audioresample = None

    def setup_equalizer_pipeline(self):
        """Setup equalizer pipeline with simplified approach to avoid segfaults"""
        print("DEBUG: Setting up equalizer pipeline...")
        if not self.equalizer:
            print("DEBUG: No equalizer element, returning")
            return

        try:
            print("DEBUG: Creating pipeline...")
            self.pipeline = Gst.Pipeline.new("audio-pipeline")
            print("DEBUG: Creating source...")
            self.source = Gst.ElementFactory.make("uridecodebin", "source")
            print("DEBUG: Creating audioconvert...")
            self.audioconvert = Gst.ElementFactory.make("audioconvert", "convert")
            print("DEBUG: Creating audioresample...")
            self.audioresample = Gst.ElementFactory.make("audioresample", "resample")
            print("DEBUG: Creating equalizer...")
            # Use the existing equalizer element, don't recreate
            self.equalizer = self.eq_element

            # Use the same working audio sink as the main player
            if hasattr(self, '_working_audio_sink'):
                sink_name, device = self._working_audio_sink
                print(f"DEBUG: Using working sink: {sink_name}")
                self.sink = Gst.ElementFactory.make(sink_name, "eq_sink")
                if device:
                    self.sink.set_property("device", device)
            else:
                print("DEBUG: Using autoaudiosink")
                self.sink = Gst.ElementFactory.make("autoaudiosink", "eq_sink")

            print("DEBUG: Checking all elements...")
            if not all([self.pipeline, self.source, self.audioconvert, self.audioresample, self.equalizer, self.sink]):
                print("Warning: Could not create all pipeline elements")
                self.pipeline = None
                return

            print("DEBUG: Adding elements to pipeline...")
            # Add elements to pipeline
            self.pipeline.add(self.source)  # CRITICAL: was missing!
            self.pipeline.add(self.audioconvert)
            self.pipeline.add(self.audioresample)
            self.pipeline.add(self.equalizer)
            self.pipeline.add(self.sink)

            print("DEBUG: Linking elements...")
            # Link elements
            if not self.audioconvert.link(self.audioresample):
                print("Failed to link audioconvert -> audioresample")
                return
            if not self.audioresample.link(self.equalizer):
                print("Failed to link audioresample -> equalizer")
                return
            if not self.equalizer.link(self.sink):
                print("Failed to link equalizer -> sink")
                return

            print("DEBUG: Connecting signals...")
            self.source.connect("pad-added", self.on_uridecodebin_pad_added)
            # Connect separate bus message handler for EQ pipeline
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self.on_eq_bus_message)
            print("DEBUG: Loading equalizer settings...")
            self.load_equalizer_settings()
            print("DEBUG: Equalizer pipeline setup complete")

        except Exception as e:
            print(f"Error setting up equalizer pipeline: {e}")
            import traceback
            traceback.print_exc()
            self.pipeline = None

    def on_uridecodebin_pad_added(self, element, pad):
        caps = pad.get_current_caps()
        if caps:
            struct = caps.get_structure(0)
            if struct.get_name().startswith("audio/"):
                sink_pad = self.audioconvert.get_static_pad("sink")
                if not sink_pad.is_linked():
                    pad.link(sink_pad)

    def do_activate(self):
        self.build_ui()
        self.load_playlist()
        self.restore_window_state()
        volume = self.config.config.get("volume", 0.8)
        if self.player:
            self.player.set_property("volume", volume)
        self.volume_scale.set_value(volume * 100)
        self.update_control_sensitivity()
        self.start_auto_save_timer()
        self.win.present()

    def build_ui(self):
        """Build the main user interface"""
        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_title("Enhanced GTK4 Winamp Player")
        self.win.set_default_size(
            self.config.config.get("window_width", 300),
            self.config.config.get("window_height", 600)
        )
        self.win.connect("destroy", self.on_window_destroy)
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_box.set_margin_top(6)
        main_box.set_margin_bottom(6)
        main_box.set_margin_start(6)
        main_box.set_margin_end(6)
        self.win.set_child(main_box)
        self.build_track_info(main_box)
        self.build_controls(main_box)
        self.build_progress_section(main_box)
        self.build_volume_control(main_box)
        self.build_equalizer(main_box)
        self.build_playlist(main_box)
        self.build_status_bar(main_box)

    def build_track_info(self, parent):
        """Build track information display"""
        info_frame = Gtk.Frame(label="Track Info")
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        info_box.set_margin_top(6)
        info_box.set_margin_bottom(6)
        info_box.set_margin_start(6)
        info_box.set_margin_end(6)
        info_frame.set_child(info_box)
        self.title_label = Gtk.Label(label="No track loaded")
        self.title_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.title_label.set_xalign(0)
        self.artist_label = Gtk.Label(label="")
        self.artist_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.artist_label.set_xalign(0)
        self.album_label = Gtk.Label(label="")
        self.album_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.album_label.set_xalign(0)
        info_box.append(self.title_label)
        info_box.append(self.artist_label)
        info_box.append(self.album_label)
        parent.append(info_frame)

    def build_controls(self, parent):
        """Build control buttons"""
        controls_frame = Gtk.Frame(label="Controls")
        controls_box = Gtk.Box(spacing=6)
        controls_box.set_margin_top(6)
        controls_box.set_margin_bottom(6)
        controls_box.set_margin_start(6)
        controls_box.set_margin_end(6)
        controls_frame.set_child(controls_box)
        self.btn_prev = Gtk.Button.new_with_label("⏮")
        self.btn_play = Gtk.Button.new_with_label("▶")
        self.btn_pause = Gtk.Button.new_with_label("⏸")
        self.btn_stop = Gtk.Button.new_with_label("⏹")
        self.btn_next = Gtk.Button.new_with_label("⏭")
        self.btn_add = Gtk.Button.new_with_label("➕")
        self.btn_add_folder = Gtk.Button.new_with_label("📁+")
        self.btn_clear = Gtk.Button.new_with_label("🗑")
        self.btn_repeat = Gtk.ToggleButton.new_with_label("🔁")
        self.btn_shuffle = Gtk.ToggleButton.new_with_label("🔀")
        self.btn_repeat.set_active(False)
        self.btn_shuffle.set_active(False)
        self.btn_prev.connect("clicked", self.on_prev_track)
        self.btn_play.connect("clicked", self.on_play)
        self.btn_pause.connect("clicked", self.on_pause)
        self.btn_stop.connect("clicked", self.on_stop)
        self.btn_next.connect("clicked", self.on_next_track)
        self.btn_repeat.connect("toggled", self.on_toggle_repeat)
        self.btn_shuffle.connect("toggled", self.on_toggle_shuffle)
        self.btn_add.connect("clicked", self.on_add_files)
        self.btn_add_folder.connect("clicked", self.on_add_folder)
        self.btn_clear.connect("clicked", self.on_clear_playlist)
        for btn in [self.btn_prev, self.btn_play, self.btn_pause,
                   self.btn_stop, self.btn_next, self.btn_repeat,
                   self.btn_shuffle, self.btn_add, self.btn_add_folder,
                   self.btn_clear]:
            controls_box.append(btn)
        parent.append(controls_frame)

    def build_progress_section(self, parent):
        """Build progress slider and time display"""
        progress_frame = Gtk.Frame(label="Progress")
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        progress_box.set_margin_top(6)
        progress_box.set_margin_bottom(6)
        progress_box.set_margin_start(6)
        progress_box.set_margin_end(6)
        progress_frame.set_child(progress_box)
        time_box = Gtk.Box(spacing=6)
        self.current_time_label = Gtk.Label(label="00:00")
        self.current_time_label.set_width_chars(6)
        self.total_time_label = Gtk.Label(label="00:00")
        self.total_time_label.set_width_chars(6)
        time_box.append(self.current_time_label)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        time_box.append(spacer)
        time_box.append(self.total_time_label)
        self.progress_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 0.1
        )
        self.progress_scale.set_hexpand(True)
        self.progress_scale.set_draw_value(False)
        self.progress_scale.set_size_request(120, -1)
        self.progress_scale.connect("change-value", self.on_seek_value_change)
        progress_box.append(time_box)
        progress_box.append(self.progress_scale)
        parent.append(progress_frame)

    def build_volume_control(self, parent):
        """Build volume control"""
        volume_frame = Gtk.Frame(label="Volume")
        volume_box = Gtk.Box(spacing=6)
        volume_box.set_margin_top(6)
        volume_box.set_margin_bottom(6)
        volume_box.set_margin_start(6)
        volume_box.set_margin_end(6)
        volume_frame.set_child(volume_box)
        self.volume_button = Gtk.Button.new_with_label("🔊")
        self.volume_button.connect("clicked", self.on_mute_toggle)
        self.volume_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 1
        )
        self.volume_scale.set_value(self.config.config["volume"] * 100)
        self.volume_scale.connect("value-changed", self.on_volume_changed)
        self.volume_scale.set_hexpand(True)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_size_request(100, -1)
        volume_box.append(self.volume_button)
        volume_box.append(self.volume_scale)
        parent.append(volume_frame)

    def build_equalizer(self, parent):
        """Build equalizer controls"""
        print("DEBUG: Building equalizer GUI - adding controls")

        eq_frame = Gtk.Frame(label="Equalizer")
        eq_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        eq_box.set_margin_top(3)
        eq_box.set_margin_bottom(3)
        eq_box.set_margin_start(3)
        eq_box.set_margin_end(3)
        eq_frame.set_child(eq_box)

        # Add compact controls
        controls_box = Gtk.Box(spacing=4)
        self.eq_enabled_toggle = Gtk.ToggleButton.new_with_label("Enable EQ")
        self.eq_enabled_toggle.set_active(False)  # Disable by default
        self.eq_enabled_toggle.connect("toggled", self.on_equalizer_enabled_toggled)

        # Rebuild preset model from current config to ensure sync
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

        controls_box.append(self.eq_enabled_toggle)
        controls_box.append(Gtk.Label(label="Preset:"))
        controls_box.append(self.eq_preset_combo)
        eq_box.append(controls_box)

        # Add compact equalizer bands
        self.eq_band_scales = []
        bands_box = Gtk.Box(spacing=1)

        for i, freq in enumerate(self.config.equalizer_frequencies):
            band_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            freq_label = Gtk.Label(label=freq)
            freq_label.set_size_request(30, -1)
            freq_label.add_css_class("caption")
            band_box.append(freq_label)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -24, 12, 0.5)
            scale.set_size_request(30, 80)
            scale.set_inverted(True)
            scale.set_draw_value(False)
            current_value = self.config.config.get("equalizer_bands", [0.0]*10)[i]
            scale.set_value(current_value)
            # Store band index and connect safe callback
            scale.connect("value-changed", self.on_eq_scale_changed, i)
            band_box.append(scale)
            value_label = Gtk.Label(label=f"{current_value:.0f}")
            value_label.set_size_request(30, -1)
            value_label.add_css_class("caption")
            band_box.append(value_label)
            bands_box.append(band_box)
            self.eq_band_scales.append({"scale": scale, "label": value_label})

        eq_box.append(bands_box)

        parent.append(eq_frame)
        print("DEBUG: Equalizer GUI completed successfully")

        # Safe scale callbacks with throttling enabled
        print("DEBUG: Safe EQ callbacks enabled with throttling")

    def build_playlist(self, parent):
        """Build playlist display"""
        playlist_frame = Gtk.Frame(label="Playlist")
        playlist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        playlist_box.set_margin_top(6)
        playlist_box.set_margin_bottom(6)
        playlist_box.set_margin_start(6)
        playlist_box.set_margin_end(6)
        playlist_frame.set_child(playlist_box)
        self.playlist_view = Gtk.ColumnView()
        self.playlist_selection = Gtk.SingleSelection(model=self.playlist_store)
        self.playlist_view.set_model(self.playlist_selection)
        self.playlist_view.connect("activate", self.on_playlist_activate)
        title_column = Gtk.ColumnViewColumn(title="Title")
        title_factory = Gtk.SignalListItemFactory()
        title_factory.connect("setup", self._title_setup)
        title_factory.connect("bind", self._title_bind)
        title_column.set_factory(title_factory)
        duration_column = Gtk.ColumnViewColumn(title="Duration")
        duration_factory = Gtk.SignalListItemFactory()
        duration_factory.connect("setup", self._duration_setup)
        duration_factory.connect("bind", self._duration_bind)
        duration_column.set_factory(duration_factory)
        self.playlist_view.append_column(title_column)
        self.playlist_view.append_column(duration_column)
        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_min_content_width(600)
        scroller.set_min_content_height(300)
        scroller.set_child(self.playlist_view)
        playlist_box.append(scroller)
        parent.append(playlist_frame)

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

    def build_status_bar(self, parent):
        """Build status bar"""
        self.status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.status_label = Gtk.Label(label="Ready")
        self.status_bar.append(self.status_label)
        parent.append(self.status_bar)

    def restore_window_state(self):
        """Restore window size and position"""
        width = self.config.config.get("window_width", 120)
        height = self.config.config.get("window_height", 300)
        self.win.set_default_size(width, height)

    def load_playlist(self):
        """Load saved playlist"""
        playlist_files = self.config.load_playlist()
        for file_info in playlist_files:
            if os.path.exists(file_info):
                filename = os.path.basename(file_info)
                track = TrackItem(file_info, filename)
                self.playlist_store.append(track)

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

    def on_play(self, button):
        """Handle play button click"""
        if self.current_track_index >= 0:
            self.play_current_track()
        else:
            self.on_add_files(button)

    def on_pause(self, button):
        """Handle pause button click"""
        if (hasattr(self, 'pipeline') and
            self.pipeline.get_state(0).state == Gst.State.PLAYING):
            self.pipeline.set_state(Gst.State.PAUSED)
            self.status_label.set_text("Paused")
        elif self.player.get_state(0).state == Gst.State.PLAYING:
            self.player.set_state(Gst.State.PAUSED)
            self.status_label.set_text("Paused")
        elif (hasattr(self, 'pipeline') and
              self.pipeline.get_state(0).state == Gst.State.PAUSED):
            self.pipeline.set_state(Gst.State.PLAYING)
            self.status_label.set_text("Playing")
        elif self.player.get_state(0).state == Gst.State.PAUSED:
            self.player.set_state(Gst.State.PLAYING)
            self.status_label.set_text("Playing")

    def on_stop(self, button):
        """Handle stop button click"""
        self.player.set_state(Gst.State.NULL)
        if hasattr(self, 'pipeline'):
            self.pipeline.set_state(Gst.State.NULL)
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
        """Play the current track"""
        if 0 <= self.current_track_index < len(self.playlist_store):
            track = self.playlist_store.get_item(self.current_track_index)
            filename = track.filename
            display_name = track.title
            try:
                def play_track():
                    try:
                        uri = GLib.filename_to_uri(filename)
                        self.current_uri = uri
                        if (self.config.config.get("equalizer_enabled", False) and
                            hasattr(self, 'pipeline') and self.equalizer):
                            self.active_backend = "eq"
                            self.player.set_state(Gst.State.NULL)
                            self.source.set_property("uri", uri)
                            self.pipeline.set_state(Gst.State.PLAYING)
                        else:
                            self.active_backend = "playbin"
                            if hasattr(self, 'pipeline'):
                                self.pipeline.set_state(Gst.State.NULL)
                            if self.player:
                                self.player.set_state(Gst.State.NULL)
                                self.player.set_property("uri", uri)
                                self.player.set_state(Gst.State.PLAYING)
                        self.title_label.set_text(display_name)
                        self.status_label.set_text(f"Playing: {display_name}")
                        self.playlist_selection.select_item(self.current_track_index, True)
                    except Exception as e:
                        self.status_label.set_text(f"Error playing: {e}")
                GLib.idle_add(play_track)
            except Exception as e:
                self.status_label.set_text(f"Error playing: {e}")

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
        """Add files to playlist"""
        dialog = Gtk.FileDialog()
        dialog.set_modal(True)
        dialog.set_title("Add Audio Files")
        dialog.set_default_size(300, 80)
        filter_audio = Gtk.FileFilter()
        filter_audio.set_name("Audio Files (*.mp3, *.wav, *.ogg, *.flac, *.m4a)")
        for ext in ["*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a", "*.aac"]:
            filter_audio.add_pattern(ext)
        filter_all = Gtk.FileFilter()
        filter_all.set_name("All Files")
        filter_all.add_pattern("*")
        filter_list = Gio.ListStore.new(Gtk.FileFilter)
        filter_list.append(filter_audio)
        filter_list.append(filter_all)
        dialog.set_filters(filter_list)
        dialog.set_initial_folder(Gio.File.new_for_path(self.config.config["last_directory"]))
        dialog.open_multiple(
            self.win,
            None,
            self._on_add_files_response
        )

    def _on_add_files_response(self, dialog, result):
        """Handle add files dialog response"""
        try:
            files = dialog.open_multiple_finish(result)
            if files:
                for file in files:
                    path = file.get_path()
                    filename = file.get_basename()
                    track = TrackItem(path, filename)
                    self.playlist_store.append(track)
                self.config.config["last_directory"] = os.path.dirname(files[0].get_path())
                self.config.save_config()
                self.save_playlist()
                self.update_control_sensitivity()
        except Exception as e:
            print(f"Error adding files: {e}")

    def on_add_folder(self, button):
        """Add all audio files from a selected folder"""
        dialog = Gtk.FileDialog()
        dialog.set_modal(True)
        dialog.set_title("Add Audio Folder")
        dialog.set_default_size(300, 80)
        dialog.select_folder(
            self.win,
            None,
            self._on_add_folder_response
        )

    def _on_add_folder_response(self, dialog, result):
        """Handle add folder dialog response"""
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                folder_path = folder.get_path()
                self.config.config["last_directory"] = folder_path
                self.config.save_config()
                audio_extensions = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac'}
                files_added = 0
                try:
                    for file_path in Path(folder_path).iterdir():
                        if file_path.is_file() and file_path.suffix.lower() in audio_extensions:
                            filename = file_path.name
                            track = TrackItem(str(file_path), filename)
                            self.playlist_store.append(track)
                            files_added += 1
                            if self.current_track_index == -1:
                                self.current_track_index = 0
                                self.title_label.set_text(filename)
                    if files_added > 0:
                        self.status_label.set_text(f"Added {files_added} audio files from folder")
                        self.save_playlist()
                        if self.current_track_index >= 0:
                            self.play_current_track()
                    else:
                        self.status_label.set_text("No audio files found in folder")
                    self.update_control_sensitivity()
                except Exception as e:
                    print(f"Error scanning folder: {e}")
                    self.status_label.set_text(f"Error scanning folder: {e}")
        except Exception as e:
            print(f"Error selecting folder: {e}")

    def clear_playlist(self):
        """Clear the playlist"""
        self.playlist_store.clear()
        self.current_track_index = -1
        self.title_label.set_text("No track loaded")
        self.artist_label.set_text("")
        self.album_label.set_text("")
        self.progress_scale.set_value(0)
        self.current_time_label.set_text("00:00")
        self.total_time_label.set_text("00:00")
        self.update_control_sensitivity()

    def on_clear_playlist(self, button):
        """Handle clear playlist button"""
        dialog = Gtk.AlertDialog(
            message="Clear Playlist",
            detail="Are you sure you want to clear the entire playlist?"
        )
        dialog.show(self.win)
        dialog.connect("response", lambda d, res: self._on_clear_response(res))

    def _on_clear_response(self, response):
        """Handle clear playlist dialog response"""
        if response == Gtk.ResponseType.OK:
            self.clear_playlist()
            if self.player:
                self.player.set_state(Gst.State.NULL)
            self.save_playlist()
            self.update_control_sensitivity()

    def on_volume_changed(self, scale):
        """Handle volume change"""
        volume = scale.get_value() / 100.0
        if self.player:
            self.player.set_property("volume", volume)
        self.config.config["volume"] = volume
        self.config.save_config()
        if volume == 0:
            self.volume_button.set_label("🔇")
        elif volume < 0.5:
            self.volume_button.set_label("🔉")
        else:
            self.volume_button.set_label("🔊")

    def on_mute_toggle(self, button):
        """Toggle mute"""
        if not self.player:
            return
        current_volume = self.player.get_property("volume")
        if current_volume > 0:
            self.last_volume = current_volume
            self.player.set_property("volume", 0)
            self.volume_scale.set_value(0)
        else:
            volume = getattr(self, 'last_volume', self.config.config["volume"])
            self.player.set_property("volume", volume)
            self.volume_scale.set_value(volume * 100)

    def on_seek_value_change(self, scale, scroll_type, value):
        """Handle seek value change in GTK4"""
        if (hasattr(self, 'pipeline') and
            self.pipeline.get_state(0).state == Gst.State.PLAYING):
            success, duration = self.pipeline.query_duration(Gst.Format.TIME)
            if success and duration > 0:
                seek_ns = int((value / 100.0) * duration)
                self.pipeline.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    seek_ns,
                )
        elif self.player.get_state(0).state == Gst.State.PLAYING:
            success, duration = self.player.query_duration(Gst.Format.TIME)
            if success and duration > 0:
                seek_ns = int((value / 100.0) * duration)
                self.player.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    seek_ns,
                )

    def on_eq_bus_message(self, bus, message):
        """Handle GStreamer bus messages for EQ pipeline only"""
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            self.status_label.set_text(f"EQ Error: {error}")
            print(f"EQ Pipeline Error: {error}")
            print(f"Debug: {debug}")

            # Switch back to playbin on EQ errors
            if hasattr(self, 'current_uri') and self.player:
                self.active_backend = "playbin"
                self.player.set_property("uri", self.current_uri)
                self.player.set_state(Gst.State.PLAYING)
                self.status_label.set_text("Switched to default audio")

        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            print(f"EQ Pipeline Warning: {warning}")

        elif message.type == Gst.MessageType.TAG:
            tags = message.parse_tag()
            title_found, title = tags.get_string("title")
            if title_found:
                self.title_label.set_text(title)

    def on_bus_message(self, bus, message):
        """Handle GStreamer bus messages with enhanced audio error handling"""
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            error_str = str(error)
            self.status_label.set_text(f"Error: {error}")
            print(f"GStreamer Error: {error}")
            print(f"Debug: {debug}")

            # Check for audio device busy errors and attempt recovery
            if "Device is being used by another application" in error_str or "busy" in error_str.lower():
                print("Audio device busy, attempting to switch audio sink...")
                self._handle_audio_device_busy()
            else:
                # For other errors, stop playback
                self.on_stop(None)

        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            print(f"GStreamer Warning: {warning}")

        elif message.type == Gst.MessageType.TAG:
            tags = message.parse_tag()
            title_found, title = tags.get_string("title")
            if title_found:
                self.title_label.set_text(title)
            artist_found, artist = tags.get_string("artist")
            if artist_found:
                # Update display with artist info if needed
                pass

    def _handle_audio_device_busy(self):
        """Handle audio device busy errors by switching to alternative audio sink"""
        if hasattr(self, '_retry_count') and self._retry_count >= 2:
            print("Max retry attempts reached, stopping playback")
            self.stop()
            return

        if not hasattr(self, '_retry_count'):
            self._retry_count = 0
        self._retry_count += 1

        # Store current position if playing
        was_playing = False
        current_pos = 0
        if hasattr(self, 'player') and self.player:
            state = self.player.get_state(0).state
            if state == Gst.State.PLAYING:
                was_playing = True
                success, pos = self.player.query_position(Gst.Format.TIME)
                if success:
                    current_pos = pos

        # Try alternative audio sinks
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

                    # Stop current playback
                    if self.player:
                        self.player.set_state(Gst.State.NULL)

                    # Set new audio sink
                    self.player.set_property("audio-sink", new_sink)
                    print(f"Switched to fallback audio sink: {sink_name}")

                    # Resume playback if it was playing
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

        print("No alternative audio sinks available")
        self.stop()

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
        """Update progress from main thread to avoid GStreamer threading issues"""
        try:
            if (hasattr(self, 'pipeline') and self.pipeline and
                self.pipeline.get_state(0).state == Gst.State.PLAYING):
                success, position = self.pipeline.query_position(Gst.Format.TIME)
                success2, duration = self.pipeline.query_duration(Gst.Format.TIME)
            elif self.player:
                success, position = self.player.query_position(Gst.Format.TIME)
                success2, duration = self.player.query_duration(Gst.Format.TIME)
            else:
                return  # No player available, skip update
            if success and success2 and duration > 0:
                self.current_time_label.set_text(self.format_time_ns(position))
                self.total_time_label.set_text(self.format_time_ns(duration))
                percent = (position / duration) * 100
                self.progress_scale.set_value(percent)
        except Exception as e:
            print(f"Error updating progress: {e}")

    def setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts"""
        pass

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
        GLib.timeout_add(500, self.update_progress)

    def start_auto_save_timer(self):
        """Start the auto-save timer to save playlist every 30 seconds"""
        if self.auto_save_timer_id:
            GLib.source_remove(self.auto_save_timer_id)
        self.auto_save_timer_id = GLib.timeout_add_seconds(30, self.auto_save_playlist)

    def stop_auto_save_timer(self):
        """Stop the auto-save timer"""
        if self.auto_save_timer_id:
            GLib.source_remove(self.auto_save_timer_id)
            self.auto_save_timer_id = None

    def auto_save_playlist(self):
        """Auto-save playlist callback"""
        self.save_playlist()
        return True

    def save_playlist(self):
        """Save current playlist to file"""
        playlist = []
        for i in range(len(self.playlist_store)):
            track = self.playlist_store.get_item(i)
            playlist.append(track.filename)
        self.config.save_playlist(playlist)

    def on_decodebin_pad_added(self, element, pad):
        """Handle new pads from decodebin"""
        sinkpad = self.audioconvert.get_static_pad("sink")
        if sinkpad and not sinkpad.is_linked():
            pad.link(sinkpad)

    def load_equalizer_settings(self):
        """Load equalizer settings from config"""
        if self.equalizer:
            pass  # Placeholder - equalizer temporarily disabled

    def on_equalizer_enabled_toggled(self, button):
        """Handle equalizer toggle with safe implementation"""
        if button.get_active():
            # Enable equalizer - switch to equalizer pipeline if playing
            if self.player and self.player.get_state(Gst.CLOCK_TIME_NONE)[1] == Gst.State.PLAYING:
                self.switch_to_equalizer_pipeline()
            print("Equalizer enabled")
        else:
            # Disable equalizer - switch back to default pipeline
            if hasattr(self, 'pipeline') and self.pipeline and self.pipeline.get_state(0).state == Gst.State.PLAYING:
                self.switch_to_default_pipeline()
            print("Equalizer disabled")

    def on_preset_changed(self, combo, param_spec):
        """Handle equalizer preset selection change"""
        if not self.equalizer:
            return

        # Safety check - if eq_band_scales doesn't exist, just update config
        if not hasattr(self, 'eq_band_scales'):
            selected = combo.get_selected()
            if selected == Gtk.INVALID_LIST_POSITION:
                return

            preset_name = combo.get_model().get_string(selected)
            if preset_name in self.config.equalizer_presets:
                bands = self.config.equalizer_presets[preset_name]
                self.config.config["equalizer_bands"] = bands.copy()
                self.config.config["equalizer_preset"] = preset_name
                self.config.save_config()
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

            # Update band scales and labels
            for i, value in enumerate(bands):
                if i < len(self.eq_band_scales):
                    self.eq_band_scales[i]["scale"].set_value(value)
                    self.eq_band_scales[i]["label"].set_text(f"{value:.1f}")
                    if self.equalizer:
                        self.equalizer.set_property(f"band{i}", value)

    def switch_to_equalizer_pipeline(self):
        """Switch from default playbin to custom pipeline with equalizer"""
        if not self.player:
            return

        # Ensure equalizer pipeline is set up
        if not hasattr(self, 'pipeline') or not self.pipeline:
            self.setup_equalizer_pipeline()
            if not hasattr(self, 'pipeline') or not self.pipeline:
                print("Failed to setup equalizer pipeline")
                return

        was_playing = False
        current_pos = 0
        if self.player.get_state(Gst.CLOCK_TIME_NONE)[1] == Gst.State.PLAYING:
            was_playing = True
            _, current_pos = self.player.query_position(Gst.Format.TIME)

        self.player.set_state(Gst.State.NULL)

        if hasattr(self, 'current_uri') and hasattr(self, 'source') and self.source:
            self.source.set_property("uri", self.current_uri)
            self.pipeline.set_state(Gst.State.PLAYING)
            if was_playing and current_pos > 0:
                self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, current_pos)

    def switch_to_default_pipeline(self):
        """Switch back to default playbin"""
        if hasattr(self, 'pipeline') and self.pipeline and self.pipeline.get_state(0).state == Gst.State.PLAYING:
            was_playing = True
            current_pos = self.pipeline.query_position(Gst.Format.TIME)[1]
        else:
            was_playing = False
            current_pos = 0
        if hasattr(self, 'pipeline') and self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        if hasattr(self, 'current_uri') and self.player:
            self.player.set_property("uri", self.current_uri)
            if was_playing:
                self.player.set_state(Gst.State.PLAYING)
                if current_pos > 0:
                    self.player.seek_simple(
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                        current_pos
                    )

    def on_eq_scale_changed(self, scale, band):
        """Handle scale value change with throttling to prevent segfaults"""
        self.eq_values[band] = scale.get_value()

        if not self.eq_update_pending:
            self.eq_update_pending = True
            GLib.timeout_add(40, self.apply_eq_safely)

    def apply_eq_safely(self):
        """Apply EQ values safely with state checking"""
        self.eq_update_pending = False

        if not self.equalizer:
            return False

        # Only apply EQ when using EQ pipeline
        if self.active_backend != "eq":
            return False

        # Check if EQ pipeline is in playing state
        if not hasattr(self, 'pipeline') or not self.pipeline:
            return False

        state = self.pipeline.get_state(0).state
        if state != Gst.State.PLAYING:
            return False

        # Apply EQ values safely
        for i, val in enumerate(self.eq_values):
            try:
                self.equalizer.set_property(f"band{i}", val)
            except Exception as e:
                print(f"EQ band {i} failed:", e)

        return False  # Run once

if __name__ == '__main__':
    app = EnhancedWinampPlayer()
    app.run(sys.argv)
