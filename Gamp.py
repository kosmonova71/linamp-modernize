#!/usr/bin/env python3
"""Linamp audio player and visualizer. Provides GUI, audio engine, and optional ProjectM visualizations."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import ctypes
import gc
import json
import logging
import math
import os
import random
import select
import subprocess
import sys
import threading
import time

import cairo
import numpy as np
import shutil

try:
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    sd = None
    SOUNDDEVICE_AVAILABLE = False

try:
    from OpenGL import GL as gl
    from OpenGL.GL import shaders
    OPENGL_AVAILABLE = True
except ImportError:
    gl = None
    shaders = None
    OPENGL_AVAILABLE = False
    logging.getLogger(__name__).warning("OpenGL support not available, using fallback mode")

import gi

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gtk, Gst, GLib, Gio, GObject, Gdk, Pango
    GTK_GST_AVAILABLE = True
except Exception as e:
    # Allow importing module in headless/test environments; GUI/GStreamer features will be disabled
    Gtk = Gst = GLib = Gio = GObject = Gdk = Pango = None
    GTK_GST_AVAILABLE = False
    _GTK_GST_LOAD_ERROR = e

if GTK_GST_AVAILABLE and Gst is not None:
    try:
        Gst.init(None)
    except Exception as e:
        # If GStreamer init fails, mark as unavailable
        GTK_GST_AVAILABLE = False
        _GTK_GST_INIT_ERROR = e

# ProjectMVisualizer is now embedded directly in this file
PROJECTM_VISUALIZER_AVAILABLE = True
ProjectMVisualizer = True  # Will be defined after ProjectMVisualizerWrapper class


APP_ID = "org.example.Linamp"
APP_NAME = "Linamp"
APP_VERSION = "2.0.0"
SAMPLE_RATE = 44100
AUDIO_BUFFER_SIZE = 1024
BEAT_THRESHOLD = 1.3
CROSSFADE_DURATION_DEFAULT = 3.0
VOLUME_DEFAULT = 0.8
VISUALIZATION_BARS = 64
SMOOTHING_FACTOR = 0.7
PARTICLE_MAX_COUNT = 100
ANIMATION_FPS = 60
SUPPORTED_AUDIO_FORMATS = ['.mp3', '.flac', '.ogg', '.wav', '.m4a', '.aac', '.wma']
PLAYLIST_FORMATS = ['.m3u', '.pls']
WINDOW_MIN_WIDTH = 500
WINDOW_MIN_HEIGHT = 700
WINDOW_DEFAULT_WIDTH = 500
WINDOW_DEFAULT_HEIGHT = 700
EQ_FREQUENCIES = [29, 59, 119, 237, 474, 947, 1889, 3770, 7523, 15005]
EQ_BAND_RANGE = (-24, 12)
METADATA_CACHE_SIZE = 1000
VISUALIZATION_FPS = 60
AUDIO_UPDATE_INTERVAL = 50
PLAYLIST_BATCH_SIZE = 50
MEMORY_CLEANUP_THRESHOLD = 5000
LOGGING_INTERVAL = 1000

@lru_cache(maxsize=METADATA_CACHE_SIZE)
def get_file_metadata(file_path: str, mtime: float) -> Tuple[str, str, str]:
    """Return (title, artist, album) inferred from filename or metadata."""
    try:
        basename = os.path.basename(file_path)
        name_without_ext = os.path.splitext(basename)[0]
        if ' - ' in name_without_ext:
            parts = name_without_ext.split(' - ', 1)
            if len(parts) == 2:
                artist = parts[0].strip()
                title = parts[1].strip()
            else:
                artist = "Unknown Artist"
                title = name_without_ext
        else:
            artist = "Unknown Artist"
            title = name_without_ext
        return (title, artist, "Unknown Album")
    except Exception:
        return (os.path.basename(file_path), "Unknown Artist", "Unknown Album")

# Ensure log directory exists and fall back gracefully if not
LOG_DIR = Path.home() / ".config" / "linamp"
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    # If we can't create the directory, fallback to a safe location
    LOG_DIR = Path(os.getenv("XDG_RUNTIME_DIR", "/tmp"))

log_handlers = [logging.StreamHandler()]
try:
    file_handler = logging.FileHandler(LOG_DIR / 'linamp.log')
    log_handlers.insert(0, file_handler)
except Exception as e:
    # If FileHandler can't be created, continue with stream-only logging
    logging.getLogger().warning(f"Could not create file handler at {LOG_DIR}: {e}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)
logger = logging.getLogger(APP_NAME)

@dataclass
class AudioConfig:
    """Configuration for audio playback and equalizer settings."""
    volume: float = VOLUME_DEFAULT
    crossfade_enabled: bool = False
    crossfade_duration: float = CROSSFADE_DURATION_DEFAULT
    beat_aware_enabled: bool = False
    eq_values: Optional[List[float]] = None
    preamp_value: float = 0.0

    def __post_init__(self):
        if self.eq_values is None:
            self.eq_values = [0.0] * len(EQ_FREQUENCIES)

@dataclass
class VisualizationConfig:
    """Settings controlling visualizer mode and appearance."""
    mode: int = 0
    color_scheme: int = 0
    intensity: float = 1.0
    glow_effect: bool = True
    particle_effects: bool = True
    projectm_enabled: bool = False

class Config:
    """Persistent application configuration management."""

    def __init__(self):
        self.config_dir = Path.home() / ".config" / "linamp"
        self.config_file = self.config_dir / "config.json"
        self.playlist_file = self.config_dir / "playlist.json"
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Config directory: {self.config_dir}")
        except OSError as e:
            logger.error(f"Failed to create config directory: {e}")
        self.default = {
            "volume": VOLUME_DEFAULT,
            "last_directory": str(Path.home()),
            "window_width": WINDOW_DEFAULT_WIDTH,
            "window_height": WINDOW_DEFAULT_HEIGHT,
            "crossfade_enabled": False,
            "crossfade_duration": CROSSFADE_DURATION_DEFAULT,
            "beat_aware_enabled": False,
            "visualization_mode": 0,
            "color_scheme": 0,
            "glow_effect": True,
            "auto_next_enabled": True,
            "repeat_enabled": False,
            "random_enabled": False
        }
        self.data = self.load()

    def load(self) -> Dict[str, Any]:
        if not self.config_file.exists():
            logger.info("Config file not found, using defaults")
            return self.default.copy()
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                logger.warning("Invalid config format, using defaults")
                return self.default.copy()
            config = {**self.default, **loaded}
            config["volume"] = max(0.0, min(1.0, float(config.get("volume", VOLUME_DEFAULT))))
            config["window_width"] = max(WINDOW_MIN_WIDTH, int(config.get("window_width", WINDOW_DEFAULT_WIDTH)))
            config["window_height"] = max(WINDOW_MIN_HEIGHT, int(config.get("window_height", WINDOW_DEFAULT_HEIGHT)))
            config["crossfade_duration"] = max(1.0, min(10.0, float(config.get("crossfade_duration", CROSSFADE_DURATION_DEFAULT))))
            logger.info("Configuration loaded successfully")
            return config
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in config: {e}")
            return self.default.copy()
        except OSError as e:
            logger.error(f"Error loading config file: {e}")
            return self.default.copy()

    def save(self) -> bool:
        try:
            if self.config_file.exists():
                backup_file = self.config_file.with_suffix('.json.bak')
                shutil.copy2(self.config_file, backup_file)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            logger.debug("Configuration saved successfully")
            return True
        except OSError as e:
            logger.error(f"Failed to save config file: {e}")
            return False
        except (TypeError, ValueError) as e:
            logger.error(f"Invalid config data: {e}")
            return False

    def load_playlist(self) -> List[str]:
        if not self.playlist_file.exists():
            logger.info("Playlist file not found")
            return []
        try:
            with open(self.playlist_file, 'r', encoding='utf-8') as f:
                playlist = json.load(f)
            if not isinstance(playlist, list):
                logger.warning("Invalid playlist format")
                return []
            valid_playlist = [path for path in playlist if os.path.exists(path)]
            if len(valid_playlist) != len(playlist):
                logger.info(f"Removed {len(playlist) - len(valid_playlist)} non-existent files from playlist")
                self.save_playlist(valid_playlist)
            logger.info(f"Loaded {len(valid_playlist)} tracks from playlist")
            return valid_playlist
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in playlist: {e}")
            return []
        except OSError as e:
            logger.error(f"Error loading playlist file: {e}")
            return []

    def save_playlist(self, playlist: List[str]) -> bool:
        try:
            if not isinstance(playlist, list):
                logger.error("Invalid playlist format for saving")
                return False
            with open(self.playlist_file, 'w', encoding='utf-8') as f:
                json.dump(playlist, f, indent=2, ensure_ascii=False)
            logger.debug(f"Saved {len(playlist)} tracks to playlist")
            return True
        except OSError as e:
            logger.error(f"Failed to save playlist file: {e}")
            return False
        except (TypeError, ValueError) as e:
            logger.error(f"Invalid playlist data: {e}")
            return False

    def get_audio_config(self) -> AudioConfig:
        return AudioConfig(
            volume=self.data.get("volume", VOLUME_DEFAULT),
            crossfade_enabled=self.data.get("crossfade_enabled", False),
            crossfade_duration=self.data.get("crossfade_duration", CROSSFADE_DURATION_DEFAULT),
            beat_aware_enabled=self.data.get("beat_aware_enabled", False),
            eq_values=self.data.get("eq_values", [0.0] * len(EQ_FREQUENCIES)),
            preamp_value=self.data.get("preamp_value", 0.0)
        )

    def get_visualization_config(self) -> VisualizationConfig:
        return VisualizationConfig(
            mode=self.data.get("visualization_mode", 0),
            color_scheme=self.data.get("color_scheme", 0),
            intensity=self.data.get("intensity", 1.0),
            glow_effect=self.data.get("glow_effect", True),
            particle_effects=self.data.get("particle_effects", True),
            projectm_enabled=self.data.get("projectm_enabled", False)
        )

class Track(GObject.Object):
    filename = GObject.Property(type=str)
    title = GObject.Property(type=str)
    artist = GObject.Property(type=str)
    album = GObject.Property(type=str)
    duration = GObject.Property(type=int)
    metadata_loaded = GObject.Property(type=bool, default=False)

    def __init__(self, filename: str, title: Optional[str] = None,
                 artist: Optional[str] = None, album: Optional[str] = None):
        super().__init__()
        if not filename or not os.path.exists(filename):
            logger.error(f"Invalid track filename: {filename}")
            raise ValueError(f"Invalid filename: {filename}")
        self.filename = filename
        try:
            mtime = os.path.getmtime(filename)
            cached_title, cached_artist, cached_album = get_file_metadata(filename, mtime)
            self.title = title or cached_title
            self.artist = artist or cached_artist
            self.album = album or cached_album
            self.duration = 0
            self.metadata_loaded = True
            logger.debug(f"Metadata loaded for: {self.title}")
        except Exception as e:
            logger.warning(f"Failed to load metadata for {self.filename}: {e}")
            self.title = title or os.path.basename(filename)
            self.artist = artist or "Unknown Artist"
            self.album = album or "Unknown Album"
            self.duration = 0
            self.metadata_loaded = False

    def get_display_name(self) -> str:
        if self.artist != "Unknown Artist":
            return f"{self.artist} - {self.title}"
        return self.title

    def get_file_size(self) -> int:
        try:
            return os.path.getsize(self.filename)
        except OSError:
            return 0

    def is_valid_audio_file(self) -> bool:
        ext = os.path.splitext(self.filename)[1].lower()
        return ext in SUPPORTED_AUDIO_FORMATS

class AudioEngine(GObject.Object):

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self._setup_audio_components()
        self._setup_crossfade()
        self._setup_beat_detection()
        self._setup_visualization()
        self._connect_signals()
        logger.info("AudioEngine initialized successfully")

    def _setup_audio_components(self):
        # Be robust in headless/test environments where GStreamer may be missing
        try:
            if not GTK_GST_AVAILABLE or Gst is None:
                logger.info("GStreamer not available; audio engine running in synthetic/test mode")
                self.player = None
                self.eq_bin = None
                self.preamp = None
                self.eq = None
                return

            self.eq_values = [0.0] * len(EQ_FREQUENCIES)
            self.preamp_value = 0.0
            self.player = Gst.ElementFactory.make("playbin", "player")
            if not self.player:
                raise RuntimeError("Failed to create playbin element")
            self.eq_bin = None
            self.preamp = Gst.ElementFactory.make("volume", "preamp")
            self.eq = Gst.ElementFactory.make("equalizer-10bands", "eq")
            if not self.preamp or not self.eq:
                logger.warning("Equalizer components not available, audio processing limited")
            else:
                self.setup_equalizer()
            self.player.set_property("volume", self.config.data.get("volume", VOLUME_DEFAULT))
        except Exception as e:
            logger.error(f"Failed to setup audio components: {e}")
            raise

    def _setup_crossfade(self):
        audio_config = self.config.get_audio_config()
        self.crossfade_enabled = audio_config.crossfade_enabled
        self.crossfade_duration = audio_config.crossfade_duration
        self.next_player = None
        self.crossfade_timer = None
        self.crossfade_start_time = 0
        self.current_volume = 1.0
        self.next_volume = 0.0
        self.crossfade_active = False
        if self.crossfade_enabled:
            logger.info(f"Crossfade enabled from config ({self.crossfade_duration}s duration)")

    def enable_crossfade(self, duration: float = CROSSFADE_DURATION_DEFAULT):
        self.crossfade_enabled = True
        self.crossfade_duration = duration
        logger.info(f"Crossfade enabled with duration: {duration}s")

    def disable_crossfade(self):
        self.crossfade_enabled = False
        self.crossfade_active = False
        if self.next_player:
            self.next_player.set_state(Gst.State.NULL)
            self.next_player = None
        if self.crossfade_timer:
            GLib.source_remove(self.crossfade_timer)
            self.crossfade_timer = None
        logger.info("Crossfade disabled")

    def prepare_crossfade(self, next_uri: str):
        if not self.crossfade_enabled or self.crossfade_active:
            return False
        try:
            if self.next_player:
                self.next_player.set_state(Gst.State.NULL)
                self.next_player = None
            self.next_player = Gst.ElementFactory.make("playbin", "next_player")
            if not self.next_player:
                return False
            self._setup_player_audio(self.next_player)
            self.next_player.set_property("uri", next_uri)
            self.next_player.set_property("volume", 0.0)
            self.next_player.set_state(Gst.State.PAUSED)
            logger.debug(f"Crossfade prepared for: {next_uri}")
            return True
        except Exception as e:
            logger.error(f"Failed to prepare crossfade: {e}")
            return False

    def start_crossfade(self):
        if not self.crossfade_enabled or not self.next_player or self.crossfade_active:
            return False
        try:
            self.crossfade_active = True
            self.next_player.set_state(Gst.State.PLAYING)
            if self.beat_aware_enabled and hasattr(self, 'current_bpm') and self.current_bpm > 0:
                next_beat = self.get_next_beat_time()
                if next_beat:
                    current_time = self.query_position_seconds()
                    delay = (next_beat - current_time) * 1000
                    if delay > 0 and delay < 5000:
                        GLib.timeout_add(int(delay), self._start_crossfade_animation)
                        logger.info(f"Beat-aware crossfade scheduled in {delay:.0f}ms (BPM: {self.current_bpm:.1f})")
                        return True
            self._start_crossfade_animation()
            return True
        except Exception as e:
            logger.error(f"Failed to start crossfade: {e}")
            self._complete_crossfade()
            return False

    def _start_crossfade_animation(self):
        if not self.next_player or not self.crossfade_active:
            return
        duration = self.crossfade_duration
        if self.beat_aware_enabled and hasattr(self, 'current_bpm') and self.current_bpm > 0:
            beat_duration = 60.0 / self.current_bpm
            phrase_duration = beat_duration * 4
            if phrase_duration >= 1.0 and phrase_duration <= 8.0:
                duration = phrase_duration
                logger.debug(f"Using beat-aware crossfade duration: {duration:.1f}s")
        self.crossfade_start_time = time.time()
        self.current_volume = 1.0
        self.next_volume = 0.0
        if self.crossfade_timer:
            GLib.source_remove(self.crossfade_timer)
        self.crossfade_timer = GLib.timeout_add(50, self._update_crossfade)
        logger.info(f"Crossfade started ({duration:.1f}s duration)")

    def _update_crossfade(self):
        try:
            if not self.crossfade_enabled or not self.next_player or not self.crossfade_active:
                self._complete_crossfade()
                return False
            elapsed = time.time() - self.crossfade_start_time
            progress = min(elapsed / self.crossfade_duration, 1.0)
            fade_curve = progress * progress
            self.current_volume = max(0.0, 1.0 - fade_curve)
            self.next_volume = min(1.0, fade_curve)
            self.player.set_property("volume", self.current_volume)
            self.next_player.set_property("volume", self.next_volume)
            if progress >= 1.0:
                self._complete_crossfade()
                return False
            return True
        except Exception as e:
            logger.error(f"Error updating crossfade: {e}")
            self._complete_crossfade()
            return False

    def _complete_crossfade(self):
        try:
            self.crossfade_active = False
            if self.crossfade_timer:
                GLib.source_remove(self.crossfade_timer)
                self.crossfade_timer = None
            if self.next_player:
                self.player.set_state(Gst.State.NULL)
                old_player = self.player
                self.player = self.next_player
                self.next_player = None
                self.player.set_property("volume", self.config.data.get("volume", VOLUME_DEFAULT))
                if old_player:
                    old_player.set_state(Gst.State.NULL)
                logger.info("Crossfade completed")
                if hasattr(self, '_app_instance') and self._app_instance:
                    GLib.idle_add(self._app_instance.update_playlist_highlight)
        except Exception as e:
            logger.error(f"Error completing crossfade: {e}")
            self.crossfade_active = False
            if self.crossfade_timer:
                GLib.source_remove(self.crossfade_timer)
                self.crossfade_timer = None

    def _setup_player_audio(self, player):
        try:
            preamp = Gst.ElementFactory.make("volume", "preamp")
            eq = Gst.ElementFactory.make("equalizer-10bands", "eq")
            if preamp and eq:
                audio_bin = Gst.Bin.new("audio_bin")
                audio_sink = Gst.ElementFactory.make("autoaudiosink", "audio_sink")
                audio_bin.add(preamp)
                audio_bin.add(eq)
                audio_bin.add(audio_sink)
                preamp.link(eq)
                eq.link(audio_sink)
                audio_sink_pad = Gst.GhostPad.new("sink", preamp.get_static_pad("sink"))
                audio_src_pad = Gst.GhostPad.new("src", audio_sink.get_static_pad("src"))
                audio_bin.add_pad(audio_sink_pad)
                audio_bin.add_pad(audio_src_pad)
                player.set_property("audio-sink", audio_bin)
        except Exception as e:
            logger.error(f"Failed to setup player audio: {e}")

    def _setup_beat_detection(self):
        audio_config = self.config.get_audio_config()
        self.beat_aware_enabled = audio_config.beat_aware_enabled
        self.beat_positions = []
        self.current_bpm = 0.0
        self.beat_analysis_thread = None
        self.buffer_lock = threading.Lock()
        self.audio_buffer = []
        self.sample_rate = SAMPLE_RATE
        self.beat_sync_offset = 0.1
        if self.beat_aware_enabled:
            self.enable_beat_aware()

    def enable_beat_aware(self):
        self.beat_aware_enabled = True
        if not self.beat_analysis_thread:
            self.beat_analysis_thread = threading.Thread(target=self._beat_analysis_loop, daemon=True)
            self.beat_analysis_thread.start()
        logger.info("Beat detection enabled")

    def disable_beat_aware(self):
        self.beat_aware_enabled = False
        if self.beat_analysis_thread:
            self.beat_analysis_thread = None
        logger.info("Beat detection disabled")

    def _beat_analysis_loop(self):
        while self.beat_aware_enabled:
            try:
                if hasattr(self, '_get_audio_samples'):
                    samples = self._get_audio_samples()
                    if samples is not None and len(samples) > 0:
                        with self.buffer_lock:
                            self.audio_buffer.extend(samples)
                            if len(self.audio_buffer) > self.sample_rate * 2:
                                self.audio_buffer = self.audio_buffer[-self.sample_rate * 2:]
                        if len(self.audio_buffer) >= 1024:
                            self._analyze_beats()
                time.sleep(0.05)
            except Exception as e:
                logger.error(f"Beat analysis error: {e}")
                time.sleep(0.1)

    def _get_audio_samples(self):
        try:
            if len(self.audio_buffer) < 512:
                return [random.uniform(-0.1, 0.1) for _ in range(512)]
            return None
        except Exception:
            return None

    def _analyze_beats(self):
        try:
            with self.buffer_lock:
                if len(self.audio_buffer) < 1024:
                    return
                audio_data = np.array(self.audio_buffer[-1024:], dtype=np.float32)
            fft = np.fft.fft(audio_data)
            freqs = np.fft.fftfreq(len(audio_data), 1/self.sample_rate)
            bass_mask = (np.abs(freqs) >= 60) & (np.abs(freqs) <= 250)
            bass_energy = np.sum(np.abs(fft[bass_mask])**2)
            total_energy = np.sum(np.abs(fft)**2)
            if total_energy > 0:
                energy_ratio = bass_energy / total_energy
                beat_threshold = 1.3
                if energy_ratio > beat_threshold:
                    current_time = time.time()
                    if (not self.beat_positions or
                        current_time - self.beat_positions[-1] > 0.2):
                        self.beat_positions.append(current_time)
                        self.beat_detected = True
                        if len(self.beat_positions) >= 4:
                            recent_beats = self.beat_positions[-8:]
                            if len(recent_beats) >= 2:
                                intervals = np.diff(recent_beats)
                                avg_interval = np.mean(intervals)
                                if avg_interval > 0:
                                    self.current_bpm = 60.0 / avg_interval
                        if len(self.beat_positions) > 100:
                            self.beat_positions = self.beat_positions[-50:]
                else:
                    self.beat_detected = False
        except Exception as e:
            logger.error(f"Beat analysis error: {e}")

    def get_current_bpm(self) -> float:
        return self.current_bpm

    def is_beat_detected(self) -> bool:
        return self.beat_detected

    def get_beat_positions(self) -> list:
        return self.beat_positions.copy()

    def _setup_visualization(self):
        self.visualizer_ref = None
        self.audio_levels = [0] * VISUALIZATION_BARS
        self.beat_detected = False

    def _connect_signals(self):
        try:
            bus = self.player.get_bus()
            if not bus:
                raise RuntimeError("Failed to get GStreamer bus")
            bus.add_signal_watch()
            bus.connect("message::error", self.on_error)
            bus.connect("message::eos", self.on_eos)
            self.player.connect("about-to-finish", self.on_about_to_finish)
        except Exception as e:
            logger.error(f"Failed to connect GStreamer signals: {e}")
            raise

    @property
    def eq_frequencies(self) -> List[int]:
        return EQ_FREQUENCIES.copy()

    @property
    def eq_presets(self) -> Dict[str, List[int]]:
        return {
            "Flat": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            "Rock": [5, 4, 3, 1, 0, -1, -2, -3, -3, -4],
            "Pop": [-1, 2, 4, 4, 2, 0, -1, -1, -1, -1],
            "Jazz": [3, 2, 1, 2, -2, -1, 1, 2, 3, 4],
            "Classical": [0, 0, 0, 0, 0, 0, -1, -1, -1, -2],
            "Electronic": [4, 3, 1, 0, 1, 2, 4, 5, 6, 7],
            "Bass Boost": [7, 6, 5, 4, 3, 2, 0, 0, 0, 0],
            "Vocal": [-4, -2, 0, 2, 4, 4, 3, 2, 1, 0],
            "Dance": [6, 5, 3, 1, 0, 1, 3, 5, 6, 7],
            "Acoustic": [0, 1, 2, 2, 2, 0, -1, -2, -2, -2],
            "Metal": [7, 6, 5, 4, 3, 2, 1, 0, 0, 0],
            "Hip Hop": [5, 4, 3, 1, 0, -1, 1, 3, 4, 5],
            "Blues": [4, 3, 2, 1, 0, 1, 2, 3, 4, 4],
            "Country": [2, 1, 0, 1, 2, 3, 3, 2, 1, 0],
            "Reggae": [0, 0, 0, -2, 0, 2, 4, 4, 2, 0],
            "Live": [-2, -1, 0, 1, 2, 2, 1, 0, -1, -2],
            "Podcast": [-3, -2, -1, 0, 2, 3, 3, 2, 1, 0],
            "Loudness": [5, 4, 3, 2, 1, 0, -1, -2, -3, -4],
            "Club": [6, 5, 4, 2, 0, 1, 3, 5, 6, 7],
            "Party": [7, 6, 5, 3, 1, 0, 2, 4, 6, 8],
            "Soft Rock": [3, 2, 1, 0, -1, 0, 1, 2, 3, 3],
            "Hard Rock": [8, 7, 6, 4, 2, 0, -1, -2, -3, -4],
            "Punk": [8, 7, 6, 5, 4, 3, 2, 1, 0, 0],
            "Alternative": [4, 3, 2, 1, 0, 1, 2, 3, 4, 4]
        }

    def setup_equalizer(self):
        try:
            audio_sink = Gst.ElementFactory.make("autoaudiosink", "audio-sink")
            if not audio_sink:
                logger.warning("Could not create autoaudiosink, equalizer disabled")
                return
            self.eq_bin = Gst.Bin.new("eq-bin")
            if not self.eq_bin:
                logger.error("Failed to create equalizer bin")
                return
            self.eq_bin.add(self.preamp)
            self.eq_bin.add(self.eq)
            self.eq_bin.add(audio_sink)
            if not self.preamp.link(self.eq):
                logger.error("Failed to link preamp to equalizer")
                return
            if not self.eq.link(audio_sink):
                logger.error("Failed to link equalizer to audio sink")
                return
            sink_pad = Gst.GhostPad.new("sink", self.preamp.get_static_pad("sink"))
            if not sink_pad:
                logger.error("Failed to create ghost pad")
                return
            self.eq_bin.add_pad(sink_pad)
            self.player.set_property("audio-sink", self.eq_bin)
            self.apply_equalizer_settings()
            logger.info("Equalizer pipeline setup successful")
        except Exception as e:
            logger.error(f"Failed to setup equalizer: {e}")
            try:
                fallback_sink = Gst.ElementFactory.make("autoaudiosink", "fallback-sink")
                if fallback_sink:
                    self.player.set_property("audio-sink", fallback_sink)
                    logger.info("Using fallback audio sink")
            except Exception as fallback_error:
                logger.error(f"Fallback audio sink also failed: {fallback_error}")

    def apply_equalizer_settings(self):
        try:
            if self.eq:
                for i, value in enumerate(self.eq_values):
                    if i < len(EQ_FREQUENCIES):
                        self.eq.set_property(f"band{i}", value)
            if self.preamp:
                linear_volume = 10 ** (self.preamp_value / 20.0)
                self.preamp.set_property("volume", linear_volume)
            logger.debug("Equalizer settings applied")
        except Exception as e:
            logger.error(f"Failed to apply equalizer settings: {e}")

    def set_eq_band(self, band: int, value: float) -> bool:
        try:
            if not (0 <= band < len(EQ_FREQUENCIES)):
                logger.warning(f"Invalid band index: {band}")
                return False
            min_db, max_db = EQ_BAND_RANGE
            if not (min_db <= value <= max_db):
                logger.warning(f"EQ value {value} out of range [{min_db}, {max_db}]")
                return False
            self.eq_values[band] = value
            if self.eq:
                self.eq.set_property(f"band{band}", value)
                logger.debug(f"Set EQ band {band} to {value} dB")
                return True
            else:
                logger.warning("Equalizer not available")
                return False
        except Exception as e:
            logger.error(f"Failed to set EQ band: {e}")
            return False

    def reset_equalizer(self) -> bool:
        """Reset all equalizer bands to 0 dB"""
        try:
            for i in range(len(EQ_FREQUENCIES)):
                self.eq_values[i] = 0.0
                if self.eq:
                    self.eq.set_property(f"band{i}", 0.0)
            logger.debug("Equalizer reset to flat")
            return True
        except Exception as e:
            logger.error(f"Failed to reset equalizer: {e}")
            return False

    def set_preamp(self, value: float) -> bool:
        try:
            min_db, max_db = EQ_BAND_RANGE
            if not (min_db <= value <= max_db):
                logger.warning(f"Preamp value {value} out of range [{min_db}, {max_db}]")
                return False
            self.preamp_value = value
            if self.preamp:
                linear_volume = 10 ** (value / 20.0)
                self.preamp.set_property("volume", linear_volume)
                logger.debug(f"Set preamp to {value} dB")
                return True
            else:
                logger.warning("Preamp not available")
                return False
        except Exception as e:
            logger.error(f"Failed to set preamp: {e}")
            return False

    def get_eq_bands(self) -> List[float]:
        return self.eq_values.copy()

    def get_preamp(self) -> float:
        return self.preamp_value

    def simulate_audio_data(self):
        try:
            current_time = self.query_position_seconds()
            buffer_size = 1024
            with self.buffer_lock:
                if len(self.audio_buffer) > buffer_size * 2:
                    self.audio_buffer = self.audio_buffer[-buffer_size:]
                for _ in range(buffer_size):
                    sample_time = current_time + len(self.audio_buffer) / self.sample_rate
                    bass_freq = 60
                    mid_freq = 200
                    high_freq = 2000
                    bass = math.sin(2 * math.pi * bass_freq * sample_time) * 0.7
                    mid = math.sin(2 * math.pi * mid_freq * sample_time) * 0.3
                    high = math.sin(2 * math.pi * high_freq * sample_time) * 0.2
                    beat_phase = (sample_time * self.current_bpm / 60.0) % 1.0 if self.current_bpm > 0 else 0
                    if beat_phase < 0.1:
                        bass += math.sin(2 * math.pi * 40 * sample_time) * 0.8
                    elif 0.4 < beat_phase < 0.5:
                        high += math.sin(2 * math.pi * 1000 * sample_time) * 0.5
                    noise = random.gauss(0, 0.1)
                    sample = bass + mid + high + noise
                    self.audio_buffer.append(max(-1.0, min(1.0, sample)))
        except Exception as e:
            logger.error(f"Audio simulation error: {e}")
            with self.buffer_lock:
                if len(self.audio_buffer) < 1024:
                    sample_time = current_time + len(self.audio_buffer) / self.sample_rate
                    sample = math.sin(2 * math.pi * 440 * sample_time) * 0.5
                    self.audio_buffer.append(sample)

    def start_beat_analysis(self):
        if self.beat_analysis_thread and self.beat_analysis_thread.is_alive():
            return
        self.beat_analysis_thread = threading.Thread(target=self.beat_analysis_loop, daemon=True)
        self.beat_analysis_thread.start()

    def stop_beat_analysis(self):
        self.beat_analysis_thread = None
        with self.buffer_lock:
            self.audio_buffer.clear()

    def beat_analysis_loop(self):
        try:
            window_size = 1024
            hop_size = 512
            energy_history = []
            beat_threshold = 1.3
            while self.beat_aware_enabled:
                time.sleep(0.01)
                with self.buffer_lock:
                    if len(self.audio_buffer) < window_size:
                        continue
                    window = self.audio_buffer[:window_size]
                    self.audio_buffer = self.audio_buffer[hop_size:]
                energy = np.sum(np.square(window))
                energy_history.append(energy)
                if len(energy_history) > 100:
                    energy_history.pop(0)
                if len(energy_history) >= 10:
                    recent_avg = np.mean(energy_history[-10:-1])
                    if recent_avg > 0 and energy / recent_avg > beat_threshold:
                        current_time = self.query_position_seconds()
                        if current_time > 0:
                            self.beat_positions.append(current_time)
                            self.beat_positions = [b for b in self.beat_positions if current_time - b < 30]
                            if len(self.beat_positions) >= 4:
                                intervals = np.diff(self.beat_positions[-8:])
                                if len(intervals) > 0:
                                    avg_interval = np.mean(intervals)
                                    if avg_interval > 0:
                                        self.current_bpm = 60.0 / avg_interval
        except Exception as e:
            logger.error(f"Beat analysis error: {e}")

    def query_position_seconds(self):
        try:
            ok_pos, pos = self.player.query_position(Gst.Format.TIME)
            if ok_pos:
                return pos / Gst.SECOND
        except (Gst.QueryError, AttributeError):
            pass
        return 0.0

    def get_next_beat_time(self, from_time=None):
        if from_time is None:
            from_time = self.query_position_seconds()
        for beat_pos in self.beat_positions:
            if beat_pos > from_time + self.beat_sync_offset:
                return beat_pos
        if self.current_bpm > 0 and self.beat_positions:
            last_beat = self.beat_positions[-1]
            beat_interval = 60.0 / self.current_bpm
            next_beat = last_beat
            while next_beat <= from_time + self.beat_sync_offset:
                next_beat += beat_interval
            return next_beat
        return None

    def set_visualizer(self, visualizer):
        self.visualizer_ref = visualizer

    def update_visualizer_data(self):
        if not self.visualizer_ref:
            return
        try:
            ok_pos, pos = self.player.query_position(Gst.Format.TIME)
            is_playing = self.player.get_state(Gst.CLOCK_TIME_NONE).state == Gst.State.PLAYING
        except (Gst.QueryError, AttributeError):
            return
        if is_playing and ok_pos:
            current_time = pos / Gst.SECOND
            self.generate_audio_levels(current_time)
            if hasattr(self.visualizer_ref, 'update_audio_levels'):
                self.visualizer_ref.update_audio_levels(self.audio_levels, self.beat_detected)

    def generate_audio_levels(self, time_position):
        try:
            current_volume = self.player.get_property("volume")
            is_playing = self.player.get_state(Gst.CLOCK_TIME_NONE).state == Gst.State.PLAYING
        except (AttributeError, Gst.QueryError):
            current_volume = 0.8
            is_playing = True

        logger.debug(f"Generating audio levels - playing: {is_playing}, volume: {current_volume}")

        if not is_playing or current_volume == 0:
            decay_factor = 0.9
            for i in range(len(self.audio_levels)):
                self.audio_levels[i] *= decay_factor
                if self.audio_levels[i] < 0.01:
                    self.audio_levels[i] = 0
            self.beat_detected = False
            return

        num_levels = len(self.audio_levels)
        base_frequencies = [60, 120, 250, 500, 1000, 2000, 4000, 8000, 12000, 16000]

        # Enhanced audio level generation with better dynamics
        bass_boost = math.sin(time_position * 2.5) * 0.4 + math.sin(time_position * 0.5) * 0.3
        mid_range = math.sin(time_position * 4.0) * 0.3 + math.sin(time_position * 8.0) * 0.2
        high_freq = math.sin(time_position * 12.0) * 0.2 + math.sin(time_position * 20.0) * 0.1

        for i in range(num_levels):
            if i < len(base_frequencies):
                freq = base_frequencies[i]
            else:
                freq_multiplier = 2 ** ((i - len(base_frequencies)) / 8.0)
                freq = min(base_frequencies[-1] * freq_multiplier, 22000)

            if freq < 200:
                freq_response = 1.2 + bass_boost * 0.8
            elif freq < 2000:
                freq_response = 1.0 + mid_range * 0.6
            else:
                freq_response = 0.8 + high_freq * 0.4

            time_mod = math.sin(time_position * 2 + i * 0.3) + math.sin(time_position * 7 + i * 0.7) * 0.5

            if self.current_bpm > 0:
                beat_phase = (time_position * self.current_bpm / 60.0) % 1.0
                if beat_phase < 0.1 and freq < 200:
                    freq_response *= 1.5
                elif 0.4 < beat_phase < 0.5 and freq > 2000:
                    freq_response *= 1.3

            dynamic_factor = 0.3 + 0.4 * (1 + time_mod * 0.5)
            level = (0.3 + freq_response * 0.4 + dynamic_factor + random.gauss(0, 0.05)) * current_volume
            self.audio_levels[i] = max(0.05, min(1.0, level))

        # Beat detection
        avg_level = sum(self.audio_levels) / num_levels
        rms_level = math.sqrt(sum(x*x for x in self.audio_levels) / num_levels)
        current_time = time_position

        if not hasattr(self, '_last_beat_time'):
            self._last_beat_time = -10.0

        beat_interval = 60.0 / self.current_bpm if self.current_bpm > 0 else 1.0
        if (avg_level > 0.65 or rms_level > 0.5) and current_time - self._last_beat_time > beat_interval * 0.6:
            self.beat_detected = True
            self._last_beat_time = current_time
            logger.debug(f"Beat detected at time {current_time:.2f}")
        else:
            self.beat_detected = False

        # Log average level for debugging
        if int(current_time * 2) % 10 == 0:  # Log every 5 seconds
            logger.debug(f"Audio levels - avg: {avg_level:.3f}, rms: {rms_level:.3f}, beat: {self.beat_detected}")

    def apply_eq_preset(self, preset_name):
        if preset_name not in self.eq_presets:
            logger.warning(f"Unknown preset: {preset_name}")
            return False
        preset_values = self.eq_presets[preset_name]
        for i, value in enumerate(preset_values):
            if i < 10:
                self.set_eq_band(i, value)
        logger.info(f"Applied equalizer preset: {preset_name}")
        return True

    def get_eq_presets(self):
        return list(self.eq_presets.keys())

    def get_current_preset_values(self):
        if not self.eq:
            return [0] * 10
        values = []
        for i in range(10):
            try:
                value = self.eq.get_property(f"band{i}")
                values.append(value)
            except (AttributeError, TypeError):
                values.append(0)
        return values

    def on_error(self, bus, msg):
        err, debug = msg.parse_error()
        logger.error(f"Playback error: {err} - {debug}")

    def on_about_to_finish(self, element):
        if not self.crossfade_enabled or self.crossfade_active:
            return
        app = None
        if hasattr(self, '_app_instance'):
            app = self._app_instance
        if app and app.store.get_n_items() > 0 and app.auto_next_enabled:
            next_track = self.get_next_track(app)
            if next_track:
                next_uri = GLib.filename_to_uri(next_track.filename)
                if self.prepare_crossfade(next_uri):
                    logger.debug(f"Crossfade prepared for next track: {next_track.title}")
                    for i in range(app.store.get_n_items()):
                        if app.store.get_item(i) == next_track:
                            app.selection.set_selected(i)
                            break

    def on_eos(self, bus, msg):
        app = None
        if hasattr(self, '_app_instance'):
            app = self._app_instance
        if self.crossfade_active:
            logger.debug("EOS received during active crossfade, letting crossfade handle transition")
            return
        if self.crossfade_enabled and self.next_player:
            if self.start_crossfade():
                return
        if app and app.store.get_n_items() > 0 and app.auto_next_enabled:
            if app.is_random:
                app.play_random_next()
                app.play_selected()
            elif app.is_repeat or app.selection.get_selected() < app.store.get_n_items() - 1:
                app.on_next_clicked(None)
            else:
                self.stop()
        else:
            self.stop()

    def get_next_track(self, app):
        if app.is_random:
            available_indices = list(range(app.store.get_n_items()))
            if app.random_history:
                for index in app.random_history[-5:]:
                    if index in available_indices:
                        available_indices.remove(index)
            if not available_indices:
                available_indices = list(range(app.store.get_n_items()))
            next_index = random.choice(available_indices)
            return app.store.get_item(next_index)
        else:
            current_index = app.selection.get_selected()
            if current_index < app.store.get_n_items() - 1:
                return app.store.get_item(current_index + 1)
            elif app.is_repeat:
                return app.store.get_item(0)
        return None

    def play_uri(self, uri: str):
        self.player.set_state(Gst.State.NULL)
        self.player.set_property("uri", uri)
        self.player.set_state(Gst.State.PLAYING)

    def play(self):
        self.player.set_state(Gst.State.PLAYING)

    def is_playing(self):
        try:
            state = self.player.get_state(Gst.CLOCK_TIME_NONE).state
            return state == Gst.State.PLAYING
        except Exception:
            return False

    def pause(self):
        self.player.set_state(Gst.State.PAUSED)

    def stop(self):
        self.player.set_state(Gst.State.NULL)

    def seek_percent(self, percent: float):
        ok, duration = self.player.query_duration(Gst.Format.TIME)
        if ok and duration > 0:
            seek_pos = int((percent / 100.0) * duration)
            self.player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, seek_pos)

    def query_position_percent(self) -> float:
        ok_pos, pos = self.player.query_position(Gst.Format.TIME)
        ok_dur, dur = self.player.query_duration(Gst.Format.TIME)
        if ok_pos and ok_dur and dur > 0:
            return (pos / dur) * 100.0
        return 0.0

    def query_position(self) -> int:
        try:
            ok_pos, pos = self.player.query_position(Gst.Format.TIME)
            if ok_pos:
                return pos
            return 0
        except (Gst.QueryError, AttributeError):
            return 0

    def query_duration(self) -> int:
        try:
            ok_dur, dur = self.player.query_duration(Gst.Format.TIME)
            if ok_dur:
                return dur
            return 0
        except (Gst.QueryError, AttributeError):
            return 0

    def set_volume(self, vol: float):
        self.player.set_property("volume", vol)
        self.config.data["volume"] = vol
        self.config.save()

    def _generate_synthetic_audio(self):
        logger.debug("Using synthetic audio data for visualization")
        while self.running:
            t = time.time()
            dummy_data = np.array([
                (np.sin(t * 2 * np.pi * 440) * 0.1 +
                 np.sin(t * 2 * np.pi * 880) * 0.05 +
                 random.random() * 0.02 - 0.01)
                for _ in range(1024)
            ], dtype=np.float32)
            if not self.audio_queue.full():
                self.audio_queue.put(dummy_data)
            time.sleep(0.1)

class PresetManager:
    """Helper class to find and load projectM presets and categorize them."""

    def __init__(self, preset_dir: Optional[str] = None, custom_preset_dir: Optional[str] = None):
        self.preset_dir = preset_dir or self.find_preset_directory()
        self.custom_preset_dir = custom_preset_dir
        self.available_presets: List[str] = []
        self.preset_categories: Dict[str, List[str]] = {}

    def check_pulseaudio(self) -> bool:
        return bool(shutil.which("projectM-pulseaudio")) or any(
            os.path.exists(p) for p in [
                "/usr/bin/projectM-pulseaudio",
                "/usr/local/bin/projectM-pulseaudio",
                "/opt/projectM/bin/projectM-pulseaudio"
            ]
        )

    def find_preset_directory(self) -> str:
        preset_dirs = [
            "/usr/share/projectM/presets",
            "/usr/local/share/projectM/presets",
            "/usr/lib64/projectM/presets",
            "/usr/lib/projectM/presets",
            "/opt/projectM/presets",
            "~/.projectM/presets"
        ]

        for preset_dir in preset_dirs:
            expanded_dir = os.path.expanduser(preset_dir)
            if os.path.exists(expanded_dir):
                return expanded_dir

        # Return default if none found
        return "/usr/share/projectM/presets"

    def load_presets(self) -> None:
        """Populate available_presets and preset_categories."""
        try:
            self.available_presets.clear()
            self.preset_categories.clear()
            if self.preset_dir and os.path.exists(self.preset_dir):
                presets = [f for f in os.listdir(self.preset_dir)
                           if f.endswith('.milk') or f.endswith('.prjm')]
                self.available_presets.extend(presets)
                for preset in presets:
                    category = self.categorize_preset(preset)
                    if category not in self.preset_categories:
                        self.preset_categories[category] = []
                    self.preset_categories[category].append(preset)

            if self.custom_preset_dir and os.path.exists(self.custom_preset_dir):
                custom_presets = [f for f in os.listdir(self.custom_preset_dir)
                                  if f.endswith('.milk') or f.endswith('.prjm')]
                self.available_presets.extend(custom_presets)
                for preset in custom_presets:
                    category = self.categorize_preset(preset)
                    if category not in self.preset_categories:
                        self.preset_categories[category] = []
                    self.preset_categories[category].append(preset)

            if not self.available_presets:
                logger.info("No projectM presets found, using embedded visualizations")
                self.create_embedded_presets()

            logger.info(f"Loaded {len(self.available_presets)} presets in {len(self.preset_categories)} categories")
        except Exception:
            logger.exception("Error loading presets, falling back to embedded presets")
            self.create_embedded_presets()

    def create_embedded_presets(self) -> None:
        embedded_presets = [
            "Wave Form", "Spectrum Analyzer", "Circular Visual",
            "Particle Field", "Crystal Motion", "Flow State",
            "Energy Pulse", "Rhythm Grid", "Beat Wave",
            "Harmonic Flow", "Frequency Dance", "Audio Landscape"
        ]
        for preset in embedded_presets:
            self.available_presets.append(preset)
            category = self.categorize_preset(preset)
            if category not in self.preset_categories:
                self.preset_categories[category] = []
            self.preset_categories[category].append(preset)

    def categorize_preset(self, preset_name: str) -> str:
        name_lower = preset_name.lower()
        if any(word in name_lower for word in ['wave', 'flow', 'fluid', 'liquid']):
            return "Flowing"
        elif any(word in name_lower for word in ['star', 'space', 'cosmic', 'galaxy']):
            return "Space"
        elif any(word in name_lower for word in ['fire', 'flame', 'heat', 'sun']):
            return "Fire"
        elif any(word in name_lower for word in ['crystal', 'gem', 'diamond', 'glass']):
            return "Crystal"
        elif any(word in name_lower for word in ['bubble', 'drop', 'water', 'ocean']):
            return "Liquid"
        elif any(word in name_lower for word in ['line', 'bar', 'spectrum', 'analyzer']):
            return "Analyzer"
        else:
            return "Abstract"


class ProjectMVisualizerWrapper:

    def check_pulseaudio(self):
        if shutil.which("projectM-pulseaudio"):
            return True
        common_paths = [
            "/usr/bin/projectM-pulseaudio",
            "/usr/local/bin/projectM-pulseaudio",
            "/opt/projectM/bin/projectM-pulseaudio"
        ]
        for path in common_paths:
            if os.path.exists(path):
                return True
        return False

    def find_preset_directory(self):
        """Find projectM preset directory (deprecated on wrapper - use PresetManager)"""
        return self.preset_manager.find_preset_directory()

    def load_presets(self):
        """Load projectM presets from directory"""
        try:
            self.preset_categories = {}
            if os.path.exists(self.preset_dir):
                presets = [f for f in os.listdir(self.preset_dir)
                          if f.endswith('.milk') or f.endswith('.prjm')]
                self.available_presets.extend(presets)
                for preset in presets:
                    category = self.categorize_preset(preset)
                    if category not in self.preset_categories:
                        self.preset_categories[category] = []
                    self.preset_categories[category].append(preset)
            if hasattr(self, 'custom_preset_dir') and self.custom_preset_dir and os.path.exists(self.custom_preset_dir):
                custom_presets = [f for f in os.listdir(self.custom_preset_dir)
                                if f.endswith('.milk') or f.endswith('.prjm')]
                self.available_presets.extend(custom_presets)
                for preset in custom_presets:
                    category = self.categorize_preset(preset)
                    if category not in self.preset_categories:
                        self.preset_categories[category] = []
                    self.preset_categories[category].append(preset)
            if not self.available_presets:
                logger.info("No projectM presets found, using embedded visualizations")
                self.create_embedded_presets()
            logger.info(f"Loaded {len(self.available_presets)} projectM presets in {len(self.preset_categories)} categories")
        except Exception:
            logger.exception("Error loading presets, falling back to embedded presets")
            self.create_embedded_presets()

    def create_embedded_presets(self):
        """Create embedded preset list when no projectM presets available"""
        embedded_presets = [
            "Wave Form", "Spectrum Analyzer", "Circular Visual",
            "Particle Field", "Crystal Motion", "Flow State",
            "Energy Pulse", "Rhythm Grid", "Beat Wave",
            "Harmonic Flow", "Frequency Dance", "Audio Landscape"
        ]
        for preset in embedded_presets:
            self.available_presets.append(preset)
            category = self.categorize_preset(preset)
            if category not in self.preset_categories:
                self.preset_categories[category] = []
            self.preset_categories[category].append(preset)

    def categorize_preset(self, preset_name):
        """Categorize preset by name"""
        name_lower = preset_name.lower()
        if any(word in name_lower for word in ['wave', 'flow', 'fluid', 'liquid']):
            return "Flowing"
        elif any(word in name_lower for word in ['star', 'space', 'cosmic', 'galaxy']):
            return "Space"
        elif any(word in name_lower for word in ['fire', 'flame', 'heat', 'sun']):
            return "Fire"
        elif any(word in name_lower for word in ['crystal', 'gem', 'diamond', 'glass']):
            return "Crystal"
        elif any(word in name_lower for word in ['bubble', 'drop', 'water', 'ocean']):
            return "Liquid"
        elif any(word in name_lower for word in ['line', 'bar', 'spectrum', 'analyzer']):
            return "Analyzer"
        else:
            return "Abstract"

    def __init__(self):
        self.projectm_process = None
        self.preset_manager = PresetManager()
        self.projectm_available = self.preset_manager.check_pulseaudio()
        self.projectm_version = "projectM-pulseaudio" if self.projectm_available else None
        self.available_presets = []
        self.preset_categories = {}
        self.current_preset_index = 0
        self.status_text = "projectM Ready" if self.projectm_available else "Embedded Visualizer"
        self.visualizer = self  # Self-reference for embedded mode
        self.audio_data = np.zeros(1024, dtype=np.float32)
        self.running = False
        # Initialize texture data for simulated projectM
        self.projectm_texture_data = np.random.randint(0, 256, (256, 256, 3), dtype=np.uint8)
        self.texture_generation_time = 0
        self.preset_dir = self.preset_manager.preset_dir
        self.projectm_config = {
            'texture_size': 1024,
            'mesh_x': 32,
            'mesh_y': 24,
            'fps': 30
        }
        # Load presets via manager
        self.preset_manager.load_presets()
        self.available_presets = self.preset_manager.available_presets
        self.preset_categories = self.preset_manager.preset_categories
        if self.projectm_available:
            self.start_projectm()
        else:
            logger.info("projectM not available, using embedded visualizer")
            # manager already created embedded presets if necessary
        self.projectm_available = self.check_pulseaudio()
        self.projectm_version = "projectM-pulseaudio" if self.projectm_available else None
        self.available_presets = []
        self.preset_categories = {}
        self.current_preset_index = 0
        self.status_text = "projectM Ready" if self.projectm_available else "Embedded Visualizer"
        self.visualizer = self  # Self-reference for embedded mode
        self.audio_data = np.zeros(1024, dtype=np.float32)
        self.running = False
        # Initialize texture data for simulated projectM
        self.projectm_texture_data = np.random.randint(0, 256, (256, 256, 3), dtype=np.uint8)
        self.texture_generation_time = 0
        self.preset_dir = self.find_preset_directory()
        self.projectm_config = {
            'texture_size': 1024,
            'mesh_x': 32,
            'mesh_y': 24,
            'fps': 30
        }
        if self.projectm_available:
            self.load_presets()
            self.start_projectm()
        else:
            logger.info("projectM not available, using embedded visualizer")
            self.create_embedded_presets()

    def update_audio_levels(self, audio_levels, beat_detected=False):
        if self.visualizer and hasattr(self.visualizer, 'audio_data'):
            try:
                audio_array = np.array(audio_levels, dtype=np.float32)

                if len(audio_array) < 1024:
                    padded = np.zeros(1024, dtype=np.float32)
                    padded[:len(audio_array)] = audio_array
                    self.visualizer.audio_data = padded
                else:
                    self.visualizer.audio_data = audio_array[:1024]

                # Update projectM instance if available
                if self.projectm_instance and hasattr(self.projectm_instance, 'addPCMData'):
                    try:
                        # Convert to the format expected by projectM
                        pcm_data = (audio_array * 32767).astype(np.int16)
                        self.projectm_instance.addPCMData(pcm_data)
                        logger.debug("Updated projectM with PCM data")
                    except Exception as e:
                        logger.debug(f"Failed to update projectM PCM data: {e}")

                # Generate dynamic texture data based on audio levels
                self.generate_projectm_texture(audio_levels, beat_detected)
            except Exception as e:
                logger.error(f"Error updating audio levels: {e}")

    def generate_projectm_texture(self, audio_levels, beat_detected=False):
        try:
            import time
            current_time = time.time()
            if current_time - self.texture_generation_time < 0.033:  # Limit to ~30 FPS
                return

            self.texture_generation_time = current_time

            # Create a dynamic texture based on audio data
            height, width = 256, 256
            texture = np.zeros((height, width, 3), dtype=np.uint8)

            # Use audio levels to create visualization
            if len(audio_levels) > 0:
                # Create radial patterns based on audio
                center_y, center_x = height // 2, width // 2
                y, x = np.ogrid[:height, :width]

                # Calculate distance from center
                dist_from_center = np.sqrt((x - center_x)**2 + (y - center_y)**2)

                # Use audio data to create patterns
                audio_intensity = max(0.1, np.mean(audio_levels)) if audio_levels else 0.1
                beat_factor = 2.0 if beat_detected else 1.0

                # Create base color gradient
                for i in range(height):
                    for j in range(width):
                        dist = dist_from_center[i, j]
                        angle = np.arctan2(i - center_y, j - center_x)

                        # Create swirling pattern
                        swirl = np.sin(angle * 3 + current_time * 2) * 0.5 + 0.5
                        wave = np.sin(dist * 0.1 + current_time * 3) * 0.5 + 0.5

                        # Audio-reactive colors
                        r = int(255 * audio_intensity * (0.5 + 0.5 * swirl))
                        g = int(255 * audio_intensity * (0.5 + 0.5 * wave))
                        b = int(255 * beat_factor * (0.5 + 0.5 * np.sin(current_time * 4)))

                        texture[i, j] = [min(255, r), min(255, g), min(255, b)]

                # Add frequency bars
                for i in range(min(len(audio_levels), 32)):
                    level = audio_levels[i] if i < len(audio_levels) else 0
                    bar_height = int(level * height * 0.8)
                    bar_x = int((i / 32.0) * width)

                    for y in range(height - bar_height, height):
                        if 0 <= bar_x < width and 0 <= y < height:
                            # Frequency-based coloring
                            hue = (i / 32.0) * 360
                            color = self.hsv_to_rgb(hue / 360, 0.8, level)
                            texture[y, bar_x] = [
                                int(color[0] * 255),
                                int(color[1] * 255),
                                int(color[2] * 255)
                            ]

                # Add beat effects
                if beat_detected:
                    # Create expanding rings
                    for ring in range(3):
                        ring_radius = (current_time * 50 + ring * 30) % (width // 2)
                        ring_mask = np.abs(dist_from_center - ring_radius) < 2
                        texture[ring_mask] = [255, 255, 255]  # White rings for beats

            else:
                # Fallback pattern when no audio
                for i in range(height):
                    for j in range(width):
                        dist = np.sqrt((i - center_y)**2 + (j - center_x)**2)
                        pattern = np.sin(dist * 0.1 + current_time) * 0.5 + 0.5
                        texture[i, j] = [
                            int(128 * pattern),
                            int(64 * pattern),
                            int(192 * pattern)
                        ]

            self.projectm_texture_data = texture

        except Exception:
            logger.exception("Error generating projectM texture")
            # Fallback to random texture
            self.projectm_texture_data = np.random.randint(0, 256, (256, 256, 3), dtype=np.uint8)

    def on_button_press(self, gesture, n_press, x, y):
        pass

    def next_visualization_mode(self):
        self.status_text = "GL Visualizer Mode"
        if self.visualizer:
            pass

    def next_projectm_preset(self):
        if self.visualizer:
            self.status_text = "Next Preset (GL Mode)"

    def previous_projectm_preset(self):
        if self.visualizer:
            self.status_text = "Previous Preset (GL Mode)"

    def initialize_projectm(self):
        if self.visualizer:
            self.status_text = "GL Visualizer Initialized"

    def stop_projectm(self):
        if self.visualizer:
            self.visualizer.running = False
            self.status_text = "GL Visualizer Stopped"

    def start_projectm(self):
        if self.visualizer:
            self.visualizer.running = True
            self.status_text = "GL Visualizer Started"

    def get_visualizer_mode_name(self):
        return "GL Visualizer"

    def get_color_scheme_name(self):
        return "Default"

    def queue_draw(self):
        pass

ProjectMVisualizer = ProjectMVisualizerWrapper

class Linamp(Gtk.Application):

    def __init__(self):
        super().__init__(application_id="com.example.gamp")
        try:
            self.config = Config()
            logger.info("Configuration loaded successfully")
            self.audio = AudioEngine(self.config)
            self.audio._app_instance = self
            logger.info("Audio engine initialized")
            self.store = Gio.ListStore.new(Track)
            self.selection = Gtk.SingleSelection.new(self.store)
            self.selection.connect("notify::selected-item", self.on_selection_changed)
            self.current_track = None
            self.visualizer = None
            self.projectm_visualizer = ProjectMVisualizerWrapper()  # Initialize ProjectM visualizer
            # Synchronize projectM attributes from wrapper
            self.projectm_available = self.projectm_visualizer.projectm_available
            self.projectm_version = self.projectm_visualizer.projectm_version
            self.available_presets = self.projectm_visualizer.available_presets
            self.current_preset_index = self.projectm_visualizer.current_preset_index
            self.projectm_texture_data = self.projectm_visualizer.projectm_texture_data
            self.is_repeat = self.config.data.get("repeat_enabled", False)
            self.is_random = self.config.data.get("random_enabled", False)
            self.random_history = []
            self.auto_next_enabled = self.config.data.get("auto_next_enabled", True)
            self.playlist_view = None
            self.last_frame_time = 0
            self.frame_interval = 1000 / VISUALIZATION_FPS
            self.animation_phase = 0.0
            self.visualization_mode = self.config.data.get("visualization_mode", 0)
            self.color_scheme = self.config.data.get("color_scheme", 0)
            self.glow_effect = self.config.data.get("glow_effect", True)
            self.intensity = self.config.data.get("intensity", 1.0)
            self.smoothing_factor = 0.8
            self.bars = VISUALIZATION_BARS
            self.smoothed_levels = [0.1] * VISUALIZATION_BARS  # Initialize with small non-zero values
            self.audio_levels = [0.1] * VISUALIZATION_BARS  # Initialize with small non-zero values
            self.beat_detected = False
            self._synthetic_audio_time = 0.0  # Initialize synthetic audio time
            self.preset_categories = {}
            self.available_presets = []
            self.current_preset_index = 0
            self._current_category_index = 0
            self._eq_popover = None
            self.particle_systems = [
                {
                    'particles': [],
                    'color': (0.2, 0.6, 1.0),
                    'max_particles': 50,
                    'birth_rate': 2,
                    'lifetime': 2.0,
                    'enabled': True
                },
                {
                    'particles': [],
                    'color': (1.0, 0.4, 0.2),
                    'max_particles': 30,
                    'birth_rate': 1,
                    'lifetime': 3.0,
                    'enabled': True
                }
            ]
            self._load_saved_playlist()
            logger.info(f"{APP_NAME} {APP_VERSION} initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize application: {e}")
            raise

    def on_selection_changed(self, selection, param):
        try:
            selected_item = selection.get_selected_item()
            if selected_item:
                logger.debug(f"Selection changed to: {selected_item.title}")
        except Exception as e:
            logger.error(f"Error handling selection change: {e}")

    def _load_saved_playlist(self):
        try:
            playlist_paths = self.config.load_playlist()
            loaded_count = 0
            for path in playlist_paths:
                if os.path.exists(path):
                    try:
                        track = Track(path)
                        self.store.append(track)
                        loaded_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to load track {path}: {e}")
            logger.info(f"Loaded {loaded_count} tracks from saved playlist")
        except Exception as e:
            logger.error(f"Error loading saved playlist: {e}")

    def do_activate(self):
        try:
            win = self.props.active_window
            if not win:
                win = self._create_main_window()
                self.build_ui(win)
            win.present()
            logger.info("Application window activated")
        except Exception as e:
            logger.error(f"Failed to activate application: {e}")
            self._show_error_dialog(f"Failed to start application: {e}")

    def on_window_close(self, window=None, *args):
        try:
            width, height = window.get_default_size()
            self.config.data["window_width"] = width
            self.config.data["window_height"] = height
            self.config.save()
            self.cleanup()
            if hasattr(self, 'audio'):
                self.audio.stop()
            return False
        except Exception as e:
            logger.error(f"Error during window close: {e}")
            return False

    def _show_error_dialog(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self.props.active_window,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Error",
            secondary_text=message
        )
        dialog.connect("response", lambda d, r: d.close())
        dialog.present()

    def _create_main_window(self) -> Gtk.ApplicationWindow:
        win = Gtk.ApplicationWindow(application=self, title=f"{APP_NAME} v{APP_VERSION}")
        width = self.config.data.get("window_width", WINDOW_DEFAULT_WIDTH)
        height = self.config.data.get("window_height", WINDOW_DEFAULT_HEIGHT)
        win.set_default_size(width, height)
        win.set_size_request(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        win.connect("close-request", self.on_window_close)
        return win

    def add_section_separator(self, parent_box):
        separator = Gtk.Separator.new(Gtk.Orientation.HORIZONTAL)
        separator.add_css_class("section-separator")
        parent_box.append(separator)

    def build_ui(self, win: Gtk.ApplicationWindow):
        try:
            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            win.set_child(main_box)
            self._setup_keyboard_shortcuts(win)
            paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
            main_box.append(paned)
            left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            left_box.set_margin_top(6)
            left_box.set_margin_bottom(6)
            left_box.set_margin_start(6)
            left_box.set_margin_end(6)
            right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            paned.set_start_child(left_box)
            paned.set_end_child(right_box)
            self.add_section_separator(left_box)
            self.create_playback_controls(left_box)
            self.add_section_separator(left_box)
            self.create_progress_section(left_box)
            self.add_section_separator(left_box)
            self.create_file_management_section(left_box)
            self.add_section_separator(left_box)
            self.create_equalizer_ui(left_box)
            self.add_section_separator(left_box)
            self.create_playlist_section(left_box)
            self.create_visualizer_section(right_box)
            self.audio.set_visualizer(self.projectm_visualizer)
            paned.set_position(int(win.get_default_size().width * 0.4))

            # Enhanced timer setup with error handling
            logger.info("Setting up visualization timers...")
            self._visualizer_timer_id = GLib.timeout_add(AUDIO_UPDATE_INTERVAL, self.update_visualizer)
            logger.info(f"Visualizer timer started with ID: {self._visualizer_timer_id}")
            self._progress_timer_id = GLib.timeout_add(500, self.update_progress)
            self._playlist_timer_id = GLib.timeout_add(200, self.update_playlist_highlight)

            # Initialize synthetic audio data immediately
            self._initialize_synthetic_audio()
            self.status_bar = Gtk.Label()
            self.status_bar.set_halign(Gtk.Align.START)
            self.status_bar.set_margin_start(10)
            self.status_bar.set_margin_end(10)
            self.status_bar.set_margin_top(5)
            self.status_bar.set_margin_bottom(5)
            main_box.append(self.status_bar)
            logger.info("User interface built successfully")
        except Exception as e:
            logger.error(f"Failed to build UI: {e}")
            raise

    def _initialize_synthetic_audio(self):
        """Initialize synthetic audio data for visualization testing"""
        try:
            logger.info("Initializing synthetic audio data for visualization")
            # Initialize with some default audio levels
            import math
            for i in range(len(self.smoothed_levels)):
                # Create a basic sine wave pattern for testing
                level = 0.3 + 0.2 * math.sin(i * 0.3)
                self.smoothed_levels[i] = max(0.1, level)
                self.audio_levels[i] = max(0.1, level)

            # Trigger initial visualization update
            GLib.idle_add(self._trigger_visualizer_redraw)
            logger.info("Synthetic audio data initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize synthetic audio: {e}")

    def on_volume_changed(self, scale):
        try:
            volume = scale.get_value() / 100.0
            if hasattr(self, 'audio') and self.audio:
                self.audio.set_volume(volume)
            self.config.data["volume"] = volume
            self.config.save()
            logger.debug(f"Volume changed to {volume*100:.0f}%")
        except Exception as e:
            logger.error(f"Error changing volume: {e}")
            self._show_error_dialog(f"Failed to change volume: {e}")

    def create_playback_controls(self, parent):
        try:
            controls_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            controls_box.set_halign(Gtk.Align.CENTER)
            controls_box.set_margin_top(6)
            controls_box.set_margin_bottom(6)
            prev_button = Gtk.Button(icon_name="media-skip-backward-symbolic")
            prev_button.set_tooltip_text("Previous Track")
            prev_button.connect("clicked", lambda *_: self.previous_track())
            controls_box.append(prev_button)
            self.play_button = Gtk.Button(icon_name="media-playback-start-symbolic")
            self.play_button.set_tooltip_text("Play/Pause")
            self.play_button.connect("clicked", lambda *_: self.toggle_play_pause())
            controls_box.append(self.play_button)
            random_button = Gtk.Button(icon_name="media-random-symbolic")
            random_button.set_tooltip_text("Random Track")
            random_button.connect("clicked", lambda *_: self.play_random_track())
            controls_box.append(random_button)
            next_button = Gtk.Button(icon_name="media-skip-forward-symbolic")
            next_button.set_tooltip_text("Next Track")
            next_button.connect("clicked", lambda *_: self.next_track())
            controls_box.append(next_button)
            volume_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            volume_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
            volume_box.append(volume_icon)
            volume_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
            volume_scale.set_range(0, 100)
            volume_scale.set_value((self.config.data.get("volume", 0.7) * 100))
            volume_scale.set_draw_value(False)
            volume_scale.set_size_request(100, -1)
            volume_scale.connect("value-changed", self.on_volume_changed)
            volume_box.append(volume_scale)
            controls_box.append(volume_box)
            parent.append(controls_box)
            self.volume_scale = volume_scale
        except Exception as e:
            logger.error(f"Failed to create playback controls: {e}")
            raise

    def _on_key_pressed(self, controller, keyval, keycode, state):
        try:
            keyname = Gdk.keyval_name(keyval).lower()
            ctrl = (state & Gdk.ModifierType.CONTROL_MASK)
            if keyname == 'space':
                if hasattr(self, 'audio'):
                    if self.audio.is_playing():
                        self.audio.pause()
                    else:
                        self.audio.play()
                return True
            elif keyname == 'right' or keyname == 'n':
                self.next_track()
                return True
            elif keyname == 'left' or keyname == 'p':
                self.previous_track()
                return True
            elif keyname == 'up':
                self.adjust_volume(0.1)
                return True
            elif keyname == 'down':
                self.adjust_volume(-0.1)
                return True
            elif keyname == 'f':
                self.seek_relative(5)
                return True
            elif keyname == 'b':
                self.seek_relative(-5)
                return True
            elif keyname == 'f11':
                win = self.props.active_window
                if win.is_fullscreen():
                    win.unfullscreen()
                else:
                    win.fullscreen()
                return True
            elif keyname == 'q' and ctrl:
                self.quit()
                return True
            elif keyname == 'v':
                self.next_visualization_mode()
                return True
            elif keyname == 'f' and ctrl:
                if hasattr(self, 'audio'):
                    if self.audio.crossfade_enabled:
                        self.audio.disable_crossfade()
                        self.status_text = "Crossfade: OFF"
                        logger.info("Crossfade disabled")
                    else:
                        self.audio.enable_crossfade()
                        self.status_text = "Crossfade: ON"
                        logger.info("Crossfade enabled")
                return True
        except Exception as e:
            logger.error(f"Error in key handler: {e}")
        return False

    def _setup_keyboard_shortcuts(self, win: Gtk.ApplicationWindow):
        try:
            key_controller = Gtk.EventControllerKey()
            key_controller.connect('key-pressed', self._on_key_pressed)
            win.add_controller(key_controller)
        except Exception as e:
            logger.warning(f"Failed to setup keyboard shortcuts: {e}")

    def show_status_message(self, message, timeout=3):
        if hasattr(self, 'status_bar'):
            self.status_bar.set_text(message)
            if hasattr(self, '_status_timeout_id') and self._status_timeout_id is not None:
                if GLib.MainContext.default().find_source_by_id(self._status_timeout_id) is not None:
                    GLib.source_remove(self._status_timeout_id)
            self._status_timeout_id = GLib.timeout_add_seconds(timeout, self._clear_status_message)

    def _clear_status_message(self):
        if hasattr(self, 'status_bar'):
            self.status_bar.set_text("")
        if hasattr(self, '_status_timeout_id'):
            self._status_timeout_id = None
        return False

    def render_textured_quad(self):
        try:
            gl.glEnable(gl.GL_TEXTURE_2D)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture_id)
            if hasattr(self, 'vertex_vbo_id'):
                gl.glEnableClientState(gl.GL_VERTEX_ARRAY)
                gl.glEnableClientState(gl.GL_TEXTURE_COORD_ARRAY)
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vertex_vbo_id)
                gl.glVertexPointer(2, gl.GL_FLOAT, 16, None)
                gl.glTexCoordPointer(2, gl.GL_FLOAT, 16, ctypes.c_void_p(8))
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
                gl.glDisableClientState(gl.GL_VERTEX_ARRAY)
                gl.glDisableClientState(gl.GL_TEXTURE_COORD_ARRAY)
            else:
                gl.glBegin(gl.GL_QUADS)
                gl.glTexCoord2f(0.0, 0.0)
                gl.glVertex2f(-1.0, -1.0)
                gl.glTexCoord2f(1.0, 0.0)
                gl.glVertex2f( 1.0, -1.0)
                gl.glTexCoord2f(1.0, 1.0)
                gl.glVertex2f( 1.0,  1.0)
                gl.glTexCoord2f(0.0, 1.0)
                gl.glVertex2f(-1.0,  1.0)
                gl.glEnd()
            gl.glDisable(gl.GL_TEXTURE_2D)
        except Exception as e:
            logger.error(f"Failed to render textured quad: {e}")

    def render_fallback_visualization(self, gl):
        try:
            if hasattr(self, 'shader_program') and self.shader_program:
                gl.glUseProgram(self.shader_program)
            if len(self.audio_levels) > 0:
                vertices = []
                colors = []
                bar_width = 2.0 / min(len(self.audio_levels), 64)
                for i in range(min(len(self.audio_levels), 64)):
                    value = abs(self.audio_levels[i])
                    height = value * 0.8
                    x = -1.0 + i * bar_width
                    vertices.extend([x, -height])
                    colors.extend([0.2, 0.6, 1.0, 1.0])
                    vertices.extend([x + bar_width * 0.8, -height])
                    colors.extend([0.2, 0.6, 1.0, 1.0])
                    vertices.extend([x + bar_width * 0.8, height])
                    colors.extend([0.2, 0.6, 1.0, 1.0])
                    vertices.extend([x, height])
                    colors.extend([0.2, 0.6, 1.0, 1.0])
                vertex_array = np.array(vertices, dtype=np.float32)
                color_array = np.array(colors, dtype=np.float32)
                if hasattr(self, 'vao_id'):
                    gl.glBindVertexArray(self.vao_id)
                else:
                    self.vao_id = gl.glGenVertexArrays(1)
                    gl.glBindVertexArray(self.vao_id)
                if not hasattr(self, 'vertex_buffer_id'):
                    self.vertex_buffer_id = gl.glGenBuffers(1)
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vertex_buffer_id)
                gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_array.nbytes, vertex_array, gl.GL_DYNAMIC_DRAW)
                gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
                gl.glEnableVertexAttribArray(0)
                if not hasattr(self, 'color_buffer_id'):
                    self.color_buffer_id = gl.glGenBuffers(1)
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.color_buffer_id)
                gl.glBufferData(gl.GL_ARRAY_BUFFER, color_array.nbytes, color_array, gl.GL_DYNAMIC_DRAW)
                gl.glVertexAttribPointer(1, 4, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
                gl.glEnableVertexAttribArray(1)
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, len(vertices) // 2)
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
                gl.glBindVertexArray(0)
            else:
                self.render_static_fallback(gl)
            if hasattr(self, 'shader_program') and self.shader_program:
                gl.glUseProgram(0)
        except Exception as e:
            logger.error(f"Fallback visualization failed: {e}")
            self.render_static_fallback(gl)

    def render_static_fallback(self, gl):
        try:
            pulse = (math.sin(time.time() * 2) + 1.0) * 0.5
            radius = 0.3 + pulse * 0.1
            segments = 32
            vertices = [0.0, 0.0]
            colors = [0.2, 0.6, 1.0, 1.0]
            for i in range(segments + 1):
                angle = (i / segments) * 2 * math.pi
                x = math.cos(angle) * radius
                y = math.sin(angle) * radius
                vertices.extend([x, y])
                colors.extend([0.2, 0.6, 1.0, 0.8])
            vertex_array = np.array(vertices, dtype=np.float32)
            color_array = np.array(colors, dtype=np.float32)
            if hasattr(self, 'vao_id'):
                gl.glBindVertexArray(self.vao_id)
            else:
                self.vao_id = gl.glGenVertexArrays(1)
                gl.glBindVertexArray(self.vao_id)
            if not hasattr(self, 'vertex_buffer_id'):
                self.vertex_buffer_id = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vertex_buffer_id)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_array.nbytes, vertex_array, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(0)
            if not hasattr(self, 'color_buffer_id'):
                self.color_buffer_id = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.color_buffer_id)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, color_array.nbytes, color_array, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(1, 4, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(1)
            gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, len(vertices) // 2)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            gl.glBindVertexArray(0)
        except Exception as e:
            logger.error(f"Static fallback failed: {e}")

    def initialize_projectm(self):
        try:
            self.projectm_available = self.start_embedded_external_projectm()
            if self.projectm_available:
                logger.info("Initialized internal visualization system")
                return True
            logger.warning("Failed to initialize internal visualization")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize visualization: {e}")
            return False

    def init_particle_systems(self):
        self.particle_systems = [
            {
                'particles': [],
                'color': (0.2, 0.6, 1.0),
                'max_particles': 50,
                'birth_rate': 2,
                'lifetime': 2.0
            },
            {
                'particles': [],
                'color': (1.0, 0.4, 0.8),
                'max_particles': 30,
                'birth_rate': 1,
                'lifetime': 1.5
            }
        ]

    def start_embedded_external_projectm(self):
        try:
            self.visualization_mode = 0
            self.visualization_modes = [
                "Enhanced Waveform",
                "Circular Spectrum",
                "Radial Analyzer",
                "Particle System",
                "Abstract Flow"
            ]
            if not hasattr(self, 'particle_systems'):
                self.init_particle_systems()
            if not hasattr(self, 'animation_id'):
                self.animation_id = GLib.timeout_add(16, self.on_visualization_timer)
            logger.info("Initialized internal visualization")
            return True
        except Exception as e:
            logger.error(f"Failed to start internal visualization: {e}")
            return False

    def update_audio_levels(self, audio_levels, beat_detected):
        try:
            self.audio_levels = audio_levels
            self.beat_detected = beat_detected
            if len(audio_levels) > 0:
                self.audio_data = np.array(audio_levels, dtype=np.float32) / 32768.0
                if len(self.audio_data) >= 256:
                    window = np.hanning(len(self.audio_data[:256]))
                    windowed = self.audio_data[:256] * window
                    fft_result = np.fft.fft(windowed)
                    self.fft_data = np.abs(fft_result[:128]) / 256.0

            # Sync texture data from ProjectMVisualizerWrapper
            if hasattr(self, 'projectm_visualizer'):
                self.projectm_texture_data = self.projectm_visualizer.projectm_texture_data

            if self.projectm_embedded and hasattr(self, 'projectm_instance'):
                try:
                    self.projectm_instance.audio_framebuffer = self.audio_data.tolist()
                    self.projectm_instance.pcm_data = self.fft_data.tolist()
                except Exception as e:
                    logger.debug(f"Failed to update projectM audio data: {e}")
            self._trigger_visualizer_redraw()
        except Exception as e:
            logger.error(f"Failed to update audio levels: {e}")

    def on_draw(self, area, cr, width, height, user_data=None):
        try:
            # If width or height is 0, nothing to draw
            if width <= 0 or height <= 0:
                return

            current_time = time.time() * 1000
            logger.debug(f"on_draw called - dimensions: {width}x{height}, visualization_mode: {self.visualization_mode}")

            # Frame rate limiting
            if current_time - self.last_frame_time < self.frame_interval:
                return
            self.last_frame_time = current_time

            # Clear the background
            cr.set_source_rgb(0.1, 0.1, 0.1)  # Dark background
            cr.paint()

            # Ensure we have audio data to visualize
            if not hasattr(self, 'audio_levels') or len(self.audio_levels) == 0:
                logger.warning("No audio levels available for visualization - initializing with defaults")
                self.audio_levels = [0.2] * VISUALIZATION_BARS
                self.smoothed_levels = [0.2] * VISUALIZATION_BARS

            # Generate audio levels if needed
            if hasattr(self, 'audio') and self.audio:
                try:
                    if hasattr(self.audio, 'player') and self.audio.player:
                        if self.audio.player.get_state(Gst.CLOCK_TIME_NONE).state == Gst.State.PLAYING:
                            current_position = self.audio.player.query_position(Gst.Format.TIME)[1] / Gst.SECOND
                            self.audio.generate_audio_levels(current_position)
                            logger.debug(f"Generated real audio levels for position {current_position:.2f}")
                        else:
                            # Use synthetic data when not playing
                            if not hasattr(self, '_synthetic_audio_time'):
                                self._synthetic_audio_time = 0.0
                            self._synthetic_audio_time += 0.016  # ~60fps
                            self.audio.generate_audio_levels(self._synthetic_audio_time)
                            logger.debug(f"Generated synthetic audio levels at time {self._synthetic_audio_time:.2f}")
                    else:
                        # No player yet, use synthetic data
                        if not hasattr(self, '_synthetic_audio_time'):
                            self._synthetic_audio_time = 0.0
                        self._synthetic_audio_time += 0.016
                        self.audio.generate_audio_levels(self._synthetic_audio_time)
                except Exception as e:
                    logger.debug(f"Could not get player state, using synthetic data: {e}")
                    if not hasattr(self, '_synthetic_audio_time'):
                        self._synthetic_audio_time = 0.0
                    self._synthetic_audio_time += 0.016
                    self.audio.generate_audio_levels(self._synthetic_audio_time)
            else:
                # No audio engine, create simple synthetic data
                if not hasattr(self, 'audio_levels') or len(self.audio_levels) == 0:
                    self.audio_levels = [0.3] * VISUALIZATION_BARS
                    self.smoothed_levels = [0.3] * VISUALIZATION_BARS

                # Simple synthetic animation
                if not hasattr(self, '_synthetic_audio_time'):
                    self._synthetic_audio_time = 0.0
                self._synthetic_audio_time += 0.016
                for i in range(len(self.audio_levels)):
                    level = 0.3 + 0.2 * math.sin(self._synthetic_audio_time * 2 + i * 0.3)
                    self.audio_levels[i] = max(0.1, level)
                    self.smoothed_levels[i] = self.audio_levels[i]

            # Update smoothed levels
            smoothing_factor = 0.3
            for i in range(len(self.audio_levels)):
                if i < len(self.smoothed_levels):
                    self.smoothed_levels[i] = (1 - smoothing_factor) * self.smoothed_levels[i] + smoothing_factor * self.audio_levels[i]
                else:
                    self.smoothed_levels.append(self.audio_levels[i])

            # Update animation phase for dynamic effects
            if hasattr(self, 'animation_phase'):
                self.animation_phase += 0.016  # ~60fps animation timing
            else:
                self.animation_phase = 0.0

            # Render the current visualization mode
            logger.debug(f"Rendering visualization mode {self.visualization_mode}")
            self._render_current_visualization(cr, width, height)

            # Add status overlay
            self._draw_status_overlay(cr, width, height)

            logger.debug(f"Visualization rendered successfully - mode: {self.visualization_mode}")

        except Exception as e:
            logger.error(f"Error in on_draw: {e}", exc_info=True)
            # Try to draw a fallback pattern
            try:
                self._draw_error_fallback(cr, width, height, str(e))
            except Exception:
                logger.critical("Even fallback visualization failed", exc_info=True)
                pass  # If even fallback fails, just return

    def _draw_test_pattern(self, cr, width, height):
        """Draw a simple test pattern when no audio data is available"""
        logger.info("Drawing test pattern for visualization")

        # Ensure we have some basic audio levels for visualization
        if not hasattr(self, 'audio_levels') or len(self.audio_levels) == 0:
            self.audio_levels = [0.3] * VISUALIZATION_BARS
            self.smoothed_levels = [0.3] * VISUALIZATION_BARS

        # Draw animated bars
        num_bars = min(16, len(self.audio_levels))
        bar_width = width / num_bars
        time_factor = time.time() * 2.0

        for i in range(num_bars):
            # Create animated height pattern using existing audio levels
            if i < len(self.smoothed_levels):
                height_factor = self.smoothed_levels[i]
            else:
                height_factor = 0.5 + 0.3 * math.sin(time_factor + i * 0.5)

            bar_height = height * height_factor * 0.8

            # Color based on position
            color_r = 0.3 + 0.7 * (i / num_bars)
            color_g = 0.2 + 0.5 * math.sin(time_factor * 2 + i)
            color_b = 0.8 + 0.2 * math.cos(time_factor + i * 0.3)

            cr.set_source_rgb(color_r, color_g, color_b)

            x = i * bar_width
            y = height - bar_height
            cr.rectangle(x + 2, y, bar_width - 4, bar_height)
            cr.fill()

        # Add test text
        cr.set_source_rgb(1, 1, 1)
        cr.set_font_size(16)
        text = "Test Pattern - Visualization Active"
        text_width = cr.text_extents(text).width
        cr.move_to((width - text_width) / 2, height / 2)
        cr.show_text(text)

    def _render_current_visualization(self, cr, width, height):
        """Render the current visualization mode"""
        try:
            logger.debug(f"Rendering visualization mode {self.visualization_mode}")

            # Add debug output to file
            with open('/tmp/viz_debug.log', 'a') as f:
                f.write(f"Rendering mode {self.visualization_mode} with {len(self.smoothed_levels)} levels\n")

            # Ensure we have valid data
            if not hasattr(self, 'smoothed_levels') or len(self.smoothed_levels) == 0:
                self.smoothed_levels = [0.3] * VISUALIZATION_BARS
                self.audio_levels = [0.3] * VISUALIZATION_BARS

            if self.visualization_mode == 0:
                self.draw_enhanced_frequency_bars(cr, width, height)
            elif self.visualization_mode == 1:
                self.draw_enhanced_waveform(cr, width, height)
            elif self.visualization_mode == 2:
                self.draw_circular_visualization(cr, width, height)
            elif self.visualization_mode == 3:
                self.draw_spectrum_visualization(cr, width, height)
            elif self.visualization_mode == 4:
                self.draw_particle_visualization(cr, width, height)
            elif self.visualization_mode == 5:
                self.draw_abstract_visualization(cr, width, height)
            elif self.visualization_mode == 6:
                self.draw_projectm_preset_info(cr, width, height)
            else:
                # Fallback to mode 0 if invalid mode
                logger.warning(f"Invalid visualization mode {self.visualization_mode}, falling back to mode 0")
                self.visualization_mode = 0
                self.draw_enhanced_frequency_bars(cr, width, height)

            # Update particles if needed
            if self.visualization_mode >= 4:
                self.update_and_draw_particles(cr, width, height)

            # Debug: Add mode indicator
            cr.set_source_rgb(1, 1, 1)
            cr.set_font_size(12)
            cr.move_to(10, height - 10)
            cr.show_text(f"Mode: {self.visualization_mode}")

        except Exception as e:
            logger.error(f"Failed to render visualization mode {self.visualization_mode}: {e}")
            # Fallback to simple bars
            self._draw_simple_bars_fallback(cr, width, height)

    def _draw_status_overlay(self, cr, width, height):
        """Draw status information overlay"""
        try:
            # Background for status text
            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.rectangle(10, 10, 300, 80)
            cr.fill()

            # Status text
            cr.set_source_rgb(1, 1, 1)
            cr.set_font_size(12)

            # Mode name
            mode_name = self.get_visualizer_mode_name()
            cr.move_to(15, 25)
            cr.show_text(f"Mode: {mode_name}")

            # Audio level indicator
            if hasattr(self, 'audio_levels') and len(self.audio_levels) > 0:
                avg_level = sum(self.audio_levels) / len(self.audio_levels)
                cr.move_to(15, 45)
                cr.show_text(f"Level: {avg_level:.3f}")

                # Beat indicator
                if hasattr(self, 'beat_detected') and self.beat_detected:
                    cr.set_source_rgb(1, 0, 0)  # Red for beat
                    cr.move_to(15, 65)
                    cr.show_text("BEAT!")
                else:
                    cr.set_source_rgb(0.7, 0.7, 0.7)
                    cr.move_to(15, 65)
                    cr.show_text("No Beat")
            else:
                cr.set_source_rgb(1, 0, 0)  # Red for no audio
                cr.move_to(15, 45)
                cr.show_text("No Audio Data")

        except Exception as e:
            logger.debug(f"Failed to draw status overlay: {e}")

    def _draw_error_fallback(self, cr, width, height, error_msg):
        """Draw error fallback pattern"""
        try:
            # Dark blue background instead of red
            cr.set_source_rgb(0.1, 0.1, 0.2)
            cr.paint()

            # Add animated fallback bars even in error mode
            num_bars = 8
            bar_width = width / num_bars
            time_factor = time.time() * 1.5

            for i in range(num_bars):
                height_factor = 0.3 + 0.2 * math.sin(time_factor + i * 0.8)
                bar_height = height * height_factor * 0.6

                # Blue gradient colors
                color_r = 0.2 + 0.3 * (i / num_bars)
                color_g = 0.3 + 0.4 * (i / num_bars)
                color_b = 0.8 + 0.2 * math.sin(time_factor + i)

                cr.set_source_rgb(color_r, color_g, color_b)

                x = i * bar_width
                y = height - bar_height
                cr.rectangle(x + 3, y, bar_width - 6, bar_height)
                cr.fill()

            # Error text
            cr.set_source_rgb(1, 1, 0.8)
            cr.set_font_size(12)
            text = f"Visualization Error: {error_msg[:30]}..." if len(error_msg) > 30 else f"Visualization Error: {error_msg}"
            text_width = cr.text_extents(text).width
            cr.move_to((width - text_width) / 2, height / 2)
            cr.show_text(text)

            # Status text
            cr.set_source_rgb(0.8, 0.8, 0.8)
            cr.set_font_size(10)
            status_text = "Fallback visualization active - timer running"
            status_width = cr.text_extents(status_text).width
            cr.move_to((width - status_width) / 2, height / 2 + 20)
            cr.show_text(status_text)

        except Exception as e:
            logger.error(f"Even error fallback failed: {e}")

    def _draw_simple_bars_fallback(self, cr, width, height):
        """Simple bar fallback when visualization fails"""
        try:
            if not hasattr(self, 'audio_levels') or len(self.audio_levels) == 0:
                return

            num_bars = len(self.audio_levels)
            bar_width = width / num_bars

            for i, level in enumerate(self.audio_levels):
                bar_height = height * level
                x = i * bar_width
                y = height - bar_height

                # Color based on level
                color = 0.3 + 0.7 * level
                cr.set_source_rgb(color, color * 0.8, color * 0.5)

                cr.rectangle(x, y, bar_width - 1, bar_height)
                cr.fill()

        except Exception as e:
            logger.error(f"Simple bars fallback failed: {e}")

    def _trigger_visualizer_redraw(self):
        """Trigger a visualizer redraw from the main thread"""
        try:
            if hasattr(self, 'drawing_area') and self.drawing_area:
                logger.debug("Triggering visualizer redraw from main thread")
                self.drawing_area.queue_draw()
            elif hasattr(self, 'visualizer') and self.visualizer:
                logger.debug("Triggering visualizer redraw")
                self.visualizer.queue_draw()
        except Exception as e:
            logger.error(f"Failed to trigger visualizer redraw: {e}")

    def draw_projectm_preset_info(self, cr, width, height):
        # Draw projectM texture if available
        if hasattr(self, 'projectm_visualizer') and hasattr(self.projectm_visualizer, 'projectm_texture_data') and self.projectm_visualizer.projectm_texture_data is not None:
            try:
                # Convert numpy array to Cairo surface
                height_tex, width_tex, channels = self.projectm_visualizer.projectm_texture_data.shape
                if channels == 3:
                    # Create ARGB32 format data - ensure writable copy
                    rgba = np.zeros((height_tex, width_tex, 4), dtype=np.uint8)
                    rgba[:, :, 0] = 255  # A
                    rgba[:, :, 1] = self.projectm_visualizer.projectm_texture_data[:, :, 0]  # R
                    rgba[:, :, 2] = self.projectm_visualizer.projectm_texture_data[:, :, 1]  # G
                    rgba[:, :, 3] = self.projectm_visualizer.projectm_texture_data[:, :, 2]  # B
                    # Make sure the array is contiguous and writable
                    rgba_data = np.ascontiguousarray(rgba)
                    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, width_tex)
                    surface = cairo.ImageSurface.create_for_data(
                        rgba_data,
                        cairo.FORMAT_ARGB32,
                        width_tex, height_tex, stride
                    )
                else:
                    # Assume already correct format - ensure writable copy
                    tex_data = np.ascontiguousarray(self.projectm_visualizer.projectm_texture_data)
                    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, width_tex)
                    surface = cairo.ImageSurface.create_for_data(
                        tex_data,
                        cairo.FORMAT_RGB24,
                        width_tex, height_tex, stride
                    )
                # Scale to fit the drawing area
                scale_x = width / width_tex
                scale_y = height / height_tex
                scale = min(scale_x, scale_y)
                scaled_width = width_tex * scale
                scaled_height = height_tex * scale
                x = (width - scaled_width) / 2
                y = (height - scaled_height) / 2
                cr.save()
                cr.translate(x, y)
                cr.scale(scale, scale)
                cr.set_source_surface(surface, 0, 0)
                cr.paint()
                cr.restore()
                surface.finish()
            except Exception as e:
                logger.error(f"Failed to draw projectM texture: {e}")
                # Fallback to text
                self._draw_projectm_fallback_text(cr, width, height)
        else:
            self._draw_projectm_fallback_text(cr, width, height)

    def _draw_projectm_fallback_text(self, cr, width, height):
        if hasattr(self, 'current_preset_index') and self.current_preset_index < len(self.available_presets):
            current_preset = self.available_presets[self.current_preset_index]
            category = self.categorize_preset(current_preset)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(16)
            title_text = "projectM Preset Mode"
            title_extents = cr.text_extents(title_text)
            title_x = (width - title_extents.width) / 2
            title_y = 50
            cr.move_to(title_x, title_y)
            cr.show_text(title_text)
            cr.set_font_size(14)
            preset_text = f"Preset: {current_preset[:30]}"
            if len(current_preset) > 30:
                preset_text += "..."
            preset_extents = cr.text_extents(preset_text)
            preset_x = (width - preset_extents.width) / 2
            preset_y = title_y + 30
            cr.move_to(preset_x, preset_y)
            cr.show_text(preset_text)
            cr.set_font_size(12)
            category_text = f"Category: {category}"
            category_extents = cr.text_extents(category_text)
            category_x = (width - category_extents.width) / 2
            category_y = preset_y + 25
            cr.move_to(category_x, category_y)
            cr.show_text(category_text)
            cr.set_font_size(11)
            cr.set_source_rgba(0.7, 0.7, 0.7, 0.8)
            instructions = [
                "Scroll down: Next category",
                "Left click: Next preset",
                "Right click: Previous preset",
                "Middle click: Back to built-in modes"
            ]
            inst_y = category_y + 40
            for instruction in instructions:
                inst_extents = cr.text_extents(instruction)
                inst_x = (width - inst_extents.width) / 2
                cr.move_to(inst_x, inst_y)
                cr.show_text(instruction)
                inst_y += 20
            self.draw_projectm_background(cr, width, height)
        else:
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(16)
            text = "projectM Mode - No Presets Loaded"
            extents = cr.text_extents(text)
            x = (width - extents.width) / 2
            y = height / 2
            cr.move_to(x, y)
            cr.show_text(text)

    def update_visualizer(self):
        try:
            logger.debug(f"Visualizer update called - visualizer: {self.visualizer}")

            # Always update synthetic audio time for animation when not playing
            if not hasattr(self, '_synthetic_audio_time'):
                self._synthetic_audio_time = 0.0
            self._synthetic_audio_time += 0.016  # ~60fps

            # Generate synthetic audio levels when not playing real audio
            if hasattr(self, 'audio') and self.audio:
                try:
                    if hasattr(self.audio, 'player') and self.audio.player:
                        if self.audio.player.get_state(Gst.CLOCK_TIME_NONE).state != Gst.State.PLAYING:
                            self.audio.generate_audio_levels(self._synthetic_audio_time)
                    else:
                        # No player yet, use synthetic data
                        self.audio.generate_audio_levels(self._synthetic_audio_time)
                except Exception:
                    # If we can't get player state, use synthetic data
                    self.audio.generate_audio_levels(self._synthetic_audio_time)
            else:
                # No audio engine, create simple synthetic data
                if not hasattr(self, 'audio_levels'):
                    self.audio_levels = [0.3] * VISUALIZATION_BARS
                    self.smoothed_levels = [0.3] * VISUALIZATION_BARS

                # Simple synthetic animation
                import math
                for i in range(len(self.audio_levels)):
                    level = 0.3 + 0.2 * math.sin(self._synthetic_audio_time * 2 + i * 0.3)
                    self.audio_levels[i] = max(0.1, level)
                    self.smoothed_levels[i] = self.audio_levels[i]

            if self.visualizer:
                logger.debug("Calling queue_draw on visualizer")
                self.visualizer.queue_draw()
                # Also trigger redraw if we have a drawing area
                if hasattr(self, 'drawing_area') and self.drawing_area:
                    logger.debug("Triggering drawing area redraw")
                    self.drawing_area.queue_draw()
            else:
                logger.warning("No visualizer reference found")
                # Try to trigger redraw on the visualizer section
                if hasattr(self, 'drawing_area') and self.drawing_area:
                    logger.debug("Fallback: triggering drawing area redraw")
                    self.drawing_area.queue_draw()
            return True
        except Exception as e:
            logger.error(f"Failed to update visualizer: {e}")
            # Don't return False - keep the timer running
            return True

    def draw_projectm_background(self, cr, width, height):
        if len(self.smoothed_levels) > 0:
            for i in range(0, len(self.smoothed_levels), 4):
                level = self.smoothed_levels[i]
                if level > 0.1:
                    x = (i / len(self.smoothed_levels)) * width
                    bar_height = level * height * 0.3
                    hue = (i / len(self.smoothed_levels)) * 360
                    color = self.hsv_to_rgb(hue, 0.7, 0.5)
                    cr.set_source_rgba(color[0], color[1], color[2], 0.3)
                    cr.rectangle(x, height - bar_height, width/len(self.smoothed_levels) - 2, bar_height)
                    cr.fill()

    def draw_enhanced_frequency_bars(self, cr, width, height):
        bar_width = width / self.bars
        margin = bar_width * 0.15
        color_scheme = self.get_color_scheme_colors()
        for i, level in enumerate(self.smoothed_levels):
            bounce = math.sin(self.animation_phase * 3 + i * 0.2) * 0.1 if self.beat_detected else 0
            effective_level = min(1.0, level + bounce)
            bar_height = effective_level * height * 0.85
            base_color = color_scheme[i % len(color_scheme)]
            intensity_factor = 0.3 + 0.7 * effective_level
            x = i * bar_width + margin
            y = height - bar_height
            bar_gradient = cairo.LinearGradient(0, y, 0, height)
            bar_gradient.add_color_stop_rgb(0,
                base_color[0] * intensity_factor,
                base_color[1] * intensity_factor,
                base_color[2] * intensity_factor)
            bar_gradient.add_color_stop_rgb(1,
                base_color[0] * 0.3,
                base_color[1] * 0.3,
                base_color[2] * 0.3)
            cr.set_source(bar_gradient)
            cr.rectangle(x, y, bar_width - 2 * margin, bar_height)
            cr.fill()
            if self.glow_effect and effective_level > 0.6:
                glow_alpha = (effective_level - 0.6) * 0.5
                cr.set_source_rgba(base_color[0], base_color[1], base_color[2], glow_alpha)
                cr.rectangle(x - 3, y - 3, bar_width - 2 * margin + 6, bar_height + 6)
                cr.fill()
            if effective_level > 0.8:
                peak_y = y - 5
                cr.set_source_rgb(1, 1, 1)
                cr.rectangle(x + bar_width * 0.3, peak_y, bar_width * 0.4, 3)
                cr.fill()

    def draw_enhanced_waveform(self, cr, width, height):
        self.draw_waveform_layer(cr, width, height, 0.8, 2.0, 0.3)
        self.draw_waveform_layer(cr, width, height, 0.5, 4.0, 0.5)
        self.draw_waveform_layer(cr, width, height, 0.3, 8.0, 0.8)
        cr.set_source_rgba(0.3, 0.3, 0.3, 0.5)
        cr.set_line_width(1)
        cr.move_to(0, height / 2)
        cr.line_to(width, height / 2)
        cr.stroke()

    def draw_waveform_layer(self, cr, width, height, amplitude, frequency, alpha):
        color_scheme = self.get_color_scheme_colors()
        base_color = color_scheme[int(self.animation_phase * 10) % len(color_scheme)]
        cr.set_source_rgba(base_color[0], base_color[1], base_color[2], alpha)
        cr.set_line_width(2 + frequency * 0.5)
        cr.move_to(0, height / 2)
        for i in range(width):
            x = i
            time_offset = i / width * 4 * math.pi + self.animation_phase
            wave1 = math.sin(time_offset * frequency) * amplitude * 0.3
            wave2 = math.sin(time_offset * frequency * 0.7 + 1.5) * amplitude * 0.2
            audio_index = int((i / width) * len(self.smoothed_levels))
            if audio_index < len(self.smoothed_levels):
                wave2 += self.smoothed_levels[audio_index] * amplitude * 0.4
            y = height / 2 + (wave1 + wave2) * height * 0.25
            cr.line_to(x, y)
        cr.stroke()

    def draw_spectrum_visualization(self, cr, width, height):
        center_x = width / 2
        center_y = height / 2
        radius = min(width, height) * 0.4
        color_scheme = self.get_color_scheme_colors()
        for ring in range(5):
            ring_radius = radius * (ring + 1) / 6
            cr.set_line_width(1)
            for i, level in enumerate(self.smoothed_levels):
                angle = (i / len(self.smoothed_levels)) * 2 * math.pi
                bar_length = level * 20 + 5
                color_index = int((i / len(self.smoothed_levels)) * len(color_scheme))
                base_color = color_scheme[color_index]
                start_x = center_x + math.cos(angle) * ring_radius
                start_y = center_y + math.sin(angle) * ring_radius
                end_x = center_x + math.cos(angle) * (ring_radius + bar_length)
                end_y = center_y + math.sin(angle) * (ring_radius + bar_length)
                cr.set_source_rgb(base_color[0], base_color[1], base_color[2])
                cr.move_to(start_x, start_y)
                cr.line_to(end_x, end_y)
                cr.stroke()

    def draw_particle_visualization(self, cr, width, height):
        center_x = width / 2
        center_y = height / 2
        self.generate_particles(center_x, center_y, width, height)
        for system in self.particle_systems:
            cr.set_source_rgb(*system['color'])
            for particle in system['particles']:
                alpha = max(0, 1.0 - (particle['age'] / particle['lifetime']))
                cr.set_source_rgba(system['color'][0], system['color'][1],
                                 system['color'][2], alpha * 0.8)
                size = particle['size'] * (1 + alpha * 0.5)
                cr.arc(particle['x'], particle['y'], size, 0, 2 * math.pi)
                cr.fill()

    def draw_abstract_visualization(self, cr, width, height):
        num_curves = 8
        color_scheme = self.get_color_scheme_colors()
        for curve in range(num_curves):
            color = color_scheme[curve % len(color_scheme)]
            cr.set_source_rgb(color[0], color[1], color[2])
            cr.set_line_width(2)
            cr.move_to(0, height / 2)
            for x in range(0, width, 5):
                audio_index = int((x / width) * len(self.smoothed_levels))
                if audio_index < len(self.smoothed_levels):
                    level = self.smoothed_levels[audio_index]
                    wave_y = math.sin(x * 0.02 + self.animation_phase * 2 + curve) * 30
                    audio_y = level * 60 * math.sin(x * 0.01 + curve * 0.5)
                    y = height / 2 + wave_y + audio_y
                    cr.line_to(x, y)
            cr.stroke()

    def get_color_scheme_colors(self):
        if self.color_scheme == 0:
            return [(0.2, 0.6, 1.0), (0.1, 0.8, 0.9), (0.3, 0.9, 0.7),
                   (0.8, 0.9, 0.3), (1.0, 0.7, 0.2), (1.0, 0.4, 0.2)]
        elif self.color_scheme == 1:
            return [(1.0, 0.2, 0.0), (1.0, 0.4, 0.0), (1.0, 0.6, 0.0),
                   (1.0, 0.8, 0.2), (1.0, 1.0, 0.4)]
        elif self.color_scheme == 2:
            return [(0.0, 0.2, 0.8), (0.0, 0.4, 0.9), (0.0, 0.6, 1.0),
                   (0.2, 0.8, 0.9), (0.4, 0.9, 0.8)]
        elif self.color_scheme == 3:
            return [(1.0, 0.0, 1.0), (0.0, 1.0, 1.0), (1.0, 1.0, 0.0),
                   (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        else:
            return [(0.8, 0.6, 0.9), (0.6, 0.8, 0.9), (0.6, 0.9, 0.6),
                   (0.9, 0.8, 0.6), (0.9, 0.6, 0.8)]

    def generate_particles(self, center_x, center_y, width, height):
        for system in self.particle_systems:
            avg_level = sum(self.smoothed_levels) / len(self.smoothed_levels)
            birth_rate = int(system['birth_rate'] * (1 + avg_level))
            for _ in range(birth_rate):
                if len(system['particles']) < system['max_particles']:
                    angle = random.uniform(0, 2 * math.pi)
                    distance = random.uniform(20, 80) * (1 + avg_level)
                    particle = {
                        'x': center_x + math.cos(angle) * distance,
                        'y': center_y + math.sin(angle) * distance,
                        'vx': math.cos(angle) * random.uniform(-2, 2),
                        'vy': math.sin(angle) * random.uniform(-2, 2),
                        'age': 0,
                        'lifetime': system['lifetime'],
                        'size': random.uniform(2, 6)
                    }
                    system['particles'].append(particle)
            for particle in system['particles'][:]:
                particle['x'] += particle['vx']
                particle['y'] += particle['vy']
                particle['age'] += 0.016
                if particle['age'] > particle['lifetime']:
                    system['particles'].remove(particle)

    def draw_circular_visualization(self, cr, width, height):
        center_x = width / 2
        center_y = height / 2
        max_radius = min(width, height) * 0.45
        color_scheme = self.get_color_scheme_colors()
        for ring in range(3):
            ring_radius = max_radius * (ring + 1) / 4
            for i, level in enumerate(self.smoothed_levels):
                angle = (i / len(self.smoothed_levels)) * 2 * math.pi
                pulse = math.sin(self.animation_phase * 2 + i * 0.1) * 0.1 if self.beat_detected else 0
                radius = ring_radius * (0.3 + level * 0.7 + pulse)
                color_index = int((i / len(self.smoothed_levels)) * len(color_scheme))
                base_color = color_scheme[color_index]
                intensity = 0.3 + 0.7 * level
                cr.set_source_rgb(base_color[0] * intensity,
                                base_color[1] * intensity,
                                base_color[2] * intensity)
                cr.set_line_width(2 + ring)
                end_x = center_x + math.cos(angle) * radius
                end_y = center_y + math.sin(angle) * radius
                cr.move_to(center_x, center_y)
                cr.line_to(end_x, end_y)
                cr.stroke()
                if level > 0.7:
                    cr.set_source_rgb(base_color[0], base_color[1], base_color[2])
                    cr.arc(end_x, end_y, 4 + ring * 2, 0, 2 * math.pi)
                    cr.fill()
        center_radius = 10 + (5 if self.beat_detected else 0)
        cr.set_source_rgb(0.8, 0.8, 0.8)
        cr.arc(center_x, center_y, center_radius, 0, 2 * math.pi)
        cr.fill()

    def draw_beat_indicator(self, cr, width, height):
        pulse_size = 20 + (10 if self.beat_detected else 0)
        alpha = 0.8 + (0.2 if self.beat_detected else 0)
        cr.set_source_rgba(1.0, 1.0, 1.0, alpha)
        cr.set_line_width(3)
        cr.rectangle(5, 5, pulse_size, pulse_size)
        cr.stroke()
        if self.beat_detected:
            cr.set_source_rgba(0.3, 1.0, 0.3, 0.6)
            cr.rectangle(8, 8, pulse_size - 6, pulse_size - 6)
            cr.fill()
        cr.set_source_rgb(0.9, 0.9, 0.9)
        cr.select_font_face("Arial", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10)
        cr.move_to(pulse_size + 10, 18)
        cr.show_text("♪ BEAT")

    def draw_enhanced_status_overlay(self, cr, width, height):
        status_height = 70
        y_offset = height - status_height - 5
        gradient = cairo.LinearGradient(0, y_offset, 0, height)
        gradient.add_color_stop_rgb(0, 0, 0, 0.8)
        gradient.add_color_stop_rgb(1, 0, 0, 0.6)
        cr.set_source(gradient)
        cr.rectangle(10, y_offset, width - 20, status_height)
        cr.fill()
        cr.set_source_rgba(0.3, 0.3, 0.3, 0.8)
        cr.set_line_width(1)
        cr.rectangle(10, y_offset, width - 20, status_height)
        cr.stroke()
        cr.set_source_rgb(0.9, 0.9, 0.9)
        cr.select_font_face("Arial", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        status_line1 = f"Visualizer: {self.get_visualizer_mode_name()}"
        cr.move_to(15, y_offset + 20)
        cr.show_text(status_line1)
        if self.projectm_available:
            status_line2 = f"projectM: {self.projectm_version}"
        else:
            status_line2 = "projectM: Not Available"
        cr.move_to(15, y_offset + 35)
        cr.show_text(status_line2)
        if self.available_presets:
            preset_text = f"Mode: {self.current_preset_index + 1}/{len(self.available_presets)}"
        else:
            preset_text = f"Mode: {self.get_visualizer_mode_name()}"
        cr.move_to(15, y_offset + 50)
        cr.show_text(preset_text)
        cr.set_font_size(9)
        cr.set_source_rgb(0.7, 0.7, 0.7)
        instructions = "L-Click: Next | R-Click: Previous | C: Cycle Colors"
        cr.move_to(width - 200, y_offset + 50)
        cr.show_text(instructions)

    def update_and_draw_particles(self, cr, width, height):
        for system in self.particle_systems:
            for particle in system['particles'][:]:
                particle['vy'] += 0.1
                particle['vx'] *= 0.99
                particle['vy'] *= 0.99
                particle['x'] += particle['vx']
                particle['y'] += particle['vy']
                particle['age'] += 0.016
                if (particle['age'] > particle['lifetime'] or
                    particle['x'] < -50 or particle['x'] > width + 50 or
                    particle['y'] < -50 or particle['y'] > height + 50):
                    system['particles'].remove(particle)
            if system['particles']:
                cr.set_source_rgba(system['color'][0], system['color'][1],
                                 system['color'][2], 0.3)
                cr.set_line_width(2)
                for particle in system['particles']:
                    trail_length = min(20, particle['age'] * 10)
                    if trail_length > 0:
                        cr.move_to(particle['x'], particle['y'])
                        cr.line_to(particle['x'] - particle['vx'] * trail_length * 0.1,
                                 particle['y'] - particle['vy'] * trail_length * 0.1)
                        cr.stroke()
                for particle in system['particles']:
                    alpha = max(0, 1.0 - (particle['age'] / particle['lifetime']))
                    size = particle['size'] * (1 + alpha * 0.3)
                    cr.set_source_rgba(system['color'][0], system['color'][1],
                                     system['color'][2], alpha * 0.8)
                    cr.arc(particle['x'], particle['y'], size, 0, 2 * math.pi)
                    cr.fill()
                    if alpha > 0.5:
                        cr.set_source_rgba(system['color'][0], system['color'][1],
                                         system['color'][2], alpha * 0.3)
                        cr.arc(particle['x'], particle['y'], size * 1.5, 0, 2 * math.pi)
                        cr.fill()

    def get_visualizer_mode_name(self):
        mode_names = [
            "Frequency Bars", "Enhanced Waveform", "Circular Spectrum",
            "Radial Analyzer", "Particle System", "Abstract Flow"
        ]
        return mode_names[self.visualization_mode % len(mode_names)]

    def get_color_scheme_name(self):
        scheme_names = ["Default", "Fire", "Ocean", "Neon", "Pastel"]
        return scheme_names[self.color_scheme % len(scheme_names)]

    def hsv_to_rgb(self, h, s, v):
        c = v * s
        x = c * (1 - abs((h * 6) % 2 - 1))
        m = v - c
        if h < 1/6:
            r, g, b = c, x, 0
        elif h < 2/6:
            r, g, b = x, c, 0
        elif h < 3/6:
            r, g, b = 0, c, x
        elif h < 4/6:
            r, g, b = 0, x, c
        elif h < 5/6:
            r, g, b = x, 0, c
        else:
            r, g, b = c, 0, x
        return r + m, g + m, b + m

    def load_presets(self):
        try:
            if os.path.exists(self.preset_dir):
                presets = [f for f in os.listdir(self.preset_dir)
                          if f.endswith('.milk') or f.endswith('.prjm')]
                self.available_presets.extend(presets)
                for preset in presets:
                    category = self.categorize_preset(preset)
                    if category not in self.preset_categories:
                        self.preset_categories[category] = []
                    self.preset_categories[category].append(preset)
            if self.custom_preset_dir and os.path.exists(self.custom_preset_dir):
                custom_presets = [f for f in os.listdir(self.custom_preset_dir)
                                if f.endswith('.milk') or f.endswith('.prjm')]
                self.available_presets.extend(custom_presets)
                for preset in custom_presets:
                    category = self.categorize_preset(preset)
                    if category not in self.preset_categories:
                        self.preset_categories[category] = []
                    self.preset_categories[category].append(preset)
            if not self.available_presets:
                logger.info("No projectM presets found, using embedded visualizations")
                self.create_embedded_presets()
            logger.info(f"Loaded {len(self.available_presets)} projectM presets in {len(self.preset_categories)} categories")
        except Exception:
            logger.exception("Error loading presets, falling back to embedded presets")
            self.create_embedded_presets()

    def create_embedded_presets(self):
        embedded_presets = [
            "Wave Form", "Spectrum Analyzer", "Circular Visual",
            "Particle Field", "Crystal Motion", "Flow State",
            "Energy Pulse", "Rhythm Grid", "Beat Wave",
            "Harmonic Flow", "Frequency Dance", "Audio Landscape"
        ]
        for preset in embedded_presets:
            self.available_presets.append(preset)
            category = self.categorize_preset(preset)
            if category not in self.preset_categories:
                self.preset_categories[category] = []
            self.preset_categories[category].append(preset)

    def categorize_preset(self, preset_name):
        # Defer to PresetManager to avoid duplication
        if hasattr(self, 'preset_manager') and self.preset_manager:
            return self.preset_manager.categorize_preset(preset_name)
        name_lower = preset_name.lower()
        if any(word in name_lower for word in ['wave', 'flow', 'fluid', 'liquid']):
            return "Flowing"
        elif any(word in name_lower for word in ['star', 'space', 'cosmic', 'galaxy']):
            return "Space"
        elif any(word in name_lower for word in ['fire', 'flame', 'heat', 'sun']):
            return "Fire"
        elif any(word in name_lower for word in ['crystal', 'gem', 'diamond', 'glass']):
            return "Crystal"
        elif any(word in name_lower for word in ['bubble', 'drop', 'water', 'ocean']):
            return "Liquid"
        elif any(word in name_lower for word in ['line', 'bar', 'spectrum', 'analyzer']):
            return "Analyzer"
        else:
            return "Abstract"

    def check_pulseaudio(self):
        if shutil.which("projectM-pulseaudio"):
            return True
        common_paths = [
            "/usr/bin/projectM-pulseaudio",
            "/usr/local/bin/projectM-pulseaudio",
            "/opt/projectM/bin/projectM-pulseaudio"
        ]
        for path in common_paths:
            if os.path.exists(path):
                return True
        return False

    def stop_projectm(self):
        try:
            if hasattr(self, 'projectm_process') and self.projectm_process:
                self.projectm_process.terminate()
                self.projectm_process.wait(timeout=5)
                self.projectm_process = None
                logger.info("projectM stopped successfully")
        except subprocess.TimeoutExpired:
            if self.projectm_process:
                self.projectm_process.kill()
                self.projectm_process.wait()
                self.projectm_process = None
                logger.warning("projectM forcefully terminated")
        except Exception as e:
            logger.error(f"Error stopping projectM: {e}")
            self.projectm_process = None

    def start_projectm(self):
        try:
            self.stop_projectm()
            if not self.projectm_available:
                logger.warning("projectM not available")
                self.status_text = "Embedded Visualization Mode"
                return False
            cmd_args = ["projectM-pulseaudio"]
            if os.path.exists(self.preset_dir):
                cmd_args.extend(["-p", self.preset_dir])
            else:
                logger.debug(f"Preset directory not found: {self.preset_dir}, checking alternative locations")
                alt_dirs = [
                    "~/.projectM/presets",
                    "/usr/local/share/projectM/presets",
                    "/usr/lib64/projectM/presets",
                    "/usr/lib/projectM/presets",
                    "/opt/projectM/presets"
                ]
                for alt_dir in alt_dirs:
                    alt_dir = os.path.expanduser(alt_dir)
                    if os.path.exists(alt_dir):
                        cmd_args.extend(["-p", alt_dir])
                        logger.info(f"Using alternative preset directory: {alt_dir}")
                        break
            cmd_args.extend([
                "--texture-size", str(self.projectm_config['texture_size']),
                "--mesh-x", str(self.projectm_config['mesh_x']),
                "--mesh-y", str(self.projectm_config['mesh_y']),
                "--fps", str(self.projectm_config['fps'])
            ])
            logger.info(f"Starting projectM with command: {' '.join(cmd_args)}")
            self.status_text = "projectM Running"
            env = os.environ.copy()
            env['G_MESSAGES_DEBUG'] = ''
            self.projectm_process = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                env=env,
                preexec_fn=os.setsid
            )
            self.start_monitoring()
            self._trigger_visualizer_redraw()
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error(f"Failed to start projectM: {e}")
            self.status_text = "projectM Failed to Start"
            self.projectm_available = False
            self._trigger_visualizer_redraw()
            return False

    def start_monitoring(self):

        def monitor():
            try:
                if self.projectm_process:
                    while self.projectm_process and self.projectm_process.poll() is None:
                        if self.projectm_process and self.projectm_process.stdout and self.projectm_process.stderr:
                            ready, _, _ = select.select(
                                [self.projectm_process.stdout, self.projectm_process.stderr],
                                [], [], 1.0
                            )
                            for stream in ready:
                                line = stream.readline()
                                if line:
                                    line = line.strip()
                                    if "GFileInfo created without standard::icon" in line:
                                        continue
                                    if "should not be reached" in line and "g_file_info_get_icon" in line:
                                        continue
                                    if stream == self.projectm_process.stderr:
                                        logger.warning(f"projectM stderr: {line}")
                                    else:
                                        logger.debug(f"projectM stdout: {line}")
                    if self.projectm_process:
                        stdout, stderr = self.projectm_process.communicate()
                        if stderr:
                            filtered_stderr = '\n'.join([
                                line for line in stderr.split('\n')
                                if "GFileInfo created without standard::icon" not in line
                                and not ("should not be reached" in line and "g_file_info_get_icon" in line)
                            ])
                            if filtered_stderr.strip():
                                logger.warning(f"projectM final stderr: {filtered_stderr}")
                        if stdout:
                            logger.debug(f"projectM final stdout: {stdout}")
            except Exception:
                logger.exception("projectM monitoring error")
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()

    def cleanup(self):
        try:
            if hasattr(self, 'projectm_process') and self.projectm_process:
                try:
                    self.projectm_process.terminate()
                    self.projectm_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.projectm_process.kill()
                    self.projectm_process.wait()
                except Exception as e:
                    logger.warning(f"Error stopping projectM process: {e}")
                finally:
                    self.projectm_process = None
            if hasattr(self, 'particle_systems'):
                for system in self.particle_systems:
                    system['particles'].clear()
                self.particle_systems.clear()
            if hasattr(self, 'audio_levels'):
                self.audio_levels.clear()
            if hasattr(self, 'smoothed_levels'):
                self.smoothed_levels.clear()
            # Clean up popover
            if hasattr(self, '_eq_popover') and self._eq_popover:
                try:
                    self._eq_popover.popdown()
                    self._eq_popover.unparent()
                    self._eq_popover = None
                except Exception as e:
                    logger.warning(f"Error cleaning up popover: {e}")
            gc.collect()
            logger.debug("Visualizer cleanup completed")
        except Exception as e:
            logger.error(f"Error during visualizer cleanup: {e}")

    def on_button_press(self, controller, n_press, x, y):
        if n_press == 1:
            if self.visualization_mode == 6:
                self.next_projectm_preset()
            else:
                self.next_visualization_mode()
        elif n_press == 2:
            if self.visualization_mode == 6:
                self.visualization_mode = 0
                self.status_text = f"Mode: {self.get_visualizer_mode_name()}"
                logger.info(f"Switched to visualization mode: {self.get_visualizer_mode_name()}")
            else:
                self.cycle_color_scheme()
        elif n_press == 3:
            if self.visualization_mode == 6:
                self.previous_projectm_preset()
            else:
                self.previous_visualization_mode()
        elif n_press == 4:
            self.toggle_glow_effect()
        elif n_press == 5:
            if self.visualization_mode == 6:
                self.next_preset_category()
            else:
                self.next_preset_category()

    def next_projectm_preset(self):
        if self.available_presets:
            self.current_preset_index = (self.current_preset_index + 1) % len(self.available_presets)
            preset_name = self.available_presets[self.current_preset_index]
            self.status_text = f"Preset: {preset_name[:25]}"
            logger.info(f"Next preset: {preset_name}")
            self._trigger_visualizer_redraw()

    def previous_projectm_preset(self):
        if self.available_presets:
            self.current_preset_index = (self.current_preset_index - 1) % len(self.available_presets)
            preset_name = self.available_presets[self.current_preset_index]
            self.status_text = f"Preset: {preset_name[:25]}"
            logger.info(f"Previous preset: {preset_name}")
            self._trigger_visualizer_redraw()

    def next_visualization_mode(self):
        self.visualization_mode = (self.visualization_mode + 1) % 7
        self.status_text = f"Mode: {self.get_visualizer_mode_name()}"
        logger.info(f"Switched to visualization mode: {self.get_visualizer_mode_name()}")
        self._trigger_visualizer_redraw()

    def previous_visualization_mode(self):
        self.visualization_mode = (self.visualization_mode - 1) % 6
        self.status_text = f"Mode: {self.get_visualizer_mode_name()}"
        logger.info(f"Switched to visualization mode: {self.get_visualizer_mode_name()}")
        self._trigger_visualizer_redraw()

    def cycle_color_scheme(self):
        self.color_scheme = (self.color_scheme + 1) % 5
        scheme_name = self.get_color_scheme_name()
        self.status_text = f"Colors: {scheme_name}"
        logger.info(f"Switched to color scheme: {scheme_name}")
        self._trigger_visualizer_redraw()

    def _trigger_visualizer_redraw(self):
        if hasattr(self, 'visualizer') and self.visualizer and hasattr(self.visualizer, 'queue_draw'):
            self.visualizer.queue_draw()
        elif hasattr(self, 'queue_draw'):
            self.queue_draw()

    def toggle_glow_effect(self):
        self.glow_effect = not self.glow_effect
        self.status_text = f"Glow: {'ON' if self.glow_effect else 'OFF'}"
        logger.info(f"Glow effect: {'enabled' if self.glow_effect else 'disabled'}")
        self._trigger_visualizer_redraw()

    def on_visualizer_scrolled(self, controller, dx, dy):
        if self.visualizer:
            current_time = time.time()
            # Add a small debounce time (100ms) to prevent rapid toggling
            if not hasattr(self, '_last_scroll_time'):
                self._last_scroll_time = 0
            if current_time - self._last_scroll_time > 0.1:  # 100ms debounce
                self._last_scroll_time = current_time
                if dy < 0:
                    self.on_button_press(None, 4, 0, 0)
                elif dy > 0:
                    self.on_button_press(None, 5, 0, 0)

    def next_preset_category(self):
        if self.preset_categories:
            self.visualization_mode = 6
            categories = list(self.preset_categories.keys())
            current_category = getattr(self, '_current_category_index', 0)
            self._current_category_index = (current_category + 1) % len(categories)
            category = categories[self._current_category_index]
            if category in self.preset_categories:
                presets_in_category = self.preset_categories[category]
                if presets_in_category:
                    self.current_preset_index = self.available_presets.index(presets_in_category[0])
                    self.status_text = f"Category: {category} - {presets_in_category[0][:20]}"
                    logger.info(f"Selected preset category: {category}")
                    logger.info(f"First preset in category: {presets_in_category[0]}")
            else:
                self.status_text = f"Category: {category}"
                logger.info(f"Selected preset category: {category}")
            self._trigger_visualizer_redraw()

    def switch_to_preset(self, index):
        if 0 <= index < len(self.available_presets):
            preset_name = self.available_presets[index]
            logger.info(f"Switching to preset: {preset_name}")
            category = self.categorize_preset(preset_name)
            mode_mapping = {
                "Analyzer": 0, "Flowing": 1, "Space": 2,
                "Fire": 3, "Crystal": 4, "Liquid": 5
            }
            if category in mode_mapping:
                self.visualization_mode = mode_mapping[category]
            else:
                self.visualization_mode = index % 6
            self.status_text = f"Preset: {preset_name[:20]}"
            self._trigger_visualizer_redraw()

    def cycle_visualization_mode(self):
        self.next_visualization_mode()

    def get_preset_list(self):
        return self.available_presets.copy()

    def get_preset_categories(self):
        return self.preset_categories.copy()

    def set_custom_preset_dir(self, directory):
        if os.path.exists(directory):
            self.custom_preset_dir = directory
            self.load_presets()
            logger.info(f"Loaded presets from custom directory: {directory}")
            return True
        return False

    def get_visualization_stats(self):
        return {
            'mode': self.get_visualizer_mode_name(),
            'color_scheme': self.get_color_scheme_name(),
            'projectm_available': self.projectm_available,
            'projectm_version': self.projectm_version,
            'available_presets': len(self.available_presets),
            'preset_categories': len(self.preset_categories),
            'glow_effect': self.glow_effect,
            'total_particles': sum(len(system['particles']) for system in self.particle_systems)
        }

    def set_intensity(self, intensity):
        self.intensity = max(0.1, min(2.0, intensity))
        self.status_text = f"Intensity: {self.intensity:.1f}"
        logger.info(f"Visualization intensity set to: {self.intensity}")
        self._trigger_visualizer_redraw()

    def toggle_particle_effects(self):
        for system in self.particle_systems:
            system['enabled'] = not system.get('enabled', True)
        enabled_count = sum(1 for system in self.particle_systems if system.get('enabled', True))
        self.status_text = f"Particles: {enabled_count}/{len(self.particle_systems)}"
        self._trigger_visualizer_redraw()

    def reset_visualization(self):
        self.visualization_mode = 0
        self.color_scheme = 0
        self.glow_effect = True
        self.intensity = 1.0
        self.animation_phase = 0
        for system in self.particle_systems:
            system['particles'].clear()
        for i in range(len(self.audio_levels)):
            self.audio_levels[i] = 0
            if i < len(self.smoothed_levels):
                self.smoothed_levels[i] = 0
        self.status_text = "Visualization Reset"
        self._trigger_visualizer_redraw()
        logger.info("Visualization reset to defaults")

    def create_opengl_visualizer(self):
        if not OPENGL_AVAILABLE:
            return None
        try:
            gl_area = Gtk.GLArea()
            gl_area.set_size_request(-1, 200)
            gl_area.set_hexpand(True)
            gl_area.set_vexpand(True)
            gl_area.set_required_version(3, 3)
            gl_area.set_has_depth_buffer(True)
            gl_area.set_has_stencil_buffer(True)
            gl_area.connect("realize", self.on_gl_realize)
            gl_area.connect("unrealize", self.on_gl_unrealize)
            gl_area.connect("render", self.on_gl_render)
            gl_area.connect("resize", self.on_gl_resize)
            scroll_controller = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
            scroll_controller.connect("scroll", self.on_gl_visualizer_scrolled)
            gl_area.add_controller(scroll_controller)
            return gl_area
        except Exception as e:
            logger.error(f"Failed to create OpenGL visualizer: {e}")
            return None

    def on_gl_realize(self, gl_area):
        try:
            gl_area.make_current()
            gl.glClearColor(0.0, 0.0, 0.0, 1.0)
            gl.glClearDepth(1.0)
            gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDepthFunc(gl.GL_LEQUAL)
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            self.texture_id = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            self.create_vertex_buffer()
            self.create_basic_shaders()
            logger.info("OpenGL context initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize OpenGL context: {e}")

    def on_gl_unrealize(self, gl_area):
        try:
            gl_area.make_current()
            if hasattr(self, 'texture_id'):
                gl.glDeleteTextures([self.texture_id])
            if hasattr(self, 'vertex_vbo_id'):
                gl.glDeleteBuffers(1, [self.vertex_vbo_id])
            if hasattr(self, 'vao_id'):
                gl.glDeleteVertexArrays(1, [self.vao_id])
            if hasattr(self, 'vertex_buffer_id'):
                gl.glDeleteBuffers(1, [self.vertex_buffer_id])
            if hasattr(self, 'color_buffer_id'):
                gl.glDeleteBuffers(1, [self.color_buffer_id])
            if hasattr(self, 'shader_program') and self.shader_program:
                gl.glDeleteProgram(self.shader_program)
            logger.info("OpenGL resources cleaned up")
        except Exception as e:
            logger.error(f"Failed to cleanup OpenGL resources: {e}")

    def on_gl_render(self, gl_area, context):
        try:
            gl_area.make_current()
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
            if self.visualization_mode == 6 and hasattr(self, 'projectm_available') and self.projectm_available:
                self.render_projectm_texture()
            else:
                self.render_fallback_visualization(gl)
            return True
        except Exception as e:
            logger.error(f"OpenGL rendering error: {e}")
            return False

    def on_gl_resize(self, gl_area, width, height):
        try:
            gl_area.make_current()
            gl.glViewport(0, 0, width, height)
            self.gl_width = width
            self.gl_height = height
        except Exception as e:
            logger.error(f"GL resize error: {e}")

    def on_gl_visualizer_scrolled(self, controller, dx, dy):
        if dy < 0:
            self.on_button_press(None, 4, 0, 0)
        elif dy > 0:
            self.on_button_press(None, 5, 0, 0)

    def create_vertex_buffer(self):
        try:
            vertices = [
                -1.0, -1.0,  0.0, 0.0,
                 1.0, -1.0,  1.0, 0.0,
                 1.0,  1.0,  1.0, 1.0,
                -1.0,  1.0,  0.0, 1.0
            ]
            vertex_array = np.array(vertices, dtype=np.float32)
            self.vertex_vbo_id = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vertex_vbo_id)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_array.nbytes, vertex_array, gl.GL_STATIC_DRAW)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            self.vertex_data = vertex_array
            self.vertex_count = len(vertices) // 4
        except Exception as e:
            logger.error(f"Failed to create vertex buffer: {e}")

    def create_basic_shaders(self):
        try:
            vertex_shader_source = """
            #version 330 core
            layout (location = 0) in vec2 aPosition;
            layout (location = 1) in vec4 aColor;
            out vec4 FragColor;
            void main() {
                gl_Position = vec4(aPosition, 0.0, 1.0);
                FragColor = aColor;
            }
            """
            fragment_shader_source = """
            #version 330 core
            in vec4 FragColor;
            out vec4 color;
            void main() {
                color = FragColor;
            }
            """
            vertex_shader = shaders.compileShader(vertex_shader_source, gl.GL_VERTEX_SHADER)
            fragment_shader = shaders.compileShader(fragment_shader_source, gl.GL_FRAGMENT_SHADER)
            self.shader_program = shaders.compileProgram(vertex_shader, fragment_shader)
            gl.glDeleteShader(vertex_shader)
            gl.glDeleteShader(fragment_shader)
            logger.info("Basic shader program created successfully")
        except Exception as e:
            logger.error(f"Failed to create basic shaders: {e}")
            self.shader_program = None

    def render_projectm_texture(self):
        try:
            if hasattr(self, 'texture_id') and hasattr(self, 'projectm_texture_data'):
                gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture_id)
                gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGB,
                              self.projectm_texture_data.shape[1],
                              self.projectm_texture_data.shape[0],
                              0, gl.GL_RGB, gl.GL_UNSIGNED_BYTE,
                              self.projectm_texture_data)
                self.render_textured_quad()
            else:
                self.render_fallback_visualization(gl)
        except Exception as e:
            logger.error(f"Failed to render projectM texture: {e}")
            self.render_fallback_visualization(gl)

    def cancel_pending_updates(self):
        pass

    def create_progress_section(self, parent):
        try:
            progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            progress_box.set_margin_top(6)
            progress_box.set_margin_bottom(6)
            time_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self.current_time_label = Gtk.Label(label="0:00")
            time_box.append(self.current_time_label)
            self.progress_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
            self.progress_scale.set_range(0, 100)
            self.progress_scale.set_draw_value(False)
            self.progress_scale.set_hexpand(True)
            self.progress_scale.connect("change-value", self.on_seek)
            self.duration_label = Gtk.Label(label="0:00")
            time_box.append(self.duration_label)
            progress_box.append(time_box)
            progress_box.append(self.progress_scale)
            parent.append(progress_box)
            self.is_seeking = False
        except Exception as e:
            logger.error(f"Failed to create progress section: {e}")
            raise

    def create_file_management_section(self, parent):
        try:
            file_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            file_box.set_margin_top(10)
            file_box.set_margin_bottom(10)
            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            button_box.set_homogeneous(True)
            file_box.append(button_box)
            self.add_files_btn = Gtk.Button(label="Add Files")
            self.add_files_btn.connect("clicked", self.on_add_files_clicked)
            button_box.append(self.add_files_btn)
            self.add_folder_btn = Gtk.Button(label="Add Folder")
            self.add_folder_btn.connect("clicked", self.on_add_folder_clicked)
            button_box.append(self.add_folder_btn)
            self.clear_btn = Gtk.Button(label="Clear All")
            self.clear_btn.connect("clicked", self.on_clear_clicked)
            button_box.append(self.clear_btn)
            parent.append(file_box)
            return file_box
        except Exception as e:
            logger.error(f"Failed to create file management section: {e}")
            raise

    def create_equalizer_ui(self, parent):
        try:
            eq_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            eq_box.set_margin_top(5)
            eq_box.set_margin_bottom(5)
            eq_header = Gtk.Label()
            eq_header.set_markup("<b>Equalizer</b>")
            eq_header.set_halign(Gtk.Align.CENTER)
            eq_box.append(eq_header)
            eq_sliders_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            eq_sliders_box.set_halign(Gtk.Align.CENTER)
            self.eq_sliders = []
            for i, freq in enumerate(EQ_FREQUENCIES):
                slider_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                adjustment = Gtk.Adjustment(
                    value=0,
                    lower=EQ_BAND_RANGE[0],
                    upper=EQ_BAND_RANGE[1],
                    step_increment=0.5,
                    page_increment=1.0
                )

                def on_eq_changed(adj, band=i):
                    if hasattr(self, 'audio') and self.audio:
                        self.audio.set_eq_band(band, adj.get_value())
                adjustment.connect("value-changed", on_eq_changed)
                scale = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=adjustment)
                scale.set_draw_value(False)
                scale.set_inverted(True)
                scale.set_size_request(30, 100)
                if freq < 1000:
                    freq_label = Gtk.Label(label=f"{freq}Hz")
                else:
                    freq_label = Gtk.Label(label=f"{freq/1000:.1f}k")
                freq_label.set_halign(Gtk.Align.CENTER)

                slider_box.append(scale)
                slider_box.append(freq_label)
                eq_sliders_box.append(slider_box)
                self.eq_sliders.append(adjustment)
            presets_btn = Gtk.Button(label="Presets")
            presets_btn.connect("clicked", self.on_eq_presets_clicked)
            reset_btn = Gtk.Button(label="Reset")
            reset_btn.connect("clicked", self.on_eq_reset_clicked)
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
            btn_box.set_halign(Gtk.Align.CENTER)
            btn_box.append(presets_btn)
            btn_box.append(reset_btn)
            eq_box.append(eq_sliders_box)
            eq_box.append(btn_box)
            parent.append(eq_box)
            self.update_equalizer_ui()
            return eq_box
        except Exception as e:
            logger.error(f"Failed to create equalizer UI: {e}")
            raise

    def create_playlist_section(self, parent):
        try:
            playlist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            playlist_box.set_margin_top(5)
            playlist_box.set_margin_bottom(5)
            playlist_header = Gtk.Label()
            playlist_header.set_markup("<b>Playlist</b>")
            playlist_header.set_halign(Gtk.Align.CENTER)
            playlist_box.append(playlist_header)
            self.playlist_view = Gtk.ColumnView()
            self.playlist_view.set_model(self.selection)
            factory_num = Gtk.SignalListItemFactory()
            factory_num.connect("setup", self._setup_track_number_column)
            factory_num.connect("bind", self._bind_track_number_column)
            col_num = Gtk.ColumnViewColumn(title="#", factory=factory_num)
            col_num.set_resizable(False)
            col_num.set_fixed_width(40)
            self.playlist_view.append_column(col_num)
            factory_title = Gtk.SignalListItemFactory()
            factory_title.connect("setup", self._setup_title_column)
            factory_title.connect("bind", self._bind_title_column)
            col_title = Gtk.ColumnViewColumn(title="Title", factory=factory_title)
            col_title.set_resizable(True)
            col_title.set_expand(True)
            self.playlist_view.append_column(col_title)
            factory_artist = Gtk.SignalListItemFactory()
            factory_artist.connect("setup", self._setup_artist_column)
            factory_artist.connect("bind", self._bind_artist_column)
            col_artist = Gtk.ColumnViewColumn(title="Artist", factory=factory_artist)
            col_artist.set_resizable(True)
            col_artist.set_expand(True)
            self.playlist_view.append_column(col_artist)
            factory_duration = Gtk.SignalListItemFactory()
            factory_duration.connect("setup", self._setup_duration_column)
            factory_duration.connect("bind", self._bind_duration_column)
            col_duration = Gtk.ColumnViewColumn(title="Duration", factory=factory_duration)
            col_duration.set_resizable(False)
            col_duration.set_fixed_width(80)
            self.playlist_view.append_column(col_duration)
            self.playlist_view.connect("activate", self.on_playlist_activate)
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_child(self.playlist_view)
            scrolled.set_vexpand(True)
            scrolled.set_min_content_height(200)
            playlist_box.append(scrolled)
            parent.append(playlist_box)
            return playlist_box
        except Exception as e:
            logger.error(f"Failed to create playlist section: {e}")
            raise

    def _setup_track_number_column(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_margin_start(5)
        list_item.set_child(label)

    def _bind_track_number_column(self, factory, list_item):
        label = list_item.get_child()
        position = list_item.get_position()
        label.set_text(str(position + 1))

    def _setup_title_column(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_margin_start(5)
        list_item.set_child(label)

    def _bind_title_column(self, factory, list_item):
        label = list_item.get_child()
        track = list_item.get_item()
        title = track.title if track.title != "Unknown Title" else track.get_display_name()
        label.set_text(title)

    def _setup_artist_column(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_margin_start(5)
        list_item.set_child(label)

    def _bind_artist_column(self, factory, list_item):
        label = list_item.get_child()
        track = list_item.get_item()
        artist = track.artist if track.artist != "Unknown Artist" else ""
        label.set_text(artist)

    def _setup_duration_column(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.END)
        label.set_margin_end(5)
        list_item.set_child(label)

    def _bind_duration_column(self, factory, list_item):
        label = list_item.get_child()
        track = list_item.get_item()
        if track.duration > 0:
            minutes = track.duration // 60
            seconds = track.duration % 60
            duration_text = f"{minutes}:{seconds:02d}"
        else:
            duration_text = "--:--"
        label.set_text(duration_text)

    def create_visualizer_section(self, parent):
        try:
            visualizer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            visualizer_box.set_margin_top(5)
            visualizer_box.set_margin_bottom(5)
            visualizer_box.set_hexpand(True)
            visualizer_box.set_vexpand(True)
            visualizer_header = Gtk.Label()
            visualizer_header.set_markup("<b>Visualizer</b>")
            visualizer_header.set_halign(Gtk.Align.CENTER)
            visualizer_box.append(visualizer_header)

            # Try OpenGL first, fallback to Cairo
            # Force Cairo fallback for debugging
            logger.info("Forcing Cairo fallback for debugging")
            visualizer = None
            # visualizer = self.create_opengl_visualizer()
            if visualizer is None:
                logger.info("OpenGL not available, using Cairo fallback")
                visualizer = Gtk.DrawingArea()
                visualizer.set_size_request(-1, 300)  # Increased height for better visibility
                visualizer.set_hexpand(True)
                visualizer.set_vexpand(True)
                visualizer.set_draw_func(self.on_draw, None)
                logger.info("Draw function connected")

                # Add event controllers for interaction
                click_controller = Gtk.GestureClick.new()
                click_controller.connect("pressed", self.on_button_press)
                visualizer.add_controller(click_controller)

                scroll_controller = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
                scroll_controller.connect("scroll", self.on_visualizer_scrolled)
                visualizer.add_controller(scroll_controller)

                # Store reference for debugging
                self.drawing_area = visualizer
                logger.info("Cairo drawing area created and configured")

            visualizer_box.append(visualizer)
            self.visualizer = visualizer

            # Add status label for diagnostics
            status_label = Gtk.Label()
            status_label.set_halign(Gtk.Align.CENTER)
            status_label.set_margin_top(5)
            status_label.set_text("Click: Next Mode | Scroll: Color | Right Click: Previous")
            visualizer_box.append(status_label)

            parent.append(visualizer_box)
            logger.info("Visualizer section created successfully")
        except Exception as e:
            logger.error(f"Failed to create visualizer section: {e}")
            raise

    def on_playlist_activate(self, column_view, position):
        try:
            self.selection.set_selected(position)
            self.play_selected()
        except Exception as e:
            logger.error(f"Error activating playlist item: {e}")
            self.show_status_message("Error playing track")

    def play_selected(self):
        try:
            selected_item = self.selection.get_selected_item()
            if selected_item:
                uri = GLib.filename_to_uri(selected_item.filename)
                self.audio.play_uri(uri)
                self.current_track = selected_item
                self.show_status_message(f"Playing: {selected_item.get_display_name()}")
            else:
                self.show_status_message("No track selected")
        except Exception as e:
            logger.error(f"Error playing selected track: {e}")
            self.show_status_message("Error playing track")

    def toggle_play_pause(self):
        try:
            if hasattr(self, 'audio') and self.audio:
                if self.audio.is_playing():
                    self.audio.pause()
                    self.play_button.set_icon_name("media-playback-start-symbolic")
                    self.show_status_message("Paused")
                else:
                    self.audio.play()
                    self.play_button.set_icon_name("media-playback-pause-symbolic")
                    self.show_status_message("Playing")
        except Exception as e:
            logger.error(f"Error toggling play/pause: {e}")
            self.show_status_message("Error toggling playback")

    def next_track(self):
        try:
            next_item = self.audio.get_next_track(self)
            if next_item:
                # Find the position of this item in the store
                for i in range(self.store.get_n_items()):
                    if self.store.get_item(i) == next_item:
                        self.selection.set_selected(i)
                        break
                uri = GLib.filename_to_uri(next_item.filename)
                self.audio.play_uri(uri)
                self.current_track = next_item
                self.play_button.set_icon_name("media-playback-pause-symbolic")
                self.show_status_message(f"Playing: {next_item.get_display_name()}")
            else:
                self.show_status_message("No next track")
        except Exception as e:
            logger.error(f"Error playing next track: {e}")
            self.show_status_message("Error playing next track")

    def previous_track(self):
        try:
            # Get current position
            current_pos = self.selection.get_selected()
            if current_pos is not None and current_pos > 0:
                prev_pos = current_pos - 1
                self.selection.set_selected(prev_pos)
                prev_item = self.selection.get_selected_item()
                if prev_item:
                    uri = GLib.filename_to_uri(prev_item.filename)
                    self.audio.play_uri(uri)
                    self.current_track = prev_item
                    self.play_button.set_icon_name("media-playback-pause-symbolic")
                    self.show_status_message(f"Playing: {prev_item.get_display_name()}")
            else:
                self.show_status_message("No previous track")
        except Exception as e:
            logger.error(f"Error playing previous track: {e}")
            self.show_status_message("Error playing previous track")

    def play_random_track(self):
        try:
            import random
            total_tracks = self.store.get_n_items()
            if total_tracks == 0:
                self.show_status_message("No tracks in playlist")
                return

            # Select a random track
            random_pos = random.randint(0, total_tracks - 1)
            self.selection.set_selected(random_pos)
            random_item = self.selection.get_selected_item()

            if random_item:
                uri = GLib.filename_to_uri(random_item.filename)
                self.audio.play_uri(uri)
                self.current_track = random_item
                self.play_button.set_icon_name("media-playback-pause-symbolic")
                self.show_status_message(f"Playing random: {random_item.get_display_name()}")
        except Exception as e:
            logger.error(f"Error playing random track: {e}")
            self.show_status_message("Error playing random track")

    def on_add_files_clicked(self, button):
        try:
            dialog = Gtk.FileChooserNative(
                title="Add Audio Files",
                action=Gtk.FileChooserAction.OPEN,
                accept_label="Add",
                cancel_label="Cancel"
            )
            audio_filter = Gtk.FileFilter()
            audio_filter.set_name("Audio Files")
            for ext in SUPPORTED_AUDIO_FORMATS:
                audio_filter.add_pattern(f"*{ext}")
            dialog.add_filter(audio_filter)
            dialog.set_select_multiple(True)
            dialog.connect("response", self.on_file_dialog_response)
            dialog.present()
        except Exception as e:
            logger.error(f"Error in on_add_files_clicked: {e}")
            self.show_status_message("Error adding files")

    def on_add_folder_clicked(self, button):
        try:
            dialog = Gtk.FileChooserNative(
                title="Select Folder with Audio Files",
                action=Gtk.FileChooserAction.SELECT_FOLDER,
                accept_label="Add",
                cancel_label="Cancel"
            )
            dialog.connect("response", self.on_folder_dialog_response)
            dialog.present()
        except Exception as e:
            logger.error(f"Error in on_add_folder_clicked: {e}")
            self.show_status_message("Error adding folder")

    def on_clear_clicked(self, button):
        try:
            dialog = Gtk.MessageDialog(
                transient_for=self.win,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Clear Playlist",
                secondary_text="Are you sure you want to remove all tracks from the playlist?"
            )
            dialog.connect("response", lambda d, r: self._handle_clear_response(r))
            dialog.present()
        except Exception as e:
            logger.error(f"Error in on_clear_clicked: {e}")
            self.show_status_message("Error clearing playlist")

    def on_eq_presets_clicked(self, button):
        try:
            if hasattr(self, '_eq_popover') and self._eq_popover:
                try:
                    self._eq_popover.popdown()
                except Exception:
                    pass
                try:
                    self._eq_popover.unparent()
                except Exception:
                    pass
                self._eq_popover = None
            dialog = Gtk.Dialog(
                title="Equalizer Presets",
                transient_for=self.props.active_window,
                modal=True,
                use_header_bar=True
            )
            dialog.set_default_size(350, 500)
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scrolled.set_vexpand(True)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            box.set_margin_top(15)
            box.set_margin_bottom(15)
            box.set_margin_start(15)
            box.set_margin_end(15)
            box.set_vexpand(True)
            presets = {
                "Flat": [0] * 10,
                "Rock": [5, 4, 3, 1, 0, -1, -2, -3, -3, -4],
                "Pop": [-1, 2, 4, 4, 2, 0, -1, -1, -1, -1],
                "Jazz": [3, 2, 1, 2, -2, -1, 1, 2, 3, 4],
                "Classical": [0, 0, 0, 0, 0, 0, -1, -1, -1, -2],
                "Electronic": [4, 3, 1, 0, 1, 2, 4, 5, 6, 7],
                "Bass Boost": [7, 6, 5, 4, 3, 2, 0, 0, 0, 0],
                "Vocal": [-4, -2, 0, 2, 4, 4, 3, 2, 1, 0],
                "Dance": [6, 5, 3, 1, 0, 1, 3, 5, 6, 7],
                "Acoustic": [0, 1, 2, 2, 2, 0, -1, -2, -2, -2],
                "Metal": [7, 6, 5, 4, 3, 2, 1, 0, 0, 0],
                "Hip Hop": [5, 4, 3, 1, 0, -1, 1, 3, 4, 5],
                "Blues": [4, 3, 2, 1, 0, 1, 2, 3, 4, 4],
                "Country": [2, 1, 0, 1, 2, 3, 3, 2, 1, 0],
                "Reggae": [0, 0, 0, -2, 0, 2, 4, 4, 2, 0],
                "Live": [-2, -1, 0, 1, 2, 2, 1, 0, -1, -2],
                "Podcast": [-3, -2, -1, 0, 2, 3, 3, 2, 1, 0],
                "Loudness": [5, 4, 3, 2, 1, 0, -1, -2, -3, -4],
                "Club": [6, 5, 4, 2, 0, 1, 3, 5, 6, 7],
                "Party": [7, 6, 5, 3, 1, 0, 2, 4, 6, 8],
                "Soft Rock": [3, 2, 1, 0, -1, 0, 1, 2, 3, 3],
                "Hard Rock": [8, 7, 6, 4, 2, 0, -1, -2, -3, -4],
                "Punk": [8, 7, 6, 5, 4, 3, 2, 1, 0, 0],
                "Alternative": [4, 3, 2, 1, 0, 1, 2, 3, 4, 4]
            }
            for preset_name, values in presets.items():
                btn = Gtk.Button(label=preset_name)
                btn.set_size_request(-1, 35)
                btn.set_margin_top(2)
                btn.set_margin_bottom(2)
                btn.add_css_class("preset-button")
                btn.connect("clicked", self.on_eq_preset_selected, values, dialog)
                box.append(btn)
            scrolled.set_child(box)
            content_area = dialog.get_child()
            content_area.append(scrolled)
            # Add Cancel button using GTK4 recommended method
            cancel_btn = dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
            cancel_btn.set_use_underline(True)
            dialog.connect("response", lambda d, r: d.close())
            dialog.present()
        except Exception as e:
            logger.error(f"Error in on_eq_presets_clicked: {e}")
            self.show_status_message("Error loading presets")

    def on_eq_preset_selected(self, button, values, dialog=None):
        try:
            if not hasattr(self, 'audio') or not self.audio:
                return
            for i, value in enumerate(values):
                if i < len(self.eq_sliders):
                    self.eq_sliders[i].set_value(value)
                    self.audio.set_eq_band(i, value)
            if dialog:
                dialog.close()
            self.show_status_message("Equalizer preset applied")
        except Exception as e:
            logger.error(f"Error applying equalizer preset: {e}")
            self.show_status_message("Error applying preset")

    def on_eq_reset_clicked(self, button):
        try:
            for slider in self.eq_sliders:
                slider.set_value(0.0)
            if hasattr(self, 'audio') and self.audio:
                self.audio.reset_equalizer()
            self.show_status_message("Equalizer reset")
        except Exception as e:
            logger.error(f"Error resetting equalizer: {e}")
            self.show_status_message("Error resetting equalizer")

    def update_equalizer_ui(self):
        try:
            if not hasattr(self, 'audio') or not self.audio or not hasattr(self, 'eq_sliders'):
                return
            current_settings = self.audio.get_eq_bands()
            for i, value in enumerate(current_settings):
                if i < len(self.eq_sliders):
                    self.eq_sliders[i].set_value(value)
        except Exception as e:
            logger.error(f"Error updating equalizer UI: {e}")

    def on_file_dialog_response(self, dialog, response):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                files = dialog.get_files()
                self._add_files_to_playlist(files)
        except Exception as e:
            logger.error(f"Error in on_file_dialog_response: {e}")
            self.show_status_message("Error adding files")
        finally:
            dialog.close()

    def on_folder_dialog_response(self, dialog, response):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                folder = dialog.get_file()
                if folder:
                    self._add_folder_to_playlist(folder)
        except Exception as e:
            logger.error(f"Error in on_folder_dialog_response: {e}")
            self.show_status_message("Error adding folder")
        finally:
            dialog.close()

    def _add_files_to_playlist(self, files):
        if not files:
            return
        added = 0
        for file in files:
            file_path = file.get_path()
            if file_path.lower().endswith(tuple(SUPPORTED_AUDIO_FORMATS)):
                try:
                    track = Track(file_path)
                    self.store.append(track)
                    added += 1
                except Exception as e:
                    logger.error(f"Error adding file {file_path}: {e}")
        if added > 0:
            self.show_status_message(f"Added {added} file(s) to playlist")
            self._save_playlist()

    def _add_folder_to_playlist(self, folder):

        def add_files_recursively(directory):
            added = 0
            try:
                for entry in directory.enumerate_children('standard::*',
                                                       Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                                                       None):
                    file_type = entry.get_file_type()
                    if file_type == Gio.FileType.DIRECTORY:

                        subdir = directory.get_child(entry.get_name())
                        added += add_files_recursively(subdir)
                    else:
                        name = entry.get_name().lower()
                        if any(name.endswith(ext) for ext in SUPPORTED_AUDIO_FORMATS):
                            try:
                                file_path = directory.get_child(entry.get_name()).get_path()
                                track = Track(file_path)
                                GLib.idle_add(self.store.append, track)
                                added += 1
                            except Exception as e:
                                logger.error(f"Error adding file {entry.get_name()}: {e}")
            except Exception as e:
                logger.error(f"Error reading directory {directory.get_path()}: {e}")
            return added
        added = add_files_recursively(folder)
        if added > 0:
            self.show_status_message(f"Added {added} file(s) from folder")
            GLib.idle_add(self._save_playlist)

    def _save_playlist(self):
        try:
            if not hasattr(self, 'store') or not hasattr(self, 'config'):
                return
            playlist = []
            for i in range(self.store.get_n_items()):
                track = self.store.get_item(i)
                if hasattr(track, 'filename'):
                    playlist.append(track.filename)
            self.config.save_playlist(playlist)
        except Exception as e:
            logger.error(f"Error saving playlist: {e}")

    def on_seek(self, scale, scroll_type, value):
        try:
            if not hasattr(self, 'audio') or not self.audio:
                return
            self.is_seeking = True
            self.audio.seek_percent(value)
        except Exception as e:
            logger.error(f"Error during seek: {e}")
        finally:
            GLib.timeout_add(100, self._reset_seeking_flag)

    def _reset_seeking_flag(self):
        self.is_seeking = False
        return False

    def update_progress(self):
        try:
            if not hasattr(self, 'audio') or not self.audio or self.is_seeking:
                return True
            position = self.audio.query_position()
            duration = self.audio.query_duration()
            if duration > 0:
                percent = (position / duration) * 100
                self.progress_scale.set_value(percent)
                self.current_time_label.set_label(self._format_time(position))
                self.duration_label.set_label(self._format_time(duration))
        except Exception as e:
            logger.error(f"Error updating progress: {e}")
        return True

    def update_playlist_highlight(self):
        try:
            if hasattr(self, 'current_track') and self.current_track and hasattr(self, 'selection'):

                playlist = self.selection.get_model()
                if playlist:
                    for i, track in enumerate(playlist):
                        if track == self.current_track:

                            self.selection.set_selected(i)
                            break
        except Exception as e:
            logger.error(f"Error updating playlist highlight: {e}")
        return True

    def _format_time(self, nanoseconds):
        try:
            seconds = int(nanoseconds / Gst.SECOND)
            minutes = seconds // 60
            seconds = seconds % 60
            return f"{minutes}:{seconds:02d}"
        except Exception as e:
            logger.error(f"Error formatting time: {e}")
            return "0:00"

def main():
    app = Linamp()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())

