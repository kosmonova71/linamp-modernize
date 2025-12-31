#!/usr/bin/env python3
"""
Simplified audio player with device conflict resolution
"""
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

Gst.init(None)

class SimpleAudioPlayer:
    def __init__(self):
        self.player = None
        self.setup_audio()
    
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
                        self.player.set_property("audio-sink", audio_sink)
                        return
                except Exception as e:
                    print(f"Failed to create {sink_name}: {e}")
                    continue
            
            print("Warning: No working audio sink found")
            
        except Exception as e:
            print(f"Error setting up audio: {e}")

if __name__ == "__main__":
    print("Testing simplified audio setup...")
    player = SimpleAudioPlayer()
    if player.player:
        print("Audio setup successful!")
    else:
        print("Audio setup failed!")
