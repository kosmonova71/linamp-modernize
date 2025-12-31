#!/usr/bin/env python3
# GTK4 Winamp-style Player with exclusive audio / gapless playback support

import gi
import sys
import os
import zipfile
try:
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gst', '1.0')
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import Gtk, Gst, Gdk, GdkPixbuf
except ImportError:
    print("Error: Could not import required modules")
    sys.exit(1)
Gst.init(None)

CLASSIC_WIDTH = 275
CLASSIC_HEIGHT = 116

# ---------------- AUDIO PLAYER ----------------
def make_player():
    playbin = Gst.ElementFactory.make('playbin')
    
    # Try different audio sinks with fallback to avoid device conflicts
    sinks_to_try = [
        ("pulsesink", None),      # PulseAudio (preferred for multi-app sharing)
        ("alsasink", "default"),  # ALSA default device
        ("autoaudiosink", None),  # Automatic detection
        ("alsasink", "hw:0,0"),  # Direct hardware access (last resort)
    ]
    
    for sink_name, device in sinks_to_try:
        try:
            sink = Gst.ElementFactory.make(sink_name)
            if sink:
                if device:
                    sink.set_property('device', device)
                # Test if sink can be created
                playbin.set_property('audio-sink', sink)
                print(f"Using audio sink: {sink_name}" + (f" with device: {device}" if device else ""))
                return playbin
        except Exception as e:
            print(f"Failed to create {sink_name}: {e}")
            continue
    
    # Fallback: let playbin choose automatically
    print("Using automatic audio sink selection")
    return playbin

# ---------------- SKIN CLASSES ----------------
class WinampSkin:
    def __init__(self, wsz_path):
        self.images = {}
        with zipfile.ZipFile(wsz_path) as z:
            for name in z.namelist():
                if name.lower().endswith('.bmp'):
                    data = z.read(name)
                    loader = GdkPixbuf.PixbufLoader.new_with_type('bmp')
                    loader.write(data)
                    loader.close()
                    self.images[name.lower()] = loader.get_pixbuf()

    def get(self, name):
        return self.images.get(name.lower())

# ---------------- MAIN APP ----------------
class WinampGTK(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='org.example.WinampGTK4')
        self.player = make_player()
        self.playlist = []
        self.current = -1
        self.skin = None
        self.is_playing = False

    def do_activate(self):
        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_title('Winamp GTK4')
        self.win.set_default_size(CLASSIC_WIDTH, CLASSIC_HEIGHT)
        self.win.set_resizable(False)
        self.win.set_decorated(False)

        # Load default skin
        if os.path.exists('default.wsz'):
            self.skin = WinampSkin('default.wsz')

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.win.set_child(root)

        # SkinRenderer placeholder - would need actual implementation
        # from canmore import get_textdoc  # placeholder for SkinRenderer
        # self.renderer = SkinRenderer(self.skin, self)
        # root.append(self.renderer)

        # Keyboard shortcuts
        controller = Gtk.EventControllerKey()
        controller.connect('key-pressed', self.on_key)
        self.win.add_controller(controller)

        # FFT Spectrum
        spectrum = Gst.ElementFactory.make('spectrum')
        spectrum.set_property('bands', 32)
        spectrum.set_property('threshold', -60)
        self.player.set_property('audio-filter', spectrum)

        bus = self.player.get_bus()
        bus.add_signal_watch()

        # Main window shown
        self.win.present()

    # ----------- Transport -----------
    def load_current(self):
        if self.current < 0 or self.current >= len(self.playlist):
            return
        uri = self.playlist[self.current]
        self.player.set_state(Gst.State.NULL)
        self.player.set_property('uri', uri)
        self.player.set_state(Gst.State.PLAYING)
        self.is_playing = True

    def play(self, *_):
        if self.current == -1 and self.playlist:
            self.current = 0
            self.load_current()
        else:
            self.player.set_state(Gst.State.PLAYING)
        self.is_playing = True

    def pause(self, *_):
        self.player.set_state(Gst.State.PAUSED)
        self.is_playing = False

    def stop(self, *_):
        self.player.set_state(Gst.State.NULL)
        self.is_playing = False

    def next(self, *_):
        if self.current + 1 < len(self.playlist):
            self.current += 1
            self.load_current()

    def prev(self, *_):
        if self.current > 0:
            self.current -= 1
            self.load_current()

    # ----------- Keyboard -----------
    def on_key(self, controller, keyval, keycode, state):
        key = Gdk.keyval_name(keyval)
        if key == 'x':
            self.play()
        elif key == 'c':
            self.pause()
        elif key == 'v':
            self.stop()
        elif key == 'b':
            self.next()
        elif key == 'z':
            self.prev()
        elif key == 'space':
            if self.is_playing:
                self.pause()
            else:
                self.play()
        return True


app = WinampGTK()
app.run(sys.argv)
