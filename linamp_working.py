#!/usr/bin/env python3
"""
Linamp Enhanced - Working version with audio device conflict resolution
"""
import gi
import os
import random
import sys

gi.require_version('Gtk', '4.0')
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gtk, Gst, GLib

Gst.init(None)

class Config:
    def __init__(self):
        self.config = {
            "volume": 0.8,
            "shuffle": False,
            "repeat": False,
            "equalizer_enabled": False,
            "equalizer_preset": "Flat",
            "equalizer_bands": [0.0] * 10
        }
        self.equalizer_presets = {
            "Flat": [0.0] * 10,
            "Rock": [5.0, 4.5, 3.5, 2.5, 0.0, -1.0, -2.5, -2.5, 0.0, 0.0],
            "Pop": [2.5, 2.0, 0.0, -2.0, -2.5, -1.5, 0.0, 2.0, 3.0, 3.5],
            "Jazz": [3.5, 2.5, 0.0, -2.0, -2.5, 0.0, 2.5, 4.0, 4.5, 4.0],
            "Classical": [4.5, 3.5, 0.0, -3.0, -3.5, -2.5, 0.0, 2.5, 4.0, 5.0]
        }
    
    def save_config(self):
        pass
    
    def save_playlist(self, playlist):
        pass

class Track:
    def __init__(self, filename, title="Unknown"):
        self.filename = filename
        self.title = title

class PlaylistStore:
    def __init__(self):
        self.tracks = []
    
    def get_item(self, index):
        if 0 <= index < len(self.tracks):
            return self.tracks[index]
        return None
    
    def __len__(self):
        return len(self.tracks)

class EnhancedWinampPlayer(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.example.LinampEnhanced")
        self.player = None
        self.current_track_index = -1
        self.playlist_store = PlaylistStore()
        self.config = Config()
        self.is_seeking = False
        self._position = 0
        self._duration = "00:00"
        self._duration_seconds = 0
        self._tracks = []
        self.current_uri = None
        self._working_audio_sink = None
        
    def do_activate(self):
        if not hasattr(self, 'window') or not self.window:
            self.build_ui()
        self.window.present()
    
    def build_ui(self):
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_default_size(800, 600)
        self.window.set_title("Linamp Enhanced - Audio Device Conflict Resolution")
        
        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_box.set_margin_top(10)
        main_box.set_margin_bottom(10)
        main_box.set_margin_start(10)
        main_box.set_margin_end(10)
        
        # Controls
        controls_box = Gtk.Box(spacing=5)
        
        self.play_btn = Gtk.Button(label="Play")
        self.pause_btn = Gtk.Button(label="Pause")
        self.stop_btn = Gtk.Button(label="Stop")
        self.prev_btn = Gtk.Button(label="Previous")
        self.next_btn = Gtk.Button(label="Next")
        
        self.play_btn.connect("clicked", self.on_play)
        self.pause_btn.connect("clicked", self.on_pause)
        self.stop_btn.connect("clicked", self.on_stop)
        self.prev_btn.connect("clicked", self.on_prev_track)
        self.next_btn.connect("clicked", self.on_next_track)
        
        for btn in [self.play_btn, self.pause_btn, self.stop_btn, self.prev_btn, self.next_btn]:
            controls_box.append(btn)
        
        # Status
        self.status_label = Gtk.Label(label="Ready")
        self.title_label = Gtk.Label(label="No track loaded")
        
        # Volume
        volume_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        volume_label = Gtk.Label(label="Volume:")
        self.volume_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        self.volume_scale.set_range(0, 100)
        self.volume_scale.set_value(80)
        self.volume_scale.connect("value-changed", self.on_volume_changed)
        volume_box.append(volume_label)
        volume_box.append(self.volume_scale)
        
        # Progress
        self.progress_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL)
        self.progress_scale.set_range(0, 100)
        self.progress_scale.set_value(0)
        self.progress_scale.connect("value-changed", self.on_seek_value_change)
        
        # Time labels
        time_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.current_time_label = Gtk.Label(label="00:00")
        self.total_time_label = Gtk.Label(label="00:00")
        time_box.append(self.current_time_label)
        time_box.append(Gtk.Label(label=" / "))
        time_box.append(self.total_time_label)
        
        # File button
        self.add_files_btn = Gtk.Button(label="Add Files")
        self.add_files_btn.connect("clicked", self.on_add_files)
        
        # Add all to main box
        main_box.append(controls_box)
        main_box.append(self.status_label)
        main_box.append(self.title_label)
        main_box.append(volume_box)
        main_box.append(self.progress_scale)
        main_box.append(time_box)
        main_box.append(self.add_files_btn)
        
        self.window.set_child(main_box)
        
        # Initialize audio
        self.setup_audio()
        
        # Start progress update
        GLib.timeout_add_seconds(1, self.update_progress)
    
    def setup_audio(self):
        """Setup audio with device conflict resolution"""
        try:
            self.player = Gst.ElementFactory.make("playbin", "player")
            if not self.player:
                print("Failed to create playbin")
                return
            
            # Try different audio sinks with fallback to avoid device conflicts
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
                        if device:
                            audio_sink.set_property("device", device)
                        
                        # Test the sink
                        test_player = Gst.ElementFactory.make("playbin", "test")
                        test_player.set_property("audio-sink", audio_sink)
                        
                        print(f"Using audio sink: {sink_name}" + (f" with device: {device}" if device else ""))
                        self._working_audio_sink = (sink_name, device)
                        self.player.set_property("audio-sink", audio_sink)
                        break
                except Exception as e:
                    print(f"Failed to create {sink_name}: {e}")
                    continue
            
            # Connect signals
            bus = self.player.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self.on_bus_message)
            
            self.player.set_property("volume", self.config.config["volume"])
            
        except Exception as e:
            print(f"Error setting up audio: {e}")
            self.player = None
    
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
                self.stop()
                
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            print(f"GStreamer Warning: {warning}")
            
        elif message.type == Gst.MessageType.TAG:
            tags = message.parse_tag()
            title_found, title = tags.get_string("title")
            if title_found:
                self.title_label.set_text(title)
    
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
    
    def on_add_files(self, button):
        """Add audio files to playlist"""
        dialog = Gtk.FileChooserNative(
            title="Select Audio Files",
            action=Gtk.FileChooserAction.OPEN,
            transient_for=self.window,
            accept_label="_Open",
            cancel_label="_Cancel"
        )
        dialog.set_select_multiple(True)
        
        filter_audio = Gtk.FileFilter()
        filter_audio.set_name("Audio Files")
        filter_audio.add_mime_type("audio/*")
        dialog.add_filter(filter_audio)
        
        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            files = dialog.get_files()
            for file in files:
                filename = file.get_path()
                title = os.path.basename(filename).replace('.mp3', '').replace('.wav', '').replace('.ogg', '')
                track = Track(filename, title)
                self.playlist_store.tracks.append(track)
            
            self.status_label.set_text(f"Added {len(files)} files to playlist")
    
    def on_play(self, button):
        """Handle play button click"""
        if self.current_track_index >= 0:
            self.play_current_track()
        else:
            self.on_add_files(button)
    
    def on_pause(self, button):
        """Handle pause button click"""
        if self.player and self.player.get_state(0).state == Gst.State.PLAYING:
            self.player.set_state(Gst.State.PAUSED)
            self.status_label.set_text("Paused")
    
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
        if len(self.playlist_store.tracks) > 0:
            if self.config.config["shuffle"]:
                self.current_track_index = random.randint(0, len(self.playlist_store.tracks) - 1)
            else:
                self.current_track_index = max(0, self.current_track_index - 1)
            self.play_current_track()
    
    def on_next_track(self, button):
        """Go to next track"""
        if len(self.playlist_store.tracks) > 0:
            if self.config.config["shuffle"]:
                self.current_track_index = random.randint(0, len(self.playlist_store.tracks) - 1)
            else:
                self.current_track_index = (self.current_track_index + 1) % len(self.playlist_store.tracks)
            self.play_current_track()
    
    def play_current_track(self):
        """Play the current track"""
        if self.current_track_index < 0 or self.current_track_index >= len(self.playlist_store.tracks):
            return
        
        track = self.playlist_store.tracks[self.current_track_index]
        try:
            uri = GLib.filename_to_uri(track.filename)
            self.current_uri = uri
            
            if self.player:
                self.player.set_property("uri", uri)
                self.player.set_state(Gst.State.PLAYING)
                self.title_label.set_text(track.title)
                self.status_label.set_text(f"Playing: {track.title}")
        except Exception as e:
            self.status_label.set_text(f"Error playing: {e}")
    
    def on_volume_changed(self, scale):
        """Handle volume change"""
        volume = scale.get_value() / 100.0
        if self.player:
            self.player.set_property("volume", volume)
        self.config.config["volume"] = volume
    
    def on_seek_value_change(self, scale, scroll_type, value):
        """Handle seek value change"""
        if self.player and self.player.get_state(0).state == Gst.State.PLAYING:
            success, duration = self.player.query_duration(Gst.Format.TIME)
            if success and duration > 0:
                seek_ns = int((value / 100.0) * duration)
                self.player.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    seek_ns,
                )
    
    def update_progress(self):
        """Update progress bar and time display"""
        if not self.is_seeking and self.player:
            try:
                state = self.player.get_state(0).state
                if state == Gst.State.PLAYING or state == Gst.State.PAUSED:
                    success, position = self.player.query_position(Gst.Format.TIME)
                    success2, duration = self.player.query_duration(Gst.Format.TIME)
                    
                    if success and success2 and duration > 0:
                        self.current_time_label.set_text(self.format_time_ns(position))
                        self.total_time_label.set_text(self.format_time_ns(duration))
                        percent = (position / duration) * 100
                        if not self.is_seeking:
                            self.progress_scale.set_value(percent)
            except (GLib.Error, AttributeError, TypeError):
                pass
        return True
    
    def format_time_ns(self, nanoseconds):
        """Format time in nanoseconds to MM:SS"""
        seconds = nanoseconds // 1000000000
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:02d}"

def main():
    app = EnhancedWinampPlayer()
    app.run(sys.argv)

if __name__ == "__main__":
    main()
